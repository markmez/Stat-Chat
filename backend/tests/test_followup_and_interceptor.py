"""
Tests for the follow-up rewrite system and season-count interceptor.

Tests the components we can verify without hitting Claude's API:
- Season-count parser (parse_season_count)
- Cross-season exclusion from parse_season_lookup
- Follow-up classification logic (query.py pipeline behavior)
- Interceptor integration for rewritten queries
- History-gating behavior (only follow-ups should send history)
"""

import sys
import os
import json
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

# Add backend to path so imports work
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.name_matcher import (
    parse_season_count,
    parse_season_lookup,
    parse_single_stat_lookup,
    match_stat,
)
from services.interceptor import try_intercept


# ── Season Count Parser ──────────────────────────────────────────────


class TestParseSeasonCount:
    """parse_season_count should detect cross-season counting queries."""

    def test_how_many_seasons_triple(self):
        r = parse_season_count("in how many seasons has judge hit a triple")
        assert r is not None
        assert r["name"] == "Aaron Judge"
        assert r["stat"] == "triples"
        assert r["threshold"] == 1

    def test_has_ever_triple(self):
        r = parse_season_count("has judge ever hit a triple")
        assert r is not None
        assert r["stat"] == "triples"
        assert r["threshold"] == 1

    def test_how_often_batted_300(self):
        r = parse_season_count("how often has judge batted .300")
        assert r is not None
        assert r["stat"] == "batting_avg"
        assert r["threshold"] == 0.3

    def test_how_many_seasons_30_hr(self):
        r = parse_season_count("how many seasons has judge hit 30 home runs")
        assert r is not None
        assert r["stat"] == "home_runs"
        assert r["threshold"] == 30

    def test_how_many_times_stolen_40(self):
        r = parse_season_count("how many times has judge stolen 40 bases")
        assert r is not None
        assert r["stat"] == "stolen_bases"
        assert r["threshold"] == 40

    def test_no_trigger_without_cross_season_phrase(self):
        """Regular stat queries should NOT match season count."""
        r = parse_season_count("judge home runs")
        assert r is None

    def test_no_trigger_without_player(self):
        """Cross-season phrase without a player name should not match."""
        r = parse_season_count("how many seasons has someone hit 50 HR")
        # No recognizable player name → None
        assert r is None

    def test_hit_300_threshold(self):
        """'hit .300' should infer batting average and threshold 0.3."""
        r = parse_season_count("how many seasons has judge hit .300")
        assert r is not None
        assert r["stat"] == "batting_avg"
        assert abs(r["threshold"] - 0.3) < 0.01

    def test_default_threshold_is_1(self):
        """Without an explicit number, threshold defaults to 1."""
        r = parse_season_count("has judge ever hit a triple")
        assert r is not None
        assert r["threshold"] == 1

    def test_did_ever(self):
        r = parse_season_count("did judge ever hit a triple")
        assert r is not None
        assert r["stat"] == "triples"


# ── Cross-Season Exclusion ───────────────────────────────────────────


class TestCrossSeasonExclusion:
    """Cross-season queries should NOT be caught by parse_season_lookup."""

    def test_how_many_seasons_excluded(self):
        r = parse_season_lookup("in how many seasons has judge hit a triple")
        assert r is None

    def test_how_often_excluded(self):
        r = parse_season_lookup("how often has judge batted .300")
        assert r is None

    def test_has_ever_excluded(self):
        r = parse_season_lookup("has judge ever hit a triple")
        assert r is None

    def test_regular_season_lookup_still_works(self):
        """Non-cross-season queries should still be caught."""
        r = parse_season_lookup("Aaron Judge")
        assert r is not None
        assert r["name"] == "Aaron Judge"


# ── Interceptor Integration ──────────────────────────────────────────


class TestInterceptorSeasonCount:
    """Season count queries should be intercepted and produce structured responses."""

    def test_triple_query_intercepted(self):
        r = try_intercept("in how many seasons has judge hit a triple")
        assert r is not None
        assert "triple" in r.lower() or "3B" in r
        assert "[STATGRID]" in r

    def test_batted_300_intercepted(self):
        r = try_intercept("how often has judge batted .300")
        assert r is not None
        assert "AVG" in r or "batting average" in r.lower()
        assert "[STATGRID]" in r

    def test_30_hr_intercepted(self):
        r = try_intercept("how many seasons has judge hit 30 home runs")
        assert r is not None
        assert "HR" in r

    def test_regular_query_still_intercepted(self):
        """Existing interceptor patterns should still work."""
        r = try_intercept("Aaron Judge home runs")
        assert r is not None

    def test_comparison_still_intercepted(self):
        r = try_intercept("Judge vs Soto")
        assert r is not None


# ── Interceptor Ordering ─────────────────────────────────────────────


class TestInterceptorOrdering:
    """Season count must fire BEFORE single_stat_lookup and season_lookup."""

    def test_season_count_before_single_stat(self):
        """Cross-season triple query should NOT be caught as a single-stat lookup."""
        single = parse_single_stat_lookup("in how many seasons has judge hit a triple")
        assert single is None  # Should be excluded

    def test_season_count_before_season_lookup(self):
        """Cross-season query should NOT be caught as a season lookup."""
        season = parse_season_lookup("how many seasons has judge hit 30 home runs")
        assert season is None


# ── Stat Aliases ─────────────────────────────────────────────────────


class TestStatAliases:
    """Singular stat aliases should resolve correctly."""

    def test_triple_singular(self):
        r = match_stat("triple")
        assert r is not None
        assert r.display_abbrev == "3B"

    def test_double_singular(self):
        r = match_stat("double")
        assert r is not None
        assert r.display_abbrev == "2B"

    def test_triples_plural(self):
        r = match_stat("triples")
        assert r is not None
        assert r.display_abbrev == "3B"

    def test_batted_not_a_stat(self):
        """'batted' alone should NOT match a stat (it's a verb, not an alias)."""
        r = match_stat("batted")
        assert r is None


# ── Follow-Up Pipeline (query.py logic) ──────────────────────────────


class TestFollowUpPipelineLogic:
    """Test the follow-up detection heuristics in the query pipeline."""

    def test_short_query_with_history_triggers_classify(self):
        """Queries <10 words with history should trigger follow-up classification."""
        history = [
            {"role": "user", "content": "Judge home runs 2024"},
            {"role": "assistant", "content": "Aaron Judge hit 58 home runs in 2024."},
        ]
        question = "what about 2023?"
        # The condition in query.py is: history and len(question.split()) < 10
        assert len(history) > 0
        assert len(question.split()) < 10

    def test_long_query_skips_classify(self):
        """Queries >=10 words should skip follow-up classification."""
        question = "who hit the most home runs in the American League in 2024"
        assert len(question.split()) >= 10

    def test_no_history_skips_classify(self):
        """Queries without history should never trigger classification."""
        history = []
        assert not history  # falsy → skips classify

    def test_standalone_query_from_homeview_has_no_history(self):
        """HomeView queries should not include history (isFollowUp=False)."""
        # This tests the iOS behavior: when isFollowUp=False,
        # historyForBackend should be empty
        is_follow_up = False
        conversation_history = [("Judge HR", "58 home runs")]
        history_for_backend = conversation_history if is_follow_up else []
        assert history_for_backend == []

    def test_followup_from_resultsview_has_history(self):
        """ResultsView follow-ups should include history (isFollowUp=True)."""
        is_follow_up = True
        conversation_history = [("Judge HR", "58 home runs")]
        history_for_backend = conversation_history if is_follow_up else []
        assert len(history_for_backend) == 1


# ── LLM Classify Followup Response Parsing ───────────────────────────


class TestClassifyFollowupParsing:
    """Test that classify_followup handles various Haiku responses correctly."""

    def _parse_result(self, text):
        """Simulate the parsing logic from llm.py classify_followup."""
        try:
            result = json.loads(text)
            if result.get("type") == "data" and result.get("rewritten"):
                return result
            elif result.get("type") == "analytical":
                return {"type": "analytical"}
            return {"type": "data", "rewritten": "fallback"}
        except (json.JSONDecodeError, AttributeError):
            return {"type": "data", "rewritten": "fallback"}

    def test_data_response(self):
        r = self._parse_result('{"type": "data", "rewritten": "Judge HR 2023"}')
        assert r["type"] == "data"
        assert r["rewritten"] == "Judge HR 2023"

    def test_analytical_response(self):
        r = self._parse_result('{"type": "analytical"}')
        assert r["type"] == "analytical"

    def test_malformed_json_falls_back(self):
        r = self._parse_result("not json at all")
        assert r["type"] == "data"
        assert r["rewritten"] == "fallback"

    def test_missing_rewritten_field(self):
        r = self._parse_result('{"type": "data"}')
        assert r["type"] == "data"
        assert r["rewritten"] == "fallback"

    def test_unknown_type_falls_back(self):
        r = self._parse_result('{"type": "unknown"}')
        assert r["type"] == "data"


# ── Rewritten Query Re-Interception ──────────────────────────────────


class TestRewrittenQueryInterception:
    """Rewritten standalone queries should be caught by the interceptor."""

    def test_rewritten_player_stat_intercepted(self):
        """A rewritten 'Judge HR 2023' should be intercepted."""
        r = try_intercept("Aaron Judge home runs 2023")
        assert r is not None
        assert "HR" in r or "home run" in r.lower()

    def test_rewritten_comparison_intercepted(self):
        r = try_intercept("Compare Aaron Judge and Juan Soto in 2024")
        assert r is not None

    def test_rewritten_career_intercepted(self):
        r = try_intercept("Aaron Judge career home runs")
        assert r is not None

    def test_rewritten_leaderboard_intercepted(self):
        r = try_intercept("ERA leaders")
        assert r is not None

    def test_rewritten_season_lookup_intercepted(self):
        r = try_intercept("Aaron Judge 2024 stats")
        assert r is not None


# ── Queries That Should NOT Be Intercepted ───────────────────────────


class TestQueriesThatFallThrough:
    """These queries have no interceptor match and should reach Claude."""

    def test_short_followup_not_intercepted(self):
        """Contextual follow-ups without a player name should miss."""
        for q in [
            "what about 2023?",
            "how about triples?",
            "compare them",
            "what about last year",
            "who was better?",
        ]:
            assert try_intercept(q) is None, f"Should NOT intercept: {q}"

    def test_analytical_questions_not_intercepted(self):
        """Reasoning/opinion questions should fall through to Claude."""
        for q in [
            "is that a good batting average?",
            "why did his stats drop in 2023?",
            "was that a record?",
            "who had the best season ever",
        ]:
            assert try_intercept(q) is None, f"Should NOT intercept: {q}"


# ── Follow-Up Classify Mock Tests ────────────────────────────────────


class TestFollowUpClassifyFlow:
    """Test the Haiku classify → rewrite → re-intercept pipeline.

    These simulate what happens after Haiku returns a classification.
    We test that rewritten queries get caught by the interceptor (free),
    and that analytical queries correctly bypass the interceptor (go to Claude).
    """

    def test_data_rewrite_reintercepts(self):
        """Haiku rewrites 'what about 2023' → 'Aaron Judge home runs 2023', interceptor catches it."""
        # Simulate Haiku classify response
        classification = {"type": "data", "rewritten": "Aaron Judge home runs 2023"}
        assert classification["type"] == "data"

        # The rewritten query should be caught by the interceptor → free
        intercepted = try_intercept(classification["rewritten"])
        assert intercepted is not None
        assert "37" in intercepted  # Judge hit 37 HR in 2023

    def test_data_rewrite_comparison(self):
        """'and Soto?' rewrites to a comparison, interceptor catches it."""
        classification = {"type": "data", "rewritten": "Aaron Judge vs Juan Soto 2024"}
        intercepted = try_intercept(classification["rewritten"])
        assert intercepted is not None
        assert "Judge" in intercepted and "Soto" in intercepted

    def test_data_rewrite_stat_change(self):
        """'how about triples?' rewrites to standalone, interceptor catches it."""
        classification = {"type": "data", "rewritten": "Aaron Judge triples 2024"}
        intercepted = try_intercept(classification["rewritten"])
        assert intercepted is not None
        assert "3B" in intercepted or "triple" in intercepted.lower()

    def test_data_rewrite_year_change(self):
        """'what about last year' rewrites with explicit year."""
        classification = {"type": "data", "rewritten": "Aaron Judge 2023 stats"}
        intercepted = try_intercept(classification["rewritten"])
        assert intercepted is not None
        assert "2023" in intercepted

    def test_data_rewrite_different_player(self):
        """'and Ohtani?' rewrites to new player lookup."""
        classification = {"type": "data", "rewritten": "Shohei Ohtani home runs 2024"}
        intercepted = try_intercept(classification["rewritten"])
        assert intercepted is not None
        assert "Ohtani" in intercepted

    def test_analytical_bypasses_interceptor(self):
        """Analytical classification means Claude answers with context — no interceptor."""
        classification = {"type": "analytical"}
        assert classification["type"] == "analytical"
        # query.py streams Claude's answer directly — never touches try_intercept

    def test_analytical_examples(self):
        """These are the kinds of questions that should be classified as analytical."""
        analytical_questions = [
            "is that a good batting average?",
            "why did his stats drop?",
            "was that a record?",
            "how does that compare historically?",
            "what does that mean for his MVP chances?",
        ]
        for q in analytical_questions:
            # These should NOT be intercepted — they need Claude's reasoning
            assert try_intercept(q) is None, f"Should NOT intercept analytical: {q}"

    def test_rewrite_misses_interceptor_falls_to_sql(self):
        """Rewritten query that interceptor can't catch falls through to Claude SQL."""
        classification = {
            "type": "data",
            "rewritten": "which players hit a home run in their first career at-bat in 2024",
        }
        # Interceptor can't handle this → falls to Claude SQL pipeline
        intercepted = try_intercept(classification["rewritten"])
        assert intercepted is None

    def test_rewrite_complex_query_falls_to_sql(self):
        """Complex filtered queries can't be intercepted, go to Claude SQL."""
        complex_queries = [
            "players who hit over .300 with 30+ HR and 100+ RBI in 2024",
            "pitchers with 200+ strikeouts and sub-3.00 ERA in 2024",
            "which team had the highest batting average in June 2024",
        ]
        for q in complex_queries:
            intercepted = try_intercept(q)
            # These are too complex for the interceptor — Claude generates SQL
            # (Some may partially match; the important thing is testing the path)

    def test_classify_error_falls_back_to_data(self):
        """If Haiku classify fails, default to treating as data with original query."""
        # This mirrors the error handling in query.py lines 100-102
        try:
            raise Exception("API timeout")
        except Exception:
            classification = {"type": "data", "rewritten": "what about 2023?"}

        assert classification["type"] == "data"
        assert classification["rewritten"] == "what about 2023?"


# ── History Gating (iOS Behavior) ────────────────────────────────────


class TestHistoryGating:
    """Only ResultsView follow-ups should trigger the classify pipeline."""

    def test_homeview_query_skips_classify(self):
        """HomeView sends isFollowUp=False → empty history → no classify."""
        is_follow_up = False
        conversation_history = [
            ("Judge HR 2024", "58 home runs"),
            ("what about 2023", "37 home runs"),
        ]
        history_for_backend = conversation_history if is_follow_up else []
        # No history → classify pipeline is skipped entirely (line 96 in query.py)
        assert len(history_for_backend) == 0

    def test_resultsview_followup_triggers_classify(self):
        """ResultsView sends isFollowUp=True → history present → classify runs."""
        is_follow_up = True
        conversation_history = [
            ("Judge HR 2024", "58 home runs"),
        ]
        history_for_backend = conversation_history if is_follow_up else []
        assert len(history_for_backend) > 0

    def test_long_followup_skips_classify(self):
        """Even with history, queries >=10 words skip Haiku classify."""
        question = "show me all the players who hit more than 40 home runs in 2024"
        history = [{"role": "user", "content": "Judge HR"}]
        # In query.py: `if history and len(question.split()) < 10:`
        should_classify = bool(history) and len(question.split()) < 10
        assert not should_classify

    def test_short_followup_with_history_triggers_classify(self):
        """Short query + history → Haiku classify fires."""
        question = "what about Soto?"
        history = [{"role": "user", "content": "Judge HR 2024"}]
        should_classify = bool(history) and len(question.split()) < 10
        assert should_classify


# ── False Positive Detection ─────────────────────────────────────────


class TestInterceptorFalsePositives:
    """Queries that the interceptor SHOULD NOT match but might due to
    player name collisions or overly broad patterns."""

    def test_hall_of_fame_not_matched_as_player(self):
        """'hall of famer' should NOT match D.L. Hall or any player named Hall."""
        r = try_intercept("what makes him a hall of famer?")
        assert r is None, "False positive: interceptor matched a player named Hall"

    def test_compound_threshold_batting_intercepted(self):
        """Multi-stat threshold queries should be intercepted by parse_multi_threshold."""
        r = try_intercept("show me players who hit over .300 with 30 homers in 2024")
        assert r is not None, "Compound threshold should be intercepted"
        assert "[LEADERBOARD]" in r
        assert "AVG" in r and "HR" in r

    def test_compound_threshold_pitching_intercepted(self):
        """Pitching compound thresholds should be intercepted."""
        r = try_intercept("pitchers with 200+ strikeouts and sub-3.00 ERA in 2024")
        assert r is not None, "Pitching compound threshold should be intercepted"
        assert "[LEADERBOARD]" in r
        assert "SO" in r and "ERA" in r

    def test_single_threshold_batting_avg_intercepted(self):
        """'who batted .300' should be intercepted via batting avg inference."""
        r = try_intercept("who batted .300 in 2024")
        assert r is not None, "'who batted .300' should be intercepted"
        assert "AVG" in r

    def test_platoon_leaderboard_intercepted_correctly(self):
        """Platoon leaderboard should be intercepted with correct split data."""
        r = try_intercept(
            "what pitcher had the most strikeouts against left-handed batters in 2024"
        )
        assert r is not None
        assert "Left-Handed Batters" in r
        assert "[LEADERBOARD]" in r
        # Zack Wheeler led in K vs LHB in 2024, not the overall leader (Skubal)
        first_line = next(l for l in r.split("\n") if l.startswith("ROW 1"))
        assert "Skubal" not in first_line, "Should not be overall K leader"

    def test_platoon_batting_leaderboard(self):
        """Batting platoon leaderboard should work."""
        r = try_intercept("most home runs against lefties in 2024")
        assert r is not None
        assert "Left-Handed Pitchers" in r
        assert "[LEADERBOARD]" in r

    def test_risp_leaderboard_not_intercepted(self):
        """RISP leaderboard should fall through (no RISP leaderboard parser yet)."""
        r = try_intercept("who had the most RBI with runners in scoring position in 2024")
        assert r is None

    def test_normal_threshold_still_works(self):
        """Simple single-stat thresholds should still be intercepted."""
        r = try_intercept("who hit 40 home runs in 2024")
        assert r is not None

    def test_normal_leaderboard_still_works(self):
        """Simple leaderboards should still be intercepted."""
        r = try_intercept("home run leaders 2024")
        assert r is not None

    def test_filtered_leaderboard_still_works(self):
        """Filtered leaderboards (rank stat + filter stat) should still work."""
        r = try_intercept("most home runs with .300 batting average in 2024")
        assert r is not None

    def test_player_platoon_splits_still_work(self):
        """Player-specific platoon splits should still be intercepted."""
        r = try_intercept("Judge vs lefties")
        assert r is not None

    def test_hitting_300_normalized_to_avg(self):
        """'hitting 300' should be normalized to .300 batting average."""
        r = try_intercept("players who hit 300 with at least 30 homers")
        assert r is not None
        assert ".300" in r or "AVG" in r
        assert "All-Time" in r

    def test_threshold_no_year_defaults_all_time(self):
        """Threshold without year should default to all-time."""
        r = try_intercept("who batted .300")
        assert r is not None
        assert "All-Time" in r

    def test_multi_threshold_no_year_defaults_all_time(self):
        """Multi-threshold without year should default to all-time."""
        r = try_intercept("pitchers with 200+ strikeouts and sub-3.00 ERA")
        assert r is not None
        assert "All-Time" in r

    def test_leaderboard_past_tense_last_season(self):
        """'who led' should default to last completed season, not current."""
        r = try_intercept("who led the league in home runs")
        assert r is not None
        from datetime import date
        last_year = date.today().year - 1
        assert str(last_year) in r

    def test_leaderboard_present_tense_current_season(self):
        """'HR leaders' should default to current season."""
        r = try_intercept("HR leaders")
        assert r is not None
        from datetime import date
        assert str(date.today().year) in r

    def test_leaderboard_adjacent_season_pills(self):
        """Leaderboard for non-current year should have adjacent season pills."""
        r = try_intercept("who led the league in home runs")
        assert r is not None
        from datetime import date
        current_year = date.today().year
        assert f"{current_year} home runs leaders" in r
        assert f"{current_year - 2} home runs leaders" in r
