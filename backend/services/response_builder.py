"""
Response builder functions for the StatChat backend.

Translates structured query results into formatted text responses
with [STATGRID], [LEADERBOARD], [SUGGEST], [TIP], and other markup
tags that the iOS parser expects.

Builder functions (38 total):
  1-24: Season summaries, comparisons, single stat lookups, slash line,
        career lookups, streaks, current form, platoon/home-away/RISP/
        pitch-type/count splits, month stats (batting + pitching)
  25-38: Leaderboards (season/allTime/since/career, batting + pitching),
         thresholds, all-time thresholds, milestones, filtered leaderboards,
         superlatives, composite threshold (HR/SB), triple crown,
         consecutive streaks, team stats, team totals, team rankings
"""

import json
import os
import re
import sqlite3
from datetime import datetime
from typing import Optional

from .name_matcher import StatInfo
from .qualification import min_pa as _qual_min_pa, min_ip_outs as _qual_min_ip_outs

DB_PATH = os.getenv(
    "DB_PATH",
    os.path.join(os.path.dirname(__file__), "..", "..", "baseball_stats_full.db"),
)

# ---------------------------------------------------------------------------
# Batting stat headers (21 stats, PA and SF excluded for compact display)
# ---------------------------------------------------------------------------
BATTING_HEADERS = [
    "G", "AB", "R", "H", "2B", "3B", "HR", "RBI", "SB", "CS",
    "BB", "IBB", "SO", "HBP",
    "AVG", "OBP", "SLG", "OPS", "OPS+", "ISO", "BABIP",
]

# ---------------------------------------------------------------------------
# Pitching stat headers
# ---------------------------------------------------------------------------
PITCHING_ALL_HEADERS = [
    "W", "L", "SV", "G", "GS", "GF", "CG", "QS", "IP", "H", "R", "ER", "HR",
    "BB", "IBB", "SO", "HBP", "WP", "BK", "BF", "SH", "SF", "SB", "CS",
    "ERA", "WHIP", "K/9", "BB/9", "K/BB", "H/9", "HR/9", "BAA", "ERA+",
]

PITCHING_HIDDEN = {"GF", "IBB", "BF", "SF", "SH", "K/BB"}

PITCHING_HEADERS = [h for h in PITCHING_ALL_HEADERS if h not in PITCHING_HIDDEN]

# ---------------------------------------------------------------------------
# AL / NL team code sets (Retrosheet abbreviations)
# ---------------------------------------------------------------------------
_AL_TEAMS = (
    "'ANA','ATH','BAL','BOS','CHA','CLE','DET','HOU','KCA',"
    "'MIN','NYA','SEA','TBA','TEX','TOR'"
)
_NL_TEAMS = (
    "'ARI','ATL','CHN','CIN','COL','LAN','MIA','MIL','NYN',"
    "'PHI','PIT','SDN','SFN','SLN','WAS'"
)

# Rate stat sets for formatting
_RATE_STATS = {"AVG", "OBP", "SLG", "OPS", "ISO", "BABIP", "BAA"}
_TWO_DEC_STATS = {"ERA", "WHIP", "K/BB"}
_ONE_DEC_STATS = {"K/9", "BB/9", "H/9", "HR/9"}

# Pitching stats where lower values are better
_LOWER_IS_BETTER_PITCHING = {"ERA", "WHIP", "BB/9", "H/9", "HR/9", "BAA"}

# Pitching stat DB columns where lower is better (for milestone queries)
_LOWER_IS_BETTER_COLUMNS = {"era", "whip", "bb_per_9", "hits_per_9", "hr_per_9"}


# ===================================================================
# Helpers
# ===================================================================

def _get_db() -> sqlite3.Connection:
    """Return a read-only connection to the stats DB."""
    return sqlite3.connect(DB_PATH, timeout=10)


def _sanitize(name: str) -> str:
    """Escape single quotes for safe SQL interpolation."""
    return name.replace("'", "''")


def _format_rate(value) -> str:
    """Format a rate stat: '.302' not '0.302', but '1.052' stays."""
    try:
        num = float(value)
    except (ValueError, TypeError):
        return str(value) if value is not None else ""
    s = f"{num:.3f}"
    if s.startswith("0."):
        return s[1:]
    if s.startswith("-0."):
        return "-" + s[2:]
    return s


def _format_pitching_rate(value, decimals: int = 2) -> str:
    """Format a pitching rate stat to the given decimal places."""
    try:
        num = float(value)
    except (ValueError, TypeError):
        return str(value) if value is not None else ""
    return f"{num:.{decimals}f}"


def _format_values(headers: list[str], values: list) -> list[str]:
    """Format a row of values according to header names (batting context)."""
    formatted = []
    for idx, val in enumerate(values):
        v = str(val) if val is not None else ""
        if idx < len(headers) and headers[idx] in _RATE_STATS:
            formatted.append(_format_rate(v))
        else:
            formatted.append(v)
    return formatted


def _format_pitching_values(headers: list[str], values: list) -> list[str]:
    """Format a row of values according to header names (pitching context)."""
    formatted = []
    for idx, val in enumerate(values):
        v = str(val) if val is not None else ""
        if idx < len(headers):
            h = headers[idx]
            if h in _TWO_DEC_STATS:
                formatted.append(_format_pitching_rate(v, 2))
            elif h in _ONE_DEC_STATS:
                formatted.append(_format_pitching_rate(v, 1))
            elif h in ("BAA",):
                formatted.append(_format_rate(v))
            else:
                formatted.append(v)
        else:
            formatted.append(v)
    return formatted


def _filter_pitching_for_display(values: list[str]) -> list[str]:
    """Filter full pitching values (aligned to PITCHING_ALL_HEADERS) to display set."""
    return [
        values[i]
        for i, h in enumerate(PITCHING_ALL_HEADERS)
        if h not in PITCHING_HIDDEN and i < len(values)
    ]


def _split_pa_floor(conn, season: int, table: str,
                    filter_col, filter_values,
                    is_pitching: bool) -> tuple[int, str]:
    """Compute the in-split sample-size floor for rate-stat leaderboards.

    Formula:
        floor = STANDARD_QUAL × occurrence_rate × (max_games / 162)

    Where:
      - STANDARD_QUAL is the regular full-season qualifier in the column's
        units (400 PA batting, 650 BF or 486 outs pitching).
      - occurrence_rate is the split's share of the league's universe of
        PAs / BF / outs this season — pulled from the canonical season
        stats table so single-split tables (e.g., first_pa_batting_splits)
        get a real ~25% denominator rather than 1.0.
      - max_games / 162 prorates by season progress.

    Returns (floor, qual_col) where qual_col is the column the caller
    should filter on (plate_appearances or ip_outs).
    """
    qual_table = "season_pitching_stats" if is_pitching else "season_batting_stats"
    cur = conn.cursor()
    cur.execute(f"SELECT MAX(games) FROM {qual_table} WHERE season = ?", (season,))
    r = cur.fetchone()
    max_games = int(r[0]) if r and r[0] else 162

    use_outs = (table == "pitching_home_away_splits")
    qual_col = "ip_outs" if use_outs else "plate_appearances"

    # Pitching: ~162 IP × 3 outs/inning = 486 outs; × ~4 BF/inning ≈ 650 BF.
    if is_pitching:
        standard_qual = 486 if use_outs else 650
    else:
        standard_qual = 400

    # Numerator: the split's actual PAs/outs this season.
    if filter_col and filter_values:
        placeholders = ", ".join("?" * len(filter_values))
        cur.execute(
            f"SELECT COALESCE(SUM({qual_col}), 0) FROM {table} "
            f"WHERE season = ? AND {filter_col} IN ({placeholders})",
            [season, *filter_values],
        )
    else:
        cur.execute(
            f"SELECT COALESCE(SUM({qual_col}), 0) FROM {table} WHERE season = ?",
            (season,),
        )
    in_split = cur.fetchone()[0] or 0

    # Denominator: league universe from the canonical season totals.
    # Pitching split tables store PA; season_pitching_stats stores BF.
    if is_pitching and not use_outs:
        canon_col = "batters_faced"
    else:
        canon_col = qual_col
    cur.execute(
        f"SELECT COALESCE(SUM({canon_col}), 0) FROM {qual_table} WHERE season = ?",
        (season,),
    )
    total = cur.fetchone()[0] or 0

    if total <= 0:
        return (1, qual_col)
    occurrence = in_split / total
    return (max(1, int(standard_qual * occurrence * max_games / 162)), qual_col)


def _league_team_clause(league: str, alias: str) -> str:
    """Return a SQL clause filtering by AL or NL team codes."""
    teams = _AL_TEAMS if league == "AL" else _NL_TEAMS
    return f"{alias}.team IN ({teams})"


def _rookie_filter(prefix: str, is_pitching: bool = False) -> str:
    """Return SQL filter clause for rookies.

    A rookie in season X has no prior season with 130+ AB (batting) or
    150+ ip_outs (pitching).
    """
    return (
        f" AND NOT EXISTS ("
        f"SELECT 1 FROM season_batting_stats s2 "
        f"WHERE s2.player_id = {prefix}.player_id AND s2.season < {prefix}.season "
        f"AND s2.at_bats >= 130) "
        f"AND NOT EXISTS ("
        f"SELECT 1 FROM season_pitching_stats sp2 "
        f"WHERE sp2.player_id = {prefix}.player_id AND sp2.season < {prefix}.season "
        f"AND sp2.ip_outs >= 150)"
    )


def _position_filter_career(positions: list[str], stats_prefix: str) -> str:
    """Player-level position filter for CAREER / all-time rollup queries.

    A player is included if their single most-played career position (by total
    games across all seasons in season_fielding_stats) is in the filter set.
    This matches Baseball Reference's "career leaders among catchers" semantic:
    Piazza's full career OPS (.922), not a catcher-only-seasons rollup (.947)
    that silently drops his late-career 1B/DH years.

    Why not use players.positions: that column is unreliable for primary-career
    position — Piazza is stored as "DH", Bench as "3B/1B/C/LF", etc. The order
    does not reflect career games played. We have to recompute from the fielding
    stats table to get the truth.

    Use with career/all-time scope. For per-season scope, _position_filter is
    still correct (seasons where the player was primarily at that position).
    """
    pos_list = ", ".join(f"'{p}'" for p in positions)
    return (
        f" AND (SELECT sf.position FROM season_fielding_stats sf "
        f"WHERE sf.player_id = {stats_prefix}.player_id "
        f"GROUP BY sf.position "
        f"ORDER BY SUM(sf.games) DESC LIMIT 1) IN ({pos_list})"
    )


def _position_filter(positions: list[str], stats_prefix: str, season_expr: str) -> str:
    """Return SQL filter clause for position-based queries.

    Uses season_fielding_stats when available (historical seasons).
    Falls back to players.positions for current season (no fielding data).
    positions: list of position codes, e.g. ["SS"] or ["LF", "CF", "RF"]
    stats_prefix: alias for the season_batting_stats/pitching table (e.g. "s")
    season_expr: expression for the season column (e.g. "s.season" or a literal)
    """
    pos_list = ", ".join(f"'{p}'" for p in positions)
    # Build OR conditions for players.positions (slash-separated: "C/DH/1B")
    # Only match PRIMARY position (first listed) as proxy for 50% games rule
    pos_like_parts = []
    for p in positions:
        pos_like_parts.append(f"(p.positions = '{p}' OR p.positions LIKE '{p}/%')")
    pos_like = " OR ".join(pos_like_parts)
    return (
        f" AND ("
        # Primary path: season_fielding_stats (historical, more accurate)
        f"EXISTS ("
        f"SELECT 1 FROM season_fielding_stats sf "
        f"WHERE sf.player_id = {stats_prefix}.player_id "
        f"AND sf.season = {season_expr} "
        f"AND sf.position IN ({pos_list}) "
        f"AND sf.games = ("
        f"SELECT MAX(sf2.games) FROM season_fielding_stats sf2 "
        f"WHERE sf2.player_id = sf.player_id AND sf2.season = sf.season)"
        f"AND sf.games >= ("
        f"SELECT MAX(s3.games) / 2 FROM season_batting_stats s3 "
        f"WHERE s3.season = sf.season AND s3.team = {stats_prefix}.team))"
        # Fallback: players.positions (current season, no fielding data)
        f" OR (NOT EXISTS ("
        f"SELECT 1 FROM season_fielding_stats sf "
        f"WHERE sf.player_id = {stats_prefix}.player_id "
        f"AND sf.season = {season_expr}) "
        f"AND ({pos_like})))"
    )


_POSITION_LABELS = {
    "C": "Catchers", "1B": "First Basemen", "2B": "Second Basemen",
    "3B": "Third Basemen", "SS": "Shortstops", "LF": "Left Fielders",
    "CF": "Center Fielders", "RF": "Right Fielders", "DH": "Designated Hitters",
    "P": "Pitchers",
}


def _position_label(positions: list[str]) -> str:
    """Human-readable label for position filter."""
    if len(positions) == 1:
        return _POSITION_LABELS.get(positions[0], positions[0])
    if set(positions) == {"LF", "CF", "RF"}:
        return "Outfielders"
    if set(positions) == {"1B", "2B", "3B", "SS"}:
        return "Infielders"
    return "/".join(positions)


def _format_date(date_string: str) -> str:
    """Convert '2024-06-12' to 'Jun 12'."""
    parts = date_string.split("-")
    if len(parts) != 3:
        return date_string
    try:
        month = int(parts[1])
        day = int(parts[2])
    except ValueError:
        return date_string
    month_names = [
        "", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
        "Jul", "Aug", "Sept", "Oct", "Nov", "Dec",
    ]
    if 1 <= month <= 12:
        return f"{month_names[month]} {day}"
    return date_string


def _team_full_name(abbreviation: str) -> str:
    """Map a Retrosheet (or common) team abbreviation to full name."""
    teams = {
        # Standard
        "ARI": "Arizona Diamondbacks", "ATL": "Atlanta Braves",
        "BAL": "Baltimore Orioles", "BOS": "Boston Red Sox",
        "CHC": "Chicago Cubs", "CHW": "Chicago White Sox",
        "CIN": "Cincinnati Reds", "CLE": "Cleveland Guardians",
        "COL": "Colorado Rockies", "DET": "Detroit Tigers",
        "HOU": "Houston Astros", "KCR": "Kansas City Royals",
        "LAA": "Los Angeles Angels", "LAD": "Los Angeles Dodgers",
        "MIA": "Miami Marlins", "MIL": "Milwaukee Brewers",
        "MIN": "Minnesota Twins", "NYM": "New York Mets",
        "NYY": "New York Yankees", "OAK": "Oakland Athletics",
        "PHI": "Philadelphia Phillies", "PIT": "Pittsburgh Pirates",
        "SDP": "San Diego Padres", "SFG": "San Francisco Giants",
        "SEA": "Seattle Mariners", "STL": "St. Louis Cardinals",
        "TBR": "Tampa Bay Rays", "TEX": "Texas Rangers",
        "TOR": "Toronto Blue Jays", "WSN": "Washington Nationals",
        # Retrosheet
        "NYA": "New York Yankees", "NYN": "New York Mets",
        "CHN": "Chicago Cubs", "CHA": "Chicago White Sox",
        "SLN": "St. Louis Cardinals", "SFN": "San Francisco Giants",
        "SDN": "San Diego Padres", "LAN": "Los Angeles Dodgers",
        "TBA": "Tampa Bay Rays", "KCA": "Kansas City Royals",
        "ANA": "Los Angeles Angels", "WAS": "Washington Nationals",
        "FLO": "Florida Marlins", "MON": "Montreal Expos",
        "ATH": "Oakland Athletics",
        # Historical
        "CAL": "California Angels", "KC1": "Kansas City Athletics",
        "ML1": "Milwaukee Braves", "BSN": "Boston Braves",
        "BRO": "Brooklyn Dodgers", "NYG": "New York Giants",
        "PHA": "Philadelphia Athletics", "SLA": "St. Louis Browns",
        "WS1": "Washington Senators", "WS2": "Washington Senators (1961)",
        "SE1": "Seattle Pilots", "ML4": "Milwaukee Brewers (AL)",
    }
    return teams.get(abbreviation, abbreviation)


def _team_nickname(code: str) -> str:
    """Extract the team nickname from a Retrosheet code (e.g., 'NYA' -> 'Yankees')."""
    full = _team_full_name(code)
    parts = full.split()
    if len(parts) >= 2:
        return parts[-1]
    return full


def _era_data_filter(prefix: str, stat: StatInfo, additional_stats: list = None) -> str:
    """Filter to exclude seasons with missing earned-runs data (1903-1908 Retrosheet)."""
    all_stats = [stat] + (additional_stats or [])
    era_related = {"era", "earned_runs", "whip", "era_plus"}
    if any(s.db_column in era_related for s in all_stats):
        return f" AND NOT ({prefix}.earned_runs = 0 AND {prefix}.ip_outs > 0)"
    return ""


def _get_player_info(conn: sqlite3.Connection, name: str):
    """Return (display_name, team) or (name, '') if not found."""
    cur = conn.cursor()
    cur.execute(
        "SELECT p.name, p.team FROM players p WHERE p.name = ? LIMIT 1",
        (_sanitize(name),),
    )
    row = cur.fetchone()
    if row:
        return row[0], row[1] or ""
    return name, ""


def _resolve_season(conn: sqlite3.Connection, name: str, requested: int,
                    table: str = "season_batting_stats", alias: str = "s") -> int:
    """If requested season has no data for this player, return their most recent season."""
    cur = conn.cursor()
    cur.execute(
        f"SELECT 1 FROM {table} {alias} "
        f"JOIN players p ON {alias}.player_id = p.player_id "
        f"WHERE p.name = ? AND {alias}.season = ? LIMIT 1",
        (_sanitize(name), requested),
    )
    if cur.fetchone():
        return requested
    # Fall back to most recent
    cur.execute(
        f"SELECT {alias}.season FROM {table} {alias} "
        f"JOIN players p ON {alias}.player_id = p.player_id "
        f"WHERE p.name = ? ORDER BY {alias}.season DESC LIMIT 1",
        (_sanitize(name),),
    )
    row = cur.fetchone()
    return row[0] if row else requested


def _most_recent_season(conn: sqlite3.Connection, name: str,
                        table: str = "season_batting_stats") -> int:
    """Return the most recent season year for a player, defaulting to current year."""
    cur = conn.cursor()
    cur.execute(
        f"SELECT MAX(s.season) FROM {table} s "
        f"JOIN players p ON s.player_id = p.player_id WHERE p.name = ?",
        (_sanitize(name),),
    )
    row = cur.fetchone()
    if row and row[0]:
        return int(row[0])
    return datetime.now().year


def _count_group_label(counts: Optional[list[str]]) -> Optional[str]:
    """Map a list of count states to a named group label."""
    if not counts:
        return None
    s = set(counts)
    if s == {"0-2", "1-2", "2-2", "3-2"}:
        return "Two Strikes"
    if s == {"1-0", "2-0", "2-1", "3-0", "3-1"}:
        return "Ahead"
    if s == {"0-1", "0-2", "1-2"}:
        return "Behind"
    return None


def _is_active_player(conn: sqlite3.Connection, name: str) -> bool:
    """Check if a player has data in the current or previous year."""
    current_year = datetime.now().year
    cur = conn.cursor()
    cur.execute(
        "SELECT MAX(s.season) FROM season_batting_stats s "
        "JOIN players p ON s.player_id = p.player_id WHERE p.name = ?",
        (_sanitize(name),),
    )
    row = cur.fetchone()
    bat_year = int(row[0]) if row and row[0] else 0
    cur.execute(
        "SELECT MAX(sp.season) FROM season_pitching_stats sp "
        "JOIN players p ON sp.player_id = p.player_id WHERE p.name = ?",
        (_sanitize(name),),
    )
    row = cur.fetchone()
    pitch_year = int(row[0]) if row and row[0] else 0
    return max(bat_year, pitch_year) >= current_year - 1


def _has_platoon_data(conn: sqlite3.Connection, name: str) -> bool:
    """Check if platoon split data exists for a player (1969+ Chadwick or 2025+ MSF)."""
    most_recent = _most_recent_season(conn, name)
    return most_recent >= 1969


def _career_rate_formula(stat: StatInfo) -> Optional[str]:
    """Return SQL expression for career rate stat, or None."""
    formulas = {
        "AVG": "ROUND(CAST(SUM(s.hits) AS REAL) / NULLIF(SUM(s.at_bats), 0), 3)",
        "OBP": ("ROUND(CAST(SUM(s.hits) + SUM(s.walks) + SUM(s.hit_by_pitch) AS REAL) / "
                "NULLIF(SUM(s.at_bats) + SUM(s.walks) + SUM(s.hit_by_pitch) + SUM(s.sacrifice_flies), 0), 3)"),
        "SLG": ("ROUND(CAST((SUM(s.hits) - SUM(s.doubles) - SUM(s.triples) - SUM(s.home_runs)) + "
                "2 * SUM(s.doubles) + 3 * SUM(s.triples) + 4 * SUM(s.home_runs) AS REAL) / "
                "NULLIF(SUM(s.at_bats), 0), 3)"),
        "OPS": ("ROUND(CAST(SUM(s.hits) + SUM(s.walks) + SUM(s.hit_by_pitch) AS REAL) / "
                "NULLIF(SUM(s.at_bats) + SUM(s.walks) + SUM(s.hit_by_pitch) + SUM(s.sacrifice_flies), 0) + "
                "CAST((SUM(s.hits) - SUM(s.doubles) - SUM(s.triples) - SUM(s.home_runs)) + "
                "2 * SUM(s.doubles) + 3 * SUM(s.triples) + 4 * SUM(s.home_runs) AS REAL) / "
                "NULLIF(SUM(s.at_bats), 0), 3)"),
        "ISO": ("ROUND(CAST(SUM(s.doubles) + 2 * SUM(s.triples) + 3 * SUM(s.home_runs) AS REAL) / "
                "NULLIF(SUM(s.at_bats), 0), 3)"),
        "BABIP": ("ROUND(CAST(SUM(s.hits) - SUM(s.home_runs) AS REAL) / "
                  "NULLIF(SUM(s.at_bats) - SUM(s.strikeouts) - SUM(s.home_runs) + SUM(s.sacrifice_flies), 0), 3)"),
    }
    return formulas.get(stat.display_abbrev)


def _career_pitching_rate_formula(stat: StatInfo) -> Optional[str]:
    """Return SQL expression for career pitching rate stat, or None."""
    formulas = {
        "ERA": "ROUND(9.0 * CAST(SUM(sp.earned_runs) AS REAL) / NULLIF(SUM(sp.ip_outs) / 3.0, 0), 2)",
        "WHIP": "ROUND(CAST(SUM(sp.walks) + SUM(sp.hits) AS REAL) / NULLIF(SUM(sp.ip_outs) / 3.0, 0), 2)",
        "K/9": "ROUND(9.0 * CAST(SUM(sp.strikeouts) AS REAL) / NULLIF(SUM(sp.ip_outs) / 3.0, 0), 1)",
        "BB/9": "ROUND(9.0 * CAST(SUM(sp.walks) AS REAL) / NULLIF(SUM(sp.ip_outs) / 3.0, 0), 1)",
        "K/BB": "ROUND(CAST(SUM(sp.strikeouts) AS REAL) / NULLIF(SUM(sp.walks), 0), 2)",
        "H/9": "ROUND(9.0 * CAST(SUM(sp.hits) AS REAL) / NULLIF(SUM(sp.ip_outs) / 3.0, 0), 1)",
        "HR/9": "ROUND(9.0 * CAST(SUM(sp.home_runs) AS REAL) / NULLIF(SUM(sp.ip_outs) / 3.0, 0), 1)",
        "BAA": ("ROUND(CAST(SUM(sp.hits) AS REAL) / NULLIF(SUM(sp.batters_faced) - SUM(sp.walks) - "
                "SUM(sp.hit_by_pitch) - SUM(sp.sacrifice_hits) - SUM(sp.sacrifice_flies), 0), 3)"),
    }
    return formulas.get(stat.display_abbrev)


# ===================================================================
# 1. build_season_summary
# ===================================================================

def build_season_summary(name: str, season: int) -> Optional[str]:
    """Batting season stats as a [STATGRID] block. Combines multi-team seasons."""
    conn = _get_db()
    try:
        season = _resolve_season(conn, name, season)
        display_name, _ = _get_player_info(conn, name)
        cur = conn.cursor()
        # Aggregate across teams for traded players
        cur.execute(
            "SELECT GROUP_CONCAT(DISTINCT s.team), "
            "SUM(s.games), SUM(s.at_bats), SUM(s.runs), SUM(s.hits), "
            "SUM(s.doubles), SUM(s.triples), SUM(s.home_runs), SUM(s.rbi), "
            "SUM(s.stolen_bases), SUM(s.caught_stealing), "
            "SUM(s.walks), SUM(s.intentional_walks), SUM(s.strikeouts), SUM(s.hit_by_pitch), "
            "SUM(s.sacrifice_flies), SUM(s.plate_appearances) "
            "FROM season_batting_stats s "
            "JOIN players p ON s.player_id = p.player_id "
            "WHERE p.name = ? AND s.season = ?",
            (_sanitize(name), season),
        )
        row = cur.fetchone()
        if not row or not row[1]:
            return None

        teams_str = row[0] or ""
        g, ab, r, h = row[1], row[2], row[3], row[4]
        d, t, hr, rbi = row[5], row[6], row[7], row[8]
        sb, cs = row[9], row[10]
        bb, ibb, so, hbp = row[11], row[12], row[13], row[14]
        sf, pa = row[15] or 0, row[16] or 0

        # Compute rate stats from aggregated counting stats
        avg = h / ab if ab else 0
        obp_denom = ab + bb + (hbp or 0) + sf
        obp = (h + bb + (hbp or 0)) / obp_denom if obp_denom else 0
        singles = h - (d or 0) - (t or 0) - (hr or 0)
        slg = (singles + 2*(d or 0) + 3*(t or 0) + 4*(hr or 0)) / ab if ab else 0
        ops = obp + slg
        iso = slg - avg if avg else 0
        babip_denom = ab - so - hr + sf
        babip = (h - hr) / babip_denom if babip_denom > 0 else 0

        # OPS+ — use the stored value if single team, otherwise skip
        ops_plus = "--"
        if "/" not in teams_str:
            single_row = cur.execute(
                "SELECT s.ops_plus FROM season_batting_stats s "
                "JOIN players p ON s.player_id = p.player_id "
                "WHERE p.name = ? AND s.season = ? LIMIT 1",
                (_sanitize(name), season),
            ).fetchone()
            if single_row and single_row[0] is not None:
                ops_plus = str(single_row[0])

        # Format team display
        team_codes = [t.strip() for t in teams_str.split(",")]
        if len(team_codes) > 1:
            team_display_str = "/".join(_team_full_name(t) for t in team_codes)
        else:
            team_display_str = _team_full_name(team_codes[0])

        values = [g, ab, r, h, d, t, hr, rbi, sb, cs, bb, ibb, so, hbp,
                  avg, obp, slg, ops, ops_plus, iso, babip]
        formatted = _format_values(BATTING_HEADERS, [str(v) if v is not None else "" for v in values])

        parts = []
        parts.append(f"**{display_name}** \u2014 {season} Season ({team_display_str})\n")
        parts.append("[STATGRID]")
        parts.append("HEADER: " + ", ".join(BATTING_HEADERS))
        parts.append("ROW: " + ", ".join(formatted))
        parts.append("[/STATGRID]")

        if _is_active_player(conn, name):
            parts.append(f"\n[SUGGEST]how is {display_name} doing lately[/SUGGEST]")
        parts.append(f"[SUGGEST]{display_name} career[/SUGGEST]")

        return "\n".join(parts)
    finally:
        conn.close()


# ===================================================================
# 2. build_pitching_season_summary
# ===================================================================

def build_pitching_season_summary(name: str, season: int) -> Optional[str]:
    """Pitching season stats as a [STATGRID] block. Combines multi-team seasons."""
    conn = _get_db()
    try:
        season = _resolve_season(conn, name, season, "season_pitching_stats", "sp")
        display_name, _ = _get_player_info(conn, name)
        cur = conn.cursor()
        # Aggregate across teams for traded pitchers
        cur.execute(
            "SELECT GROUP_CONCAT(DISTINCT sp.team), "
            "SUM(sp.wins), SUM(sp.losses), SUM(sp.saves), SUM(sp.games), SUM(sp.games_started), "
            "SUM(sp.games_finished), SUM(sp.complete_games), SUM(sp.quality_starts), "
            "SUM(sp.ip_outs), "
            "SUM(sp.hits), SUM(sp.runs), SUM(sp.earned_runs), SUM(sp.home_runs), "
            "SUM(sp.walks), SUM(sp.intentional_walks), "
            "SUM(sp.strikeouts), SUM(sp.hit_by_pitch), SUM(sp.wild_pitches), SUM(sp.balks), "
            "SUM(sp.batters_faced), SUM(sp.sacrifice_hits), SUM(sp.sacrifice_flies), "
            "SUM(sp.stolen_bases), SUM(sp.caught_stealing) "
            "FROM season_pitching_stats sp "
            "JOIN players p ON sp.player_id = p.player_id "
            "WHERE p.name = ? AND sp.season = ?",
            (_sanitize(name), season),
        )
        row = cur.fetchone()
        if not row or not row[1]:
            return None

        teams_str = row[0] or ""
        w, l, sv, g, gs = row[1], row[2], row[3], row[4], row[5]
        gf, cg, qs = row[6], row[7], row[8]
        ip_outs = row[9] or 0
        h, r, er, hr = row[10], row[11], row[12], row[13]
        bb, ibb, so, hbp = row[14], row[15], row[16], row[17]
        wp, bk = row[18], row[19]
        bf, sh, sf = row[20], row[21], row[22]
        sb_p, cs_p = row[23], row[24]

        # Compute rate stats
        ip = ip_outs / 3.0 if ip_outs else 0
        ip_display = f"{ip_outs // 3}.{ip_outs % 3}" if ip_outs else "0.0"
        era = (er * 9.0) / ip if ip > 0 else 0
        whip = (h + bb) / ip if ip > 0 else 0
        k9 = (so * 9.0) / ip if ip > 0 else 0
        bb9 = (bb * 9.0) / ip if ip > 0 else 0
        k_bb = so / bb if bb > 0 else 0
        h9 = (h * 9.0) / ip if ip > 0 else 0
        hr9 = (hr * 9.0) / ip if ip > 0 else 0
        baa_denom = bf - bb - (hbp or 0) - (sh or 0) - (sf or 0) if bf else 0
        baa = h / baa_denom if baa_denom > 0 else 0

        # ERA+ from single team if not traded
        era_plus = "--"
        if "," not in teams_str:
            single_row = cur.execute(
                "SELECT sp.era_plus FROM season_pitching_stats sp "
                "JOIN players p ON sp.player_id = p.player_id "
                "WHERE p.name = ? AND sp.season = ? LIMIT 1",
                (_sanitize(name), season),
            ).fetchone()
            if single_row and single_row[0] is not None:
                era_plus = str(single_row[0])

        # Format team display
        team_codes = [t.strip() for t in teams_str.split(",")]
        if len(team_codes) > 1:
            team_display_str = "/".join(_team_full_name(t) for t in team_codes)
        else:
            team_display_str = _team_full_name(team_codes[0])

        # Build values matching PITCHING_ALL_HEADERS order
        values = [str(v) if v is not None else "" for v in [
            w, l, sv, g, gs, gf, cg, qs, ip_display,
            h, r, er, hr, bb, ibb, so, hbp, wp, bk,
            bf, sh, sf, sb_p, cs_p,
            f"{era:.2f}", f"{whip:.2f}", f"{k9:.1f}", f"{bb9:.1f}", f"{k_bb:.1f}",
            f"{h9:.1f}", f"{hr9:.1f}", f"{baa:.3f}", era_plus,
        ]]
        formatted = _format_pitching_values(PITCHING_ALL_HEADERS, values)
        display_values = _filter_pitching_for_display(formatted)

        parts = []
        parts.append(f"**{display_name}** \u2014 {season} Season ({team_display_str})\n")
        parts.append("[STATGRID]")
        parts.append("HEADER: " + ", ".join(PITCHING_HEADERS))
        parts.append("ROW: " + ", ".join(display_values))
        parts.append("[/STATGRID]")

        if _is_active_player(conn, name):
            parts.append(f"\n[SUGGEST]how is {display_name} doing lately[/SUGGEST]")
        parts.append(f"[SUGGEST]{display_name} career[/SUGGEST]")

        return "\n".join(parts)
    finally:
        conn.close()


# ===================================================================
# 3. build_comparison
# ===================================================================

def _aggregate_batting_season(conn, name, year):
    """Aggregate batting stats across teams for a season. Returns (year, formatted_values) or None."""
    cur = conn.cursor()
    cur.execute(
        "SELECT SUM(s.games), SUM(s.at_bats), SUM(s.runs), SUM(s.hits), "
        "SUM(s.doubles), SUM(s.triples), SUM(s.home_runs), SUM(s.rbi), "
        "SUM(s.stolen_bases), SUM(s.caught_stealing), "
        "SUM(s.walks), SUM(s.intentional_walks), SUM(s.strikeouts), SUM(s.hit_by_pitch), "
        "SUM(s.sacrifice_flies), SUM(s.plate_appearances) "
        "FROM season_batting_stats s "
        "JOIN players p ON s.player_id = p.player_id "
        "WHERE p.name = ? AND s.season = ?",
        (_sanitize(name), year),
    )
    row = cur.fetchone()
    if not row or not row[0]:
        return None
    g, ab, r, h, d, t, hr, rbi, sb, cs, bb, ibb, so, hbp, sf, pa = row
    avg = h / ab if ab else 0
    obp_d = ab + bb + (hbp or 0) + (sf or 0)
    obp = (h + bb + (hbp or 0)) / obp_d if obp_d else 0
    singles = h - (d or 0) - (t or 0) - (hr or 0)
    slg = (singles + 2*(d or 0) + 3*(t or 0) + 4*(hr or 0)) / ab if ab else 0
    ops = obp + slg
    iso = slg - avg
    babip_d = ab - so - hr + (sf or 0)
    babip = (h - hr) / babip_d if babip_d > 0 else 0
    # OPS+ from single-team row (skip for multi-team)
    ops_plus_row = cur.execute(
        "SELECT s.ops_plus FROM season_batting_stats s "
        "JOIN players p ON s.player_id = p.player_id "
        "WHERE p.name = ? AND s.season = ? ORDER BY s.plate_appearances DESC LIMIT 1",
        (_sanitize(name), year),
    ).fetchone()
    ops_plus = ops_plus_row[0] if ops_plus_row and ops_plus_row[0] is not None else "--"
    values = [str(v) if v is not None else "" for v in
              [g, ab, r, h, d, t, hr, rbi, sb, cs, bb, ibb, so, hbp,
               avg, obp, slg, ops, ops_plus, iso, babip]]
    return year, _format_values(BATTING_HEADERS, values)


def _fetch_season_row(conn, name, year):
    """Fetch a specific season's formatted batting values. Aggregates across teams."""
    return _aggregate_batting_season(conn, name, year)


def _fetch_latest_season_row(conn, name):
    """Fetch latest season's formatted batting values. Aggregates across teams."""
    cur = conn.cursor()
    cur.execute(
        "SELECT MAX(s.season) FROM season_batting_stats s "
        "JOIN players p ON s.player_id = p.player_id "
        "WHERE p.name = ?",
        (_sanitize(name),),
    )
    row = cur.fetchone()
    if not row or not row[0]:
        return None
    yr = int(row[0])
    return _aggregate_batting_season(conn, name, yr)



def _fetch_career_row(conn, name):
    """Fetch career aggregate batting values (19 raw + computed OPS/OPS+)."""
    cur = conn.cursor()
    cur.execute(
        "SELECT SUM(s.games), SUM(s.at_bats), SUM(s.runs), SUM(s.hits), "
        "SUM(s.doubles), SUM(s.triples), SUM(s.home_runs), SUM(s.rbi), "
        "SUM(s.stolen_bases), SUM(s.caught_stealing), "
        "SUM(s.walks), SUM(s.intentional_walks), SUM(s.strikeouts), SUM(s.hit_by_pitch), "
        "ROUND(CAST(SUM(s.hits) AS REAL) / NULLIF(SUM(s.at_bats), 0), 3), "
        "ROUND((CAST(SUM(s.hits) + SUM(s.walks) + SUM(s.hit_by_pitch) AS REAL) / "
        "  NULLIF(SUM(s.at_bats) + SUM(s.walks) + SUM(s.hit_by_pitch) + SUM(s.sacrifice_flies), 0)), 3), "
        "ROUND(CAST(SUM(s.hits - s.doubles - s.triples - s.home_runs) + "
        "  2 * SUM(s.doubles) + 3 * SUM(s.triples) + 4 * SUM(s.home_runs) AS REAL) / "
        "  NULLIF(SUM(s.at_bats), 0), 3), "
        "ROUND(CAST(SUM(s.hits - s.doubles - s.triples - s.home_runs) + "
        "  2 * SUM(s.doubles) + 3 * SUM(s.triples) + 4 * SUM(s.home_runs) AS REAL) / "
        "  NULLIF(SUM(s.at_bats), 0) - "
        "  CAST(SUM(s.hits) AS REAL) / NULLIF(SUM(s.at_bats), 0), 3), "
        "ROUND(CAST(SUM(s.hits) - SUM(s.home_runs) AS REAL) / "
        "  NULLIF(SUM(s.at_bats) - SUM(s.strikeouts) - SUM(s.home_runs) + SUM(s.sacrifice_flies), 0), 3) "
        "FROM season_batting_stats s "
        "JOIN players p ON s.player_id = p.player_id "
        "WHERE p.name = ? "
        "GROUP BY p.player_id "
        "HAVING COUNT(DISTINCT s.season) > 1",
        (_sanitize(name),),
    )
    row = cur.fetchone()
    if not row:
        return None

    # row: 14 counting + AVG, OBP, SLG, ISO, BABIP (no OPS, no OPS+)
    headers_no_ops = [h for h in BATTING_HEADERS if h not in ("OPS", "OPS+")]
    values = [str(v) if v is not None else "" for v in row]
    formatted = _format_values(headers_no_ops, values)

    # Insert OPS after SLG (index 16), then OPS+ after OPS
    if len(formatted) >= 17:
        try:
            obp_val = float(formatted[15])
            slg_val = float(formatted[16])
        except (ValueError, IndexError):
            obp_val, slg_val = 0, 0
        ops = _format_rate(f"{obp_val + slg_val:.3f}")
        formatted.insert(17, ops)
        formatted.insert(18, "--")  # Career OPS+ not computed

    return formatted


def build_comparison(name1: str, name2: str, season: Optional[int] = None) -> Optional[str]:
    """Side-by-side batting comparison with [STATGRID] blocks."""
    conn = _get_db()
    try:
        header = "HEADER: " + ", ".join(BATTING_HEADERS)

        s1 = (_fetch_season_row(conn, name1, season) if season
              else None) or _fetch_latest_season_row(conn, name1)
        s2 = (_fetch_season_row(conn, name2, season) if season
              else None) or _fetch_latest_season_row(conn, name2)

        c1 = _fetch_career_row(conn, name1)
        c2 = _fetch_career_row(conn, name2)

        dn1, _ = _get_player_info(conn, name1)
        dn2, _ = _get_player_info(conn, name2)

        parts = []

        if s1 and s2:
            if s1[0] == s2[0]:
                parts.append(f"{s1[0]} Season:\n")
                parts.append("[STATGRID]")
                parts.append(header)
                parts.append(f"ROW: {dn1}, " + ", ".join(s1[1]))
                parts.append(f"ROW: {dn2}, " + ", ".join(s2[1]))
                parts.append("[/STATGRID]")
            else:
                parts.append("Best Seasons:\n")
                parts.append("[STATGRID]")
                parts.append(header)
                parts.append(f"ROW: {dn1} ({s1[0]}), " + ", ".join(s1[1]))
                parts.append(f"ROW: {dn2} ({s2[0]}), " + ", ".join(s2[1]))
                parts.append("[/STATGRID]")

        # Career grid — only when no specific season was requested
        if not season and c1 and c2:
            parts.append("\nCareer:\n")
            parts.append("[STATGRID]")
            parts.append(header)
            parts.append(f"ROW: {dn1}, " + ", ".join(c1))
            parts.append(f"ROW: {dn2}, " + ", ".join(c2))
            parts.append("[/STATGRID]")

        if not parts:
            return "I don't have enough data to compare these two players."

        if _has_platoon_data(conn, name1):
            parts.append(f"\n[SUGGEST]{dn1} vs lefties[/SUGGEST]")
        if _has_platoon_data(conn, name2):
            parts.append(f"[SUGGEST]{dn2} vs lefties[/SUGGEST]")

        return "\n".join(parts)
    finally:
        conn.close()


# ===================================================================
# 4. build_pitching_comparison
# ===================================================================

def _aggregate_pitching_season(conn, name, year):
    """Aggregate pitching stats across teams for a season. Returns (year, display_values) or None."""
    cur = conn.cursor()
    cur.execute(
        "SELECT SUM(sp.wins), SUM(sp.losses), SUM(sp.saves), SUM(sp.games), SUM(sp.games_started), "
        "SUM(sp.games_finished), SUM(sp.complete_games), SUM(sp.quality_starts), "
        "SUM(sp.ip_outs), "
        "SUM(sp.hits), SUM(sp.runs), SUM(sp.earned_runs), SUM(sp.home_runs), "
        "SUM(sp.walks), SUM(sp.intentional_walks), "
        "SUM(sp.strikeouts), SUM(sp.hit_by_pitch), SUM(sp.wild_pitches), SUM(sp.balks), "
        "SUM(sp.batters_faced), SUM(sp.sacrifice_hits), SUM(sp.sacrifice_flies), "
        "SUM(sp.stolen_bases), SUM(sp.caught_stealing) "
        "FROM season_pitching_stats sp "
        "JOIN players p ON sp.player_id = p.player_id "
        "WHERE p.name = ? AND sp.season = ?",
        (_sanitize(name), year),
    )
    row = cur.fetchone()
    if not row or not row[0]:
        return None
    w, l, sv, g, gs, gf, cg, qs = row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7]
    ip_outs = row[8] or 0
    h, r, er, hr = row[9], row[10], row[11], row[12]
    bb, ibb, so, hbp = row[13], row[14], row[15], row[16]
    wp, bk, bf, sh, sf = row[17], row[18], row[19], row[20], row[21]
    sb_p, cs_p = row[22], row[23]
    ip = ip_outs / 3.0 if ip_outs else 0
    ip_display = f"{ip_outs // 3}.{ip_outs % 3}" if ip_outs else "0.0"
    era = (er * 9.0) / ip if ip > 0 else 0
    whip = (h + bb) / ip if ip > 0 else 0
    k9 = (so * 9.0) / ip if ip > 0 else 0
    bb9 = (bb * 9.0) / ip if ip > 0 else 0
    k_bb = so / bb if bb else 0
    h9 = (h * 9.0) / ip if ip > 0 else 0
    hr9 = (hr * 9.0) / ip if ip > 0 else 0
    baa_d = bf - bb - (hbp or 0) - (sh or 0) - (sf or 0) if bf else 0
    baa = h / baa_d if baa_d > 0 else 0
    era_plus_row = cur.execute(
        "SELECT sp.era_plus FROM season_pitching_stats sp "
        "JOIN players p ON sp.player_id = p.player_id "
        "WHERE p.name = ? AND sp.season = ? ORDER BY sp.ip_outs DESC LIMIT 1",
        (_sanitize(name), year),
    ).fetchone()
    era_plus = era_plus_row[0] if era_plus_row and era_plus_row[0] is not None else "--"
    values = [str(v) if v is not None else "" for v in [
        w, l, sv, g, gs, gf, cg, qs, ip_display,
        h, r, er, hr, bb, ibb, so, hbp, wp, bk,
        bf, sh, sf, sb_p, cs_p,
        f"{era:.2f}", f"{whip:.2f}", f"{k9:.1f}", f"{bb9:.1f}", f"{k_bb:.1f}",
        f"{h9:.1f}", f"{hr9:.1f}", f"{baa:.3f}", era_plus,
    ]]
    formatted = _format_pitching_values(PITCHING_ALL_HEADERS, values)
    return year, _filter_pitching_for_display(formatted)


def _fetch_pitching_season_row(conn, name, year):
    """Fetch a specific season's formatted pitching values. Aggregates across teams."""
    return _aggregate_pitching_season(conn, name, year)


def _fetch_pitching_latest_season_row(conn, name):
    """Fetch latest pitching season. Aggregates across teams."""
    cur = conn.cursor()
    cur.execute(
        "SELECT MAX(sp.season) FROM season_pitching_stats sp "
        "JOIN players p ON sp.player_id = p.player_id "
        "WHERE p.name = ?",
        (_sanitize(name),),
    )
    row = cur.fetchone()
    if not row or not row[0]:
        return None
    return _aggregate_pitching_season(conn, name, int(row[0]))


def _fetch_pitching_career_row(conn, name):
    """Fetch career aggregate pitching values."""
    cur = conn.cursor()
    cur.execute(
        "SELECT SUM(sp.wins), SUM(sp.losses), SUM(sp.saves), "
        "SUM(sp.games), SUM(sp.games_started), SUM(sp.games_finished), "
        "SUM(sp.complete_games), SUM(sp.quality_starts), "
        "CAST(SUM(sp.ip_outs) / 3 AS TEXT) || '.' || CAST(SUM(sp.ip_outs) % 3 AS TEXT), "
        "SUM(sp.hits), SUM(sp.runs), SUM(sp.earned_runs), SUM(sp.home_runs), "
        "SUM(sp.walks), SUM(sp.intentional_walks), "
        "SUM(sp.strikeouts), SUM(sp.hit_by_pitch), SUM(sp.wild_pitches), SUM(sp.balks), "
        "SUM(sp.batters_faced), SUM(sp.sacrifice_hits), SUM(sp.sacrifice_flies), "
        "SUM(sp.stolen_bases), SUM(sp.caught_stealing), "
        "ROUND(9.0 * CAST(SUM(sp.earned_runs) AS REAL) / NULLIF(SUM(sp.ip_outs) / 3.0, 0), 2), "
        "ROUND(CAST(SUM(sp.walks) + SUM(sp.hits) AS REAL) / NULLIF(SUM(sp.ip_outs) / 3.0, 0), 2), "
        "ROUND(9.0 * CAST(SUM(sp.strikeouts) AS REAL) / NULLIF(SUM(sp.ip_outs) / 3.0, 0), 1), "
        "ROUND(9.0 * CAST(SUM(sp.walks) AS REAL) / NULLIF(SUM(sp.ip_outs) / 3.0, 0), 1), "
        "ROUND(CAST(SUM(sp.strikeouts) AS REAL) / NULLIF(SUM(sp.walks), 0), 2), "
        "ROUND(9.0 * CAST(SUM(sp.hits) AS REAL) / NULLIF(SUM(sp.ip_outs) / 3.0, 0), 1), "
        "ROUND(9.0 * CAST(SUM(sp.home_runs) AS REAL) / NULLIF(SUM(sp.ip_outs) / 3.0, 0), 1), "
        "ROUND(CAST(SUM(sp.hits) AS REAL) / NULLIF(SUM(sp.batters_faced) - SUM(sp.walks) - "
        "  SUM(sp.hit_by_pitch) - SUM(sp.sacrifice_hits) - SUM(sp.sacrifice_flies), 0), 3) "
        "FROM season_pitching_stats sp "
        "JOIN players p ON sp.player_id = p.player_id "
        "WHERE p.name = ? "
        "GROUP BY p.player_id "
        "HAVING COUNT(DISTINCT sp.season) > 1",
        (_sanitize(name),),
    )
    row = cur.fetchone()
    if not row:
        return None
    # 32 values (no ERA+); format them
    headers_no_era_plus = [h for h in PITCHING_ALL_HEADERS if h != "ERA+"]
    values = [str(v) if v is not None else "" for v in row]
    formatted = _format_pitching_values(headers_no_era_plus, values)
    formatted.append("--")  # Career ERA+ not computed
    return _filter_pitching_for_display(formatted)


def build_pitching_comparison(name1: str, name2: str, season: Optional[int] = None) -> Optional[str]:
    """Side-by-side pitching comparison."""
    conn = _get_db()
    try:
        header = "HEADER: " + ", ".join(PITCHING_HEADERS)

        s1 = (_fetch_pitching_season_row(conn, name1, season) if season
              else None) or _fetch_pitching_latest_season_row(conn, name1)
        s2 = (_fetch_pitching_season_row(conn, name2, season) if season
              else None) or _fetch_pitching_latest_season_row(conn, name2)

        c1 = _fetch_pitching_career_row(conn, name1)
        c2 = _fetch_pitching_career_row(conn, name2)

        dn1, t1 = _get_player_info(conn, name1)
        dn2, t2 = _get_player_info(conn, name2)
        label1 = f"{dn1} ({t1})" if t1 else dn1
        label2 = f"{dn2} ({t2})" if t2 else dn2

        parts = []

        if s1 and s2:
            yr = s1[0]
            parts.append(f"{yr} Season:\n")
            parts.append("[STATGRID]")
            parts.append(header)
            parts.append(f"ROW: {label1}, " + ", ".join(s1[1]))
            parts.append(f"ROW: {label2}, " + ", ".join(s2[1]))
            parts.append("[/STATGRID]")

        # Career grid — only when no specific season was requested
        if not season and c1 and c2:
            parts.append("\nCareer:\n")
            parts.append("[STATGRID]")
            parts.append(header)
            parts.append(f"ROW: {label1}, " + ", ".join(c1))
            parts.append(f"ROW: {label2}, " + ", ".join(c2))
            parts.append("[/STATGRID]")

        if not parts:
            return "I don't have enough pitching data to compare these two players."

        if _has_platoon_data(conn, name1):
            parts.append(f"\n[SUGGEST]{dn1} vs lefties[/SUGGEST]")
        if _has_platoon_data(conn, name2):
            parts.append(f"[SUGGEST]{dn2} vs lefties[/SUGGEST]")

        return "\n".join(parts)
    finally:
        conn.close()


# ===================================================================
# 5. build_single_stat_lookup
# ===================================================================

def build_single_stat_lookup(name: str, stat_info: StatInfo, season: int) -> Optional[str]:
    """Single batting stat value formatted as natural-language text. Aggregates across teams."""
    conn = _get_db()
    try:
        season = _resolve_season(conn, name, season)
        cur = conn.cursor()
        col = stat_info.db_column
        if stat_info.is_rate:
            # Rate stats need recomputation from components
            result = _aggregate_batting_season(conn, name, season)
            if not result:
                return None
            # Find the stat value from the formatted values
            idx = BATTING_HEADERS.index(stat_info.display_abbrev) if stat_info.display_abbrev in BATTING_HEADERS else -1
            if idx < 0:
                return None
            raw_value = result[1][idx]
        else:
            # Counting stats: simple SUM
            cur.execute(
                f"SELECT p.name, SUM(s.{col}) "
                "FROM season_batting_stats s "
                "JOIN players p ON s.player_id = p.player_id "
                "WHERE p.name = ? AND s.season = ?",
                (_sanitize(name), season),
            )
            row = cur.fetchone()
            if not row or row[1] is None:
                return None
            raw_value = str(row[1])

        display_name, team = _get_player_info(conn, name)

        formatted_value = _format_rate(raw_value) if stat_info.is_rate else raw_value

        abbr = stat_info.display_abbrev
        sentence_map = {
            "HR": f"**{display_name}** hit **{formatted_value}** home runs in {season}.",
            "AVG": f"**{display_name}** posted a **{formatted_value} AVG** in {season}.",
            "RBI": f"**{display_name}** drove in **{formatted_value}** runs in {season}.",
            "SB": f"**{display_name}** stole **{formatted_value}** bases in {season}.",
            "R": f"**{display_name}** scored **{formatted_value}** runs in {season}.",
            "H": f"**{display_name}** had **{formatted_value}** hits in {season}.",
            "SO": f"**{display_name}** struck out **{formatted_value}** times in {season}.",
            "BB": f"**{display_name}** drew **{formatted_value}** walks in {season}.",
            "OPS": f"**{display_name}** posted a **{formatted_value} OPS** in {season}.",
            "OPS+": f"**{display_name}** posted a **{formatted_value} OPS+** in {season}.",
            "OBP": f"**{display_name}** posted a **{formatted_value} OBP** in {season}.",
            "SLG": f"**{display_name}** posted a **{formatted_value} SLG** in {season}.",
        }
        if abbr in sentence_map:
            sentence = sentence_map[abbr]
        elif stat_info.is_rate:
            sentence = f"**{display_name}** posted a **{formatted_value} {abbr}** in {season}."
        else:
            sentence = f"**{display_name}** had **{formatted_value} {abbr}** in {season}."

        stat_name = stat_info.pill_name
        return (
            f"{sentence}\n\n"
            f"[TIP]Tap a player name for their full profile.[/TIP]\n\n"
            f"[SUGGEST]{season} {stat_name} leaders[/SUGGEST]\n"
            f"[SUGGEST]{display_name} career {stat_name}[/SUGGEST]"
        )
    finally:
        conn.close()


# ===================================================================
# 6. build_pitching_single_stat_lookup
# ===================================================================

def build_pitching_single_stat_lookup(name: str, stat_info: StatInfo, season: int) -> Optional[str]:
    """Single pitching stat value formatted as natural-language text. Aggregates across teams."""
    conn = _get_db()
    try:
        season = _resolve_season(conn, name, season, "season_pitching_stats", "sp")
        cur = conn.cursor()
        col = stat_info.db_column
        if stat_info.is_rate:
            result = _aggregate_pitching_season(conn, name, season)
            if not result:
                return None
            # Find stat in the display values
            idx = PITCHING_HEADERS.index(stat_info.display_abbrev) if stat_info.display_abbrev in PITCHING_HEADERS else -1
            if idx < 0:
                return None
            raw_value = result[1][idx]
        else:
            cur.execute(
                f"SELECT p.name, SUM(sp.{col}) "
                "FROM season_pitching_stats sp "
                "JOIN players p ON sp.player_id = p.player_id "
                "WHERE p.name = ? AND sp.season = ?",
                (_sanitize(name), season),
            )
            row = cur.fetchone()
            if not row or row[1] is None:
                return None
            raw_value = str(row[1])

        display_name, team = _get_player_info(conn, name)

        formatted_value = _format_pitching_rate(raw_value, 2) if stat_info.is_rate else raw_value

        abbr = stat_info.display_abbrev
        sentence_map = {
            "W": f"**{display_name}** won **{formatted_value}** games in {season}.",
            "SV": f"**{display_name}** had **{formatted_value}** saves in {season}.",
            "SO": f"**{display_name}** struck out **{formatted_value}** batters in {season}.",
            "ERA": f"**{display_name}** posted a **{formatted_value} ERA** in {season}.",
            "WHIP": f"**{display_name}** posted a **{formatted_value} WHIP** in {season}.",
            "K/9": f"**{display_name}** posted a **{_format_pitching_rate(raw_value, 1)} K/9** in {season}.",
        }
        if abbr in sentence_map:
            sentence = sentence_map[abbr]
        elif stat_info.is_rate:
            sentence = f"**{display_name}** posted a **{formatted_value} {abbr}** in {season}."
        else:
            sentence = f"**{display_name}** had **{formatted_value} {abbr}** in {season}."

        stat_name = stat_info.pill_name
        return (
            f"{sentence}\n\n"
            f"[TIP]Tap a player name for their full profile.[/TIP]\n\n"
            f"[SUGGEST]{season} {stat_name} leaders[/SUGGEST]\n"
            f"[SUGGEST]{display_name} career[/SUGGEST]"
        )
    finally:
        conn.close()


# ===================================================================
# 7. build_slash_line_lookup
# ===================================================================

def build_slash_line_lookup(name: str, season: int) -> Optional[str]:
    """AVG/OBP/SLG formatted as a small [STATGRID]. Aggregates across teams."""
    conn = _get_db()
    try:
        season = _resolve_season(conn, name, season)
        result = _aggregate_batting_season(conn, name, season)
        if not result:
            return None
        _, values = result
        display_name, team = _get_player_info(conn, name)
        # AVG, OBP, SLG, OPS indices in BATTING_HEADERS
        avg_idx = BATTING_HEADERS.index("AVG")
        obp_idx = BATTING_HEADERS.index("OBP")
        slg_idx = BATTING_HEADERS.index("SLG")
        ops_idx = BATTING_HEADERS.index("OPS")
        avg = values[avg_idx]
        obp = values[obp_idx]
        slg = values[slg_idx]
        ops = values[ops_idx]
        team_display = _team_full_name(team)

        result = (
            f"**{display_name}**'s slash line in {season} ({team_display}):\n\n"
            f"[STATGRID]\nHEADER: AVG, OBP, SLG, OPS\n"
            f"ROW: {avg}, {obp}, {slg}, {ops}\n[/STATGRID]\n\n"
            f"[TIP]Tap a player name for their full profile.[/TIP]\n\n"
            f"[SUGGEST]{display_name} last season[/SUGGEST]"
        )
        if _has_platoon_data(conn, name):
            result += f"\n[SUGGEST]{display_name} vs lefties[/SUGGEST]"
        return result
    finally:
        conn.close()


# ===================================================================
# 8. build_career_lookup
# ===================================================================

def build_career_lookup(name: str, stat_info: Optional[StatInfo] = None) -> Optional[str]:
    """Career batting totals or a single career stat."""
    conn = _get_db()
    try:
        display_name, team = _get_player_info(conn, name)
        team_display = _team_full_name(team)
        most_recent = _most_recent_season(conn, name)

        if stat_info:
            # Single career stat
            if stat_info.is_rate:
                formula = _career_rate_formula(stat_info)
                if not formula:
                    return None
                select_expr = formula
            else:
                select_expr = f"SUM(s.{stat_info.db_column})"

            cur = conn.cursor()
            cur.execute(
                f"SELECT {select_expr}, COUNT(DISTINCT s.season) "
                "FROM season_batting_stats s "
                "JOIN players p ON s.player_id = p.player_id "
                "WHERE p.name = ?",
                (_sanitize(name),),
            )
            row = cur.fetchone()
            if not row or row[0] is None:
                return None
            seasons = int(row[1]) if row[1] else 0
            if seasons <= 1:
                return None

            formatted_value = _format_rate(row[0]) if stat_info.is_rate else str(int(row[0]))

            abbr = stat_info.display_abbrev
            sentence_map = {
                "HR": f"**{display_name}** has hit **{formatted_value}** career home runs.",
                "AVG": f"**{display_name}** has a **{formatted_value}** career batting average.",
                "RBI": f"**{display_name}** has driven in **{formatted_value}** career runs.",
                "SB": f"**{display_name}** has stolen **{formatted_value}** career bases.",
                "H": f"**{display_name}** has **{formatted_value}** career hits.",
                "R": f"**{display_name}** has scored **{formatted_value}** career runs.",
            }
            if abbr in sentence_map:
                sentence = sentence_map[abbr]
            elif stat_info.is_rate:
                sentence = f"**{display_name}** has a **{formatted_value}** career {abbr}."
            else:
                sentence = f"**{display_name}** has **{formatted_value}** career {abbr}."

            stat_name = stat_info.pill_name
            return (
                f"{sentence}\n\n"
                f"[SUGGEST]career {stat_name} leaders[/SUGGEST]\n"
                f"[SUGGEST]{display_name} {most_recent}[/SUGGEST]"
            )
        else:
            # Full career grid
            career_values = _fetch_career_row(conn, name)
            if not career_values:
                return None

            cur = conn.cursor()
            cur.execute(
                "SELECT COUNT(DISTINCT s.season) FROM season_batting_stats s "
                "JOIN players p ON s.player_id = p.player_id WHERE p.name = ?",
                (_sanitize(name),),
            )
            row = cur.fetchone()
            season_count = str(row[0]) if row and row[0] else "?"

            parts = []
            parts.append(f"**{display_name}** \u2014 Career Totals ({team_display})\n")
            parts.append("[STATGRID]")
            parts.append("HEADER: " + ", ".join(BATTING_HEADERS))
            parts.append(f"ROW {season_count} Seasons: " + ", ".join(career_values))
            parts.append("[/STATGRID]")
            parts.append(f"\n[SUGGEST]{display_name} {most_recent}[/SUGGEST]")
            if _has_platoon_data(conn, name):
                parts.append(f"[SUGGEST]{display_name} vs lefties[/SUGGEST]")

            return "\n".join(parts)
    finally:
        conn.close()


# ===================================================================
# 8b. build_player_single_season_max
# ===================================================================

def build_player_single_season_max(name: str, stat_info: StatInfo, direction: str = "max",
                                    is_pitching: bool = False) -> Optional[str]:
    """Find the single season where this player had their max (or min) for the given stat.

    Used by queries like "Most home runs Hank Aaron ever hit (in a season)" — wants the
    BEST single-season tally, not the career sum, not the most recent season.

    For counting stats: ORDER BY stat DESC (or ASC for min) LIMIT 1.
    For rate stats: apply qualification minimum (per CLAUDE.md PA / IP rules) so we
    don't surface a 12-AB .500 BA fluke as the player's "best".
    """
    conn = _get_db()
    try:
        display_name, _ = _get_player_info(conn, name)
        col = stat_info.db_column
        order = "ASC" if direction == "min" else "DESC"

        cur = conn.cursor()
        if is_pitching:
            # Pitching qualification: 162 IP full season = ip_outs >= 486.
            # For partial-season early-career or short stints, this would
            # exclude them — but for "career-best single season" we WANT
            # qualified seasons. Use the standard threshold.
            qual_clause = ""
            if stat_info.is_rate:
                qual_clause = " AND s.ip_outs >= 300"  # 100 IP minimum
            cur.execute(
                f"SELECT s.season, s.{col}, s.team "
                f"FROM season_pitching_stats s "
                f"JOIN players p ON s.player_id = p.player_id "
                f"WHERE p.name = ? AND s.{col} IS NOT NULL{qual_clause} "
                f"ORDER BY s.{col} {order}, s.season ASC LIMIT 1",
                (_sanitize(name),),
            )
        else:
            qual_clause = ""
            if stat_info.is_rate:
                qual_clause = " AND s.plate_appearances >= 400"
            cur.execute(
                f"SELECT s.season, s.{col}, s.team "
                f"FROM season_batting_stats s "
                f"JOIN players p ON s.player_id = p.player_id "
                f"WHERE p.name = ? AND s.{col} IS NOT NULL{qual_clause} "
                f"ORDER BY s.{col} {order}, s.season ASC LIMIT 1",
                (_sanitize(name),),
            )
        row = cur.fetchone()
        if not row:
            return None
        season, value, team = row
        if value is None:
            return None

        formatted = _format_rate(str(value)) if stat_info.is_rate else f"{int(value):,}"
        abbr = stat_info.display_abbrev
        direction_label = "fewest" if direction == "min" else "most"
        if stat_info.is_rate:
            adj = "lowest" if direction == "min" else "highest"
            sentence = f"**{display_name}**'s {adj} {abbr} in a single season was **{formatted}** in **{season}** ({_team_full_name(team)})."
        else:
            sentence = f"**{display_name}** had his {direction_label} {stat_info.display_name} in a single season in **{season}**: **{formatted}** ({_team_full_name(team)})."

        parts = [sentence]
        # Useful follow-ups
        stat_name = stat_info.pill_name
        parts.append(f"\n[SUGGEST]{display_name} career {stat_name}[/SUGGEST]")
        parts.append(f"[SUGGEST]{display_name} {season} stats[/SUGGEST]")
        if stat_info.is_rate:
            parts.append(f"[SUGGEST]all-time single-season {stat_name} leaders[/SUGGEST]")
        return "\n".join(parts)
    finally:
        conn.close()


# ===================================================================
# 9. build_pitching_career_lookup
# ===================================================================

def build_pitching_career_lookup(name: str, stat_info: Optional[StatInfo] = None) -> Optional[str]:
    """Career pitching totals or a single career pitching stat."""
    conn = _get_db()
    try:
        display_name, team = _get_player_info(conn, name)
        team_display = _team_full_name(team)
        most_recent = _most_recent_season(conn, name, "season_pitching_stats")

        if stat_info:
            if stat_info.is_rate:
                formula = _career_pitching_rate_formula(stat_info)
                if not formula:
                    return None
                select_expr = formula
            else:
                select_expr = f"SUM(sp.{stat_info.db_column})"

            cur = conn.cursor()
            cur.execute(
                f"SELECT {select_expr}, COUNT(DISTINCT sp.season) "
                "FROM season_pitching_stats sp "
                "JOIN players p ON sp.player_id = p.player_id "
                "WHERE p.name = ?",
                (_sanitize(name),),
            )
            row = cur.fetchone()
            if not row or row[0] is None:
                return None
            seasons = int(row[1]) if row[1] else 0
            if seasons <= 1:
                return None

            formatted_value = _format_pitching_rate(row[0], 2) if stat_info.is_rate else str(int(row[0]))

            abbr = stat_info.display_abbrev
            sentence_map = {
                "W": f"**{display_name}** has **{formatted_value}** career wins.",
                "SV": f"**{display_name}** has **{formatted_value}** career saves.",
                "SO": f"**{display_name}** has **{formatted_value}** career strikeouts.",
                "ERA": f"**{display_name}** has a **{formatted_value}** career ERA.",
                "WHIP": f"**{display_name}** has a **{formatted_value}** career WHIP.",
            }
            if abbr in sentence_map:
                sentence = sentence_map[abbr]
            elif stat_info.is_rate:
                sentence = f"**{display_name}** has a **{formatted_value}** career {abbr}."
            else:
                sentence = f"**{display_name}** has **{formatted_value}** career {abbr}."

            stat_name = stat_info.pill_name
            return (
                f"{sentence}\n\n"
                f"[SUGGEST]career {stat_name} leaders[/SUGGEST]\n"
                f"[SUGGEST]{display_name} {most_recent}[/SUGGEST]"
            )
        else:
            career_values = _fetch_pitching_career_row(conn, name)
            if not career_values:
                return None

            cur = conn.cursor()
            cur.execute(
                "SELECT COUNT(DISTINCT sp.season) FROM season_pitching_stats sp "
                "JOIN players p ON sp.player_id = p.player_id WHERE p.name = ?",
                (_sanitize(name),),
            )
            row = cur.fetchone()
            season_count = str(row[0]) if row and row[0] else "?"

            parts = []
            parts.append(f"**{display_name}** \u2014 Career Totals ({team_display})\n")
            parts.append("[STATGRID]")
            parts.append("HEADER: " + ", ".join(PITCHING_HEADERS))
            parts.append(f"ROW {season_count} Seasons: " + ", ".join(career_values))
            parts.append("[/STATGRID]")
            parts.append(f"\n[SUGGEST]{display_name} {most_recent}[/SUGGEST]")
            if _has_platoon_data(conn, name):
                parts.append(f"[SUGGEST]{display_name} vs lefties[/SUGGEST]")

            return "\n".join(parts)
    finally:
        conn.close()


# ===================================================================
# 10. build_streak_list
# ===================================================================

def _fetch_streaks_for_season(conn, name, season, performance="hot"):
    """Fetch batting streaks with T1->T2->T3 fallback. Returns list of row tuples or []."""
    order_dir = "ASC" if performance == "cold" else "DESC"
    tables = [
        ("streaks", "st"),
        ("streaks_sensitive", "ss"),
        ("streaks_sliding", "sl"),
    ]
    for table, alias in tables:
        cur = conn.cursor()
        try:
            cur.execute(
                f"SELECT {alias}.start_date, {alias}.end_date, {alias}.num_games, "
                f"{alias}.at_bats, {alias}.hits, {alias}.walks, {alias}.strikeouts, "
                f"{alias}.batting_avg, {alias}.obp, {alias}.slg, {alias}.ops, {alias}.home_runs "
                f"FROM {table} {alias} "
                f"JOIN players p ON {alias}.player_id = p.player_id "
                f"WHERE p.name = ? AND {alias}.season = ? AND {alias}.performance = ? "
                f"ORDER BY {alias}.ops {order_dir}",
                (_sanitize(name), season, performance),
            )
            rows = cur.fetchall()
            if rows:
                return rows
        except Exception:
            continue
    return []


def build_streak_list(name: str, performance: str, season: Optional[int] = None) -> Optional[str]:
    """Format batting streaks with dates, games, stats."""
    conn = _get_db()
    try:
        display_name, _ = _get_player_info(conn, name)

        if season is None:
            cur = conn.cursor()
            cur.execute(
                "SELECT MAX(st.season) FROM streaks st "
                "JOIN players p ON st.player_id = p.player_id WHERE p.name = ?",
                (_sanitize(name),),
            )
            row = cur.fetchone()
            if not row or not row[0]:
                return None
            target_season = int(row[0])
        else:
            target_season = season

        rows = _fetch_streaks_for_season(conn, name, target_season, performance)
        if not rows:
            label = "cold streaks" if performance == "cold" else "hot streaks"
            return f"No {label} found for **{display_name}** in {target_season}."

        # League avg OPS for cold streak context
        league_ops = None
        if performance == "cold":
            cur = conn.cursor()
            cur.execute("SELECT league_ops FROM league_averages WHERE season = ?", (target_season,))
            lr = cur.fetchone()
            if lr:
                league_ops = float(lr[0]) if lr[0] else None

        headers = ["G", "AB", "H", "BB", "SO", "AVG", "OBP", "SLG", "OPS", "HR"]
        label = "Cold Streaks" if performance == "cold" else "Hot Streaks"
        parts = []
        parts.append(f"**{display_name}** \u2014 {target_season} {label}\n")
        parts.append("[STATGRID]")
        parts.append("HEADER: " + ", ".join(headers))

        for row in rows[:4]:
            start_date = _format_date(str(row[0]))
            end_date = _format_date(str(row[1]))
            row_label = f"{start_date} \u2013 {end_date}"
            games, ab, hits, walks, so = str(row[2]), str(row[3]), str(row[4]), str(row[5]), str(row[6])
            avg = _format_rate(row[7])
            obp = _format_rate(row[8])
            slg = _format_rate(row[9])
            ops = _format_rate(row[10])
            hr = str(row[11])

            parts.append(f"ROW {row_label}: {games}, {ab}, {hits}, {walks}, {so}, {avg}, {obp}, {slg}, {ops}, {hr}")

            if performance == "cold" and league_ops is not None:
                try:
                    streak_ops = float(row[10])
                    if streak_ops > league_ops:
                        parts.append(
                            f'NOTE: This "cold" streak was still above the {target_season} '
                            f"league average OPS of {_format_rate(league_ops)}"
                        )
                except (ValueError, TypeError):
                    pass

        parts.append("[/STATGRID]")

        count = min(len(rows), 4)
        streak_word = "streak" if count == 1 else "streaks"
        if rows:
            adjective = "coldest" if performance == "cold" else "hottest"
            top_ops = _format_rate(rows[0][10])
            top_games = str(rows[0][2])
            top_label = f"{_format_date(str(rows[0][0]))} \u2013 {_format_date(str(rows[0][1]))}"
            parts.append(
                f"\n{count} {performance} {streak_word} detected. "
                f"The {adjective} was {top_games} games ({top_label}) with a {top_ops} OPS."
            )

        opposite = "cold" if performance == "hot" else "hot"
        parts.append(f"\n[SUGGEST]{display_name} {opposite} streaks {target_season}[/SUGGEST]")

        return "\n".join(parts)
    finally:
        conn.close()


# ===================================================================
# 11. build_pitching_streak_list
# ===================================================================

def _fetch_pitching_streaks_for_season(conn, name, season, performance="hot"):
    """Fetch pitching streaks with T1->T2->T3 fallback."""
    order_dir = "DESC" if performance == "cold" else "ASC"  # Lower ERA = hotter
    tables = [
        ("pitching_streaks", "ps"),
        ("pitching_streaks_sensitive", "pss"),
        ("pitching_streaks_sliding", "psl"),
    ]
    for table, alias in tables:
        cur = conn.cursor()
        try:
            cur.execute(
                f"SELECT {alias}.start_date, {alias}.end_date, {alias}.num_games, "
                f"{alias}.innings_pitched, {alias}.hits, {alias}.earned_runs, "
                f"{alias}.walks, {alias}.strikeouts, {alias}.home_runs, "
                f"{alias}.era, {alias}.whip, {alias}.k_per_9 "
                f"FROM {table} {alias} "
                f"JOIN players p ON {alias}.player_id = p.player_id "
                f"WHERE p.name = ? AND {alias}.season = ? AND {alias}.performance = ? "
                f"ORDER BY {alias}.era {order_dir}",
                (_sanitize(name), season, performance),
            )
            rows = cur.fetchall()
            if rows:
                return rows
        except Exception:
            continue
    return []


def build_pitching_streak_list(name: str, performance: str, season: Optional[int] = None) -> Optional[str]:
    """Format pitching streaks."""
    conn = _get_db()
    try:
        display_name, _ = _get_player_info(conn, name)

        if season is None:
            cur = conn.cursor()
            cur.execute(
                "SELECT MAX(ps.season) FROM pitching_streaks ps "
                "JOIN players p ON ps.player_id = p.player_id WHERE p.name = ?",
                (_sanitize(name),),
            )
            row = cur.fetchone()
            if not row or not row[0]:
                return None
            target_season = int(row[0])
        else:
            target_season = _resolve_season(conn, name, season, "season_pitching_stats", "sp")

        rows = _fetch_pitching_streaks_for_season(conn, name, target_season, performance)
        if not rows:
            label = "cold streaks" if performance == "cold" else "hot streaks"
            return f"No {label} found for **{display_name}** in {target_season}."

        # League avg ERA for cold streak context
        league_era = None
        if performance == "cold":
            cur = conn.cursor()
            try:
                cur.execute("SELECT league_era FROM league_pitching_averages WHERE season = ?", (target_season,))
                lr = cur.fetchone()
                if lr:
                    league_era = float(lr[0]) if lr[0] else None
            except Exception:
                pass

        headers = ["G", "IP", "H", "ER", "BB", "SO", "HR", "ERA", "WHIP", "K/9"]
        label = "Cold Streaks" if performance == "cold" else "Hot Streaks"
        parts = []
        parts.append(f"**{display_name}** \u2014 {target_season} {label}\n")
        parts.append("[STATGRID]")
        parts.append("HEADER: " + ", ".join(headers))

        for row in rows[:4]:
            start_date = _format_date(str(row[0]))
            end_date = _format_date(str(row[1]))
            row_label = f"{start_date} \u2013 {end_date}"
            games = str(row[2])
            ip, h, er = str(row[3]), str(row[4]), str(row[5])
            bb, so, hr = str(row[6]), str(row[7]), str(row[8])
            era = _format_pitching_rate(row[9], 2)
            whip = _format_pitching_rate(row[10], 2)
            k9 = _format_pitching_rate(row[11], 1)

            parts.append(f"ROW {row_label}: {games}, {ip}, {h}, {er}, {bb}, {so}, {hr}, {era}, {whip}, {k9}")

            if performance == "cold" and league_era is not None:
                try:
                    streak_era = float(row[9])
                    if streak_era < league_era:
                        parts.append(
                            f'NOTE: This "cold" streak was still below the {target_season} '
                            f"league average ERA of {_format_pitching_rate(league_era, 2)}"
                        )
                except (ValueError, TypeError):
                    pass

        parts.append("[/STATGRID]")

        count = min(len(rows), 4)
        streak_word = "streak" if count == 1 else "streaks"
        if rows:
            adjective = "coldest" if performance == "cold" else "hottest"
            top_era = _format_pitching_rate(rows[0][9], 2)
            top_games = str(rows[0][2])
            top_label = f"{_format_date(str(rows[0][0]))} \u2013 {_format_date(str(rows[0][1]))}"
            parts.append(
                f"\n{count} {performance} {streak_word} detected. "
                f"The {adjective} was {top_games} games ({top_label}) with a {top_era} ERA."
            )

        opposite = "cold" if performance == "hot" else "hot"
        parts.append(f"\n[SUGGEST]{display_name} {opposite} streaks {target_season}[/SUGGEST]")

        return "\n".join(parts)
    finally:
        conn.close()


# ===================================================================
# 12. build_current_form
# ===================================================================

def build_current_form(name: str) -> Optional[str]:
    """Current batting form from the current_form table."""
    conn = _get_db()
    try:
        display_name, _ = _get_player_info(conn, name)
        cur = conn.cursor()
        cur.execute(
            "SELECT cf.season, cf.form_start_date, cf.form_start_game_number, "
            "cf.total_season_games, cf.num_games, "
            "cf.at_bats, cf.hits, cf.home_runs, cf.runs, cf.rbi, "
            "cf.walks, cf.strikeouts, "
            "cf.batting_avg, cf.obp, cf.slg, cf.ops, "
            "s.batting_avg, s.obp, s.slg, s.ops, s.team "
            "FROM current_form cf "
            "JOIN players p ON cf.player_id = p.player_id "
            "LEFT JOIN season_batting_stats s ON cf.player_id = s.player_id AND cf.season = s.season "
            "WHERE p.name = ? "
            "ORDER BY cf.season DESC LIMIT 1",
            (_sanitize(name),),
        )
        row = cur.fetchone()
        if not row or len(row) < 21:
            return None

        season = str(row[0])
        start_date = _format_date(str(row[1]))
        start_game_num = str(row[2])
        total_games = str(row[3])
        num_games = int(row[4]) if row[4] else 0
        ab, h, hr, r, rbi = str(row[5]), str(row[6]), str(row[7]), str(row[8]), str(row[9])
        bb, so = str(row[10]), str(row[11])
        avg = _format_rate(row[12])
        obp = _format_rate(row[13])
        slg = _format_rate(row[14])
        ops = _format_rate(row[15])
        season_avg = _format_rate(row[16])
        season_ops = _format_rate(row[19])
        team = str(row[20]) if row[20] else ""

        # Get team games for FORM metadata
        team_games = 162
        tcur = conn.cursor()
        tcur.execute(
            "SELECT MAX(games) FROM season_batting_stats WHERE season = ?",
            (int(season),),
        )
        tr = tcur.fetchone()
        if tr and tr[0]:
            team_games = min(int(tr[0]), 162)

        parts = []
        parts.append(f"{display_name} has been on fire over the last {num_games} games (since {start_date}):\n")
        parts.append("[STATGRID]")
        parts.append("HEADER: G, AB, R, H, HR, RBI, BB, SO, AVG, OBP, SLG, OPS")
        parts.append(f"FORM: {display_name}, {season}, {start_game_num}, {total_games}, {team_games}")
        parts.append(f"ROW: {num_games}, {ab}, {r}, {h}, {hr}, {rbi}, {bb}, {so}, {avg}, {obp}, {slg}, {ops}")
        parts.append("[/STATGRID]")
        parts.append(f"\nThat's up from his {season} season line of {season_avg}/{season_ops} (AVG/OPS).")
        parts.append(f"\n[SUGGEST]{display_name} hot streaks {season}[/SUGGEST]")

        return "\n".join(parts)
    finally:
        conn.close()


# ===================================================================
# 13. build_pitching_current_form
# ===================================================================

def build_pitching_current_form(name: str) -> Optional[str]:
    """Current pitching form from the pitching_current_form table."""
    conn = _get_db()
    try:
        display_name, _ = _get_player_info(conn, name)
        cur = conn.cursor()
        cur.execute(
            "SELECT pcf.season, pcf.form_start_date, pcf.form_start_game_number, "
            "pcf.total_season_games, pcf.num_games, pcf.role, "
            "pcf.innings_pitched, pcf.hits, pcf.earned_runs, "
            "pcf.walks, pcf.strikeouts, pcf.home_runs, "
            "pcf.era, pcf.whip, pcf.k_per_9, pcf.bb_per_9, "
            "pcf.season_era, sp.team "
            "FROM pitching_current_form pcf "
            "JOIN players p ON pcf.player_id = p.player_id "
            "LEFT JOIN season_pitching_stats sp ON pcf.player_id = sp.player_id AND pcf.season = sp.season "
            "WHERE p.name = ? "
            "ORDER BY pcf.season DESC LIMIT 1",
            (_sanitize(name),),
        )
        row = cur.fetchone()
        if not row or len(row) < 18:
            return None

        season = str(row[0])
        start_date = _format_date(str(row[1]))
        start_game_num = str(row[2])
        total_games = str(row[3])
        num_games = int(row[4]) if row[4] else 0
        ip, h, er = str(row[6]), str(row[7]), str(row[8])
        bb, so, hr = str(row[9]), str(row[10]), str(row[11])
        era = _format_pitching_rate(row[12], 2)
        whip = _format_pitching_rate(row[13], 2)
        k9 = _format_pitching_rate(row[14], 1)
        bb9 = _format_pitching_rate(row[15], 1)
        season_era = _format_pitching_rate(row[16], 2)
        team = str(row[17]) if row[17] else ""

        team_games = 162
        tcur = conn.cursor()
        tcur.execute(
            "SELECT MAX(games) FROM season_batting_stats WHERE season = ?",
            (int(season),),
        )
        tr = tcur.fetchone()
        if tr and tr[0]:
            team_games = min(int(tr[0]), 162)

        parts = []
        parts.append(f"{display_name} has been on fire over the last {num_games} games (since {start_date}):\n")
        parts.append("[STATGRID]")
        parts.append("HEADER: G, IP, H, ER, HR, BB, SO, ERA, WHIP, K/9, BB/9")
        parts.append(f"FORM: {display_name}, {season}, {start_game_num}, {total_games}, {team_games}")
        parts.append(f"ROW: {num_games}, {ip}, {h}, {er}, {hr}, {bb}, {so}, {era}, {whip}, {k9}, {bb9}")
        parts.append("[/STATGRID]")
        parts.append(f"\nThat's compared to his {season} season ERA of {season_era}.")
        parts.append(f"\n[SUGGEST]{display_name} hot streaks {season}[/SUGGEST]")

        return "\n".join(parts)
    finally:
        conn.close()


# ===================================================================
# 14. build_platoon_splits
# ===================================================================

def build_platoon_splits(name: str, hand: Optional[str] = None, season: int = 0) -> Optional[str]:
    """Batting platoon splits (vs LHP / vs RHP)."""
    conn = _get_db()
    try:
        season = _resolve_season(conn, name, season)
        display_name, _ = _get_player_info(conn, name)

        split_filter = ""
        params = [_sanitize(name), season]
        if hand:
            split_value = "vs_LHP" if hand == "LHP" else "vs_RHP"
            split_filter = " AND ps.split = ?"
            params.append(split_value)

        cur = conn.cursor()
        cur.execute(
            "SELECT ps.split, ps.plate_appearances, ps.at_bats, ps.hits, "
            "ps.doubles, ps.triples, ps.home_runs, ps.rbi, "
            "ps.walks, ps.strikeouts, "
            "ps.batting_avg, ps.obp, ps.slg, ps.ops, ps.iso, ps.babip "
            "FROM platoon_splits ps "
            "JOIN players p ON ps.player_id = p.player_id "
            f"WHERE p.name = ? AND ps.season = ?{split_filter} "
            "ORDER BY ps.split",
            tuple(params),
        )
        rows = cur.fetchall()
        if not rows:
            return None

        headers = ["PA", "AB", "H", "2B", "3B", "HR", "RBI", "BB", "SO", "AVG", "OBP", "SLG", "OPS", "ISO", "BABIP"]

        subtitle = "Platoon Splits"
        if hand:
            subtitle = "vs Left-Handed Pitchers" if hand == "LHP" else "vs Right-Handed Pitchers"

        parts = []
        parts.append(f"**{display_name}** \u2014 {season} {subtitle}\n")
        parts.append("[STATGRID]")
        parts.append("HEADER: " + ", ".join(headers))
        for row in rows[:2]:
            split_label = "vs LHP" if row[0] == "vs_LHP" else "vs RHP"
            values = _format_values(headers, [str(v) if v is not None else "" for v in row[1:]])
            parts.append(f"ROW {split_label}: " + ", ".join(values))
        parts.append("[/STATGRID]")
        parts.append("\n[TIP]Tap a player name for their full profile.[/TIP]")
        parts.append(f"\n[SUGGEST]{display_name} {season}[/SUGGEST]")

        return "\n".join(parts)
    finally:
        conn.close()


def build_platoon_stat_single(name: str, hand: str, season: int, stat_info) -> Optional[str]:
    """Single stat from platoon splits: 'Judge home runs vs lefties 2025'."""
    conn = _get_db()
    try:
        season = _resolve_season(conn, name, season)
        display_name, _ = _get_player_info(conn, name)

        split_value = "vs_LHP" if hand == "LHP" else "vs_RHP"
        hand_label = "left-handed" if hand == "LHP" else "right-handed"

        col = stat_info.db_column
        _col_map = {
            "home_runs": "home_runs", "hits": "hits", "rbi": "rbi",
            "doubles": "doubles", "triples": "triples",
            "walks": "walks", "strikeouts": "strikeouts",
            "batting_avg": "batting_avg", "obp": "obp", "slg": "slg",
            "ops": "ops", "iso": "iso", "babip": "babip",
            "plate_appearances": "plate_appearances", "at_bats": "at_bats",
        }
        ps_col = _col_map.get(col)
        if not ps_col:
            return None

        cur = conn.cursor()
        cur.execute(
            f"SELECT ps.{ps_col}, ps.plate_appearances "
            "FROM platoon_splits ps "
            "JOIN players p ON ps.player_id = p.player_id "
            "WHERE p.name = ? AND ps.season = ? AND ps.split = ?",
            (_sanitize(name), season, split_value),
        )
        row = cur.fetchone()
        if not row:
            return None

        value = row[0]
        pa = row[1]

        if stat_info.is_rate and value is not None:
            formatted = _format_rate(value)
        else:
            formatted = str(int(value)) if value is not None else "0"

        return (
            f"**{display_name}** had **{formatted} {stat_info.display_name.lower()}** "
            f"vs {hand_label} pitchers in {season} ({pa} PA).\n\n"
            f"[SUGGEST]{display_name} vs {'lefties' if hand == 'LHP' else 'righties'} {season}[/SUGGEST]\n"
            f"[SUGGEST]{display_name} {season}[/SUGGEST]"
        )
    finally:
        conn.close()


# ===================================================================
# 15. build_pitching_platoon_splits
# ===================================================================

def build_pitching_platoon_splits(name: str, hand: Optional[str] = None, season: int = 0) -> Optional[str]:
    """Pitching platoon splits (vs LHB / vs RHB)."""
    conn = _get_db()
    try:
        season = _resolve_season(conn, name, season, "season_pitching_stats", "sp")
        display_name, _ = _get_player_info(conn, name)

        split_filter = ""
        params = [_sanitize(name), season]
        if hand:
            split_value = "vs_LHB" if hand == "LHB" else "vs_RHB"
            split_filter = " AND pps.split = ?"
            params.append(split_value)

        cur = conn.cursor()
        cur.execute(
            "SELECT pps.split, pps.at_bats, pps.hits, "
            "pps.doubles, pps.triples, pps.home_runs, "
            "pps.walks, pps.strikeouts, "
            "pps.batting_avg_against, pps.obp_against, pps.slg_against, pps.ops_against "
            "FROM pitching_platoon_splits pps "
            "JOIN players p ON pps.player_id = p.player_id "
            f"WHERE p.name = ? AND pps.season = ?{split_filter} "
            "ORDER BY pps.split",
            tuple(params),
        )
        rows = cur.fetchall()
        if not rows:
            return None

        headers = ["AB", "H", "2B", "3B", "HR", "BB", "SO", "AVG", "OBP", "SLG", "OPS"]

        subtitle = "Platoon Splits"
        if hand:
            subtitle = "vs Left-Handed Batters" if hand == "LHB" else "vs Right-Handed Batters"

        parts = []
        parts.append(f"**{display_name}** \u2014 {season} {subtitle}\n")
        parts.append("[STATGRID]")
        parts.append("HEADER: " + ", ".join(headers))
        for row in rows[:2]:
            split_label = "vs LHB" if row[0] == "vs_LHB" else "vs RHB"
            values = _format_values(headers, [str(v) if v is not None else "" for v in row[1:]])
            parts.append(f"ROW {split_label}: " + ", ".join(values))
        parts.append("[/STATGRID]")
        parts.append("\n[TIP]Tap a player name for their full profile.[/TIP]")
        parts.append(f"\n[SUGGEST]{display_name} {season}[/SUGGEST]")

        return "\n".join(parts)
    finally:
        conn.close()


# ===================================================================
# 15b. build_platoon_leaderboard
# ===================================================================

def build_platoon_leaderboard(stat_info, hand: str, is_pitching: bool = False,
                              season: int = 0, limit: int = 50,
                              league: Optional[str] = None) -> Optional[str]:
    """Leaderboard for a stat filtered by platoon split (vs LHP/RHP or vs LHB/RHB)."""
    conn = _get_db()
    try:
        yr = season or datetime.now().year

        # Map stat db_column to the correct column in the split table
        col = stat_info.db_column

        if is_pitching:
            table = "pitching_platoon_splits"
            alias = "pps"
            split_value = f"vs_L{('H' if hand == 'LHP' else 'H')}B"
            split_value = "vs_LHB" if hand == "LHP" else "vs_RHB"
            hand_label = "vs Left-Handed Batters" if hand == "LHP" else "vs Right-Handed Batters"
            # Pitching platoon table uses _against suffix for rate stats
            pitching_col_map = {
                "batting_avg": "batting_avg_against",
                "obp": "obp_against",
                "slg": "slg_against",
                "ops": "ops_against",
            }
            col = pitching_col_map.get(col, col)
        else:
            table = "platoon_splits"
            alias = "ps"
            split_value = "vs_LHP" if hand == "LHP" else "vs_RHP"
            hand_label = "vs Left-Handed Pitchers" if hand == "LHP" else "vs Right-Handed Pitchers"

        # Verify the column exists in the split table
        cur = conn.cursor()
        cur.execute(f"PRAGMA table_info({table})")
        valid_cols = {r[1] for r in cur.fetchall()}
        if col not in valid_cols:
            return None

        # PA minimum for rate stats — occurrence-rate floor, same logic
        # as build_split_leaderboard's helper. The hardcoded 100 floor here
        # was too high mid-season and produced empty leaderboards (e.g.,
        # "best hitter vs lefties" had no qualifiers in May).
        pa_filter = ""
        if stat_info.is_rate:
            split_pa_min, _qual_col = _split_pa_floor(
                conn, yr, table, "split", [split_value], is_pitching,
            )
            pa_filter = f" AND {alias}.plate_appearances >= {split_pa_min}"

        league_filter = ""
        league_label = ""
        if league:
            league_filter = f" AND {_league_team_clause(league, 'p')}"
            league_label = f" ({league})"

        # Sort direction: lower is better for batting_avg_against, obp_against, etc.
        lower_is_better = col in ("batting_avg_against", "obp_against", "slg_against",
                                   "ops_against", "earned_run_avg", "whip")
        order = "ASC" if lower_is_better else "DESC"

        cur.execute(
            f"SELECT p.name, {alias}.{col} "
            f"FROM {table} {alias} "
            f"JOIN players p ON {alias}.player_id = p.player_id "
            f"WHERE {alias}.season = ? AND {alias}.split = ?{pa_filter}{league_filter} "
            f"AND {alias}.{col} IS NOT NULL "
            f"ORDER BY {alias}.{col} {order} LIMIT ?",
            (yr, split_value, limit),
        )
        rows = cur.fetchall()
        if not rows:
            return f"No {stat_info.display_name} {hand_label.lower()} leaders found for {yr}."

        abbrev = stat_info.display_abbrev
        title = f"**{yr} {stat_info.display_name} Leaders {hand_label}{league_label}**\n"
        parts = [title]
        parts.append("[TIP]Tap a player name for their full profile.[/TIP]")
        parts.append("[LEADERBOARD]")
        parts.append(f"HEADER: {abbrev}")
        for i, row in enumerate(rows):
            val = _format_rate(str(row[1])) if stat_info.is_rate else str(row[1])
            parts.append(f"ROW {i+1}. {row[0]}: {val}")
        parts.append("[/LEADERBOARD]")
        if stat_info.is_rate:
            parts.append(f"\n_Min. {split_pa_min} PA in split._")

        return "\n".join(parts)
    finally:
        conn.close()


# ===================================================================
# 15c. build_multi_threshold
# ===================================================================

def build_multi_threshold(filters: list, season: int,
                          is_pitching: bool = False,
                          league: Optional[str] = None,
                          rookie: bool = False) -> Optional[str]:
    """Build a multi-stat threshold: '.300 AVG with 30+ HR', '200 K and sub-3.00 ERA'."""
    conn = _get_db()
    try:
        table = "season_pitching_stats" if is_pitching else "season_batting_stats"
        prefix = "sp" if is_pitching else "s"
        league_filter = f" AND {_league_team_clause(league, prefix)}" if league else ""
        league_label = f" ({league})" if league else ""
        rookie_clause = _rookie_filter(prefix, is_pitching) if rookie else ""

        # Verify all columns exist
        cur = conn.cursor()
        cur.execute(f"PRAGMA table_info({table})")
        valid_cols = {r[1] for r in cur.fetchall()}
        for f in filters:
            if f["stat"].db_column not in valid_cols:
                return None

        # Build WHERE clauses
        where_parts = [f"{prefix}.season = ?"]
        params = [season]
        for f in filters:
            where_parts.append(f"{prefix}.{f['stat'].db_column} {f['comparison']} ?")
            params.append(f["threshold"])

        # PA/IP minimum for rate stats
        has_rate = any(f["stat"].is_rate for f in filters)
        if has_rate:
            if is_pitching:
                ip_min = _qual_min_ip_outs(conn, season)
                where_parts.append(f"{prefix}.ip_outs >= {ip_min}")
            else:
                pa_min = _qual_min_pa(conn, season)
                where_parts.append(f"{prefix}.plate_appearances >= {pa_min}")

        # Select all filter stat columns
        select_cols = ", ".join(f"{prefix}.{f['stat'].db_column}" for f in filters)
        where_clause = " AND ".join(where_parts)

        # Sort by first stat
        first_stat = filters[0]["stat"]
        order = "ASC" if first_stat.db_column in ("earned_run_avg", "whip") else "DESC"

        cur.execute(
            f"SELECT p.name, {select_cols} "
            f"FROM {table} {prefix} "
            f"JOIN players p ON {prefix}.player_id = p.player_id "
            f"WHERE {where_clause}{league_filter}{rookie_clause} "
            f"ORDER BY {prefix}.{first_stat.db_column} {order}",
            tuple(params),
        )
        rows = cur.fetchall()

        # Build title
        title_parts = []
        for f in filters:
            t = _format_rate(str(f["threshold"])) if f["stat"].is_rate else str(int(f["threshold"]))
            if f["stat"].is_rate and f["comparison"] == ">=":
                title_parts.append(f"{t}+ {f['stat'].display_abbrev}")
            elif f["comparison"] == "<=":
                title_parts.append(f"Sub-{t} {f['stat'].display_abbrev}")
            else:
                title_parts.append(f"{t}+ {f['stat'].display_abbrev}")
        who = "Rookie Pitchers" if is_pitching and rookie else "Rookies" if rookie else "Pitchers" if is_pitching else "Players"
        title = f"{who} with {' and '.join(title_parts)} in {season}{league_label}"

        if not rows:
            return f"No {who.lower()} matched {' and '.join(title_parts)} in {season}{league_label}."

        count = len(rows)
        who_lower = "pitcher" if is_pitching else "player"
        header_abbrevs = [f["stat"].display_abbrev for f in filters]
        parts = [f"**{title}**"]
        parts.append(f"{count} matched.\n")
        parts.append("[TIP]Tap a player name for their full profile.[/TIP]")
        parts.append("[LEADERBOARD]")
        parts.append("HEADER: " + ", ".join(header_abbrevs))
        for i, row in enumerate(rows):
            vals = []
            for j, f in enumerate(filters):
                v = row[1 + j]
                vals.append(_format_rate(str(v)) if f["stat"].is_rate else str(v))
            parts.append(f"ROW {i+1}. {row[0]}: {', '.join(vals)}")
        parts.append("[/LEADERBOARD]")

        return "\n".join(parts)
    finally:
        conn.close()


def build_all_time_multi_threshold(filters: list,
                                   is_pitching: bool = False,
                                   league: Optional[str] = None,
                                   since_year: Optional[int] = None,
                                   rookie: bool = False) -> Optional[str]:
    """All-time multi-stat threshold: '.300 with 30 HR' (no season specified)."""
    conn = _get_db()
    try:
        table = "season_pitching_stats" if is_pitching else "season_batting_stats"
        prefix = "sp" if is_pitching else "s"
        league_filter = f" AND {_league_team_clause(league, prefix)}" if league else ""
        league_label = f" ({league})" if league else ""
        rookie_clause = _rookie_filter(prefix, is_pitching) if rookie else ""

        cur = conn.cursor()
        cur.execute(f"PRAGMA table_info({table})")
        valid_cols = {r[1] for r in cur.fetchall()}
        for f in filters:
            if f["stat"].db_column not in valid_cols:
                return None

        where_parts = []
        params = []
        for f in filters:
            where_parts.append(f"{prefix}.{f['stat'].db_column} {f['comparison']} ?")
            params.append(f["threshold"])

        if since_year:
            where_parts.append(f"{prefix}.season >= ?")
            params.append(since_year)

        # PA/IP minimum for rate stats
        has_rate = any(f["stat"].is_rate for f in filters)
        if has_rate:
            if is_pitching:
                where_parts.append(f"{prefix}.ip_outs >= 486")
            else:
                where_parts.append(f"{prefix}.plate_appearances >= 400")

        select_cols = ", ".join(f"{prefix}.{f['stat'].db_column}" for f in filters)
        where_clause = " AND ".join(where_parts)

        first_stat = filters[0]["stat"]
        order = "ASC" if first_stat.db_column in ("earned_run_avg", "whip") else "DESC"

        cur.execute(
            f"SELECT p.name, {select_cols}, {prefix}.season "
            f"FROM {table} {prefix} "
            f"JOIN players p ON {prefix}.player_id = p.player_id "
            f"WHERE {where_clause}{league_filter}{rookie_clause} "
            f"ORDER BY {prefix}.{first_stat.db_column} {order}",
            tuple(params),
        )
        rows = cur.fetchall()

        # Build title
        title_parts = []
        for f in filters:
            t = _format_rate(str(f["threshold"])) if f["stat"].is_rate else str(int(f["threshold"]))
            if f["stat"].is_rate and f["comparison"] == ">=":
                title_parts.append(f"{t}+ {f['stat'].display_abbrev}")
            elif f["comparison"] == "<=":
                title_parts.append(f"Sub-{t} {f['stat'].display_abbrev}")
            else:
                title_parts.append(f"{t}+ {f['stat'].display_abbrev}")
        who = "Rookie Pitchers" if is_pitching and rookie else "Rookies" if rookie else "Pitchers" if is_pitching else "Players"
        scope_label = f"Since {since_year}" if since_year else "All-Time"
        title = f"{who} with {' and '.join(title_parts)} ({scope_label}){league_label}"

        if not rows:
            scope_msg = f"since {since_year}" if since_year else "in a season"
            return f"No {who.lower()} have matched {' and '.join(title_parts)} {scope_msg}."

        count = len(rows)
        header_abbrevs = [f["stat"].display_abbrev for f in filters]
        parts = [f"**{title}**"]
        parts.append(f"{count} matched.\n")
        parts.append("[TIP]Tap a player name for their full profile.[/TIP]")
        parts.append("[LEADERBOARD]")
        parts.append("HEADER: Year, " + ", ".join(header_abbrevs))
        n_filters = len(filters)
        for i, row in enumerate(rows):
            vals = []
            for j, f in enumerate(filters):
                v = row[1 + j]
                vals.append(_format_rate(str(v)) if f["stat"].is_rate else str(v))
            year = row[1 + n_filters]
            parts.append(f"ROW {i+1}. {row[0]}: {year}, {', '.join(vals)}")
        parts.append("[/LEADERBOARD]")

        # Suggestion pills
        filter_desc = " and ".join(title_parts)
        current_year = datetime.now().year
        parts.append(f"\n[SUGGEST]{filter_desc} in {current_year}[/SUGGEST]")
        parts.append(f"[SUGGEST]{filter_desc} in {current_year - 1}[/SUGGEST]")
        if not since_year:
            parts.append(f"[SUGGEST]{filter_desc} this century[/SUGGEST]")

        return "\n".join(parts)
    finally:
        conn.close()


# ===================================================================
# 16. build_home_away_splits
# ===================================================================

def build_home_away_splits(name: str, location: Optional[str] = None, season: int = 0) -> Optional[str]:
    """Batting home/away splits."""
    conn = _get_db()
    try:
        season = _resolve_season(conn, name, season)
        display_name, _ = _get_player_info(conn, name)

        split_filter = ""
        params = [_sanitize(name), season]
        if location:
            split_filter = " AND has.split = ?"
            params.append(location)

        cur = conn.cursor()
        cur.execute(
            "SELECT has.split, has.games, has.at_bats, has.runs, has.hits, "
            "has.doubles, has.triples, has.home_runs, has.rbi, "
            "has.walks, has.strikeouts, "
            "has.batting_avg, has.obp, has.slg, has.ops, has.iso, has.babip "
            "FROM home_away_splits has "
            "JOIN players p ON has.player_id = p.player_id "
            f"WHERE p.name = ? AND has.season = ?{split_filter} "
            "ORDER BY has.split DESC",
            tuple(params),
        )
        rows = cur.fetchall()
        if not rows:
            return None

        headers = ["G", "AB", "R", "H", "2B", "3B", "HR", "RBI", "BB", "SO", "AVG", "OBP", "SLG", "OPS", "ISO", "BABIP"]

        subtitle = "Home vs Away"
        if location:
            subtitle = "Home" if location == "home" else "Away"

        parts = []
        parts.append(f"**{display_name}** \u2014 {season} {subtitle}\n")
        parts.append("[STATGRID]")
        parts.append("HEADER: " + ", ".join(headers))
        for row in rows[:2]:
            split_label = "Home" if row[0] == "home" else "Away"
            values = _format_values(headers, [str(v) if v is not None else "" for v in row[1:]])
            parts.append(f"ROW {split_label}: " + ", ".join(values))
        parts.append("[/STATGRID]")
        parts.append("\n[TIP]Tap a player name for their full profile.[/TIP]")
        parts.append(f"\n[SUGGEST]{display_name} {season}[/SUGGEST]")

        return "\n".join(parts)
    finally:
        conn.close()


# ===================================================================
# 17. build_pitching_home_away_splits
# ===================================================================

def build_pitching_home_away_splits(name: str, location: Optional[str] = None, season: int = 0) -> Optional[str]:
    """Pitching home/away splits."""
    conn = _get_db()
    try:
        season = _resolve_season(conn, name, season, "season_pitching_stats", "sp")
        display_name, _ = _get_player_info(conn, name)

        split_filter = ""
        params = [_sanitize(name), season]
        if location:
            split_filter = " AND phas.split = ?"
            params.append(location)

        cur = conn.cursor()
        cur.execute(
            "SELECT phas.split, phas.games, phas.games_started, phas.innings_pitched, "
            "phas.hits, phas.earned_runs, phas.home_runs, phas.walks, phas.strikeouts, "
            "phas.era, phas.whip, phas.k_per_9, phas.bb_per_9, phas.baa "
            "FROM pitching_home_away_splits phas "
            "JOIN players p ON phas.player_id = p.player_id "
            f"WHERE p.name = ? AND phas.season = ?{split_filter} "
            "ORDER BY phas.split DESC",
            tuple(params),
        )
        rows = cur.fetchall()
        if not rows:
            return None

        headers = ["G", "GS", "IP", "H", "ER", "HR", "BB", "SO", "ERA", "WHIP", "K/9", "BB/9", "BAA"]

        subtitle = "Home vs Away"
        if location:
            subtitle = "Home" if location == "home" else "Away"

        parts = []
        parts.append(f"**{display_name}** \u2014 {season} {subtitle}\n")
        parts.append("[STATGRID]")
        parts.append("HEADER: " + ", ".join(headers))
        for row in rows[:2]:
            split_label = "Home" if row[0] == "home" else "Away"
            values = _format_pitching_values(headers, [str(v) if v is not None else "" for v in row[1:]])
            parts.append(f"ROW {split_label}: " + ", ".join(values))
        parts.append("[/STATGRID]")
        parts.append("\n[TIP]Tap a player name for their full profile.[/TIP]")
        parts.append(f"\n[SUGGEST]{display_name} {season}[/SUGGEST]")

        return "\n".join(parts)
    finally:
        conn.close()


# ===================================================================
# 18. build_risp_splits
# ===================================================================

def build_risp_splits(name: str, season: int) -> Optional[str]:
    """Batting RISP splits."""
    conn = _get_db()
    try:
        season = _resolve_season(conn, name, season)
        display_name, _ = _get_player_info(conn, name)
        cur = conn.cursor()
        cur.execute(
            "SELECT rs.split, rs.at_bats, rs.hits, "
            "rs.doubles, rs.triples, rs.home_runs, rs.rbi, "
            "rs.walks, rs.strikeouts, "
            "rs.batting_avg, rs.obp, rs.slg, rs.ops "
            "FROM risp_batting_splits rs "
            "JOIN players p ON rs.player_id = p.player_id "
            "WHERE p.name = ? AND rs.season = ? "
            "ORDER BY rs.split DESC",
            (_sanitize(name), season),
        )
        rows = cur.fetchall()
        if not rows:
            return None

        headers = ["AB", "H", "2B", "3B", "HR", "RBI", "BB", "SO", "AVG", "OBP", "SLG", "OPS"]

        parts = []
        parts.append(f"**{display_name}** \u2014 {season} With Runners in Scoring Position\n")
        parts.append("[STATGRID]")
        parts.append("HEADER: " + ", ".join(headers))
        for row in rows[:2]:
            split_label = "RISP" if row[0] == "RISP" else "Non-RISP"
            values = _format_values(headers, [str(v) if v is not None else "" for v in row[1:]])
            parts.append(f"ROW {split_label}: " + ", ".join(values))
        parts.append("[/STATGRID]")
        parts.append(f"\n[SUGGEST]{display_name} {season}[/SUGGEST]")
        if _has_platoon_data(conn, name):
            parts.append(f"[SUGGEST]{display_name} vs lefties {season}[/SUGGEST]")

        return "\n".join(parts)
    finally:
        conn.close()


# ===================================================================
# 19. build_pitching_risp_splits
# ===================================================================

def build_pitching_risp_splits(name: str, season: int) -> Optional[str]:
    """Pitching RISP splits."""
    conn = _get_db()
    try:
        season = _resolve_season(conn, name, season, "season_pitching_stats", "sp")
        display_name, _ = _get_player_info(conn, name)
        cur = conn.cursor()
        cur.execute(
            "SELECT rs.split, rs.at_bats, rs.hits, "
            "rs.doubles, rs.triples, rs.home_runs, "
            "rs.walks, rs.strikeouts, "
            "rs.batting_avg_against, rs.obp_against, rs.slg_against, rs.ops_against "
            "FROM risp_pitching_splits rs "
            "JOIN players p ON rs.player_id = p.player_id "
            "WHERE p.name = ? AND rs.season = ? "
            "ORDER BY rs.split DESC",
            (_sanitize(name), season),
        )
        rows = cur.fetchall()
        if not rows:
            return None

        headers = ["AB", "H", "2B", "3B", "HR", "BB", "SO", "AVG", "OBP", "SLG", "OPS"]

        parts = []
        parts.append(f"**{display_name}** \u2014 {season} With RISP (Pitching)\n")
        parts.append("[STATGRID]")
        parts.append("HEADER: " + ", ".join(headers))
        for row in rows[:2]:
            split_label = "RISP" if row[0] == "RISP" else "Non-RISP"
            values = _format_pitching_values(headers, [str(v) if v is not None else "" for v in row[1:]])
            parts.append(f"ROW {split_label}: " + ", ".join(values))
        parts.append("[/STATGRID]")
        parts.append(f"\n[SUGGEST]{display_name} {season}[/SUGGEST]")

        return "\n".join(parts)
    finally:
        conn.close()


# ===================================================================
# 19a. build_first_pa_splits — batter's stats restricted to first PA per game
# ===================================================================

def build_first_pa_splits(name: str, season: int) -> Optional[str]:
    """Batting stats from each game's first PA (one row per player-season)."""
    conn = _get_db()
    try:
        season = _resolve_season(conn, name, season)
        display_name, _ = _get_player_info(conn, name)
        cur = conn.cursor()
        cur.execute(
            "SELECT fps.plate_appearances, fps.at_bats, fps.hits, "
            "fps.doubles, fps.triples, fps.home_runs, fps.rbi, "
            "fps.walks, fps.strikeouts, "
            "fps.batting_avg, fps.obp, fps.slg, fps.ops "
            "FROM first_pa_batting_splits fps "
            "JOIN players p ON fps.player_id = p.player_id "
            "WHERE p.name = ? AND fps.season = ?",
            (_sanitize(name), season),
        )
        row = cur.fetchone()
        if not row:
            return None

        headers = ["PA", "AB", "H", "2B", "3B", "HR", "RBI", "BB", "SO",
                   "AVG", "OBP", "SLG", "OPS"]
        parts = []
        parts.append(f"**{display_name}** — {season} First PA of Game\n")
        parts.append("[STATGRID]")
        parts.append("HEADER: " + ", ".join(headers))
        values = _format_values(headers, [str(v) if v is not None else "" for v in row])
        parts.append("ROW First PA: " + ", ".join(values))
        parts.append("[/STATGRID]")
        parts.append(f"\n[SUGGEST]{display_name} {season}[/SUGGEST]")
        return "\n".join(parts)
    finally:
        conn.close()


# ===================================================================
# 19b. build_pitching_inning_splits — pitcher stats by inning
# ===================================================================

def build_pitching_inning_splits(name: str, inning: Optional[str], season: int) -> Optional[str]:
    """Pitching stats by inning. If inning is None, show all innings; if
    a specific inning is given (e.g. "1" or "10+"), show that one row only."""
    conn = _get_db()
    try:
        season = _resolve_season(conn, name, season, "season_pitching_stats", "sp")
        display_name, _ = _get_player_info(conn, name)
        cur = conn.cursor()
        if inning:
            cur.execute(
                "SELECT pis.inning, pis.at_bats, pis.hits, "
                "pis.doubles, pis.triples, pis.home_runs, "
                "pis.walks, pis.strikeouts, "
                "pis.batting_avg_against, pis.obp_against, pis.slg_against, pis.ops_against "
                "FROM pitching_inning_splits pis "
                "JOIN players p ON pis.player_id = p.player_id "
                "WHERE p.name = ? AND pis.season = ? AND pis.inning = ?",
                (_sanitize(name), season, inning),
            )
        else:
            cur.execute(
                "SELECT pis.inning, pis.at_bats, pis.hits, "
                "pis.doubles, pis.triples, pis.home_runs, "
                "pis.walks, pis.strikeouts, "
                "pis.batting_avg_against, pis.obp_against, pis.slg_against, pis.ops_against "
                "FROM pitching_inning_splits pis "
                "JOIN players p ON pis.player_id = p.player_id "
                "WHERE p.name = ? AND pis.season = ? "
                "ORDER BY CAST(pis.inning AS INTEGER), pis.inning",
                (_sanitize(name), season),
            )
        rows = cur.fetchall()
        if not rows:
            return None

        headers = ["AB", "H", "2B", "3B", "HR", "BB", "SO",
                   "AVG", "OBP", "SLG", "OPS"]
        title_inning = f" — Inning {inning}" if inning else " — By Inning"
        parts = []
        parts.append(f"**{display_name}** — {season} Pitching{title_inning}\n")
        parts.append("[STATGRID]")
        parts.append("HEADER: " + ", ".join(headers))
        for row in rows:
            inning_label = f"Inning {row[0]}"
            values = _format_pitching_values(headers, [str(v) if v is not None else "" for v in row[1:]])
            parts.append(f"ROW {inning_label}: " + ", ".join(values))
        parts.append("[/STATGRID]")
        parts.append(f"\n[SUGGEST]{display_name} {season}[/SUGGEST]")
        return "\n".join(parts)
    finally:
        conn.close()


# ===================================================================
# 19c. build_pitching_tto_splits — pitcher stats by times through the order
# ===================================================================

def build_pitching_tto_splits(name: str, tto: Optional[str], season: int) -> Optional[str]:
    """Pitching stats by times-through-the-order. If tto is None, show all
    rows; if specific (e.g. "3" or "4+"), show that one only."""
    conn = _get_db()
    try:
        season = _resolve_season(conn, name, season, "season_pitching_stats", "sp")
        display_name, _ = _get_player_info(conn, name)
        cur = conn.cursor()
        if tto:
            cur.execute(
                "SELECT pts.tto, pts.at_bats, pts.hits, "
                "pts.doubles, pts.triples, pts.home_runs, "
                "pts.walks, pts.strikeouts, "
                "pts.batting_avg_against, pts.obp_against, pts.slg_against, pts.ops_against "
                "FROM pitching_tto_splits pts "
                "JOIN players p ON pts.player_id = p.player_id "
                "WHERE p.name = ? AND pts.season = ? AND pts.tto = ?",
                (_sanitize(name), season, tto),
            )
        else:
            cur.execute(
                "SELECT pts.tto, pts.at_bats, pts.hits, "
                "pts.doubles, pts.triples, pts.home_runs, "
                "pts.walks, pts.strikeouts, "
                "pts.batting_avg_against, pts.obp_against, pts.slg_against, pts.ops_against "
                "FROM pitching_tto_splits pts "
                "JOIN players p ON pts.player_id = p.player_id "
                "WHERE p.name = ? AND pts.season = ? "
                "ORDER BY pts.tto",
                (_sanitize(name), season),
            )
        rows = cur.fetchall()
        if not rows:
            return None

        headers = ["AB", "H", "2B", "3B", "HR", "BB", "SO",
                   "AVG", "OBP", "SLG", "OPS"]
        _ordinals = {"1": "1st", "2": "2nd", "3": "3rd", "4+": "4+"}
        if tto:
            ordinal = _ordinals.get(tto, tto)
            noun = "Times" if tto == "4+" else "Time"
            title_tto = f" — {ordinal} {noun} Through the Order"
        else:
            title_tto = " — Times Through the Order"
        parts = []
        parts.append(f"**{display_name}** — {season} Pitching{title_tto}\n")
        parts.append("[STATGRID]")
        parts.append("HEADER: " + ", ".join(headers))
        single_row = tto is not None
        for row in rows:
            # Single-TTO requests: drop the row label (the title already names the
            # bucket). Multi-row "all TTO" view: short ordinal ("1st", "2nd", ...) —
            # avoids triggering the iOS player-name extractor, which lights up any
            # row label containing a space.
            tto_label = "" if single_row else _ordinals.get(row[0], row[0])
            values = _format_pitching_values(headers, [str(v) if v is not None else "" for v in row[1:]])
            if tto_label:
                parts.append(f"ROW {tto_label}: " + ", ".join(values))
            else:
                parts.append("ROW: " + ", ".join(values))
        parts.append("[/STATGRID]")
        parts.append(f"\n[SUGGEST]{display_name} {season}[/SUGGEST]")
        return "\n".join(parts)
    finally:
        conn.close()


# ===================================================================
# 20. build_pitch_type_splits
# ===================================================================

def build_pitch_type_splits(name: str, pitch_type: Optional[str] = None, season: int = 0) -> Optional[str]:
    """Batting pitch type splits."""
    conn = _get_db()
    try:
        season = _resolve_season(conn, name, season)
        display_name, _ = _get_player_info(conn, name)

        filter_clause = ""
        params = [_sanitize(name), season]
        if pitch_type:
            filter_clause = " AND pts.pitch_type = ?"
            params.append(pitch_type)

        cur = conn.cursor()
        cur.execute(
            "SELECT pts.pitch_type, pts.at_bats, pts.hits, "
            "pts.doubles, pts.triples, pts.home_runs, pts.rbi, "
            "pts.walks, pts.strikeouts, "
            "pts.batting_avg, pts.obp, pts.slg, pts.ops "
            "FROM pitch_type_batting_splits pts "
            "JOIN players p ON pts.player_id = p.player_id "
            f"WHERE p.name = ? AND pts.season = ?{filter_clause} "
            "ORDER BY pts.at_bats DESC",
            tuple(params),
        )
        rows = cur.fetchall()
        if not rows:
            return None

        headers = ["AB", "H", "2B", "3B", "HR", "RBI", "BB", "SO", "AVG", "OBP", "SLG", "OPS"]
        subtitle = f"vs {pitch_type}" if pitch_type else "By Pitch Type"

        parts = []
        parts.append(f"**{display_name}** \u2014 {season} {subtitle}\n")
        parts.append("[STATGRID]")
        parts.append("HEADER: " + ", ".join(headers))
        for row in rows:
            label = str(row[0])
            values = _format_values(headers, [str(v) if v is not None else "" for v in row[1:]])
            parts.append(f"ROW {label}: " + ", ".join(values))
        parts.append("[/STATGRID]")
        parts.append(f"\n[SUGGEST]{display_name} {season}[/SUGGEST]")

        return "\n".join(parts)
    finally:
        conn.close()


# ===================================================================
# 21. build_pitching_pitch_type_splits
# ===================================================================

def build_pitching_pitch_type_splits(name: str, pitch_type: Optional[str] = None, season: int = 0) -> Optional[str]:
    """Pitching pitch type splits."""
    conn = _get_db()
    try:
        season = _resolve_season(conn, name, season, "season_pitching_stats", "sp")
        display_name, _ = _get_player_info(conn, name)

        filter_clause = ""
        params = [_sanitize(name), season]
        if pitch_type:
            filter_clause = " AND pts.pitch_type = ?"
            params.append(pitch_type)

        cur = conn.cursor()
        cur.execute(
            "SELECT pts.pitch_type, pts.at_bats, pts.hits, "
            "pts.doubles, pts.triples, pts.home_runs, "
            "pts.walks, pts.strikeouts, "
            "pts.batting_avg_against, pts.obp_against, pts.slg_against, pts.ops_against "
            "FROM pitch_type_pitching_splits pts "
            "JOIN players p ON pts.player_id = p.player_id "
            f"WHERE p.name = ? AND pts.season = ?{filter_clause} "
            "ORDER BY pts.at_bats DESC",
            tuple(params),
        )
        rows = cur.fetchall()
        if not rows:
            return None

        headers = ["AB", "H", "2B", "3B", "HR", "BB", "SO", "AVG", "OBP", "SLG", "OPS"]
        subtitle = f"vs {pitch_type}" if pitch_type else "By Pitch Type"

        parts = []
        parts.append(f"**{display_name}** \u2014 {season} {subtitle} (Pitching)\n")
        parts.append("[STATGRID]")
        parts.append("HEADER: " + ", ".join(headers))
        for row in rows:
            label = str(row[0])
            values = _format_pitching_values(headers, [str(v) if v is not None else "" for v in row[1:]])
            parts.append(f"ROW {label}: " + ", ".join(values))
        parts.append("[/STATGRID]")
        parts.append(f"\n[SUGGEST]{display_name} {season}[/SUGGEST]")

        return "\n".join(parts)
    finally:
        conn.close()


# ===================================================================
# 22. build_count_splits
# ===================================================================

def build_count_splits(name: str, counts: Optional[list[str]] = None, season: int = 0) -> Optional[str]:
    """Batting count splits."""
    conn = _get_db()
    try:
        season = _resolve_season(conn, name, season)
        display_name, _ = _get_player_info(conn, name)

        filter_clause = ""
        params: list = [_sanitize(name), season]
        if counts:
            placeholders = ", ".join(["?"] * len(counts))
            filter_clause = f" AND cs.count_state IN ({placeholders})"
            params.extend(counts)

        cur = conn.cursor()
        cur.execute(
            "SELECT cs.count_state, cs.at_bats, cs.hits, "
            "cs.doubles, cs.triples, cs.home_runs, cs.rbi, "
            "cs.walks, cs.strikeouts, "
            "cs.batting_avg, cs.obp, cs.slg, cs.ops "
            "FROM count_batting_splits cs "
            "JOIN players p ON cs.player_id = p.player_id "
            f"WHERE p.name = ? AND cs.season = ?{filter_clause} "
            "ORDER BY cs.count_state",
            tuple(params),
        )
        rows = cur.fetchall()
        if not rows:
            return None

        headers = ["AB", "H", "2B", "3B", "HR", "RBI", "BB", "SO", "AVG", "OBP", "SLG", "OPS"]

        group_label = _count_group_label(counts)
        if counts and len(counts) == 1:
            subtitle = f"in {counts[0]} Counts"
        elif group_label:
            subtitle = f"With {group_label}"
        elif counts and len(counts) <= 4:
            subtitle = f"in {'/'.join(counts)} Counts"
        else:
            subtitle = "By Count"

        parts = []
        parts.append(f"**{display_name}** \u2014 {season} {subtitle}\n")
        parts.append("[STATGRID]")
        parts.append("HEADER: " + ", ".join(headers))

        # When showing a named group with multiple counts, add an aggregate row first
        if group_label and len(rows) > 1:
            tot_ab = sum(int(r[1] or 0) for r in rows)
            tot_h = sum(int(r[2] or 0) for r in rows)
            tot_2b = sum(int(r[3] or 0) for r in rows)
            tot_3b = sum(int(r[4] or 0) for r in rows)
            tot_hr = sum(int(r[5] or 0) for r in rows)
            tot_rbi = sum(int(r[6] or 0) for r in rows)
            tot_bb = sum(int(r[7] or 0) for r in rows)
            tot_so = sum(int(r[8] or 0) for r in rows)
            avg = tot_h / tot_ab if tot_ab > 0 else 0
            pa = tot_ab + tot_bb
            obp = (tot_h + tot_bb) / pa if pa > 0 else 0
            tb = tot_h - tot_2b - tot_3b - tot_hr + 2 * tot_2b + 3 * tot_3b + 4 * tot_hr
            slg = tb / tot_ab if tot_ab > 0 else 0
            ops = obp + slg
            agg_vals = [str(tot_ab), str(tot_h), str(tot_2b), str(tot_3b), str(tot_hr),
                        str(tot_rbi), str(tot_bb), str(tot_so),
                        _format_rate(f"{avg:.3f}"), _format_rate(f"{obp:.3f}"),
                        _format_rate(f"{slg:.3f}"), _format_rate(f"{ops:.3f}")]
            parts.append(f"ROW {group_label}: " + ", ".join(agg_vals))

        for row in rows:
            label = str(row[0])
            values = _format_values(headers, [str(v) if v is not None else "" for v in row[1:]])
            parts.append(f"ROW {label}: " + ", ".join(values))
        parts.append("[/STATGRID]")
        parts.append(f"\n[SUGGEST]{display_name} {season}[/SUGGEST]")
        if _has_platoon_data(conn, name):
            parts.append(f"[SUGGEST]{display_name} vs lefties {season}[/SUGGEST]")

        return "\n".join(parts)
    finally:
        conn.close()


# ===================================================================
# 23. build_pitching_count_splits
# ===================================================================

def build_pitching_count_splits(name: str, counts: Optional[list[str]] = None, season: int = 0) -> Optional[str]:
    """Pitching count splits."""
    conn = _get_db()
    try:
        season = _resolve_season(conn, name, season, "season_pitching_stats", "sp")
        display_name, _ = _get_player_info(conn, name)

        filter_clause = ""
        params: list = [_sanitize(name), season]
        if counts:
            placeholders = ", ".join(["?"] * len(counts))
            filter_clause = f" AND cs.count_state IN ({placeholders})"
            params.extend(counts)

        cur = conn.cursor()
        cur.execute(
            "SELECT cs.count_state, cs.at_bats, cs.hits, "
            "cs.doubles, cs.triples, cs.home_runs, "
            "cs.walks, cs.strikeouts, "
            "cs.batting_avg_against, cs.obp_against, cs.slg_against, cs.ops_against "
            "FROM count_pitching_splits cs "
            "JOIN players p ON cs.player_id = p.player_id "
            f"WHERE p.name = ? AND cs.season = ?{filter_clause} "
            "ORDER BY cs.count_state",
            tuple(params),
        )
        rows = cur.fetchall()
        if not rows:
            return None

        headers = ["AB", "H", "2B", "3B", "HR", "BB", "SO", "AVG", "OBP", "SLG", "OPS"]

        group_label = _count_group_label(counts)
        if counts and len(counts) == 1:
            subtitle = f"in {counts[0]} Counts"
        elif group_label:
            subtitle = f"With {group_label}"
        elif counts and len(counts) <= 4:
            subtitle = f"in {'/'.join(counts)} Counts"
        else:
            subtitle = "By Count"

        parts = []
        parts.append(f"**{display_name}** \u2014 {season} {subtitle} (Pitching)\n")
        parts.append("[STATGRID]")
        parts.append("HEADER: " + ", ".join(headers))

        # When showing a named group with multiple counts, add an aggregate row first
        if group_label and len(rows) > 1:
            tot_ab = sum(int(r[1] or 0) for r in rows)
            tot_h = sum(int(r[2] or 0) for r in rows)
            tot_2b = sum(int(r[3] or 0) for r in rows)
            tot_3b = sum(int(r[4] or 0) for r in rows)
            tot_hr = sum(int(r[5] or 0) for r in rows)
            tot_bb = sum(int(r[6] or 0) for r in rows)
            tot_so = sum(int(r[7] or 0) for r in rows)
            avg = tot_h / tot_ab if tot_ab > 0 else 0
            pa = tot_ab + tot_bb
            obp = (tot_h + tot_bb) / pa if pa > 0 else 0
            tb = tot_h - tot_2b - tot_3b - tot_hr + 2 * tot_2b + 3 * tot_3b + 4 * tot_hr
            slg = tb / tot_ab if tot_ab > 0 else 0
            ops = obp + slg
            agg_vals = [str(tot_ab), str(tot_h), str(tot_2b), str(tot_3b), str(tot_hr),
                        str(tot_bb), str(tot_so),
                        _format_rate(f"{avg:.3f}"), _format_rate(f"{obp:.3f}"),
                        _format_rate(f"{slg:.3f}"), _format_rate(f"{ops:.3f}")]
            parts.append(f"ROW {group_label}: " + ", ".join(agg_vals))

        for row in rows:
            label = str(row[0])
            values = _format_pitching_values(headers, [str(v) if v is not None else "" for v in row[1:]])
            parts.append(f"ROW {label}: " + ", ".join(values))
        parts.append("[/STATGRID]")
        parts.append(f"\n[SUGGEST]{display_name} {season}[/SUGGEST]")

        return "\n".join(parts)
    finally:
        conn.close()


# ===================================================================
# 24. build_month_stats
# ===================================================================

def build_month_stats(name: str, month: int, season: int) -> Optional[str]:
    """Aggregate game batting logs by month."""
    conn = _get_db()
    try:
        display_name, _ = _get_player_info(conn, name)
        month_pad = f"{month:02d}"
        month_names = [
            "", "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December",
        ]
        month_name = month_names[month] if 1 <= month <= 12 else f"Month {month}"

        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) as g, "
            "SUM(g.at_bats) as ab, SUM(g.hits) as h, SUM(g.doubles) as d2b, "
            "SUM(g.triples) as d3b, SUM(g.home_runs) as hr, "
            "SUM(g.runs) as r, SUM(g.rbi) as rbi, "
            "SUM(g.walks) as bb, SUM(g.strikeouts) as so, "
            "SUM(g.plate_appearances) as pa, "
            "SUM(g.hit_by_pitch) as hbp, SUM(g.sacrifice_flies) as sf "
            "FROM game_batting_logs g "
            "JOIN players p ON g.player_id = p.player_id "
            "WHERE p.name = ? AND g.season = ? AND substr(g.date, 6, 2) = ?",
            (_sanitize(name), season, month_pad),
        )
        row = cur.fetchone()
        if not row or not row[0] or int(row[0]) == 0:
            return None

        games = int(row[0])
        ab = int(row[1] or 0)
        h = int(row[2] or 0)
        d2b = int(row[3] or 0)
        d3b = int(row[4] or 0)
        hr = int(row[5] or 0)
        r = int(row[6] or 0)
        rbi = int(row[7] or 0)
        bb = int(row[8] or 0)
        so = int(row[9] or 0)
        hbp = int(row[11] or 0)
        sf = int(row[12] or 0)

        # Compute rate stats
        avg = h / ab if ab > 0 else 0.0
        obp_denom = ab + bb + hbp + sf
        obp = (h + bb + hbp) / obp_denom if obp_denom > 0 else 0.0
        tb = h + d2b + 2 * d3b + 3 * hr
        slg = tb / ab if ab > 0 else 0.0
        ops = obp + slg

        avg_s = _format_rate(f"{avg:.3f}")
        obp_s = _format_rate(f"{obp:.3f}")
        slg_s = _format_rate(f"{slg:.3f}")
        ops_s = _format_rate(f"{ops:.3f}")

        parts = []
        parts.append(f"**{display_name}** \u2014 {month_name} {season}\n")
        parts.append("[STATGRID]")
        parts.append("HEADER: G, AB, R, H, 2B, 3B, HR, RBI, BB, SO, AVG, OBP, SLG, OPS")
        parts.append(
            f"ROW: {games}, {ab}, {r}, {h}, {d2b}, {d3b}, {hr}, {rbi}, "
            f"{bb}, {so}, {avg_s}, {obp_s}, {slg_s}, {ops_s}"
        )
        parts.append("[/STATGRID]")
        parts.append(f"\n[SUGGEST]{display_name} {season}[/SUGGEST]")
        if _is_active_player(conn, name):
            parts.append(f"[SUGGEST]how is {display_name} doing lately[/SUGGEST]")

        return "\n".join(parts)
    finally:
        conn.close()


def build_player_date_range(name: str, since_date: Optional[str] = None,
                            end_date: Optional[str] = None) -> Optional[str]:
    """Aggregate a player's batting game logs over a date range.

    Generalizes build_month_stats to an arbitrary [since_date, end_date]
    window (either bound may be None for open-ended). Powers "stats after
    June 30", "since the all-star break", "in the last 30 days". Batting
    only (matches build_month_stats); returns None if no batting rows so
    pitchers fall through to Haiku.
    """
    if not since_date and not end_date:
        return None
    conn = _get_db()
    try:
        display_name, _ = _get_player_info(conn, name)
        where = ["p.name = ?"]
        params: list = [_sanitize(name)]
        if since_date:
            where.append("g.date >= ?"); params.append(since_date)
        if end_date:
            where.append("g.date <= ?"); params.append(end_date)
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) as g, "
            "SUM(g.at_bats) as ab, SUM(g.hits) as h, SUM(g.doubles) as d2b, "
            "SUM(g.triples) as d3b, SUM(g.home_runs) as hr, "
            "SUM(g.runs) as r, SUM(g.rbi) as rbi, "
            "SUM(g.walks) as bb, SUM(g.strikeouts) as so, "
            "SUM(g.plate_appearances) as pa, "
            "SUM(g.hit_by_pitch) as hbp, SUM(g.sacrifice_flies) as sf "
            "FROM game_batting_logs g "
            "JOIN players p ON g.player_id = p.player_id "
            "WHERE " + " AND ".join(where),
            params,
        )
        row = cur.fetchone()
        if not row or not row[0] or int(row[0]) == 0:
            return None

        games = int(row[0]); ab = int(row[1] or 0); h = int(row[2] or 0)
        d2b = int(row[3] or 0); d3b = int(row[4] or 0); hr = int(row[5] or 0)
        r = int(row[6] or 0); rbi = int(row[7] or 0); bb = int(row[8] or 0)
        so = int(row[9] or 0); hbp = int(row[11] or 0); sf = int(row[12] or 0)

        avg = h / ab if ab > 0 else 0.0
        obp_denom = ab + bb + hbp + sf
        obp = (h + bb + hbp) / obp_denom if obp_denom > 0 else 0.0
        tb = h + d2b + 2 * d3b + 3 * hr
        slg = tb / ab if ab > 0 else 0.0
        ops = obp + slg
        avg_s = _format_rate(f"{avg:.3f}"); obp_s = _format_rate(f"{obp:.3f}")
        slg_s = _format_rate(f"{slg:.3f}"); ops_s = _format_rate(f"{ops:.3f}")

        from datetime import datetime as _dt
        def _fmt(d):
            try:
                return _dt.strptime(d, "%Y-%m-%d").strftime("%b %-d, %Y")
            except Exception:
                return d
        if since_date and end_date:
            label = f"{_fmt(since_date)} – {_fmt(end_date)}"
        elif since_date:
            label = f"since {_fmt(since_date)}"
        else:
            label = f"through {_fmt(end_date)}"

        parts = []
        parts.append(f"**{display_name}** — {label}\n")
        parts.append("[STATGRID]")
        parts.append("HEADER: G, AB, R, H, 2B, 3B, HR, RBI, BB, SO, AVG, OBP, SLG, OPS")
        parts.append(
            f"ROW: {games}, {ab}, {r}, {h}, {d2b}, {d3b}, {hr}, {rbi}, "
            f"{bb}, {so}, {avg_s}, {obp_s}, {slg_s}, {ops_s}"
        )
        parts.append("[/STATGRID]")
        szn = (since_date or end_date or "")[:4]
        if szn:
            parts.append(f"\n[SUGGEST]{display_name} {szn}[/SUGGEST]")
        if _is_active_player(conn, name):
            parts.append(f"[SUGGEST]how is {display_name} doing lately[/SUGGEST]")
        return "\n".join(parts)
    finally:
        conn.close()


def build_player_vs_team(name: str, opponent_code: str,
                         season: Optional[int] = None) -> Optional[str]:
    """A player's batting line vs a specific opponent, from game logs.

    Powers "{player} vs the {team}". season=None → career split (all years);
    else that season only. Batting only — returns None if no batting rows so
    pitchers / unknown players fall through to Haiku.
    """
    if not opponent_code:
        return None
    conn = _get_db()
    try:
        display_name, _ = _get_player_info(conn, name)
        where = ["p.name = ?", "g.opponent = ?"]
        params: list = [_sanitize(name), opponent_code]
        if season:
            where.append("g.season = ?"); params.append(season)
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) as g, "
            "SUM(g.at_bats) as ab, SUM(g.hits) as h, SUM(g.doubles) as d2b, "
            "SUM(g.triples) as d3b, SUM(g.home_runs) as hr, "
            "SUM(g.runs) as r, SUM(g.rbi) as rbi, "
            "SUM(g.walks) as bb, SUM(g.strikeouts) as so, "
            "SUM(g.plate_appearances) as pa, "
            "SUM(g.hit_by_pitch) as hbp, SUM(g.sacrifice_flies) as sf "
            "FROM game_batting_logs g "
            "JOIN players p ON g.player_id = p.player_id "
            "WHERE " + " AND ".join(where),
            params,
        )
        row = cur.fetchone()
        if not row or not row[0] or int(row[0]) == 0:
            return None

        games = int(row[0]); ab = int(row[1] or 0); h = int(row[2] or 0)
        d2b = int(row[3] or 0); d3b = int(row[4] or 0); hr = int(row[5] or 0)
        r = int(row[6] or 0); rbi = int(row[7] or 0); bb = int(row[8] or 0)
        so = int(row[9] or 0); hbp = int(row[11] or 0); sf = int(row[12] or 0)

        avg = h / ab if ab > 0 else 0.0
        obp_denom = ab + bb + hbp + sf
        obp = (h + bb + hbp) / obp_denom if obp_denom > 0 else 0.0
        tb = h + d2b + 2 * d3b + 3 * hr
        slg = tb / ab if ab > 0 else 0.0
        ops = obp + slg
        avg_s = _format_rate(f"{avg:.3f}"); obp_s = _format_rate(f"{obp:.3f}")
        slg_s = _format_rate(f"{slg:.3f}"); ops_s = _format_rate(f"{ops:.3f}")

        opp = _team_full_name(opponent_code)
        scope_label = f"{season}" if season else "career"
        parts = [f"**{display_name}** vs {opp} — {scope_label}\n"]
        parts.append("[STATGRID]")
        parts.append("HEADER: G, AB, R, H, 2B, 3B, HR, RBI, BB, SO, AVG, OBP, SLG, OPS")
        parts.append(
            f"ROW: {games}, {ab}, {r}, {h}, {d2b}, {d3b}, {hr}, {rbi}, "
            f"{bb}, {so}, {avg_s}, {obp_s}, {slg_s}, {ops_s}"
        )
        parts.append("[/STATGRID]")
        if _is_active_player(conn, name):
            parts.append(f"\n[SUGGEST]how is {display_name} doing lately[/SUGGEST]")
        return "\n".join(parts)
    finally:
        conn.close()


# Game-log counting columns we can sum over a sliding window. Rate stats
# (AVG/OBP/...) and derived stats are excluded — small-denominator rates over
# a few games are meaningless.
_SLIDING_BAT_COLS = {"hits", "doubles", "triples", "home_runs", "runs", "rbi",
                     "walks", "strikeouts", "stolen_bases", "at_bats",
                     "plate_appearances"}
_SLIDING_PITCH_COLS = {"strikeouts", "walks", "earned_runs", "hits",
                       "home_runs", "runs"}


def build_player_sliding_window(name: str, stat_info: StatInfo, n: int,
                                direction: str = "max",
                                is_pitching: bool = False) -> Optional[str]:
    """Best/worst N-consecutive-game stretch for a player + counting stat.

    Scans the player's game logs season by season (windows never cross an
    offseason) and slides an N-game window. Returns the extreme window with its
    date range. Counting stats only — anything else returns None (→ Haiku).
    """
    if not stat_info or n is None or n < 2:
        return None
    col = stat_info.db_column
    table = "game_pitching_logs" if is_pitching else "game_batting_logs"
    allowed = _SLIDING_PITCH_COLS if is_pitching else _SLIDING_BAT_COLS
    if col not in allowed:
        return None
    conn = _get_db()
    try:
        display_name, _ = _get_player_info(conn, name)
        cur = conn.cursor()
        cur.execute(
            f"SELECT g.season, g.date, g.{col} "
            f"FROM {table} g JOIN players p ON g.player_id = p.player_id "
            f"WHERE p.name = ? ORDER BY g.season, g.date",
            (_sanitize(name),),
        )
        rows = cur.fetchall()
        if not rows:
            return None

        seasons: dict = {}
        for season, dt, val in rows:
            seasons.setdefault(season, []).append((dt, int(val or 0)))

        best = None  # (value, season, start_date, end_date)
        for season, games in seasons.items():
            if len(games) < n:
                continue
            window = sum(v for _, v in games[:n])
            for end_i in range(n - 1, len(games)):
                if end_i >= n:
                    window += games[end_i][1] - games[end_i - n][1]
                if (best is None
                        or (direction == "max" and window > best[0])
                        or (direction == "min" and window < best[0])):
                    best = (window, season, games[end_i - n + 1][0], games[end_i][0])
        if best is None:
            return None
        value, season, start_d, end_d = best

        from datetime import datetime as _dt
        def _fmt(d):
            try:
                return _dt.strptime(d, "%Y-%m-%d").strftime("%b %-d")
            except Exception:
                return d
        noun = stat_info.display_name.lower()
        dir_word = "fewest" if direction == "min" else "most"
        parts = [
            f"**{display_name}** — {dir_word} {noun} in a {n}-game stretch\n",
            f"**{value}** {noun} over {n} games "
            f"({_fmt(start_d)}–{_fmt(end_d)}, {season}).",
        ]
        if _is_active_player(conn, name):
            parts.append(f"\n[SUGGEST]how is {display_name} doing lately[/SUGGEST]")
        return "\n".join(parts)
    finally:
        conn.close()


# ===================================================================
# 25. build_leaderboard
# ===================================================================

def build_leaderboard(stat_info: StatInfo, scope: str, limit: int = 10,
                      league: Optional[str] = None,
                      season: Optional[int] = None,
                      since_year: Optional[int] = None,
                      rookie: bool = False,
                      position: Optional[list[str]] = None,
                      pitcher_role: Optional[str] = None,
                      sort_asc: bool = False) -> Optional[str]:
    """
    Build a batting leaderboard.

    scope is one of: "season", "allTimeSingleSeason", "allTimeSince", "career".
    For "season", pass season=YYYY.  For "allTimeSince", pass since_year=YYYY.

    Also accepts name_matcher scope strings: "season_2024", "all_time",
    "all_time_since_2000", "career" — auto-parsed into canonical form.
    """
    # Parse name_matcher scope strings into canonical form
    if scope.startswith("season_"):
        season = season or int(scope.split("_", 1)[1])
        scope = "season"
    elif scope.startswith("all_time_since_"):
        since_year = since_year or int(scope.rsplit("_", 1)[1])
        scope = "allTimeSince"
    elif scope == "all_time":
        scope = "allTimeSingleSeason"

    if scope == "career" and stat_info.display_abbrev == "OPS+":
        return ("Career OPS+ leaders require weighted season averaging, which isn't "
                "supported yet. Try **career OPS leaders** instead.\n\n"
                "[SUGGEST]career ops leaders[/SUGGEST]")

    conn = _get_db()
    try:
        league_filter = f" AND {_league_team_clause(league, 's')}" if league else ""
        league_label = f" ({league})" if league else ""
        rookie_clause = _rookie_filter("s") if rookie else ""
        rookie_label = "Rookie " if rookie else ""
        pos_clause = _position_filter(position, "s", "s.season") if position else ""
        pos_label = f"{_position_label(position)} " if position else ""
        stat_name = stat_info.pill_name
        # Sort direction: "worst"/"fewest" → ASC for counting stats, DESC→ASC for rate
        order = "ASC" if sort_asc else "DESC"
        direction_label = "Fewest " if sort_asc and not stat_info.is_rate else "Worst " if sort_asc else ""

        if scope == "season":
            yr = season or datetime.now().year
            # PA minimum for rate stats
            pa_min = None
            if stat_info.is_rate:
                pa_min = _qual_min_pa(conn, yr)
            pa_filter = f" AND s.plate_appearances >= {pa_min}" if pa_min else ""

            cur = conn.cursor()
            cur.execute(
                f"SELECT p.name, s.{stat_info.db_column} "
                f"FROM season_batting_stats s "
                f"JOIN players p ON s.player_id = p.player_id "
                f"WHERE s.season = ?{pa_filter}{league_filter}{rookie_clause}{pos_clause} "
                f"ORDER BY s.{stat_info.db_column} {order} LIMIT ?",
                (yr, limit),
            )
            rows = cur.fetchall()
            if not rows:
                return f"No {pos_label.lower()}{rookie_label.lower()}{stat_info.display_name} leaders found for {yr}{league_label}."

            parts = [f"**{yr} {direction_label}{pos_label}{rookie_label}{stat_info.display_name} Leaders{league_label}**\n"]
            parts.append("[TIP]Tap a player name for their full profile.[/TIP]")
            parts.append("[LEADERBOARD]")
            parts.append(f"HEADER: {stat_info.display_abbrev}")
            for i, row in enumerate(rows):
                val = _format_rate(str(row[1])) if stat_info.is_rate else str(row[1])
                parts.append(f"ROW {i+1}. {row[0]}: {val}")
            parts.append("[/LEADERBOARD]")
            if pa_min:
                parts.append(f"\n_Min. {pa_min} PA._")
            # Adjacent season pills for easy navigation
            current_year = datetime.now().year
            if yr != current_year:
                parts.append(f"\n[SUGGEST]{current_year} {stat_name} leaders[/SUGGEST]")
            if yr > 1898:
                parts.append(f"[SUGGEST]{yr - 1} {stat_name} leaders[/SUGGEST]")
            parts.append(f"[SUGGEST]all-time single season {stat_name} leaders[/SUGGEST]")
            parts.append(f"[SUGGEST]career {stat_name} leaders[/SUGGEST]")
            if league:
                other = "NL" if league == "AL" else "AL"
                parts.append(f"[SUGGEST]{yr} {stat_name} leaders ({other})[/SUGGEST]")
                parts.append(f"[SUGGEST]{yr} {stat_name} leaders (MLB)[/SUGGEST]")
            else:
                parts.append(f"[SUGGEST]{yr} {stat_name} leaders (AL)[/SUGGEST]")
                parts.append(f"[SUGGEST]{yr} {stat_name} leaders (NL)[/SUGGEST]")
            return "\n".join(parts)

        elif scope == "allTimeSingleSeason":
            # Build WHERE clause from parts
            conditions = []
            if stat_info.is_rate:
                conditions.append("s.plate_appearances >= 400")
            if league_filter:
                conditions.append(league_filter[5:])  # strip leading " AND "
            if rookie:
                conditions.append(rookie_clause[5:])  # strip leading " AND "
            if pos_clause:
                conditions.append(pos_clause[5:])  # strip leading " AND "
            pa_filter = f" WHERE {' AND '.join(conditions)}" if conditions else ""
            cur = conn.cursor()
            cur.execute(
                f"SELECT p.name, s.{stat_info.db_column}, s.season "
                f"FROM season_batting_stats s "
                f"JOIN players p ON s.player_id = p.player_id "
                f"{pa_filter} "
                f"ORDER BY s.{stat_info.db_column} DESC LIMIT ?",
                (limit,),
            )
            rows = cur.fetchall()
            if not rows:
                return f"No all-time {pos_label.lower()}{rookie_label.lower()}{stat_info.display_name} leaders found{league_label}."

            parts = [f"**All-Time Single Season {pos_label}{rookie_label}{stat_info.display_name} Leaders{league_label}**\n"]
            parts.append("[TIP]Tap a player name for their full profile.[/TIP]")
            parts.append("[LEADERBOARD]")
            parts.append(f"HEADER: {stat_info.display_abbrev}, Year")
            for i, row in enumerate(rows):
                val = _format_rate(str(row[1])) if stat_info.is_rate else str(row[1])
                parts.append(f"ROW {i+1}. {row[0]}: {val}, {row[2]}")
            parts.append("[/LEADERBOARD]")
            if stat_info.is_rate:
                parts.append("\n_Min. 400 PA._")
            parts.append(f"\n[SUGGEST]career {stat_name} leaders[/SUGGEST]")
            if league:
                other = "NL" if league == "AL" else "AL"
                parts.append(f"[SUGGEST]all-time single season {stat_name} leaders ({other})[/SUGGEST]")
                parts.append(f"[SUGGEST]all-time single season {stat_name} leaders (MLB)[/SUGGEST]")
            else:
                parts.append(f"[SUGGEST]all-time single season {stat_name} leaders (AL)[/SUGGEST]")
                parts.append(f"[SUGGEST]all-time single season {stat_name} leaders (NL)[/SUGGEST]")
            return "\n".join(parts)

        elif scope == "allTimeSince":
            sy = since_year or 2000
            pa_filter = " AND s.plate_appearances >= 400" if stat_info.is_rate else ""
            cur = conn.cursor()
            cur.execute(
                f"SELECT p.name, s.{stat_info.db_column}, s.season "
                f"FROM season_batting_stats s "
                f"JOIN players p ON s.player_id = p.player_id "
                f"WHERE s.season >= ?{pa_filter}{league_filter}{rookie_clause}{pos_clause} "
                f"ORDER BY s.{stat_info.db_column} DESC LIMIT ?",
                (sy, limit),
            )
            rows = cur.fetchall()
            if not rows:
                return f"No {pos_label.lower()}{rookie_label.lower()}{stat_info.display_name} leaders found since {sy}{league_label}."

            parts = [f"**{pos_label}{rookie_label}{stat_info.display_name} Leaders Since {sy}{league_label}**\n"]
            parts.append("[TIP]Tap a player name for their full profile.[/TIP]")
            parts.append("[LEADERBOARD]")
            parts.append(f"HEADER: {stat_info.display_abbrev}, Year")
            for i, row in enumerate(rows):
                val = _format_rate(str(row[1])) if stat_info.is_rate else str(row[1])
                parts.append(f"ROW {i+1}. {row[0]}: {val}, {row[2]}")
            parts.append("[/LEADERBOARD]")
            if stat_info.is_rate:
                parts.append("\n_Min. 400 PA._")
            parts.append(f"\n[SUGGEST]all-time single season {stat_name} leaders[/SUGGEST]")
            if league:
                other = "NL" if league == "AL" else "AL"
                parts.append(f"[SUGGEST]{stat_name} leaders since {sy} ({other})[/SUGGEST]")
                parts.append(f"[SUGGEST]{stat_name} leaders since {sy} (MLB)[/SUGGEST]")
            else:
                parts.append(f"[SUGGEST]{stat_name} leaders since {sy} (AL)[/SUGGEST]")
                parts.append(f"[SUGGEST]{stat_name} leaders since {sy} (NL)[/SUGGEST]")
            return "\n".join(parts)

        elif scope == "career":
            if stat_info.is_rate:
                formula = _career_rate_formula(stat_info)
                if not formula:
                    return f"Career {stat_info.display_name} leaders are not available."
                select_expr = f"{formula} as career_val"
            else:
                select_expr = f"SUM(s.{stat_info.db_column}) as career_val"

            pa_having = "\n            HAVING SUM(s.plate_appearances) >= 400" if stat_info.is_rate else ""
            # Career-scope position filter: BR-style primary-career-position match.
            # Without this, "career HR leaders among catchers" silently ignored
            # the position filter here (though query_engine's career path does
            # apply one; this closes that gap for any direct build_leaderboard
            # caller that hits the career branch).
            career_pos_clause = _position_filter_career(position, "s") if position else ""
            pieces = [p for p in (league_filter, career_pos_clause) if p]
            where_clause = f" WHERE {' AND '.join(p[5:] for p in pieces)}" if pieces else ""

            cur = conn.cursor()
            cur.execute(
                f"SELECT p.name, {select_expr} "
                f"FROM season_batting_stats s "
                f"JOIN players p ON s.player_id = p.player_id "
                f"{where_clause} "
                f"GROUP BY p.player_id{pa_having} "
                f"ORDER BY career_val DESC LIMIT ?",
                (limit,),
            )
            rows = cur.fetchall()
            if not rows:
                return f"No career {stat_info.display_name} leaders found{league_label}."

            parts = [f"**Career {stat_info.display_name} Leaders{league_label}**\n"]
            parts.append("[TIP]Tap a player name for their full profile.[/TIP]")
            parts.append("[LEADERBOARD]")
            parts.append(f"HEADER: {stat_info.display_abbrev}")
            for i, row in enumerate(rows):
                val = _format_rate(str(row[1])) if stat_info.is_rate else str(row[1])
                parts.append(f"ROW {i+1}. {row[0]}: {val}")
            parts.append("[/LEADERBOARD]")
            if stat_info.is_rate:
                parts.append("\n_Min. 400 PA._")
            parts.append(f"\n[SUGGEST]all-time single season {stat_name} leaders[/SUGGEST]")
            if league:
                other = "NL" if league == "AL" else "AL"
                parts.append(f"[SUGGEST]career {stat_name} leaders ({other})[/SUGGEST]")
                parts.append(f"[SUGGEST]career {stat_name} leaders (MLB)[/SUGGEST]")
            else:
                parts.append(f"[SUGGEST]career {stat_name} leaders (AL)[/SUGGEST]")
                parts.append(f"[SUGGEST]career {stat_name} leaders (NL)[/SUGGEST]")
            return "\n".join(parts)

        return None
    finally:
        conn.close()


# ===================================================================
# 26. build_pitching_leaderboard
# ===================================================================

def build_pitching_leaderboard(stat_info: StatInfo, scope: str, limit: int = 10,
                               league: Optional[str] = None,
                               season: Optional[int] = None,
                               since_year: Optional[int] = None,
                               pitcher_role: Optional[str] = None,
                               sort_asc: bool = False) -> Optional[str]:
    """
    Build a pitching leaderboard.

    scope: "season", "allTimeSingleSeason", "allTimeSince", "career".
    Also accepts name_matcher scope strings (auto-parsed).
    pitcher_role: "starter" or "reliever" to filter by role.
    """
    # Parse name_matcher scope strings into canonical form
    if scope.startswith("season_"):
        season = season or int(scope.split("_", 1)[1])
        scope = "season"
    elif scope.startswith("all_time_since_"):
        since_year = since_year or int(scope.rsplit("_", 1)[1])
        scope = "allTimeSince"
    elif scope == "all_time":
        scope = "allTimeSingleSeason"

    conn = _get_db()
    try:
        league_filter = f" AND {_league_team_clause(league, 'sp')}" if league else ""
        league_label = f" ({league})" if league else ""
        stat_name = stat_info.pill_name
        # Pitcher role filter
        role_clause = ""
        role_label = ""
        if pitcher_role == "starter":
            role_clause = " AND sp.games_started > sp.games / 2"
            role_label = "Starting "
        elif pitcher_role == "reliever":
            role_clause = " AND sp.games_started <= sp.games / 2"
            role_label = "Relief "
        # Sort direction — "worst" inverts, but for pitching "lower is better" stats
        # are already ASC, so "worst ERA" would be DESC
        natural_asc = stat_info.display_abbrev in _LOWER_IS_BETTER_PITCHING
        if sort_asc:
            order_dir = "DESC" if natural_asc else "ASC"  # Invert natural
        else:
            order_dir = "ASC" if natural_asc else "DESC"
        direction_label = "Worst " if sort_asc else ""

        def _fmt(raw):
            return _format_pitching_rate(raw, 2) if stat_info.is_rate else str(raw)

        if scope == "season":
            yr = season or datetime.now().year
            ip_filter = ""
            ip_note = None
            if stat_info.is_rate:
                ip_outs_min = _qual_min_ip_outs(conn, yr)
                ip_filter = f" AND sp.ip_outs >= {ip_outs_min}"
                ip_note = f"Min. {ip_outs_min // 3}.{ip_outs_min % 3} IP."

            cur = conn.cursor()
            cur.execute(
                f"SELECT p.name, sp.{stat_info.db_column} "
                f"FROM season_pitching_stats sp "
                f"JOIN players p ON sp.player_id = p.player_id "
                f"WHERE sp.season = ?{ip_filter}{league_filter}{role_clause} "
                f"ORDER BY sp.{stat_info.db_column} {order_dir} LIMIT ?",
                (yr, limit),
            )
            rows = cur.fetchall()
            if not rows:
                return f"No {role_label.lower()}{stat_info.display_name} leaders found for {yr}{league_label}."

            parts = [f"**{yr} {direction_label}{role_label}{stat_info.display_name} Leaders{league_label} (Pitching)**\n"]
            parts.append("[TIP]Tap a player name for their full profile.[/TIP]")
            parts.append("[LEADERBOARD]")
            parts.append(f"HEADER: {stat_info.display_abbrev}")
            for i, row in enumerate(rows):
                parts.append(f"ROW {i+1}. {row[0]}: {_fmt(row[1])}")
            parts.append("[/LEADERBOARD]")
            if ip_note:
                parts.append(f"\n_{ip_note}_")
            # Adjacent season pills for easy navigation
            current_year = datetime.now().year
            if yr != current_year:
                parts.append(f"\n[SUGGEST]{current_year} {stat_name} leaders[/SUGGEST]")
            if yr > 1898:
                parts.append(f"[SUGGEST]{yr - 1} {stat_name} leaders[/SUGGEST]")
            parts.append(f"[SUGGEST]career {stat_name} leaders[/SUGGEST]")
            if league:
                other = "NL" if league == "AL" else "AL"
                parts.append(f"[SUGGEST]{yr} {stat_name} leaders ({other})[/SUGGEST]")
                parts.append(f"[SUGGEST]{yr} {stat_name} leaders (MLB)[/SUGGEST]")
            else:
                parts.append(f"[SUGGEST]{yr} {stat_name} leaders (AL)[/SUGGEST]")
                parts.append(f"[SUGGEST]{yr} {stat_name} leaders (NL)[/SUGGEST]")
            return "\n".join(parts)

        elif scope == "allTimeSingleSeason":
            ip_filter = f" WHERE sp.ip_outs >= 486{league_filter}" if stat_info.is_rate else (
                f" WHERE {league_filter[5:]}" if league_filter else ""
            )
            cur = conn.cursor()
            cur.execute(
                f"SELECT p.name, sp.{stat_info.db_column}, sp.season "
                f"FROM season_pitching_stats sp "
                f"JOIN players p ON sp.player_id = p.player_id "
                f"{ip_filter} "
                f"ORDER BY sp.{stat_info.db_column} {order_dir} LIMIT ?",
                (limit,),
            )
            rows = cur.fetchall()
            if not rows:
                return f"No all-time {stat_info.display_name} leaders found{league_label}."

            parts = [f"**All-Time Single Season {stat_info.display_name} Leaders{league_label} (Pitching)**\n"]
            parts.append("[TIP]Tap a player name for their full profile.[/TIP]")
            parts.append("[LEADERBOARD]")
            parts.append(f"HEADER: {stat_info.display_abbrev}, Year")
            for i, row in enumerate(rows):
                parts.append(f"ROW {i+1}. {row[0]}: {_fmt(row[1])}, {row[2]}")
            parts.append("[/LEADERBOARD]")
            if stat_info.is_rate:
                parts.append("\n_Min. 162 IP._")
            if league:
                other = "NL" if league == "AL" else "AL"
                parts.append(f"\n[SUGGEST]all-time single season {stat_name} leaders ({other})[/SUGGEST]")
                parts.append(f"[SUGGEST]all-time single season {stat_name} leaders (MLB)[/SUGGEST]")
            else:
                parts.append(f"\n[SUGGEST]all-time single season {stat_name} leaders (AL)[/SUGGEST]")
                parts.append(f"[SUGGEST]all-time single season {stat_name} leaders (NL)[/SUGGEST]")
            return "\n".join(parts)

        elif scope == "allTimeSince":
            sy = since_year or 2000
            ip_filter = " AND sp.ip_outs >= 486" if stat_info.is_rate else ""
            cur = conn.cursor()
            cur.execute(
                f"SELECT p.name, sp.{stat_info.db_column}, sp.season "
                f"FROM season_pitching_stats sp "
                f"JOIN players p ON sp.player_id = p.player_id "
                f"WHERE sp.season >= ?{ip_filter}{league_filter} "
                f"ORDER BY sp.{stat_info.db_column} {order_dir} LIMIT ?",
                (sy, limit),
            )
            rows = cur.fetchall()
            if not rows:
                return f"No {stat_info.display_name} leaders found since {sy}{league_label}."

            parts = [f"**{stat_info.display_name} Leaders Since {sy}{league_label} (Pitching)**\n"]
            parts.append("[TIP]Tap a player name for their full profile.[/TIP]")
            parts.append("[LEADERBOARD]")
            parts.append(f"HEADER: {stat_info.display_abbrev}, Year")
            for i, row in enumerate(rows):
                parts.append(f"ROW {i+1}. {row[0]}: {_fmt(row[1])}, {row[2]}")
            parts.append("[/LEADERBOARD]")
            if stat_info.is_rate:
                parts.append("\n_Min. 162 IP._")
            if league:
                other = "NL" if league == "AL" else "AL"
                parts.append(f"\n[SUGGEST]{stat_name} leaders since {sy} ({other})[/SUGGEST]")
                parts.append(f"[SUGGEST]{stat_name} leaders since {sy} (MLB)[/SUGGEST]")
            else:
                parts.append(f"\n[SUGGEST]{stat_name} leaders since {sy} (AL)[/SUGGEST]")
                parts.append(f"[SUGGEST]{stat_name} leaders since {sy} (NL)[/SUGGEST]")
            return "\n".join(parts)

        elif scope == "career":
            if stat_info.is_rate:
                formula = _career_pitching_rate_formula(stat_info)
                if not formula:
                    return f"Career {stat_info.display_name} leaders are not available."
                select_expr = f"{formula} as career_val"
            else:
                select_expr = f"SUM(sp.{stat_info.db_column}) as career_val"

            ip_having = "\n            HAVING SUM(sp.ip_outs) >= 486" if stat_info.is_rate else ""
            where_clause = f"\n            WHERE {league_filter[5:]}" if league_filter else ""

            cur = conn.cursor()
            cur.execute(
                f"SELECT p.name, {select_expr} "
                f"FROM season_pitching_stats sp "
                f"JOIN players p ON sp.player_id = p.player_id"
                f"{where_clause} "
                f"GROUP BY p.player_id{ip_having} "
                f"ORDER BY career_val {order_dir} LIMIT ?",
                (limit,),
            )
            rows = cur.fetchall()
            if not rows:
                return f"No career {stat_info.display_name} leaders found{league_label}."

            parts = [f"**Career {stat_info.display_name} Leaders{league_label} (Pitching)**\n"]
            parts.append("[TIP]Tap a player name for their full profile.[/TIP]")
            parts.append("[LEADERBOARD]")
            parts.append(f"HEADER: {stat_info.display_abbrev}")
            for i, row in enumerate(rows):
                parts.append(f"ROW {i+1}. {row[0]}: {_fmt(row[1])}")
            parts.append("[/LEADERBOARD]")
            if stat_info.is_rate:
                parts.append("\n_Min. 162 IP._")
            if league:
                other = "NL" if league == "AL" else "AL"
                parts.append(f"\n[SUGGEST]career {stat_name} leaders ({other})[/SUGGEST]")
                parts.append(f"[SUGGEST]career {stat_name} leaders (MLB)[/SUGGEST]")
            else:
                parts.append(f"\n[SUGGEST]career {stat_name} leaders (AL)[/SUGGEST]")
                parts.append(f"[SUGGEST]career {stat_name} leaders (NL)[/SUGGEST]")
            return "\n".join(parts)

        return None
    finally:
        conn.close()


# Wrap list builders so every successful leaderboard response carries a
# `[LIST_STATE:truncated:N]` trailer. Used by the follow-up rewriter to
# answer "what else?" by re-running with a larger limit. Added here
# instead of inside each function because each has many return paths.
def _append_truncated_marker(default_limit: int = 10):
    import functools
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            result = fn(*args, **kwargs)
            if isinstance(result, str) and "[LEADERBOARD]" in result and "[LIST_STATE:" not in result:
                limit = kwargs.get('limit', default_limit)
                result = result + f"\n[LIST_STATE:truncated:{limit}]"
            return result
        return wrapper
    return decorator


build_leaderboard = _append_truncated_marker(10)(build_leaderboard)


# ===================================================================
# 27. build_threshold
# ===================================================================

def build_threshold(stat_info: StatInfo, threshold: float, comparison: str,
                    season: int, league: Optional[str] = None,
                    is_pitching: bool = False, rookie: bool = False) -> Optional[str]:
    """Build a threshold leaderboard: 'who hit 40 HR?' or 'pitchers with ERA under 3.00'."""
    conn = _get_db()
    try:
        table = "season_pitching_stats" if is_pitching else "season_batting_stats"
        prefix = "sp" if is_pitching else "s"
        league_filter = f" AND {_league_team_clause(league, prefix)}" if league else ""
        league_label = f" ({league})" if league else ""
        rookie_clause = _rookie_filter(prefix, is_pitching) if rookie else ""

        # PA/IP minimum for rate stats
        pa_note = None
        qual_filter = ""
        if stat_info.is_rate:
            if is_pitching:
                ip_min = _qual_min_ip_outs(conn, season)
                qual_filter = f" AND {prefix}.ip_outs >= {ip_min}"
            else:
                pa_min = _qual_min_pa(conn, season)
                qual_filter = f" AND {prefix}.plate_appearances >= {pa_min}"
                pa_note = f"Min. {pa_min} PA."

        cur = conn.cursor()
        cur.execute(
            f"SELECT p.name, {prefix}.{stat_info.db_column} "
            f"FROM {table} {prefix} "
            f"JOIN players p ON {prefix}.player_id = p.player_id "
            f"WHERE {prefix}.season = ? AND {prefix}.{stat_info.db_column} {comparison} ?{qual_filter}{league_filter}{rookie_clause} "
            f"ORDER BY {prefix}.{stat_info.db_column} DESC",
            (season, threshold),
        )
        rows = cur.fetchall()
        if not rows:
            threshold_str = _format_rate(str(threshold)) if stat_info.is_rate else str(int(threshold))
            op = "at least" if comparison == ">=" else "no more than"
            who = "rookie pitchers" if is_pitching and rookie else "rookies" if rookie else "pitchers" if is_pitching else "players"
            return f"No {who} had {op} {threshold_str} {stat_info.display_abbrev} in {season}{league_label}."

        threshold_display = _format_rate(str(threshold)) if stat_info.is_rate else str(int(threshold))
        who = "Rookies" if rookie else "Players"

        if comparison == ">=":
            if stat_info.is_rate:
                title = f"{who} Batting Over {threshold_display} {stat_info.display_abbrev} in {season}{league_label}"
            else:
                title = f"{who} with {threshold_display}+ {stat_info.display_name} in {season}{league_label}"
        else:
            title = f"{who} with {threshold_display} or Fewer {stat_info.display_name} in {season}{league_label}"

        count = len(rows)
        parts = [f"**{title}**"]
        parts.append(f"{count} matched.\n")
        parts.append("[TIP]Tap a player name for their full profile.[/TIP]")
        parts.append("[LEADERBOARD]")
        parts.append(f"HEADER: {stat_info.display_abbrev}")
        for i, row in enumerate(rows):
            val = _format_rate(str(row[1])) if stat_info.is_rate else str(row[1])
            parts.append(f"ROW {i+1}. {row[0]}: {val}")
        parts.append("[/LEADERBOARD]")
        if pa_note:
            parts.append(f"\n_{pa_note}_")

        stat_name = stat_info.pill_name
        parts.append(f"\n[SUGGEST]{season} {stat_name} leaders[/SUGGEST]")
        parts.append(f"[SUGGEST]career {stat_name} leaders[/SUGGEST]")
        if league:
            other = "NL" if league == "AL" else "AL"
            parts.append(f"[SUGGEST]{threshold_display}+ {stat_name} in {season} ({other})[/SUGGEST]")
            parts.append(f"[SUGGEST]{threshold_display}+ {stat_name} in {season} (MLB)[/SUGGEST]")
        else:
            parts.append(f"[SUGGEST]{threshold_display}+ {stat_name} in {season} (AL)[/SUGGEST]")
            parts.append(f"[SUGGEST]{threshold_display}+ {stat_name} in {season} (NL)[/SUGGEST]")

        # Threshold queries have no SQL LIMIT — the list is always the complete
        # set of matching rows. Marker lets the follow-up rewriter answer
        # "what else?" / "who else?" with "that's everyone".
        parts.append("[LIST_STATE:complete]")

        return "\n".join(parts)
    finally:
        conn.close()


# ===================================================================
# 28. build_all_time_threshold
# ===================================================================

def build_all_time_threshold(stat_info: StatInfo, threshold: float, comparison: str,
                             is_pitching: bool = False,
                             league: Optional[str] = None,
                             since_year: Optional[int] = None,
                             rookie: bool = False) -> Optional[str]:
    """All-time threshold: 'who hit 50 home runs?' (no season specified)."""
    conn = _get_db()
    try:
        table = "season_pitching_stats" if is_pitching else "season_batting_stats"
        prefix = "sp" if is_pitching else "s"
        bad_era = _era_data_filter(prefix, stat_info) if is_pitching else ""
        league_filter = f" AND {_league_team_clause(league, prefix)}" if league else ""
        league_label = f" ({league})" if league else ""
        rookie_clause = _rookie_filter(prefix, is_pitching) if rookie else ""

        since_filter = f" AND {prefix}.season >= {since_year}" if since_year else ""

        # PA/IP minimum for rate stats
        qual_filter = ""
        if stat_info.is_rate:
            if is_pitching:
                qual_filter = f" AND {prefix}.ip_outs >= 486"
            else:
                qual_filter = f" AND {prefix}.plate_appearances >= 400"

        cur = conn.cursor()
        cur.execute(
            f"SELECT p.name, {prefix}.{stat_info.db_column}, {prefix}.season "
            f"FROM {table} {prefix} "
            f"JOIN players p ON {prefix}.player_id = p.player_id "
            f"WHERE {prefix}.{stat_info.db_column} {comparison} ?{bad_era}{qual_filter}{league_filter}{since_filter}{rookie_clause} "
            f"ORDER BY {prefix}.{stat_info.db_column} DESC",
            (threshold,),
        )
        rows = cur.fetchall()
        if not rows:
            threshold_str = _format_rate(str(threshold)) if stat_info.is_rate else str(int(threshold))
            op = "at least" if comparison == ">=" else "no more than"
            who = "rookie pitchers" if is_pitching and rookie else "rookies" if rookie else "pitchers" if is_pitching else "players"
            scope_msg = f"since {since_year}" if since_year else "in a season"
            return f"No {who} have had {op} {threshold_str} {stat_info.display_abbrev} {scope_msg}."

        threshold_display = _format_rate(str(threshold)) if stat_info.is_rate else str(int(threshold))
        who = "Rookie Pitchers" if is_pitching and rookie else "Rookies" if rookie else "Pitchers" if is_pitching else "Players"
        scope_label = f"Since {since_year}" if since_year else "All-Time"
        if comparison == ">=":
            if stat_info.is_rate:
                title = f"{who} with {threshold_display}+ {stat_info.display_abbrev} ({scope_label}){league_label}"
            else:
                title = f"{who} with {threshold_display}+ {stat_info.display_name} ({scope_label}){league_label}"
        else:
            title = f"{who} with {threshold_display} or Fewer {stat_info.display_name} ({scope_label}){league_label}"

        count = len(rows)
        parts = [f"**{title}**"]
        parts.append(f"{count} matched.\n")
        parts.append("[TIP]Tap a player name for their full profile.[/TIP]")
        parts.append("[LEADERBOARD]")
        parts.append(f"HEADER: Year, {stat_info.display_abbrev}")
        for i, row in enumerate(rows):
            val = _format_rate(str(row[1])) if stat_info.is_rate else str(row[1])
            parts.append(f"ROW {i+1}. {row[0]}: {row[2]}, {val}")
        parts.append("[/LEADERBOARD]")

        stat_name = stat_info.pill_name
        parts.append(f"\n[SUGGEST]{threshold_display}+ {stat_name} this season[/SUGGEST]")
        parts.append(f"[SUGGEST]{threshold_display}+ {stat_name} last season[/SUGGEST]")
        if league:
            other = "NL" if league == "AL" else "AL"
            parts.append(f"[SUGGEST]{threshold_display}+ {stat_name} all-time ({other})[/SUGGEST]")
            parts.append(f"[SUGGEST]{threshold_display}+ {stat_name} all-time (MLB)[/SUGGEST]")
        else:
            parts.append(f"[SUGGEST]{threshold_display}+ {stat_name} all-time (AL)[/SUGGEST]")
            parts.append(f"[SUGGEST]{threshold_display}+ {stat_name} all-time (NL)[/SUGGEST]")

        return "\n".join(parts)
    finally:
        conn.close()


# ===================================================================
# 29. build_milestone
# ===================================================================

def build_milestone(stat_info: StatInfo, threshold: float,
                    since: Optional[int] = None,
                    is_pitching: bool = False,
                    league: Optional[str] = None) -> Optional[str]:
    """Cross-season milestone: 'how many times has someone hit 50 HR?'."""
    conn = _get_db()
    try:
        table = "season_pitching_stats" if is_pitching else "season_batting_stats"
        since_filter = f" AND s.season >= {since}" if since else ""
        league_filter = f" AND {_league_team_clause(league, 's')}" if league else ""
        league_label = f" ({league})" if league else ""

        lower_is_better = stat_info.db_column in _LOWER_IS_BETTER_COLUMNS
        comparison = "<=" if lower_is_better else ">="

        cur = conn.cursor()
        cur.execute(
            f"SELECT p.name, s.season, s.{stat_info.db_column} "
            f"FROM {table} s "
            f"JOIN players p ON s.player_id = p.player_id "
            f"WHERE s.{stat_info.db_column} {comparison} ?{since_filter}{league_filter} "
            f"ORDER BY s.season DESC, s.{stat_info.db_column} {'ASC' if lower_is_better else 'DESC'}",
            (threshold,),
        )
        rows = cur.fetchall()
        if not rows:
            threshold_display = _format_rate(str(threshold)) if stat_info.is_rate else str(int(threshold))
            since_label = f" since {since}" if since else ""
            return f"No player has reached {threshold_display} {stat_info.display_abbrev}{since_label}{league_label}."

        threshold_display = _format_rate(str(threshold)) if stat_info.is_rate else str(int(threshold))
        since_label = f" since {since}" if since else ""
        verb = "or lower" if lower_is_better else "or more"
        # Title respects comparison direction so "Sub-2.50 ERA Seasons" reads
        # correctly for ERA-style stats. Previously hardcoded "+" which lied
        # for lower-is-better stats — SQL filter was correct, title was not.
        if lower_is_better:
            prefix = "Sub-" if stat_info.is_rate else "≤"
            title = f"{prefix}{threshold_display} {stat_info.display_name} Seasons{since_label}{league_label}"
        else:
            title = f"{threshold_display}+ {stat_info.display_name} Seasons{since_label}{league_label}"

        count = len(rows)
        parts = [f"**{title}**\n"]
        parts.append(f"{count} time{'s' if count != 1 else ''} a player has recorded "
                     f"{threshold_display} {verb} {stat_info.display_abbrev}{since_label}.\n")
        parts.append("[TIP]Tap a player name for their full profile.[/TIP]")
        parts.append("[LEADERBOARD]")
        parts.append(f"HEADER: Year, {stat_info.display_abbrev}")
        for i, row in enumerate(rows):
            val = _format_rate(str(row[2])) if stat_info.is_rate else str(row[2])
            parts.append(f"ROW {i+1}. {row[0]}: {row[1]}, {val}")
        parts.append("[/LEADERBOARD]")

        stat_name = stat_info.pill_name
        parts.append(f"\n[SUGGEST]{stat_name} leaders[/SUGGEST]")
        parts.append(f"[SUGGEST]career {stat_name} leaders[/SUGGEST]")
        if league:
            other = "NL" if league == "AL" else "AL"
            parts.append(f"[SUGGEST]{threshold_display}+ {stat_name} seasons ({other})[/SUGGEST]")
            parts.append(f"[SUGGEST]{threshold_display}+ {stat_name} seasons (MLB)[/SUGGEST]")
        else:
            parts.append(f"[SUGGEST]{threshold_display}+ {stat_name} seasons (AL)[/SUGGEST]")
            parts.append(f"[SUGGEST]{threshold_display}+ {stat_name} seasons (NL)[/SUGGEST]")

        return "\n".join(parts)
    finally:
        conn.close()


# ===================================================================
# 30. build_filtered_leaderboard
# ===================================================================

def build_filtered_leaderboard(rank_stat: StatInfo, filter_stat: StatInfo,
                               threshold: float, comparison: str,
                               season: Optional[int] = None, limit: int = 10,
                               is_pitching: bool = False,
                               league: Optional[str] = None) -> Optional[str]:
    """Filtered leaderboard: 'most HR with .300+ batting average'."""
    conn = _get_db()
    try:
        table = "season_pitching_stats" if is_pitching else "season_batting_stats"
        prefix = "sp" if is_pitching else "s"
        season_filter = f" AND {prefix}.season = {season}" if season else ""
        league_filter = f" AND {_league_team_clause(league, prefix)}" if league else ""
        league_label = f" ({league})" if league else ""
        scope_label = str(season) if season else "All-Time"

        # Sort direction for ranking stat
        if is_pitching and rank_stat.display_abbrev in _LOWER_IS_BETTER_PITCHING:
            order_dir = "ASC"
        else:
            order_dir = "DESC"

        # PA/IP minimum when rate stats involved
        qual_filter = ""
        if not is_pitching and (rank_stat.is_rate or filter_stat.is_rate):
            pa_min = _qual_min_pa(conn, season or datetime.now().year)
            qual_filter = f" AND {prefix}.plate_appearances >= {pa_min}"
        elif is_pitching and (rank_stat.is_rate or filter_stat.is_rate):
            ip_min = _qual_min_ip_outs(conn, season or datetime.now().year)
            qual_filter = f" AND {prefix}.ip_outs >= {ip_min}"

        # Handle innings_pitched specially (TEXT column, use ip_outs for comparison)
        if filter_stat.db_column == "innings_pitched":
            filter_column = f"{prefix}.ip_outs"
            filter_threshold = threshold * 3
            display_column = f"{prefix}.innings_pitched"
        else:
            filter_column = f"{prefix}.{filter_stat.db_column}"
            filter_threshold = threshold
            display_column = f"{prefix}.{filter_stat.db_column}"

        if rank_stat.db_column == "innings_pitched":
            rank_column = f"{prefix}.ip_outs"
            rank_display_column = f"{prefix}.innings_pitched"
        else:
            rank_column = f"{prefix}.{rank_stat.db_column}"
            rank_display_column = f"{prefix}.{rank_stat.db_column}"

        bad_era = _era_data_filter(prefix, rank_stat, [filter_stat]) if is_pitching else ""

        cur = conn.cursor()
        cur.execute(
            f"SELECT p.name, {rank_display_column}, {display_column}, {prefix}.season "
            f"FROM {table} {prefix} "
            f"JOIN players p ON {prefix}.player_id = p.player_id "
            f"WHERE {filter_column} {comparison} ?{season_filter}{qual_filter}{bad_era}{league_filter} "
            f"ORDER BY {rank_column} {order_dir} LIMIT ?",
            (filter_threshold, limit),
        )
        rows = cur.fetchall()
        if not rows:
            threshold_str = _format_rate(str(threshold)) if filter_stat.is_rate else str(int(threshold))
            op = "at least" if comparison == ">=" else "no more than"
            who = "pitchers" if is_pitching else "players"
            return f"No {who} found with {op} {threshold_str} {filter_stat.display_abbrev} ({scope_label})."

        threshold_display = _format_rate(str(threshold)) if filter_stat.is_rate else str(int(threshold))
        filter_label = f"{threshold_display}+" if comparison == ">=" else f"\u2264{threshold_display}"
        title_prefix = "Highest" if rank_stat.is_rate else "Most"
        title = f"{title_prefix} {rank_stat.display_name} with {filter_label} {filter_stat.display_abbrev} ({scope_label}){league_label}"

        show_year = season is None
        parts = [f"**{title}**\n"]
        parts.append("[TIP]Tap a player name for their full profile.[/TIP]")
        parts.append("[LEADERBOARD]")
        header = f"{'Year, ' if show_year else ''}{rank_stat.display_abbrev}, {filter_stat.display_abbrev}"
        parts.append(f"HEADER: {header}")
        for i, row in enumerate(rows):
            rank_formatted = _format_rate(str(row[1])) if rank_stat.is_rate else str(row[1])
            filter_formatted = _format_rate(str(row[2])) if filter_stat.is_rate else str(row[2])
            if show_year:
                parts.append(f"ROW {i+1}. {row[0]}: {row[3]}, {rank_formatted}, {filter_formatted}")
            else:
                parts.append(f"ROW {i+1}. {row[0]}: {rank_formatted}, {filter_formatted}")
        parts.append("[/LEADERBOARD]")

        count = len(rows)
        parts.append(f"\n{count} result{'s' if count != 1 else ''}.")

        rank_name = rank_stat.pill_name
        pill_cap = "Highest" if rank_stat.is_rate else "Most"
        pill_low = "highest" if rank_stat.is_rate else "most"
        if season is not None:
            parts.append(f"\n[SUGGEST]{pill_cap} {rank_name} with {filter_label} {filter_stat.display_abbrev} all-time[/SUGGEST]")
        else:
            current_year = datetime.now().year
            parts.append(f"\n[SUGGEST]{pill_cap} {rank_name} with {filter_label} {filter_stat.display_abbrev} in {current_year}[/SUGGEST]")

        if league:
            other = "NL" if league == "AL" else "AL"
            parts.append(f"[SUGGEST]{pill_low} {rank_name} with {filter_label} {filter_stat.display_abbrev} ({other})[/SUGGEST]")
            parts.append(f"[SUGGEST]{pill_low} {rank_name} with {filter_label} {filter_stat.display_abbrev} (MLB)[/SUGGEST]")
        else:
            parts.append(f"[SUGGEST]{pill_low} {rank_name} with {filter_label} {filter_stat.display_abbrev} (AL)[/SUGGEST]")
            parts.append(f"[SUGGEST]{pill_low} {rank_name} with {filter_label} {filter_stat.display_abbrev} (NL)[/SUGGEST]")

        return "\n".join(parts)
    finally:
        conn.close()


# ===================================================================
# 31. build_superlative
# ===================================================================

def build_superlative(stat_info: StatInfo, threshold: float, superlative: str,
                      is_pitching: bool = False,
                      league: Optional[str] = None,
                      since_year: Optional[int] = None) -> Optional[str]:
    """
    Superlative+threshold: 'youngest to hit 50 HR', 'last player to bat .400'.

    superlative: one of "youngest", "oldest", "first", "last".
    """
    conn = _get_db()
    try:
        table = "season_pitching_stats" if is_pitching else "season_batting_stats"
        prefix = "sp" if is_pitching else "s"
        since_filter = f" AND {prefix}.season >= {since_year}" if since_year else ""
        since_label = f" Since {since_year}" if since_year else ""

        if superlative in ("youngest", "oldest"):
            age_select = f", {prefix}.season - CAST(SUBSTR(p.birthdate, 1, 4) AS INT) AS age_at_season"
            order_by = "age_at_season ASC" if superlative == "youngest" else "age_at_season DESC"
            birthdate_filter = " AND p.birthdate IS NOT NULL"
        else:
            age_select = ""
            order_by = f"{prefix}.season ASC" if superlative == "first" else f"{prefix}.season DESC"
            birthdate_filter = ""

        bad_era = _era_data_filter(prefix, stat_info) if is_pitching else ""
        league_filter = f" AND {_league_team_clause(league, prefix)}" if league else ""
        league_label = f" ({league})" if league else ""

        cur = conn.cursor()
        cur.execute(
            f"SELECT p.name, {prefix}.{stat_info.db_column}, {prefix}.season{age_select} "
            f"FROM {table} {prefix} "
            f"JOIN players p ON {prefix}.player_id = p.player_id "
            f"WHERE {prefix}.{stat_info.db_column} >= ?{birthdate_filter}{bad_era}{league_filter}{since_filter} "
            f"ORDER BY {order_by} LIMIT 10",
            (threshold,),
        )
        rows = cur.fetchall()
        if not rows:
            threshold_str = _format_rate(str(threshold)) if stat_info.is_rate else str(int(threshold))
            who = "pitcher" if is_pitching else "player"
            return f"No {who} has reached {threshold_str} {stat_info.display_abbrev} in a season."

        threshold_display = _format_rate(str(threshold)) if stat_info.is_rate else str(int(threshold))
        superlative_labels = {
            "youngest": "Youngest", "oldest": "Oldest",
            "first": "First", "last": "Most Recent",
        }
        sup_label = superlative_labels.get(superlative, superlative.title())
        who = "Pitchers" if is_pitching else "Players"
        title = f"{sup_label} {who} with {threshold_display}+ {stat_info.display_name}{since_label}{league_label}"

        has_age = superlative in ("youngest", "oldest")
        parts = [f"**{title}**\n"]
        parts.append("[TIP]Tap a player name for their full profile.[/TIP]")
        parts.append("[LEADERBOARD]")
        if has_age:
            # Age first (primary sort), then year, then stat
            parts.append(f"HEADER: Age, Year, {stat_info.display_abbrev}")
        else:
            parts.append(f"HEADER: Year, {stat_info.display_abbrev}")
        for i, row in enumerate(rows):
            val = _format_rate(str(row[1])) if stat_info.is_rate else str(row[1])
            if has_age and len(row) > 3:
                parts.append(f"ROW {i+1}. {row[0]}: {row[3]}, {row[2]}, {val}")
            else:
                parts.append(f"ROW {i+1}. {row[0]}: {row[2]}, {val}")
        parts.append("[/LEADERBOARD]")

        stat_name = stat_info.pill_name
        alt_suggestions = {
            "youngest": [f"Oldest player to hit {threshold_display}+ {stat_name}",
                         f"All players with {threshold_display}+ {stat_name}"],
            "oldest": [f"Youngest player to hit {threshold_display}+ {stat_name}",
                       f"All players with {threshold_display}+ {stat_name}"],
            "first": [f"Most recent player with {threshold_display}+ {stat_name}",
                      f"All players with {threshold_display}+ {stat_name}"],
            "last": [f"First player with {threshold_display}+ {stat_name}",
                     f"All players with {threshold_display}+ {stat_name}"],
        }
        for s in alt_suggestions.get(superlative, []):
            parts.append(f"\n[SUGGEST]{s}[/SUGGEST]")

        if league:
            other = "NL" if league == "AL" else "AL"
            parts.append(f"[SUGGEST]{sup_label.lower()} with {threshold_display}+ {stat_name} ({other})[/SUGGEST]")
            parts.append(f"[SUGGEST]{sup_label.lower()} with {threshold_display}+ {stat_name} (MLB)[/SUGGEST]")
        else:
            parts.append(f"[SUGGEST]{sup_label.lower()} with {threshold_display}+ {stat_name} (AL)[/SUGGEST]")
            parts.append(f"[SUGGEST]{sup_label.lower()} with {threshold_display}+ {stat_name} (NL)[/SUGGEST]")

        return "\n".join(parts)
    finally:
        conn.close()


# ===================================================================
# 32. build_composite_threshold
# ===================================================================

def build_composite_threshold(threshold: int) -> Optional[str]:
    """Composite threshold: '30/30 seasons' (HR and SB)."""
    conn = _get_db()
    try:
        cur = conn.cursor()

        # Player ranking -- who did it the most times
        cur.execute(
            "SELECT p.name, COUNT(*) as times, "
            "GROUP_CONCAT(s.season || ' (' || s.home_runs || '/' || s.stolen_bases || ')', ', ') as seasons "
            "FROM season_batting_stats s "
            "JOIN players p ON s.player_id = p.player_id "
            "WHERE s.home_runs >= ? AND s.stolen_bases >= ? "
            "GROUP BY p.player_id "
            "ORDER BY times DESC, MAX(s.home_runs + s.stolen_bases) DESC",
            (threshold, threshold),
        )
        rank_rows = cur.fetchall()
        if not rank_rows:
            return f"No player has ever achieved a {threshold}/{threshold} season (HR and SB)."

        # All individual seasons
        cur.execute(
            "SELECT p.name, s.season, s.home_runs, s.stolen_bases "
            "FROM season_batting_stats s "
            "JOIN players p ON s.player_id = p.player_id "
            "WHERE s.home_runs >= ? AND s.stolen_bases >= ? "
            "ORDER BY s.season DESC, s.home_runs + s.stolen_bases DESC",
            (threshold, threshold),
        )
        all_rows = cur.fetchall()

        total_seasons = len(all_rows)
        total_players = len(rank_rows)
        parts = [f"**{threshold}/{threshold} Seasons (HR & SB)**\n"]
        parts.append(f"{total_seasons} time{'s' if total_seasons != 1 else ''} by "
                     f"{total_players} player{'s' if total_players != 1 else ''}.\n")
        parts.append("[TIP]Tap a player name for their full profile.[/TIP]")

        parts.append("[LEADERBOARD]")
        parts.append("HEADER: Times, Seasons")
        for i, row in enumerate(rank_rows):
            parts.append(f"ROW {i+1}. {row[0]}: {row[1]}, {row[2]}")
        parts.append("[/LEADERBOARD]")

        parts.append(f"\n**All {threshold}/{threshold} Seasons**\n")
        parts.append("[LEADERBOARD]")
        parts.append("HEADER: Year, HR, SB")
        for i, row in enumerate(all_rows):
            parts.append(f"ROW {i+1}. {row[0]}: {row[1]}, {row[2]}, {row[3]}")
        parts.append("[/LEADERBOARD]")

        return "\n".join(parts)
    finally:
        conn.close()


# ===================================================================
# 33. build_triple_crown
# ===================================================================

def build_triple_crown() -> Optional[str]:
    """Triple Crown winners (led league in AVG, HR, and RBI same season)."""
    conn = _get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            "WITH avg_leaders AS ("
            "  SELECT s.season, p.name, s.batting_avg, "
            "    ROW_NUMBER() OVER (PARTITION BY s.season ORDER BY s.batting_avg DESC) as rn "
            "  FROM season_batting_stats s "
            "  JOIN players p ON s.player_id = p.player_id "
            "  WHERE s.at_bats >= 400"
            "), "
            "hr_leaders AS ("
            "  SELECT s.season, p.name, s.home_runs, "
            "    ROW_NUMBER() OVER (PARTITION BY s.season ORDER BY s.home_runs DESC) as rn "
            "  FROM season_batting_stats s "
            "  JOIN players p ON s.player_id = p.player_id"
            "), "
            "rbi_leaders AS ("
            "  SELECT s.season, p.name, s.rbi, "
            "    ROW_NUMBER() OVER (PARTITION BY s.season ORDER BY s.rbi DESC) as rn "
            "  FROM season_batting_stats s "
            "  JOIN players p ON s.player_id = p.player_id"
            ") "
            "SELECT a.season, a.name, a.batting_avg, h.home_runs, r.rbi "
            "FROM avg_leaders a "
            "JOIN hr_leaders h ON a.season = h.season AND a.name = h.name AND h.rn = 1 "
            "JOIN rbi_leaders r ON a.season = r.season AND a.name = r.name AND r.rn = 1 "
            "WHERE a.rn = 1 "
            "ORDER BY a.season DESC"
        )
        rows = cur.fetchall()
        if not rows:
            return "No Triple Crown winners found in the database."

        count = len(rows)
        parts = ["**Triple Crown Winners**\n"]
        parts.append(
            "The Triple Crown is awarded when a player leads their league (or all of MLB) "
            "in batting average, home runs, and RBI in the same season. It has happened "
            f"{count} time{'s' if count != 1 else ''} in our records.\n"
        )
        parts.append("[TIP]Tap a player name for their full profile.[/TIP]")
        parts.append("[LEADERBOARD]")
        parts.append("HEADER: Year, AVG, HR, RBI")
        for i, row in enumerate(rows):
            avg = _format_rate(str(row[2]))
            parts.append(f"ROW {i+1}. {row[1]}: {row[0]}, {avg}, {row[3]}, {row[4]}")
        parts.append("[/LEADERBOARD]")
        parts.append("\n_Note: Based on overall MLB leaders with min. 400 AB. "
                     "Historical league-specific Triple Crowns may differ._")

        return "\n".join(parts)
    finally:
        conn.close()


# ===================================================================
# 34. build_consecutive_streak
# ===================================================================

def build_consecutive_streak(streak_type: str, player_name: Optional[str] = None,
                             season: Optional[int] = None) -> Optional[str]:
    """
    Consecutive game streak: hitting streak or on-base streak.

    streak_type: "hit" or "onbase".
    season: explicit year to filter, or None for all-time (scans full history).

    Caller is responsible for the default-to-current-year safety net — passing
    None here means "user asked for all-time" and we honor it.
    """
    conn = _get_db()
    try:
        if streak_type == "hit":
            hit_condition = "hits > 0"
            streak_label = "Hitting"
        else:
            hit_condition = "(hits + walks + COALESCE(hit_by_pitch, 0)) > 0"
            streak_label = "On-Base"

        # Player-specific path can use a name pre-filter; for all-player
        # leaderboards we drop the players JOIN inside the CTE entirely
        # (was hauling player name into every row of a 4.8M-row scan for
        # no reason — JOIN now happens once on the final 15 rows).
        # Window functions partition by (player_id, season) so each window
        # spans at most one season's worth of games — keeps the all-time
        # historical scan inside the gunicorn worker timeout (~25s instead
        # of OOM-territory).
        season_filter = f"AND g.season = {season}" if season else ""
        params: list = []
        if player_name:
            # Pre-resolve player_id so we can avoid the JOIN inside the CTE
            row = conn.execute(
                "SELECT player_id FROM players WHERE name = ? LIMIT 1",
                (_sanitize(player_name),),
            ).fetchone()
            if not row:
                return f"No {streak_label.lower()} streak data found for {player_name}."
            player_filter = "AND g.player_id = ?"
            params.append(row[0])
        else:
            player_filter = ""

        cur = conn.cursor()
        cur.execute(
            f"WITH numbered AS ("
            f"  SELECT g.player_id, g.date, g.hits, g.walks, "
            f"    COALESCE(g.hit_by_pitch, 0) as hbp, g.season, "
            f"    ROW_NUMBER() OVER (PARTITION BY g.player_id, g.season ORDER BY g.date) as game_num "
            f"  FROM game_batting_logs g "
            f"  WHERE 1=1 {player_filter} {season_filter}"
            f"), "
            f"qualifying AS ("
            f"  SELECT *, ROW_NUMBER() OVER (PARTITION BY player_id, season ORDER BY date) as qual_num "
            f"  FROM numbered "
            f"  WHERE {hit_condition}"
            f"), "
            f"streaks AS ("
            f"  SELECT player_id, season, "
            f"    COUNT(*) as streak_len, "
            f"    MIN(date) as start_date, "
            f"    MAX(date) as end_date "
            f"  FROM qualifying "
            f"  GROUP BY player_id, season, game_num - qual_num"
            f") "
            f"SELECT p.name, s.streak_len, s.season, s.start_date, s.end_date "
            f"FROM streaks s "
            f"JOIN players p ON s.player_id = p.player_id "
            f"ORDER BY s.streak_len DESC LIMIT 15",
            tuple(params),
        )
        rows = cur.fetchall()
        if not rows:
            scope = player_name or "any player"
            return f"No {streak_label.lower()} streak data found for {scope}."

        # Scope label
        if player_name:
            display_name, _ = _get_player_info(conn, player_name)
            if season:
                scope_label = f"{display_name} \u2014 {season}"
            else:
                scope_label = display_name
        else:
            if season:
                scope_label = str(season)
            else:
                scope_label = "All-Time"

        parts = [f"**Longest {streak_label} Streaks \u2014 {scope_label}**\n"]
        parts.append("[TIP]Tap a player name for their full profile.[/TIP]")
        parts.append("[LEADERBOARD]")
        parts.append("HEADER: Games")
        for i, row in enumerate(rows):
            parts.append(f"ROW {i+1}. {row[0]}: {row[1]}")
        parts.append("[/LEADERBOARD]")

        return "\n".join(parts)
    finally:
        conn.close()


# ===================================================================
# 35. build_team_stats
# ===================================================================

def build_team_stats(team_code: str, stat_info: Optional[StatInfo] = None,
                     season: int = 0) -> Optional[str]:
    """Team batting leaderboard or overview."""
    conn = _get_db()
    try:
        full_name = _team_full_name(team_code)
        nickname = _team_nickname(team_code)
        team_filter = (f"(s.team = ? OR s.team LIKE ? OR s.team LIKE ?)")
        team_params = [team_code, f"{team_code}/%", f"%/{team_code}"]

        if stat_info:
            pa_min = 50 if stat_info.is_rate else None
            pa_filter = f" AND s.plate_appearances >= {pa_min}" if pa_min else ""

            cur = conn.cursor()
            cur.execute(
                f"SELECT p.name, s.{stat_info.db_column} "
                f"FROM season_batting_stats s "
                f"JOIN players p ON s.player_id = p.player_id "
                f"WHERE {team_filter} AND s.season = ?{pa_filter} "
                f"ORDER BY s.{stat_info.db_column} DESC LIMIT 15",
                tuple(team_params + [season]),
            )
            rows = cur.fetchall()
            if not rows:
                return f"No {stat_info.display_name} data found for the {full_name} in {season}."

            parts = [f"**{full_name}** \u2014 {season} {stat_info.display_name} Leaders\n"]
            parts.append("[TIP]Tap a player name for their full profile.[/TIP]")
            parts.append("[LEADERBOARD]")
            parts.append(f"HEADER: {stat_info.display_abbrev}")
            for i, row in enumerate(rows):
                val = _format_rate(str(row[1])) if stat_info.is_rate else str(row[1])
                parts.append(f"ROW {i+1}. {row[0]}: {val}")
            parts.append("[/LEADERBOARD]")
            if pa_min:
                parts.append(f"\n_Min. {pa_min} PA._")
            stat_name = stat_info.pill_name
            parts.append(f"\n[SUGGEST]{season} {stat_name} leaders[/SUGGEST]")
            parts.append(f"[SUGGEST]{nickname} hitters[/SUGGEST]")
            return "\n".join(parts)
        else:
            # Team overview sorted by OPS — no PA minimum for roster view
            cur = conn.cursor()
            cur.execute(
                "SELECT p.name, s.games, s.batting_avg, s.home_runs, s.rbi, s.ops "
                "FROM season_batting_stats s "
                "JOIN players p ON s.player_id = p.player_id "
                f"WHERE {team_filter} AND s.season = ? AND s.plate_appearances >= 1 "
                "ORDER BY s.plate_appearances DESC LIMIT 15",
                tuple(team_params + [season]),
            )
            rows = cur.fetchall()
            if not rows:
                return f"No hitting data found for the {full_name} in {season}."

            parts = [f"**{full_name}** \u2014 {season} Hitters\n"]
            parts.append("[TIP]Tap a player name for their full profile.[/TIP]")
            parts.append("[LEADERBOARD]")
            parts.append("HEADER: AVG, HR, RBI, OPS")
            for i, row in enumerate(rows):
                avg = _format_rate(str(row[2]))
                ops = _format_rate(str(row[5]))
                parts.append(f"ROW {i+1}. {row[0]}: {avg}, {row[3]}, {row[4]}, {ops}")
            parts.append("[/LEADERBOARD]")
            parts.append(f"\n[SUGGEST]{nickname} home runs[/SUGGEST]")
            parts.append(f"[SUGGEST]{nickname} batting average[/SUGGEST]")
            return "\n".join(parts)
    finally:
        conn.close()


# ===================================================================
# 36. build_pitching_team_stats
# ===================================================================

def build_pitching_team_stats(team_code: str, stat_info: Optional[StatInfo] = None,
                              season: int = 0) -> Optional[str]:
    """Team pitching leaderboard or overview."""
    conn = _get_db()
    try:
        full_name = _team_full_name(team_code)
        nickname = _team_nickname(team_code)
        team_filter = "(sp.team = ? OR sp.team LIKE ? OR sp.team LIKE ?)"
        team_params = [team_code, f"{team_code}/%", f"%/{team_code}"]

        if stat_info:
            # Prorate IP minimum for early season
            ip_min_p = _qual_min_ip_outs(conn, season)
            ip_filter = f" AND sp.ip_outs >= {ip_min_p}" if stat_info.is_rate else ""
            order_dir = "ASC" if stat_info.display_abbrev in _LOWER_IS_BETTER_PITCHING else "DESC"

            cur = conn.cursor()
            cur.execute(
                f"SELECT p.name, sp.{stat_info.db_column} "
                f"FROM season_pitching_stats sp "
                f"JOIN players p ON sp.player_id = p.player_id "
                f"WHERE {team_filter} AND sp.season = ?{ip_filter} "
                f"ORDER BY sp.{stat_info.db_column} {order_dir} LIMIT 15",
                tuple(team_params + [season]),
            )
            rows = cur.fetchall()
            if not rows:
                return f"No {stat_info.display_name} data found for the {full_name} in {season}."

            parts = [f"**{full_name}** \u2014 {season} {stat_info.display_name} Leaders (Pitching)\n"]
            parts.append("[TIP]Tap a player name for their full profile.[/TIP]")
            parts.append("[LEADERBOARD]")
            parts.append(f"HEADER: {stat_info.display_abbrev}")
            for i, row in enumerate(rows):
                val = _format_pitching_rate(row[1], 2) if stat_info.is_rate else str(row[1])
                parts.append(f"ROW {i+1}. {row[0]}: {val}")
            parts.append("[/LEADERBOARD]")
            if stat_info.is_rate:
                parts.append("\n_Min. 18 IP._")
            stat_name = stat_info.pill_name
            parts.append(f"\n[SUGGEST]{season} {stat_name} leaders[/SUGGEST]")
            parts.append(f"[SUGGEST]{nickname} pitchers[/SUGGEST]")
            return "\n".join(parts)
        else:
            # Team pitching overview — no IP minimum for roster view
            cur = conn.cursor()
            cur.execute(
                "SELECT p.name, sp.era, sp.strikeouts, sp.ip_outs "
                "FROM season_pitching_stats sp "
                "JOIN players p ON sp.player_id = p.player_id "
                f"WHERE {team_filter} AND sp.season = ? AND sp.ip_outs >= 1 "
                "ORDER BY sp.ip_outs DESC LIMIT 15",
                tuple(team_params + [season]),
            )
            rows = cur.fetchall()
            if not rows:
                return None

            parts = [f"**{full_name}** \u2014 {season} Pitchers\n"]
            parts.append("[TIP]Tap a player name for their full profile.[/TIP]")
            parts.append("[LEADERBOARD]")
            parts.append("HEADER: IP, ERA, K")
            for i, row in enumerate(rows):
                era = _format_pitching_rate(row[1], 2)
                ip_outs = row[3] if len(row) > 3 else 0
                ip = f"{ip_outs // 3}.{ip_outs % 3}" if ip_outs else "0.0"
                parts.append(f"ROW {i+1}. {row[0]}: {ip}, {era}, {row[2]}")
            parts.append("[/LEADERBOARD]")
            parts.append(f"\n[SUGGEST]{nickname} ERA leaders[/SUGGEST]")
            parts.append(f"[SUGGEST]{nickname} strikeout leaders[/SUGGEST]")
            return "\n".join(parts)
    finally:
        conn.close()


# ===================================================================
# 37. build_team_total
# ===================================================================

def build_team_record(team_code: str, season: int) -> Optional[str]:
    """Team season W-L from team_game_results.

    Examples returned:
      "The Yankees are 13-9 in 2026 (.591), tied for 8th in MLB."
      "The Brewers finished 97-65 in 2025 (.599)."
    """
    conn = _get_db()
    try:
        full_name = _team_full_name(team_code)
        nickname = _team_nickname(team_code)
        cur = conn.cursor()

        # Latest row per team gives final record (or in-season current).
        # Restrict to regular season — playoff games would inflate the W-L
        # totals (and "Yankees record" almost always means regular season).
        cur.execute("""
            SELECT MAX(wins_after), MAX(losses_after), COUNT(*),
                   MAX(date) as last_game_date
            FROM team_game_results
            WHERE team = ? AND season = ?
              AND COALESCE(gametype, 'regular') = 'regular'
        """, (team_code, season))
        row = cur.fetchone()
        if not row or row[0] is None:
            return f"No game results recorded for the {full_name} in {season}."
        wins, losses, games_played, last_game_date = row
        pct_val = wins / max(wins + losses, 1)
        pct_str = f".{int(round(pct_val * 1000)):03d}"

        # Determine if season is over (compare to current calendar year)
        from datetime import date as _date
        current_year = _date.today().year
        is_in_season = season == current_year
        verb = "are" if is_in_season else "finished"

        # MLB rank for context (in-season only — final standings already
        # convey that through game count)
        rank_clause = ""
        if is_in_season:
            cur.execute("""
                SELECT team, MAX(wins_after) AS w, MAX(losses_after) AS l
                FROM team_game_results
                WHERE season = ?
                  AND COALESCE(gametype, 'regular') = 'regular'
                GROUP BY team
                ORDER BY w * 1.0 / NULLIF(w + l, 0) DESC
            """, (season,))
            standings = cur.fetchall()
            for i, (t, w, l) in enumerate(standings, 1):
                if t == team_code:
                    suffix = "st" if i % 10 == 1 and i % 100 != 11 else \
                             "nd" if i % 10 == 2 and i % 100 != 12 else \
                             "rd" if i % 10 == 3 and i % 100 != 13 else "th"
                    rank_clause = f", {i}{suffix} in MLB"
                    break

        return (f"The **{full_name}** {verb} **{wins}-{losses}** in {season} "
                f"({pct_str}){rank_clause}.\n\n"
                f"[SUGGEST]{nickname} home runs leaders[/SUGGEST]\n"
                f"[SUGGEST]{nickname} batting leaders[/SUGGEST]\n"
                f"[SUGGEST]{nickname} ERA leaders[/SUGGEST]")
    finally:
        conn.close()


def build_team_game_score(team_code: str, opponent_code: Optional[str],
                          date_keyword: str) -> Optional[str]:
    """Single-game score lookup from team_game_results.

    date_keyword: 'yesterday', 'last night', 'tonight', 'today',
    'last_game' (most recent regardless of date), or a YYYY-MM-DD string.
    """
    conn = _get_db()
    try:
        full_name = _team_full_name(team_code)
        nickname = _team_nickname(team_code)
        cur = conn.cursor()

        # Resolve target date — use Eastern time since baseball "yesterday"
        # is the previous calendar day in ET, regardless of server timezone.
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz
        et_now = _dt.now(_tz(_td(hours=-4)))  # EDT during baseball season
        today = et_now.date()
        if date_keyword in ("yesterday", "last night"):
            target_date = (today - _td(days=1)).isoformat()
        elif date_keyword in ("tonight", "today"):
            target_date = today.isoformat()
        elif date_keyword == "last_game":
            target_date = None  # find most recent
        elif len(date_keyword) == 10 and date_keyword[4] == "-":
            target_date = date_keyword
        else:
            target_date = None

        opponent_filter = ""
        params = [team_code]
        if opponent_code:
            opponent_filter = " AND opponent = ?"
            params.append(opponent_code)

        if target_date:
            params.append(target_date)
            cur.execute(f"""
                SELECT date, team, opponent, is_home, team_runs, opp_runs,
                       result, innings, game_number
                FROM team_game_results
                WHERE team = ?{opponent_filter} AND date = ?
                ORDER BY game_number ASC
            """, tuple(params))
        else:
            cur.execute(f"""
                SELECT date, team, opponent, is_home, team_runs, opp_runs,
                       result, innings, game_number
                FROM team_game_results
                WHERE team = ?{opponent_filter}
                ORDER BY date DESC, game_number DESC
                LIMIT 1
            """, tuple(params))
        rows = cur.fetchall()

        if not rows:
            opp_phrase = f" vs the {_team_full_name(opponent_code)}" if opponent_code else ""
            if target_date:
                return f"No {full_name} game{opp_phrase} found on {target_date}."
            return f"No recent {full_name} game found{opp_phrase}."

        sentences = []
        for date_s, team, opp, is_home, t_runs, o_runs, result, innings, gnum in rows:
            opp_full = _team_full_name(opp)
            home_away = "at home" if is_home else "on the road"
            extras = "" if (innings or 9) <= 9 else f" in {innings} innings"
            verb = {"W": "beat", "L": "lost to", "T": "tied"}.get(result, "played")
            score = f"{t_runs}-{o_runs}" if result != "L" else f"{o_runs}-{t_runs}"
            game_label = ""
            if gnum and any(r[8] for r in rows):  # doubleheader
                game_label = f" (game {gnum + 1})"
            sentences.append(
                f"The **{full_name}** {verb} the **{opp_full}** "
                f"**{score}**{game_label} {home_away} on {date_s}{extras}."
            )

        body = " ".join(sentences)
        return (f"{body}\n\n"
                f"[SUGGEST]{nickname} record[/SUGGEST]\n"
                f"[SUGGEST]{nickname} home runs leaders[/SUGGEST]")
    finally:
        conn.close()


def build_team_total(team_code: str, stat_info: StatInfo, season: int) -> Optional[str]:
    """Team aggregate total: 'The Yankees hit 234 home runs in 2024.'"""
    conn = _get_db()
    try:
        full_name = _team_full_name(team_code)
        nickname = _team_nickname(team_code)
        team_filter = "(s.team = ? OR s.team LIKE ? OR s.team LIKE ?)"
        team_params = [team_code, f"{team_code}/%", f"%/{team_code}"]

        cur = conn.cursor()

        if stat_info.is_rate:
            # Rate stats: compute from raw components
            rate_queries = {
                "batting_avg": (
                    "SELECT CAST(SUM(s.hits) AS REAL) / SUM(s.at_bats)",
                    "batting average",
                ),
                "obp": (
                    "SELECT CAST(SUM(s.hits + s.walks + s.hit_by_pitch) AS REAL) / "
                    "SUM(s.at_bats + s.walks + s.hit_by_pitch + s.sacrifice_flies)",
                    "on-base percentage",
                ),
                "slg": (
                    "SELECT CAST(SUM(s.hits - s.doubles - s.triples - s.home_runs "
                    "+ 2*s.doubles + 3*s.triples + 4*s.home_runs) AS REAL) / SUM(s.at_bats)",
                    "slugging percentage",
                ),
                "ops": (
                    "SELECT CAST(SUM(s.hits + s.walks + s.hit_by_pitch) AS REAL) / "
                    "SUM(s.at_bats + s.walks + s.hit_by_pitch + s.sacrifice_flies) + "
                    "CAST(SUM(s.hits - s.doubles - s.triples - s.home_runs "
                    "+ 2*s.doubles + 3*s.triples + 4*s.home_runs) AS REAL) / SUM(s.at_bats)",
                    "OPS",
                ),
            }

            if stat_info.db_column in rate_queries:
                select_expr, label = rate_queries[stat_info.db_column]
            else:
                select_expr = (f"SELECT SUM(s.{stat_info.db_column} * s.plate_appearances) / "
                               "SUM(s.plate_appearances)")
                label = stat_info.display_name.lower()

            cur.execute(
                f"{select_expr} FROM season_batting_stats s "
                f"WHERE {team_filter} AND s.season = ? AND s.plate_appearances >= 1",
                tuple(team_params + [season]),
            )
            row = cur.fetchone()
            if not row or row[0] is None:
                return f"No {stat_info.display_name} data found for the {full_name} in {season}."

            formatted = _format_rate(str(row[0]))
            return (f"The **{full_name}** had a team {label} of **{formatted}** in {season}.\n\n"
                    f"[SUGGEST]{nickname} {stat_info.pill_name} leaders[/SUGGEST]\n"
                    f"[SUGGEST]{nickname} hitters[/SUGGEST]")
        else:
            # Counting stats: SUM
            cur.execute(
                f"SELECT SUM(s.{stat_info.db_column}) "
                f"FROM season_batting_stats s "
                f"WHERE {team_filter} AND s.season = ?",
                tuple(team_params + [season]),
            )
            row = cur.fetchone()
            if not row or row[0] is None:
                return f"No {stat_info.display_name} data found for the {full_name} in {season}."

            total = int(row[0])
            phrase_map = {
                "home_runs": f"hit **{total} home runs**",
                "hits": f"hit **{total} hits**",
                "doubles": f"hit **{total} doubles**",
                "triples": f"hit **{total} triples**",
                "rbi": f"drove in **{total} runs**",
                "runs": f"scored **{total} runs**",
                "stolen_bases": f"stole **{total} bases**",
                "walks": f"drew **{total} walks**",
                "strikeouts": f"struck out **{total} times**",
            }
            phrase = phrase_map.get(stat_info.db_column,
                                   f"had **{total} {stat_info.display_name.lower()}**")

            return (f"The **{full_name}** {phrase} in {season}.\n\n"
                    f"[SUGGEST]{nickname} {stat_info.pill_name} leaders[/SUGGEST]\n"
                    f"[SUGGEST]{nickname} hitters[/SUGGEST]")
    finally:
        conn.close()


# ===================================================================
# 38. build_team_ranking
# ===================================================================

def build_team_ranking(stat_info: StatInfo, season: int) -> Optional[str]:
    """Top 10 teams by a stat."""
    from services.name_matcher import is_pitching_stat
    conn = _get_db()
    try:
        cur = conn.cursor()
        is_pitching = is_pitching_stat(stat_info)
        table = "season_pitching_stats" if is_pitching else "season_batting_stats"
        pa_col = "ip_outs" if is_pitching else "plate_appearances"

        # Lower-is-better stats
        lower_better = stat_info.db_column in ("era", "whip", "bb_per_9", "hr_per_9")
        order = "ASC" if lower_better else "DESC"

        if stat_info.is_rate:
            pitching_rate_exprs = {
                "era": "SUM(s.earned_runs) * 9.0 / (SUM(s.ip_outs) / 3.0)",
                "whip": "CAST(SUM(s.hits) + SUM(s.walks) AS REAL) / (SUM(s.ip_outs) / 3.0)",
                "k_per_9": "SUM(s.strikeouts) * 9.0 / (SUM(s.ip_outs) / 3.0)",
                "bb_per_9": "SUM(s.walks) * 9.0 / (SUM(s.ip_outs) / 3.0)",
            }
            batting_rate_exprs = {
                "batting_avg": "CAST(SUM(s.hits) AS REAL) / SUM(s.at_bats)",
                "obp": ("CAST(SUM(s.hits + s.walks + s.hit_by_pitch) AS REAL) / "
                        "SUM(s.at_bats + s.walks + s.hit_by_pitch + s.sacrifice_flies)"),
                "slg": ("CAST(SUM(s.hits - s.doubles - s.triples - s.home_runs "
                         "+ 2*s.doubles + 3*s.triples + 4*s.home_runs) AS REAL) / SUM(s.at_bats)"),
                "ops": ("CAST(SUM(s.hits + s.walks + s.hit_by_pitch) AS REAL) / "
                        "SUM(s.at_bats + s.walks + s.hit_by_pitch + s.sacrifice_flies) + "
                        "CAST(SUM(s.hits - s.doubles - s.triples - s.home_runs "
                        "+ 2*s.doubles + 3*s.triples + 4*s.home_runs) AS REAL) / SUM(s.at_bats)"),
            }
            rate_exprs = pitching_rate_exprs if is_pitching else batting_rate_exprs
            select_expr = rate_exprs.get(
                stat_info.db_column,
                f"SUM(s.{stat_info.db_column} * s.{pa_col}) / SUM(s.{pa_col})"
            )
            min_pa = 300 if is_pitching else 100
            cur.execute(
                f"SELECT s.team, {select_expr} AS team_stat "
                f"FROM {table} s "
                f"WHERE s.season = ? AND s.{pa_col} >= 1 "
                f"GROUP BY s.team HAVING SUM(s.{pa_col}) >= {min_pa} "
                f"ORDER BY team_stat {order} LIMIT 30",
                (season,),
            )
        else:
            cur.execute(
                f"SELECT s.team, SUM(s.{stat_info.db_column}) AS team_stat "
                f"FROM {table} s "
                f"WHERE s.season = ? "
                f"GROUP BY s.team ORDER BY team_stat {order} LIMIT 30",
                (season,),
            )

        rows = cur.fetchall()
        if not rows:
            return f"No team {stat_info.display_name} data found for {season}."

        parts = [f"**{season} Team {stat_info.display_name} Rankings**\n"]
        parts.append("[LEADERBOARD]")
        parts.append(f"HEADER: {stat_info.display_abbrev}")
        for i, row in enumerate(rows):
            team_name = _team_full_name(str(row[0]))
            raw = row[1]
            if stat_info.db_column in ("era", "whip", "k_per_9", "bb_per_9", "hr_per_9", "k_per_bb"):
                val = f"{raw:.2f}"
            elif stat_info.is_rate:
                val = _format_rate(str(raw))
            else:
                val = str(int(raw)) if raw == int(raw) else str(raw)
            parts.append(f"ROW {i+1}. {team_name}: {val}")
        parts.append("[/LEADERBOARD]")

        stat_name = stat_info.pill_name
        parts.append(f"\n[SUGGEST]{season} {stat_name} leaders[/SUGGEST]")

        return "\n".join(parts)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Season count — "how many seasons has Judge hit a triple?"
# ---------------------------------------------------------------------------

def build_season_count(name: str, db_column: str, stat_abbrev: str,
                       stat_name: str, threshold, is_rate: bool,
                       is_pitching: bool) -> Optional[str]:
    """Count seasons where a player reached a stat threshold."""
    conn = sqlite3.connect(DB_PATH)
    try:
        table = "season_pitching_stats" if is_pitching else "season_batting_stats"
        comp = ">=" if not is_rate else ">="
        rows = conn.execute(
            f"""
            SELECT s.season, s.{db_column}
            FROM {table} s
            JOIN players p ON s.player_id = p.player_id
            WHERE p.name = ? AND s.{db_column} {comp} ?
            ORDER BY s.season
            """,
            (name, threshold),
        ).fetchall()

        total_seasons = conn.execute(
            f"""
            SELECT COUNT(DISTINCT s.season)
            FROM {table} s
            JOIN players p ON s.player_id = p.player_id
            WHERE p.name = ?
            """,
            (name,),
        ).fetchone()[0]

        # Singularize stat name for "at least one" phrasing
        singular_name = stat_name.lower()
        if singular_name.endswith("s") and singular_name not in ("walks", "saves", "losses"):
            singular_name = singular_name[:-1]  # "triples" → "triple", "home runs" → "home run"

        if not rows:
            if threshold == 1:
                return f"{name} has never recorded a {singular_name} in any season in our database."
            else:
                fmt = _format_threshold_value(threshold, stat_abbrev)
                return f"{name} has never had {fmt} {stat_name.lower()} in a season."

        count = len(rows)
        seasons_list = [str(r[0]) for r in rows]

        # Format the answer
        if threshold == 1:
            desc = f"recorded at least one {singular_name}"
        else:
            fmt = _format_threshold_value(threshold, stat_abbrev)
            desc = f"had {fmt}+ {stat_name.lower()}"

        parts = [f"{name} has {desc} in **{count}** of {total_seasons} career seasons"]
        if count <= 10:
            parts[0] += f": {', '.join(seasons_list)}."
        else:
            parts[0] += "."

        # Show the values per season as a compact leaderboard if reasonable count
        if 2 <= count <= 20:
            parts.append("")
            parts.append("[LEADERBOARD]")
            parts.append(f"HEADER: {stat_abbrev}")
            for season, val in rows:
                formatted = _format_stat_value(val, stat_abbrev)
                parts.append(f"ROW {season}: {formatted}")
            parts.append("[/LEADERBOARD]")

        parts.append(f"\n[SUGGEST]{name} career[/SUGGEST]")

        return "\n".join(parts)
    finally:
        conn.close()


def _format_threshold_value(threshold, stat_abbrev: str) -> str:
    """Format a threshold value for display."""
    if isinstance(threshold, float) and threshold != int(threshold):
        return f"{threshold:.3f}".lstrip("0") if stat_abbrev in _RATE_STATS else f"{threshold}"
    return str(int(threshold))


def _format_stat_value(val, stat_abbrev: str) -> str:
    """Format a single stat value for display."""
    if stat_abbrev in _RATE_STATS:
        return f"{val:.3f}".lstrip("0") if isinstance(val, float) else str(val)
    if stat_abbrev in _TWO_DEC_STATS:
        return f"{val:.2f}" if isinstance(val, float) else str(val)
    if stat_abbrev in _ONE_DEC_STATS:
        return f"{val:.1f}" if isinstance(val, float) else str(val)
    return str(int(val)) if isinstance(val, (int, float)) else str(val)


# ===================================================================
# 30a. build_player_game_window — first/last N games of a player's season(s)
# ===================================================================


def build_player_game_logs(name: str, season: int) -> Optional[str]:
    """Full game log table for a player's season."""
    conn = _get_db()
    try:
        display_name, team = _get_player_info(conn, name)
        row = conn.execute("SELECT player_id FROM players WHERE name = ? LIMIT 1",
                           (_sanitize(name),)).fetchone()
        if not row:
            return None
        pid = row[0]

        # Check if pitcher
        is_pitcher = False
        try:
            from services.name_matcher import is_pitcher as _is_pitcher
            is_pitcher = _is_pitcher(name)
        except Exception:
            pass

        if is_pitcher:
            cur = conn.execute("""
                SELECT g.date, g.opponent, g.innings_pitched, g.hits, g.earned_runs,
                       g.strikeouts, g.walks, g.home_runs, g.win, g.loss, g.save
                FROM game_pitching_logs g
                WHERE g.player_id = ? AND g.season = ?
                ORDER BY g.date DESC
            """, (pid, season))
            rows = cur.fetchall()
            if not rows:
                return None

            parts = [f"**{display_name} — {season} Game Logs**\n"]
            parts.append("[TIP]Tap a player name for their full profile.[/TIP]")
            parts.append("[GAMELOGS]")
            for row in rows:
                dt, opp, ip, h, er, k, bb, hr, w, l, sv = row
                dec = "W" if w else ("L" if l else ("SV" if sv else ""))
                dec_str = f", {dec}" if dec else ""
                parts.append(f"GAME {dt}|{ip or 0} IP, {h or 0} H, {er or 0} ER, {k or 0} K, {bb or 0} BB{dec_str}")
            parts.append("[/GAMELOGS]")
        else:
            cur = conn.execute("""
                SELECT g.date, g.hits, g.at_bats, g.home_runs,
                       g.rbi, g.runs, g.walks, g.strikeouts, g.stolen_bases
                FROM game_batting_logs g
                WHERE g.player_id = ? AND g.season = ?
                ORDER BY g.date DESC
            """, (pid, season))
            rows = cur.fetchall()
            if not rows:
                return None

            parts = [f"**{display_name} — {season} Game Logs**\n"]
            parts.append("[TIP]Tap a player name for their full profile.[/TIP]")
            parts.append("[GAMELOGS]")
            for row in rows:
                dt, h, ab, hr, rbi, r, bb, so, sb = row
                parts.append(f"GAME {dt}|{h or 0}-{ab or 0}, {hr or 0} HR, {rbi or 0} RBI, {r or 0} R, {bb or 0} BB, {so or 0} SO, {sb or 0} SB")
            parts.append("[/GAMELOGS]")

        parts.append(f"\n[SUGGEST]{display_name} this season[/SUGGEST]")
        parts.append(f"[SUGGEST]{display_name} career stats[/SUGGEST]")

        return "\n".join(parts)
    finally:
        conn.close()


# ===================================================================


def _render_pitching_window(pitch_rows, row_label: str,
                             max_gamelogs: int = 30) -> tuple[str, str]:
    """Render a pitching career-window block (totals STATGRID + per-game GAMELOGS).

    Used by every "first/last N starts" branch in build_player_game_window
    so output shape is consistent: same 4 totals columns, same per-game
    line format, same row label convention.

    `pitch_rows` columns: (gdate, ip_text, ip_outs, h, er, so, bb, hr, win, loss, opp)
      — same shape SELECTed by both pitching branches.
    `row_label`: prefix for the totals row (e.g. "First 7 Starts" or "Last 5 Starts").
    Returns (grid_block, gamelogs_block) — caller decides ordering and titles.
    """
    total_outs = sum(r[2] or 0 for r in pitch_rows)
    total_h = sum(r[3] or 0 for r in pitch_rows)
    total_er = sum(r[4] or 0 for r in pitch_rows)
    total_so = sum(r[5] or 0 for r in pitch_rows)
    total_bb = sum(r[6] or 0 for r in pitch_rows)
    total_ip = total_outs / 3 if total_outs else 0.0
    era = (total_er / total_ip * 9) if total_ip > 0 else 0.0
    whip = ((total_h + total_bb) / total_ip) if total_ip > 0 else 0.0
    ip_str = f"{total_outs // 3}.{total_outs % 3}"

    grid = (
        "[STATGRID]\n"
        "HEADER: IP, K, ERA, WHIP\n"
        f"ROW {row_label}: {ip_str}, {total_so}, {era:.2f}, {whip:.2f}\n"
        "[/STATGRID]"
    )
    gamelogs = ""
    if len(pitch_rows) <= max_gamelogs:
        gl_lines = ["[GAMELOGS]"]
        for row in pitch_rows:
            gdate, ip_text, ip_outs, h, er, so, bb, hr, win, loss, opp = row
            ip_display = ip_text or f"{(ip_outs or 0) // 3}.{(ip_outs or 0) % 3}"
            decision = ""
            if win:
                decision = ", W"
            elif loss:
                decision = ", L"
            gl_lines.append(
                f"GAME {gdate}|{ip_display} IP, {h or 0} H, {er or 0} ER, "
                f"{bb or 0} BB, {so or 0} K, {hr or 0} HR{decision}"
            )
        gl_lines.append("[/GAMELOGS]")
        gamelogs = "\n".join(gl_lines)
    return grid, gamelogs


def build_player_game_window(name: str, window_type: str, n_games: int,
                              stat_info=None, season: Optional[int] = None,
                              window_noun: str = "Games") -> Optional[str]:
    """Build a response for 'first/last N games' queries.

    If season is None, compares across all seasons (which season had the most X
    in the first/last N games). If season is set, shows the stat line for that
    specific window.

    `window_noun` controls how the window unit is labelled in the title
    ("Games", "Starts", "Appearances"). The underlying SQL doesn't change —
    pitching game logs are one row per appearance, so "starts" maps onto
    the same per-row aggregation as "games".
    """
    conn = _get_db()
    if not conn:
        return None
    display_name, _ = _get_player_info(conn, name)
    # Get player_id
    row = conn.execute("SELECT player_id FROM players WHERE name = ? LIMIT 1",
                       (_sanitize(name),)).fetchone()
    if not row:
        conn.close()
        return None
    pid = row[0]

    order = "ASC" if window_type == "first" else "DESC"
    label = "First" if window_type == "first" else "Last"

    cur = conn.cursor()

    # "Starts" / "Appearances" forces the pitching path. The default
    # batting-first fallthrough below would otherwise grab an interleague
    # at-bat (e.g. Skubal's 1 PA from 2021) and serve a misleading batting
    # line. By skipping straight to pitching when the user said "starts",
    # we avoid that — and pitchers who never bat in the period in question
    # still get pitching logs.
    pitching_first = window_noun in ("Starts", "Appearances")

    # "last N games" without a season = most recent N games across all seasons
    if season is None and window_type == "last":
        if pitching_first:
            rows = []  # skip batting branch
        else:
            cur.execute(f"""
                SELECT g.date, g.hits, g.at_bats, g.doubles, g.triples, g.home_runs,
                       g.runs, g.rbi, g.walks, g.strikeouts, g.opponent
                FROM game_batting_logs g
                WHERE g.player_id = ? AND g.at_bats > 0
                ORDER BY g.date DESC
                LIMIT ?
            """, (pid, n_games))
            rows = cur.fetchall()
        if not rows:
            # Try pitching game logs for pitchers. Same SQL shape and
            # render format as the season-set first-N branch below so
            # outputs are consistent across all branches of this builder.
            cur.execute(f"""
                SELECT g.date, g.innings_pitched, g.ip_outs, g.hits, g.earned_runs,
                       g.strikeouts, g.walks, g.home_runs, g.win, g.loss, g.opponent
                FROM game_pitching_logs g
                WHERE g.player_id = ?
                ORDER BY g.date DESC
                LIMIT ?
            """, (pid, n_games))
            pitch_rows = cur.fetchall()
            if not pitch_rows:
                conn.close()
                return None

            row_label = f"{label} {n_games} {window_noun}"
            grid_block, gamelogs_block = _render_pitching_window(
                # reverse to chronological order so [GAMELOGS] reads top→bottom
                # as oldest→newest, matching the season-set branch.
                list(reversed(pitch_rows)),
                row_label=row_label, max_gamelogs=30,
            )
            parts = [
                f"**{display_name} — {label} {n_games} {window_noun}**\n",
                grid_block,
                gamelogs_block,
            ]

            conn.close()
            return "\n".join(parts)

        totals = {"g": len(rows), "ab": 0, "h": 0, "2b": 0, "3b": 0, "hr": 0,
                  "r": 0, "rbi": 0, "bb": 0, "so": 0}
        for row in rows:
            totals["h"] += row[1] or 0
            totals["ab"] += row[2] or 0
            totals["2b"] += row[3] or 0
            totals["3b"] += row[4] or 0
            totals["hr"] += row[5] or 0
            totals["r"] += row[6] or 0
            totals["rbi"] += row[7] or 0
            totals["bb"] += row[8] or 0
            totals["so"] += row[9] or 0

        avg = totals["h"] / max(totals["ab"], 1)
        slg_num = (totals["h"] - totals["2b"] - totals["3b"] - totals["hr"]) + \
                  2*totals["2b"] + 3*totals["3b"] + 4*totals["hr"]
        slg = slg_num / max(totals["ab"], 1)
        obp = (totals["h"] + totals["bb"]) / max(totals["ab"] + totals["bb"], 1)
        ops = obp + slg

        # Date range for display
        earliest = rows[-1][0] if rows else ""
        latest = rows[0][0] if rows else ""

        title = f"**{display_name} — Last {n_games} {window_noun}**\n"
        parts = [title]
        if earliest and latest:
            try:
                from datetime import datetime as _dt
                e_fmt = _dt.strptime(earliest, "%Y-%m-%d").strftime("%b %-d, %Y")
                l_fmt = _dt.strptime(latest, "%Y-%m-%d").strftime("%b %-d, %Y")
                parts.append(f"[SUBTITLE]{e_fmt} – {l_fmt}[/SUBTITLE]")
            except:
                pass
        parts.append("[STATGRID]")
        parts.append("HEADER: H, HR, RBI, OPS")
        parts.append(f"ROW Cumulative: {totals['h']}, {totals['hr']}, {totals['rbi']}, "
                    f"{_format_rate(ops)}")
        parts.append("[/STATGRID]")

        # Per-game breakdown for spans ≤ 30 games
        if n_games <= 30:
            # Re-fetch with SB
            cur.execute(f"""
                SELECT g.date, g.hits, g.at_bats, g.home_runs,
                       g.rbi, g.runs, g.walks, g.strikeouts, g.stolen_bases
                FROM game_batting_logs g
                WHERE g.player_id = ? AND g.at_bats > 0
                ORDER BY g.date DESC
                LIMIT ?
            """, (pid, n_games))
            detail_rows = cur.fetchall()
            parts.append("")
            parts.append("[GAMELOGS]")
            for row in detail_rows:
                dt, h, ab, hr, rbi, r, bb, so, sb = row
                parts.append(f"GAME {dt}|{h or 0}-{ab or 0}, {hr or 0} HR, {rbi or 0} RBI, {r or 0} R, {bb or 0} BB, {so or 0} SO, {sb or 0} SB")
            parts.append("[/GAMELOGS]")

        parts.append(f"\n[SUGGEST]{display_name} this season[/SUGGEST]")
        parts.append(f"[SUGGEST]{display_name} career stats[/SUGGEST]")

        conn.close()
        return "\n".join(parts)

    if season is None:
        # Compare across all seasons: for each season, compute stats in the window
        cur.execute(f"""
            SELECT g.season, g.hits, g.at_bats, g.doubles, g.triples, g.home_runs,
                   g.runs, g.rbi, g.walks, g.strikeouts, g.plate_appearances,
                   g.game_num
            FROM (
                SELECT *, ROW_NUMBER() OVER (PARTITION BY season ORDER BY date {order}) as game_num
                FROM game_batting_logs
                WHERE player_id = ? AND at_bats > 0
            ) g
            WHERE g.game_num <= ?
            ORDER BY g.season, g.game_num
        """, (pid, n_games))
        rows = cur.fetchall()
        if not rows:
            conn.close()
            return None

        # Aggregate per season
        from collections import defaultdict
        seasons = defaultdict(lambda: {"g": 0, "ab": 0, "h": 0, "2b": 0, "3b": 0,
                                        "hr": 0, "r": 0, "rbi": 0, "bb": 0, "so": 0, "pa": 0})
        for row in rows:
            szn = row[0]
            s = seasons[szn]
            s["g"] += 1
            s["h"] += row[1] or 0
            s["ab"] += row[2] or 0
            s["2b"] += row[3] or 0
            s["3b"] += row[4] or 0
            s["hr"] += row[5] or 0
            s["r"] += row[6] or 0
            s["rbi"] += row[7] or 0
            s["bb"] += row[8] or 0
            s["so"] += row[9] or 0
            s["pa"] += row[10] or 0

        # Sort by the target stat (or OPS if no specific stat)
        if stat_info and stat_info.db_column in ("hits", "home_runs", "doubles", "triples",
                                                   "runs", "rbi", "walks", "strikeouts"):
            col_map = {"hits": "h", "home_runs": "hr", "doubles": "2b", "triples": "3b",
                       "runs": "r", "rbi": "rbi", "walks": "bb", "strikeouts": "so"}
            sort_key = col_map.get(stat_info.db_column, "h")
            sorted_seasons = sorted(seasons.items(), key=lambda x: x[1][sort_key], reverse=True)
            stat_label = stat_info.display_abbrev
        else:
            # Sort by OPS
            def _ops(s):
                obp_num = s["h"] + s["bb"]
                obp_den = s["ab"] + s["bb"] + s["pa"] - s["ab"]  # rough
                slg_num = (s["h"] - s["2b"] - s["3b"] - s["hr"]) + 2*s["2b"] + 3*s["3b"] + 4*s["hr"]
                obp = obp_num / max(obp_den, 1)
                slg = slg_num / max(s["ab"], 1)
                return obp + slg
            sorted_seasons = sorted(seasons.items(), key=lambda x: _ops(x[1]), reverse=True)
            stat_label = None

        # Build output
        if stat_info and stat_label:
            title = f"**{display_name} — {label} {n_games} {window_noun} by Season ({stat_label})**\n"
        else:
            title = f"**{display_name} — {label} {n_games} {window_noun} by Season**\n"

        parts = [title]
        parts.append("[TIP]Tap a player name for their full profile.[/TIP]")
        parts.append("[LEADERBOARD]")
        parts.append("HEADER: H, HR, RBI, OPS")

        for szn, s in sorted_seasons:
            if s["g"] == 0:
                continue
            obp_num = s["h"] + s["bb"]
            obp_den = s["ab"] + s["bb"]
            obp = obp_num / max(obp_den, 1)
            slg_num = (s["h"] - s["2b"] - s["3b"] - s["hr"]) + 2*s["2b"] + 3*s["3b"] + 4*s["hr"]
            slg = slg_num / max(s["ab"], 1)
            ops = obp + slg
            parts.append(f"ROW {szn}: {s['h']}, {s['hr']}, {s['rbi']}, "
                        f"{_format_rate(ops)}")
        parts.append("[/LEADERBOARD]")

        # Best season callout
        best_szn, best_s = sorted_seasons[0]
        if stat_label:
            col_map = {"H": "h", "HR": "hr", "2B": "2b", "3B": "3b",
                       "R": "r", "RBI": "rbi", "BB": "bb", "SO": "so"}
            val = best_s[col_map.get(stat_label, "h")]
            parts.append(f"\n{display_name}'s best {label.lower()} {n_games} games by {stat_label}: "
                        f"**{best_szn}** with **{val} {stat_label}**.")
        parts.append(f"\n[SUGGEST]{display_name} career stats[/SUGGEST]")
        parts.append(f"[SUGGEST]{display_name} this season[/SUGGEST]")

        conn.close()
        return "\n".join(parts)

    else:
        # Specific season — show the stat line for that window
        if pitching_first:
            rows = []  # skip batting branch when user said "starts"
        else:
            cur.execute(f"""
                SELECT g.date, g.hits, g.at_bats, g.doubles, g.triples, g.home_runs,
                       g.runs, g.rbi, g.walks, g.strikeouts
                FROM (
                    SELECT *, ROW_NUMBER() OVER (ORDER BY date {order}) as game_num
                    FROM game_batting_logs
                    WHERE player_id = ? AND season = ? AND at_bats > 0
                ) g
                WHERE g.game_num <= ?
                ORDER BY g.date
            """, (pid, season, n_games))
            rows = cur.fetchall()
        if not rows:
            # No batting logs — try pitching. Pitchers asked about "first N
            # starts of 2025" land here. Same window math, different schema.
            cur.execute(f"""
                SELECT g.date, g.innings_pitched, g.ip_outs, g.hits, g.earned_runs,
                       g.strikeouts, g.walks, g.home_runs, g.win, g.loss, g.opponent
                FROM (
                    SELECT *, ROW_NUMBER() OVER (ORDER BY date {order}) as game_num
                    FROM game_pitching_logs
                    WHERE player_id = ? AND season = ?
                ) g
                WHERE g.game_num <= ?
                ORDER BY g.date
            """, (pid, season, n_games))
            pitch_rows = cur.fetchall()
            if not pitch_rows:
                conn.close()
                return None

            row_label = f"{label} {n_games} {window_noun}"
            grid_block, gamelogs_block = _render_pitching_window(
                pitch_rows, row_label=row_label, max_gamelogs=30,
            )
            title = f"**{display_name} — {label} {n_games} {window_noun} of {season}**\n"
            parts = [title, grid_block]
            if gamelogs_block:
                parts.append("")
                parts.append(gamelogs_block)
            parts.append(f"\n[SUGGEST]{display_name} {season}[/SUGGEST]")
            parts.append(f"[SUGGEST]{display_name} career stats[/SUGGEST]")
            conn.close()
            return "\n".join(parts)

        # Aggregate
        totals = {"g": len(rows), "ab": 0, "h": 0, "2b": 0, "3b": 0, "hr": 0,
                  "r": 0, "rbi": 0, "bb": 0, "so": 0}
        for row in rows:
            totals["h"] += row[1] or 0
            totals["ab"] += row[2] or 0
            totals["2b"] += row[3] or 0
            totals["3b"] += row[4] or 0
            totals["hr"] += row[5] or 0
            totals["r"] += row[6] or 0
            totals["rbi"] += row[7] or 0
            totals["bb"] += row[8] or 0
            totals["so"] += row[9] or 0

        avg = totals["h"] / max(totals["ab"], 1)
        slg_num = (totals["h"] - totals["2b"] - totals["3b"] - totals["hr"]) + \
                  2*totals["2b"] + 3*totals["3b"] + 4*totals["hr"]
        slg = slg_num / max(totals["ab"], 1)
        obp = (totals["h"] + totals["bb"]) / max(totals["ab"] + totals["bb"], 1)
        ops = obp + slg

        title = f"**{display_name} — {label} {n_games} {window_noun} of {season}**\n"
        parts = [title]
        parts.append("[STATGRID]")
        parts.append("HEADER: H, HR, RBI, OPS")
        parts.append(f"ROW {label} {n_games} {window_noun}: {totals['h']}, {totals['hr']}, "
                    f"{totals['rbi']}, {_format_rate(ops)}")
        parts.append("[/STATGRID]")
        parts.append(f"\n[SUGGEST]{display_name} {season}[/SUGGEST]")
        parts.append(f"[SUGGEST]{display_name} career stats[/SUGGEST]")

        conn.close()
        return "\n".join(parts)


# 30. build_matchup — batter vs pitcher matchup preview
# ===================================================================

def build_matchup(batter_name: str, pitcher_name: str,
                  season: Optional[int] = None,
                  game_context: Optional[str] = None) -> Optional[str]:
    """Matchup preview: batter vs pitcher with pitch-mix-weighted projection."""
    conn = _get_db()
    try:
        batter_display, _ = _get_player_info(conn, batter_name)
        pitcher_display, _ = _get_player_info(conn, pitcher_name)
        cur = conn.cursor()

        # Resolve season — use current year, fall back to last year for components
        if season:
            resolved_season = season
            fallback_season = None
        else:
            resolved_season = _current_year()
            fallback_season = resolved_season - 1

        # --- Pitcher's throwing hand ---
        cur.execute("SELECT throws FROM players WHERE name = ?", (_sanitize(pitcher_name),))
        r = cur.fetchone()
        pitcher_hand = r[0] if r and r[0] else None

        # --- Section 1 & 3: Pitcher's pitch mix + batter's pitch-type splits ---
        # Try current season first, fall back to last season if no data
        def _query_with_fallback(sql, params_template, name, seasons, min_pa=0):
            """Run a query trying each season until rich-enough results are found.
            min_pa: minimum total PA across all rows to consider the result sufficient.
            If the first season has fewer total PA, try the next season.
            """
            for szn in seasons:
                cur.execute(sql, (*params_template, szn))
                rows = cur.fetchall()
                if rows:
                    total_pa = sum(r[1] for r in rows)  # column 1 is always PA
                    if total_pa >= min_pa:
                        return rows
            # If no season met min_pa, return whatever the first season had
            if seasons:
                cur.execute(sql, (*params_template, seasons[0]))
                return cur.fetchall()
            return []

        seasons_to_try = [resolved_season]
        if fallback_season:
            seasons_to_try.append(fallback_season)

        # Get pitcher pitch mix from pitching pitch-type splits (PA distribution)
        # Need 50+ total PA for a meaningful mix — fall back to prior season early in the year
        pitcher_mix_rows = _query_with_fallback(
            "SELECT pts.pitch_type, pts.plate_appearances "
            "FROM pitch_type_pitching_splits pts "
            "JOIN players p ON pts.player_id = p.player_id "
            "WHERE p.name = ? AND pts.season = ? "
            "ORDER BY pts.plate_appearances DESC",
            (_sanitize(pitcher_name),), pitcher_name, seasons_to_try,
            min_pa=50,
        )

        # Get batter's pitch-type batting splits
        # Need 50+ total PA for meaningful per-pitch stats — fall back to prior season early
        batter_pitch_rows = _query_with_fallback(
            "SELECT pts.pitch_type, pts.plate_appearances, "
            "pts.batting_avg, pts.obp, pts.slg, pts.ops "
            "FROM pitch_type_batting_splits pts "
            "JOIN players p ON pts.player_id = p.player_id "
            "WHERE p.name = ? AND pts.season = ?",
            (_sanitize(batter_name),), batter_name, seasons_to_try,
            min_pa=50,
        )
        batter_by_pitch = {row[0]: row for row in batter_pitch_rows}

        # Compute pitch-mix-weighted projection
        total_pitcher_pa = sum(r[1] for r in pitcher_mix_rows) if pitcher_mix_rows else 0
        projection = None
        mix_table_rows = []

        # Dynamic PA minimum: 10 if we have a full season of data, 5 early in season
        min_pa_per_pitch = 10 if total_pitcher_pa >= 200 else 5

        if total_pitcher_pa > 0 and batter_by_pitch:
            weighted_avg = 0.0
            weighted_obp = 0.0
            weighted_slg = 0.0
            total_weight = 0.0

            for pitch_type, pitcher_pa in pitcher_mix_rows:
                mix_pct = pitcher_pa / total_pitcher_pa
                batter_row = batter_by_pitch.get(pitch_type)
                if not batter_row or batter_row[1] < min_pa_per_pitch:
                    continue

                b_pa, b_avg, b_obp, b_slg, b_ops = batter_row[1], batter_row[2], batter_row[3], batter_row[4], batter_row[5]
                if b_avg is None or b_obp is None or b_slg is None:
                    continue

                weighted_avg += mix_pct * b_avg
                weighted_obp += mix_pct * b_obp
                weighted_slg += mix_pct * b_slg
                total_weight += mix_pct

                # Only show pitch types that are >= 5% of the pitcher's mix in the table
                if mix_pct >= 0.05:
                    mix_table_rows.append((
                        pitch_type, round(mix_pct * 100), b_pa,
                        b_avg, b_obp, b_slg, b_ops or (b_obp + b_slg),
                    ))

            if total_weight > 0.5:  # Need at least 50% of mix covered
                # Renormalize
                proj_avg = weighted_avg / total_weight
                proj_obp = weighted_obp / total_weight
                proj_slg = weighted_slg / total_weight
                proj_ops = proj_obp + proj_slg
                projection = (proj_avg, proj_obp, proj_slg, proj_ops)

        # --- Section 2: Platoon split ---
        platoon_line = None
        if pitcher_hand:
            split_key = "vs_LHP" if pitcher_hand == "L" else "vs_RHP"
            pr = None
            for szn in seasons_to_try:
                cur.execute(
                    "SELECT ps.plate_appearances, ps.at_bats, ps.hits, "
                    "ps.home_runs, ps.walks, ps.strikeouts, "
                    "ps.batting_avg, ps.obp, ps.slg, ps.ops "
                    "FROM platoon_splits ps "
                    "JOIN players p ON ps.player_id = p.player_id "
                    "WHERE p.name = ? AND ps.season = ? AND ps.split = ?",
                    (_sanitize(batter_name), szn, split_key),
                )
                pr = cur.fetchone()
                if pr:
                    break
            if pr:
                platoon_line = {
                    "hand": "LHP" if pitcher_hand == "L" else "RHP",
                    "pa": pr[0], "ab": pr[1], "h": pr[2], "hr": pr[3],
                    "bb": pr[4], "so": pr[5],
                    "avg": pr[6], "obp": pr[7], "slg": pr[8], "ops": pr[9],
                }

        # --- Section 4: Current form ---
        batter_form = None
        cur.execute(
            "SELECT cf.num_games, cf.at_bats, cf.hits, cf.home_runs, "
            "cf.runs, cf.rbi, cf.walks, cf.strikeouts, "
            "cf.batting_avg, cf.obp, cf.slg, cf.ops "
            "FROM current_form cf "
            "JOIN players p ON cf.player_id = p.player_id "
            "WHERE p.name = ? AND cf.season = ?",
            (_sanitize(batter_name), resolved_season),
        )
        bf = cur.fetchone()
        if bf:
            batter_form = bf

        pitcher_form = None
        cur.execute(
            "SELECT pcf.num_games, pcf.innings_pitched, pcf.era, "
            "pcf.whip, pcf.k_per_9, pcf.bb_per_9 "
            "FROM pitching_current_form pcf "
            "JOIN players p ON pcf.player_id = p.player_id "
            "WHERE p.name = ? AND pcf.season = ?",
            (_sanitize(pitcher_name), resolved_season),
        )
        pf = cur.fetchone()
        if pf:
            pitcher_form = pf

        # --- Section 5: H2H (combine all available seasons) ---
        h2h = None
        try:
            cur.execute(
                "SELECT SUM(h.plate_appearances), SUM(h.at_bats), SUM(h.hits), "
                "SUM(h.home_runs), SUM(h.walks), SUM(h.strikeouts), "
                "CASE WHEN SUM(h.at_bats) > 0 THEN CAST(SUM(h.hits) AS REAL) / SUM(h.at_bats) END, "
                "CASE WHEN SUM(h.at_bats) + SUM(h.walks) > 0 THEN "
                "  CAST(SUM(h.hits) + SUM(h.walks) AS REAL) / (SUM(h.at_bats) + SUM(h.walks)) END, "
                "CASE WHEN SUM(h.at_bats) > 0 THEN "
                "  CAST(SUM(h.hits) + SUM(h.home_runs) * 3 AS REAL) / SUM(h.at_bats) END, "
                "NULL "  # OPS computed below
                "FROM head_to_head h "
                "JOIN players pb ON h.batter_id = pb.player_id "
                "JOIN players pp ON h.pitcher_id = pp.player_id "
                "WHERE pb.name = ? AND pp.name = ?",
                (_sanitize(batter_name), _sanitize(pitcher_name)),
            )
            hr = cur.fetchone()
            if hr and hr[0] and hr[0] > 0:
                h2h = hr
        except sqlite3.OperationalError:
            pass  # head_to_head table may not exist yet

        # --- Build output ---
        if not projection and not platoon_line and not batter_form and not h2h:
            return None  # No useful data

        parts = []
        title = f"**{batter_display} vs. {pitcher_display}**"
        if game_context:
            title += f" · {game_context}"
        parts.append(title)

        # --- Blended projection (43% pitch mix, 43% platoon, 14% current form) ---
        # Compute blend from whichever components are available, reweighting if missing
        blend_components = []  # (weight, avg, obp, slg)
        if projection:
            blend_components.append((0.43, *projection[:3]))
        if platoon_line:
            pl = platoon_line
            blend_components.append((0.43, pl['avg'], pl['obp'], pl['slg']))
        if batter_form:
            bf = batter_form
            blend_components.append((0.14, bf[8], bf[9], bf[10]))

        blended = None
        if blend_components:
            # Reweight to sum to 1.0 if some components missing
            total_w = sum(c[0] for c in blend_components)
            if total_w > 0:
                b_avg = sum(c[0] * c[1] for c in blend_components) / total_w
                b_obp = sum(c[0] * c[2] for c in blend_components) / total_w
                b_slg = sum(c[0] * c[3] for c in blend_components) / total_w
                b_ops = b_obp + b_slg
                blended = (b_avg, b_obp, b_slg, b_ops)

        if blended:
            parts.append("[SUBTITLE] [/SUBTITLE]")  # segment break for spacing
            parts.append("**Projected:**")
            parts.append(f"**{_format_rate(blended[0])} AVG / {_format_rate(blended[1])} OBP / {_format_rate(blended[2])} SLG ({_format_rate(blended[3])} OPS)**")
            parts.append("[SUBTITLE]Blending pitch mix, platoon, and recent performance[/SUBTITLE]")
        parts.append("")

        # H2H — show even with 1 PA
        if h2h and h2h[0]:
            pa = h2h[0]
            parts.append(f"**Head-to-Head ({pa} PA)**")
            parts.append("[STATGRID]")
            parts.append("HEADER: PA, AB, H, HR, BB, SO, AVG, OBP, SLG, OPS")
            parts.append(f"ROW {batter_display}: {h2h[0]}, {h2h[1]}, {h2h[2]}, {h2h[3]}, "
                        f"{h2h[4]}, {h2h[5]}, "
                        f"{_format_rate(h2h[6])}, {_format_rate(h2h[7])}, "
                        f"{_format_rate(h2h[8])}, {_format_rate(h2h[9])}")
            parts.append("[/STATGRID]\n")

        # Platoon
        if platoon_line:
            pl = platoon_line
            parts.append(f"**vs {pl['hand']} This Season**")
            parts.append("[STATGRID]")
            parts.append("HEADER: PA, H, HR, BB, SO, AVG, OBP, SLG, OPS")
            parts.append(f"ROW {batter_display}: {pl['pa']}, {pl['h']}, {pl['hr']}, "
                        f"{pl['bb']}, {pl['so']}, "
                        f"{_format_rate(pl['avg'])}, {_format_rate(pl['obp'])}, "
                        f"{_format_rate(pl['slg'])}, {_format_rate(pl['ops'])}")
            parts.append("[/STATGRID]\n")

        # Pitch mix projection table
        if mix_table_rows:
            batter_last = batter_display.split()[-1] if batter_display else batter_display
            pitcher_last = pitcher_display.split()[-1] if pitcher_display else pitcher_display
            parts.append(f"**Pitch Mix Projection**")
            parts.append(f"[SUBTITLE]{batter_last}'s stats against each pitch in {pitcher_last}'s arsenal[/SUBTITLE]")
            parts.append(f"[SUBTITLE]% is how often {pitcher_last} throws this pitch historically[/SUBTITLE]")
            parts.append("[LEADERBOARD]")
            _pitch_display = {"4-Seam": "4-Seamers", "2-Seam": "2-Seamers"}
            parts.append("HEADER: PA, AVG, OBP, SLG")
            for i, (pt, pct, pa, avg, obp, slg, ops) in enumerate(mix_table_rows):
                pt_display = _pitch_display.get(pt, pt)
                parts.append(f"ROW {pt_display} ({pct}%): {pa}, "
                            f"{_format_rate(avg)}, {_format_rate(obp)}, "
                            f"{_format_rate(slg)}")
            if projection:
                parts.append(f"FOOTER: Weighted stats for {batter_last} against this pitch mix")
                parts.append(f"FOOTER: {_format_rate(projection[0])} AVG / {_format_rate(projection[1])} OBP / {_format_rate(projection[2])} SLG ({_format_rate(projection[3])} OPS)")
            parts.append("[/LEADERBOARD]\n")

        # Recent streak (always show — season debut note if no form data)
        if True:
            parts.append("**Recent Streak**")
            # Build one combined statgrid with both players' form + debut notes
            streak_rows = []
            streak_notes = []
            if batter_form:
                bf = batter_form
                streak_rows.append(("batting", bf))
            else:
                streak_notes.append(f"This is {batter_display}'s season debut.")
            if pitcher_form:
                pf = pitcher_form
                streak_rows.append(("pitching", pf))
            else:
                streak_notes.append(f"This is {pitcher_display}'s season debut.")

            if streak_rows:
                # Emit batter form grid
                for kind, form in streak_rows:
                    parts.append("[STATGRID]")
                    if kind == "batting":
                        parts.append("HEADER: G, AB, H, HR, RBI, BB, SO, AVG, OBP, SLG, OPS")
                        parts.append(f"ROW {batter_display} (last {form[0]}G): "
                                    f"{form[1]}, {form[2]}, {form[3]}, {form[5]}, {form[6]}, {form[7]}, "
                                    f"{_format_rate(form[8])}, {_format_rate(form[9])}, "
                                    f"{_format_rate(form[10])}, {_format_rate(form[11])}")
                    else:
                        parts.append("HEADER: G, IP, ERA, WHIP, K/9, BB/9")
                        ip_str = _format_pitching_rate(form[1], 1) if form[1] else "0"
                        parts.append(f"ROW {pitcher_display} (last {form[0]}G): "
                                    f"{ip_str}, {_format_pitching_rate(form[2])}, "
                                    f"{_format_pitching_rate(form[3])}, "
                                    f"{_format_pitching_rate(form[4], 1)}, "
                                    f"{_format_pitching_rate(form[5], 1)}")
                    # Append debut notes as footers in the last grid
                    if kind == streak_rows[-1][0] and streak_notes:
                        for note in streak_notes:
                            parts.append(f"FOOTER: {note}")
                    parts.append("[/STATGRID]")
            parts.append("")

        # Suggestion pills
        parts.append(f"[SUGGEST]{batter_display} this season[/SUGGEST]")
        parts.append(f"[SUGGEST]{pitcher_display} this season[/SUGGEST]")
        parts.append(f"[SUGGEST]{batter_display} by pitch type[/SUGGEST]")

        return "\n".join(parts)
    finally:
        conn.close()


def _current_year() -> int:
    return datetime.now().year


# ===================================================================
# build_single_game_extreme
# ===================================================================

# Mapping from season stat columns to game log columns
_GAME_LOG_BAT_COLS = {
    "home_runs": "home_runs", "hits": "hits", "runs": "runs", "rbi": "rbi",
    "stolen_bases": "stolen_bases", "walks": "walks", "strikeouts": "strikeouts",
    "at_bats": "at_bats", "doubles": "doubles", "triples": "triples",
}
_GAME_LOG_PITCH_COLS = {
    "strikeouts": "strikeouts", "walks": "walks", "hits": "hits",
    "home_runs": "home_runs", "earned_runs": "earned_runs", "runs": "runs",
    "ip_outs": "ip_outs", "innings_pitched": "ip_outs",
}


def build_single_game_extreme(stat_info: StatInfo, season: Optional[int],
                              is_pitching: bool = False,
                              position: Optional[list[str]] = None) -> Optional[str]:
    """Build 'most K in one game' style queries from game logs."""
    # Default to current season — prevents unbounded scan of 661K+ rows
    if season is None:
        season = date.today().year

    conn = _get_db()
    try:
        if is_pitching:
            table = "game_pitching_logs"
            col_map = _GAME_LOG_PITCH_COLS
            prefix = "g"
        else:
            table = "game_batting_logs"
            col_map = _GAME_LOG_BAT_COLS
            prefix = "g"

        game_col = col_map.get(stat_info.db_column)
        if not game_col:
            return None  # Stat not available in game logs

        season_filter = f" AND {prefix}.season = ?" if season else ""
        params = [season] if season else []
        pos_clause = ""
        if position and not is_pitching:
            pos_clause = _position_filter(position, prefix, f"{prefix}.season")

        cur = conn.cursor()
        cur.execute(
            f"SELECT p.name, {prefix}.{game_col}, {prefix}.date, {prefix}.opponent "
            f"FROM {table} {prefix} "
            f"JOIN players p ON {prefix}.player_id = p.player_id "
            f"WHERE {prefix}.{game_col} IS NOT NULL{season_filter}{pos_clause} "
            f"ORDER BY {prefix}.{game_col} DESC LIMIT 25",
            tuple(params),
        )
        rows = cur.fetchall()
        if not rows:
            return None  # Fall through to Haiku/Sonnet

        pos_label = f"{_position_label(position)} " if position else ""
        scope_label = str(season) if season else "2016-2025"
        who = "Pitchers" if is_pitching else "Players"
        title = f"Most {stat_info.display_name} in a Single Game ({scope_label})"
        if pos_label:
            title = f"Most {stat_info.display_name} in a Single Game by {pos_label.strip()} ({scope_label})"

        # Use short date format when all results are from the same year
        single_year = season is not None

        parts = [f"**{title}**\n"]
        parts.append("[TIP]Tap a player name for their full profile.[/TIP]")
        parts.append("[LEADERBOARD]")
        parts.append(f"HEADER: {stat_info.display_abbrev}, Date, Opp")
        for i, row in enumerate(rows):
            val = str(row[1])
            if game_col == "ip_outs":
                try:
                    outs = int(row[1])
                    val = f"{outs // 3}.{outs % 3}"
                except (ValueError, TypeError):
                    pass
            date_str = _format_date(row[2]) if row[2] else ""
            opp = row[3] or ""
            parts.append(f"ROW {i+1}. {row[0]}: {val}, {date_str}, {opp}")
        parts.append("[/LEADERBOARD]")

        return "\n".join(parts)
    finally:
        conn.close()


# ===================================================================
# build_count_query
# ===================================================================

def build_count_query(stat_info: StatInfo, threshold: float, season: Optional[int],
                      is_pitching: bool = False,
                      position: Optional[list[str]] = None) -> Optional[str]:
    """Build 'how many players hit 30 HR in 2025' style queries."""
    conn = _get_db()
    try:
        table = "season_pitching_stats" if is_pitching else "season_batting_stats"
        prefix = "sp" if is_pitching else "s"

        season_filter = f" AND {prefix}.season = ?" if season else ""
        params: list = [threshold]
        if season:
            params.append(season)

        pos_clause = ""
        if position and not is_pitching:
            pos_clause = _position_filter(position, prefix, f"{prefix}.season")

        # Get the count and the actual players
        cur = conn.cursor()
        cur.execute(
            f"SELECT p.name, {prefix}.{stat_info.db_column} "
            f"FROM {table} {prefix} "
            f"JOIN players p ON {prefix}.player_id = p.player_id "
            f"WHERE {prefix}.{stat_info.db_column} >= ?{season_filter}{pos_clause} "
            f"ORDER BY {prefix}.{stat_info.db_column} DESC",
            tuple(params),
        )
        rows = cur.fetchall()
        count = len(rows)

        threshold_display = _format_rate(str(threshold)) if stat_info.is_rate else str(int(threshold))
        pos_label = _position_label(position) if position else ""
        who = "pitchers" if is_pitching else pos_label.lower() if pos_label else "players"
        scope = str(season) if season else "all time"

        if stat_info.is_rate:
            summary = f"**{count}** {who} have batted {threshold_display}+ {stat_info.display_abbrev} in {scope}."
        else:
            summary = f"**{count}** {who} have had {threshold_display}+ {stat_info.display_name} in {scope}."

        parts = [summary + "\n"]
        parts.append("[TIP]Tap a player name for their full profile.[/TIP]")
        parts.append("[LEADERBOARD]")
        parts.append(f"HEADER: {stat_info.display_abbrev}")
        for i, row in enumerate(rows):
            val = _format_rate(str(row[1])) if stat_info.is_rate else str(row[1])
            parts.append(f"ROW {i+1}. {row[0]}: {val}")
        parts.append("[/LEADERBOARD]")

        return "\n".join(parts)
    finally:
        conn.close()


# ===================================================================
# build_split_leaderboard
# ===================================================================

def build_split_leaderboard(stat_info: 'StatInfo', split_context, season: int,
                            limit: int = 50, league: Optional[str] = None,
                            sort_asc: bool = False) -> Optional[str]:
    """Build a leaderboard from a split table (count, pitch type, RISP, home/away, platoon, TTO, first-PA).

    Branches on `split_context.is_pitching`:
      - pitching: source rate columns from `_against` variants, sort ASC for rates,
        qualify against `season_pitching_stats`, league join via pitching team.
      - batting (default): rate columns as-is, sort DESC, qualify via
        `season_batting_stats`.
    Handles tables that are already a single bucket (filter_col=None,
    filter_values=[]): no IN-clause is emitted.
    """
    conn = _get_db()
    try:
        table = split_context.table
        filter_col = split_context.filter_col
        filter_values = split_context.filter_values
        label = split_context.label
        is_pitching = getattr(split_context, "is_pitching", False)

        # Filter clause: skip entirely when the table is already a single split
        if filter_col and filter_values:
            placeholders = ", ".join("?" for _ in filter_values)
            split_filter = f"AND t.{filter_col} IN ({placeholders})"
            split_params: tuple = tuple(filter_values)
        else:
            split_filter = ""
            split_params = ()

        cur = conn.cursor()
        cur.execute(f"PRAGMA table_info({table})")
        valid_cols = {r[1] for r in cur.fetchall()}

        # For pitching split tables, rate columns are stored as `_against` variants.
        # Map the requested stat's db_column to the actual column on the table.
        col = stat_info.db_column
        if is_pitching:
            _pitching_rate_col_map = {
                "ops": "ops_against", "obp": "obp_against",
                "slg": "slg_against", "batting_avg": "batting_avg_against",
                "avg": "batting_avg_against",
            }
            col = _pitching_rate_col_map.get(col, col)
        if col not in valid_cols:
            return None

        # Natural sort: pitching rate ASC (lower=better), batting rate DESC
        # (higher=better), counting DESC. "Worst" / "fewest" (sort_asc=True)
        # inverts whatever the natural direction was, so:
        #   batting rate: DESC (best) → ASC (worst)
        #   pitching rate: ASC (best) → DESC (worst)
        natural_asc = is_pitching and stat_info.is_rate
        # XOR: flip natural when sort_asc=True
        final_asc = natural_asc != sort_asc
        sort_dir = "ASC" if final_asc else "DESC"

        pa_filter = ""
        if stat_info.is_rate:
            split_pa_min, qual_col = _split_pa_floor(
                conn, season, table, filter_col, filter_values, is_pitching,
            )
            pa_filter = f" AND t.{qual_col} >= {split_pa_min}"

        league_filter = ""
        league_label = ""
        if league:
            league_qual_table = "season_pitching_stats" if is_pitching else "season_batting_stats"
            league_qual_alias = "spq"
            league_filter = (
                f" AND EXISTS (SELECT 1 FROM {league_qual_table} {league_qual_alias} "
                f"WHERE {league_qual_alias}.player_id = t.player_id "
                f"AND {league_qual_alias}.season = t.season "
                f"AND {_league_team_clause(league, league_qual_alias)})"
            )
            league_label = f" ({league})"

        # For rate stats: if multiple filter values, recompute from components.
        # If single filter value (or single-bucket table), use the precomputed
        # column directly. Pitching split tables don't carry the raw
        # _against rate-component pieces, so we never aggregate for them — the
        # `_against` precomputed column is always single-bucket.
        needs_aggregation = len(filter_values) > 1 and not is_pitching
        if needs_aggregation and stat_info.is_rate and col in ("batting_avg", "obp", "slg", "ops", "iso", "babip"):
            rate_formulas = {
                "batting_avg": "CAST(SUM(t.hits) AS REAL) / NULLIF(SUM(t.at_bats), 0)",
                "obp": ("CAST(SUM(t.hits) + SUM(t.walks) + SUM(COALESCE(t.hit_by_pitch, 0)) AS REAL) / "
                        "NULLIF(SUM(t.at_bats) + SUM(t.walks) + SUM(COALESCE(t.hit_by_pitch, 0)) + SUM(COALESCE(t.sacrifice_flies, 0)), 0)"),
                "slg": ("CAST(SUM(t.hits) - SUM(t.doubles) - SUM(t.triples) - SUM(t.home_runs) "
                        "+ 2*SUM(t.doubles) + 3*SUM(t.triples) + 4*SUM(t.home_runs) AS REAL) "
                        "/ NULLIF(SUM(t.at_bats), 0)"),
                "ops": ("(CAST(SUM(t.hits) + SUM(t.walks) + SUM(COALESCE(t.hit_by_pitch, 0)) AS REAL) / "
                        "NULLIF(SUM(t.at_bats) + SUM(t.walks) + SUM(COALESCE(t.hit_by_pitch, 0)) + SUM(COALESCE(t.sacrifice_flies, 0)), 0)) + "
                        "(CAST(SUM(t.hits) - SUM(t.doubles) - SUM(t.triples) - SUM(t.home_runs) "
                        "+ 2*SUM(t.doubles) + 3*SUM(t.triples) + 4*SUM(t.home_runs) AS REAL) "
                        "/ NULLIF(SUM(t.at_bats), 0))"),
                "iso": ("CAST(SUM(t.doubles) + 2*SUM(t.triples) + 3*SUM(t.home_runs) AS REAL) "
                        "/ NULLIF(SUM(t.at_bats), 0)"),
                "babip": ("CAST(SUM(t.hits) - SUM(t.home_runs) AS REAL) / "
                          "NULLIF(SUM(t.at_bats) - SUM(t.strikeouts) - SUM(t.home_runs) + SUM(COALESCE(t.sacrifice_flies, 0)), 0)"),
            }
            agg_select = rate_formulas.get(col, f"SUM(t.{col})")
            cur.execute(
                f"SELECT p.name, {agg_select} AS stat_val, SUM(t.plate_appearances) AS pa "
                f"FROM {table} t "
                f"JOIN players p ON t.player_id = p.player_id "
                f"WHERE t.season = ? {split_filter}{pa_filter}{league_filter} "
                f"GROUP BY t.player_id "
                f"HAVING stat_val IS NOT NULL "
                f"ORDER BY stat_val {sort_dir} LIMIT ?",
                (season, *split_params, limit),
            )
        elif not needs_aggregation:
            # Single filter value (or single-bucket table) — use precomputed column directly
            cur.execute(
                f"SELECT p.name, t.{col} AS stat_val "
                f"FROM {table} t "
                f"JOIN players p ON t.player_id = p.player_id "
                f"WHERE t.season = ? {split_filter}{pa_filter}{league_filter} "
                f"ORDER BY stat_val {sort_dir} LIMIT ?",
                (season, *split_params, limit),
            )
        else:
            # Counting stat — sum across filter values
            cur.execute(
                f"SELECT p.name, SUM(t.{col}) AS stat_val "
                f"FROM {table} t "
                f"JOIN players p ON t.player_id = p.player_id "
                f"WHERE t.season = ? {split_filter}{league_filter} "
                f"GROUP BY t.player_id "
                f"ORDER BY stat_val {sort_dir} LIMIT ?",
                (season, *split_params, limit),
            )

        rows = cur.fetchall()
        if not rows:
            return None  # No data — let query fall through to Haiku/Sonnet
        # If best result is 0 for a counting stat, the data isn't populated — fall through
        # (Don't apply for rate stats — 0.000 AVG is a valid early-season result)
        if not stat_info.is_rate and rows[0][1] is not None and float(rows[0][1]) == 0:
            return None

        # For pitching splits, rate columns are "_against" variants — call out
        # that we're showing what hitters did against the pitcher, not what
        # the pitcher did at the plate.
        title_stat = (f"{stat_info.display_name} Allowed" if is_pitching and stat_info.is_rate
                      else stat_info.display_name)
        header_stat = (f"{stat_info.display_abbrev} Allowed" if is_pitching and stat_info.is_rate
                       else stat_info.display_abbrev)
        # "Worst" prefix for sort_asc=True (counting → "Fewest"). Drops the
        # "Leaders" suffix since "Worst X Leaders" reads contradictorily.
        if sort_asc:
            direction = "Fewest " if not stat_info.is_rate else "Worst "
            title = f"**{season} {direction}{title_stat} {label}{league_label}**\n"
        else:
            title = f"**{season} {title_stat} Leaders {label}{league_label}**\n"
        parts = [title]
        parts.append("[TIP]Tap a player name for their full profile.[/TIP]")
        parts.append("[LEADERBOARD]")
        parts.append(f"HEADER: {header_stat}")
        for i, row in enumerate(rows):
            val = _format_rate(str(row[1])) if stat_info.is_rate else str(int(row[1]))
            parts.append(f"ROW {i+1}. {row[0]}: {val}")
        parts.append("[/LEADERBOARD]")

        if stat_info.is_rate:
            # Show the ACTUAL qualifier value, not a hardcoded full-season
            # one. The PA floor prorates by season progress (max_games/162)
            # so mid-May max_games≈40 → split_pa_min≈5. Hardcoding "20"
            # read as a hard 20-PA floor when the real filter was much
            # looser — a #1 result with 17 PA in split surfaced and made
            # the label look wrong. Use the same number the filter uses.
            if table == "pitching_home_away_splits":
                # split_pa_min is in ip_outs here; convert to IP for readability.
                ip_min = split_pa_min / 3
                parts.append(f"\n_Min. {ip_min:.1f} IP in split._")
            else:
                parts.append(f"\n_Min. {split_pa_min} PA in split._")

        # Cross-side pivot: when a counterpart split table exists, surface a
        # tappable pill that flips between hitters/pitchers without making the
        # user retype the whole query. The pivot phrase is set on each
        # SplitContext that has a counterpart (no first_pa_pitching_splits, so
        # first-PA entries leave it None and skip this affordance).
        pivot = getattr(split_context, "pivot_phrase", None)
        if pivot:
            parts.append(f"\n[SUGGEST]{pivot}[/SUGGEST]")

        return "\n".join(parts)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Perfect games — hand-curated JSON list. Zero Claude cost.
# ---------------------------------------------------------------------------

_PERFECT_GAMES_JSON = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                   "data", "perfect_games.json")
# MLB all-time: 21 modern regular season + Larsen WS + 2 pre-1900 = 24 (matches
# notable_events.py _TOTAL_PERFECT_GAMES_MLB).
_TOTAL_PERFECT_GAMES_MLB = 24


def _load_perfect_games_list():
    try:
        with open(_PERFECT_GAMES_JSON) as f:
            games = json.load(f)
    except Exception:
        return []
    games.sort(key=lambda g: g.get("date", ""))
    return games


def build_perfect_games(filter_dict: dict) -> Optional[str]:
    """Format a perfect-games response from the curated JSON list."""
    mode = filter_dict.get("mode")
    games = _load_perfect_games_list()
    if not games:
        return None

    if mode == "count":
        return (f"There have been **{_TOTAL_PERFECT_GAMES_MLB} perfect games** in MLB history "
                f"(21 modern regular season + Don Larsen's 1956 World Series + 2 pre-1900)."
                f"\n\n[SUGGEST]most recent perfect game[/SUGGEST]"
                f"\n[SUGGEST]perfect games since 2010[/SUGGEST]"
                f"\n[SUGGEST]all perfect games[/SUGGEST]")

    if mode == "latest":
        g = games[-1]
        return (f"The most recent MLB perfect game was **{g['player']}** on "
                f"{g['date']} against the {g['opponent']}."
                f"\n\n[SUGGEST]all perfect games[/SUGGEST]"
                f"\n[SUGGEST]how many perfect games in MLB history[/SUGGEST]"
                f"\n[SUGGEST]perfect games since 2010[/SUGGEST]")

    if mode == "since":
        year = filter_dict["year"]
        filtered = [g for g in games if int(g["date"][:4]) >= year]
        title = f"**MLB Perfect Games Since {year}**"
        empty_msg = f"No MLB perfect games since {year}."
    elif mode == "in":
        year = filter_dict["year"]
        filtered = [g for g in games if int(g["date"][:4]) == year]
        title = f"**MLB Perfect Games in {year}**"
        empty_msg = f"No MLB perfect games in {year}."
    else:  # "all"
        filtered = games
        title = "**MLB Perfect Games (Modern Era)**"
        empty_msg = None

    if not filtered:
        return (f"{empty_msg}"
                f"\n\n[SUGGEST]all perfect games[/SUGGEST]"
                f"\n[SUGGEST]most recent perfect game[/SUGGEST]"
                f"\n[SUGGEST]how many perfect games in MLB history[/SUGGEST]")

    parts = [title, "[LEADERBOARD]", "HEADER: Perfect Game"]
    for i, g in enumerate(filtered):
        parts.append(f"ROW {i+1}. {g['player']} ({g['date']}) — vs {g['opponent']}")
    parts.append("[/LEADERBOARD]")
    parts.append(f"\n_Modern-era list. {_TOTAL_PERFECT_GAMES_MLB} total in MLB history including 2 pre-1900._")
    parts.append("\n[SUGGEST]most recent perfect game[/SUGGEST]")
    parts.append("[SUGGEST]how many perfect games in MLB history[/SUGGEST]")
    parts.append("[SUGGEST]perfect games since 2010[/SUGGEST]")
    parts.append("[LIST_STATE:complete]")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Apply list-state markers to other leaderboard-style builders
# ---------------------------------------------------------------------------
# Truncated (LIMIT-based) — "what else?" can expand by re-running with
# a larger limit. Defaults encoded in each signature.
build_pitching_leaderboard = _append_truncated_marker(10)(build_pitching_leaderboard)
build_platoon_leaderboard = _append_truncated_marker(50)(build_platoon_leaderboard)
build_filtered_leaderboard = _append_truncated_marker(10)(build_filtered_leaderboard)


# Complete (no LIMIT) — "what else?" gets a canned "that's the full list".
def _append_complete_marker(fn):
    import functools
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        result = fn(*args, **kwargs)
        if isinstance(result, str) and "[LEADERBOARD]" in result and "[LIST_STATE:" not in result:
            result = result + "\n[LIST_STATE:complete]"
        return result
    return wrapper


build_all_time_threshold = _append_complete_marker(build_all_time_threshold)
build_multi_threshold = _append_complete_marker(build_multi_threshold)
build_all_time_multi_threshold = _append_complete_marker(build_all_time_multi_threshold)
