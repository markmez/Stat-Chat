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
    """Generate a historical context string like 'the longest since X in YYYY'.

    Scans one season at a time, most recent first, using Python to walk
    each player's game logs and find consecutive runs. Stops at the first
    season where someone matched.

    Returns empty string if streak is too short, too common, or no match found.
    """
    if streak_len < 10:
        return ""  # Short streaks happen all the time

    exclude_season = exclude_season or 0
    exclude_player = exclude_player or ""

    # Check recent seasons first, stop at first match
    seasons = conn.execute(f"""
        SELECT DISTINCT season FROM {table}
        WHERE season < ?
        ORDER BY season DESC
    """, (exclude_season,)).fetchall()

    for (szn,) in seasons:
        # Get all games for this season, ordered by player + date
        games = conn.execute(f"""
            SELECT player_id, ({condition_sql}) as met
            FROM {table}
            WHERE season = ? AND ({at_bat_filter})
            ORDER BY player_id, date
        """, (szn,)).fetchall()

        # Find max consecutive run per player in this season
        best_pid = None
        best_run = 0
        current_pid = None
        current_run = 0
        max_run_for_player = 0

        for pid, met in games:
            if pid != current_pid:
                # Finalize previous player
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

        # Finalize last player
        if current_pid and current_pid != exclude_player and max_run_for_player > best_run:
            best_run = max_run_for_player
            best_pid = current_pid

        if best_run >= streak_len:
            name = _player_name(conn, best_pid)
            return f"The longest since {name} ({best_run} games) in {szn}."

    return ""


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


def detect_single_game_performances(conn, season, latest_date):
    """Find standout single-game performances from recent games."""
    events = []

    # Outstanding batting games (last 2 dates)
    rows = conn.execute("""
        SELECT p.name, g.date, g.hits, g.home_runs, g.rbi, g.at_bats, g.doubles, g.triples
        FROM game_batting_logs g
        JOIN players p ON g.player_id = p.player_id
        WHERE g.season = ? AND g.date >= (
            SELECT DISTINCT date FROM game_batting_logs WHERE season = ?
            ORDER BY date DESC LIMIT 1 OFFSET 1
        )
        AND (g.home_runs >= 3 OR g.hits >= 5 OR g.rbi >= 6
             OR (g.hits >= 4 AND g.home_runs >= 2))
        ORDER BY g.home_runs DESC, g.hits DESC
    """, (season, season)).fetchall()

    for name, game_date, h, hr, rbi, ab, doubles, triples in rows:
        if hr and hr >= 3:
            headline = f"{name} hit {hr} home runs, going {h}-for-{ab} with {rbi} RBI."
            detail = ""
        elif h and h >= 5:
            headline = f"{name} went {h}-for-{ab} with {hr or 0} HR and {rbi or 0} RBI."
            detail = ""
        elif rbi and rbi >= 6:
            headline = f"{name} drove in {rbi} runs, going {h}-for-{ab} with {hr or 0} home runs."
            detail = ""
        else:
            headline = f"{name} went {h}-for-{ab} with {hr} HR and {rbi or 0} RBI."
            detail = ""

        events.append({
            "headline": headline,
            "detail": detail,
            "category": "Rarity",
            "game_date": game_date,
            "player_names": [name],
            "team_names": [],
            "detection_type": "big_batting_game",
            "priority": 2,
        })

    # Outstanding pitching games
    rows = conn.execute("""
        SELECT p.name, g.date, g.innings_pitched, g.strikeouts, g.hits,
               g.walks, g.earned_runs, g.ip_outs
        FROM game_pitching_logs g
        JOIN players p ON g.player_id = p.player_id
        WHERE g.season = ? AND g.date >= (
            SELECT DISTINCT date FROM game_pitching_logs WHERE season = ?
            ORDER BY date DESC LIMIT 1 OFFSET 1
        )
        AND (g.strikeouts >= 12
             OR (g.ip_outs >= 27 AND g.hits <= 1)
             OR (g.ip_outs >= 21 AND g.earned_runs = 0 AND g.strikeouts >= 10)
             OR (g.ip_outs >= 27 AND g.walks = 0 AND g.strikeouts >= 8))
        ORDER BY g.strikeouts DESC
    """, (season, season)).fetchall()

    for name, game_date, ip, so, h, bb, er, ip_outs in rows:
        ip_display = ip or f"{(ip_outs or 0) // 3}.{(ip_outs or 0) % 3}"
        if ip_outs and ip_outs >= 27 and (h is not None and h <= 1):
            headline = f"{name} threw a {h}-hitter over {ip_display} innings, striking out {so or 0}."
            detail = ""
        elif so and so >= 12:
            headline = f"{name} struck out {so} in {ip_display} innings, allowing {h or 0} hits and {er or 0} earned runs."
            detail = ""
        else:
            headline = f"{name} dominated over {ip_display} innings — {so or 0} strikeouts, {h or 0} hits, {er or 0} earned runs."
            detail = ""

        events.append({
            "headline": headline,
            "detail": detail,
            "category": "Rarity",
            "game_date": game_date,
            "player_names": [name],
            "team_names": [],
            "detection_type": "big_pitching_game",
            "priority": 2,
        })

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
    events += detect_single_game_performances(conn, season, latest_date)
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
