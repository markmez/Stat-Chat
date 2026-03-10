"""
Stats endpoints — leaderboards, thresholds, milestones.

These endpoints run structured SQL queries against the full historical DB,
replacing Claude for queries the app can compute directly.
"""

import os
import sqlite3
from typing import Optional, List
from fastapi import APIRouter, Query as QueryParam
from pydantic import BaseModel

router = APIRouter()

DB_PATH = os.getenv(
    "DB_PATH",
    os.path.join(os.path.dirname(__file__), "..", "..", "baseball_stats_full.db"),
)

# Whitelist of valid DB columns to prevent SQL injection
BATTING_COLUMNS = {
    "games", "at_bats", "runs", "hits", "doubles", "triples",
    "home_runs", "rbi", "stolen_bases", "caught_stealing",
    "walks", "intentional_walks", "strikeouts", "hit_by_pitch",
    "batting_avg", "obp", "slg", "ops", "ops_plus", "iso", "babip",
    "plate_appearances",
}

PITCHING_COLUMNS = {
    "wins", "losses", "saves", "games", "games_started", "games_finished",
    "complete_games", "quality_starts", "ip_outs", "innings_pitched",
    "hits", "runs", "earned_runs", "home_runs", "walks", "intentional_walks",
    "strikeouts", "hit_by_pitch", "wild_pitches", "balks",
    "batters_faced", "sacrifice_hits", "sacrifice_flies",
    "stolen_bases_allowed", "caught_stealing_allowed",
    "era", "whip", "k_per_9", "bb_per_9", "k_bb_ratio",
    "hits_per_9", "hr_per_9", "batting_avg_against", "era_plus",
}

RATE_STATS = {
    "batting_avg", "obp", "slg", "ops", "iso", "babip",
    "era", "whip", "batting_avg_against",
}

LOWER_IS_BETTER = {"era", "whip", "bb_per_9", "hits_per_9", "hr_per_9"}


def _get_conn():
    return sqlite3.connect(DB_PATH)


def _is_valid_column(col: str) -> bool:
    return col in BATTING_COLUMNS or col in PITCHING_COLUMNS


def _is_pitching(col: str) -> bool:
    return col in PITCHING_COLUMNS and col not in BATTING_COLUMNS


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

class LeaderboardRow(BaseModel):
    rank: int
    name: str
    value: str
    season: Optional[int] = None


class LeaderboardResponse(BaseModel):
    title: str
    stat: str
    rows: List[LeaderboardRow]
    count: int
    pa_min: Optional[int] = None


class MilestoneResponse(BaseModel):
    title: str
    stat: str
    count: int
    rows: List[LeaderboardRow]


# ---------------------------------------------------------------------------
# GET /stats/leaderboard
# ---------------------------------------------------------------------------

@router.get("/stats/leaderboard", response_model=LeaderboardResponse)
def leaderboard(
    stat: str = QueryParam(..., description="DB column name"),
    season: Optional[int] = QueryParam(None, description="Season year"),
    scope: str = QueryParam("season", description="season|career|all_time"),
    limit: int = QueryParam(50, ge=1, le=100),
    is_pitching: bool = QueryParam(False),
):
    if not _is_valid_column(stat):
        return LeaderboardResponse(title="Invalid stat", stat=stat, rows=[], count=0)

    conn = _get_conn()
    cur = conn.cursor()

    table = "season_pitching_stats" if is_pitching else "season_batting_stats"
    lower_better = stat in LOWER_IS_BETTER
    order = "ASC" if lower_better else "DESC"

    pa_min = None

    if scope == "career":
        # Career aggregates
        if stat in RATE_STATS:
            # Can't just SUM rate stats — need to recompute
            # For now, return weighted career rate stats for common ones
            if stat == "batting_avg":
                sql = f"""
                    SELECT p.name,
                           ROUND(CAST(SUM(s.hits) AS REAL) / NULLIF(SUM(s.at_bats), 0), 3) as val
                    FROM {table} s JOIN players p ON s.player_id = p.player_id
                    GROUP BY s.player_id
                    HAVING SUM(s.plate_appearances) >= 3000
                    ORDER BY val {order} LIMIT {limit}
                """
            elif stat == "obp":
                sql = f"""
                    SELECT p.name,
                           ROUND(CAST(SUM(s.hits) + SUM(s.walks) + SUM(s.hit_by_pitch) AS REAL) /
                                 NULLIF(SUM(s.at_bats) + SUM(s.walks) + SUM(s.hit_by_pitch) + SUM(s.sacrifice_flies), 0), 3) as val
                    FROM {table} s JOIN players p ON s.player_id = p.player_id
                    GROUP BY s.player_id
                    HAVING SUM(s.plate_appearances) >= 3000
                    ORDER BY val {order} LIMIT {limit}
                """
            elif stat == "slg":
                sql = f"""
                    SELECT p.name,
                           ROUND(CAST(SUM(s.hits - s.doubles - s.triples - s.home_runs) +
                                      2*SUM(s.doubles) + 3*SUM(s.triples) + 4*SUM(s.home_runs) AS REAL) /
                                 NULLIF(SUM(s.at_bats), 0), 3) as val
                    FROM {table} s JOIN players p ON s.player_id = p.player_id
                    GROUP BY s.player_id
                    HAVING SUM(s.plate_appearances) >= 3000
                    ORDER BY val {order} LIMIT {limit}
                """
            elif stat == "ops":
                sql = f"""
                    SELECT p.name,
                           ROUND(
                               CAST(SUM(s.hits) + SUM(s.walks) + SUM(s.hit_by_pitch) AS REAL) /
                               NULLIF(SUM(s.at_bats) + SUM(s.walks) + SUM(s.hit_by_pitch) + SUM(s.sacrifice_flies), 0) +
                               CAST(SUM(s.hits - s.doubles - s.triples - s.home_runs) +
                                    2*SUM(s.doubles) + 3*SUM(s.triples) + 4*SUM(s.home_runs) AS REAL) /
                               NULLIF(SUM(s.at_bats), 0),
                           3) as val
                    FROM {table} s JOIN players p ON s.player_id = p.player_id
                    GROUP BY s.player_id
                    HAVING SUM(s.plate_appearances) >= 3000
                    ORDER BY val {order} LIMIT {limit}
                """
            elif stat == "era":
                sql = f"""
                    SELECT p.name,
                           ROUND(9.0 * CAST(SUM(s.earned_runs) AS REAL) /
                                 NULLIF(SUM(s.ip_outs) / 3.0, 0), 2) as val
                    FROM {table} s JOIN players p ON s.player_id = p.player_id
                    GROUP BY s.player_id
                    HAVING SUM(s.ip_outs) >= 3000
                    ORDER BY val {order} LIMIT {limit}
                """
            elif stat == "whip":
                sql = f"""
                    SELECT p.name,
                           ROUND(CAST(SUM(s.walks) + SUM(s.hits) AS REAL) /
                                 NULLIF(SUM(s.ip_outs) / 3.0, 0), 2) as val
                    FROM {table} s JOIN players p ON s.player_id = p.player_id
                    GROUP BY s.player_id
                    HAVING SUM(s.ip_outs) >= 3000
                    ORDER BY val {order} LIMIT {limit}
                """
            else:
                # Fallback: not all rate stats have career formulas
                return LeaderboardResponse(title=f"Career {stat} leaders", stat=stat, rows=[], count=0)
        else:
            sql = f"""
                SELECT p.name, SUM(s.{stat}) as val
                FROM {table} s JOIN players p ON s.player_id = p.player_id
                GROUP BY s.player_id
                ORDER BY val {order} LIMIT {limit}
            """
    elif scope == "all_time":
        # Best single season ever
        if stat in RATE_STATS and not is_pitching:
            pa_min = 400
            sql = f"""
                SELECT p.name, s.{stat}, s.season
                FROM {table} s JOIN players p ON s.player_id = p.player_id
                WHERE s.plate_appearances >= {pa_min}
                ORDER BY s.{stat} {order} LIMIT {limit}
            """
        elif stat in RATE_STATS and is_pitching:
            sql = f"""
                SELECT p.name, s.{stat}, s.season
                FROM {table} s JOIN players p ON s.player_id = p.player_id
                WHERE s.ip_outs >= 450
                ORDER BY s.{stat} {order} LIMIT {limit}
            """
        else:
            sql = f"""
                SELECT p.name, s.{stat}, s.season
                FROM {table} s JOIN players p ON s.player_id = p.player_id
                ORDER BY s.{stat} {order} LIMIT {limit}
            """
    else:
        # Single season
        if season is None:
            season = 2025
        if stat in RATE_STATS and not is_pitching:
            # Check if full season
            max_g_sql = f"SELECT MAX(games) FROM {table} WHERE season = {season}"
            cur.execute(max_g_sql)
            max_g = cur.fetchone()
            max_games = max_g[0] if max_g and max_g[0] else 162
            pa_min = 400 if max_games >= 140 else 200
            sql = f"""
                SELECT p.name, s.{stat}
                FROM {table} s JOIN players p ON s.player_id = p.player_id
                WHERE s.season = {season} AND s.plate_appearances >= {pa_min}
                ORDER BY s.{stat} {order} LIMIT {limit}
            """
        elif stat in RATE_STATS and is_pitching:
            sql = f"""
                SELECT p.name, s.{stat}
                FROM {table} s JOIN players p ON s.player_id = p.player_id
                WHERE s.season = {season} AND s.ip_outs >= 450
                ORDER BY s.{stat} {order} LIMIT {limit}
            """
        else:
            sql = f"""
                SELECT p.name, s.{stat}
                FROM {table} s JOIN players p ON s.player_id = p.player_id
                WHERE s.season = {season}
                ORDER BY s.{stat} {order} LIMIT {limit}
            """

    cur.execute(sql)
    rows_raw = cur.fetchall()
    conn.close()

    rows = []
    for i, row in enumerate(rows_raw):
        name = row[0]
        val = row[1]
        s = row[2] if len(row) > 2 else None
        val_str = str(round(val, 3)) if isinstance(val, float) else str(val)
        rows.append(LeaderboardRow(rank=i + 1, name=name, value=val_str, season=s))

    # Build title
    if scope == "career":
        title = f"Career {stat} Leaders"
    elif scope == "all_time":
        title = f"All-Time Single Season {stat} Leaders"
    else:
        title = f"{season} {stat} Leaders"

    return LeaderboardResponse(
        title=title, stat=stat, rows=rows, count=len(rows), pa_min=pa_min
    )


# ---------------------------------------------------------------------------
# GET /stats/threshold
# ---------------------------------------------------------------------------

@router.get("/stats/threshold", response_model=LeaderboardResponse)
def threshold(
    stat: str = QueryParam(..., description="DB column name"),
    value: float = QueryParam(..., description="Threshold value"),
    comparison: str = QueryParam(">=", description=">= or <="),
    season: int = QueryParam(2025),
    is_pitching: bool = QueryParam(False),
    limit: int = QueryParam(50, ge=1, le=100),
):
    if not _is_valid_column(stat):
        return LeaderboardResponse(title="Invalid stat", stat=stat, rows=[], count=0)
    if comparison not in (">=", "<="):
        comparison = ">="

    conn = _get_conn()
    cur = conn.cursor()

    table = "season_pitching_stats" if is_pitching else "season_batting_stats"
    order = "ASC" if comparison == "<=" else "DESC"

    sql = f"""
        SELECT p.name, s.{stat}
        FROM {table} s JOIN players p ON s.player_id = p.player_id
        WHERE s.season = {season} AND s.{stat} {comparison} {value}
        ORDER BY s.{stat} {order} LIMIT {limit}
    """
    cur.execute(sql)
    rows_raw = cur.fetchall()
    conn.close()

    rows = []
    for i, row in enumerate(rows_raw):
        val_str = str(round(row[1], 3)) if isinstance(row[1], float) else str(row[1])
        rows.append(LeaderboardRow(rank=i + 1, name=row[0], value=val_str))

    op = "at least" if comparison == ">=" else "no more than"
    val_display = str(round(value, 3)) if isinstance(value, float) and value < 10 else str(int(value))
    title = f"Players with {op} {val_display} {stat} in {season}"

    return LeaderboardResponse(
        title=title, stat=stat, rows=rows, count=len(rows)
    )


# ---------------------------------------------------------------------------
# GET /stats/milestone
# ---------------------------------------------------------------------------

@router.get("/stats/milestone", response_model=MilestoneResponse)
def milestone(
    stat: str = QueryParam(..., description="DB column name"),
    value: float = QueryParam(..., description="Threshold value"),
    since: Optional[int] = QueryParam(None, description="Since year"),
    is_pitching: bool = QueryParam(False),
    limit: int = QueryParam(50, ge=1, le=100),
):
    if not _is_valid_column(stat):
        return MilestoneResponse(title="Invalid stat", stat=stat, count=0, rows=[])

    conn = _get_conn()
    cur = conn.cursor()

    table = "season_pitching_stats" if is_pitching else "season_batting_stats"
    lower_better = stat in LOWER_IS_BETTER
    comparison = "<=" if lower_better else ">="
    order = "ASC" if lower_better else "DESC"

    since_filter = f" AND s.season >= {since}" if since else ""

    sql = f"""
        SELECT p.name, s.season, s.{stat}
        FROM {table} s JOIN players p ON s.player_id = p.player_id
        WHERE s.{stat} {comparison} {value}{since_filter}
        ORDER BY s.season DESC, s.{stat} {order}
        LIMIT {limit}
    """
    cur.execute(sql)
    rows_raw = cur.fetchall()
    conn.close()

    rows = []
    for i, row in enumerate(rows_raw):
        val_str = str(round(row[2], 3)) if isinstance(row[2], float) else str(row[2])
        rows.append(LeaderboardRow(rank=i + 1, name=row[0], value=val_str, season=row[1]))

    since_label = f" since {since}" if since else ""
    val_display = str(round(value, 3)) if isinstance(value, float) and value < 10 else str(int(value))
    title = f"{val_display}+ {stat} Seasons{since_label}"

    return MilestoneResponse(
        title=title, stat=stat, count=len(rows), rows=rows
    )
