"""Stat leaders endpoint — current season leaderboards for the feed drawer."""

import os
import sqlite3
from datetime import date

from fastapi import APIRouter, Query as QueryParam

router = APIRouter()

DB_PATH = os.getenv("DB_PATH", "/data/baseball_stats_full.db")

# Retrosheet team code → league
_AL_TEAMS = {"NYA", "BOS", "BAL", "TBA", "TOR", "CLE", "CHA", "DET", "KCA", "MIN",
             "HOU", "ANA", "SEA", "TEX", "OAK"}
_NL_TEAMS = {"NYN", "ATL", "MIA", "PHI", "WAS", "CHN", "CIN", "MIL", "PIT", "SLN",
             "ARI", "COL", "LAN", "SDN", "SFN"}

BATTING_STATS = [
    {"col": "home_runs", "label": "Home Runs", "min_pa": 0, "is_rate": False, "order": "DESC"},
    {"col": "batting_avg", "label": "Batting Avg", "min_pa": 100, "is_rate": True, "order": "DESC"},
    {"col": "rbi", "label": "RBI", "min_pa": 0, "is_rate": False, "order": "DESC"},
    {"col": "hits", "label": "Hits", "min_pa": 0, "is_rate": False, "order": "DESC"},
    {"col": "stolen_bases", "label": "Stolen Bases", "min_pa": 0, "is_rate": False, "order": "DESC"},
    {"col": "runs", "label": "Runs", "min_pa": 0, "is_rate": False, "order": "DESC"},
    {"col": "ops", "label": "OPS", "min_pa": 100, "is_rate": True, "order": "DESC"},
]

PITCHING_STATS = [
    {"col": "era", "label": "ERA", "min_ip": 20, "is_rate": True, "order": "ASC"},
    {"col": "wins", "label": "Wins", "min_ip": 0, "is_rate": False, "order": "DESC"},
    {"col": "strikeouts", "label": "Strikeouts", "min_ip": 0, "is_rate": False, "order": "DESC"},
    {"col": "saves", "label": "Saves", "min_ip": 0, "is_rate": False, "order": "DESC"},
    {"col": "whip", "label": "WHIP", "min_pa": 20, "is_rate": True, "order": "ASC"},
]


def _format_rate(val, pitching=False):
    if val is None:
        return "--"
    if isinstance(val, float):
        if pitching:
            # ERA, WHIP: 2 decimals, keep leading digit
            return f"{val:.2f}"
        if val >= 10:
            return f"{val:.2f}"
        s = f"{val:.3f}"
        return s.lstrip("0") if s.startswith("0.") else s
    return str(val)


@router.get("/leaders")
async def get_leaders(
    league: str = QueryParam("MLB", regex="^(MLB|AL|NL)$"),
    limit: int = QueryParam(10, le=50),
):
    """Return current season stat leaders for the feed drawer."""
    season = date.today().year
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        # Determine league filter
        if league == "AL":
            teams = _AL_TEAMS
        elif league == "NL":
            teams = _NL_TEAMS
        else:
            teams = None

        batting_team_filter = ""
        pitching_team_filter = ""
        if teams:
            placeholders = ",".join(f"'{t}'" for t in teams)
            batting_team_filter = f" AND s.team IN ({placeholders})"
            pitching_team_filter = f" AND sp.team IN ({placeholders})"

        # Prorated PA minimum: (team_games / 162) * 502
        # Get max team games played
        max_games = conn.execute(
            "SELECT MAX(games) FROM season_batting_stats WHERE season = ?",
            (season,),
        ).fetchone()
        team_games = max_games[0] if max_games and max_games[0] else 10
        prorated_pa = int((team_games / 162) * 502)

        # Same for IP: (team_games / 162) * 162
        prorated_ip_outs = int((team_games / 162) * 162 * 3)

        batting = []
        for stat in BATTING_STATS:
            min_pa = prorated_pa if stat["is_rate"] else 0
            pa_filter = f" AND s.plate_appearances >= {min_pa}" if min_pa > 0 else ""

            rows = conn.execute(f"""
                SELECT p.name, s.{stat['col']}, s.team
                FROM season_batting_stats s
                JOIN players p ON s.player_id = p.player_id
                WHERE s.season = ?{batting_team_filter}{pa_filter}
                  AND s.{stat['col']} IS NOT NULL
                ORDER BY s.{stat['col']} {stat['order']}
                LIMIT ?
            """, (season, limit)).fetchall()

            leaders = []
            for name, val, team in rows:
                display_val = _format_rate(val) if stat["is_rate"] else str(val)
                leaders.append({"name": name, "value": display_val, "team": team})

            batting.append({"stat": stat["label"], "leaders": leaders})

        pitching = []
        for stat in PITCHING_STATS:
            min_ip = prorated_ip_outs if stat["is_rate"] else 0
            ip_filter = f" AND sp.ip_outs >= {min_ip}" if min_ip > 0 else ""

            rows = conn.execute(f"""
                SELECT p.name, sp.{stat['col']}, sp.team
                FROM season_pitching_stats sp
                JOIN players p ON sp.player_id = p.player_id
                WHERE sp.season = ?{pitching_team_filter}{ip_filter}
                  AND sp.{stat['col']} IS NOT NULL
                ORDER BY sp.{stat['col']} {stat['order']}
                LIMIT ?
            """, (season, limit)).fetchall()

            leaders = []
            for name, val, team in rows:
                display_val = _format_rate(val, pitching=True) if stat["is_rate"] else str(val)
                leaders.append({"name": name, "value": display_val, "team": team})

            pitching.append({"stat": stat["label"], "leaders": leaders})

        return {
            "season": season,
            "league": league,
            "batting": batting,
            "pitching": pitching,
        }
    finally:
        conn.close()
