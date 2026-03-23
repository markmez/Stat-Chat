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

import os
import re
import sqlite3
from datetime import datetime
from typing import Optional

from .name_matcher import StatInfo

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
    return sqlite3.connect(DB_PATH)


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


def _league_team_clause(league: str, alias: str) -> str:
    """Return a SQL clause filtering by AL or NL team codes."""
    teams = _AL_TEAMS if league == "AL" else _NL_TEAMS
    return f"{alias}.team IN ({teams})"


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
    """Batting season stats as a [STATGRID] block."""
    conn = _get_db()
    try:
        season = _resolve_season(conn, name, season)
        display_name, _ = _get_player_info(conn, name)
        cur = conn.cursor()
        cur.execute(
            "SELECT s.team, s.games, s.at_bats, s.runs, s.hits, "
            "s.doubles, s.triples, s.home_runs, s.rbi, s.stolen_bases, s.caught_stealing, "
            "s.walks, s.intentional_walks, s.strikeouts, s.hit_by_pitch, "
            "s.batting_avg, s.obp, s.slg, s.ops, s.ops_plus, s.iso, s.babip "
            "FROM season_batting_stats s "
            "JOIN players p ON s.player_id = p.player_id "
            "WHERE p.name = ? AND s.season = ?",
            (_sanitize(name), season),
        )
        row = cur.fetchone()
        if not row or len(row) < 22:
            return None

        team = _team_full_name(row[0])
        values = list(row[1:22])
        formatted = _format_values(BATTING_HEADERS, [str(v) if v is not None else "" for v in values])

        parts = []
        parts.append(f"**{display_name}** \u2014 {season} Season ({team})\n")
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
    """Pitching season stats as a [STATGRID] block."""
    conn = _get_db()
    try:
        season = _resolve_season(conn, name, season, "season_pitching_stats", "sp")
        display_name, _ = _get_player_info(conn, name)
        cur = conn.cursor()
        cur.execute(
            "SELECT sp.team, sp.wins, sp.losses, sp.saves, sp.games, sp.games_started, "
            "sp.games_finished, sp.complete_games, sp.quality_starts, sp.innings_pitched, "
            "sp.hits, sp.runs, sp.earned_runs, sp.home_runs, sp.walks, sp.intentional_walks, "
            "sp.strikeouts, sp.hit_by_pitch, sp.wild_pitches, sp.balks, "
            "sp.batters_faced, sp.sacrifice_hits, sp.sacrifice_flies, "
            "sp.stolen_bases, sp.caught_stealing, "
            "sp.era, sp.whip, sp.k_per_9, sp.bb_per_9, sp.k_per_bb, "
            "sp.h_per_9, sp.hr_per_9, sp.baa, sp.era_plus "
            "FROM season_pitching_stats sp "
            "JOIN players p ON sp.player_id = p.player_id "
            "WHERE p.name = ? AND sp.season = ?",
            (_sanitize(name), season),
        )
        row = cur.fetchone()
        if not row or len(row) < 34:
            return None

        team = _team_full_name(row[0])
        values = [str(v) if v is not None else "" for v in row[1:34]]
        formatted = _format_pitching_values(PITCHING_ALL_HEADERS, values)
        display_values = _filter_pitching_for_display(formatted)

        parts = []
        parts.append(f"**{display_name}** \u2014 {season} Season ({team})\n")
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

def _fetch_season_row(conn, name, year):
    """Fetch a specific season's formatted batting values. Returns (year, values) or None."""
    cur = conn.cursor()
    cur.execute(
        "SELECT s.season, s.games, s.at_bats, s.runs, s.hits, "
        "s.doubles, s.triples, s.home_runs, s.rbi, s.stolen_bases, s.caught_stealing, "
        "s.walks, s.intentional_walks, s.strikeouts, s.hit_by_pitch, "
        "s.batting_avg, s.obp, s.slg, s.ops, s.ops_plus, s.iso, s.babip "
        "FROM season_batting_stats s "
        "JOIN players p ON s.player_id = p.player_id "
        "WHERE p.name = ? AND s.season = ? LIMIT 1",
        (_sanitize(name), year),
    )
    row = cur.fetchone()
    if not row:
        return None
    yr = int(row[0])
    values = [str(v) if v is not None else "" for v in row[1:22]]
    return yr, _format_values(BATTING_HEADERS, values)


def _fetch_latest_season_row(conn, name):
    """Fetch latest season's formatted batting values."""
    cur = conn.cursor()
    cur.execute(
        "SELECT s.season, s.games, s.at_bats, s.runs, s.hits, "
        "s.doubles, s.triples, s.home_runs, s.rbi, s.stolen_bases, s.caught_stealing, "
        "s.walks, s.intentional_walks, s.strikeouts, s.hit_by_pitch, "
        "s.batting_avg, s.obp, s.slg, s.ops, s.ops_plus, s.iso, s.babip "
        "FROM season_batting_stats s "
        "JOIN players p ON s.player_id = p.player_id "
        "WHERE p.name = ? ORDER BY s.season DESC LIMIT 1",
        (_sanitize(name),),
    )
    row = cur.fetchone()
    if not row:
        return None
    yr = int(row[0])
    values = [str(v) if v is not None else "" for v in row[1:22]]
    return yr, _format_values(BATTING_HEADERS, values)


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

def _fetch_pitching_season_row(conn, name, year):
    """Fetch a specific season's formatted pitching values."""
    cur = conn.cursor()
    cur.execute(
        "SELECT sp.season, sp.wins, sp.losses, sp.saves, sp.games, sp.games_started, "
        "sp.games_finished, sp.complete_games, sp.quality_starts, sp.innings_pitched, "
        "sp.hits, sp.runs, sp.earned_runs, sp.home_runs, sp.walks, sp.intentional_walks, "
        "sp.strikeouts, sp.hit_by_pitch, sp.wild_pitches, sp.balks, "
        "sp.batters_faced, sp.sacrifice_hits, sp.sacrifice_flies, "
        "sp.stolen_bases, sp.caught_stealing, "
        "sp.era, sp.whip, sp.k_per_9, sp.bb_per_9, sp.k_per_bb, "
        "sp.h_per_9, sp.hr_per_9, sp.baa, sp.era_plus "
        "FROM season_pitching_stats sp "
        "JOIN players p ON sp.player_id = p.player_id "
        "WHERE p.name = ? AND sp.season = ? LIMIT 1",
        (_sanitize(name), year),
    )
    row = cur.fetchone()
    if not row:
        return None
    yr = int(row[0])
    values = [str(v) if v is not None else "" for v in row[1:34]]
    formatted = _format_pitching_values(PITCHING_ALL_HEADERS, values)
    return yr, _filter_pitching_for_display(formatted)


def _fetch_pitching_latest_season_row(conn, name):
    """Fetch latest pitching season."""
    cur = conn.cursor()
    cur.execute(
        "SELECT sp.season, sp.wins, sp.losses, sp.saves, sp.games, sp.games_started, "
        "sp.games_finished, sp.complete_games, sp.quality_starts, sp.innings_pitched, "
        "sp.hits, sp.runs, sp.earned_runs, sp.home_runs, sp.walks, sp.intentional_walks, "
        "sp.strikeouts, sp.hit_by_pitch, sp.wild_pitches, sp.balks, "
        "sp.batters_faced, sp.sacrifice_hits, sp.sacrifice_flies, "
        "sp.stolen_bases, sp.caught_stealing, "
        "sp.era, sp.whip, sp.k_per_9, sp.bb_per_9, sp.k_per_bb, "
        "sp.h_per_9, sp.hr_per_9, sp.baa, sp.era_plus "
        "FROM season_pitching_stats sp "
        "JOIN players p ON sp.player_id = p.player_id "
        "WHERE p.name = ? ORDER BY sp.season DESC LIMIT 1",
        (_sanitize(name),),
    )
    row = cur.fetchone()
    if not row:
        return None
    yr = int(row[0])
    values = [str(v) if v is not None else "" for v in row[1:34]]
    formatted = _format_pitching_values(PITCHING_ALL_HEADERS, values)
    return yr, _filter_pitching_for_display(formatted)


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
    """Single batting stat value formatted as natural-language text."""
    conn = _get_db()
    try:
        season = _resolve_season(conn, name, season)
        cur = conn.cursor()
        cur.execute(
            f"SELECT p.name, s.team, s.{stat_info.db_column} "
            "FROM season_batting_stats s "
            "JOIN players p ON s.player_id = p.player_id "
            "WHERE p.name = ? AND s.season = ? LIMIT 1",
            (_sanitize(name), season),
        )
        row = cur.fetchone()
        if not row or len(row) < 3:
            return None

        display_name = row[0]
        team = row[1]
        raw_value = str(row[2]) if row[2] is not None else ""

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

        team_display = _team_full_name(team)
        stat_name = stat_info.pill_name
        return (
            f"{sentence} ({team_display})\n\n"
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
    """Single pitching stat value formatted as natural-language text."""
    conn = _get_db()
    try:
        season = _resolve_season(conn, name, season, "season_pitching_stats", "sp")
        cur = conn.cursor()
        cur.execute(
            f"SELECT p.name, sp.team, sp.{stat_info.db_column} "
            "FROM season_pitching_stats sp "
            "JOIN players p ON sp.player_id = p.player_id "
            "WHERE p.name = ? AND sp.season = ? LIMIT 1",
            (_sanitize(name), season),
        )
        row = cur.fetchone()
        if not row or len(row) < 3:
            return None

        display_name = row[0]
        team = row[1]
        raw_value = str(row[2]) if row[2] is not None else ""

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

        team_display = _team_full_name(team)
        stat_name = stat_info.pill_name
        return (
            f"{sentence} ({team_display})\n\n"
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
    """AVG/OBP/SLG formatted as a small [STATGRID]."""
    conn = _get_db()
    try:
        season = _resolve_season(conn, name, season)
        cur = conn.cursor()
        cur.execute(
            "SELECT p.name, s.team, s.batting_avg, s.obp, s.slg, s.ops "
            "FROM season_batting_stats s "
            "JOIN players p ON s.player_id = p.player_id "
            "WHERE p.name = ? AND s.season = ? LIMIT 1",
            (_sanitize(name), season),
        )
        row = cur.fetchone()
        if not row or len(row) < 6:
            return None

        display_name = row[0]
        team = row[1]
        avg = _format_rate(row[2])
        obp = _format_rate(row[3])
        slg = _format_rate(row[4])
        ops = _format_rate(row[5])
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
                f"{sentence} ({team_display})\n\n"
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
                f"{sentence} ({team_display})\n\n"
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

        # PA minimum for rate stats
        pa_filter = ""
        if stat_info.is_rate:
            pa_filter = f" AND {alias}.plate_appearances >= 100"

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
            parts.append("\n_Min. 100 PA._")

        return "\n".join(parts)
    finally:
        conn.close()


# ===================================================================
# 15c. build_multi_threshold
# ===================================================================

def build_multi_threshold(filters: list, season: int,
                          is_pitching: bool = False,
                          league: Optional[str] = None) -> Optional[str]:
    """Build a multi-stat threshold: '.300 AVG with 30+ HR', '200 K and sub-3.00 ERA'."""
    conn = _get_db()
    try:
        table = "season_pitching_stats" if is_pitching else "season_batting_stats"
        prefix = "sp" if is_pitching else "s"
        league_filter = f" AND {_league_team_clause(league, prefix)}" if league else ""
        league_label = f" ({league})" if league else ""

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
                where_parts.append(f"{prefix}.ip_outs >= 486")
            else:
                cur.execute(f"SELECT MAX(games) FROM {table} WHERE season = ?", (season,))
                r = cur.fetchone()
                max_games = int(r[0]) if r and r[0] else 162
                pa_min = 400 if max_games >= 140 else 200
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
            f"WHERE {where_clause}{league_filter} "
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
        who = "Pitchers" if is_pitching else "Players"
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
                                   since_year: Optional[int] = None) -> Optional[str]:
    """All-time multi-stat threshold: '.300 with 30 HR' (no season specified)."""
    conn = _get_db()
    try:
        table = "season_pitching_stats" if is_pitching else "season_batting_stats"
        prefix = "sp" if is_pitching else "s"
        league_filter = f" AND {_league_team_clause(league, prefix)}" if league else ""
        league_label = f" ({league})" if league else ""

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
            f"WHERE {where_clause}{league_filter} "
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
        who = "Pitchers" if is_pitching else "Players"
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


# ===================================================================
# 25. build_leaderboard
# ===================================================================

def build_leaderboard(stat_info: StatInfo, scope: str, limit: int = 10,
                      league: Optional[str] = None,
                      season: Optional[int] = None,
                      since_year: Optional[int] = None) -> Optional[str]:
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
        stat_name = stat_info.pill_name

        if scope == "season":
            yr = season or datetime.now().year
            # PA minimum for rate stats
            pa_min = None
            if stat_info.is_rate:
                cur = conn.cursor()
                cur.execute("SELECT MAX(games) FROM season_batting_stats WHERE season = ?", (yr,))
                r = cur.fetchone()
                max_games = int(r[0]) if r and r[0] else 162
                pa_min = 400 if max_games >= 140 else 200
            pa_filter = f" AND s.plate_appearances >= {pa_min}" if pa_min else ""

            cur = conn.cursor()
            cur.execute(
                f"SELECT p.name, s.{stat_info.db_column} "
                f"FROM season_batting_stats s "
                f"JOIN players p ON s.player_id = p.player_id "
                f"WHERE s.season = ?{pa_filter}{league_filter} "
                f"ORDER BY s.{stat_info.db_column} DESC LIMIT ?",
                (yr, limit),
            )
            rows = cur.fetchall()
            if not rows:
                return f"No {stat_info.display_name} leaders found for {yr}{league_label}."

            parts = [f"**{yr} {stat_info.display_name} Leaders{league_label}**\n"]
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
            pa_filter = f" WHERE s.plate_appearances >= 400{league_filter}" if stat_info.is_rate else (
                f" WHERE {league_filter[5:]}" if league_filter else ""
            )
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
                return f"No all-time {stat_info.display_name} leaders found{league_label}."

            parts = [f"**All-Time Single Season {stat_info.display_name} Leaders{league_label}**\n"]
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
                f"WHERE s.season >= ?{pa_filter}{league_filter} "
                f"ORDER BY s.{stat_info.db_column} DESC LIMIT ?",
                (sy, limit),
            )
            rows = cur.fetchall()
            if not rows:
                return f"No {stat_info.display_name} leaders found since {sy}{league_label}."

            parts = [f"**{stat_info.display_name} Leaders Since {sy}{league_label}**\n"]
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
            where_clause = f" WHERE {league_filter[5:]}" if league_filter else ""

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
                               since_year: Optional[int] = None) -> Optional[str]:
    """
    Build a pitching leaderboard.

    scope: "season", "allTimeSingleSeason", "allTimeSince", "career".
    Also accepts name_matcher scope strings (auto-parsed).
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
        order_dir = "ASC" if stat_info.display_abbrev in _LOWER_IS_BETTER_PITCHING else "DESC"

        def _fmt(raw):
            return _format_pitching_rate(raw, 2) if stat_info.is_rate else str(raw)

        if scope == "season":
            yr = season or datetime.now().year
            ip_filter = ""
            ip_note = None
            if stat_info.is_rate:
                cur = conn.cursor()
                cur.execute("SELECT MAX(games) FROM season_pitching_stats WHERE season = ?", (yr,))
                r = cur.fetchone()
                max_games = int(r[0]) if r and r[0] else 162
                ip_outs_min = 486 if max_games >= 140 else 243
                ip_filter = f" AND sp.ip_outs >= {ip_outs_min}"
                ip_note = "Min. qualified IP."

            cur = conn.cursor()
            cur.execute(
                f"SELECT p.name, sp.{stat_info.db_column} "
                f"FROM season_pitching_stats sp "
                f"JOIN players p ON sp.player_id = p.player_id "
                f"WHERE sp.season = ?{ip_filter}{league_filter} "
                f"ORDER BY sp.{stat_info.db_column} {order_dir} LIMIT ?",
                (yr, limit),
            )
            rows = cur.fetchall()
            if not rows:
                return f"No {stat_info.display_name} leaders found for {yr}{league_label}."

            parts = [f"**{yr} {stat_info.display_name} Leaders{league_label} (Pitching)**\n"]
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


# ===================================================================
# 27. build_threshold
# ===================================================================

def build_threshold(stat_info: StatInfo, threshold: float, comparison: str,
                    season: int, league: Optional[str] = None,
                    is_pitching: bool = False) -> Optional[str]:
    """Build a threshold leaderboard: 'who hit 40 HR?' or 'pitchers with ERA under 3.00'."""
    conn = _get_db()
    try:
        table = "season_pitching_stats" if is_pitching else "season_batting_stats"
        prefix = "sp" if is_pitching else "s"
        league_filter = f" AND {_league_team_clause(league, prefix)}" if league else ""
        league_label = f" ({league})" if league else ""

        # PA/IP minimum for rate stats
        pa_note = None
        qual_filter = ""
        if stat_info.is_rate:
            if is_pitching:
                qual_filter = f" AND {prefix}.ip_outs >= 486"
            else:
                cur = conn.cursor()
                cur.execute(f"SELECT MAX(games) FROM {table} WHERE season = ?", (season,))
                r = cur.fetchone()
                max_games = int(r[0]) if r and r[0] else 162
                pa_min = 400 if max_games >= 140 else 200
                qual_filter = f" AND {prefix}.plate_appearances >= {pa_min}"
                pa_note = f"Min. {pa_min} PA."

        cur = conn.cursor()
        cur.execute(
            f"SELECT p.name, {prefix}.{stat_info.db_column} "
            f"FROM {table} {prefix} "
            f"JOIN players p ON {prefix}.player_id = p.player_id "
            f"WHERE {prefix}.season = ? AND {prefix}.{stat_info.db_column} {comparison} ?{qual_filter}{league_filter} "
            f"ORDER BY {prefix}.{stat_info.db_column} DESC",
            (season, threshold),
        )
        rows = cur.fetchall()
        if not rows:
            threshold_str = _format_rate(str(threshold)) if stat_info.is_rate else str(int(threshold))
            op = "at least" if comparison == ">=" else "no more than"
            who = "pitchers" if is_pitching else "players"
            return f"No {who} had {op} {threshold_str} {stat_info.display_abbrev} in {season}{league_label}."

        threshold_display = _format_rate(str(threshold)) if stat_info.is_rate else str(int(threshold))

        if comparison == ">=":
            if stat_info.is_rate:
                title = f"Players Batting Over {threshold_display} {stat_info.display_abbrev} in {season}{league_label}"
            else:
                title = f"Players with {threshold_display}+ {stat_info.display_name} in {season}{league_label}"
        else:
            title = f"Players with {threshold_display} or Fewer {stat_info.display_name} in {season}{league_label}"

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

        return "\n".join(parts)
    finally:
        conn.close()


# ===================================================================
# 28. build_all_time_threshold
# ===================================================================

def build_all_time_threshold(stat_info: StatInfo, threshold: float, comparison: str,
                             is_pitching: bool = False,
                             league: Optional[str] = None,
                             since_year: Optional[int] = None) -> Optional[str]:
    """All-time threshold: 'who hit 50 home runs?' (no season specified)."""
    conn = _get_db()
    try:
        table = "season_pitching_stats" if is_pitching else "season_batting_stats"
        prefix = "sp" if is_pitching else "s"
        bad_era = _era_data_filter(prefix, stat_info) if is_pitching else ""
        league_filter = f" AND {_league_team_clause(league, prefix)}" if league else ""
        league_label = f" ({league})" if league else ""

        since_filter = f" AND {prefix}.season >= {since_year}" if since_year else ""

        cur = conn.cursor()
        cur.execute(
            f"SELECT p.name, {prefix}.{stat_info.db_column}, {prefix}.season "
            f"FROM {table} {prefix} "
            f"JOIN players p ON {prefix}.player_id = p.player_id "
            f"WHERE {prefix}.{stat_info.db_column} {comparison} ?{bad_era}{league_filter}{since_filter} "
            f"ORDER BY {prefix}.{stat_info.db_column} DESC",
            (threshold,),
        )
        rows = cur.fetchall()
        if not rows:
            threshold_str = _format_rate(str(threshold)) if stat_info.is_rate else str(int(threshold))
            op = "at least" if comparison == ">=" else "no more than"
            who = "pitchers" if is_pitching else "players"
            scope_msg = f"since {since_year}" if since_year else "in a season"
            return f"No {who} have had {op} {threshold_str} {stat_info.display_abbrev} {scope_msg}."

        threshold_display = _format_rate(str(threshold)) if stat_info.is_rate else str(int(threshold))
        who = "Pitchers" if is_pitching else "Players"
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
            qual_filter = f" AND {prefix}.plate_appearances >= 400"
        elif is_pitching and (rank_stat.is_rate or filter_stat.is_rate):
            qual_filter = f" AND {prefix}.ip_outs >= 486"

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
                      league: Optional[str] = None) -> Optional[str]:
    """
    Superlative+threshold: 'youngest to hit 50 HR', 'last player to bat .400'.

    superlative: one of "youngest", "oldest", "first", "last".
    """
    conn = _get_db()
    try:
        table = "season_pitching_stats" if is_pitching else "season_batting_stats"
        prefix = "sp" if is_pitching else "s"

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
            f"WHERE {prefix}.{stat_info.db_column} >= ?{birthdate_filter}{bad_era}{league_filter} "
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
        title = f"{sup_label} {who} with {threshold_display}+ {stat_info.display_name}{league_label}"

        has_age = superlative in ("youngest", "oldest")
        parts = [f"**{title}**\n"]
        parts.append("[TIP]Tap a player name for their full profile.[/TIP]")
        parts.append("[LEADERBOARD]")
        parts.append(f"HEADER: Year, {stat_info.display_abbrev}" + (", Age" if has_age else ""))
        for i, row in enumerate(rows):
            val = _format_rate(str(row[1])) if stat_info.is_rate else str(row[1])
            if has_age and len(row) > 3:
                parts.append(f"ROW {i+1}. {row[0]}: {row[2]}, {val}, age {row[3]}")
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
    """
    conn = _get_db()
    try:
        if streak_type == "hit":
            hit_condition = "hits > 0"
            streak_label = "Hitting"
        else:
            hit_condition = "(hits + walks + COALESCE(hit_by_pitch, 0)) > 0"
            streak_label = "On-Base"

        player_filter = ""
        if player_name:
            player_filter = f"AND p.name = ?"
        season_filter = ""
        if season:
            season_filter = f"AND g.season = {season}"

        params = [_sanitize(player_name)] if player_name else []

        cur = conn.cursor()
        cur.execute(
            f"WITH numbered AS ("
            f"  SELECT g.player_id, p.name, g.date, g.hits, g.walks, "
            f"    COALESCE(g.hit_by_pitch, 0) as hbp, g.season, "
            f"    ROW_NUMBER() OVER (PARTITION BY g.player_id ORDER BY g.date) as game_num "
            f"  FROM game_batting_logs g "
            f"  JOIN players p ON g.player_id = p.player_id "
            f"  WHERE 1=1 {player_filter} {season_filter}"
            f"), "
            f"qualifying AS ("
            f"  SELECT *, ROW_NUMBER() OVER (PARTITION BY player_id ORDER BY date) as qual_num "
            f"  FROM numbered "
            f"  WHERE {hit_condition}"
            f"), "
            f"streaks AS ("
            f"  SELECT player_id, name, "
            f"    COUNT(*) as streak_len, "
            f"    MIN(date) as start_date, "
            f"    MAX(date) as end_date, "
            f"    MIN(season) as season "
            f"  FROM qualifying "
            f"  GROUP BY player_id, game_num - qual_num"
            f") "
            f"SELECT name, streak_len, season, start_date, end_date "
            f"FROM streaks "
            f"ORDER BY streak_len DESC LIMIT 15",
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
                scope_label = "Since 2016"

        parts = [f"**Longest {streak_label} Streaks \u2014 {scope_label}**\n"]
        parts.append("[TIP]Tap a player name for their full profile.[/TIP]")
        parts.append("[LEADERBOARD]")
        parts.append("HEADER: Games, Season, Dates")
        for i, row in enumerate(rows):
            start = _format_date(str(row[3]))
            end = _format_date(str(row[4]))
            parts.append(f"ROW {i+1}. {row[0]}: {row[1]}, {row[2]}, {start}\u2013{end}")
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
            # Team overview sorted by OPS
            cur = conn.cursor()
            cur.execute(
                "SELECT p.name, s.games, s.batting_avg, s.home_runs, s.rbi, s.ops "
                "FROM season_batting_stats s "
                "JOIN players p ON s.player_id = p.player_id "
                f"WHERE {team_filter} AND s.season = ? AND s.plate_appearances >= 50 "
                "ORDER BY s.ops DESC LIMIT 15",
                tuple(team_params + [season]),
            )
            rows = cur.fetchall()
            if not rows:
                return f"No hitting data found for the {full_name} in {season}."

            parts = [f"**{full_name}** \u2014 {season} Hitters\n"]
            parts.append("[TIP]Tap a player name for their full profile.[/TIP]")
            parts.append("[LEADERBOARD]")
            parts.append("HEADER: G, AVG, HR, RBI, OPS")
            for i, row in enumerate(rows):
                avg = _format_rate(str(row[2]))
                ops = _format_rate(str(row[5]))
                parts.append(f"ROW {i+1}. {row[0]}: {row[1]}, {avg}, {row[3]}, {row[4]}, {ops}")
            parts.append("[/LEADERBOARD]")
            parts.append("\n_Min. 50 PA._")
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
            ip_filter = " AND sp.ip_outs >= 54" if stat_info.is_rate else ""
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
            # Team pitching overview sorted by ERA
            cur = conn.cursor()
            cur.execute(
                "SELECT p.name, sp.games, sp.innings_pitched, sp.wins, sp.losses, sp.era "
                "FROM season_pitching_stats sp "
                "JOIN players p ON sp.player_id = p.player_id "
                f"WHERE {team_filter} AND sp.season = ? AND sp.ip_outs >= 54 "
                "ORDER BY sp.era ASC LIMIT 15",
                tuple(team_params + [season]),
            )
            rows = cur.fetchall()
            if not rows:
                return f"No pitching data found for the {full_name} in {season}."

            parts = [f"**{full_name}** \u2014 {season} Pitchers\n"]
            parts.append("[TIP]Tap a player name for their full profile.[/TIP]")
            parts.append("[LEADERBOARD]")
            parts.append("HEADER: G, IP, W, L, ERA")
            for i, row in enumerate(rows):
                era = _format_pitching_rate(row[5], 2)
                parts.append(f"ROW {i+1}. {row[0]}: {row[1]}, {row[2]}, {row[3]}, {row[4]}, {era}")
            parts.append("[/LEADERBOARD]")
            parts.append("\n_Min. 18 IP._")
            parts.append(f"\n[SUGGEST]{nickname} ERA leaders[/SUGGEST]")
            parts.append(f"[SUGGEST]{nickname} strikeout leaders[/SUGGEST]")
            return "\n".join(parts)
    finally:
        conn.close()


# ===================================================================
# 37. build_team_total
# ===================================================================

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
    conn = _get_db()
    try:
        cur = conn.cursor()

        if stat_info.is_rate:
            rate_exprs = {
                "batting_avg": "CAST(SUM(s.hits) AS REAL) / SUM(s.at_bats)",
                "obp": ("CAST(SUM(s.hits + s.walks + s.hit_by_pitch) AS REAL) / "
                        "SUM(s.at_bats + s.walks + s.hit_by_pitch + s.sacrifice_flies)"),
                "slg": ("CAST(SUM(s.hits - s.doubles - s.triples - s.home_runs "
                         "+ 2*s.doubles + 3*s.triples + 4*s.home_runs) AS REAL) / SUM(s.at_bats)"),
                "ops": ("CAST(SUM(s.hits + s.walks + s.hit_by_pitch) AS REAL) / "
                        "SUM(s.at_bats + s.walks + s.hit_by_pitch + s.sacrifice_flies) + "
                        "CAST(SUM(s.hits - s.doubles - s.triples - s.home_runs "
                        "+ 2*s.doubles + 3*s.triples + 4*s.home_runs) AS REAL) / SUM(s.at_bats)"),
                "iso": ("CAST(SUM(s.hits - s.doubles - s.triples - s.home_runs "
                         "+ 2*s.doubles + 3*s.triples + 4*s.home_runs) AS REAL) / SUM(s.at_bats) "
                         "- CAST(SUM(s.hits) AS REAL) / SUM(s.at_bats)"),
            }
            select_expr = rate_exprs.get(
                stat_info.db_column,
                f"SUM(s.{stat_info.db_column} * s.plate_appearances) / SUM(s.plate_appearances)"
            )
            cur.execute(
                f"SELECT s.team, {select_expr} AS team_stat "
                f"FROM season_batting_stats s "
                f"WHERE s.season = ? AND s.plate_appearances >= 1 "
                f"GROUP BY s.team HAVING SUM(s.plate_appearances) >= 100 "
                f"ORDER BY team_stat DESC LIMIT 10",
                (season,),
            )
        else:
            cur.execute(
                f"SELECT s.team, SUM(s.{stat_info.db_column}) AS team_stat "
                f"FROM season_batting_stats s "
                f"WHERE s.season = ? "
                f"GROUP BY s.team ORDER BY team_stat DESC LIMIT 10",
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
            val = _format_rate(str(row[1])) if stat_info.is_rate else str(row[1])
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

        # Show the values per season in a compact grid if reasonable count
        if 2 <= count <= 20:
            parts.append("")
            parts.append("[STATGRID]")
            parts.append(f"HEADER: {stat_abbrev}")
            for season, val in rows:
                formatted = _format_stat_value(val, stat_abbrev)
                parts.append(f"ROW: {season}, {formatted}")
            parts.append("[/STATGRID]")

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
