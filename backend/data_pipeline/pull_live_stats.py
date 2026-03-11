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
from datetime import date

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


def find_or_create_player(cursor, player_info, team_abbrev, season):
    """Find existing player by name or create a new entry. Returns player_id."""
    first = player_info.get("firstName", "")
    last = player_info.get("lastName", "")
    full_name = f"{first} {last}".strip()

    # Try exact name match first
    cursor.execute("SELECT player_id FROM players WHERE name = ?", (full_name,))
    row = cursor.fetchone()
    if row:
        # Update team to most recent
        cursor.execute("UPDATE players SET team = ? WHERE player_id = ?",
                        (retro_team(team_abbrev), row[0]))
        return row[0]

    # Try last name + first initial match
    cursor.execute("SELECT player_id, name FROM players WHERE name LIKE ?",
                    (f"{first[0]}% {last}" if first else f"% {last}",))
    rows = cursor.fetchall()
    if len(rows) == 1:
        cursor.execute("UPDATE players SET team = ? WHERE player_id = ?",
                        (retro_team(team_abbrev), rows[0][0]))
        return rows[0][0]

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
    return pid


def pull_season_batting(conn, season_str):
    """Pull season batting stats from MySportsFeeds."""
    season_year = detect_season(season_str)
    print(f"  Pulling season batting stats for {season_str}...")

    data = msf_get(f"{season_str}/player_stats_totals.json", {"position": "C,1B,2B,3B,SS,LF,CF,RF,DH,OF"})
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
    """Get all dates with games in this season."""
    data = msf_get(f"{season_str}/games.json")
    if not data:
        return []
    dates = set()
    for game in data.get("games", []):
        sched = game.get("schedule", {})
        start = sched.get("startTime", "")
        if start:
            dates.add(start[:10].replace("-", ""))  # "2026-03-07T..." → "20260307"
    return sorted(dates)


def pull_game_logs(conn, season_str):
    """Pull batting and pitching game logs from MySportsFeeds (daily batches)."""
    season_year = detect_season(season_str)
    print(f"  Pulling game logs for {season_str}...")

    game_dates = get_game_dates(season_str)
    if not game_dates:
        print("    No game dates found")
        return 0, 0
    print(f"    Found {len(game_dates)} game days")

    cursor = conn.cursor()
    bat_count = 0
    pitch_count = 0
    cursor.execute("DELETE FROM game_batting_logs WHERE season = ?", (season_year,))
    cursor.execute("DELETE FROM game_pitching_logs WHERE season = ?", (season_year,))

    for i, gdate in enumerate(game_dates):
        time.sleep(2)  # Rate limit courtesy
        data = msf_get(f"{season_str}/date/{gdate}/player_gamelogs.json")
        if not data:
            continue
        logs = data.get("gamelogs", [])

        for entry in logs:
            player = entry.get("player", {})
            team_info = entry.get("team", {})
            game = entry.get("game", {})
            all_stats = entry.get("stats", {})
            team_abbrev = team_info.get("abbreviation", "")

            # Daily format uses flat abbreviation keys
            game_date = game.get("startTime", "")[:10]
            away_team = game.get("awayTeamAbbreviation", "")
            home_team = game.get("homeTeamAbbreviation", "")
            is_home = team_abbrev == home_team
            opponent = away_team if is_home else home_team
            vishome = "H" if is_home else "V"

            # Batting log
            bat = all_stats.get("batting", {})
            if bat and (safe_int(bat.get("atBats")) > 0 or safe_int(bat.get("batterWalks")) > 0 or safe_int(bat.get("hitByPitch")) > 0):
                pid = find_or_create_player(cursor, player, team_abbrev, season_year)
                ab = safe_int(bat.get("atBats"))
                h = safe_int(bat.get("hits"))
                doubles = safe_int(bat.get("secondBaseHits"))
                triples = safe_int(bat.get("thirdBaseHits"))
                hr = safe_int(bat.get("homeruns"))
                bb = safe_int(bat.get("batterWalks"))
                so = safe_int(bat.get("batterStrikeouts"))
                hbp = safe_int(bat.get("hitByPitch"))
                sf = safe_int(bat.get("batterSacrificeFlies", 0))
                pa, avg, obp, slg, ops, _, _ = compute_rate_stats(h, ab, bb, hbp, sf, doubles, triples, hr, so)

                cursor.execute("""
                    INSERT OR REPLACE INTO game_batting_logs
                    (player_id, season, date, opponent, vishome, plate_appearances, at_bats,
                     hits, doubles, triples, home_runs, runs, rbi, walks, strikeouts,
                     hit_by_pitch, sacrifice_flies,
                     batting_avg, obp, slg, ops)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    pid, season_year, game_date, retro_team(opponent), vishome,
                    pa, ab, h, doubles, triples, hr,
                    safe_int(bat.get("runs")),
                    safe_int(bat.get("runsBattedIn")),
                    bb, so, hbp, sf, avg, obp, slg, ops,
                ))
                bat_count += 1

            # Pitching log
            pitch = all_stats.get("pitching", {})
            if pitch and safe_float(pitch.get("inningsPitched"), 0) > 0:
                pid = find_or_create_player(cursor, player, team_abbrev, season_year)
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
                    (player_id, season, date, opponent, vishome, is_start, ip_outs, innings_pitched,
                     hits, runs, earned_runs, home_runs, walks, strikeouts, hit_by_pitch,
                     batters_faced, win, loss, save, era)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    pid, season_year, game_date, retro_team(opponent), vishome,
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

    print(f"    Loaded {bat_count} batting + {pitch_count} pitching game logs across {len(game_dates)} days")
    return bat_count, pitch_count


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
    """Compute batting and pitching platoon splits from play-by-play data.

    For each plate appearance, we know the pitcher handedness and batter handedness.
    Batting platoon: group by (batter, pitcher_hand) → vs_LHP / vs_RHP
    Pitching platoon: group by (pitcher, batter_hand) → vs_LHB / vs_RHB
    """
    season_year = detect_season(season_str)
    print(f"  Computing platoon splits from play-by-play for {season_year}...")

    # Result types → counting stat mapping
    HIT_RESULTS = {"SINGLE", "DOUBLE", "TRIPLE", "HOMERUN"}
    OUT_RESULTS = {"FLYOUT", "GROUNDOUT", "LINEOUT", "POPOUT", "DOUBLE_PLAY"}
    # These count as PA but not AB:
    NON_AB_RESULTS = {"WALK", "HIT_BY_PITCH", "SACRIFICE_FLY", "SACRIFICE_BUNT",
                      "CATCHER_INTERFERENCE"}

    # Get all game IDs for this season
    data = msf_get(f"{season_str}/games.json")
    if not data:
        print("    No games found")
        return 0, 0
    games = [g["schedule"]["id"] for g in data.get("games", [])
             if g.get("schedule", {}).get("playedStatus") == "COMPLETED"]
    print(f"    Found {len(games)} completed games to process")

    # Accumulators: {(player_name, pitcher_hand): {stats}}
    # We use MSF player IDs, then resolve to Retrosheet IDs at insert time
    batting_splits = {}   # (msf_batter_id, batter_name, pitcher_hand) → stats
    pitching_splits = {}  # (msf_pitcher_id, pitcher_name, batter_hand) → stats

    def empty_bat_stats():
        return {"pa": 0, "ab": 0, "h": 0, "2b": 0, "3b": 0, "hr": 0,
                "rbi": 0, "bb": 0, "so": 0, "hbp": 0, "sf": 0}

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

        for ab in pbp.get("atBats", []):
            for play in ab.get("atBatPlay", []):
                if not isinstance(play, dict):
                    continue
                bu = play.get("batterUp", {})
                if not bu or not isinstance(bu, dict) or not bu.get("result"):
                    continue

                result = bu["result"]
                batter_info = bu.get("battingPlayer", {})
                batter_id = batter_info.get("id")
                batter_name = f"{batter_info.get('firstName', '')} {batter_info.get('lastName', '')}".strip()

                # Get pitcher from playStatus
                ps = play.get("playStatus", {})
                pitcher_info = ps.get("pitcher", {})
                pitcher_id = pitcher_info.get("id")
                pitcher_name = f"{pitcher_info.get('firstName', '')} {pitcher_info.get('lastName', '')}".strip()

                if not batter_id or not pitcher_id:
                    continue

                # Get handedness from the pitch data or batterUp
                pitcher_hand = None
                batter_hand = bu.get("standingLeftOrRight")

                # Look through plays for pitch data with throwingLeftOrRight
                for p2 in ab.get("atBatPlay", []):
                    pitch_data = p2.get("pitch", {})
                    if pitch_data and pitch_data.get("throwingLeftOrRight"):
                        pitcher_hand = pitch_data["throwingLeftOrRight"]
                        break

                if not pitcher_hand or not batter_hand:
                    continue

                # --- Batting platoon: batter vs pitcher_hand ---
                bkey = (batter_id, batter_name, pitcher_hand)
                if bkey not in batting_splits:
                    batting_splits[bkey] = empty_bat_stats()
                bs = batting_splits[bkey]

                is_hit = result in HIT_RESULTS
                is_out = result in OUT_RESULTS or result == "STRIKEOUT"
                is_ab = is_hit or is_out  # AB = H + outs (excl walks, HBP, SF)

                bs["pa"] += 1
                if is_ab:
                    bs["ab"] += 1
                if is_hit:
                    bs["h"] += 1
                if result == "DOUBLE":
                    bs["2b"] += 1
                elif result == "TRIPLE":
                    bs["3b"] += 1
                elif result == "HOMERUN":
                    bs["hr"] += 1
                if result == "WALK":
                    bs["bb"] += 1
                if result == "STRIKEOUT":
                    bs["so"] += 1
                if result == "HIT_BY_PITCH":
                    bs["hbp"] += 1
                if result == "SACRIFICE_FLY":
                    bs["sf"] += 1

                # --- Pitching platoon: pitcher vs batter_hand ---
                pkey = (pitcher_id, pitcher_name, batter_hand)
                if pkey not in pitching_splits:
                    pitching_splits[pkey] = empty_bat_stats()
                ps_stats = pitching_splits[pkey]

                ps_stats["pa"] += 1
                if is_ab:
                    ps_stats["ab"] += 1
                if is_hit:
                    ps_stats["h"] += 1
                if result == "DOUBLE":
                    ps_stats["2b"] += 1
                elif result == "TRIPLE":
                    ps_stats["3b"] += 1
                elif result == "HOMERUN":
                    ps_stats["hr"] += 1
                if result == "WALK":
                    ps_stats["bb"] += 1
                if result == "STRIKEOUT":
                    ps_stats["so"] += 1
                if result == "HIT_BY_PITCH":
                    ps_stats["hbp"] += 1
                if result == "SACRIFICE_FLY":
                    ps_stats["sf"] += 1

        if (i + 1) % 50 == 0:
            print(f"    Processed {i + 1}/{len(games)} games...")

    print(f"    Processed all {len(games)} games: {len(batting_splits)} batter splits, {len(pitching_splits)} pitcher splits")

    # Now insert into tables, resolving MSF names to Retrosheet player IDs
    cursor = conn.cursor()
    cursor.execute("DELETE FROM platoon_splits WHERE season = ?", (season_year,))
    cursor.execute("DELETE FROM pitching_platoon_splits WHERE season = ?", (season_year,))

    bat_count = 0
    for (msf_id, name, pitcher_hand), stats in batting_splits.items():
        # Resolve player ID by name
        cursor.execute("SELECT player_id FROM players WHERE name = ? LIMIT 1", (name,))
        row = cursor.fetchone()
        if not row:
            continue
        pid = row[0]

        split = "vs_LHP" if pitcher_hand == "L" else "vs_RHP"
        h, ab, bb, hbp, sf = stats["h"], stats["ab"], stats["bb"], stats["hbp"], stats["sf"]
        doubles, triples, hr, so = stats["2b"], stats["3b"], stats["hr"], stats["so"]

        pa_calc, avg, obp, slg, ops, iso, babip = compute_rate_stats(
            h, ab, bb, hbp, sf, doubles, triples, hr, so
        )

        cursor.execute("""
            INSERT OR REPLACE INTO platoon_splits
            (player_id, season, split, plate_appearances, at_bats,
             hits, doubles, triples, home_runs, rbi, walks, strikeouts,
             batting_avg, obp, slg, ops, iso, babip)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            pid, season_year, split, pa_calc, ab,
            h, doubles, triples, hr, stats["rbi"], bb, so,
            avg, obp, slg, ops, iso, babip,
        ))
        bat_count += 1

    pitch_count = 0
    for (msf_id, name, batter_hand), stats in pitching_splits.items():
        cursor.execute("SELECT player_id FROM players WHERE name = ? LIMIT 1", (name,))
        row = cursor.fetchone()
        if not row:
            continue
        pid = row[0]

        split = "vs_LHB" if batter_hand == "L" else "vs_RHB"
        h, ab, bb, hbp, sf = stats["h"], stats["ab"], stats["bb"], stats["hbp"], stats["sf"]
        doubles, triples, hr, so = stats["2b"], stats["3b"], stats["hr"], stats["so"]

        # Compute batting-against rates
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
        """, (
            pid, season_year, split, stats["pa"], ab,
            h, doubles, triples, hr, bb, 0,
            so, hbp, 0, sf,
            avg_against, obp_against, slg_against, ops_against,
        ))
        pitch_count += 1

    conn.commit()
    print(f"    Generated {bat_count} batting platoon + {pitch_count} pitching platoon split rows")
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
    args = parser.parse_args()

    # Auto-detect season
    if args.season is None:
        year = date.today().year
        month = date.today().month
        if month < 3 or (month == 3 and date.today().day < 25):
            args.season = f"{year}-preseason"
        elif month >= 10:
            args.season = f"{year}-playoff"
        else:
            args.season = f"{year}-regular"
        print(f"Auto-detected season: {args.season}")

    if not MSF_API_KEY:
        print("ERROR: Set MSF_API_KEY environment variable")
        return

    print(f"Pulling live stats for {args.season} into {args.db}")
    conn = sqlite3.connect(args.db)

    try:
        t0 = time.time()
        season_year = detect_season(args.season)
        # Player info comes from stats responses (players.json requires higher tier)
        pull_season_batting(conn, args.season)
        compute_league_averages_and_ops_plus(conn, season_year)
        pull_season_pitching(conn, args.season)
        compute_pitching_league_averages(conn, season_year)
        pull_game_logs(conn, args.season)

        # Compute home/away splits from game logs
        compute_batting_home_away_splits(conn, season_year)
        compute_pitching_home_away_splits(conn, season_year)

        # Compute platoon splits from play-by-play
        compute_platoon_splits(conn, args.season)

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

        elapsed = time.time() - t0
        print(f"\nDone in {elapsed:.1f}s")
    except Exception:
        conn.close()
        raise


if __name__ == "__main__":
    main()
