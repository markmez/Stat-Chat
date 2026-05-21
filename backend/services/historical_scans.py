"""
Historical scan engine for notable events.

Uses the pre-computed `historical_index` table for instant lookups.
The index is built once by `build_historical_index.py` and covers 1920-2025.

Each scan computes a current-season stat, then looks up historical
comparisons from the index.
"""

import sqlite3
from datetime import date
from .qualification import min_pa as _qual_min_pa, min_ip_outs as _qual_min_ip_outs


def _player_name(conn, player_id):
    row = conn.execute("SELECT name FROM players WHERE player_id = ?", (player_id,)).fetchone()
    return row[0] if row else player_id


def _continue_with_context(base: str, continuation: str) -> str:
    """Append a follow-up clause as a separate sentence rather than an
    em-dash continuation. Mirrors the helper in notable_events.py — kept
    local here to avoid a circular import."""
    if not continuation:
        return base
    cont = continuation.strip().rstrip(".!?").strip()
    if not cont:
        return base
    boundary = " " if base.rstrip().endswith(("!", "?", ".")) else ". "
    if cont[0].isupper():
        sentence = cont + "."
    else:
        sentence = "That's " + cont + "."
    return base.rstrip() + boundary + sentence


def _team_display(conn, player_id, season):
    row = conn.execute(
        "SELECT team FROM season_batting_stats WHERE player_id = ? AND season = ?",
        (player_id, season)
    ).fetchone()
    if not row:
        row = conn.execute(
            "SELECT team FROM season_pitching_stats WHERE player_id = ? AND season = ?",
            (player_id, season)
        ).fetchone()
    return row[0] if row and row[0] else ""


def _has_index(conn):
    """Check if historical_index table exists."""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='historical_index'"
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Scan: start-of-season batting streaks
# ---------------------------------------------------------------------------

def scan_start_of_season_streaks(conn, season, latest_date):
    """Find players with streaks from game 1 of the season, with historical context."""
    facts = []

    streak_types = [
        {
            "scan_type": "start_hit_streak",
            "condition": lambda h, bb, hbp, hr: h > 0,
            "min_games": 6,
            "label": "has hit safely in each of the first {n} games this season",
        },
        {
            "scan_type": "start_onbase_streak",
            "condition": lambda h, bb, hbp, hr: (h + bb + hbp) > 0,
            "min_games": 8,
            "label": "has reached base in each of the first {n} games this season",
        },
        {
            "scan_type": "start_multi_hit_streak",
            "condition": lambda h, bb, hbp, hr: h >= 2,
            "min_games": 4,
            "label": "has had multiple hits in each of the first {n} games this season",
        },
        {
            "scan_type": "start_hr_streak",
            "condition": lambda h, bb, hbp, hr: hr > 0,
            "min_games": 3,
            "label": "has homered in each of the first {n} games this season",
        },
    ]

    for stype in streak_types:
        # Only evaluate players who played on latest_date
        players = conn.execute("""
            SELECT DISTINCT player_id FROM game_batting_logs
            WHERE season = ? AND date = ? AND (plate_appearances > 0 OR at_bats > 0)
        """, (season, latest_date)).fetchall()

        for (pid,) in players:
            games = conn.execute("""
                SELECT hits, walks, COALESCE(hit_by_pitch, 0), home_runs
                FROM game_batting_logs
                WHERE player_id = ? AND season = ? AND (plate_appearances > 0 OR at_bats > 0)
                ORDER BY date ASC
            """, (pid, season)).fetchall()

            streak = 0
            for h, bb, hbp, hr in games:
                if stype["condition"](h or 0, bb or 0, hbp or 0, hr or 0):
                    streak += 1
                else:
                    break

            if streak >= stype["min_games"] and streak == len(games):
                name = _player_name(conn, pid)
                team = _team_display(conn, pid, season)

                # Lookup from historical index (exclude current season)
                historical = conn.execute("""
                    SELECT player_name, season, value
                    FROM historical_index
                    WHERE scan_type = ? AND value >= ? AND season < ?
                    ORDER BY season DESC
                """, (stype["scan_type"], streak, season)).fetchall()

                hist_list = [{"player": h[0], "season": h[1], "games": h[2]} for h in historical]

                # Only include if historically rare (≤100 total, or last occurrence was 2+ years ago)
                if len(hist_list) > 100 and hist_list and hist_list[0]["season"] >= season - 1:
                    continue  # Too common and happened recently

                facts.append({
                    "type": stype["scan_type"],
                    "player": name,
                    "player_id": pid,
                    "team": team,
                    "streak": streak,
                    "label": stype["label"].format(n=streak),
                    "historical": hist_list,
                    "historical_count": len(hist_list),
                })

    return facts


# ---------------------------------------------------------------------------
# Scan: cross-season active streaks
# ---------------------------------------------------------------------------

def _compute_active_streak(conn, pid, season, check):
    """Walk a player's game logs backwards to find their current active streak.

    Streak length is measured in games played (not calendar days) — a streak can
    legitimately span off-days and games the player sat. Returns (length, spans_seasons).
    """
    current = conn.execute(f"""
        SELECT ({check['condition_sql']}) as met
        FROM game_batting_logs
        WHERE player_id = ? AND season = ? AND {check['filter']}
        ORDER BY date DESC
    """, (pid, season)).fetchall()

    streak = 0
    for (met,) in current:
        if met:
            streak += 1
        else:
            break

    spans = False
    if streak == len(current) and streak > 0:
        prev = conn.execute(f"""
            SELECT ({check['condition_sql']}) as met
            FROM game_batting_logs
            WHERE player_id = ? AND season = ? AND {check['filter']}
            ORDER BY date DESC
        """, (pid, season - 1)).fetchall()
        for (met,) in prev:
            if met:
                streak += 1
                spans = True
            else:
                break

    return streak, spans


# Rebuild thresholds — if a streak at or above these lengths completes today,
# trigger a refresh of the historical_streaks leaderboard. Chosen so the table
# stays fresh for ranking without rebuilding on trivial streaks.
_REBUILD_TRIGGER_THRESHOLDS = {
    "hitting": 20,   # hitting streak of 20+ ended today
    "on_base": 30,   # on-base streak of 30+ ended today
}


def check_if_historical_streaks_rebuild_needed(conn, season, latest_date):
    """Returns True if any player broke a streak today that's long enough
    to warrant refreshing the historical_streaks leaderboard."""
    checks = [
        ("hitting", "hits > 0",
         "plate_appearances > 0",
         _REBUILD_TRIGGER_THRESHOLDS["hitting"]),
        ("on_base", "(hits + walks + COALESCE(hit_by_pitch, 0)) > 0",
         "(plate_appearances > 0 OR walks > 0 OR COALESCE(hit_by_pitch, 0) > 0)",
         _REBUILD_TRIGGER_THRESHOLDS["on_base"]),
    ]

    today_players = conn.execute("""
        SELECT DISTINCT player_id FROM game_batting_logs
        WHERE date = ? AND season = ?
    """, (latest_date, season)).fetchall()

    for (pid,) in today_players:
        for _kind, cond_sql, filter_sql, threshold in checks:
            # Did today break the condition?
            today_met = conn.execute(f"""
                SELECT ({cond_sql}) FROM game_batting_logs
                WHERE player_id = ? AND date = ? AND {filter_sql}
            """, (pid, latest_date)).fetchone()
            if not today_met or today_met[0]:
                continue  # didn't play today, or met the condition (streak continues)

            # Count streak length ending yesterday
            rows = conn.execute(f"""
                SELECT ({cond_sql}) FROM game_batting_logs
                WHERE player_id = ? AND date < ? AND season = ? AND {filter_sql}
                ORDER BY date DESC
            """, (pid, latest_date, season)).fetchall()
            streak = 0
            for (met,) in rows:
                if met:
                    streak += 1
                else:
                    break
            # Walk into previous season if exhausted
            if streak == len(rows) and streak > 0:
                prev = conn.execute(f"""
                    SELECT ({cond_sql}) FROM game_batting_logs
                    WHERE player_id = ? AND season = ? AND {filter_sql}
                    ORDER BY date DESC
                """, (pid, season - 1)).fetchall()
                for (met,) in prev:
                    if met:
                        streak += 1
                    else:
                        break

            if streak >= threshold:
                return True
    return False


# Iconic career-milestone thresholds. Must stay in sync with
# data_pipeline/build_historic_moments.py — when a crossing happens, the
# historic_moments table goes stale until rebuilt.
_HISTORIC_MOMENT_BATTING_THRESHOLDS = {
    "home_runs": [500, 600, 700],
    "hits":      [3000],
    "rbi":       [2000, 2500, 3000],
    "stolen_bases": [500, 600],
}
_HISTORIC_MOMENT_PITCHING_THRESHOLDS = {
    "strikeouts": [3000, 3500, 4000],
    "wins":       [300, 350],
    "saves":      [400, 500],
}
_HISTORIC_MOMENT_PITCHING_GAME_COL = {
    "wins": "win", "saves": "save", "strikeouts": "strikeouts",
}


def check_if_historic_moments_rebuild_needed(conn, latest_date):
    """Returns True if any player who played today crossed one of the iconic
    career milestone thresholds (500 HR, 3000 hits, 3000 K, etc.) on this
    date. Uses game-log cumulative totals: if career total was below
    threshold before today and is now at or above, rebuild is needed.
    """
    # Players who batted today
    batters = conn.execute("""
        SELECT DISTINCT player_id FROM game_batting_logs WHERE date = ?
    """, (latest_date,)).fetchall()
    for (pid,) in batters:
        for stat, thresholds in _HISTORIC_MOMENT_BATTING_THRESHOLDS.items():
            prior = conn.execute(f"""
                SELECT COALESCE(SUM({stat}), 0) FROM game_batting_logs
                WHERE player_id = ? AND date < ?
            """, (pid, latest_date)).fetchone()[0]
            today_val = conn.execute(f"""
                SELECT COALESCE(SUM({stat}), 0) FROM game_batting_logs
                WHERE player_id = ? AND date = ?
            """, (pid, latest_date)).fetchone()[0]
            current = prior + today_val
            for t in thresholds:
                if prior < t <= current:
                    return True

    # Players who pitched today
    pitchers = conn.execute("""
        SELECT DISTINCT player_id FROM game_pitching_logs WHERE date = ?
    """, (latest_date,)).fetchall()
    for (pid,) in pitchers:
        for stat, thresholds in _HISTORIC_MOMENT_PITCHING_THRESHOLDS.items():
            col = _HISTORIC_MOMENT_PITCHING_GAME_COL[stat]
            prior = conn.execute(f"""
                SELECT COALESCE(SUM({col}), 0) FROM game_pitching_logs
                WHERE player_id = ? AND date < ?
            """, (pid, latest_date)).fetchone()[0]
            today_val = conn.execute(f"""
                SELECT COALESCE(SUM({col}), 0) FROM game_pitching_logs
                WHERE player_id = ? AND date = ?
            """, (pid, latest_date)).fetchone()[0]
            current = prior + today_val
            for t in thresholds:
                if prior < t <= current:
                    return True

    return False


# ---------------------------------------------------------------------------
# Pitching-shape helpers: find the last pitcher to have a consecutive run of
# N+ starts meeting some condition. Uses gaps-and-islands pattern.
# ---------------------------------------------------------------------------

def _find_last_consecutive_start_streak(conn, exclude_pid, season, min_length,
                                         condition_sql, team_codes=None):
    """Return {name, season} for the most recent pitcher (excluding
    exclude_pid) who had a consecutive run of min_length+ starts meeting
    condition_sql within a single season. Iterates seasons backwards.

    condition_sql: SQL fragment evaluated per start (e.g. "earned_runs = 0
    AND ip_outs >= 15" for scoreless, or "strikeouts >= 10" for 10+K).
    team_codes: optional franchise filter; when set, lookback is 100yrs.
    """
    lookback = 101 if team_codes else 26
    tf_join = ""
    tf_where = ""
    tf_params = []
    if team_codes:
        placeholders = " OR ".join(["('/' || ss.team || '/') LIKE ?"] * len(team_codes))
        tf_join = "JOIN season_pitching_stats ss ON ss.player_id = sub.player_id AND ss.season = sub.season"
        tf_where = f"AND ({placeholders})"
        tf_params = [f"%/{c}/%" for c in team_codes]

    for check_season in range(season - 1, season - lookback, -1):
        row = conn.execute(f"""
            SELECT p.name, sub.season
            FROM (
                SELECT player_id, season, run_id, COUNT(*) as streak_len
                FROM (
                    SELECT player_id, season,
                           SUM(CASE WHEN ({condition_sql}) THEN 0 ELSE 1 END)
                               OVER (PARTITION BY player_id ORDER BY date
                                     ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) as run_id,
                           ({condition_sql}) as in_streak
                    FROM game_pitching_logs
                    WHERE is_start = 1 AND season = ?
                ) labeled
                WHERE in_streak = 1
                GROUP BY player_id, season, run_id
                HAVING streak_len >= ?
            ) sub
            JOIN players p ON p.player_id = sub.player_id
            {tf_join}
            WHERE sub.player_id != ?
            {tf_where}
            LIMIT 1
        """, [check_season, min_length, exclude_pid] + tf_params).fetchone()

        if row:
            return {"name": row[0], "season": row[1]}

    return None


def _find_last_opening_streak(conn, exclude_pid, season, min_length,
                               condition_sql, team_codes=None):
    """Return {name, season} for the most recent pitcher who OPENED a season
    with min_length+ starts all meeting condition_sql. Streak must start
    from their first start of the season."""
    lookback = 101 if team_codes else 26
    tf_join = ""
    tf_where = ""
    tf_params = []
    if team_codes:
        placeholders = " OR ".join(["('/' || ss.team || '/') LIKE ?"] * len(team_codes))
        tf_join = "JOIN season_pitching_stats ss ON ss.player_id = sub.player_id AND ss.season = sub.season"
        tf_where = f"AND ({placeholders})"
        tf_params = [f"%/{c}/%" for c in team_codes]

    for check_season in range(season - 1, season - lookback, -1):
        row = conn.execute(f"""
            SELECT p.name, sub.season
            FROM (
                SELECT player_id, season, COUNT(*) as streak_len
                FROM (
                    SELECT player_id, season,
                           ROW_NUMBER() OVER (PARTITION BY player_id ORDER BY date) as start_rank,
                           SUM(CASE WHEN ({condition_sql}) THEN 0 ELSE 1 END)
                               OVER (PARTITION BY player_id ORDER BY date
                                     ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) as broken
                    FROM game_pitching_logs
                    WHERE is_start = 1 AND season = ?
                ) labeled
                WHERE broken = 0  -- no breaks yet (from start of season)
                GROUP BY player_id, season
                HAVING streak_len >= ?
            ) sub
            JOIN players p ON p.player_id = sub.player_id
            {tf_join}
            WHERE sub.player_id != ?
            {tf_where}
            LIMIT 1
        """, [check_season, min_length, exclude_pid] + tf_params).fetchone()

        if row:
            return {"name": row[0], "season": row[1]}

    return None


def _format_ordinal(n):
    """Convert 1 → '1st', 2 → '2nd', 3 → '3rd', 4+ → 'Nth'."""
    if 10 <= n % 100 <= 20:
        return f"{n}th"
    suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def get_streak_historical_context(conn, streak_type, current_length, current_start_date,
                                   player_team=None, latest_date=None):
    """Rank a currently-active streak against the historical_streaks table.

    Returns (phrases, mentioned_player_names). Phrases are the prose for
    embedding in the headline; mentioned_player_names is a parallel list
    so the caller can register them in the event's `player_names` field
    and the iOS feed can render them as tappable links. Fires when a
    streak is:
      (a) Longer than anything in the last year (MLB-wide), AND/OR
      (b) Top 100 in the last 100+ years (MLB-wide), AND/OR
      (c) Team anchor: longest on this franchise in ≥10 years MORE than
          the MLB comparison (or MLB has no match but franchise does).

    player_team: optional team code for the current player — used to
    anchor (c). Without it, team context is skipped.

    Team context rules:
      - Franchise match requires the historical player was cleanly on one
        of this franchise's codes (exact match; excludes slash-separated
        mid-season trades).
      - Single-season streaks only (start_season = end_season) — cross-
        season streaks skip team anchoring.
      - Delta threshold: team_gap − mlb_gap ≥ 10 years, OR MLB empty and
        team gap ≥ 10.
    """
    try:
        conn.execute("SELECT 1 FROM historical_streaks LIMIT 1").fetchone()
    except Exception:
        return [], []

    phrases = []
    mentioned: list[str] = []

    # Human-readable streak noun for the phrasing referent. Without this,
    # "the longest by any player since X's 53" leaves the reader asking
    # "longest WHAT?" — the noun lives in the main headline but the
    # follow-on sentence breaks the antecedent chain.
    _STREAK_NOUN = {"hitting": "hitting streak", "on_base": "on-base streak"}
    noun = _STREAK_NOUN.get(streak_type, "streak")

    # (1) "Longest since X's Y in YEAR" — MLB-wide most recent prior streak.
    #
    # Cutoff is `latest_date` (today) when provided, so we catch any prior
    # streak that has ENDED BY NOW — including ones that overlapped with
    # the start of the current streak (e.g., Ohtani's 53-game on-base
    # streak Aug 2025 → Apr 21 2026 overlapped with Kurtz's Apr 3 2026
    # start; using start_date as the cutoff missed Ohtani entirely and
    # fell back to older shorter streaks). The natural reading of
    # "longest since X" is "no one else has reached this length since X's
    # run ended" — that depends on end_date relative to TODAY, not
    # relative to the current streak's start.
    #
    # When the prior match is significantly longer than the current streak
    # (>=1.5x), the lopsided-drop below removes the MLB-since phrase
    # entirely rather than framing the current streak against something
    # much bigger. The franchise anchor picks up the slack there.
    cutoff = latest_date or current_start_date
    # Require the anchor be a COMPLETED streak so an active/ongoing run is
    # never cited as a finished "since X" prior. A streak is completed if it's
    # from a PRIOR season (history is always completed) OR current-season with
    # a breaking game after end_date. Staleness-proof (reads the game log), and
    # the prior-season carve-out keeps career-end historical streaks (no later
    # games in the data) eligible as anchors instead of misreading them active.
    _cur_season = int((latest_date or current_start_date or "0")[:4] or 0)
    _cond = {"hitting": "hits > 0",
             "on_base": "(hits + walks + COALESCE(hit_by_pitch, 0)) > 0"}.get(streak_type, "hits > 0")
    _filt = {"hitting": "plate_appearances > 0",
             "on_base": "(plate_appearances > 0 OR walks > 0 OR COALESCE(hit_by_pitch, 0) > 0)"}.get(streak_type, "plate_appearances > 0")
    mlb_prior = conn.execute(f"""
        SELECT player_name, length, end_date, end_season
        FROM historical_streaks
        WHERE streak_type = ? AND length >= ? AND end_date < ?
          AND (historical_streaks.end_season != {_cur_season} OR EXISTS (
              SELECT 1 FROM game_batting_logs
              WHERE game_batting_logs.player_id = historical_streaks.player_id
                AND game_batting_logs.date > historical_streaks.end_date
                AND ({_filt}) AND NOT ({_cond})
          ))
        ORDER BY end_date DESC
        LIMIT 1
    """, (streak_type, current_length, cutoff)).fetchone()
    mlb_season = None
    mlb_length_lopsided = False
    if mlb_prior:
        pname, plen, pend, pseason = mlb_prior
        mlb_season = pseason
        mlb_length_lopsided = plen >= current_length * 1.5
        if not mlb_length_lopsided:
            mentioned.append(pname)
            # Same-season anchor reads as "earlier this season" rather than the
            # bare year ("in 2026"), which sounds odd when it's the current year.
            _when = "earlier this season" if pseason == _cur_season else f"in {pseason}"
            if plen == current_length:
                _from = "earlier this season" if pseason == _cur_season else f"from {pseason}"
                phrases.append(f"matching {pname}'s {plen}-game {noun} {_from}")
            else:
                phrases.append(f"the longest {noun} by any player since {pname}'s {plen} {_when}")

    # (2) Nth-longest (MLB-wide)
    longer_count = conn.execute("""
        SELECT COUNT(*) FROM historical_streaks
        WHERE streak_type = ? AND length > ? AND end_date < ?
    """, (streak_type, current_length, current_start_date)).fetchone()[0]
    rank = longer_count + 1
    if rank <= 100:
        phrases.append(f"the {_format_ordinal(rank)}-longest {noun} in the last 100+ years")

    # (3) Team-since anchor: "the longest by a {Franchise} player since X in Y"
    # Only when team history goes meaningfully deeper than MLB (10+ yr delta).
    if player_team:
        try:
            from services.franchise import get_franchise_codes, get_franchise_name
            team_codes = get_franchise_codes(player_team)
            franchise_name = get_franchise_name(player_team)
        except Exception:
            team_codes = []
            franchise_name = None

        if team_codes and franchise_name:
            placeholders = ",".join("?" * len(team_codes))
            team_prior = conn.execute(f"""
                SELECT hs.player_name, hs.length, hs.end_season
                FROM historical_streaks hs
                JOIN season_batting_stats ss
                  ON ss.player_id = hs.player_id AND ss.season = hs.start_season
                WHERE hs.streak_type = ?
                  AND hs.length >= ?
                  AND hs.end_date < ?
                  AND hs.start_season = hs.end_season  -- single-season only
                  AND ss.team IN ({placeholders})  -- exact match; excludes 'NYA/BOS' style
                ORDER BY hs.end_date DESC
                LIMIT 1
            """, [streak_type, current_length, current_start_date] + team_codes).fetchone()

            if team_prior:
                t_name, t_len, t_season = team_prior
                current_year = int(current_start_date[:4])
                team_gap = current_year - t_season
                mlb_gap = (current_year - mlb_season) if mlb_season is not None else 9999
                # Fire team phrase if the delta is meaningful OR MLB had no
                # match and the team gap is substantial on its own
                # Fire team phrase when:
                #  - MLB had no qualifying match, or
                #  - franchise time-gap is meaningfully deeper than MLB, or
                #  - the MLB match length was lopsided (much longer than
                #    current) and got dropped above — franchise anchor then
                #    becomes the primary comparable reference.
                if (
                    (mlb_season is None and team_gap >= 10)
                    or (team_gap - mlb_gap >= 10)
                    or mlb_length_lopsided
                ):
                    mentioned.append(t_name)
                    # "a" / "an" by first-letter vowel sound. Covers the four
                    # vowel-starting MLB franchises (Astros, Angels, Athletics,
                    # Orioles) cleanly; everything else stays "a".
                    article = "an" if franchise_name and franchise_name[0].lower() in "aeio" else "a"
                    _t_when = "earlier this season" if t_season == _cur_season else f"in {t_season}"
                    if t_len == current_length:
                        phrases.append(
                            f"matching {t_name}'s {t_len}-game {noun} as {article} "
                            f"{franchise_name} {_t_when}"
                        )
                    else:
                        phrases.append(
                            f"the longest {noun} by {article} {franchise_name} player "
                            f"since {t_name}'s {t_len} {_t_when}"
                        )

    return phrases, mentioned


def scan_cross_season_streaks(conn, season, latest_date):
    """Find active streaks carrying over from last season.

    Headline claims "longest active in MLB," so we must verify the claim against
    ALL active batters, not just those who played today. Without this guard, on
    days the true #1 holder sits out, a shorter streak from a player who DID play
    would get falsely labeled as longest. Streaks are measured in games played,
    so a player's streak remains active even on days they don't appear.
    """
    facts = []

    checks = [
        {
            "name": "hitting_streak",
            "condition_sql": "hits > 0",
            "filter": "plate_appearances > 0",
            "min_games": 10,
            "label": "hitting streak",
        },
        {
            "name": "on_base_streak",
            "condition_sql": "(hits + walks + COALESCE(hit_by_pitch, 0)) > 0",
            "filter": "(plate_appearances > 0 OR walks > 0 OR COALESCE(hit_by_pitch, 0) > 0)",
            "min_games": 15,
            "label": "on-base streak",
        },
    ]

    for check in checks:
        # Global max across every active batter this season — this is the true
        # "longest active" bar. We check every player regardless of whether they
        # played on latest_date so a sitting #1 holder (e.g., Ohtani resting)
        # doesn't get overlooked, letting a shorter streak masquerade as longest.
        all_pids = conn.execute(f"""
            SELECT DISTINCT player_id FROM game_batting_logs
            WHERE season = ? AND {check['filter']}
        """, (season,)).fetchall()

        global_max = 0
        for (pid,) in all_pids:
            length, _ = _compute_active_streak(conn, pid, season, check)
            if length > global_max:
                global_max = length

        # Only today's players are eligible to fire an event — the feed is
        # about what happened on latest_date. But we gate the "longest active"
        # claim on matching/exceeding the true global max above.
        players = conn.execute(f"""
            SELECT DISTINCT player_id FROM game_batting_logs
            WHERE season = ? AND date = ? AND {check['filter']}
        """, (season, latest_date)).fetchall()

        for (pid,) in players:
            streak, spans = _compute_active_streak(conn, pid, season, check)

            if streak < check["min_games"]:
                continue
            # Suppress false "longest" claims: if someone who sat today has a
            # longer streak, don't fire this event for today's player.
            if streak < global_max:
                continue

            name = _player_name(conn, pid)
            team = _team_display(conn, pid, season)

            # Find the actual start date of the active streak for ranking
            streak_type_key = "hitting" if check["name"] == "hitting_streak" else "on_base"
            start_row = conn.execute(f"""
                SELECT MIN(date) FROM (
                    SELECT date,
                           SUM(CASE WHEN NOT ({check['condition_sql']}) THEN 1 ELSE 0 END)
                               OVER (PARTITION BY player_id ORDER BY date DESC) AS break_group
                    FROM game_batting_logs
                    WHERE player_id = ? AND {check['filter']} AND date <= ?
                ) WHERE break_group = 0
            """, (pid, latest_date)).fetchone()
            streak_start = start_row[0] if start_row and start_row[0] else latest_date

            # Derive player_team for franchise anchor (uses players.team —
            # current team, sufficient for surfacing recent franchise moves).
            pt_row = conn.execute(
                "SELECT team FROM players WHERE player_id = ?", (pid,)
            ).fetchone()
            player_team = None
            if pt_row and pt_row[0]:
                player_team = pt_row[0].split("/")[0].strip() or None
            historical_context, hist_mentioned = get_streak_historical_context(
                conn, streak_type_key, streak, streak_start,
                player_team=player_team, latest_date=latest_date,
            )

            facts.append({
                "type": f"cross_season_{check['name']}",
                "player": name,
                "player_id": pid,
                "team": team,
                "streak": streak,
                "spans_seasons": spans,
                "label": check["label"],
                "streak_type_key": streak_type_key,
                "historical_context": historical_context,
                "historical_mentioned": hist_mentioned,
            })

    # Keep top per type (now guaranteed to match the true global max if emitted)
    by_type = {}
    for f in facts:
        t = f["type"]
        if t not in by_type or f["streak"] > by_type[t]["streak"]:
            by_type[t] = f
    return list(by_type.values())


# ---------------------------------------------------------------------------
# Scan: pitching start-of-season
# ---------------------------------------------------------------------------

def scan_pitching_start_of_season(conn, season, latest_date):
    """Find notable pitching feats in first N starts, with historical lookup.
    Only evaluates pitchers who started on latest_date."""
    facts = []

    # Only look at pitchers who started on latest_date
    starters = conn.execute("""
        SELECT player_id, COUNT(*) as starts
        FROM game_pitching_logs
        WHERE season = ? AND is_start = 1
        GROUP BY player_id
        HAVING starts >= 2
        AND player_id IN (
            SELECT DISTINCT player_id FROM game_pitching_logs
            WHERE season = ? AND is_start = 1 AND date = ?
        )
    """, (season, season, latest_date)).fetchall()

    for pid, num_starts in starters:
        starts = conn.execute("""
            SELECT strikeouts, walks, earned_runs, ip_outs, hits, date
            FROM game_pitching_logs
            WHERE player_id = ? AND season = ? AND is_start = 1
            ORDER BY date ASC
        """, (pid, season)).fetchall()

        name = _player_name(conn, pid)
        team = _team_display(conn, pid, season)

        # Check first 2 starts: 15+ K and 0 BB
        # Only fire when the player has exactly 2 starts (their 2nd start just happened on latest_date)
        if num_starts == 2:
            first_2 = starts[:2]
            k2 = sum(s[0] or 0 for s in first_2)
            bb2 = sum(s[1] or 0 for s in first_2)
            if k2 >= 15 and bb2 == 0:
                # Lookup from index (exclude current season, dedup by player+season)
                historical = conn.execute("""
                    SELECT DISTINCT player_name, season, value as k
                    FROM historical_index
                    WHERE scan_type = 'pitcher_first_2_starts'
                    AND value >= 15 AND value2 = 0 AND season < ?
                    ORDER BY season DESC
                """, (season,)).fetchall()
                hist_list = [{"player": h[0], "season": h[1], "k": h[2]} for h in historical]

                facts.append({
                    "type": "10k_0bb_first_2_starts",
                    "player": name,
                    "player_id": pid,
                    "team": team,
                    "k": k2,
                    "starts": 2,
                    "historical": hist_list,
                })

        # All starts scoreless with 5+ IP — only fire if latest start was also scoreless
        all_scoreless = all((s[2] or 0) == 0 and (s[3] or 0) >= 15 for s in starts)
        latest_scoreless = starts and (starts[-1][2] or 0) == 0 and (starts[-1][3] or 0) >= 15
        if all_scoreless and latest_scoreless and num_starts >= 2:
            total_outs = sum(s[3] or 0 for s in starts)
            total_k = sum(s[0] or 0 for s in starts)
            ip = f"{total_outs // 3}.{total_outs % 3}"

            # Historical context: when did anyone last open a season with N+
            # consecutive scoreless starts? Include team-since if deeper.
            mlb_open = _find_last_opening_streak(
                conn, pid, season, num_starts,
                "earned_runs = 0 AND ip_outs >= 15",
            )
            team_open = None
            team_codes = []
            franchise_display_name = None
            try:
                t_row = conn.execute(
                    "SELECT team FROM players WHERE player_id = ?", (pid,)
                ).fetchone()
                if t_row and t_row[0]:
                    primary = t_row[0].split("/")[0].strip()
                    from services.franchise import get_franchise_codes, get_franchise_name
                    team_codes = get_franchise_codes(primary)
                    franchise_display_name = get_franchise_name(primary)
                    team_open = _find_last_opening_streak(
                        conn, pid, season, num_starts,
                        "earned_runs = 0 AND ip_outs >= 15",
                        team_codes=team_codes,
                    )
            except Exception:
                pass

            facts.append({
                "type": "scoreless_first_n_starts",
                "player": name,
                "player_id": pid,
                "team": team,
                "starts": num_starts,
                "ip": ip,
                "k": total_k,
                "mlb_match": mlb_open,
                "team_match": team_open,
                "franchise_name": franchise_display_name,
            })

    return facts


# ---------------------------------------------------------------------------
# Scan: team historical
# ---------------------------------------------------------------------------

def scan_team_historical(conn, season, latest_date):
    """Find team-level historical facts using pre-computed index."""
    facts = []

    # Current season team ER through N games
    teams = conn.execute("""
        SELECT s.team, COUNT(DISTINCT g.date) as games, SUM(g.earned_runs) as total_er
        FROM game_pitching_logs g
        JOIN season_pitching_stats s ON g.player_id = s.player_id AND g.season = s.season
        WHERE g.season = ?
        GROUP BY s.team
        HAVING games >= 5
    """, (season,)).fetchall()

    for team, games, team_er in teams:
        if team_er is None:
            continue

        # Find closest game count in the index
        index_game_count = None
        for gc in [5, 6, 7, 8, 10, 15, 20]:
            if gc <= games:
                index_game_count = gc

        if not index_game_count:
            continue

        # How many teams historically had fewer ER through this many games?
        fewer = conn.execute("""
            SELECT team, season, value as er
            FROM historical_index
            WHERE scan_type = ? AND value < ? AND season < ?
            ORDER BY value ASC
        """, (f"team_er_through_{index_game_count}", team_er, season)).fetchall()

        if len(fewer) <= 10:
            rank = len(fewer) + 1
            hist_list = [{"team": h[0], "season": h[1], "er": h[2]} for h in fewer[:5]]
            facts.append({
                "type": "team_fewest_er",
                "team": team,
                "er": team_er,
                "games": index_game_count,
                "rank": rank,
                "historical": hist_list,
            })

    return facts


# ---------------------------------------------------------------------------
# Scan: team starter ERA
# ---------------------------------------------------------------------------

def scan_team_starter_era(conn, season, latest_date):
    """Find teams with historically low starter ERA through N games."""
    facts = []

    # Current season: compute starter ERA per team through latest_date
    teams = conn.execute("""
        SELECT s.team, COUNT(DISTINCT g.date) as games,
               SUM(g.earned_runs) as total_er, SUM(g.ip_outs) as total_outs
        FROM game_pitching_logs g
        JOIN season_pitching_stats s ON g.player_id = s.player_id AND g.season = s.season
        WHERE g.season = ? AND g.is_start = 1 AND g.date <= ?
        GROUP BY s.team
        HAVING games >= 5
    """, (season, latest_date)).fetchall()

    for team, games, total_er, total_outs in teams:
        if not total_outs or total_outs == 0:
            continue

        era = total_er * 9.0 / (total_outs / 3.0)
        era_x100 = int(round(total_er * 2700 / total_outs))

        # Find closest game count in index
        index_game_count = None
        for gc in [5, 6, 7, 8, 10, 15, 20]:
            if gc <= games:
                index_game_count = gc
        if not index_game_count:
            continue

        # How many teams historically had a LOWER starter ERA through this many games?
        lower = conn.execute("""
            SELECT team, season, value as era_x100, detail
            FROM historical_index
            WHERE scan_type = ? AND value < ? AND season < ?
            ORDER BY value ASC
        """, (f"team_starter_era_through_{index_game_count}", era_x100, season)).fetchall()

        rank = len(lower) + 1

        if rank <= 10:
            hist_list = [{"team": h[0], "season": h[1], "era_x100": h[2], "detail": h[3]} for h in lower[:5]]
            ip_display = f"{total_outs // 3}.{total_outs % 3}"
            facts.append({
                "type": "team_starter_era",
                "team": team,
                "era": era,
                "er": total_er,
                "ip": ip_display,
                "games": index_game_count,
                "rank": rank,
                "historical": hist_list,
            })

    return facts


# ---------------------------------------------------------------------------
# Scan: career-start milestones ("most HR in first N career games")
# ---------------------------------------------------------------------------

def scan_career_start(conn, season, latest_date):
    """Find players early in their careers who just set or approached records
    in 'most X in first N career games.'

    Only surfaces when the player's game on latest_date contributed to the stat
    (homered → show HR rank, doubled → show doubles rank, etc.).
    Checks every career game through 30.
    """
    facts = []

    # Get all current-season players with ≤30 career games
    # who played on latest_date
    active = conn.execute("""
        SELECT g.player_id,
               MAX(CASE WHEN g.date = ? THEN g.home_runs ELSE 0 END) as g_hr,
               MAX(CASE WHEN g.date = ? THEN g.hits ELSE 0 END) as g_hits,
               MAX(CASE WHEN g.date = ? THEN g.rbi ELSE 0 END) as g_rbi,
               MAX(CASE WHEN g.date = ? THEN g.doubles ELSE 0 END) as g_doubles,
               MAX(CASE WHEN g.date = ? THEN g.triples ELSE 0 END) as g_triples
        FROM game_batting_logs g
        WHERE g.season = ?
        GROUP BY g.player_id
        HAVING COUNT(*) <= 30
        AND player_id IN (
            SELECT DISTINCT player_id FROM game_batting_logs
            WHERE season = ? AND date = ?
        )
    """, (latest_date, latest_date, latest_date, latest_date, latest_date, season, season, latest_date)).fetchall()

    for pid, g_hr, g_hits, g_rbi, g_doubles, g_triples in active:
        # Count total career games
        career_count = conn.execute("""
            SELECT COUNT(*) FROM game_batting_logs
            WHERE player_id = ? 
        """, (pid,)).fetchone()[0]

        if career_count > 30:
            continue

        # Compute cumulative stats through their full career
        first_n = conn.execute("""
            SELECT hits, home_runs, rbi, doubles, triples
            FROM game_batting_logs
            WHERE player_id = ? 
            ORDER BY date ASC
            LIMIT ?
        """, (pid, career_count)).fetchall()

        cum_hr = sum(r[1] or 0 for r in first_n)
        cum_rbi = sum(r[2] or 0 for r in first_n)
        cum_xbh = sum((r[3] or 0) + (r[4] or 0) + (r[1] or 0) for r in first_n)
        cum_hits = sum(r[0] or 0 for r in first_n)
        cum_doubles = sum(r[3] or 0 for r in first_n)
        cum_triples = sum(r[4] or 0 for r in first_n)

        name = _player_name(conn, pid)
        team = _team_display(conn, pid, season)

        # Only check stats where the player contributed TODAY
        # (homered → check HR, doubled → check doubles, etc.)
        stats_to_check = []
        if (g_hr or 0) > 0:
            stats_to_check.append(("hr", cum_hr, "home runs"))
        if (g_doubles or 0) > 0:
            stats_to_check.append(("doubles", cum_doubles, "doubles"))
        if (g_triples or 0) > 0:
            stats_to_check.append(("triples", cum_triples, "triples"))
        if (g_rbi or 0) > 0:
            stats_to_check.append(("rbi", cum_rbi, "RBI"))
        if (g_hits or 0) > 0:
            stats_to_check.append(("hits", cum_hits, "hits"))
        if (g_hr or 0) + (g_doubles or 0) + (g_triples or 0) > 0:
            stats_to_check.append(("xbh", cum_xbh, "extra-base hits"))

        for stat_name, cum_val, label in stats_to_check:
            if cum_val == 0:
                continue

            scan_type = f"career_first_{career_count}_{stat_name}"

            # Check if this scan type exists in the index
            exists = conn.execute("""
                SELECT COUNT(*) FROM historical_index WHERE scan_type = ?
            """, (scan_type,)).fetchone()[0]
            if exists == 0:
                continue

            # How many players historically had MORE than this?
            more = conn.execute("""
                SELECT COUNT(DISTINCT player_id) FROM historical_index
                WHERE scan_type = ? AND value > ?
            """, (scan_type, cum_val)).fetchone()[0]

            rank = more + 1

            # Only notable if top 5 all-time
            if rank > 5:
                continue

            # Get the players they just passed or tied
            passed = conn.execute("""
                SELECT DISTINCT player_name, season, value
                FROM historical_index
                WHERE scan_type = ? AND value = ? AND player_id != ?
                ORDER BY season DESC
                LIMIT 5
            """, (scan_type, cum_val, pid)).fetchall()

            # Get who's still ahead
            ahead = conn.execute("""
                SELECT DISTINCT player_name, season, value
                FROM historical_index
                WHERE scan_type = ? AND value > ?
                ORDER BY value DESC
                LIMIT 3
            """, (scan_type, cum_val)).fetchall()

            facts.append({
                "type": "career_start",
                "player": name,
                "player_id": pid,
                "team": team,
                "stat": stat_name,
                "stat_label": label,
                "value": cum_val,
                "career_games": career_count,
                "rank": rank,
                "tied_with": [{"player": p[0], "season": p[1], "value": p[2]} for p in passed],
                "ahead": [{"player": p[0], "season": p[1], "value": p[2]} for p in ahead],
            })

    return facts


def scan_debut_youngest(conn, season, latest_date):
    """Find players whose debut was on the latest date and check if they're
    the youngest to achieve their debut stat line."""
    facts = []

    # Find players whose first-ever game was on the latest date
    debuts = conn.execute("""
        SELECT g.player_id, g.hits, g.home_runs, g.rbi, g.doubles, g.triples
        FROM game_batting_logs g
        WHERE g.date = ? AND g.season = ? 
        AND NOT EXISTS (
            SELECT 1 FROM game_batting_logs g2
            WHERE g2.player_id = g.player_id AND g2.date < g.date
        )
    """, (latest_date, season)).fetchall()

    for pid, h, hr, rbi, d, t in debuts:
        xbh = (d or 0) + (t or 0) + (hr or 0)
        if xbh == 0 and (rbi or 0) == 0:
            continue  # Unremarkable debut

        name = _player_name(conn, pid)
        team = _team_display(conn, pid, season)

        # Get this player's age in days at debut
        age_row = conn.execute("""
            SELECT p.birthdate FROM players p WHERE p.player_id = ?
        """, (pid,)).fetchone()
        if not age_row or not age_row[0]:
            continue

        try:
            from datetime import datetime
            birth = datetime.strptime(age_row[0], "%Y-%m-%d")
            debut = datetime.strptime(latest_date, "%Y-%m-%d")
            age_days = (debut - birth).days
            age_years = age_days // 365
        except:
            continue

        # Check: youngest with XBH + RBI in debut
        if xbh > 0 and (rbi or 0) > 0:
            younger = conn.execute("""
                SELECT COUNT(*) FROM historical_index
                WHERE scan_type = 'debut_xbh_rbi' AND value < ?
            """, (age_days,)).fetchone()[0]

            if younger == 0:
                # This is THE youngest ever
                prev_youngest = conn.execute("""
                    SELECT player_name, season, value, detail
                    FROM historical_index
                    WHERE scan_type = 'debut_xbh_rbi'
                    ORDER BY value ASC
                    LIMIT 1
                """).fetchone()
                facts.append({
                    "type": "youngest_debut",
                    "player": name,
                    "player_id": pid,
                    "team": team,
                    "age_years": age_years,
                    "xbh": xbh,
                    "rbi": rbi or 0,
                    "hr": hr or 0,
                    "previous_youngest": {
                        "player": prev_youngest[0], "season": prev_youngest[1]
                    } if prev_youngest else None,
                })
            else:
                # Check if youngest since a notable year
                younger_recent = conn.execute("""
                    SELECT player_name, season FROM historical_index
                    WHERE scan_type = 'debut_xbh_rbi' AND value < ? AND season > ?
                    ORDER BY value ASC LIMIT 1
                """, (age_days, season - 30)).fetchone()

                if not younger_recent:
                    # Youngest in 30+ years
                    last_younger = conn.execute("""
                        SELECT player_name, season FROM historical_index
                        WHERE scan_type = 'debut_xbh_rbi' AND value < ?
                        ORDER BY season DESC LIMIT 1
                    """, (age_days,)).fetchone()

                    if last_younger:
                        facts.append({
                            "type": "youngest_debut_since",
                            "player": name,
                            "player_id": pid,
                            "team": team,
                            "age_years": age_years,
                            "xbh": xbh,
                            "rbi": rbi or 0,
                            "last_younger": {
                                "player": last_younger[0], "season": last_younger[1]
                            },
                        })

    return facts


# ---------------------------------------------------------------------------
# Scan: leaderboard changes (took the lead in a stat)
# ---------------------------------------------------------------------------

AL_TEAMS = {"NYA", "BOS", "TOR", "BAL", "TBA",
            "CLE", "CHA", "MIN", "DET", "KCA",
            "HOU", "SEA", "ANA", "TEX", "OAK", "ATH"}  # ATH = MSF code for Oakland
NL_TEAMS = {"NYN", "PHI", "ATL", "MIA", "WAS",
            "CHN", "MIL", "SLN", "PIT", "CIN",
            "LAN", "SDN", "SFN", "ARI", "COL"}


def _league_for_team(team):
    if team in AL_TEAMS:
        return "AL"
    if team in NL_TEAMS:
        return "NL"
    return None


def scan_leaderboard_changes(conn, season, latest_date):
    """Find players who took the lead in a stat leaderboard (MLB, AL, or NL)
    as a result of their most recent game."""
    facts = []

    # Use latest_date directly — only attribute events to players who played today

    # Batting stats: (season_col, abbrev, label, min_val, game_log_col)
    # game_log_col is used to check if the player contributed to this stat today
    bat_stats = [
        ("home_runs", "HR", "home runs", 3, "home_runs"),
        ("rbi", "RBI", "RBI", 5, "rbi"),
        ("hits", "hits", "hits", 10, "hits"),
        ("stolen_bases", "SB", "stolen bases", 3, "stolen_bases"),
        ("batting_avg", "AVG", "batting average", None, "hits"),
        ("obp", "OBP", "OBP", None, "hits"),
        ("ops", "OPS", "OPS", None, "hits"),
        ("slg", "SLG", "slugging", None, "hits"),
    ]

    min_pa_rate = _qual_min_pa(conn, season)

    for col, abbrev, label, min_val, game_col in bat_stats:
        is_rate = col in ("batting_avg", "obp", "ops", "slg")
        pa_filter = f"AND s.plate_appearances >= {min_pa_rate}" if is_rate else ""
        val_filter = f"AND s.{col} >= {min_val}" if min_val else ""

        # Get top 3 for MLB, AL, NL
        for scope, team_filter in [
            ("MLB", ""),
            ("AL", f"AND s.team IN ({','.join(repr(t) for t in AL_TEAMS)})"),
            ("NL", f"AND s.team IN ({','.join(repr(t) for t in NL_TEAMS)})"),
        ]:
            rows = conn.execute(f"""
                SELECT p.player_id, p.name, s.{col}, s.team
                FROM season_batting_stats s
                JOIN players p ON s.player_id = p.player_id
                WHERE s.season = ? {pa_filter} {val_filter} {team_filter}
                ORDER BY s.{col} DESC
                LIMIT 3
            """, (season,)).fetchall()

            if len(rows) < 2:
                continue

            leader_pid, leader_name, leader_val, leader_team = rows[0]
            runner_up_pid, runner_up_name, runner_up_val, runner_up_team = rows[1]

            if leader_val is None or runner_up_val is None:
                continue

            # Did the leader play on latest_date? Required for all stats.
            played = conn.execute("""
                SELECT COUNT(*) FROM game_batting_logs
                WHERE player_id = ? AND season = ? AND date = ?
            """, (leader_pid, season, latest_date)).fetchone()[0]
            if not played:
                continue

            # Get the leader's game contribution today
            game_contribution = 0
            if game_col:
                game = conn.execute(f"""
                    SELECT {game_col}
                    FROM game_batting_logs
                    WHERE player_id = ? AND season = ? AND date = ?
                """, (leader_pid, season, latest_date)).fetchone()
                game_contribution = game[0] if game and game[0] else 0
            else:
                # No game log column (e.g., stolen_bases) — just check if they played
                game_contribution = 1

            # For counting stats: did they JUST take the lead or tie?
            if not is_rate:
                margin = leader_val - runner_up_val
                if margin > 0 and game_contribution > 0 and margin <= game_contribution:
                    # Took sole lead
                    team = leader_team
                    league = _league_for_team(team)

                    if scope == "MLB":
                        league_rows = conn.execute(f"""
                            SELECT p.player_id
                            FROM season_batting_stats s
                            JOIN players p ON s.player_id = p.player_id
                            WHERE s.season = ? {val_filter}
                            AND s.team IN ({','.join(repr(t) for t in (AL_TEAMS if league == 'AL' else NL_TEAMS))})
                            ORDER BY s.{col} DESC LIMIT 1
                        """, (season,)).fetchone()
                        if league_rows and league_rows[0] == leader_pid:
                            continue

                    facts.append({
                        "type": "leaderboard_change",
                        "player": leader_name,
                        "player_id": leader_pid,
                        "team": team,
                        "stat": col,
                        "stat_label": label,
                        "stat_abbrev": abbrev,
                        "value": leader_val,
                        "scope": scope,
                        "runner_up": runner_up_name,
                        "runner_up_val": runner_up_val,
                    })
                elif margin == 0:
                    # Tie — check which player(s) just joined the tie today
                    for tied_pid, tied_name, tied_val, tied_team in rows:
                        if tied_val != leader_val:
                            break
                        # Did this player play today and did their contribution create the tie?
                        tied_game = None
                        if game_col:
                            tied_game = conn.execute(f"""
                                SELECT {game_col} FROM game_batting_logs
                                WHERE player_id = ? AND season = ? AND date = ?
                            """, (tied_pid, season, latest_date)).fetchone()
                        tied_contribution = tied_game[0] if tied_game and tied_game[0] else 0
                        if tied_contribution > 0 and tied_val - tied_contribution < leader_val:
                            # This player just tied up — find who they tied
                            other = [n for p, n, v, t in rows if v == leader_val and p != tied_pid]
                            if other:
                                facts.append({
                                    "type": "leaderboard_tie",
                                    "player": tied_name,
                                    "player_id": tied_pid,
                                    "team": tied_team,
                                    "stat": col,
                                    "stat_label": label,
                                    "stat_abbrev": abbrev,
                                    "value": tied_val,
                                    "scope": scope,
                                    "tied_with": other[0],
                                })
                            break  # Only report one tie per stat/scope

            # For rate stats: leader must have played today (no inaction events)
            else:
                if leader_val <= runner_up_val:
                    continue

                team = leader_team
                league = _league_for_team(team)

                if scope == "MLB":
                    league_rows = conn.execute(f"""
                        SELECT p.player_id
                        FROM season_batting_stats s
                        JOIN players p ON s.player_id = p.player_id
                        WHERE s.season = ? {pa_filter}
                        AND s.team IN ({','.join(repr(t) for t in (AL_TEAMS if league == 'AL' else NL_TEAMS))})
                        ORDER BY s.{col} DESC LIMIT 1
                    """, (season,)).fetchone()
                    if league_rows and league_rows[0] == leader_pid:
                        continue

                facts.append({
                    "type": "leaderboard_change",
                    "player": leader_name,
                    "player_id": leader_pid,
                    "team": leader_team,
                    "stat": col,
                    "stat_label": label,
                    "stat_abbrev": abbrev,
                    "value": leader_val,
                    "scope": scope,
                    "runner_up": runner_up_name,
                    "runner_up_val": runner_up_val,
                })
    # Pitching leaderboard changes
    pitch_stats = [
        ("strikeouts", "K", "strikeouts", 5, "strikeouts"),
        ("wins", "W", "wins", 2, "win"),
        ("saves", "SV", "saves", 2, "save"),
        ("era", "ERA", "ERA", None, "earned_runs"),
        ("whip", "WHIP", "WHIP", None, "walks"),
    ]

    min_ip_outs_val = _qual_min_ip_outs(conn, season)

    for col, abbrev, label, min_val, game_col in pitch_stats:
        is_rate = col in ("era", "whip")
        ip_filter = f"AND sp.ip_outs >= {min_ip_outs_val}" if is_rate else ""
        val_filter = f"AND sp.{col} >= {min_val}" if min_val else ""
        order = "ASC" if col in ("era", "whip") else "DESC"

        for scope, team_filter in [
            ("MLB", ""),
            ("AL", f"AND sp.team IN ({','.join(repr(t) for t in AL_TEAMS)})"),
            ("NL", f"AND sp.team IN ({','.join(repr(t) for t in NL_TEAMS)})"),
        ]:
            rows = conn.execute(f"""
                SELECT p.player_id, p.name, sp.{col}, sp.team
                FROM season_pitching_stats sp
                JOIN players p ON sp.player_id = p.player_id
                WHERE sp.season = ? {ip_filter} {val_filter} {team_filter}
                ORDER BY sp.{col} {order}
                LIMIT 3
            """, (season,)).fetchall()

            if len(rows) < 2:
                continue

            leader_pid, leader_name, leader_val, leader_team = rows[0]
            runner_up_pid, runner_up_name, runner_up_val, runner_up_team = rows[1]

            if leader_val is None or runner_up_val is None:
                continue

            # Did the leader pitch on latest_date? Required for all stats.
            played = conn.execute("""
                SELECT COUNT(*) FROM game_pitching_logs
                WHERE player_id = ? AND season = ? AND date = ?
            """, (leader_pid, season, latest_date)).fetchone()[0]
            if not played:
                continue

            # Get the leader's game contribution today
            game_contribution = 0
            if game_col:
                game = conn.execute(f"""
                    SELECT {game_col}
                    FROM game_pitching_logs
                    WHERE player_id = ? AND season = ? AND date = ?
                """, (leader_pid, season, latest_date)).fetchone()
                game_contribution = game[0] if game and game[0] else 0
            else:
                game_contribution = 1

            if not is_rate:
                margin = leader_val - runner_up_val
                if game_contribution > 0 and margin <= game_contribution and margin > 0:
                    team = leader_team
                    league = _league_for_team(team)

                    if scope == "MLB":
                        league_rows = conn.execute(f"""
                            SELECT p.player_id
                            FROM season_pitching_stats sp
                            JOIN players p ON sp.player_id = p.player_id
                            WHERE sp.season = ? {val_filter}
                            AND sp.team IN ({','.join(repr(t) for t in (AL_TEAMS if league == 'AL' else NL_TEAMS))})
                            ORDER BY sp.{col} {order} LIMIT 1
                        """, (season,)).fetchone()
                        if league_rows and league_rows[0] == leader_pid:
                            continue

                    facts.append({
                        "type": "leaderboard_change",
                        "player": leader_name,
                        "player_id": leader_pid,
                        "team": team,
                        "stat": col,
                        "stat_label": label,
                        "stat_abbrev": abbrev,
                        "value": leader_val,
                        "runner_up": runner_up_name,
                        "runner_up_val": runner_up_val,
                        "scope": scope,
                        "is_pitching": True,
                    })
            else:
                # Rate stat: leader must have played today (already checked above)
                facts.append({
                    "type": "leaderboard_change",
                    "player": leader_name,
                    "player_id": leader_pid,
                    "team": leader_team,
                    "stat": col,
                    "stat_label": label,
                    "stat_abbrev": abbrev,
                    "value": leader_val,
                    "runner_up": runner_up_name,
                    "runner_up_val": runner_up_val,
                    "scope": scope,
                    "is_pitching": True,
                })

    return facts


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_all_scans(conn, season, latest_date):
    """Run all historical scans using pre-computed index."""
    if not _has_index(conn):
        print("  WARNING: historical_index table not found. Run build_historical_index.py first.")
        return []

    all_facts = []
    print("  Running historical scans (using index)...")

    facts = scan_start_of_season_streaks(conn, season, latest_date)
    print(f"    Start-of-season streaks: {len(facts)}")
    all_facts.extend(facts)

    facts = scan_cross_season_streaks(conn, season, latest_date)
    print(f"    Cross-season streaks: {len(facts)}")
    all_facts.extend(facts)

    facts = scan_pitching_start_of_season(conn, season, latest_date)
    print(f"    Pitching start-of-season: {len(facts)}")
    all_facts.extend(facts)

    facts = scan_team_historical(conn, season, latest_date)
    print(f"    Team historical: {len(facts)}")
    all_facts.extend(facts)

    facts = scan_team_starter_era(conn, season, latest_date)
    print(f"    Team starter ERA: {len(facts)}")
    all_facts.extend(facts)

    facts = scan_career_start(conn, season, latest_date)
    print(f"    Career-start milestones: {len(facts)}")
    all_facts.extend(facts)

    facts = scan_debut_youngest(conn, season, latest_date)
    print(f"    Debut youngest: {len(facts)}")
    all_facts.extend(facts)

    facts = scan_leaderboard_changes(conn, season, latest_date)
    print(f"    Leaderboard changes: {len(facts)}")
    all_facts.extend(facts)

    print(f"  Total facts: {len(all_facts)}")
    return all_facts


def _get_game_line(conn, player_id, date, season):
    """Get a player's batting or pitching line from a specific game.
    Returns pitching line for starters, batting line otherwise."""
    # Check pitching first (starters get pitching line)
    pitch_row = conn.execute("""
        SELECT innings_pitched, ip_outs, hits, earned_runs, strikeouts, walks, win, is_start
        FROM game_pitching_logs
        WHERE player_id = ? AND date = ? AND season = ?
    """, (player_id, date, season)).fetchone()
    if pitch_row and pitch_row[7]:  # is_start
        ip, outs, h, er, so, bb, w, _ = pitch_row
        ip_display = ip or f"{(outs or 0) // 3}.{(outs or 0) % 3}"
        parts = [f"{ip_display} IP", f"{h} H", f"{er} ER", f"{so} K"]
        if bb == 0: parts.append("0 BB")
        if w: parts.append("W")
        return ", ".join(parts), "pitching"

    # Batting line
    row = conn.execute("""
        SELECT hits, at_bats, home_runs, rbi, doubles, triples, walks,
               stolen_bases, COALESCE(hit_by_pitch, 0)
        FROM game_batting_logs
        WHERE player_id = ? AND date = ? AND season = ?
    """, (player_id, date, season)).fetchone()
    if row and (row[0] or 0) + (row[6] or 0) + (row[8] or 0) > 0:
        h, ab, hr, rbi, d, t, bb, sb, hbp = row
        sb = sb or 0
        hbp = hbp or 0
        base = f"{h}-for-{ab}"
        extras = []
        if hr: extras.append(f"{'a homer' if hr == 1 else f'{hr} HR'}")
        if d: extras.append(f"{d} 2B")
        if t: extras.append(f"{t} 3B")
        if sb: extras.append(f"{'a stolen base' if sb == 1 else f'{sb} SB'}")
        if rbi: extras.append(f"{rbi} RBI")
        if bb and h == 0: extras.append(f"{bb} BB")
        if hbp and h == 0 and bb == 0: extras.append("a HBP")
        if not extras:
            return base, "batting"
        elif len(extras) == 1:
            return f"{base} with {extras[0]}", "batting"
        else:
            return f"{base} with {', '.join(extras[:-1])} and {extras[-1]}", "batting"

    return None, None


def template_facts(conn, facts, season, latest_date):
    """Convert structured facts into templated feed-ready text.
    No Sonnet needed — deterministic copy from DB facts.
    Deduplicates by player — keeps the most historically interesting fact per player."""
    events = []
    seen_players = set()  # Track player_ids already in events

    # Sort facts so historically rarer ones come first (lower hist count = rarer)
    def _rarity_score(f):
        hist = f.get("historical", [])
        count = f.get("historical_count", len(hist))
        if count == 0: return 0  # Unique in history — most rare
        return count
    facts = sorted(facts, key=_rarity_score)

    for f in facts:
        # Dedup: skip if we already have an event for this player
        pid = f.get("player_id")
        if pid and pid in seen_players:
            continue

        # Get the triggering game line (most recent game, not necessarily latest_date)
        game_line, line_type = None, None
        if f.get("player_id"):
            # Find the player's most recent game date (check both batting and pitching)
            bat_date = conn.execute("""
                SELECT MAX(date) FROM game_batting_logs
                WHERE player_id = ? AND season = ?
            """, (f["player_id"], season)).fetchone()
            pitch_date = conn.execute("""
                SELECT MAX(date) FROM game_pitching_logs
                WHERE player_id = ? AND season = ?
            """, (f["player_id"], season)).fetchone()
            bat_d = bat_date[0] if bat_date and bat_date[0] else ""
            pitch_d = pitch_date[0] if pitch_date and pitch_date[0] else ""
            player_last_date = max(bat_d, pitch_d) or latest_date
            game_line, line_type = _get_game_line(conn, f["player_id"], player_last_date, season)

        player = f.get("player", "")
        team = f.get("team", "")
        hist = f.get("historical", [])
        hist_count = f.get("historical_count", len(hist))
        secondary_names = []  # Players mentioned in historical context

        if f["type"].startswith("start_") and f["type"] != "start_onbase_streak":
            # Batting start-of-season streak
            streak = f["streak"]
            label = f["label"]
            if hist_count == 0:
                context = f"no other player has done this in over 100 years"
            elif hist_count <= 10:
                last = hist[0]
                context = f"only {hist_count} players have done this in over 100 years, the last being {last['player']} in {last['season']}"
                secondary_names.append(last["player"])
            else:
                last = hist[0]
                context = f"the last player to do this was {last['player']} in {last['season']}"
                secondary_names.append(last["player"])

            if game_line:
                headline = _continue_with_context(
                    f"{player} went {game_line}, and {label}", context
                )
            else:
                headline = _continue_with_context(f"{player} {label}", context)

        elif f["type"].startswith("cross_season_"):
            streak = f["streak"]
            label = f["label"]
            ctx = "dating back to last season" if f["spans_seasons"] else "this season"

            # Register secondary players mentioned in the historical phrases
            # so iOS renders them as tappable links. Without this, "the longest
            # by any player since Aaron Judge in 2017" leaves "Aaron Judge"
            # as plain text in the feed.
            secondary_names.extend(f.get("historical_mentioned") or [])

            # Historical ranking context. Use a period boundary + "That's" lead-in
            # rather than an em-dash — em-dashes as connectors read AI-generated
            # and the appositive after a comma+ctx is awkward to parse. Multi-
            # phrase context still chains as separate sentences.
            hist_phrases = f.get("historical_context") or []
            if not hist_phrases:
                hist_suffix = ""
            elif len(hist_phrases) == 1:
                hist_suffix = ". That's " + hist_phrases[0]
            elif len(hist_phrases) == 2:
                # Oxford-style: comma before "and" so the second clause reads
                # as a parallel item, not a run-on continuation.
                hist_suffix = ". That's " + hist_phrases[0] + ", and " + hist_phrases[1]
            else:
                # 3+: middle phrases as sentences, last two joined with ", and"
                first = hist_phrases[0]
                middle = [(p[0].upper() + p[1:]) for p in hist_phrases[1:-1]]
                last = hist_phrases[-1]
                if middle:
                    middle_minus_last = middle[:-1]
                    last_middle = middle[-1]
                    sentences = ["That's " + first] + middle_minus_last
                    joined = ". ".join(sentences)
                    hist_suffix = ". " + joined + ". " + last_middle + ", and " + last
                else:
                    hist_suffix = ". That's " + first + ", and " + last

            # Participle-absolute join — the streak claim is a consequence
            # of the box-score line. Compact sportswriter style: "X went
            # 4-for-4, extending the longest active hitting streak..."
            if game_line:
                headline = f"{player} went {game_line}, extending the longest active {label} in MLB to {streak} games {ctx}{hist_suffix}."
            else:
                headline = f"{player} extended the longest active {label} in MLB to {streak} games {ctx}{hist_suffix}."

        elif f["type"] == "10k_0bb_first_2_starts":
            k = f["k"]
            game_intro = f"{player} went {game_line}" if game_line else f"{player}"

            if not hist:
                context = f"the only pitcher to do this in over 100 years"
            else:
                last = hist[0]
                context = f"only {len(hist)} pitchers have done this in over 100 years, the last being {last['player']} in {last['season']}"
                secondary_names.append(last["player"])

            headline = _continue_with_context(
                f"{game_intro}, reaching {k} K and 0 BB through his first 2 starts of the season",
                context,
            )

        elif f["type"] == "scoreless_first_n_starts":
            ip = f["ip"]
            k = f["k"]
            starts = f["starts"]
            game_intro = f"{player} went {game_line}" if game_line else f"{player}"

            headline = f"{game_intro}, and has now thrown {starts} consecutive scoreless starts to open the season ({ip} IP, {k} K, 0 ER)."

            # MLB + team historical context for "first to open a season with N+"
            mlb_match = f.get("mlb_match")
            team_match = f.get("team_match")
            franchise_name = f.get("franchise_name")
            ctx_parts = []
            if mlb_match and (season - mlb_match["season"]) >= 2:
                ctx_parts.append(f"the first pitcher to do that since {mlb_match['name']} in {mlb_match['season']}")
                secondary_names.append(mlb_match["name"])
            if team_match and franchise_name:
                gap_team = season - team_match["season"]
                gap_mlb = (season - mlb_match["season"]) if mlb_match else 9999
                if gap_team - gap_mlb >= 6:
                    ctx_parts.append(f"the first {franchise_name} pitcher to do it since {team_match['name']} in {team_match['season']}")
                    secondary_names.append(team_match["name"])
            elif not mlb_match and team_match and franchise_name:
                ctx_parts.append(f"the first {franchise_name} pitcher to do it since {team_match['name']} in {team_match['season']}")
                secondary_names.append(team_match["name"])
            if ctx_parts:
                headline = _continue_with_context(
                    headline, ", and ".join(ctx_parts)
                )

        elif f["type"] == "team_fewest_er":
            er = f["er"]
            games = f["games"]
            rank = f["rank"]

            if rank == 1:
                headline = _continue_with_context(
                    f"The {team} have allowed just {er} earned run{'s' if er != 1 else ''} through {games} games",
                    "the fewest by any team in over 100 years",
                )
            elif rank <= 5:
                hist_str = ", ".join(f"the {h['season']} {h['team']} ({h['er']})"
                                   for h in hist[:3])
                headline = f"The {team} have allowed just {er} earned run{'s' if er != 1 else ''} through {games} games, #{rank} all-time behind only {hist_str}."
            else:
                headline = f"The {team} have allowed just {er} earned run{'s' if er != 1 else ''} through {games} games, #{rank} all-time."

        elif f["type"] == "team_starter_era":
            era = f["era"]
            er = f["er"]
            ip = f["ip"]
            games = f["games"]
            rank = f["rank"]

            if rank == 1:
                headline = _continue_with_context(
                    f"The {team}'s starters have a {era:.2f} ERA ({er} ER in {ip} IP) through {games} games",
                    "the lowest by any team's starters in over 100 years",
                )
            elif rank <= 5:
                hist_str = ", ".join(
                    f"the {h['season']} {h['team']} ({h['era_x100'] / 100:.2f})"
                    for h in hist[:3]
                )
                headline = f"The {team}'s starters have a {era:.2f} ERA through {games} games, #{rank} lowest all-time behind only {hist_str}."
            else:
                headline = f"The {team}'s starters have a {era:.2f} ERA through {games} games, #{rank} lowest all-time."

        elif f["type"] == "career_start":
            val = f["value"]
            stat_label = f["stat_label"]
            cg = f["career_games"]
            rank = f["rank"]
            game_intro = f"{player} went {game_line}" if game_line else f"{player}"

            # Build the "passing" context
            tied = f.get("tied_with", [])
            ahead = f.get("ahead", [])

            if rank == 1:
                if tied:
                    passed_str = ", ".join(f"{t['player']} ({t['season']})" for t in tied[:3])
                    headline = f"{game_intro} and now has the most {stat_label} ({val}) in a player's first {cg} career games in over 100 years, passing {passed_str}."
                    secondary_names.extend(t["player"] for t in tied[:3])
                else:
                    headline = f"{game_intro} and now has the most {stat_label} ({val}) in a player's first {cg} career games in over 100 years."
            else:
                if ahead:
                    ahead_str = ", ".join(f"{a['player']} ({a['value']}, {a['season']})" for a in ahead[:3])
                    headline = _continue_with_context(
                        f"{game_intro}, giving him {val} {stat_label} in his first {cg} career games",
                        f"#{rank} all-time, behind only {ahead_str}",
                    )
                    secondary_names.extend(a["player"] for a in ahead[:3])
                else:
                    headline = _continue_with_context(
                        f"{game_intro}, giving him {val} {stat_label} in his first {cg} career games",
                        f"#{rank} all-time",
                    )

        elif f["type"] == "youngest_debut":
            age = f["age_years"]
            xbh = f["xbh"]
            rbi = f["rbi"]
            prev = f.get("previous_youngest")
            game_intro = f"{player} went {game_line} in his MLB debut" if game_line else f"{player} made his MLB debut"

            if prev:
                headline = f"{game_intro}, becoming the youngest player ({age}) with an extra-base hit and an RBI in his debut in over 100 years, surpassing {prev['player']} in {prev['season']}."
                secondary_names.append(prev["player"])
            else:
                headline = f"{game_intro}, becoming the youngest player ({age}) with an extra-base hit and an RBI in his debut in over 100 years."

        elif f["type"] == "leaderboard_change":
            val = f["value"]
            scope = f["scope"]
            stat_label = f["stat_label"]
            stat = f.get("stat", "")
            abbrev = f["stat_abbrev"]
            runner_up = f["runner_up"]
            runner_up_val = f["runner_up_val"]

            # Build stat-appropriate game intro
            if stat in ("batting_avg", "obp", "ops", "slg") and pid:
                # For rate stats, show the components that drive the rate
                game_row = conn.execute("""
                    SELECT hits, at_bats, walks, hit_by_pitch, home_runs,
                           doubles, triples, rbi, stolen_bases
                    FROM game_batting_logs
                    WHERE player_id = ? AND date = (
                        SELECT MAX(date) FROM game_batting_logs
                        WHERE player_id = ? AND season = ?
                    ) AND season = ?
                    LIMIT 1
                """, (pid, pid, season, season)).fetchone()
                if game_row:
                    h, ab, bb, hbp, hr, d, t, rbi, sb = game_row
                    h, ab, bb = h or 0, ab or 0, bb or 0
                    hbp, hr, d, t = hbp or 0, hr or 0, d or 0, t or 0
                    rbi, sb = rbi or 0, sb or 0
                    parts = [f"{h}-for-{ab}"]
                    if bb > 0:
                        parts.append(f"{'a walk' if bb == 1 else f'{bb} walks'}")
                    if hr > 0:
                        parts.append(f"{'a homer' if hr == 1 else f'{hr} homers'}")
                    elif d > 0 or t > 0:
                        xbh_parts = []
                        if d > 0: xbh_parts.append(f"{'a double' if d == 1 else f'{d} doubles'}")
                        if t > 0: xbh_parts.append(f"{'a triple' if t == 1 else f'{t} triples'}")
                        parts.extend(xbh_parts)
                    if sb > 0:
                        parts.append(f"{'a stolen base' if sb == 1 else f'{sb} stolen bases'}")
                    if rbi > 0:
                        parts.append(f"{rbi} RBI")
                    if len(parts) == 1:
                        game_intro = f"{player} went {parts[0]}"
                    elif len(parts) == 2:
                        game_intro = f"{player} went {parts[0]} with {parts[1]}"
                    else:
                        extras = parts[1:]
                        game_intro = f"{player} went {parts[0]} with {', '.join(extras[:-1])} and {extras[-1]}"
                else:
                    game_intro = f"{player}"
            elif stat in ("era", "whip", "k_per_9", "bb_per_9") and pid:
                # Pitching rate stats — show last start line
                pitch_row = conn.execute("""
                    SELECT innings_pitched, ip_outs, hits, earned_runs, strikeouts, walks
                    FROM game_pitching_logs
                    WHERE player_id = ? AND date = (
                        SELECT MAX(date) FROM game_pitching_logs
                        WHERE player_id = ? AND season = ?
                    ) AND season = ?
                    LIMIT 1
                """, (pid, pid, season, season)).fetchone()
                if pitch_row:
                    ip_text, ip_outs, ph, per, pso, pbb = pitch_row
                    ip_display = ip_text or f"{(ip_outs or 0) // 3}.{(ip_outs or 0) % 3}"
                    parts = [f"{ip_display} IP"]
                    parts.append(f"{per or 0} ER")
                    if pso: parts.append(f"{pso} K")
                    game_intro = f"{player} threw {', '.join(parts)}"
                else:
                    game_intro = f"{player}"
            elif game_line:
                game_intro = f"{player} went {game_line}"
            elif stat == "stolen_bases":
                _sb_phrases = ["swiped a bag", "stole a base", "added a stolen base"]
                game_intro = f"{player} {_sb_phrases[hash(player) % len(_sb_phrases)]}"
            elif stat == "saves":
                _sv_phrases = ["earned a save", "closed it out", "nailed down a save"]
                game_intro = f"{player} {_sv_phrases[hash(player) % len(_sv_phrases)]}"
            elif stat == "wins":
                _win_phrases = ["picked up a win", "earned the victory", "got the win"]
                game_intro = f"{player} {_win_phrases[hash(player) % len(_win_phrases)]}"
            else:
                game_intro = f"{player}"

            if isinstance(val, float):
                # ERA/WHIP: 2 decimals, keep leading digit
                if stat in ("era", "whip"):
                    val_str = f"{val:.2f}"
                    ru_str = f"{runner_up_val:.2f}"
                elif val >= 1.0:
                    val_str = f"{val:.3f}"
                    ru_str = f"{runner_up_val:.3f}" if runner_up_val >= 1.0 else f".{int(runner_up_val * 1000):03d}"
                else:
                    val_str = f".{int(val * 1000):03d}"
                    ru_str = f"{runner_up_val:.3f}" if runner_up_val >= 1.0 else f".{int(runner_up_val * 1000):03d}"
            else:
                val_str = str(val)
                ru_str = str(runner_up_val)

            # Check if this player previously held this lead (within last 7 days)
            took_verb = "taking"
            try:
                prev_lead = conn.execute("""
                    SELECT 1 FROM notable_events
                    WHERE detection_type = 'historical'
                    AND headline LIKE ? AND headline LIKE ?
                    AND game_date >= date(?, '-7 days') AND game_date < ?
                    LIMIT 1
                """, (f"%{player}%", f"%lead in {stat_label}%", latest_date, latest_date)).fetchone()
                if prev_lead:
                    took_verb = "taking back"
            except Exception:
                pass

            # "but still took" for rate stats when player had a bad day (0 hits)
            if stat in ("batting_avg", "obp", "ops", "slg") and game_row and (game_row[0] or 0) == 0:
                if took_verb == "taking back":
                    took_verb = "but still took back"
                else:
                    took_verb = "but still took"

            # Past-tense main verb instead of present-participle ("taking" →
            # "took") so the second clause reads unambiguously as a past
            # Comma + participle absolute — the lead change is a consequence
            # of the box-score line, not a separate beat. Participle form
            # captures cause→consequence naturally (compact sportswriter
            # style: "X went 1-for-4, taking the NL lead in slugging").
            # The "but still" variant keeps its finite past-tense verb
            # since the "but" contrast conjunction needs one.
            headline = f"{game_intro}, {took_verb} the {scope} lead in {stat_label} ({val_str}), passing {runner_up} ({ru_str})."
            secondary_names.append(runner_up)

        elif f["type"] == "leaderboard_tie":
            val = f["value"]
            scope = f["scope"]
            stat_label = f["stat_label"]
            tied_with = f["tied_with"]
            if game_line:
                # Participle-absolute form same as the lead-change template.
                headline = f"{player} went {game_line}, tying {tied_with} for the {scope} lead in {stat_label} ({val})."
            else:
                headline = f"{player} tied {tied_with} for the {scope} lead in {stat_label} ({val})."
            secondary_names.append(tied_with)

        elif f["type"] == "youngest_debut_since":
            age = f["age_years"]
            last = f["last_younger"]
            game_intro = f"{player} went {game_line} in his MLB debut" if game_line else f"{player} made his MLB debut"

            headline = f"{game_intro}, becoming the youngest player ({age}) with an extra-base hit and an RBI in his debut since {last['player']} in {last['season']}."
            secondary_names.append(last["player"])

        else:
            continue

        all_names = ([player] if player else []) + secondary_names
        # Tag cross-season hitting/on-base streaks with a distinct detection_type
        # so detect_all (and the poll path) can suppress the breadth-tier
        # notable_events streak event for the same player — otherwise the feed
        # merge concatenates two versions of the same streak. Other historical
        # facts keep the generic "historical_scan" type (set by the caller).
        evt_detection_type = None
        if f.get("type", "").startswith("cross_season_"):
            stk = f.get("streak_type_key")  # "hitting" or "on_base"
            if stk in ("hitting", "on_base"):
                evt_detection_type = f"cross_season_streak_{stk}"
        events.append({
            "headline": headline,
            "category": "historical",
            "player_names": all_names,
            "team_names": [team] if team else [],
            "detection_type": evt_detection_type,
        })
        if pid:
            seen_players.add(pid)

    return events
