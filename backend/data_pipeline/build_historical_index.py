"""
One-time: build a historical index table for fast notable events lookups.

Scans all game logs (1920-2025) and pre-computes:
  - Start-of-season batting streaks (hit/OBP/multi-hit/HR in each of first N games)
  - Pitching first-N-starts stats (K, BB, ER totals)
  - Team runs allowed through N games per season

Results stored in `historical_index` table for instant daily lookups.
Run once after loading historical game logs, then never again unless data changes.

Usage:
    python build_historical_index.py --db /data/baseball_stats_full.db
"""

import argparse
import os
import sqlite3
import time

DB_PATH = os.getenv("DB_PATH", "/data/baseball_stats_full.db")


def _player_name(conn, player_id):
    row = conn.execute("SELECT name FROM players WHERE player_id = ?", (player_id,)).fetchone()
    return row[0] if row else player_id


def create_index_table(conn):
    conn.execute("DROP TABLE IF EXISTS historical_index")
    conn.execute("""
        CREATE TABLE historical_index (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_type TEXT NOT NULL,
            player_id TEXT,
            player_name TEXT,
            team TEXT,
            season INTEGER NOT NULL,
            value INTEGER,
            value2 INTEGER,
            detail TEXT
        )
    """)
    conn.commit()
    # Indexes added after all inserts for performance


def add_indexes(conn):
    conn.execute("CREATE INDEX IF NOT EXISTS idx_hist_type ON historical_index(scan_type)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_hist_type_val ON historical_index(scan_type, value)")
    conn.commit()


def build_batting_season_start_streaks(conn):
    """For each player-season, compute how many games from the start of the
    season they maintained various streaks."""
    print("  Building batting start-of-season streaks...")

    streak_types = [
        ("start_hit_streak", lambda h, bb, hbp, hr: h > 0),
        ("start_onbase_streak", lambda h, bb, hbp, hr: (h + bb + hbp) > 0),
        ("start_multi_hit_streak", lambda h, bb, hbp, hr: h >= 2),
        ("start_hr_streak", lambda h, bb, hbp, hr: hr > 0),
    ]

    seasons = conn.execute("""
        SELECT DISTINCT season FROM game_batting_logs
        WHERE season >= 1920 ORDER BY season
    """).fetchall()

    total = 0
    for (szn,) in seasons:
        games = conn.execute("""
            SELECT player_id, hits, walks, COALESCE(hit_by_pitch, 0), home_runs
            FROM game_batting_logs
            WHERE season = ? AND at_bats > 0
            ORDER BY player_id, date ASC
        """, (szn,)).fetchall()

        # Track per-player streaks
        current_pid = None
        streaks = {}  # type_name -> current count
        broken = {}   # type_name -> bool

        for pid, h, bb, hbp, hr in games:
            if pid != current_pid:
                # Save previous player's streaks
                if current_pid:
                    for stype, _ in streak_types:
                        if streaks.get(stype, 0) >= 3:
                            name = _player_name(conn, current_pid)
                            conn.execute("""
                                INSERT INTO historical_index
                                (scan_type, player_id, player_name, season, value)
                                VALUES (?, ?, ?, ?, ?)
                            """, (stype, current_pid, name, szn, streaks[stype]))
                            total += 1

                current_pid = pid
                streaks = {s: 0 for s, _ in streak_types}
                broken = {s: False for s, _ in streak_types}

            for stype, check_fn in streak_types:
                if not broken[stype]:
                    if check_fn(h or 0, bb or 0, hbp or 0, hr or 0):
                        streaks[stype] += 1
                    else:
                        broken[stype] = True

        # Save last player
        if current_pid:
            for stype, _ in streak_types:
                if streaks.get(stype, 0) >= 3:
                    name = _player_name(conn, current_pid)
                    conn.execute("""
                        INSERT INTO historical_index
                        (scan_type, player_id, player_name, season, value)
                        VALUES (?, ?, ?, ?, ?)
                    """, (stype, current_pid, name, szn, streaks[stype]))
                    total += 1

        conn.commit()

    print(f"    {total} batting streak records")


def build_pitching_first_starts(conn):
    """For each pitcher-season, compute stats from first N starts."""
    print("  Building pitching first-starts index...")

    seasons = conn.execute("""
        SELECT DISTINCT season FROM game_pitching_logs
        WHERE season >= 1920 AND is_start = 1 ORDER BY season
    """).fetchall()

    total = 0
    for (szn,) in seasons:
        starts = conn.execute("""
            SELECT player_id, strikeouts, walks, earned_runs, ip_outs, hits
            FROM game_pitching_logs
            WHERE season = ? AND is_start = 1
            ORDER BY player_id, date ASC
        """, (szn,)).fetchall()

        current_pid = None
        start_num = 0
        cum_k = 0
        cum_bb = 0
        cum_er = 0
        cum_outs = 0
        cum_h = 0

        for pid, k, bb, er, outs, h in starts:
            if pid != current_pid:
                # Save previous pitcher's cumulative stats at various start counts
                if current_pid:
                    _save_pitcher_index(conn, current_pid, szn, start_num,
                                       cum_k, cum_bb, cum_er, cum_outs, cum_h)
                    total += 1
                current_pid = pid
                start_num = 0
                cum_k = cum_bb = cum_er = cum_outs = cum_h = 0

            start_num += 1
            cum_k += (k or 0)
            cum_bb += (bb or 0)
            cum_er += (er or 0)
            cum_outs += (outs or 0)
            cum_h += (h or 0)

            # Save snapshot at start 2 and start 3
            if start_num in (2, 3):
                _save_pitcher_index(conn, pid, szn, start_num,
                                    cum_k, cum_bb, cum_er, cum_outs, cum_h)
                total += 1

        if current_pid:
            _save_pitcher_index(conn, current_pid, szn, start_num,
                                cum_k, cum_bb, cum_er, cum_outs, cum_h)
            total += 1

        conn.commit()

    print(f"    {total} pitching records")


def _save_pitcher_index(conn, pid, season, starts, k, bb, er, outs, h):
    name = _player_name(conn, pid)
    # Store cumulative stats through N starts
    detail = f"{starts} GS, {k} K, {bb} BB, {er} ER, {outs} outs, {h} H"

    # Scoreless through N starts (5+ IP each)?
    # We'll store general stats and query at lookup time
    conn.execute("""
        INSERT INTO historical_index
        (scan_type, player_id, player_name, season, value, value2, detail)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (f"pitcher_first_{starts}_starts", pid, name, season, k, bb, detail))


def build_team_runs_allowed(conn):
    """For each team-season, compute total runs allowed through N games."""
    print("  Building team runs allowed index...")

    seasons = conn.execute("""
        SELECT DISTINCT season FROM game_pitching_logs
        WHERE season >= 1920 ORDER BY season
    """).fetchall()

    total = 0
    for (szn,) in seasons:
        # Sum earned runs by team for the season, game by game
        team_games = conn.execute("""
            SELECT team, date, SUM(earned_runs) as game_er
            FROM game_pitching_logs
            WHERE season = ?
            GROUP BY team, date
            ORDER BY team, date ASC
        """, (szn,)).fetchall()

        current_team = None
        game_num = 0
        cum_er = 0

        for team, dt, game_er in team_games:
            if team != current_team:
                current_team = team
                game_num = 0
                cum_er = 0

            game_num += 1
            cum_er += (game_er or 0)

            # Save snapshots at key game counts
            if game_num in (5, 6, 7, 8, 10, 15, 20):
                conn.execute("""
                    INSERT INTO historical_index
                    (scan_type, player_id, player_name, team, season, value, value2)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (f"team_er_through_{game_num}", None, None, team, szn, cum_er, game_num))
                total += 1

        conn.commit()

    print(f"    {total} team records")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=DB_PATH)
    args = parser.parse_args()

    print(f"Building historical index in {args.db}")
    conn = sqlite3.connect(args.db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-50000")  # 50MB cache

    t0 = time.time()
    create_index_table(conn)
    build_batting_season_start_streaks(conn)
    build_pitching_first_starts(conn)
    build_team_runs_allowed(conn)
    add_indexes(conn)

    count = conn.execute("SELECT COUNT(*) FROM historical_index").fetchone()[0]
    elapsed = time.time() - t0
    conn.close()

    print(f"\nDone! {count} records in {elapsed:.0f}s")


if __name__ == "__main__":
    main()
