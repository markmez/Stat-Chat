"""
GET /team-card?code=NYA — structured team card data.
"""

import os
import sqlite3
from typing import Optional, List
from fastapi import APIRouter, Query
from pydantic import BaseModel

router = APIRouter()

DB_PATH = os.getenv(
    "DB_PATH",
    os.path.join(os.path.dirname(__file__), "..", "..", "baseball_stats_full.db"),
)


def _sanitize(s: str) -> str:
    return s.replace("'", "''").replace('"', '""')


def _fmt_rate(val, decimals=3):
    if val is None:
        return ".000"
    v = float(val)
    s = f"{v:.{decimals}f}"
    if decimals == 3 and s.startswith("0."):
        return s[1:]
    if decimals == 3 and s.startswith("-0."):
        return "-" + s[2:]
    return s


# --- Response models ---

class StatLeaderData(BaseModel):
    category: str
    name: str
    value: str

class RosterEntryData(BaseModel):
    name: str
    position: str

class TeamSeasonStatsData(BaseModel):
    headers: List[str]
    values: List[str]

class TeamSeasonDataResp(BaseModel):
    year: int
    batting_stats: Optional[TeamSeasonStatsData] = None
    pitching_stats: Optional[TeamSeasonStatsData] = None
    leaders: List[StatLeaderData] = []
    roster: List[RosterEntryData] = []

class TeamCardResponse(BaseModel):
    team_code: str
    full_name: str
    seasons: List[TeamSeasonDataResp] = []


# --- Team name mapping ---
_TEAM_NAMES = {
    "ANA": "Los Angeles Angels", "ARI": "Arizona Diamondbacks",
    "ATH": "Athletics", "ATL": "Atlanta Braves",
    "BAL": "Baltimore Orioles", "BOS": "Boston Red Sox",
    "CHA": "Chicago White Sox", "CHN": "Chicago Cubs",
    "CIN": "Cincinnati Reds", "CLE": "Cleveland Guardians",
    "COL": "Colorado Rockies", "DET": "Detroit Tigers",
    "HOU": "Houston Astros", "KCA": "Kansas City Royals",
    "LAN": "Los Angeles Dodgers", "MIA": "Miami Marlins",
    "MIL": "Milwaukee Brewers", "MIN": "Minnesota Twins",
    "NYA": "New York Yankees", "NYN": "New York Mets",
    "OAK": "Oakland Athletics", "PHI": "Philadelphia Phillies",
    "PIT": "Pittsburgh Pirates", "SDN": "San Diego Padres",
    "SEA": "Seattle Mariners", "SFN": "San Francisco Giants",
    "SLN": "St. Louis Cardinals", "TBA": "Tampa Bay Rays",
    "TEX": "Texas Rangers", "TOR": "Toronto Blue Jays",
    "WAS": "Washington Nationals",
}


@router.get("/team-card")
async def team_card(
    code: str = Query(..., description="Retrosheet team code (e.g., NYA)"),
):
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        full_name = _TEAM_NAMES.get(code, code)
        safe = _sanitize(code)
        team_filter = f"(s.team = '{safe}' OR s.team LIKE '{safe}/%' OR s.team LIKE '%/{safe}')"
        pitch_filter = f"(sp.team = '{safe}' OR sp.team LIKE '{safe}/%' OR sp.team LIKE '%/{safe}')"

        # Get seasons
        years = [r[0] for r in conn.execute(f"""
            SELECT DISTINCT s.season FROM season_batting_stats s
            WHERE {team_filter} ORDER BY s.season DESC
        """).fetchall()]

        seasons = []
        for year in years:
            # Batting aggregates
            agg = conn.execute(f"""
                SELECT SUM(s.runs), SUM(s.hits), SUM(s.doubles), SUM(s.triples),
                       SUM(s.home_runs), SUM(s.rbi), SUM(s.stolen_bases),
                       SUM(s.walks), SUM(s.strikeouts), SUM(s.at_bats)
                FROM season_batting_stats s WHERE {team_filter} AND s.season = ?
            """, (year,)).fetchone()

            batting_stats = None
            if agg and agg[9] and int(agg[9]) > 0:
                ab = float(agg[9])
                h = float(agg[1])
                bb = float(agg[7])
                d, t, hr = float(agg[2]), float(agg[3]), float(agg[4])
                avg = h / ab if ab > 0 else 0
                pa = ab + bb
                obp = (h + bb) / pa if pa > 0 else 0
                tb = h + d + 2 * t + 3 * hr
                slg = tb / ab if ab > 0 else 0
                ops = obp + slg

                batting_stats = TeamSeasonStatsData(
                    headers=["R", "H", "2B", "3B", "HR", "RBI", "SB", "BB", "SO", "AVG", "OBP", "SLG", "OPS"],
                    values=[str(int(agg[0] or 0)), str(int(agg[1] or 0)),
                            str(int(agg[2] or 0)), str(int(agg[3] or 0)),
                            str(int(agg[4] or 0)), str(int(agg[5] or 0)),
                            str(int(agg[6] or 0)), str(int(agg[7] or 0)),
                            str(int(agg[8] or 0)),
                            _fmt_rate(avg), _fmt_rate(obp), _fmt_rate(slg), _fmt_rate(ops)]
                )

            # Pitching aggregates
            pagg = conn.execute(f"""
                SELECT SUM(sp.wins), SUM(sp.losses), SUM(sp.saves),
                       SUM(sp.ip_outs), SUM(sp.hits), SUM(sp.earned_runs),
                       SUM(sp.walks), SUM(sp.strikeouts), SUM(sp.home_runs)
                FROM season_pitching_stats sp WHERE {pitch_filter} AND sp.season = ?
            """, (year,)).fetchone()

            pitching_stats = None
            if pagg and pagg[3] and int(pagg[3]) > 0:
                ip_outs = float(pagg[3])
                er = float(pagg[5] or 0)
                ph = float(pagg[4] or 0)
                pbb = float(pagg[6] or 0)
                era = (er * 9.0) / (ip_outs / 3.0) if ip_outs > 0 else 0
                whip = (ph + pbb) / (ip_outs / 3.0) if ip_outs > 0 else 0

                pitching_stats = TeamSeasonStatsData(
                    headers=["W", "L", "SV", "HR", "BB", "SO", "ERA", "WHIP"],
                    values=[str(int(pagg[0] or 0)), str(int(pagg[1] or 0)),
                            str(int(pagg[2] or 0)), str(int(pagg[8] or 0)),
                            str(int(pagg[6] or 0)), str(int(pagg[7] or 0)),
                            f"{era:.2f}", f"{whip:.2f}"]
                )

            # Batting leaders
            leaders = []
            bat_cats = [
                ("HR", "home_runs", False), ("SB", "stolen_bases", False),
                ("H", "hits", False), ("AVG", "batting_avg", True),
                ("OBP", "obp", True), ("OPS", "ops", True),
            ]
            for label, col, is_rate in bat_cats:
                rows = conn.execute(f"""
                    SELECT p.name, s.{col} FROM season_batting_stats s
                    JOIN players p ON s.player_id = p.player_id
                    WHERE {team_filter} AND s.season = ? AND s.plate_appearances >= 50
                    ORDER BY s.{col} DESC LIMIT 20
                """, (year,)).fetchall()
                for name, val in rows:
                    v = _fmt_rate(val) if is_rate else str(int(val)) if val else "0"
                    leaders.append(StatLeaderData(category=label, name=name, value=v))

            # Pitching leaders
            pitch_cats = [
                ("W", "wins", False, False), ("SV", "saves", False, False),
                ("SO", "strikeouts", False, False), ("ERA", "era", True, True),
            ]
            for label, col, asc, min_ip in pitch_cats:
                ip_filter = "AND sp.ip_outs >= 54" if min_ip else ""
                order = "ASC" if asc else "DESC"
                rows = conn.execute(f"""
                    SELECT p.name, sp.{col} FROM season_pitching_stats sp
                    JOIN players p ON sp.player_id = p.player_id
                    WHERE {pitch_filter} AND sp.season = ? {ip_filter}
                    ORDER BY sp.{col} {order} LIMIT 20
                """, (year,)).fetchall()
                for name, val in rows:
                    v = f"{float(val):.2f}" if asc else str(int(val)) if val else "0"
                    leaders.append(StatLeaderData(category=label, name=name, value=v))

            # Roster
            roster = []
            roster_rows = conn.execute(f"""
                SELECT p.name, p.positions FROM season_batting_stats s
                JOIN players p ON s.player_id = p.player_id
                WHERE {team_filter} AND s.season = ?
                ORDER BY s.plate_appearances DESC
            """, (year,)).fetchall()
            for name, pos in roster_rows:
                roster.append(RosterEntryData(name=name, position=pos or ""))

            seasons.append(TeamSeasonDataResp(
                year=year,
                batting_stats=batting_stats,
                pitching_stats=pitching_stats,
                leaders=leaders,
                roster=roster,
            ))

        return TeamCardResponse(team_code=code, full_name=full_name, seasons=seasons)
    finally:
        conn.close()
