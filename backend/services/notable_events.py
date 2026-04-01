"""
Notable Events Detection Engine

Runs after each cron refresh to scan game logs, streaks, and stats
for notable baseball events. Results stored in `notable_events` table
and served via GET /notable-events.

Tiered detection:
  Tier 1 (high-signal): Active streaks, season pace milestones
  Tier 2 (medium): Career milestones, standout single-game performances, hot streaks
  Tier 3 (backfill): Relaxed thresholds to guarantee 3+ events/day
"""

import json
import sqlite3
import os
from datetime import date, datetime

DB_PATH = os.getenv("DB_PATH", "/data/baseball_stats_full.db")

# Retrosheet team code → display name
RETRO_TO_DISPLAY = {
    "NYA": "Yankees", "NYN": "Mets", "LAN": "Dodgers", "ANA": "Angels",
    "CHN": "Cubs", "CHA": "White Sox", "SFN": "Giants", "SDN": "Padres",
    "SLN": "Cardinals", "KCA": "Royals", "TBA": "Rays", "WAS": "Nationals",
    "BOS": "Red Sox", "HOU": "Astros", "ATL": "Braves", "PHI": "Phillies",
    "TEX": "Rangers", "TOR": "Blue Jays", "BAL": "Orioles", "MIN": "Twins",
    "CLE": "Guardians", "SEA": "Mariners", "MIL": "Brewers", "CIN": "Reds",
    "PIT": "Pirates", "DET": "Tigers", "ARI": "Diamondbacks", "COL": "Rockies",
    "MIA": "Marlins", "OAK": "Athletics",
}


def team_display(retro_code):
    """Convert Retrosheet team code to display name."""
    return RETRO_TO_DISPLAY.get(retro_code, retro_code)


def ensure_table(conn):
    """Create the notable_events table if it doesn't exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS notable_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            headline TEXT NOT NULL,
            detail TEXT NOT NULL,
            category TEXT NOT NULL,
            game_date TEXT NOT NULL,
            player_names TEXT,
            team_names TEXT,
            detection_type TEXT NOT NULL,
            priority INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(detection_type, game_date, headline)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_notable_date ON notable_events(game_date)
    """)
    conn.commit()


def _get_latest_date(conn, season):
    """Get the most recent game date in the season."""
    row = conn.execute(
        "SELECT MAX(date) FROM game_batting_logs WHERE season = ?", (season,)
    ).fetchone()
    return row[0] if row and row[0] else None


def _player_name(conn, player_id):
    """Look up player display name."""
    row = conn.execute(
        "SELECT name FROM players WHERE player_id = ?", (player_id,)
    ).fetchone()
    return row[0] if row else player_id


def _player_team_display(conn, player_id, season):
    """Get team display name for a player in a season."""
    row = conn.execute(
        "SELECT team FROM season_batting_stats WHERE player_id = ? AND season = ?",
        (player_id, season)
    ).fetchone()
    if not row:
        row = conn.execute(
            "SELECT team FROM season_pitching_stats WHERE player_id = ? AND season = ?",
            (player_id, season)
        ).fetchone()
    return team_display(row[0]) if row and row[0] else ""


# ---------------------------------------------------------------------------
# Historical comparison engine
# ---------------------------------------------------------------------------

def _historical_context(conn, streak_len, condition_sql, table="game_batting_logs",
                        exclude_season=None, exclude_player=None,
                        at_bat_filter="at_bats > 0"):
    """For streaks: find the last time someone had a consecutive-game streak
    of this length. Returns context string or empty."""
    if streak_len < 10:
        return ""

    exclude_season = exclude_season or 0
    exclude_player = exclude_player or ""

    seasons = conn.execute(f"""
        SELECT DISTINCT season FROM {table}
        WHERE season < ?
        ORDER BY season DESC
    """, (exclude_season,)).fetchall()

    for (szn,) in seasons:
        games = conn.execute(f"""
            SELECT player_id, ({condition_sql}) as met
            FROM {table}
            WHERE season = ? AND ({at_bat_filter})
            ORDER BY player_id, date
        """, (szn,)).fetchall()

        best_pid = None
        best_run = 0
        current_pid = None
        current_run = 0
        max_run_for_player = 0

        for pid, met in games:
            if pid != current_pid:
                if current_pid and current_pid != exclude_player and max_run_for_player > best_run:
                    best_run = max_run_for_player
                    best_pid = current_pid
                current_pid = pid
                current_run = 0
                max_run_for_player = 0
            if met:
                current_run += 1
                max_run_for_player = max(max_run_for_player, current_run)
            else:
                current_run = 0

        if current_pid and current_pid != exclude_player and max_run_for_player > best_run:
            best_run = max_run_for_player
            best_pid = current_pid

        if best_run >= streak_len:
            name = _player_name(conn, best_pid)
            return f"The longest since {name} ({best_run} games) in {szn}."

    return ""


def _rarity_last_occurrence(conn, condition_sql, table="game_batting_logs",
                            exclude_season=None):
    """For rare single-game events (≤5/yr): find the last time this happened.
    Only returns context if it's been ≥1 full season since last occurrence.
    Returns string like 'the first since X in Y' or empty."""
    exclude_season = exclude_season or 0

    seasons = conn.execute(f"""
        SELECT DISTINCT season FROM {table}
        WHERE season < ?
        ORDER BY season DESC
    """, (exclude_season,)).fetchall()

    for (szn,) in seasons:
        row = conn.execute(f"""
            SELECT p.name
            FROM {table} g
            JOIN players p ON g.player_id = p.player_id
            WHERE g.season = ? AND ({condition_sql})
            LIMIT 1
        """, (szn,)).fetchone()

        if row:
            # Only show "first since" if it's been at least 1 full season
            if szn < exclude_season - 1:
                return f"the first since {row[0]} in {szn}."
            return ""  # Happened last season, not notable enough

    return "the first in recorded history."


def _is_first_this_season(conn, condition_sql, table="game_batting_logs",
                          season=None, before_date=None):
    """For noteworthy events (6-13/yr): check if this is the first occurrence
    this season (before the given date). Returns True/False."""
    row = conn.execute(f"""
        SELECT COUNT(*) FROM {table}
        WHERE season = ? AND date < ? AND ({condition_sql})
    """, (season, before_date)).fetchone()
    return row[0] == 0 if row else True


# ---------------------------------------------------------------------------
# Tier 1: High-signal detectors
# ---------------------------------------------------------------------------

def detect_hitting_streaks(conn, season, latest_date, min_games=8):
    """Find active consecutive-game hitting streaks."""
    events = []

    # Get all players who played on the latest date
    players = conn.execute("""
        SELECT DISTINCT player_id FROM game_batting_logs
        WHERE season = ? AND date = ? AND at_bats > 0
    """, (season, latest_date)).fetchall()

    for (pid,) in players:
        # Walk backwards through game logs
        games = conn.execute("""
            SELECT date, hits, at_bats FROM game_batting_logs
            WHERE player_id = ? AND season = ? AND at_bats > 0
            ORDER BY date DESC
        """, (pid, season)).fetchall()

        streak = 0
        for game_date, hits, ab in games:
            if hits > 0:
                streak += 1
            else:
                break

        if streak >= min_games:
            name = _player_name(conn, pid)
            team = _player_team_display(conn, pid, season)
            context = _historical_context(
                conn, streak, "hits > 0",
                exclude_season=season, exclude_player=pid
            )
            headline = f"{name} has hit safely in {streak} straight games"
            if context:
                headline += f", {context.lower()}"
            else:
                headline += "."
            events.append({
                "headline": headline,
                "detail": "",
                "category": "Streak",
                "game_date": latest_date,
                "player_names": [name],
                "team_names": [team] if team else [],
                "detection_type": "hitting_streak",
                "priority": 1,
                "_streak_len": streak,
            })

    # Sort by streak length, keep top 3
    events.sort(key=lambda e: e.get("_streak_len", 0), reverse=True)
    for e in events:
        e.pop("_streak_len", None)
    return events[:3]


def detect_onbase_streaks(conn, season, latest_date, min_games=12):
    """Find active consecutive-game on-base streaks."""
    events = []

    players = conn.execute("""
        SELECT DISTINCT player_id FROM game_batting_logs
        WHERE season = ? AND date = ? AND (at_bats > 0 OR walks > 0 OR hit_by_pitch > 0)
    """, (season, latest_date)).fetchall()

    for (pid,) in players:
        games = conn.execute("""
            SELECT date, hits, walks, COALESCE(hit_by_pitch, 0) as hbp
            FROM game_batting_logs
            WHERE player_id = ? AND season = ?
                AND (at_bats > 0 OR walks > 0 OR COALESCE(hit_by_pitch, 0) > 0)
            ORDER BY date DESC
        """, (pid, season)).fetchall()

        streak = 0
        for game_date, hits, walks, hbp in games:
            if (hits + walks + hbp) > 0:
                streak += 1
            else:
                break

        if streak >= min_games:
            name = _player_name(conn, pid)
            context = _historical_context(
                conn, streak,
                "(hits + walks + COALESCE(hit_by_pitch, 0)) > 0",
                exclude_season=season, exclude_player=pid,
                at_bat_filter="(at_bats > 0 OR walks > 0 OR COALESCE(hit_by_pitch, 0) > 0)"
            )
            headline = f"{name} has reached base in {streak} straight games"
            if context:
                headline += f", {context.lower()}"
            else:
                headline += "."
            events.append({
                "headline": headline,
                "detail": "",
                "category": "Streak",
                "game_date": latest_date,
                "player_names": [name],
                "team_names": [],
                "detection_type": "onbase_streak",
                "priority": 1,
                "_streak_len": streak,
            })

    events.sort(key=lambda e: e.get("_streak_len", 0), reverse=True)
    for e in events:
        e.pop("_streak_len", None)
    return events[:2]


def detect_hr_streaks(conn, season, latest_date, min_games=4):
    """Find active consecutive-game HR streaks."""
    events = []

    players = conn.execute("""
        SELECT DISTINCT player_id FROM game_batting_logs
        WHERE season = ? AND date = ? AND at_bats > 0
    """, (season, latest_date)).fetchall()

    for (pid,) in players:
        games = conn.execute("""
            SELECT date, home_runs FROM game_batting_logs
            WHERE player_id = ? AND season = ? AND at_bats > 0
            ORDER BY date DESC
        """, (pid, season)).fetchall()

        streak = 0
        for game_date, hr in games:
            if hr and hr > 0:
                streak += 1
            else:
                break

        if streak >= min_games:
            name = _player_name(conn, pid)
            context = _historical_context(
                conn, streak, "home_runs > 0",
                exclude_season=season, exclude_player=pid
            )
            headline = f"{name} has homered in {streak} straight games"
            if context:
                headline += f", {context.lower()}"
            else:
                headline += "."
            events.append({
                "headline": headline,
                "detail": "",
                "category": "Streak",
                "game_date": latest_date,
                "player_names": [name],
                "team_names": [],
                "detection_type": "hr_streak",
                "priority": 1,
            })

    return events[:2]


def detect_pitching_streaks(conn, season, latest_date):
    """Find active pitching dominance streaks (scoreless starts, quality starts)."""
    events = []

    # Get starters who pitched on or near the latest date
    starters = conn.execute("""
        SELECT DISTINCT player_id FROM game_pitching_logs
        WHERE season = ? AND is_start = 1
    """, (season,)).fetchall()

    for (pid,) in starters:
        starts = conn.execute("""
            SELECT date, ip_outs, earned_runs, innings_pitched, strikeouts
            FROM game_pitching_logs
            WHERE player_id = ? AND season = ? AND is_start = 1
            ORDER BY date DESC
        """, (pid, season)).fetchall()

        if not starts:
            continue

        # Scoreless starts streak (0 ER, 5+ IP)
        scoreless = 0
        for game_date, ip_outs, er, ip_text, so in starts:
            ip = (ip_outs or 0) / 3.0
            if (er is not None and er == 0) and ip >= 5.0:
                scoreless += 1
            else:
                break

        if scoreless >= 2:
            name = _player_name(conn, pid)
            events.append({
                "headline": f"{name} has thrown {scoreless} consecutive scoreless starts, allowing 0 earned runs in 5+ innings each time.",
                "detail": "",
                "category": "Streak",
                "game_date": starts[0][0],
                "player_names": [name],
                "team_names": [],
                "detection_type": "scoreless_streak",
                "priority": 1,
            })

        # Quality start streak (6+ IP, 3 or fewer ER)
        qs = 0
        for game_date, ip_outs, er, ip_text, so in starts:
            ip = (ip_outs or 0) / 3.0
            if ip >= 6.0 and (er is not None and er <= 3):
                qs += 1
            else:
                break

        if qs >= 4:
            name = _player_name(conn, pid)
            events.append({
                "headline": f"{name} has {qs} consecutive quality starts — at least 6 IP with 3 or fewer earned runs each outing.",
                "detail": "",
                "category": "Streak",
                "game_date": starts[0][0],
                "player_names": [name],
                "team_names": [],
                "detection_type": "qs_streak",
                "priority": 1,
            })

    return events


def detect_season_pace(conn, season, latest_date=None):
    """Find players on pace for notable season milestones."""
    events = []

    # Need enough games to be meaningful
    min_games = 15

    # Batting pace milestones
    pace_checks = [
        ("home_runs", [(60, "challenge the single-season record"), (50, "join the 50-HR club"),
                       (40, "reach 40 home runs")]),
        ("stolen_bases", [(60, "reach 60 stolen bases"), (50, "reach 50 stolen bases")]),
        ("hits", [(220, "challenge for the most hits in a season since Ichiro"),
                  (200, "reach 200 hits")]),
        ("rbi", [(140, "reach 140 RBI"), (120, "reach 120 RBI")]),
    ]

    for stat_col, thresholds in pace_checks:
        rows = conn.execute(f"""
            SELECT p.name, s.{stat_col}, s.games, p.team
            FROM season_batting_stats s
            JOIN players p ON s.player_id = p.player_id
            WHERE s.season = ? AND s.games >= ?
            ORDER BY s.{stat_col} DESC
            LIMIT 10
        """, (season, min_games)).fetchall()

        for name, stat_val, games, team_code in rows:
            if not stat_val or games == 0:
                continue
            pace = int(stat_val * 162.0 / games)

            # Find highest threshold cleared
            for threshold, desc in thresholds:
                if pace >= threshold:
                    team = team_display(team_code) if team_code else ""
                    events.append({
                        "headline": f"{name} is on pace for {pace} {stat_col.replace('_', ' ')}",
                        "detail": f"Would {desc}.",
                        "category": "Milestone",
                        "game_date": latest_date,
                        "player_names": [name],
                        "team_names": [team] if team else [],
                        "detection_type": f"pace_{stat_col}",
                        "priority": 1,
                    })
                    break  # Only highest threshold

    # Pitching pace: strikeouts
    rows = conn.execute("""
        SELECT p.name, s.strikeouts, s.games, p.team
        FROM season_pitching_stats s
        JOIN players p ON s.player_id = p.player_id
        WHERE s.season = ? AND s.games >= ? AND s.games_started >= ?
        ORDER BY s.strikeouts DESC
        LIMIT 5
    """, (season, min_games, min_games - 3)).fetchall()

    for name, so, games, team_code in rows:
        if not so or games == 0:
            continue
        pace = int(so * 162.0 / games)
        if pace >= 300:
            team = team_display(team_code) if team_code else ""
            events.append({
                "headline": f"{name} is on pace for {pace} strikeouts",
                "detail": "Would be among the highest single-season totals in MLB history.",
                "category": "Milestone",
                "game_date": latest_date,
                "player_names": [name],
                "team_names": [team] if team else [],
                "detection_type": "pace_pitching_k",
                "priority": 1,
            })

    return events


# ---------------------------------------------------------------------------
# Tier 2: Medium-signal detectors
# ---------------------------------------------------------------------------

def detect_career_milestones(conn, season, latest_date):
    """Find players approaching career milestone numbers.

    Only triggers when the player contributed to the milestone stat in
    their most recent game (e.g., hit a HR → show HR milestone proximity).
    Capped at 5 away from milestone.
    """
    events = []

    # Batting milestones: (career_col, game_log_col, milestones, label, action_template)
    # action_template: lambda(game_value) -> "hit 2 home runs"
    bat_milestones = [
        ("home_runs", "home_runs", [600, 500, 400, 300, 200, 100], "career home runs",
         lambda v: f"hit {v} home run{'s' if v != 1 else ''}"),
        ("hits", "hits", [3000, 2500, 2000, 1500, 1000], "career hits",
         lambda v: f"went {v}-for with {v} hit{'s' if v != 1 else ''}"),
        ("rbi", "rbi", [1500, 1000, 500], "career RBI",
         lambda v: f"drove in {v} run{'s' if v != 1 else ''}"),
    ]

    for col, game_col, milestones, label, action_fn in bat_milestones:
        # Find players who contributed to this stat in their most recent game
        contributors = conn.execute(f"""
            SELECT g.player_id, g.date, g.{game_col}
            FROM game_batting_logs g
            INNER JOIN (
                SELECT player_id, MAX(date) as max_date
                FROM game_batting_logs WHERE season = ?
                GROUP BY player_id
            ) latest ON g.player_id = latest.player_id AND g.date = latest.max_date
            WHERE g.season = ? AND g.{game_col} > 0
        """, (season, season)).fetchall()
        contributor_info = {r[0]: (r[1], r[2]) for r in contributors}  # pid -> (date, stat_val)

        if not contributor_info:
            continue

        # Career totals for those players
        placeholders = ",".join("?" * len(contributor_info))
        rows = conn.execute(f"""
            SELECT s.player_id, p.name, SUM(s.{col}) as career_total
            FROM season_batting_stats s
            JOIN players p ON s.player_id = p.player_id
            WHERE s.player_id IN ({placeholders})
            GROUP BY s.player_id
            ORDER BY career_total DESC
        """, list(contributor_info.keys())).fetchall()

        found = 0
        for pid, name, total in rows:
            if not total or found >= 3:
                break
            for m in milestones:
                remaining = m - total
                if 1 <= remaining <= 5:
                    game_date, stat_val = contributor_info[pid]
                    action = action_fn(stat_val)
                    # For hits, fix the template with actual AB
                    if col == "hits":
                        game_row = conn.execute("""
                            SELECT hits, at_bats FROM game_batting_logs
                            WHERE player_id = ? AND date = ? AND season = ?
                        """, (pid, game_date, season)).fetchone()
                        if game_row:
                            action = f"collected {game_row[0]} hit{'s' if game_row[0] != 1 else ''}"
                    events.append({
                        "headline": f"{name} {action}, and is now {remaining} away from {m} {label}.",
                        "detail": "",
                        "category": "Milestone",
                        "game_date": game_date,
                        "player_names": [name],
                        "team_names": [],
                        "detection_type": f"career_{col}_{m}",
                        "priority": 2,
                    })
                    found += 1
                    break

    # Pitching milestones: (career_col, game_log_col, milestones, label, action_template)
    pitch_milestones = [
        ("strikeouts", "strikeouts", [3000, 2500, 2000, 1500, 1000], "career strikeouts",
         lambda v: f"struck out {v}"),
        ("wins", "win", [200, 150, 100], "career wins",
         lambda v: "picked up a win"),
        ("saves", "save", [400, 300, 200], "career saves",
         lambda v: "recorded a save"),
    ]

    for col, game_col, milestones, label, action_fn in pitch_milestones:
        contributors = conn.execute(f"""
            SELECT g.player_id, g.date, g.{game_col}
            FROM game_pitching_logs g
            INNER JOIN (
                SELECT player_id, MAX(date) as max_date
                FROM game_pitching_logs WHERE season = ?
                GROUP BY player_id
            ) latest ON g.player_id = latest.player_id AND g.date = latest.max_date
            WHERE g.season = ? AND g.{game_col} > 0
        """, (season, season)).fetchall()
        contributor_info = {r[0]: (r[1], r[2]) for r in contributors}

        if not contributor_info:
            continue

        placeholders = ",".join("?" * len(contributor_info))
        rows = conn.execute(f"""
            SELECT s.player_id, p.name, SUM(s.{col}) as career_total
            FROM season_pitching_stats s
            JOIN players p ON s.player_id = p.player_id
            WHERE s.player_id IN ({placeholders})
            GROUP BY s.player_id
            ORDER BY career_total DESC
        """, list(contributor_info.keys())).fetchall()

        found = 0
        for pid, name, total in rows:
            if not total or found >= 3:
                break
            for m in milestones:
                remaining = m - total
                if 1 <= remaining <= 5:
                    game_date, stat_val = contributor_info[pid]
                    action = action_fn(stat_val)
                    events.append({
                        "headline": f"{name} {action}, and is now {remaining} away from {m} {label}.",
                        "detail": "",
                        "category": "Milestone",
                        "game_date": game_date,
                        "player_names": [name],
                        "team_names": [],
                        "detection_type": f"career_p_{col}_{m}",
                        "priority": 2,
                    })
                    found += 1
                    break

    return events


def detect_rarities(conn, season, latest_date):
    """Detect rare and noteworthy single-game performances.

    Two tiers, frequency-validated from 2016-2025 data:

    RARITY (≤5/yr) — always show, with "first since [player] in [year]":
      Cycle (~4.5), 4+ HR (~1.5), 6+ hits (~2), 5+ hits w/ triple (~2),
      8+ RBI (~3), 5+ hits w/ 2+ HR (~3.4), 4+ hits w/ 3+ HR (~5.5),
      No-hitter (~2.4), 1-hitter (~5), 15+ K (~2.8)

    NOTEWORTHY (6-13/yr) — show with "first this season" if applicable:
      2+ triples (~8.5), 14+ K (~9), 3 HR + 5+ RBI (~9.4),
      2-hitter 9IP (~10), 5+ hits w/ HR (~10.5), 7+ RBI (~11),
      3 HR game (~13), Complete game (~25, included by design)
    """
    events = []
    seen = set()  # (player_id, date) dedup

    # --- Helper to process a batch of checks ---
    def _check_batting(checks, category, use_first_since=False, use_first_this_season=False):
        for check in checks:
            rows = conn.execute(f"""
                SELECT p.name, g.player_id, g.date, g.hits, g.home_runs, g.rbi,
                       g.at_bats, g.doubles, g.triples
                FROM game_batting_logs g
                JOIN players p ON g.player_id = p.player_id
                WHERE g.season = ? AND g.date >= (
                    SELECT DISTINCT date FROM game_batting_logs WHERE season = ?
                    ORDER BY date DESC LIMIT 1 OFFSET 1
                )
                AND ({check['condition']})
            """, (season, season)).fetchall()

            for name, pid, game_date, h, hr, rbi, ab, doubles, triples in rows:
                key = (pid, game_date)
                if key in seen:
                    continue
                seen.add(key)

                r = {"h": h or 0, "hr": hr or 0, "rbi": rbi or 0, "ab": ab or 0}
                headline = check["headline"](name, r)

                if use_first_since:
                    context = _rarity_last_occurrence(
                        conn, check["history_sql"], "game_batting_logs",
                        exclude_season=season
                    )
                    if context:
                        headline = headline.rstrip(".") + f" — {context}"
                elif use_first_this_season:
                    if _is_first_this_season(conn, check["history_sql"],
                                             "game_batting_logs", season, game_date):
                        headline = headline.rstrip(".") + " — the first this season."

                events.append({
                    "headline": headline, "detail": "",
                    "category": category, "game_date": game_date,
                    "player_names": [name], "team_names": [],
                    "detection_type": check["type"],
                    "priority": 1 if category == "Rarity" else 2,
                })

    # ---- RARITY TIER (≤5/yr) — batting ----
    _check_batting([
        {
            "condition": "g.doubles >= 1 AND g.triples >= 1 AND g.home_runs >= 1 AND g.hits >= 4",
            "type": "cycle", "history_sql": "doubles >= 1 AND triples >= 1 AND home_runs >= 1 AND hits >= 4",
            "headline": lambda n, r: f"{n} hit for the cycle, going {r['h']}-for-{r['ab']} with {r['rbi']} RBI.",
        },
        {
            "condition": "g.home_runs >= 4",
            "type": "4hr_game", "history_sql": "home_runs >= 4",
            "headline": lambda n, r: f"{n} hit {r['hr']} home runs, going {r['h']}-for-{r['ab']} with {r['rbi']} RBI.",
        },
        {
            "condition": "g.hits >= 6",
            "type": "6hit_game", "history_sql": "hits >= 6",
            "headline": lambda n, r: f"{n} collected {r['h']} hits, going {r['h']}-for-{r['ab']} with {r['hr']} HR and {r['rbi']} RBI.",
        },
        {
            "condition": "g.hits >= 5 AND g.triples >= 1",
            "type": "5hit_3b_game", "history_sql": "hits >= 5 AND triples >= 1",
            "headline": lambda n, r: f"{n} went {r['h']}-for-{r['ab']} including a triple, with {r['hr']} HR and {r['rbi']} RBI.",
        },
        {
            "condition": "g.rbi >= 8",
            "type": "8rbi_game", "history_sql": "rbi >= 8",
            "headline": lambda n, r: f"{n} drove in {r['rbi']} runs, going {r['h']}-for-{r['ab']} with {r['hr']} home runs.",
        },
        {
            "condition": "g.hits >= 5 AND g.home_runs >= 2",
            "type": "5hit_2hr_game", "history_sql": "hits >= 5 AND home_runs >= 2",
            "headline": lambda n, r: f"{n} went {r['h']}-for-{r['ab']} with {r['hr']} home runs and {r['rbi']} RBI.",
        },
        {
            "condition": "g.hits >= 4 AND g.home_runs >= 3",
            "type": "4hit_3hr_game", "history_sql": "hits >= 4 AND home_runs >= 3",
            "headline": lambda n, r: f"{n} hit {r['hr']} home runs, going {r['h']}-for-{r['ab']} with {r['rbi']} RBI.",
        },
    ], category="Rarity", use_first_since=True)

    # ---- NOTEWORTHY TIER (6-13/yr) — batting ----
    _check_batting([
        {
            "condition": "g.triples >= 2",
            "type": "2_triples", "history_sql": "triples >= 2",
            "headline": lambda n, r: f"{n} hit 2 triples, going {r['h']}-for-{r['ab']}.",
        },
        {
            "condition": "g.home_runs >= 3 AND g.rbi >= 5",
            "type": "3hr_5rbi", "history_sql": "home_runs >= 3 AND rbi >= 5",
            "headline": lambda n, r: f"{n} hit {r['hr']} home runs and drove in {r['rbi']} runs.",
        },
        {
            "condition": "g.hits >= 5 AND g.home_runs >= 1",
            "type": "5hit_hr", "history_sql": "hits >= 5 AND home_runs >= 1",
            "headline": lambda n, r: f"{n} went {r['h']}-for-{r['ab']} with a home run and {r['rbi']} RBI.",
        },
        {
            "condition": "g.rbi >= 7",
            "type": "7rbi_game", "history_sql": "rbi >= 7",
            "headline": lambda n, r: f"{n} drove in {r['rbi']} runs, going {r['h']}-for-{r['ab']}.",
        },
        {
            "condition": "g.home_runs >= 3",
            "type": "3hr_game", "history_sql": "home_runs >= 3",
            "headline": lambda n, r: f"{n} hit {r['hr']} home runs, going {r['h']}-for-{r['ab']} with {r['rbi']} RBI.",
        },
    ], category="Rarity", use_first_this_season=True)

    # ---- PITCHING — both tiers ----
    rows = conn.execute("""
        SELECT p.name, g.player_id, g.date, g.innings_pitched, g.strikeouts,
               g.hits, g.walks, g.earned_runs, g.ip_outs
        FROM game_pitching_logs g
        JOIN players p ON g.player_id = p.player_id
        WHERE g.season = ? AND g.date >= (
            SELECT DISTINCT date FROM game_pitching_logs WHERE season = ?
            ORDER BY date DESC LIMIT 1 OFFSET 1
        )
    """, (season, season)).fetchall()

    for name, pid, game_date, ip, so, h, bb, er, ip_outs in rows:
        ip_outs = ip_outs or 0
        so = so or 0
        h = h or 0
        bb = bb or 0
        er = er or 0
        ip_display = ip or f"{ip_outs // 3}.{ip_outs % 3}"
        key = (pid, game_date)

        # -- RARITY pitching --

        # No-hitter
        if ip_outs >= 27 and h == 0 and key not in seen:
            seen.add(key)
            headline = f"{name} threw a no-hitter over {ip_display} innings, striking out {so}."
            context = _rarity_last_occurrence(conn, "ip_outs >= 27 AND hits = 0", "game_pitching_logs", exclude_season=season)
            if context:
                headline = headline.rstrip(".") + f" — {context}"
            events.append({"headline": headline, "detail": "", "category": "Rarity", "game_date": game_date,
                           "player_names": [name], "team_names": [], "detection_type": "no_hitter", "priority": 1})
            continue

        # 1-hitter
        if ip_outs >= 27 and h == 1 and key not in seen:
            seen.add(key)
            headline = f"{name} threw a 1-hitter over {ip_display} innings, striking out {so}."
            context = _rarity_last_occurrence(conn, "ip_outs >= 27 AND hits <= 1", "game_pitching_logs", exclude_season=season)
            if context:
                headline = headline.rstrip(".") + f" — {context}"
            events.append({"headline": headline, "detail": "", "category": "Rarity", "game_date": game_date,
                           "player_names": [name], "team_names": [], "detection_type": "1_hitter", "priority": 1})

        # 15+ K
        if so >= 15 and (pid, game_date, "15k") not in seen:
            seen.add((pid, game_date, "15k"))
            headline = f"{name} struck out {so} in {ip_display} innings, allowing {h} hits and {er} earned runs."
            context = _rarity_last_occurrence(conn, f"strikeouts >= {so}", "game_pitching_logs", exclude_season=season)
            if context:
                headline = headline.rstrip(".") + f" — {context}"
            events.append({"headline": headline, "detail": "", "category": "Rarity", "game_date": game_date,
                           "player_names": [name], "team_names": [], "detection_type": "15k_game", "priority": 1})

        # -- NOTEWORTHY pitching --

        # 14+ K (if not already 15+)
        if 14 <= so < 15 and key not in seen:
            seen.add(key)
            headline = f"{name} struck out {so} in {ip_display} innings, allowing {h} hits and {er} earned runs."
            if _is_first_this_season(conn, "strikeouts >= 14", "game_pitching_logs", season, game_date):
                headline = headline.rstrip(".") + " — the first 14+ strikeout game this season."
            events.append({"headline": headline, "detail": "", "category": "Rarity", "game_date": game_date,
                           "player_names": [name], "team_names": [], "detection_type": "14k_game", "priority": 2})

        # 2-hitter (9 IP)
        if ip_outs >= 27 and h == 2 and key not in seen:
            seen.add(key)
            headline = f"{name} threw a 2-hitter over {ip_display} innings, striking out {so}."
            if _is_first_this_season(conn, "ip_outs >= 27 AND hits <= 2", "game_pitching_logs", season, game_date):
                headline = headline.rstrip(".") + " — the first 2-hitter this season."
            events.append({"headline": headline, "detail": "", "category": "Rarity", "game_date": game_date,
                           "player_names": [name], "team_names": [], "detection_type": "2_hitter", "priority": 2})

        # Complete game
        if ip_outs >= 27 and key not in seen:
            seen.add(key)
            if er == 0:
                headline = f"{name} threw a complete game shutout — {ip_display} IP, {so} K, {h} hits."
            else:
                headline = f"{name} threw a complete game — {ip_display} IP, {er} ER, {so} K."
            if _is_first_this_season(conn, "ip_outs >= 27", "game_pitching_logs", season, game_date):
                headline = headline.rstrip(".") + " — the first complete game this season."
            events.append({"headline": headline, "detail": "", "category": "Rarity", "game_date": game_date,
                           "player_names": [name], "team_names": [], "detection_type": "complete_game", "priority": 2})

    return events


def detect_hot_streaks_pelt(conn, season, latest_date=None):
    """Find players in a PELT-detected hot streak with high OPS."""
    events = []

    rows = conn.execute("""
        SELECT p.name, cf.ops, cf.batting_avg, cf.num_games, cf.home_runs,
               cf.hits, cf.at_bats, cf.obp, cf.slg,
               s.ops as season_ops
        FROM current_form cf
        JOIN players p ON cf.player_id = p.player_id
        JOIN season_batting_stats s ON cf.player_id = s.player_id AND cf.season = s.season
        WHERE cf.season = ?
          AND cf.ops >= 1.000
          AND cf.num_games >= 7
          AND (cf.ops - s.ops) >= 0.200
        ORDER BY cf.ops DESC
        LIMIT 5
    """, (season,)).fetchall()

    for name, ops, avg, num_games, hr, h, ab, obp, slg, season_ops in rows:
        events.append({
            "headline": f"{name} is on a tear — .{int(avg*1000):03d}/.{int(obp*1000):03d}/.{int(slg*1000):03d} over the last {num_games} games"
                if avg and obp and slg else f"{name} is on a tear — {ops:.3f} OPS over the last {num_games} games",
            "detail": f"Season OPS is {season_ops:.3f}. Current stretch: {hr or 0} HR in {num_games} games.",
            "category": "Streak",
            "game_date": latest_date,
            "player_names": [name],
            "team_names": [],
            "detection_type": "hot_streak_pelt",
            "priority": 2,
        })

    return events


# ---------------------------------------------------------------------------
# Tier 3: Backfill detectors (only used when Tiers 1+2 < 3 events)
# ---------------------------------------------------------------------------

def detect_hitting_streaks_relaxed(conn, season, latest_date):
    """Same as detect_hitting_streaks but with a lower threshold (5 games)."""
    return detect_hitting_streaks(conn, season, latest_date, min_games=5)


def detect_league_leaders(conn, season, latest_date=None):
    """Surface current league leaders in key stats."""
    events = []
    min_pa = max(20, int(10 * 3.1))  # rough early-season minimum

    leaders = [
        ("home_runs", "home runs", "plate_appearances", min_pa),
        ("batting_avg", "batting average", "plate_appearances", min_pa),
        ("rbi", "RBI", "plate_appearances", min_pa),
        ("stolen_bases", "stolen bases", "plate_appearances", min_pa),
    ]

    for col, label, qual_col, qual_min in leaders:
        row = conn.execute(f"""
            SELECT p.name, s.{col}
            FROM season_batting_stats s
            JOIN players p ON s.player_id = p.player_id
            WHERE s.season = ? AND s.{qual_col} >= ?
            ORDER BY s.{col} DESC
            LIMIT 1
        """, (season, qual_min)).fetchone()

        if row and row[1]:
            name, val = row
            if isinstance(val, float):
                val_str = f".{int(val*1000):03d}"
            else:
                val_str = str(val)
            events.append({
                "headline": f"{name} leads MLB with {val_str} {label}",
                "detail": f"The current leader through early-season action.",
                "category": "Milestone",
                "game_date": latest_date,
                "player_names": [name],
                "team_names": [],
                "detection_type": f"leader_{col}",
                "priority": 3,
            })

    return events


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def detect_all(db_path=None, season=None):
    """Run all detectors, insert results, prune old events."""
    if db_path is None:
        db_path = DB_PATH

    conn = sqlite3.connect(db_path)
    conn.row_factory = None  # ensure tuples

    ensure_table(conn)

    # Auto-detect season if not provided
    if season is None:
        today = date.today()
        season = today.year

    latest_date = _get_latest_date(conn, season)
    if not latest_date:
        print(f"  No game logs found for season {season}")
        conn.close()
        return 0

    print(f"  Latest game date: {latest_date}")

    # Clear and rebuild — ensures stale events don't persist
    conn.execute("DELETE FROM notable_events")
    conn.commit()

    events = []

    # Tier 1
    print("  Running Tier 1 detectors...")
    events += detect_hitting_streaks(conn, season, latest_date, min_games=8)
    events += detect_onbase_streaks(conn, season, latest_date, min_games=12)
    events += detect_hr_streaks(conn, season, latest_date, min_games=4)
    events += detect_pitching_streaks(conn, season, latest_date)
    events += detect_season_pace(conn, season, latest_date)
    t1_count = len(events)
    print(f"    Tier 1: {t1_count} events")

    # Tier 2
    print("  Running Tier 2 detectors...")
    events += detect_career_milestones(conn, season, latest_date)
    events += detect_rarities(conn, season, latest_date)
    events += detect_hot_streaks_pelt(conn, season, latest_date)
    t2_count = len(events) - t1_count
    print(f"    Tier 2: {t2_count} events")

    # Tier 3 backfill if needed
    if len(events) < 3:
        print("  Running Tier 3 backfill...")
        events += detect_hitting_streaks_relaxed(conn, season, latest_date)
        events += detect_league_leaders(conn, season, latest_date)
        t3_count = len(events) - t1_count - t2_count
        print(f"    Tier 3: {t3_count} events")

    # Deduplicated insert
    cursor = conn.cursor()
    inserted = 0
    for e in events:
        try:
            cursor.execute("""
                INSERT OR IGNORE INTO notable_events
                (headline, detail, category, game_date, player_names, team_names,
                 detection_type, priority)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                e["headline"], e["detail"], e["category"], e["game_date"],
                json.dumps(e.get("player_names", [])),
                json.dumps(e.get("team_names", [])),
                e["detection_type"], e["priority"],
            ))
            if cursor.rowcount > 0:
                inserted += 1
        except sqlite3.IntegrityError:
            pass  # Duplicate, skip

    # Prune events older than 7 days
    cursor.execute("""
        DELETE FROM notable_events WHERE game_date < date(?, '-7 days')
    """, (latest_date,))
    pruned = cursor.rowcount

    conn.commit()
    conn.close()

    print(f"  Notable events: {inserted} new, {pruned} pruned, {len(events)} total detected")
    return len(events)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--season", type=int, default=None)
    args = parser.parse_args()
    detect_all(args.db, args.season)
