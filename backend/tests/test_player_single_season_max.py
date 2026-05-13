"""
Regression tests for the player single-season-max query family.

Hits the live /query endpoint and verifies the response matches expected
single-season-max output (sentence form: "Name had his most|fewest|lowest
[stat] in a single season in {year}: {value} ({team})").

This family caused a long QA cycle on 2026-05-13 — multiple parsers
were claiming queries before query_engine.decompose got a chance to see
them (parse_season_lookup, parse_leaderboard, parse_single_stat_lookup).
These tests pin down the end-to-end behavior so we don't regress.

Run: cd backend && python -m pytest tests/test_player_single_season_max.py -v

Or via direct script:    cd backend && python tests/test_player_single_season_max.py
"""

import os
import re
import sys

import pytest
import requests


API_URL = os.getenv("STATCHAT_API_URL", "https://api.secondsignalapps.com") + "/query"
TIMEOUT = 30


def _ask(question: str) -> str:
    """POST the question to /query and return the first text-chunk body
    (the SSE stream's first text event — the entire interceptor response
    arrives in one chunk for single-shot intercepts)."""
    r = requests.post(
        API_URL,
        json={"question": question, "device_id": "pytest-regression"},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    # Find the first "type": "text" SSE event and return its text body.
    for line in r.text.splitlines():
        if line.startswith("data: ") and '"type": "text"' in line:
            # Extract the "text" field via a tolerant regex (the body
            # itself contains JSON-escaped newlines and quotes).
            m = re.search(r'"text":\s*"(.*)"\s*}', line)
            if m:
                return m.group(1)
    return r.text


# ── Player single-season-max — positive cases ──────────────────────


class TestPlayerSingleSeasonMax:
    """Each query should return the player's career-best single season for
    the named stat — sentence form with year and team."""

    @pytest.mark.parametrize("query, expected_player, expected_value, expected_year", [
        # Original bug — "ever hit in a season"
        ("Most home runs Hank Aaron ever hit in a season",
         "Hank Aaron", "47", "1971"),
        # No "in a season" — bare "ever"
        ("Most home runs Hank Aaron ever hit",
         "Hank Aaron", "47", "1971"),
        # Different sentence order
        ("most home runs by hank aaron ever",
         "Hank Aaron", "47", "1971"),
        # "Best [stat] season" pattern with stat in between
        ("Best HR season Babe Ruth ever had",
         "Babe Ruth", "60", "1927"),
        ("Mike Trout's best HR season",
         "Mike Trout", "45", "2019"),
        ("Albert Pujols best RBI season",
         "Albert Pujols", "137", "2006"),
        # Pitcher routing — ambiguous stat (strikeouts) routed correctly
        ("Most strikeouts Nolan Ryan ever recorded",
         "Nolan Ryan", "383", "1973"),
        # Pitcher routing — fewest walks (issued) routes to pitching table
        ("Fewest walks Greg Maddux ever issued",
         "Greg Maddux", "11", "1986"),
    ])
    def test_counting_stat(self, query, expected_player, expected_value, expected_year):
        body = _ask(query)
        assert expected_player in body, f"player {expected_player!r} missing in: {body[:200]}"
        assert expected_value in body, f"value {expected_value!r} missing in: {body[:200]}"
        assert expected_year in body, f"year {expected_year!r} missing in: {body[:200]}"

    @pytest.mark.parametrize("query, expected_player, expected_year, value_pattern", [
        # Rate stat — WHIP (lower is better; "lowest" → MIN)
        ("Greg Maddux's lowest WHIP",
         "Greg Maddux", "1995", r"\.810|0\.81"),
        # Rate stat — ERA (lower is better; "lowest" → MIN)
        ("Lowest ERA Pedro Martinez ever had",
         "Pedro Martinez", "2000", r"1\.74"),
    ])
    def test_rate_stat(self, query, expected_player, expected_year, value_pattern):
        body = _ask(query)
        assert expected_player in body
        assert expected_year in body
        assert re.search(value_pattern, body), f"value pattern {value_pattern!r} not in: {body[:200]}"


# ── Negative cases — must NOT be hijacked into single-season-max ──


class TestNotHijacked:
    """Queries that share keywords with the single-season-max family but
    have different intent. Each must produce its proper, non-hijacked
    response."""

    def test_career_total_preserved(self):
        body = _ask("Hank Aaron career home runs")
        # Career path returns "755 career home runs"
        assert "755" in body
        assert "career" in body.lower()

    def test_current_season_lookup_preserved(self):
        body = _ask("Aaron Judge home runs this season")
        # Current season — 2026 stats for Judge. Should contain "2026".
        assert "Aaron Judge" in body
        assert "2026" in body

    def test_specific_year_preserved(self):
        body = _ask("Hank Aaron 1971 stats")
        # Specific-year lookup. Should mention 1971 (or 1976 if season_lookup
        # falls back), but NOT the player-single-season-max sentence shape.
        assert "Hank Aaron" in body
        # The sentence shape "had his most ... in a single season" is the
        # single-season-max signature. If it appears here, we hijacked.
        assert "in a single season" not in body, "Hijacked specific-year query into single-season-max"

    def test_all_time_leaderboard_preserved(self):
        body = _ask("Most home runs in a single season ever")
        # No player named → all-time HR single-season leaderboard.
        # Should be a leaderboard with multiple ROWs, not a single-player sentence.
        assert "Barry Bonds" in body or "ROW 1" in body, "Expected all-time leaderboard"

    def test_top_leaderboard_preserved(self):
        body = _ask("Top 10 in OPS this season")
        assert "ROW 1" in body or "[LEADERBOARD]" in body


if __name__ == "__main__":
    # Standalone runner — prints pass/fail per assertion when run directly
    # without pytest. Useful for quick verification or piping into CI.
    import traceback

    tests: list[tuple[str, callable]] = []
    for cls in (TestPlayerSingleSeasonMax, TestNotHijacked):
        inst = cls()
        for attr in dir(inst):
            if attr.startswith("test_"):
                fn = getattr(inst, attr)
                # pytest.mark.parametrize stuffs the cases in pytestmark
                marks = getattr(fn, "pytestmark", [])
                params = next((m for m in marks if m.name == "parametrize"), None)
                if params:
                    argnames = params.args[0].split(", ")
                    for case in params.args[1]:
                        if not isinstance(case, tuple):
                            case = (case,)
                        kwargs = dict(zip(argnames, case))
                        label = f"{cls.__name__}.{attr}[{kwargs}]"
                        tests.append((label, lambda f=fn, k=kwargs: f(**k)))
                else:
                    tests.append((f"{cls.__name__}.{attr}", fn))

    passed = failed = 0
    for label, fn in tests:
        try:
            fn()
            print(f"PASS {label}")
            passed += 1
        except Exception as e:
            print(f"FAIL {label}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed of {len(tests)} total")
    sys.exit(0 if failed == 0 else 1)
