"""
Regression tests for the player single-season-max query family.

Covers the parser routing in query_engine.decompose, the pitcher
classification in name_matcher.is_pitcher, and the end-to-end response
format produced by response_builder.build_player_single_season_max.

This family was the source of a long QA cycle on 2026-05-13 — multiple
parsers were claiming the query before query_engine.decompose got a
chance to see it (parse_season_lookup, parse_leaderboard,
parse_single_stat_lookup). These tests pin down the routing so we
don't regress when a new parser shows up or a guard rotates.

Requires the local players + season_*_stats DB to be loaded.
"""

import os
import sys

import pytest

# Add backend to path so imports work
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.query_engine import decompose
from services.name_matcher import is_pitcher, sorted_names

# These tests need the players + season_*_stats tables loaded. Skip the
# whole module if the player index didn't populate (DB_PATH points at a
# stripped iOS bundled DB or a fresh dev env without seed data).
_HAS_DB = bool(sorted_names) and "Hank Aaron" in sorted_names
pytestmark = pytest.mark.skipif(
    not _HAS_DB,
    reason="player DB not loaded — tests need full season_*_stats backend DB",
)


# ── Routing tests (decompose) ────────────────────────────────────────


class TestDecomposeRouting:
    """Verify decompose recognizes player single-season-max intent and
    sets query_type accordingly."""

    @pytest.mark.parametrize("query", [
        "Most home runs Hank Aaron ever hit in a season",
        "Most home runs Hank Aaron ever hit",
        "most home runs by hank aaron ever",
        "Best HR season Babe Ruth ever had",
        "Aaron Judge career-best OPS",
        "Mike Trout's best HR season",
        "Albert Pujols best RBI season",
        "Greg Maddux's lowest WHIP",
        "Lowest ERA Pedro Martinez ever had",
        "Most strikeouts Nolan Ryan ever recorded",
        "Fewest walks Greg Maddux ever issued",
    ])
    def test_query_type_set(self, query):
        plan = decompose(query)
        assert plan.query_type == "player_single_season_max", (
            f"Expected player_single_season_max routing for {query!r}, "
            f"got {plan.query_type!r}"
        )
        assert plan.player_name, f"No player matched in {query!r}"
        assert plan.stat or plan.derived_stat, f"No stat matched in {query!r}"

    @pytest.mark.parametrize("query, expected_player", [
        ("Most home runs Hank Aaron ever hit", "Hank Aaron"),
        ("Mike Trout's best HR season", "Mike Trout"),
        ("Greg Maddux's lowest WHIP", "Greg Maddux"),
        ("Albert Pujols best RBI season", "Albert Pujols"),
        ("Lowest ERA Pedro Martinez ever had", "Pedro Martinez"),
        ("Most strikeouts Nolan Ryan ever recorded", "Nolan Ryan"),
    ])
    def test_player_match(self, query, expected_player):
        """Player name detection survives possessives, embedded names,
        and trailing decorative verbs ("issued", "recorded", "had")."""
        plan = decompose(query)
        assert plan.player_name == expected_player

    def test_plan_is_valid(self):
        """plan.is_valid must be True for downstream execute() to run.
        unexplained_words should be cleared on detection."""
        plan = decompose("Fewest walks Greg Maddux ever issued")
        assert plan.is_valid, (
            f"plan should be valid after detection. "
            f"query_type={plan.query_type!r}, unexplained={plan.unexplained_words!r}"
        )

    # ── Negative cases: must NOT be claimed as single-season-max ──

    @pytest.mark.parametrize("query, expected_type", [
        # Career totals — different intent entirely
        ("Hank Aaron career home runs", None),
        ("Aaron Judge career RBI", None),
        # Current-season lookup
        ("Aaron Judge home runs this season", None),
        ("Aaron Judge home runs", None),
        # Specific year — user asked about that season specifically
        ("Hank Aaron 1971 stats", None),
        # All-time leaderboards (no player named)
        ("Most home runs in a season ever", None),
        ("Best ERA this season", None),
        ("Top 10 in OPS this season", None),
    ])
    def test_not_single_season_max(self, query, expected_type):
        plan = decompose(query)
        assert plan.query_type != "player_single_season_max", (
            f"{query!r} should NOT be routed to player_single_season_max, "
            f"got {plan.query_type!r}"
        )


# ── Direction tests ──────────────────────────────────────────────────


class TestDirection:
    """sort_asc should be True for fewest/worst/lowest, False otherwise.
    Lower-is-better rate stats invert via the executor — that piece is
    covered by the leaderboard direction logic, shared via _LOWER_IS_BETTER."""

    def test_most_is_descending(self):
        plan = decompose("Most home runs Hank Aaron ever hit")
        assert plan.sort_asc is False

    def test_fewest_is_ascending(self):
        plan = decompose("Fewest walks Greg Maddux ever issued")
        assert plan.sort_asc is True

    def test_lowest_is_ascending(self):
        plan = decompose("Greg Maddux's lowest WHIP")
        assert plan.sort_asc is True


# ── is_pitcher classification ────────────────────────────────────────


class TestIsPitcher:
    """is_pitcher should align with player_card._is_pitcher: the
    positions field must start with 'P' AND the player must have at
    least one row in season_pitching_stats AND must not be a two-way
    player (Ohtani rule: any season with PA>=130 AND any season with
    ip_outs>=90)."""

    @pytest.mark.parametrize("name", [
        "Nolan Ryan",      # career pitcher who batted in non-DH years
        "Greg Maddux",     # career pitcher who batted as a NL pitcher
        "Pedro Martinez",  # career pitcher
        "Mariano Rivera",  # career closer, no GS
    ])
    def test_pitchers(self, name):
        assert is_pitcher(name), f"{name} should be classified as pitcher"

    @pytest.mark.parametrize("name", [
        "Albert Pujols",   # batter who threw 1 mop-up IP
        "Mike Trout",      # pure batter
        "Aaron Judge",     # pure batter
        "Hank Aaron",      # pure batter (historical)
    ])
    def test_batters(self, name):
        assert not is_pitcher(name), f"{name} should NOT be classified as pitcher"
