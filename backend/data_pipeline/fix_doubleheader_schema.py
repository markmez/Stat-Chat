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
    """Compare season stats game counts vs game log row counts.
    Reports any discrepancies."""
    print("\n  Running data integrity check...")

    mismatches = conn.execute("""
        SELECT p.name, s.season, s.games as season_games,
               COUNT(g.id) as log_rows,
               s.games - COUNT(g.id) as missing
        FROM season_batting_stats s
        JOIN players p ON s.player_id = p.player_id
        LEFT JOIN game_batting_logs g ON s.player_id = g.player_id AND s.season = g.season
        WHERE s.season >= 2026
        GROUP BY s.player_id, s.season
        HAVING s.games != COUNT(g.id) AND s.games > 0
        ORDER BY missing DESC
        LIMIT 20
    """).fetchall()

    if mismatches:
        print(f"  WARNING: {len(mismatches)} players with game count mismatches:")
        for name, season, sg, lr, missing in mismatches:
            print(f"    {name} ({season}): season_stats={sg}G, game_logs={lr} rows, missing={missing}")
    else:
        print("  All game counts match!")

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
