"""
One-time migration: backfill stolen_bases and caught_stealing in game_batting_logs.

The columns already exist (INTEGER DEFAULT 0) but are all zeros for historical
Retrosheet data (1920-2025). This script downloads Retrosheet batting.csv files
and updates each row by matching on (player_id, season, date, game_number).

For 2025-2026 MSF data: the forward fix is already in pull_live_stats.py.
Existing 2025-2026 rows will be updated on the next full refresh (weekly
reconciliation or manual --full-refresh). This script only handles Retrosheet
seasons.

Usage:
    # Dry run (show what would be updated, no DB writes)
    python backfill_sb_cs.py --dry-run

    # Full backfill (default DB path /data/baseball_stats_full.db)
    python backfill_sb_cs.py

    # Custom DB path
    python backfill_sb_cs.py --db /path/to/baseball_stats_full.db

    # Specific season range
    python backfill_sb_cs.py --start 2000 --end 2020

    # Single season
    python backfill_sb_cs.py --start 2024 --end 2024
"""

import argparse
import csv
import io
import sqlite3
import sys
import time
import zipfile
from urllib.request import urlopen

RETROSHEET_URL = "https://www.retrosheet.org/downloads/{year}/{year}csvs.zip"


def download_retrosheet_zip(season):
    """Download and return a ZipFile for the given Retrosheet season."""
    url = RETROSHEET_URL.format(year=season)
    resp = urlopen(url, timeout=60)
    return zipfile.ZipFile(io.BytesIO(resp.read()))


def read_csv_from_zip(zf, filename):
    """Read a CSV from a ZIP, return list of dicts (like DictReader)."""
    for name in zf.namelist():
        if name.lower().endswith(filename.lower()):
            with zf.open(name) as f:
                text = io.TextIOWrapper(f, encoding="utf-8", errors="replace")
                return list(csv.DictReader(text))
    return None


def safe_int(val, default=0):
    if val is None or val == "":
        return default
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return default


def format_date(raw):
    """Convert '20240401' to '2024-04-01'."""
    s = str(raw).strip()
    if len(s) == 8:
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s


def get_season_range_in_db(conn):
    """Get the min and max season in game_batting_logs."""
    row = conn.execute(
        "SELECT MIN(season), MAX(season) FROM game_batting_logs"
    ).fetchone()
    if row and row[0] is not None:
        return row[0], row[1]
    return None, None


def backfill_season(conn, season, dry_run=False):
    """
    Download Retrosheet batting.csv for one season, update SB/CS in game_batting_logs.
    Returns (rows_updated, rows_with_sb_or_cs, rows_not_found).
    """
    t0 = time.time()

    try:
        zf = download_retrosheet_zip(season)
    except Exception as e:
        print(f"  {season}: SKIP (download failed: {e})")
        return 0, 0, 0

    rows_data = read_csv_from_zip(zf, "batting.csv")
    if rows_data is None:
        print(f"  {season}: SKIP (no batting.csv in ZIP)")
        return 0, 0, 0

    cursor = conn.cursor()
    updated = 0
    has_sb_cs = 0
    not_found = 0

    for row in rows_data:
        # Filter to regular season, stat type = value (same as load_historical_gamelogs.py)
        if row.get("gametype", "regular") != "regular":
            continue
        if row.get("stattype", "value") != "value":
            continue

        pid = row.get("id", "").strip()
        if not pid:
            continue

        sb = safe_int(row.get("b_sb"))
        cs = safe_int(row.get("b_cs"))

        # Skip rows where both are 0 -- no update needed (already defaulted to 0)
        if sb == 0 and cs == 0:
            continue

        has_sb_cs += 1

        date_str = format_date(row.get("date", ""))
        game_number = safe_int(row.get("number", 0))

        if dry_run:
            # Check if the row exists
            existing = cursor.execute(
                """SELECT id, stolen_bases, caught_stealing
                   FROM game_batting_logs
                   WHERE player_id = ? AND season = ? AND date = ? AND game_number = ?""",
                (pid, season, date_str, game_number),
            ).fetchone()
            if existing:
                updated += 1
            else:
                not_found += 1
        else:
            result = cursor.execute(
                """UPDATE game_batting_logs
                   SET stolen_bases = ?, caught_stealing = ?
                   WHERE player_id = ? AND season = ? AND date = ? AND game_number = ?""",
                (sb, cs, pid, season, date_str, game_number),
            )
            if result.rowcount > 0:
                updated += 1
            else:
                not_found += 1

    if not dry_run:
        conn.commit()

    elapsed = time.time() - t0
    status = "[DRY RUN] " if dry_run else ""
    print(
        f"  {status}{season}: {updated:,} rows updated, "
        f"{has_sb_cs:,} CSV rows with SB/CS, "
        f"{not_found:,} not found in DB ({elapsed:.1f}s)"
    )

    return updated, has_sb_cs, not_found


def verify_results(conn):
    """
    Print verification: top 10 SB seasons from game logs vs season_batting_stats.
    This helps confirm the backfill is correct.
    """
    print("\n" + "=" * 70)
    print("VERIFICATION: Top 10 SB seasons (game_batting_logs vs season_batting_stats)")
    print("=" * 70)

    # From game logs (summed)
    print("\nFrom game_batting_logs (SUM of per-game stolen_bases):")
    print(f"  {'Player':<15} {'Season':<8} {'SB (logs)':<10}")
    print(f"  {'-'*15} {'-'*8} {'-'*10}")
    log_rows = conn.execute("""
        SELECT player_id, season, SUM(stolen_bases) as total_sb
        FROM game_batting_logs
        WHERE stolen_bases > 0
        GROUP BY player_id, season
        ORDER BY total_sb DESC
        LIMIT 10
    """).fetchall()
    for row in log_rows:
        print(f"  {row[0]:<15} {row[1]:<8} {row[2]:<10}")

    # From season stats
    print("\nFrom season_batting_stats (stored stolen_bases):")
    print(f"  {'Player':<15} {'Season':<8} {'SB (season)':<12}")
    print(f"  {'-'*15} {'-'*8} {'-'*12}")
    season_rows = conn.execute("""
        SELECT player_id, season, stolen_bases
        FROM season_batting_stats
        WHERE stolen_bases > 0
        ORDER BY stolen_bases DESC
        LIMIT 10
    """).fetchall()
    for row in season_rows:
        print(f"  {row[0]:<15} {row[1]:<8} {row[2]:<12}")

    # Discrepancy check: compare summed game logs vs season totals for top SB players
    print("\nDiscrepancy check (top 20 SB seasons where game log sum != season total):")
    print(f"  {'Player':<15} {'Season':<8} {'Log Sum':<10} {'Season':<10} {'Diff':<8}")
    print(f"  {'-'*15} {'-'*8} {'-'*10} {'-'*10} {'-'*8}")
    discrepancies = conn.execute("""
        SELECT g.player_id, g.season, g.log_sb, s.stolen_bases,
               g.log_sb - s.stolen_bases as diff
        FROM (
            SELECT player_id, season, SUM(stolen_bases) as log_sb
            FROM game_batting_logs
            GROUP BY player_id, season
        ) g
        JOIN season_batting_stats s ON g.player_id = s.player_id AND g.season = s.season
        WHERE g.log_sb != s.stolen_bases AND s.stolen_bases > 0
        ORDER BY ABS(g.log_sb - s.stolen_bases) DESC
        LIMIT 20
    """).fetchall()
    if discrepancies:
        for row in discrepancies:
            print(f"  {row[0]:<15} {row[1]:<8} {row[2]:<10} {row[3]:<10} {row[4]:<8}")
    else:
        print("  None -- all match!")

    # Summary stats
    total_nonzero = conn.execute(
        "SELECT COUNT(*) FROM game_batting_logs WHERE stolen_bases > 0 OR caught_stealing > 0"
    ).fetchone()[0]
    total_rows = conn.execute("SELECT COUNT(*) FROM game_batting_logs").fetchone()[0]
    print(f"\nTotal game_batting_logs rows: {total_rows:,}")
    print(f"Rows with SB > 0 or CS > 0: {total_nonzero:,}")


def main():
    parser = argparse.ArgumentParser(
        description="Backfill stolen_bases and caught_stealing in game_batting_logs from Retrosheet"
    )
    parser.add_argument("--db", default="/data/baseball_stats_full.db",
                        help="Path to SQLite database (default: /data/baseball_stats_full.db)")
    parser.add_argument("--start", type=int, default=None,
                        help="Start season (default: earliest in DB)")
    parser.add_argument("--end", type=int, default=None,
                        help="End season (default: latest Retrosheet season, i.e. 2025)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be updated without writing to DB")
    args = parser.parse_args()

    # Connect to DB
    print(f"Connecting to {args.db}...")
    try:
        conn = sqlite3.connect(args.db)
    except Exception as e:
        print(f"ERROR: Cannot open database: {e}")
        sys.exit(1)

    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")

    # Verify columns exist
    cols = {row[1] for row in conn.execute("PRAGMA table_info(game_batting_logs)").fetchall()}
    if "stolen_bases" not in cols or "caught_stealing" not in cols:
        print("ERROR: stolen_bases and/or caught_stealing columns not found in game_batting_logs.")
        print("Run the MSF pipeline first (pull_live_stats.py) to add the columns.")
        conn.close()
        sys.exit(1)

    # Determine season range
    db_min, db_max = get_season_range_in_db(conn)
    if db_min is None:
        print("ERROR: No data in game_batting_logs.")
        conn.close()
        sys.exit(1)

    # Retrosheet data available through 2025 (2026 is MSF-only)
    max_retrosheet = min(db_max, 2025) if db_max else 2025

    start = args.start if args.start is not None else db_min
    end = args.end if args.end is not None else max_retrosheet

    # Clamp to what's actually in the DB and available from Retrosheet
    if end > 2025:
        print(f"  Note: Clamping end year from {end} to 2025 (2026 data comes from MSF, not Retrosheet)")
        end = 2025

    if args.dry_run:
        print(f"\n*** DRY RUN MODE -- no database writes ***\n")

    print(f"Backfilling SB/CS for seasons {start}-{end}")
    print(f"DB has game logs from {db_min} to {db_max}")
    print()

    total_updated = 0
    total_sb_cs = 0
    total_not_found = 0
    t_start = time.time()

    for season in range(start, end + 1):
        updated, sb_cs, not_found = backfill_season(conn, season, dry_run=args.dry_run)
        total_updated += updated
        total_sb_cs += sb_cs
        total_not_found += not_found

    elapsed = time.time() - t_start
    print(f"\n{'DRY RUN ' if args.dry_run else ''}COMPLETE in {elapsed:.0f}s")
    print(f"  Seasons processed: {start}-{end}")
    print(f"  Total rows updated: {total_updated:,}")
    print(f"  CSV rows with SB/CS: {total_sb_cs:,}")
    print(f"  Not found in DB: {total_not_found:,}")

    if not args.dry_run:
        verify_results(conn)

    conn.close()


if __name__ == "__main__":
    main()
