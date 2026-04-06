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
    _POSITION_MAP, team_alias_map, _sorted_team_aliases,
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
    end_year: Optional[int] = None  # For decade ranges: "last decade" = 2010-2019

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

    # Sequential game-log queries ("hit in N consecutive games", "reached base in first 10 PAs")
    streak_conditions: list = field(default_factory=list)  # List of SQL conditions per game, ANDed together
    streak_condition_labels: list = field(default_factory=list)  # Display labels for each condition
    streak_length: Optional[int] = None  # N consecutive games
    streak_direction: Optional[str] = None  # "leading" (from start), "sliding" (anywhere), "trailing" (current)

    # Validation
    is_pitching: bool = False
    unexplained_words: list = field(default_factory=list)
    consumed_words: set = field(default_factory=set)

    @property
    def is_valid(self) -> bool:
        """A plan is valid if we have a stat, no unexplained words, and it's a type we handle."""
        if self.query_type in ("definition", "multi_threshold"):
            return False  # Handled by specialized parsers
        if self.query_type == "streak_sequence":
            return len(self.streak_conditions) > 0 and len(self.unexplained_words) == 0
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
        # Strip "top N" so the number doesn't get parsed as a stat threshold
        stat_text = re.sub(r'\btop\s+\d+\b', '', stat_text)
        # Strip game-context phrases so "game" doesn't match as a stat
        for phrase in ["in a game", "in one game", "in a single game"]:
            stat_text = stat_text.replace(phrase, " ")
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

    # "single season" / "in a season" / "in a year" = best single season
    # This OVERRIDES career if both present ("most HR in a season ever")
    single_season_triggers = ["single season", "in a season", "in a year", "of a season"]
    if any(t in lower for t in single_season_triggers):
        plan.scope = "all_time"  # all_time = best single season records
        _add_consumed(plan, "single season in a season in a year")

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
        # Full phrases (check longest first)
        ("left-handed batter", "L"), ("left handed batter", "L"),
        ("left-handed hitter", "L"), ("left handed hitter", "L"),
        ("right-handed batter", "R"), ("right handed batter", "R"),
        ("right-handed hitter", "R"), ("right handed hitter", "R"),
        ("switch hitter", "B"), ("switch-hitter", "B"),
        # Short forms
        ("lefty batter", "L"), ("lefty hitter", "L"), ("lefty", "L"),
        ("righty batter", "R"), ("righty hitter", "R"), ("righty", "R"),
    ]
    for pattern, bats_val in bats_patterns:
        if pattern in lower:
            plan.bats = bats_val
            _add_consumed(plan, pattern)
            break

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
        if any(w in context for w in ["hr", "home run", "homer"]):
            plan.game_log_stat = "home_runs"
        elif "rbi" in context:
            plan.game_log_stat = "rbi"
        elif any(w in context for w in ["strikeout", "k"]):
            plan.game_log_stat = "strikeouts"
        elif "hit" in context:
            plan.game_log_stat = "hits"
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

    # --- Detect sequential streak queries ---
    # Patterns: "hit in N consecutive/straight games", "reached base in first N",
    # "homered in N straight", "when was the last time someone hit in 10 straight"
    # "longest hitting streak"

    # Condition map: what counts as "success" per game
    _streak_conditions = {
        # Reaching base
        "reached base": ("(g.hits + g.walks + COALESCE(g.hit_by_pitch, 0)) >= 1", "reaching base"),
        "got on base": ("(g.hits + g.walks + COALESCE(g.hit_by_pitch, 0)) >= 1", "reaching base"),
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
        "home run": ("g.home_runs >= 1", "a HR"),
        "hr in": ("g.home_runs >= 1", "a HR"),
        # Extra base hits
        "extra base hit": ("(g.doubles + g.triples + g.home_runs) >= 1", "an XBH"),
        "xbh": ("(g.doubles + g.triples + g.home_runs) >= 1", "an XBH"),
        "extra-base hit": ("(g.doubles + g.triples + g.home_runs) >= 1", "an XBH"),
        # RBI
        "drove in a run": ("g.rbi >= 1", "an RBI"),
        "rbi in": ("g.rbi >= 1", "an RBI"),
        "had an rbi": ("g.rbi >= 1", "an RBI"),
        # Stolen bases
        "stole a base": ("g.stolen_bases >= 1", "a SB"),
        "stolen base": ("g.stolen_bases >= 1", "a SB"),
        # Strikeouts (pitching)
        "struck out": ("g.strikeouts >= 1", "a K"),
        # Walks
        "walked": ("g.walks >= 1", "a BB"),
    }

    # Only detect streaks when streak-context words are present
    streak_context_words = ["consecutive", "straight", "in a row", "streak", "streaks",
                            "first", "opening", "current", "longest"]
    has_streak_context = any(w in lower for w in streak_context_words)

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
            if "longest" in lower or "most consecutive" in lower:
                plan.streak_length = None  # means "find the longest"
                plan.streak_direction = "sliding"
                _add_consumed(plan, "longest most consecutive")
            elif "current" in lower:
                plan.streak_length = None
                plan.streak_direction = "trailing"
                _add_consumed(plan, "current")
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
        # Number+letter combined tokens ("200k", "30hr") — split and check parts
        num_letter = re.match(r'^(\d+\.?\d*)([a-z]+)$', w_clean)
        if num_letter:
            letter_part = num_letter.group(2)
            if letter_part in plan.consumed_words or letter_part in _STOP_WORDS or len(letter_part) <= 2:
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


def execute(plan: QueryPlan) -> Optional[str]:
    """Execute a QueryPlan and return formatted response text, or None."""
    if not plan.is_valid:
        return None

    conn = _get_db()
    try:
        if plan.query_type == "streak_sequence":
            return _execute_streak_sequence(conn, plan)
        elif plan.query_type == "game_log_count":
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
    # innings_pitched is TEXT — use ip_outs (integer) for sorting/computation
    if stat.db_column == "innings_pitched":
        return f"{prefix}.ip_outs", stat.display_abbrev, stat.display_name, stat.is_rate
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


def _pa_filter(plan: QueryPlan, prefix: str, conn, season: Optional[int] = None) -> tuple[str, str]:
    """Get PA/IP minimum filter for rate stats, prorated for current season.
    Returns (sql_clause, display_label) tuple."""
    stat_col = plan.stat.db_column if plan.stat else ""
    is_rate = (plan.stat and plan.stat.is_rate) or (plan.derived_stat and _DERIVED_STATS[plan.derived_stat]["is_rate"])
    # Apply PA/IP minimums for rate stats AND for "fewest"/"worst" counting stat queries
    # (otherwise "fewest walks" returns players with 1 PA and 0 BB)
    needs_minimum = is_rate or plan.sort_asc
    if not needs_minimum:
        return "", ""

    if plan.is_pitching:
        # MLB qualification rule: 1.0 IP per team game scheduled
        if season:
            cur = conn.cursor()
            cur.execute(f"SELECT MAX(games) FROM season_pitching_stats WHERE season = ?", (season,))
            r = cur.fetchone()
            max_games = int(r[0]) if r and r[0] else 162
            ip_min = max(1, max_games * 3)  # 1 IP per game = 3 ip_outs per game
        else:
            ip_min = 162 * 3  # full season: 162 IP
        ip_display = f"{ip_min // 3}.{ip_min % 3}"
        return f" AND {prefix}.ip_outs >= {ip_min}", f"Min. {ip_display} IP."
    else:
        if season:
            cur = conn.cursor()
            cur.execute(f"SELECT MAX(games) FROM season_batting_stats WHERE season = ?", (season,))
            r = cur.fetchone()
            max_games = int(r[0]) if r and r[0] else 162
            # Prorate: 400 PA for 162 games
            pa_min = max(1, int(400 * max_games / 162))
        else:
            pa_min = 400
        return f" AND {prefix}.plate_appearances >= {pa_min}", f"Min. {pa_min} PA."


# ---------------------------------------------------------------------------
# Executors — one per query type
# ---------------------------------------------------------------------------

def _execute_leaderboard(conn, plan: QueryPlan) -> Optional[str]:
    """Standard stat leaderboard."""
    table, prefix = _table_and_prefix(plan)
    stat_expr, abbrev, name, is_rate = _stat_expr(plan, prefix)
    filters_str, params = _build_filters(plan, prefix)
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
            f"{where} "
            f"ORDER BY stat_val {order} LIMIT ?",
            tuple(query_params + [plan.limit]),
        )
        rows = cur.fetchall()
        scope_label = str(yr)

    elif plan.scope == "all_time" or plan.scope.startswith("since_"):
        pa, pa_label = _pa_filter(plan, prefix, conn)
        where_parts = []
        query_params = list(params)
        if plan.since_year:
            where_parts.append(f"{prefix}.season >= ?")
            query_params.append(plan.since_year)
        if plan.end_year:
            where_parts.append(f"{prefix}.season <= ?")
            query_params.append(plan.end_year)
        if filters_str:
            where_parts.append(filters_str)
        if pa:
            where_parts.append(pa[5:])  # strip " AND "

        where = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
        if plan.since_year and plan.end_year:
            scope_label = f"{plan.since_year}-{plan.end_year}"
        elif plan.since_year:
            scope_label = f"Since {plan.since_year}"
        else:
            scope_label = "All-Time"

        # For counting stats with "since", aggregate across seasons per player
        if plan.since_year and not is_rate:
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
                f"{where} "
                f"GROUP BY p.player_id "
                f"ORDER BY stat_val {order} LIMIT ?",
                tuple(query_params + [plan.limit]),
            )
        else:
            # Rate stats or all-time: show individual seasons
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

    elif plan.scope == "career":
        # Career needs GROUP BY with aggregate formulas for rate stats
        where_parts = []
        query_params = list(params)
        if filters_str:
            where_parts.append(filters_str)
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
        scope_label = "Career"
    else:
        return None

    if not rows:
        return None  # Fall through to Haiku/Sonnet

    # Format
    title_prefix = f"{direction_label}{age_label}{bats_label}{position_label}{rookie_label}{role_label}"
    # Extra filter label: "with ≤10 HR", "with 30+ SB"
    filter_label = ""
    for ef in plan.extra_filters:
        ef_stat = ef["stat"]
        ef_val = int(ef["threshold"]) if ef["threshold"] == int(ef["threshold"]) else ef["threshold"]
        if ef["comparison"] == "<=":
            filter_label += f" with ≤{ef_val} {ef_stat.display_abbrev}"
        else:
            filter_label += f" with {ef_val}+ {ef_stat.display_abbrev}"
    has_year = plan.scope in ("all_time",) or plan.scope.startswith("since_")
    title = f"**{scope_label} {title_prefix}{name} Leaders{filter_label}**\n" if not has_year else f"**{title_prefix}{name} Leaders{filter_label} ({scope_label})**\n"

    parts = [title]
    parts.append("[TIP]Tap a player name for their full profile.[/TIP]")
    parts.append("[LEADERBOARD]")
    stat_col_key = plan.stat.db_column if plan.stat else (plan.derived_stat or "")
    has_age_col = (plan.age_max or plan.age_min) and not has_year
    n_extra = len(plan.extra_filters)
    extra_headers = [ef["stat"].display_abbrev for ef in plan.extra_filters if ef.get("stat")]

    # Build header
    header_parts = [abbrev]
    if has_year:
        header_parts.append("Year")
    elif has_age_col:
        header_parts.append("Age")
    header_parts.extend(extra_headers)
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
        elif pa_label:
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


def _execute_threshold(conn, plan: QueryPlan) -> Optional[str]:
    """Threshold query: 'who hit 40 HR', 'players with .800 OPS'."""
    table, prefix = _table_and_prefix(plan)
    stat_expr, abbrev, name, is_rate = _stat_expr(plan, prefix)
    filters_str, params = _build_filters(plan, prefix)

    season = plan.season
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

    # Build extra column selects for extra filters
    extra_selects = ", ".join(f"{prefix}.{ef['stat'].db_column}" for ef in plan.extra_filters)
    extra_select_clause = f", {extra_selects}" if extra_selects else ""

    cur = conn.cursor()
    if season:
        cur.execute(
            f"SELECT p.name, {stat_expr} AS stat_val{extra_select_clause} "
            f"FROM {table} {prefix} "
            f"JOIN players p ON {prefix}.player_id = p.player_id "
            f"{where} ORDER BY stat_val DESC",
            tuple(query_params),
        )
    else:
        cur.execute(
            f"SELECT p.name, {stat_expr} AS stat_val, {prefix}.season{extra_select_clause} "
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
        return None

    # Build title with extra filters
    filter_parts = [f"{threshold_display}+ {abbrev}"]
    for ef in plan.extra_filters:
        ef_stat = ef["stat"]
        ef_val = int(ef["threshold"]) if ef["threshold"] == int(ef["threshold"]) else ef["threshold"]
        if ef["comparison"] == "<=":
            filter_parts.append(f"≤{ef_val} {ef_stat.display_abbrev}")
        else:
            filter_parts.append(f"{ef_val}+ {ef_stat.display_abbrev}")
    title = f"**{rookie_label} with {' and '.join(filter_parts)} ({scope_label})**"

    count = len(rows)
    parts = [title, f"{count} matched.\n"]
    parts.append("[TIP]Tap a player name for their full profile.[/TIP]")
    parts.append("[LEADERBOARD]")

    # Build header with extra filter columns
    extra_headers = [ef["stat"].display_abbrev for ef in plan.extra_filters]
    has_year = not season

    if has_year and len(rows[0]) > 2:
        all_headers = ["Year", abbrev] + extra_headers
        parts.append(f"HEADER: {', '.join(all_headers)}")
    else:
        all_headers = [abbrev] + extra_headers
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
    filters_str, params = _build_filters(plan, prefix)

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
        f"{where} ORDER BY {order_by} LIMIT 50",
        tuple(query_params),
    )
    rows = cur.fetchall()
    if not rows:
        return None

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
        return None

    scope_label = str(plan.season) if plan.season else "2016-2025"
    _game_stat_labels = {
        "hits": "Hits", "home_runs": "HR", "rbi": "RBI", "runs": "Runs",
        "stolen_bases": "SB", "walks": "BB", "strikeouts": "K", "doubles": "2B",
        "triples": "3B",
    }
    stat_name = _game_stat_labels.get(col, col.replace("_", " ").title())
    title = f"**Most Games with {threshold}+ {stat_name} ({scope_label})**\n"
    parts = [title]
    parts.append("[TIP]Tap a player name for their full profile.[/TIP]")
    parts.append("[LEADERBOARD]")
    parts.append("HEADER: Games")
    for i, row in enumerate(rows):
        parts.append(f"ROW {i+1}. {row[0]}: {row[1]}")
    parts.append("[/LEADERBOARD]")

    # Suggestion pills for game-log counts
    if col == "strikeouts" and not plan.is_pitching:
        parts.append(f"\n[SUGGEST]most {threshold}+ K games by a pitcher[/SUGGEST]")
    elif col == "strikeouts" and plan.is_pitching:
        parts.append(f"\n[SUGGEST]most {threshold}+ K games by a hitter[/SUGGEST]")
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
    pitching_cols = {"g.ip_outs", "g.earned_runs", "g.is_start"}
    is_pitching = plan.is_pitching or any(pc in c for c in plan.streak_conditions for pc in pitching_cols)
    table = "game_pitching_logs" if is_pitching else "game_batting_logs"

    # Build season filter
    season_filter = ""
    season_params = []
    if plan.season:
        season_filter = " AND g.season = ?"
        season_params = [plan.season]
    elif plan.since_year:
        season_filter = " AND g.season >= ?"
        season_params = [plan.since_year]

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
        return None

    label = " + ".join(plan.streak_condition_labels) if plan.streak_condition_labels else "success"
    target_length = plan.streak_length

    if plan.streak_direction == "leading":
        return _streak_leading(rows, target_length, label, plan)
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
        if target_length:
            return None  # Fall through
        return None

    # Sort by streak length descending
    results.sort(key=lambda x: x[0], reverse=True)

    # Title
    scope = str(plan.season) if plan.season else f"Since {plan.since_year}" if plan.since_year else "2016-2025"
    if target_length:
        title = f"**Players with {label} in {target_length}+ Consecutive Games ({scope})**\n"
    else:
        title = f"**Longest Streaks of {label} ({scope})**\n"

    parts = [title]
    parts.append("[TIP]Tap a player name for their full profile.[/TIP]")
    parts.append("[LEADERBOARD]")
    parts.append("HEADER: Games, Year")
    for i, (streak_len, name, season) in enumerate(results[:50]):
        parts.append(f"ROW {i+1}. {name}: {streak_len}, {season}")
    parts.append("[/LEADERBOARD]")

    return "\n".join(parts)


def _streak_leading(rows, target_length, label, plan) -> Optional[str]:
    """Find players who achieved the condition in their first N games of a season."""
    from collections import defaultdict
    player_seasons = defaultdict(list)
    player_names = {}
    for pid, name, season, date, success in rows:
        player_seasons[(pid, season)].append(success)
        player_names[pid] = name

    if not target_length:
        target_length = 10  # default

    results = []
    for (pid, season), games in player_seasons.items():
        if len(games) < target_length:
            continue
        first_n = games[:target_length]
        if all(g == 1 for g in first_n):
            results.append((player_names[pid], season, target_length))

    if not results:
        scope = str(plan.season) if plan.season else "all seasons"
        return None  # Fall through to Haiku/Sonnet

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
        return None

    results.sort(key=lambda x: x[0], reverse=True)

    title = f"**Current Active Streaks of {label}**\n"
    parts = [title]
    parts.append("[TIP]Tap a player name for their full profile.[/TIP]")
    parts.append("[LEADERBOARD]")
    parts.append("HEADER: Games")
    for i, (streak_len, name, season) in enumerate(results[:50]):
        parts.append(f"ROW {i+1}. {name}: {streak_len}")
    parts.append("[/LEADERBOARD]")

    return "\n".join(parts)
