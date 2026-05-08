"""Build the historical_streaks table — one-time precompute of every
on-base and hitting streak of length >= threshold across all game logs.

Uses SQLite gaps-and-islands (running sum of 'break' days) to detect
consecutive streaks per player. Stores the top streaks so the feed can
rank active ones historically ("longest since X", "Nth in 100 years").

Run via: POST /admin/build-historical-streaks
"""

import sqlite3
import sys
from datetime import datetime


# Thresholds: we only need streaks long enough to matter historically.
# 20+ hitting games or 25+ on-base games captures the interesting tail.
HITTING_MIN_LENGTH = 20
ON_BASE_MIN_LENGTH = 25


def build(db_path):
    """Compute and store all significant historical streaks."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode = WAL")

    print("Creating historical_streaks table…")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS historical_streaks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id TEXT NOT NULL,
            player_name TEXT NOT NULL,
            streak_type TEXT NOT NULL,
            length INTEGER NOT NULL,
            start_date TEXT NOT NULL,
            end_date TEXT NOT NULL,
            start_season INTEGER NOT NULL,
            end_season INTEGER NOT NULL
        )
    """)
    conn.execute("DELETE FROM historical_streaks")
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_hist_streaks_type_length
        ON historical_streaks(streak_type, length DESC)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_hist_streaks_type_end_date
        ON historical_streaks(streak_type, end_date)
    """)

    # Hitting streaks: consecutive games with hits > 0
    print(f"Computing hitting streaks (length >= {HITTING_MIN_LENGTH})…")
    start = datetime.now()
    conn.execute(f"""
        INSERT INTO historical_streaks
            (player_id, player_name, streak_type, length,
             start_date, end_date, start_season, end_season)
        SELECT
            s.player_id,
            COALESCE(p.name, s.player_id) AS player_name,
            'hitting' AS streak_type,
            COUNT(*) AS length,
            MIN(s.date) AS start_date,
            MAX(s.date) AS end_date,
            CAST(substr(MIN(s.date), 1, 4) AS INTEGER) AS start_season,
            CAST(substr(MAX(s.date), 1, 4) AS INTEGER) AS end_season
        FROM (
            SELECT
                player_id,
                date,
                hits,
                SUM(CASE WHEN hits = 0 THEN 1 ELSE 0 END)
                    OVER (PARTITION BY player_id ORDER BY date) AS grp
            FROM game_batting_logs
            WHERE plate_appearances > 0
        ) s
        LEFT JOIN players p ON p.player_id = s.player_id
        WHERE s.hits > 0
        GROUP BY s.player_id, s.grp
        HAVING COUNT(*) >= {HITTING_MIN_LENGTH}
    """)
    hit_count = conn.execute(
        "SELECT COUNT(*) FROM historical_streaks WHERE streak_type = 'hitting'"
    ).fetchone()[0]
    print(f"  Inserted {hit_count} hitting streaks in {(datetime.now()-start).total_seconds():.1f}s")

    # On-base streaks: consecutive games with H + BB + HBP > 0
    print(f"Computing on-base streaks (length >= {ON_BASE_MIN_LENGTH})…")
    start = datetime.now()
    conn.execute(f"""
        INSERT INTO historical_streaks
            (player_id, player_name, streak_type, length,
             start_date, end_date, start_season, end_season)
        SELECT
            s.player_id,
            COALESCE(p.name, s.player_id) AS player_name,
            'on_base' AS streak_type,
            COUNT(*) AS length,
            MIN(s.date) AS start_date,
            MAX(s.date) AS end_date,
            CAST(substr(MIN(s.date), 1, 4) AS INTEGER) AS start_season,
            CAST(substr(MAX(s.date), 1, 4) AS INTEGER) AS end_season
        FROM (
            SELECT
                player_id,
                date,
                hits,
                walks,
                COALESCE(hit_by_pitch, 0) AS hbp,
                SUM(CASE WHEN (hits + walks + COALESCE(hit_by_pitch, 0)) = 0 THEN 1 ELSE 0 END)
                    OVER (PARTITION BY player_id ORDER BY date) AS grp
            FROM game_batting_logs
            WHERE plate_appearances > 0 OR walks > 0 OR COALESCE(hit_by_pitch, 0) > 0
        ) s
        LEFT JOIN players p ON p.player_id = s.player_id
        WHERE (s.hits + s.walks + s.hbp) > 0
        GROUP BY s.player_id, s.grp
        HAVING COUNT(*) >= {ON_BASE_MIN_LENGTH}
    """)
    ob_count = conn.execute(
        "SELECT COUNT(*) FROM historical_streaks WHERE streak_type = 'on_base'"
    ).fetchone()[0]
    print(f"  Inserted {ob_count} on-base streaks in {(datetime.now()-start).total_seconds():.1f}s")

    conn.commit()
    conn.close()

    print(f"\nDone. Total: {hit_count + ob_count} streaks stored.")
    return {"hitting": hit_count, "on_base": ob_count}


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Rebuild historical_streaks table")
    p.add_argument("--db", default="/data/baseball_stats_full.db",
                   help="Path to SQLite DB (default: /data/baseball_stats_full.db)")
    # Accept positional too, for back-compat with prior callers.
    p.add_argument("db_positional", nargs="?", default=None)
    args = p.parse_args()
    db = args.db_positional or args.db
    build(db)
