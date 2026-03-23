"""
Daily games service — fetches today's MLB schedule and probable pitchers.

Uses the free MLB Stats API (statsapi.mlb.com) for probable starting pitchers.
MSF doesn't include probable pitchers in their games endpoint at DETAILS tier.

Provides `get_opponent_starter(team_code)` for resolving "how will Judge do tonight"
queries into batter-vs-pitcher matchups.

Results are cached in-memory for 1 hour to avoid repeated API calls.
"""

import os
import sqlite3
import time
from datetime import date
from typing import Optional

import requests

# MLB Stats API team ID → Retrosheet code mapping
_MLB_TEAM_TO_RETRO = {
    108: "ANA", 109: "ARI", 110: "BAL", 111: "BOS",
    112: "CHN", 113: "CIN", 114: "CLE", 115: "COL",
    116: "DET", 117: "HOU", 118: "KCA", 119: "LAN",
    120: "WAS", 121: "NYN", 133: "OAK", 134: "PIT",
    135: "SDN", 136: "SEA", 137: "SFN", 138: "SLN",
    139: "TBA", 140: "TEX", 141: "TOR", 142: "MIN",
    143: "PHI", 144: "ATL", 145: "CHA",
    146: "MIA", 147: "NYA", 158: "MIL",
}

# Also map by team name for safety
_MLB_NAME_TO_RETRO = {
    "Arizona Diamondbacks": "ARI", "Atlanta Braves": "ATL",
    "Baltimore Orioles": "BAL", "Boston Red Sox": "BOS",
    "Chicago Cubs": "CHN", "Chicago White Sox": "CHA",
    "Cincinnati Reds": "CIN", "Cleveland Guardians": "CLE",
    "Colorado Rockies": "COL", "Detroit Tigers": "DET",
    "Houston Astros": "HOU", "Kansas City Royals": "KCA",
    "Los Angeles Angels": "ANA", "Los Angeles Dodgers": "LAN",
    "Miami Marlins": "MIA", "Milwaukee Brewers": "MIL",
    "Minnesota Twins": "MIN", "New York Mets": "NYN",
    "New York Yankees": "NYA", "Oakland Athletics": "OAK",
    "Philadelphia Phillies": "PHI", "Pittsburgh Pirates": "PIT",
    "San Diego Padres": "SDN", "San Francisco Giants": "SFN",
    "Seattle Mariners": "SEA", "St. Louis Cardinals": "SLN",
    "Tampa Bay Rays": "TBA", "Texas Rangers": "TEX",
    "Toronto Blue Jays": "TOR", "Washington Nationals": "WAS",
}

# Cache: {"date": "YYYY-MM-DD", "games": [...], "fetched_at": epoch}
_cache: dict = {}
_CACHE_TTL = 3600  # 1 hour


def _team_to_retro(team_info: dict) -> str:
    """Convert MLB Stats API team info to Retrosheet code."""
    team_id = team_info.get("id", 0)
    team_name = team_info.get("name", "")
    return (_MLB_TEAM_TO_RETRO.get(team_id)
            or _MLB_NAME_TO_RETRO.get(team_name)
            or "")


def _fetch_daily_games() -> list:
    """Fetch today's games from MLB Stats API. Cached for 1 hour."""
    today_str = date.today().isoformat()
    now = time.time()

    if (_cache.get("date") == today_str
            and now - _cache.get("fetched_at", 0) < _CACHE_TTL):
        return _cache.get("games", [])

    try:
        resp = requests.get(
            "https://statsapi.mlb.com/api/v1/schedule",
            params={"sportId": 1, "hydrate": "probablePitcher", "date": today_str},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        _cache.update({"date": today_str, "games": [], "fetched_at": now})
        return []

    dates = data.get("dates", [])
    games = dates[0].get("games", []) if dates else []
    _cache.update({"date": today_str, "games": games, "fetched_at": now})
    return games


def get_todays_games() -> list:
    """Get today's games as simplified dicts.

    Each dict: {
        "away": "NYA",  (Retrosheet code)
        "home": "BOS",
        "away_starter": "Gerrit Cole" or None,
        "home_starter": "Tanner Houck" or None,
        "start_time": "2026-04-01T19:10:00Z"
    }
    """
    raw = _fetch_daily_games()
    result = []
    for game in raw:
        teams = game.get("teams", {})
        away_info = teams.get("away", {})
        home_info = teams.get("home", {})

        away_retro = _team_to_retro(away_info.get("team", {}))
        home_retro = _team_to_retro(home_info.get("team", {}))

        away_starter = None
        home_starter = None

        away_sp = away_info.get("probablePitcher", {})
        if away_sp:
            away_starter = away_sp.get("fullName")

        home_sp = home_info.get("probablePitcher", {})
        if home_sp:
            home_starter = home_sp.get("fullName")

        result.append({
            "away": away_retro,
            "home": home_retro,
            "away_starter": away_starter,
            "home_starter": home_starter,
            "start_time": game.get("gameDate", ""),
        })
    return result


def get_opponent_starter(player_team: str) -> Optional[tuple]:
    """Given a player's team (Retrosheet code), find tonight's opponent's probable starter.

    Returns (pitcher_name, opponent_team_code) or None if no game or no probable starter.
    """
    games = get_todays_games()
    for game in games:
        if game["away"] == player_team:
            starter = game["home_starter"]
            if starter:
                return (starter, game["home"])
        elif game["home"] == player_team:
            starter = game["away_starter"]
            if starter:
                return (starter, game["away"])
    return None


def has_game_today(player_team: str) -> bool:
    """Check if the team has a game scheduled today."""
    games = get_todays_games()
    return any(g["away"] == player_team or g["home"] == player_team for g in games)


def get_player_team(player_name: str) -> Optional[str]:
    """Look up a player's current team (Retrosheet code) from the DB."""
    db_path = os.getenv(
        "DB_PATH",
        os.path.join(os.path.dirname(__file__), "..", "baseball_stats_full.db"),
    )
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("SELECT team FROM players WHERE name = ?", (player_name,))
        row = cur.fetchone()
        conn.close()
        if row and row[0]:
            return row[0]
    except Exception:
        pass
    return None
