"""Backfill team_game_results from Retrosheet gameinfo.csv (1898-2024).

Standalone script — does NOT call pull_stats.py main() and does NOT touch
batting/pitching/fielding tables. Reads each season's gameinfo.csv from the
existing cached Retrosheet ZIP (or downloads it via pull_stats helpers),
extracts per-game team-level results, and inserts two rows per game (one
from each team's perspective) into team_game_results. Computes cumulative
wins_after / losses_after per team in a window-function pass.

Usage:
    python3 backfill_team_game_results.py --start 1898 --end 2024 --db PATH

Or via admin endpoint per-season for chunked runs without HTTP timeouts.
"""

import argparse
import csv
import io
import os
import sqlite3
import sys
import zipfile

import requests

# Download URLs match pull_stats.py for consistency.
RETROSHEET_SEASON_URL = "https://www.retrosheet.org/downloads/{year}/{year}csvs.zip"
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache")


def _ensure_cache_dir():
    os.makedirs(CACHE_DIR, exist_ok=True)


def download_retrosheet_zip(season):
    """Download a Retrosheet season ZIP and return a ZipFile. Cached on disk
    after first download. Standalone copy of pull_stats.download_retrosheet_zip
    so this script doesn't depend on pandas being installed."""
    _ensure_cache_dir()
    cache_path = os.path.join(CACHE_DIR, f"retrosheet_{season}.zip")
    if not os.path.exists(cache_path):
        url = RETROSHEET_SEASON_URL.format(year=season)
        resp = requests.get(url, timeout=120)
        resp.raise_for_status()
        with open(cache_path, "wb") as f:
            f.write(resp.content)
    return zipfile.ZipFile(cache_path)


def _read_gameinfo_rows(zf):
    """Yield dicts for each row of {season}gameinfo.csv inside the ZIP."""
    for name in zf.namelist():
        if "gameinfo" in name.lower():
            with zf.open(name) as raw:
                # Wrap binary stream in TextIOWrapper for csv module
                text = io.TextIOWrapper(raw, encoding="utf-8", newline="")
                reader = csv.DictReader(text)
                for row in reader:
                    yield row
            return
    return


def ensure_table(conn):
    """Create team_game_results with all columns. Idempotent.

    Adds daynight + gametype columns if running against an older schema
    that pre-dates the Retrosheet backfill. Live MSF pipeline creates
    the table without those columns; this migration patches it.
    """
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS team_game_results (
            date TEXT NOT NULL,
            season INTEGER NOT NULL,
            game_number INTEGER NOT NULL DEFAULT 0,
            team TEXT NOT NULL,
            opponent TEXT NOT NULL,
            is_home INTEGER NOT NULL,
            team_runs INTEGER NOT NULL,
            opp_runs INTEGER NOT NULL,
            result TEXT NOT NULL,
            innings INTEGER DEFAULT 9,
            start_time_utc TEXT,
            attendance INTEGER,
            duration_min INTEGER,
            venue TEXT,
            weather TEXT,
            wins_after INTEGER,
            losses_after INTEGER,
            PRIMARY KEY (date, game_number, team)
        )
    """)
    cols = {row[1] for row in cursor.execute("PRAGMA table_info(team_game_results)").fetchall()}
    if "daynight" not in cols:
        cursor.execute("ALTER TABLE team_game_results ADD COLUMN daynight TEXT")
    if "gametype" not in cols:
        cursor.execute("ALTER TABLE team_game_results ADD COLUMN gametype TEXT")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_tgr_team_season ON team_game_results(team, season)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_tgr_date ON team_game_results(date)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_tgr_team_date ON team_game_results(team, date)")
    conn.commit()


def _fmt_date(yyyymmdd):
    """Convert int 20240320 → '2024-03-20'."""
    s = str(int(yyyymmdd))
    if len(s) != 8:
        return None
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}"


def _compose_weather(row):
    """Build a compact weather string from Retrosheet fields. Returns None
    when nothing useful is available (common for older seasons)."""
    parts = []
    sky = str(row.get("sky") or "").strip().lower()
    precip = str(row.get("precip") or "").strip().lower()
    temp = row.get("temp")
    wind_dir = str(row.get("winddir") or "").strip().lower()
    wind_speed = row.get("windspeed")

    # Sky takes priority for the headline word
    if sky and sky not in ("unknown", "dome", "nan"):
        parts.append(sky.capitalize())
    elif sky == "dome":
        parts.append("Indoor")

    if precip and precip not in ("none", "unknown", "nan") and sky != "dome":
        parts.append(precip.capitalize())

    try:
        t = int(temp)
        if 0 < t < 130:
            parts.append(f"{t}F")
    except (TypeError, ValueError):
        pass

    try:
        ws = int(wind_speed)
        if ws > 0 and wind_dir not in ("unknown", "nan", ""):
            parts.append(f"wind {ws} mph {wind_dir}")
        elif ws > 0:
            parts.append(f"wind {ws} mph")
    except (TypeError, ValueError):
        pass

    return ", ".join(parts) if parts else None


def backfill_season(conn, season, ballpark_lookup=None):
    """Backfill team_game_results for one season from Retrosheet.

    Returns (rows_inserted, games). Skips if the season's gameinfo.csv
    doesn't exist in the ZIP (some early years have format quirks).
    """
    cursor = conn.cursor()

    try:
        zf = download_retrosheet_zip(season)
    except Exception as e:
        raise RuntimeError(f"{season}: download failed ({e})")

    namelist = zf.namelist()
    rows = list(_read_gameinfo_rows(zf))
    if not rows:
        raise RuntimeError(
            f"{season}: no gameinfo rows extracted. ZIP contains: {namelist[:10]}"
        )

    # Wipe existing rows for this season — keeps backfill idempotent and
    # ensures stale data from a prior partial run is replaced.
    cursor.execute("DELETE FROM team_game_results WHERE season = ?", (season,))

    inserted = 0
    games = 0
    # Per-team game-number tracking for doubleheaders. Retrosheet's
    # `number` column is 0 for single, 1 / 2 for doubleheader halves.
    for row in rows:
        date = _fmt_date(row.get("date"))
        if not date:
            continue
        visteam = str(row.get("visteam") or "").strip()
        hometeam = str(row.get("hometeam") or "").strip()
        if not visteam or not hometeam:
            continue
        try:
            vruns = int(row.get("vruns") or 0)
            hruns = int(row.get("hruns") or 0)
        except (TypeError, ValueError):
            continue
        if vruns == 0 and hruns == 0:
            # Forfeit / no-play — skip (Retrosheet uses 0-0 for some)
            forfeit = str(row.get("forfeit") or "").strip()
            suspend = str(row.get("suspend") or "").strip()
            if forfeit or suspend:
                continue

        innings = 9
        try:
            innings = int(row.get("innings") or 9)
        except (TypeError, ValueError):
            pass

        # Game number: Retrosheet's `number` is 0/1/2; we store 0-indexed
        # so a single game is 0, doubleheader halves are 0 and 1.
        game_num = 0
        try:
            n = int(row.get("number") or 0)
            game_num = max(0, n - 1) if n > 0 else 0
        except (TypeError, ValueError):
            pass

        venue_code = str(row.get("site") or "").strip() or None
        venue = ballpark_lookup.get(venue_code) if (ballpark_lookup and venue_code) else venue_code

        try:
            attendance = int(row.get("attendance") or 0) or None
        except (TypeError, ValueError):
            attendance = None
        try:
            duration_min = int(row.get("timeofgame") or 0) or None
        except (TypeError, ValueError):
            duration_min = None

        daynight = str(row.get("daynight") or "").strip().lower() or None
        if daynight not in ("day", "night"):
            daynight = None

        gametype = str(row.get("gametype") or "").strip().lower() or "regular"
        weather = _compose_weather(row)

        # Two rows per game
        for team, opp, is_home, t_runs, o_runs in (
            (visteam, hometeam, 0, vruns, hruns),
            (hometeam, visteam, 1, hruns, vruns),
        ):
            if t_runs > o_runs:
                result = "W"
            elif t_runs < o_runs:
                result = "L"
            else:
                result = "T"
            cursor.execute("""
                INSERT OR REPLACE INTO team_game_results
                (date, season, game_number, team, opponent, is_home,
                 team_runs, opp_runs, result, innings,
                 start_time_utc, attendance, duration_min, venue, weather,
                 daynight, gametype)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (date, season, game_num, team, opp, is_home,
                  t_runs, o_runs, result, innings,
                  None,  # Retrosheet doesn't have UTC timestamp; daynight covers the use case
                  attendance, duration_min, venue, weather,
                  daynight, gametype))
            inserted += 1
        games += 1
    conn.commit()

    # Compute cumulative wins_after / losses_after for the season — same
    # window-function pass as the live MSF pipeline.
    cursor.execute("""
        WITH running AS (
            SELECT date, game_number, team,
                   SUM(CASE WHEN result = 'W' THEN 1 ELSE 0 END)
                       OVER (PARTITION BY team, season ORDER BY date, game_number
                             ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
                       AS wa,
                   SUM(CASE WHEN result = 'L' THEN 1 ELSE 0 END)
                       OVER (PARTITION BY team, season ORDER BY date, game_number
                             ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
                       AS la
            FROM team_game_results
            WHERE season = ?
        )
        UPDATE team_game_results
        SET wins_after = (SELECT wa FROM running r
                          WHERE r.date = team_game_results.date
                            AND r.game_number = team_game_results.game_number
                            AND r.team = team_game_results.team),
            losses_after = (SELECT la FROM running r
                            WHERE r.date = team_game_results.date
                              AND r.game_number = team_game_results.game_number
                              AND r.team = team_game_results.team)
        WHERE season = ?
    """, (season, season))
    conn.commit()

    return inserted, games


def load_ballpark_lookup(conn=None):
    """Load Retrosheet ballpark code → name mapping from biodata.zip.

    Returns dict {code: full_name} or empty dict if biodata isn't cached.
    """
    biodata_path = os.path.join(CACHE_DIR, "biodata.zip")
    if not os.path.exists(biodata_path):
        return {}
    try:
        zf = zipfile.ZipFile(biodata_path)
        for name in zf.namelist():
            if "ballpark" in name.lower():
                with zf.open(name) as raw:
                    text = io.TextIOWrapper(raw, encoding="utf-8", newline="")
                    reader = csv.DictReader(text)
                    fieldnames = reader.fieldnames or []
                    code_col = next(
                        (c for c in fieldnames if c.lower() in ("parkid", "park.id", "id")), None)
                    name_col = next(
                        (c for c in fieldnames if "name" in c.lower()), None)
                    if not (code_col and name_col):
                        return {}
                    return {row[code_col]: row[name_col] for row in reader if row.get(code_col)}
        return {}
    except Exception:
        return {}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--start", type=int, required=True)
    parser.add_argument("--end", type=int, required=True)
    args = parser.parse_args()

    conn = sqlite3.connect(args.db, timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    ensure_table(conn)
    ballpark_lookup = load_ballpark_lookup()
    print(f"Backfill team_game_results {args.start}-{args.end}")
    if ballpark_lookup:
        print(f"  Loaded {len(ballpark_lookup)} ballpark name mappings")
    grand_inserted = 0
    grand_games = 0
    for season in range(args.start, args.end + 1):
        inserted, games = backfill_season(conn, season, ballpark_lookup)
        if games:
            print(f"  {season}: {games} games, {inserted} rows")
        grand_inserted += inserted
        grand_games += games
    print(f"Done. {grand_games} games, {grand_inserted} rows total.")
    conn.close()


if __name__ == "__main__":
    main()
