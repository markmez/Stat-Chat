"""
Routing audit — runs a comprehensive set of canonical queries against
prod and classifies which path handled each: interceptor / query_engine,
Haiku SQL, or Sonnet sql_planner. Compares actual routing to expected,
flags drift.

Usage:
    python3 backend/tests/audit_routing.py [--deep] [--filter SUBSTR] [--no-throttle]

Default mode (shallow) uses /admin/debug-intercept which is fast and only
tells us "did query_engine intercept yes/no" — sufficient for most
regressions. Pass --deep to also hit /query for misses to see final
routing (Haiku vs Sonnet). --deep is much slower and costs real money.

Throttling: 1.5s sleep between requests by default to avoid hammering
prod. Pass --no-throttle for testing locally against a dev backend.

Early exit: if 3+ consecutive timeouts/errors, audit stops to avoid
piling on a struggling backend.

Add new test cases by appending to TESTS below.
"""

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request

PROD = "https://api.secondsignalapps.com"
ADMIN_KEY = "I9-NNJ-GBen3SZ-wf8JkZX5-_zvvt8Qri2EtTxWUo-I"

THROTTLE_SECONDS = 1.5
MAX_CONSECUTIVE_ERRORS = 5  # tolerate a few slow queries before assuming meltdown
DEEP_TIMEOUT = 90
SHALLOW_TIMEOUT = 60  # some career-scope window queries legitimately take 20-30s

# Expected routing values — strict binary, no hand-waving:
#   "intercepted"  — interceptor parser OR query_engine handled the query.
#                     This is the ONLY accepted result for any query that
#                     can be answered structurally from the DB. Falling to
#                     Haiku or Sonnet here is a regression to fix.
#   "sonnet"       — Sonnet sql_planner / insight engine handled. Reserve
#                     ONLY for genuinely narrative / off-topic / out-of-
#                     schema queries (e.g. "explain FIP vs xERA" when those
#                     metrics aren't in our DB). If you find yourself
#                     reaching for "sonnet" because "well, query engine
#                     can't really do that" — it's actually a query engine
#                     gap. Add a parser instead of weakening the test.
#   "haiku"        — kept as a deep-mode bucket for diagnostic reporting,
#                     but no test SHOULD expect it (Haiku is a fallback,
#                     not a target).

TESTS: list[tuple[str, str, str]] = [
    # ============================================================
    # Tier 1: Foundational career milestones — MUST intercept
    # ============================================================
    ("Players with 3000 hits", "intercepted", "Foundational milestone"),
    ("Players with 500 home runs", "intercepted", "Foundational milestone"),
    ("Players with 300 wins", "intercepted", "Foundational milestone"),
    ("Players with 3000 strikeouts", "intercepted", "Foundational milestone"),
    ("Players with 500 stolen bases", "intercepted", "Foundational milestone"),
    ("Pitchers with 200 wins all time", "intercepted", "Foundational milestone"),
    ("Career 3000 strikeout pitchers", "intercepted", "Foundational milestone"),

    # ============================================================
    # Tier 2: All-time single-season records
    # ============================================================
    ("Most HR in a single season", "intercepted", "All-time single-season"),
    ("Most hits in a single season", "intercepted", "All-time single-season"),
    ("Most stolen bases in a single season", "intercepted", "All-time single-season"),
    ("Most RBI in a season ever", "intercepted", "All-time single-season"),
    ("Most strikeouts by a pitcher in a season", "intercepted", "All-time single-season"),

    # ============================================================
    # Tier 2b: Player single-season max (career-best single-season).
    # Same shape as Tier 2 but with a named player — the answer is THIS
    # player's career-best single season, not the all-time leaderboard.
    # Added 2026-05-13 after a long QA cycle uncovered three parsers
    # (parse_season_lookup, parse_leaderboard, parse_single_stat_lookup)
    # all claiming these queries before query_engine.decompose got them.
    # ============================================================
    ("Most home runs Hank Aaron ever hit in a season", "intercepted", "Player single-season max"),
    ("Most home runs Hank Aaron ever hit", "intercepted", "Player single-season max"),
    ("most home runs by hank aaron ever", "intercepted", "Player single-season max"),
    ("Best HR season Babe Ruth ever had", "intercepted", "Player single-season max"),
    ("Mike Trout's best HR season", "intercepted", "Player single-season max"),
    ("Albert Pujols best RBI season", "intercepted", "Player single-season max"),
    ("Greg Maddux's lowest WHIP", "intercepted", "Player single-season max"),
    ("Lowest ERA Pedro Martinez ever had", "intercepted", "Player single-season max"),
    ("Most strikeouts Nolan Ryan ever recorded", "intercepted", "Player single-season max"),
    ("Fewest walks Greg Maddux ever issued", "intercepted", "Player single-season max"),

    # Tier 2c: Sliding-window stretch (best/worst N-consecutive-game window).
    # Counting stats only; requires an explicit N. Singular "rbi" is the
    # regression guard — "game" in the window phrase used to hijack stat=games.
    ("most hits Judge in a 5 game stretch", "intercepted", "Player sliding-window"),
    ("most rbi Volpe in a 3 game stretch", "intercepted", "Player sliding-window"),
    ("Skubal most strikeouts in a 3 game stretch", "intercepted", "Player sliding-window"),

    # Tier 2d: Player vs a specific team (opponent split from game logs).
    # "Judge" is ambiguous, so this also guards parse_team_stats from grabbing
    # the query and returning the opponent's roster instead.
    ("Judge vs the Red Sox", "intercepted", "Player vs team split"),
    ("Soto vs the Dodgers 2025", "intercepted", "Player vs team split"),
    # Full team name must route to vs-team, NOT a player-vs-player comparison
    # ("Boston" resolves to a player) — guards the parse order vs parse_comparison.
    ("Aaron Judge vs the Boston Red Sox", "intercepted", "Player vs team split"),

    # Tier 2e: Guard precision — the central _claim choke point must pin BOTH
    # directions. Over-guard protection: legit queries with an extra-but-
    # accountable word still intercept (a too-aggressive guard would silently
    # strangle them). Leak guard: an unsupported trailing qualifier ("on
    # Tuesdays" — no day/night data) must bail, not return a partial answer.
    ("Aaron Judge vs lefties in 2024", "intercepted", "Guard: legit qualified"),
    ("Aaron Judge 10 game hitting streak", "intercepted", "Guard: legit qualified"),
    ("how is Aaron Judge doing this year", "intercepted", "Guard: legit qualified"),
    ("Aaron Judge at home on Tuesdays", "miss", "Guard: junk qualifier bails"),
    ("most home runs Aaron Judge ever on Tuesdays", "miss", "Guard: junk qualifier bails"),
    ("most hits Aaron Judge in a 5 game stretch on Tuesdays", "miss", "Guard: junk qualifier bails"),
    ("Aaron Judge vs the Red Sox on Tuesdays", "miss", "Guard: junk qualifier bails"),

    # Tier 2f: "best/worst stretch|span" family — moved off the Claude raw-dump
    # path. No N → PELT streak handler (hot/cold, distinguished, career scope);
    # explicit N + no counting stat → rate window (OPS hitters / ERA pitchers).
    ("judge's best stretch this year", "intercepted", "Stretch: streak (no N)"),
    ("worst stretch of judge's career", "intercepted", "Stretch: streak (no N)"),
    ("judge's worst 13 game span", "intercepted", "Stretch: N-game rate window"),
    ("Tarik Skubal best 5 game stretch", "intercepted", "Stretch: N-game rate window"),

    # Tier 2g: stretch-family follow-ups (all moved off Claude).
    # Rate windows beyond OPS/ERA:
    ("Aaron Judge best 10 game AVG stretch", "intercepted", "Stretch: N-game rate window"),
    ("Tarik Skubal best 5 game WHIP stretch", "intercepted", "Stretch: N-game rate window"),
    # Colloquial streak terms:
    ("Aaron Judge skid this year", "intercepted", "Streak: colloquial"),
    ("Aaron Judge on a tear this year", "intercepted", "Streak: colloquial"),
    # Best/worst calendar month for a player:
    ("Aaron Judge best month this year", "intercepted", "Best month"),
    ("Aaron Judge worst month this year", "intercepted", "Best month"),
    # Multi-player leaderboard sliding window (no specific player):
    ("who had the best 10 game stretch this year", "intercepted", "Leaderboard window"),
    ("most home runs in a 10 game stretch this year", "intercepted", "Leaderboard window"),
    # current_form regression guard — "doing"/"playing" once tripped the
    # _strip_consumed substring-corruption bug ("in" inside "doing").
    ("how is Aaron Judge playing lately", "intercepted", "Current form"),

    # ============================================================
    # Tier 3: Single-player career + season + slash line
    # ============================================================
    ("Aaron Judge career stats", "intercepted", "Single-player career"),
    ("Aaron Judge career home runs", "intercepted", "Single-player career"),
    ("Mike Trout career", "intercepted", "Single-player career"),
    ("Pedro Martinez career ERA", "intercepted", "Single-player career"),
    ("Aaron Judge 2024", "intercepted", "Single-player season"),
    ("Mike Trout 2017", "intercepted", "Single-player season"),
    ("Pedro Martinez 2000", "intercepted", "Single-player season"),
    ("Aaron Judge slash line 2024", "intercepted", "Slash line"),

    # ============================================================
    # Tier 4: Standard leaderboards
    # ============================================================
    ("Most HR this year", "intercepted", "Standard leaderboard"),
    ("Best ERA in 2024", "intercepted", "Standard leaderboard"),
    ("Most strikeouts in 2023", "intercepted", "Standard leaderboard"),
    ("Highest OPS in 2024", "intercepted", "Standard leaderboard"),
    ("Most saves in 2023", "intercepted", "Standard leaderboard"),

    # ============================================================
    # Tier 5: Filtered leaderboards (handedness, position, age, role)
    # ============================================================
    ("Most HR by a left-handed batter since 2020", "intercepted", "Filtered: handedness"),
    ("Most wins by a left-handed pitcher since 2020", "intercepted", "Filtered: handedness"),
    ("Best batting average by a pitcher all time", "intercepted", "Filtered: position-as-pitcher"),
    ("Most HR by a catcher in 2024", "intercepted", "Filtered: position"),
    ("Best ERA by a starter in 2024", "intercepted", "Filtered: pitcher role"),
    ("Most saves by a closer in 2023", "intercepted", "Filtered: pitcher role"),
    ("Most HR by a player under 25 in 2024", "intercepted", "Filtered: age"),
    ("Best ERA by a pitcher over 35", "intercepted", "Filtered: age"),
    ("Best rookie ERA all time", "intercepted", "Filtered: rookie"),
    ("Most HR by a rookie all time", "intercepted", "Filtered: rookie"),

    # ============================================================
    # Tier 6: Multi-condition / compound thresholds
    # ============================================================
    ("Players with 30 HR and 30 SB", "intercepted", "Multi-condition season"),
    ("Players who batted .300 with 30 HR", "intercepted", "Multi-condition season"),
    ("Pitchers with 200 K and sub-3 ERA", "intercepted", "Multi-condition season"),
    ("Players with 3000 hits and 500 HR", "intercepted", "Multi-condition career"),
    ("Pitchers with 300 wins and 3000 K", "intercepted", "Multi-condition career"),

    # ============================================================
    # Tier 7: Date-range / closed window
    # ============================================================
    ("Most RBI in April last year", "intercepted", "Date range: bare month"),
    ("Most HR in May 2024", "intercepted", "Date range: bare month"),
    # Use May — current month as of audit creation (May 2026). "June this
    # year" was failing because June 2026 hasn't happened yet → empty
    # leaderboard → fallthrough. Future-empty is legitimate but the audit
    # should exercise a populated month. If the current month is past May,
    # update this to a populated month (the bare-month-range parser is
    # what we're really testing).
    ("Best ERA in May this year", "intercepted", "Date range: bare month"),
    ("Most HR since June 1 2024", "intercepted", "Date range: since date"),
    ("Best ERA in the first half of a season since 2020", "intercepted", "Half-window"),
    ("Most HR in the second half of 2024", "intercepted", "Half-window"),
    ("Most HR in a single month all time", "intercepted", "Month-grouped"),

    # ============================================================
    # Tier 8: Recent stretch / current form
    # ============================================================
    ("Most HR in the last 30 days", "intercepted", "Recent stretch"),
    ("Best ERA in the last 30 days", "intercepted", "Recent stretch"),
    ("How is Aaron Judge doing lately", "intercepted", "Current form"),
    ("Aaron Judge current form", "intercepted", "Current form"),

    # ============================================================
    # Tier 9: Career window (cumulative + per-game-each + variants)
    # ============================================================
    ("Players who hit .300 in their first 50 games this season",
     "intercepted", "Career-window cumulative"),
    ("Players with 10 HR in their first 50 games of 2024",
     "intercepted", "Career-window cumulative"),
    ("Best OPS over their last 100 games", "intercepted",
     "Career-window cumulative recent"),
    ("Best OPS in their first 50 career games", "intercepted",
     "Career-window cumulative debut"),
    ("Pitchers with 8+ K in each of their first 2 starts of 2026",
     "intercepted", "Per-game-each first"),
    ("Pitchers with 8+ K in each of their last 3 starts of 2026",
     "intercepted", "Per-game-each tail"),
    ("Hitters with 2+ HR in each of their first 5 games of 2024",
     "intercepted", "Per-game-each first"),
    ("Pitchers with 8+ K in each of their first 3 career starts",
     "intercepted", "Per-game-each career-debut"),
    # PA / AB window queries — Phase 1 refuse + redirect (NO_COUNT). The
    # interceptor returns a deterministic "we don't have at-bat granularity"
    # message with a game-equivalent SUGGEST. Counts as intercepted (the
    # response IS structurally generated locally, even though it's a refusal).
    ("Highest batting avg in first 50 PA of a career",
     "intercepted", "PA/AB window refuse"),
    ("Most HR in first 100 plate appearances of a career",
     "intercepted", "PA/AB window refuse"),
    ("Best OBP through first 30 ABs of his career",
     "intercepted", "PA/AB window refuse"),

    # ============================================================
    # Tier 10: Splits (single-player + leaderboard)
    # ============================================================
    ("Judge vs lefties this year", "intercepted", "Split: single-player platoon"),
    ("Aaron Judge home and away 2024", "intercepted", "Split: single-player home/away"),
    ("Pedro Martinez vs righties 2000", "intercepted", "Split: single-player platoon"),
    ("Aaron Judge with RISP this year", "intercepted", "Split: single-player RISP"),
    ("Most HR vs righties this year", "intercepted", "Split: leaderboard platoon"),
    # NOTE: "Best ERA vs LHB" is a real schema gap — pitching_platoon_splits
    # has _against rate stats and counting stats but NO earned_runs / ip_outs
    # per split, so ERA-per-platoon is structurally not computable. Audit
    # uses OPS-allowed instead which IS in the table.
    ("Best OPS allowed vs LHB this year", "intercepted", "Split: leaderboard platoon"),
    ("Best ERA in the 7th inning", "intercepted", "Split: inning leaderboard"),
    # Inning splits exist only for pitchers (pitching_inning_splits) and only
    # for 2026. A batter + a season without data never tested the feature — it
    # just fell to season_lookup and got a full-season line. Use a real one.
    ("Tarik Skubal in the 1st inning 2026", "intercepted", "Split: single-player inning"),
    # Stage 2 pitching split leaderboards (count, RISP, home/away, pitch type).
    # Each one routes to its *_pitching_splits table via explicit "pitchers"
    # prefix or "throwing X" phrasing for pitch type.
    ("Best pitchers with 2 strikes", "intercepted", "Split: leaderboard count pitching"),
    ("Best pitchers with RISP", "intercepted", "Split: leaderboard RISP pitching"),
    ("Best pitchers at home this year", "intercepted", "Split: leaderboard home/away pitching"),
    ("Best pitchers throwing sliders", "intercepted", "Split: leaderboard pitch-type pitching"),
    # "against X" / "vs X" naturally read as hitter phrasing but when the
    # subject is "pitchers" we pivot to pitch_type_pitching_splits.
    ("Best pitchers against sliders", "intercepted", "Split: leaderboard pitch-type pitching"),
    ("Best pitchers vs changeups", "intercepted", "Split: leaderboard pitch-type pitching"),
    ("Worst pitchers against fastballs", "intercepted", "Split: leaderboard pitch-type pitching"),
    ("Best pitchers ahead in the count", "intercepted", "Split: leaderboard count pitching"),

    # ============================================================
    # Tier 11: Streaks
    # ============================================================
    ("Longest hitting streak in 2024", "intercepted", "Streak: longest"),
    ("Longest active hitting streak", "intercepted", "Streak: active"),
    ("20-game hitting streaks in 2024", "intercepted", "Streak: threshold"),
    ("Pitchers with 10+ K in 3 consecutive starts of 2024", "intercepted",
     "Streak: consecutive threshold"),

    # ============================================================
    # Tier 12: Comparisons, matchups, year-over-year
    # ============================================================
    ("Judge vs Soto", "intercepted", "Comparison: player vs player"),
    ("Compare Maddux and Glavine", "intercepted", "Comparison: career"),
    ("Mookie Betts 2018 vs 2019", "intercepted", "Year-over-year"),
    ("Aaron Judge 2022 vs 2024", "intercepted", "Year-over-year"),
    ("Judge vs Skubal", "intercepted", "Matchup: batter vs pitcher"),
    ("How will Soto do against Cole", "intercepted", "Matchup: batter vs pitcher"),

    # ============================================================
    # Tier 13: Game-log / per-game queries
    # ============================================================
    ("Aaron Judge game logs", "intercepted", "Game logs"),
    ("Most 4-hit games in 2024", "intercepted", "Game-level extreme"),
    ("Most multi-homer games by a player in 2024", "intercepted",
     "Game-level extreme"),

    # ============================================================
    # Tier 14: Stat definitions
    # ============================================================
    ("What is OPS?", "intercepted", "Stat definition"),
    ("Define WHIP", "intercepted", "Stat definition"),
    ("Explain BABIP", "intercepted", "Stat definition"),

    # ============================================================
    # Tier 15: League / division filter
    # ============================================================
    ("Best ERA in the AL 2024", "intercepted", "League filter"),
    ("Most HR in the NL 2023", "intercepted", "League filter"),

    # ============================================================
    # Tier 15b: Award-filtered
    # ============================================================
    ("MVP winners with 50+ HR", "intercepted", "Award filter"),
    ("Cy Young winners with sub-2 ERA", "intercepted", "Award filter"),
    ("Most HR by a Hall of Famer", "intercepted", "Award filter"),

    # ============================================================
    # Tier 15c: Decade / cross-season range
    # ============================================================
    ("Most HR in the 2010s", "intercepted", "Decade range"),
    ("Best ERA in the 2000s", "intercepted", "Decade range"),
    ("Most wins in the last decade", "intercepted", "Decade range"),

    # ============================================================
    # Tier 16: Team
    # ============================================================
    ("Most wins by a team in 2024", "intercepted", "Team ranking"),
    ("Best team ERA in 2024", "intercepted", "Team ranking"),
    ("HR leader on each team this year", "intercepted", "Per-team leader"),

    # ============================================================
    # Tier 16b: Team conditional record — aggregates team_game_results
    # under a per-game condition (team_runs, opp_runs, is_home). Added
    # 2026-06-04 after a "best record when scoring 5+ runs" query fell
    # through to Sonnet. See query_engine._detect_team_conditional_record.
    # ============================================================
    ("What team has the best record when scoring 5 runs or more?", "intercepted", "Team conditional record (score >=)"),
    ("Best record when scoring at least 7 runs", "intercepted", "Team conditional record (score >=)"),
    ("Worst record when scoring 3 or fewer runs", "intercepted", "Team conditional record (score <=)"),
    ("Best record when allowing 2 or fewer runs", "intercepted", "Team conditional record (allow <=)"),
    ("Best record allowing 6+ runs", "intercepted", "Team conditional record (allow >=)"),
    ("Best home record this year", "intercepted", "Team conditional record (home)"),
    ("Worst road record", "intercepted", "Team conditional record (road)"),

    # ============================================================
    # Tier 16c: Player career filtered — team or year-range filter.
    # New handler (2026-06-05). Aggregates season_stats with team include/
    # exclude and/or season start/end filter. See decompose._detect_player_
    # career_filter. Mid-season trade years (combined "OAK/NYA" rows) noted
    # via caveat in the response, never silently miscounted.
    # ============================================================
    ("Sonny Gray career ERA excluding Yankees", "intercepted", "Player career filtered (team exclude)"),
    ("Aaron Judge career OPS as a Yankee", "intercepted", "Player career filtered (team include)"),
    ("Pujols career HR with the Cardinals", "intercepted", "Player career filtered (team include)"),
    ("Verlander career ERA after 2018", "intercepted", "Player career filtered (after year)"),
    ("Trout career OPS through 2019", "intercepted", "Player career filtered (through year)"),
    ("Pujols career HR before 2010", "intercepted", "Player career filtered (before year)"),
    ("Trout career HR from 2012 to 2019", "intercepted", "Player career filtered (year range)"),

    # ============================================================
    # Tier 17: Narrative — Sonnet expected
    # ============================================================
    ("Why is Skenes considered a generational pitcher", "sonnet", "Narrative"),
    ("Tell me about Roberto Clemente", "sonnet", "Narrative"),
    ("Explain why teams are throwing more sliders", "sonnet", "Narrative"),

    # ============================================================
    # ============================================================
    # Tier 18b: Off-topic / metric comparisons not in schema — Sonnet expected
    # FIP and xERA aren't in our DB; this is a genuine narrative
    # explanation, no structural answer possible.
    # ============================================================
    ("What's the difference between FIP and xERA", "sonnet",
     "Off-topic / metric comparison"),
]


def http_post(path: str, body: dict, timeout: int = DEEP_TIMEOUT) -> dict:
    """POST JSON, return parsed response. Streaming responses join all data."""
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{PROD}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        text = r.read().decode("utf-8", errors="replace")
        if "data: " in text:
            return {"_stream": text}
        return json.loads(text)


_ZERO_RESULT_MARKERS = (
    "0 ", "No players", "No game streaks", "No active",
    "No qualifying", "No qualified", "No matching",
)


def _is_zero_result(preview: str) -> bool:
    """Heuristic: detect 'query engine ran, found 0 rows' from result preview.
    Looks at the first ~60 chars where titles/zero-sentences live."""
    if not preview:
        return False
    head = preview.lstrip("*# \n").lower()
    return any(head.startswith(m.lower()) for m in _ZERO_RESULT_MARKERS)


def classify_intercept_only(query: str) -> tuple[str, int, str, bool]:
    """Hit /admin/debug-intercept. Returns (route, latency_ms, snippet, is_zero).
    route is "intercepted" or "miss" or "error". is_zero is True when
    intercepted but the response was a deterministic "0 matched" answer."""
    q_enc = urllib.parse.quote(query)
    url = f"{PROD}/admin/debug-intercept?key={ADMIN_KEY}&q={q_enc}"
    t0 = time.time()
    try:
        with urllib.request.urlopen(url, timeout=SHALLOW_TIMEOUT) as r:
            body = json.loads(r.read().decode("utf-8", errors="replace"))
    except Exception as e:
        return ("error", int((time.time() - t0) * 1000), f"err: {e}", False)
    ms = int((time.time() - t0) * 1000)
    if body.get("error"):
        return ("error", ms, f"err: {body['error'][:120]}", False)
    if body.get("intercepted"):
        preview = body.get("result_preview") or ""
        snip = preview.replace("\n", " ")[:90]
        return ("intercepted", ms, snip, _is_zero_result(preview))
    return ("miss", ms, "", False)


def classify_deep(query: str) -> tuple[str, int, str, bool]:
    """Hit /query streaming. Returns (route, latency_ms, snippet).
    route is "intercepted" / "haiku" / "sonnet" / "error"."""
    body = {
        "question": query,
        "device_id": "audit-routing-script-001",
        "input_method": "text",
        "no_count": True,
    }
    t0 = time.time()
    try:
        resp = http_post("/query", body, timeout=DEEP_TIMEOUT)
    except Exception as e:
        return ("error", int((time.time() - t0) * 1000), f"err: {e}")
    ms = int((time.time() - t0) * 1000)
    text = resp.get("_stream", "")
    route = "unknown"
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        try:
            evt = json.loads(line[5:].strip())
        except Exception:
            continue
        if evt.get("type") == "done":
            if evt.get("intercepted"):
                route = "intercepted"
            elif evt.get("haiku_sql"):
                route = "haiku"
            elif evt.get("insight"):
                route = "sonnet"
            break
    snip = ""
    for line in text.splitlines():
        if line.startswith("data:"):
            try:
                evt = json.loads(line[5:].strip())
                if evt.get("type") == "text" and evt.get("text"):
                    snip = evt["text"][:90].replace("\n", " ")
                    break
            except Exception:
                continue
    return (route, ms, snip, _is_zero_result(snip))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--deep", action="store_true",
                    help="Probe /query for full routing (Haiku/Sonnet detection). Slower.")
    ap.add_argument("--filter", default="",
                    help="Only run tests where query contains this substring.")
    ap.add_argument("--no-throttle", action="store_true",
                    help="Disable inter-request sleep (use against local backends only).")
    args = ap.parse_args()

    tests = TESTS
    if args.filter:
        tests = [t for t in TESTS if args.filter.lower() in t[0].lower()]

    pass_count = 0
    fail_count = 0
    regressions: list[dict] = []
    zero_intercepts: list[dict] = []  # passed but with 0-row response
    per_category: dict[str, dict[str, int]] = {}
    consecutive_errors = 0
    aborted = False

    print(f"\nAuditing {len(tests)} queries against {PROD}")
    print(f"Mode: {'deep' if args.deep else 'shallow'} | "
          f"throttle: {'off' if args.no_throttle else f'{THROTTLE_SECONDS}s'}\n")
    print(f"{'#':>3} {'CAT':<32} {'EXPECT':<12} {'ACTUAL':<12} {'MS':>5} {'0?':>3}  QUERY")
    print("-" * 130)

    for i, (query, expected, category) in enumerate(tests, start=1):
        if not args.no_throttle and i > 1:
            time.sleep(THROTTLE_SECONDS)

        if args.deep:
            actual, ms, snip, is_zero = classify_deep(query)
        else:
            route, ms, snip, is_zero = classify_intercept_only(query)
            actual = route if route in ("intercepted", "error") else "miss"

        # Normalize expected for comparison.
        # In shallow mode, classify_intercept_only returns "intercepted" /
        # "miss" / "error". `expected="sonnet"` maps to "actual==miss"
        # (anything past the interceptor — shallow mode can't distinguish
        # Haiku vs Sonnet). Errors always fail. No "either" — the audit
        # is binary: query_engine handled it, or it's a regression.
        if actual == "error":
            ok = False
        elif expected == "intercepted":
            ok = actual == "intercepted"
        elif expected == "sonnet":
            ok = actual == "sonnet" if args.deep else actual == "miss"
        elif expected == "haiku":
            # Diagnostic-only; no test SHOULD expect this. Kept for clarity.
            ok = actual == "haiku" if args.deep else actual == "miss"
        else:
            ok = actual == expected

        status_marker = " " if ok else "✗"
        if ok:
            pass_count += 1
            consecutive_errors = 0
            if is_zero:
                # Track passing intercepts that returned 0 rows. These are
                # legitimate deterministic answers but worth reviewing
                # periodically — could be a parser misclassification (silent
                # wrong answer), seasonal-data gap, or genuinely just zero.
                zero_intercepts.append({
                    "query": query, "category": category,
                    "snippet": snip, "ms": ms,
                })
        else:
            fail_count += 1
            regressions.append({
                "query": query, "category": category,
                "expected": expected, "actual": actual,
                "ms": ms, "snippet": snip,
            })
            if actual == "error":
                consecutive_errors += 1
            else:
                consecutive_errors = 0

        per_category.setdefault(category, {"pass": 0, "fail": 0})
        per_category[category]["pass" if ok else "fail"] += 1

        zero_marker = "0" if is_zero else ""
        print(f"{status_marker}{i:>3} {category[:32]:<32} {expected:<12} {actual:<12} {ms:>5} {zero_marker:>3}  {query[:55]}")

        if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
            print(f"\n  ! {MAX_CONSECUTIVE_ERRORS} consecutive errors — backend may be struggling. Aborting.")
            aborted = True
            break

    print("\n" + "=" * 130)
    print(f"\nResult: {pass_count} pass / {fail_count} fail / "
          f"{pass_count + fail_count} run "
          f"({len(tests) - pass_count - fail_count} skipped)\n")

    print("By category:")
    for cat, counts in sorted(per_category.items()):
        total = counts["pass"] + counts["fail"]
        bar = "✓" if counts["fail"] == 0 else "✗"
        print(f"  {bar} {cat:<38} {counts['pass']}/{total}")

    if regressions:
        print(f"\nRegressions ({len(regressions)}):\n")
        for r in regressions:
            print(f"  [{r['category']}] expected={r['expected']} actual={r['actual']}")
            print(f"    Query: {r['query']}")
            if r["snippet"]:
                print(f"    Got:   {r['snippet']}")
            print()

    if zero_intercepts:
        print(f"\nZero-result intercepts ({len(zero_intercepts)}) — query_engine "
              f"answered deterministically with 0 rows. Review periodically:\n")
        for z in zero_intercepts:
            print(f"  [{z['category']}] {z['query']}")
            if z["snippet"]:
                print(f"    Got: {z['snippet']}")
            print()

    if aborted:
        print("AUDIT ABORTED EARLY due to consecutive errors.")
        sys.exit(2)

    sys.exit(0 if fail_count == 0 else 1)


if __name__ == "__main__":
    main()
