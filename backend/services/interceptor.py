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


def try_intercept(question: str):
    """
    Try to answer the question locally from the DB.
    Returns formatted response text (with [STATGRID], [SUGGEST], etc. tags),
    or None if the query should fall through to Claude.
    """
    trimmed = question.strip()
    if not trimmed:
        return None

    # Statcast queries for current season — graceful rejection, don't count query
    import re
    lower = trimmed.lower()
    _statcast_keywords = ["exit velo", "exit velocity", "launch angle", "barrel rate",
                          "barrels", "sprint speed", "hard hit", "hard-hit",
                          "spin rate", "pitch velocity", "xba", "xslg", "xwoba",
                          "expected batting", "expected slugging", "chase rate",
                          "whiff rate", "sweet spot"]
    if any(kw in lower for kw in _statcast_keywords):
        current_year = date.today().year
        is_current = (str(current_year) in lower or "this season" in lower
                      or "this year" in lower or "2026" in lower
                      or not re.search(r'20[012]\d', lower))  # no year specified = current
        if is_current:
            return ("__NO_COUNT__We don't have Statcast data (exit velocity, launch angle, barrel rate, etc.) "
                    "in our database. We focus on verified game stats — batting, pitching, splits, streaks, "
                    "and matchups."
                    "\n\n[SUGGEST]OPS leaders this season[/SUGGEST]"
                    "\n[SUGGEST]most home runs this season[/SUGGEST]"
                    "\n[SUGGEST]best ERA this season[/SUGGEST]")

    # 0a. Tonight preview — "how will Judge do tonight" (auto-resolve probable pitcher)
    tonight = nm.parse_tonight_preview(trimmed)
    if tonight:
        try:
            from services.daily_games import get_player_team, get_opponent_starter, has_game_today
            player_name = tonight["name"]
            team = get_player_team(player_name)
            if team:
                result = get_opponent_starter(team)
                if result:
                    pitcher_name, _ = result
                    # Match pitcher name against our DB
                    matched_pitcher = nm.match_player(pitcher_name)
                    if matched_pitcher:
                        response = rb.build_matchup(player_name, matched_pitcher)
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
            game_window.get("season"))
        if response:
            return response

    # 1. Comparison — "Judge vs Soto", "Compare Lindor and Witt"
    comp = nm.parse_comparison(trimmed)
    if comp:
        p1, p2 = comp["name1"], comp["name2"]
        season = comp["season"]
        alts = comp.get("alternatives", [])
        see_also = ""
        if alts:
            see_also = f"\n[SEEALSO]{','.join(alts)}[/SEEALSO]"

        if nm.is_pitcher(p1) and nm.is_pitcher(p2):
            response = rb.build_pitching_comparison(p1, p2, season)
        else:
            response = rb.build_comparison(p1, p2, season)
        if response:
            return response + see_also

    # 2. Streak history — "Judge hot streaks 2025", "Ohtani cold streak"
    streak = nm.parse_streak_query(trimmed)
    if streak:
        name, perf, season = streak["name"], streak["performance"], streak["season"]
        if nm.is_pitcher(name):
            response = rb.build_pitching_streak_list(name, perf, season)
        else:
            response = rb.build_streak_list(name, perf, season)
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
    slash = nm.parse_slash_line_lookup(trimmed)
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

    # 7. Platoon splits — "Judge vs lefties"
    platoon = nm.parse_platoon_splits(trimmed)
    if platoon:
        name, hand, season = platoon["name"], platoon["hand"], platoon["season"]
        if nm.is_pitcher(name):
            response = rb.build_pitching_platoon_splits(name, hand, season)
        else:
            response = rb.build_platoon_splits(name, hand, season)
        if response:
            return response

    # 8. Home/away splits — "Judge home vs away"
    home_away = nm.parse_home_away_splits(trimmed)
    if home_away:
        name, loc, season = home_away["name"], home_away["location"], home_away["season"]
        if nm.is_pitcher(name):
            response = rb.build_pitching_home_away_splits(name, loc, season)
        else:
            response = rb.build_home_away_splits(name, loc, season)
        if response:
            return response

    # 9. RISP splits — "Judge with runners in scoring position"
    risp = nm.parse_risp_splits(trimmed)
    if risp:
        name, season = risp["name"], risp["season"]
        if nm.is_pitcher(name):
            response = rb.build_pitching_risp_splits(name, season)
        else:
            response = rb.build_risp_splits(name, season)
        if response:
            return response

    # 10. Pitch type splits — "Judge vs sliders"
    pitch_type = nm.parse_pitch_type_splits(trimmed)
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
    count = nm.parse_count_splits(trimmed)
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
    consec = nm.parse_consecutive_streak(trimmed)
    if consec:
        response = rb.build_consecutive_streak(
            consec["type"], consec.get("player_name"), consec.get("season"))
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
        pitching_context = stat and nm.is_pitching_stat(stat)
        if not pitching_context:
            pitching_context = any(w in lower_q for w in ["pitching", "pitcher", "pitchers", "pitching stats"])
        if pitching_context:
            response = rb.build_pitching_team_stats(
                team_stats["team_code"], stat, team_stats["season"])
        else:
            response = rb.build_team_stats(
                team_stats["team_code"], stat, team_stats["season"])
        if response:
            return response

    # --- Query Engine — the primary stat query handler ---
    # Handles leaderboards, thresholds, counts, superlatives, game-log queries,
    # split leaderboards, team rankings, derived stats, multi-threshold,
    # and any combination of filters (position, bats, age, rookie, pitcher role).
    # Only answers when it fully understands the query (no unexplained words).
    # If it bails, go straight to Haiku/Sonnet — no old parsers.
    from services.query_engine import decompose, execute as qe_execute
    plan = decompose(trimmed)
    logger.info("query_engine_plan question=%r valid=%s scope=%s stat=%s unexplained=%s active=%s since_date=%s",
                trimmed, plan.is_valid, plan.scope, plan.stat, plan.unexplained_words, plan.active_only, plan.since_date)
    if plan.is_valid:
        response = qe_execute(plan)
        if response:
            logger.info("query_engine_handled question=%r type=%s", trimmed, plan.query_type)
            return response
        else:
            logger.warning("query_engine_valid_but_no_result question=%r type=%s streak_len=%s", trimmed, plan.query_type, plan.streak_length)
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
