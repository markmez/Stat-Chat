"""One-time (then incremental) historical_index shelf widening — Phase 2
of the trend/discovery engine (memory: project-trend-discovery-engine).

Adds:
  pitcher_first_{4..25}_starts  value=K value2=BB detail=full line
  window{4,6,10}_{hr,rbi,xbh,sb,hits}   best K-game window per player-season
  window{4,6,10}_hr_sb          best joint HR+SB window (value=HR, value2=SB)

Reads game logs (1920+) already in the DB. Idempotent: deletes only the
scan_types it owns, then rebuilds. Batched commits + busy_timeout so the
nightly pipeline never blocks behind it. Progress goes to trend_state
key 'shelf_build_progress' (poll via /admin/run-sql).
"""
import argparse
import json
import os
import sqlite3
import time
from datetime import datetime

DB_DEFAULT = os.environ.get("DB_PATH", os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "baseball_stats.db"))

PITCH_NS = list(range(4, 26))
WINDOW_KS = (4, 6, 10)
BAT_STATS = {"hr": 6, "rbi": 7, "xbh": None, "sb": 8, "hits": 3}  # col index below


def progress(conn, phase, done, note=""):
    conn.execute("INSERT OR REPLACE INTO trend_state (key, value, updated_at) VALUES (?,?,?)",
                 ("shelf_build_progress",
                  json.dumps({"phase": phase, "done": done, "note": note}),
                  datetime.utcnow().isoformat()))
    conn.commit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB_DEFAULT)
    args = ap.parse_args()
    conn = sqlite3.connect(args.db)
    conn.execute("PRAGMA busy_timeout = 60000")
    conn.execute("""CREATE TABLE IF NOT EXISTS trend_state (
        key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)""")
    t0 = time.time()

    # ---------- Part A: pitcher first-N starts ----------
    own = [f"pitcher_first_{n}_starts" for n in PITCH_NS]
    conn.execute(f"DELETE FROM historical_index WHERE scan_type IN ({','.join('?' * len(own))})", own)
    conn.commit()
    progress(conn, "pitching", 0, "deleting done, streaming starts")

    cur = conn.cursor()
    cur.execute("""SELECT g.player_id, p.name, g.season, g.strikeouts, g.walks,
                          g.earned_runs, g.ip_outs, g.hits
                   FROM game_pitching_logs g JOIN players p ON p.player_id = g.player_id
                   WHERE g.is_start = 1
                   ORDER BY g.player_id, g.season, g.date""")
    batch, cur_key, acc, n_done = [], None, None, 0
    ins = ("INSERT INTO historical_index"
           " (scan_type, player_id, player_name, team, season, value, value2, detail)"
           " VALUES (?,?,?,?,?,?,?,?)")

    def flush_starts(key, acc):
        pid, name, season = key
        k = bb = er = outs = h = 0
        for i, (gk, gbb, ger, gouts, gh) in enumerate(acc, 1):
            k += gk or 0; bb += gbb or 0; er += ger or 0
            outs += gouts or 0; h += gh or 0
            if i in PITCH_NS_SET:
                batch.append((f"pitcher_first_{i}_starts", pid, name, None, season,
                              k, bb, f"{i} GS, {k} K, {bb} BB, {er} ER, {outs} outs, {h} H"))

    PITCH_NS_SET = set(PITCH_NS)
    for pid, name, season, gk, gbb, ger, gouts, gh in cur:
        key = (pid, name, season)
        if key != cur_key:
            if cur_key is not None:
                flush_starts(cur_key, acc)
                n_done += 1
                if len(batch) >= 5000:
                    conn.executemany(ins, batch)
                    conn.commit()
                    batch.clear()
                    progress(conn, "pitching", n_done)
            cur_key, acc = key, []
        acc.append((gk, gbb, ger, gouts, gh))
    if cur_key is not None:
        flush_starts(cur_key, acc)
    if batch:
        conn.executemany(ins, batch)
        conn.commit()
        batch.clear()
    progress(conn, "pitching_done", n_done, f"{time.time() - t0:.0f}s")

    # ---------- Part B: batter best-K-game windows ----------
    own_b = [f"window{k}_{s}" for k in WINDOW_KS for s in list(BAT_STATS) + ["hr_sb"]]
    conn.execute(f"DELETE FROM historical_index WHERE scan_type IN ({','.join('?' * len(own_b))})", own_b)
    conn.commit()
    progress(conn, "batting", 0, "deleting done, streaming games")

    cur = conn.cursor()
    cur.execute("""SELECT g.player_id, p.name, g.season, g.hits, g.doubles, g.triples,
                          g.home_runs, g.rbi, g.stolen_bases
                   FROM game_batting_logs g JOIN players p ON p.player_id = g.player_id
                   ORDER BY g.player_id, g.season, g.date""")
    cur_key, games, n_done = None, [], 0

    def flush_batter(key, games):
        pid, name, season = key
        if len(games) < 4:
            return
        # per-game stat tuples: (hits, xbh, hr, rbi, sb)
        seq = [((h or 0), (d or 0) + (t or 0) + (hr or 0), (hr or 0), (r or 0), (s or 0))
               for h, d, t, hr, r, s in games]
        for K in WINDOW_KS:
            if len(seq) < K:
                continue
            sums = [sum(x) for x in zip(*seq[:K])]  # hits, xbh, hr, rbi, sb
            best = {"hits": sums[0], "xbh": sums[1], "hr": sums[2],
                    "rbi": sums[3], "sb": sums[4]}
            best_combo = (sums[2] + sums[4], sums[2], sums[4])
            for i in range(K, len(seq)):
                for j in range(5):
                    sums[j] += seq[i][j] - seq[i - K][j]
                if sums[0] > best["hits"]: best["hits"] = sums[0]
                if sums[1] > best["xbh"]: best["xbh"] = sums[1]
                if sums[2] > best["hr"]: best["hr"] = sums[2]
                if sums[3] > best["rbi"]: best["rbi"] = sums[3]
                if sums[4] > best["sb"]: best["sb"] = sums[4]
                if sums[2] + sums[4] > best_combo[0] and sums[2] >= 1 and sums[4] >= 1:
                    best_combo = (sums[2] + sums[4], sums[2], sums[4])
            for stat, v in best.items():
                if v > 0:
                    batch.append((f"window{K}_{stat}", pid, name, None, season,
                                  v, None, f"best {stat.upper()} in a {K}-game span"))
            if best_combo[0] > 1:
                batch.append((f"window{K}_hr_sb", pid, name, None, season,
                              best_combo[1], best_combo[2],
                              f"{best_combo[1]} HR + {best_combo[2]} SB in a {K}-game span"))

    for pid, name, season, h, d, t, hr, r, s in cur:
        key = (pid, name, season)
        if key != cur_key:
            if cur_key is not None:
                flush_batter(cur_key, games)
                n_done += 1
                if len(batch) >= 5000:
                    conn.executemany(ins, batch)
                    conn.commit()
                    batch.clear()
                    if n_done % 5000 < 2:
                        progress(conn, "batting", n_done)
            cur_key, games = key, []
        games.append((h, d, t, hr, r, s))
    if cur_key is not None:
        flush_batter(cur_key, games)
    if batch:
        conn.executemany(ins, batch)
        conn.commit()
    # Part C: oddity shelves (single SQL each)
    conn.execute("DELETE FROM historical_index WHERE scan_type = 'start_scoreless_no_k'")
    conn.execute("""INSERT INTO historical_index
        (scan_type, player_id, player_name, team, season, value, value2, detail)
        SELECT 'start_scoreless_no_k', g.player_id, p.name, NULL, g.season,
               MAX(g.ip_outs), NULL, 'longest scoreless 0-K start (outs)'
        FROM game_pitching_logs g JOIN players p ON p.player_id = g.player_id
        WHERE g.is_start = 1 AND g.runs = 0 AND g.strikeouts = 0 AND g.ip_outs >= 12
        GROUP BY g.player_id, g.season""")
    conn.commit()
    conn.execute("CREATE INDEX IF NOT EXISTS idx_hi_scan ON historical_index(scan_type, season)")
    conn.commit()
    progress(conn, "done", n_done, f"total {time.time() - t0:.0f}s")
    print(f"done in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
