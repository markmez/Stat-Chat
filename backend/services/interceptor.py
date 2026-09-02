"""
Local query interceptor — handles predictable query patterns without Claude.

Sits in the query pipeline BEFORE any Claude API call. If a query matches
a known pattern (leaderboard, player lookup, comparison, splits, etc.),
returns a formatted response directly from the DB. Returns None to fall
through to Claude for truly unpredictable queries.

This mirrors the iOS AppState.sendQuestion() intercept chain exactly.
"""

import logging
from datetime import date, datetime

from services import name_matcher as nm
from services import response_builder as rb
from services.stat_definitions import lookup as stat_def_lookup

logger = logging.getLogger("statchat.interceptor")

# Healthchecks.io endpoint for query engine errors
# Uses /fail on a SEPARATE check from uptime. Create at healthchecks.io, set grace=0.
import os
_QE_ERROR_HC_UUID = os.getenv("QE_ERROR_HC_UUID", "")
_QE_ERROR_HC_URL = f"https://hc-ping.com/{_QE_ERROR_HC_UUID}/fail" if _QE_ERROR_HC_UUID else ""


def _ping_qe_error(question: str, error: str):
    """Log query engine error to dashboard + ping Healthchecks.io."""
    # Log to dashboard as its own response type
    try:
        from services.metering import log_query
        log_query(question, "system", "query_engine_error")
    except Exception:
        pass
    # Ping Healthchecks.io (if configured)
    if _QE_ERROR_HC_URL:
        try:
            import requests
            requests.post(_QE_ERROR_HC_URL, data=f"query: {question}\nerror: {error}",
                           timeout=3)
        except Exception:
            pass


# Game-event qualifiers we don't model structurally. Module-level so the
# Haiku-SQL and sql_planner paths can check the same list — otherwise the
# interceptor bails but the SQL paths still produce hallucinated leaderboards
# (e.g., regular HR totals labeled "Leadoff Home Runs"). Queries matching
# this list must skip every structural attempt and go straight to
# knowledge_mode for a pure narrative answer.
_EVENT_QUALIFIERS = (
    "inside the park", "inside-the-park",
    "walk-off", "walkoff", "walk off",
    "grand slam", "grand-slam", "grand slams",
    "pinch-hit home", "pinch hit home", "pinch-hit hr", "pinch hit hr",
    "pinch-hit homer", "pinch hit homer",
    "leadoff home run", "leadoff home runs", "leadoff homer",
    "leadoff homers", "leadoff hr",
    "extra-inning home", "extra inning home", "extra-innings home",
    "go-ahead home", "go-ahead hr", "go-ahead homer",
    "tying home run", "tying hr", "tying homer",
    "first-pitch home", "first pitch home",
)


def is_game_event_qualifier(question: str) -> bool:
    """Does this question reference a game-event qualifier (inside-the-park,
    walk-off, grand slam, leadoff HR, etc.) that we can't answer with
    precision? Used by every structural layer — interceptor, Haiku SQL,
    sql_planner — to skip itself and let knowledge_mode produce a clean
    narrative answer."""
    if not question:
        return False
    return any(p in question.lower() for p in _EVENT_QUALIFIERS)


# Stat-perspective split indicators — phrases that require a season-aggregate
# split table (pitching_platoon_splits, count_pitching_splits, etc.). Season
# aggregate; no game-level context.
_STAT_SPLIT_INDICATORS = (
    "against lefties", "against righties", "vs lefties", "vs righties",
    "vs lhp", "vs rhp", "vs lhh", "vs rhh",
    "against left-handed", "against right-handed",
    "vs left-handed", "vs right-handed",
    "against sliders", "against fastballs", "against curveballs",
    "against changeups", "against sinkers", "against cutters",
    "against sweepers", "against splitters",
    "vs sliders", "vs fastballs", "vs curveballs",
    "with risp", "runners in scoring position",
    "with 2 strikes", "with two strikes", "with the bases loaded",
    "in the count",  # "ahead in the count", "behind in the count"
    "full count",
)

# Team-context indicators — per-game filters derived from team_game_results.
# Live at game-log granularity; no per-plate-appearance detail.
_TEAM_CONTEXT_INDICATORS = (
    "in day games", "in night games", "day game", "night game",
    "on the road", "at home", "at fenway", "at home this",
    "in extra innings", "in extra-innings",
    "in rain games", "in cold weather", "in hot weather",
    "on tuesdays", "on wednesdays", "on thursdays",
    "on fridays", "on saturdays", "on sundays", "on mondays",
    "with team above .500", "with a winning record",
    "with team below .500", "with a losing record",
)


# Game-state / leverage / base-out situations we don't have splits or a
# crossproduct table for. Distinct from _EVENT_QUALIFIERS (which are play
# TYPES like walk-off HR); these are game STATES ("late and close", "in
# save situations", "with 2 outs"). Query engine, comparison, and every
# other structural parser silently drop these qualifiers if we don't bail
# early — user asks "Bellinger vs Rice in late and close games this year"
# and gets a normal full-season comparison with the leverage filter
# completely dropped (2026-07-23 bug). Route to knowledge_mode so Sonnet
# can honestly hedge.
_UNANSWERABLE_SITUATIONS = (
    "late and close", "close and late",
    "high leverage", "low leverage", "high-leverage", "low-leverage",
    "in save situations", "in save opportunities", "in save spots",
    "with the game on the line", "with the game tied",
    "in tie games", "in one-run games", "in one run games",
    "with the bases loaded", "with the bases empty",
    "with a runner on third", "with runners in scoring",
    "with two outs", "with 2 outs",
    "in clutch situations", "in the clutch",
    # Weather / temperature — MSF flags rain/cold but not fine-grained
    # temperature. Sonnet handles these queries via narrative knowledge.
    "warm day", "warm days", "cold day", "cold days",
    "hot day", "hot days", "on warm", "on cold", "on hot",
    "in warm weather", "in cold weather", "in hot weather",
    "in rain", "in the rain", "rain games", "rainy",
    "temperature", "degrees",
)


def is_unanswerable_situation(question: str) -> bool:
    """Does this question reference a game-state / leverage situation we
    don't have a split table for? Same pattern as is_game_event_qualifier —
    every structural parser gets the chance to silently drop these
    qualifiers if we let them through, so bail at the top of the chain and
    let knowledge_mode narrate honestly."""
    if not question:
        return False
    return any(p in question.lower() for p in _UNANSWERABLE_SITUATIONS)


def is_compound_split_unanswerable(question: str) -> bool:
    """Does this question combine a stat-perspective split with a team-context
    filter — the crossproduct we don't store (e.g., "vs lefties on the road")?

    Query engine already bails this shape internally (see _execute_team_context_
    leaderboard). But Haiku SQL and sql_planner run anyway and silently drop
    ONE of the two filters, returning a confident-wrong number labeled as the
    combined answer. This bail routes matching queries directly to
    knowledge_mode so Sonnet can narrate honestly ("we don't cleanly break
    out that intersection — his vs-lefty number is X, his road number is Y").

    Matches when BOTH a stat-split phrase AND a team-context phrase appear.
    Either alone is answerable via a single split table and shouldn't bail.
    """
    if not question:
        return False
    lower = question.lower()
    has_split = any(p in lower for p in _STAT_SPLIT_INDICATORS)
    has_context = any(p in lower for p in _TEAM_CONTEXT_INDICATORS)
    return has_split and has_context


def _claim(result, query):
    """Default-on guard for single-player stat parsers (the central choke point).

    A matched parser only *claims* the query when no meaningful words are left
    unaccounted for: the player name + stat aliases + filler + numbers + the
    parser's declared `consumed` phrases. Otherwise this returns None so the
    dispatch falls through to the query engine / Haiku instead of shipping a
    confident partial answer that silently drops a qualifier.

    Wrap EVERY new single-player stat parser's dispatch call in this, and have
    the parser declare `consumed` (the trigger phrases it accounts for). A
    parser that forgets `consumed` fails *loud* (its queries bail to Haiku),
    not *silent* (wrong-but-confident) — that's the point.
    """
    if not isinstance(result, dict):
        return result
    name = result.get("name") or result.get("player_name")
    if not name:
        return result  # no single player → no partial-answer risk to guard
    if nm._residual_qualifier_words(query.strip().lower(), name,
                                    extra_consumed=result.get("consumed", [])):
        return None
    return result


def _try_intercept_once(question: str):
    """
    Try to answer the question locally from the DB.
    Returns formatted response text (with [STATGRID], [SUGGEST], etc. tags),
    or None if the query should fall through to Claude.
    """
    trimmed = question.strip()
    if not trimmed:
        return None

    import re

    # Curated typo normalization — EXPLICIT map only, never fuzzy (the
    # fuzzyMatch trap: edit-distance matching collides with player names,
    # e.g. 'ganes'~'Gomes'). Observed-in-the-wild unit-word typos that
    # defeat both the parsers AND their bail regexes ('last 5 ganes'
    # slipped past parse_team_stats' window bail into a TEAMCARD claim).
    for _typo, _fix in (("ganes", "games"), ("gmaes", "games"), ("gamse", "games"),
                        ("clevland", "cleveland"), ("cleaveland", "cleveland"),
                        ("pticher", "pitcher"), ("pitcer", "pitcher"), ("pither", "pitcher"),
                        ("metings", "meetings"), ("meetigns", "meetings"),
                        ("tonite", "tonight"), ("picthers", "pitchers"),
                        ("pichers", "pitchers"), ("standigns", "standings")):
        trimmed = re.sub(rf"\b{_typo}\b", _fix, trimmed, flags=re.I)

    lower = trimmed.lower()

    # Game-event qualifiers we don't model structurally — see
    # is_game_event_qualifier above. Bail so the request falls through to
    # knowledge_mode (the Haiku/sql_planner paths also short-circuit on this
    # check; we want only pure narrative answers for these).
    if is_game_event_qualifier(trimmed):
        logger.info("intercept_bail_event_qualifier question=%r", trimmed)
        return None

    # Game-state / leverage situations — see is_unanswerable_situation above.
    # Bail so downstream parsers (comparison, single-stat, etc.) don't
    # silently drop the qualifier and return a wrong-labeled answer.
    if is_unanswerable_situation(trimmed):
        logger.info("intercept_bail_situation question=%r", trimmed)
        return None

    # Statcast queries for current season — graceful rejection, don't count query
    _statcast_keywords = ["exit velo", "exit velocity", "launch angle", "barrel rate",
                          "barrels", "sprint speed", "hard hit", "hard-hit",
                          "spin rate", "pitch velocity", "xba", "xslg", "xwoba",
                          "expected batting", "expected slugging", "chase rate",
                          "whiff rate", "sweet spot", "statcast", "bat speed",
                          "swing length", "extension"]
    _war_keywords = ["wins above replacement", "fwar", "bwar", "rwar", "wrc+", "wrc plus"]
    _war_word = re.search(r'\bwar\b', lower)  # word-boundary match for "war"
    current_year = date.today().year
    is_current = (str(current_year) in lower or "this season" in lower
                  or "this year" in lower
                  or not re.search(r'20[012]\d', lower))  # no year specified = current
    if any(kw in lower for kw in _statcast_keywords):
        if is_current:
            return ("__NO_COUNT__We don't have Statcast data (exit velocity, launch angle, barrel rate, etc.) "
                    "in our database. We focus on verified game stats — batting, pitching, splits, streaks, "
                    "and matchups.\n\n"
                    "_This search didn't count against your free queries._"
                    "\n\n[SUGGEST]OPS leaders this season[/SUGGEST]"
                    "\n[SUGGEST]most home runs this season[/SUGGEST]"
                    "\n[SUGGEST]best ERA this season[/SUGGEST]")
    if _war_word or any(kw in lower for kw in _war_keywords):
        if is_current:
            return ("__NO_COUNT__We don't have WAR (Wins Above Replacement) in our database — "
                    "it's a proprietary metric computed differently by FanGraphs and Baseball Reference. "
                    "We focus on verified game stats like OPS, ERA, and OPS+.\n\n"
                    "_This search didn't count against your free queries._"
                    "\n\n[SUGGEST]OPS+ leaders this season[/SUGGEST]"
                    "\n[SUGGEST]OPS leaders this season[/SUGGEST]"
                    "\n[SUGGEST]best ERA this season[/SUGGEST]")

    # Advanced fielding metrics — proprietary, never in our DB.
    # "Gold Glove" is NOT in this list: it's an award we store in the
    # awards table and answer via award_lookup / award_intersection.
    _adv_fielding_keywords = ["uzr", "ultimate zone", "drs", "defensive runs saved",
                              "oaa", "outs above average", "range factor", "zone rating",
                              "framing", "catcher framing", "arm strength", "pop time",
                              "defensive value"]
    if any(kw in lower for kw in _adv_fielding_keywords):
        return ("__NO_COUNT__We don't have advanced defensive metrics (UZR, DRS, OAA, framing, etc.) "
                "in our database — these are proprietary to FanGraphs, Sports Info Solutions, and Statcast. "
                "We do have fielding percentage, errors, putouts, assists, and double plays.\n\n"
                "_This search didn't count against your free queries._"
                "\n\n[SUGGEST]best fielding percentage at shortstop 2025[/SUGGEST]"
                "\n[SUGGEST]most errors 2025[/SUGGEST]"
                "\n[SUGGEST]most double plays by a second baseman 2025[/SUGGEST]")

    # Basic fielding queries for current season — no 2026 fielding data in our DB
    _basic_fielding_keywords = ["fielding", "fielding %", "fielding pct", "fielding percentage",
                                "putouts", "assists", "errors", "double plays turned",
                                "passed balls", "defensive"]
    if any(kw in lower for kw in _basic_fielding_keywords):
        if is_current:
            return ("__NO_COUNT__We don't have fielding stats for the current season yet. "
                    "We have fielding percentage, putouts, assists, errors, and double plays "
                    "for previous seasons (through 2025).\n\n"
                    "_This search didn't count against your free queries._"
                    "\n\n[SUGGEST]best fielding percentage at shortstop 2025[/SUGGEST]"
                    "\n[SUGGEST]most errors 2025[/SUGGEST]"
                    "\n[SUGGEST]most double plays by a second baseman 2025[/SUGGEST]")

    # PA/AB-window queries — game logs aggregate per game (no per-PA records),
    # so "first 50 PAs of a career" can't be computed exactly. Refuse with
    # NO_COUNT + game-equivalent SUGGEST. When the per-PA log infrastructure
    # ships for 2025-2026 (project-pa-window-queries.md), this branch can
    # narrow to historical-only refusals and let current-season queries
    # answer structurally.
    pa_window_match = re.search(
        r'\b(first|last)\s+'
        r'(\d+|two|three|four|five|six|seven|eight|nine|ten|'
        r'eleven|twelve|fifteen|twenty|twenty-five|thirty|forty|fifty|'
        r'sixty|seventy|eighty|ninety|hundred|two\s+hundred)\s+'
        r'(?:career\s+|of\s+)?'
        r'(plate\s+appearances?|pas?|at[- ]?bats?|abs?)\b'
        # Consume trailing "of a/his/her/their career" / "in MLB" so the
        # game-equivalent SUGGEST doesn't read "first 12 career games of a career".
        r'(?:\s+(?:of\s+(?:a|his|her|their)\s+career|in\s+(?:mlb|the\s+majors|his|her|their\s+career)))?',
        lower,
    )
    if pa_window_match:
        direction = pa_window_match.group(1)
        n_raw = pa_window_match.group(2)
        unit_raw = pa_window_match.group(3)
        _word_nums = {
            "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
            "seven": 7, "eight": 8, "nine": 9, "ten": 10,
            "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20,
            "twenty-five": 25, "thirty": 30, "forty": 40, "fifty": 50,
            "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
            "hundred": 100, "two hundred": 200,
        }
        n_val = int(n_raw) if n_raw.isdigit() else _word_nums.get(n_raw.replace(" ", " "))
        if n_val and n_val >= 5:
            # PA → games at ~4.2 PA/game (regular full-time hitter avg);
            # AB → games at ~3.7 AB/game.
            unit_compact = unit_raw.replace(" ", "").replace("-", "")
            is_ab = unit_compact in ("ab", "abs", "atbats", "atbat")
            per_game = 3.7 if is_ab else 4.2
            import math
            game_count = max(2, math.ceil(n_val / per_game))
            unit_display = "at-bat" if is_ab else "plate appearance"
            unit_plural = f"{unit_display}s"
            # Build a reformulated SUGGEST by swapping the window phrase
            # in the user's actual question for the game-equivalent.
            window_phrase = pa_window_match.group(0)
            game_phrase = f"{direction} {game_count} career games"
            # Best-effort reformulation against the original casing.
            reformulated = re.sub(
                re.escape(window_phrase), game_phrase, trimmed,
                count=1, flags=re.IGNORECASE,
            )
            if reformulated == trimmed:
                # Fallback — present a generic pivot.
                reformulated = f"{direction} {game_count} career games"
            return (
                f"__NO_COUNT__We don't have {unit_display}-level history at this "
                f"granularity. Game logs aggregate per game (one row per player "
                f"per game), so we can't compute exactly {n_val} {unit_plural} — "
                f"game boundaries don't line up with PA/AB counts.\n\n"
                f"_This search didn't count against your free queries._\n\n"
                f"The closest equivalent we can answer is the same question over "
                f"a comparable number of **games** (~{per_game:.1f} {unit_plural}/game, "
                f"so {n_val} {unit_plural} ≈ {game_count} games):\n\n"
                f"[SUGGEST]{reformulated}[/SUGGEST]"
            )

    # 0a. Tonight preview — "how will Judge do tonight" (auto-resolve probable pitcher)
    tonight = nm.parse_tonight_preview(trimmed)
    if tonight:
        try:
            from services.daily_games import get_player_team, get_opponent_starter, get_todays_games, has_game_today
            player_name = tonight["name"]
            team = get_player_team(player_name)
            if team:
                result = get_opponent_starter(team)
                if result:
                    pitcher_name, opp_team = result
                    # Match pitcher name against our DB
                    matched_pitcher = nm.match_player(pitcher_name)
                    if matched_pitcher:
                        # Build game context (e.g., "Yankees at Red Sox")
                        from services.notable_events import RETRO_TO_DISPLAY
                        game_ctx = None
                        games = get_todays_games()
                        for g in games:
                            if g["away"] == team:
                                game_ctx = f"{RETRO_TO_DISPLAY.get(team, team)} at {RETRO_TO_DISPLAY.get(opp_team, opp_team)}"
                                break
                            elif g["home"] == team:
                                game_ctx = f"{RETRO_TO_DISPLAY.get(opp_team, opp_team)} at {RETRO_TO_DISPLAY.get(team, team)}"
                                break
                        response = rb.build_matchup(player_name, matched_pitcher, game_context=game_ctx)
                        if response:
                            return response
                    else:
                        logger.info("tonight_pitcher_not_found pitcher=%r", pitcher_name)
                else:
                    # Check if the team has a game at all today
                    if has_game_today(team):
                        return f"Probable starters haven't been announced yet for tonight's game. Try again closer to game time, or ask about a specific matchup like \"{player_name} vs [pitcher name]\"."
                    else:
                        return f"{player_name}'s team doesn't appear to have a game scheduled today. Try asking about a specific matchup like \"{player_name} vs [pitcher name]\"."
        except Exception as e:
            logger.warning("tonight_preview_error error=%s", e)

    # 0a-team-context. Team-game-context filters ("in extra innings", "in day
    # games", "when team had won 40 games"). These compose with stat queries
    # via query_engine — but if we let the simple parsers below run first,
    # they mismatch on phrase words: "innings" hits IP, "won at least N games"
    # hits wins. Detect team_context EARLY and force-route to query_engine,
    # which decompose() handles natively.
    if nm._detect_team_context(lower) is not None:
        from services.query_engine import decompose, execute as qe_execute
        try:
            plan = decompose(trimmed)
        except Exception as e:
            logger.error("query_engine_decompose_error question=%r error=%s", trimmed, e)
            plan = None
        if plan and plan.is_valid:
            response = qe_execute(plan)
            if response:
                logger.info("query_engine_handled_team_context question=%r", trimmed)
                return response
        # Fall through to other parsers if query_engine couldn't handle it
        # (rare — usually means the stat itself wasn't recognized)

    # 0a-team-conditional-record. "best/worst (team) record when scoring 5+
    # runs", "best home record", etc. Same problem as team_context: if the
    # threshold/leaderboard parsers below run first they'll grab "5+ runs"
    # as a runs-stat filter and return a player leaderboard. Force-route to
    # query_engine, which has the dedicated team_conditional_record detector.
    # Gate is cheap: needs "record" + a direction word + a condition trigger.
    if "record" in lower and ("best" in lower or "worst" in lower) and \
       (re.search(r"\b(?:scoring|scored|allowing|allowed|giving\s+up|gave\s+up|gives\s+up)\b", lower)
        or re.search(r"\b(?:home|road|away)\s+record\b", lower)):
        from services.query_engine import decompose, execute as qe_execute
        try:
            plan = decompose(trimmed)
        except Exception as e:
            logger.error("query_engine_decompose_error question=%r error=%s", trimmed, e)
            plan = None
        if plan and plan.is_valid and plan.query_type == "team_conditional_record":
            response = qe_execute(plan)
            if response:
                logger.info("query_engine_handled_team_conditional_record question=%r", trimmed)
                return response
        # Fall through if query_engine didn't recognize it.

    # Award queries — force-route to query_engine before the player-name
    # parsers fire. "How many Gold Gloves does Aaron Judge have" otherwise
    # falls through to the career-lookup path and returns Judge's stat grid
    # instead of a Gold Glove count. Gate on award keyword present AND no
    # stat keyword (so "most HR by an MVP" still hits the stat-filtered
    # leaderboard route).
    _award_kw = ("mvp", "cy young", "rookie of the year", " roy ",
                 "all-star", "all star", "allstar",
                 "gold glove", "silver slugger",
                 "hall of fame", " hof ")
    _stat_kw_re = re.compile(
        r'\b(?:home runs?|hr|rbi|hits?|avg|average|ops|obp|slg|era|whip|'
        r'strikeouts?|stolen bases?|wins?|saves?|innings)\b',
        re.I,
    )
    # "all-star break" is a DATE reference (second half), not an awards query —
    # let it fall through to parse_player_date_range / the date-range path.
    _is_asg_break = "all-star break" in lower or "all star break" in lower
    if any(kw in f" {lower} " for kw in _award_kw) and not _stat_kw_re.search(lower) \
            and not _is_asg_break:
        from services.query_engine import decompose, execute as qe_execute
        try:
            plan = decompose(trimmed)
        except Exception as e:
            logger.error("query_engine_decompose_error question=%r error=%s", trimmed, e)
            plan = None
        if plan and plan.is_valid and plan.query_type in (
            "award_lookup", "award_intersection",
        ):
            response = qe_execute(plan)
            if response:
                logger.info("query_engine_handled_award question=%r", trimmed)
                return response
        # Fall through if query_engine didn't recognize it.

    # 0a-player-career-filter. "Judge OPS with the Yankees", "Ohtani career OPS
    # with the Angels", "Pujols HR before 2010", etc. — decompose() handles
    # these via query_type="player_career_filtered", but parse_team_stats
    # below claims "OPS with the Yankees" as a team-roster query first
    # (bare last name "Judge" doesn't register via _has_player_name because
    # more than one player has that last name in history). Force-route to
    # query_engine when a career-filter phrase signature is present.
    if (re.search(r"\bwith\s+the\s+\w+", lower)
            or re.search(r"\bas\s+an?\s+\w+", lower)
            or re.search(r"\bwith\s+his\s+\w+", lower)
            or "excluding " in lower
            or re.search(r"\b(?:after|before|through|since)\s+(?:19|20)\d{2}\b", lower)
            or re.search(r"\bfrom\s+(?:19|20)\d{2}\s+to\s+(?:19|20)\d{2}\b", lower)):
        from services.query_engine import decompose, execute as qe_execute
        try:
            plan = decompose(trimmed)
        except Exception as e:
            logger.error("query_engine_decompose_error question=%r error=%s", trimmed, e)
            plan = None
        if plan and plan.is_valid and plan.query_type == "player_career_filtered":
            response = qe_execute(plan)
            if response:
                logger.info("query_engine_handled_player_career_filter question=%r", trimmed)
                return response
        # Fall through if not matched.

    # 0a-perfect. Perfect games — hand-curated JSON list. Must come before the
    # leaderboard/threshold parsers so "perfect games since 2010" doesn't get
    # parsed as a threshold query on "games".
    pg = nm.parse_perfect_games(trimmed)
    if pg:
        response = rb.build_perfect_games(pg)
        if response:
            return response

    # 0b. Matchup — "Judge vs Verlander" (batter vs pitcher)
    matchup = nm.parse_matchup(trimmed)
    if matchup:
        response = rb.build_matchup(
            matchup["batter"], matchup["pitcher"], matchup.get("season"))
        if response:
            return response

    # 0c. Player game window — "first/last N games" (must be before single stat/season lookup
    # which would incorrectly match "games" as a stat and "ryan" as Joe Ryan)
    game_window = nm.parse_player_game_window(trimmed)
    if game_window:
        response = rb.build_player_game_window(
            game_window["name"], game_window["window_type"],
            game_window["n_games"], game_window.get("stat"),
            game_window.get("season"),
            window_noun=game_window.get("window_noun", "Games"))
        if response:
            return response

    # 0d. Player date range — "stats after June 30 2025", "in the last 30 days".
    # Must run before parse_month_query (which would grab the bare month name and
    # drop the "after"/"30" qualifier) and the single-stat/season parsers.
    # Non-greedy: requires an explicit date bound + passes the unexplained guard.
    date_range = nm.parse_player_date_range(trimmed)
    if date_range:
        response = rb.build_player_date_range(
            date_range["name"], date_range.get("since_date"),
            date_range.get("end_date"))
        if response:
            return response

    # Player vs a specific team — "{player} vs the Red Sox" / "...the Boston Red
    # Sox". An opponent split from the player's game logs. Must come BEFORE
    # comparison: a full team name contains a city word that resolves to a
    # player (e.g. "Boston"), so comparison would otherwise treat it as a
    # player-vs-player matchup. When match_team fails (real player-vs-player),
    # this returns None and comparison handles it. Also beats the team parsers,
    # which would return the opponent's roster (parse_team_stats misses
    # ambiguous surnames like "Judge").
    pvl = nm.parse_pitcher_vs_lineup(trimmed)
    if pvl:
        response = rb.build_pitcher_vs_lineup(
            pvl["name"], pvl["team_code"], pvl["season"])
        if response:
            logger.info("pitcher_vs_lineup question=%r", trimmed)
            return response

    pvt = _claim(nm.parse_player_vs_team(trimmed), trimmed)
    if pvt:
        response = rb.build_player_vs_team(
            pvt["name"], pvt["opponent_code"], pvt["season"])
        if response:
            return response

    # 1. Comparison — "Judge vs Soto", "Compare Lindor and Witt"
    comp = nm.parse_comparison(trimmed)
    if comp:
        # Claim-confidence: both names + comparison markers must account for
        # the whole query, or this parser declines (2026-08-31 discipline).
        _cmp_consumed = [comp["name2"], "vs", "vs.", "v.", "versus", "compare",
                         "compared", "better", "than", "head", "head-to-head",
                         "or", "matchup"]
        if nm._residual_qualifier_words(trimmed.strip().lower(), comp["name1"],
                                        extra_consumed=_cmp_consumed):
            comp = None
    if comp:
        p1, p2 = comp["name1"], comp["name2"]
        season = comp["season"]
        alts = comp.get("alternatives", [])
        see_also = ""
        if alts:
            see_also = f"\n[SEEALSO]{','.join(alts)}[/SEEALSO]"

        _p1_pitch, _p2_pitch = nm.is_pitcher(p1), nm.is_pitcher(p2)
        if _p1_pitch and _p2_pitch:
            response = rb.build_pitching_comparison(p1, p2, season)
        elif _p1_pitch != _p2_pitch:
            # Batter vs pitcher is a MATCHUP, not a comparison (the Kikuchi
            # batting-line-of-zeros incident, 2026-08-31). Delegate to the
            # existing matchup engine — comparison's fuzzier name matching
            # acts as the typo net parse_matchup lacks ("De La Crus").
            batter, pitcher = (p2, p1) if _p1_pitch else (p1, p2)
            response = rb.build_matchup(batter, pitcher, season)
        else:
            response = rb.build_comparison(p1, p2, season)
        if response:
            return response + see_also

    # Best/worst calendar month — "Judge's best month this year" — from the
    # monthly aggregate tables. Before the streak + query-engine paths, which
    # would otherwise return an all-time month leaderboard ignoring the player.
    best_month = nm.parse_best_month(trimmed)
    if best_month:
        response = rb.build_best_month(
            best_month["name"], best_month["performance"],
            best_month["season"], career=best_month["career"])
        if response:
            return response

    # 2. Streak history — "Judge hot streaks 2025", "Ohtani cold streak"
    streak = nm.parse_streak_query(trimmed)
    if streak:
        name, perf, season = streak["name"], streak["performance"], streak["season"]
        career = streak.get("career", False)
        if nm.is_pitcher(name):
            response = rb.build_pitching_streak_list(name, perf, season, career=career)
        else:
            response = rb.build_streak_list(name, perf, season, career=career)
        if response:
            return response

    # 3. Current form — "how is Judge doing lately?"
    form_name = nm.parse_current_form(trimmed)
    if form_name:
        if nm.is_pitcher(form_name):
            response = rb.build_pitching_current_form(form_name)
        else:
            response = rb.build_current_form(form_name)
        if response:
            return response

    # 4. Slash line — "Judge's slash line"
    slash = _claim(nm.parse_slash_line_lookup(trimmed), trimmed)
    if slash:
        response = rb.build_slash_line_lookup(slash["name"], slash["season"])
        if response:
            return response

    # 5. Season count — "how many seasons has Judge hit a triple?"
    season_count = nm.parse_season_count(trimmed)
    if season_count:
        is_pitching = nm.is_pitching_stat(season_count["stat"])
        response = rb.build_season_count(
            season_count["name"], season_count["stat"],
            season_count["stat_abbrev"], season_count["stat_name"],
            season_count["threshold"], season_count["is_rate"], is_pitching)
        if response:
            return response

    # 5a. First PA of game (batting only) — "Judge OPS in his first at bat"
    # MUST run before single_stat_lookup, which would otherwise match "Judge
    # OPS" generically and return a season figure that ignores the qualifier.
    fpa = nm.parse_first_pa(trimmed)
    if fpa:
        name, season = fpa["name"], fpa["season"]
        if not nm.is_pitcher(name):
            response = rb.build_first_pa_splits(name, season)
            if response:
                return response

    # 5b. Pitcher inning splits — "Cole's 1st inning ERA"
    pi = nm.parse_pitching_inning(trimmed)
    if pi:
        name, inning, season = pi["name"], pi["inning"], pi["season"]
        if nm.is_pitcher(name):
            response = rb.build_pitching_inning_splits(name, inning, season)
            if response:
                return response

    # 5c. Pitcher times-through-the-order — "Skubal 3rd time through"
    tto = nm.parse_pitching_tto(trimmed)
    if tto:
        name, t, season = tto["name"], tto["tto"], tto["season"]
        if nm.is_pitcher(name):
            response = rb.build_pitching_tto_splits(name, t, season)
            if response:
                return response

    # 6. Single stat lookup — "Judge home runs", "Ohtani ERA"
    stat_lookup = nm.parse_single_stat_lookup(trimmed)
    if stat_lookup:
        name, stat, season = stat_lookup["name"], stat_lookup["stat"], stat_lookup["season"]
        if nm.is_pitcher(name) or nm.is_pitching_stat(stat):
            response = rb.build_pitching_single_stat_lookup(name, stat, season)
        else:
            response = rb.build_single_stat_lookup(name, stat, season)
        if response:
            return response

    # 6. Career lookup — "Judge career stats", "Judge career home runs"
    career = nm.parse_career_lookup(trimmed)
    if career:
        name, stat = career["name"], career["stat"]
        if nm.is_pitcher(name) or (stat and nm.is_pitching_stat(stat)):
            response = rb.build_pitching_career_lookup(name, stat)
        else:
            response = rb.build_career_lookup(name, stat)
        if response:
            return response

    # 7. Platoon splits — "Judge vs lefties", "Judge home runs vs lefties 2025"
    platoon = _claim(nm.parse_platoon_splits(trimmed), trimmed)
    if platoon:
        name, hand, season = platoon["name"], platoon["hand"], platoon["season"]
        # Check if a specific stat was requested
        stat_match = nm.match_stat(trimmed.lower())
        if stat_match and not nm.is_pitcher(name):
            # Stat-filtered platoon: extract just that stat from split data
            response = rb.build_platoon_stat_single(name, hand, season, stat_match)
            if response:
                return response
        if nm.is_pitcher(name):
            response = rb.build_pitching_platoon_splits(name, hand, season)
        else:
            response = rb.build_platoon_splits(name, hand, season)
        if response:
            return response

    # 7b. Platoon leaderboard — "best ERA vs LHB", "most HR against lefties".
    # No player name in query; ranks across all players who faced the
    # specified handedness. Routes batting and pitching variants based on
    # is_pitching flag from the parser.
    h2h = nm.parse_team_h2h_recent(trimmed)
    if h2h:
        response = rb.build_team_h2h_recent(h2h["team_a"], h2h["team_b"], h2h["n"])
        if response:
            logger.info("team_h2h_recent question=%r", trimmed)
            return response

    prb = nm.parse_probables(trimmed)
    if prb:
        response = rb.build_probables(prb.get("team_code"))
        if response:
            logger.info("probables question=%r", trimmed)
            return response

    tng = nm.parse_team_next_game(trimmed)
    if tng:
        response = rb.build_team_next_game(tng["team_code"])
        if response:
            logger.info("team_next_game question=%r", trimmed)
            return response

    slate = nm.parse_todays_slate(trimmed)
    if slate is not None:
        response = rb.build_todays_slate()
        if response:
            logger.info("todays_slate question=%r", trimmed)
            return response

    cvl = nm.parse_career_value_lookup(trimmed)
    if cvl:
        response = rb.build_career_value_lookup(cvl["stat"], cvl["value"])
        if response:
            logger.info("career_value_lookup question=%r", trimmed)
            return response

    platoon_lb = nm.parse_platoon_leaderboard(trimmed)
    if platoon_lb:
        response = rb.build_platoon_leaderboard(
            platoon_lb["stat"], platoon_lb["hand"],
            is_pitching=platoon_lb["is_pitching"],
            season=platoon_lb["season"],
            limit=platoon_lb["limit"],
            league=platoon_lb.get("league"),
        )
        if response:
            return response

    # 8. Home/away splits — "Judge home vs away"
    home_away = _claim(nm.parse_home_away_splits(trimmed), trimmed)
    if home_away:
        name, loc, season = home_away["name"], home_away["location"], home_away["season"]
        if nm.is_pitcher(name):
            response = rb.build_pitching_home_away_splits(name, loc, season)
        else:
            response = rb.build_home_away_splits(name, loc, season)
        if response:
            return response

    # 9. RISP splits — "Judge with runners in scoring position"
    risp = _claim(nm.parse_risp_splits(trimmed), trimmed)
    if risp:
        name, season = risp["name"], risp["season"]
        if nm.is_pitcher(name):
            response = rb.build_pitching_risp_splits(name, season)
        else:
            response = rb.build_risp_splits(name, season)
        if response:
            return response

    # 10. Pitch type splits — "Judge vs sliders"
    pitch_type = _claim(nm.parse_pitch_type_splits(trimmed), trimmed)
    if pitch_type:
        name = pitch_type["name"]
        pt = pitch_type["pitch_type"]
        season = pitch_type["season"]
        if nm.is_pitcher(name):
            response = rb.build_pitching_pitch_type_splits(name, pt, season)
        else:
            response = rb.build_pitch_type_splits(name, pt, season)
        if response:
            return response

    # 11. Count splits — "Judge with two strikes"
    count = _claim(nm.parse_count_splits(trimmed), trimmed)
    if count:
        name, counts, season = count["name"], count["counts"], count["season"]
        if nm.is_pitcher(name):
            response = rb.build_pitching_count_splits(name, counts, season)
        else:
            response = rb.build_count_splits(name, counts, season)
        if response:
            return response

    # 12. Month stats — "Judge in September"
    month = nm.parse_month_query(trimmed)
    if month:
        response = rb.build_month_stats(month["player_name"], month["month"], month["season"])
        if response:
            return response

    # 13. Game logs — "Devers game logs", "Judge game log 2025"
    import re as _re
    game_log_match = _re.search(r'\bgame\s*logs?\b', lower)
    if game_log_match:
        player = nm.find_player_in_text(trimmed)
        if not player:
            result = nm.match_player_with_prominence(trimmed)
            player = result[0] if result else None
        if player:
            season = nm.detect_season(trimmed, default_to_most_recent=False)
            if not season:
                season = date.today().year
            response = rb.build_player_game_logs(player, season)
            if response:
                return response

    # 14. Season lookup — "How did Judge do last season?", just "Aaron Judge"
    season_lookup = nm.parse_season_lookup(trimmed)
    if season_lookup:
        name, season = season_lookup["name"], season_lookup["season"]
        if nm.is_pitcher(name):
            response = rb.build_pitching_season_summary(name, season)
        else:
            response = rb.build_season_summary(name, season)
        if response:
            return response

    # --- Niche parsers (specific triggers, no false-match risk) ---

    # Composite threshold — "30/30 seasons", "40/40"
    composite = nm.parse_composite_threshold(trimmed)
    if composite:
        response = rb.build_composite_threshold(composite)
        if response:
            return response

    # Triple crown — "who won the triple crown?"
    if nm.parse_triple_crown(trimmed):
        response = rb.build_triple_crown()
        if response:
            return response

    # Consecutive streak — "longest hitting streak"
    consec = _claim(nm.parse_consecutive_streak(trimmed), trimmed)
    if consec:
        season = consec.get("season")
        # "all time" / "career" / "ever" / "in history" → scan full game-log
        # history (build_consecutive_streak treats season=None as all-time).
        # The optimized SQL (window functions partitioned by player+season,
        # JOIN deferred to the final 15-row SELECT) runs in ~25s; gunicorn
        # worker timeout was bumped to 90s to give it room.
        _all_time_signals = ["all time", "all-time", "in history", "career", " ever"]
        is_all_time = any(sig in lower for sig in _all_time_signals)
        if not season and not is_all_time:
            # No year AND not all-time → default to current season
            # (prevents unbounded scan for vague queries)
            season = date.today().year
        elif is_all_time:
            season = None
        response = rb.build_consecutive_streak(
            consec["type"], consec.get("player_name"), season)
        if response:
            return response

    # Team game score — "Yankees score yesterday", "did the Mets win last night",
    # "Dodgers vs Padres tonight". Must come before parse_team_record because
    # "Yankees score yesterday" doesn't contain "record" and could match it.
    team_score = nm.parse_team_game_score(trimmed)
    if team_score:
        response = rb.build_team_game_score(
            team_score["team_code"],
            team_score.get("opponent_code"),
            team_score["date_keyword"],
        )
        if response:
            return response

    # Team record — "Yankees record", "how are the Mets doing"
    team_record = nm.parse_team_record(trimmed)
    if team_record:
        response = rb.build_team_record(
            team_record["team_code"], team_record["season"])
        if response:
            return response

    # Team total — "how many HR did the Yankees hit?"
    team_total = nm.parse_team_total(trimmed)
    if team_total:
        response = rb.build_team_total(
            team_total["team_code"], team_total["stat"], team_total["season"])
        if response:
            return response

    # Team stats — "Yankees hitters", "Dodgers OPS leaders"
    team_stats = nm.parse_team_stats(trimmed)
    if team_stats:
        stat = team_stats.get("stat")
        lower_q = trimmed.lower()
        # Explicit signals to lock into one side. Without a signal AND
        # without a specific stat, previous behavior silently defaulted
        # to hitters — surprising when the user asked "Diamondbacks
        # stats" and got a batting-only leaderboard with no pitching.
        wants_hitters = any(w in lower_q for w in
                             ["hitter", "hitters", "batting", "batter", "batters", "offense", "offensive"])
        wants_pitchers = any(w in lower_q for w in
                              ["pitching", "pitcher", "pitchers", "pitching stats"])
        pitching_context = (stat and nm.is_pitching_stat(stat)) or wants_pitchers
        batting_context = wants_hitters or (stat and not nm.is_pitching_stat(stat))
        if pitching_context and not batting_context:
            response = rb.build_pitching_team_stats(
                team_stats["team_code"], stat, team_stats["season"])
        elif batting_context and not pitching_context:
            response = rb.build_team_stats(
                team_stats["team_code"], stat, team_stats["season"])
        else:
            # No side specified — the user just said "Diamondbacks stats"
            # or "Yankees 2026 stats". Rather than picking hitters (which
            # silently hides pitching) or building a combined text response
            # (redundant with the existing team card in iOS), emit a
            # [TEAMCARD:CODE] control tag that the client intercepts and
            # uses to navigate to the existing TeamCardView. Clean split
            # of responsibilities: chat handles targeted queries, the
            # team card handles browsing.
            response = f"[TEAMCARD:{team_stats['team_code']}]"
        if response:
            return response

    # Leaderboard sliding window — "who had the best 10-game stretch this year"
    # (no specific player). A multi-player N-game window scan, season-scoped.
    # Before the query engine, which has no multi-player window concept.
    lbw = nm.parse_leaderboard_window(trimmed)
    if lbw:
        response = rb.build_leaderboard_sliding_window(
            lbw["stat"], lbw["n"], lbw["sort_asc"], lbw["season"], lbw["is_pitching"])
        if response:
            return response

    # --- Query Engine — the primary stat query handler ---
    # Handles leaderboards, thresholds, counts, superlatives, game-log queries,
    # split leaderboards, team rankings, derived stats, multi-threshold,
    # and any combination of filters (position, bats, age, rookie, pitcher role).
    # Only answers when it fully understands the query (no unexplained words).
    # If it bails, go straight to Haiku/Sonnet — no old parsers.
    from services.query_engine import decompose, execute as qe_execute
    try:
        plan = decompose(trimmed)
    except Exception as e:
        logger.error("query_engine_decompose_error question=%r error=%s type=%s",
                     trimmed, e, type(e).__name__)
        _ping_qe_error(trimmed, f"decompose: {type(e).__name__}: {e}")
        return None
    logger.info("query_engine_plan question=%r valid=%s scope=%s stat=%s unexplained=%s active=%s since_date=%s",
                trimmed, plan.is_valid, plan.scope, plan.stat, plan.unexplained_words, plan.active_only, plan.since_date)
    if plan.is_valid:
        response = qe_execute(plan)
        if response:
            logger.info("query_engine_handled question=%r type=%s", trimmed, plan.query_type)
            return response
        else:
            if plan.execution_error:
                logger.error("query_engine_execution_error question=%r type=%s error=%s",
                             trimmed, plan.query_type, plan.execution_error)
                _ping_qe_error(trimmed, plan.execution_error)
            else:
                logger.warning("query_engine_valid_but_no_result question=%r type=%s streak_len=%s",
                               trimmed, plan.query_type, plan.streak_length)
    elif plan.unexplained_words and (plan.stat or plan.derived_stat):
        logger.info("query_engine_bail question=%r unexplained=%s", trimmed, plan.unexplained_words)
        return None

    # 25. Stat definition — "what is OPS?", "explain BABIP"
    defn = nm.parse_stat_definition(trimmed)
    if defn:
        abbrev = defn["abbrev"]
        display = defn["display_name"]
        definition = defn["definition"]
        stat_name = display if display == abbrev else display.lower()
        response = f"**{abbrev}** — {definition}"
        # Add leaderboard suggestions if stat is queryable
        if nm.match_stat(abbrev):
            response += f"\n\n[SUGGEST]{stat_name} leaders[/SUGGEST]\n[SUGGEST]career {stat_name} leaders[/SUGGEST]"
        return response

    # 25b. Player single-season max — "most HR Aaron ever hit", "career-best OPS"
    # Must run BEFORE the catch-all so "Most HR Hank Aaron ever hit in a season"
    # doesn't get silently routed to a most-recent-season lookup.
    single_season_max = nm.parse_player_single_season_max(trimmed)
    if single_season_max:
        name = single_season_max["name"]
        stat = single_season_max["stat"]
        direction = single_season_max["direction"]
        is_pitching = nm.is_pitcher(name) or nm.is_pitching_stat(stat)
        response = rb.build_player_single_season_max(name, stat, direction=direction, is_pitching=is_pitching)
        if response:
            return response

    # 26. Catch-all — any query with a recognizable player name + stat keyword
    catch_all = nm.parse_catch_all_player_stat(trimmed)
    if catch_all:
        name, stat = catch_all["name"], catch_all["stat"]
        is_career = catch_all["is_career"]
        is_pitching = nm.is_pitcher(name) or nm.is_pitching_stat(stat)
        if is_career:
            if is_pitching:
                response = rb.build_pitching_career_lookup(name, stat)
            else:
                response = rb.build_career_lookup(name, stat)
        else:
            season = catch_all["season"]
            if is_pitching:
                response = rb.build_pitching_single_stat_lookup(name, stat, season)
            else:
                response = rb.build_single_stat_lookup(name, stat, season)
        if response:
            return response

    # No match — fall through to Claude
    return None


def try_intercept(question: str, _no_split: bool = False):
    """Public entry: one full parser pass; on a total miss, the compound
    splitter tries the query as TWO questions joined by and/&/plus (Mark
    2026-09-01: "Nolan Ryan career ERA and innings pitched all-time
    rankings"). Single intents containing "and" are untouched — they claim
    the whole query in the first pass. Wrapper (not end-of-function code)
    because _try_intercept_once has many early return-None exits that
    would otherwise skip the splitter."""
    result = _try_intercept_once(question)
    if _no_split:
        return result
    if result is not None and result != "__DIMS_DROPPED__":
        return result
    _sentinel = result  # None or the guard sentinel; a clean split beats both
    import re as _re
    trimmed = question.strip()
    m = _re.search(r"\s+(?:and|&|plus|;)\s+", trimmed, _re.I)
    if not m:
        return _sentinel
    left, right = trimmed[:m.start()].strip(), trimmed[m.end():].strip()
    if len(left.split()) < 2 or len(right.split()) < 2:
        return _sentinel
    r1 = _try_intercept_once(left)
    if r1 == "__DIMS_DROPPED__" or not r1:
        return _sentinel
    # SUBJECT-FIRST (Mark 2026-09-01): "Luis Gil ERA and innings pitched all
    # time" means GIL'S innings, not the league board — a conjoined fragment
    # inherits the sentence's subject, same convention as follow-up
    # fragments. When both readings answer, the other becomes a SEEALSO
    # (scope-inference convention generalized: the engine's choice among
    # plausible readings always surfaces the road not taken).
    _subj = nm.find_player_in_text(left)
    r2 = None
    _alt_pill = None
    if _subj:
        _subj_q = f"{_subj} {right}"
        r2 = _try_intercept_once(_subj_q)
        if r2 == "__DIMS_DROPPED__":
            r2 = None
        if r2:
            _alt = _try_intercept_once(right)
            if _alt and _alt != "__DIMS_DROPPED__" and _alt != r2:
                _alt_pill = right  # league-wide reading, one tap away
    if not r2:
        r2 = _try_intercept_once(right)
        if r2 == "__DIMS_DROPPED__":
            r2 = None
        if r2 and _subj:
            # standalone won; offer the subject reading if it answers
            _alt_pill = None
    if not r2:
        return _sentinel
    logger.info("compound_split question=%r", trimmed)
    no_count = r1.startswith("__NO_COUNT__") or r2.startswith("__NO_COUNT__")
    r1 = r1.replace("__NO_COUNT__", "", 1)
    r2 = r2.replace("__NO_COUNT__", "", 1)
    combined = r1.rstrip() + "\n\n" + r2.lstrip()
    if _alt_pill:
        combined += f"\n\n[SEEALSO]{_alt_pill}[/SEEALSO]"
    return ("__NO_COUNT__" + combined) if no_count else combined


# QE_ERROR_HC_UUID configured via deploy workflow
