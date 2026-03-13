"""
GET /player-card?name=... — structured player card data.

Returns JSON with player info, batting seasons, career totals,
pitching data (if applicable), and platoon splits — all from the
full historical database.
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


def _get_conn():
    return sqlite3.connect(DB_PATH)


def _sanitize(name: str) -> str:
    return name.replace("'", "''").replace('"', '""')


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

class PlayerInfo(BaseModel):
    name: str
    team: str
    birthdate: Optional[str] = None
    bats: Optional[str] = None
    throws: Optional[str] = None
    positions: Optional[str] = None


class BattingSeason(BaseModel):
    year: int
    team: str
    age: int
    G: int
    AB: int
    R: int
    H: int
    doubles: int  # "2B"
    triples: int  # "3B"
    HR: int
    RBI: int
    SB: int
    CS: int
    BB: int
    IBB: int
    SO: int
    HBP: int
    AVG: str
    OBP: str
    SLG: str
    OPS: str
    OPS_plus: str
    ISO: str
    BABIP: str


class PitchingSeason(BaseModel):
    year: int
    team: str
    W: int
    L: int
    SV: int
    G: int
    GS: int
    GF: int
    CG: int
    QS: int
    IP: str
    H: int
    R: int
    ER: int
    HR: int
    BB: int
    IBB: int
    SO: int
    HBP: int
    WP: int
    BK: int
    BF: int
    SH: int
    SF: int
    SB_allowed: int
    CS_allowed: int
    ERA: str
    WHIP: str
    K9: str
    BB9: str
    K_BB: str
    H9: str
    HR9: str
    BAA: str
    ERA_plus: str


class SplitRow(BaseModel):
    label: str
    values: List[str]


class SplitGrid(BaseModel):
    headers: List[str]
    rows: List[SplitRow]


class PlayerCardResponse(BaseModel):
    player_info: Optional[PlayerInfo] = None
    batting_seasons: List[BattingSeason] = []
    pitching_seasons: List[PitchingSeason] = []
    is_pitcher: bool = False
    is_two_way: bool = False
    career_platoon_splits: Optional[SplitGrid] = None
    career_home_away_splits: Optional[SplitGrid] = None
    pitching_career_platoon_splits: Optional[SplitGrid] = None
    pitching_career_home_away_splits: Optional[SplitGrid] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fetch_player_info(conn: sqlite3.Connection, name: str) -> Optional[PlayerInfo]:
    cur = conn.cursor()
    cur.execute(
        "SELECT name, team, birthdate, bats, throws, positions FROM players "
        "WHERE name = ? LIMIT 1",
        (_sanitize(name),),
    )
    row = cur.fetchone()
    if not row:
        return None
    return PlayerInfo(
        name=row[0],
        team=row[1] or "",
        birthdate=row[2] if row[2] else None,
        bats=row[3] if row[3] else None,
        throws=row[4] if row[4] else None,
        positions=row[5] if row[5] else None,
    )


def _safe_int(val) -> int:
    if val is None:
        return 0
    try:
        return int(val)
    except (ValueError, TypeError):
        return 0


def _safe_str(val, decimals: int = 3) -> str:
    if val is None:
        return "--"
    try:
        f = float(val)
        return f"{f:.{decimals}f}"
    except (ValueError, TypeError):
        return str(val) if val else "--"


def _fetch_batting_seasons(conn: sqlite3.Connection, name: str) -> List[BattingSeason]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT s.season, s.team, s.age,
               s.games, s.at_bats, s.runs, s.hits,
               s.doubles, s.triples, s.home_runs, s.rbi,
               s.stolen_bases, s.caught_stealing,
               s.walks, s.intentional_walks, s.strikeouts, s.hit_by_pitch,
               s.batting_avg, s.obp, s.slg, s.ops, s.ops_plus, s.iso, s.babip
        FROM season_batting_stats s
        JOIN players p ON s.player_id = p.player_id
        WHERE p.name = ?
        ORDER BY s.season DESC
        """,
        (_sanitize(name),),
    )
    rows = cur.fetchall()
    seasons = []
    for r in rows:
        seasons.append(BattingSeason(
            year=_safe_int(r[0]),
            team=r[1] or "",
            age=_safe_int(r[2]),
            G=_safe_int(r[3]),
            AB=_safe_int(r[4]),
            R=_safe_int(r[5]),
            H=_safe_int(r[6]),
            doubles=_safe_int(r[7]),
            triples=_safe_int(r[8]),
            HR=_safe_int(r[9]),
            RBI=_safe_int(r[10]),
            SB=_safe_int(r[11]),
            CS=_safe_int(r[12]),
            BB=_safe_int(r[13]),
            IBB=_safe_int(r[14]),
            SO=_safe_int(r[15]),
            HBP=_safe_int(r[16]),
            AVG=_safe_str(r[17]),
            OBP=_safe_str(r[18]),
            SLG=_safe_str(r[19]),
            OPS=_safe_str(r[20]),
            OPS_plus=_safe_str(r[21], 0) if r[21] else "--",
            ISO=_safe_str(r[22]),
            BABIP=_safe_str(r[23]),
        ))
    return seasons


def _fetch_pitching_seasons(conn: sqlite3.Connection, name: str) -> List[PitchingSeason]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT sp.season, sp.team,
               sp.wins, sp.losses, sp.saves, sp.games, sp.games_started,
               sp.games_finished, sp.complete_games, sp.quality_starts,
               sp.innings_pitched,
               sp.hits, sp.runs, sp.earned_runs, sp.home_runs,
               sp.walks, sp.intentional_walks, sp.strikeouts, sp.hit_by_pitch,
               sp.wild_pitches, sp.balks, sp.batters_faced,
               sp.sacrifice_hits, sp.sacrifice_flies,
               sp.stolen_bases, sp.caught_stealing,
               sp.era, sp.whip, sp.k_per_9, sp.bb_per_9, sp.k_per_bb,
               sp.h_per_9, sp.hr_per_9, sp.baa, sp.era_plus
        FROM season_pitching_stats sp
        JOIN players p ON sp.player_id = p.player_id
        WHERE p.name = ?
        ORDER BY sp.season DESC
        """,
        (_sanitize(name),),
    )
    rows = cur.fetchall()
    seasons = []
    for r in rows:
        seasons.append(PitchingSeason(
            year=_safe_int(r[0]),
            team=r[1] or "",
            W=_safe_int(r[2]),
            L=_safe_int(r[3]),
            SV=_safe_int(r[4]),
            G=_safe_int(r[5]),
            GS=_safe_int(r[6]),
            GF=_safe_int(r[7]),
            CG=_safe_int(r[8]),
            QS=_safe_int(r[9]),
            IP=_safe_str(r[10], 1) if r[10] else "0.0",
            H=_safe_int(r[11]),
            R=_safe_int(r[12]),
            ER=_safe_int(r[13]),
            HR=_safe_int(r[14]),
            BB=_safe_int(r[15]),
            IBB=_safe_int(r[16]),
            SO=_safe_int(r[17]),
            HBP=_safe_int(r[18]),
            WP=_safe_int(r[19]),
            BK=_safe_int(r[20]),
            BF=_safe_int(r[21]),
            SH=_safe_int(r[22]),
            SF=_safe_int(r[23]),
            SB_allowed=_safe_int(r[24]),
            CS_allowed=_safe_int(r[25]),
            ERA=_safe_str(r[26], 2),
            WHIP=_safe_str(r[27], 2),
            K9=_safe_str(r[28], 1),
            BB9=_safe_str(r[29], 1),
            K_BB=_safe_str(r[30], 2),
            H9=_safe_str(r[31], 1),
            HR9=_safe_str(r[32], 1),
            BAA=_safe_str(r[33]),
            ERA_plus=_safe_str(r[34], 0) if r[34] else "--",
        ))
    return seasons


def _is_pitcher(conn: sqlite3.Connection, name: str) -> bool:
    cur = conn.cursor()
    cur.execute(
        "SELECT positions FROM players WHERE name = ? LIMIT 1",
        (_sanitize(name),),
    )
    row = cur.fetchone()
    if not row or not row[0] or not row[0].startswith("P"):
        return False
    cur.execute(
        "SELECT 1 FROM season_pitching_stats sp "
        "JOIN players p ON sp.player_id = p.player_id "
        "WHERE p.name = ? LIMIT 1",
        (_sanitize(name),),
    )
    return cur.fetchone() is not None


def _is_two_way(conn: sqlite3.Connection, name: str) -> bool:
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM season_batting_stats s "
        "JOIN players p ON s.player_id = p.player_id "
        "WHERE p.name = ? AND s.plate_appearances >= 130 LIMIT 1",
        (_sanitize(name),),
    )
    has_bat = cur.fetchone() is not None
    if not has_bat:
        return False
    cur.execute(
        "SELECT 1 FROM season_pitching_stats sp "
        "JOIN players p ON sp.player_id = p.player_id "
        "WHERE p.name = ? AND sp.ip_outs >= 90 LIMIT 1",
        (_sanitize(name),),
    )
    return cur.fetchone() is not None


def _fetch_career_platoon_splits(conn: sqlite3.Connection, name: str) -> Optional[SplitGrid]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT ps.split,
               SUM(ps.at_bats), SUM(ps.hits),
               SUM(ps.doubles), SUM(ps.triples), SUM(ps.home_runs),
               SUM(ps.rbi), SUM(ps.walks), SUM(ps.strikeouts),
               ROUND(CAST(SUM(ps.hits) AS REAL) / NULLIF(SUM(ps.at_bats), 0), 3),
               ROUND(CAST(SUM(ps.hits) + SUM(ps.walks) AS REAL) /
                     NULLIF(SUM(ps.plate_appearances), 0), 3),
               ROUND(CAST(SUM(ps.hits - ps.doubles - ps.triples - ps.home_runs) +
                          2 * SUM(ps.doubles) + 3 * SUM(ps.triples) + 4 * SUM(ps.home_runs) AS REAL) /
                     NULLIF(SUM(ps.at_bats), 0), 3)
        FROM platoon_splits ps
        JOIN players p ON ps.player_id = p.player_id
        WHERE p.name = ?
        GROUP BY ps.split
        HAVING COUNT(DISTINCT ps.season) > 1
        ORDER BY ps.split
        """,
        (_sanitize(name),),
    )
    rows = cur.fetchall()
    if not rows:
        return None
    headers = ["AB", "H", "2B", "3B", "HR", "RBI", "BB", "SO", "AVG", "OBP", "SLG", "OPS", "ISO", "BABIP"]
    grid_rows = []
    for r in rows:
        label = "vs LHP" if r[0] == "vs_LHP" else "vs RHP"
        vals = [str(v) if v is not None else "0" for v in r[1:]]
        obp = float(vals[9]) if vals[9] != "0" else 0
        slg = float(vals[10]) if vals[10] != "0" else 0
        avg = float(vals[8]) if vals[8] != "0" else 0
        vals.append(f"{obp + slg:.3f}")
        vals.append(f"{slg - avg:.3f}")
        h = float(r[2] or 0)
        hr = float(r[5] or 0)
        ab = float(r[1] or 0)
        so = float(r[8] or 0)
        denom = ab - so - hr
        vals.append(f"{(h - hr) / denom:.3f}" if denom > 0 else ".000")
        grid_rows.append(SplitRow(label=label, values=vals))
    return SplitGrid(headers=headers, rows=grid_rows)


def _fetch_career_home_away_splits(conn: sqlite3.Connection, name: str) -> Optional[SplitGrid]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT has.split,
               SUM(has.games), SUM(has.at_bats), SUM(has.runs), SUM(has.hits),
               SUM(has.doubles), SUM(has.triples), SUM(has.home_runs),
               SUM(has.rbi), SUM(has.walks), SUM(has.strikeouts),
               ROUND(CAST(SUM(has.hits) AS REAL) / NULLIF(SUM(has.at_bats), 0), 3),
               ROUND(CAST(SUM(has.hits) + SUM(has.walks) + SUM(has.hit_by_pitch) AS REAL) /
                     NULLIF(SUM(has.at_bats) + SUM(has.walks) + SUM(has.hit_by_pitch) + SUM(has.sacrifice_flies), 0), 3),
               ROUND(CAST(SUM(has.hits - has.doubles - has.triples - has.home_runs) +
                          2 * SUM(has.doubles) + 3 * SUM(has.triples) + 4 * SUM(has.home_runs) AS REAL) /
                     NULLIF(SUM(has.at_bats), 0), 3)
        FROM home_away_splits has
        JOIN players p ON has.player_id = p.player_id
        WHERE p.name = ?
        GROUP BY has.split
        HAVING COUNT(DISTINCT has.season) > 1
        ORDER BY has.split DESC
        """,
        (_sanitize(name),),
    )
    rows = cur.fetchall()
    if not rows:
        return None
    headers = ["G", "AB", "R", "H", "2B", "3B", "HR", "RBI", "BB", "SO", "AVG", "OBP", "SLG", "OPS", "ISO", "BABIP"]
    grid_rows = []
    for r in rows:
        label = "Home" if r[0] == "home" else "Away"
        vals = [str(v) if v is not None else "0" for v in r[1:]]
        obp = float(vals[11]) if vals[11] != "0" else 0
        slg = float(vals[12]) if vals[12] != "0" else 0
        avg = float(vals[10]) if vals[10] != "0" else 0
        vals.append(f"{obp + slg:.3f}")
        vals.append(f"{slg - avg:.3f}")
        h = float(r[4] or 0)
        hr = float(r[7] or 0)
        ab = float(r[2] or 0)
        so = float(r[10] or 0)
        denom = ab - so - hr
        vals.append(f"{(h - hr) / denom:.3f}" if denom > 0 else ".000")
        grid_rows.append(SplitRow(label=label, values=vals))
    return SplitGrid(headers=headers, rows=grid_rows)


def _fetch_pitching_career_platoon_splits(conn: sqlite3.Connection, name: str) -> Optional[SplitGrid]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT pps.split,
               SUM(pps.at_bats), SUM(pps.hits),
               SUM(pps.doubles), SUM(pps.triples), SUM(pps.home_runs),
               SUM(pps.walks), SUM(pps.strikeouts),
               ROUND(CAST(SUM(pps.hits) AS REAL) / NULLIF(SUM(pps.at_bats), 0), 3),
               ROUND(CAST(SUM(pps.hits) + SUM(pps.walks) + SUM(pps.hit_by_pitch) AS REAL) /
                     NULLIF(SUM(pps.at_bats) + SUM(pps.walks) + SUM(pps.hit_by_pitch) + SUM(pps.sacrifice_flies), 0), 3),
               ROUND(CAST(SUM(pps.hits - pps.doubles - pps.triples - pps.home_runs) +
                          2 * SUM(pps.doubles) + 3 * SUM(pps.triples) + 4 * SUM(pps.home_runs) AS REAL) /
                     NULLIF(SUM(pps.at_bats), 0), 3),
               ROUND(CAST(SUM(pps.hits) + SUM(pps.walks) + SUM(pps.hit_by_pitch) AS REAL) /
                     NULLIF(SUM(pps.at_bats) + SUM(pps.walks) + SUM(pps.hit_by_pitch) + SUM(pps.sacrifice_flies), 0) +
                     CAST(SUM(pps.hits - pps.doubles - pps.triples - pps.home_runs) +
                          2 * SUM(pps.doubles) + 3 * SUM(pps.triples) + 4 * SUM(pps.home_runs) AS REAL) /
                     NULLIF(SUM(pps.at_bats), 0), 3)
        FROM pitching_platoon_splits pps
        JOIN players p ON pps.player_id = p.player_id
        WHERE p.name = ?
        GROUP BY pps.split
        HAVING COUNT(DISTINCT pps.season) > 1
        ORDER BY pps.split
        """,
        (_sanitize(name),),
    )
    rows = cur.fetchall()
    if not rows:
        return None
    headers = ["AB", "H", "2B", "3B", "HR", "BB", "SO", "AVG", "OBP", "SLG", "OPS"]
    grid_rows = []
    for r in rows:
        label = "vs LHB" if r[0] == "vs_LHB" else "vs RHB"
        vals = [_safe_str(v) for v in r[1:]]
        grid_rows.append(SplitRow(label=label, values=vals))
    return SplitGrid(headers=headers, rows=grid_rows)


def _fetch_pitching_career_home_away_splits(conn: sqlite3.Connection, name: str) -> Optional[SplitGrid]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT phas.split,
               SUM(phas.games), SUM(phas.games_started),
               CAST(SUM(phas.ip_outs) / 3 AS TEXT) || '.' || CAST(SUM(phas.ip_outs) %% 3 AS TEXT),
               SUM(phas.hits), SUM(phas.earned_runs), SUM(phas.home_runs),
               SUM(phas.walks), SUM(phas.strikeouts),
               ROUND(9.0 * CAST(SUM(phas.earned_runs) AS REAL) / NULLIF(SUM(phas.ip_outs) / 3.0, 0), 2),
               ROUND(CAST(SUM(phas.walks) + SUM(phas.hits) AS REAL) / NULLIF(SUM(phas.ip_outs) / 3.0, 0), 2),
               ROUND(9.0 * CAST(SUM(phas.strikeouts) AS REAL) / NULLIF(SUM(phas.ip_outs) / 3.0, 0), 1),
               ROUND(9.0 * CAST(SUM(phas.walks) AS REAL) / NULLIF(SUM(phas.ip_outs) / 3.0, 0), 1),
               ROUND(CAST(SUM(phas.hits) AS REAL) / NULLIF(SUM(phas.games) * 3, 0), 3)
        FROM pitching_home_away_splits phas
        JOIN players p ON phas.player_id = p.player_id
        WHERE p.name = ?
        GROUP BY phas.split
        HAVING COUNT(DISTINCT phas.season) > 1
        ORDER BY phas.split DESC
        """,
        (_sanitize(name),),
    )
    rows = cur.fetchall()
    if not rows:
        return None
    headers = ["G", "GS", "IP", "H", "ER", "HR", "BB", "SO", "ERA", "WHIP", "K/9", "BB/9", "BAA"]
    grid_rows = []
    for r in rows:
        label = "Home" if r[0] == "home" else "Away"
        vals = [_safe_str(v) for v in r[1:]]
        grid_rows.append(SplitRow(label=label, values=vals))
    return SplitGrid(headers=headers, rows=grid_rows)


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.get("/player-card")
async def player_card(name: str = Query(..., description="Player name to look up")):
    conn = _get_conn()
    try:
        info = _fetch_player_info(conn, name)
        batting = _fetch_batting_seasons(conn, name)
        pitcher = _is_pitcher(conn, name)
        two_way = not pitcher and _is_two_way(conn, name)
        pitching = _fetch_pitching_seasons(conn, name) if (pitcher or two_way) else []

        career_platoon = _fetch_career_platoon_splits(conn, name)
        career_home_away = _fetch_career_home_away_splits(conn, name)
        pitching_career_platoon = _fetch_pitching_career_platoon_splits(conn, name) if (pitcher or two_way) else None
        pitching_career_home_away = _fetch_pitching_career_home_away_splits(conn, name) if (pitcher or two_way) else None

        return PlayerCardResponse(
            player_info=info,
            batting_seasons=batting,
            pitching_seasons=pitching,
            is_pitcher=pitcher,
            is_two_way=two_way,
            career_platoon_splits=career_platoon,
            career_home_away_splits=career_home_away,
            pitching_career_platoon_splits=pitching_career_platoon,
            pitching_career_home_away_splits=pitching_career_home_away,
        )
    finally:
        conn.close()
