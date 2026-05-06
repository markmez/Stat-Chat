"""
Live data pipeline: Pull current-season stats from MySportsFeeds into SQLite.

Supplements Retrosheet historical data with in-season stats (spring training + regular season).
Maps MySportsFeeds data into the same schema used by Retrosheet so all existing queries work.

Usage:
    python3 pull_live_stats.py                          # Pull current season (auto-detect)
    python3 pull_live_stats.py --season 2026-pre        # Pull spring training 2026
    python3 pull_live_stats.py --season 2026-regular    # Pull regular season 2026

Requires:
    MSF_API_KEY environment variable set to your MySportsFeeds API key.
"""

import argparse
import base64
import json
import math
import os
import sqlite3
import subprocess
import sys
import time
from datetime import date, datetime, timedelta

import requests
from dotenv import load_dotenv

load_dotenv()

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(os.path.dirname(__file__)), "baseball_stats.db"))
MSF_API_KEY = os.environ.get("MSF_API_KEY", "")
MSF_BASE = "https://api.mysportsfeeds.com/v2.1/pull/mlb"

# Map MSportsFeeds team abbreviations to Retrosheet codes
MSF_TO_RETRO_TEAM = {
    "NYY": "NYA", "NYM": "NYN", "LAD": "LAN", "LAA": "ANA",
    "CHC": "CHN", "CWS": "CHA", "SF": "SFN", "SD": "SDN",
    "STL": "SLN", "KC": "KCA", "TB": "TBA", "WSH": "WAS",
    "BOS": "BOS", "HOU": "HOU", "ATL": "ATL", "PHI": "PHI",
    "TEX": "TEX", "TOR": "TOR", "BAL": "BAL", "MIN": "MIN",
    "CLE": "CLE", "SEA": "SEA", "MIL": "MIL", "CIN": "CIN",
    "PIT": "PIT", "DET": "DET", "ARI": "ARI", "COL": "COL",
    "MIA": "MIA", "OAK": "OAK",
}

# UTC offsets for each MLB venue during DST (which spans the entire MLB
# regular season). Used to derive day/night from start_time_utc against the
# home venue's local clock. ARI does not observe DST and is year-round -7
# (matches PDT in summer). Both legacy and current Retrosheet codes are
# included (ANA/LAA for Anaheim, OAK/ATH for Athletics).
TEAM_TZ_OFFSET = {
    # Eastern (UTC-4 during DST)
    "NYA": -4, "NYN": -4, "BOS": -4, "BAL": -4, "WAS": -4,
    "PHI": -4, "ATL": -4, "MIA": -4, "TBA": -4, "TOR": -4,
    "PIT": -4, "CIN": -4, "DET": -4, "CLE": -4,
    # Central (UTC-5 during DST)
    "CHA": -5, "CHN": -5, "MIL": -5, "MIN": -5, "KCA": -5,
    "SLN": -5, "HOU": -5, "TEX": -5,
    # Mountain (UTC-6 during DST)
    "COL": -6,
    # Arizona — no DST, year-round UTC-7
    "ARI": -7,
    # Pacific (UTC-7 during DST)
    "LAN": -7, "ANA": -7, "LAA": -7, "SDN": -7, "SFN": -7,
    "SEA": -7, "OAK": -7, "ATH": -7,
}


def msf_get(endpoint, params=None):
    """Make an authenticated GET request to MySportsFeeds API."""
    if not MSF_API_KEY:
        raise RuntimeError("MSF_API_KEY environment variable not set")
    auth = base64.b64encode(f"{MSF_API_KEY}:MYSPORTSFEEDS".encode()).decode()
    headers = {"Authorization": f"Basic {auth}"}
    url = f"{MSF_BASE}/{endpoint}"
    if params is None:
        params = {}
    params.setdefault("force", "true")
    for attempt in range(3):
        resp = requests.get(url, headers=headers, params=params, timeout=180)
        if resp.status_code == 304:
            return None  # Data unchanged
        if resp.status_code == 429:
            wait = 5 * (attempt + 1)
            print(f"    Rate limited, waiting {wait}s...")
            time.sleep(wait)
            continue
        if resp.status_code >= 500:
            wait = 5 * (attempt + 1)
            print(f"    Server error {resp.status_code}, retrying in {wait}s...")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        if not resp.content:
            return None
        return resp.json()
    resp.raise_for_status()  # Final attempt failed
    return None


def retro_team(msf_abbrev):
    """Convert MySportsFeeds team abbreviation to Retrosheet code."""
    return MSF_TO_RETRO_TEAM.get(msf_abbrev, msf_abbrev)


def safe_int(val, default=0):
    if val is None:
        return default
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def safe_float(val, default=None):
    if val is None:
        return default
    try:
        f = float(val)
        return None if math.isnan(f) else f
    except (ValueError, TypeError):
        return default


def compute_rate_stats(h, ab, bb, hbp, sf, doubles, triples, hr, so):
    """Compute rate stats from counting stats."""
    pa = ab + bb + hbp + sf
    singles = h - doubles - triples - hr

    avg = h / ab if ab > 0 else None
    obp = (h + bb + hbp) / (ab + bb + hbp + sf) if (ab + bb + hbp + sf) > 0 else None
    slg = (singles + 2 * doubles + 3 * triples + 4 * hr) / ab if ab > 0 else None
    ops = (obp or 0) + (slg or 0) if obp is not None and slg is not None else None
    iso = slg - avg if slg is not None and avg is not None else None

    # BABIP = (H - HR) / (AB - SO - HR + SF)
    babip_denom = ab - so - hr + sf
    babip = (h - hr) / babip_denom if babip_denom > 0 else None

    return pa, avg, obp, slg, ops, iso, babip


def detect_season(season_str):
    """Extract numeric year from season string like '2026-pre' or '2026-regular'."""
    return int(season_str.split("-")[0])


def build_player_id(player_info):
    """Build a Retrosheet-style player ID from MySportsFeeds player data.

    Format: first 5 chars of last name + first char of first name + 3-digit serial (001).
    This won't perfectly match Retrosheet IDs, so we also try name-based lookup.
    """
    first = player_info.get("firstName", "")
    last = player_info.get("lastName", "")
    last_part = last.lower().replace("'", "").replace("-", "").replace(" ", "")[:5]
    first_part = first.lower()[:1]
    return f"{last_part}{first_part}001"


def _strip_accents(s):
    """Normalize accented characters: 'Acuña' → 'Acuna'."""
    import unicodedata
    return ''.join(
        c for c in unicodedata.normalize('NFD', s)
        if unicodedata.category(c) != 'Mn'
    )


def _is_temporally_plausible(cursor, player_id, season):
    """Check if matching to an existing player is plausible based on career timeline.
    Returns False if the player's last recorded activity was 15+ years ago."""
    cursor.execute("""
        SELECT MAX(season) FROM (
            SELECT MAX(season) as season FROM season_batting_stats WHERE player_id = ?
            UNION ALL
            SELECT MAX(season) FROM season_pitching_stats WHERE player_id = ?
        )
    """, (player_id, player_id))
    row = cursor.fetchone()
    if row and row[0]:
        return (season - row[0]) < 5
    # No stats yet — could be a newly created entry, allow match
    return True


def _get_stored_team(cursor, player_id):
    """Get the team currently stored in the players table for this ID."""
    cursor.execute("SELECT team FROM players WHERE player_id = ?", (player_id,))
    row = cursor.fetchone()
    return row[0] if row else None


def _redirect_alias(cursor, pid):
    """If `pid` is the alias id of a merged player, return the canonical id
    instead. Belt-and-suspenders so future pulls always land on canonical
    even if the matcher resolves to an alias id somehow."""
    if not pid:
        return pid
    cursor.execute(
        "SELECT canonical_id FROM player_id_aliases WHERE alias_id = ?", (pid,)
    )
    row = cursor.fetchone()
    return row[0] if row else pid


def find_or_create_player(cursor, player_info, team_abbrev, season):
    """Find existing player by name or create a new entry. Returns player_id.
    Uses accent-insensitive matching and temporal plausibility checks to avoid
    mapping modern players to historical entries with the same name. Final
    return is run through `_redirect_alias` so merged players always land on
    their canonical id."""
    first = player_info.get("firstName", "")
    last = player_info.get("lastName", "")
    full_name = f"{first} {last}".strip()
    ascii_name = _strip_accents(full_name)

    # Try exact name match first (including accent-insensitive)
    # When multiple matches exist, prefer the most recently active player
    cursor.execute("SELECT player_id, name FROM players WHERE name = ? OR name = ?",
                   (full_name, ascii_name))
    rows = cursor.fetchall()
    plausible_exact = [(r[0], r[1]) for r in rows if _is_temporally_plausible(cursor, r[0], season)]
    if plausible_exact:
        if len(plausible_exact) > 1:
            # Tiebreak 1: prefer player whose stored team matches the incoming team
            retro_team_code = retro_team(team_abbrev)
            team_matches = [(pid, pname) for pid, pname in plausible_exact
                           if _get_stored_team(cursor, pid) == retro_team_code]
            if len(team_matches) == 1:
                plausible_exact = team_matches
            else:
                # Tiebreak 2: most recently active
                best = None
                best_season = -1
                for pid, pname in plausible_exact:
                    cursor.execute("""
                        SELECT MAX(season) FROM (
                            SELECT MAX(season) as season FROM season_batting_stats WHERE player_id = ?
                            UNION ALL
                            SELECT MAX(season) FROM season_pitching_stats WHERE player_id = ?
                        )
                    """, (pid, pid))
                    last = cursor.fetchone()
                    last_season = last[0] if last and last[0] else 0
                    if last_season > best_season:
                        best_season = last_season
                        best = (pid, pname)
                if best:
                    plausible_exact = [best]

        pid, pname = plausible_exact[0]
        if pname != full_name:
            cursor.execute("UPDATE players SET name = ?, team = ? WHERE player_id = ?",
                           (full_name, retro_team(team_abbrev), pid))
        else:
            cursor.execute("UPDATE players SET team = ? WHERE player_id = ?",
                           (retro_team(team_abbrev), pid))
        return _redirect_alias(cursor, pid)

    # Try last name + first initial match (with temporal check)
    cursor.execute("SELECT player_id, name FROM players WHERE name LIKE ?",
                    (f"{first[0]}% {last}" if first else f"% {last}",))
    rows = cursor.fetchall()
    plausible = [(r[0], r[1]) for r in rows if _is_temporally_plausible(cursor, r[0], season)]
    if len(plausible) == 1:
        cursor.execute("UPDATE players SET team = ? WHERE player_id = ?",
                        (retro_team(team_abbrev), plausible[0][0]))
        return _redirect_alias(cursor, plausible[0][0])

    # Create new player entry
    pid = build_player_id(player_info)
    # Check for collision
    cursor.execute("SELECT 1 FROM players WHERE player_id = ?", (pid,))
    if cursor.fetchone():
        # Increment serial
        for i in range(2, 100):
            alt = f"{pid[:-3]}{i:03d}"
            cursor.execute("SELECT 1 FROM players WHERE player_id = ?", (alt,))
            if not cursor.fetchone():
                pid = alt
                break

    bats = player_info.get("handedness", {}).get("bats", None)
    throws = player_info.get("handedness", {}).get("throws", None)
    position = player_info.get("primaryPosition", None)

    cursor.execute(
        "INSERT OR IGNORE INTO players (player_id, name, team, positions, bats, throws) VALUES (?, ?, ?, ?, ?, ?)",
        (pid, full_name, retro_team(team_abbrev), position, bats, throws),
    )
    return _redirect_alias(cursor, pid)


def pull_season_batting(conn, season_str):
    """Pull season batting stats from MySportsFeeds."""
    season_year = detect_season(season_str)
    print(f"  Pulling season batting stats for {season_str}...")

    # Include P for two-way players like Ohtani (primaryPosition=P but also bats)
    data = msf_get(f"{season_str}/player_stats_totals.json", {"position": "C,1B,2B,3B,SS,LF,CF,RF,DH,OF,P"})
    if not data:
        print("    No new data (304)")
        return 0

    totals = data.get("playerStatsTotals", [])
    cursor = conn.cursor()
    count = 0

    # Delete existing data for this season (full replace)
    cursor.execute("DELETE FROM season_batting_stats WHERE season = ?", (season_year,))

    for entry in totals:
        player = entry.get("player", {})
        team_info = entry.get("team", {})
        all_stats = entry.get("stats", {})
        stats = all_stats.get("batting", {})
        if not stats:
            continue

        team_abbrev = team_info.get("abbreviation", "")
        pid = find_or_create_player(cursor, player, team_abbrev, season_year)

        ab = safe_int(stats.get("atBats"))
        h = safe_int(stats.get("hits"))
        doubles = safe_int(stats.get("secondBaseHits"))
        triples = safe_int(stats.get("thirdBaseHits"))
        hr = safe_int(stats.get("homeruns"))
        bb = safe_int(stats.get("batterWalks"))
        so = safe_int(stats.get("batterStrikeouts"))
        hbp = safe_int(stats.get("hitByPitch"))
        sf = safe_int(stats.get("batterSacrificeFlies"))
        ibb = safe_int(stats.get("batterIntentionalWalks"))

        pa, avg, obp, slg, ops, iso, babip = compute_rate_stats(h, ab, bb, hbp, sf, doubles, triples, hr, so)

        cursor.execute("""
            INSERT OR REPLACE INTO season_batting_stats
            (player_id, season, team, games, plate_appearances, at_bats, hits, doubles,
             triples, home_runs, runs, rbi, stolen_bases, caught_stealing, walks,
             strikeouts, hit_by_pitch, sacrifice_flies, intentional_walks,
             batting_avg, obp, slg, ops, iso, babip)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            pid, season_year, retro_team(team_abbrev),
            safe_int(all_stats.get("gamesPlayed")),
            pa, ab, h, doubles, triples, hr,
            safe_int(stats.get("runs")),
            safe_int(stats.get("runsBattedIn")),
            safe_int(stats.get("stolenBases")),
            safe_int(stats.get("caughtBaseSteals")),
            bb, so, hbp, sf, ibb,
            avg, obp, slg, ops, iso, babip,
        ))
        count += 1

    conn.commit()
    print(f"    Loaded {count} batter season totals")
    return count


def pull_season_pitching(conn, season_str):
    """Pull season pitching stats from MySportsFeeds."""
    season_year = detect_season(season_str)
    print(f"  Pulling season pitching stats for {season_str}...")

    data = msf_get(f"{season_str}/player_stats_totals.json", {"position": "P"})
    if not data:
        print("    No new data (304)")
        return 0

    totals = data.get("playerStatsTotals", [])
    cursor = conn.cursor()
    count = 0

    cursor.execute("DELETE FROM season_pitching_stats WHERE season = ?", (season_year,))

    for entry in totals:
        player = entry.get("player", {})
        team_info = entry.get("team", {})
        all_stats = entry.get("stats", {})
        stats = all_stats.get("pitching", {})
        misc = all_stats.get("miscellaneous", {})
        if not stats:
            continue

        team_abbrev = team_info.get("abbreviation", "")
        pid = find_or_create_player(cursor, player, team_abbrev, season_year)

        ip_raw = safe_float(stats.get("inningsPitched"), 0)
        # Convert IP float (e.g., 6.1 = 6 1/3) to outs
        ip_whole = int(ip_raw)
        ip_frac = round((ip_raw - ip_whole) * 10)
        ip_outs = ip_whole * 3 + ip_frac
        innings_text = f"{ip_whole}.{ip_frac}"

        h = safe_int(stats.get("hitsAllowed"))
        er = safe_int(stats.get("earnedRunsAllowed"))
        bb = safe_int(stats.get("pitcherWalks"))
        so = safe_int(stats.get("pitcherStrikeouts"))
        hr = safe_int(stats.get("homerunsAllowed"))
        bf = safe_int(stats.get("totalBattersFaced"))

        era = (er * 9.0) / (ip_outs / 3.0) if ip_outs > 0 else None
        whip = (h + bb) / (ip_outs / 3.0) if ip_outs > 0 else None
        k9 = (so * 9.0) / (ip_outs / 3.0) if ip_outs > 0 else None
        bb9 = (bb * 9.0) / (ip_outs / 3.0) if ip_outs > 0 else None
        k_bb = so / bb if bb > 0 else None
        h9 = (h * 9.0) / (ip_outs / 3.0) if ip_outs > 0 else None
        hr9 = (hr * 9.0) / (ip_outs / 3.0) if ip_outs > 0 else None

        hbp = safe_int(stats.get("battersHit"))
        sac_bunts = safe_int(stats.get("pitcherSacrificeBunts"))
        sac_flies = safe_int(stats.get("pitcherSacrificeFlies"))
        ab_approx = bf - bb - hbp - sac_flies - sac_bunts
        baa = h / ab_approx if ab_approx > 0 else None

        cursor.execute("""
            INSERT OR REPLACE INTO season_pitching_stats
            (player_id, season, team, games, games_started, games_finished, complete_games,
             wins, losses, saves, ip_outs, innings_pitched, hits, runs, earned_runs,
             home_runs, walks, intentional_walks, strikeouts, hit_by_pitch, wild_pitches,
             balks, batters_faced, sacrifice_hits, sacrifice_flies,
             era, whip, k_per_9, bb_per_9, k_per_bb, h_per_9, hr_per_9, baa)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            pid, season_year, retro_team(team_abbrev),
            safe_int(all_stats.get("gamesPlayed")),
            safe_int(misc.get("gamesStarted")),
            safe_int(stats.get("gamesFinished")),
            safe_int(stats.get("completedGames")),
            safe_int(stats.get("wins")),
            safe_int(stats.get("losses")),
            safe_int(stats.get("saves")),
            ip_outs, innings_text, h,
            safe_int(stats.get("runsAllowed")),
            er, hr, bb,
            safe_int(stats.get("pitcherIntentionalWalks")),
            so, hbp,
            safe_int(stats.get("pitcherWildPitches")),
            safe_int(stats.get("balks")),
            bf, sac_bunts, sac_flies,
            era, whip, k9, bb9, k_bb, h9, hr9, baa,
        ))
        count += 1

    conn.commit()
    print(f"    Loaded {count} pitcher season totals")
    return count


def get_game_dates(season_str):
    """Get all dates with games in this season (up to today).

    Uses the schedule to find the season start, then generates every date
    from start to today. This avoids a UTC/local timezone mismatch where
    the schedule's startTime is in UTC (e.g., 2026-03-26T00:05Z for a
    March 25 EDT game) but the gamelogs endpoint uses local dates.
    """
    data = msf_get(f"{season_str}/games.json")
    if not data:
        return []
    # Find earliest game date, converting UTC to Eastern (subtract a day
    # for games starting between midnight and 6 AM UTC = evening Eastern)
    dates = set()
    for game in data.get("games", []):
        sched = game.get("schedule", {})
        start = sched.get("startTime", "")
        if start:
            # Convert UTC to local date for the API request.
            # MSF indexes games by their MLB-official date (local),
            # but the schedule startTime is UTC.
            dt = datetime.strptime(start[:19], "%Y-%m-%dT%H:%M:%S")
            # Games starting 00:00-05:59 UTC are evening Eastern games (previous day)
            if dt.hour < 6:
                dt = dt - timedelta(days=1)
            dates.add(dt.strftime("%Y%m%d"))

    if not dates:
        return []

    # Only return dates up to today (no point hitting API for future games)
    today = datetime.now().strftime("%Y%m%d")
    return sorted(d for d in dates if d <= today)


def _get_last_game_date_pulled(conn, season_year):
    """Get the last game date we successfully pulled logs for."""
    try:
        row = conn.execute("""
            SELECT updated_at FROM data_freshness WHERE key = ?
        """, (f"last_game_date_{season_year}",)).fetchone()
        return row[0] if row else None
    except:
        return None


def _set_last_game_date_pulled(conn, season_year, last_date):
    """Record the last game date we successfully pulled."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS data_freshness (
            key TEXT PRIMARY KEY, updated_at TEXT NOT NULL, season TEXT
        )
    """)
    conn.execute("""
        INSERT OR REPLACE INTO data_freshness (key, updated_at, season)
        VALUES (?, ?, ?)
    """, (f"last_game_date_{season_year}", last_date, str(season_year)))
    conn.commit()


def pull_game_logs(conn, season_str, full_refresh=False):
    """Pull batting and pitching game logs from MySportsFeeds.

    Cumulative by default: only pulls dates since the last successful pull.
    full_refresh=True: wipes and re-pulls all dates (weekly reconciliation).
    """
    season_year = detect_season(season_str)
    print(f"  Pulling game logs for {season_str}...")

    # Migrate: add stolen_bases/caught_stealing if missing
    cols = {row[1] for row in conn.execute("PRAGMA table_info(game_batting_logs)").fetchall()}
    if "stolen_bases" not in cols:
        conn.execute("ALTER TABLE game_batting_logs ADD COLUMN stolen_bases INTEGER DEFAULT 0")
        print("    Added stolen_bases column to game_batting_logs")
    if "caught_stealing" not in cols:
        conn.execute("ALTER TABLE game_batting_logs ADD COLUMN caught_stealing INTEGER DEFAULT 0")
        print("    Added caught_stealing column to game_batting_logs")
    conn.commit()

    game_dates = get_game_dates(season_str)
    if not game_dates:
        print("    No game dates found")
        return 0, 0

    cursor = conn.cursor()
    bat_count = 0
    pitch_count = 0

    if full_refresh:
        print(f"    Full refresh: wiping and re-pulling all {len(game_dates)} game days")
        cursor.execute("DELETE FROM game_batting_logs WHERE season = ?", (season_year,))
        cursor.execute("DELETE FROM game_pitching_logs WHERE season = ?", (season_year,))
    else:
        # Only pull dates since last successful pull
        last_pulled = _get_last_game_date_pulled(conn, season_year)
        if last_pulled:
            # Pull from 1 day before last_pulled (to catch late corrections)
            # and all new dates
            cutoff = last_pulled.replace("-", "")
            old_count = len(game_dates)
            # Include the last pulled date (for corrections) + all newer
            game_dates = [d for d in game_dates if d >= cutoff]
            print(f"    Incremental: {len(game_dates)} new/recent dates (of {old_count} total)")
        else:
            print(f"    First run: pulling all {len(game_dates)} game days")
            cursor.execute("DELETE FROM game_batting_logs WHERE season = ?", (season_year,))
            cursor.execute("DELETE FROM game_pitching_logs WHERE season = ?", (season_year,))

    # Track game numbers per player per stored date — persists across all API date requests.
    # Handles cases where two different request dates (e.g., 20260326 and 20260327) both
    # return games that store under the same date (e.g., 2026-03-27 due to UTC offset).
    player_date_game_num = {}

    dates_with_data = []  # Track which dates actually returned game logs

    for i, gdate in enumerate(game_dates):
        time.sleep(2)  # Rate limit courtesy
        try:
            data = msf_get(f"{season_str}/date/{gdate}/player_gamelogs.json")
        except Exception as e:
            print(f"    Skipping {gdate}: {e}")
            continue
        if not data:
            continue
        logs = data.get("gamelogs", [])
        if logs:
            dates_with_data.append(gdate)

        for entry in logs:
            player = entry.get("player", {})
            team_info = entry.get("team", {})
            game = entry.get("game", {})
            all_stats = entry.get("stats", {})
            team_abbrev = team_info.get("abbreviation", "")

            # Use the MSF request date (gdate) as the game date — this is the
            # official MLB game date. Don't parse startTime which is UTC and
            # puts late ET games on the wrong calendar day.
            game_date = f"{gdate[:4]}-{gdate[4:6]}-{gdate[6:8]}"
            game_id = game.get("id", 0)  # MSF game ID for doubleheader ordering
            away_team = game.get("awayTeamAbbreviation", "")
            home_team = game.get("homeTeamAbbreviation", "")
            is_home = team_abbrev == home_team
            opponent = away_team if is_home else home_team
            vishome = "H" if is_home else "V"

            # Batting log
            bat = all_stats.get("batting", {})
            if bat and (safe_int(bat.get("atBats")) > 0 or safe_int(bat.get("batterWalks")) > 0 or safe_int(bat.get("hitByPitch")) > 0):
                pid = find_or_create_player(cursor, player, team_abbrev, season_year)
                # Debug: log matching for players we know are problematic
                _debug_name = f"{player.get('firstName','')} {player.get('lastName','')}"
                if 'ramírez' in _debug_name.lower() and 'josé' in _debug_name.lower():
                    print(f"    DEBUG: {_debug_name} ({team_abbrev}) → {pid} on {game_date}")

                # Determine game number for doubleheaders
                pkey = (pid, game_date)
                game_num = player_date_game_num.get(pkey, 0)
                player_date_game_num[pkey] = game_num + 1

                ab = safe_int(bat.get("atBats"))
                h = safe_int(bat.get("hits"))
                doubles = safe_int(bat.get("secondBaseHits"))
                triples = safe_int(bat.get("thirdBaseHits"))
                hr = safe_int(bat.get("homeruns"))
                bb = safe_int(bat.get("batterWalks"))
                so = safe_int(bat.get("batterStrikeouts"))
                hbp = safe_int(bat.get("hitByPitch"))
                sf = safe_int(bat.get("batterSacrificeFlies", 0))
                sb = safe_int(bat.get("stolenBases"))
                cs = safe_int(bat.get("caughtBaseSteals"))
                pa, avg, obp, slg, ops, _, _ = compute_rate_stats(h, ab, bb, hbp, sf, doubles, triples, hr, so)

                cursor.execute("""
                    INSERT OR REPLACE INTO game_batting_logs
                    (player_id, season, date, game_number, opponent, vishome,
                     plate_appearances, at_bats,
                     hits, doubles, triples, home_runs, runs, rbi, walks, strikeouts,
                     hit_by_pitch, sacrifice_flies, stolen_bases, caught_stealing,
                     batting_avg, obp, slg, ops)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    pid, season_year, game_date, game_num, retro_team(opponent), vishome,
                    pa, ab, h, doubles, triples, hr,
                    safe_int(bat.get("runs")),
                    safe_int(bat.get("runsBattedIn")),
                    bb, so, hbp, sf, sb, cs, avg, obp, slg, ops,
                ))
                bat_count += 1

            # Pitching log
            pitch = all_stats.get("pitching", {})
            if pitch and safe_float(pitch.get("inningsPitched"), 0) > 0:
                pid = find_or_create_player(cursor, player, team_abbrev, season_year)

                # Determine game number for doubleheaders
                pkey = (pid, game_date)
                if pkey not in player_date_game_num:
                    player_date_game_num[pkey] = 0
                game_num = player_date_game_num[pkey]
                # Don't increment again if batting already incremented for this player+date

                ip_raw = safe_float(pitch.get("inningsPitched"), 0)
                ip_whole = int(ip_raw)
                ip_frac = round((ip_raw - ip_whole) * 10)
                ip_outs = ip_whole * 3 + ip_frac
                innings_text = f"{ip_whole}.{ip_frac}"
                h_p = safe_int(pitch.get("hitsAllowed"))
                er = safe_int(pitch.get("earnedRunsAllowed"))
                era = (er * 9.0) / (ip_outs / 3.0) if ip_outs > 0 else None
                misc = all_stats.get("miscellaneous", {})

                cursor.execute("""
                    INSERT OR REPLACE INTO game_pitching_logs
                    (player_id, season, date, game_number, opponent, vishome, is_start,
                     ip_outs, innings_pitched,
                     hits, runs, earned_runs, home_runs, walks, strikeouts, hit_by_pitch,
                     batters_faced, win, loss, save, era)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    pid, season_year, game_date, game_num, retro_team(opponent), vishome,
                    1 if safe_int(misc.get("gamesStarted")) > 0 else 0,
                    ip_outs, innings_text, h_p,
                    safe_int(pitch.get("runsAllowed")),
                    er,
                    safe_int(pitch.get("homerunsAllowed")),
                    safe_int(pitch.get("pitcherWalks")),
                    safe_int(pitch.get("pitcherStrikeouts")),
                    safe_int(pitch.get("battersHit")),
                    safe_int(pitch.get("totalBattersFaced")),
                    safe_int(pitch.get("wins")),
                    safe_int(pitch.get("losses")),
                    safe_int(pitch.get("saves")),
                    era,
                ))
                pitch_count += 1

        conn.commit()

    # Record the last date that actually returned data (not just attempted)
    # This prevents skipping dates that MSF hasn't published yet
    if dates_with_data:
        last_date = f"{dates_with_data[-1][:4]}-{dates_with_data[-1][4:6]}-{dates_with_data[-1][6:8]}"
        _set_last_game_date_pulled(conn, season_year, last_date)

    print(f"    Loaded {bat_count} batting + {pitch_count} pitching game logs across {len(game_dates)} days")

    # Check play-by-play availability for yesterday AND today
    # Overnight runs (after midnight UTC) need yesterday's date to catch last night's games
    from datetime import date as _date, timedelta as _td
    for check_date in [_date.today() - _td(days=1), _date.today()]:
        check_gdate = check_date.strftime("%Y%m%d")
        check_has_gamelogs = check_gdate in dates_with_data
        try:
            pbp_data = msf_get(f"{season_str}/date/{check_gdate}/games.json")
            if pbp_data and pbp_data.get("games"):
                games = pbp_data["games"]
                completed = sum(1 for g in games if g.get("schedule", {}).get("playedStatus") == "COMPLETED")
                in_progress = sum(1 for g in games if g.get("schedule", {}).get("playedStatus") == "LIVE")
                if check_has_gamelogs:
                    print(f"    PBP CHECK ({check_gdate}): {len(games)} games ({completed} completed, {in_progress} live) — game logs ALSO available")
                else:
                    print(f"    PBP CHECK ({check_gdate}): {len(games)} games ({completed} completed, {in_progress} live) — game logs NOT yet available")
                    if completed > 0:
                        print(f"    >>> PLAY-BY-PLAY AVAILABLE BEFORE GAME LOGS — could derive game stats for faster detection")
            else:
                print(f"    PBP CHECK ({check_gdate}): no games data from MSF")
        except Exception as e:
            print(f"    PBP CHECK ({check_gdate}): failed ({e})")

    return bat_count, pitch_count


def pull_team_game_results(conn, season_str):
    """Pull per-team per-game results from MSF games.json.

    Stores TWO rows per game (one per team) for symmetric querying — a
    "Yankees record" lookup is just `WHERE team = 'NYA'`, and temporal
    joins to player game logs key on (team, date) cleanly. Computes
    cumulative `wins_after` / `losses_after` per team in a second pass
    using a window-function-style running sum.
    """
    season_year = detect_season(season_str)
    print(f"  Pulling team game results for {season_str}...")

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
    # Migration: add daynight + gametype columns if running against an older
    # schema (table existed before the Retrosheet backfill landed).
    cols = {row[1] for row in cursor.execute("PRAGMA table_info(team_game_results)").fetchall()}
    if "daynight" not in cols:
        cursor.execute("ALTER TABLE team_game_results ADD COLUMN daynight TEXT")
    if "gametype" not in cols:
        cursor.execute("ALTER TABLE team_game_results ADD COLUMN gametype TEXT")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_tgr_team_season ON team_game_results(team, season)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_tgr_date ON team_game_results(date)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_tgr_team_date ON team_game_results(team, date)")
    conn.commit()

    data = msf_get(f"{season_str}/games.json")
    if not data:
        print("    No games returned from MSF")
        return 0
    games = data.get("games", [])

    # Per-team per-date game-number tracking for doubleheader ordering.
    # MSF returns games in schedule order; if two games share a date+team
    # we increment the counter so the second gets game_number=1.
    team_date_count = {}
    inserted = 0
    for game in games:
        sched = game.get("schedule", {})
        score = game.get("score", {}) or {}
        if sched.get("playedStatus") != "COMPLETED":
            continue

        start = sched.get("startTime", "")
        if not start:
            continue
        # Convert UTC to local Eastern date for MLB-official date alignment
        # (00:00-05:59 UTC = previous evening Eastern).
        try:
            dt = datetime.strptime(start[:19], "%Y-%m-%dT%H:%M:%S")
            if dt.hour < 6:
                dt = dt - timedelta(days=1)
            game_date = dt.strftime("%Y-%m-%d")
        except Exception:
            continue

        away_msf = sched.get("awayTeam", {}).get("abbreviation", "")
        home_msf = sched.get("homeTeam", {}).get("abbreviation", "")
        if not away_msf or not home_msf:
            continue
        away_team = retro_team(away_msf)
        home_team = retro_team(home_msf)

        away_runs = safe_int(score.get("awayScoreTotal"))
        home_runs = safe_int(score.get("homeScoreTotal"))

        innings_played = len(score.get("innings", [])) or 9

        venue = (sched.get("venue") or {}).get("name") or None
        attendance = safe_int(sched.get("attendance")) or None
        weather_obj = sched.get("weather") or {}
        if isinstance(weather_obj, dict) and weather_obj:
            # Compact weather string: "Cloudy, 68F, wind 10 mph SE"
            parts = []
            if weather_obj.get("type"):
                parts.append(str(weather_obj["type"]))
            temp = weather_obj.get("temperature", {})
            if isinstance(temp, dict) and temp.get("fahrenheit") is not None:
                parts.append(f"{temp['fahrenheit']}F")
            wind = weather_obj.get("wind", {})
            if isinstance(wind, dict) and wind.get("speed", {}).get("milesPerHour") is not None:
                w = f"wind {wind['speed']['milesPerHour']} mph"
                if wind.get("direction", {}).get("label"):
                    w += f" {wind['direction']['label']}"
                parts.append(w)
            weather = ", ".join(parts) if parts else None
        else:
            weather = None

        ended = sched.get("endedTime", "")
        duration_min = None
        if ended and start:
            try:
                t1 = datetime.strptime(start[:19], "%Y-%m-%dT%H:%M:%S")
                t2 = datetime.strptime(ended[:19], "%Y-%m-%dT%H:%M:%S")
                duration_min = int((t2 - t1).total_seconds() / 60)
                if duration_min < 30 or duration_min > 600:  # sanity bounds
                    duration_min = None
            except Exception:
                pass

        # Doubleheader ordering: increment per (date, team)
        key_away = (game_date, away_team)
        key_home = (game_date, home_team)
        away_game_num = team_date_count.get(key_away, 0)
        home_game_num = team_date_count.get(key_home, 0)
        team_date_count[key_away] = away_game_num + 1
        team_date_count[key_home] = home_game_num + 1

        # Day/night from the home venue's local clock. Games starting before
        # 17:00 (5 PM) local are "day". Falls back to ET (-4) if home_team
        # isn't in TEAM_TZ_OFFSET — should never happen for real MLB games.
        daynight = None
        try:
            hour_utc = int(start[11:13])
            tz_offset = TEAM_TZ_OFFSET.get(home_team, -4)
            local_hour = (hour_utc + tz_offset) % 24
            daynight = "day" if 6 <= local_hour < 17 else "night"
        except Exception:
            pass

        # Two rows per game
        for team, opponent, is_home, t_runs, o_runs, gnum in (
            (away_team, home_team, 0, away_runs, home_runs, away_game_num),
            (home_team, away_team, 1, home_runs, away_runs, home_game_num),
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
            """, (game_date, season_year, gnum, team, opponent, is_home,
                  t_runs, o_runs, result, innings_played,
                  start[:19] + "Z" if start else None,
                  attendance, duration_min, venue, weather,
                  daynight, "regular"))
            inserted += 1

    conn.commit()

    # Phase 2: compute cumulative wins_after / losses_after per team.
    # Done in a single pass using row_number windowing — cheap and lets us
    # backfill running totals every refresh in case games were corrected.
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
            WHERE season = ? AND COALESCE(gametype, 'regular') = 'regular'
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
        WHERE season = ? AND COALESCE(gametype, 'regular') = 'regular'
    """, (season_year, season_year))
    conn.commit()

    print(f"    Loaded {inserted} team-game rows ({inserted // 2} games)")
    return inserted


def pull_player_info(conn, season_str):
    """Pull/update player biographical info from MySportsFeeds."""
    print(f"  Pulling player info for {season_str}...")

    data = msf_get("players.json", {"season": season_str})
    if not data:
        print("    No new data (304)")
        return 0

    players = data.get("players", [])
    cursor = conn.cursor()
    count = 0

    for entry in players:
        player = entry.get("player", {})
        first = player.get("firstName", "")
        last = player.get("lastName", "")
        full_name = f"{first} {last}".strip()
        if not full_name:
            continue

        birthdate = player.get("birthDate", None)
        bats = player.get("handedness", {}).get("bats", None)
        throws = player.get("handedness", {}).get("throws", None)
        position = player.get("primaryPosition", None)
        team = player.get("currentTeam", {}).get("abbreviation", "")

        # Update existing player or skip (don't overwrite Retrosheet IDs)
        cursor.execute("SELECT player_id FROM players WHERE name = ?", (full_name,))
        row = cursor.fetchone()
        if row:
            updates = []
            params = []
            if birthdate:
                updates.append("birthdate = ?")
                params.append(birthdate)
            if bats:
                updates.append("bats = ?")
                params.append(bats)
            if throws:
                updates.append("throws = ?")
                params.append(throws)
            if team:
                updates.append("team = ?")
                params.append(retro_team(team))
            if updates:
                params.append(row[0])
                cursor.execute(f"UPDATE players SET {', '.join(updates)} WHERE player_id = ?", params)
                count += 1

    conn.commit()
    print(f"    Updated {count} player records")
    return count


def reconcile_season_totals_from_game_logs(conn, season_year):
    """Recompute season counting + rate stats from the game-log tables.

    Why: MSF exposes season totals and daily game logs as separate endpoints.
    They can lag each other by minutes-to-hours after a game completes — if
    detection runs in that window, the events freeze with stale season totals
    while per-game lines reference the just-loaded game. Symptom: "Judge now
    has 13 HR" written into a feed event that describes the 14th HR. After
    this runs, season totals are derived from the game logs we just ingested,
    so the two are guaranteed consistent.

    Only reconciles single-team season rows (the common case). Players with
    multiple team rows in a season (mid-season trades) keep MSF's per-team
    breakdown since game logs aren't team-keyed by date. That's <1% of rows
    in practice.

    Counting columns absent from game logs (intentional_walks, sacrifice_hits,
    games_finished, complete_games, quality_starts, wild_pitches, balks) are
    preserved from MSF. Rate stats (AVG/OBP/SLG/OPS/ISO/BABIP/ERA/WHIP/K/9/etc.)
    are recomputed from the (possibly updated) counting stats. ops_plus,
    era_plus, league are not touched."""
    print(f"  Reconciling season totals from game logs for {season_year}...")
    cursor = conn.cursor()

    # --- Batting reconciliation ---
    cursor.execute("""
        SELECT player_id,
               COUNT(*) AS games,
               COALESCE(SUM(plate_appearances), 0) AS pa,
               COALESCE(SUM(at_bats), 0) AS ab,
               COALESCE(SUM(hits), 0) AS h,
               COALESCE(SUM(doubles), 0) AS d,
               COALESCE(SUM(triples), 0) AS t,
               COALESCE(SUM(home_runs), 0) AS hr,
               COALESCE(SUM(runs), 0) AS r,
               COALESCE(SUM(rbi), 0) AS rbi,
               COALESCE(SUM(walks), 0) AS bb,
               COALESCE(SUM(strikeouts), 0) AS so,
               COALESCE(SUM(hit_by_pitch), 0) AS hbp,
               COALESCE(SUM(sacrifice_flies), 0) AS sf,
               COALESCE(SUM(stolen_bases), 0) AS sb,
               COALESCE(SUM(caught_stealing), 0) AS cs
        FROM game_batting_logs
        WHERE season = ?
        GROUP BY player_id
    """, (season_year,))
    bat_sums = cursor.fetchall()

    cursor.execute("""
        SELECT player_id FROM season_batting_stats
        WHERE season = ? GROUP BY player_id HAVING COUNT(*) = 1
    """, (season_year,))
    single_team_bat = {r[0] for r in cursor.fetchall()}

    bat_updated = 0
    for row in bat_sums:
        pid, games, pa, ab, h, d, t, hr, r, rbi, bb, so, hbp, sf, sb, cs = row
        if pid not in single_team_bat:
            continue
        avg = h / ab if ab else 0.0
        obp_denom = ab + bb + hbp + sf
        obp = (h + bb + hbp) / obp_denom if obp_denom else 0.0
        singles = h - d - t - hr
        tb = singles + 2 * d + 3 * t + 4 * hr
        slg = tb / ab if ab else 0.0
        ops = obp + slg
        iso = slg - avg
        babip_denom = ab - so - hr + sf
        babip = (h - hr) / babip_denom if babip_denom else 0.0

        cursor.execute("""
            UPDATE season_batting_stats
            SET games=?, plate_appearances=?, at_bats=?, hits=?,
                doubles=?, triples=?, home_runs=?, runs=?, rbi=?,
                walks=?, strikeouts=?, hit_by_pitch=?, sacrifice_flies=?,
                stolen_bases=?, caught_stealing=?,
                batting_avg=?, obp=?, slg=?, ops=?, iso=?, babip=?
            WHERE player_id=? AND season=?
        """, (games, pa, ab, h, d, t, hr, r, rbi, bb, so, hbp, sf, sb, cs,
              round(avg, 3), round(obp, 3), round(slg, 3), round(ops, 3),
              round(iso, 3), round(babip, 3),
              pid, season_year))
        if cursor.rowcount > 0:
            bat_updated += 1

    # --- Pitching reconciliation ---
    cursor.execute("""
        SELECT player_id,
               COUNT(*) AS games,
               COALESCE(SUM(is_start), 0) AS gs,
               COALESCE(SUM(ip_outs), 0) AS ip_outs,
               COALESCE(SUM(hits), 0) AS h,
               COALESCE(SUM(runs), 0) AS r,
               COALESCE(SUM(earned_runs), 0) AS er,
               COALESCE(SUM(home_runs), 0) AS hr,
               COALESCE(SUM(walks), 0) AS bb,
               COALESCE(SUM(strikeouts), 0) AS so,
               COALESCE(SUM(hit_by_pitch), 0) AS hbp,
               COALESCE(SUM(batters_faced), 0) AS bf,
               COALESCE(SUM(win), 0) AS wins,
               COALESCE(SUM(loss), 0) AS losses,
               COALESCE(SUM(save), 0) AS saves
        FROM game_pitching_logs
        WHERE season = ?
        GROUP BY player_id
    """, (season_year,))
    pit_sums = cursor.fetchall()

    cursor.execute("""
        SELECT player_id FROM season_pitching_stats
        WHERE season = ? GROUP BY player_id HAVING COUNT(*) = 1
    """, (season_year,))
    single_team_pit = {r[0] for r in cursor.fetchall()}

    pit_updated = 0
    for row in pit_sums:
        (pid, games, gs, ip_outs, h, r, er, hr, bb, so, hbp, bf,
         wins, losses, saves) = row
        if pid not in single_team_pit:
            continue
        innings_str = f"{ip_outs // 3}.{ip_outs % 3}"
        era = er * 27.0 / ip_outs if ip_outs else 0.0
        whip = (h + bb) * 3.0 / ip_outs if ip_outs else 0.0
        k_per_9 = so * 27.0 / ip_outs if ip_outs else 0.0
        bb_per_9 = bb * 27.0 / ip_outs if ip_outs else 0.0
        k_per_bb = (so / bb) if bb else 0.0
        h_per_9 = h * 27.0 / ip_outs if ip_outs else 0.0
        hr_per_9 = hr * 27.0 / ip_outs if ip_outs else 0.0

        cursor.execute("""
            UPDATE season_pitching_stats
            SET games=?, games_started=?, ip_outs=?, innings_pitched=?,
                hits=?, runs=?, earned_runs=?, home_runs=?,
                walks=?, strikeouts=?, hit_by_pitch=?, batters_faced=?,
                wins=?, losses=?, saves=?,
                era=?, whip=?, k_per_9=?, bb_per_9=?, k_per_bb=?,
                h_per_9=?, hr_per_9=?
            WHERE player_id=? AND season=?
        """, (games, gs, ip_outs, innings_str, h, r, er, hr, bb, so, hbp, bf,
              wins, losses, saves,
              round(era, 2), round(whip, 3), round(k_per_9, 2),
              round(bb_per_9, 2), round(k_per_bb, 2),
              round(h_per_9, 2), round(hr_per_9, 2),
              pid, season_year))
        if cursor.rowcount > 0:
            pit_updated += 1

    conn.commit()
    print(f"    Reconciled {bat_updated} batter rows, {pit_updated} pitcher rows")


def verify_season_total_consistency(conn, season_year):
    """Post-condition for reconcile_season_totals_from_game_logs: every
    single-team current-season player's season counting stats must equal
    SUM(game_logs).

    Logs drift loudly but does not crash the pipeline — downstream steps
    (detection, matchup previews) still run on the best data we have.
    The invariant is a tripwire for future reconciliation regressions or
    drift introduced by a downstream step that mutates season totals.
    """
    print(f"  Verifying season-total / game-log consistency for {season_year}...")
    cursor = conn.cursor()

    # --- Batting ---
    cursor.execute("""
        WITH single_team AS (
            SELECT player_id FROM season_batting_stats
            WHERE season = ? GROUP BY player_id HAVING COUNT(*) = 1
        ),
        gl_sums AS (
            SELECT player_id,
                   SUM(home_runs) AS hr, SUM(hits) AS h, SUM(rbi) AS rbi,
                   SUM(at_bats) AS ab, SUM(walks) AS bb, SUM(strikeouts) AS so
            FROM game_batting_logs WHERE season = ? GROUP BY player_id
        )
        SELECT p.name, s.home_runs, gl.hr, s.hits, gl.h,
               s.rbi, gl.rbi, s.at_bats, gl.ab, s.walks, gl.bb,
               s.strikeouts, gl.so
        FROM season_batting_stats s
        JOIN players p ON s.player_id = p.player_id
        JOIN gl_sums gl ON gl.player_id = s.player_id
        WHERE s.season = ?
          AND s.player_id IN (SELECT player_id FROM single_team)
          AND (s.home_runs != gl.hr OR s.hits != gl.h OR s.rbi != gl.rbi
               OR s.at_bats != gl.ab OR s.walks != gl.bb OR s.strikeouts != gl.so)
        ORDER BY ABS(s.home_runs - gl.hr) DESC
    """, (season_year, season_year, season_year))
    bat_drift = cursor.fetchall()

    # --- Pitching ---
    cursor.execute("""
        WITH single_team AS (
            SELECT player_id FROM season_pitching_stats
            WHERE season = ? GROUP BY player_id HAVING COUNT(*) = 1
        ),
        gl_sums AS (
            SELECT player_id,
                   SUM(ip_outs) AS ip_outs, SUM(earned_runs) AS er,
                   SUM(strikeouts) AS so, SUM(walks) AS bb,
                   SUM(hits) AS h, SUM(home_runs) AS hr
            FROM game_pitching_logs WHERE season = ? GROUP BY player_id
        )
        SELECT p.name, s.ip_outs, gl.ip_outs, s.earned_runs, gl.er,
               s.strikeouts, gl.so, s.walks, gl.bb, s.hits, gl.h,
               s.home_runs, gl.hr
        FROM season_pitching_stats s
        JOIN players p ON s.player_id = p.player_id
        JOIN gl_sums gl ON gl.player_id = s.player_id
        WHERE s.season = ?
          AND s.player_id IN (SELECT player_id FROM single_team)
          AND (s.ip_outs != gl.ip_outs OR s.earned_runs != gl.er
               OR s.strikeouts != gl.so OR s.walks != gl.bb
               OR s.hits != gl.h OR s.home_runs != gl.hr)
        ORDER BY ABS(s.ip_outs - gl.ip_outs) DESC
    """, (season_year, season_year, season_year))
    pit_drift = cursor.fetchall()

    if not bat_drift and not pit_drift:
        print(f"    ✓ Season totals consistent with game logs (batting + pitching)")
        return True

    if bat_drift:
        print(f"    ⚠️ DATA DRIFT: {len(bat_drift)} batter(s) — season != SUM(game_logs)")
        for row in bat_drift[:5]:
            name, s_hr, gl_hr, s_h, gl_h, s_rbi, gl_rbi, s_ab, gl_ab, s_bb, gl_bb, s_so, gl_so = row
            print(f"      {name}: HR {s_hr}/{gl_hr}, H {s_h}/{gl_h}, RBI {s_rbi}/{gl_rbi}, "
                  f"AB {s_ab}/{gl_ab}, BB {s_bb}/{gl_bb}, SO {s_so}/{gl_so}")
        if len(bat_drift) > 5:
            print(f"      ...and {len(bat_drift) - 5} more")
    if pit_drift:
        print(f"    ⚠️ DATA DRIFT: {len(pit_drift)} pitcher(s) — season != SUM(game_logs)")
        for row in pit_drift[:5]:
            name, s_ip, gl_ip, s_er, gl_er, s_so, gl_so, s_bb, gl_bb, s_h, gl_h, s_hr, gl_hr = row
            print(f"      {name}: IP_outs {s_ip}/{gl_ip}, ER {s_er}/{gl_er}, "
                  f"K {s_so}/{gl_so}, BB {s_bb}/{gl_bb}, H {s_h}/{gl_h}, HR {s_hr}/{gl_hr}")
        if len(pit_drift) > 5:
            print(f"      ...and {len(pit_drift) - 5} more")
    return False


def compute_batting_home_away_splits(conn, season_year):
    """Compute batting home/away splits from game logs for this season."""
    print(f"  Computing batting home/away splits for {season_year}...")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM home_away_splits WHERE season = ?", (season_year,))

    cursor.execute("""
        SELECT player_id, vishome,
               COUNT(*) as games,
               SUM(plate_appearances), SUM(at_bats), SUM(hits),
               SUM(doubles), SUM(triples), SUM(home_runs),
               SUM(runs), SUM(rbi), SUM(walks), SUM(strikeouts),
               SUM(COALESCE(hit_by_pitch, 0)), SUM(COALESCE(sacrifice_flies, 0))
        FROM game_batting_logs
        WHERE season = ?
        GROUP BY player_id, vishome
    """, (season_year,))

    count = 0
    for row in cursor.fetchall():
        pid, vh, games, pa, ab, h, doubles, triples, hr, r, rbi, bb, so, hbp, sf = row
        split = "home" if vh == "H" else "away"

        pa_calc, avg, obp, slg, ops, iso, babip = compute_rate_stats(
            h, ab, bb, hbp, sf, doubles, triples, hr, so
        )

        cursor.execute("""
            INSERT OR REPLACE INTO home_away_splits
            (player_id, season, split, games, plate_appearances, at_bats,
             hits, doubles, triples, home_runs, runs, rbi, walks, strikeouts,
             hit_by_pitch, sacrifice_flies, batting_avg, obp, slg, ops, iso, babip)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            pid, season_year, split, games, pa_calc, ab,
            h, doubles, triples, hr, r, rbi, bb, so,
            hbp, sf, avg, obp, slg, ops, iso, babip,
        ))
        count += 1

    conn.commit()
    print(f"    Generated {count} batting home/away split rows")
    return count


def compute_pitching_home_away_splits(conn, season_year):
    """Compute pitching home/away splits from game logs for this season."""
    print(f"  Computing pitching home/away splits for {season_year}...")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM pitching_home_away_splits WHERE season = ?", (season_year,))

    cursor.execute("""
        SELECT player_id, vishome,
               COUNT(*) as games,
               SUM(CASE WHEN is_start = 1 THEN 1 ELSE 0 END),
               SUM(ip_outs), SUM(hits), SUM(earned_runs), SUM(home_runs),
               SUM(walks), SUM(strikeouts), SUM(batters_faced)
        FROM game_pitching_logs
        WHERE season = ?
        GROUP BY player_id, vishome
    """, (season_year,))

    count = 0
    for row in cursor.fetchall():
        pid, vh, games, gs, ip_outs, h, er, hr, bb, so, bf = row
        split = "home" if vh == "H" else "away"
        ip = ip_outs / 3.0 if ip_outs else 0

        era = (er * 9.0) / ip if ip > 0 else None
        whip = (h + bb) / ip if ip > 0 else None
        k9 = (so * 9.0) / ip if ip > 0 else None
        bb9 = (bb * 9.0) / ip if ip > 0 else None
        ab_approx = (bf or 0) - (bb or 0)
        baa = h / ab_approx if ab_approx > 0 else None

        ip_whole = ip_outs // 3
        ip_frac = ip_outs % 3
        innings_text = f"{ip_whole}.{ip_frac}"

        cursor.execute("""
            INSERT OR REPLACE INTO pitching_home_away_splits
            (player_id, season, split, games, games_started, ip_outs, innings_pitched,
             hits, earned_runs, home_runs, walks, strikeouts, era, whip, k_per_9, bb_per_9, baa)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            pid, season_year, split, games, gs, ip_outs, innings_text,
            h, er, hr, bb, so, era, whip, k9, bb9, baa,
        ))
        count += 1

    conn.commit()
    print(f"    Generated {count} pitching home/away split rows")
    return count


def compute_platoon_splits(conn, season_str):
    """Compute platoon, pitch type, and count splits from play-by-play data.

    All three split types are collected in a single pass over the play-by-play API
    to avoid redundant API calls (the most expensive part of the pipeline).

    Platoon: group by handedness → vs_LHP / vs_RHP / vs_LHB / vs_RHB
    Pitch type: group by final pitch throwType → per pitch type PA outcomes
    Count: group by ball-strike count when PA ended → per count PA outcomes
    """
    season_year = detect_season(season_str)
    print(f"  Computing splits from play-by-play for {season_year}...")

    # Result types → counting stat mapping
    HIT_RESULTS = {"SINGLE", "DOUBLE", "TRIPLE", "HOMERUN"}
    OUT_RESULTS = {"FLYOUT", "GROUNDOUT", "LINEOUT", "POPOUT", "DOUBLE_PLAY"}
    NON_AB_RESULTS = {"WALK", "HIT_BY_PITCH", "SACRIFICE_FLY", "SACRIFICE_BUNT",
                      "CATCHER_INTERFERENCE"}

    # Pitch result → count update
    STRIKE_RESULTS = {"CALLED_STRIKE", "SWINGING_STRIKE", "SWINGING_STRIKE_BLOCKED"}
    BALL_RESULTS = {"BALL", "BALL_IN_DIRT", "WILD_PITCH", "PASSED_BALL"}
    FOUL_RESULTS = {"FOUL", "FOUL_TIP"}

    # Normalize pitch type names for display
    PITCH_TYPE_MAP = {
        "FOUR_SEAM_FASTBALL": "4-Seam",
        "SINKER": "Sinker",
        "CUTTER": "Cutter",
        "SLIDER": "Slider",
        "CURVEBALL": "Curve",
        "CHANGEUP": "Change",
        "SPLITTER": "Split",
        "KNUCKLE_CURVE": "Knuckle Curve",
        "SWEEPER": "Sweeper",
        "SCREWBALL": "Screwball",
        "KNUCKLEBALL": "Knuckle",
        "EEPHUS": "Eephus",
        "TWO_SEAM_FASTBALL": "2-Seam",
        "SLURVE": "Slurve",
    }

    # Get all game IDs for this season
    data = msf_get(f"{season_str}/games.json")
    if not data:
        print("    No games found")
        return 0, 0
    games = [g["schedule"]["id"] for g in data.get("games", [])
             if g.get("schedule", {}).get("playedStatus") == "COMPLETED"]
    print(f"    Found {len(games)} completed games to process")

    # Accumulators
    batting_splits = {}       # (msf_batter_id, batter_name, pitcher_hand) → stats
    pitching_splits = {}      # (msf_pitcher_id, pitcher_name, batter_hand) → stats
    bat_pitch_type = {}       # (msf_batter_id, batter_name, pitch_type) → stats
    pitch_pitch_type = {}     # (msf_pitcher_id, pitcher_name, pitch_type) → stats
    bat_count_splits = {}     # (msf_batter_id, batter_name, count_str) → stats
    pitch_count_splits = {}   # (msf_pitcher_id, pitcher_name, count_str) → stats
    bat_risp_splits = {}      # (msf_batter_id, batter_name, "RISP"/"Non-RISP") → stats
    pitch_risp_splits = {}    # (msf_pitcher_id, pitcher_name, "RISP"/"Non-RISP") → stats
    h2h_splits = {}           # (msf_batter_id, batter_name, msf_pitcher_id, pitcher_name) → stats
    bat_first_pa = {}         # (msf_batter_id, batter_name) → stats from each batter's FIRST PA per game
    pitch_inning_splits = {}  # (msf_pitcher_id, pitcher_name, inning_label) → stats; inning bucketed 1..9, "10+"
    pitch_tto_splits = {}     # (msf_pitcher_id, pitcher_name, tto_label) → stats; "1"/"2"/"3"/"4+" times through order

    def empty_bat_stats():
        return {"pa": 0, "ab": 0, "h": 0, "2b": 0, "3b": 0, "hr": 0,
                "rbi": 0, "bb": 0, "so": 0, "hbp": 0, "sf": 0}

    def accumulate(bucket, result):
        """Add a PA outcome to a stats bucket."""
        is_hit = result in HIT_RESULTS
        is_out = result in OUT_RESULTS or result == "STRIKEOUT"
        is_ab = is_hit or is_out

        bucket["pa"] += 1
        if is_ab:
            bucket["ab"] += 1
        if is_hit:
            bucket["h"] += 1
        if result == "DOUBLE":
            bucket["2b"] += 1
        elif result == "TRIPLE":
            bucket["3b"] += 1
        elif result == "HOMERUN":
            bucket["hr"] += 1
        if result == "WALK":
            bucket["bb"] += 1
        if result == "STRIKEOUT":
            bucket["so"] += 1
        if result == "HIT_BY_PITCH":
            bucket["hbp"] += 1
        if result == "SACRIFICE_FLY":
            bucket["sf"] += 1

    for i, gid in enumerate(games):
        if i > 0:
            time.sleep(2)  # Rate limit
        try:
            pbp = msf_get(f"{season_str}/games/{gid}/playbyplay.json")
        except Exception as e:
            print(f"    Game {gid}: error {e}")
            continue
        if not pbp:
            continue

        # Per-game state for first-PA and times-through-order tracking.
        # Reset at the top of each game.
        seen_batters_this_game = set()
        tto_counter = {}  # (pitcher_id, batter_id) → encounter count (1-indexed)

        for ab in pbp.get("atBats", []):
            # Track pitch sequence for this at-bat to find final pitch type and count
            balls = 0
            strikes = 0
            last_pitch_type = None

            all_plays = ab.get("atBatPlay", [])
            for play in all_plays:
                if not isinstance(play, dict):
                    continue
                pitch_data = play.get("pitch", {})
                if pitch_data:
                    # Track count
                    pr = pitch_data.get("result", "")
                    if pr in BALL_RESULTS:
                        balls = min(balls + 1, 3)
                    elif pr in STRIKE_RESULTS:
                        strikes = min(strikes + 1, 2)
                    elif pr in FOUL_RESULTS and strikes < 2:
                        strikes += 1
                    # Track pitch type (keep updating — last one wins)
                    tt = pitch_data.get("throwType")
                    if tt:
                        last_pitch_type = tt

            # Now find the batterUp play (PA outcome)
            for play in all_plays:
                if not isinstance(play, dict):
                    continue
                bu = play.get("batterUp", {})
                if not bu or not isinstance(bu, dict) or not bu.get("result"):
                    continue

                result = bu["result"]
                batter_info = bu.get("battingPlayer", {})
                batter_id = batter_info.get("id")
                batter_name = f"{batter_info.get('firstName', '')} {batter_info.get('lastName', '')}".strip()

                ps = play.get("playStatus", {})
                pitcher_info = ps.get("pitcher", {})
                pitcher_id = pitcher_info.get("id")
                pitcher_name = f"{pitcher_info.get('firstName', '')} {pitcher_info.get('lastName', '')}".strip()

                if not batter_id or not pitcher_id:
                    continue

                # Get handedness
                pitcher_hand = None
                batter_hand = bu.get("standingLeftOrRight")
                for p2 in all_plays:
                    if isinstance(p2, dict):
                        pd = p2.get("pitch", {})
                        if pd and pd.get("throwingLeftOrRight"):
                            pitcher_hand = pd["throwingLeftOrRight"]
                            break

                # --- Platoon splits (requires handedness) ---
                if pitcher_hand and batter_hand:
                    bkey = (batter_id, batter_name, pitcher_hand)
                    if bkey not in batting_splits:
                        batting_splits[bkey] = empty_bat_stats()
                    accumulate(batting_splits[bkey], result)

                    pkey = (pitcher_id, pitcher_name, batter_hand)
                    if pkey not in pitching_splits:
                        pitching_splits[pkey] = empty_bat_stats()
                    accumulate(pitching_splits[pkey], result)

                # --- Pitch type splits (requires last pitch type) ---
                if last_pitch_type:
                    pt_label = PITCH_TYPE_MAP.get(last_pitch_type, last_pitch_type)
                    bt_key = (batter_id, batter_name, pt_label)
                    if bt_key not in bat_pitch_type:
                        bat_pitch_type[bt_key] = empty_bat_stats()
                    accumulate(bat_pitch_type[bt_key], result)

                    pt_key = (pitcher_id, pitcher_name, pt_label)
                    if pt_key not in pitch_pitch_type:
                        pitch_pitch_type[pt_key] = empty_bat_stats()
                    accumulate(pitch_pitch_type[pt_key], result)

                # --- Count splits ---
                count_str = f"{balls}-{strikes}"
                bc_key = (batter_id, batter_name, count_str)
                if bc_key not in bat_count_splits:
                    bat_count_splits[bc_key] = empty_bat_stats()
                accumulate(bat_count_splits[bc_key], result)

                pc_key = (pitcher_id, pitcher_name, count_str)
                if pc_key not in pitch_count_splits:
                    pitch_count_splits[pc_key] = empty_bat_stats()
                accumulate(pitch_count_splits[pc_key], result)

                # --- RISP splits (runners on 2nd and/or 3rd) ---
                has_risp = ps.get("secondBaseRunner") is not None or ps.get("thirdBaseRunner") is not None
                risp_label = "RISP" if has_risp else "Non-RISP"
                br_key = (batter_id, batter_name, risp_label)
                if br_key not in bat_risp_splits:
                    bat_risp_splits[br_key] = empty_bat_stats()
                accumulate(bat_risp_splits[br_key], result)

                pr_key = (pitcher_id, pitcher_name, risp_label)
                if pr_key not in pitch_risp_splits:
                    pitch_risp_splits[pr_key] = empty_bat_stats()
                accumulate(pitch_risp_splits[pr_key], result)

                # --- Head-to-head (batter vs specific pitcher) ---
                h2h_key = (batter_id, batter_name, pitcher_id, pitcher_name)
                if h2h_key not in h2h_splits:
                    h2h_splits[h2h_key] = empty_bat_stats()
                accumulate(h2h_splits[h2h_key], result)

                # --- First PA of game (batting only) ---
                if batter_id not in seen_batters_this_game:
                    seen_batters_this_game.add(batter_id)
                    fpa_key = (batter_id, batter_name)
                    if fpa_key not in bat_first_pa:
                        bat_first_pa[fpa_key] = empty_bat_stats()
                    accumulate(bat_first_pa[fpa_key], result)

                # --- Pitcher inning splits ---
                inning = ab.get("inning")
                if inning is not None:
                    inning_label = str(inning) if inning < 10 else "10+"
                    pi_key = (pitcher_id, pitcher_name, inning_label)
                    if pi_key not in pitch_inning_splits:
                        pitch_inning_splits[pi_key] = empty_bat_stats()
                    accumulate(pitch_inning_splits[pi_key], result)

                # --- Pitcher times-through-order splits ---
                tto_pair = (pitcher_id, batter_id)
                tto_counter[tto_pair] = tto_counter.get(tto_pair, 0) + 1
                tto_n = tto_counter[tto_pair]
                tto_label = str(tto_n) if tto_n < 4 else "4+"
                pt_key = (pitcher_id, pitcher_name, tto_label)
                if pt_key not in pitch_tto_splits:
                    pitch_tto_splits[pt_key] = empty_bat_stats()
                accumulate(pitch_tto_splits[pt_key], result)

                break  # Only process one batterUp per at-bat

        if (i + 1) % 50 == 0:
            print(f"    Processed {i + 1}/{len(games)} games...")

    print(f"    Processed all {len(games)} games: {len(batting_splits)} platoon, "
          f"{len(bat_pitch_type)} pitch type, {len(bat_count_splits)} count, "
          f"{len(bat_risp_splits)} RISP, {len(h2h_splits)} H2H, "
          f"{len(bat_first_pa)} first-PA, {len(pitch_inning_splits)} inning, "
          f"{len(pitch_tto_splits)} TTO splits")

    # --- Insert into tables ---
    cursor = conn.cursor()

    # Ensure new tables exist
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pitch_type_batting_splits (
            player_id TEXT NOT NULL, season INTEGER NOT NULL, pitch_type TEXT NOT NULL,
            plate_appearances INTEGER, at_bats INTEGER, hits INTEGER,
            doubles INTEGER, triples INTEGER, home_runs INTEGER,
            rbi INTEGER, walks INTEGER, strikeouts INTEGER,
            hit_by_pitch INTEGER, sacrifice_flies INTEGER,
            batting_avg REAL, obp REAL, slg REAL, ops REAL, iso REAL, babip REAL,
            UNIQUE(player_id, season, pitch_type)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pitch_type_pitching_splits (
            player_id TEXT NOT NULL, season INTEGER NOT NULL, pitch_type TEXT NOT NULL,
            plate_appearances INTEGER, at_bats INTEGER, hits INTEGER,
            doubles INTEGER, triples INTEGER, home_runs INTEGER,
            walks INTEGER, strikeouts INTEGER, hit_by_pitch INTEGER, sacrifice_flies INTEGER,
            batting_avg_against REAL, obp_against REAL, slg_against REAL, ops_against REAL,
            UNIQUE(player_id, season, pitch_type)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS count_batting_splits (
            player_id TEXT NOT NULL, season INTEGER NOT NULL, count_state TEXT NOT NULL,
            plate_appearances INTEGER, at_bats INTEGER, hits INTEGER,
            doubles INTEGER, triples INTEGER, home_runs INTEGER,
            rbi INTEGER, walks INTEGER, strikeouts INTEGER,
            hit_by_pitch INTEGER, sacrifice_flies INTEGER,
            batting_avg REAL, obp REAL, slg REAL, ops REAL, iso REAL, babip REAL,
            UNIQUE(player_id, season, count_state)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS count_pitching_splits (
            player_id TEXT NOT NULL, season INTEGER NOT NULL, count_state TEXT NOT NULL,
            plate_appearances INTEGER, at_bats INTEGER, hits INTEGER,
            doubles INTEGER, triples INTEGER, home_runs INTEGER,
            walks INTEGER, strikeouts INTEGER, hit_by_pitch INTEGER, sacrifice_flies INTEGER,
            batting_avg_against REAL, obp_against REAL, slg_against REAL, ops_against REAL,
            UNIQUE(player_id, season, count_state)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS risp_batting_splits (
            player_id TEXT NOT NULL, season INTEGER NOT NULL, split TEXT NOT NULL,
            plate_appearances INTEGER, at_bats INTEGER, hits INTEGER,
            doubles INTEGER, triples INTEGER, home_runs INTEGER,
            rbi INTEGER, walks INTEGER, strikeouts INTEGER,
            hit_by_pitch INTEGER, sacrifice_flies INTEGER,
            batting_avg REAL, obp REAL, slg REAL, ops REAL, iso REAL, babip REAL,
            UNIQUE(player_id, season, split)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS risp_pitching_splits (
            player_id TEXT NOT NULL, season INTEGER NOT NULL, split TEXT NOT NULL,
            plate_appearances INTEGER, at_bats INTEGER, hits INTEGER,
            doubles INTEGER, triples INTEGER, home_runs INTEGER,
            walks INTEGER, strikeouts INTEGER, hit_by_pitch INTEGER, sacrifice_flies INTEGER,
            batting_avg_against REAL, obp_against REAL, slg_against REAL, ops_against REAL,
            UNIQUE(player_id, season, split)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS head_to_head (
            batter_id TEXT NOT NULL, pitcher_id TEXT NOT NULL,
            season INTEGER NOT NULL,
            plate_appearances INTEGER, at_bats INTEGER, hits INTEGER,
            doubles INTEGER, triples INTEGER, home_runs INTEGER,
            rbi INTEGER, walks INTEGER, strikeouts INTEGER,
            hit_by_pitch INTEGER, sacrifice_flies INTEGER,
            batting_avg REAL, obp REAL, slg REAL, ops REAL,
            UNIQUE(batter_id, pitcher_id, season)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS first_pa_batting_splits (
            player_id TEXT NOT NULL, season INTEGER NOT NULL,
            plate_appearances INTEGER, at_bats INTEGER, hits INTEGER,
            doubles INTEGER, triples INTEGER, home_runs INTEGER,
            rbi INTEGER, walks INTEGER, strikeouts INTEGER,
            hit_by_pitch INTEGER, sacrifice_flies INTEGER,
            batting_avg REAL, obp REAL, slg REAL, ops REAL, iso REAL, babip REAL,
            UNIQUE(player_id, season)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pitching_inning_splits (
            player_id TEXT NOT NULL, season INTEGER NOT NULL, inning TEXT NOT NULL,
            plate_appearances INTEGER, at_bats INTEGER, hits INTEGER,
            doubles INTEGER, triples INTEGER, home_runs INTEGER,
            walks INTEGER, strikeouts INTEGER, hit_by_pitch INTEGER, sacrifice_flies INTEGER,
            batting_avg_against REAL, obp_against REAL, slg_against REAL, ops_against REAL,
            UNIQUE(player_id, season, inning)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pitching_tto_splits (
            player_id TEXT NOT NULL, season INTEGER NOT NULL, tto TEXT NOT NULL,
            plate_appearances INTEGER, at_bats INTEGER, hits INTEGER,
            doubles INTEGER, triples INTEGER, home_runs INTEGER,
            walks INTEGER, strikeouts INTEGER, hit_by_pitch INTEGER, sacrifice_flies INTEGER,
            batting_avg_against REAL, obp_against REAL, slg_against REAL, ops_against REAL,
            UNIQUE(player_id, season, tto)
        )
    """)

    # Clear existing data for this season
    cursor.execute("DELETE FROM head_to_head WHERE season = ?", (season_year,))
    cursor.execute("DELETE FROM platoon_splits WHERE season = ?", (season_year,))
    cursor.execute("DELETE FROM pitching_platoon_splits WHERE season = ?", (season_year,))
    cursor.execute("DELETE FROM pitch_type_batting_splits WHERE season = ?", (season_year,))
    cursor.execute("DELETE FROM pitch_type_pitching_splits WHERE season = ?", (season_year,))
    cursor.execute("DELETE FROM count_batting_splits WHERE season = ?", (season_year,))
    cursor.execute("DELETE FROM count_pitching_splits WHERE season = ?", (season_year,))
    cursor.execute("DELETE FROM risp_batting_splits WHERE season = ?", (season_year,))
    cursor.execute("DELETE FROM risp_pitching_splits WHERE season = ?", (season_year,))
    cursor.execute("DELETE FROM first_pa_batting_splits WHERE season = ?", (season_year,))
    cursor.execute("DELETE FROM pitching_inning_splits WHERE season = ?", (season_year,))
    cursor.execute("DELETE FROM pitching_tto_splits WHERE season = ?", (season_year,))

    def resolve_player(name):
        cursor.execute("SELECT player_id FROM players WHERE name = ? LIMIT 1", (name,))
        row = cursor.fetchone()
        return row[0] if row else None

    # --- Insert platoon splits ---
    bat_count = 0
    for (msf_id, name, pitcher_hand), stats in batting_splits.items():
        pid = resolve_player(name)
        if not pid:
            continue
        split = "vs_LHP" if pitcher_hand == "L" else "vs_RHP"
        h, ab, bb, hbp, sf = stats["h"], stats["ab"], stats["bb"], stats["hbp"], stats["sf"]
        doubles, triples, hr, so = stats["2b"], stats["3b"], stats["hr"], stats["so"]
        pa_calc, avg, obp, slg, ops, iso, babip = compute_rate_stats(h, ab, bb, hbp, sf, doubles, triples, hr, so)
        cursor.execute("""
            INSERT OR REPLACE INTO platoon_splits
            (player_id, season, split, plate_appearances, at_bats,
             hits, doubles, triples, home_runs, rbi, walks, strikeouts,
             batting_avg, obp, slg, ops, iso, babip)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (pid, season_year, split, pa_calc, ab, h, doubles, triples, hr, stats["rbi"], bb, so, avg, obp, slg, ops, iso, babip))
        bat_count += 1

    pitch_count = 0
    for (msf_id, name, batter_hand), stats in pitching_splits.items():
        pid = resolve_player(name)
        if not pid:
            continue
        split = "vs_LHB" if batter_hand == "L" else "vs_RHB"
        h, ab, bb, hbp, sf = stats["h"], stats["ab"], stats["bb"], stats["hbp"], stats["sf"]
        doubles, triples, hr, so = stats["2b"], stats["3b"], stats["hr"], stats["so"]
        avg_against = h / ab if ab > 0 else None
        obp_denom = ab + bb + hbp + sf
        obp_against = (h + bb + hbp) / obp_denom if obp_denom > 0 else None
        singles = h - doubles - triples - hr
        tb = singles + 2 * doubles + 3 * triples + 4 * hr
        slg_against = tb / ab if ab > 0 else None
        ops_against = (obp_against or 0) + (slg_against or 0) if obp_against is not None and slg_against is not None else None
        cursor.execute("""
            INSERT OR REPLACE INTO pitching_platoon_splits
            (player_id, season, split, plate_appearances, at_bats,
             hits, doubles, triples, home_runs, walks, intentional_walks,
             strikeouts, hit_by_pitch, sacrifice_hits, sacrifice_flies,
             batting_avg_against, obp_against, slg_against, ops_against)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (pid, season_year, split, stats["pa"], ab, h, doubles, triples, hr, bb, 0, so, hbp, 0, sf,
              avg_against, obp_against, slg_against, ops_against))
        pitch_count += 1

    # --- Insert pitch type batting splits ---
    pt_bat_count = 0
    for (msf_id, name, pt), stats in bat_pitch_type.items():
        pid = resolve_player(name)
        if not pid:
            continue
        h, ab, bb, hbp, sf = stats["h"], stats["ab"], stats["bb"], stats["hbp"], stats["sf"]
        doubles, triples, hr, so = stats["2b"], stats["3b"], stats["hr"], stats["so"]
        pa_calc, avg, obp, slg, ops, iso, babip = compute_rate_stats(h, ab, bb, hbp, sf, doubles, triples, hr, so)
        cursor.execute("""
            INSERT OR REPLACE INTO pitch_type_batting_splits
            (player_id, season, pitch_type, plate_appearances, at_bats,
             hits, doubles, triples, home_runs, rbi, walks, strikeouts,
             hit_by_pitch, sacrifice_flies, batting_avg, obp, slg, ops, iso, babip)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (pid, season_year, pt, pa_calc, ab, h, doubles, triples, hr, stats["rbi"], bb, so,
              hbp, sf, avg, obp, slg, ops, iso, babip))
        pt_bat_count += 1

    # --- Insert pitch type pitching splits ---
    pt_pitch_count = 0
    for (msf_id, name, pt), stats in pitch_pitch_type.items():
        pid = resolve_player(name)
        if not pid:
            continue
        h, ab, bb, hbp, sf = stats["h"], stats["ab"], stats["bb"], stats["hbp"], stats["sf"]
        doubles, triples, hr, so = stats["2b"], stats["3b"], stats["hr"], stats["so"]
        avg_against = h / ab if ab > 0 else None
        obp_denom = ab + bb + hbp + sf
        obp_against = (h + bb + hbp) / obp_denom if obp_denom > 0 else None
        singles = h - doubles - triples - hr
        tb = singles + 2 * doubles + 3 * triples + 4 * hr
        slg_against = tb / ab if ab > 0 else None
        ops_against = (obp_against or 0) + (slg_against or 0) if obp_against is not None and slg_against is not None else None
        cursor.execute("""
            INSERT OR REPLACE INTO pitch_type_pitching_splits
            (player_id, season, pitch_type, plate_appearances, at_bats,
             hits, doubles, triples, home_runs, walks, strikeouts,
             hit_by_pitch, sacrifice_flies,
             batting_avg_against, obp_against, slg_against, ops_against)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (pid, season_year, pt, stats["pa"], ab, h, doubles, triples, hr, bb, so, hbp, sf,
              avg_against, obp_against, slg_against, ops_against))
        pt_pitch_count += 1

    # --- Insert count batting splits ---
    ct_bat_count = 0
    for (msf_id, name, count_str), stats in bat_count_splits.items():
        pid = resolve_player(name)
        if not pid:
            continue
        h, ab, bb, hbp, sf = stats["h"], stats["ab"], stats["bb"], stats["hbp"], stats["sf"]
        doubles, triples, hr, so = stats["2b"], stats["3b"], stats["hr"], stats["so"]
        pa_calc, avg, obp, slg, ops, iso, babip = compute_rate_stats(h, ab, bb, hbp, sf, doubles, triples, hr, so)
        cursor.execute("""
            INSERT OR REPLACE INTO count_batting_splits
            (player_id, season, count_state, plate_appearances, at_bats,
             hits, doubles, triples, home_runs, rbi, walks, strikeouts,
             hit_by_pitch, sacrifice_flies, batting_avg, obp, slg, ops, iso, babip)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (pid, season_year, count_str, pa_calc, ab, h, doubles, triples, hr, stats["rbi"], bb, so,
              hbp, sf, avg, obp, slg, ops, iso, babip))
        ct_bat_count += 1

    # --- Insert count pitching splits ---
    ct_pitch_count = 0
    for (msf_id, name, count_str), stats in pitch_count_splits.items():
        pid = resolve_player(name)
        if not pid:
            continue
        h, ab, bb, hbp, sf = stats["h"], stats["ab"], stats["bb"], stats["hbp"], stats["sf"]
        doubles, triples, hr, so = stats["2b"], stats["3b"], stats["hr"], stats["so"]
        avg_against = h / ab if ab > 0 else None
        obp_denom = ab + bb + hbp + sf
        obp_against = (h + bb + hbp) / obp_denom if obp_denom > 0 else None
        singles = h - doubles - triples - hr
        tb = singles + 2 * doubles + 3 * triples + 4 * hr
        slg_against = tb / ab if ab > 0 else None
        ops_against = (obp_against or 0) + (slg_against or 0) if obp_against is not None and slg_against is not None else None
        cursor.execute("""
            INSERT OR REPLACE INTO count_pitching_splits
            (player_id, season, count_state, plate_appearances, at_bats,
             hits, doubles, triples, home_runs, walks, strikeouts,
             hit_by_pitch, sacrifice_flies,
             batting_avg_against, obp_against, slg_against, ops_against)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (pid, season_year, count_str, stats["pa"], ab, h, doubles, triples, hr, bb, so, hbp, sf,
              avg_against, obp_against, slg_against, ops_against))
        ct_pitch_count += 1

    # --- Insert RISP batting splits ---
    risp_bat_count = 0
    for (msf_id, name, risp_label), stats in bat_risp_splits.items():
        pid = resolve_player(name)
        if not pid:
            continue
        h, ab, bb, hbp, sf = stats["h"], stats["ab"], stats["bb"], stats["hbp"], stats["sf"]
        doubles, triples, hr, so = stats["2b"], stats["3b"], stats["hr"], stats["so"]
        pa_calc, avg, obp, slg, ops, iso, babip = compute_rate_stats(h, ab, bb, hbp, sf, doubles, triples, hr, so)
        cursor.execute("""
            INSERT OR REPLACE INTO risp_batting_splits
            (player_id, season, split, plate_appearances, at_bats,
             hits, doubles, triples, home_runs, rbi, walks, strikeouts,
             hit_by_pitch, sacrifice_flies, batting_avg, obp, slg, ops, iso, babip)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (pid, season_year, risp_label, pa_calc, ab, h, doubles, triples, hr, stats["rbi"], bb, so,
              hbp, sf, avg, obp, slg, ops, iso, babip))
        risp_bat_count += 1

    # --- Insert RISP pitching splits ---
    risp_pitch_count = 0
    for (msf_id, name, risp_label), stats in pitch_risp_splits.items():
        pid = resolve_player(name)
        if not pid:
            continue
        h, ab, bb, hbp, sf = stats["h"], stats["ab"], stats["bb"], stats["hbp"], stats["sf"]
        doubles, triples, hr, so = stats["2b"], stats["3b"], stats["hr"], stats["so"]
        avg_against = h / ab if ab > 0 else None
        obp_denom = ab + bb + hbp + sf
        obp_against = (h + bb + hbp) / obp_denom if obp_denom > 0 else None
        singles = h - doubles - triples - hr
        tb = singles + 2 * doubles + 3 * triples + 4 * hr
        slg_against = tb / ab if ab > 0 else None
        ops_against = (obp_against or 0) + (slg_against or 0) if obp_against is not None and slg_against is not None else None
        cursor.execute("""
            INSERT OR REPLACE INTO risp_pitching_splits
            (player_id, season, split, plate_appearances, at_bats,
             hits, doubles, triples, home_runs, walks, strikeouts,
             hit_by_pitch, sacrifice_flies,
             batting_avg_against, obp_against, slg_against, ops_against)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (pid, season_year, risp_label, stats["pa"], ab, h, doubles, triples, hr, bb, so, hbp, sf,
              avg_against, obp_against, slg_against, ops_against))
        risp_pitch_count += 1

    # --- Insert H2H splits ---
    h2h_count = 0
    for (msf_bat_id, bat_name, msf_pit_id, pit_name), stats in h2h_splits.items():
        bat_pid = resolve_player(bat_name)
        pit_pid = resolve_player(pit_name)
        if not bat_pid or not pit_pid:
            continue
        h, ab, bb, hbp, sf = stats["h"], stats["ab"], stats["bb"], stats["hbp"], stats["sf"]
        doubles, triples, hr, so = stats["2b"], stats["3b"], stats["hr"], stats["so"]
        pa_calc, avg, obp, slg, ops, iso, babip = compute_rate_stats(h, ab, bb, hbp, sf, doubles, triples, hr, so)
        cursor.execute("""
            INSERT OR REPLACE INTO head_to_head
            (batter_id, pitcher_id, season, plate_appearances, at_bats,
             hits, doubles, triples, home_runs, rbi, walks, strikeouts,
             hit_by_pitch, sacrifice_flies, batting_avg, obp, slg, ops)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (bat_pid, pit_pid, season_year, pa_calc, ab, h, doubles, triples, hr, stats["rbi"], bb, so,
              hbp, sf, avg, obp, slg, ops))
        h2h_count += 1

    # --- Insert first PA batting splits ---
    fpa_count = 0
    for (msf_id, name), stats in bat_first_pa.items():
        pid = resolve_player(name)
        if not pid:
            continue
        h, ab, bb, hbp, sf = stats["h"], stats["ab"], stats["bb"], stats["hbp"], stats["sf"]
        doubles, triples, hr, so = stats["2b"], stats["3b"], stats["hr"], stats["so"]
        pa_calc, avg, obp, slg, ops, iso, babip = compute_rate_stats(h, ab, bb, hbp, sf, doubles, triples, hr, so)
        cursor.execute("""
            INSERT OR REPLACE INTO first_pa_batting_splits
            (player_id, season, plate_appearances, at_bats,
             hits, doubles, triples, home_runs, rbi, walks, strikeouts,
             hit_by_pitch, sacrifice_flies, batting_avg, obp, slg, ops, iso, babip)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (pid, season_year, pa_calc, ab, h, doubles, triples, hr, stats["rbi"], bb, so,
              hbp, sf, avg, obp, slg, ops, iso, babip))
        fpa_count += 1

    # --- Insert pitcher inning splits ---
    inning_count = 0
    for (msf_id, name, inning_label), stats in pitch_inning_splits.items():
        pid = resolve_player(name)
        if not pid:
            continue
        h, ab, bb, hbp, sf = stats["h"], stats["ab"], stats["bb"], stats["hbp"], stats["sf"]
        doubles, triples, hr, so = stats["2b"], stats["3b"], stats["hr"], stats["so"]
        avg_against = h / ab if ab > 0 else None
        obp_denom = ab + bb + hbp + sf
        obp_against = (h + bb + hbp) / obp_denom if obp_denom > 0 else None
        singles = h - doubles - triples - hr
        tb = singles + 2 * doubles + 3 * triples + 4 * hr
        slg_against = tb / ab if ab > 0 else None
        ops_against = (obp_against or 0) + (slg_against or 0) if obp_against is not None and slg_against is not None else None
        cursor.execute("""
            INSERT OR REPLACE INTO pitching_inning_splits
            (player_id, season, inning, plate_appearances, at_bats,
             hits, doubles, triples, home_runs, walks, strikeouts,
             hit_by_pitch, sacrifice_flies,
             batting_avg_against, obp_against, slg_against, ops_against)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (pid, season_year, inning_label, stats["pa"], ab, h, doubles, triples, hr, bb, so, hbp, sf,
              avg_against, obp_against, slg_against, ops_against))
        inning_count += 1

    # --- Insert pitcher times-through-order splits ---
    tto_count = 0
    for (msf_id, name, tto_label), stats in pitch_tto_splits.items():
        pid = resolve_player(name)
        if not pid:
            continue
        h, ab, bb, hbp, sf = stats["h"], stats["ab"], stats["bb"], stats["hbp"], stats["sf"]
        doubles, triples, hr, so = stats["2b"], stats["3b"], stats["hr"], stats["so"]
        avg_against = h / ab if ab > 0 else None
        obp_denom = ab + bb + hbp + sf
        obp_against = (h + bb + hbp) / obp_denom if obp_denom > 0 else None
        singles = h - doubles - triples - hr
        tb = singles + 2 * doubles + 3 * triples + 4 * hr
        slg_against = tb / ab if ab > 0 else None
        ops_against = (obp_against or 0) + (slg_against or 0) if obp_against is not None and slg_against is not None else None
        cursor.execute("""
            INSERT OR REPLACE INTO pitching_tto_splits
            (player_id, season, tto, plate_appearances, at_bats,
             hits, doubles, triples, home_runs, walks, strikeouts,
             hit_by_pitch, sacrifice_flies,
             batting_avg_against, obp_against, slg_against, ops_against)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (pid, season_year, tto_label, stats["pa"], ab, h, doubles, triples, hr, bb, so, hbp, sf,
              avg_against, obp_against, slg_against, ops_against))
        tto_count += 1

    conn.commit()
    print(f"    Platoon: {bat_count} batting + {pitch_count} pitching")
    print(f"    Pitch type: {pt_bat_count} batting + {pt_pitch_count} pitching")
    print(f"    Count: {ct_bat_count} batting + {ct_pitch_count} pitching")
    print(f"    RISP: {risp_bat_count} batting + {risp_pitch_count} pitching")
    print(f"    H2H: {h2h_count} matchups")
    print(f"    First PA: {fpa_count} batters; Inning: {inning_count} pitcher-innings; TTO: {tto_count} pitcher-times")
    return bat_count, pitch_count


def record_last_update(conn, season_str):
    """Store a timestamp of the last successful data refresh."""
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS data_freshness (
            key TEXT PRIMARY KEY,
            updated_at TEXT NOT NULL,
            season TEXT
        )
    """)
    cursor.execute("""
        INSERT OR REPLACE INTO data_freshness (key, updated_at, season)
        VALUES ('live_stats', datetime('now'), ?)
    """, (season_str,))
    conn.commit()


def compute_league_averages_and_ops_plus(conn, season_year):
    """Compute league averages from season batting stats and update OPS+ for each player."""
    cursor = conn.cursor()

    # Check if there are enough games to compute meaningful averages
    cursor.execute("SELECT SUM(games) FROM season_batting_stats WHERE season = ?", (season_year,))
    row = cursor.fetchone()
    total_games = row[0] if row and row[0] else 0
    if total_games < 100:
        print(f"    Skipping league averages — only {total_games} total games (need 100+)")
        return

    # Sum counting stats across all players for this season
    cursor.execute("""
        SELECT
            SUM(plate_appearances), SUM(at_bats), SUM(hits), SUM(doubles),
            SUM(triples), SUM(home_runs), SUM(walks), SUM(hit_by_pitch),
            SUM(sacrifice_flies), SUM(strikeouts)
        FROM season_batting_stats WHERE season = ?
    """, (season_year,))
    row = cursor.fetchone()
    if not row or not row[1]:
        print("    No batting stats to compute league averages")
        return

    total_pa, total_ab, total_h, total_2b, total_3b, total_hr, total_bb, total_hbp, total_sf, total_so = row

    # Compute league rate stats
    league_avg = total_h / total_ab if total_ab > 0 else 0
    obp_denom = total_ab + total_bb + total_hbp + total_sf
    league_obp = (total_h + total_bb + total_hbp) / obp_denom if obp_denom > 0 else 0
    singles = total_h - total_2b - total_3b - total_hr
    league_slg = (singles + 2 * total_2b + 3 * total_3b + 4 * total_hr) / total_ab if total_ab > 0 else 0
    league_ops = league_obp + league_slg
    league_iso = league_slg - league_avg
    babip_denom = total_ab - total_so - total_hr + total_sf
    league_babip = (total_h - total_hr) / babip_denom if babip_denom > 0 else 0

    # Insert/replace league averages
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS league_averages (
            season INTEGER PRIMARY KEY,
            total_pa, total_ab, total_hits, total_doubles, total_triples, total_hr,
            total_bb, total_hbp, total_sf, total_so,
            league_avg REAL, league_obp REAL, league_slg REAL, league_ops REAL,
            league_iso REAL, league_babip REAL
        )
    """)
    cursor.execute("""
        INSERT OR REPLACE INTO league_averages
        (season, total_pa, total_ab, total_hits, total_doubles, total_triples, total_hr,
         total_bb, total_hbp, total_sf, total_so,
         league_avg, league_obp, league_slg, league_ops, league_iso, league_babip)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        season_year, total_pa, total_ab, total_h, total_2b, total_3b, total_hr,
        total_bb, total_hbp, total_sf, total_so,
        league_avg, league_obp, league_slg, league_ops, league_iso, league_babip,
    ))

    # Update OPS+ for each player: 100 * (player_obp / league_obp + player_slg / league_slg - 1)
    if league_obp > 0 and league_slg > 0:
        cursor.execute("""
            UPDATE season_batting_stats
            SET ops_plus = CAST(100.0 * (obp / ? + slg / ? - 1.0) AS INTEGER)
            WHERE season = ? AND obp IS NOT NULL AND slg IS NOT NULL
        """, (league_obp, league_slg, season_year))
        print(f"    Computed league averages (OBP={league_obp:.3f}, SLG={league_slg:.3f}) and OPS+ for {season_year}")
    else:
        print(f"    League averages computed but OBP/SLG too low for OPS+ calculation")

    conn.commit()


def compute_pitching_league_averages(conn, season_year):
    """Compute league pitching averages and update ERA+ for each pitcher."""
    cursor = conn.cursor()

    # Check if there are enough games
    cursor.execute("SELECT SUM(games) FROM season_pitching_stats WHERE season = ?", (season_year,))
    row = cursor.fetchone()
    total_games = row[0] if row and row[0] else 0
    if total_games < 100:
        print(f"    Skipping pitching league averages — only {total_games} total games (need 100+)")
        return

    # Sum counting stats across all pitchers with 3+ innings (ip_outs >= 9)
    cursor.execute("""
        SELECT
            SUM(ip_outs), SUM(earned_runs), SUM(hits), SUM(walks),
            SUM(strikeouts), SUM(home_runs), SUM(batters_faced)
        FROM season_pitching_stats WHERE season = ? AND ip_outs >= 9
    """, (season_year,))
    row = cursor.fetchone()
    if not row or not row[0]:
        print("    No pitching stats to compute league averages")
        return

    total_ip_outs, total_er, total_h, total_bb, total_so, total_hr, total_bf = row
    total_ip = total_ip_outs / 3.0

    # Compute league rate stats
    league_era = (total_er * 9.0) / total_ip if total_ip > 0 else 0
    league_whip = (total_h + total_bb) / total_ip if total_ip > 0 else 0
    league_k9 = (total_so * 9.0) / total_ip if total_ip > 0 else 0
    league_bb9 = (total_bb * 9.0) / total_ip if total_ip > 0 else 0
    # BAA approximation: H / (BF - BB)
    baa_denom = total_bf - total_bb if total_bf and total_bb else 0
    league_baa = total_h / baa_denom if baa_denom > 0 else 0

    # Create table if needed and insert/replace
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS league_pitching_averages (
            season INTEGER PRIMARY KEY,
            total_ip_outs INTEGER, total_er INTEGER, total_h INTEGER,
            total_bb INTEGER, total_so INTEGER, total_hr INTEGER, total_bf INTEGER,
            league_era REAL, league_whip REAL, league_k_per_9 REAL,
            league_bb_per_9 REAL, league_baa REAL
        )
    """)
    cursor.execute("""
        INSERT OR REPLACE INTO league_pitching_averages
        (season, total_ip_outs, total_er, total_h, total_bb, total_so, total_hr, total_bf,
         league_era, league_whip, league_k_per_9, league_bb_per_9, league_baa)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        season_year, total_ip_outs, total_er, total_h, total_bb, total_so, total_hr, total_bf,
        league_era, league_whip, league_k9, league_bb9, league_baa,
    ))

    # Update ERA+ for each pitcher: 100 * (league_era / player_era)
    if league_era > 0:
        cursor.execute("""
            UPDATE season_pitching_stats
            SET era_plus = CAST(100.0 * (? / era) AS INTEGER)
            WHERE season = ? AND era IS NOT NULL AND era > 0 AND ip_outs >= 9
        """, (league_era, season_year))
        print(f"    Computed pitching league averages (ERA={league_era:.2f}) and ERA+ for {season_year}")
    else:
        print(f"    League pitching averages computed but ERA too low for ERA+ calculation")

    conn.commit()


def main():
    parser = argparse.ArgumentParser(description="Pull live MLB stats from MySportsFeeds")
    parser.add_argument("--season", default=None,
                        help="Season identifier (e.g. '2026-pre', '2026-regular'). Auto-detects if omitted.")
    parser.add_argument("--db", default=DB_PATH, help="Path to SQLite database")
    parser.add_argument("--full-refresh", action="store_true",
                        help="Wipe and re-pull all game logs (weekly reconciliation)")
    args = parser.parse_args()

    # Auto-detect season
    if args.season is None:
        year = date.today().year
        month = date.today().month
        day = date.today().day
        if month < 3 or (month == 3 and day < 25):
            args.season = f"{year}-preseason"
        elif month >= 10:
            args.season = f"{year}-playoff"
        elif month == 3 and day <= 27:
            # Opening Day window (March 25-27): only switch to regular once
            # MSF actually has regular season data. Avoids wiping spring training
            # before any regular season games have been played.
            try:
                probe = msf_get(f"{year}-regular/player_stats_totals.json", {"position": "C,1B,2B,3B,SS,LF,CF,RF,DH,OF,P", "limit": "1"})
                totals = (probe or {}).get("playerStatsTotals", [])
                if totals:
                    args.season = f"{year}-regular"
                    print(f"Regular season data available — switching to regular")
                else:
                    args.season = f"{year}-preseason"
                    print(f"No regular season data yet — staying on preseason")
            except Exception:
                args.season = f"{year}-preseason"
                print(f"Could not probe regular season — staying on preseason")
        else:
            args.season = f"{year}-regular"
        print(f"Auto-detected season: {args.season}")

    if not MSF_API_KEY:
        print("ERROR: Set MSF_API_KEY environment variable")
        return

    print(f"Pulling live stats for {args.season} into {args.db}")
    conn = sqlite3.connect(args.db)
    conn.execute("PRAGMA journal_mode=WAL")

    try:
        t0 = time.time()
        season_year = detect_season(args.season)
        print(f"  Season year: {season_year}, full_refresh: {args.full_refresh}")
        # Player info comes from stats responses (players.json requires higher tier)
        pull_season_batting(conn, args.season)
        compute_league_averages_and_ops_plus(conn, season_year)
        pull_season_pitching(conn, args.season)
        compute_pitching_league_averages(conn, season_year)
        pull_game_logs(conn, args.season, full_refresh=args.full_refresh)

        # Reconcile season totals from game logs. Eliminates the race where
        # MSF's season-totals endpoint lags the daily game logs by minutes
        # to hours after game completion. Must run AFTER pull_game_logs so
        # game logs are the latest data, and BEFORE detect_all later in main()
        # so events are computed against consistent totals.
        reconcile_season_totals_from_game_logs(conn, season_year)
        # Tripwire: assert post-condition. If reconciliation skipped a player
        # (e.g. mid-season trade) or a downstream step mutates season_batting_stats
        # again, this prints loudly. Doesn't fail the pipeline — detection still
        # runs — but the next operator on call will see the diff in logs.
        verify_season_total_consistency(conn, season_year)

        # Team-level game results (scores, W/L, innings, attendance, weather).
        # Cheap: one MSF endpoint, ~15 seconds. Powers "team record" /
        # "yesterday's score" queries and Phase 2 temporal joins.
        pull_team_game_results(conn, args.season)

        # Compute home/away splits from game logs
        compute_batting_home_away_splits(conn, season_year)
        compute_pitching_home_away_splits(conn, season_year)

        # Compute platoon splits from play-by-play
        compute_platoon_splits(conn, args.season)

        # Update prominence columns for iOS disambiguation
        print("\nUpdating player prominence columns...")
        cursor = conn.cursor()
        # Ensure columns exist (migration)
        cols = {row[1] for row in cursor.execute("PRAGMA table_info(players)").fetchall()}
        if "career_games" not in cols:
            cursor.execute("ALTER TABLE players ADD COLUMN career_games INTEGER DEFAULT 0")
            print("  Added career_games column")
        if "last_season" not in cols:
            cursor.execute("ALTER TABLE players ADD COLUMN last_season INTEGER DEFAULT 0")
            print("  Added last_season column")
        cursor.execute("""
            UPDATE players SET
                career_games = COALESCE((SELECT SUM(s.games) FROM season_batting_stats s WHERE s.player_id = players.player_id), 0) +
                               COALESCE((SELECT SUM(sp.games) FROM season_pitching_stats sp WHERE sp.player_id = players.player_id), 0),
                last_season = MAX(
                    COALESCE((SELECT MAX(s.season) FROM season_batting_stats s WHERE s.player_id = players.player_id), 0),
                    COALESCE((SELECT MAX(sp.season) FROM season_pitching_stats sp WHERE sp.player_id = players.player_id), 0)
                )
        """)
        conn.commit()
        print(f"  Updated {cursor.rowcount} players")

        # prominence_score for disambiguation. TEMP TABLE pattern matches
        # data_pipeline/compute_prominence.py — duplicated here so it runs
        # automatically every refresh instead of as a separate manual step.
        # Without this, injured players (Cole post-TJ) and offseason roster
        # changes leave prominence stale and disambiguation picks the wrong
        # player.
        print("  Updating prominence_score...")
        cursor.execute("""
            CREATE TEMP TABLE IF NOT EXISTS _prom_bat AS
            SELECT p.player_id, COALESCE(SUM(s.games), 0) AS score
            FROM players p
            LEFT JOIN season_batting_stats s ON s.player_id = p.player_id
            GROUP BY p.player_id
        """)
        cursor.execute("""
            CREATE TEMP TABLE IF NOT EXISTS _prom_pit AS
            SELECT p.player_id,
                   COALESCE(SUM(sp.games_started), 0) * 5
                   + COALESCE(SUM(sp.saves), 0) * 3
                   + COALESCE(SUM(CASE WHEN sp.games > sp.games_started
                       THEN sp.games - sp.games_started ELSE 0 END), 0) AS score
            FROM players p
            LEFT JOIN season_pitching_stats sp ON sp.player_id = p.player_id
            GROUP BY p.player_id
        """)
        cursor.execute("""
            CREATE TEMP TABLE IF NOT EXISTS _prom_awd AS
            SELECT p.player_id,
                   COALESCE(SUM(CASE
                       WHEN a.award IN ('MVP', 'CY', 'ROY', 'HOF') THEN 1000
                       WHEN a.award IN ('ALL_STAR', 'GG', 'SS') THEN 500
                       WHEN a.award IN ('WS_MVP', 'ALCS_MVP', 'NLCS_MVP') THEN 300
                       ELSE 0
                   END), 0) AS score
            FROM players p
            LEFT JOIN awards a ON a.player_id = p.player_id
            GROUP BY p.player_id
        """)
        cursor.execute("""
            UPDATE players SET prominence_score = (
                SELECT COALESCE(b.score, 0) + COALESCE(pi.score, 0) + COALESCE(a.score, 0)
                FROM _prom_bat b
                LEFT JOIN _prom_pit pi ON pi.player_id = b.player_id
                LEFT JOIN _prom_awd a ON a.player_id = b.player_id
                WHERE b.player_id = players.player_id
            )
        """)
        cursor.execute("DROP TABLE _prom_bat")
        cursor.execute("DROP TABLE _prom_pit")
        cursor.execute("DROP TABLE _prom_awd")
        conn.commit()

        record_last_update(conn, args.season)
        conn.close()

        # Run streak detection for this season
        print(f"\nRunning streak detection for {season_year}...")
        streak_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "detect_streaks.py")
        result = subprocess.run(
            [sys.executable, streak_script, "--season", str(season_year), "--db", args.db],
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode == 0:
            print(result.stdout)
        else:
            print(f"Streak detection failed: {result.stderr}")

        # Detect notable events
        print(f"\nDetecting notable events for {season_year}...")
        try:
            # Add parent dir to path so we can import services
            services_parent = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
            if services_parent not in sys.path:
                sys.path.insert(0, services_parent)
            from services.notable_events import detect_all
            detect_all(args.db, season_year)
        except Exception as e:
            print(f"Notable events detection failed: {e}")

        # AI-powered insights (Sonnet) — kicked off async in a detached
        # subprocess so the rule-based feed events become visible to
        # users as soon as the pipeline lock releases. AI narrative
        # tends to take 5-15 min; the subprocess writes its results
        # straight to notable_events when it completes. If it crashes,
        # the rule-based feed is still correct.
        print(f"\nKicking off AI insights (background)...")
        try:
            ai_log = "/data/ai_insights.log"
            with open(ai_log, "ab") as logf:
                subprocess.Popen(
                    [sys.executable, "-m", "services.ai_notable_events",
                     "--db", args.db, "--season", str(season_year)],
                    stdout=logf, stderr=subprocess.STDOUT,
                    close_fds=True, start_new_session=True,
                    cwd=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."),
                )
            print(f"  AI insights subprocess launched; output → {ai_log}")
        except Exception as e:
            print(f"AI insights launch failed: {e}")

        elapsed = time.time() - t0
        print(f"\nDone in {elapsed:.1f}s")
    except Exception:
        conn.close()
        raise


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        print(f"FATAL ERROR: {e}")
        traceback.print_exc()
        sys.exit(1)
