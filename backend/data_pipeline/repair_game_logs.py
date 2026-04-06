"""Repair game logs for a specific season — re-pull from MSF with corrected player matching.

Only pulls game logs. Does not touch season stats, splits, streaks, or anything else.
"""

import argparse
import os
import sqlite3
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DB_PATH = os.getenv("DB_PATH", "/data/baseball_stats_full.db")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--season", required=True, help="e.g. 2025-regular")
    parser.add_argument("--db", default=DB_PATH)
    args = parser.parse_args()

    if not os.getenv("MSF_API_KEY"):
        print("ERROR: MSF_API_KEY not set")
        sys.exit(1)

    print(f"Repairing game logs for {args.season} in {args.db}")

    try:
        from pull_live_stats import pull_game_logs

        conn = sqlite3.connect(args.db)
        conn.execute("PRAGMA journal_mode=WAL")

        bat, pitch = pull_game_logs(conn, args.season, full_refresh=True)

        conn.close()
        print(f"Done: {bat} batting, {pitch} pitching game logs")

    except Exception as e:
        print(f"FATAL: {e}")
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
