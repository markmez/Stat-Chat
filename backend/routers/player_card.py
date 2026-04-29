"""
GET /player-card?name=... — structured player card data.

Returns JSON with player info, batting seasons, career totals,
pitching data (if applicable), and platoon splits — all from the
full historical database.
"""

import logging
import os
import sqlite3
import time
import uuid
from typing import Optional, List, Tuple
from fastapi import APIRouter, Query
from pydantic import BaseModel

from services.metering import log_query, check_quota, increment_count

log = logging.getLogger(__name__)

router = APIRouter()

DB_PATH = os.getenv(
    "DB_PATH",
    os.path.join(os.path.dirname(__file__), "..", "..", "baseball_stats_full.db"),
)


def _get_conn():
    return sqlite3.connect(DB_PATH, timeout=10)


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
    # True if any of this player's seasons were with one of the seven Negro
    # Leagues (1920-1948) MLB officially recognized as major leagues. iOS
    # surfaces this next to team name so users can contextualize stats from
    # that era (smaller samples, different competition, separate record book).
    is_negro_leagues: bool = False


class BattingSeason(BaseModel):
    year: int
    team: str
    age: int
    G: int
    team_games: int = 162
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
    SF: int
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
    team_games: int = 162
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


class SeasonSplits(BaseModel):
    year: int
    platoon: Optional[SplitGrid] = None
    home_away: Optional[SplitGrid] = None
    risp: Optional[SplitGrid] = None
    pitch_type: Optional[List[SplitGrid]] = None  # array of single-row grids
    count: Optional[List[SplitGrid]] = None  # array of single-row grids
    streaks: Optional[SplitGrid] = None
    fielding: Optional[SplitGrid] = None


class CurrentForm(BaseModel):
    form_start_date: str
    form_start_game_number: int
    total_season_games: int
    num_games: int
    stats: SplitGrid  # reuse SplitGrid for the stat grid
    counting_values: dict
    season_counting_values: dict


class PitchingCurrentForm(BaseModel):
    form_start_date: str
    form_start_game_number: int
    total_season_games: int
    num_games: int
    role: str
    stats: SplitGrid
    counting_values: dict
    season_counting_values: dict


class PitchingSeasonSplits(BaseModel):
    year: int
    platoon: Optional[SplitGrid] = None
    home_away: Optional[SplitGrid] = None
    risp: Optional[SplitGrid] = None
    pitch_type: Optional[List[SplitGrid]] = None
    count: Optional[List[SplitGrid]] = None
    streaks: Optional[SplitGrid] = None


class AchievementItem(BaseModel):
    text: str  # "4th in Yankees HR history (372)"
    type: str  # "mlb_record", "alltime_rank", "franchise_rank", "franchise_record"
    sort_key: int = 0  # rank number for ordering within type group

class SeasonAward(BaseModel):
    season: int
    awards: List[str]  # ["MVP", "ALL_STAR", "SS"]

class Achievements(BaseModel):
    awards_summary: List[str] = []  # ["2x MVP", "8x All-Star", "ROY"]
    items: List[AchievementItem] = []  # ranking items
    season_awards: List[SeasonAward] = []  # per-year awards for career display

class PlayerCardResponse(BaseModel):
    request_id: Optional[str] = None
    player_info: Optional[PlayerInfo] = None
    batting_seasons: List[BattingSeason] = []
    pitching_seasons: List[PitchingSeason] = []
    is_pitcher: bool = False
    is_two_way: bool = False
    career_platoon_splits: Optional[SplitGrid] = None
    career_home_away_splits: Optional[SplitGrid] = None
    pitching_career_platoon_splits: Optional[SplitGrid] = None
    pitching_career_home_away_splits: Optional[SplitGrid] = None
    season_splits: List[SeasonSplits] = []
    pitching_season_splits: List[PitchingSeasonSplits] = []
    current_form: Optional[dict] = None
    pitching_current_form: Optional[dict] = None
    game_logs: List["GameLogEntry"] = []
    pitching_game_logs: List["PitchingGameLogEntry"] = []
    achievements: Optional[Achievements] = None


# ---------------------------------------------------------------------------
# Achievements
# ---------------------------------------------------------------------------

_AWARD_DISPLAY = {
    "MVP": "MVP", "CY": "Cy Young", "ROY": "Rookie of the Year",
    "ALL_STAR": "All-Star", "GG": "Gold Glove", "SS": "Silver Slugger",
    "HOF": "Hall of Famer", "WS_MVP": "World Series MVP",
    "ALCS_MVP": "ALCS MVP", "NLCS_MVP": "NLCS MVP",
}

_BATTING_RANK_STATS = [
    ("home_runs", "HR", 100), ("hits", "hits", 150), ("rbi", "RBI", 150),
    ("stolen_bases", "SB", 100), ("doubles", "2B", 100),
    ("runs", "runs", 100), ("at_bats", "AB", 100),
    ("games", "games played", 100), ("walks", "walks", 100),
]
_PITCHING_RANK_STATS = [
    ("wins", "wins", 75), ("strikeouts", "strikeouts", 75),
    ("saves", "saves", 50), ("games", "games pitched", 75),
]


def _build_achievements(conn, player_id: str, name: str, is_pitcher: bool) -> Achievements:
    from services.franchise import get_franchise_codes, get_franchise_name, get_canonical_code

    achievements = Achievements()

    # --- Awards summary ---
    award_rows = conn.execute(
        "SELECT award, COUNT(*) FROM awards WHERE player_id = ? GROUP BY award ORDER BY COUNT(*) DESC",
        (player_id,)
    ).fetchall()

    # Get individual years for each award
    award_years = {}
    for row in conn.execute(
        "SELECT award, season FROM awards WHERE player_id = ? ORDER BY season",
        (player_id,)
    ).fetchall():
        award_years.setdefault(row[0], []).append(row[1])

    # Display order for summary
    # All awards grouped with year lists. HOF is standalone (no year list).
    _summary_order = ["HOF", "MVP", "CY", "ROY", "WS_MVP", "ALL_STAR", "GG", "SS"]
    for award_code in _summary_order:
        years = award_years.get(award_code, [])
        if not years:
            continue
        label = _AWARD_DISPLAY.get(award_code, award_code)
        if award_code == "HOF":
            achievements.awards_summary.append(label)
        else:
            year_strs = ", ".join(f"'{str(y)[2:]}" for y in years)
            if len(years) > 1:
                achievements.awards_summary.append(f"{len(years)}x {label} ({year_strs})")
            else:
                achievements.awards_summary.append(f"{label} ({year_strs})")

    # --- Per-season awards ---
    season_rows = conn.execute(
        "SELECT season, award FROM awards WHERE player_id = ? ORDER BY season",
        (player_id,)
    ).fetchall()
    by_season = {}
    for season, award in season_rows:
        by_season.setdefault(season, []).append(_AWARD_DISPLAY.get(award, award))
    for season in sorted(by_season.keys()):
        achievements.season_awards.append(SeasonAward(season=season, awards=by_season[season]))

    # --- All-time career rankings ---
    # Looks up precomputed `career_ranks` (built nightly by build_career_ranks.py).
    # Each row has the player's MLB rank for one stat — the inline
    # `COUNT(*) + 1 FROM (SUM GROUP BY HAVING)` aggregation is gone.
    stats = _PITCHING_RANK_STATS if is_pitcher else _BATTING_RANK_STATS
    side = "pitching" if is_pitcher else "batting"
    table = "season_pitching_stats" if is_pitcher else "season_batting_stats"
    top_n_by_stat = {col: top_n for col, _, top_n in stats}
    label_by_stat = {col: label for col, label, _ in stats}

    rank_rows = conn.execute(
        "SELECT stat, total, mlb_rank FROM career_ranks "
        "WHERE player_id = ? AND side = ?",
        (player_id, side),
    ).fetchall()
    for stat, total, rank in rank_rows:
        top_n = top_n_by_stat.get(stat)
        label = label_by_stat.get(stat)
        if not top_n or not label or rank > top_n:
            continue
        career_total = int(total)
        if rank == 1:
            achievements.items.append(AchievementItem(
                text=f"All-time MLB leader in {label}: {career_total:,}",
                type="mlb_record", sort_key=0,
            ))
        else:
            achievements.items.append(AchievementItem(
                text=f"{_ordinal(rank)} all-time in {label}: {career_total:,}",
                type="alltime_rank", sort_key=rank,
            ))

    # --- Franchise rankings (all teams played for) ---
    # Same shape: lookups against `career_franchise_ranks`, keyed by canonical
    # franchise code. We still need to enumerate which franchises the player
    # played for (the deduplication via franchise_key is also reused below
    # for the per-franchise records section).
    all_teams = conn.execute(
        f"SELECT DISTINCT team FROM {table} WHERE player_id = ?", (player_id,)
    ).fetchall()
    seen_franchises = set()
    canonical_to_name = {}  # canonical_code -> display_name
    for (team_str,) in all_teams:
        for t in team_str.split("/"):
            t = t.strip()
            if not t:
                continue
            franchise_key = tuple(sorted(get_franchise_codes(t)))
            if franchise_key in seen_franchises:
                continue
            seen_franchises.add(franchise_key)
            canonical_to_name[get_canonical_code(t)] = get_franchise_name(t)

    if canonical_to_name:
        codes = list(canonical_to_name.keys())
        ph = ",".join(["?"] * len(codes))
        fran_rows = conn.execute(
            f"SELECT stat, total, franchise_code, fran_rank FROM career_franchise_ranks "
            f"WHERE player_id = ? AND side = ? AND franchise_code IN ({ph})",
            (player_id, side, *codes),
        ).fetchall()
        for stat, total, fcode, fran_rank in fran_rows:
            label = label_by_stat.get(stat)
            if not label or fran_rank > 5:
                continue
            ft = int(total)
            franchise_name = canonical_to_name.get(fcode, fcode)
            achievements.items.append(AchievementItem(
                text=f"{_ordinal(fran_rank)} in {franchise_name} history in {label}: {ft:,}",
                type="franchise_rank", sort_key=fran_rank,
            ))

    # --- Single-season records held ---
    _STAT_DISPLAY = {
        "home_runs": "HR", "hits": "hits", "rbi": "RBI", "runs": "runs",
        "stolen_bases": "SB", "doubles": "2B", "batting_avg": "AVG",
        "ops": "OPS", "wins": "wins", "strikeouts": "K", "saves": "saves",
        "era": "ERA", "whip": "WHIP",
    }

    # MLB records
    mlb_records = conn.execute(
        "SELECT stat, value, season FROM mlb_records "
        "WHERE player_id = ? AND record_type = 'season'",
        (player_id,)
    ).fetchall()
    for stat, value, season in mlb_records:
        stat_label = _STAT_DISPLAY.get(stat, stat)
        if stat in ("batting_avg", "ops", "era", "whip"):
            val_str = f"{value:.3f}".lstrip("0") if value < 1 else f"{value:.3f}"
        else:
            val_str = f"{int(value):,}"
        achievements.items.append(AchievementItem(
            text=f"MLB single-season {stat_label} record: {val_str} ({season})",
            type="mlb_record",
        ))

    # Former MLB records (from curated record_progression table)
    try:
        former_records = conn.execute(
            "SELECT stat, record_type, value, year_set, year_broken "
            "FROM record_progression WHERE player_id = ? AND year_broken IS NOT NULL "
            "ORDER BY year_set",
            (player_id,)
        ).fetchall()
        for stat, rtype, value, year_set, year_broken in former_records:
            stat_label = _STAT_DISPLAY.get(stat, stat)
            if stat in ("batting_avg", "ops", "era", "whip"):
                val_str = f"{value:.3f}".lstrip("0") if value < 1 else f"{value:.3f}"
            else:
                val_str = f"{int(value):,}"
            rtype_label = "single-season" if rtype == "season" else "career"
            achievements.items.append(AchievementItem(
                text=f"Former MLB {rtype_label} {stat_label} record: {val_str} ({year_set}-{year_broken})",
                type="mlb_record", sort_key=1,
            ))
    except Exception:
        pass  # Table might not exist yet

    # Franchise records (only #1, across all teams played for)
    franchise_records = []
    franchise_record_names = []  # track which franchise for per-season display
    for franchise_key in seen_franchises:
        fc = list(franchise_key)
        team_code = fc[0]  # use first code for team_records lookup
        fn = get_franchise_name(team_code)
        fr_rows = conn.execute(
            "SELECT stat, value, season FROM team_records "
            "WHERE player_id = ? AND record_type = 'season' AND team_code = ?",
            (player_id, team_code)
        ).fetchall()
        for stat, value, season in fr_rows:
            lower_better = stat in ("era", "whip")
            best = conn.execute(
                f"SELECT value FROM team_records WHERE team_code = ? AND stat = ? "
                f"AND record_type = 'season' ORDER BY value {'ASC' if lower_better else 'DESC'} LIMIT 1",
                (team_code, stat)
            ).fetchone()
            if best and best[0] == value:
                franchise_records.append((stat, value, season, fn))
    if True:  # maintain indentation for the filter below
        # Filter out stats where they also hold the MLB record (avoid duplication)
        mlb_record_stats = {r[0] for r in mlb_records}
        for stat, value, season, fn in franchise_records:
            if stat in mlb_record_stats:
                continue
            stat_label = _STAT_DISPLAY.get(stat, stat)
            if stat in ("batting_avg", "ops", "era", "whip"):
                val_str = f"{value:.3f}".lstrip("0") if value < 1 else f"{value:.3f}"
            else:
                val_str = f"{int(value):,}"
            achievements.items.append(AchievementItem(
                text=f"{fn} single-season {stat_label} record: {val_str} ({season})",
                type="franchise_record",
            ))

    # --- Per-season records for season_awards ---
    for stat, value, season, *rest in franchise_records:
        stat_label = _STAT_DISPLAY.get(stat, stat)
        existing = next((sa for sa in achievements.season_awards if sa.season == season), None)
        record_text = f"Record: {stat_label}"
        if existing:
            existing.awards.append(record_text)
        else:
            achievements.season_awards.append(SeasonAward(season=season, awards=[record_text]))
    for stat, value, season in mlb_records:
        stat_label = _STAT_DISPLAY.get(stat, stat)
        existing = next((sa for sa in achievements.season_awards if sa.season == season), None)
        record_text = f"MLB Record: {stat_label}"
        if existing:
            existing.awards.append(record_text)
        else:
            achievements.season_awards.append(SeasonAward(season=season, awards=[record_text]))

    # Re-sort season awards by year
    achievements.season_awards.sort(key=lambda sa: sa.season)

    # Sort items: MLB records first, then all-time ranks, then franchise ranks, then franchise records
    _type_order = {"mlb_record": 0, "alltime_rank": 1, "franchise_record": 2, "franchise_rank": 3}
    achievements.items.sort(key=lambda item: (_type_order.get(item.type, 9), item.sort_key))

    return achievements


def _ordinal(n: int) -> str:
    if 11 <= (n % 100) <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_player_name(conn: sqlite3.Connection, name: str) -> str:
    """Resolve ambiguous names using the same prominence logic as search.
    Returns the resolved full name, or the original name if no match."""
    from services.name_matcher import match_player, match_player_with_prominence
    # Try exact match first
    exact = match_player(name)
    if exact:
        return exact
    # Try prominence-based matching (handles last names, ambiguous names)
    result = match_player_with_prominence(name)
    if result:
        return result[0]
    return name


def _resolve_player_id(conn: sqlite3.Connection, name: str) -> Optional[Tuple[str, str]]:
    """Resolve a (possibly ambiguous) name to (player_id, canonical_name).

    Done once per request — every downstream helper is keyed on player_id so
    we don't pay for `JOIN players p ... WHERE p.name = ?` on every split query.
    """
    canonical = _resolve_player_name(conn, name)
    row = conn.execute(
        "SELECT player_id, name FROM players WHERE name = ? LIMIT 1",
        (_sanitize(canonical),),
    ).fetchone()
    if not row:
        return None
    return row[0], row[1]


def _fetch_player_info(conn: sqlite3.Connection, player_id: str) -> Optional[PlayerInfo]:
    cur = conn.cursor()
    cur.execute(
        "SELECT name, team, birthdate, bats, throws, positions FROM players "
        "WHERE player_id = ? LIMIT 1",
        (player_id,),
    )
    row = cur.fetchone()
    if not row:
        return None

    # Check Negro Leagues — pull the player's per-season team strings and
    # ask the franchise module if any stint was with an NL team.
    from services.franchise import is_negro_leagues_player
    team_history: list[str] = []
    for tbl in ("season_batting_stats", "season_pitching_stats"):
        try:
            cur.execute(f"SELECT team FROM {tbl} WHERE player_id = ?", (player_id,))
            team_history.extend(t for (t,) in cur.fetchall() if t)
        except Exception:
            pass
    is_nl = is_negro_leagues_player(team_history)

    return PlayerInfo(
        name=row[0],
        team=row[1] or "",
        birthdate=row[2] if row[2] else None,
        bats=row[3] if row[3] else None,
        throws=row[4] if row[4] else None,
        positions=row[5] if row[5] else None,
        is_negro_leagues=is_nl,
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


def _team_games(conn: sqlite3.Connection, team: str, season: int) -> int:
    """Get max games played by any player on this team in this season."""
    cur = conn.cursor()
    cur.execute(
        "SELECT MAX(games) FROM season_batting_stats WHERE team = ? AND season = ?",
        (team, season),
    )
    r = cur.fetchone()
    return int(r[0]) if r and r[0] else 162


def _fetch_batting_seasons(conn: sqlite3.Connection, player_id: str) -> List[BattingSeason]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT s.season, s.team, s.age,
               s.games, s.at_bats, s.runs, s.hits,
               s.doubles, s.triples, s.home_runs, s.rbi,
               s.stolen_bases, s.caught_stealing,
               s.walks, s.intentional_walks, s.strikeouts, s.hit_by_pitch,
               s.batting_avg, s.obp, s.slg, s.ops, s.ops_plus, s.iso, s.babip,
               s.sacrifice_flies
        FROM season_batting_stats s
        WHERE s.player_id = ?
        ORDER BY s.season DESC
        """,
        (player_id,),
    )
    rows = cur.fetchall()
    seasons = []
    for r in rows:
        year = _safe_int(r[0])
        team = r[1] or ""
        tg = _team_games(conn, team, year)
        seasons.append(BattingSeason(
            year=year,
            team=team,
            age=_safe_int(r[2]),
            G=_safe_int(r[3]),
            team_games=tg,
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
            SF=_safe_int(r[24]),
            AVG=_safe_str(r[17]),
            OBP=_safe_str(r[18]),
            SLG=_safe_str(r[19]),
            OPS=_safe_str(r[20]),
            OPS_plus=_safe_str(r[21], 0) if r[21] else "--",
            ISO=_safe_str(r[22]),
            BABIP=_safe_str(r[23]),
        ))
    return seasons


def _fetch_pitching_seasons(conn: sqlite3.Connection, player_id: str) -> List[PitchingSeason]:
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
        WHERE sp.player_id = ?
        ORDER BY sp.season DESC
        """,
        (player_id,),
    )
    rows = cur.fetchall()
    seasons = []
    for r in rows:
        year = _safe_int(r[0])
        team = r[1] or ""
        tg = _team_games(conn, team, year)
        seasons.append(PitchingSeason(
            year=year,
            team=team,
            team_games=tg,
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


def _is_pitcher(conn: sqlite3.Connection, player_id: str) -> bool:
    cur = conn.cursor()
    cur.execute(
        "SELECT positions FROM players WHERE player_id = ? LIMIT 1",
        (player_id,),
    )
    row = cur.fetchone()
    if not row or not row[0] or not row[0].startswith("P"):
        return False
    cur.execute(
        "SELECT 1 FROM season_pitching_stats WHERE player_id = ? LIMIT 1",
        (player_id,),
    )
    return cur.fetchone() is not None


def _is_two_way(conn: sqlite3.Connection, player_id: str) -> bool:
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM season_batting_stats "
        "WHERE player_id = ? AND plate_appearances >= 130 LIMIT 1",
        (player_id,),
    )
    has_bat = cur.fetchone() is not None
    if not has_bat:
        return False
    cur.execute(
        "SELECT 1 FROM season_pitching_stats "
        "WHERE player_id = ? AND ip_outs >= 90 LIMIT 1",
        (player_id,),
    )
    return cur.fetchone() is not None


def _fetch_career_platoon_splits(conn: sqlite3.Connection, player_id: str) -> Optional[SplitGrid]:
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
        WHERE ps.player_id = ?
        GROUP BY ps.split
        HAVING COUNT(DISTINCT ps.season) > 1
        ORDER BY ps.split
        """,
        (player_id,),
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


def _fetch_career_home_away_splits(conn: sqlite3.Connection, player_id: str) -> Optional[SplitGrid]:
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
        WHERE has.player_id = ?
        GROUP BY has.split
        HAVING COUNT(DISTINCT has.season) > 1
        ORDER BY has.split DESC
        """,
        (player_id,),
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


def _fetch_pitching_career_platoon_splits(conn: sqlite3.Connection, player_id: str) -> Optional[SplitGrid]:
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
        WHERE pps.player_id = ?
        GROUP BY pps.split
        HAVING COUNT(DISTINCT pps.season) > 1
        ORDER BY pps.split
        """,
        (player_id,),
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


def _fetch_pitching_career_home_away_splits(conn: sqlite3.Connection, player_id: str) -> Optional[SplitGrid]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT phas.split,
               SUM(phas.games), SUM(phas.games_started),
               CAST(SUM(phas.ip_outs) / 3 AS TEXT) || '.' || CAST(SUM(phas.ip_outs) % 3 AS TEXT),
               SUM(phas.hits), SUM(phas.earned_runs), SUM(phas.home_runs),
               SUM(phas.walks), SUM(phas.strikeouts),
               ROUND(9.0 * CAST(SUM(phas.earned_runs) AS REAL) / NULLIF(SUM(phas.ip_outs) / 3.0, 0), 2),
               ROUND(CAST(SUM(phas.walks) + SUM(phas.hits) AS REAL) / NULLIF(SUM(phas.ip_outs) / 3.0, 0), 2),
               ROUND(9.0 * CAST(SUM(phas.strikeouts) AS REAL) / NULLIF(SUM(phas.ip_outs) / 3.0, 0), 1),
               ROUND(9.0 * CAST(SUM(phas.walks) AS REAL) / NULLIF(SUM(phas.ip_outs) / 3.0, 0), 1),
               ROUND(CAST(SUM(phas.hits) AS REAL) / NULLIF(SUM(phas.games) * 3, 0), 3)
        FROM pitching_home_away_splits phas
        WHERE phas.player_id = ?
        GROUP BY phas.split
        HAVING COUNT(DISTINCT phas.season) > 1
        ORDER BY phas.split DESC
        """,
        (player_id,),
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
# Per-season split helpers (batched: one query covers all seasons for a player,
# group rows in Python). Each returns Dict[year, SplitGrid] or
# Dict[year, List[SplitGrid]] for the multi-row pitch_type / count flavors.
# ---------------------------------------------------------------------------

def _fetch_all_season_platoon_splits(conn, player_id) -> dict:
    """Batch all-seasons batting platoon splits."""
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT ps.season, ps.split, ps.at_bats, ps.hits, ps.doubles, ps.triples, ps.home_runs,
                   ps.rbi, ps.walks, ps.strikeouts,
                   ps.batting_avg, ps.obp, ps.slg, ps.ops, ps.iso, ps.babip
            FROM platoon_splits ps
            WHERE ps.player_id = ?
            ORDER BY ps.season, ps.split
        """, (player_id,))
        by_season = {}
        for r in cur.fetchall():
            by_season.setdefault(r[0], []).append(r[1:])
        headers = ["AB", "H", "2B", "3B", "HR", "RBI", "BB", "SO", "AVG", "OBP", "SLG", "OPS", "ISO", "BABIP"]
        result = {}
        for year, rows in by_season.items():
            grid_rows = []
            for r in rows[:2]:
                label = "vs LHP" if r[0] == "vs_LHP" else "vs RHP"
                vals = [_safe_str(v) for v in r[1:]]
                grid_rows.append(SplitRow(label=label, values=vals))
            if grid_rows:
                result[year] = SplitGrid(headers=headers, rows=grid_rows)
        return result
    except Exception:
        return {}


def _fetch_all_season_home_away_splits(conn, player_id) -> dict:
    """Batch all-seasons batting home/away splits."""
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT has.season, has.split, has.games, has.at_bats, has.runs, has.hits,
                   has.doubles, has.triples, has.home_runs, has.rbi,
                   has.walks, has.strikeouts,
                   has.batting_avg, has.obp, has.slg, has.ops, has.iso, has.babip
            FROM home_away_splits has
            WHERE has.player_id = ?
            ORDER BY has.season, has.split DESC
        """, (player_id,))
        by_season = {}
        for r in cur.fetchall():
            by_season.setdefault(r[0], []).append(r[1:])
        headers = ["G", "AB", "R", "H", "2B", "3B", "HR", "RBI", "BB", "SO", "AVG", "OBP", "SLG", "OPS", "ISO", "BABIP"]
        result = {}
        for year, rows in by_season.items():
            grid_rows = []
            for r in rows[:2]:
                label = "Home" if r[0] == "home" else "Away"
                vals = [_safe_str(v) for v in r[1:]]
                grid_rows.append(SplitRow(label=label, values=vals))
            if grid_rows:
                result[year] = SplitGrid(headers=headers, rows=grid_rows)
        return result
    except Exception:
        return {}


def _fetch_all_season_risp_splits(conn, player_id) -> dict:
    """Batch all-seasons batting RISP splits."""
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT rs.season, rs.split, rs.at_bats, rs.hits, rs.doubles, rs.triples, rs.home_runs,
                   rs.rbi, rs.walks, rs.strikeouts,
                   rs.batting_avg, rs.obp, rs.slg, rs.ops, rs.iso, rs.babip
            FROM risp_batting_splits rs
            WHERE rs.player_id = ?
            ORDER BY rs.season, rs.split DESC
        """, (player_id,))
        by_season = {}
        for r in cur.fetchall():
            by_season.setdefault(r[0], []).append(r[1:])
        headers = ["AB", "H", "2B", "3B", "HR", "RBI", "BB", "SO", "AVG", "OBP", "SLG", "OPS", "ISO", "BABIP"]
        result = {}
        for year, rows in by_season.items():
            grid_rows = []
            for r in rows[:2]:
                label = "RISP" if r[0] == "RISP" else "Non-RISP"
                vals = [_safe_str(v) for v in r[1:]]
                grid_rows.append(SplitRow(label=label, values=vals))
            if grid_rows:
                result[year] = SplitGrid(headers=headers, rows=grid_rows)
        return result
    except Exception:
        return {}


def _fetch_all_season_pitch_type_batting(conn, player_id) -> dict:
    """Batch all-seasons batting pitch type splits. Returns {year: [SplitGrid, ...]}."""
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT pts.season, pts.pitch_type, pts.at_bats, pts.hits, pts.doubles, pts.triples,
                   pts.home_runs, pts.rbi, pts.walks, pts.strikeouts,
                   pts.batting_avg, pts.obp, pts.slg, pts.ops
            FROM pitch_type_batting_splits pts
            WHERE pts.player_id = ?
            ORDER BY pts.season, pts.at_bats DESC
        """, (player_id,))
        by_season = {}
        for r in cur.fetchall():
            by_season.setdefault(r[0], []).append(r[1:])
        headers = ["AB", "H", "2B", "3B", "HR", "RBI", "BB", "SO", "AVG", "OBP", "SLG", "OPS"]
        result = {}
        for year, rows in by_season.items():
            grids = []
            for r in rows:
                label = r[0]
                vals = [_safe_str(v) for v in r[1:]]
                grids.append(SplitGrid(headers=headers, rows=[SplitRow(label=label, values=vals)]))
            if grids:
                result[year] = grids
        return result
    except Exception:
        return {}


def _fetch_all_season_count_batting(conn, player_id) -> dict:
    """Batch all-seasons batting count splits. Returns {year: [SplitGrid, ...]}."""
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT cs.season, cs.count_state, cs.at_bats, cs.hits, cs.doubles, cs.triples,
                   cs.home_runs, cs.rbi, cs.walks, cs.strikeouts,
                   cs.batting_avg, cs.obp, cs.slg, cs.ops
            FROM count_batting_splits cs
            WHERE cs.player_id = ?
            ORDER BY cs.season, cs.count_state
        """, (player_id,))
        by_season = {}
        for r in cur.fetchall():
            by_season.setdefault(r[0], []).append(r[1:])
        headers = ["AB", "H", "2B", "3B", "HR", "RBI", "BB", "SO", "AVG", "OBP", "SLG", "OPS"]
        result = {}
        for year, rows in by_season.items():
            grids = []
            for r in rows:
                label = r[0]
                vals = [_safe_str(v) for v in r[1:]]
                grids.append(SplitGrid(headers=headers, rows=[SplitRow(label=label, values=vals)]))
            if grids:
                result[year] = grids
        return result
    except Exception:
        return {}


def _fetch_all_season_streaks(conn, player_id, performance="hot") -> dict:
    """Batch all-seasons batting streaks with 3-tier fallback. One query per tier
    instead of one per (tier, season). Per-season tier preference preserved."""
    try:
        order_dir = "ASC" if performance == "cold" else "DESC"
        tables = ["streaks", "streaks_sensitive", "streaks_sliding"]
        tiers_by_season = []  # [{year: [rows]}, ...] one per tier
        for table in tables:
            cur = conn.cursor()
            cur.execute(f"""
                SELECT st.season, st.start_date, st.end_date, st.num_games,
                       st.at_bats, st.hits, st.walks, st.strikeouts,
                       st.batting_avg, st.obp, st.slg, st.ops, st.home_runs
                FROM {table} st
                WHERE st.player_id = ? AND st.performance = ?
                ORDER BY st.season, st.ops {order_dir}
            """, (player_id, performance))
            tier = {}
            for r in cur.fetchall():
                tier.setdefault(r[0], []).append(r[1:])
            tiers_by_season.append(tier)

        headers = ["G", "AB", "H", "BB", "SO", "AVG", "OBP", "SLG", "OPS", "HR"]
        result = {}
        all_seasons = set()
        for tier in tiers_by_season:
            all_seasons.update(tier.keys())
        for year in all_seasons:
            rows = None
            for tier in tiers_by_season:
                if tier.get(year):
                    rows = tier[year]
                    break
            if not rows:
                continue
            grid_rows = []
            for r in rows[:4]:
                start = r[0] or ""
                end = r[1] or ""
                label = f"{start} – {end}"
                vals = [_safe_str(r[2], 0), _safe_str(r[3], 0), _safe_str(r[4], 0),
                        _safe_str(r[5], 0), _safe_str(r[6], 0),
                        _safe_str(r[7]), _safe_str(r[8]), _safe_str(r[9]), _safe_str(r[10]),
                        _safe_str(r[11], 0)]
                grid_rows.append(SplitRow(label=label, values=vals))
            if grid_rows:
                result[year] = SplitGrid(headers=headers, rows=grid_rows)
        return result
    except Exception:
        return {}


def _fetch_all_season_fielding(conn, player_id) -> dict:
    """Batch all-seasons fielding stats."""
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT sfs.season, sfs.position, sfs.putouts, sfs.assists, sfs.errors,
                   sfs.double_plays, sfs.passed_balls, sfs.fielding_pct
            FROM season_fielding_stats sfs
            WHERE sfs.player_id = ? AND sfs.games > 0
            ORDER BY sfs.season, sfs.games DESC
        """, (player_id,))
        by_season = {}
        for r in cur.fetchall():
            by_season.setdefault(r[0], []).append(r[1:])
        result = {}
        for year, rows in by_season.items():
            has_pb = any((r[5] or 0) > 0 for r in rows)
            headers = ["PO", "A", "E", "DP"]
            if has_pb:
                headers.append("PB")
            headers.append("FLD%")
            grid_rows = []
            for r in rows:
                vals = [_safe_str(r[1], 0), _safe_str(r[2], 0), _safe_str(r[3], 0), _safe_str(r[4], 0)]
                if has_pb:
                    vals.append(_safe_str(r[5], 0))
                vals.append(_safe_str(r[6]))
                grid_rows.append(SplitRow(label=r[0] or "?", values=vals))
            if grid_rows:
                result[year] = SplitGrid(headers=headers, rows=grid_rows)
        return result
    except Exception:
        return {}


def _fetch_current_form(conn, player_id, season):
    """Fetch batting current form for a season."""
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT cf.form_start_date, cf.form_start_game_number, cf.total_season_games, cf.num_games,
                   cf.at_bats, cf.hits, cf.doubles, cf.triples, cf.home_runs,
                   cf.runs, cf.rbi, cf.walks, cf.strikeouts,
                   cf.batting_avg, cf.obp, cf.slg, cf.ops,
                   cf.season_at_bats, cf.season_hits, cf.season_doubles, cf.season_triples,
                   cf.season_home_runs, cf.season_runs, cf.season_rbi,
                   cf.season_walks, cf.season_strikeouts
            FROM current_form cf
            WHERE cf.player_id = ? AND cf.season = ?
        """, (player_id, season))
        row = cur.fetchone()
        if not row:
            return None
        num_games = _safe_int(row[3])
        headers = ["G", "AB", "R", "H", "HR", "RBI", "BB", "SO", "AVG", "OBP", "SLG", "OPS"]
        vals = [_safe_str(num_games, 0), _safe_str(row[4], 0), _safe_str(row[9], 0),
                _safe_str(row[5], 0), _safe_str(row[8], 0), _safe_str(row[10], 0),
                _safe_str(row[11], 0), _safe_str(row[12], 0),
                _safe_str(row[13]), _safe_str(row[14]),
                _safe_str(row[15]), _safe_str(row[16])]
        grid = SplitGrid(headers=headers, rows=[SplitRow(label="", values=vals)])

        # Counting values for form period
        counting = {
            "G": num_games, "AB": _safe_int(row[4]), "H": _safe_int(row[5]),
            "2B": _safe_int(row[6]), "3B": _safe_int(row[7]), "HR": _safe_int(row[8]),
            "R": _safe_int(row[9]), "RBI": _safe_int(row[10]),
            "BB": _safe_int(row[11]), "SO": _safe_int(row[12]),
        }

        # Season counting values (stored directly in the current_form table)
        season_counting = {
            "AB": _safe_int(row[17]), "H": _safe_int(row[18]),
            "2B": _safe_int(row[19]), "3B": _safe_int(row[20]),
            "HR": _safe_int(row[21]), "R": _safe_int(row[22]),
            "RBI": _safe_int(row[23]), "BB": _safe_int(row[24]),
            "SO": _safe_int(row[25]),
        }

        return {
            "form_start_date": row[0] or "",
            "form_start_game_number": _safe_int(row[1]),
            "total_season_games": _safe_int(row[2]),
            "num_games": num_games,
            "stats": grid.model_dump(),
            "counting_values": counting,
            "season_counting_values": season_counting,
        }
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Pitching per-season split helpers (batched).
# ---------------------------------------------------------------------------

def _fetch_all_pitching_season_platoon(conn, player_id) -> dict:
    """Batch all-seasons pitching platoon splits."""
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT pps.season, pps.split, pps.at_bats, pps.hits, pps.doubles, pps.triples, pps.home_runs,
                   pps.walks, pps.strikeouts,
                   ROUND(CAST(pps.hits AS REAL) / NULLIF(pps.at_bats, 0), 3),
                   ROUND(CAST(pps.hits + pps.walks + COALESCE(pps.hit_by_pitch, 0) AS REAL) /
                         NULLIF(pps.at_bats + pps.walks + COALESCE(pps.hit_by_pitch, 0) + COALESCE(pps.sacrifice_flies, 0), 0), 3),
                   ROUND(CAST(pps.hits - pps.doubles - pps.triples - pps.home_runs +
                              2 * pps.doubles + 3 * pps.triples + 4 * pps.home_runs AS REAL) /
                         NULLIF(pps.at_bats, 0), 3)
            FROM pitching_platoon_splits pps
            WHERE pps.player_id = ?
            ORDER BY pps.season, pps.split
        """, (player_id,))
        by_season = {}
        for r in cur.fetchall():
            by_season.setdefault(r[0], []).append(r[1:])
        headers = ["AB", "H", "2B", "3B", "HR", "BB", "SO", "AVG", "OBP", "SLG", "OPS"]
        result = {}
        for year, rows in by_season.items():
            grid_rows = []
            for r in rows[:2]:
                label = "vs LHB" if r[0] == "vs_LHB" else "vs RHB"
                vals = [_safe_str(v) for v in r[1:8]]
                avg = float(r[8]) if r[8] else 0
                obp = float(r[9]) if r[9] else 0
                slg = float(r[10]) if r[10] else 0
                vals.extend([_safe_str(avg), _safe_str(obp), _safe_str(slg), f"{obp + slg:.3f}"])
                grid_rows.append(SplitRow(label=label, values=vals))
            if grid_rows:
                result[year] = SplitGrid(headers=headers, rows=grid_rows)
        return result
    except Exception:
        return {}


def _fetch_all_pitching_season_home_away(conn, player_id) -> dict:
    """Batch all-seasons pitching home/away splits."""
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT phas.season, phas.split, phas.games, phas.games_started,
                   CAST(phas.ip_outs / 3 AS TEXT) || '.' || CAST(phas.ip_outs % 3 AS TEXT),
                   phas.hits, phas.earned_runs, phas.home_runs, phas.walks, phas.strikeouts,
                   ROUND(9.0 * CAST(phas.earned_runs AS REAL) / NULLIF(phas.ip_outs / 3.0, 0), 2),
                   ROUND(CAST(phas.walks + phas.hits AS REAL) / NULLIF(phas.ip_outs / 3.0, 0), 2),
                   ROUND(9.0 * CAST(phas.strikeouts AS REAL) / NULLIF(phas.ip_outs / 3.0, 0), 1),
                   ROUND(9.0 * CAST(phas.walks AS REAL) / NULLIF(phas.ip_outs / 3.0, 0), 1),
                   ROUND(CAST(phas.hits AS REAL) / NULLIF(phas.at_bats, 0), 3)
            FROM pitching_home_away_splits phas
            WHERE phas.player_id = ?
            ORDER BY phas.season, phas.split DESC
        """, (player_id,))
        by_season = {}
        for r in cur.fetchall():
            by_season.setdefault(r[0], []).append(r[1:])
        headers = ["G", "GS", "IP", "H", "ER", "HR", "BB", "SO", "ERA", "WHIP", "K/9", "BB/9", "BAA"]
        result = {}
        for year, rows in by_season.items():
            grid_rows = []
            for r in rows[:2]:
                label = "Home" if r[0] == "home" else "Away"
                vals = [_safe_str(v) for v in r[1:]]
                grid_rows.append(SplitRow(label=label, values=vals))
            if grid_rows:
                result[year] = SplitGrid(headers=headers, rows=grid_rows)
        return result
    except Exception:
        return {}


def _fetch_all_pitching_season_risp(conn, player_id) -> dict:
    """Batch all-seasons pitching RISP splits."""
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT rs.season, rs.split, rs.at_bats, rs.hits, rs.doubles, rs.triples, rs.home_runs,
                   rs.walks, rs.strikeouts,
                   rs.batting_avg_against, rs.obp_against, rs.slg_against, rs.ops_against
            FROM risp_pitching_splits rs
            WHERE rs.player_id = ?
            ORDER BY rs.season, rs.split DESC
        """, (player_id,))
        by_season = {}
        for r in cur.fetchall():
            by_season.setdefault(r[0], []).append(r[1:])
        headers = ["AB", "H", "2B", "3B", "HR", "BB", "SO", "AVG", "OBP", "SLG", "OPS"]
        result = {}
        for year, rows in by_season.items():
            grid_rows = []
            for r in rows[:2]:
                label = "RISP" if r[0] == "RISP" else "Non-RISP"
                vals = [_safe_str(v) for v in r[1:]]
                grid_rows.append(SplitRow(label=label, values=vals))
            if grid_rows:
                result[year] = SplitGrid(headers=headers, rows=grid_rows)
        return result
    except Exception:
        return {}


def _fetch_all_pitching_season_pitch_type(conn, player_id) -> dict:
    """Batch all-seasons pitching pitch type splits. Returns {year: [SplitGrid, ...]}."""
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT pts.season, pts.pitch_type, pts.at_bats, pts.hits, pts.doubles, pts.triples,
                   pts.home_runs, pts.walks, pts.strikeouts,
                   pts.batting_avg_against, pts.obp_against, pts.slg_against, pts.ops_against
            FROM pitch_type_pitching_splits pts
            WHERE pts.player_id = ?
            ORDER BY pts.season, pts.at_bats DESC
        """, (player_id,))
        by_season = {}
        for r in cur.fetchall():
            by_season.setdefault(r[0], []).append(r[1:])
        headers = ["AB", "H", "2B", "3B", "HR", "BB", "SO", "AVG", "OBP", "SLG", "OPS"]
        result = {}
        for year, rows in by_season.items():
            grids = []
            for r in rows:
                label = r[0]
                vals = [_safe_str(v) for v in r[1:]]
                grids.append(SplitGrid(headers=headers, rows=[SplitRow(label=label, values=vals)]))
            if grids:
                result[year] = grids
        return result
    except Exception:
        return {}


def _fetch_all_pitching_season_count(conn, player_id) -> dict:
    """Batch all-seasons pitching count splits. Returns {year: [SplitGrid, ...]}."""
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT cs.season, cs.count_state, cs.at_bats, cs.hits, cs.doubles, cs.triples,
                   cs.home_runs, cs.walks, cs.strikeouts,
                   cs.batting_avg_against, cs.obp_against, cs.slg_against, cs.ops_against
            FROM count_pitching_splits cs
            WHERE cs.player_id = ?
            ORDER BY cs.season, cs.count_state
        """, (player_id,))
        by_season = {}
        for r in cur.fetchall():
            by_season.setdefault(r[0], []).append(r[1:])
        headers = ["AB", "H", "2B", "3B", "HR", "BB", "SO", "AVG", "OBP", "SLG", "OPS"]
        result = {}
        for year, rows in by_season.items():
            grids = []
            for r in rows:
                label = r[0]
                vals = [_safe_str(v) for v in r[1:]]
                grids.append(SplitGrid(headers=headers, rows=[SplitRow(label=label, values=vals)]))
            if grids:
                result[year] = grids
        return result
    except Exception:
        return {}


def _fetch_all_pitching_season_streaks(conn, player_id, performance="hot") -> dict:
    """Batch all-seasons pitching streaks with 3-tier fallback."""
    try:
        order_dir = "ASC" if performance == "cold" else "DESC"
        tables = ["pitching_streaks", "pitching_streaks_sensitive", "pitching_streaks_sliding"]
        tiers_by_season = []
        for table in tables:
            cur = conn.cursor()
            cur.execute(f"""
                SELECT st.season, st.start_date, st.end_date, st.num_games,
                       st.ip_outs, st.hits, st.earned_runs, st.walks, st.strikeouts,
                       st.era, st.whip, st.k_per_9, st.home_runs
                FROM {table} st
                WHERE st.player_id = ? AND st.performance = ?
                ORDER BY st.season, st.era {order_dir}
            """, (player_id, performance))
            tier = {}
            for r in cur.fetchall():
                tier.setdefault(r[0], []).append(r[1:])
            tiers_by_season.append(tier)

        headers = ["G", "IP", "H", "ER", "BB", "SO", "ERA", "WHIP", "K/9", "HR"]
        result = {}
        all_seasons = set()
        for tier in tiers_by_season:
            all_seasons.update(tier.keys())
        for year in all_seasons:
            rows = None
            for tier in tiers_by_season:
                if tier.get(year):
                    rows = tier[year]
                    break
            if not rows:
                continue
            grid_rows = []
            for r in rows[:4]:
                start = r[0] or ""
                end = r[1] or ""
                label = f"{start} – {end}"
                ip_outs = _safe_int(r[3])
                ip = f"{ip_outs // 3}.{ip_outs % 3}"
                vals = [_safe_str(r[2], 0), ip, _safe_str(r[4], 0), _safe_str(r[5], 0),
                        _safe_str(r[6], 0), _safe_str(r[7], 0),
                        _safe_str(r[8], 2), _safe_str(r[9], 2), _safe_str(r[10], 1),
                        _safe_str(r[11], 0)]
                grid_rows.append(SplitRow(label=label, values=vals))
            if grid_rows:
                result[year] = SplitGrid(headers=headers, rows=grid_rows)
        return result
    except Exception:
        return {}


def _fetch_pitching_current_form(conn, player_id, season):
    """Fetch pitching current form for a season."""
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT pcf.form_start_date, pcf.form_start_game_number, pcf.total_season_games, pcf.num_games,
                   pcf.role, pcf.ip_outs, pcf.hits, pcf.earned_runs,
                   pcf.walks, pcf.strikeouts, pcf.home_runs,
                   pcf.era, pcf.whip, pcf.k_per_9,
                   pcf.season_ip_outs, pcf.season_hits, pcf.season_earned_runs,
                   pcf.season_home_runs, pcf.season_walks, pcf.season_strikeouts
            FROM pitching_current_form pcf
            WHERE pcf.player_id = ? AND pcf.season = ?
        """, (player_id, season))
        row = cur.fetchone()
        if not row:
            return None
        num_games = _safe_int(row[3])
        ip_outs = _safe_int(row[5])
        ip = f"{ip_outs // 3}.{ip_outs % 3}"
        headers = ["G", "IP", "H", "ER", "BB", "SO", "HR", "ERA", "WHIP", "K/9"]
        vals = [_safe_str(num_games, 0), ip, _safe_str(row[6], 0), _safe_str(row[7], 0),
                _safe_str(row[8], 0), _safe_str(row[9], 0), _safe_str(row[10], 0),
                _safe_str(row[11], 2), _safe_str(row[12], 2), _safe_str(row[13], 1)]
        grid = SplitGrid(headers=headers, rows=[SplitRow(label="", values=vals)])

        counting = {
            "G": num_games, "IP_OUTS": ip_outs, "H": _safe_int(row[6]),
            "ER": _safe_int(row[7]), "BB": _safe_int(row[8]),
            "SO": _safe_int(row[9]), "HR": _safe_int(row[10]),
        }

        # Season counting from the current_form table itself
        season_counting = {
            "IP_OUTS": _safe_int(row[14]), "H": _safe_int(row[15]),
            "ER": _safe_int(row[16]), "HR": _safe_int(row[17]),
            "BB": _safe_int(row[18]), "SO": _safe_int(row[19]),
        }

        return {
            "form_start_date": row[0] or "",
            "form_start_game_number": _safe_int(row[1]),
            "total_season_games": _safe_int(row[2]),
            "num_games": _safe_int(row[3]),
            "role": row[4] or "SP",
            "stats": grid.model_dump(),
            "counting_values": counting,
            "season_counting_values": season_counting,
        }
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Game logs (for current form slider recomputation)
# ---------------------------------------------------------------------------

class GameLogEntry(BaseModel):
    date: str
    opponent: str = ""
    at_bats: int
    hits: int
    doubles: int
    triples: int
    home_runs: int
    runs: int
    rbi: int
    walks: int
    strikeouts: int
    plate_appearances: int


class PitchingGameLogEntry(BaseModel):
    date: str
    opponent: str = ""
    ip_outs: int
    innings_pitched: str = ""
    hits: int
    earned_runs: int
    walks: int
    strikeouts: int
    home_runs: int
    is_start: bool
    win: bool = False
    loss: bool = False


def _fetch_batting_game_logs(conn, player_id, season):
    """Fetch batting game logs for a player-season, most recent first."""
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT g.date, g.opponent, g.at_bats, g.hits, g.doubles, g.triples,
                   g.home_runs, g.runs, g.rbi, g.walks, g.strikeouts, g.plate_appearances
            FROM game_batting_logs g
            WHERE g.player_id = ? AND g.season = ?
            ORDER BY g.date DESC
        """, (player_id, season))
        rows = cur.fetchall()
        return [GameLogEntry(
            date=r[0] or "", opponent=r[1] or "", at_bats=_safe_int(r[2]),
            hits=_safe_int(r[3]), doubles=_safe_int(r[4]), triples=_safe_int(r[5]),
            home_runs=_safe_int(r[6]), runs=_safe_int(r[7]), rbi=_safe_int(r[8]),
            walks=_safe_int(r[9]), strikeouts=_safe_int(r[10]),
            plate_appearances=_safe_int(r[11])
        ) for r in rows]
    except Exception:
        return []


def _fetch_pitching_game_logs(conn, player_id, season):
    """Fetch pitching game logs for a player-season, most recent first."""
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT g.date, g.opponent, g.ip_outs, g.innings_pitched, g.hits,
                   g.earned_runs, g.walks, g.strikeouts, g.home_runs,
                   g.is_start, g.win, g.loss
            FROM game_pitching_logs g
            WHERE g.player_id = ? AND g.season = ?
            ORDER BY g.date DESC
        """, (player_id, season))
        rows = cur.fetchall()
        return [PitchingGameLogEntry(
            date=r[0] or "", opponent=r[1] or "", ip_outs=_safe_int(r[2]),
            innings_pitched=r[3] or "", hits=_safe_int(r[4]),
            earned_runs=_safe_int(r[5]), walks=_safe_int(r[6]),
            strikeouts=_safe_int(r[7]), home_runs=_safe_int(r[8]),
            is_start=bool(r[9]) if r[9] is not None else False,
            win=bool(r[10]) if r[10] is not None else False,
            loss=bool(r[11]) if r[11] is not None else False,
        ) for r in rows]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.get("/player-card")
async def player_card(
    name: str = Query(..., description="Player name to look up"),
    source: str = Query("link", description="'search' if user typed it, 'link' if tapped a name"),
    device_id: str = Query("", description="Device ID for metering (required when source=search)"),
):
    # Short per-request ID so client events (partial_player_card) can be correlated
    # back to this specific request in gunicorn logs. 12 hex chars is unique enough
    # to grep for without taking up space in log lines.
    rid = uuid.uuid4().hex[:12]
    start = time.perf_counter()

    # Log and meter player card searches (not link navigations)
    if source == "search" and device_id:
        log_query(name, device_id, "query engine")
        increment_count(device_id)

    conn = _get_conn()
    try:
        # Resolve to (player_id, canonical_name) once. Every helper below is
        # keyed on player_id so we don't pay for `JOIN players p ... WHERE p.name = ?`
        # on each split/seasons/game-log query.
        resolved = _resolve_player_id(conn, name)
        if not resolved:
            log.info("PLAYERCARD rid=%s name=%s elapsed_ms=%d (not found)",
                     rid, name, int((time.perf_counter() - start) * 1000))
            return PlayerCardResponse(request_id=rid)
        player_id, name = resolved

        info = _fetch_player_info(conn, player_id)
        batting = _fetch_batting_seasons(conn, player_id)
        pitcher = _is_pitcher(conn, player_id)
        two_way = not pitcher and _is_two_way(conn, player_id)
        pitching = _fetch_pitching_seasons(conn, player_id) if (pitcher or two_way) else []

        career_platoon = _fetch_career_platoon_splits(conn, player_id)
        career_home_away = _fetch_career_home_away_splits(conn, player_id)
        pitching_career_platoon = _fetch_pitching_career_platoon_splits(conn, player_id) if (pitcher or two_way) else None
        pitching_career_home_away = _fetch_pitching_career_home_away_splits(conn, player_id) if (pitcher or two_way) else None

        # Per-season splits for batting — fetch all seasons in one query per
        # split type, then assemble per-season objects from the dicts.
        all_platoon = _fetch_all_season_platoon_splits(conn, player_id)
        all_home_away = _fetch_all_season_home_away_splits(conn, player_id)
        all_risp = _fetch_all_season_risp_splits(conn, player_id)
        all_pitch_type = _fetch_all_season_pitch_type_batting(conn, player_id)
        all_count = _fetch_all_season_count_batting(conn, player_id)
        all_streaks = _fetch_all_season_streaks(conn, player_id)
        all_fielding = _fetch_all_season_fielding(conn, player_id)
        season_splits = []
        for bs in batting:
            yr = bs.year
            ss = SeasonSplits(
                year=yr,
                platoon=all_platoon.get(yr),
                home_away=all_home_away.get(yr),
                risp=all_risp.get(yr),
                pitch_type=all_pitch_type.get(yr),
                count=all_count.get(yr),
                streaks=all_streaks.get(yr),
                fielding=all_fielding.get(yr),
            )
            season_splits.append(ss)

        # Current form for most recent batting season (single-season, no batching)
        current_form = None
        if batting:
            current_form = _fetch_current_form(conn, player_id, batting[0].year)

        # Per-season splits for pitching — same batch pattern.
        pitching_season_splits = []
        pitching_current_form = None
        if pitcher or two_way:
            all_p_platoon = _fetch_all_pitching_season_platoon(conn, player_id)
            all_p_home_away = _fetch_all_pitching_season_home_away(conn, player_id)
            all_p_risp = _fetch_all_pitching_season_risp(conn, player_id)
            all_p_pitch_type = _fetch_all_pitching_season_pitch_type(conn, player_id)
            all_p_count = _fetch_all_pitching_season_count(conn, player_id)
            all_p_streaks = _fetch_all_pitching_season_streaks(conn, player_id)
            for ps in pitching:
                yr = ps.year
                pss = PitchingSeasonSplits(
                    year=yr,
                    platoon=all_p_platoon.get(yr),
                    home_away=all_p_home_away.get(yr),
                    risp=all_p_risp.get(yr),
                    pitch_type=all_p_pitch_type.get(yr),
                    count=all_p_count.get(yr),
                    streaks=all_p_streaks.get(yr),
                )
                pitching_season_splits.append(pss)
            if pitching:
                pitching_current_form = _fetch_pitching_current_form(conn, player_id, pitching[0].year)

        # Game logs for current season only (most recent first)
        from datetime import date
        current_year = date.today().year
        game_logs = _fetch_batting_game_logs(conn, player_id, current_year) if batting else []
        pitching_game_logs = _fetch_pitching_game_logs(conn, player_id, current_year) if (pitcher or two_way) else []

        # Achievements
        achievements = None
        try:
            achievements = _build_achievements(conn, player_id, name, pitcher and not two_way)
        except Exception:
            pass

        # Breadcrumb: one line per request, correlatable by rid with client events.
        # Counts are the key shape signals — if a client_event reports missing
        # season_splits for year Y but this log shows ss=N>0, we know the backend
        # returned the data and the client lost it (or vice versa).
        current_yr_bs = sum(1 for bs in batting if bs.year == current_year)
        current_yr_ss = sum(1 for s in season_splits if s.year == current_year)
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        log.info(
            "PLAYERCARD rid=%s name=%s yr=%s bs_total=%d bs_curr=%d ss_total=%d ss_curr=%d gl=%d pit=%d elapsed_ms=%d",
            rid, name, current_year,
            len(batting), current_yr_bs,
            len(season_splits), current_yr_ss,
            len(game_logs), len(pitching),
            elapsed_ms,
        )

        return PlayerCardResponse(
            request_id=rid,
            player_info=info,
            batting_seasons=batting,
            pitching_seasons=pitching,
            is_pitcher=pitcher,
            is_two_way=two_way,
            career_platoon_splits=career_platoon,
            career_home_away_splits=career_home_away,
            pitching_career_platoon_splits=pitching_career_platoon,
            pitching_career_home_away_splits=pitching_career_home_away,
            season_splits=season_splits,
            pitching_season_splits=pitching_season_splits,
            current_form=current_form,
            pitching_current_form=pitching_current_form,
            game_logs=game_logs,
            pitching_game_logs=pitching_game_logs,
            achievements=achievements,
        )
    finally:
        conn.close()


@router.get("/player-card/game-logs")
async def player_game_logs(
    name: str = Query(..., description="Player name"),
    season: int = Query(..., description="Season year"),
    type: str = Query("batting", description="'batting' or 'pitching'"),
):
    """Fetch game logs for current form slider recomputation."""
    conn = _get_conn()
    try:
        resolved = _resolve_player_id(conn, name)
        if not resolved:
            return []
        player_id, _ = resolved
        if type == "pitching":
            logs = _fetch_pitching_game_logs(conn, player_id, season)
        else:
            logs = _fetch_batting_game_logs(conn, player_id, season)
        return [log.model_dump() for log in logs]
    finally:
        conn.close()
