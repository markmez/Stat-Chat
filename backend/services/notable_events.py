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
import time
from datetime import date, datetime
from services.historical_scans import _get_game_line
from services.qualification import min_pa as _qual_min_pa

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
    "MIA": "Marlins", "OAK": "Athletics", "ATH": "Athletics",
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
            game_context TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(detection_type, game_date, headline)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_notable_date ON notable_events(game_date)
    """)
    # Migrate: add columns if missing
    cols = {row[1] for row in conn.execute("PRAGMA table_info(notable_events)").fetchall()}
    if "game_context" not in cols:
        conn.execute("ALTER TABLE notable_events ADD COLUMN game_context TEXT")
    if "expires_at" not in cols:
        conn.execute("ALTER TABLE notable_events ADD COLUMN expires_at TEXT")
    conn.commit()


def backfill_game_context(conn, season):
    """One-time: fill game_context for existing events that don't have it."""
    rows = conn.execute("""
        SELECT id, player_names, game_date FROM notable_events
        WHERE (game_context IS NULL OR game_context = '') AND player_names IS NOT NULL
    """).fetchall()

    if not rows:
        return 0

    updated = 0
    for row_id, player_names_json, game_date in rows:
        try:
            names = json.loads(player_names_json) if player_names_json else []
            if not names:
                continue
            pid_row = conn.execute(
                "SELECT player_id FROM players WHERE name = ?", (names[0],)
            ).fetchone()
            if not pid_row:
                continue
            context = _get_game_context(conn, pid_row[0], game_date, season)
            if context:
                conn.execute("UPDATE notable_events SET game_context = ? WHERE id = ?",
                             (context, row_id))
                updated += 1
        except:
            continue

    conn.commit()
    return updated


def _get_game_context(conn, player_id, game_date, season):
    """Build game context string like 'April 5 · Dodgers 4 - Astros 3'.

    Looks up the player's team and opponent from game logs, then fetches
    the actual score from the MLB Stats API.
    """
    # Get player's game info (team + opponent)
    game = conn.execute("""
        SELECT g.opponent, g.vishome
        FROM game_batting_logs g
        WHERE g.player_id = ? AND g.date = ? AND g.season = ?
        LIMIT 1
    """, (player_id, game_date, season)).fetchone()

    if not game:
        game = conn.execute("""
            SELECT g.opponent, g.vishome
            FROM game_pitching_logs g
            WHERE g.player_id = ? AND g.date = ? AND g.season = ?
            LIMIT 1
        """, (player_id, game_date, season)).fetchone()

    if not game:
        try:
            from datetime import datetime
            dt = datetime.strptime(game_date, "%Y-%m-%d")
            return dt.strftime("%B %-d")
        except:
            return game_date

    opponent, vishome = game

    # Get player's team from players table
    team_row = conn.execute(
        "SELECT team FROM players WHERE player_id = ?", (player_id,)
    ).fetchone()
    team_code = team_row[0] if team_row else None

    # Format date
    try:
        from datetime import datetime
        dt = datetime.strptime(game_date, "%Y-%m-%d")
        date_str = dt.strftime("%B %-d")
    except:
        date_str = game_date

    if not team_code:
        return date_str

    team_name = team_display(team_code)
    opp_name = team_display(opponent)

    # Fetch actual score from MLB Stats API
    team_runs, opp_runs = _fetch_game_score(game_date, team_code, opponent)
    if team_runs is None:
        return f"{date_str} · {team_name} vs {opp_name}"

    # Winning team listed first
    if team_runs >= opp_runs:
        return f"{date_str} · {team_name} {team_runs} - {opp_name} {opp_runs}"
    else:
        return f"{date_str} · {opp_name} {opp_runs} - {team_name} {team_runs}"


# Cache for MLB Stats API score lookups: {"YYYY-MM-DD": {(away, home): (away_runs, home_runs)}}
_score_cache: dict = {}


def _fetch_game_score(game_date, team_code, opponent_code):
    """Fetch the actual game score from the MLB Stats API.

    Returns (team_runs, opponent_runs) or (None, None) if not found.
    """
    import requests

    # Check cache first
    if game_date in _score_cache:
        cached = _score_cache[game_date]
        for (away, home), (away_r, home_r) in cached.items():
            if away == team_code and home == opponent_code:
                return away_r, home_r
            if home == team_code and away == opponent_code:
                return home_r, away_r
        return None, None

    # Fetch all games for this date and cache them
    try:
        resp = requests.get(
            "https://statsapi.mlb.com/api/v1/schedule",
            params={"sportId": 1, "date": game_date, "hydrate": "linescore"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return None, None

    from services.daily_games import _team_to_retro

    date_scores = {}
    dates = data.get("dates", [])
    games = dates[0].get("games", []) if dates else []
    for game in games:
        teams = game.get("teams", {})
        away_info = teams.get("away", {})
        home_info = teams.get("home", {})
        away_retro = _team_to_retro(away_info.get("team", {}))
        home_retro = _team_to_retro(home_info.get("team", {}))

        linescore = game.get("linescore", {})
        away_runs = linescore.get("teams", {}).get("away", {}).get("runs")
        home_runs = linescore.get("teams", {}).get("home", {}).get("runs")

        if away_retro and home_retro and away_runs is not None and home_runs is not None:
            date_scores[(away_retro, home_retro)] = (away_runs, home_runs)

    _score_cache[game_date] = date_scores

    # Look up this specific matchup
    for (away, home), (away_r, home_r) in date_scores.items():
        if away == team_code and home == opponent_code:
            return away_r, home_r
        if home == team_code and away == opponent_code:
            return home_r, away_r

    return None, None


def _get_latest_date(conn, season):
    """Get the most recent game date in the season."""
    row = conn.execute(
        "SELECT MAX(date) FROM game_batting_logs WHERE season = ?", (season,)
    ).fetchone()
    return row[0] if row and row[0] else None


def _ordinal(n):
    """Convert number to ordinal: 1st, 2nd, 3rd, 4th..."""
    n = int(n)
    if 11 <= n % 100 <= 13:
        return f"{n}th"
    return f"{n}{['th','st','nd','rd','th','th','th','th','th','th'][n % 10]}"


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
    # Only add "longest since" context for genuinely rare streaks
    # Short streaks (< 20 games) happen dozens of times per season
    if streak_len < 20:
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
            game_line, _ = _get_game_line(conn, pid, latest_date, season)
            context = _historical_context(
                conn, streak, "hits > 0",
                exclude_season=season, exclude_player=pid
            )
            intro = f"{name} went {game_line}" if game_line else name
            headline = f"{intro}, extending his hitting streak to {streak} straight games"
            if context:
                headline += f" — {context.lower()}"
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

    # Sort by streak length (longest first)
    events.sort(key=lambda e: e.get("_streak_len", 0), reverse=True)
    for e in events:
        e.pop("_streak_len", None)
    return events


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
            team = _player_team_display(conn, pid, season)
            game_line, _ = _get_game_line(conn, pid, latest_date, season)
            context = _historical_context(
                conn, streak,
                "(hits + walks + COALESCE(hit_by_pitch, 0)) > 0",
                exclude_season=season, exclude_player=pid,
                at_bat_filter="(at_bats > 0 OR walks > 0 OR COALESCE(hit_by_pitch, 0) > 0)"
            )
            intro = f"{name} went {game_line}" if game_line else name
            headline = f"{intro}, extending his on-base streak to {streak} straight games"
            if context:
                headline += f" — {context.lower()}"
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
    return events


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
            team = _player_team_display(conn, pid, season)
            # Get today's HR count and season total
            today_hr = conn.execute("""
                SELECT home_runs FROM game_batting_logs
                WHERE player_id = ? AND date = ? AND season = ?
            """, (pid, latest_date, season)).fetchone()
            season_hr = conn.execute("""
                SELECT home_runs FROM season_batting_stats
                WHERE player_id = ? AND season = ?
            """, (pid, season)).fetchone()
            hr_today = today_hr[0] if today_hr else 1
            hr_total = season_hr[0] if season_hr else 0
            game_line, _ = _get_game_line(conn, pid, latest_date, season)
            context = _historical_context(
                conn, streak, "home_runs > 0",
                exclude_season=season, exclude_player=pid
            )
            # Build intro with HR number
            ordinal = f"his {_ordinal(hr_total)}" if hr_total else "a"
            intro = f"{name} homered ({ordinal}) and has now gone deep in {streak} straight games"
            if context:
                intro += f" — {context.lower()}"
            else:
                intro += "."
            headline = intro
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

    return events


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
            last = starts[0]
            last_ip = f"{(last[1] or 0) // 3}.{(last[1] or 0) % 3}"
            last_so = last[4] or 0
            game_line = f"{name} threw {last_ip} scoreless IP with {last_so} K and "
            events.append({
                "headline": f"{game_line}now has {scoreless} consecutive scoreless starts.",
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
            last = starts[0]
            last_ip = f"{(last[1] or 0) // 3}.{(last[1] or 0) % 3}"
            last_er = last[2] or 0
            last_so = last[4] or 0
            game_line = f"{name} went {last_ip} IP, {last_er} ER, {last_so} K and "
            events.append({
                "headline": f"{game_line}now has {qs} consecutive quality starts (6+ IP, ≤3 ER).",
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
    """Fire once per player per threshold when their pace first crosses it.

    Only HR (50/60/70/80) and SB (50/60/70/80). Only after 2 weeks (~14 games).
    Checks notable_events to see if we've already reported this player+threshold.
    """
    events = []
    min_games = 14  # ~2 weeks

    pace_checks = [
        ("home_runs", "HR", [80, 70, 60, 50]),
        ("stolen_bases", "SB", [80, 70, 60, 50]),
    ]

    for stat_col, abbrev, thresholds in pace_checks:
        rows = conn.execute(f"""
            SELECT p.name, s.{stat_col}, s.games, p.team
            FROM season_batting_stats s
            JOIN players p ON s.player_id = p.player_id
            WHERE s.season = ? AND s.games >= ?
            AND s.{stat_col} > 0
            ORDER BY s.{stat_col} DESC
            LIMIT 20
        """, (season, min_games)).fetchall()

        for name, stat_val, games, team_code in rows:
            if not stat_val or games == 0:
                continue

            # Must have contributed to this stat on latest_date
            contributed = conn.execute(f"""
                SELECT {stat_col} FROM game_batting_logs g
                JOIN players p ON g.player_id = p.player_id
                WHERE p.name = ? AND g.date = ? AND g.{stat_col} > 0
            """, (name, latest_date)).fetchone()
            if not contributed:
                continue

            pace = int(stat_val * 162.0 / games)

            for threshold in thresholds:
                if pace >= threshold:
                    # Check if we already fired this player+threshold this season
                    already = conn.execute("""
                        SELECT 1 FROM notable_events
                        WHERE headline LIKE ? AND detection_type = ?
                        AND game_date >= ? LIMIT 1
                    """, (f"%{name}%", f"pace_{stat_col}_{threshold}",
                          f"{season}-01-01")).fetchone()

                    if already:
                        break  # Already reported this threshold, skip lower ones too

                    team = team_display(team_code) if team_code else ""
                    # Get last game line
                    game_row = conn.execute("""
                        SELECT hits, at_bats, home_runs, rbi, stolen_bases
                        FROM game_batting_logs g
                        JOIN players p ON g.player_id = p.player_id
                        WHERE p.name = ? AND g.date = ?
                    """, (name, latest_date)).fetchone()
                    game_intro = ""
                    if game_row:
                        gh, gab, ghr, grbi, gsb = [x or 0 for x in game_row]
                        parts = [f"{gh}-for-{gab}"]
                        if ghr: parts.append(f"{'a homer' if ghr == 1 else f'{ghr} homers'}")
                        if gsb: parts.append(f"{'a stolen base' if gsb == 1 else f'{gsb} steals'}")
                        if grbi: parts.append(f"{grbi} RBI")
                        game_intro = f"{name} went {parts[0]}"
                        if len(parts) > 1:
                            game_intro += f" with {', '.join(parts[1:])}"
                        game_intro += ". "
                    else:
                        game_intro = ""
                    projected = int(stat_val * 162 / games)
                    if game_intro:
                        pace_line = f"{game_intro}He now has {stat_val} {abbrev} and is on pace for {projected}."
                    else:
                        pace_line = f"{name} now has {stat_val} {abbrev} and is on pace for {projected}."
                    events.append({
                        "headline": pace_line,
                        "detail": "",
                        "category": "Milestone",
                        "game_date": latest_date,
                        "player_names": [name],
                        "team_names": [team] if team else [],
                        "detection_type": f"pace_{stat_col}_{threshold}",
                        "priority": 2,
                    })
                    break  # Only report highest new threshold

    return events


# ---------------------------------------------------------------------------
# Tier 2: Medium-signal detectors
# ---------------------------------------------------------------------------

# Iconic career milestone thresholds — these get the "last since X" anchor.
# Lower thresholds happen too often (1000 hits every year) to be worth it.
_ICONIC_CAREER_THRESHOLDS = {
    "home_runs": {500, 600, 700},
    "hits": {3000},
    "rbi": {2000, 2500, 3000},
    "stolen_bases": {500, 600},
    "strikeouts": {3000, 3500, 4000},  # pitching
    "wins": {300, 350},                 # pitching
    "saves": {400, 500},                # pitching
}


def _last_player_to_cross_career_threshold(conn, stat, threshold, exclude_player_id, is_pitching=False):
    """Find the most recent player to cross this career threshold, plus the
    total number of players who ever have. Returns (rank, name, year) or None."""
    table = "season_pitching_stats" if is_pitching else "season_batting_stats"
    rows = conn.execute(f"""
        SELECT p.name, x.season
        FROM (
            SELECT s.player_id, s.season,
                   SUM(s.{stat}) OVER (
                     PARTITION BY s.player_id ORDER BY s.season
                     ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                   ) AS cumul,
                   COALESCE(
                     SUM(s.{stat}) OVER (
                       PARTITION BY s.player_id ORDER BY s.season
                       ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
                     ), 0
                   ) AS prev_cumul
            FROM {table} s
        ) x
        JOIN players p ON p.player_id = x.player_id
        WHERE x.cumul >= ? AND x.prev_cumul < ? AND x.player_id != ?
        ORDER BY x.season DESC
    """, (threshold, threshold, exclude_player_id)).fetchall()
    if not rows:
        return None
    # Total players who ever crossed = len(rows) + 1 (plus this player)
    rank = len(rows) + 1
    last_name, last_year = rows[0]
    return (rank, last_name, last_year)


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
            game_date, stat_val = contributor_info[pid]
            for m in milestones:
                remaining = m - total
                if 1 <= remaining <= 5:
                    # Approaching milestone
                    action = action_fn(stat_val)
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
                elif remaining <= 0 and remaining > -stat_val:
                    # Just crossed milestone
                    action = action_fn(stat_val)
                    if col == "hits":
                        game_row = conn.execute("""
                            SELECT hits, at_bats FROM game_batting_logs
                            WHERE player_id = ? AND date = ? AND season = ?
                        """, (pid, game_date, season)).fetchone()
                        if game_row:
                            action = f"collected {game_row[0]} hit{'s' if game_row[0] != 1 else ''}"
                    headline = f"{name} {action}, reaching {m:,} {label}!"
                    # Iconic-threshold anchor: "Nth player ever; last since X in YEAR"
                    if m in _ICONIC_CAREER_THRESHOLDS.get(col, set()):
                        ctx = _last_player_to_cross_career_threshold(
                            conn, col, m, pid, is_pitching=False
                        )
                        if ctx:
                            rank, last_name, last_year = ctx
                            headline = headline.rstrip("!") + (
                                f" — the {_ordinal(rank)} player ever to reach {m:,} "
                                f"{label.replace('career ', '')}, and the first since "
                                f"{last_name} in {last_year}."
                            )
                    events.append({
                        "headline": headline,
                        "detail": "",
                        "category": "Milestone",
                        "game_date": game_date,
                        "player_names": [name],
                        "team_names": [],
                        "detection_type": f"career_{col}_{m}",
                        "priority": 1,
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
            game_date, stat_val = contributor_info[pid]
            for m in milestones:
                remaining = m - total
                if 1 <= remaining <= 5:
                    # Approaching milestone
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
                elif remaining <= 0 and remaining > -stat_val:
                    # Just crossed milestone (total passed m, and today's contribution pushed them over)
                    action = action_fn(stat_val)
                    headline = f"{name} {action}, reaching {m:,} {label}!"
                    if m in _ICONIC_CAREER_THRESHOLDS.get(col, set()):
                        ctx = _last_player_to_cross_career_threshold(
                            conn, col, m, pid, is_pitching=True
                        )
                        if ctx:
                            rank, last_name, last_year = ctx
                            headline = headline.rstrip("!") + (
                                f" — the {_ordinal(rank)} pitcher ever to reach {m:,} "
                                f"{label.replace('career ', '')}, and the first since "
                                f"{last_name} in {last_year}."
                            )
                    events.append({
                        "headline": headline,
                        "detail": "",
                        "category": "Milestone",
                        "game_date": game_date,
                        "player_names": [name],
                        "team_names": [],
                        "detection_type": f"career_p_{col}_{m}",
                        "priority": 1,
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

    _streak_phrases = ["is on a tear", "is on a hot streak", "has been red hot", "is locked in"]
    _refire_phrases = ["is still rolling", "is still locked in", "is still red hot", "keeps it going"]
    STREAK_COOLDOWN_DAYS = 5
    REFIRE_LOOKBACK_DAYS = 14  # Within this window, treat a re-surface as "still"

    for idx, (name, ops, avg, num_games, hr, h, ab, obp, slg, season_ops) in enumerate(rows):
        # Cooldown: skip if this player had a hot_streak_pelt event within the last N days
        recent = conn.execute("""
            SELECT 1 FROM notable_events
            WHERE detection_type = 'hot_streak_pelt'
              AND headline LIKE ?
              AND game_date > date(?, '-' || ? || ' days')
              AND game_date < ?
            LIMIT 1
        """, (f"%{name}%", latest_date, STREAK_COOLDOWN_DAYS, latest_date)).fetchone()
        if recent:
            continue

        # Refire detection: is this a "still going" event? Past cooldown but
        # still within the lookback window means the player was surfaced
        # recently and is being re-reported. Use different phrasing that
        # acknowledges continuity instead of pretending it's fresh.
        prior = conn.execute("""
            SELECT game_date FROM notable_events
            WHERE detection_type = 'hot_streak_pelt'
              AND headline LIKE ?
              AND game_date > date(?, '-' || ? || ' days')
              AND game_date < ?
            ORDER BY game_date DESC
            LIMIT 1
        """, (f"%{name}%", latest_date, REFIRE_LOOKBACK_DAYS, latest_date)).fetchone()
        is_refire = prior is not None
        prior_date = prior[0] if prior else None

        # Get last game line for context
        game_row = conn.execute("""
            SELECT g.hits, g.at_bats, g.home_runs, g.rbi, g.walks
            FROM game_batting_logs g
            JOIN players p ON g.player_id = p.player_id
            WHERE p.name = ? AND g.date = ?
        """, (name, latest_date)).fetchone()

        # "On a tear" needs to match today's line. PELT windows are rolling, so
        # a 0-for-4 (or a sit-out) still "qualifies" statistically — but putting
        # that headline next to "on a tear" reads as wrong. Require 2+ H OR a HR
        # today. Silently skip otherwise; we DON'T want to burn the player's
        # 5-day cooldown on an event that never actually rendered, so no row is
        # written to notable_events and no visible signal is emitted.
        gh = (game_row[0] or 0) if game_row else 0
        gab = (game_row[1] or 0) if game_row else 0
        ghr = (game_row[2] or 0) if game_row else 0
        grbi = (game_row[3] or 0) if game_row else 0
        if gh < 2 and ghr < 1:
            continue

        game_intro = ""
        if game_row:
            parts = [f"{gh}-for-{gab}"]
            if ghr:
                parts.append(f"{'a homer' if ghr == 1 else f'{ghr} homers'}")
            if grbi:
                parts.append(f"{grbi} RBI")
            if len(parts) == 1:
                game_intro = f"{name} went {parts[0]} and "
            else:
                game_intro = f"{name} went {parts[0]} with {', '.join(parts[1:])} and "

        # Slash line + HR
        if avg and obp and slg:
            slash = f".{int(avg*1000):03d}/.{int(obp*1000):03d}/.{int(slg*1000):03d}"
        else:
            slash = f"{_fmt_ops(ops)} OPS"
        hr_part = f" with {hr} HR" if hr else ""

        # Vary the language. Refires get "still rolling"-style phrasing so
        # the feed doesn't read as "he was hot, now fresh news: he's hot"
        # when the player has been continuously hot. Current_form's own
        # variance-resistant algorithm tends to pick a longer window for
        # refires, so the stats themselves already reflect the longer arc.
        if is_refire:
            phrase = _refire_phrases[idx % len(_refire_phrases)]
        else:
            phrase = _streak_phrases[idx % len(_streak_phrases)]

        if game_intro:
            headline = f"{game_intro}{phrase} — {slash}{hr_part} over the last {num_games} games."
        else:
            headline = f"{name} {phrase} — {slash}{hr_part} over the last {num_games} games."

        events.append({
            "headline": headline,
            "detail": "",
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
    min_pa = _qual_min_pa(conn, season)

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
# Tonight's Matchup Previews
# ---------------------------------------------------------------------------

def detect_matchup_previews(conn, season):
    """Generate matchup preview feed cards for tonight's games.

    Selection: pick top 3 pitchers by career ERA (240+ IP, sub-3.50 ERA) or from
    the manual prominence list. Suppress pitchers featured in last 12 days.
    Then find the best opposing batter by career OPS (800+ PA).
    Time-gated: weekdays noon ET+, weekends 9 AM ET+.
    """
    events = []

    try:
        from services.daily_games import get_todays_games
        from services.name_matcher import match_player
    except ImportError:
        return events

    games = get_todays_games()
    if not games:
        return events

    # Load prominence list from config
    import json, os
    config_path = os.path.join(os.path.dirname(__file__), "stat_config.json")
    prominence_list = set()
    try:
        with open(config_path) as f:
            cfg = json.load(f)
        prominence_list = set(cfg.get("pitcher_prominence_list", []))
    except Exception:
        pass

    from datetime import timedelta
    today = date.today().isoformat()
    suppression_cutoff = (date.today() - timedelta(days=12)).isoformat()

    # Step 1: Get recently featured pitchers (suppress for 12 days)
    suppressed = set()
    try:
        rows = conn.execute("""
            SELECT player_names FROM notable_events
            WHERE detection_type = 'matchup_preview' AND game_date > ?
        """, (suppression_cutoff,)).fetchall()
        for r in rows:
            names = json.loads(r[0]) if r[0] else []
            if len(names) >= 2:
                suppressed.add(names[1])  # pitcher is second in player_names
    except Exception:
        pass

    # Step 2: Collect all tonight's starters with game info
    starters = []  # (pitcher_name, matched_pitcher, batting_team, pitcher_team, game)
    for game in games:
        away_starter = game.get("away_starter")
        home_starter = game.get("home_starter")
        away_team = game.get("away", "")
        home_team = game.get("home", "")

        if not away_starter or not home_starter:
            continue

        # Home starter faces away batters, away starter faces home batters
        for pitcher_raw, pitcher_team, batting_team in [
            (home_starter, home_team, away_team),
            (away_starter, away_team, home_team),
        ]:
            matched = match_player(pitcher_raw)
            if matched:
                starters.append((pitcher_raw, matched, batting_team, pitcher_team, game))

    if not starters:
        return events

    # Step 3: Bulk career ERA query for all matched pitcher names
    pitcher_names = list(set(s[1] for s in starters))
    placeholders = ",".join("?" * len(pitcher_names))
    career_rows = conn.execute(f"""
        SELECT p.name,
               SUM(s.innings_pitched) as career_ip,
               SUM(s.earned_runs) * 9.0 / NULLIF(SUM(s.innings_pitched), 0) as career_era
        FROM season_pitching_stats s
        JOIN players p ON s.player_id = p.player_id
        WHERE p.name IN ({placeholders})
        GROUP BY p.name
    """, pitcher_names).fetchall()
    career_stats = {r[0]: (r[1], r[2]) for r in career_rows}

    # Step 4: Score and rank pitchers
    # Qualified: career ERA < 3.50 with 240+ IP, OR on prominence list
    def pitcher_score(matched_name):
        ip, era = career_stats.get(matched_name, (0, 99))
        on_prominence = matched_name in prominence_list
        qualified = (ip >= 240 and era < 3.50) or on_prominence
        if not qualified:
            return None
        # Prominence list pitchers without enough IP sort after qualified pitchers
        # but before unqualified ones — use a synthetic ERA of 3.49
        sort_era = era if ip >= 240 else 3.49
        return sort_era

    scored = []
    for pitcher_raw, matched, batting_team, pitcher_team, game in starters:
        score = pitcher_score(matched)
        if score is not None:
            scored.append((score, matched, batting_team, pitcher_team, game))
    # Sort by ERA (best first)
    scored.sort(key=lambda x: x[0])

    # Step 5: Pick top 3, respecting suppression
    selected = []
    for score, matched, batting_team, pitcher_team, game in scored:
        if matched in suppressed:
            continue
        # Avoid two matchups from the same game
        if any(s[4] is game for s in selected):
            continue
        selected.append((score, matched, batting_team, pitcher_team, game))
        if len(selected) >= 3:
            break

    # Step 6: If fewer than 3, relax suppression
    if len(selected) < 3:
        for score, matched, batting_team, pitcher_team, game in scored:
            if any(s[1] == matched for s in selected):
                continue
            if any(s[4] is game for s in selected):
                continue
            selected.append((score, matched, batting_team, pitcher_team, game))
            if len(selected) >= 3:
                break

    # Step 7: For each selected pitcher, find best opposing batter by career OPS (800+ PA)
    # Show first 2 in feed, use 3rd as example text for "try searching" hint
    matchup_data = []  # (batter_name, pitcher_name, batting_team, pitcher_team, game, compelling)
    for _, pitcher_name, batting_team, pitcher_team, game in selected:
        top_batter = conn.execute("""
            SELECT p.name
            FROM season_batting_stats cur
            JOIN players p ON cur.player_id = p.player_id
            JOIN (
                SELECT player_id,
                       (SUM(hits)+SUM(walks)+SUM(hit_by_pitch))*1.0
                           /NULLIF(SUM(at_bats)+SUM(walks)+SUM(hit_by_pitch)+SUM(sacrifice_flies),0)
                       + (SUM(hits)-SUM(doubles)-SUM(triples)-SUM(home_runs)
                          +2*SUM(doubles)+3*SUM(triples)+4*SUM(home_runs))*1.0
                           /NULLIF(SUM(at_bats),0) as career_ops,
                       SUM(plate_appearances) as career_pa
                FROM season_batting_stats
                GROUP BY player_id
                HAVING career_pa >= 800
            ) career ON cur.player_id = career.player_id
            WHERE cur.season = ? AND cur.team = ?
            ORDER BY career.career_ops DESC LIMIT 1
        """, (season, batting_team)).fetchone()

        if not top_batter:
            continue

        batter_name = top_batter[0]

        compelling = _find_compelling_matchup_stat(
            conn, batter_name, pitcher_name, season
        )
        if not compelling:
            compelling = f"{batter_name} faces {pitcher_name} tonight."

        matchup_data.append((batter_name, pitcher_name, batting_team, pitcher_team, game, compelling))

    # Build example hint from 3rd matchup (if available)
    example_hint = ""
    if len(matchup_data) >= 3:
        ex_batter = matchup_data[2][0].split()[-1]  # Last name
        ex_pitcher = matchup_data[2][1].split()[-1]
        example_hint = 'Search any "player vs pitcher" matchup or "player tonight" for other previews.'

    # Generate feed events for first 2 only
    for batter_name, pitcher_name, batting_team, pitcher_team, game, compelling in matchup_data[:2]:
        compelling += " See more in this"

        game_context = "Matchup Preview"

        hint_part = f"[MATCHUP_HINT]{example_hint}[/MATCHUP_HINT]" if example_hint else ""
        events.append({
            "headline": compelling,
            "detail": f" matchup preview.{hint_part}",
            "category": "Tonight",
            "game_date": today,
            "expires_at": game.get("start_time", ""),
            "player_names": [batter_name, pitcher_name],
            "team_names": [team_display(batting_team), team_display(pitcher_team)],
            "detection_type": "matchup_preview",
            "priority": 1,
            "game_context": game_context,
        })

    return events


def _parse_game_time_et(start_time):
    """Parse ISO start time to 'H:MM AM/PM ET' string."""
    if not start_time:
        return ""
    try:
        from datetime import datetime, timedelta, timezone
        dt = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
        et = dt - timedelta(hours=4)
        hour = et.hour
        minute = et.minute
        ampm = "PM" if hour >= 12 else "AM"
        if hour > 12: hour -= 12
        if hour == 0: hour = 12
        return f"{hour}:{minute:02d} {ampm} ET"
    except Exception:
        return ""


def _fmt_ops(val):
    """Format OPS/rate stat: .995 not 0.995, 1.024 stays 1.024."""
    if val < 1.0:
        return f".{int(round(val * 1000)):03d}"
    return f"{val:.3f}"


def _find_compelling_matchup_stat(conn, batter_name, pitcher_name, season):
    """Find one compelling stat for a matchup preview card.

    Tiers (first match wins):
    1. H2H history (any PA)
    2. Pitch mix angle (best/worst pitch matchup from pitcher's arsenal)
    3. Platoon split (.800+ or .650-, 50+ PA)
    4. PELT current form (if exists — player is in a detected streak)
    5. Fallback: season OPS vs season ERA
    """
    try:
        cur = conn.cursor()

        # --- Tier 1: H2H history (even 1 PA) ---
        cur.execute("""
            SELECT SUM(h.plate_appearances), SUM(h.hits), SUM(h.home_runs), SUM(h.at_bats)
            FROM head_to_head h
            JOIN players pb ON h.batter_id = pb.player_id
            JOIN players pp ON h.pitcher_id = pp.player_id
            WHERE pb.name = ? AND pp.name = ?
        """, (batter_name, pitcher_name))
        h2h = cur.fetchone()
        if h2h and h2h[0] and h2h[0] >= 1:
            pa, hits, hr, ab = h2h
            if hr and hr >= 1:
                return f"{batter_name} is {hits}-for-{ab} with {hr} HR in {pa} career PA against {pitcher_name}."
            elif ab and ab > 0:
                return f"{batter_name} is {hits}-for-{ab} in {pa} career PA against {pitcher_name}."

        # --- Tier 2: Pitch mix angle ---
        # Find pitcher's top pitches, check batter's splits against them
        pitcher_mix = None
        for szn in [season, season - 1]:
            cur.execute("""
                SELECT pts.pitch_type, pts.plate_appearances
                FROM pitch_type_pitching_splits pts
                JOIN players p ON pts.player_id = p.player_id
                WHERE p.name = ? AND pts.season = ?
                ORDER BY pts.plate_appearances DESC
            """, (pitcher_name, szn))
            rows = cur.fetchall()
            total_pa = sum(r[1] for r in rows) if rows else 0
            if total_pa >= 50:
                pitcher_mix = rows
                break

        if pitcher_mix:
            total_pa = sum(r[1] for r in pitcher_mix)
            # Check batter's splits against each pitch (use 2025 fallback)
            batter_pitch = {}
            for szn in [season, season - 1]:
                cur.execute("""
                    SELECT pts.pitch_type, pts.ops, pts.plate_appearances, pts.batting_avg
                    FROM pitch_type_batting_splits pts
                    JOIN players p ON pts.player_id = p.player_id
                    WHERE p.name = ? AND pts.season = ?
                """, (batter_name, szn))
                rows = cur.fetchall()
                if rows and sum(r[2] for r in rows) >= 50:
                    batter_pitch = {r[0]: r for r in rows}
                    break

            if batter_pitch:
                # Find most extreme matchup among pitcher's top pitches (>= 10% of mix)
                best_angle = None
                for pitch_type, pitcher_pa in pitcher_mix:
                    mix_pct = pitcher_pa / total_pa
                    if mix_pct < 0.10:
                        continue
                    bp = batter_pitch.get(pitch_type)
                    if not bp or bp[2] < 10:
                        continue
                    ops = bp[1]
                    pct_label = round(mix_pct * 100)
                    if ops >= 0.800:
                        if not best_angle or ops > best_angle[0]:
                            best_angle = (ops, f"{batter_name} has hit {_fmt_ops(ops)} OPS against {pitch_type.lower()}s — {pct_label}% of {pitcher_name}'s pitch mix.")
                    elif ops <= 0.650:
                        if not best_angle or ops < best_angle[0]:
                            best_angle = (-ops, f"{batter_name} has struggled against {pitch_type.lower()}s ({_fmt_ops(ops)} OPS) — {pct_label}% of {pitcher_name}'s pitch mix.")
                if best_angle:
                    return best_angle[1]

        # --- Tier 3: Platoon split (.800+ or .650-) ---
        cur.execute("SELECT throws FROM players WHERE name = ?", (pitcher_name,))
        hand_row = cur.fetchone()
        if hand_row and hand_row[0]:
            pitcher_hand = hand_row[0]
            split_key = "vs_LHP" if pitcher_hand == "L" else "vs_RHP"
            split = None
            for szn in [season, season - 1]:
                cur.execute("""
                    SELECT ps.ops, ps.home_runs, ps.plate_appearances
                    FROM platoon_splits ps
                    JOIN players p ON ps.player_id = p.player_id
                    WHERE p.name = ? AND ps.season = ? AND ps.split = ?
                """, (batter_name, szn, split_key))
                row = cur.fetchone()
                if row and row[2] and row[2] >= 50:
                    split = row
                    break

            if split and split[0]:
                ops = split[0]
                hand_label = "lefties" if pitcher_hand == "L" else "righties"
                if ops >= 0.800:
                    return f"{batter_name} has hit {_fmt_ops(ops)} OPS against {hand_label} — {pitcher_name} throws {pitcher_hand}HP."
                elif ops <= 0.650:
                    return f"{batter_name} has hit just {_fmt_ops(ops)} OPS against {hand_label} — {pitcher_name} throws {pitcher_hand}HP."

        # --- Tier 4: PELT current form ---
        cur.execute("""
            SELECT cf.ops, cf.num_games, cf.batting_avg
            FROM current_form cf
            JOIN players p ON cf.player_id = p.player_id
            WHERE p.name = ? AND cf.season = ?
        """, (batter_name, season))
        form = cur.fetchone()
        if form and form[0]:
            return f"{batter_name} is hitting {_fmt_ops(form[0])} OPS over his last {form[1]} games heading into tonight."

        # --- Tier 5: Fallback — season OPS vs season ERA ---
        cur.execute("""
            SELECT s.ops FROM season_batting_stats s
            JOIN players p ON s.player_id = p.player_id
            WHERE p.name = ? AND s.season = ?
            ORDER BY s.plate_appearances DESC LIMIT 1
        """, (batter_name, season))
        batter_ops_row = cur.fetchone()
        cur.execute("""
            SELECT s.era FROM season_pitching_stats s
            JOIN players p ON s.player_id = p.player_id
            WHERE p.name = ? AND s.season = ?
            ORDER BY s.ip_outs DESC LIMIT 1
        """, (pitcher_name, season))
        pitcher_era_row = cur.fetchone()
        if batter_ops_row and pitcher_era_row:
            bops = batter_ops_row[0]
            pera = pitcher_era_row[0]
            if bops and pera:
                return f"{batter_name} ({bops:.3f} OPS) faces {pitcher_name} ({pera:.2f} ERA) tonight."

    except Exception:
        pass

    return None


# ---------------------------------------------------------------------------
# On This Date — historic moments from today's date in past years
# ---------------------------------------------------------------------------

def detect_on_this_date(conn, season, latest_date):
    """Find historic moments from today's month-day in past seasons.

    Very high threshold — no-hitters, 4+ HR games, 20+ K, etc.
    Returns 1-2 items max to avoid competing with current events.
    """
    events = []

    try:
        today = date.today().isoformat()  # Use today's actual date, not latest game date
        month_day = today[5:]  # "04-09" from "2026-04-09"
    except:
        return events

    # No-hitters on this date
    nohitters = conn.execute("""
        SELECT p.name, g.season, g.innings_pitched, g.strikeouts, g.walks, g.opponent
        FROM game_pitching_logs g
        JOIN players p ON g.player_id = p.player_id
        WHERE substr(g.date, 6) = ? AND g.season < ? AND g.ip_outs >= 27 AND g.hits = 0
        ORDER BY g.season DESC
    """, (month_day, season)).fetchall()

    for name, yr, ip, so, bb, opp in nohitters[:1]:  # Just the most recent
        opp_name = team_display(opp) if opp else ""
        headline = f"On this date in {yr}, {name} threw a no-hitter"
        if so:
            headline += f" with {so} strikeouts"
        if opp_name:
            headline += f" against the {opp_name}"
        headline += "."
        events.append({
            "headline": headline,
            "detail": "",
            "category": "On This Date",
            "game_date": today,
            "player_names": [name],
            "team_names": [opp_name] if opp_name else [],
            "detection_type": "on_this_date",
            "priority": 3,
        })

    # 4+ HR games on this date
    big_hr = conn.execute("""
        SELECT p.name, g.season, g.home_runs, g.hits, g.at_bats, g.rbi, g.opponent
        FROM game_batting_logs g
        JOIN players p ON g.player_id = p.player_id
        WHERE substr(g.date, 6) = ? AND g.season < ? AND g.home_runs >= 4
        ORDER BY g.home_runs DESC, g.season DESC
    """, (month_day, season)).fetchall()

    for name, yr, hr, h, ab, rbi, opp in big_hr[:1]:
        opp_name = team_display(opp) if opp else ""
        headline = f"On this date in {yr}, {name} hit {hr} home runs"
        if opp_name:
            headline += f" against the {opp_name}"
        headline += f", going {h}-for-{ab} with {rbi or 0} RBI."
        events.append({
            "headline": headline,
            "detail": "",
            "category": "On This Date",
            "game_date": today,
            "player_names": [name],
            "team_names": [opp_name] if opp_name else [],
            "detection_type": "on_this_date",
            "priority": 3,
        })

    # 18+ K games on this date (extremely rare)
    big_k = conn.execute("""
        SELECT p.name, g.season, g.strikeouts, g.innings_pitched, g.ip_outs,
               g.hits, g.earned_runs, g.opponent
        FROM game_pitching_logs g
        JOIN players p ON g.player_id = p.player_id
        WHERE substr(g.date, 6) = ? AND g.season < ? AND g.strikeouts >= 18
        ORDER BY g.strikeouts DESC, g.season DESC
    """, (month_day, season)).fetchall()

    for name, yr, so, ip, ip_outs, h, er, opp in big_k[:1]:
        opp_name = team_display(opp) if opp else ""
        ip_display = ip or f"{(ip_outs or 0) // 3}.{(ip_outs or 0) % 3}"
        headline = f"On this date in {yr}, {name} struck out {so} in {ip_display} innings"
        if opp_name:
            headline += f" against the {opp_name}"
        headline += "."
        events.append({
            "headline": headline,
            "detail": "",
            "category": "On This Date",
            "game_date": today,
            "player_names": [name],
            "team_names": [opp_name] if opp_name else [],
            "detection_type": "on_this_date",
            "priority": 3,
        })

    # 10+ RBI game on this date (extremely rare)
    big_rbi = conn.execute("""
        SELECT p.name, g.season, g.rbi, g.hits, g.at_bats, g.home_runs, g.opponent
        FROM game_batting_logs g
        JOIN players p ON g.player_id = p.player_id
        WHERE substr(g.date, 6) = ? AND g.season < ? AND g.rbi >= 10
        ORDER BY g.rbi DESC, g.season DESC
    """, (month_day, season)).fetchall()

    for name, yr, rbi, h, ab, hr, opp in big_rbi[:1]:
        opp_name = team_display(opp) if opp else ""
        headline = f"On this date in {yr}, {name} drove in {rbi} runs, going {h}-for-{ab} with {hr or 0} home runs"
        if opp_name:
            headline += f" against the {opp_name}"
        headline += "."
        events.append({
            "headline": headline,
            "detail": "",
            "category": "On This Date",
            "game_date": today,
            "player_names": [name],
            "team_names": [opp_name] if opp_name else [],
            "detection_type": "on_this_date",
            "priority": 3,
        })

    # Iconic career milestone crossings on this date — from historic_moments table.
    # Ordered most recent first, cap to 2 so we don't flood.
    try:
        moments = conn.execute("""
            SELECT player_name, season, stat, threshold, context
            FROM historic_moments
            WHERE substr(date, 6) = ? AND season < ?
            ORDER BY season DESC, threshold DESC
            LIMIT 2
        """, (month_day, season)).fetchall()
        for pname, yr, stat, threshold, context in moments:
            # context looks like "hit his 500th career home run"
            headline = f"On this date in {yr}, {pname} {context}."
            events.append({
                "headline": headline,
                "detail": "",
                "category": "On This Date",
                "game_date": today,
                "player_names": [pname],
                "team_names": [],
                "detection_type": "on_this_date",
                "priority": 3,
            })
    except Exception as e:
        # Table may not exist yet; silently skip
        print(f"  On This Date milestones check failed (non-fatal): {e}")

    return events


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

DETECTION_LOCK = "/tmp/statchat_detection.lock"


def detect_for_players(db_path, season, player_ids):
    """Targeted event detection for specific players whose games just ended.

    Called by the poll after new game logs are added. Only computes streaks
    and milestones for the given player_ids, not the full slate.
    """
    import time as _time

    if is_detection_locked():
        print("  Detection locked (daily pipeline running) — skipping")
        return 0

    conn = sqlite3.connect(db_path)
    conn.row_factory = None
    ensure_table(conn)

    latest_date = _get_latest_date(conn, season)
    if not latest_date:
        conn.close()
        return 0

    print(f"  Targeted detection for {len(player_ids)} players, latest_date={latest_date}")

    events = []

    # Streaks — only for the specific players
    for pid in player_ids:
        name = _player_name(conn, pid)
        if not name:
            continue

        # Hitting streak
        games = conn.execute("""
            SELECT date, hits, at_bats FROM game_batting_logs
            WHERE player_id = ? AND season = ? AND at_bats > 0
            ORDER BY date DESC
        """, (pid, season)).fetchall()

        if games:
            streak = 0
            for gd, hits, ab in games:
                if hits > 0:
                    streak += 1
                else:
                    break
            if streak >= 8:
                team = _player_team_display(conn, pid, season)
                context = _historical_context(conn, streak, "hits > 0",
                                              exclude_season=season, exclude_player=pid)
                headline = f"{name} has hit safely in {streak} straight games"
                if context:
                    headline += f", {context.lower()}"
                else:
                    headline += "."
                events.append({
                    "headline": headline, "detail": "", "category": "Streak",
                    "game_date": latest_date, "player_names": [name],
                    "team_names": [team] if team else [],
                    "detection_type": "hitting_streak", "priority": 1,
                })

            # On-base streak
            ob_games = conn.execute("""
                SELECT date, hits, walks, COALESCE(hit_by_pitch, 0) as hbp
                FROM game_batting_logs
                WHERE player_id = ? AND season = ?
                    AND (at_bats > 0 OR walks > 0 OR COALESCE(hit_by_pitch, 0) > 0)
                ORDER BY date DESC
            """, (pid, season)).fetchall()

            ob_streak = 0
            for gd, hits, walks, hbp in ob_games:
                if (hits + walks + hbp) > 0:
                    ob_streak += 1
                else:
                    break
            if ob_streak >= 12:
                team = _player_team_display(conn, pid, season)
                context = _historical_context(
                    conn, ob_streak, "(hits + walks + COALESCE(hit_by_pitch, 0)) > 0",
                    exclude_season=season, exclude_player=pid)
                headline = f"{name} has reached base in {ob_streak} straight games"
                if context:
                    headline += f", {context.lower()}"
                else:
                    headline += "."
                events.append({
                    "headline": headline, "detail": "", "category": "Streak",
                    "game_date": latest_date, "player_names": [name],
                    "team_names": [team] if team else [],
                    "detection_type": "onbase_streak", "priority": 1,
                })

            # HR streak
            hr_streak = 0
            for gd, hits, ab in games:
                hr = conn.execute("""
                    SELECT home_runs FROM game_batting_logs
                    WHERE player_id = ? AND date = ? AND season = ?
                    LIMIT 1
                """, (pid, gd, season)).fetchone()
                if hr and hr[0] and hr[0] > 0:
                    hr_streak += 1
                else:
                    break
            if hr_streak >= 4:
                context = _historical_context(conn, hr_streak, "home_runs > 0",
                                              exclude_season=season, exclude_player=pid)
                headline = f"{name} has homered in {hr_streak} straight games"
                if context:
                    headline += f", {context.lower()}"
                else:
                    headline += "."
                events.append({
                    "headline": headline, "detail": "", "category": "Streak",
                    "game_date": latest_date, "player_names": [name],
                    "team_names": [],
                    "detection_type": "hr_streak", "priority": 1,
                })

    if not events:
        print("  No notable events for these players")
        conn.close()
        return 0

    # Remove stale streak events for these players on this date, then insert
    streak_types = {"hitting_streak", "onbase_streak", "hr_streak"}
    cursor = conn.cursor()
    for e in events:
        if e["detection_type"] in streak_types:
            cursor.execute("""
                DELETE FROM notable_events
                WHERE detection_type = ? AND game_date = ? AND player_names = ?
            """, (e["detection_type"], e["game_date"],
                  json.dumps(e.get("player_names", []))))

    inserted = 0
    for e in events:
        # Look up game context
        player_names = e.get("player_names", [])
        game_context = None
        if player_names:
            pid_row = conn.execute(
                "SELECT player_id FROM players WHERE name = ?", (player_names[0],)
            ).fetchone()
            if pid_row:
                game_context = _get_game_context(conn, pid_row[0], e["game_date"], season)
        if not game_context:
            try:
                from datetime import datetime
                dt = datetime.strptime(e["game_date"], "%Y-%m-%d")
                game_context = dt.strftime("%B %-d")
            except:
                game_context = e["game_date"]

        try:
            cursor.execute("""
                INSERT OR IGNORE INTO notable_events
                (headline, detail, category, game_date, player_names, team_names,
                 detection_type, priority, game_context, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                e["headline"], e["detail"], e["category"], e["game_date"],
                json.dumps(e.get("player_names", [])),
                json.dumps(e.get("team_names", [])),
                e["detection_type"], e["priority"], game_context,
                e.get("expires_at", ""),
            ))
            if cursor.rowcount > 0:
                inserted += 1
        except sqlite3.IntegrityError:
            pass

    conn.commit()
    print(f"  Inserted {inserted} targeted events")
    conn.close()
    return inserted


def detect_alltime_passing(conn, season, latest_date):
    """Detect when an active player passes someone on an all-time career list.

    Checks HR, Hits, RBI, SB, 2B (batting) and W, K (pitching).
    Fires when today's game pushed a player's career total past someone
    ranked in the top N all-time for that stat.
    """
    events = []

    CONFIGS = [
        # (col, table, game_table, label, abbrev, top_n)
        ("home_runs",     "season_batting_stats",  "game_batting_logs",   "home runs",    "HR",  75),
        ("hits",          "season_batting_stats",  "game_batting_logs",   "hits",         "H",   150),
        ("rbi",           "season_batting_stats",  "game_batting_logs",   "RBI",          "RBI", 150),
        ("stolen_bases",  "season_batting_stats",  "game_batting_logs",   "stolen bases", "SB",  100),
        ("doubles",       "season_batting_stats",  "game_batting_logs",   "doubles",      "2B",  100),
        ("wins",          "season_pitching_stats", "game_pitching_logs",  "wins",         "W",   75),
        ("strikeouts",    "season_pitching_stats", "game_pitching_logs",  "strikeouts",   "K",   75),
    ]

    # Derived game-log stats: (sql_for_leaderboard, game_table, label, abbrev, top_n, trigger_condition)
    DERIVED_CONFIGS = [
        (
            """SELECT g.player_id, p.name, COUNT(*) as career_total
               FROM game_batting_logs g JOIN players p ON g.player_id = p.player_id
               WHERE g.home_runs >= 2
               GROUP BY g.player_id ORDER BY career_total DESC""",
            "game_batting_logs", "multi-HR games", "multi-HR", 50,
            "g.home_runs >= 2",  # trigger: today's game had 2+ HR
        ),
        (
            """SELECT g.player_id, p.name, COUNT(*) as career_total
               FROM game_batting_logs g JOIN players p ON g.player_id = p.player_id
               WHERE g.home_runs >= 3
               GROUP BY g.player_id ORDER BY career_total DESC""",
            "game_batting_logs", "3-HR games", "3-HR", 25,
            "g.home_runs >= 3",  # trigger: today's game had 3+ HR
        ),
    ]

    for col, table, game_table, label, abbrev, top_n in CONFIGS:
        # Build all-time career leaderboard
        all_time = conn.execute(f"""
            SELECT s.player_id, p.name, SUM(s.{col}) as career_total
            FROM {table} s JOIN players p ON s.player_id = p.player_id
            GROUP BY s.player_id
            HAVING career_total > 0
            ORDER BY career_total DESC
        """).fetchall()

        if len(all_time) < top_n:
            continue

        # Build rank lookup: player_id → (rank, name, total)
        rank_map = {}
        for rank, (pid, name, total) in enumerate(all_time, 1):
            rank_map[pid] = (rank, name, total)

        # Threshold: minimum career total to be in the top N
        cutoff_total = all_time[top_n - 1][2]

        # Find active players who played on latest_date and have career totals near the cutoff
        # Check game contribution on latest_date
        game_col = col
        if col == "wins":
            game_col = "win"  # game_pitching_logs uses 'win' not 'wins'

        active_with_games = conn.execute(f"""
            SELECT DISTINCT g.player_id
            FROM {game_table} g
            WHERE g.date = ? AND g.season = ?
        """, (latest_date, season)).fetchall()

        for (pid,) in active_with_games:
            if pid not in rank_map:
                continue
            player_rank, player_name, career_total = rank_map[pid]

            # Only care about players in or near the top N
            if career_total < cutoff_total - 5:
                continue

            # Get today's contribution
            if col == "wins":
                contrib = conn.execute("""
                    SELECT SUM(CASE WHEN win = 1 THEN 1 ELSE 0 END)
                    FROM game_pitching_logs
                    WHERE player_id = ? AND date = ? AND season = ?
                """, (pid, latest_date, season)).fetchone()[0] or 0
            else:
                contrib = conn.execute(f"""
                    SELECT SUM({col}) FROM {game_table}
                    WHERE player_id = ? AND date = ? AND season = ?
                """, (pid, latest_date, season)).fetchone()[0] or 0

            if contrib == 0:
                continue

            career_before = career_total - contrib

            # Find the highest-ranked person this player just passed
            best_passed = None  # (rank, name, total)
            for passed_rank, (passed_pid, passed_name, passed_total) in enumerate(all_time, 1):
                if passed_pid == pid:
                    continue
                if passed_rank > top_n:
                    break
                if career_before < passed_total and career_total >= passed_total:
                    if best_passed is None or passed_rank < best_passed[0]:
                        best_passed = (passed_rank, passed_name, passed_total)

            if best_passed:
                passed_rank, passed_name, _ = best_passed
                game_line, _ = _get_game_line(conn, pid, latest_date, season)
                team = conn.execute(
                    "SELECT team FROM players WHERE player_id = ?", (pid,)
                ).fetchone()
                team_name = team[0] if team else ""

                ordinal = _ordinal(passed_rank)
                if game_line:
                    headline = (
                        f"{player_name} went {game_line}. "
                        f"He now has {career_total} career {label}, "
                        f"passing {passed_name} for {ordinal} on the all-time list."
                    )
                else:
                    headline = (
                        f"{player_name} now has {career_total} career {label}, "
                        f"passing {passed_name} for {ordinal} on the all-time list."
                    )

                events.append({
                    "headline": headline,
                    "detail": "",
                    "category": "Milestone",
                    "game_date": latest_date,
                    "player_names": [player_name, passed_name],
                    "team_names": [team_name] if team_name else [],
                    "detection_type": f"alltime_passing_{col}",
                    "priority": 1,
                })

            # --- All-time RECORD approach/break (only #1 spot) ---
            # Use verified record totals — our DB starts at 1898, so some
            # all-time leaders have incomplete data (e.g., Cy Young 511 W
            # but we only have 295 from 1898+).
            VERIFIED_RECORDS = {
                "home_runs":    ("Barry Bonds", 762),
                "hits":         ("Pete Rose", 4256),
                "rbi":          ("Hank Aaron", 2297),
                "stolen_bases": ("Rickey Henderson", 1406),
                "doubles":      ("Tris Speaker", 792),  # BBRef: 792; our DB has 793
                "wins":         ("Cy Young", 511),
                "strikeouts":   ("Nolan Ryan", 5714),
            }
            verified = VERIFIED_RECORDS.get(col)
            if verified:
                record_name, record_total = verified
            else:
                record_name, record_total = all_time[0][1], all_time[0][2]
            record_pid = all_time[0][0]  # still need pid to avoid self-check

            if pid != record_pid and contrib > 0:
                gap = record_total - career_total
                prev_gap = record_total - career_before

                if career_total > record_total and career_before <= record_total:
                    # Broke the record
                    game_line, _ = _get_game_line(conn, pid, latest_date, season)
                    player_name = rank_map[pid][1]
                    team = conn.execute(
                        "SELECT team FROM players WHERE player_id = ?", (pid,)
                    ).fetchone()
                    team_name = team[0] if team else ""
                    if game_line:
                        headline = (
                            f"{player_name} went {game_line}. "
                            f"He has set a new all-time record with {career_total} career {label}, "
                            f"passing {record_name} ({record_total})."
                        )
                    else:
                        headline = (
                            f"{player_name} has set a new all-time record with {career_total} career {label}, "
                            f"passing {record_name} ({record_total})."
                        )
                    events.append({
                        "headline": headline,
                        "detail": "",
                        "category": "Milestone",
                        "game_date": latest_date,
                        "player_names": [player_name, record_name],
                        "team_names": [team_name] if team_name else [],
                        "detection_type": f"alltime_record_broken_{col}",
                        "priority": 0,
                    })
                elif 0 < gap <= 5 and prev_gap > gap:
                    # Approaching the record
                    game_line, _ = _get_game_line(conn, pid, latest_date, season)
                    player_name = rank_map[pid][1]
                    team = conn.execute(
                        "SELECT team FROM players WHERE player_id = ?", (pid,)
                    ).fetchone()
                    team_name = team[0] if team else ""
                    if game_line:
                        headline = (
                            f"{player_name} went {game_line}. "
                            f"He now has {career_total} career {label}, "
                            f"just {gap} away from {record_name}'s all-time record of {record_total}."
                        )
                    else:
                        headline = (
                            f"{player_name} now has {career_total} career {label}, "
                            f"just {gap} away from {record_name}'s all-time record of {record_total}."
                        )
                    events.append({
                        "headline": headline,
                        "detail": "",
                        "category": "Milestone",
                        "game_date": latest_date,
                        "player_names": [player_name, record_name],
                        "team_names": [team_name] if team_name else [],
                        "detection_type": f"alltime_record_approach_{col}",
                        "priority": 0,
                    })

    # --- Derived game-log stats (multi-HR games, 3-HR games) ---
    # These scan game logs, so we only compute counts for triggered players
    # (those who had a qualifying game today), not the full leaderboard.
    for leaderboard_sql, game_table, label, abbrev, top_n, trigger_cond in DERIVED_CONFIGS:
        # Find players who had a qualifying game today (e.g., 2+ HR)
        triggered = conn.execute(f"""
            SELECT g.player_id, p.name
            FROM {game_table} g JOIN players p ON g.player_id = p.player_id
            WHERE g.date = ? AND g.season = ? AND {trigger_cond}
        """, (latest_date, season)).fetchall()

        if not triggered:
            continue

        # Build the full leaderboard once (cached per config), but only
        # if someone was triggered today. This is expensive (~10s) but
        # only runs on days someone actually hit 2+ or 3+ HR.
        all_time_derived = conn.execute(leaderboard_sql).fetchall()
        if len(all_time_derived) < top_n:
            continue

        derived_rank_map = {}
        for rank, (dpid, dname, dtotal) in enumerate(all_time_derived, 1):
            derived_rank_map[dpid] = (rank, dname, dtotal)

        for pid, player_name in triggered:
            if pid not in derived_rank_map:
                continue
            player_rank, _, career_total = derived_rank_map[pid]
            if player_rank > top_n:
                continue

            career_before = career_total - 1

            # Find best person passed
            best_passed = None
            for pr, (ppid, pname, ptotal) in enumerate(all_time_derived, 1):
                if ppid == pid or pr > top_n:
                    break
                if career_before < ptotal and career_total >= ptotal:
                    if best_passed is None or pr < best_passed[0]:
                        best_passed = (pr, pname, ptotal)

            if best_passed:
                passed_rank, passed_name, _ = best_passed
                game_line, _ = _get_game_line(conn, pid, latest_date, season)
                team = conn.execute(
                    "SELECT team FROM players WHERE player_id = ?", (pid,)
                ).fetchone()
                team_name = team[0] if team else ""

                ordinal = _ordinal(passed_rank)
                if game_line:
                    headline = (
                        f"{player_name} went {game_line}. "
                        f"That's his {_ordinal(career_total)} career {label}, "
                        f"passing {passed_name} for {ordinal} on the all-time list."
                    )
                else:
                    headline = (
                        f"{player_name} now has {career_total} career {label}, "
                        f"passing {passed_name} for {ordinal} on the all-time list."
                    )

                events.append({
                    "headline": headline,
                    "detail": "",
                    "category": "Milestone",
                    "game_date": latest_date,
                    "player_names": [player_name, passed_name],
                    "team_names": [team_name] if team_name else [],
                    "detection_type": f"alltime_passing_{abbrev.lower().replace('-', '_')}",
                    "priority": 1,
                })

    return events


def detect_franchise_passing(conn, season, latest_date):
    """Detect when a player passes someone on their franchise's all-time career list,
    or approaches within 5 of the franchise record.

    Checks top 5 per franchise for HR, Hits, RBI, SB, 2B (batting) and W, K (pitching).
    """
    from .franchise import get_franchise_codes, get_franchise_name
    events = []

    CONFIGS = [
        ("home_runs",     "season_batting_stats",  "game_batting_logs",   "home runs",    "HR"),
        ("hits",          "season_batting_stats",  "game_batting_logs",   "hits",         "H"),
        ("rbi",           "season_batting_stats",  "game_batting_logs",   "RBI",          "RBI"),
        ("stolen_bases",  "season_batting_stats",  "game_batting_logs",   "stolen bases", "SB"),
        ("doubles",       "season_batting_stats",  "game_batting_logs",   "doubles",      "2B"),
        ("wins",          "season_pitching_stats", "game_pitching_logs",  "wins",         "W"),
        ("strikeouts",    "season_pitching_stats", "game_pitching_logs",  "strikeouts",   "K"),
    ]

    TOP_N = 5
    APPROACH_WITHIN = 5

    for col, table, game_table, label, abbrev in CONFIGS:
        # Get today's contributors with their current team
        if col == "wins":
            contrib_rows = conn.execute(f"""
                SELECT g.player_id, SUM(CASE WHEN g.win = 1 THEN 1 ELSE 0 END) as val,
                       (SELECT team FROM season_pitching_stats
                        WHERE player_id = g.player_id AND season = ? LIMIT 1) as team
                FROM game_pitching_logs g
                WHERE g.date = ? AND g.season = ?
                GROUP BY g.player_id
                HAVING val > 0
            """, (season, latest_date, season)).fetchall()
        else:
            contrib_rows = conn.execute(f"""
                SELECT g.player_id, SUM(g.{col}) as val,
                       (SELECT team FROM season_batting_stats
                        WHERE player_id = g.player_id AND season = ? LIMIT 1) as team
                FROM {game_table} g
                WHERE g.date = ? AND g.season = ?
                GROUP BY g.player_id
                HAVING val > 0
            """, (season, latest_date, season)).fetchall()

        if not contrib_rows:
            continue

        # Group contributors by franchise (only query each franchise once)
        teams_to_check = {}  # team_code → [(pid, contrib)]
        for pid, contrib, team in contrib_rows:
            if not team:
                continue
            team = team.split("/")[0].strip()
            teams_to_check.setdefault(team, []).append((pid, contrib))

        for team, player_contribs in teams_to_check.items():
            franchise_codes = get_franchise_codes(team)
            ph = ",".join(["?"] * len(franchise_codes))
            franchise_name = get_franchise_name(team)

            # Franchise career leaderboard (top 10 for context)
            leaders = conn.execute(f"""
                SELECT s.player_id, p.name, SUM(s.{col}) as career_total
                FROM {table} s JOIN players p ON s.player_id = p.player_id
                WHERE s.team IN ({ph})
                GROUP BY s.player_id
                HAVING career_total > 0
                ORDER BY career_total DESC
                LIMIT 10
            """, franchise_codes).fetchall()

            if len(leaders) < 2:
                continue

            record_holder_pid, record_holder_name, record_total = leaders[0]

            for pid, contrib in player_contribs:
                # Find this player in the leaderboard
                player_entry = None
                for rank, (lpid, lname, ltotal) in enumerate(leaders, 1):
                    if lpid == pid:
                        player_entry = (rank, lname, ltotal)
                        break

                if not player_entry or player_entry[0] > TOP_N + 2:
                    continue

                player_rank, player_name, career_total = player_entry
                career_before = career_total - contrib

                # Check if player has only played for this franchise
                other_teams = conn.execute(f"""
                    SELECT COUNT(DISTINCT team) FROM {table}
                    WHERE player_id = ? AND team NOT IN ({ph})
                """, (pid, *franchise_codes)).fetchone()[0]
                is_lifer = other_teams == 0

                # Build franchise context phrase
                fn = franchise_name[:-1] if franchise_name.endswith("s") else franchise_name
                if is_lifer:
                    career_phrase = f"{career_total} career {label}"
                    franchise_suffix = f" in {franchise_name} history"
                else:
                    career_phrase = f"{career_total} career {label} as a {fn}"
                    franchise_suffix = " in franchise history"

                # Check for passing someone in top N
                best_passed = None
                for rank, (lpid, lname, ltotal) in enumerate(leaders, 1):
                    if lpid == pid or rank > TOP_N:
                        continue
                    if career_before < ltotal and career_total >= ltotal:
                        if best_passed is None or rank < best_passed[0]:
                            best_passed = (rank, lname, ltotal)

                if best_passed:
                    passed_rank, passed_name, _ = best_passed
                    game_line, _ = _get_game_line(conn, pid, latest_date, season)
                    ordinal = _ordinal(passed_rank)
                    if game_line:
                        headline = (
                            f"{player_name} went {game_line}. "
                            f"He now has {career_phrase}, "
                            f"passing {passed_name} for {ordinal}{franchise_suffix}."
                        )
                    else:
                        headline = (
                            f"{player_name} now has {career_phrase}, "
                            f"passing {passed_name} for {ordinal}{franchise_suffix}."
                        )

                    events.append({
                        "headline": headline,
                        "detail": "",
                        "category": "Milestone",
                        "game_date": latest_date,
                        "player_names": [player_name, passed_name],
                        "team_names": [franchise_name],
                        "detection_type": f"franchise_passing_{col}",
                        "priority": 1,
                    })

                # Check for approaching franchise record (within 5)
                if pid != record_holder_pid:
                    gap = record_total - career_total
                    prev_gap = record_total - career_before
                    if 0 < gap <= APPROACH_WITHIN and prev_gap > gap:
                        game_line, _ = _get_game_line(conn, pid, latest_date, season)
                        if game_line:
                            headline = (
                                f"{player_name} went {game_line}. "
                                f"He now has {career_phrase}, "
                                f"just {gap} away from {record_holder_name}'s franchise record of {record_total}."
                            )
                        else:
                            headline = (
                                f"{player_name} now has {career_phrase}, "
                                f"just {gap} away from {record_holder_name}'s franchise record of {record_total}."
                            )

                        events.append({
                            "headline": headline,
                            "detail": "",
                            "category": "Milestone",
                            "game_date": latest_date,
                            "player_names": [player_name, record_holder_name],
                            "team_names": [franchise_name],
                            "detection_type": f"franchise_record_approach_{col}",
                            "priority": 1,
                        })

    return events


def _ordinal(n):
    """Convert number to ordinal string: 1 → '1st', 2 → '2nd', etc."""
    if 11 <= (n % 100) <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def is_detection_locked():
    """Check if the full pipeline is running (polls should skip detection)."""
    import os
    if not os.path.exists(DETECTION_LOCK):
        return False
    # Stale lock (older than 30 min) — ignore it
    age = time.time() - os.path.getmtime(DETECTION_LOCK)
    if age > 1800:
        os.remove(DETECTION_LOCK)
        return False
    return True


def detect_all(db_path=None, season=None, from_poll=False):
    """Run all detectors, insert results, prune old events.

    from_poll=True: called from the 15-min poll. Will skip if the daily
    pipeline is running (lock file present).
    """
    if from_poll and is_detection_locked():
        print("  Detection locked (daily pipeline running) — skipping")
        return 0

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

    # Don't wipe old events — let them age out via the retention window.
    # INSERT OR IGNORE handles dedup (UNIQUE constraint on headline + date).

    # Backfill game_context for any events that don't have it yet
    backfilled = backfill_game_context(conn, season)
    if backfilled:
        print(f"  Backfilled game_context for {backfilled} events")

    events = []

    # Dynamic streak thresholds — higher bar as season progresses
    games_played = conn.execute(
        "SELECT MAX(games) FROM season_batting_stats WHERE season = ?", (season,)
    ).fetchone()
    gp = games_played[0] if games_played and games_played[0] else 10
    hit_streak_min = max(8, min(15, int(gp * 0.75)))
    onbase_streak_min = max(12, min(25, int(gp * 1.0)))
    hr_streak_min = max(3, min(5, int(gp * 0.3)))

    # Tier 1
    print(f"  Running Tier 1 detectors... (gp={gp}, hit_min={hit_streak_min}, ob_min={onbase_streak_min}, hr_min={hr_streak_min})")
    events += detect_hitting_streaks(conn, season, latest_date, min_games=hit_streak_min)
    events += detect_onbase_streaks(conn, season, latest_date, min_games=onbase_streak_min)
    events += detect_hr_streaks(conn, season, latest_date, min_games=hr_streak_min)
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

    # Records & personal bests
    print("  Running records detection...")
    try:
        from routers.admin import _simulate_records_for_date
        record_events = _simulate_records_for_date(conn, latest_date)
        for re in record_events:
            player_names = [re.get("player", "")] if re.get("player") else []
            team_names = [re.get("team", "")] if re.get("team") else []
            events.append({
                "headline": re["detail"],
                "detail": "",
                "category": re["type"].replace("_", " ").title(),
                "game_date": latest_date,
                "player_names": player_names,
                "team_names": team_names,
                "detection_type": re["type"],
                "priority": 2,
            })
        print(f"    Records: {len(record_events)} events")
    except Exception as e:
        print(f"    Records detection failed: {e}")

    # All-time career list passing
    print("  Running all-time passing detection...")
    try:
        passing_events = detect_alltime_passing(conn, season, latest_date)
        events += passing_events
        print(f"    All-time passing: {len(passing_events)} events")
    except Exception as e:
        print(f"    All-time passing failed: {e}")

    # Franchise career list passing
    print("  Running franchise passing detection...")
    try:
        franchise_events = detect_franchise_passing(conn, season, latest_date)
        events += franchise_events
        print(f"    Franchise passing: {len(franchise_events)} events")
    except Exception as e:
        print(f"    Franchise passing failed: {e}")

    # Tier 3 backfill if needed
    if len(events) < 3:
        print("  Running Tier 3 backfill...")
        events += detect_hitting_streaks_relaxed(conn, season, latest_date)
        events += detect_league_leaders(conn, season, latest_date)
        t3_count = len(events) - t1_count - t2_count
        print(f"    Tier 3: {t3_count} events")

    # Historical scans (DB-verified facts with templates)
    try:
        from services.historical_scans import run_all_scans, template_facts
        print("  Running historical scans...")
        hist_facts = run_all_scans(conn, season, latest_date)
        hist_events = template_facts(conn, hist_facts, season, latest_date)
        for he in hist_events:
            events.append({
                "headline": he["headline"],
                "detail": "",
                "category": he.get("category", "historical"),
                "game_date": latest_date,
                "player_names": he.get("player_names", []),
                "team_names": he.get("team_names", []),
                "detection_type": "historical_scan",
                "priority": 1,
            })
        print(f"    Historical: {len(hist_events)} events")
    except Exception as e:
        print(f"    Historical scans failed: {e}")

    # Tonight's matchup previews — wipe previous previews for today first
    # (multiple pipeline runs would otherwise accumulate 3 per run)
    print("  Running matchup previews...")
    today_str = date.today().isoformat()
    try:
        conn.execute("""
            DELETE FROM notable_events
            WHERE detection_type = 'matchup_preview' AND game_date = ?
        """, (today_str,))
        conn.commit()
    except Exception:
        pass
    try:
        preview_events = detect_matchup_previews(conn, season)
        events += preview_events
        print(f"    Matchup previews: {len(preview_events)} events")
    except Exception as e:
        print(f"    Matchup previews failed: {e}")

    # On This Date — historic moments from today's date in past years
    print("  Running On This Date...")
    otd_events = detect_on_this_date(conn, season, latest_date)
    events += otd_events
    print(f"    On This Date: {len(otd_events)} events")

    # Remove ALL streak events for latest_date — full recompute replaces them
    # This ensures stale streaks (from bad data) get cleaned up even if the
    # player no longer qualifies for a streak event
    # Wipe ALL events for latest_date — full recompute replaces them.
    # This is the only safe approach: each poll/pipeline run produces a complete
    # set of events for the latest date from the current DB state. Any events
    # from prior runs (which may have been computed on incomplete data) are removed.
    # Events for prior dates are NOT touched — they were computed when that date
    # was latest_date and the data was complete.
    conn.execute("""
        DELETE FROM notable_events WHERE game_date = ? AND detection_type != 'ai_insight'
    """, (latest_date,))
    conn.commit()

    # Suppress hot_streak_pelt for players who already have other events today
    players_with_events = set()
    for e in events:
        if e.get("detection_type") != "hot_streak_pelt" and e.get("game_date") == latest_date:
            for name in e.get("player_names", []):
                players_with_events.add(name)
    events = [
        e for e in events
        if not (e.get("detection_type") == "hot_streak_pelt"
                and any(n in players_with_events for n in e.get("player_names", [])))
    ]

    # Deduplicated insert with game context
    cursor = conn.cursor()
    inserted = 0
    for e in events:
        # Use event's own game_context if provided (e.g. matchup previews set theirs)
        game_context = e.get("game_context")

        # Otherwise look up from game logs
        if not game_context:
            player_names = e.get("player_names", [])
            if player_names:
                first_name = player_names[0]
                pid_row = conn.execute(
                    "SELECT player_id FROM players WHERE name = ?", (first_name,)
                ).fetchone()
                if pid_row:
                    game_context = _get_game_context(conn, pid_row[0], e["game_date"], season)

        if not game_context:
            try:
                from datetime import datetime
                dt = datetime.strptime(e["game_date"], "%Y-%m-%d")
                game_context = dt.strftime("%B %-d")
            except:
                game_context = e["game_date"]

        try:
            cursor.execute("""
                INSERT OR IGNORE INTO notable_events
                (headline, detail, category, game_date, player_names, team_names,
                 detection_type, priority, game_context, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                e["headline"], e["detail"], e["category"], e["game_date"],
                json.dumps(e.get("player_names", [])),
                json.dumps(e.get("team_names", [])),
                e["detection_type"], e["priority"], game_context,
                e.get("expires_at", ""),
            ))
            if cursor.rowcount > 0:
                inserted += 1
        except sqlite3.IntegrityError:
            pass  # Duplicate, skip

    # Prune: keep 7 days, but if fewer than 5 events remain, keep up to 14 days
    cursor.execute("""
        DELETE FROM notable_events WHERE game_date < date(?, '-14 days')
    """, (latest_date,))
    pruned = cursor.rowcount

    # Check if we have enough recent events
    recent_count = cursor.execute("""
        SELECT COUNT(*) FROM notable_events WHERE game_date >= date(?, '-7 days')
    """, (latest_date,)).fetchone()[0]

    if recent_count >= 5:
        # Plenty of recent events — prune the 7-14 day old ones
        cursor.execute("""
            DELETE FROM notable_events WHERE game_date < date(?, '-7 days')
        """, (latest_date,))
        pruned += cursor.rowcount

    conn.commit()

    # Refresh historical_streaks leaderboard if any player broke a
    # ranking-worthy streak today (20+ hitting or 30+ on-base)
    try:
        from services.historical_scans import check_if_historical_streaks_rebuild_needed
        if check_if_historical_streaks_rebuild_needed(conn, season, latest_date):
            print("  Historical streaks leaderboard: rebuild triggered")
            from data_pipeline.build_historical_streaks import build
            conn.close()  # build() opens its own connection
            build(db_path or DB_PATH)
            conn = sqlite3.connect(db_path or DB_PATH)
    except Exception as e:
        print(f"  Historical streaks rebuild check failed (non-fatal): {e}")

    conn.close()

    print(f"  Notable events: {inserted} new, {pruned} pruned, {len(events)} total detected")

    # Archive events permanently (metering.db — not pruned)
    try:
        from services.metering import archive_events
        archived = archive_events(events)
        print(f"  Archived {archived} events")
    except Exception as e:
        print(f"  Archive failed: {e}")

    return len(events)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--season", type=int, default=None)
    args = parser.parse_args()
    detect_all(args.db, args.season)
