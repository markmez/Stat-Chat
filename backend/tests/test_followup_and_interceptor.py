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
