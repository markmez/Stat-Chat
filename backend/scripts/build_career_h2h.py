"""Career head-to-head backfill from Retrosheet plays CSVs (1920-2025).

Downloads each season's plays zip from retrosheet.org (public, no auth),
streams the CSV inside the zip (never extracted to disk), aggregates
batter-vs-pitcher lines per season, and inserts into the existing
head_to_head table alongside the MSF-derived 2026 rows. build_matchup
already SUMs head_to_head without a season filter, so the card becomes
CAREER head-to-head the moment this lands — no reader change needed.

The plays CSVs carry pre-decoded outcome flags (pa/ab/single/double/
triple/hr/walk/k/hbp/sf per row), so no event-string parsing. Player IDs
are Retrosheet format, matching players.player_id.

Memory-safe for the 1.9GB box: one season aggregated at a time (~190K
plays -> ~60K pairs), executemany insert, zip deleted before the next
season. Idempotent: wipes seasons < 2026 that it owns, then rebuilds.
Progress: trend_state key 'career_h2h_progress'.

Data license: Retrosheet (retrosheet.org), free use with attribution —
already credited in schema_description.py.
"""
import csv
import io
import json
import os
import sqlite3
import sys
import time
import urllib.request
import zipfile
from datetime import datetime

DB = os.environ.get("DB_PATH", os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "baseball_stats.db"))
YEARS = range(1920, 2026)
URL = "https://www.retrosheet.org/downloads/plays/{y}plays.zip"


def progress(conn, note):
    conn.execute("INSERT OR REPLACE INTO trend_state VALUES (?,?,?)",
                 ("career_h2h_progress", json.dumps(note), datetime.utcnow().isoformat()))
    conn.commit()


def main():
    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA busy_timeout = 60000")
    conn.execute("""CREATE TABLE IF NOT EXISTS trend_state (
        key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_h2h_pair ON head_to_head(batter_id, pitcher_id)")
    conn.execute("DELETE FROM head_to_head WHERE season < 2026")
    conn.commit()

    t0 = time.time()
    total_pairs = 0
    done_years = 0
    for y in YEARS:
        tmp = f"/tmp/{y}plays.zip"
        try:
            urllib.request.urlretrieve(URL.format(y=y), tmp)
        except Exception as e:
            progress(conn, {"year": y, "skip": str(e)[:80], "pairs": total_pairs})
            continue
        agg = {}
        try:
            with zipfile.ZipFile(tmp) as z:
                name = next(n for n in z.namelist() if n.endswith(".csv"))
                with z.open(name) as fh:
                    r = csv.reader(io.TextIOWrapper(fh, errors="replace"))
                    header = next(r)
                    ix = {h: i for i, h in enumerate(header)}
                    B, P = ix["batter"], ix["pitcher"]
                    PA, AB = ix["pa"], ix["ab"]
                    S1, S2, S3, HR = ix["single"], ix["double"], ix["triple"], ix["hr"]
                    BB, K, HBP, SF = ix["walk"], ix["k"], ix["hbp"], ix["sf"]
                    IW = ix["iw"]
                    GT = ix.get("gametype")
                    for row in r:
                        try:
                            # Regular season ONLY — the files include
                            # postseason (Ruth's 1927 showed 62 HR: 60
                            # regular + 2 World Series) and All-Star games.
                            # Same rule as everywhere: no non-regular leaks.
                            if GT is not None and row[GT] != "regular":
                                continue
                            if row[PA] != "1":
                                continue
                            key = (row[B], row[P])
                            a = agg.get(key)
                            if a is None:
                                a = agg[key] = [0] * 10
                            a[0] += 1                      # PA
                            a[1] += row[AB] == "1"         # AB
                            s1 = row[S1] == "1"; s2 = row[S2] == "1"
                            s3 = row[S3] == "1"; hr = row[HR] == "1"
                            a[2] += s1 or s2 or s3 or hr   # H
                            a[3] += s2                     # 2B
                            a[4] += s3                     # 3B
                            a[5] += hr                     # HR
                            a[6] += (row[BB] == "1") or (row[IW] == "1")  # BB
                            a[7] += row[K] == "1"          # SO
                            a[8] += row[HBP] == "1"        # HBP
                            a[9] += row[SF] == "1"         # SF
                        except IndexError:
                            continue
        except Exception as e:
            progress(conn, {"year": y, "error": str(e)[:100], "pairs": total_pairs})
            try:
                os.remove(tmp)
            except OSError:
                pass
            continue
        try:
            os.remove(tmp)
        except OSError:
            pass

        rows = []
        for (b, p), a in agg.items():
            pa, ab, h, d2, t3, hr, bb, so, hbp, sf = a
            avg = h / ab if ab else None
            obp_den = ab + bb + hbp + sf
            obp = (h + bb + hbp) / obp_den if obp_den else None
            slg = (h + d2 + 2 * t3 + 3 * hr) / ab if ab else None
            ops = (obp + slg) if (obp is not None and slg is not None) else None
            rows.append((b, p, y, pa, ab, h, d2, t3, hr, None, bb, so, hbp, sf,
                         avg, obp, slg, ops))
        conn.executemany(
            "INSERT INTO head_to_head VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows)
        conn.commit()
        total_pairs += len(rows)
        done_years += 1
        if done_years % 5 == 0 or y >= 2024:
            progress(conn, {"year": y, "years_done": done_years,
                            "pairs": total_pairs, "secs": int(time.time() - t0)})
    progress(conn, {"done": True, "years_done": done_years,
                    "pairs": total_pairs, "secs": int(time.time() - t0)})
    print(f"career h2h: {total_pairs} pair-seasons from {done_years} years"
          f" in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
