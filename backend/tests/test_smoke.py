"""
Smoke test — fast regression suite for trivial changes.

25 canonical cases covering each major query routing path. ~70s
runtime against live /query. Most queries are 1-2s; a couple of the
slower paths (narrative/AI-routed, multi-stat threshold) push the
average up.

PURPOSE: catch "did anything obvious break?" before pushing trivial
changes (regex tweaks, copy edits, prompt nudges, small parser
adjustments). For NON-trivial changes (new query type, parser added,
decompose logic change), run audit_routing.py — the 106-case deep suite.

Each case asserts:
  - Required substrings in the response body
  - Routing: did the interceptor (zero-cost path) handle it, or did
    it fall to Haiku/Sonnet? Detected via the [AIDISCLAIMER] tag,
    which is only added for AI-answered responses.

Coverage shape (25 cases):
  - 17 one-case-per-path baseline (career, specific year, current
    season, single-season max, leaderboard, comparison, splits,
    streak, current form, stat def, team, milestone, etc.)
  - 4 high-risk additions in areas where real bugs surfaced (possessive
    single-season max, ambiguous-stat pitcher routing, multi-stat
    threshold, negative routing — a narrative query that should go to
    Sonnet)
  - 2 phrasing variants (paranoia for accidental regex over-tightening)
  - 2 explicit negatives (current-season and career-total queries must
    NOT be hijacked into single-season-max)

Run: cd backend && python -m pytest tests/test_smoke.py -v
Or:  cd backend && python tests/test_smoke.py
"""

import os
import re
import sys
from typing import Optional

import pytest
import requests


API_URL = os.getenv("STATCHAT_API_URL", "https://api.secondsignalapps.com") + "/query"
TIMEOUT = 30


def _ask(question: str) -> tuple[str, bool]:
    """POST to /query, return (response_body, was_intercepted).

    was_intercepted is True when the interceptor (zero-cost path)
    handled the query — i.e., no [AIDISCLAIMER] tag in the response.
    Haiku and Sonnet responses always include the AI disclaimer.
    """
    r = requests.post(
        API_URL,
        json={"question": question, "device_id": "pytest-smoke"},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    text_chunks: list[str] = []
    for line in r.text.splitlines():
        if line.startswith("data: ") and '"type": "text"' in line:
            m = re.search(r'"text":\s*"(.*)"\s*}', line)
            if m:
                text_chunks.append(m.group(1))
    body = "\n".join(text_chunks)
    was_intercepted = "[AIDISCLAIMER]" not in body
    return body, was_intercepted


# Each case: (query, list_of_required_substrings_or_regexes, expected_intercepted)
#   - Substrings starting with "re:" are regex patterns, others are literal.
#   - expected_intercepted: True (interceptor handled), False (AI handled),
#     or None to skip the routing check.

SMOKE_CASES = [
    # ── Baseline: one case per major routing path ──────────────────

    # Career lookups
    ("Aaron Judge career home runs",
     ["Aaron Judge", "career", "re:\\d{3}"], True),
    ("Pedro Martinez career ERA",
     ["Pedro Martinez", "career", "re:\\d+\\.\\d{2}"], True),

    # Specific year (historical → stable)
    ("Aaron Judge 2022",
     ["Aaron Judge", "2022", "62"], True),

    # Current season — assert structure, not exact value (changes daily)
    ("Aaron Judge home runs",
     ["Aaron Judge", "2026", "re:\\d+\\s+home runs"], True),

    # Single-season max — recent bug area (Tier 2b in audit_routing)
    ("Most home runs Hank Aaron ever hit",
     ["Hank Aaron", "47", "1971"], True),

    # All-time leaderboards
    ("Most HR in a single season",
     ["Barry Bonds", "73"], True),

    # Filtered leaderboards — current season (structure assertion)
    ("Top 10 in OPS this season",
     ["LEADERBOARD", "OPS"], True),

    # Comparison
    ("Compare Aaron Judge and Juan Soto",
     ["Aaron Judge", "Juan Soto"], True),

    # Slash line
    ("Aaron Judge slash line",
     ["Aaron Judge", "AVG", "OBP", "SLG"], True),

    # Platoon split
    ("Aaron Judge vs lefties",
     ["Aaron Judge", "re:[Ll]eft|LHP"], True),

    # RISP split
    ("Aaron Judge with RISP",
     ["Aaron Judge", "re:RISP|scoring position"], True),

    # Home/away split
    ("Aaron Judge at home",
     ["Aaron Judge", "re:[Hh]ome"], True),

    # Streak history
    ("Aaron Judge hot streaks",
     ["Aaron Judge", "re:[Ss]treak|stretch"], True),

    # Current form
    ("How is Aaron Judge doing lately",
     ["Aaron Judge"], True),

    # Stat definition (zero-cost path)
    ("what is OPS",
     ["OPS"], True),

    # Team query
    ("Yankees record",
     ["Yankees", "re:\\d+[-–]\\d+|\\d+ wins"], True),

    # Milestone threshold
    ("Players with 3000 hits",
     ["re:Aaron|Cobb|Rose|Wagner"], True),

    # ── High-risk additions (real bugs found today) ────────────────

    # Single-season max — possessive (broke today, now fixed)
    ("Greg Maddux's lowest WHIP",
     ["Greg Maddux", "1995", "re:\\.810|0\\.81"], True),

    # Single-season max — ambiguous stat for pitcher (broke today)
    ("Most strikeouts Nolan Ryan ever recorded",
     ["Nolan Ryan", "383", "1973"], True),

    # Multi-stat composite threshold
    ("Players with .300 and 30 HR last season",
     ["re:[A-Z][a-z]+ [A-Z][a-z]+"], True),  # at least one player name in result

    # Negative routing — narrative / opinion query SHOULD go to Sonnet
    ("Why was 1968 the year of the pitcher",
     [], False),

    # ── Phrasing variants ────────────────────────────────────────

    # Current form alt phrasing
    ("How is Juan Soto doing recently",
     ["Juan Soto"], True),

    # Relative year reference
    ("Aaron Judge home runs last year",
     ["Aaron Judge", "2025"], True),

    # ── Explicit negative cases (must NOT be hijacked) ────────────

    # Career total — NOT single-season max
    ("Hank Aaron career home runs",
     ["Hank Aaron", "755", "career"], True),

    # Current season lookup — NOT single-season max
    ("Aaron Judge home runs this season",
     ["Aaron Judge", "2026"], True),
]


@pytest.mark.parametrize("query, must_contain, expected_intercepted", SMOKE_CASES)
def test_smoke(query, must_contain, expected_intercepted):
    body, was_intercepted = _ask(query)

    # Routing check
    if expected_intercepted is not None:
        assert was_intercepted == expected_intercepted, (
            f"Routing regression for {query!r}: expected "
            f"{'intercepted' if expected_intercepted else 'AI-answered'}, "
            f"got {'intercepted' if was_intercepted else 'AI-answered'}. "
            f"Response head: {body[:200]}"
        )

    # Content check
    for needle in must_contain:
        if needle.startswith("re:"):
            pattern = needle[3:]
            assert re.search(pattern, body), (
                f"Pattern {pattern!r} not in response for {query!r}: {body[:200]}"
            )
        else:
            assert needle in body, (
                f"{needle!r} missing from response for {query!r}: {body[:200]}"
            )


if __name__ == "__main__":
    # Standalone runner for direct execution
    passed = failed = 0
    failures: list[str] = []
    for query, must_contain, expected_intercepted in SMOKE_CASES:
        try:
            body, was_intercepted = _ask(query)
            if expected_intercepted is not None and was_intercepted != expected_intercepted:
                raise AssertionError(
                    f"routing: expected {expected_intercepted}, got {was_intercepted}"
                )
            for needle in must_contain:
                if needle.startswith("re:"):
                    pattern = needle[3:]
                    if not re.search(pattern, body):
                        raise AssertionError(f"missing pattern {pattern!r}")
                elif needle not in body:
                    raise AssertionError(f"missing {needle!r}")
            print(f"PASS {query}")
            passed += 1
        except Exception as e:
            print(f"FAIL {query}: {type(e).__name__}: {e}")
            failures.append(f"{query} → {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed of {len(SMOKE_CASES)} total")
    if failures:
        print("\nFailures:")
        for f in failures:
            print(f"  - {f}")
    sys.exit(0 if failed == 0 else 1)
