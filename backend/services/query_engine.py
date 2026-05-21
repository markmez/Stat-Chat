"""
Structured Query Engine — decomposes natural language baseball queries into SQL.

Replaces pattern-matching interceptors with a general-purpose query composer
that knows the full database schema and can handle any combination of
stat + filters + scope + grouping.

Claude (Haiku/Sonnet) is the fallback for questions requiring knowledge
OUTSIDE the database (game events, historical context).
"""

import logging
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

from .name_matcher import (
    StatInfo, SplitContext, TeamContextFilter,
    match_stat, detect_season, detect_league,
    _detect_since_year, _detect_rookie, _detect_position,
    _detect_split_context, _detect_pitcher_role, _detect_team_context,
    is_pitching_stat, find_player_in_text, match_player,
    stat_alias_map, stat_fallback_alias_map, _extract_threshold,
    _POSITION_MAP, team_alias_map, _sorted_team_aliases,
)
from .qualification import min_pa as _qual_min_pa, min_ip_outs as _qual_min_ip_outs

logger = logging.getLogger("statchat.query_engine")

# Month name → number mapping
_MONTH_MAP = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6,
    "july": 7, "jul": 7, "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}


_ASG_DATES = {
    1933: "1933-07-06", 1934: "1934-07-10", 1935: "1935-07-08",
    1936: "1936-07-07", 1937: "1937-07-07", 1938: "1938-07-06",
    1939: "1939-07-11", 1940: "1940-07-09", 1941: "1941-07-08",
    1942: "1942-07-06", 1943: "1943-07-13", 1944: "1944-07-11",
    1946: "1946-07-09", 1947: "1947-07-08", 1948: "1948-07-13",
    1949: "1949-07-12", 1950: "1950-07-11", 1951: "1951-07-10",
    1952: "1952-07-08", 1953: "1953-07-14", 1954: "1954-07-13",
    1955: "1955-07-12", 1956: "1956-07-10", 1957: "1957-07-09",
    1958: "1958-07-08", 1959: "1959-07-07", 1960: "1960-07-11",
    1961: "1961-07-11", 1962: "1962-07-10", 1963: "1963-07-09",
    1964: "1964-07-07", 1965: "1965-07-13", 1966: "1966-07-12",
    1967: "1967-07-11", 1968: "1968-07-09", 1969: "1969-07-23",
    1970: "1970-07-14", 1971: "1971-07-13", 1972: "1972-07-25",
    1973: "1973-07-24", 1974: "1974-07-23", 1975: "1975-07-15",
    1976: "1976-07-13", 1977: "1977-07-19", 1978: "1978-07-11",
    1979: "1979-07-17", 1980: "1980-07-08", 1981: "1981-08-09",
    1982: "1982-07-13", 1983: "1983-07-06", 1984: "1984-07-10",
    1985: "1985-07-16", 1986: "1986-07-15", 1987: "1987-07-14",
    1988: "1988-07-12", 1989: "1989-07-11", 1990: "1990-07-10",
    1991: "1991-07-09", 1992: "1992-07-14", 1993: "1993-07-13",
    1994: "1994-07-12", 1995: "1995-07-11", 1996: "1996-07-09",
    1997: "1997-07-08", 1998: "1998-07-07", 1999: "1999-07-13",
    2000: "2000-07-11", 2001: "2001-07-10", 2002: "2002-07-09",
    2003: "2003-07-15", 2004: "2004-07-13", 2005: "2005-07-12",
    2006: "2006-07-11", 2007: "2007-07-10", 2008: "2008-07-15",
    2009: "2009-07-14", 2010: "2010-07-13", 2011: "2011-07-12",
    2012: "2012-07-10", 2013: "2013-07-16", 2014: "2014-07-15",
    2015: "2015-07-14", 2016: "2016-07-12", 2017: "2017-07-11",
    2018: "2018-07-17", 2019: "2019-07-09",
    2021: "2021-07-13", 2022: "2022-07-19", 2023: "2023-07-11",
    2024: "2024-07-16", 2025: "2025-07-15", 2026: "2026-07-14",
}


def _current_year() -> int:
    return date.today().year


def _detect_half_window(lower: str) -> Optional[dict]:
    """Detect "first half" / "second half" date window — either single-season
    (closed range within one year) or multi-season recurring (the equivalent
    window in each year of a multi-year scope).

    Returns one of:
      - {"kind": "first_half"|"second_half", "single_season": True,
         "since_date": "YYYY-MM-DD", "end_date": "YYYY-MM-DD"|None}
      - {"kind": "first_half"|"second_half", "single_season": False,
         "since_year": int}
      - None
    """
    import re
    is_first = bool(re.search(r"\b(?:in\s+the\s+)?first\s+half\b", lower))
    is_second = bool(re.search(r"\b(?:in\s+the\s+)?second\s+half\b", lower))
    if not (is_first or is_second):
        return None
    kind = "first_half" if is_first else "second_half"
    today = date.today()

    # Multi-season triggers: "first half of a season since YYYY", "first half
    # of any season since YYYY". The recurring-window executor will rebuild
    # the start/end dates per year using _ASG_DATES.
    multi_match = re.search(
        r"\b(?:of\s+(?:a|any|each|every)\s+season\s+)?since\s+((?:19|20)\d{2})\b", lower
    )
    if multi_match:
        return {"kind": kind, "single_season": False,
                "since_year": int(multi_match.group(1))}
    if re.search(r"\bof\s+(?:a|any|each|every)\s+season\b", lower):
        # "of a season" with no since-year — interpret as all-time recurring
        # (since 1933, the start of ASG records).
        return {"kind": kind, "single_season": False, "since_year": 1933}

    # Single-season — extract a 4-digit year if present, else default to current.
    year_match = re.search(r"\b((?:19|20)\d{2})\b", lower)
    year = int(year_match.group(1)) if year_match else today.year
    while year not in _ASG_DATES and year > 1933:
        year -= 1
    asg = _ASG_DATES.get(year, f"{year}-07-15")
    if kind == "first_half":
        return {"kind": "first_half", "single_season": True,
                "since_date": f"{year}-04-01", "end_date": asg}
    return {"kind": "second_half", "single_season": True,
            "since_date": asg, "end_date": None}


def _detect_since_date(lower: str) -> Optional[str]:
    """Detect sub-season date ranges: 'since June 16, 2025', 'since May 2025',
    'in the last 30 days', 'since the all-star break'.
    Returns 'YYYY-MM-DD' or None."""
    import re
    from datetime import timedelta

    today = date.today()

    # "in the last N days"
    m = re.search(r'\b(?:in|over)\s+the\s+last\s+(\d+)\s+days?\b', lower)
    if m:
        days = int(m.group(1))
        return (today - timedelta(days=days)).isoformat()

    # "since the all-star break" — use actual ASG dates (1933-2026, see _ASG_DATES above)
    if "all-star break" in lower or "all star break" in lower:
        # Check if user specified a year: "2024 all star break"
        year_match = re.search(r'\b((?:19|20)\d{2})\b', lower)
        if year_match:
            year = int(year_match.group(1))
        else:
            year = today.year if today.month >= 7 else today.year - 1
        # Skip years with no ASG (1945, 2020)
        while year not in _ASG_DATES and year > 1933:
            year -= 1
        return _ASG_DATES.get(year, f"{year}-07-15")

    # "since [Month] [day], [year]" or "since [Month] [day] [year]"
    # or "since [Month] [year]" or "since [Month] [day]"
    # "after"/"from" are treated as "since" synonyms — an open-ended lower
    # bound on the date range. (The rigid month parser used to grab the bare
    # month name here and drop the qualifier; this lets the query engine claim
    # "stats after June 30" as a real date slice instead.)
    since_match = re.search(
        r'\b(?:since|after|from)\s+([a-z]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?\s*,?\s*(\d{4})\b', lower
    )
    if since_match:
        month_str, day, year = since_match.group(1), int(since_match.group(2)), int(since_match.group(3))
        month = _MONTH_MAP.get(month_str)
        if month:
            return f"{year}-{month:02d}-{day:02d}"

    # "since [Month] [day]" (no year — assume current season)
    since_md = re.search(
        r'\b(?:since|after|from)\s+([a-z]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b', lower
    )
    if since_md:
        month_str, day = since_md.group(1), int(since_md.group(2))
        month = _MONTH_MAP.get(month_str)
        if month:
            year = today.year if month <= today.month else today.year - 1
            return f"{year}-{month:02d}-{day:02d}"

    # "since [Month] [year]" (no day — first of month)
    since_my = re.search(
        r'\b(?:since|after|from)\s+([a-z]+)\.?\s+(\d{4})\b', lower
    )
    if since_my:
        month_str, year = since_my.group(1), int(since_my.group(2))
        month = _MONTH_MAP.get(month_str)
        if month:
            return f"{year}-{month:02d}-01"

    # "since [Month]" (no day, no year — first of that month, current year)
    since_m = re.search(r'\b(?:since|after|from)\s+([a-z]+)\.?\s*$', lower)
    if not since_m:
        since_m = re.search(r'\b(?:since|after|from)\s+([a-z]+)\b', lower)
    if since_m:
        month_str = since_m.group(1)
        month = _MONTH_MAP.get(month_str)
        if month:
            year = today.year if month <= today.month else today.year - 1
            return f"{year}-{month:02d}-01"

    return None


def _detect_end_date(lower: str) -> Optional[str]:
    """Open UPPER bound from 'before/through/until/as of [date]'. Returns
    'YYYY-MM-DD' (inclusive — paired with `date <= end_date` in the executor)
    or None. Mirror of _detect_since_date for the upper bound; lets the query
    engine handle "OPS leaders before Aug 1" / "...through July 31"."""
    import re
    today = date.today()
    quals = r'(?:before|through|thru|until|up\s+to|as\s+of)'
    # "[qual] Month day, year" / "Month day year"
    m = re.search(rf'\b{quals}\s+([a-z]+)\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\s*,?\s*(\d{{4}})\b', lower)
    if m and m.group(1) in _MONTH_MAP:
        return f"{int(m.group(3))}-{_MONTH_MAP[m.group(1)]:02d}-{int(m.group(2)):02d}"
    # "[qual] Month day" (no year — infer current season)
    m = re.search(rf'\b{quals}\s+([a-z]+)\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b', lower)
    if m and m.group(1) in _MONTH_MAP:
        mo = _MONTH_MAP[m.group(1)]
        yr = today.year if mo <= today.month else today.year - 1
        return f"{yr}-{mo:02d}-{int(m.group(2)):02d}"
    return None


def _detect_month_range(lower: str) -> Optional[tuple]:
    """Detect bare month references as closed date ranges.

    Catches patterns like:
      "in April 2025"      → ("2025-04-01", "2025-04-30")
      "in April last year" → ("2025-04-01", "2025-04-30")  (current=2026)
      "in April this year" → ("2026-04-01", "2026-04-30")
      "during May 2024"    → ("2024-05-01", "2024-05-31")
      "in June"            → infer year, return month range

    Returns (since_date, end_date) tuple, both YYYY-MM-DD, or None.

    Distinct from _detect_since_date (open-ended "since X") and
    parse_month_query (single-player "Judge in May 2024"). This one
    handles leaderboard queries where the month is the time window:
    "who had the most RBI in April last year".
    """
    from datetime import timedelta
    today = date.today()

    # Year resolver: explicit YYYY, "last year", "this year", "two years ago"
    def _resolve_year(text: str) -> Optional[int]:
        m = re.search(r'\b(20[012]\d|19\d{2})\b', text)
        if m:
            return int(m.group(1))
        if "last year" in text or "last season" in text:
            return today.year - 1
        if "this year" in text or "this season" in text or "current season" in text:
            return today.year
        if "two years ago" in text or "2 years ago" in text:
            return today.year - 2
        if "three years ago" in text or "3 years ago" in text:
            return today.year - 3
        return None

    # Find a month name in the query. Use word boundary to avoid matching
    # "may" inside "many" / "april" inside something fluky.
    month_word = None
    month_num = None
    for name, num in _MONTH_MAP.items():
        if re.search(rf'\b{name}\b', lower):
            month_word = name
            month_num = num
            break

    if month_num is None:
        return None

    # Don't fire when the query is a "since" pattern — that's
    # _detect_since_date's job (open-ended), not a closed range.
    if re.search(rf'\bsince\s+{month_word}\b', lower):
        return None

    # Don't fire when there's a specific day attached: "April 15" / "April
    # 15 2024" should be a date_range starting from that exact day, which
    # _detect_since_date handles (or in a similar pattern).
    if re.search(rf'\b{month_word}\.?\s+\d{{1,2}}(?:st|nd|rd|th)?\b', lower):
        return None

    # Resolve year — explicit, relative, or fallback.
    year = _resolve_year(lower)
    if year is None:
        # No year context. Default: most recent occurrence of that month.
        # If we're past it this year, use this year; otherwise last year.
        year = today.year if month_num <= today.month else today.year - 1

    # Compute last day of month
    if month_num == 12:
        next_first = date(year + 1, 1, 1)
    else:
        next_first = date(year, month_num + 1, 1)
    last_day = (next_first - timedelta(days=1)).day

    return (f"{year}-{month_num:02d}-01", f"{year}-{month_num:02d}-{last_day:02d}")


DB_PATH = os.getenv(
    "DB_PATH",
    os.path.join(os.path.dirname(__file__), "..", "..", "baseball_stats_full.db"),
)

# ---------------------------------------------------------------------------
# Schema knowledge — what columns exist in which tables
# ---------------------------------------------------------------------------

# Columns available in season_batting_stats
_SEASON_BAT_COLS = {
    "games", "plate_appearances", "at_bats", "hits", "doubles", "triples",
    "home_runs", "runs", "rbi", "stolen_bases", "caught_stealing",
    "walks", "strikeouts", "hit_by_pitch", "sacrifice_flies", "intentional_walks",
    "batting_avg", "obp", "slg", "ops", "iso", "babip", "ops_plus",
}

# Columns available in season_pitching_stats
_SEASON_PITCH_COLS = {
    "games", "games_started", "games_finished", "complete_games",
    "wins", "losses", "saves", "quality_starts",
    "ip_outs", "innings_pitched",
    "hits", "runs", "earned_runs", "home_runs", "walks", "intentional_walks",
    "strikeouts", "hit_by_pitch", "wild_pitches", "balks", "batters_faced",
    "sacrifice_hits", "sacrifice_flies", "stolen_bases", "caught_stealing",
    "era", "whip", "k_per_9", "bb_per_9", "k_per_bb", "h_per_9", "hr_per_9", "baa",
    "era_plus",
}

# Columns in game_batting_logs
_GAME_BAT_COLS = {
    "date", "opponent", "vishome",
    "plate_appearances", "at_bats", "hits", "doubles", "triples", "home_runs",
    "runs", "rbi", "walks", "strikeouts", "stolen_bases",
    "batting_avg", "obp", "slg", "ops",
}

# Columns in game_pitching_logs
_GAME_PITCH_COLS = {
    "date", "opponent", "vishome", "is_start",
    "ip_outs", "innings_pitched",
    "hits", "runs", "earned_runs", "home_runs", "walks", "strikeouts",
    "hit_by_pitch", "batters_faced",
    "win", "loss", "save", "era",
}

# Columns in split tables (all batting splits share this schema)
_SPLIT_BAT_COLS = {
    "plate_appearances", "at_bats", "hits", "doubles", "triples", "home_runs",
    "rbi", "walks", "strikeouts", "hit_by_pitch", "sacrifice_flies",
    "batting_avg", "obp", "slg", "ops", "iso", "babip",
}

# Derivable stats — formulas from existing columns
_DERIVED_STATS = {
    "total_bases": {
        "formula": "(s.hits - s.doubles - s.triples - s.home_runs) + 2*s.doubles + 3*s.triples + 4*s.home_runs",
        "display": "TB",
        "name": "Total Bases",
        "is_rate": False,
        "requires": {"hits", "doubles", "triples", "home_runs"},
    },
    "extra_base_hits": {
        "formula": "s.doubles + s.triples + s.home_runs",
        "display": "XBH",
        "name": "Extra Base Hits",
        "is_rate": False,
        "requires": {"doubles", "triples", "home_runs"},
    },
    "sb_percentage": {
        "formula": "CAST(s.stolen_bases AS REAL) / NULLIF(s.stolen_bases + s.caught_stealing, 0) * 100",
        "display": "SB%",
        "name": "Stolen Base Percentage",
        "is_rate": True,
        "requires": {"stolen_bases", "caught_stealing"},
    },
    "k_percentage": {
        "formula": "CAST(s.strikeouts AS REAL) / NULLIF(s.plate_appearances, 0) * 100",
        "display": "K%",
        "name": "Strikeout Percentage",
        "is_rate": True,
        "requires": {"strikeouts", "plate_appearances"},
    },
    "bb_percentage": {
        "formula": "CAST(s.walks AS REAL) / NULLIF(s.plate_appearances, 0) * 100",
        "display": "BB%",
        "name": "Walk Percentage",
        "is_rate": True,
        "requires": {"walks", "plate_appearances"},
    },
    "fip": {
        "formula": "(13*s.home_runs + 3*(s.walks + s.hit_by_pitch) - 2*s.strikeouts) / NULLIF(s.ip_outs / 3.0, 0) + 3.10",
        "display": "FIP",
        "name": "FIP",
        "is_rate": True,
        "requires": {"home_runs", "walks", "hit_by_pitch", "strikeouts", "ip_outs"},
    },
}

# Rate stats that need PA/IP minimums
_RATE_STATS = {
    "batting_avg", "obp", "slg", "ops", "iso", "babip", "ops_plus",
    "era", "whip", "k_per_9", "bb_per_9", "k_per_bb", "h_per_9", "hr_per_9", "baa", "era_plus",
}

# Pitching stats where lower is better
_LOWER_IS_BETTER = {"era", "whip", "bb_per_9", "h_per_9", "hr_per_9", "baa", "earned_run_avg"}

# Stop words — these are always ignored in the unexplained-words check
# Max stat columns in leaderboard output — iOS overflows beyond 4.
# Matches the cap in query.py's Haiku SQL formatter.
_MAX_DISPLAY_COLS = 4

_STOP_WORDS = {
    "the", "a", "an", "in", "of", "by", "for", "to", "and", "or", "with",
    "who", "what", "which", "how", "many", "much", "did", "do", "does", "has",
    "had", "have", "is", "was", "were", "are", "been", "be",
    "that", "this", "than", "then", "from", "at", "on", "it", "its",
    "all", "any", "each", "every", "some", "no", "not",
    "player", "players", "pitcher", "pitchers", "hitter", "hitters",
    "batter", "batters", "team", "teams",
    "time", "times", "season", "seasons", "year", "years",
    "hit", "got", "get", "scored", "threw", "pitched", "batted",
    "ever", "just", "also", "over", "under", "least", "more", "most",
    "them", "their", "those", "these", "there",
    "game", "games",  # consumed by context
    "played", "play", "playing",
    "hit", "hitting", "batted", "batting",
    "won", "win", "winning",
    "stole", "stolen", "stealing", "bases",
    "pas", "abs", "plate", "appearance", "appearances",
    "hits", "runs", "any", "multiple", "each", "allowed", "pitched", "fewer",
    "threw", "thrown", "throwing",
    "pitched", "pitching",
    "drove", "driven",
    "struck", "walked", "walking",
    "scored", "allowed", "given", "gave",
    "during", "when", "where", "only",
    "ago", "back", "since", "sub", "among",
    "between", "across", "through", "around", "about", "within",
    "per", "into", "both", "either", "but", "so", "yet",
    "while", "above", "below", "without", "like",
    "really", "actually", "currently", "recently",
    "appeared", "appearing", "recorded", "posted", "put",
    "league", "led", "leading", "leads", "leader", "leaders",
    "outing", "outings", "start", "starts", "appearance", "appearances",
    "baseman", "basemen", "fielder", "fielders",
    # "qualified" is redundant — every rate-stat leaderboard already enforces
    # minimum PA / IP. "Worst ERA among qualified starters" means the same as
    # "worst ERA among starters". Don't bail out on it.
    "qualified", "qualifying", "qualify",
    "?", "!", ".",
}


# ---------------------------------------------------------------------------
# QueryPlan — the decomposed query
# ---------------------------------------------------------------------------

@dataclass
class QueryPlan:
    """Structured representation of a decomposed query."""
    # What stat to query
    stat: Optional[StatInfo] = None
    derived_stat: Optional[str] = None  # key into _DERIVED_STATS

    # Where to look
    scope: str = "current_season"  # "current_season", "all_time", "career", "since_YYYY", "date_range"
    season: Optional[int] = None
    since_year: Optional[int] = None
    end_year: Optional[int] = None  # For decade ranges: "last decade" = 2010-2019
    since_date: Optional[str] = None  # "YYYY-MM-DD" for sub-season date ranges
    end_date: Optional[str] = None    # "YYYY-MM-DD" for closed sub-season ranges (e.g. "in the first half" = season opener → ASG break)
    recurring_half: Optional[str] = None  # "first_half" | "second_half" — multi-season recurring window combined with since_year
    month_grouped: bool = False  # group game logs by (player, year, month) — "best HR in a single month all time"
    # Game-window scope: "first/last N (career) games" — aggregated from the
    # first/last N rows of each player's chronologically-ordered game logs.
    # Shape: {"direction": "first"|"last", "n": int,
    #         "scope": "season"|"career"|"recent"}
    career_game_window: Optional[dict] = None

    # Filters
    league: Optional[str] = None
    position: Optional[list] = None
    bats: Optional[str] = None  # "L", "R", "B"
    throws: Optional[str] = None  # "L", "R"
    rookie: bool = False
    pitcher_role: Optional[str] = None  # "starter", "reliever"
    age_max: Optional[int] = None
    age_min: Optional[int] = None
    split_context: Optional[SplitContext] = None
    team_context: Optional["TeamContextFilter"] = None
    team_code: Optional[str] = None
    active_only: bool = False
    player_name: Optional[str] = None  # Filter results to a specific player

    # Query shape
    query_type: str = "leaderboard"  # "leaderboard", "threshold", "count", "superlative", "game_log_count", "game_log_extreme", "team_ranking", "per_team_leaders"
    threshold: Optional[float] = None
    comparison: str = ">="  # ">=" or "<="
    sort_asc: bool = False
    limit: int = 50
    superlative: Optional[str] = None  # "youngest", "oldest", "first", "last"
    game_log_stat: Optional[str] = None  # for game-log queries: column in game logs
    game_log_threshold: Optional[int] = None  # for game-log counting: hits >= N

    # Multi-threshold filters (e.g., ".300 with 30 HR")
    extra_filters: list = field(default_factory=list)  # [{stat, threshold, comparison}]

    # Multi-season consistency ("50+ games in each of the last 3 seasons")
    per_season: bool = False  # condition must hold in EVERY season individually
    season_count: Optional[int] = None  # number of consecutive seasons required

    # Sequential game-log queries ("hit in N consecutive games", "reached base in first 10 PAs")
    streak_conditions: list = field(default_factory=list)  # List of SQL conditions per game, ANDed together
    streak_condition_labels: list = field(default_factory=list)  # Display labels for each condition
    streak_length: Optional[int] = None  # N consecutive games
    streak_direction: Optional[str] = None  # "leading" (from start), "sliding" (anywhere), "trailing" (current)

    # Sliding-window stretch ("most X in an N-game stretch") for one player
    sliding_window_n: Optional[int] = None  # window size in consecutive games

    # Validation
    is_pitching: bool = False
    ambiguous_stat: bool = False  # True when stat exists in both batting and pitching
    has_team_context: bool = False  # "team" was in the query — might mean team-level aggregate
    original_question: str = ""  # Original query text for post-execution logic
    execution_error: Optional[str] = None  # Set by execute() if an exception occurred
    compare_years: Optional[tuple] = None  # (year1, year2) for year-over-year comparison
    award_filter: Optional[str] = None  # "MVP", "CY", "ROY", "ALL_STAR", "GG", "SS", "HOF"
    award_filter_secondary: Optional[str] = None  # For "won X and Y in the same season" intersection
    unexplained_words: list = field(default_factory=list)
    consumed_words: set = field(default_factory=set)

    @property
    def is_valid(self) -> bool:
        """A plan is valid if we have a stat, no unexplained words, and it's a type we handle."""
        if self.query_type in ("definition", "multi_threshold"):
            return False  # Handled by specialized parsers
        if self.query_type == "award_lookup":
            return self.award_filter is not None
        if self.query_type == "award_intersection":
            return self.award_filter is not None and self.award_filter_secondary is not None
        if self.query_type == "streak_sequence":
            return len(self.streak_conditions) > 0 and len(self.unexplained_words) == 0
        if self.query_type == "year_comparison":
            return self.player_name is not None and self.compare_years is not None and len(self.unexplained_words) == 0
        if self.query_type == "player_sliding_window":
            return (self.player_name is not None and self.stat is not None
                    and self.sliding_window_n is not None
                    and len(self.unexplained_words) == 0)
        return (self.stat is not None or self.derived_stat is not None) and len(self.unexplained_words) == 0


# ---------------------------------------------------------------------------
# Stat condition parser — finds stat + threshold from natural language
# ---------------------------------------------------------------------------

@dataclass
class StatCondition:
    """A single stat + threshold condition parsed from natural language."""
    stat: Optional[StatInfo]
    derived: Optional[str]  # key into _DERIVED_STATS
    threshold: Optional[float]
    comparison: str  # ">=" or "<="
    consumed_text: str  # the text this condition consumed

    @property
    def is_rate(self) -> bool:
        if self.stat:
            return self.stat.is_rate
        if self.derived:
            return _DERIVED_STATS[self.derived]["is_rate"]
        return False


def _parse_stat_condition(text: str) -> Optional[StatCondition]:
    """Parse a stat + threshold from a text segment.

    Handles all natural language patterns:
    - Direct: "30 HR", "200+ K", ".300 AVG", "3.00 ERA"
    - Verb forms: "batted .300", "hit 300", "hitting .350"
    - Sub pattern: "sub-3.00 ERA", "sub 2.50 WHIP"
    - Won/stole/drove: "won 20 games", "stole 50 bases", "drove in 100 runs"
    - Struck out: "struck out 200"
    - Implicit: just "batted .300" → batting avg >= .300
    """
    lower = text.strip().lower()

    # Determine comparison direction
    under_patterns = ["under ", "fewer than ", "less than ", "below ",
                      "sub-", "sub ", "no more than ", "or fewer", "or less"]
    comparison = "<=" if any(p in lower for p in under_patterns) else ">="

    # Check derived stats first
    derived_triggers = {
        "total bases": "total_bases",
        "extra base hits": "extra_base_hits", "extra base hit": "extra_base_hits",
        "extra-base hits": "extra_base_hits", "xbh": "extra_base_hits",
        "stolen base percentage": "sb_percentage", "sb%": "sb_percentage",
    }
    for trigger, key in sorted(derived_triggers.items(), key=lambda x: len(x[0]), reverse=True):
        if trigger in lower:
            threshold = _extract_threshold(lower)
            return StatCondition(None, key, threshold, comparison, trigger)

    # --- Step 1: Check for "verb + number" patterns ---
    # These indicate the verb is describing an action, not a stat name.
    # e.g., "hit 30 HR" → verb "hit", threshold 30, stat HR
    # e.g., "hit sub .250" → verb "hit", threshold .250, stat batting_avg (inferred)
    # e.g., "stole 50 bases" → verb "stole", threshold 50, stat stolen_bases

    stat = None
    verb_number = re.search(
        r'\b(hit|batted|batting|hitting|won|stole|stolen|drove\s+in|struck\s+out|walked|threw|pitched)'
        r'\s+(?:over\s+|above\s+|at least\s+|sub[- ]?)?'
        r'(\.?\d+\.?\d*)\+?',
        lower
    )

    if verb_number:
        verb = verb_number.group(1).strip()
        number = float(verb_number.group(2))

        # Some verbs have a fixed meaning regardless of what follows
        _verb_to_stat = {
            "won": "wins",
            "stole": "stolen bases", "stolen": "stolen bases",
            "drove in": "rbi",
            "struck out": "strikeouts",
            "walked": "walks",
        }

        stat_after = None  # Initialize before branches
        if verb in _verb_to_stat:
            # Verb has a fixed stat meaning
            stat = stat_alias_map.get(_verb_to_stat[verb])
        else:
            # "hit/batted/pitched/threw" — look for a stat keyword AFTER the number
            after_number = lower[verb_number.end():].strip()
            stat_after = match_stat(after_number) if after_number else None

            if stat_after:
                # "hit 30 HR" → stat is HR, threshold is 30
                stat = stat_after
            elif verb in ("hit", "batted", "batting", "hitting"):
                # No stat after number → infer batting average
                # "hit .300" / "batted 300" / "hit sub .250"
                stat = stat_alias_map.get("batting average") or stat_alias_map.get("avg")
            elif verb in ("threw", "pitched"):
                stat = stat_alias_map.get("innings pitched") or stat_alias_map.get("ip")

        if stat:
            threshold = _extract_threshold(lower, stat=stat)
            if threshold is not None:
                # Consumed: the verb, number, and stat keyword
                consumed = verb_number.group(0)
                if stat_after:
                    for alias in sorted(stat_alias_map.keys(), key=len, reverse=True):
                        if alias in after_number:
                            consumed += " " + alias
                            break
                return StatCondition(stat, None, threshold, comparison, consumed)

    # --- Step 2: Check for "sub + number" without a verb ---
    # "sub-.250 AVG", "sub 3.00 ERA", "sub .250" (infer batting avg)
    sub_match = re.search(r'\bsub[- ]?(\.?\d+\.?\d*)', lower)
    if sub_match:
        after_sub = lower[sub_match.end():].strip()
        stat_after = match_stat(after_sub) if after_sub else None
        threshold = float(sub_match.group(1))
        if stat_after:
            # "sub-3.00 ERA" → ERA <= 3.00
            if stat_after.db_column in ("batting_avg", "obp", "slg", "ops", "iso", "babip") \
                    and 100 <= threshold <= 999:
                threshold = threshold / 1000
            return StatCondition(stat_after, None, threshold, "<=", text)
        else:
            # "sub .250" / "sub 250" with no stat keyword → infer batting average
            if threshold < 1 or (100 <= threshold <= 500):
                if 100 <= threshold <= 500:
                    threshold = threshold / 1000
                avg_stat = stat_alias_map.get("batting average") or stat_alias_map.get("avg")
                if avg_stat:
                    return StatCondition(avg_stat, None, threshold, "<=", text)

    # --- Step 3: Direct stat match (no verb pattern) ---
    # "30 HR", "HR leaders", ".800 OPS", "200+ K", "200k"
    if not stat:
        # Normalize: strip +, add space between number and letter ("200k" → "200 k", "200+ K" → "200 K")
        cleaned = re.sub(r'(\d)\+', r'\1 ', lower)
        cleaned = re.sub(r'(\d)([a-z])', r'\1 \2', cleaned)
        stat = match_stat(cleaned)

    if not stat:
        # Last resort: if there's a rate-looking number (.XXX or 0.XXX) with no stat,
        # infer batting average. Handles ".300 hitters", ".400 club", etc.
        rate_match = re.search(r'\.(\d{3})\b', lower)
        if rate_match:
            val = float("0." + rate_match.group(1))
            if 0.1 <= val <= 0.5:  # batting average range
                avg_stat = stat_alias_map.get("batting average") or stat_alias_map.get("avg")
                if avg_stat:
                    return StatCondition(avg_stat, None, val, ">=", text)
        return None

    # Build consumed text from just the matched stat alias + threshold.
    # Only consider aliases that resolve to the SAME stat match_stat returned —
    # otherwise "top batters by strikeouts" would consume "top batters" (an OPS
    # fallback) when SO was actually the matched stat. Use word boundaries so
    # "hit" inside "hitter" doesn't falsely consume.
    same_stat_aliases = [
        a for a, info in stat_alias_map.items()
        if info.db_column == stat.db_column
    ] + [
        a for a, info in stat_fallback_alias_map.items()
        if info.db_column == stat.db_column
    ]
    consumed_parts = []
    for alias in sorted(same_stat_aliases, key=len, reverse=True):
        if re.search(rf'\b{re.escape(alias)}\b', lower):
            consumed_parts.append(alias)
            break

    threshold = _extract_threshold(lower, stat=stat)
    if threshold is not None:
        consumed_parts.append(str(threshold))
    consumed = " ".join(consumed_parts)

    if threshold is None:
        return StatCondition(stat, None, None, ">=", consumed)

    return StatCondition(stat, None, threshold, comparison, consumed)


# ---------------------------------------------------------------------------
# Decompose — parse the query into a QueryPlan
# ---------------------------------------------------------------------------

def _add_consumed(plan: QueryPlan, words: str):
    """Mark words as consumed."""
    for w in words.lower().split():
        plan.consumed_words.add(w.strip("?.!,"))


def decompose(question: str) -> QueryPlan:
    """Decompose a natural language query into a structured QueryPlan."""
    plan = QueryPlan()
    plan.original_question = question.strip()
    lower = question.strip().lower()

    # --- Early team-context detection ---
    # Team-game-context phrases ("in extra innings", "when team had won 40
    # games") contain words that ARE stat aliases ("innings", "won"). If we
    # let match_stat run first it'd misidentify the stat. Detect + strip the
    # phrase from `lower` here so all subsequent parsing sees a clean query.
    plan.team_context = _detect_team_context(lower)
    if plan.team_context:
        for phrase in plan.team_context.consumed_phrases:
            if phrase in lower:
                lower = lower.replace(phrase, " ")
                _add_consumed(plan, phrase)
        # Collapse extra whitespace introduced by the strip
        lower = " ".join(lower.split())

    # --- Early split-context detection ---
    # Split-context phrases ("first at bat", "3rd time through the order") also
    # contain words that ARE stat aliases ("at bat" → AB, "time" → time-on-base
    # nope but still). Detect + strip the phrase from `lower` here so match_stat
    # later sees a clean remainder. Without this, "best hitter in 1st at bat"
    # would match AB (at-bats) instead of falling through to the OPS fallback.
    plan.split_context = _detect_split_context(lower)
    if plan.split_context:
        for phrase in plan.split_context.consumed_phrases:
            if phrase in lower:
                lower = lower.replace(phrase, " ")
                _add_consumed(plan, phrase)
        lower = " ".join(lower.split())
        if plan.split_context.is_pitching:
            plan.is_pitching = True

    # --- Early detection: stat definitions ---
    # "what is OPS", "explain BABIP", "define ERA" — not a DB query
    definition_triggers = ["what is ", "what's ", "explain ", "define ", "what does ", "how is ", "how do you calculate"]
    if any(lower.startswith(t) or t in lower for t in definition_triggers):
        # Check if there's a stat keyword and no leaderboard/threshold trigger
        has_leaderboard = any(t in lower for t in ["leaders", "leader", "most", "best", "top", "highest", "lowest"])
        if not has_leaderboard:
            plan.query_type = "definition"
            return plan  # Let the old stat definition parser handle it

    # --- Pure award lookup ---
    # "who won MVP last year", "2024 Cy Young winner", "all-star selections for Judge"
    _AWARD_LOOKUP_PATTERNS = {
        "mvp": "MVP", "cy young": "CY", "rookie of the year": "ROY", "roy": "ROY",
        "all-star": "ALL_STAR", "all star": "ALL_STAR", "allstar": "ALL_STAR",
        "gold glove": "GG", "silver slugger": "SS",
        "hall of fame": "HOF", "hof": "HOF",
    }
    # Only match if query is ABOUT the award itself, not using award as a filter on stats
    # e.g., "who won MVP" = award lookup; "most HR by an MVP" = stat leaderboard with award filter
    _has_stat_keyword = bool(re.search(
        r'\b(?:home runs?|hr|rbi|hits?|avg|ops|obp|slg|era|whip|strikeouts?|stolen bases?|wins?|saves?|innings)\b',
        lower))

    # --- Two-award same-season intersection ---
    # "every player who has won both MVP and Gold Glove in the same season",
    # "MVP and Gold Glove same year", "won X and Y in the same season".
    # Trigger requires both award names AND a same-season qualifier — without
    # the qualifier, "won X and Y" could mean career-spanning and we leave
    # that to the single-award lookup (which picks one and answers it).
    if not _has_stat_keyword and ("same season" in lower or "same year" in lower):
        matched_codes: list = []
        seen_codes: set = set()
        for award_text, award_code in sorted(_AWARD_LOOKUP_PATTERNS.items(), key=lambda x: -len(x[0])):
            if award_text in lower and award_code not in seen_codes:
                seen_codes.add(award_code)
                matched_codes.append(award_code)
                if len(matched_codes) == 2:
                    break
        if len(matched_codes) == 2:
            plan.query_type = "award_intersection"
            plan.award_filter = matched_codes[0]
            plan.award_filter_secondary = matched_codes[1]
            # Optional season scope ("in 2024", "this year")
            year_match = re.search(r'\b(1[89]\d{2}|20[0-3]\d)\b', lower)
            if year_match:
                plan.season = int(year_match.group(1))
            elif "last year" in lower or "last season" in lower:
                plan.season = _current_year() - 1
            elif "this year" in lower or "this season" in lower:
                plan.season = _current_year()
            if re.search(r'\bal\b', lower):
                plan.league = "AL"
            elif re.search(r'\bnl\b', lower):
                plan.league = "NL"
            return plan

    if not _has_stat_keyword:
        for award_text, award_code in sorted(_AWARD_LOOKUP_PATTERNS.items(), key=lambda x: -len(x[0])):
            if award_text in lower:
                plan.query_type = "award_lookup"
                plan.award_filter = award_code
                # Extract season
                year_match = re.search(r'\b(1[89]\d{2}|20[0-3]\d)\b', lower)
                if year_match:
                    plan.season = int(year_match.group(1))
                elif "last year" in lower or "last season" in lower:
                    plan.season = _current_year() - 1
                elif "this year" in lower or "this season" in lower:
                    plan.season = _current_year()
                # Extract league
                if re.search(r'\bal\b', lower):
                    plan.league = "AL"
                elif re.search(r'\bnl\b', lower):
                    plan.league = "NL"
                # Extract player name for "how many X does Judge have"
                from services import name_matcher as _nm
                player = _nm.find_player_in_text(lower)
                if player:
                    plan.player_name = player
                # is_valid is a @property; QueryPlan.is_valid returns True
                # when query_type == "award_lookup" and award_filter is set.
                return plan

    # --- Multi-stat detection ---
    # Split on separators and parse each segment for stat+threshold.
    # This handles ".300 with 30 HR", "200 K and sub-3.00 ERA", etc.
    _separators = [" with ", " and ", " while ", " plus "]
    has_separator = any(s in lower for s in _separators)
    all_conditions: list[StatCondition] = []

    if has_separator:
        temp = lower
        for s in _separators:
            temp = temp.replace(s, " |SEP| ")
        segments = [p.strip() for p in temp.split("|SEP|") if p.strip()]

        for seg in segments:
            # Strip career-window phrases so "10" in "first 10 at-bats" or
            # "first 3 starts" doesn't get pulled in as a stat threshold by
            # _parse_stat_condition. Without this, "players with hits in
            # each of their first 10 at-bats" builds a bogus threshold of
            # 10 from the window N. The whole-query path (no separator)
            # already does this strip; mirror it here for parity.
            seg = re.sub(
                r'\b(first|last)\s+\d+\s+(?:career\s+)?'
                r'(?:games?|starts?|at[- ]?bats?|abs?|appearances)\b',
                ' ', seg,
            )
            cond = _parse_stat_condition(seg)
            if cond:
                all_conditions.append(cond)

    # --- Detect league (modifies the text) ---
    league_result = detect_league(lower)
    if league_result:
        plan.league = league_result[0]
        lower = league_result[1]
        _add_consumed(plan, "al nl american league national league")

    # --- Detect query type triggers ---
    # Superlative
    if "youngest" in lower or "how young" in lower:
        plan.superlative = "youngest"
        plan.query_type = "superlative"
        _add_consumed(plan, "youngest how young")
    elif "oldest" in lower or "how old" in lower:
        plan.superlative = "oldest"
        plan.query_type = "superlative"
        _add_consumed(plan, "oldest how old")
    elif any(p in lower for p in ("first player", "first to", "who was the first", "first person")):
        plan.superlative = "first"
        plan.query_type = "superlative"
        _add_consumed(plan, "first player first to who was the first first person")
    elif any(p in lower for p in ("last player", "last to", "most recent", "last time someone", "when was the last", "last person")):
        plan.superlative = "last"
        plan.query_type = "superlative"
        _add_consumed(plan, "last player last to most recent last time someone when was the last last person")

    # Count query
    count_triggers = ["how many player", "how many pitcher", "how many batter", "how many hitter"]
    if any(t in lower for t in count_triggers):
        plan.query_type = "count"
        _add_consumed(plan, "how many players pitchers batters hitters")

    # Per-team individual leaders (must check BEFORE team_ranking and leaderboard triggers)
    # "on each team" / "per team" / "by team" / "for each team"
    per_team_triggers = ["on each team", "per team", "for each team", "by team",
                         "each team", "every team"]
    if any(p in lower for p in per_team_triggers):
        plan.query_type = "per_team_leaders"
        _add_consumed(plan, "on each per for by every team teams")
    # Team ranking: "what team" / "which team"
    elif any(t in lower for t in ["what team", "which team", "what teams", "which teams"]):
        plan.query_type = "team_ranking"
        _add_consumed(plan, "what which team teams")
    # Bare "team [stat]" without specific team name → team ranking with alternate pill.
    # Skip when a team-context filter is set (e.g. "on a team with a losing record"):
    # that filter is the reason "team" appears in the query, and the intent is a
    # player leaderboard filtered by team game context, not a team-level ranking.
    elif re.search(r'\bteam\b', lower) and not plan.team_code and not plan.team_context:
        plan.query_type = "team_ranking"
        plan.has_team_context = True
        _add_consumed(plan, "team teams")

    # Sort direction (worst/fewest invert default sort order)
    if any(t in lower for t in ["worst", "fewest"]):
        plan.sort_asc = True
        _add_consumed(plan, "worst fewest")
    # Leaderboard intent — any ranking trigger. "worst" and "fewest" are
    # included here so they activate leaderboard mode (they already set
    # sort_asc above; without leaderboard-mode the plan would be invalid
    # and the query falls through to Sonnet, producing "I'm not sure"
    # for queries like "worst hitters first at bat").
    if any(t in lower for t in ["best", "highest", "most", "top", "leaders", "leader",
                                  "leaderboard", "lowest", "who led", "who leads", "leading",
                                  "worst", "fewest"]):
        if plan.query_type not in ("count", "superlative", "team_ranking", "per_team_leaders"):
            plan.query_type = "leaderboard"
        _add_consumed(plan, "best highest most top leaders leader leaderboard lowest who led leads leading worst fewest")

    # "Lowest" for rate stats is actually best (not worst)
    if "lowest" in lower:
        # Will be resolved after stat is known
        _add_consumed(plan, "lowest")

    # --- Detect stat(s) ---
    # If we found multiple conditions from separator splitting, use those.
    # Otherwise, parse the whole query as a single condition.
    if len(all_conditions) >= 2:
        # Multiple stat+threshold pairs found.
        # Determine which is the rank stat (no threshold or has a ranking signal)
        # and which are filters (have thresholds).
        rank_cond = None
        filter_conds = []

        for cond in all_conditions:
            if cond.threshold is None:
                rank_cond = cond  # stat without threshold = rank by this
            else:
                filter_conds.append(cond)

        if rank_cond:
            # Filtered leaderboard: rank by one stat, filter by others
            plan.stat = rank_cond.stat
            plan.derived_stat = rank_cond.derived
            for fc in filter_conds:
                plan.extra_filters.append({
                    "stat": fc.stat, "threshold": fc.threshold, "comparison": fc.comparison
                })
        else:
            # Pure multi-threshold: all have thresholds, sort by first
            first = filter_conds[0]
            plan.stat = first.stat
            plan.derived_stat = first.derived
            plan.threshold = first.threshold
            plan.comparison = first.comparison
            for fc in filter_conds[1:]:
                plan.extra_filters.append({
                    "stat": fc.stat, "threshold": fc.threshold, "comparison": fc.comparison
                })
            # Multi-threshold with no ranking signal = threshold query type
            if plan.query_type == "leaderboard":
                plan.query_type = "threshold"

        # Mark all condition text as consumed
        for cond in all_conditions:
            _add_consumed(plan, cond.consumed_text)

        # Pitching context: check primary stat AND extra filters
        if plan.stat and is_pitching_stat(plan.stat):
            plan.is_pitching = True
        elif any(is_pitching_stat(ef["stat"]) for ef in plan.extra_filters if ef.get("stat")):
            plan.is_pitching = True

    elif len(all_conditions) == 1:
        # Single condition from a segment — use it
        cond = all_conditions[0]
        plan.stat = cond.stat
        plan.derived_stat = cond.derived
        if cond.threshold is not None:
            plan.threshold = cond.threshold
            plan.comparison = cond.comparison
            if plan.query_type == "leaderboard" and plan.superlative is None:
                plan.query_type = "threshold"
        _add_consumed(plan, cond.consumed_text)
        if plan.stat:
            plan.is_pitching = is_pitching_stat(plan.stat)

    else:
        # No separators — try _parse_stat_condition on the whole query first.
        # This handles "sub .250 AVG", "batted .300", "stole 50 bases" etc.
        # Strip age patterns first so "under 25" isn't treated as a stat threshold
        stat_text = lower
        # Strip "top N" so the number doesn't get parsed as a stat threshold
        stat_text = re.sub(r'\btop\s+\d+\b', '', stat_text)
        # Strip game-context phrases so "game" doesn't match as a stat
        for phrase in ["in a game", "in one game", "in a single game"]:
            stat_text = stat_text.replace(phrase, " ")
        # Strip career-game-window phrases so "50" in "first 50 games" doesn't
        # get read as a stat threshold and "games" doesn't match as a stat.
        stat_text = re.sub(
            r'\b(?:first|last)\s+\d+\s+(?:career\s+)?'
            r'(?:games?|starts?|at[- ]?bats?|abs?|appearances)\b',
            ' ', stat_text,
        )
        age_pre = re.search(r'\b(?:under|younger than|over|older than)\s+(\d+)(?:\s+(?:years?\s+old|year-old))?\b', stat_text)
        if age_pre:
            # Check if followed by a stat keyword — if so, it's a stat filter, not age
            after = stat_text[age_pre.end():].strip()
            following_stat = match_stat(after.split()[0] if after.split() else "")
            if not following_stat:
                stat_text = stat_text[:age_pre.start()] + stat_text[age_pre.end():]
        whole_cond = _parse_stat_condition(stat_text)
        if whole_cond and (whole_cond.stat or whole_cond.derived):
            plan.stat = whole_cond.stat
            plan.derived_stat = whole_cond.derived
            if whole_cond.threshold is not None:
                plan.threshold = whole_cond.threshold
                plan.comparison = whole_cond.comparison
                if plan.query_type == "leaderboard" and plan.superlative is None:
                    plan.query_type = "threshold"
            _add_consumed(plan, whole_cond.consumed_text)
            if plan.stat:
                plan.is_pitching = is_pitching_stat(plan.stat)
        else:
            # Fallback: check derived stats, then regular stats
            _derived_triggers = {
                "total bases": "total_bases",
                "extra base hits": "extra_base_hits", "extra base hit": "extra_base_hits",
                "extra-base hits": "extra_base_hits", "extra-base hit": "extra_base_hits",
                "xbh": "extra_base_hits",
                "stolen base percentage": "sb_percentage", "sb%": "sb_percentage",
                "sb percentage": "sb_percentage", "steal percentage": "sb_percentage",
                "strikeout percentage": "k_percentage", "k%": "k_percentage",
                "walk percentage": "bb_percentage", "bb%": "bb_percentage",
            }
            for trigger, key in sorted(_derived_triggers.items(), key=lambda x: len(x[0]), reverse=True):
                if trigger in lower:
                    plan.derived_stat = key
                    _add_consumed(plan, trigger)
                    break

            if plan.derived_stat is None:
                plan.stat = match_stat(lower)
                if plan.stat:
                    plan.is_pitching = is_pitching_stat(plan.stat)
                    # Consume the actual alias text that matched so the words don't
                    # show up as unexplained. Must check both the regular alias map
                    # AND the fallback alias map ("best hitter", "top batter", etc.).
                    _consume_candidates = sorted(
                        list(stat_alias_map.keys()) + list(stat_fallback_alias_map.keys()),
                        key=len, reverse=True,
                    )
                    for alias in _consume_candidates:
                        if alias in lower:
                            _add_consumed(plan, alias)
                            break

    # --- Detect scope/season ---

    # Date-range detection (sub-season granularity) — check before since_year.
    # First half / second half can be single-season (closed range) or multi-
    # season recurring (the same Apr-Jul window applied across each year in
    # the scope).
    half_window = _detect_half_window(lower)
    if half_window:
        plan.query_type = "leaderboard"
        plan.threshold = None
        _add_consumed(plan,
            "in the first second half of a any each every season the all star all-star break since")
        for token in lower.split():
            cleaned = token.strip(",.;")
            if re.match(r"^\d{4}$", cleaned):
                _add_consumed(plan, cleaned)
        if half_window["single_season"]:
            plan.since_date = half_window["since_date"]
            plan.end_date = half_window["end_date"]
            plan.scope = "date_range"
        else:
            # Multi-season recurring window — set since_year scope and the
            # recurring_half flag. Executor builds per-season WHERE clauses
            # from _ASG_DATES.
            plan.recurring_half = half_window["kind"]
            plan.since_year = half_window["since_year"]
            plan.scope = f"since_{half_window['since_year']}"
    # Month-grouped leaderboard: "most HR in a single month all time", "best
    # OPS in any calendar month since 2010". Aggregates game logs by
    # (player, year, month) and ranks the resulting player-month rows.
    # Distinct from single-month parser (parse_month_query) which picks one
    # specific month for one specific player. Triggers must be specific —
    # bare "in May" must NOT fire this.
    if not plan.recurring_half and not plan.since_date:
        month_grouped_triggers = [
            "in a single month", "in any single month", "in any one month",
            "in one month", "in a calendar month", "in any calendar month",
            "in any month", "in a month all time", "in a month ever",
            "single month all time", "single month ever", "best single month",
            "best month all time", "best month ever",
            "monthly leaderboard",
            # "in any one calendar month" / "in a single calendar month" /
            # "in one calendar month" — natural phrasings that don't substring-
            # match "in any one month" (because "calendar" intervenes).
            "in any one calendar month", "in a single calendar month",
            "in one calendar month",
            # Plural "months" + history/ever phrasings — "best HR months in
            # history" / "top RBI months ever" / "best months in history".
            # Different shape than singular variants but same query intent.
            "months in history", "months ever", "months all time",
            "best months ever", "best months in history", "best months all time",
            "top months ever", "top months in history", "top months all time",
        ]
        if any(t in lower for t in month_grouped_triggers):
            plan.month_grouped = True
            plan.query_type = "leaderboard"
            plan.threshold = None
            _add_consumed(plan,
                "in a single any one calendar month months monthly leaderboard best ever all time")

    # Career-game-window: "first/last N (career) games", "first N starts".
    # Aggregates across the first/last N rows of each player's chronologically
    # ordered game logs. Three scopes:
    #   season — "in their first 50 games this season"
    #   career — "in a player's first 50 career games"
    #   recent — "over their last 100 games" (no season anchor)
    # Skip when month_grouped or recurring_half already claimed the query.
    if (not plan.month_grouped and not plan.recurring_half
            and plan.career_game_window is None):
        # "(first|last) N (career) games|starts|appearances".
        # NOTE: "at-bats" is intentionally NOT in this regex. At-bats are a
        # per-PA unit, not a per-game unit, and we don't have a per-PA log
        # table. Routing "first 10 at-bats" through the game-window executor
        # would silently mistreat 10 ABs as 10 games. Let those queries fall
        # through to Haiku, which can write SQL against game_batting_logs.at_bats
        # cumulatively to find when a player accumulated their first 10 ABs.
        m = re.search(
            r'\b(first|last)\s+(\d+|\w+)\s+(?:career\s+)?(games?|starts?|appearances)',
            lower,
        )
        if m:
            direction = m.group(1)
            n_raw = m.group(2)
            n_val: Optional[int] = None
            if n_raw.isdigit():
                n_val = int(n_raw)
            else:
                _word_nums = {"two": 2, "three": 3, "four": 4, "five": 5,
                              "six": 6, "seven": 7, "eight": 8, "nine": 9,
                              "ten": 10, "fifteen": 15, "twenty": 20,
                              "thirty": 30, "forty": 40, "fifty": 50,
                              "hundred": 100}
                n_val = _word_nums.get(n_raw)
            if n_val and 2 <= n_val <= 500:
                # Scope determination
                career_words = ("career", "of his career", "of their career",
                                 "of a career", "of a player's career",
                                 "in a career")
                if any(w in lower for w in career_words):
                    scope = "career"
                elif direction == "last" and not any(
                        s in lower for s in
                        ["this season", "this year", str(date.today().year)]):
                    # "last N games" without a season anchor = recent rolling
                    scope = "recent"
                else:
                    scope = "season"
                plan.career_game_window = {
                    "direction": direction,
                    "n": n_val,
                    "scope": scope,
                }
                # Reset stray scope assignments — the executor will override
                # via career_game_window. Don't clobber explicit since_year.
                if scope == "career" and plan.scope != f"since_{plan.since_year}":
                    plan.scope = "career"
                _add_consumed(plan,
                    "first last career games game starts start at-bats at-bat appearances")
                # Suppress threshold being read as the N count
                # (e.g. "first 50 games" — "50" is N, not a threshold).
                # parse_threshold runs later and will see the consumed words.

    # Closed range "from X to Y" / "between X and Y" — reuse the single-player
    # date-bound detector (handles to/and/between + no-day months). Only claim
    # when BOTH bounds are present (a true closed range); single open bounds are
    # left to _detect_since_date / _detect_end_date below. Player-bearing
    # variants bail later as unexplained → handled by parse_player_date_range.
    if not plan.since_date and not plan.end_date and not plan.month_grouped \
            and not plan.recurring_half:
        try:
            from . import name_matcher as _nm_cr
            cr_since, cr_end, cr_text = _nm_cr._detect_date_bounds(lower)
        except Exception:
            cr_since = cr_end = None
            cr_text = ""
        if cr_since and cr_end:
            plan.since_date, plan.end_date = cr_since, cr_end
            plan.scope = "date_range"
            plan.query_type = "leaderboard"
            plan.threshold = None
            _add_consumed(plan, cr_text)
            month_names = " ".join(_MONTH_MAP.keys())
            _add_consumed(plan, f"from to through thru until between and {month_names}")
            for token in lower.split():
                cleaned = token.strip(",.;")
                if re.match(r'^\d{1,4}(st|nd|rd|th)?$', cleaned):
                    _add_consumed(plan, cleaned)

    since_date = _detect_since_date(lower) if not plan.since_date else None
    if since_date:
        plan.since_date = since_date
        plan.scope = "date_range"
        # Date-range queries are always leaderboards (aggregate from game logs)
        plan.query_type = "leaderboard"
        # Clear any threshold that was extracted from date numbers (e.g. "16" from "june 16")
        plan.threshold = None
        # Consume all date-related words: month names, day numbers, year, keywords
        month_names = " ".join(_MONTH_MAP.keys())
        _add_consumed(plan, f"since from after starting the all star all-star break last days {month_names}")
        # Also consume any 4-digit year and 1-2 digit day numbers in the query
        for token in lower.split():
            cleaned = token.strip(",.;")
            if re.match(r'^\d{1,4}(st|nd|rd|th)?$', cleaned):
                _add_consumed(plan, cleaned)

    # Bare month-as-closed-range: "in April last year", "during May 2024".
    # Distinct from since_date (open-ended). Sets both since_date and end_date
    # so the date_range branch aggregates from game logs over a full month.
    # Only fire if month_grouped / recurring_half / since_date paths haven't
    # already claimed the query.
    if (not plan.since_date and not plan.month_grouped
            and not plan.recurring_half):
        month_range = _detect_month_range(lower)
        if month_range:
            plan.since_date, plan.end_date = month_range
            plan.scope = "date_range"
            plan.query_type = "leaderboard"
            plan.threshold = None
            month_names = " ".join(_MONTH_MAP.keys())
            _add_consumed(plan,
                f"in during last this year season current two three years ago {month_names}")
            for token in lower.split():
                cleaned = token.strip(",.;")
                if re.match(r'^\d{1,4}$', cleaned):
                    _add_consumed(plan, cleaned)

    # Open UPPER bound: "before/through/until Aug 1" → end_date, with the lower
    # bound floored at that year's season opener (executor applies date <=
    # end_date). Player-bearing variants ("Judge OPS before Aug 1") bail here as
    # unexplained and are caught by the single-player parse_player_date_range.
    if not plan.end_date and not plan.month_grouped and not plan.recurring_half:
        end_date = _detect_end_date(lower)
        if end_date:
            plan.end_date = end_date
            if not plan.since_date:
                plan.since_date = f"{int(end_date[:4])}-03-25"  # season-opener floor; avoids spring
            plan.scope = "date_range"
            plan.query_type = "leaderboard"
            plan.threshold = None
            month_names = " ".join(_MONTH_MAP.keys())
            _add_consumed(plan, f"before through thru until up to as of {month_names}")
            for token in lower.split():
                cleaned = token.strip(",.;")
                if re.match(r'^\d{1,4}(st|nd|rd|th)?$', cleaned):
                    _add_consumed(plan, cleaned)

    since_year = _detect_since_year(lower)
    if since_year and not plan.since_date:
        plan.since_year = since_year
        plan.scope = f"since_{since_year}"
        _add_consumed(plan, "since this last past decade century years year the in over for during")
        # "last decade" (standalone, not "in the last decade") = named decade with end year
        if re.search(r'\blast\s+decade\b', lower) and not re.search(r'\b(?:in|over|for|during)\s+the\s+last\s+decade', lower):
            current_year = datetime.now().year
            decade_start = current_year - (current_year % 10) - 10
            plan.end_year = decade_start + 9  # 2010 → 2019

    # "all time" / "ever" / "in history" / "career" = career totals
    # Use word boundaries for short words to avoid substring matches ("saves" contains "ever")
    career_phrase_triggers = ["all time", "all-time", "in history", "career"]
    career_word_triggers = [r'\bever\b', r'\brecord\b']
    if any(t in lower for t in career_phrase_triggers) or any(re.search(t, lower) for t in career_word_triggers):
        plan.scope = "career"
        _add_consumed(plan, "all time all-time in history ever career record")

    # "any season" / "any year" / "of any year" / "any of his seasons" — same
    # semantic as all-time for window/streak queries. Without this, "10+ K
    # in first 3 starts of any season" defaults to current season only and
    # silently drops the "any season" qualifier.
    any_season_triggers = [
        "any season", "any year", "of any year", "in any season", "in any year",
        "any of his seasons", "any of their seasons", "across all seasons",
    ]
    if any(t in lower for t in any_season_triggers):
        plan.scope = "all_time"
        plan.season = None
        # Force career_game_window.scope to "season" — "any season" means
        # per-season grouping regardless of how the original detector
        # classified the window (it might have set "recent" for last-N
        # without a season anchor). The streak executor's season-mode
        # AND the cumulative executor's per_season_any branch both key
        # off window_scope=="season" + plan.scope=="all_time".
        if plan.career_game_window:
            plan.career_game_window["scope"] = "season"
        _add_consumed(plan, "any season year of his their across all seasons years")

    # "active" filter — current or previous year has stats
    active_triggers = ["active", "still playing", "playing today", "current players"]
    if any(t in lower for t in active_triggers):
        plan.active_only = True
        _add_consumed(plan, "active still playing playing today current players among")
        # "active" implies career scope if no other scope set
        if plan.scope == "current_season":
            plan.scope = "career"
            _add_consumed(plan, "career")

    # "single season" / "in a season" / "in a year" = best single season
    # This OVERRIDES career if both present ("most HR in a season ever").
    # Skip when recurring_half is set — "of a season" in "first half of a
    # season since 2020" is grammatical, not a single-season-records signal,
    # and we want to keep the since_year scope already chosen.
    single_season_triggers = ["single season", "in a season", "in a year", "of a season"]
    if any(t in lower for t in single_season_triggers) and not plan.recurring_half and not plan.month_grouped:
        plan.scope = "all_time"  # all_time = best single season records
        _add_consumed(plan, "single season in a season in a year")

    # Multi-season consistency: "in each of the last N seasons", "every year since",
    # "N straight seasons", "back to back", "consecutive seasons",
    # "over the last N years ... in seasons" (implied per-season)
    per_season_match = re.search(
        r'(?:each|every|all)\s+(?:of\s+)?(?:the\s+)?(?:last|past)\s+(\d+)\s*(?:season|year)',
        lower
    )
    if not per_season_match:
        per_season_match = re.search(r'(\d+)\s+(?:straight|consecutive)\s+(?:season|year)', lower)
    if not per_season_match:
        if "back to back" in lower or "back-to-back" in lower:
            per_season_match = type('M', (), {'group': lambda self, n: '2'})()
    if not per_season_match:
        # "over/in the last N years" + "in seasons" or "in each season" or per-season context
        last_n = re.search(r'(?:over|in|during)\s+(?:the\s+)?(?:last|past)\s+(\d+)\s*(?:season|year)', lower)
        if last_n and any(p in lower for p in ["in seasons", "in each", "every season", "per season", "each season"]):
            per_season_match = last_n
    if per_season_match:
        plan.per_season = True
        plan.season_count = int(per_season_match.group(1))
        plan.query_type = "threshold"
        # Don't override scope if "all time" / "career" / "ever" was already detected
        if plan.scope not in ("career", "all_time"):
            current_year = datetime.now().year
            plan.since_year = current_year - plan.season_count + 1
            plan.scope = f"since_{plan.since_year}"
        _add_consumed(plan, "each every all of the last past over in during straight consecutive seasons years back to back back-to-back season year")

    # Year-over-year comparison: "Mookie Betts 2023 vs 2024"
    yoy_match = re.search(r'(20[012]\d)\s*(?:vs\.?|versus|compared to|to)\s*(20[012]\d)', lower)
    if yoy_match and not plan.since_year and not plan.since_date:
        plan.compare_years = (int(yoy_match.group(1)), int(yoy_match.group(2)))
        plan.query_type = "year_comparison"
        _add_consumed(plan, f"{yoy_match.group(1)} {yoy_match.group(2)} vs versus compared to")
        # Detect player name from the rest of the query
        name_text = lower[:yoy_match.start()].strip()
        if name_text:
            from services import name_matcher as _nm
            player = _nm.match_player(name_text)
            if not player:
                result = _nm.match_player_with_prominence(name_text)
                player = result[0] if result else None
            if player:
                plan.player_name = player
                _add_consumed(plan, name_text)

    # Relative year comparison: "Soto last year vs this year"
    if not plan.compare_years:
        rel_yoy = re.search(r'last\s+(?:year|season)\s+(?:vs\.?|versus|compared to)\s+this\s+(?:year|season)', lower)
        if not rel_yoy:
            rel_yoy = re.search(r'this\s+(?:year|season)\s+(?:vs\.?|versus|compared to)\s+last\s+(?:year|season)', lower)
        if rel_yoy:
            current_year = date.today().year
            plan.compare_years = (current_year - 1, current_year)
            plan.query_type = "year_comparison"
            _add_consumed(plan, rel_yoy.group(0))
            name_text = lower[:rel_yoy.start()].strip()
            if name_text:
                from services import name_matcher as _nm
                player = _nm.match_player(name_text)
                if not player:
                    result = _nm.match_player_with_prominence(name_text)
                    player = result[0] if result else None
                if player:
                    plan.player_name = player
                    _add_consumed(plan, name_text)

    # Only detect explicit season if since_year/since_date didn't already claim the year
    if not plan.since_year and not plan.since_date and not plan.compare_years:
        season = detect_season(lower, default_to_most_recent=False)
        if season:
            plan.season = season
            plan.scope = f"season_{season}"
            _add_consumed(plan, str(season) + " last this year season")
    if plan.scope == "current_season":
        # Default to current year for leaderboards, no default for all-time
        if plan.query_type in ("leaderboard", "team_ranking", "threshold"):
            # Threshold exceeding 110% of the single-season record → must be career
            if plan.query_type == "threshold" and plan.threshold and plan.stat and plan.comparison == ">=":
                if _exceeds_season_record(plan.stat, plan.threshold):
                    plan.scope = "all_time"
                    # Skip season defaulting — leave as all-time
                else:
                    plan.season = datetime.now().year
                    plan.scope = f"season_{plan.season}"
            # Past tense → last year
            elif any(p in lower for p in ["who led", "who had", "who hit the most"]):
                plan.season = datetime.now().year - 1
                plan.scope = f"season_{plan.season}"
            else:
                plan.season = datetime.now().year
                plan.scope = f"season_{plan.season}"

    # --- Detect filters ---
    plan.position = _detect_position(lower)
    if plan.position:
        # Mark position words as consumed
        for kw in sorted(_POSITION_MAP.keys(), key=len, reverse=True):
            if kw in lower:
                _add_consumed(plan, kw)
                break

    plan.rookie = _detect_rookie(lower)
    if plan.rookie:
        _add_consumed(plan, "rookie rookies first year first-year")
        # A rookie has by definition one rookie season. "Best rookie ERA all
        # time" / "Most HR by a rookie all time" means "best single rookie
        # season ever," not "career rookie aggregate" — career-rate formulas
        # would apply a 1000-IP / 5000-AB qualifier across the (one) rookie
        # season per player, which excludes virtually everyone. Demote
        # career scope to all_time (per-season ranking) when rookie is set.
        if plan.scope == "career":
            plan.scope = "all_time"

    # Award filter — "by an MVP", "among MVPs", "MVP seasons", "Cy Young winners"
    _AWARD_PATTERNS = [
        (r'\b(?:by an?|among|for)\s+mvps?\b|mvp\s+(?:seasons?|winners?|award)|\bmvps?\b', "MVP"),
        (r'\b(?:by an?|among|for)\s+(?:cy young|cy)\s+(?:winners?|award)?\b|cy young\s+(?:seasons?|winners?)|\bcy young winners?\b', "CY"),
        (r'\b(?:by an?|among|for)\s+(?:rookies? of the year|roy)\b|rookie of the year\s+(?:seasons?|winners?)|\brookies? of the year\b', "ROY"),
        (r'\ball[- ]?stars?\b', "ALL_STAR"),
        (r'\bgold glove(?:rs?|s|\s+winners?)?\b', "GG"),
        (r'\bsilver slugger(?:s|\s+winners?)?\b', "SS"),
        (r'\bhall of fame(?:rs?)?\b|\bhof(?:ers?)?\b', "HOF"),
    ]
    for pattern, award_code in _AWARD_PATTERNS:
        if re.search(pattern, lower):
            plan.award_filter = award_code
            # Consume the matched words
            m = re.search(pattern, lower)
            if m:
                _add_consumed(plan, m.group(0))
            break

    # Award-filtered queries: "fewest HR by an MVP" means per-season, not career total
    if plan.award_filter and plan.scope == "career":
        plan.scope = "all_time"

    plan.pitcher_role = _detect_pitcher_role(lower)
    if plan.pitcher_role:
        _add_consumed(plan, "starter starters starting reliever relievers relief closer closers bullpen")
        plan.is_pitching = True

    # Stat sets used by both the bare-pitcher gate below and downstream
    # ambiguous-stat resolution. Defined early so the gate can consult them.
    _PITCHING_DEFAULT_STATS = {"strikeouts", "walks"}
    _AMBIGUOUS_STATS = {"strikeouts", "walks"}
    _BATTING_ONLY_STATS = {"home_runs", "rbi", "stolen_bases", "doubles", "triples",
                           "batting_avg", "obp", "slg", "ops", "ops_plus", "iso", "babip"}

    # Bare "pitcher/pitching" context. By default this means "the QUERIED
    # STATS are pitching stats" → set is_pitching=True. But when the stat
    # is unambiguously a BATTING stat (batting_avg, OPS, HR, etc.), the
    # "pitcher" word means "filter players to position=P" — _detect_position
    # already populated plan.position=['P']. Setting is_pitching=True here
    # would then route to season_pitching_stats which has no batting_avg
    # column, the SQL fails, and the chain falls to Haiku unnecessarily.
    # Example: "best batting average by a pitcher all time" — query batting
    # stats with position=P filter, NOT pitching stats.
    _stat_is_batting_only = (
        plan.stat and plan.stat.db_column in _BATTING_ONLY_STATS
    )
    if not plan.is_pitching and any(w in lower for w in ["pitcher", "pitchers", "pitching", "pitched"]):
        if not _stat_is_batting_only:
            plan.is_pitching = True
    # Consume subject-noun forms once they've served their purpose flagging
    # pitching context. Without this, possessives like "pitcher's" leak into
    # plan.unexplained_words → valid=False → bail to Haiku ("first 10 career
    # starts" should reach the career-window executor instead).
    _add_consumed(plan, "pitcher pitchers pitching pitched pitcher's pitchers'")
    # NOTE: "players" / "player" intentionally excluded — they're generic
    # subjects that don't disambiguate batting vs pitching. With "players"
    # in this list, "Players with 3000 strikeouts" was being interpreted
    # as career batting K (where no one has 3000) instead of pitching K
    # (where Nolan Ryan, Randy Johnson, Clemens, etc. clearly qualify).
    # The other words ("batter", "hitting", "hitters") do uniquely signal
    # batting context.
    has_batting_context = any(w in lower for w in ["hitter", "hitters", "batter", "batters", "batting", "hitting"])
    _add_consumed(plan, "hitter hitters batter batters batting hitting hitter's batter's")
    # Extra filters containing batting-only stats imply batting context
    # e.g., "fewest strikeouts with 30+ HR" — HR is batting-only, so K must be batting too
    if not has_batting_context and plan.extra_filters:
        has_batting_context = any(
            ef.get("stat") and ef["stat"].db_column in _BATTING_ONLY_STATS
            for ef in plan.extra_filters
        )
    has_pitching_context = plan.is_pitching
    if (not plan.is_pitching and not has_batting_context
            and plan.stat and plan.stat.db_column in _PITCHING_DEFAULT_STATS):
        plan.is_pitching = True

    # "HR hit by a pitcher" / "as a batter" = batting stats for pitchers
    # Plain "HR by a pitcher" stays as pitching (with see-also for batting)
    if (plan.is_pitching and plan.stat
            and plan.stat.db_column in ("home_runs", "hits")
            and re.search(r'\b(?:hit|drove|collected|as a batter)\b', lower)
            and not any(w in lower for w in ["allowed", "given up", "surrendered", "gave up"])):
        plan.is_pitching = False
        if not plan.position:
            plan.position = ["P"]

    # Mark ambiguous when no explicit batting/pitching context was given
    # Game log queries (multi-HR games, 4-hit games) are inherently unambiguous
    if (plan.stat and plan.stat.db_column in _AMBIGUOUS_STATS
            and not has_batting_context and not has_pitching_context
            and not plan.pitcher_role
            and plan.query_type not in ("game_log_count", "game_log_extreme")):
        plan.ambiguous_stat = True

    # split_context detection happens early in decompose() (alongside team_context)
    # so consumed phrases are stripped before match_stat runs — avoiding mismatches
    # where "at bat" inside "first at bat" was being read as the AB stat keyword.

    # Bats filter
    bats_patterns = [
        # Full phrases (check longest first, plurals before singulars)
        ("left-handed batters", "L"), ("left-handed batter", "L"),
        ("left handed batters", "L"), ("left handed batter", "L"),
        ("left-handed hitters", "L"), ("left-handed hitter", "L"),
        ("left handed hitters", "L"), ("left handed hitter", "L"),
        ("right-handed batters", "R"), ("right-handed batter", "R"),
        ("right handed batters", "R"), ("right handed batter", "R"),
        ("right-handed hitters", "R"), ("right-handed hitter", "R"),
        ("right handed hitters", "R"), ("right handed hitter", "R"),
        ("switch hitters", "B"), ("switch-hitters", "B"),
        ("switch hitter", "B"), ("switch-hitter", "B"),
        # Short forms
        ("lefty batters", "L"), ("lefty hitters", "L"),
        ("lefty batter", "L"), ("lefty hitter", "L"), ("lefty", "L"),
        ("righty batters", "R"), ("righty hitters", "R"),
        ("righty batter", "R"), ("righty hitter", "R"), ("righty", "R"),
        # Plurals as standalone nouns
        ("lefties", "L"), ("righties", "R"), ("switch-hitters", "B"),
    ]
    # Skip bats detection in pitching context — "lefty starters" should set
    # throws=L, not bats=L. Without this guard the bats parser greedily
    # consumed "lefty" before the throws parser had a chance.
    if not plan.is_pitching:
        for pattern, bats_val in bats_patterns:
            if pattern in lower:
                plan.bats = bats_val
                _add_consumed(plan, pattern)
                break

    # Throws filter (pitchers)
    throws_patterns = [
        ("left-handed pitcher", "L"), ("left handed pitcher", "L"),
        ("right-handed pitcher", "R"), ("right handed pitcher", "R"),
        ("lefty pitcher", "L"), ("righty pitcher", "R"),
        ("lhp", "L"), ("rhp", "R"),
        # "right-handed pitchers" / "left-handed pitchers" with the s
        ("left-handed pitchers", "L"), ("left handed pitchers", "L"),
        ("right-handed pitchers", "R"), ("right handed pitchers", "R"),
        # Pitcher role variants: starter / reliever / closer / southpaw
        ("lefty starter", "L"), ("lefty starters", "L"),
        ("righty starter", "R"), ("righty starters", "R"),
        ("lefty reliever", "L"), ("lefty relievers", "L"),
        ("righty reliever", "R"), ("righty relievers", "R"),
        ("lefty closer", "L"), ("lefty closers", "L"),
        ("righty closer", "R"), ("righty closers", "R"),
        ("southpaw", "L"), ("southpaws", "L"),
    ]
    for pattern, throws_val in throws_patterns:
        if pattern in lower:
            plan.throws = throws_val
            plan.is_pitching = True
            _add_consumed(plan, pattern)
            break
    # Also catch bare "right-handed" / "left-handed" when in pitching context
    if not plan.throws and plan.is_pitching:
        if "right-handed" in lower or "right handed" in lower:
            plan.throws = "R"
            _add_consumed(plan, "right-handed right handed")
        elif "left-handed" in lower or "left handed" in lower:
            plan.throws = "L"
            _add_consumed(plan, "left-handed left handed")

    # Special condition patterns
    # "without getting caught" / "without being caught" → CS = 0
    if any(p in lower for p in ["without getting caught", "without being caught",
                                 "never caught", "100% steal", "perfect steal"]):
        plan.extra_filters.append({
            "stat": stat_alias_map.get("caught stealing") or stat_alias_map.get("cs"),
            "threshold": 0,
            "comparison": "<=",
        })
        _add_consumed(plan, "without getting caught without being caught never caught perfect")

    # Team filter
    for alias in _sorted_team_aliases:
        if alias in lower:
            plan.team_code = team_alias_map[alias]
            _add_consumed(plan, alias)
            break

    # Age filter — only match "under/over N" when it's about age, not stats.
    # "player under 25" = age. "with under 10 HR" = stat filter (not age).
    # Heuristic: age if followed by nothing or age-like words, not if followed by a stat keyword.
    age_match = re.search(r'\b(?:under|younger than)\s+(\d+)(?:\s+(?:years?\s+old|year-old|y/?o))?\b', lower)
    if age_match:
        # Check if this "under N" is preceded by a stat keyword (ERA under 3) → not age
        before = lower[:age_match.start()].strip()
        preceding_stat = match_stat(before.split()[-1] if before.split() else "")
        # Check if followed by a stat keyword → also not age
        after = lower[age_match.end():].strip()
        following_stat = match_stat(after.split()[0] if after.split() else "")
        if not following_stat and not preceding_stat:
            plan.age_max = int(age_match.group(1))
            _add_consumed(plan, f"under younger than {age_match.group(1)} years old year-old")
    age_match = re.search(r'\b(?:over|older than)\s+(\d+)(?:\s+(?:years?\s+old|year-old|y/?o))?\b', lower)
    if age_match:
        after = lower[age_match.end():].strip()
        following_stat = match_stat(after.split()[0] if after.split() else "")
        if not following_stat:
            plan.age_min = int(age_match.group(1))
            _add_consumed(plan, f"over older than {age_match.group(1)} years old year-old")

    # --- Detect "top N" as limit, not threshold ---
    top_match = re.search(r'\btop\s+(\d+)\b', lower)
    if top_match:
        plan.limit = max(1, min(int(top_match.group(1)), 50))
        _add_consumed(plan, top_match.group(0))

    # --- Detect threshold (only if multi-condition didn't already set it) ---
    # Skip when a career_game_window is set — the "50" in "first 50 games"
    # is the window length, not a stat threshold.
    if plan.threshold is None and plan.stat and not plan.extra_filters and not plan.career_game_window:
        threshold_text = lower
        if plan.age_max:
            threshold_text = re.sub(rf'\b(?:under|younger than)\s+{plan.age_max}\b', '', threshold_text)
        if plan.age_min:
            threshold_text = re.sub(rf'\b(?:over|older than)\s+{plan.age_min}\b', '', threshold_text)
        if top_match:
            threshold_text = threshold_text.replace(top_match.group(0), "")

        threshold = _extract_threshold(threshold_text, stat=plan.stat)
        if threshold is not None:
            plan.threshold = threshold
            _add_consumed(plan, str(threshold))
            if plan.query_type == "leaderboard" and plan.superlative is None:
                plan.query_type = "threshold"

    elif plan.threshold is None and plan.derived_stat and not plan.extra_filters and not plan.career_game_window:
        threshold = _extract_threshold(lower)
        if threshold is not None:
            plan.threshold = threshold
            _add_consumed(plan, str(threshold))
            if plan.query_type == "leaderboard" and plan.superlative is None:
                plan.query_type = "threshold"

    # Handle word-number thresholds for count queries
    if plan.threshold is None and plan.query_type == "count":
        _word_nums = [("fifty", 50), ("forty", 40), ("thirty", 30), ("twenty", 20),
                      ("ten", 10), ("five", 5), ("four", 4), ("three", 3), ("two", 2), ("one", 1)]
        for word, num in _word_nums:
            if re.search(rf'\b{word}\b', lower):
                plan.threshold = float(num)
                _add_consumed(plan, word)
                break
        if plan.threshold is None:
            plan.threshold = 1.0  # "how many players hit a HR" → threshold 1

    # Under/below patterns (for primary threshold only, if no secondary filter consumed it)
    under_patterns = ["under ", "fewer than ", "less than ", "below ", "no more than ", "or fewer", "or less"]
    if any(p in lower for p in under_patterns) and not plan.extra_filters:
        plan.comparison = "<="
        _add_consumed(plan, "under fewer than less than below no more than or fewer or less")

    # --- Detect game-log query patterns ---
    game_log_triggers = ["in a game", "in one game", "in a single game"]
    if any(t in lower for t in game_log_triggers):
        plan.query_type = "game_log_extreme"
        plan.ambiguous_stat = False  # game-level queries are never ambiguous
        _add_consumed(plan, "in a game in one game in a single game single")

    # Game-log counting: "most 3-hit games", "most games with 3+ RBI", "most 10-K games"
    multi_game_match = None
    # Pattern 0: "multi-stat games" — treat "multi" as 2+
    _multi_match = re.search(r'multi[- ]?(hit|hr|home run|homer|rbi|strikeout|k|xbh|extra[- ]?base[- ]?hit|walk|bb|steal|stolen base|sb|run)\s*game', lower)
    if _multi_match:
        _full = _multi_match.group(0)
        class _MultiMatch:
            def group(self, n):
                return "2" if n == 1 else _full
        multi_game_match = _MultiMatch()
        _add_consumed(plan, "multi")
    # Pattern 1: "N-stat games" / "N+ stat games"
    if not multi_game_match:
        multi_game_match = re.search(r'(\d+)[+-]?\s*(?:hit|hr|home run|homer|rbi|strikeout|k|xbh|extra[- ]?base[- ]?hit|walk|bb|steal|stolen base|sb|run)\s*game', lower)
    # Pattern 2: "games with N+ stat"
    if not multi_game_match:
        multi_game_match = re.search(r'games?\s+with\s+(\d+)\+?\s*(?:hit|hr|home run|homer|rbi|strikeout|k|xbh|extra[- ]?base[- ]?hit|walk|bb|steal|stolen base|sb|run)', lower)

    if multi_game_match:
        plan.query_type = "game_log_count"
        plan.ambiguous_stat = False  # game-level queries are never ambiguous
        n = int(multi_game_match.group(1))
        context = multi_game_match.group(0).lower()
        if any(w in context for w in ["hr", "home run", "homer"]):
            plan.game_log_stat = "home_runs"
        elif "rbi" in context:
            plan.game_log_stat = "rbi"
        elif any(w in context for w in ["strikeout", "k"]):
            plan.game_log_stat = "strikeouts"
            plan.is_pitching = True  # 10-K games = pitching
        elif any(w in context for w in ["xbh", "extra base", "extra-base"]):
            plan.game_log_stat = "xbh"
        elif any(w in context for w in ["walk", "bb"]):
            plan.game_log_stat = "walks"
        elif any(w in context for w in ["steal", "stolen", "sb"]):
            plan.game_log_stat = "stolen_bases"
        elif any(w in context for w in ["run ", "runs"]):
            plan.game_log_stat = "runs"
        elif "hit" in context:
            plan.game_log_stat = "hits"
        plan.game_log_threshold = n
        _add_consumed(plan, multi_game_match.group(0).replace("+", ""))

    # XBH-specific patterns: "4xbh games", "4+ xbh", "extra base hit games"
    if not plan.game_log_stat:
        xbh_match = re.search(r'(\d+)\+?\s*(?:xbh|extra[- ]?base[- ]?hit)', lower)
        if xbh_match:
            plan.query_type = "game_log_count"
            plan.ambiguous_stat = False
            plan.game_log_stat = "xbh"
            plan.game_log_threshold = int(xbh_match.group(1))
            # Consume the full matched text and all variations
            full_match = xbh_match.group(0).replace("+", "")
            _add_consumed(plan, f"{full_match} extra base hit xbh")
            # Also consume combined tokens like "4xbh+" as whole words
            for token in lower.split():
                if "xbh" in token or "extra" in token:
                    plan.consumed_words.add(token.strip("+-.,"))
                    plan.consumed_words.add(token)  # include with punctuation too

    if "multi-hit" in lower or "multi hit" in lower:
        plan.query_type = "game_log_count"
        plan.ambiguous_stat = False
        plan.game_log_stat = "hits"
        plan.game_log_threshold = 2
        _add_consumed(plan, "multi-hit multi hit")

    if "multi-homer" in lower or "multi homer" in lower or "multi-hr" in lower or "multi hr" in lower:
        plan.query_type = "game_log_count"
        plan.ambiguous_stat = False
        plan.game_log_stat = "home_runs"
        plan.game_log_threshold = 2
        _add_consumed(plan, "multi-homer multi homer multi-hr multi hr")

    # --- Detect sequential streak queries ---
    # Patterns: "hit in N consecutive/straight games", "reached base in first N",
    # "homered in N straight", "when was the last time someone hit in 10 straight"
    # "longest hitting streak"

    # Condition map: what counts as "success" per game
    _streak_conditions = {
        # Reaching base
        "reached base": ("(g.hits + g.walks + COALESCE(g.hit_by_pitch, 0)) >= 1", "reaching base"),
        "reaching base": ("(g.hits + g.walks + COALESCE(g.hit_by_pitch, 0)) >= 1", "reaching base"),
        "got on base": ("(g.hits + g.walks + COALESCE(g.hit_by_pitch, 0)) >= 1", "reaching base"),
        "on-base streak": ("(g.hits + g.walks + COALESCE(g.hit_by_pitch, 0)) >= 1", "reaching base"),
        "on base": ("(g.hits + g.walks + COALESCE(g.hit_by_pitch, 0)) >= 1", "reaching base"),
        # Multiple hits
        "multiple hits": ("g.hits >= 2", "multiple hits"),
        "multi-hit": ("g.hits >= 2", "multiple hits"),
        "multi hit": ("g.hits >= 2", "multiple hits"),
        "2+ hits": ("g.hits >= 2", "2+ hits"),
        # Hits
        "hit": ("g.hits >= 1", "a hit"),
        "got a hit": ("g.hits >= 1", "a hit"),
        "had a hit": ("g.hits >= 1", "a hit"),
        "hitting streak": ("g.hits >= 1", "a hit"),
        "hit streak": ("g.hits >= 1", "a hit"),
        # Home runs
        "homered": ("g.home_runs >= 1", "a HR"),
        "hit a homer": ("g.home_runs >= 1", "a HR"),
        "hit a home run": ("g.home_runs >= 1", "a HR"),
        "with a home run": ("g.home_runs >= 1", "a HR"),
        "with a hr": ("g.home_runs >= 1", "a HR"),
        "home run": ("g.home_runs >= 1", "a HR"),
        "hr in": ("g.home_runs >= 1", "a HR"),
        # Extra base hits
        "extra base hit": ("(g.doubles + g.triples + g.home_runs) >= 1", "an XBH"),
        "xbh": ("(g.doubles + g.triples + g.home_runs) >= 1", "an XBH"),
        "extra-base hit": ("(g.doubles + g.triples + g.home_runs) >= 1", "an XBH"),
        # RBI
        "drove in a run": ("g.rbi >= 1", "an RBI"),
        "with an rbi": ("g.rbi >= 1", "an RBI"),
        "with a rbi": ("g.rbi >= 1", "an RBI"),
        "an rbi": ("g.rbi >= 1", "an RBI"),
        "rbi in": ("g.rbi >= 1", "an RBI"),
        "had an rbi": ("g.rbi >= 1", "an RBI"),
        # Stolen bases
        "stole a base": ("g.stolen_bases >= 1", "a SB"),
        "with a stolen base": ("g.stolen_bases >= 1", "a SB"),
        "stolen base": ("g.stolen_bases >= 1", "a SB"),
        # Strikeouts (pitching)
        "struck out": ("g.strikeouts >= 1", "a K"),
        # Walks
        "walked": ("g.walks >= 1", "a BB"),
        "with a walk": ("g.walks >= 1", "a BB"),
        # Scoreless (pitching)
        "scoreless": ("g.earned_runs = 0", "scoreless"),
        "shutout innings": ("g.earned_runs = 0", "scoreless"),
        "scoreless innings": ("g.earned_runs = 0", "scoreless"),
        # Wins (pitching)
        "wins": ("g.win >= 1", "a W"),
        "consecutive wins": ("g.win >= 1", "a W"),
        "winning streak": ("g.win >= 1", "a W"),
        "win streak": ("g.win >= 1", "a W"),
    }

    # Only detect streaks when streak-context words are present
    streak_context_words = ["consecutive", "straight", "in a row", "streak", "streaks",
                            "first", "opening", "current", "longest"]
    has_streak_context = any(w in lower for w in streak_context_words)

    # "3 straight seasons" / "consecutive years" = per-season threshold, NOT game-level streak.
    # Skip streak parsing entirely — per_season detection (above) already handles this.
    season_level_streak = has_streak_context and re.search(
        r'(?:consecutive|straight|back.to.back)\s+(?:season|year)', lower)
    if season_level_streak:
        has_streak_context = False

    # Half-window queries ("first half of a season since 2020", "best ERA in
    # the first half of 2024") are date-window aggregations, not game-by-game
    # streak detections. The word "first" in "first half" otherwise trips the
    # streak_context flag and a generic "home run" / "hit" stat keyword would
    # then misroute to streak_sequence.
    if (plan.recurring_half or plan.since_date or plan.month_grouped
            or plan.career_game_window):
        has_streak_context = False

    # At-bats are a per-PA unit; we don't have a per-PA log table. A query
    # like "hit safely in his first 10 at-bats" can't be answered by the
    # per-game streak executor (which would interpret 10 ABs as 10 games).
    # Suppress streak detection so the query falls through to Haiku, which
    # can write SQL against game_batting_logs.at_bats cumulatively.
    if re.search(r'\b(?:first|last)\s+\d+\s+(?:career\s+)?(?:at[- ]?bats?|abs?)\b', lower):
        has_streak_context = False

    streak_detected = False
    if has_streak_context:
        # Check if query has explicit per-game numeric thresholds (13+ IP, 0 runs, etc.)
        # If so, skip named triggers — the thresholds define the conditions
        has_numeric_thresholds = bool(re.search(r'\d+\+?\s*(?:ip|hits|runs|hr|rbi|walks|strikeouts|k\b|era|innings)', lower))

        # Check for named condition triggers (only when no numeric thresholds)
        if not has_numeric_thresholds:
            for trigger, (condition, label) in sorted(_streak_conditions.items(), key=lambda x: len(x[0]), reverse=True):
                if trigger in lower:
                    plan.streak_conditions.append(condition)
                    plan.streak_condition_labels.append(label)
                    _add_consumed(plan, trigger)
                    streak_detected = True
                    break

        # Also parse per-game stat thresholds: "13+ IP, 0 runs and 5 or fewer hits"
        # Map game-log column names for threshold parsing
        _game_log_stat_map = {
            "innings_pitched": ("g.ip_outs", 3),  # multiply threshold by 3 (IP → ip_outs)
            "ip": ("g.ip_outs", 3),
            "hits": ("g.hits", 1),
            "home_runs": ("g.home_runs", 1),
            "runs": ("g.runs", 1),
            "rbi": ("g.rbi", 1),
            "walks": ("g.walks", 1),
            "strikeouts": ("g.strikeouts", 1),
            "stolen_bases": ("g.stolen_bases", 1),
            "earned_runs": ("g.earned_runs", 1),
        }

        # Strip streak-context phrases before parsing conditions
        # so "5 or fewer hits in first 2 games" doesn't match "games" as a stat
        cond_text = lower
        for phrase in ["in first", "in the first", "in their first", "opening",
                        "consecutive", "straight", "in a row", "of a season",
                        "of the season", "games pitched", "games played",
                        "games started"]:
            cond_text = cond_text.replace(phrase, " ")
        # Remove streak length numbers and "games" that remain after stripping
        cond_text = re.sub(r'(?:first|opening)\s+\d+', '', cond_text)
        cond_text = re.sub(r'\d+\s*games?\b', '', cond_text)  # "3 games" → ""
        cond_text = re.sub(r'\bgames?\b', '', cond_text)  # standalone "games"

        # Split on commas and "and" to find multiple conditions
        _cond_separators = [",", " and "]
        for sep in _cond_separators:
            cond_text = cond_text.replace(sep, " |CSEP| ")
        cond_parts = [p.strip() for p in cond_text.split("|CSEP|") if p.strip()]

        for part in cond_parts:
            # Look for "N+ STAT" or "N or fewer STAT" or "0 STAT" patterns
            stat_found = match_stat(part)
            if stat_found and stat_found.db_column in _game_log_stat_map:
                threshold = _extract_threshold(part, stat=stat_found)
                if threshold is not None:
                    col, multiplier = _game_log_stat_map[stat_found.db_column]
                    adj_threshold = threshold * multiplier

                    # Determine comparison
                    under = any(p in part for p in ["or fewer", "fewer than", "or less",
                                                     "less than", "under", "at most", "no more"])
                    if under or threshold == 0:
                        sql_cond = f"{col} <= {int(adj_threshold)}"
                        label = f"≤{int(threshold)} {stat_found.display_abbrev}"
                    else:
                        sql_cond = f"{col} >= {int(adj_threshold)}"
                        label = f"{int(threshold)}+ {stat_found.display_abbrev}"

                    # Don't duplicate if already captured by trigger
                    if sql_cond not in plan.streak_conditions:
                        plan.streak_conditions.append(sql_cond)
                        plan.streak_condition_labels.append(label)
                        _add_consumed(plan, part.strip())
                        streak_detected = True

    if streak_detected:
        # Find the number of consecutive games
        n_match = re.search(r'(\d+)\s*(?:consecutive|straight|in a row)', lower)
        if not n_match:
            n_match = re.search(r'(?:first|opening)\s+(\d+)', lower)
            if n_match:
                plan.streak_direction = "leading"
        if not n_match:
            # "longest hitting streak" — no number, find the longest
            if "current" in lower or "active" in lower:
                plan.streak_length = None
                plan.streak_direction = "trailing"
                _add_consumed(plan, "current active longest")
            elif "longest" in lower or "most consecutive" in lower:
                plan.streak_length = None  # means "find the longest"
                plan.streak_direction = "sliding"
                _add_consumed(plan, "longest most consecutive")
        else:
            plan.streak_length = int(n_match.group(1))
            _add_consumed(plan, n_match.group(0))

        if plan.streak_direction is None:
            plan.streak_direction = "sliding"  # default: find it anywhere

        # "when was the last time" / "last player to" → most recent occurrence
        if any(p in lower for p in ["last time", "last player", "most recent", "when was the last"]):
            _add_consumed(plan, "last time last player most recent when was the last")

        plan.query_type = "streak_sequence"
        _add_consumed(plan, "consecutive straight in a row games game first opening streak streaks")

        # If no streak_length was extracted but we have a threshold, use it
        # "8+ game hitting streaks" → threshold=8, streak_length=None → fix
        if plan.streak_length is None and plan.threshold is not None:
            plan.streak_length = int(plan.threshold)
            plan.threshold = None  # consumed into streak_length

        # "of a season" / "in a season" in streak context = search all seasons, not a specific one
        if any(p in lower for p in ["of a season", "in a season"]) and not plan.since_year:
            plan.season = None
            plan.scope = "all_time"

    # --- Resolve "lowest" for rate stats ---
    if "lowest" in question.lower():
        if plan.stat and plan.stat.db_column in _LOWER_IS_BETTER:
            plan.sort_asc = False  # "lowest ERA" = best ERA = natural sort
        else:
            plan.sort_asc = True  # "lowest OPS" = worst

    # --- Career-window per-game threshold → streak_sequence ---
    # "10+ K in each of their first 3 starts of 2026" (leading)
    # "10+ K in each of their last 3 starts" (tail_window)
    # "2+ HR in first 5 career games" (leading + career-debut grouping)
    # "10+ K AND 0 BB in first 3 starts" (compound: stat + extra_filters)
    #
    # These are per-game-each checks: every one of the N games must satisfy
    # the threshold(s). The existing _streak_leading and new _streak_tail_window
    # executors already implement this — we just wire the plan into
    # streak_sequence shape and translate (stat, threshold, comparison) into
    # SQL conditions.
    #
    # Trigger: career_game_window + threshold + stat + EXPLICIT
    # per-game-each phrasing ("each of", "every one of", etc.).
    #
    # Without explicit per-game phrasing, the natural reading is
    # CUMULATIVE over the window:
    #   "10 HRs in their first 50 games"     → 10 total HRs, 50-game window
    #   "hit .300 in their first 50 games"   → cumulative .300 AVG over window
    # These route to the cumulative executor below (with threshold
    # filter on the window aggregate).
    #
    # With explicit per-game phrasing, the user wants every game in
    # the window to satisfy the condition independently:
    #   "10+ K in EACH of their first 3 starts"   → every game >= 10 K
    #   "0 BB in EACH of their first 5 starts"    → every game has 0 BB
    # These convert to streak_sequence + leading direction below.
    _per_game_each_triggers = (
        "each of", "every one of", "every of", "in each of",
        "in every of", "all of their first", "all of his first",
        "all of her first", "all of their last", "all of his last",
        "all of her last",
    )
    _per_game_each = any(t in lower for t in _per_game_each_triggers)
    if (plan.career_game_window
            and plan.threshold is not None
            and plan.stat is not None
            and _per_game_each
            and not plan.streak_conditions):
        # Game-log column map (season-stats → game-log column where they differ)
        _gl_col_map = {
            "innings_pitched": "ip_outs",  # stored as outs in game logs
            "saves": "save",
            "wins": "win",
            "losses": "loss",
        }

        def _build_streak_condition(stat_obj, threshold_val, comparison):
            """Translate (stat, threshold, comparison) to a SQL condition + label.
            Returns (sql_cond_str, label_str) or (None, None) if not buildable.
            """
            if stat_obj is None or threshold_val is None:
                return None, None
            col = _gl_col_map.get(stat_obj.db_column, stat_obj.db_column)
            t_int = int(threshold_val) if threshold_val == int(threshold_val) else threshold_val
            op = comparison if comparison in (">=", "<=", ">", "<", "=") else ">="
            # threshold==0 with default >= → always true. Flip to <=.
            if t_int == 0 and op == ">=":
                op = "<="
            # IP stored as outs: 6 IP = 18 outs.
            sql_t = t_int * 3 if col == "ip_outs" and isinstance(t_int, (int, float)) else t_int
            abbrev = stat_obj.display_abbrev or stat_obj.db_column.upper()
            # Label format:
            #   0 + <stat>     →  "0 BB" (zero is unambiguous after threshold-zero flip)
            #   N + ≥ filter   →  "N+ HR"
            #   N + ≤ filter   →  "≤N HR"
            #   N + > / < / =  →  "{op} N HR"
            if t_int == 0:
                label = f"0 {abbrev}"
            elif op == ">=":
                label = f"{t_int}+ {abbrev}"
            elif op == "<=":
                label = f"≤{t_int} {abbrev}"
            else:
                label = f"{op} {t_int} {abbrev}"
            return f"g.{col} {op} {sql_t}", label

        # Primary stat condition
        cond_str, cond_label = _build_streak_condition(
            plan.stat, plan.threshold, plan.comparison)
        if cond_str:
            plan.streak_conditions = [cond_str]
            plan.streak_condition_labels = [cond_label]

            # Extra filters (compound: "10+ K AND 0 BB in first 3 starts")
            for ef in plan.extra_filters:
                ef_cond, ef_label = _build_streak_condition(
                    ef.get("stat"), ef.get("threshold"), ef.get("comparison", ">="))
                if ef_cond:
                    plan.streak_conditions.append(ef_cond)
                    plan.streak_condition_labels.append(ef_label)
            plan.extra_filters = []  # consumed into streak conditions

            plan.streak_length = plan.career_game_window["n"]
            direction = plan.career_game_window.get("direction")
            window_scope = plan.career_game_window.get("scope", "season")
            # Map career_game_window direction → streak_direction
            #   first → leading (first N games per scope)
            #   last  → tail_window (last N games per scope)
            plan.streak_direction = "leading" if direction == "first" else "tail_window"
            # When the window scope is "career" or "recent", the executor
            # should group by player_id only and slice the player's full
            # history — not group by (player_id, season) per the default
            # leading/tail_window grouping. Promoting plan.scope to "career"
            # both drops the SQL season filter and signals the executor
            # to use per-player grouping (see _streak_leading /
            # _streak_tail_window scope check).
            if window_scope in ("career", "recent"):
                plan.scope = "career"
                plan.season = None

            plan.query_type = "streak_sequence"
            plan.threshold = None
            plan.career_game_window = None
        plan.query_type = "streak_sequence"
        # Consume the threshold + window — the streak path owns them now.
        plan.threshold = None
        plan.career_game_window = None

    # --- Check for unexplained words ---
    words = re.findall(r"[a-z0-9'+%-]+", lower)
    for w in words:
        w_clean = w.strip("?.!,'+%-")
        if not w_clean:
            continue
        if w_clean in _STOP_WORDS:
            continue
        if w_clean in plan.consumed_words:
            continue
        # Numbers and number-like tokens (3.00, sub-3, 200+) are consumed
        if w_clean.isdigit():
            continue
        if re.match(r'^[\d.+-]+$', w_clean):
            continue
        # Tokens starting with "sub-" are comparison modifiers
        if w_clean.startswith("sub-") or w_clean.startswith("sub"):
            continue
        # Short words (1-2 chars) are usually noise
        if len(w_clean) <= 2:
            continue
        # Number+letter combined tokens ("200k", "30hr") — split and check parts
        num_letter = re.match(r'^(\d+\.?\d*)([a-z]+)$', w_clean)
        if num_letter:
            letter_part = num_letter.group(2)
            if letter_part in plan.consumed_words or letter_part in _STOP_WORDS or len(letter_part) <= 2:
                continue
        plan.unexplained_words.append(w_clean)

    # Try to match unexplained words as a player name
    if plan.unexplained_words:
        # Strip possessive "'s" from each token before matching — "maddux's"
        # → "maddux". match_player itself only strips trailing "'s" from the
        # whole input, so an embedded possessive ("greg maddux's lowest")
        # blocks the lookup. Apply token-level cleaning here.
        _cleaned_tokens = [
            re.sub(r"'s\b|’s\b", "", w) for w in plan.unexplained_words
        ]
        unexplained_str = " ".join(_cleaned_tokens)
        matched = match_player(unexplained_str)
        # Fallback: match_player wants the whole input to BE a player name.
        # When unexplained still contains non-name words like "issued",
        # "recorded", "had", etc., that fails. find_player_in_text scans
        # for embedded names so it handles "greg maddux issued" → Maddux.
        if not matched:
            from . import name_matcher as _nm
            matched = _nm.find_player_in_text(unexplained_str)
        if matched:
            plan.player_name = matched
            # Remove matched name words from unexplained
            matched_words = set(matched.lower().split())
            plan.unexplained_words = [
                w for w in plan.unexplained_words
                if re.sub(r"'s\b|’s\b", "", w) not in matched_words
            ]

    # Player single-season-max detection. The user asking "Most X player ever"
    # or "best X season player ever had" or "player's career-high in X" wants
    # THIS player's career-best single season — NOT an all-time leaderboard
    # (ignoring the player) and NOT a career total. Triggers:
    #   - scope=all_time (set above by "in a season" / "single season")
    #   - OR superlative ("most/best/highest/fewest/worst/lowest") + "ever"
    #   - OR "best/worst [season|year]" phrase
    #   - OR "career high/best/low" phrase
    # Combined with a named player + stat. Without this expansion, "Most home
    # runs Hank Aaron ever hit" hits scope=career and returns the all-time
    # career HR leaderboard (Bonds 762, Aaron 755, ...) — Aaron isn't even
    # the answer to his own question.
    _has_superlative = bool(re.search(r'\b(?:most|best|highest|peak|top|fewest|worst|lowest|least)\b', lower))
    _has_ever = bool(re.search(r'\bever\b', lower))
    # "best season" / "best HR season" / "worst ERA year" — allow up to 3 words
    # between the superlative and season/year for "best [stat] season" patterns.
    _has_best_season = bool(re.search(
        r'\b(?:best|highest|peak|top|worst|lowest)\b(?:\s+\w+){0,3}\s+(?:season|year)\b', lower
    ))
    _has_career_best = bool(re.search(r'\bcareer[- ]?(?:high|best|worst|low|lowest|highest)\b', lower))
    _has_career_total = bool(re.search(
        r'\bcareer\s+(?:total|totals|stat|stats|statistics|number|numbers|sum|sums)\b', lower
    ))
    # Explicit time qualifiers — if the question names a year ("2024") or
    # uses "this year"/"last year"/etc., the user wants a specific season,
    # NOT the player's career-best. Used to gate the bare-possessive trigger
    # below. (plan.season may have been defaulted to current year by other
    # decompose logic even when the user didn't specify — so don't rely on
    # that as the signal.)
    _has_explicit_year = bool(re.search(r'\b(?:19|20)\d{2}\b', lower)) or any(
        p in lower for p in ("this year", "this season", "current season",
                              "last year", "last season", "previous season", "prior season",
                              "two years ago", "three years ago"))
    # Naked possessive — "Maddux's lowest WHIP", "Trout's best OPS", "Aaron's most HR".
    # When the user says "[player]'s [superlative] [stat]" with no other scope cue,
    # the natural reading is "his career-best single-season value." Plays well
    # with the player_name + stat already detected by decompose.
    # Sliding-window stretch ("most X in a 3 game stretch", "best 5-game
    # span", "most X in any 10 games") is NOT a single-season max — claiming
    # it would ship "most X in a single season" and silently drop the window.
    # Bail so it routes to Haiku for now (real sliding-window support is scoped
    # separately).
    _has_sliding_window = bool(re.search(
        r'\b(?:stretch|span)\b|\b(?:in|over|across)\s+(?:a|any|his|their)?\s*\d+[- ]?games?\b',
        lower))
    _is_player_ssn_max = (
        plan.player_name and (plan.stat or plan.derived_stat)
        and not _has_career_total and not _has_sliding_window
        and (
            plan.scope == "all_time"
            or (_has_superlative and _has_ever)
            or _has_best_season
            or _has_career_best
            # Bare possessive form: superlative + player + stat with no
            # explicit year/time qualifier and no career-total phrasing.
            # Default to single-season max ("Maddux's lowest WHIP").
            or (_has_superlative and not _has_explicit_year)
        )
    )
    if _is_player_ssn_max and plan.query_type in ("leaderboard", "threshold"):
        plan.query_type = "player_single_season_max"
        # We've confidently identified single-season-max intent (superlative +
        # player + stat + context like "ever" / "best season" / "career
        # high"). Any leftover unexplained words are decorative — verbs like
        # "had", "hit", "issued", "threw", "recorded", or just dangling
        # adjectives. Clear them so plan.is_valid returns True and the
        # executor can produce the right answer.
        plan.unexplained_words = []

    # Sliding-window stretch: "{player} most {stat} in a {N}-game stretch/span".
    # Buildable from game logs (rolling N-consecutive-game windows within one
    # season). Requires an explicit N — without it the window is undefined, so
    # "best stretch" alone keeps bailing. Counting stats only (handled by the
    # executor); derived stats stay a bail.
    if (_has_sliding_window and plan.player_name and plan.stat
            and plan.query_type in ("leaderboard", "threshold", "superlative")):
        _win_m = re.search(r'(\d+)[\s-]*games?\b', lower)
        if _win_m:
            plan.sliding_window_n = int(_win_m.group(1))
            plan.query_type = "player_sliding_window"
            # Window intent is unambiguous now; leftover decorative words
            # ("ever", "had", "in a", "stretch") shouldn't block the plan.
            plan.unexplained_words = []

    # Final overrides for date_range scope — must run last since earlier steps
    # may have set threshold from date numbers (e.g. "16" from "june 16")
    # and reclassified query_type to "threshold"
    if plan.scope == "date_range":
        plan.threshold = None
        plan.query_type = "leaderboard"

    # Final overrides for per_season — if the primary threshold equals the season count,
    # it was likely extracted from "last 3 years" not from a stat condition.
    # Try to re-extract the real threshold from the query text.
    if plan.per_season and plan.threshold is not None and plan.season_count:
        if plan.threshold == float(plan.season_count):
            # The threshold is the season count leaking. Try to find the real one.
            # Look for "at least N", "N+", or bare numbers near the stat keyword
            numbers = re.findall(r'(?:at least |least |minimum )?(\d+\.?\d*)\+?\s*(?:game|hit|home|rbi|run|walk|steal|win|save|strikeout|sb|hr|k|bb)', lower)
            if numbers:
                plan.threshold = float(numbers[0])
            else:
                plan.threshold = None  # let the extra_filters carry the conditions

    # Default-stat rule for vague "best X" leaderboards. The stat_config fallback
    # aliases ("best hitter" → OPS, "best pitcher" → ERA) cover queries where
    # those phrases are the entire ranking signal — but only when match_stat
    # actually ran on a string containing those phrases. If we got here with
    # no stat (e.g. a leaderboard trigger like "best" or "highest" with the
    # stat noun not matching anything), fall back by intent:
    #   - pitching + split_context  → OPS-against. Split tables (count, RISP,
    #                                  pitch type, inning, TTO) don't carry
    #                                  earned runs or innings pitched, so ERA
    #                                  isn't computable. Of the available
    #                                  _against rate cols, OPS combines OBP
    #                                  (reach) + SLG (power) — a richer single
    #                                  pitcher-quality signal than BAA alone.
    #                                  FIP would be the right long-term answer
    #                                  for ERA parity; queued separately.
    #   - pitching, no split        → ERA (the open-ended pitching standard)
    #   - batting (default)         → OPS
    # Pitching split tables that DO carry ERA directly (vs. _against rate cols).
    # Used by the default-stat rule below to pick ERA over OPS for these
    # specific tables. pitching_home_away_splits stores era/whip/k_per_9/baa
    # directly; the count/risp/pitch_type/inning/TTO tables only carry
    # _against rate columns and need OPS-against.
    _PITCHING_SPLIT_TABLES_WITH_ERA = {"pitching_home_away_splits"}

    if (plan.query_type == "leaderboard"
            and plan.stat is None and plan.derived_stat is None
            and any(t in lower for t in ["best", "highest", "lowest", "top",
                                         "leaders", "leader", "leading", "most",
                                         "worst", "fewest"])):
        if plan.is_pitching:
            if plan.split_context is not None:
                if plan.split_context.table in _PITCHING_SPLIT_TABLES_WITH_ERA:
                    plan.stat = stat_alias_map.get("era")
                else:
                    plan.stat = stat_alias_map.get("ops")
            else:
                plan.stat = stat_alias_map.get("era")
        else:
            plan.stat = stat_alias_map.get("ops")

    # Most pitching split tables don't carry ERA — if we resolved to ERA via the
    # fallback aliases ("best pitcher") AND a pitching split context is set,
    # swap to OPS. build_split_leaderboard maps to ops_against under the hood.
    # Skip tables that DO have ERA — they want it kept.
    if (plan.split_context is not None
            and plan.split_context.is_pitching
            and plan.split_context.table not in _PITCHING_SPLIT_TABLES_WITH_ERA
            and plan.stat is not None
            and plan.stat.db_column == "era"):
        plan.stat = stat_alias_map.get("ops")

    return plan


# ---------------------------------------------------------------------------
# Season record cache — for detecting "obviously career" thresholds
# ---------------------------------------------------------------------------

_season_record_cache: dict[str, float] = {}


def _load_season_records():
    """Load max single-season values for counting stats from the DB."""
    if _season_record_cache:
        return
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        counting_cols = ["home_runs", "rbi", "hits", "runs", "stolen_bases",
                         "doubles", "triples", "walks", "strikeouts_batting"]
        for col in counting_cols:
            try:
                row = conn.execute(f"SELECT MAX({col}) FROM season_batting_stats").fetchone()
                if row and row[0]:
                    _season_record_cache[col] = float(row[0])
            except Exception:
                pass
        pitching_cols = ["wins", "strikeouts", "saves", "complete_games", "shutouts"]
        for col in pitching_cols:
            try:
                row = conn.execute(f"SELECT MAX({col}) FROM season_pitching_stats").fetchone()
                if row and row[0]:
                    _season_record_cache[col] = float(row[0])
            except Exception:
                pass
        conn.close()
    except Exception:
        pass


def _exceeds_season_record(stat: StatInfo, threshold: float) -> bool:
    """Return True if the threshold exceeds 110% of the all-time single-season record."""
    _load_season_records()
    record = _season_record_cache.get(stat.db_column)
    if record is None:
        return False  # Unknown stat or rate stat — don't override
    return threshold > record * 1.1


# ---------------------------------------------------------------------------
# Compose SQL — build and execute the query from a QueryPlan
# ---------------------------------------------------------------------------

def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _format_rate(value) -> str:
    """Format rate stats: .302 not 0.302."""
    try:
        fv = float(value)
        if fv < 1:
            return f".{int(round(fv * 1000)):03d}"
        return f"{fv:.3f}"
    except (ValueError, TypeError):
        return str(value)


def _format_rate_for_abbrev(value, abbrev: str) -> str:
    """Stat-aware rate formatting.

    ERA-class stats (ERA/WHIP/K/9/BB/9/etc.) conventionally show 2 decimals
    — "1.08", "2.50". Batting rates (AVG/OBP/SLG/OPS) show 3 decimals via
    _format_rate — ".302", "1.422". When rendering a leaderboard cell we
    know the stat abbrev, so dispatch to the right precision.
    """
    if abbrev in _ERA_CLASS_ABBREVS:
        try:
            return f"{float(value):.2f}"
        except (ValueError, TypeError):
            return str(value)
    return _format_rate(value)


_ERA_CLASS_ABBREVS = {"ERA", "WHIP", "K/9", "BB/9", "K/BB", "H/9", "HR/9"}


def _format_threshold_clause(threshold, comparison, abbrev, is_rate=False) -> str:
    """Render a user-facing filter clause for (threshold, comparison, abbrev).

    Examples:
      (30, ">=", "HR")        → "30+ HR"
      (100, "<=", "K")        → "≤100 K"
      (3.50, "<=", "ERA", T)  → "sub-3.50 ERA"  (rate stats use "sub-" prefix;
                                                  ERA-class shows 2 decimals)
      (.300, ">=", "AVG", T)  → ".300+ AVG"     (batting rates show 3 decimals)
      (0, anything, "BB")     → "0 BB"          (zero is unambiguous)

    Centralized so leaderboard / threshold / career-filter titles all render
    consistently. Previously several call sites hardcoded "+" regardless of
    comparison, which silently lied for "under N" / "fewer than N" / lower-
    is-better-stat queries — the SQL filter was correct but the title told
    the user the opposite.

    For rate stats, decimal precision is abbrev-aware:
      - ERA, WHIP, K/9, BB/9, etc. (always ≥ 1) → 2 decimals (3.50 not 3.500)
      - AVG, OBP, SLG, OPS, etc. (≥ 1 only for elite seasons) → 3 decimals
        via _format_rate, which renders ".300" / "1.422" conventionally.
    """
    if threshold is None:
        return abbrev
    try:
        t_int = int(threshold) if threshold == int(threshold) else None
    except (TypeError, ValueError):
        t_int = None
    if is_rate:
        if abbrev in _ERA_CLASS_ABBREVS:
            try:
                t_disp = f"{float(threshold):.2f}"
            except (TypeError, ValueError):
                t_disp = str(threshold)
        else:
            t_disp = _format_rate(str(threshold))
    else:
        t_disp = str(t_int) if t_int is not None else str(threshold)
    if t_int == 0:
        return f"0 {abbrev}"
    if comparison == ">=":
        return f"{t_disp}+ {abbrev}"
    if comparison == "<=":
        return f"sub-{t_disp} {abbrev}" if is_rate else f"≤{t_disp} {abbrev}"
    return f"{comparison} {t_disp} {abbrev}"


def _format_val(stat_col: str, value, is_rate: bool = False) -> str:
    """Format a value for display."""
    if value is None:
        return "--"
    # Percentage stats (0-100 range) — check before rate stats
    if "percentage" in stat_col or stat_col.endswith("_pct"):
        try:
            fv = float(value)
            if fv == int(fv):
                return f"{int(fv)}%"
            return f"{fv:.1f}%"
        except (ValueError, TypeError):
            return str(value)
    if is_rate or stat_col in _RATE_STATS:
        if stat_col in ("era", "whip", "k_per_9", "bb_per_9", "hr_per_9", "k_per_bb",
                         "h_per_9", "hr_per_9", "fip"):
            try:
                return f"{float(value):.2f}"
            except (ValueError, TypeError):
                return str(value)
        return _format_rate(value)
    if stat_col in ("ip_outs", "innings_pitched"):
        try:
            outs = int(value)
            return f"{outs // 3}.{outs % 3}"
        except (ValueError, TypeError):
            return str(value)
    # Derived percentage stats
    if "percentage" in stat_col or stat_col.endswith("_pct"):
        try:
            return f"{float(value):.1f}%"
        except (ValueError, TypeError):
            return str(value)
    try:
        fv = float(value)
        if fv == int(fv):
            return str(int(fv))
    except (ValueError, TypeError):
        pass
    return str(value)


# Natural-language verb phrases per stat for the zero-result sentence.
# IMPORTANT: when a new stat is added to stat_config.json / the DB, add entries
# here too. Missing entries fall back to a generic sentence (still correct, just
# less natural). See CLAUDE.md "Adding a new stat" checklist.

# Singular form: "hit a home run" — used for leaderboards ("most HR") with no
# explicit threshold. Subject/scope are added around this phrase.
_ZERO_BATTING_SINGULAR = {
    "HR": "hit a home run",
    "H": "recorded a hit",
    "RBI": "drove in a run",
    "SB": "stole a base",
    "R": "scored a run",
    "2B": "hit a double",
    "3B": "hit a triple",
    "BB": "drew a walk",
    "IBB": "drew an intentional walk",
    "SO": "struck out",
    "HBP": "were hit by a pitch",
    "CS": "were caught stealing",
    "G": "played a game",
    "AB": "had an at-bat",
    "PA": "had a plate appearance",
    "TB": "had a total base",
    "XBH": "had an extra-base hit",
}
_ZERO_PITCHING_SINGULAR = {
    "W": "won a game",
    "L": "lost a game",
    "SV": "recorded a save",
    "SO": "struck out a batter",
    "K": "struck out a batter",
    "BB": "walked a batter",
    "IP": "pitched an inning",
    "ER": "allowed an earned run",
    "H": "allowed a hit",
    "HR": "allowed a home run",
    "G": "pitched in a game",
    "GS": "made a start",
    "CG": "pitched a complete game",
    "SHO": "pitched a shutout",
}

# Threshold form: "hit 30+ home runs" — used for threshold queries.
# {n} is the threshold value.
_ZERO_BATTING_THRESHOLD = {
    "HR": "hit {n}+ home runs",
    "H": "recorded {n}+ hits",
    "RBI": "drove in {n}+ runs",
    "SB": "stole {n}+ bases",
    "R": "scored {n}+ runs",
    "2B": "hit {n}+ doubles",
    "3B": "hit {n}+ triples",
    "BB": "drew {n}+ walks",
    "SO": "struck out {n}+ times",
    "HBP": "were hit by a pitch {n}+ times",
    "G": "played {n}+ games",
    "AB": "had {n}+ at-bats",
    "PA": "had {n}+ plate appearances",
}
_ZERO_PITCHING_THRESHOLD = {
    "W": "won {n}+ games",
    "L": "lost {n}+ games",
    "SV": "recorded {n}+ saves",
    "SO": "struck out {n}+ batters",
    "K": "struck out {n}+ batters",
    "BB": "walked {n}+ batters",
    "IP": "pitched {n}+ innings",
    "ER": "allowed {n}+ earned runs",
    "H": "allowed {n}+ hits",
    "HR": "allowed {n}+ home runs",
    "G": "pitched in {n}+ games",
    "GS": "made {n}+ starts",
}


def _zero_result_subject(plan: QueryPlan) -> str:
    """Build the subject noun phrase for the zero-result sentence.
    'players', 'pitchers', 'catchers', 'players over 40', 'AL rookies', etc."""
    from .response_builder import _position_label
    # Position overrides the base noun: 'catchers', 'shortstops', etc.
    if plan.position:
        base = _position_label(plan.position).lower()
    else:
        base = "pitchers" if plan.is_pitching else "players"

    prefix_bits = []
    if plan.league:
        prefix_bits.append(plan.league)
    if plan.rookie:
        prefix_bits.append("rookie")
    if plan.bats == "L":
        prefix_bits.append("left-handed")
    elif plan.bats == "R":
        prefix_bits.append("right-handed")
    elif plan.bats == "B":
        prefix_bits.append("switch-hitting")
    if plan.throws == "L" and plan.is_pitching:
        prefix_bits.append("left-handed")
    elif plan.throws == "R" and plan.is_pitching:
        prefix_bits.append("right-handed")
    if plan.pitcher_role:
        prefix_bits.append(plan.pitcher_role)

    subject = " ".join(prefix_bits + [base]) if prefix_bits else base

    suffix_bits = []
    if plan.age_min:
        suffix_bits.append(f"over {plan.age_min}")
    if plan.age_max:
        suffix_bits.append(f"under {plan.age_max}")

    if suffix_bits:
        return f"{subject} {' '.join(suffix_bits)}"
    return subject


def _zero_result_scope(plan: QueryPlan) -> str:
    """Scope phrase for the zero-result sentence. 'in 2024', 'in a single
    season', 'over their career', 'since 2000', or empty."""
    if plan.scope == "all_time":
        return "in a single season"
    if plan.scope == "career":
        return "over their career"
    if plan.scope and plan.scope.startswith("season_"):
        try:
            return f"in {plan.scope.split('_', 1)[1]}"
        except Exception:
            return ""
    if plan.scope and plan.scope.startswith("since_"):
        try:
            return f"since {plan.scope.split('_', 1)[1]}"
        except Exception:
            return ""
    return ""


def _zero_result_sentence(plan: QueryPlan) -> str:
    """Natural zero-result sentence: '0 players over 40 hit a home run in 2024.'

    Falls back to a generic structured sentence for stats not in the verb maps
    (maintain them when new stats are added — see module top)."""
    subject = _zero_result_subject(plan)
    scope = _zero_result_scope(plan)
    scope_suffix = f" {scope}" if scope else ""

    # Pure rate-stat queries don't fit verb templating cleanly. "0 players
    # with a qualifying {stat} in {scope}." is honest and doesn't strain.
    if plan.stat and plan.stat.is_rate:
        return f"0 {subject} had a qualifying {plan.stat.display_name}{scope_suffix}."

    if not plan.stat:
        return f"0 {subject} matched{scope_suffix}."

    abbrev = plan.stat.display_abbrev
    is_pitching = plan.is_pitching

    # Threshold form when user said "X+ HR" or similar
    if plan.threshold is not None:
        threshold_map = _ZERO_PITCHING_THRESHOLD if is_pitching else _ZERO_BATTING_THRESHOLD
        phrase_template = threshold_map.get(abbrev)
        n = int(plan.threshold) if plan.threshold == int(plan.threshold) else plan.threshold
        if phrase_template:
            return f"0 {subject} {phrase_template.format(n=n)}{scope_suffix}."
        # Unknown stat → graceful fallback
        return f"0 {subject} reached {n}+ {plan.stat.display_name}{scope_suffix}."

    # Pure leaderboard — singular form ("hit a home run") reads naturally when
    # filters are restrictive enough that nobody qualifies.
    singular_map = _ZERO_PITCHING_SINGULAR if is_pitching else _ZERO_BATTING_SINGULAR
    phrase = singular_map.get(abbrev)
    if phrase:
        return f"0 {subject} {phrase}{scope_suffix}."
    # Unknown stat
    return f"0 {subject} recorded {plan.stat.display_name}{scope_suffix}."


def _empty_result_pills(plan: QueryPlan) -> str:
    """Generate suggestion pills for empty results — don't leave users at a dead end."""
    pills = []

    # Determine the right stat name — game_log_stat and streak labels
    # take priority over plan.stat (which is often "games" for these types)
    _gl_labels = {
        "hits": "H", "home_runs": "HR", "rbi": "RBI", "runs": "R",
        "stolen_bases": "SB", "walks": "BB", "strikeouts": "K",
        "doubles": "2B", "triples": "3B", "xbh": "XBH",
    }
    if plan.query_type == "game_log_count" and plan.game_log_stat:
        abbrev = _gl_labels.get(plan.game_log_stat, plan.game_log_stat.upper())
        threshold = plan.game_log_threshold
    elif plan.query_type == "streak_sequence" and plan.streak_condition_labels:
        # For streaks, suggest related searches
        raw_label = plan.streak_condition_labels[0]  # "a hit", "a HR", "scoreless"
        # Clean up for pill text: "a hit" → "hitting", "a HR" → "HR", "scoreless" stays
        _streak_pill_labels = {
            "a hit": "hitting", "a HR": "HR", "a BB": "walk",
            "a SB": "stolen base", "an RBI": "RBI", "an XBH": "XBH",
            "reaching base": "on-base", "a W": "win",
        }
        label = _streak_pill_labels.get(raw_label, raw_label)
        streak_len = plan.streak_length
        season = plan.season or date.today().year
        if streak_len:
            pills.append(f"longest {label} streak in {season}")
        pills.append(f"{label} streak leaders")
        if not pills:
            return ""
        return "\n\n" + "\n".join(f"[SUGGEST]{p}[/SUGGEST]" for p in pills[:3])
    else:
        abbrev = plan.stat.display_abbrev if plan.stat else ""
        threshold = plan.threshold

    # Context prefix (handedness, position, role, rookie)
    ctx = []
    if plan.throws:
        ctx.append("RHP" if plan.throws == "R" else "LHP")
    elif plan.bats:
        ctx.append({"L": "LHB", "R": "RHB", "B": "switch hitters"}.get(plan.bats, ""))
    if plan.rookie:
        ctx.append("rookies")
    if plan.pitcher_role:
        ctx.append(f"{plan.pitcher_role}s")
    if plan.position and not plan.is_pitching:
        ctx.append("/".join(plan.position))
    prefix = " ".join(c for c in ctx if c)
    if prefix:
        prefix += " "

    # Format threshold display
    t_display = ""
    if threshold:
        if plan.stat and plan.stat.is_rate and threshold < 1:
            t_display = f".{int(threshold * 1000):03d}+"
        else:
            t_display = f"{int(threshold)}+"

    # For game log counts, suggest "most N+ stat games" instead of raw threshold
    if plan.query_type == "game_log_count" and plan.game_log_threshold:
        season = plan.season or date.today().year
        last_year = season - 1 if season == date.today().year else None
        if last_year:
            pills.append(f"most {plan.game_log_threshold}+ {abbrev} games in {last_year}")
        pills.append(f"most {plan.game_log_threshold}+ {abbrev} games all time")
        pills.append(f"{abbrev} leaders")
    else:
        # Suggest last season
        if plan.season and plan.season == date.today().year and t_display:
            last_year = plan.season - 1
            pills.append(f"{prefix}{t_display} {abbrev} in {last_year}")

        # Suggest all-time if searching a specific season
        if plan.season and t_display and abbrev:
            pills.append(f"{prefix}{t_display} {abbrev} all time")

        # Suggest the stat leaders if we have a stat
        if abbrev:
            pills.append(f"{abbrev} leaders")

    if not pills:
        return ""
    return "\n\n" + "\n".join(f"[SUGGEST]{p}[/SUGGEST]" for p in pills[:3])


def _ambiguous_suggest(plan: QueryPlan) -> str:
    """Generate alternate interpretation for ambiguous batting/pitching stats.
    Truly ambiguous (SO, BB): DIDYOUMEAN before results.
    Less ambiguous (H, HR, etc.): SUGGEST pill after results.

    Reconstructs the user's original query with the opposite context
    instead of a generic "SO leaders (hitters)" label.
    """
    if not plan.ambiguous_stat or not plan.stat:
        return ""
    col = plan.stat.db_column
    truly_ambiguous = col in ("strikeouts", "walks")

    # Reconstruct the query with the opposite interpretation
    oq = plan.original_question.strip()
    if plan.is_pitching:
        # Currently pitching → suggest batting version
        # Insert "by a hitter" or "by hitters" into the query
        alt = f"{oq} (hitters)"
    else:
        # Currently batting → suggest pitching version
        alt = f"{oq} (pitchers)"

    if truly_ambiguous:
        return f"__DIDYOUMEAN__{alt}"
    else:
        return f"\n[SUGGEST]{alt}[/SUGGEST]"


def execute(plan: QueryPlan) -> Optional[str]:
    """Execute a QueryPlan and return formatted response text, or None."""
    if not plan.is_valid:
        return None

    conn = _get_db()
    try:
        result = None
        if plan.query_type == "award_lookup":
            result = _execute_award_lookup(conn, plan)
            if result:
                conn.close()
                return result
            conn.close()
            return None
        if plan.query_type == "award_intersection":
            result = _execute_award_intersection(conn, plan)
            if result:
                conn.close()
                return result
            conn.close()
            return None
        if plan.query_type == "year_comparison":
            result = _execute_year_comparison(conn, plan)
        elif plan.query_type == "streak_sequence":
            # "100 RBI in 3 straight seasons" — per_season + streak_sequence
            # means season-level consistency, not game-level streaks
            if plan.per_season:
                result = _execute_per_season_threshold(conn, plan)
            else:
                result = _execute_streak_sequence(conn, plan)
        elif plan.query_type == "game_log_count":
            result = _execute_game_log_count(conn, plan)
        elif plan.query_type == "game_log_extreme":
            result = _execute_game_log_extreme(conn, plan)
        elif plan.query_type == "team_ranking":
            result = _execute_team_ranking(conn, plan)
        elif plan.query_type == "per_team_leaders":
            result = _execute_per_team_leaders(conn, plan)
        elif plan.query_type == "player_single_season_max":
            result = _execute_player_single_season_max(plan)
        elif plan.query_type == "player_sliding_window":
            result = _execute_player_sliding_window(plan)
        elif plan.team_context is not None:
            result = _execute_team_context_leaderboard(conn, plan)
        elif plan.split_context is not None:
            result = _execute_split_leaderboard(conn, plan)
        elif plan.query_type == "count":
            result = _execute_count(conn, plan)
        elif plan.query_type == "superlative":
            result = _execute_superlative(conn, plan)
        elif plan.query_type == "threshold":
            # career_game_window + threshold = cumulative-over-window
            # filter ("hit .300 in their first 50 games", "10 HR in
            # their first 50 games"). Route to the game-window
            # executor which knows how to aggregate over the window
            # AND apply a threshold filter on the result.
            if plan.career_game_window:
                result = _execute_game_window_leaderboard(conn, plan)
            else:
                result = _execute_threshold(conn, plan)
        elif plan.query_type == "leaderboard":
            result = _execute_leaderboard(conn, plan)

        # Collect all "see also" alternatives, output as single combined DIDYOUMEAN
        see_also = []

        # Auto-fallback: a leaderboard/threshold with INFERRED current-year scope
        # that returned zero rows almost always means the filter itself is niche
        # (e.g. "most HR by a player over 40") rather than "this hasn't happened
        # in 2026 yet." Re-run the same query against all-time single-season
        # records, annotate with a subtitle explaining the switch, and offer the
        # original current-year query as a see-also so the user can pop back.
        _explicit_season = any(p in plan.original_question.lower() for p in [
            "this season", "this year", str(date.today().year)])
        # Zero-result detection: the new _zero_result_sentence always leads with
        # "0 " (e.g. "0 players over 40 hit a home run in 2026."). Older non-
        # leaderboard/threshold executors may still emit "No results found" —
        # match both for safety.
        def _is_zero_result(s: str) -> bool:
            return bool(s) and (s.startswith("0 ") or "No results found" in s)

        if (result and _is_zero_result(result)
                and plan.query_type in ("leaderboard", "threshold")
                and plan.season and plan.season == date.today().year
                and not _explicit_season):
            original_year = plan.season
            plan.scope = "all_time"
            plan.season = None
            if plan.query_type == "leaderboard":
                fallback = _execute_leaderboard(conn, plan)
            else:
                fallback = _execute_threshold(conn, plan)
            if fallback and not _is_zero_result(fallback):
                # Silent fallback — don't surface the zero current-year attempt.
                # The 0 count was just our signal that the inference was wrong;
                # the subtitle only needs to make the chosen scope explicit so
                # the user isn't wondering "which records am I looking at?"
                subtitle_tag = (
                    "[SUBTITLE]Showing all-time single-season records[/SUBTITLE]"
                )
                lines = fallback.split("\n")
                inserted = False
                for i, line in enumerate(lines):
                    if line.strip().startswith("**") and line.strip().endswith("**"):
                        lines.insert(i + 1, subtitle_tag)
                        inserted = True
                        break
                if not inserted:
                    lines.insert(0, subtitle_tag)
                result = "\n".join(lines)
                # Offer the original current-year query as a see-also
                oq = plan.original_question.strip().rstrip("?.!,;:")
                see_also.append(f"{oq} {original_year}")

        # Safety net: pure "best / most / leaders" leaderboard queries
        # returning 0 rows are almost always a misclassification (we picked
        # the wrong table, qualifier was too strict, etc.) — a leaderboard
        # by definition is "rank everyone, show top N." 0 means we asked
        # the wrong SQL. Fall through to LLM rather than ship a confusing
        # "0 players had a qualifying X" message. Threshold queries with
        # explicit user thresholds CAN legitimately return 0 (the user
        # asked for ".400 AVG since 2010" and nobody did it), so they
        # keep the deterministic 0-matched response. Streak / window /
        # split executors handle their own empty-result returns elsewhere.
        if (result and _is_zero_result(result)
                and plan.query_type == "leaderboard"
                and not plan.threshold
                and not plan.extra_filters):
            logger.info(
                "leaderboard_zero_fallthrough question=%r — "
                "treating empty leaderboard as misclassification",
                plan.original_question,
            )
            result = None

        # Alternate interpretation for ambiguous stats
        if result and plan.ambiguous_stat:
            alt = _ambiguous_suggest(plan)
            if alt.startswith("__DIDYOUMEAN__"):
                see_also.append(alt.replace("__DIDYOUMEAN__", ""))
            elif alt:
                result += alt  # Less ambiguous — keep as pill

        # "HR/hits by a pitcher" ambiguity: allowed vs hit
        # Only when user didn't explicitly say "allowed/given up"
        _oq_lower = plan.original_question.lower()
        if (result and plan.is_pitching and plan.stat
                and plan.stat.db_column in ("home_runs", "hits")
                and (not plan.season or plan.season < 2022 or plan.scope in ("career", "all_time"))
                and "by a pitcher" in _oq_lower
                and not any(w in _oq_lower for w in ["allowed", "given up", "surrendered", "gave up"])):
            if plan.stat.db_column == "home_runs":
                see_also.append("most HR hit by a pitcher all time")
            else:
                see_also.append("most hits by a pitcher as a batter all time")

        # "Within vs by" alternate interpretation for team queries
        if result and plan.has_team_context and plan.stat:
            abbrev = plan.stat.display_abbrev
            season = plan.season or date.today().year
            see_also.append(f"{abbrev} leader on each team {season}")

        # Unqualified season: user didn't specify a year, we defaulted to the
        # current in-progress season. Offer the same query at alternate scopes
        # (last year, all time) so the user can pivot without retyping filters.
        # Uses plan.original_question as the base so age/position/rookie/bats/
        # etc. filters carry over verbatim — previously we rebuilt a minimal
        # "{abbrev} leaders" string and silently dropped the user's filters.
        _explicit_season = any(p in plan.original_question.lower() for p in [
            "this season", "this year", str(date.today().year)])
        # Suppress current-season alts when the query already carries an
        # intrinsic non-season scope. Examples:
        #   - "best OPS over their last 100 games"  → career_game_window
        #     scope=recent (inherently spans all history)
        #   - "best OPS in first 50 career games"   → career_game_window
        #     scope=career
        # In both cases plan.season may still be 2026 (default) but no
        # year inference actually drives the answer, so offering "last
        # year / all time" alternatives is misleading.
        _intrinsic_scope = (plan.career_game_window
                            and plan.career_game_window.get("scope")
                                in ("career", "recent"))
        if (result and plan.season and plan.season == date.today().year
                and plan.stat and plan.query_type in ("leaderboard", "threshold")
                and not _explicit_season
                and not _intrinsic_scope):
            oq = plan.original_question.strip().rstrip("?.!,;:")
            last_year = plan.season - 1

            # Early-season alt: offer last year. Later in season (> 40 team
            # games in), the current-season numbers are meaningful enough to
            # not crowd the see-also with last year.
            try:
                gp = conn.execute(
                    "SELECT MAX(games) FROM season_batting_stats WHERE season = ?",
                    (plan.season,)
                ).fetchone()
                games_played = gp[0] if gp and gp[0] else 0
                if games_played < 40:
                    see_also.append(f"{oq} last year")
            except Exception:
                pass

            # All-time is always a useful alt for a current-season query.
            see_also.append(f"{oq} all time")

        # Combine all see-alsos into one DIDYOUMEAN — insert after title, before container
        if see_also and result:
            dym_tag = f"[DIDYOUMEAN]{'|'.join(see_also)}[/DIDYOUMEAN]"
            lines = result.split("\n")
            # Find first non-empty line (the title), insert after it
            for i, line in enumerate(lines):
                if line.strip().startswith("**") and line.strip().endswith("**"):
                    lines.insert(i + 1, dym_tag)
                    break
            else:
                lines.insert(0, dym_tag)  # fallback: prepend
            result = "\n".join(lines)

        return result
    except Exception as e:
        logger.warning("query_engine_error error=%s type=%s plan_type=%s streak_len=%s", e, type(e).__name__, plan.query_type, plan.streak_length)
        plan.execution_error = f"{type(e).__name__}: {e}"
        return None
    finally:
        conn.close()


def _stat_expr(plan: QueryPlan, prefix: str = "s") -> tuple[str, str, str, bool]:
    """Get the SQL expression, display abbrev, display name, and is_rate for the stat.
    Returns (sql_expr, abbrev, name, is_rate)."""
    if plan.derived_stat:
        d = _DERIVED_STATS[plan.derived_stat]
        expr = d["formula"].replace("s.", f"{prefix}.")
        return expr, d["display"], d["name"], d["is_rate"]
    stat = plan.stat
    # innings_pitched is TEXT — use ip_outs (integer) for sorting/computation
    if stat.db_column == "innings_pitched":
        return f"{prefix}.ip_outs", stat.display_abbrev, stat.display_name, stat.is_rate
    return f"{prefix}.{stat.db_column}", stat.display_abbrev, stat.display_name, stat.is_rate


def _table_and_prefix(plan: QueryPlan) -> tuple[str, str]:
    """Pick the right table and alias."""
    if plan.is_pitching:
        return "season_pitching_stats", "sp"
    return "season_batting_stats", "s"


def _build_filters(plan: QueryPlan, prefix: str, conn=None) -> tuple[str, list]:
    """Build WHERE clause fragments and params from plan filters."""
    clauses = []
    params = []

    if plan.league:
        from .response_builder import _league_team_clause
        clauses.append(_league_team_clause(plan.league, prefix))

    if plan.rookie:
        from .response_builder import _rookie_filter
        clauses.append(_rookie_filter(prefix, plan.is_pitching)[5:])  # strip " AND "

    if plan.position and not plan.is_pitching:
        # Career rollup (e.g. "best OPS by a catcher all time") uses a
        # player-level filter on primary career position. Season / all-time-
        # single-season / since-year queries keep the per-season filter.
        if plan.scope == "career":
            from .response_builder import _position_filter_career
            clauses.append(_position_filter_career(plan.position, prefix)[5:])
        else:
            from .response_builder import _position_filter
            clauses.append(_position_filter(plan.position, prefix, f"{prefix}.season")[5:])

    if plan.pitcher_role == "starter":
        clauses.append(f"{prefix}.games_started > {prefix}.games / 2")
    elif plan.pitcher_role == "reliever":
        clauses.append(f"{prefix}.games_started <= {prefix}.games / 2")

    if plan.bats:
        clauses.append(f"p.bats = ?")
        params.append(plan.bats)

    if plan.throws:
        clauses.append(f"p.throws = ?")
        params.append(plan.throws)

    if plan.age_max:
        clauses.append(f"({prefix}.season - CAST(SUBSTR(p.birthdate, 1, 4) AS INT)) < ?")
        params.append(plan.age_max)
        clauses.append("p.birthdate IS NOT NULL")

    if plan.age_min:
        clauses.append(f"({prefix}.season - CAST(SUBSTR(p.birthdate, 1, 4) AS INT)) > ?")
        params.append(plan.age_min)
        clauses.append("p.birthdate IS NOT NULL")

    # Extra stat filters: "with under 10 HR", "with 30+ SB"
    # Prorate IP/PA thresholds ONLY when the query is about the current
    # in-progress season. For all_time / since_YYYY / past season_YYYY /
    # career, the user's threshold is meant literally — "200+ IP" in an
    # all-time single-season query means 200 IP, not 25.
    _current_year = date.today().year
    _is_current_season_scope = plan.scope == f"season_{_current_year}"
    for ef in plan.extra_filters:
        threshold = ef['threshold']
        if (conn and _is_current_season_scope
                and ef['stat'].db_column in ("innings_pitched", "plate_appearances", "ip_outs")):
            try:
                tbl = "season_pitching_stats" if "ip" in ef['stat'].db_column or "inn" in ef['stat'].db_column else "season_batting_stats"
                _tg = conn.execute(f"SELECT MAX(games) FROM {tbl} WHERE season = ?",
                                   (_current_year,)).fetchone()
                _mg = int(_tg[0]) if _tg and _tg[0] else 162
                if _mg < 140:
                    threshold = max(1, int(threshold * _mg / 162))
            except:
                pass
        clauses.append(f"{prefix}.{ef['stat'].db_column} {ef['comparison']} ?")
        params.append(threshold)

    return " AND ".join(clauses), params


_AWARD_LABELS = {
    "MVP": "MVP", "CY": "Cy Young", "ROY": "Rookie of the Year",
    "ALL_STAR": "All-Star", "GG": "Gold Glove", "SS": "Silver Slugger",
    "HOF": "Hall of Famer", "WS_MVP": "World Series MVP",
}


def _award_join(plan: QueryPlan, prefix: str) -> tuple[str, str]:
    """Build JOIN clause and title label for award-filtered queries.
    Returns (join_sql, label) — join_sql is empty if no award filter."""
    if not plan.award_filter:
        return "", ""
    label = _AWARD_LABELS.get(plan.award_filter, plan.award_filter)
    join = f" JOIN awards aw ON aw.player_id = {prefix}.player_id AND aw.season = {prefix}.season AND aw.award = '{plan.award_filter}'"
    return join, label


def _pa_filter(plan: QueryPlan, prefix: str, conn, season: Optional[int] = None) -> tuple[str, str]:
    """Get PA/IP minimum filter for rate stats, prorated for current season.
    Returns (sql_clause, display_label) tuple."""
    stat_col = plan.stat.db_column if plan.stat else ""
    is_rate = (plan.stat and plan.stat.is_rate) or (plan.derived_stat and _DERIVED_STATS[plan.derived_stat]["is_rate"])
    # A counting-stat threshold filter (e.g. "30+ HR", "500+ PA") already gates
    # playing time — stacking a PA floor on top is redundant and wrongly excludes
    # legit high-HR seasons in sub-500 PA. Rate-stat filters (e.g. ".300+ AVG")
    # are meaningless without a PA floor, so they still need one.
    has_counting_threshold = any(
        ef.get("stat") and not ef["stat"].is_rate
        for ef in plan.extra_filters
    )
    # Apply PA/IP minimums for rate stats, or for pure "fewest X" sorts with no
    # threshold filter (otherwise "fewest walks" returns players with 1 PA and 0 BB).
    needs_minimum = is_rate or (plan.sort_asc and not has_counting_threshold)
    if not needs_minimum:
        return "", ""

    # For current in-progress season, prorate against team_games to allow
    # "on pace" semantics. For everything else (all_time, since_YYYY, past
    # season_YYYY, career), use the full-season qualifier literally —
    # historical seasons are complete, their rate stats don't need proration.
    _current_year = datetime.now().year
    _is_current_season_scope = plan.scope == f"season_{_current_year}"
    if _is_current_season_scope:
        qual_season = season or _current_year
    else:
        # Full-season qualifier (3.1 × 162 = 502 PA, 1.0 × 162 = 162 IP = 486 outs)
        qual_season = None  # signal: use 162-game baseline below

    if plan.is_pitching:
        if qual_season is None:
            ip_min = 486  # 162 IP for a standard 162-game season
        else:
            ip_min = _qual_min_ip_outs(conn, qual_season)
        # Rookies often don't reach a full 162-IP qualifying season —
        # first big-league year is usually 100-150 IP for starters and far
        # less for relievers (Strider 2022 = 131, Skenes 2024 = 133, etc.).
        # Without lowering the floor, "best rookie ERA" returns 0. Use
        # 100 IP (300 outs) — the threshold for "real meaningful workload."
        if plan.rookie and ip_min > 300:
            ip_min = 300
        # Data-integrity check: require batters_faced >= ip_outs (1 BF per
        # out minimum). Modern MLB has ~1.3-1.5 BF/out; a record with
        # BF significantly < ip_outs has incomplete play-tracking
        # (common in pre-1910 data and Negro Leagues entries). Without
        # this filter, "best rookie ERA all time" surfaced 1903-1904
        # pitchers with 0.00 ERA over 300+ outs but only 30 BF, which
        # is mathematically impossible. Same defensive pattern as
        # monthly_aggregates.
        ip_display = f"{ip_min // 3}.{ip_min % 3}"
        if _is_current_season_scope and season and ip_min < 486:
            label = f"Showing pitchers on pace for 162+ IP ({ip_display} IP minimum)"
        elif plan.rookie:
            label = f"Min. {ip_display} IP (rookie qualifier)."
        else:
            label = f"Min. {ip_display} IP."
        return (f" AND {prefix}.ip_outs >= {ip_min} "
                f"AND {prefix}.batters_faced >= {prefix}.ip_outs", label)
    else:
        if qual_season is None:
            pa_min = 502  # 3.1 PA × 162 scheduled games
        else:
            pa_min = _qual_min_pa(conn, qual_season)
        # Rookies same story for batting — rookie position players often
        # don't get a full 502 PA in their debut year. Lower to 300 PA
        # (roughly 75 games of starting work).
        if plan.rookie and pa_min > 300:
            pa_min = 300
        if _is_current_season_scope and season and pa_min < 502:
            label = f"Showing hitters on pace for 502+ PA ({pa_min} PA minimum)"
        elif plan.rookie:
            label = f"Min. {pa_min} PA (rookie qualifier)."
        else:
            label = f"Min. {pa_min} PA."
        return f" AND {prefix}.plate_appearances >= {pa_min}", label


# ---------------------------------------------------------------------------
# Executors — one per query type
# ---------------------------------------------------------------------------

def _execute_award_lookup(conn, plan: QueryPlan) -> Optional[str]:
    """Pure award query — 'who won MVP last year', 'Judge All-Star selections'."""
    award_code = plan.award_filter
    _AWARD_DISPLAY = {
        "MVP": "MVP", "CY": "Cy Young", "ROY": "Rookie of the Year",
        "ALL_STAR": "All-Star", "GG": "Gold Glove", "SS": "Silver Slugger",
        "HOF": "Hall of Fame", "WS_MVP": "World Series MVP",
        "ALCS_MVP": "ALCS MVP", "NLCS_MVP": "NLCS MVP",
    }
    display = _AWARD_DISPLAY.get(award_code, award_code)
    cur = conn.cursor()

    # Player-specific: "how many All-Star selections does Judge have"
    if plan.player_name:
        sanitized = plan.player_name.replace("'", "''")
        cur.execute("""
            SELECT a.season, a.league FROM awards a
            JOIN players p ON a.player_id = p.player_id
            WHERE p.name = ? AND a.award = ?
            ORDER BY a.season
        """, (plan.player_name, award_code))
        rows = cur.fetchall()
        if not rows:
            return f"{plan.player_name} has no {display} awards on record."
        if len(rows) == 1:
            league_str = f" ({rows[0][1]})" if rows[0][1] else ""
            return f"{plan.player_name} won the {display}{league_str} in {rows[0][0]}."
        years = [str(r[0]) for r in rows]
        return f"{plan.player_name} has {len(rows)} {display} selections: {', '.join(years)}."

    # Season-specific: "who won MVP last year", "2024 Cy Young"
    if plan.season:
        where = "WHERE a.award = ? AND a.season = ?"
        params: list = [award_code, plan.season]
        if plan.league:
            where += " AND a.league = ?"
            params.append(plan.league)
        cur.execute(f"""
            SELECT p.name, a.league FROM awards a
            JOIN players p ON a.player_id = p.player_id
            {where} ORDER BY a.league
        """, params)
        rows = cur.fetchall()
        if not rows:
            return f"No {display} winner found for {plan.season}."
        if len(rows) == 1:
            league_str = f" ({rows[0][1]})" if rows[0][1] else ""
            return f"The {plan.season} {display}{league_str} was **{rows[0][0]}**."
        parts = []
        for name, league in rows:
            league_str = f" ({league})" if league else ""
            parts.append(f"**{name}**{league_str}")
        return f"The {plan.season} {display} winners were {' and '.join(parts)}."

    # General: "MVP winners", "Hall of Fame members"
    if award_code == "HOF":
        cur.execute("""
            SELECT p.name, a.season FROM awards a
            JOIN players p ON a.player_id = p.player_id
            WHERE a.award = 'HOF' ORDER BY a.season DESC, p.name LIMIT 50
        """)
        rows = cur.fetchall()
        if not rows:
            return None
        parts = []
        current_year = None
        for name, year in rows:
            if year != current_year:
                current_year = year
                parts.append(f"\n**{year}**: {name}")
            else:
                parts[-1] += f", {name}"
        return f"Hall of Fame inductees (recent):\n{''.join(parts)}"

    # Default: show recent winners
    cur.execute("""
        SELECT p.name, a.season, a.league FROM awards a
        JOIN players p ON a.player_id = p.player_id
        WHERE a.award = ? ORDER BY a.season DESC, a.league LIMIT 20
    """, (award_code,))
    rows = cur.fetchall()
    if not rows:
        return None
    parts = []
    for name, season, league in rows:
        league_str = f" ({league})" if league else ""
        parts.append(f"{season}{league_str}: **{name}**")
    return f"Recent {display} winners:\n" + "\n".join(parts)


def _execute_award_intersection(conn, plan: QueryPlan) -> Optional[str]:
    """Players who won two awards in the same season.

    Powers "every player who has won both MVP and Gold Glove in the same
    season" and similar two-award same-season questions.
    """
    a1 = plan.award_filter
    a2 = plan.award_filter_secondary
    _AWARD_DISPLAY = {
        "MVP": "MVP", "CY": "Cy Young", "ROY": "Rookie of the Year",
        "ALL_STAR": "All-Star", "GG": "Gold Glove", "SS": "Silver Slugger",
        "HOF": "Hall of Fame", "WS_MVP": "World Series MVP",
        "ALCS_MVP": "ALCS MVP", "NLCS_MVP": "NLCS MVP",
    }
    d1 = _AWARD_DISPLAY.get(a1, a1)
    d2 = _AWARD_DISPLAY.get(a2, a2)
    cur = conn.cursor()

    where_extra = ""
    params: list = [a1, a2]
    if plan.season:
        where_extra += " AND a1.season = ?"
        params.append(plan.season)
    if plan.league:
        where_extra += " AND a1.league = ?"
        params.append(plan.league)

    cur.execute(f"""
        SELECT p.name, a1.season, a1.league
        FROM awards a1
        JOIN awards a2 ON a1.player_id = a2.player_id AND a1.season = a2.season
        JOIN players p ON a1.player_id = p.player_id
        WHERE a1.award = ? AND a2.award = ?{where_extra}
        ORDER BY a1.season DESC, p.name
    """, params)
    rows = cur.fetchall()
    if not rows:
        scope = f" in {plan.season}" if plan.season else ""
        return f"No player has won both {d1} and {d2} in the same season{scope}."

    # Group identical (name, season) pairs across leagues if any (rare).
    parts = []
    last_year = None
    for name, season, league in rows:
        league_str = f" ({league})" if league else ""
        if season != last_year:
            parts.append(f"\n**{season}**: {name}{league_str}")
            last_year = season
        else:
            parts[-1] += f", {name}{league_str}"

    header = f"Players who won both {d1} and {d2} in the same season ({len(rows)} total):"
    return header + "".join(parts)


def _execute_recurring_half_leaderboard(conn, plan: QueryPlan) -> Optional[str]:
    """Multi-season recurring-window leaderboard.

    Powers queries like "best ERA in the first half of a season since 2020"
    or "most home runs in the second half of any season since 2015". For
    each year in [since_year, current_year], applies the season's specific
    half-window (Apr 1 → ASG break for first; ASG break → end-of-season for
    second), aggregates game logs by (player_id, season), qualifies with a
    half-season floor, then ranks the resulting player-season rows.

    Output rows label both player and season ("Max Fried (2024): 1.85")
    since the same player can appear multiple times — once per qualifying
    half-season.
    """
    if not plan.stat and not plan.derived_stat:
        return None
    is_pitching = plan.is_pitching
    gl_table = "game_pitching_logs" if is_pitching else "game_batting_logs"
    gl = "gl"
    is_rate = (plan.stat and plan.stat.is_rate) or (
        plan.derived_stat and _DERIVED_STATS.get(plan.derived_stat, {}).get("is_rate", False)
    )

    # Sort direction: lower-is-better rate stats invert
    is_lower_better = plan.stat and plan.stat.db_column in _LOWER_IS_BETTER
    if plan.sort_asc and is_lower_better:
        order = "DESC"
    elif plan.sort_asc:
        order = "ASC"
    elif is_lower_better:
        order = "ASC"
    else:
        order = "DESC"

    # Build per-year date windows. Skip 1945 (WWII) and 2020 (COVID) — those
    # years have no ASG date, so we can't define a half-window for them.
    today = date.today()
    year_windows = []
    year_params: list = []
    for year in range(plan.since_year, today.year + 1):
        if year not in _ASG_DATES:
            continue
        asg = _ASG_DATES[year]
        if plan.recurring_half == "first_half":
            year_windows.append(f"({gl}.season = ? AND {gl}.date >= ? AND {gl}.date <= ?)")
            year_params.extend([year, f"{year}-04-01", asg])
        else:  # second_half
            # No per-season end_date stored — cap at Oct 31 (covers regular
            # season + early postseason; gametype filter further narrows).
            year_windows.append(f"({gl}.season = ? AND {gl}.date >= ? AND {gl}.date <= ?)")
            year_params.extend([year, asg, f"{year}-10-31"])

    if not year_windows:
        return None
    window_clause = "(" + " OR ".join(year_windows) + ")"

    # Stat expression — game-log columns. Same formulas as the date_range
    # executor (_execute_leaderboard's date_range branch).
    if is_pitching:
        rate_formulas = {
            "era": f"9.0 * SUM({gl}.earned_runs) / NULLIF(SUM({gl}.ip_outs) / 3.0, 0)",
            "whip": f"CAST(SUM({gl}.hits) + SUM({gl}.walks) AS REAL) / NULLIF(SUM({gl}.ip_outs) / 3.0, 0)",
            "k_per_9": f"9.0 * SUM({gl}.strikeouts) / NULLIF(SUM({gl}.ip_outs) / 3.0, 0)",
            "bb_per_9": f"9.0 * SUM({gl}.walks) / NULLIF(SUM({gl}.ip_outs) / 3.0, 0)",
        }
        # Half-season qualifier: ~80 IP (240 outs) — half of the standard 162 IP.
        having_clause = f"HAVING SUM({gl}.ip_outs) >= 240"
    else:
        rate_formulas = {
            "batting_avg": f"CAST(SUM({gl}.hits) AS REAL) / NULLIF(SUM({gl}.at_bats), 0)",
            "obp": (f"CAST(SUM({gl}.hits) + SUM({gl}.walks) + SUM(COALESCE({gl}.hit_by_pitch, 0)) AS REAL) / "
                    f"NULLIF(SUM({gl}.at_bats) + SUM({gl}.walks) + SUM(COALESCE({gl}.hit_by_pitch, 0)) + SUM(COALESCE({gl}.sacrifice_flies, 0)), 0)"),
            "slg": (f"CAST(SUM({gl}.hits) - SUM({gl}.doubles) - SUM({gl}.triples) - SUM({gl}.home_runs) "
                    f"+ 2*SUM({gl}.doubles) + 3*SUM({gl}.triples) + 4*SUM({gl}.home_runs) AS REAL) / NULLIF(SUM({gl}.at_bats), 0)"),
            "ops": (f"(CAST(SUM({gl}.hits) + SUM({gl}.walks) + SUM(COALESCE({gl}.hit_by_pitch, 0)) AS REAL) / "
                    f"NULLIF(SUM({gl}.at_bats) + SUM({gl}.walks) + SUM(COALESCE({gl}.hit_by_pitch, 0)) + SUM(COALESCE({gl}.sacrifice_flies, 0)), 0)) + "
                    f"(CAST(SUM({gl}.hits) - SUM({gl}.doubles) - SUM({gl}.triples) - SUM({gl}.home_runs) "
                    f"+ 2*SUM({gl}.doubles) + 3*SUM({gl}.triples) + 4*SUM({gl}.home_runs) AS REAL) / NULLIF(SUM({gl}.at_bats), 0))"),
            "iso": (f"CAST(SUM({gl}.doubles) + 2*SUM({gl}.triples) + 3*SUM({gl}.home_runs) AS REAL) / NULLIF(SUM({gl}.at_bats), 0)"),
        }
        # Half-season PA qualifier: ~250 PA (half of the standard 502).
        having_clause = f"HAVING SUM({gl}.plate_appearances) >= 250"

    stat_col = plan.stat.db_column if plan.stat else (plan.derived_stat or "")
    if is_rate and stat_col in rate_formulas:
        stat_expr = rate_formulas[stat_col]
    elif plan.stat and not is_rate:
        stat_expr = f"SUM({gl}.{stat_col})"
        having_clause = ""  # No qualifier on counting stats — every total stands
    elif plan.derived_stat and plan.derived_stat in rate_formulas:
        stat_expr = rate_formulas[plan.derived_stat]
    else:
        return None

    # Build full WHERE — recurring window + filters_str via EXISTS subquery
    # (filters_str references season-table columns).
    table, prefix = _table_and_prefix(plan)
    filters_str, fparams = _build_filters(plan, prefix, conn)
    where_parts = [window_clause]
    full_params = list(year_params)
    if filters_str:
        ss_table = "season_pitching_stats" if is_pitching else "season_batting_stats"
        ss_alias = "ss"
        ss_filter = filters_str.replace(f"{prefix}.", f"{ss_alias}.")
        where_parts.append(
            f"EXISTS (SELECT 1 FROM {ss_table} {ss_alias} "
            f"WHERE {ss_alias}.player_id = {gl}.player_id "
            f"AND {ss_alias}.season = {gl}.season "
            f"AND {ss_filter})"
        )
        full_params += fparams
    where = "WHERE " + " AND ".join(where_parts)

    sql = (
        f"SELECT p.name, {gl}.season, {stat_expr} AS stat_val "
        f"FROM {gl_table} {gl} "
        f"JOIN players p ON {gl}.player_id = p.player_id "
        f"{where} "
        f"GROUP BY {gl}.player_id, {gl}.season "
        f"{having_clause} "
        f"ORDER BY stat_val {order} LIMIT ?"
    )
    cur = conn.cursor()
    try:
        cur.execute(sql, tuple(full_params + [plan.limit]))
    except Exception as e:
        return f"Could not run recurring-half query: {e}"
    rows = cur.fetchall()

    half_label = "First Half" if plan.recurring_half == "first_half" else "Second Half"
    direction_label = "Worst " if (plan.sort_asc and not is_rate) else ""
    if plan.sort_asc and is_rate:
        direction_label = "Worst " if is_lower_better else "Lowest "
    title_stat = plan.stat.display_name if plan.stat else (
        _DERIVED_STATS[plan.derived_stat]["name"] if plan.derived_stat else ""
    )
    title = f"**{direction_label}{title_stat} — {half_label} of a Season Since {plan.since_year}**\n"
    if not rows:
        return f"{title}\nNo players qualified."

    abbrev = plan.stat.display_abbrev if plan.stat else (
        _DERIVED_STATS[plan.derived_stat]["display"] if plan.derived_stat else ""
    )
    parts = [title, "[TIP]Tap a player name for their full profile.[/TIP]", "[LEADERBOARD]"]
    parts.append(f"HEADER: {abbrev}, Year")
    for name, season_yr, stat_val in rows:
        if is_rate:
            val_str = _format_rate_for_abbrev(stat_val, abbrev)
        else:
            val_str = _format_val(stat_col, stat_val, is_rate=False)
        parts.append(f"ROW {name}: {val_str}, {season_yr}")
    parts.append("[/LEADERBOARD]")
    if is_rate:
        qual_note = "Min. 80.0 IP" if is_pitching else "Min. 250 PA"
        parts.append(f"\n_{qual_note} in the half-season window._")
    return "\n".join(parts)


def _execute_month_grouped_leaderboard(conn, plan: QueryPlan) -> Optional[str]:
    """Top single-month performances across history.

    Powers queries like "most home runs in a single month all time" or
    "best OPS in any calendar month since 2010". Reads from the
    materialized monthly aggregates tables (one row per
    player-season-month) instead of aggregating game_batting_logs /
    game_pitching_logs at query time. Sub-100ms vs the 20+s a full
    game-log GROUP BY took. The aggregates tables are repopulated by
    the cron pipeline after each game-log refresh.

    Scope handling:
      - since_year set → year >= since_year (with optional end_year cap)
      - season set     → year = season
      - else           → all-time

    Qualifiers:
      - batting rate stats: 80 PA in the month
      - pitching rate stats: 90 ip_outs (30 IP) in the month
      - counting stats: no qualifier
    """
    if not plan.stat and not plan.derived_stat:
        return None
    is_pitching = plan.is_pitching
    is_rate = (plan.stat and plan.stat.is_rate) or (
        plan.derived_stat and _DERIVED_STATS.get(plan.derived_stat, {}).get("is_rate", False)
    )

    is_lower_better = plan.stat and plan.stat.db_column in _LOWER_IS_BETTER
    if plan.sort_asc and is_lower_better:
        order = "DESC"
    elif plan.sort_asc:
        order = "ASC"
    elif is_lower_better:
        order = "ASC"
    else:
        order = "DESC"

    # Materialized aggregates tables are pre-grouped to one row per
    # player-season-month. Stat expressions reference columns directly —
    # no SUM, no GROUP BY. Rate stats compose from monthly component
    # totals already on the row.
    agg_table = "monthly_pitching_aggregates" if is_pitching else "monthly_batting_aggregates"
    ma = "ma"

    where_parts: list = []
    where_params: list = []
    if plan.since_year:
        where_parts.append(f"{ma}.season >= ?")
        where_params.append(plan.since_year)
    if plan.end_year:
        where_parts.append(f"{ma}.season <= ?")
        where_params.append(plan.end_year)
    if plan.season and not plan.since_year:
        where_parts.append(f"{ma}.season = ?")
        where_params.append(plan.season)

    if is_pitching:
        rate_formulas = {
            "era": f"9.0 * {ma}.earned_runs / NULLIF({ma}.ip_outs / 3.0, 0)",
            "whip": f"CAST({ma}.hits + {ma}.walks AS REAL) / NULLIF({ma}.ip_outs / 3.0, 0)",
            "k_per_9": f"9.0 * {ma}.strikeouts / NULLIF({ma}.ip_outs / 3.0, 0)",
            "bb_per_9": f"9.0 * {ma}.walks / NULLIF({ma}.ip_outs / 3.0, 0)",
        }
        # Sample-size qualifier: 30 IP minimum AND batters_faced >= ip_outs.
        # Modern MLB pitchers face ~1.3-1.5 batters per recorded out; the
        # floor of 1.0 BF/out filters records where batters_faced wasn't
        # reliably captured (some Negro Leagues entries have ip_outs and
        # ER but a partial BF count, surfacing as bogus 0.10-0.40 ratios).
        # Real dominant months — Hershiser Sep 1988 at 1.22, Gooden Sep
        # 1985 at 1.24, Joe Black Aug 1947 at 1.45 — all clear the floor.
        rate_qualifier = f"{ma}.ip_outs >= 90 AND {ma}.batters_faced >= {ma}.ip_outs"
    else:
        rate_formulas = {
            "batting_avg": f"CAST({ma}.hits AS REAL) / NULLIF({ma}.at_bats, 0)",
            "obp": (f"CAST({ma}.hits + {ma}.walks + {ma}.hit_by_pitch AS REAL) / "
                    f"NULLIF({ma}.at_bats + {ma}.walks + {ma}.hit_by_pitch + {ma}.sacrifice_flies, 0)"),
            "slg": (f"CAST({ma}.hits - {ma}.doubles - {ma}.triples - {ma}.home_runs "
                    f"+ 2*{ma}.doubles + 3*{ma}.triples + 4*{ma}.home_runs AS REAL) / NULLIF({ma}.at_bats, 0)"),
            "ops": (f"(CAST({ma}.hits + {ma}.walks + {ma}.hit_by_pitch AS REAL) / "
                    f"NULLIF({ma}.at_bats + {ma}.walks + {ma}.hit_by_pitch + {ma}.sacrifice_flies, 0)) + "
                    f"(CAST({ma}.hits - {ma}.doubles - {ma}.triples - {ma}.home_runs "
                    f"+ 2*{ma}.doubles + 3*{ma}.triples + 4*{ma}.home_runs AS REAL) / NULLIF({ma}.at_bats, 0))"),
            "iso": f"CAST({ma}.doubles + 2*{ma}.triples + 3*{ma}.home_runs AS REAL) / NULLIF({ma}.at_bats, 0)",
        }
        rate_qualifier = f"{ma}.plate_appearances >= 80"

    stat_col = plan.stat.db_column if plan.stat else (plan.derived_stat or "")
    if is_rate and stat_col in rate_formulas:
        stat_expr = rate_formulas[stat_col]
        where_parts.append(rate_qualifier)
    elif plan.stat and not is_rate:
        # Counting stat — direct column read. The agg tables expose the
        # same column names as the source game-log tables for these.
        stat_expr = f"{ma}.{stat_col}"
    elif plan.derived_stat and plan.derived_stat in rate_formulas:
        stat_expr = rate_formulas[plan.derived_stat]
        where_parts.append(rate_qualifier)
    else:
        return None

    # Optional handedness/league/position filters via EXISTS on season-stats
    table, prefix = _table_and_prefix(plan)
    filters_str, fparams = _build_filters(plan, prefix, conn)
    if filters_str:
        ss_table = "season_pitching_stats" if is_pitching else "season_batting_stats"
        ss_alias = "ss"
        ss_filter = filters_str.replace(f"{prefix}.", f"{ss_alias}.")
        where_parts.append(
            f"EXISTS (SELECT 1 FROM {ss_table} {ss_alias} "
            f"WHERE {ss_alias}.player_id = {ma}.player_id "
            f"AND {ss_alias}.season = {ma}.season "
            f"AND {ss_filter})"
        )
        where_params += fparams

    where = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

    # Tiebreak rate stats by sample size (more IP / more PA means a more
    # impressive equal rate). Without this, ties at 0.00 ERA fall back to
    # alphabetical and lesser-known short stretches outrank Hershiser Sep
    # 1988 (55 IP / 0 ER), Gooden Sep 1985 (44 IP / 0 ER), etc. Counting
    # stats don't tie nearly as often; leave their ORDER BY single-key.
    if is_rate:
        tiebreak_col = f"{ma}.ip_outs" if is_pitching else f"{ma}.plate_appearances"
        order_by = f"stat_val {order}, {tiebreak_col} DESC"
    else:
        order_by = f"stat_val {order}"

    sql = (
        f"SELECT p.name, {ma}.season, {ma}.month, {stat_expr} AS stat_val "
        f"FROM {agg_table} {ma} "
        f"JOIN players p ON {ma}.player_id = p.player_id "
        f"{where} "
        f"ORDER BY {order_by} LIMIT ?"
    )
    cur = conn.cursor()
    try:
        cur.execute(sql, tuple(where_params + [plan.limit]))
        rows = cur.fetchall()
    except sqlite3.OperationalError as e:
        # Aggregates table missing (pre-rebuild on a fresh deploy) — caller
        # falls through to Haiku rather than returning a confusing error.
        if "no such table" in str(e).lower():
            return None
        return f"Could not run month-grouped query: {e}"
    except Exception as e:
        return f"Could not run month-grouped query: {e}"

    direction_label = "Fewest " if (plan.sort_asc and not is_rate) else ""
    if plan.sort_asc and is_rate:
        direction_label = "Worst " if is_lower_better else "Lowest "
    title_stat = plan.stat.display_name if plan.stat else (
        _DERIVED_STATS[plan.derived_stat]["name"] if plan.derived_stat else ""
    )
    if plan.since_year and plan.end_year:
        scope_phrase = f"{plan.since_year}-{plan.end_year}"
    elif plan.since_year:
        scope_phrase = f"Since {plan.since_year}"
    elif plan.season:
        scope_phrase = str(plan.season)
    else:
        scope_phrase = "All-Time"
    title = f"**{direction_label}{title_stat} in a Single Month — {scope_phrase}**\n"
    if not rows:
        return f"{title}\nNo qualifying months found."

    abbrev = plan.stat.display_abbrev if plan.stat else (
        _DERIVED_STATS[plan.derived_stat]["display"] if plan.derived_stat else ""
    )
    month_names = ["", "January", "February", "March", "April", "May", "June",
                   "July", "August", "September", "October", "November", "December"]
    parts = [title, "[TIP]Tap a player name for their full profile.[/TIP]", "[LEADERBOARD]"]
    parts.append(f"HEADER: {abbrev}, Month")
    for name, season_yr, mo, stat_val in rows:
        if is_rate:
            val_str = _format_rate_for_abbrev(stat_val, abbrev)
        else:
            val_str = _format_val(stat_col, stat_val, is_rate=False)
        mo_idx = int(mo) if mo is not None else 0
        month_label = f"{month_names[mo_idx]} {season_yr}" if 1 <= mo_idx <= 12 else f"{season_yr}"
        parts.append(f"ROW {name}: {val_str}, {month_label}")
    parts.append("[/LEADERBOARD]")
    if is_rate:
        qual_note = "Min. 30.0 IP" if is_pitching else "Min. 80 PA"
        parts.append(f"\n_{qual_note} in the month._")
    return "\n".join(parts)


def _execute_game_window_leaderboard(conn, plan: QueryPlan) -> Optional[str]:
    """First/last N games leaderboard — four scope variants.

    Aggregates the first or last N game-log rows per partition (ordered
    chronologically), then ranks the resulting aggregates.

    Scopes:
      - season: first/last N games WITHIN one season (single year filter,
        partition by player_id)
      - career: first N rows ever — career debut window (no year filter,
        partition by player_id, ASC inner order)
      - recent: last N rows ever — most recent N appearances (no year
        filter, partition by player_id, DESC inner order)
      - per-season any-year: triggered when window scope=="season" AND
        plan.scope=="all_time" (user said "any season"). No year filter,
        partition by (player_id, season) so each player-season is its
        own window, then rank across all such windows. Output rows
        include the season label.

    Players who haven't reached N games in their partition are excluded
    by HAVING COUNT(*) >= n.
    """
    if not plan.stat and not plan.derived_stat:
        return None
    win = plan.career_game_window or {}
    direction = win.get("direction", "first")
    n_val = int(win.get("n", 0) or 0)
    scope = win.get("scope", "season")
    if n_val < 2:
        return None

    # Per-season any-year mode: window scope says "season" but plan.scope
    # is "all_time" (the any-season trigger fired). Partition each
    # (player, season) independently, no year filter.
    per_season_any = (scope == "season" and plan.scope == "all_time")

    is_pitching = plan.is_pitching
    gl_table = "game_pitching_logs" if is_pitching else "game_batting_logs"
    is_rate = (plan.stat and plan.stat.is_rate) or (
        plan.derived_stat and _DERIVED_STATS.get(plan.derived_stat, {}).get("is_rate", False)
    )

    is_lower_better = plan.stat and plan.stat.db_column in _LOWER_IS_BETTER
    if plan.sort_asc and is_lower_better:
        order = "DESC"
    elif plan.sort_asc:
        order = "ASC"
    elif is_lower_better:
        order = "ASC"
    else:
        order = "DESC"

    # Inner ORDER BY direction:
    #  - first/season + first/career: ASC (chronologically earliest rows)
    #  - last/recent: DESC (chronologically latest rows)
    inner_order = "ASC" if direction == "first" else "DESC"

    # Build inner WHERE — narrows the partition source rows by scope
    inner_where_parts: list = []
    inner_params: list = []
    if scope == "season" and not per_season_any:
        # Single explicit season
        season_yr = plan.season or date.today().year
        inner_where_parts.append("gl.season = ?")
        inner_params.append(season_yr)
    elif scope == "recent":
        # "Last N games" with no year scope: user means recent / currently
        # active players' last N. A retired player's "last 100 games" from
        # 1965 isn't what someone asks for when they say "last 100 games"
        # — they mean Judge / Trout / Ohtani's most recent 100. Restrict
        # to last ~3 seasons so the ROW_NUMBER OVER PARTITION BY scan is
        # over ~500K rows instead of ~5M, and aligns with user intent.
        # Players who've been retired more than 3 years don't qualify
        # for "recent." For users who genuinely want all-time tail
        # windows, the "career" scope ("first/last N career games") is
        # the right phrasing.
        recent_floor = date.today().year - 2
        inner_where_parts.append("gl.season >= ?")
        inner_params.append(recent_floor)
    # career, per_season_any → no year filter

    # Optional handedness/league/position filters via EXISTS on season-stats
    table, prefix = _table_and_prefix(plan)
    filters_str, fparams = _build_filters(plan, prefix, conn)
    if filters_str:
        ss_table = "season_pitching_stats" if is_pitching else "season_batting_stats"
        ss_alias = "ss"
        ss_filter = filters_str.replace(f"{prefix}.", f"{ss_alias}.")
        inner_where_parts.append(
            f"EXISTS (SELECT 1 FROM {ss_table} {ss_alias} "
            f"WHERE {ss_alias}.player_id = gl.player_id "
            f"AND {ss_alias}.season = gl.season "
            f"AND {ss_filter})"
        )
        inner_params += fparams

    inner_where = ("WHERE " + " AND ".join(inner_where_parts)) if inner_where_parts else ""

    # Stat formulas — operate on the windowed CTE alias `r`
    if is_pitching:
        rate_formulas = {
            "era": "9.0 * SUM(r.earned_runs) / NULLIF(SUM(r.ip_outs) / 3.0, 0)",
            "whip": "CAST(SUM(r.hits) + SUM(r.walks) AS REAL) / NULLIF(SUM(r.ip_outs) / 3.0, 0)",
            "k_per_9": "9.0 * SUM(r.strikeouts) / NULLIF(SUM(r.ip_outs) / 3.0, 0)",
            "bb_per_9": "9.0 * SUM(r.walks) / NULLIF(SUM(r.ip_outs) / 3.0, 0)",
        }
    else:
        rate_formulas = {
            "batting_avg": "CAST(SUM(r.hits) AS REAL) / NULLIF(SUM(r.at_bats), 0)",
            "obp": ("CAST(SUM(r.hits) + SUM(r.walks) + SUM(COALESCE(r.hit_by_pitch, 0)) AS REAL) / "
                    "NULLIF(SUM(r.at_bats) + SUM(r.walks) + SUM(COALESCE(r.hit_by_pitch, 0)) + SUM(COALESCE(r.sacrifice_flies, 0)), 0)"),
            "slg": ("CAST(SUM(r.hits) - SUM(r.doubles) - SUM(r.triples) - SUM(r.home_runs) "
                    "+ 2*SUM(r.doubles) + 3*SUM(r.triples) + 4*SUM(r.home_runs) AS REAL) / NULLIF(SUM(r.at_bats), 0)"),
            "ops": ("(CAST(SUM(r.hits) + SUM(r.walks) + SUM(COALESCE(r.hit_by_pitch, 0)) AS REAL) / "
                    "NULLIF(SUM(r.at_bats) + SUM(r.walks) + SUM(COALESCE(r.hit_by_pitch, 0)) + SUM(COALESCE(r.sacrifice_flies, 0)), 0)) + "
                    "(CAST(SUM(r.hits) - SUM(r.doubles) - SUM(r.triples) - SUM(r.home_runs) "
                    "+ 2*SUM(r.doubles) + 3*SUM(r.triples) + 4*SUM(r.home_runs) AS REAL) / NULLIF(SUM(r.at_bats), 0))"),
            "iso": "CAST(SUM(r.doubles) + 2*SUM(r.triples) + 3*SUM(r.home_runs) AS REAL) / NULLIF(SUM(r.at_bats), 0)",
        }

    stat_col = plan.stat.db_column if plan.stat else (plan.derived_stat or "")
    if is_rate and stat_col in rate_formulas:
        stat_expr = rate_formulas[stat_col]
    elif plan.stat and not is_rate:
        stat_expr = f"SUM(r.{stat_col})"
    elif plan.derived_stat and plan.derived_stat in rate_formulas:
        stat_expr = rate_formulas[plan.derived_stat]
    else:
        return None

    # Rate-stat qualifier — keep pitchers with one bat-PA out of an OPS
    # leaderboard, and short-relievers out of an ERA leaderboard. Scales
    # with N: ~2 PA/game for batting, ~1 IP/game for pitching, with a
    # floor so very small Ns still filter chaff.
    rate_qual = ""
    if is_rate and plan.stat:
        if is_pitching:
            ip_outs_min = max(30, n_val * 3)  # ~1 IP per game in window
            rate_qual = f" AND SUM(r.ip_outs) >= {ip_outs_min}"
        else:
            pa_min = max(20, n_val * 2)  # ~2 PA per game in window
            rate_qual = f" AND SUM(r.plate_appearances) >= {pa_min}"

    # CTE: row-number rows within each partition (player, or player+season).
    # Per-season-any mode partitions by (player_id, season) so each player-
    # season is independently windowed. All other modes partition by
    # player_id only.
    if per_season_any:
        partition_clause = "PARTITION BY gl.player_id, gl.season"
        select_extra = ", r.season AS row_season"
        group_by_clause = "GROUP BY r.player_id, r.season"
    else:
        partition_clause = "PARTITION BY gl.player_id"
        select_extra = ""
        group_by_clause = "GROUP BY r.player_id"

    # Optional threshold filter: when the user wrote "10 HRs in their
    # first 50 games" or "hit .300 in their first 50 games" — cumulative
    # over the window, return all qualifying players (not just top N
    # ranked). Threshold semantics share the same comparison direction
    # logic as elsewhere: ">=" by default, "<=" for under/fewer-than
    # phrasing or zero-threshold edge cases.
    threshold_having = ""
    threshold_having_params: list = []
    if plan.threshold is not None:
        op = plan.comparison if plan.comparison in (">=", "<=", ">", "<", "=") else ">="
        # threshold==0 with default >= is always true on counting stats.
        # Flip to <= so "0 BB in their first 5 games" reads as "no walks
        # over the window" rather than "any walk count over the window."
        if plan.threshold == 0 and op == ">=" and not is_rate:
            op = "<="
        threshold_having = f" AND stat_val {op} ?"
        threshold_having_params = [plan.threshold]

    # Fast path: career scope uses the materialized `career_game_num` column
    # so the SQL is a simple WHERE filter instead of ROW_NUMBER OVER PARTITION.
    # The window-function variant takes 22-44s on 5M batting rows; this path
    # is 2-3s. Only kicks in when partition is per-player (not per_season_any)
    # and there are no scope-specific filters (which "career" doesn't have —
    # it's the no-year-filter case).
    use_career_fast_path = (
        scope == "career"
        and not per_season_any
        and not inner_where_parts  # no extra inner filters
    )

    if use_career_fast_path:
        # Translate inner stat_expr (which references the windowed alias `r`)
        # to use `gl` directly — same column names, just different alias.
        gl_stat_expr = stat_expr.replace("r.", "gl.")
        gl_rate_qual = rate_qual.replace("r.", "gl.")

        # Filter rows to the player's first or last N career games using
        # the materialized career_game_num. For "first N" it's a direct
        # column filter; for "last N" we need each player's max to compute
        # the tail window.
        if direction == "first":
            window_filter = "WHERE gl.career_game_num <= ?"
            window_params: list = [n_val]
        else:  # "last" / tail-of-career
            window_filter = (
                "JOIN (SELECT player_id, MAX(career_game_num) AS max_g "
                f"FROM {gl_table} GROUP BY player_id) m "
                "ON m.player_id = gl.player_id "
                "WHERE gl.career_game_num > m.max_g - ?"
            )
            window_params = [n_val]

        # Stat-table filters via EXISTS (handedness, league, position, etc.)
        # were captured in inner_where_parts via `_build_filters` earlier;
        # since we early-out when inner_where_parts is non-empty, no filters
        # apply on this path.
        sql = (
            f"SELECT p.name, {gl_stat_expr} AS stat_val, COUNT(*) AS games "
            f"FROM {gl_table} gl "
            f"JOIN players p ON gl.player_id = p.player_id "
            f"{window_filter} "
            f"GROUP BY gl.player_id "
            f"HAVING COUNT(*) >= ?{gl_rate_qual}{threshold_having} "
            f"ORDER BY stat_val {order} LIMIT ?"
        )
        sql_params = tuple(window_params + [n_val] + threshold_having_params + [plan.limit])
    else:
        sql = (
            f"WITH ranked AS ("
            f"  SELECT gl.*, ROW_NUMBER() OVER ("
            f"    {partition_clause} ORDER BY gl.date {inner_order}, gl.game_number {inner_order}"
            f"  ) AS rn FROM {gl_table} gl {inner_where}"
            f") "
            f"SELECT p.name{select_extra}, {stat_expr} AS stat_val, COUNT(*) AS games "
            f"FROM ranked r "
            f"JOIN players p ON r.player_id = p.player_id "
            f"WHERE r.rn <= ? "
            f"{group_by_clause} "
            f"HAVING COUNT(*) >= ?{rate_qual}{threshold_having} "
            f"ORDER BY stat_val {order} LIMIT ?"
        )
        sql_params = tuple(inner_params + [n_val, n_val] + threshold_having_params + [plan.limit])
    cur = conn.cursor()
    try:
        cur.execute(sql, sql_params)
    except Exception as e:
        # Some game-log tables may not have game_number — retry without
        # the secondary ordering. Doubleheaders can swap order but the
        # effect on N-game windows is tiny.
        if "game_number" in str(e):
            sql_fallback = sql.replace(f", gl.game_number {inner_order}", "")
            try:
                cur.execute(sql_fallback, sql_params)
            except Exception as e2:
                return f"Could not run game-window query: {e2}"
        else:
            return f"Could not run game-window query: {e}"
    rows = cur.fetchall()

    direction_label = "Fewest " if (plan.sort_asc and not is_rate) else ""
    if plan.sort_asc and is_rate:
        direction_label = "Worst " if is_lower_better else "Lowest "
    title_stat = plan.stat.display_name if plan.stat else (
        _DERIVED_STATS[plan.derived_stat]["name"] if plan.derived_stat else ""
    )
    if per_season_any:
        scope_phrase = (
            f"First {n_val} Games of Any Season" if direction == "first"
            else f"Last {n_val} Games of Any Season"
        )
    elif scope == "season":
        season_yr = plan.season or date.today().year
        scope_phrase = f"First {n_val} Games of {season_yr}" if direction == "first" else f"Last {n_val} Games of {season_yr}"
    elif scope == "career":
        scope_phrase = f"First {n_val} Career Games" if direction == "first" else f"Last {n_val} Career Games"
    else:  # recent
        scope_phrase = f"Last {n_val} Games"
    title = f"**{direction_label}{title_stat} — {scope_phrase}**\n"
    if not rows:
        return f"{title}\nNo qualifying players found."

    abbrev = plan.stat.display_abbrev if plan.stat else (
        _DERIVED_STATS[plan.derived_stat]["display"] if plan.derived_stat else ""
    )
    parts = [title, "[TIP]Tap a player name for their full profile.[/TIP]", "[LEADERBOARD]"]
    if per_season_any:
        parts.append(f"HEADER: {abbrev}, Year")
    else:
        parts.append(f"HEADER: {abbrev}")
    for row in rows:
        name = row[0]
        if per_season_any:
            row_season = row[1]
            stat_val = row[2]
        else:
            stat_val = row[1]
        if is_rate:
            val_str = _format_rate_for_abbrev(stat_val, abbrev)
        else:
            val_str = _format_val(stat_col, stat_val, is_rate=False)
        if per_season_any:
            parts.append(f"ROW {name}: {val_str}, {row_season}")
        else:
            parts.append(f"ROW {name}: {val_str}")
    parts.append("[/LEADERBOARD]")
    return "\n".join(parts)


def _execute_leaderboard(conn, plan: QueryPlan) -> Optional[str]:
    """Standard stat leaderboard."""
    # Recurring-window queries ("first half of a season since 2020") aggregate
    # game logs across multiple years' Apr-Jul windows. They share the same
    # game-logs aggregation shape as date_range but with a multi-season WHERE
    # built from _ASG_DATES — handle in a dedicated executor.
    if plan.recurring_half:
        return _execute_recurring_half_leaderboard(conn, plan)
    if plan.month_grouped:
        return _execute_month_grouped_leaderboard(conn, plan)
    if plan.career_game_window:
        return _execute_game_window_leaderboard(conn, plan)
    table, prefix = _table_and_prefix(plan)
    stat_expr, abbrev, name, is_rate = _stat_expr(plan, prefix)
    filters_str, params = _build_filters(plan, prefix, conn)
    pa_label = ""  # Will be set by _pa_filter if applicable

    # Determine sort
    # "worst" (sort_asc=True) inverts the natural direction:
    # - For normal stats: worst = ASC (lowest values)
    # - For lower-is-better stats (ERA, WHIP): worst = DESC (highest values)
    is_lower_better = plan.stat and plan.stat.db_column in _LOWER_IS_BETTER
    if plan.sort_asc and is_lower_better:
        order = "DESC"  # worst ERA = highest
    elif plan.sort_asc:
        order = "ASC"   # worst OPS = lowest
    elif is_lower_better:
        order = "ASC"   # best ERA = lowest
    else:
        order = "DESC"

    award_join, award_label = _award_join(plan, prefix)

    direction_label = "Fewest " if plan.sort_asc and not is_rate else "Worst " if plan.sort_asc else ""
    position_label = ""
    if plan.position:
        from .response_builder import _position_label
        position_label = _position_label(plan.position) + " "
    rookie_label = "Rookie " if plan.rookie else ""
    role_label = "Starting " if plan.pitcher_role == "starter" else "Relief " if plan.pitcher_role == "reliever" else ""
    bats_label = "Left-Handed " if plan.bats == "L" else "Right-Handed " if plan.bats == "R" else "Switch-Hitting " if plan.bats == "B" else ""
    if plan.throws:
        bats_label = "Left-Handed " if plan.throws == "L" else "Right-Handed "
    age_label = f"Under {plan.age_max} " if plan.age_max else f"Over {plan.age_min} " if plan.age_min else ""

    if plan.scope.startswith("season_"):
        yr = plan.season or datetime.now().year
        pa, pa_label = _pa_filter(plan, prefix, conn, yr)
        where = f"WHERE {prefix}.season = ?"
        query_params = [yr] + params
        if filters_str:
            where += f" AND {filters_str}"
        where += pa
        # Exclude zeros for counting stats (don't show players with 0 HR in "most HR" queries)
        if not is_rate and not plan.sort_asc:
            where += f" AND {stat_expr} > 0"

        has_age_filter = plan.age_max or plan.age_min
        age_select = f", ({prefix}.season - CAST(SUBSTR(p.birthdate, 1, 4) AS INT)) AS player_age" if has_age_filter else ""
        extra_selects = ", ".join(f"{prefix}.{ef['stat'].db_column}" for ef in plan.extra_filters if ef.get('stat'))
        extra_select_clause = f", {extra_selects}" if extra_selects else ""

        cur = conn.cursor()
        cur.execute(
            f"SELECT p.name, {stat_expr} AS stat_val{age_select}{extra_select_clause} "
            f"FROM {table} {prefix} "
            f"JOIN players p ON {prefix}.player_id = p.player_id "
            f"{award_join} "
            f"{where} "
            f"ORDER BY stat_val {order} LIMIT ?",
            tuple(query_params + [plan.limit]),
        )
        rows = cur.fetchall()
        scope_label = str(yr)

    elif plan.scope == "all_time" or plan.scope.startswith("since_"):
        pa, pa_label = _pa_filter(plan, prefix, conn)
        # Build WHERE clauses and params in lockstep — appending each clause's
        # params at the same time we append its '?' placeholders. Previously we
        # seeded query_params with `params` up-front but added filters_str (which
        # produced those params) at the end of where_parts, so the bindings
        # mis-aligned: a query like "most wins by a LHP since 2020" wound up
        # executing `sp.season >= 'L' AND p.throws = 2020` and returning zero.
        where_parts = []
        query_params: list = []
        if plan.since_year:
            where_parts.append(f"{prefix}.season >= ?")
            query_params.append(plan.since_year)
        if plan.end_year:
            where_parts.append(f"{prefix}.season <= ?")
            query_params.append(plan.end_year)
        if filters_str:
            where_parts.append(filters_str)
            query_params += params
        if pa:
            where_parts.append(pa[5:])  # strip " AND "
        if plan.active_only:
            this_year = date.today().year
            last_year = this_year - 1
            active_table = "season_pitching_stats" if plan.is_pitching else "season_batting_stats"
            where_parts.append(
                f"EXISTS (SELECT 1 FROM {active_table} act "
                f"WHERE act.player_id = {prefix}.player_id AND act.season >= ?)"
            )
            query_params.append(last_year)

        where = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
        if plan.since_year and plan.end_year:
            scope_label = f"{plan.since_year}-{plan.end_year}"
        elif plan.since_year:
            scope_label = f"Since {plan.since_year}"
        else:
            scope_label = "All-Time"

        # For counting stats with "since YYYY" scope, aggregate across
        # seasons per player ("most wins by LHP since 2020" → cumulative
        # wins). When the user explicitly said "in a season" / "single
        # season", the single_season_triggers detector promoted scope to
        # "all_time" — that intent is "best single-season since YYYY",
        # which falls through to per-season ranking with year column.
        # Rookie queries always per-season (one season per player).
        if plan.scope.startswith("since_") and not is_rate and not plan.rookie:
            if plan.derived_stat:
                # Derived formula: replace "s.col" with "SUM(s.col)" for aggregation
                d = _DERIVED_STATS[plan.derived_stat]
                agg_formula = d["formula"]
                for req_col in d["requires"]:
                    agg_formula = agg_formula.replace(f"s.{req_col}", f"SUM(s.{req_col})")
                agg_expr = agg_formula.replace("s.", f"{prefix}.")
            elif plan.stat:
                agg_expr = f"SUM({prefix}.{plan.stat.db_column})"
            else:
                return None
            cur = conn.cursor()
            cur.execute(
                f"SELECT p.name, {agg_expr} AS stat_val "
                f"FROM {table} {prefix} "
                f"JOIN players p ON {prefix}.player_id = p.player_id "
                f"{award_join} "
                f"{where} "
                f"GROUP BY p.player_id "
                f"ORDER BY stat_val {order} LIMIT ?",
                tuple(query_params + [plan.limit]),
            )
        else:
            # Rate stats or all-time: show individual seasons
            extra_selects = ", ".join(f"{prefix}.{ef['stat'].db_column}" for ef in plan.extra_filters if ef.get('stat'))
            extra_select_clause = f", {extra_selects}" if extra_selects else ""
            has_age_filter = plan.age_max or plan.age_min
            age_select = f", ({prefix}.season - CAST(SUBSTR(p.birthdate, 1, 4) AS INT)) AS player_age" if has_age_filter else ""
            cur = conn.cursor()
            cur.execute(
                f"SELECT p.name, {stat_expr} AS stat_val, {prefix}.season{age_select}{extra_select_clause} "
                f"FROM {table} {prefix} "
                f"JOIN players p ON {prefix}.player_id = p.player_id "
                f"{award_join} "
                f"{where} "
                f"ORDER BY stat_val {order} LIMIT ?",
                tuple(query_params + [plan.limit]),
            )
        rows = cur.fetchall()

    elif plan.scope == "date_range":
        # Aggregate from game logs between since_date and today
        gl_table = "game_pitching_logs" if plan.is_pitching else "game_batting_logs"
        gl = "gl"

        # Floor the start date at the season opener (March 25) to avoid spring training
        since_date = plan.since_date
        date_was_floored = False
        try:
            requested_start = datetime.strptime(since_date, "%Y-%m-%d").date()
            current_year = date.today().year
            # Season opener is typically March 25 (could store per year later)
            season_opener = date(current_year, 3, 25)
            if requested_start.year == current_year and requested_start < season_opener:
                since_date = season_opener.isoformat()
                date_was_floored = True
        except:
            pass

        query_params = [since_date]

        # Rate stat formulas from game log columns
        if plan.is_pitching:
            _gl_rate_formulas = {
                "era": f"9.0 * SUM({gl}.earned_runs) / NULLIF(SUM({gl}.ip_outs) / 3.0, 0)",
                "whip": f"CAST(SUM({gl}.hits) + SUM({gl}.walks) AS REAL) / NULLIF(SUM({gl}.ip_outs) / 3.0, 0)",
                "k_per_9": f"9.0 * SUM({gl}.strikeouts) / NULLIF(SUM({gl}.ip_outs) / 3.0, 0)",
                "bb_per_9": f"9.0 * SUM({gl}.walks) / NULLIF(SUM({gl}.ip_outs) / 3.0, 0)",
            }
            # Prorated IP minimum based on date range. Mirrors the batting
            # PA prorating below. 1 IP/day floor approximates a qualifying
            # starter's pace (162 IP / 162 team-games). Without this,
            # "Best ERA in May this year" with only ~6 days played returns
            # 0 rows (every pitcher under 10 IP) and falls through to LLM.
            try:
                start = datetime.strptime(plan.since_date, "%Y-%m-%d").date()
                if plan.end_date:
                    end = datetime.strptime(plan.end_date, "%Y-%m-%d").date()
                    days_in_range = (min(end, date.today()) - start).days + 1
                else:
                    days_in_range = (date.today() - start).days + 1
                # 3 outs/day = 1 IP/day average. Floor 18 outs (6 IP),
                # ceiling 240 outs (80 IP, half-season).
                min_outs = max(18, min(days_in_range * 3, 240))
            except Exception:
                min_outs = 30
            min_pa_sql = f"HAVING SUM({gl}.ip_outs) >= {min_outs}"
        else:
            _gl_rate_formulas = {
                "batting_avg": f"CAST(SUM({gl}.hits) AS REAL) / NULLIF(SUM({gl}.at_bats), 0)",
                "obp": (f"CAST(SUM({gl}.hits) + SUM({gl}.walks) + SUM(COALESCE({gl}.hit_by_pitch, 0)) AS REAL) / "
                        f"NULLIF(SUM({gl}.at_bats) + SUM({gl}.walks) + SUM(COALESCE({gl}.hit_by_pitch, 0)) + SUM(COALESCE({gl}.sacrifice_flies, 0)), 0)"),
                "slg": (f"CAST(SUM({gl}.hits) - SUM({gl}.doubles) - SUM({gl}.triples) - SUM({gl}.home_runs) "
                        f"+ 2*SUM({gl}.doubles) + 3*SUM({gl}.triples) + 4*SUM({gl}.home_runs) AS REAL) / NULLIF(SUM({gl}.at_bats), 0)"),
                "ops": (f"(CAST(SUM({gl}.hits) + SUM({gl}.walks) + SUM(COALESCE({gl}.hit_by_pitch, 0)) AS REAL) / "
                        f"NULLIF(SUM({gl}.at_bats) + SUM({gl}.walks) + SUM(COALESCE({gl}.hit_by_pitch, 0)) + SUM(COALESCE({gl}.sacrifice_flies, 0)), 0)) + "
                        f"(CAST(SUM({gl}.hits) - SUM({gl}.doubles) - SUM({gl}.triples) - SUM({gl}.home_runs) "
                        f"+ 2*SUM({gl}.doubles) + 3*SUM({gl}.triples) + 4*SUM({gl}.home_runs) AS REAL) / NULLIF(SUM({gl}.at_bats), 0))"),
                "iso": (f"CAST(SUM({gl}.doubles) + 2*SUM({gl}.triples) + 3*SUM({gl}.home_runs) AS REAL) / NULLIF(SUM({gl}.at_bats), 0)"),
            }
            # Prorated PA minimum based on date range
            # ~2.7 PA per calendar day for a full-time player (162G × 4PA / 243 days)
            # Use 1.5 PA/day as qualification floor (catches most regulars).
            # Honor end_date for closed ranges — otherwise a single-month rate
            # leaderboard ("OPS leaders in July") computed days to TODAY and
            # demanded ~400 PA, zeroing every row (matches the pitching branch).
            try:
                start = datetime.strptime(plan.since_date, "%Y-%m-%d").date()
                if plan.end_date:
                    end = datetime.strptime(plan.end_date, "%Y-%m-%d").date()
                    days_in_range = (min(end, date.today()) - start).days + 1
                else:
                    days_in_range = (date.today() - start).days + 1
                min_pa = max(10, min(int(days_in_range * 1.5), 400))
            except:
                min_pa = 50
            min_pa_sql = f"HAVING SUM({gl}.plate_appearances) >= {min_pa}"

        stat_col = plan.stat.db_column if plan.stat else (plan.derived_stat or "")

        if is_rate and stat_col in _gl_rate_formulas:
            stat_expr = _gl_rate_formulas[stat_col]
        elif plan.stat and not is_rate:
            stat_expr = f"SUM({gl}.{stat_col})"
            min_pa_sql = ""  # No PA minimum for counting stats
        else:
            # Try derived stat
            if plan.derived_stat and plan.derived_stat in _gl_rate_formulas:
                stat_expr = _gl_rate_formulas[plan.derived_stat]
            else:
                return None  # Can't compute this stat from game logs

        # Build WHERE — clauses + params in lockstep
        where_parts = [f"{gl}.date >= ?"]
        # Closed range: "in the first half of YEAR" sets both since_date and
        # end_date so we get start_of_season → ASG break.
        if plan.end_date:
            where_parts.append(f"{gl}.date <= ?")
            query_params.append(plan.end_date)
        # Apply filter_str (handedness, role, league, position, rookie, etc.) —
        # required by queries like "best ERA in the second half by left-handed
        # starters". Pre-fix this branch dropped the filter silently.
        if filters_str:
            # filter_str references the season table prefix (e.g. "sp.team"),
            # but our FROM is game logs aliased as gl. Use an EXISTS subquery
            # against the season table to apply the filter.
            ss_table = "season_pitching_stats" if plan.is_pitching else "season_batting_stats"
            ss_alias = "ss"
            ss_filter = filters_str.replace(f"{prefix}.", f"{ss_alias}.")
            where_parts.append(
                f"EXISTS (SELECT 1 FROM {ss_table} {ss_alias} "
                f"WHERE {ss_alias}.player_id = {gl}.player_id "
                f"AND {ss_alias}.season = {gl}.season "
                f"AND {ss_filter})"
            )
            query_params += params
        if plan.active_only:
            this_year = date.today().year
            last_year = this_year - 1
            active_table = "season_pitching_stats" if plan.is_pitching else "season_batting_stats"
            where_parts.append(
                f"EXISTS (SELECT 1 FROM {active_table} act "
                f"WHERE act.player_id = {gl}.player_id AND act.season >= ?)"
            )
            query_params.append(last_year)

        where = f"WHERE {' AND '.join(where_parts)}"

        cur = conn.cursor()
        cur.execute(
            f"SELECT p.name, {stat_expr} AS stat_val "
            f"FROM {gl_table} {gl} "
            f"JOIN players p ON {gl}.player_id = p.player_id "
            f"{where} "
            f"GROUP BY {gl}.player_id "
            f"{min_pa_sql} "
            f"ORDER BY stat_val {order} LIMIT ?",
            tuple(query_params + [plan.limit]),
        )
        rows = cur.fetchall()
        # Format date for display
        try:
            start_dt = datetime.strptime(since_date, "%Y-%m-%d")
            if plan.end_date:
                from datetime import timedelta as _td
                end_dt = datetime.strptime(plan.end_date, "%Y-%m-%d")
                # Closed range labels, in priority order:
                #   start=Apr 1 + end matches ASG break for year   → "First Half YYYY"
                #   start=day 1 of month + end=last day same month → "{Month} YYYY"
                #   anything else                                  → date span
                _asg = _ASG_DATES.get(start_dt.year)
                _is_first_half = (start_dt.month == 4 and start_dt.day == 1
                                  and _asg and plan.end_date == _asg)
                # Last day of start_dt.month
                if start_dt.month == 12:
                    _next_first = datetime(start_dt.year + 1, 1, 1)
                else:
                    _next_first = datetime(start_dt.year, start_dt.month + 1, 1)
                _last_day_same_month = (_next_first - _td(days=1)).day
                _is_full_month = (start_dt.day == 1
                                  and end_dt.year == start_dt.year
                                  and end_dt.month == start_dt.month
                                  and end_dt.day == _last_day_same_month)
                if _is_first_half:
                    scope_label = f"First Half {start_dt.year}"
                elif _is_full_month:
                    scope_label = f"{start_dt.strftime('%B %Y')}"
                else:
                    scope_label = (
                        f"{start_dt.strftime('%B %-d, %Y')} "
                        f"– {end_dt.strftime('%B %-d, %Y')}"
                    )
            else:
                scope_label = f"Since {start_dt.strftime('%B %-d, %Y')}"
                if date_was_floored:
                    scope_label += " (season opener)"
        except:
            scope_label = f"Since {since_date}"

    elif plan.scope == "career":
        # Career needs GROUP BY with aggregate formulas for rate stats
        where_parts = []
        query_params = list(params)
        if filters_str:
            where_parts.append(filters_str)
        # Active player filter: has stats in current or previous year
        if plan.active_only:
            this_year = date.today().year
            last_year = this_year - 1
            active_table = "season_pitching_stats" if plan.is_pitching else "season_batting_stats"
            where_parts.append(
                f"EXISTS (SELECT 1 FROM {active_table} act "
                f"WHERE act.player_id = {prefix}.player_id AND act.season >= ?)"
            )
            query_params.append(last_year)
        where = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""

        if is_rate:
            # Career rate stat formulas
            _career_rate_formulas = {
                "batting_avg": ("CAST(SUM({p}.hits) AS REAL) / NULLIF(SUM({p}.at_bats), 0)",
                                "HAVING SUM({p}.at_bats) >= 5000"),
                "obp": ("CAST(SUM({p}.hits) + SUM({p}.walks) + SUM(COALESCE({p}.hit_by_pitch, 0)) AS REAL) / "
                        "NULLIF(SUM({p}.at_bats) + SUM({p}.walks) + SUM(COALESCE({p}.hit_by_pitch, 0)) + SUM(COALESCE({p}.sacrifice_flies, 0)), 0)",
                        "HAVING SUM({p}.at_bats) >= 5000"),
                "slg": ("CAST((SUM({p}.hits) - SUM({p}.doubles) - SUM({p}.triples) - SUM({p}.home_runs)) + 2*SUM({p}.doubles) + 3*SUM({p}.triples) + 4*SUM({p}.home_runs) AS REAL) / NULLIF(SUM({p}.at_bats), 0)",
                        "HAVING SUM({p}.at_bats) >= 5000"),
                "ops": ("(CAST(SUM({p}.hits) + SUM({p}.walks) + SUM(COALESCE({p}.hit_by_pitch, 0)) AS REAL) / "
                        "NULLIF(SUM({p}.at_bats) + SUM({p}.walks) + SUM(COALESCE({p}.hit_by_pitch, 0)) + SUM(COALESCE({p}.sacrifice_flies, 0)), 0)) + "
                        "(CAST((SUM({p}.hits) - SUM({p}.doubles) - SUM({p}.triples) - SUM({p}.home_runs)) + 2*SUM({p}.doubles) + 3*SUM({p}.triples) + 4*SUM({p}.home_runs) AS REAL) / NULLIF(SUM({p}.at_bats), 0))",
                        "HAVING SUM({p}.at_bats) >= 5000"),
                "iso": ("CAST(SUM({p}.doubles) + 2*SUM({p}.triples) + 3*SUM({p}.home_runs) AS REAL) / NULLIF(SUM({p}.at_bats), 0)",
                        "HAVING SUM({p}.at_bats) >= 5000"),
                "babip": ("CAST(SUM({p}.hits) - SUM({p}.home_runs) AS REAL) / NULLIF(SUM({p}.at_bats) - SUM({p}.strikeouts) - SUM({p}.home_runs) + SUM(COALESCE({p}.sacrifice_flies, 0)), 0)",
                          "HAVING SUM({p}.at_bats) >= 5000"),
                # Pitching rate stats
                "era": ("9.0 * SUM({p}.earned_runs) / NULLIF(SUM({p}.ip_outs) / 3.0, 0)",
                        "HAVING SUM({p}.ip_outs) >= 3000"),
                "whip": ("CAST(SUM({p}.hits) + SUM({p}.walks) AS REAL) / NULLIF(SUM({p}.ip_outs) / 3.0, 0)",
                         "HAVING SUM({p}.ip_outs) >= 3000"),
                "k_per_9": ("9.0 * SUM({p}.strikeouts) / NULLIF(SUM({p}.ip_outs) / 3.0, 0)",
                            "HAVING SUM({p}.ip_outs) >= 3000"),
                "bb_per_9": ("9.0 * SUM({p}.walks) / NULLIF(SUM({p}.ip_outs) / 3.0, 0)",
                             "HAVING SUM({p}.ip_outs) >= 3000"),
                "k_per_bb": ("CAST(SUM({p}.strikeouts) AS REAL) / NULLIF(SUM({p}.walks), 0)",
                             "HAVING SUM({p}.ip_outs) >= 3000"),
            }
            stat_col = plan.stat.db_column if plan.stat else ""
            if stat_col not in _career_rate_formulas:
                return None  # Can't compute this career rate stat

            formula_template, having = _career_rate_formulas[stat_col]
            formula = formula_template.format(p=prefix)
            having = having.format(p=prefix)

            # Position filter dramatically narrows the qualifying universe.
            # Pitchers don't accumulate 5000 career AB; catchers / DHs /
            # short-career players also won't. Lower the qualifier so
            # "best batting AVG by a pitcher" doesn't return zero rows.
            # 200 AB ≈ ~50 games of meaningful hitting; reasonable for the
            # pitcher-as-batter universe (Bumgarner, Wainwright, Greinke,
            # Ohtani-pre-DH-era types).
            if plan.position and stat_col in (
                    "batting_avg", "obp", "slg", "ops", "iso", "babip"):
                having = having.replace(">= 5000", ">= 200")

            cur = conn.cursor()
            cur.execute(
                f"SELECT p.name, {formula} AS stat_val "
                f"FROM {table} {prefix} "
                f"JOIN players p ON {prefix}.player_id = p.player_id "
                f"{where} "
                f"GROUP BY p.player_id "
                f"{having} "
                f"ORDER BY stat_val {order} LIMIT ?",
                tuple(query_params + [plan.limit]),
            )
        else:
            # Career counting stat — simple SUM
            cur = conn.cursor()
            cur.execute(
                f"SELECT p.name, SUM({prefix}.{plan.stat.db_column}) AS stat_val "
                f"FROM {table} {prefix} "
                f"JOIN players p ON {prefix}.player_id = p.player_id "
                f"{where} "
                f"GROUP BY p.player_id "
                f"ORDER BY stat_val {order} LIMIT ?",
                tuple(query_params + [plan.limit]),
            )
        rows = cur.fetchall()
        scope_label = "Career (Active)" if plan.active_only else "Career"
    else:
        return None

    if not rows:
        return _zero_result_sentence(plan) + _empty_result_pills(plan)

    # Format
    award_title = f"{award_label} " if award_label else ""
    title_prefix = f"{direction_label}{age_label}{bats_label}{position_label}{rookie_label}{role_label}{award_title}"
    # Extra filter label: "with ≤10 HR", "with 30+ SB"
    # Build filter labels — detect prorated IP/PA thresholds early in season
    filter_label = ""
    proration_subtitle = ""
    # Check if we're early in the season (max team games < 140)
    _max_team_games = 162
    if plan.season:
        try:
            _tg = conn.execute("SELECT MAX(games) FROM season_batting_stats WHERE season = ?", (plan.season,)).fetchone()
            _max_team_games = int(_tg[0]) if _tg and _tg[0] else 162
        except:
            pass

    for ef in plan.extra_filters:
        ef_stat = ef["stat"]
        ef_val = int(ef["threshold"]) if ef["threshold"] == int(ef["threshold"]) else ef["threshold"]
        is_ip_pa = ef_stat.db_column in ("innings_pitched", "plate_appearances", "ip_outs")

        if is_ip_pa and _max_team_games < 140:
            # Prorated — change title to "on pace for" and add explainer
            prorated_val = max(1, int(ef_val * _max_team_games / 162))
            unit = "IP" if "inn" in ef_stat.db_column or "ip" in ef_stat.db_column else "PA"
            if ef["comparison"] == "<=":
                filter_label += f" (on pace for ≤{ef_val} {unit})"
            else:
                filter_label += f" (on pace for {ef_val}+ {unit})"
            proration_subtitle = f"Pro-rating {ef_val} {unit} for current season"
        elif ef["comparison"] == "<=":
            filter_label += f" with ≤{ef_val} {ef_stat.display_abbrev}"
        else:
            filter_label += f" with {ef_val}+ {ef_stat.display_abbrev}"

    # has_year: True when rows contain a season column (index 2).
    # Aggregated counting stats (since_YYYY grouped by player) only have name + stat_val.
    has_year = (plan.scope in ("all_time",) or plan.scope.startswith("since_")) and len(rows[0]) > 2 if rows else False
    title = f"**{scope_label} {title_prefix}{name} Leaders{filter_label}**\n" if not has_year else f"**{title_prefix}{name} Leaders{filter_label} ({scope_label})**\n"

    user_has_ip_pa_filter = bool(proration_subtitle)
    parts = [title]
    if proration_subtitle:
        parts.append(f"[SUBTITLE]{proration_subtitle}[/SUBTITLE]")
    elif pa_label and "on pace" in pa_label:
        parts.append(f"[SUBTITLE]{pa_label}[/SUBTITLE]")
    parts.append("[TIP]Tap a player name for their full profile.[/TIP]")
    parts.append("[LEADERBOARD]")
    stat_col_key = plan.stat.db_column if plan.stat else (plan.derived_stat or "")
    has_age_col = (plan.age_max or plan.age_min) and not has_year
    n_extra = len(plan.extra_filters)
    extra_headers = [ef["stat"].display_abbrev for ef in plan.extra_filters if ef.get("stat")]

    # Build header (cap at _MAX_DISPLAY_COLS)
    header_parts = [abbrev]
    if has_year:
        header_parts.append("Year")
    elif has_age_col:
        header_parts.append("Age")
    header_parts.extend(extra_headers)
    if len(header_parts) > _MAX_DISPLAY_COLS:
        header_parts = header_parts[:_MAX_DISPLAY_COLS]
        n_extra = len(header_parts) - (2 if (has_year or has_age_col) else 1)
    parts.append(f"HEADER: {', '.join(header_parts)}")

    # Build rows
    for i, row in enumerate(rows):
        val = _format_val(stat_col_key, row[1], is_rate)
        row_parts = [val]
        col_offset = 2  # after name, stat_val
        if has_year or has_age_col:
            row_parts.append(str(row[col_offset]))
            col_offset += 1
        # Extra filter values
        for j, ef in enumerate(plan.extra_filters):
            if col_offset + j < len(row):
                ef_val = _format_val(ef["stat"].db_column, row[col_offset + j], ef["stat"].is_rate)
                row_parts.append(ef_val)
        parts.append(f"ROW {i+1}. {row[0]}: {', '.join(row_parts)}")
    parts.append("[/LEADERBOARD]")

    if is_rate:
        if plan.scope == "career":
            if plan.is_pitching:
                parts.append(f"\n_Min. 1,000 IP._")
            else:
                parts.append(f"\n_Min. 5,000 AB._")
        elif pa_label and "on pace" not in pa_label:
            parts.append(f"\n_{pa_label}_")

    # Suggestion pills
    parts.extend(_build_suggestions(plan, name, scope_label))

    return "\n".join(parts)


def _build_suggestions(plan: QueryPlan, stat_name: str, scope_label: str) -> list[str]:
    """Generate suggestion pills based on the query plan."""
    pills = []
    current_year = datetime.now().year
    stat_lower = stat_name.lower()

    # Suggest other time scopes — always explicit about the year
    last_year = current_year - 1
    if plan.scope.startswith("season_"):
        yr = plan.season or current_year
        if yr == current_year:
            pills.append(f"\n[SUGGEST]{last_year} {stat_lower} leaders[/SUGGEST]")
        else:
            pills.append(f"\n[SUGGEST]{current_year} {stat_lower} leaders[/SUGGEST]")
        pills.append(f"[SUGGEST]career {stat_lower} leaders[/SUGGEST]")
    elif plan.scope == "career":
        pills.append(f"\n[SUGGEST]{current_year} {stat_lower} leaders[/SUGGEST]")
        pills.append(f"[SUGGEST]all-time single season {stat_lower} leaders[/SUGGEST]")
    elif plan.scope == "all_time" or plan.scope.startswith("since_"):
        pills.append(f"\n[SUGGEST]{current_year} {stat_lower} leaders[/SUGGEST]")
        pills.append(f"[SUGGEST]career {stat_lower} leaders[/SUGGEST]")

    # For batting stats, suggest pitching equivalent and vice versa
    # Include the same scope so the pill matches the query context
    scope_suffix = ""
    if plan.scope == "career":
        scope_suffix = " all time"
    elif plan.scope == "all_time":
        scope_suffix = " in a season"
    elif plan.scope.startswith("season_") and plan.season:
        scope_suffix = f" in {plan.season}"
    elif plan.scope.startswith("since_") and plan.since_year:
        scope_suffix = f" since {plan.since_year}"

    # For stats that exist in both batting and pitching, suggest the other side
    ambiguous_stats = {"strikeouts", "walks", "hits", "home_runs"}
    if plan.stat and plan.stat.db_column in ambiguous_stats:
        stat_abbrev = plan.stat.display_abbrev
        if not plan.is_pitching:
            pills.append(f"[SUGGEST]most {stat_abbrev} by a pitcher{scope_suffix}[/SUGGEST]")
        else:
            pills.append(f"[SUGGEST]most {stat_abbrev} by a hitter{scope_suffix}[/SUGGEST]")

    return pills


def _execute_consecutive_seasons_alltime(conn, plan, table, n_seasons) -> Optional[str]:
    """All-time: find players who met a threshold in N consecutive seasons."""
    conditions = []
    if plan.stat:
        stat_col = plan.stat.db_column
        if plan.threshold is not None:
            conditions.append((stat_col, plan.comparison, plan.threshold, plan.stat.is_rate))
    for ef in plan.extra_filters:
        if ef.get("stat"):
            conditions.append((ef["stat"].db_column, ef.get("comparison", ">="), ef["threshold"], ef["stat"].is_rate))

    if not conditions:
        return None

    # Build self-join: s1 JOIN s2 ON same player + season+1, etc.
    first_col = conditions[0][0]
    first_thresh = conditions[0][2]
    _labels = {
        "home_runs": "HR", "hits": "H", "rbi": "RBI", "runs": "R",
        "stolen_bases": "SB", "walks": "BB", "strikeouts": "K", "wins": "W",
        "saves": "SV", "batting_avg": "AVG", "ops": "OPS", "era": "ERA",
    }
    abbrev = _labels.get(first_col, first_col)
    thresh_display = int(first_thresh) if first_thresh == int(first_thresh) else first_thresh

    # Build JOIN chain
    aliases = [f"s{i+1}" for i in range(n_seasons)]
    joins = [f"{table} {aliases[0]}"]
    for i in range(1, n_seasons):
        joins.append(
            f"JOIN {table} {aliases[i]} ON {aliases[0]}.player_id = {aliases[i]}.player_id "
            f"AND {aliases[i]}.season = {aliases[0]}.season + {i}"
        )

    # WHERE conditions for each season alias
    wheres = []
    for alias in aliases:
        for col, comp, thresh, is_rate in conditions:
            wheres.append(f"{alias}.{col} {comp} {thresh}")

    # Build SELECT with stat values per season
    selects = [f"p.name", f"{aliases[0]}.season AS start_year"]
    for alias in aliases:
        selects.append(f"{alias}.{first_col}")

    sql = (
        f"SELECT {', '.join(selects)} "
        f"FROM {' '.join(joins)} "
        f"JOIN players p ON {aliases[0]}.player_id = p.player_id "
        f"WHERE {' AND '.join(wheres)} "
        f"ORDER BY {aliases[0]}.season DESC "
        f"LIMIT 500"
    )

    rows = conn.execute(sql).fetchall()

    if not rows:
        return f"No players found with {thresh_display}+ {abbrev} in {n_seasons} consecutive seasons." + _empty_result_pills(plan)

    # Dedupe: show each player's most recent streak only
    seen_players = set()
    unique_rows = []
    for row in rows:
        name = row[0]
        if name not in seen_players:
            seen_players.add(name)
            unique_rows.append(row)

    title = f"**{thresh_display}+ {abbrev} in {n_seasons} Consecutive Seasons (All-Time)**"
    parts = [title]
    parts.append(f"{len(unique_rows)} matched.")
    parts.append("[TIP]Tap a player name for their full profile.[/TIP]")
    parts.append("[LEADERBOARD]")

    parts.append("HEADER: Years")

    for i, row in enumerate(unique_rows):
        name = row[0]
        start_yr = row[1]
        end_yr = start_yr + n_seasons - 1
        parts.append(f"ROW {i+1}. {name}: {start_yr}–{end_yr}")

    parts.append("[/LEADERBOARD]")
    return "\n".join(parts)


def _execute_per_season_threshold(conn, plan: QueryPlan) -> Optional[str]:
    """Multi-season consistency: find players meeting criteria in EVERY season."""
    table, prefix = _table_and_prefix(plan)
    current_year = date.today().year
    n_seasons = plan.season_count or 3

    # All-time consecutive season scan (self-join approach)
    if plan.scope in ("all_time", "career") or (not plan.season and not plan.since_year):
        return _execute_consecutive_seasons_alltime(conn, plan, table, n_seasons)

    # Use completed seasons — current year likely has too few games
    # If we're past July, include current year; otherwise use last N completed
    month = date.today().month
    most_recent = current_year if month >= 8 else current_year - 1
    start_year = most_recent - n_seasons + 1
    seasons = list(range(start_year, most_recent + 1))

    # Build the per-season condition
    conditions = []
    if plan.stat:
        stat_col = plan.stat.db_column
        if plan.threshold is not None:
            conditions.append((stat_col, plan.comparison, plan.threshold, plan.stat.is_rate))
    for ef in plan.extra_filters:
        if ef.get("stat"):
            conditions.append((ef["stat"].db_column, ef.get("comparison", ">="), ef["threshold"], ef["stat"].is_rate))

    if not conditions:
        return None

    # Find players who qualify in EACH season
    qualifying = None  # set of player_ids
    season_data = {}  # {player_id: {season: {col: val}}}

    filters_str, params = _build_filters(plan, prefix, conn)

    for szn in seasons:
        where_parts = [f"{prefix}.season = ?"]
        query_params = [szn]
        for col, comp, thresh, is_r in conditions:
            where_parts.append(f"{prefix}.{col} {comp} ?")
            query_params.append(thresh)
        if filters_str:
            where_parts.append(filters_str)
            query_params.extend(params)

        where = "WHERE " + " AND ".join(where_parts)

        # Collect all relevant stat columns
        stat_cols = list(set(c[0] for c in conditions))
        select_cols = ", ".join(f"{prefix}.{c}" for c in stat_cols)

        cur = conn.cursor()
        cur.execute(
            f"SELECT {prefix}.player_id, p.name, {prefix}.season, {prefix}.games, {select_cols} "
            f"FROM {table} {prefix} "
            f"JOIN players p ON {prefix}.player_id = p.player_id "
            f"{where}",
            tuple(query_params),
        )
        rows = cur.fetchall()

        season_pids = set()
        for row in rows:
            pid, pname, yr, games = row[0], row[1], row[2], row[3]
            season_pids.add(pid)
            if pid not in season_data:
                season_data[pid] = {"name": pname}
            season_data[pid][szn] = {"games": games}
            for i, col in enumerate(stat_cols):
                season_data[pid][szn][col] = row[4 + i]

        if qualifying is None:
            qualifying = season_pids
        else:
            qualifying &= season_pids

    if not qualifying:
        stat_label = conditions[0][0].replace("_", " ") if conditions else "criteria"
        return f"No players met that criteria in each of the last {n_seasons} seasons ({start_year}-{most_recent})." + _empty_result_pills(plan)

    # Format results
    results = []
    for pid in qualifying:
        info = season_data[pid]
        results.append(info)

    # Sort by first condition's value in the most recent season
    first_col = conditions[0][0]
    results.sort(
        key=lambda r: r.get(seasons[-1], {}).get(first_col, 0) or 0,
        reverse=(conditions[0][1] == ">="),
    )

    # Build output
    stat_cols = list(set(c[0] for c in conditions))
    _labels = {
        "games": "G", "home_runs": "HR", "hits": "H", "rbi": "RBI", "runs": "R",
        "stolen_bases": "SB", "walks": "BB", "strikeouts": "K", "wins": "W",
        "saves": "SV", "batting_avg": "AVG", "obp": "OBP", "slg": "SLG", "ops": "OPS",
        "era": "ERA", "whip": "WHIP", "k_per_9": "K/9", "innings_pitched": "IP",
    }

    title = f"**Players meeting criteria in each of the last {n_seasons} seasons ({start_year}-{most_recent})**"
    parts = [title]
    parts.append(f"{len(results)} matched.")
    parts.append("[TIP]Tap a player name for their full profile.[/TIP]")

    # Build header: STAT 'YR for each season (no Games column — keeps it compact)
    # If total columns would exceed max, trim oldest seasons first
    total_cols = len(seasons) * len(stat_cols)
    display_seasons = seasons
    if total_cols > _MAX_DISPLAY_COLS and len(stat_cols) > 0:
        max_seasons = _MAX_DISPLAY_COLS // len(stat_cols)
        if max_seasons < len(seasons):
            display_seasons = seasons[-max_seasons:]  # keep most recent

    headers = []
    single_stat = len(stat_cols) == 1
    for szn in display_seasons:
        yr_label = f"'{str(szn)[-2:]}"
        for col in stat_cols:
            # If only one stat, just show the year — stat is obvious from the query
            if single_stat:
                headers.append(yr_label)
            else:
                headers.append(f"{_labels.get(col, col)} {yr_label}")
    parts.append("[LEADERBOARD]")
    parts.append("HEADER: " + ", ".join(headers))

    for i, info in enumerate(results[:plan.limit]):
        name = info["name"]
        vals = []
        for szn in display_seasons:
            szn_data = info.get(szn, {})
            for col in stat_cols:
                v = szn_data.get(col, 0)
                if isinstance(v, float) and v < 1:
                    vals.append(_format_rate(v))
                elif isinstance(v, float):
                    vals.append(f"{v:.2f}")
                else:
                    vals.append(str(v or 0))
        parts.append(f"ROW {i+1}. {name}: " + ", ".join(vals))

    parts.append("[/LEADERBOARD]")

    # See-also for all-time version
    _labels_sa = {"home_runs": "HR", "rbi": "RBI", "hits": "H", "stolen_bases": "SB",
                  "strikeouts": "K", "wins": "W"}
    first_col_sa = conditions[0][0] if conditions else ""
    first_label_sa = _labels_sa.get(first_col_sa, first_col_sa)
    thresh_sa = int(conditions[0][2]) if conditions and conditions[0][2] == int(conditions[0][2]) else ""
    if thresh_sa:
        parts.insert(2, f"[DIDYOUMEAN]{thresh_sa}+ {first_label_sa} in {n_seasons} straight seasons all time[/DIDYOUMEAN]")

    return "\n".join(parts)


def _execute_player_threshold(conn, plan: QueryPlan) -> Optional[str]:
    """Player-filtered threshold: show qualifying seasons for a specific player.
    E.g., 'Ohtani 30+ HR seasons' → list of Ohtani's seasons with 30+ HR."""
    table, prefix = _table_and_prefix(plan)
    stat_expr, abbrev, stat_name, is_rate = _stat_expr(plan, prefix)

    threshold_display = _format_val("", plan.threshold, is_rate) if is_rate else str(int(plan.threshold))

    cur = conn.cursor()
    cur.execute(
        f"SELECT {prefix}.season, {stat_expr} AS stat_val, {prefix}.games, "
        f"{prefix}.hits, {prefix}.at_bats, {prefix}.home_runs, {prefix}.rbi, {prefix}.runs "
        f"FROM {table} {prefix} "
        f"JOIN players p ON {prefix}.player_id = p.player_id "
        f"WHERE p.name = ? AND {stat_expr} {plan.comparison} ? "
        f"ORDER BY {prefix}.season ASC",
        (plan.player_name, plan.threshold),
    )
    rows = cur.fetchall()

    # Render filter clause respecting comparison direction. The SQL query
    # already filters by plan.comparison correctly (line above); the title
    # was previously hardcoding "+" which silently lied for "under N" /
    # "fewer than N" / lower-is-better-stat queries.
    _filter_clause = _format_threshold_clause(plan.threshold, plan.comparison, abbrev, is_rate)

    if not rows:
        return f"{plan.player_name} has no seasons with {_filter_clause}." + _empty_result_pills(plan)

    title = f"**{plan.player_name} — {len(rows)} season{'s' if len(rows) != 1 else ''} with {_filter_clause}**\n"
    parts = [title]
    parts.append("[LEADERBOARD]")
    parts.append(f"HEADER: {abbrev}, G, H-AB, RBI, R")

    for season, stat_val, games, h, ab, hr, rbi, r in rows:
        val = _format_val(plan.stat.db_column if plan.stat else "", stat_val, is_rate)
        parts.append(f"ROW {season}: {val}, {games or 0}, {h or 0}-{ab or 0}, {rbi or 0}, {r or 0}")

    parts.append("[/LEADERBOARD]")
    return "\n".join(parts)


def _execute_career_threshold(conn, plan: QueryPlan) -> Optional[str]:
    """Career aggregation threshold: SUM counting stats across all seasons per player."""
    table, prefix = _table_and_prefix(plan)
    stat_col = plan.stat.db_column
    abbrev = plan.stat.display_abbrev
    threshold_display = str(int(plan.threshold))
    # is_rate must be defined here for _format_threshold_clause below.
    # _execute_career_threshold is only ever called for COUNTING stats
    # (the gate at the call site is `not is_rate and ... _exceeds_season_record`),
    # but the formatter takes is_rate explicitly so we set it for clarity
    # and so the function is robust if the call site ever changes.
    is_rate = plan.stat.is_rate if plan.stat else False

    # Build extra filter SUMs and HAVINGs
    extra_sums = ""
    extra_having = ""
    extra_headers = []
    for ef in plan.extra_filters:
        ef_col = ef["stat"].db_column
        extra_sums += f", SUM({prefix}.{ef_col}) AS career_{ef_col}"
        ef_thresh = int(ef["threshold"])
        op = ef["comparison"]
        extra_having += f" AND SUM({prefix}.{ef_col}) {op} {ef_thresh}"
        extra_headers.append(ef["stat"].display_abbrev)

    # League/bats/throws filters need to go in WHERE (not HAVING)
    where_parts = []
    where_params = []
    if plan.league:
        from .response_builder import _league_team_clause
        where_parts.append(_league_team_clause(plan.league, prefix))
    if plan.bats:
        where_parts.append("p.bats = ?")
        where_params.append(plan.bats)
    if plan.throws:
        where_parts.append("p.throws = ?")
        where_params.append(plan.throws)
    if plan.active_only:
        current_year = datetime.now().year
        where_parts.append(f"{prefix}.season >= {current_year - 1}")
    if plan.since_year:
        where_parts.append(f"{prefix}.season >= ?")
        where_params.append(plan.since_year)

    where_clause = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""

    sql = (
        f"SELECT p.name, SUM({prefix}.{stat_col}) AS career_total, "
        f"MIN({prefix}.season) AS first_yr, MAX({prefix}.season) AS last_yr, "
        f"COUNT({prefix}.season) AS seasons{extra_sums} "
        f"FROM {table} {prefix} "
        f"JOIN players p ON {prefix}.player_id = p.player_id "
        f"{where_clause} "
        f"GROUP BY {prefix}.player_id "
        f"HAVING SUM({prefix}.{stat_col}) {plan.comparison} ?{extra_having} "
        f"ORDER BY career_total DESC LIMIT 500"
    )

    cur = conn.cursor()
    cur.execute(sql, tuple(where_params + [plan.threshold]))
    rows = cur.fetchall()

    _primary_clause = _format_threshold_clause(
        plan.threshold, plan.comparison, abbrev, is_rate)

    if not rows:
        return f"No players with career {_primary_clause} found." + _empty_result_pills(plan)

    # Title — primary uses comparison-aware helper; extras already do
    filter_desc = _primary_clause
    for ef in plan.extra_filters:
        ef_stat = ef.get("stat")
        ef_abbrev = ef_stat.display_abbrev if ef_stat else ""
        ef_is_rate = ef_stat.is_rate if ef_stat else False
        filter_desc += " and " + _format_threshold_clause(
            ef.get("threshold"), ef.get("comparison", ">="),
            ef_abbrev, ef_is_rate)

    scope_label = f"Since {plan.since_year}" if plan.since_year else "All-Time"
    parts = [f"**Career {filter_desc} ({scope_label})**"]
    parts.append(f"{len(rows)} matched.\n")
    parts.append("[TIP]Tap a player name for their full profile.[/TIP]")
    parts.append("[LEADERBOARD]")

    headers = [abbrev] + extra_headers
    parts.append(f"HEADER: {', '.join(headers)}")

    for i, row in enumerate(rows):
        name = row[0]
        total = int(row[1])
        extra_vals = ""
        for j in range(len(plan.extra_filters)):
            ev = int(row[5 + j])
            extra_vals += f", {ev}"
        parts.append(f"ROW {i+1}. {name}: {total}{extra_vals}")

    parts.append("[/LEADERBOARD]")
    return "\n".join(parts)


def _execute_year_comparison(conn, plan: QueryPlan) -> Optional[str]:
    """Year-over-year comparison: 'Mookie Betts 2023 vs 2024'."""
    if not plan.player_name or not plan.compare_years:
        return None

    year1, year2 = plan.compare_years
    name = plan.player_name

    # Get player_id
    row = conn.execute("SELECT player_id FROM players WHERE name = ? LIMIT 1",
                       (name,)).fetchone()
    if not row:
        return None
    pid = row[0]

    # Check if pitcher using the standard detection
    from services.name_matcher import is_pitcher as _is_pitcher
    is_pitcher = _is_pitcher(name)

    bat_cols = "games, at_bats, runs, hits, doubles, triples, home_runs, rbi, stolen_bases, caught_stealing, walks, strikeouts, batting_avg, obp, slg, ops, ops_plus"
    bat_headers = "G, AB, R, H, 2B, 3B, HR, RBI, SB, CS, BB, SO, AVG, OBP, SLG, OPS, OPS+"
    pitch_cols = "games, wins, losses, era, games_started, saves, innings_pitched, hits, earned_runs, strikeouts, walks, whip"
    pitch_headers = "G, W, L, ERA, GS, SV, IP, H, ER, K, BB, WHIP"
    rate_cols = {"era", "batting_avg", "obp", "slg", "ops", "whip"}

    def _format_row(row_data, col_list):
        vals = []
        for i, col_name in enumerate(col_list):
            v = row_data[i]
            if v is None:
                vals.append("--")
            elif col_name.strip() in rate_cols:
                vals.append(_format_val(col_name.strip(), v, True))
            elif col_name.strip() == "innings_pitched":
                vals.append(str(v) if v else "--")
            else:
                vals.append(str(int(v)) if isinstance(v, float) and v == int(v) else str(v))
        return ", ".join(vals)

    # Determine which tables to show
    if is_pitcher:
        primary_table = "season_pitching_stats"
        primary_cols, primary_headers = pitch_cols, pitch_headers
    else:
        primary_table = "season_batting_stats"
        primary_cols, primary_headers = bat_cols, bat_headers

    primary_col_list = [c.strip() for c in primary_cols.split(",")]
    primary_header_list = [h.strip() for h in primary_headers.split(",")]

    rows = {}
    for yr in (year1, year2):
        r = conn.execute(
            f"SELECT {primary_cols} FROM {primary_table} WHERE player_id = ? AND season = ?",
            (pid, yr)).fetchone()
        if r:
            rows[yr] = r

    if not rows:
        return f"No stats found for {name} in {year1} or {year2}."

    parts = [f"**{name} — {year1} vs {year2}**\n"]
    parts.append("[TIP]Tap a player name for their full profile.[/TIP]")

    # Primary stats (batting or pitching)
    for yr in (year1, year2):
        parts.append("[STATGRID]")
        parts.append(f"HEADER: {primary_headers}")
        if yr in rows:
            parts.append(f"ROW {yr}: {_format_row(rows[yr], primary_col_list)}")
        else:
            parts.append(f"ROW {yr}: {'--' + (', --' * (len(primary_header_list) - 1))}")
        parts.append("[/STATGRID]")

    # Check for secondary stats (two-way player: show pitching if primary is batting, vice versa)
    if not is_pitcher:
        secondary_table = "season_pitching_stats"
        secondary_cols, secondary_headers = pitch_cols, pitch_headers
    else:
        secondary_table = "season_batting_stats"
        secondary_cols, secondary_headers = bat_cols, bat_headers

    secondary_col_list = [c.strip() for c in secondary_cols.split(",")]
    secondary_header_list = [h.strip() for h in secondary_headers.split(",")]

    secondary_rows = {}
    for yr in (year1, year2):
        r = conn.execute(
            f"SELECT {secondary_cols} FROM {secondary_table} WHERE player_id = ? AND season = ?",
            (pid, yr)).fetchone()
        if r:
            secondary_rows[yr] = r

    if secondary_rows:
        label = "Pitching" if not is_pitcher else "Batting"
        parts.append(f"\n**{label}**")
        for yr in (year1, year2):
            if yr in secondary_rows:
                parts.append("[STATGRID]")
                parts.append(f"HEADER: {secondary_headers}")
                parts.append(f"ROW {yr}: {_format_row(secondary_rows[yr], secondary_col_list)}")
                parts.append("[/STATGRID]")

    # Suggestions
    parts.append(f"\n[SUGGEST]{name} career stats[/SUGGEST]")
    parts.append(f"[SUGGEST]{name} {year2}[/SUGGEST]")

    return "\n".join(parts)


def _execute_threshold(conn, plan: QueryPlan) -> Optional[str]:
    """Threshold query: 'who hit 40 HR', 'players with .800 OPS'."""
    if plan.per_season:
        return _execute_per_season_threshold(conn, plan)

    # Player-filtered threshold: show this player's qualifying seasons
    if plan.player_name:
        return _execute_player_threshold(conn, plan)

    table, prefix = _table_and_prefix(plan)
    stat_expr, abbrev, name, is_rate = _stat_expr(plan, prefix)
    filters_str, params = _build_filters(plan, prefix, conn)

    # Career aggregation: when no season + counting stat + threshold exceeds season record
    # SUM across all seasons per player instead of checking each season individually
    season = plan.season
    if (not season and not is_rate and plan.threshold
            and plan.stat and _exceeds_season_record(plan.stat, plan.threshold)):
        return _execute_career_threshold(conn, plan)

    if season:
        pa, pa_label = _pa_filter(plan, prefix, conn, season)
        where = f"WHERE {prefix}.season = ? AND {stat_expr} {plan.comparison} ?"
        query_params = [season, plan.threshold] + params
        if filters_str:
            where += f" AND {filters_str}"
        where += pa
        scope_label = str(season)
    else:
        pa, pa_label = _pa_filter(plan, prefix, conn)
        # Keep clauses + params in lockstep. Previously seeded query_params
        # with `[threshold] + params` upfront but added filters_str (the
        # source of those params) after since_year, mis-aligning bindings.
        where_parts = [f"{stat_expr} {plan.comparison} ?"]
        query_params: list = [plan.threshold]
        if plan.since_year:
            where_parts.append(f"{prefix}.season >= ?")
            query_params.append(plan.since_year)
        if filters_str:
            where_parts.append(filters_str)
            query_params += params
        if pa:
            where_parts.append(pa[5:])
        where = f"WHERE {' AND '.join(where_parts)}"
        scope_label = f"Since {plan.since_year}" if plan.since_year else "All-Time"

    # Build extra column selects for extra filters
    extra_selects = ", ".join(f"{prefix}.{ef['stat'].db_column}" for ef in plan.extra_filters)
    extra_select_clause = f", {extra_selects}" if extra_selects else ""

    cur = conn.cursor()
    if season:
        cur.execute(
            f"SELECT p.name, {stat_expr} AS stat_val{extra_select_clause} "
            f"FROM {table} {prefix} "
            f"JOIN players p ON {prefix}.player_id = p.player_id "
            f"{where} ORDER BY stat_val DESC LIMIT 500",
            tuple(query_params),
        )
    else:
        cur.execute(
            f"SELECT p.name, {stat_expr} AS stat_val, {prefix}.season{extra_select_clause} "
            f"FROM {table} {prefix} "
            f"JOIN players p ON {prefix}.player_id = p.player_id "
            f"{where} ORDER BY stat_val DESC LIMIT 500",
            tuple(query_params),
        )
    rows = cur.fetchall()

    threshold_display = _format_val("", plan.threshold, is_rate) if is_rate else str(int(plan.threshold))
    # Build descriptive label with filters
    bats_label = {"L": "Left-Handed ", "R": "Right-Handed ", "B": "Switch-Hitting "}.get(plan.bats or "", "")
    throws_label = {"L": "Left-Handed ", "R": "Right-Handed "}.get(plan.throws or "", "")
    position_label = "/".join(plan.position) + " " if plan.position else ""
    role_label = f"{plan.pitcher_role.title()} " if plan.pitcher_role else ""
    base_label = "Rookies" if plan.rookie else "Pitchers" if plan.is_pitching else "Players"
    rookie_label = f"{bats_label}{throws_label}{position_label}{role_label}{base_label}"
    op = "with" if plan.comparison == ">=" else "with no more than"

    if not rows:
        # On-pace fallback: for current-season counting stats with no results,
        # check who's on pace to reach the threshold over 162 games
        if (season and season == date.today().year and not is_rate
                and plan.comparison == ">=" and plan.threshold):
            try:
                tbl_for_gp = "season_pitching_stats" if plan.is_pitching else "season_batting_stats"
                gp_row = conn.execute(
                    f"SELECT MAX(games) FROM {tbl_for_gp} WHERE season = ?",
                    (season,)
                ).fetchone()
                max_games = int(gp_row[0]) if gp_row and gp_row[0] else 0
                if 5 <= max_games < 140:
                    pace_factor = 162 / max_games
                    # Prorated threshold: what you'd need now to be on pace
                    prorated = plan.threshold / pace_factor
                    pace_where = f"WHERE {prefix}.season = ? AND {stat_expr} {plan.comparison} ?"
                    pace_params = [season, prorated]
                    # Prorate extra filter thresholds
                    for ef in plan.extra_filters:
                        if not ef["stat"].is_rate:
                            ef_prorated = ef["threshold"] / pace_factor
                        else:
                            ef_prorated = ef["threshold"]
                        pace_where += f" AND {prefix}.{ef['stat'].db_column} {ef['comparison']} ?"
                        pace_params.append(ef_prorated)
                    # Non-stat filters (league, bats, etc.) — rebuild without extra_filters
                    saved_extras = plan.extra_filters
                    plan.extra_filters = []
                    non_stat_filters, non_stat_params = _build_filters(plan, prefix, conn)
                    plan.extra_filters = saved_extras
                    if non_stat_filters:
                        pace_where += f" AND {non_stat_filters}"
                        pace_params.extend(non_stat_params)
                    # Require minimum games played (at least half of team max)
                    pace_where += f" AND {prefix}.games >= ?"
                    pace_params.append(max(1, max_games // 2))
                    # Build SELECT with extra stat columns
                    extra_cols = ", ".join(f"{prefix}.{ef['stat'].db_column}" for ef in plan.extra_filters)
                    extra_select = f", {extra_cols}" if extra_cols else ""
                    pace_cur = conn.cursor()
                    pace_cur.execute(
                        f"SELECT p.name, {stat_expr} AS stat_val{extra_select}, {prefix}.games "
                        f"FROM {table} {prefix} "
                        f"JOIN players p ON {prefix}.player_id = p.player_id "
                        f"{pace_where} ORDER BY stat_val DESC LIMIT 10",
                        tuple(pace_params),
                    )
                    pace_rows = pace_cur.fetchall()
                    if pace_rows:
                        # Build pace display — primary now respects plan.comparison
                        filter_desc = _format_threshold_clause(
                            plan.threshold, plan.comparison, abbrev, is_rate)
                        for ef in plan.extra_filters:
                            filter_desc += " and " + _format_threshold_clause(
                                ef.get("threshold"), ef.get("comparison", ">="),
                                ef["stat"].display_abbrev, ef["stat"].is_rate)
                        parts = [f"No one has reached {filter_desc} yet in {season}."]
                        parts.append(f"Through {max_games} games, {len(pace_rows)} {'player is' if len(pace_rows) == 1 else 'players are'} on pace:\n")
                        parts.append("[LEADERBOARD]")
                        # Header: counting stats get Pace column, rate stats don't
                        header_parts = [abbrev, "Pace"]
                        for ef in plan.extra_filters:
                            header_parts.append(ef["stat"].display_abbrev)
                            if not ef["stat"].is_rate:
                                header_parts.append("Pace")
                        parts.append(f"HEADER: {', '.join(header_parts)}")
                        n_extras = len(plan.extra_filters)
                        for i, row in enumerate(pace_rows):
                            # row: name, primary_val, [extra1, extra2, ...], games
                            primary_val = int(row[1])
                            primary_pace = int(row[1] * pace_factor)
                            vals = [str(primary_val), str(primary_pace)]
                            for j in range(n_extras):
                                ef = plan.extra_filters[j]
                                raw = row[2 + j]
                                if ef["stat"].is_rate:
                                    vals.append(_format_val(ef["stat"].db_column, raw, True))
                                else:
                                    vals.extend([str(int(raw)), str(int(raw * pace_factor))])
                            parts.append(f"ROW {i+1}. {row[0]}: {', '.join(vals)}")
                        parts.append("[/LEADERBOARD]")

                        # Add time-scope pills since nobody has reached the threshold yet
                        last_year = season - 1
                        parts.append(f"\n[SUGGEST]{filter_desc} in {last_year}[/SUGGEST]")
                        parts.append(f"[SUGGEST]{filter_desc} all time[/SUGGEST]")

                        return "\n".join(parts)
            except Exception as e:
                logger.warning("pace_fallback_error error=%s", e)
        return _zero_result_sentence(plan) + _empty_result_pills(plan)

    # Build title with extra filters — primary now respects plan.comparison
    filter_parts = [_format_threshold_clause(
        plan.threshold, plan.comparison, abbrev, is_rate)]
    for ef in plan.extra_filters:
        ef_stat = ef["stat"]
        filter_parts.append(_format_threshold_clause(
            ef.get("threshold"), ef.get("comparison", ">="),
            ef_stat.display_abbrev, ef_stat.is_rate))
    title = f"**{rookie_label} with {' and '.join(filter_parts)} ({scope_label})**"

    count = len(rows)
    parts = [title, f"{count} matched.\n"]
    # Show PA/IP minimum note for current-season rate stats
    if is_rate and season and season == date.today().year and pa_label:
        parts.append(f"[SUBTITLE]{pa_label}[/SUBTITLE]")
    parts.append("[TIP]Tap a player name for their full profile.[/TIP]")
    parts.append("[LEADERBOARD]")

    # Build header with extra filter columns
    extra_headers = [ef["stat"].display_abbrev for ef in plan.extra_filters]
    has_year = not season

    if has_year and len(rows[0]) > 2:
        all_headers = ["Year", abbrev] + extra_headers
    else:
        all_headers = [abbrev] + extra_headers
    if len(all_headers) > _MAX_DISPLAY_COLS:
        all_headers = all_headers[:_MAX_DISPLAY_COLS]
        n_extra = len(all_headers) - (2 if has_year else 1)
    parts.append(f"HEADER: {', '.join(all_headers)}")

    stat_col_key = plan.stat.db_column if plan.stat else (plan.derived_stat or "")
    n_extra = len(plan.extra_filters)
    for i, row in enumerate(rows):
        val = _format_val(stat_col_key, row[1], is_rate)
        if has_year:
            # row: name, stat_val, season, [extra1, extra2, ...]
            offset = 3
            extra_vals = [_format_val(plan.extra_filters[j]["stat"].db_column, row[offset + j], plan.extra_filters[j]["stat"].is_rate)
                          for j in range(n_extra) if offset + j < len(row)]
            extra_str = ", ".join(extra_vals)
            row_text = f"ROW {i+1}. {row[0]}: {row[2]}, {val}"
            if extra_str:
                row_text += f", {extra_str}"
            parts.append(row_text)
        else:
            # row: name, stat_val, [extra1, extra2, ...]
            offset = 2
            extra_vals = [_format_val(plan.extra_filters[j]["stat"].db_column, row[offset + j], plan.extra_filters[j]["stat"].is_rate)
                          for j in range(n_extra) if offset + j < len(row)]
            extra_str = ", ".join(extra_vals)
            row_text = f"ROW {i+1}. {row[0]}: {val}"
            if extra_str:
                row_text += f", {extra_str}"
            parts.append(row_text)
    parts.append("[/LEADERBOARD]")
    return "\n".join(parts)


def _execute_count(conn, plan: QueryPlan) -> Optional[str]:
    """Count query: 'how many players hit 30 HR in 2025'."""
    table, prefix = _table_and_prefix(plan)
    stat_expr, abbrev, name, is_rate = _stat_expr(plan, prefix)
    filters_str, params = _build_filters(plan, prefix, conn)

    where_parts = [f"{stat_expr} >= ?"]
    query_params = [plan.threshold] + params
    if plan.season:
        where_parts.append(f"{prefix}.season = ?")
        query_params.append(plan.season)
    elif plan.since_year:
        where_parts.append(f"{prefix}.season >= ?")
        query_params.append(plan.since_year)
        if plan.end_year:
            where_parts.append(f"{prefix}.season <= ?")
            query_params.append(plan.end_year)
    if filters_str:
        where_parts.append(filters_str)
    where = f"WHERE {' AND '.join(where_parts)}"

    cur = conn.cursor()
    cur.execute(
        f"SELECT p.name, {stat_expr} AS stat_val "
        f"FROM {table} {prefix} "
        f"JOIN players p ON {prefix}.player_id = p.player_id "
        f"{where} ORDER BY stat_val DESC",
        tuple(query_params),
    )
    rows = cur.fetchall()
    count = len(rows)

    threshold_display = _format_val("", plan.threshold, is_rate) if is_rate else str(int(plan.threshold))
    if plan.season:
        scope = str(plan.season)
    elif plan.since_year and plan.end_year:
        scope = f"in the {plan.since_year}s"
    elif plan.since_year:
        scope = f"since {plan.since_year}"
    else:
        scope = "all time"
    scope_prefix = "in " if scope != "all time" else ""
    summary = f"**{count}** players have had {threshold_display}+ {name} {scope_prefix}{scope}.\n"

    parts = [summary]
    parts.append("[TIP]Tap a player name for their full profile.[/TIP]")
    parts.append("[LEADERBOARD]")
    parts.append(f"HEADER: {abbrev}")
    for i, row in enumerate(rows):
        val = _format_val(plan.stat.db_column if plan.stat else "", row[1], is_rate)
        parts.append(f"ROW {i+1}. {row[0]}: {val}")
    parts.append("[/LEADERBOARD]")
    return "\n".join(parts)


def _execute_superlative(conn, plan: QueryPlan) -> Optional[str]:
    """Superlative query: 'youngest player to hit 50 HR'."""
    table, prefix = _table_and_prefix(plan)
    stat_expr, abbrev, name, is_rate = _stat_expr(plan, prefix)
    filters_str, params = _build_filters(plan, prefix, conn)

    if plan.superlative in ("youngest", "oldest"):
        age_select = f", {prefix}.season - CAST(SUBSTR(p.birthdate, 1, 4) AS INT) AS age_at_season"
        order_by = "age_at_season ASC" if plan.superlative == "youngest" else "age_at_season DESC"
        birthdate_filter = " AND p.birthdate IS NOT NULL"
    else:
        age_select = ""
        order_by = f"{prefix}.season ASC" if plan.superlative == "first" else f"{prefix}.season DESC"
        birthdate_filter = ""

    where_parts = [f"{stat_expr} >= ?"]
    query_params = [plan.threshold] + params
    if plan.since_year:
        where_parts.append(f"{prefix}.season >= ?")
        query_params.append(plan.since_year)
    if filters_str:
        where_parts.append(filters_str)
    # Rate stats require full-season qualification (400 PA / 162 IP)
    if is_rate:
        if plan.is_pitching:
            where_parts.append(f"{prefix}.ip_outs >= {162 * 3}")
        else:
            where_parts.append(f"{prefix}.plate_appearances >= 400")
    where = f"WHERE {' AND '.join(where_parts)}{birthdate_filter}"

    cur = conn.cursor()
    cur.execute(
        f"SELECT p.name, {stat_expr} AS stat_val, {prefix}.season{age_select} "
        f"FROM {table} {prefix} "
        f"JOIN players p ON {prefix}.player_id = p.player_id "
        f"{where} ORDER BY {order_by} LIMIT 50",
        tuple(query_params),
    )
    rows = cur.fetchall()
    if not rows:
        return f"No players found matching that criteria." + _empty_result_pills(plan)

    sup_labels = {"youngest": "Youngest", "oldest": "Oldest", "first": "First", "last": "Most Recent"}
    sup_label = sup_labels.get(plan.superlative, "")
    since_label = f" Since {plan.since_year}" if plan.since_year else ""
    title = f"**{sup_label} Players with {int(plan.threshold)}+ {name}{since_label}**\n"

    has_age = plan.superlative in ("youngest", "oldest")
    parts = [title]
    parts.append("[TIP]Tap a player name for their full profile.[/TIP]")
    parts.append("[LEADERBOARD]")
    if has_age:
        parts.append(f"HEADER: Age, Year, {abbrev}")
        for i, row in enumerate(rows):
            val = _format_val(plan.stat.db_column if plan.stat else "", row[1], is_rate)
            parts.append(f"ROW {i+1}. {row[0]}: {row[3]}, {row[2]}, {val}")
    else:
        parts.append(f"HEADER: Year, {abbrev}")
        for i, row in enumerate(rows):
            val = _format_val(plan.stat.db_column if plan.stat else "", row[1], is_rate)
            parts.append(f"ROW {i+1}. {row[0]}: {row[2]}, {val}")
    parts.append("[/LEADERBOARD]")
    return "\n".join(parts)


def _execute_split_leaderboard(conn, plan: QueryPlan) -> Optional[str]:
    """Leaderboard from a split table."""
    from .response_builder import build_split_leaderboard  # type: ignore
    season = plan.season or datetime.now().year
    return build_split_leaderboard(
        plan.stat, plan.split_context, season, plan.limit, plan.league,
        sort_asc=plan.sort_asc,
    )


def _execute_team_context_leaderboard(conn, plan: QueryPlan) -> Optional[str]:
    """Aggregate game-log stats over games filtered by team_game_results context.

    Powers queries like:
      "best batting avg when team had won at least 40 games at the time"
      "highest OPS in extra-innings games"
      "most home runs in day games"

    Joins game_batting_logs / game_pitching_logs to team_game_results on
    (date, opponent, is_home), applies the team-context WHERE clause, then
    aggregates per-player and ranks.
    """
    if not plan.team_context or not plan.stat:
        return None

    is_pitching = plan.is_pitching
    table = "game_pitching_logs" if is_pitching else "game_batting_logs"
    tc = plan.team_context
    # Scope handling — mirrors the conventions used elsewhere:
    #   explicit plan.season → that year only
    #   "all_time" / "career" → no season filter
    #   "since_YYYY" → plan.since_year through current year (range)
    #   default → current year
    is_all_time = plan.scope in ("all_time", "career")
    is_since = plan.scope and plan.scope.startswith("since_") and plan.since_year
    if plan.season:
        season = plan.season
        season_range = None
    elif is_all_time:
        season = None
        season_range = None
    elif is_since:
        season = None
        season_range = (plan.since_year, date.today().year)
    else:
        season = date.today().year
        season_range = None

    # Stat aggregation expressions. Rate stats compute from raw components;
    # counting stats are simple SUMs.
    stat_col = plan.stat.db_column
    if not is_pitching:
        rate_exprs = {
            "batting_avg": ("CAST(SUM(g.hits) AS REAL) / NULLIF(SUM(g.at_bats), 0)",
                            "SUM(g.at_bats)", "AB"),
            "obp": ("CAST(SUM(g.hits + g.walks + COALESCE(g.hit_by_pitch, 0)) AS REAL) / "
                    "NULLIF(SUM(g.at_bats + g.walks + COALESCE(g.hit_by_pitch, 0) "
                    "+ COALESCE(g.sacrifice_flies, 0)), 0)",
                    "SUM(g.at_bats + g.walks + COALESCE(g.hit_by_pitch, 0))", "PA"),
            "slg": ("CAST(SUM(g.hits - g.doubles - g.triples - g.home_runs "
                    "+ 2*g.doubles + 3*g.triples + 4*g.home_runs) AS REAL) / "
                    "NULLIF(SUM(g.at_bats), 0)",
                    "SUM(g.at_bats)", "AB"),
            "ops": ("(CAST(SUM(g.hits + g.walks + COALESCE(g.hit_by_pitch, 0)) AS REAL) / "
                    "NULLIF(SUM(g.at_bats + g.walks + COALESCE(g.hit_by_pitch, 0) "
                    "+ COALESCE(g.sacrifice_flies, 0)), 0)) + "
                    "(CAST(SUM(g.hits - g.doubles - g.triples - g.home_runs "
                    "+ 2*g.doubles + 3*g.triples + 4*g.home_runs) AS REAL) / "
                    "NULLIF(SUM(g.at_bats), 0))",
                    "SUM(g.at_bats)", "AB"),
        }
        if stat_col in rate_exprs:
            stat_expr, qual_expr, qual_label = rate_exprs[stat_col]
            # Use the canonical qualification rule scaled by subset fraction.
            # Single-season: 3.1 × team games × (subset fraction).
            # All-time: 5000 AB × subset fraction. Floor 30 always.
            from .qualification import min_pa_subset
            sort_asc = False
            is_rate = True
        else:
            # Counting stats: SUM the column directly
            if stat_col not in {"hits", "home_runs", "rbi", "runs", "stolen_bases",
                                "walks", "strikeouts", "doubles", "triples"}:
                return None  # unsupported stat
            stat_expr = f"SUM(g.{stat_col})"
            qual_expr = "COUNT(*)"
            qual_label = "G"
            min_qual = 1
            sort_asc = False
            is_rate = False
    else:
        # Pitching stats
        rate_exprs_p = {
            "era": ("CAST(SUM(g.earned_runs) AS REAL) * 27 / NULLIF(SUM(g.ip_outs), 0)",
                    "SUM(g.ip_outs)", "Outs"),
            "whip": ("CAST(SUM(g.hits + g.walks) AS REAL) * 3 / NULLIF(SUM(g.ip_outs), 0)",
                     "SUM(g.ip_outs)", "Outs"),
        }
        if stat_col in rate_exprs_p:
            stat_expr, qual_expr, qual_label = rate_exprs_p[stat_col]
            # Same canonical-rule scaling as batting: 1 IP × team games × subset
            # fraction for season; 1000 IP × subset fraction for all-time.
            from .qualification import min_ip_outs_subset
            sort_asc = True  # lower is better
            is_rate = True
        else:
            if stat_col not in {"strikeouts", "wins", "losses", "saves",
                                "walks", "hits", "earned_runs", "home_runs"}:
                return None
            stat_expr = f"SUM(g.{stat_col})"
            qual_expr = "COUNT(*)"
            qual_label = "G"
            min_qual = 1
            sort_asc = False
            is_rate = False

    # Build the SQL. The JOIN pivots on (date, opponent, is_home) which is the
    # natural game-key shared between player game logs and team_game_results.
    join_clause = (
        "JOIN team_game_results tgr "
        "ON tgr.date = g.date "
        "AND tgr.opponent = g.opponent "
        "AND tgr.is_home = (CASE WHEN g.vishome='H' THEN 1 ELSE 0 END) "
        "AND tgr.season = g.season"
    )

    base_filter = (
        "COALESCE(tgr.gametype, 'regular') = 'regular' "
        f"AND ({tc.sql_clause})"
    )
    params = list(tc.sql_params)
    if season is not None:
        base_filter = "g.season = ? AND " + base_filter
        params = [season] + params
    elif season_range is not None:
        base_filter = "g.season BETWEEN ? AND ? AND " + base_filter
        params = [season_range[0], season_range[1]] + params

    # Always JOIN to season_*_stats so position / league / rookie / pitcher_role
    # filters work. Tiny cost (one extra equi-join), pays for full filter
    # composition.
    ss_table = "season_pitching_stats" if is_pitching else "season_batting_stats"
    ss_join = (
        f"JOIN {ss_table} ss "
        f"ON ss.player_id = g.player_id AND ss.season = g.season "
    )
    ss_prefix = "ss"

    # Compose remaining filters using the standard helpers
    if plan.team_code:
        base_filter += " AND ss.team = ? AND tgr.team = ?"
        params += [plan.team_code, plan.team_code]

    if plan.league:
        from .response_builder import _league_team_clause
        base_filter += f" AND {_league_team_clause(plan.league, ss_prefix)}"

    if plan.rookie:
        from .response_builder import _rookie_filter
        base_filter += f" AND {_rookie_filter(ss_prefix, is_pitching)[5:]}"  # strip leading ' AND '

    if plan.position and not is_pitching:
        from .response_builder import _position_filter
        base_filter += f" AND {_position_filter(plan.position, ss_prefix, f'{ss_prefix}.season')[5:]}"

    if plan.pitcher_role == "starter":
        base_filter += f" AND {ss_prefix}.games_started > {ss_prefix}.games / 2"
    elif plan.pitcher_role == "reliever":
        base_filter += f" AND {ss_prefix}.games_started <= {ss_prefix}.games / 2"

    if plan.bats:
        base_filter += " AND p.bats = ?"
        params.append(plan.bats)
    if plan.throws:
        base_filter += " AND p.throws = ?"
        params.append(plan.throws)
    if plan.age_max:
        base_filter += f" AND ({ss_prefix}.season - CAST(SUBSTR(p.birthdate, 1, 4) AS INT)) < ? AND p.birthdate IS NOT NULL"
        params.append(plan.age_max)
    if plan.age_min:
        base_filter += f" AND ({ss_prefix}.season - CAST(SUBSTR(p.birthdate, 1, 4) AS INT)) > ? AND p.birthdate IS NOT NULL"
        params.append(plan.age_min)

    sort_order = "ASC" if sort_asc else "DESC"
    sort_label = "lowest" if sort_asc else "highest"

    sql = (
        f"SELECT p.name, "
        f"({stat_expr}) AS metric, "
        f"({qual_expr}) AS qual, "
        f"COUNT(*) AS games_n "
        f"FROM {table} g "
        f"{ss_join}"
        f"{join_clause} "
        f"JOIN players p ON p.player_id = g.player_id "
        f"WHERE {base_filter} "
        f"GROUP BY g.player_id "
        f"HAVING ({qual_expr}) >= ? AND ({stat_expr}) IS NOT NULL "
        f"ORDER BY metric {sort_order} "
        f"LIMIT ?"
    )
    # Compute the qualification threshold from the canonical rule, scaled by
    # subset fraction. Counting stats keep min_qual=1 (set above when picking
    # the stat branch); rate stats use the subset-aware qualifier.
    if is_rate:
        from .qualification import min_pa_subset, min_ip_outs_subset
        _since_year = season_range[0] if season_range else None
        if is_pitching:
            min_qual = min_ip_outs_subset(conn, season, tc.sql_clause, tc.sql_params,
                                          since_year=_since_year)
        else:
            min_qual = min_pa_subset(conn, season, tc.sql_clause, tc.sql_params,
                                     since_year=_since_year)
    params += [min_qual, plan.limit]

    cur = conn.cursor()
    try:
        cur.execute(sql, tuple(params))
    except Exception as e:
        return f"Could not run team-context query: {e}"
    rows = cur.fetchall()

    if season is not None:
        season_label = str(season)
    elif season_range is not None:
        season_label = f"Since {season_range[0]}"
    else:
        season_label = "All-Time"
    if not rows:
        # Build a description of every active filter so the user knows why the
        # result is empty. "No players found" alone reads like "the stat
        # doesn't exist" rather than "your specific filter combination has
        # zero matches" — which is what's actually happening on queries like
        # "best OPS by Yankees rookies in day games".
        filter_parts: list[str] = []
        if plan.team_code:
            from .response_builder import _team_full_name
            filter_parts.append(_team_full_name(plan.team_code))
        if plan.rookie:
            filter_parts.append("rookies")
        if plan.position:
            filter_parts.append(f"{plan.position}s")
        if plan.bats:
            filter_parts.append(
                {"L": "left-handed", "R": "right-handed", "B": "switch-hitting"}.get(plan.bats, plan.bats) + " batters"
            )
        if plan.throws:
            filter_parts.append(
                {"L": "left-handed", "R": "right-handed"}.get(plan.throws, plan.throws) + " pitchers"
            )
        if plan.pitcher_role == "starter":
            filter_parts.append("starters")
        elif plan.pitcher_role == "reliever":
            filter_parts.append("relievers")
        if plan.age_max:
            filter_parts.append(f"under {plan.age_max}")
        if plan.age_min:
            filter_parts.append(f"over {plan.age_min}")

        subject = " ".join(filter_parts) if filter_parts else "players"
        scope_phrase = season_label if (season is None and season_range is None) else f"in {season_label}"
        # Mention rate-stat qualification so users understand the floor — a
        # common reason the empty result fires (especially for rookies early
        # in the season). Counting stats don't have a meaningful threshold.
        qual_note = ""
        if is_rate:
            if qual_label == "Outs":
                ip_full = int(min_qual) // 3
                ip_part = int(min_qual) % 3
                qual_note = f" (min. {ip_full}.{ip_part} IP in {tc.label.lower()})"
            else:
                qual_note = f" (min. {int(min_qual)} {qual_label} in {tc.label.lower()})"
        return (
            f"No {subject} qualified for the {plan.stat.display_name} leaderboard "
            f"in **{tc.label}** {scope_phrase}{qual_note}."
        )

    title = (
        f"**{sort_label.capitalize()} {plan.stat.display_name} "
        f"— {tc.label} ({season_label})**\n"
    )
    parts = [title, "[LEADERBOARD]"]
    # Two-column display: stat + G. The qualification metric (ip_outs / AB)
    # is enforced internally but not surfaced — labels like "Outs" alongside
    # ERA or "AB" alongside OPS read cluttered and mislead users about what
    # the leaderboard's measuring. For rate stats we add a footer note with
    # the minimum so the qualification rule is still visible.
    parts.append(f"HEADER: {plan.stat.display_name}, G")
    for name, metric, qual, games_n in rows:
        if is_rate:
            val_str = _format_rate(metric)
        else:
            val_str = _format_val(stat_col, metric, is_rate=False)
        parts.append(f"ROW {name}: {val_str}, {games_n}")
    parts.append("[/LEADERBOARD]")
    if is_rate:
        # Format the minimum qualifier for the footer. Pitching uses ip_outs
        # internally; convert to IP display (3 outs = 1 inning) for clarity.
        if qual_label == "Outs":
            min_ip_full = int(min_qual) // 3
            min_ip_part = int(min_qual) % 3
            parts.append(f"\n_Min. {min_ip_full}.{min_ip_part} IP in subset._")
        else:
            parts.append(f"\n_Min. {int(min_qual)} {qual_label} in subset._")

    # Suggestion pills — offer obvious follow-ups for the same filter
    stat_lower = plan.stat.display_name.lower()
    pills = []
    if season is not None:
        pills.append(f"{stat_lower} {tc.label.lower()} all time")
        if plan.team_code is None:
            pills.append(f"{stat_lower} all {season} leaders")
    elif season is None and season_range is None:
        pills.append(f"{stat_lower} {tc.label.lower()} {date.today().year}")
        pills.append(f"all-time {stat_lower} leaders")
    else:
        pills.append(f"{stat_lower} {tc.label.lower()} all time")
    parts.append("\n" + "\n".join(f"[SUGGEST]{p}[/SUGGEST]" for p in pills[:3]))
    return "\n".join(parts)


def _execute_game_log_count(conn, plan: QueryPlan) -> Optional[str]:
    """Game-log counting: 'most multi-hit games', 'most 3-HR games', '4+ XBH games'."""
    if not plan.game_log_stat or not plan.game_log_threshold:
        return None

    table = "game_pitching_logs" if plan.is_pitching else "game_batting_logs"
    col = plan.game_log_stat
    threshold = plan.game_log_threshold

    # Computed stats
    _computed_cols = {
        "xbh": "(g.doubles + g.triples + g.home_runs)",
    }
    col_expr = _computed_cols.get(col, f"g.{col}")

    # Default to current season if none — prevents unbounded scan of 661K+ rows
    effective_season = plan.season or date.today().year
    season_filter = f" AND g.season = ?"
    params = [threshold, effective_season]

    cur = conn.cursor()

    _game_stat_labels = {
        "hits": "Hits", "home_runs": "HR", "rbi": "RBI", "runs": "Runs",
        "stolen_bases": "SB", "walks": "BB", "strikeouts": "K", "doubles": "2B",
        "triples": "3B", "xbh": "XBH",
    }
    stat_name = _game_stat_labels.get(col, col.replace("_", " ").title())

    # Player-filtered: show individual games for this player
    if plan.player_name:
        player_filter = " AND p.name = ?"
        player_params = list(params) + [plan.player_name]
        cur.execute(
            f"SELECT g.date, g.opponent, g.hits, g.at_bats, "
            f"g.doubles, g.triples, g.home_runs, g.rbi, g.runs "
            f"FROM {table} g "
            f"JOIN players p ON g.player_id = p.player_id "
            f"WHERE {col_expr} >= ?{season_filter}{player_filter} "
            f"ORDER BY g.date ASC LIMIT 50",
            tuple(player_params),
        )
        games = cur.fetchall()
        scope_label = str(plan.season) if plan.season else "All-Time"
        if not games:
            return f"{plan.player_name} has no games with {threshold}+ {stat_name} ({scope_label})."

        title = f"**{plan.player_name} — {len(games)} games with {threshold}+ {stat_name} ({scope_label})**\n"
        parts = [title]
        parts.append("[LEADERBOARD]")

        if col == "xbh":
            parts.append("HEADER: XBH, 2B, 3B, HR, H-AB")
            for dt, opp, h, ab, d, t, hr, rbi, r in games:
                xbh = (d or 0) + (t or 0) + (hr or 0)
                try:
                    from datetime import datetime as _dt
                    dt_fmt = _dt.strptime(dt, "%Y-%m-%d").strftime("%-m/%-d")
                except Exception:
                    dt_fmt = dt
                parts.append(f"ROW {dt_fmt} vs {opp or '?'}: {xbh}, {d or 0}, {t or 0}, {hr or 0}, {h}-{ab}")
        else:
            parts.append("HEADER: H-AB, HR, 2B, RBI, R")
            for dt, opp, h, ab, d, t, hr, rbi, r in games:
                try:
                    from datetime import datetime as _dt
                    dt_fmt = _dt.strptime(dt, "%Y-%m-%d").strftime("%-m/%-d")
                except Exception:
                    dt_fmt = dt
                parts.append(f"ROW {dt_fmt} vs {opp or '?'}: {h}-{ab}, {hr or 0}, {d or 0}, {rbi or 0}, {r or 0}")

        parts.append("[/LEADERBOARD]")
        return "\n".join(parts)

    # Leaderboard mode: who had the most such games
    cur.execute(
        f"SELECT p.name, COUNT(*) AS game_count "
        f"FROM {table} g "
        f"JOIN players p ON g.player_id = p.player_id "
        f"WHERE {col_expr} >= ?{season_filter} "
        f"GROUP BY g.player_id "
        f"ORDER BY game_count DESC LIMIT 500",
        tuple(params),
    )
    rows = cur.fetchall()
    if not rows:
        season_note = f" in {plan.season}" if plan.season else ""
        extra = ""
        if col == "strikeouts" and plan.is_pitching:
            extra = f"\n[DIDYOUMEAN]most {threshold}+ K games by a hitter[/DIDYOUMEAN]"
        elif col == "strikeouts" and not plan.is_pitching:
            extra = f"\n[DIDYOUMEAN]most {threshold}+ K games by a pitcher[/DIDYOUMEAN]"
        return f"No players found with games meeting that criteria{season_note}.{extra}" + _empty_result_pills(plan)

    # Count total qualifying players (not just the limited set)
    cur.execute(
        f"SELECT COUNT(DISTINCT g.player_id) "
        f"FROM {table} g "
        f"WHERE {col_expr} >= ?{season_filter}",
        tuple(params[:1] + (params[1:2] if plan.season else [])),
    )
    total_players = cur.fetchone()[0]

    scope_label = str(plan.season) if plan.season else "All-Time"
    title = f"**Most {threshold}+ {stat_name} Games ({scope_label})**"
    parts = [title]
    parts.append(f"{total_players} players.\n")
    parts.append("[TIP]Tap a player name for their full profile.[/TIP]")
    parts.append("[LEADERBOARD]")
    parts.append("HEADER: Games")
    season_suffix = f" {plan.season}" if plan.season else ""
    for i, row in enumerate(rows):
        player_name = row[0]
        count = row[1]
        drilldown_query = f"{player_name} games with {threshold}+ {stat_name.lower()}{season_suffix}"
        parts.append(f"ROW {i+1}. {player_name}: [DRILLDOWN]{drilldown_query}[/DRILLDOWN]{count}")
    parts.append("[/LEADERBOARD]")

    # See-also for K disambiguation — insert after title, before leaderboard
    if col == "strikeouts" and plan.is_pitching:
        parts.insert(2, f"[DIDYOUMEAN]most {threshold}+ K games by a hitter[/DIDYOUMEAN]")
    elif col == "strikeouts" and not plan.is_pitching:
        parts.insert(2, f"[DIDYOUMEAN]most {threshold}+ K games by a pitcher[/DIDYOUMEAN]")
    parts.append(f"[SUGGEST]{stat_name} leaders[/SUGGEST]")

    return "\n".join(parts)


def _execute_game_log_extreme(conn, plan: QueryPlan) -> Optional[str]:
    """Per-game extreme: 'most K in one game'."""
    from .response_builder import build_single_game_extreme
    result = build_single_game_extreme(plan.stat, plan.season, plan.is_pitching, plan.position)
    if result and plan.stat:
        stat_name = plan.stat.display_name.lower()
        # Add suggestion for the other side (batting/pitching)
        if plan.stat.db_column == "strikeouts" and not plan.is_pitching:
            result += f"\n\n[SUGGEST]most K in one game by a pitcher[/SUGGEST]"
        elif plan.stat.db_column == "strikeouts" and plan.is_pitching:
            result += f"\n\n[SUGGEST]most K in one game by a hitter[/SUGGEST]"
        result += f"\n[SUGGEST]{stat_name} leaders[/SUGGEST]"
    return result


def _execute_per_team_leaders(conn, plan: QueryPlan) -> Optional[str]:
    """Per-team individual leaders: 'ERA leaders on each team'."""
    if not plan.stat:
        return None

    season = plan.season or date.today().year
    stat_expr, abbrev, name, is_rate = _stat_expr(plan, "s")
    table = "season_pitching_stats" if plan.is_pitching else "season_batting_stats"
    col = plan.stat.db_column

    # Lower-is-better stats
    lower_better = col in ("era", "whip", "bb_per_9", "hr_per_9")
    order = "ASC" if lower_better else "DESC"

    # PA/IP minimum for rate stats — MLB prorated
    if is_rate:
        if plan.is_pitching:
            min_filter = f"AND s.ip_outs >= {_qual_min_ip_outs(conn, season)}"
        else:
            min_filter = f"AND s.plate_appearances >= {_qual_min_pa(conn, season)}"
    else:
        min_filter = ""

    # Use window function to rank within each team
    cur = conn.cursor()
    cur.execute(f"""
        SELECT name, team, stat_val FROM (
            SELECT p.name, s.team, {stat_expr} AS stat_val,
                   ROW_NUMBER() OVER (PARTITION BY s.team ORDER BY {stat_expr} {order}) AS rn
            FROM {table} s
            JOIN players p ON s.player_id = p.player_id
            WHERE s.season = ? {min_filter}
        ) ranked
        WHERE rn = 1
        ORDER BY stat_val {order}
    """, (season,))
    rows = cur.fetchall()

    if not rows:
        return f"No per-team {abbrev} leaders found ({season})." + _empty_result_pills(plan)

    # Import team display
    from services.notable_events import team_display

    title = f"**{season} {name} Leader on Each Team**\n"
    parts = [title]
    parts.append("[TIP]Tap a player name for their full profile.[/TIP]")
    parts.append("[LEADERBOARD]")
    parts.append(f"HEADER: Team, {abbrev}")
    for player_name, team_code, val in rows:
        team_name = team_display(team_code) if team_code else team_code
        formatted = _format_val(plan.stat.db_column, val, is_rate)
        parts.append(f"ROW {player_name}: {team_name}, {formatted}")
    parts.append("[/LEADERBOARD]")

    return "\n".join(parts)


def _execute_player_single_season_max(plan: QueryPlan) -> Optional[str]:
    """A specific player's career-best single season for a stat.

    Triggered when `decompose()` sees `scope=all_time` (single-season records
    semantics) combined with a `player_name`. Delegates to
    response_builder.build_player_single_season_max, which does the SQL.

    Direction maps from the plan's sort_asc + lower-is-better flag the same
    way the leaderboard executor does:
      - sort_asc=False, !lower_better → max (e.g., "most HR")
      - sort_asc=False,  lower_better → min (e.g., "best ERA")
      - sort_asc=True,  !lower_better → min (e.g., "fewest HR")
      - sort_asc=True,   lower_better → max (e.g., "worst ERA")
    """
    if not plan.player_name or not plan.stat:
        return None
    from .response_builder import build_player_single_season_max
    from . import name_matcher as _nm

    is_lower_better = plan.stat.db_column in _LOWER_IS_BETTER
    if plan.sort_asc and is_lower_better:
        direction = "max"
    elif plan.sort_asc:
        direction = "min"
    elif is_lower_better:
        direction = "min"
    else:
        direction = "max"

    is_pitching = _nm.is_pitcher(plan.player_name) or _nm.is_pitching_stat(plan.stat)
    return build_player_single_season_max(
        plan.player_name, plan.stat, direction=direction, is_pitching=is_pitching
    )


def _execute_player_sliding_window(plan: QueryPlan) -> Optional[str]:
    """Best/worst N-consecutive-game stretch for a player + counting stat.

    Delegates to response_builder.build_player_sliding_window, which scans the
    player's game logs and slides an N-game window within each season.
    """
    if not plan.player_name or not plan.stat or not plan.sliding_window_n:
        return None
    from .response_builder import build_player_sliding_window
    from . import name_matcher as _nm

    direction = "min" if plan.sort_asc else "max"
    is_pitching = _nm.is_pitcher(plan.player_name) or _nm.is_pitching_stat(plan.stat)
    return build_player_sliding_window(
        plan.player_name, plan.stat, plan.sliding_window_n,
        direction=direction, is_pitching=is_pitching,
    )


def _execute_team_ranking(conn, plan: QueryPlan) -> Optional[str]:
    """Team aggregate: 'which team had the most HR'."""
    from .response_builder import build_team_ranking
    if plan.stat:
        season = plan.season or datetime.now().year
        return build_team_ranking(plan.stat, season)
    return None


# ===================================================================
# Streak sequence executor
# ===================================================================

def _execute_streak_sequence(conn, plan: QueryPlan) -> Optional[str]:
    """Find consecutive-game streaks from game logs.

    Handles:
    - "sliding" — find longest/most recent streak of N games anywhere in season(s)
    - "leading" — check from start of season (first N games)
    - "trailing" — current active streak
    """
    if not plan.streak_conditions:
        return None

    # Combine all conditions with AND
    condition = " AND ".join(plan.streak_conditions)

    # Auto-detect pitching if conditions reference pitching columns
    pitching_cols = {"g.ip_outs", "g.earned_runs", "g.is_start", "g.win", "g.loss", "g.save"}
    is_pitching = plan.is_pitching or any(pc in c for c in plan.streak_conditions for pc in pitching_cols)
    table = "game_pitching_logs" if is_pitching else "game_batting_logs"

    # Build season filter. Scope precedence:
    #   1. career / all_time → no season filter (full historical scan, ~4.8M rows)
    #   2. explicit plan.season → single-season filter
    #   3. plan.since_year → since-year filter
    #   4. trailing (active) streaks → current + prior season
    #   5. fallback → current season only (to keep accidental scans off hot path)
    # The career/all_time branch must come FIRST so that "longest hitting
    # streak all time" (decompose correctly sets scope=career) doesn't get
    # silently narrowed to the current year by the fallback.
    season_filter = ""
    season_params = []
    if plan.streak_direction == "trailing":
        # Active/current streaks: 90-day window. This branch runs FIRST
        # (before the career/all_time check) because "active" in the user's
        # query promotes plan.scope to "career" upstream, but trailing
        # detection is fundamentally about CURRENT streaks — a full
        # historical scan doesn't help find them, it just hangs the
        # query for 60+s on the 4.8M-row sort. The longest active streak
        # in MLB history (DiMaggio 56) took ~60 days, so 90 days is
        # plenty of headroom. Uses the (player_id, date) composite index.
        from datetime import datetime as _dt, timedelta as _td
        floor_date = (_dt.now() - _td(days=90)).date().isoformat()
        season_filter = " AND g.date >= ?"
        season_params = [floor_date]
        # Clear any career/all_time scope set by "active" trigger upstream
        # so downstream title/header logic doesn't render this as a
        # career-scope result.
        plan.season = None
    elif plan.scope in ("career", "all_time"):
        # Full historical scan — needed for records like DiMaggio's 56.
        plan.season = None
    elif plan.season:
        season_filter = " AND g.season = ?"
        season_params = [plan.season]
    elif plan.since_year:
        season_filter = " AND g.season >= ?"
        season_params = [plan.since_year]
    else:
        # No season specified — default to current season to avoid scanning 600K+ rows
        from datetime import datetime as _dt
        season_filter = " AND g.season = ?"
        season_params = [_dt.now().year]

    # Team filter
    team_filter = ""
    team_params = []
    if plan.team_code:
        # Join with season stats to get team
        team_filter = (" AND EXISTS (SELECT 1 FROM season_batting_stats sbs "
                       "WHERE sbs.player_id = g.player_id AND sbs.season = g.season "
                       "AND (sbs.team = ? OR sbs.team LIKE ? OR sbs.team LIKE ?))")
        team_params = [plan.team_code, f"{plan.team_code}/%", f"%/{plan.team_code}"]

    # Get all qualifying game logs, ordered by player + date
    cur = conn.cursor()
    cur.execute(
        f"SELECT g.player_id, p.name, g.season, g.date, "
        f"CASE WHEN {condition} THEN 1 ELSE 0 END AS success "
        f"FROM {table} g "
        f"JOIN players p ON g.player_id = p.player_id "
        f"WHERE 1=1{season_filter}{team_filter} "
        f"ORDER BY g.player_id, g.season, g.date",
        tuple(season_params + team_params),
    )
    rows = cur.fetchall()

    if not rows:
        return f"No game log data found to search for that streak."

    label = " + ".join(plan.streak_condition_labels) if plan.streak_condition_labels else "success"
    target_length = plan.streak_length

    if plan.streak_direction == "leading":
        return _streak_leading(rows, target_length, label, plan)
    elif plan.streak_direction == "tail_window":
        return _streak_tail_window(rows, target_length, label, plan)
    elif plan.streak_direction == "trailing":
        return _streak_trailing(rows, target_length, label, plan)
    else:  # sliding
        return _streak_sliding(rows, target_length, label, plan)


def _streak_sliding(rows, target_length, label, plan) -> Optional[str]:
    """Find players with the longest consecutive-game streaks, or who achieved N+ games."""
    # Group by player-season
    from collections import defaultdict
    player_seasons = defaultdict(list)
    player_names = {}
    for pid, name, season, date, success in rows:
        player_seasons[(pid, season)].append(success)
        player_names[pid] = name

    # Find streaks
    results = []  # (streak_len, player_name, season, start_game, end_game)
    for (pid, season), games in player_seasons.items():
        current_streak = 0
        max_streak = 0
        max_end = 0
        for i, success in enumerate(games):
            if success:
                current_streak += 1
                if current_streak > max_streak:
                    max_streak = current_streak
                    max_end = i
            else:
                current_streak = 0
        if max_streak > 0:
            if target_length is None or max_streak >= target_length:
                results.append((max_streak, player_names[pid], season))

    if not results:
        scope = str(plan.season) if plan.season else f"Since {plan.since_year}" if plan.since_year else "All-Time"
        if target_length:
            return f"No {target_length}+ game streaks of {label} found ({scope})." + _empty_result_pills(plan)
        # No target_length means "find the longest streak ever". An empty
        # result here means there are no games meeting the per-game
        # condition at all — still a deterministic answer, not a
        # mystery for Sonnet.
        return f"No game streaks of {label} found ({scope})." + _empty_result_pills(plan)

    # Sort by streak length descending
    results.sort(key=lambda x: x[0], reverse=True)

    # Title with count
    scope = str(plan.season) if plan.season else f"Since {plan.since_year}" if plan.since_year else "All-Time"
    total = len(results)
    if target_length:
        title = f"**{total} streak{'s' if total != 1 else ''} of {target_length}+ consecutive games with {label} ({scope})**\n"
    else:
        title = f"**Longest Streaks of {label} ({scope})**\n"

    _MAX_ROWS = 500
    parts = [title]
    parts.append("[TIP]Tap a player name for their full profile.[/TIP]")
    parts.append("[LEADERBOARD]")
    single_season = plan.season is not None
    if single_season:
        parts.append("HEADER: Games")
        for i, (streak_len, name, season) in enumerate(results[:_MAX_ROWS]):
            parts.append(f"ROW {i+1}. {name}: {streak_len}")
    else:
        parts.append("HEADER: Games, Year")
        for i, (streak_len, name, season) in enumerate(results[:_MAX_ROWS]):
            parts.append(f"ROW {i+1}. {name}: {streak_len}, {season}")
    parts.append("[/LEADERBOARD]")

    return "\n".join(parts)


def _streak_leading(rows, target_length, label, plan) -> Optional[str]:
    """Find players who achieved the condition in their first N games.

    Two modes based on plan.scope:
      - season-mode (default): group by (player_id, season), check
        each player-season's first N games independently. Powers
        "10+ K in first 3 starts of 2026" and "in first 3 starts of
        any season."
      - career-mode (plan.scope in {"career", "all_time"}): group by
        player_id only, check the player's first N games of their
        entire career. Powers "10+ K in first 3 career starts" — the
        debut window. Each player produces one row labeled by debut
        season span.
    """
    from collections import defaultdict

    if not target_length:
        target_length = 10  # default

    # career-debut grouping fires ONLY when the user said "career" explicitly
    # (plan.scope=="career"). plan.scope=="all_time" is set by the any-season
    # trigger, which means "for each player-season across all years" — that's
    # the default season-mode (group by player+season), NOT career-debut.
    career_mode = plan.scope == "career"

    if career_mode:
        # rows is already ordered by (player_id, season, date). Group
        # by player_id only and slice the first N games chronologically.
        player_games = defaultdict(list)
        player_names = {}
        for pid, name, season, date, success in rows:
            player_games[pid].append((season, date, success))
            player_names[pid] = name

        results = []
        for pid, games in player_games.items():
            if len(games) < target_length:
                continue
            first_n = games[:target_length]
            if all(s == 1 for _, _, s in first_n):
                debut_season = first_n[0][0]
                end_season = first_n[-1][0]
                span = (str(debut_season) if debut_season == end_season
                        else f"{debut_season}-{end_season}")
                results.append((player_names[pid], span))

        if not results:
            return (f"**No players have had {label} in Each of Their "
                    f"First {target_length} Career Games.**" + _empty_result_pills(plan))

        title = (f"**Players with {label} in Each of Their First "
                 f"{target_length} Career Games**\n")
        # Sort: prefer recent debut spans first
        results.sort(key=lambda x: x[1], reverse=True)
        parts = [title, "[TIP]Tap a player name for their full profile.[/TIP]",
                 "[LEADERBOARD]", "HEADER: Debut"]
        for i, (name, span) in enumerate(results[:50]):
            parts.append(f"ROW {i+1}. {name}: {span}")
        parts.append("[/LEADERBOARD]")
        return "\n".join(parts)

    # Season-mode (default)
    player_seasons = defaultdict(list)
    player_names = {}
    for pid, name, season, date, success in rows:
        player_seasons[(pid, season)].append(success)
        player_names[pid] = name

    results = []
    for (pid, season), games in player_seasons.items():
        if len(games) < target_length:
            continue
        first_n = games[:target_length]
        if all(g == 1 for g in first_n):
            results.append((player_names[pid], season, target_length))

    if not results:
        # Deterministic 0-matched answer. Previously fell through to LLM
        # but that mixed two different cases (legit-zero vs misparse) and
        # made audit discipline impossible. Audit-discipline note in
        # feedback-routing-audit-discipline.md.
        scope_label = (str(plan.season) if plan.season
                       else f"since {plan.since_year}" if plan.since_year
                       else "across the eras we have data for")
        return (f"**No players had {label} in each of their first "
                f"{target_length} games ({scope_label}).**" + _empty_result_pills(plan))

    scope = str(plan.season) if plan.season else f"Since {plan.since_year}" if plan.since_year else "2016-2025"
    title = f"**Players with {label} in Each of Their First {target_length} Games ({scope})**\n"
    # Sort by most recent first
    results.sort(key=lambda x: x[1], reverse=True)

    parts = [title]
    parts.append("[TIP]Tap a player name for their full profile.[/TIP]")
    parts.append("[LEADERBOARD]")
    parts.append("HEADER: Year")
    for i, (name, season, _) in enumerate(results[:50]):
        parts.append(f"ROW {i+1}. {name}: {season}")
    parts.append("[/LEADERBOARD]")

    return "\n".join(parts)


def _streak_tail_window(rows, target_length, label, plan) -> Optional[str]:
    """Find players who achieved the condition in EACH of their last N games.

    Symmetric to _streak_leading but operates on the tail. Distinct from
    _streak_trailing (which finds active streaks of varying length from the
    most recent game backwards) — this checks "all of last N met threshold"
    where N is fixed.

    Modes:
      - season-mode: per (player_id, season), last N games of that season
      - career-mode (plan.scope in {"career","all_time"}): per player_id,
        last N games chronologically across all their history
    """
    from collections import defaultdict

    if not target_length:
        target_length = 10

    # career-debut grouping fires ONLY when the user said "career" explicitly
    # (plan.scope=="career"). plan.scope=="all_time" is set by the any-season
    # trigger, which means "for each player-season across all years" — that's
    # the default season-mode (group by player+season), NOT career-debut.
    career_mode = plan.scope == "career"

    if career_mode:
        player_games = defaultdict(list)
        player_names = {}
        for pid, name, season, date, success in rows:
            player_games[pid].append((season, date, success))
            player_names[pid] = name

        results = []
        for pid, games in player_games.items():
            if len(games) < target_length:
                continue
            last_n = games[-target_length:]
            if all(s == 1 for _, _, s in last_n):
                start_season = last_n[0][0]
                end_season = last_n[-1][0]
                span = (str(end_season) if start_season == end_season
                        else f"{start_season}-{end_season}")
                results.append((player_names[pid], span))

        if not results:
            return (f"**No players had {label} in each of their last "
                    f"{target_length} games (career-mode).**"
                    + _empty_result_pills(plan))

        title = (f"**Players with {label} in Each of Their Last "
                 f"{target_length} Games**\n")
        results.sort(key=lambda x: x[1], reverse=True)
        parts = [title, "[TIP]Tap a player name for their full profile.[/TIP]",
                 "[LEADERBOARD]", "HEADER: Span"]
        for i, (name, span) in enumerate(results[:50]):
            parts.append(f"ROW {i+1}. {name}: {span}")
        parts.append("[/LEADERBOARD]")
        return "\n".join(parts)

    # Season-mode
    player_seasons = defaultdict(list)
    player_names = {}
    for pid, name, season, date, success in rows:
        player_seasons[(pid, season)].append(success)
        player_names[pid] = name

    results = []
    for (pid, season), games in player_seasons.items():
        if len(games) < target_length:
            continue
        last_n = games[-target_length:]
        if all(g == 1 for g in last_n):
            results.append((player_names[pid], season, target_length))

    if not results:
        scope_label = (str(plan.season) if plan.season
                       else f"since {plan.since_year}" if plan.since_year
                       else "across the eras we have data for")
        return (f"**No players had {label} in each of their last "
                f"{target_length} games ({scope_label}).**"
                + _empty_result_pills(plan))

    scope = str(plan.season) if plan.season else f"Since {plan.since_year}" if plan.since_year else "2016-2025"
    title = f"**Players with {label} in Each of Their Last {target_length} Games ({scope})**\n"
    results.sort(key=lambda x: x[1], reverse=True)
    parts = [title, "[TIP]Tap a player name for their full profile.[/TIP]",
             "[LEADERBOARD]", "HEADER: Year"]
    for i, (name, season, _) in enumerate(results[:50]):
        parts.append(f"ROW {i+1}. {name}: {season}")
    parts.append("[/LEADERBOARD]")
    return "\n".join(parts)


def _streak_trailing(rows, target_length, label, plan) -> Optional[str]:
    """Find current active streaks (from most recent game backwards)."""
    from collections import defaultdict
    player_seasons = defaultdict(list)
    player_names = {}
    for pid, name, season, date, success in rows:
        player_seasons[(pid, season)].append(success)
        player_names[pid] = name

    # For trailing, we want the most recent season per player
    current_year = datetime.now().year
    results = []
    seen_players = set()
    for (pid, season) in sorted(player_seasons.keys(), key=lambda x: x[1], reverse=True):
        if pid in seen_players:
            continue
        seen_players.add(pid)
        games = player_seasons[(pid, season)]
        # Count from the end
        streak = 0
        for success in reversed(games):
            if success:
                streak += 1
            else:
                break
        if streak > 0:
            if target_length is None or streak >= target_length:
                results.append((streak, player_names[pid], season))

    if not results:
        # Clean up label for the empty-result message
        _empty_label_map = {"a hit": "hitting", "a HR": "HR", "a BB": "walk",
                            "scoreless": "scoreless"}
        display_label = _empty_label_map.get(label, label)
        return (f"**No active {display_label} streaks of any length right now.**"
                + _empty_result_pills(plan))

    results.sort(key=lambda x: x[0], reverse=True)

    # Clean up label for title: "a hit" → "Hitting", "a HR" → "HR"
    _label_map = {"a hit": "Hitting", "a HR": "HR", "a BB": "Walk", "scoreless": "Scoreless"}
    display_label = _label_map.get(label, label)
    title = f"**Active {display_label} Streaks**\n"
    parts = [title]
    parts.append("[TIP]Tap a player name for their full profile.[/TIP]")
    parts.append("[LEADERBOARD]")
    parts.append("HEADER: Games")
    for i, (streak_len, name, season) in enumerate(results[:50]):
        parts.append(f"ROW {i+1}. {name}: {streak_len}")
    parts.append("[/LEADERBOARD]")

    return "\n".join(parts)
# force redeploy
