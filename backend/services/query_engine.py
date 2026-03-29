"""
Structured Query Engine — decomposes natural language baseball queries into SQL.

Replaces pattern-matching interceptors with a general-purpose query composer
that knows the full database schema and can handle any combination of
stat + filters + scope + grouping.

Claude (Haiku/Sonnet) is the fallback for questions requiring knowledge
OUTSIDE the database (awards, game events, historical context).
"""

import logging
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from .name_matcher import (
    StatInfo, SplitContext,
    match_stat, detect_season, detect_league,
    _detect_since_year, _detect_rookie, _detect_position,
    _detect_split_context, _detect_pitcher_role,
    is_pitching_stat, find_player_in_text, match_player,
    stat_alias_map, _extract_threshold,
    _POSITION_MAP,
)

logger = logging.getLogger("statchat.query_engine")

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
    "stole", "stolen", "stealing",
    "threw", "thrown", "throwing",
    "pitched", "pitching",
    "drove", "driven",
    "struck", "walked", "walking",
    "scored", "allowed", "given", "gave",
    "during", "when", "where", "only",
    "ago", "back", "since", "sub",
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
    scope: str = "current_season"  # "current_season", "all_time", "career", "since_YYYY"
    season: Optional[int] = None
    since_year: Optional[int] = None

    # Filters
    league: Optional[str] = None
    position: Optional[list] = None
    bats: Optional[str] = None  # "L", "R", "B"
    rookie: bool = False
    pitcher_role: Optional[str] = None  # "starter", "reliever"
    age_max: Optional[int] = None
    age_min: Optional[int] = None
    split_context: Optional[SplitContext] = None
    team_code: Optional[str] = None

    # Query shape
    query_type: str = "leaderboard"  # "leaderboard", "threshold", "count", "superlative", "game_log_count", "game_log_extreme", "team_ranking"
    threshold: Optional[float] = None
    comparison: str = ">="  # ">=" or "<="
    sort_asc: bool = False
    limit: int = 50
    superlative: Optional[str] = None  # "youngest", "oldest", "first", "last"
    game_log_stat: Optional[str] = None  # for game-log queries: column in game logs
    game_log_threshold: Optional[int] = None  # for game-log counting: hits >= N

    # Multi-threshold filters (e.g., ".300 with 30 HR")
    extra_filters: list = field(default_factory=list)  # [{stat, threshold, comparison}]

    # Validation
    is_pitching: bool = False
    unexplained_words: list = field(default_factory=list)
    consumed_words: set = field(default_factory=set)

    @property
    def is_valid(self) -> bool:
        """A plan is valid if we have a stat, no unexplained words, and it's a type we handle."""
        if self.query_type in ("definition", "multi_threshold"):
            return False  # Handled by specialized parsers
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
            if threshold is not None:
                return StatCondition(None, key, threshold, comparison, text)
            return None

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
    # "30 HR", "HR leaders", ".800 OPS", "200+ K"
    if not stat:
        # Strip +/- from numbers before stat matching ("200+ K" → "200 K")
        cleaned = re.sub(r'(\d)\+', r'\1 ', lower)
        stat = match_stat(cleaned)

    if not stat:
        return None

    # Build consumed text from just the matched stat alias + threshold
    consumed_parts = []
    for alias in sorted(stat_alias_map.keys(), key=len, reverse=True):
        if alias in lower:
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
    lower = question.strip().lower()

    # --- Early detection: stat definitions ---
    # "what is OPS", "explain BABIP", "define ERA" — not a DB query
    definition_triggers = ["what is ", "what's ", "explain ", "define ", "what does ", "how is ", "how do you calculate"]
    if any(lower.startswith(t) or t in lower for t in definition_triggers):
        # Check if there's a stat keyword and no leaderboard/threshold trigger
        has_leaderboard = any(t in lower for t in ["leaders", "leader", "most", "best", "top", "highest", "lowest"])
        if not has_leaderboard:
            plan.query_type = "definition"
            return plan  # Let the old stat definition parser handle it

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

    # Team ranking
    team_triggers = ["what team", "which team", "what teams", "which teams"]
    if any(t in lower for t in team_triggers):
        plan.query_type = "team_ranking"
        _add_consumed(plan, "what which team teams")

    # Sort direction
    if any(t in lower for t in ["worst", "fewest"]):
        plan.sort_asc = True
        _add_consumed(plan, "worst fewest")
    if any(t in lower for t in ["best", "highest", "most", "top", "leaders", "leader",
                                  "leaderboard", "lowest", "who led", "who leads", "leading"]):
        if plan.query_type not in ("count", "superlative", "team_ranking"):
            plan.query_type = "leaderboard"
        _add_consumed(plan, "best highest most top leaders leader leaderboard lowest who led leads leading")

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
                    for alias in sorted(stat_alias_map.keys(), key=len, reverse=True):
                        if alias in lower:
                            _add_consumed(plan, alias)
                            break

    # --- Detect scope/season ---
    since_year = _detect_since_year(lower)
    if since_year:
        plan.since_year = since_year
        plan.scope = f"since_{since_year}"
        _add_consumed(plan, "since this last past decade century years year")

    # "in a season" / "ever" / "in history" = all-time
    all_time_triggers = ["all time", "all-time", "single season", "in a season",
                         "in a year", "in history", "record"]
    if any(t in lower for t in all_time_triggers):
        plan.scope = "all_time"
        _add_consumed(plan, "all time all-time single season in a season in a year in history record")

    if "career" in lower:
        plan.scope = "career"
        _add_consumed(plan, "career")

    # Only detect explicit season if since_year didn't already claim the year
    if not plan.since_year:
        season = detect_season(lower, default_to_most_recent=False)
        if season:
            plan.season = season
            plan.scope = f"season_{season}"
            _add_consumed(plan, str(season) + " last this year season")
    if plan.scope == "current_season":
        # Default to current year for leaderboards, no default for all-time
        if plan.query_type in ("leaderboard", "team_ranking"):
            # Past tense → last year
            if any(p in lower for p in ["who led", "who had", "who hit the most"]):
                plan.season = datetime.now().year - 1
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

    plan.pitcher_role = _detect_pitcher_role(lower)
    if plan.pitcher_role:
        _add_consumed(plan, "starter starters starting reliever relievers relief closer closers bullpen")
        plan.is_pitching = True

    # Bare "pitcher/pitching" context (without starter/reliever) still means pitching
    if not plan.is_pitching and any(w in lower for w in ["pitcher", "pitchers", "pitching", "pitched"]):
        plan.is_pitching = True

    plan.split_context = _detect_split_context(lower)
    if plan.split_context:
        for phrase in plan.split_context.consumed_phrases:
            _add_consumed(plan, phrase)

    # Bats filter
    bats_patterns = [
        ("left-handed batter", "L"), ("left handed batter", "L"), ("lefty batter", "L"),
        ("left-handed hitter", "L"), ("left handed hitter", "L"), ("lefty hitter", "L"),
        ("right-handed batter", "R"), ("right handed batter", "R"), ("righty batter", "R"),
        ("right-handed hitter", "R"), ("right handed hitter", "R"), ("righty hitter", "R"),
        ("switch hitter", "B"), ("switch-hitter", "B"),
    ]
    for pattern, bats_val in bats_patterns:
        if pattern in lower:
            plan.bats = bats_val
            _add_consumed(plan, pattern)
            break

    # Age filter — only match "under/over N" when it's about age, not stats.
    # "player under 25" = age. "with under 10 HR" = stat filter (not age).
    # Heuristic: age if followed by nothing or age-like words, not if followed by a stat keyword.
    age_match = re.search(r'\b(?:under|younger than)\s+(\d+)(?:\s+(?:years?\s+old|year-old|y/?o))?\b', lower)
    if age_match:
        # Check if this "under N" is followed by a stat keyword → not age
        after = lower[age_match.end():].strip()
        following_stat = match_stat(after.split()[0] if after.split() else "")
        if not following_stat:
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
    if plan.threshold is None and plan.stat and not plan.extra_filters:
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

    elif plan.threshold is None and plan.derived_stat and not plan.extra_filters:
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
        _add_consumed(plan, "in a game in one game in a single game single")

    # Game-log counting: "most 3-hit games", "most games with 3+ RBI", "most 10-K games"
    # Pattern 1: "N-stat games" / "N+ stat games"
    multi_game_match = re.search(r'(\d+)[+-]?\s*(?:hit|hr|home run|homer|rbi|strikeout|k)\s*game', lower)
    # Pattern 2: "games with N+ stat"
    if not multi_game_match:
        multi_game_match = re.search(r'games?\s+with\s+(\d+)\+?\s*(?:hit|hr|home run|homer|rbi|strikeout|k)', lower)

    if multi_game_match:
        plan.query_type = "game_log_count"
        n = int(multi_game_match.group(1))
        context = multi_game_match.group(0).lower()
        if any(w in context for w in ["hit"]):
            plan.game_log_stat = "hits"
        elif any(w in context for w in ["hr", "home run", "homer"]):
            plan.game_log_stat = "home_runs"
        elif "rbi" in context:
            plan.game_log_stat = "rbi"
        elif any(w in context for w in ["strikeout", " k"]):
            plan.game_log_stat = "strikeouts"
        plan.game_log_threshold = n
        _add_consumed(plan, multi_game_match.group(0).replace("+", ""))

    if "multi-hit" in lower or "multi hit" in lower:
        plan.query_type = "game_log_count"
        plan.game_log_stat = "hits"
        plan.game_log_threshold = 2
        _add_consumed(plan, "multi-hit multi hit")

    if "multi-homer" in lower or "multi homer" in lower or "multi-hr" in lower or "multi hr" in lower:
        plan.query_type = "game_log_count"
        plan.game_log_stat = "home_runs"
        plan.game_log_threshold = 2
        _add_consumed(plan, "multi-homer multi homer multi-hr multi hr")

    # --- Resolve "lowest" for rate stats ---
    if "lowest" in question.lower():
        if plan.stat and plan.stat.db_column in _LOWER_IS_BETTER:
            plan.sort_asc = False  # "lowest ERA" = best ERA = natural sort
        else:
            plan.sort_asc = True  # "lowest OPS" = worst

    # --- Check for unexplained words ---
    words = re.findall(r"[a-z0-9'+%-]+", lower)
    for w in words:
        w_clean = w.strip("?.!,'")
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
        plan.unexplained_words.append(w_clean)

    return plan


# ---------------------------------------------------------------------------
# Compose SQL — build and execute the query from a QueryPlan
# ---------------------------------------------------------------------------

def _get_db() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH)


def _format_rate(value) -> str:
    """Format rate stats: .302 not 0.302."""
    try:
        fv = float(value)
        if fv < 1:
            return f".{int(round(fv * 1000)):03d}"
        return f"{fv:.3f}"
    except (ValueError, TypeError):
        return str(value)


def _format_val(stat_col: str, value, is_rate: bool = False) -> str:
    """Format a value for display."""
    if value is None:
        return "--"
    if is_rate or stat_col in _RATE_STATS:
        if stat_col in ("era", "whip", "k_per_9", "bb_per_9", "hr_per_9", "k_per_bb",
                         "h_per_9", "hr_per_9", "fip"):
            try:
                return f"{float(value):.2f}"
            except (ValueError, TypeError):
                return str(value)
        return _format_rate(value)
    if stat_col == "ip_outs":
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


def execute(plan: QueryPlan) -> Optional[str]:
    """Execute a QueryPlan and return formatted response text, or None."""
    if not plan.is_valid:
        return None

    conn = _get_db()
    try:
        if plan.query_type == "game_log_count":
            return _execute_game_log_count(conn, plan)
        elif plan.query_type == "game_log_extreme":
            return _execute_game_log_extreme(conn, plan)
        elif plan.query_type == "team_ranking":
            return _execute_team_ranking(conn, plan)
        elif plan.split_context is not None:
            return _execute_split_leaderboard(conn, plan)
        elif plan.query_type == "count":
            return _execute_count(conn, plan)
        elif plan.query_type == "superlative":
            return _execute_superlative(conn, plan)
        elif plan.query_type == "threshold":
            return _execute_threshold(conn, plan)
        elif plan.query_type == "leaderboard":
            return _execute_leaderboard(conn, plan)
        return None
    except Exception as e:
        logger.warning("query_engine_error error=%s plan=%s", e, plan)
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
    return f"{prefix}.{stat.db_column}", stat.display_abbrev, stat.display_name, stat.is_rate


def _table_and_prefix(plan: QueryPlan) -> tuple[str, str]:
    """Pick the right table and alias."""
    if plan.is_pitching:
        return "season_pitching_stats", "sp"
    return "season_batting_stats", "s"


def _build_filters(plan: QueryPlan, prefix: str) -> tuple[str, list]:
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
        from .response_builder import _position_filter
        clauses.append(_position_filter(plan.position, prefix, f"{prefix}.season")[5:])

    if plan.pitcher_role == "starter":
        clauses.append(f"{prefix}.games_started > {prefix}.games / 2")
    elif plan.pitcher_role == "reliever":
        clauses.append(f"{prefix}.games_started <= {prefix}.games / 2")

    if plan.bats:
        clauses.append(f"p.bats = ?")
        params.append(plan.bats)

    if plan.age_max:
        clauses.append(f"({prefix}.season - CAST(SUBSTR(p.birthdate, 1, 4) AS INT)) < ?")
        params.append(plan.age_max)
        clauses.append("p.birthdate IS NOT NULL")

    if plan.age_min:
        clauses.append(f"({prefix}.season - CAST(SUBSTR(p.birthdate, 1, 4) AS INT)) > ?")
        params.append(plan.age_min)
        clauses.append("p.birthdate IS NOT NULL")

    # Extra stat filters: "with under 10 HR", "with 30+ SB"
    for ef in plan.extra_filters:
        clauses.append(f"{prefix}.{ef['stat'].db_column} {ef['comparison']} ?")
        params.append(ef['threshold'])

    return " AND ".join(clauses), params


def _pa_filter(plan: QueryPlan, prefix: str, conn, season: Optional[int] = None) -> str:
    """Get PA/IP minimum filter for rate stats."""
    stat_col = plan.stat.db_column if plan.stat else ""
    is_rate = (plan.stat and plan.stat.is_rate) or (plan.derived_stat and _DERIVED_STATS[plan.derived_stat]["is_rate"])
    if not is_rate:
        return ""

    if plan.is_pitching:
        if season:
            cur = conn.cursor()
            cur.execute(f"SELECT MAX(games) FROM season_pitching_stats WHERE season = ?", (season,))
            r = cur.fetchone()
            max_games = int(r[0]) if r and r[0] else 162
            ip_min = 486 if max_games >= 140 else 243
        else:
            ip_min = 486
        return f" AND {prefix}.ip_outs >= {ip_min}"
    else:
        if season:
            cur = conn.cursor()
            cur.execute(f"SELECT MAX(games) FROM season_batting_stats WHERE season = ?", (season,))
            r = cur.fetchone()
            max_games = int(r[0]) if r and r[0] else 162
            pa_min = 400 if max_games >= 140 else 200
        else:
            pa_min = 400
        return f" AND {prefix}.plate_appearances >= {pa_min}"


# ---------------------------------------------------------------------------
# Executors — one per query type
# ---------------------------------------------------------------------------

def _execute_leaderboard(conn, plan: QueryPlan) -> Optional[str]:
    """Standard stat leaderboard."""
    table, prefix = _table_and_prefix(plan)
    stat_expr, abbrev, name, is_rate = _stat_expr(plan, prefix)
    filters_str, params = _build_filters(plan, prefix)

    # Determine sort
    if plan.sort_asc:
        order = "ASC"
    elif plan.stat and plan.stat.db_column in _LOWER_IS_BETTER:
        order = "ASC"
    else:
        order = "DESC"

    direction_label = "Fewest " if plan.sort_asc and not is_rate else "Worst " if plan.sort_asc else ""
    position_label = ""
    if plan.position:
        from .response_builder import _position_label
        position_label = _position_label(plan.position) + " "
    rookie_label = "Rookie " if plan.rookie else ""
    role_label = "Starting " if plan.pitcher_role == "starter" else "Relief " if plan.pitcher_role == "reliever" else ""
    bats_label = "Left-Handed " if plan.bats == "L" else "Right-Handed " if plan.bats == "R" else "Switch-Hitting " if plan.bats == "B" else ""
    age_label = f"Under {plan.age_max} " if plan.age_max else f"Over {plan.age_min} " if plan.age_min else ""

    if plan.scope.startswith("season_"):
        yr = plan.season or datetime.now().year
        pa = _pa_filter(plan, prefix, conn, yr)
        where = f"WHERE {prefix}.season = ?"
        query_params = [yr] + params
        if filters_str:
            where += f" AND {filters_str}"
        where += pa

        cur = conn.cursor()
        cur.execute(
            f"SELECT p.name, {stat_expr} AS stat_val "
            f"FROM {table} {prefix} "
            f"JOIN players p ON {prefix}.player_id = p.player_id "
            f"{where} "
            f"ORDER BY stat_val {order} LIMIT ?",
            tuple(query_params + [plan.limit]),
        )
        rows = cur.fetchall()
        scope_label = str(yr)

    elif plan.scope == "all_time" or plan.scope.startswith("since_"):
        pa = _pa_filter(plan, prefix, conn)
        where_parts = []
        query_params = list(params)
        if plan.since_year:
            where_parts.append(f"{prefix}.season >= ?")
            query_params.append(plan.since_year)
        if filters_str:
            where_parts.append(filters_str)
        if pa:
            where_parts.append(pa[5:])  # strip " AND "

        where = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""

        cur = conn.cursor()
        cur.execute(
            f"SELECT p.name, {stat_expr} AS stat_val, {prefix}.season "
            f"FROM {table} {prefix} "
            f"JOIN players p ON {prefix}.player_id = p.player_id "
            f"{where} "
            f"ORDER BY stat_val {order} LIMIT ?",
            tuple(query_params + [plan.limit]),
        )
        rows = cur.fetchall()
        scope_label = f"Since {plan.since_year}" if plan.since_year else "All-Time"

    elif plan.scope == "career":
        # Career needs GROUP BY
        if is_rate:
            return None  # Career rate stats need weighted averaging — complex
        pa = ""
        where_parts = []
        query_params = list(params)
        if filters_str:
            where_parts.append(filters_str)
        where = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""

        cur = conn.cursor()
        cur.execute(
            f"SELECT p.name, SUM({stat_expr.replace(f'{prefix}.', '')}) AS stat_val "
            f"FROM {table} {prefix} "
            f"JOIN players p ON {prefix}.player_id = p.player_id "
            f"{where} "
            f"GROUP BY p.player_id "
            f"ORDER BY stat_val {order} LIMIT ?",
            tuple(query_params + [plan.limit]),
        )
        rows = cur.fetchall()
        scope_label = "Career"
    else:
        return None

    if not rows:
        return f"No {name} leaders found."

    # Format
    title_prefix = f"{direction_label}{age_label}{bats_label}{position_label}{rookie_label}{role_label}"
    has_year = plan.scope in ("all_time",) or plan.scope.startswith("since_")
    title = f"**{scope_label} {title_prefix}{name} Leaders**\n" if not has_year else f"**{title_prefix}{name} Leaders ({scope_label})**\n"

    parts = [title]
    parts.append("[TIP]Tap a player name for their full profile.[/TIP]")
    parts.append("[LEADERBOARD]")
    if has_year and len(rows[0]) > 2:
        parts.append(f"HEADER: {abbrev}, Year")
        for i, row in enumerate(rows):
            val = _format_val(plan.stat.db_column if plan.stat else "", row[1], is_rate)
            parts.append(f"ROW {i+1}. {row[0]}: {val}, {row[2]}")
    else:
        parts.append(f"HEADER: {abbrev}")
        for i, row in enumerate(rows):
            val = _format_val(plan.stat.db_column if plan.stat else "", row[1], is_rate)
            parts.append(f"ROW {i+1}. {row[0]}: {val}")
    parts.append("[/LEADERBOARD]")

    if is_rate and pa:
        parts.append(f"\n_Min. qualified._")

    return "\n".join(parts)


def _execute_threshold(conn, plan: QueryPlan) -> Optional[str]:
    """Threshold query: 'who hit 40 HR', 'players with .800 OPS'."""
    table, prefix = _table_and_prefix(plan)
    stat_expr, abbrev, name, is_rate = _stat_expr(plan, prefix)
    filters_str, params = _build_filters(plan, prefix)

    season = plan.season
    if season:
        pa = _pa_filter(plan, prefix, conn, season)
        where = f"WHERE {prefix}.season = ? AND {stat_expr} {plan.comparison} ?"
        query_params = [season, plan.threshold] + params
        if filters_str:
            where += f" AND {filters_str}"
        where += pa
        scope_label = str(season)
    else:
        pa = _pa_filter(plan, prefix, conn)
        where_parts = [f"{stat_expr} {plan.comparison} ?"]
        query_params = [plan.threshold] + params
        if plan.since_year:
            where_parts.append(f"{prefix}.season >= ?")
            query_params.append(plan.since_year)
        if filters_str:
            where_parts.append(filters_str)
        if pa:
            where_parts.append(pa[5:])
        where = f"WHERE {' AND '.join(where_parts)}"
        scope_label = f"Since {plan.since_year}" if plan.since_year else "All-Time"

    cur = conn.cursor()
    if season:
        cur.execute(
            f"SELECT p.name, {stat_expr} AS stat_val "
            f"FROM {table} {prefix} "
            f"JOIN players p ON {prefix}.player_id = p.player_id "
            f"{where} ORDER BY stat_val DESC",
            tuple(query_params),
        )
    else:
        cur.execute(
            f"SELECT p.name, {stat_expr} AS stat_val, {prefix}.season "
            f"FROM {table} {prefix} "
            f"JOIN players p ON {prefix}.player_id = p.player_id "
            f"{where} ORDER BY stat_val DESC",
            tuple(query_params),
        )
    rows = cur.fetchall()

    threshold_display = _format_val("", plan.threshold, is_rate) if is_rate else str(int(plan.threshold))
    rookie_label = "Rookies" if plan.rookie else "Players"
    op = "with" if plan.comparison == ">=" else "with no more than"

    if not rows:
        return f"No {rookie_label.lower()} {op} {threshold_display} {abbrev} in {scope_label}."

    title = f"**{rookie_label} {op} {threshold_display}+ {name} ({scope_label})**"
    count = len(rows)
    parts = [title, f"{count} matched.\n"]
    parts.append("[TIP]Tap a player name for their full profile.[/TIP]")
    parts.append("[LEADERBOARD]")

    has_year = not season
    if has_year and len(rows[0]) > 2:
        parts.append(f"HEADER: Year, {abbrev}")
        for i, row in enumerate(rows):
            val = _format_val(plan.stat.db_column if plan.stat else "", row[1], is_rate)
            parts.append(f"ROW {i+1}. {row[0]}: {row[2]}, {val}")
    else:
        parts.append(f"HEADER: {abbrev}")
        for i, row in enumerate(rows):
            val = _format_val(plan.stat.db_column if plan.stat else "", row[1], is_rate)
            parts.append(f"ROW {i+1}. {row[0]}: {val}")
    parts.append("[/LEADERBOARD]")
    return "\n".join(parts)


def _execute_count(conn, plan: QueryPlan) -> Optional[str]:
    """Count query: 'how many players hit 30 HR in 2025'."""
    table, prefix = _table_and_prefix(plan)
    stat_expr, abbrev, name, is_rate = _stat_expr(plan, prefix)
    filters_str, params = _build_filters(plan, prefix)

    where_parts = [f"{stat_expr} >= ?"]
    query_params = [plan.threshold] + params
    if plan.season:
        where_parts.append(f"{prefix}.season = ?")
        query_params.append(plan.season)
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
    scope = str(plan.season) if plan.season else "all time"
    summary = f"**{count}** players have had {threshold_display}+ {name} in {scope}.\n"

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
    filters_str, params = _build_filters(plan, prefix)

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
    where = f"WHERE {' AND '.join(where_parts)}{birthdate_filter}"

    cur = conn.cursor()
    cur.execute(
        f"SELECT p.name, {stat_expr} AS stat_val, {prefix}.season{age_select} "
        f"FROM {table} {prefix} "
        f"JOIN players p ON {prefix}.player_id = p.player_id "
        f"{where} ORDER BY {order_by} LIMIT 10",
        tuple(query_params),
    )
    rows = cur.fetchall()
    if not rows:
        return f"No player has reached {plan.threshold} {abbrev} in a season."

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
    from .response_builder import build_split_leaderboard
    season = plan.season or datetime.now().year
    return build_split_leaderboard(plan.stat, plan.split_context, season, plan.limit, plan.league)


def _execute_game_log_count(conn, plan: QueryPlan) -> Optional[str]:
    """Game-log counting: 'most multi-hit games', 'most 3-HR games'."""
    if not plan.game_log_stat or not plan.game_log_threshold:
        return None

    table = "game_pitching_logs" if plan.is_pitching else "game_batting_logs"
    col = plan.game_log_stat
    threshold = plan.game_log_threshold

    season_filter = f" AND g.season = ?" if plan.season else ""
    params = [threshold]
    if plan.season:
        params.append(plan.season)

    cur = conn.cursor()
    cur.execute(
        f"SELECT p.name, COUNT(*) AS game_count "
        f"FROM {table} g "
        f"JOIN players p ON g.player_id = p.player_id "
        f"WHERE g.{col} >= ?{season_filter} "
        f"GROUP BY g.player_id "
        f"ORDER BY game_count DESC LIMIT ?",
        tuple(params + [plan.limit]),
    )
    rows = cur.fetchall()
    if not rows:
        return f"No players found."

    scope_label = str(plan.season) if plan.season else "2016-2025"
    stat_name = col.replace("_", " ").title()
    title = f"**Most Games with {threshold}+ {stat_name} ({scope_label})**\n"
    parts = [title]
    parts.append("[TIP]Tap a player name for their full profile.[/TIP]")
    parts.append("[LEADERBOARD]")
    parts.append("HEADER: Games")
    for i, row in enumerate(rows):
        parts.append(f"ROW {i+1}. {row[0]}: {row[1]}")
    parts.append("[/LEADERBOARD]")
    return "\n".join(parts)


def _execute_game_log_extreme(conn, plan: QueryPlan) -> Optional[str]:
    """Per-game extreme: 'most K in one game'."""
    from .response_builder import build_single_game_extreme
    return build_single_game_extreme(plan.stat, plan.season, plan.is_pitching, plan.position)


def _execute_team_ranking(conn, plan: QueryPlan) -> Optional[str]:
    """Team aggregate: 'which team had the most HR'."""
    from .response_builder import build_team_ranking
    if plan.stat:
        season = plan.season or datetime.now().year
        return build_team_ranking(plan.stat, season)
    return None
