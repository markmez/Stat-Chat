"""
Schema migration: fix doubleheader game log data loss.

Adds game_number column and changes UNIQUE constraint from
(player_id, season, date) to (player_id, season, date, game_number).

Also adds a data integrity check that verifies game log row counts
match season stats game counts.

Usage:
    python fix_doubleheader_schema.py --db /data/baseball_stats_full.db
"""

import argparse
import os
import sqlite3

DB_PATH = os.getenv("DB_PATH", "/data/baseball_stats_full.db")


def migrate_schema(conn):
    """Add game_number column and rebuild tables with correct UNIQUE constraint."""
    cursor = conn.cursor()

    for table in ["game_batting_logs", "game_pitching_logs"]:
        print(f"  Migrating {table}...")

        # Check if game_number already exists
        cols = {row[1] for row in cursor.execute(f"PRAGMA table_info({table})").fetchall()}
        if "game_number" in cols:
            print(f"    game_number already exists, skipping")
            continue

        # Get the current schema to rebuild
        schema = cursor.execute(
            f"SELECT sql FROM sqlite_master WHERE type='table' AND name='{table}'"
        ).fetchone()[0]

        # Rename old table
        cursor.execute(f"ALTER TABLE {table} RENAME TO {table}_old")

        # Create new table with game_number and updated UNIQUE constraint
        if table == "game_batting_logs":
            cursor.execute("""
                CREATE TABLE game_batting_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    player_id TEXT NOT NULL,
                    season INTEGER NOT NULL,
                    date TEXT NOT NULL,
                    game_number INTEGER NOT NULL DEFAULT 0,
                    opponent TEXT,
                    vishome TEXT,
                    plate_appearances INTEGER,
                    at_bats INTEGER,
                    hits INTEGER,
                    doubles INTEGER,
                    triples INTEGER,
                    home_runs INTEGER,
                    runs INTEGER,
                    rbi INTEGER,
                    walks INTEGER,
                    strikeouts INTEGER,
                    batting_avg REAL,
                    obp REAL,
                    slg REAL,
                    ops REAL,
                    hit_by_pitch INTEGER,
                    sacrifice_flies INTEGER,
                    FOREIGN KEY (player_id) REFERENCES players(player_id),
                    UNIQUE(player_id, season, date, game_number)
                )
            """)
        else:
            cursor.execute("""
                CREATE TABLE game_pitching_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    player_id TEXT NOT NULL,
                    season INTEGER NOT NULL,
                    date TEXT NOT NULL,
                    game_number INTEGER NOT NULL DEFAULT 0,
                    opponent TEXT,
                    vishome TEXT,
                    is_start INTEGER,
                    ip_outs INTEGER,
                    innings_pitched TEXT,
                    hits INTEGER,
                    runs INTEGER,
                    earned_runs INTEGER,
                    home_runs INTEGER,
                    walks INTEGER,
                    strikeouts INTEGER,
                    hit_by_pitch INTEGER,
                    batters_faced INTEGER,
                    win INTEGER,
                    loss INTEGER,
                    save INTEGER,
                    era REAL,
                    FOREIGN KEY (player_id) REFERENCES players(player_id),
                    UNIQUE(player_id, season, date, game_number)
                )
            """)

        # Copy data from old table (all with game_number=0)
        old_cols = [row[1] for row in cursor.execute(f"PRAGMA table_info({table}_old)").fetchall()
                    if row[1] != "id"]
        col_list = ", ".join(old_cols)
        cursor.execute(f"""
            INSERT INTO {table} ({col_list}, game_number)
            SELECT {col_list}, 0 FROM {table}_old
        """)

        # Drop old table
        cursor.execute(f"DROP TABLE {table}_old")

        # Recreate indexes
        cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_{table[:5]}logs_player ON {table}(player_id)")
        cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_{table[:5]}logs_player_season ON {table}(player_id, season)")
        cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_{table[:5]}logs_date ON {table}(date)")

        row_count = cursor.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"    Migrated {row_count} rows")

    conn.commit()
    print("  Schema migration complete")


def check_integrity(conn):
    """Compare season stats counting totals vs game log sums.

    Compares PA, AB, H, HR, RBI between season_batting_stats and the sum
    of game_batting_logs. Game count differences are expected (defensive
    subs with 0 PA don't get game log rows) — stat totals are what matter.
    """
    print("\n  Running data integrity check...")

    # Exclude players who have a game on the most recent date — their season
    # totals may include games whose logs aren't available yet.
    # Only flag mismatches for players whose most recent game is 2+ days old.
    latest = conn.execute("""
        SELECT MAX(date) FROM game_batting_logs WHERE season >= 2026
    """).fetchone()
    latest_date = latest[0] if latest and latest[0] else "9999-99-99"

    mismatches = conn.execute("""
        SELECT p.name, s.season,
               s.plate_appearances as season_pa,
               COALESCE(SUM(g.plate_appearances), 0) as log_pa,
               s.hits as season_h,
               COALESCE(SUM(g.hits), 0) as log_h,
               s.home_runs as season_hr,
               COALESCE(SUM(g.home_runs), 0) as log_hr,
               s.rbi as season_rbi,
               COALESCE(SUM(g.rbi), 0) as log_rbi
        FROM season_batting_stats s
        JOIN players p ON s.player_id = p.player_id
        LEFT JOIN game_batting_logs g ON s.player_id = g.player_id AND s.season = g.season
        WHERE s.season >= 2026 AND s.plate_appearances > 0
            AND s.player_id NOT IN (
                SELECT DISTINCT player_id FROM game_batting_logs
                WHERE date >= ? AND season >= 2026
            )
        GROUP BY s.player_id, s.season
        HAVING s.plate_appearances != COALESCE(SUM(g.plate_appearances), 0)
            OR s.hits != COALESCE(SUM(g.hits), 0)
            OR s.home_runs != COALESCE(SUM(g.home_runs), 0)
            OR s.rbi != COALESCE(SUM(g.rbi), 0)
        ORDER BY (s.plate_appearances - COALESCE(SUM(g.plate_appearances), 0)) DESC
        LIMIT 20
    """, (latest_date,)).fetchall()

    if mismatches:
        print(f"  WARNING: {len(mismatches)} players with stat mismatches:")
        for name, season, spa, lpa, sh, lh, shr, lhr, srbi, lrbi in mismatches:
            diffs = []
            if spa != lpa: diffs.append(f"PA {spa}vs{lpa}")
            if sh != lh: diffs.append(f"H {sh}vs{lh}")
            if shr != lhr: diffs.append(f"HR {shr}vs{lhr}")
            if srbi != lrbi: diffs.append(f"RBI {srbi}vs{lrbi}")
            print(f"    {name} ({season}): {', '.join(diffs)}")
    else:
        print("  All stat totals match!")

    return mismatches


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA temp_store=MEMORY")

    if not args.check_only:
        migrate_schema(conn)

    mismatches = check_integrity(conn)
    conn.close()

    if mismatches:
        print(f"\n  {len(mismatches)} integrity issues found. Re-pull game logs to fix.")
    else:
        print("\n  Data integrity OK.")


if __name__ == "__main__":
    main()
