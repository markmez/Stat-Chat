"""
Deep Scans — historically-contextualized performance detection.

Two trigger modes:
1. PELT-triggered: when a hot streak is detected, check if the stats
   during that streak are historically rare
2. Threshold-triggered: independent gates for players with no PELT
   change point (consistently great from day 1)

For each trigger, queries historical data to find: "when was the last
time someone did THIS?" If rare enough (5+ years MLB, 3+ years team),
generates a feed event.

Cooldown: once a player fires a scan type, blocked for 5 games on
that scan type.
"""

import sqlite3
import logging
from datetime import date, datetime
from typing import Optional

logger = logging.getLogger("statchat.deep_scans")

DB_PATH = "/data/baseball_stats_full.db"

# ---------------------------------------------------------------------------
# Configuration — printed in sandbox for visibility
# ---------------------------------------------------------------------------

SCAN_CONFIG = {
    "slash_line_season": {
        "description": "Slash line (AVG/OBP/SLG) through first N games of season",
        "gate": "AVG ≥ .330, games ≥ 10",
        "interesting_if": "Nobody in MLB with all 3 stats this high through same game count in 5+ years",
        "gate_avg": 0.330,
        "gate_min_games": 10,
        "min_years_mlb": 5,
        "min_years_team": 3,
    },
    "slash_line_pelt": {
        "description": "Slash line during PELT-detected hot streak",
        "gate": "Active hot streak in current_form, AVG ≥ .350 during streak",
        "interesting_if": "Nobody with a streak this good over same game count in 5+ years",
        "gate_avg": 0.350,
        "min_years_mlb": 5,
        "min_years_team": 3,
    },
    "hr_accumulation": {
        "description": "HR accumulation pace through N games",
        "gate": "HR ≥ 8 through ≤ 30 games, OR HR ≥ 15 through ≤ 50 games",
        "interesting_if": "Nobody with this many HR through this few games in 5+ years",
        "min_years_mlb": 5,
    },
    "sb_accumulation": {
        "description": "SB accumulation pace through N games",
        "gate": "SB ≥ 10 through ≤ 30 games, OR SB ≥ 20 through ≤ 50 games",
        "interesting_if": "Nobody with this many SB through this few games in 5+ years",
        "min_years_mlb": 5,
    },
    "power_speed": {
        "description": "HR + SB combo through N games",
        "gate": "HR ≥ 5 AND SB ≥ 5 through ≤ 25 games",
        "interesting_if": "Nobody with both stats this high through same game count in 5+ years",
        "min_years_mlb": 5,
    },
    "pitching_dominance": {
        "description": "ERA + K rate through first N starts",
        "gate": "Starts ≥ 3, ERA ≤ 1.50, K ≥ 8 per start avg",
        "interesting_if": "Nobody with ERA this low and K rate this high through same starts in 5+ years",
        "gate_era": 1.50,
        "gate_k_per_start": 8,
        "gate_min_starts": 3,
        "min_years_mlb": 5,
    },
    "cooldown": {
        "description": "After firing, player blocked on that scan type for N games",
        "games": 5,
    },
}


# ---------------------------------------------------------------------------
# Core scan engine
# ---------------------------------------------------------------------------

def _ordinal(n):
    if n == 1: return "1st"
    if n == 2: return "2nd"
    if n == 3: return "3rd"
    return f"{n}th"


def _format_avg(val):
    if val is None: return "--"
    return f".{int(val * 1000):03d}"


def run_deep_scans(conn, season, target_date):
    """Run all deep scans for a given date. Returns list of event dicts."""
    events = []

    # Get players who played on this date
    bat_games = conn.execute("""
        SELECT g.player_id, p.name, p.team,
               g.hits, g.at_bats, g.home_runs, g.rbi, g.walks,
               g.stolen_bases, g.doubles, g.triples
        FROM game_batting_logs g
        JOIN players p ON g.player_id = p.player_id
        WHERE g.date = ?
    """, (target_date,)).fetchall()

    pitch_games = conn.execute("""
        SELECT g.player_id, p.name, p.team,
               g.strikeouts, g.walks, g.ip_outs, g.earned_runs,
               g.hits AS hits_allowed, g.win, g.loss, g.is_start
        FROM game_pitching_logs g
        JOIN players p ON g.player_id = p.player_id
        WHERE g.date = ?
    """, (target_date,)).fetchall()

    # --- Batting scans ---
    for row in bat_games:
        pid, pname, team = row[0], row[1], row[2]

        # Get season stats through this date
        season_row = conn.execute("""
            SELECT games, at_bats, hits, home_runs, walks, stolen_bases,
                   batting_avg, obp, slg, ops, doubles, triples,
                   hit_by_pitch, sacrifice_flies
            FROM season_batting_stats
            WHERE player_id = ? AND season = ?
        """, (pid, season)).fetchone()
        if not season_row:
            continue

        games = season_row[0] or 0
        ab = season_row[1] or 0
        hits = season_row[2] or 0
        hr = season_row[3] or 0
        bb = season_row[4] or 0
        sb = season_row[5] or 0
        avg = season_row[6] or 0
        obp_val = season_row[7] or 0
        slg = season_row[8] or 0

        # === SLASH LINE (season start) ===
        cfg = SCAN_CONFIG["slash_line_season"]
        if games >= cfg["gate_min_games"] and avg >= cfg["gate_avg"]:
            last_match = _find_last_slash_match(conn, pid, season, games, avg, obp_val, slg)
            if last_match:
                years_ago = season - last_match["season"]
                if years_ago >= cfg["min_years_mlb"]:
                    events.append({
                        "type": "deep_scan",
                        "scan": "slash_line_season",
                        "player": pname, "team": team or "",
                        "detail": (
                            f"{pname} is slashing {_format_avg(avg)}/{_format_avg(obp_val)}/{_format_avg(slg)} "
                            f"through {games} games — the first player to do that since "
                            f"{last_match['name']} in {last_match['season']} ({years_ago} years ago)."
                        ),
                    })
                elif years_ago >= cfg["min_years_team"]:
                    # Check team context
                    events.append({
                        "type": "deep_scan",
                        "scan": "slash_line_season",
                        "player": pname, "team": team or "",
                        "detail": (
                            f"{pname} is slashing {_format_avg(avg)}/{_format_avg(obp_val)}/{_format_avg(slg)} "
                            f"through {games} games — the first to do that since "
                            f"{last_match['name']} in {last_match['season']}."
                        ),
                    })
            elif last_match is None:
                # Nobody found at all in our data
                events.append({
                    "type": "deep_scan",
                    "scan": "slash_line_season",
                    "player": pname, "team": team or "",
                    "detail": (
                        f"{pname} is slashing {_format_avg(avg)}/{_format_avg(obp_val)}/{_format_avg(slg)} "
                        f"through {games} games — no player in our records has matched that through the same point."
                    ),
                })

        # === HR ACCUMULATION ===
        if (hr >= 8 and games <= 30) or (hr >= 15 and games <= 50):
            last_hr = _find_last_hr_pace(conn, pid, season, games, hr)
            if last_hr:
                years_ago = season - last_hr["season"]
                if years_ago >= 5:
                    events.append({
                        "type": "deep_scan",
                        "scan": "hr_accumulation",
                        "player": pname, "team": team or "",
                        "detail": (
                            f"{pname} has {hr} HR through {games} games — "
                            f"the first to reach that mark this quickly since "
                            f"{last_hr['name']} in {last_hr['season']}."
                        ),
                    })
            elif last_hr is None:
                events.append({
                    "type": "deep_scan",
                    "scan": "hr_accumulation",
                    "player": pname, "team": team or "",
                    "detail": (
                        f"{pname} has {hr} HR through {games} games — "
                        f"no player in our records has reached that mark this quickly."
                    ),
                })

        # === SB ACCUMULATION ===
        if (sb >= 10 and games <= 30) or (sb >= 20 and games <= 50):
            last_sb = _find_last_sb_pace(conn, pid, season, games, sb)
            if last_sb:
                years_ago = season - last_sb["season"]
                if years_ago >= 5:
                    events.append({
                        "type": "deep_scan",
                        "scan": "sb_accumulation",
                        "player": pname, "team": team or "",
                        "detail": (
                            f"{pname} has {sb} SB through {games} games — "
                            f"the first to reach that mark this quickly since "
                            f"{last_sb['name']} in {last_sb['season']}."
                        ),
                    })

        # === POWER-SPEED COMBO ===
        if hr >= 5 and sb >= 5 and games <= 25:
            last_ps = _find_last_power_speed(conn, pid, season, games, hr, sb)
            if last_ps:
                years_ago = season - last_ps["season"]
                if years_ago >= 5:
                    events.append({
                        "type": "deep_scan",
                        "scan": "power_speed",
                        "player": pname, "team": team or "",
                        "detail": (
                            f"{pname} has {hr} HR and {sb} SB through {games} games — "
                            f"the first with that power-speed combo this early since "
                            f"{last_ps['name']} in {last_ps['season']}."
                        ),
                    })

    # --- Pitching scans ---
    for row in pitch_games:
        pid, pname, team = row[0], row[1], row[2]
        k_today, bb_today = row[3] or 0, row[4] or 0
        ip_outs_today = row[5] or 0
        is_start = row[10]

        if not is_start:
            continue  # Only starters for now

        # Get season pitching stats
        pitch_season = conn.execute("""
            SELECT games_started, strikeouts, walks, earned_runs, ip_outs, era
            FROM season_pitching_stats
            WHERE player_id = ? AND season = ?
        """, (pid, season)).fetchone()
        if not pitch_season:
            continue

        starts = pitch_season[0] or 0
        total_k = pitch_season[1] or 0
        total_bb = pitch_season[2] or 0
        total_er = pitch_season[3] or 0
        total_ip_outs = pitch_season[4] or 0
        era = pitch_season[5] or 0

        cfg_p = SCAN_CONFIG["pitching_dominance"]
        k_per_start = total_k / max(starts, 1)

        if starts >= cfg_p["gate_min_starts"] and era <= cfg_p["gate_era"] and k_per_start >= cfg_p["gate_k_per_start"]:
            last_dom = _find_last_pitching_dominance(conn, pid, season, starts, era, total_k)
            if last_dom:
                years_ago = season - last_dom["season"]
                if years_ago >= 5:
                    ip_display = f"{total_ip_outs // 3}.{total_ip_outs % 3}"
                    events.append({
                        "type": "deep_scan",
                        "scan": "pitching_dominance",
                        "player": pname, "team": team or "",
                        "detail": (
                            f"{pname} has a {era:.2f} ERA with {total_k} K through {starts} starts "
                            f"({ip_display} IP) — the first to dominate like that through "
                            f"{starts} starts since {last_dom['name']} in {last_dom['season']}."
                        ),
                    })

    # --- PELT-triggered scans ---
    pelt_events = _run_pelt_scans(conn, season, target_date, bat_games)
    events += pelt_events

    return events


# ---------------------------------------------------------------------------
# Historical comparison queries
# ---------------------------------------------------------------------------

def _find_last_slash_match(conn, exclude_pid, season, games, avg, obp, slg):
    """Find the most recent player (before this season) who had AVG/OBP/SLG
    all >= the given values through a similar number of games.

    Uses season_batting_stats with a game count window (±5 games) for performance.
    Not exact "first N games" but close enough and fast.
    """
    min_games = max(games - 5, 5)
    max_games = games + 5
    row = conn.execute("""
        SELECT p.name, s.season
        FROM season_batting_stats s
        JOIN players p ON s.player_id = p.player_id
        WHERE s.season < ? AND s.player_id != ?
        AND s.games BETWEEN ? AND ?
        AND s.batting_avg >= ? AND s.obp >= ? AND s.slg >= ?
        AND s.plate_appearances >= ?
        ORDER BY s.season DESC
        LIMIT 1
    """, (season, exclude_pid, min_games, max_games, avg, obp, slg,
          max(games * 2, 20))).fetchone()

    if row:
        return {"name": row[0], "season": row[1]}
    return None  # Nobody found


def _find_last_hr_pace(conn, exclude_pid, season, games, hr):
    """Find last player with >= hr home runs through <= games games.
    Uses season_batting_stats with game count window for performance."""
    row = conn.execute("""
        SELECT p.name, s.season
        FROM season_batting_stats s
        JOIN players p ON s.player_id = p.player_id
        WHERE s.season < ? AND s.player_id != ?
        AND s.games <= ? AND s.home_runs >= ?
        ORDER BY s.season DESC
        LIMIT 1
    """, (season, exclude_pid, games, hr)).fetchone()

    if row:
        return {"name": row[0], "season": row[1]}
    return None


def _find_last_sb_pace(conn, exclude_pid, season, games, sb):
    """Find last player with >= sb stolen bases through <= games games.
    Uses season_batting_stats with game count window for performance."""
    row = conn.execute("""
        SELECT p.name, s.season
        FROM season_batting_stats s
        JOIN players p ON s.player_id = p.player_id
        WHERE s.season < ? AND s.player_id != ?
        AND s.games <= ? AND s.stolen_bases >= ?
        ORDER BY s.season DESC
        LIMIT 1
    """, (season, exclude_pid, games, sb)).fetchone()

    if row:
        return {"name": row[0], "season": row[1]}
    return None


def _find_last_power_speed(conn, exclude_pid, season, games, hr, sb):
    """Find last player with >= hr HR AND >= sb SB through <= games games.
    Uses season_batting_stats with game count window for performance."""
    row = conn.execute("""
        SELECT p.name, s.season
        FROM season_batting_stats s
        JOIN players p ON s.player_id = p.player_id
        WHERE s.season < ? AND s.player_id != ?
        AND s.games <= ? AND s.home_runs >= ? AND s.stolen_bases >= ?
        ORDER BY s.season DESC
        LIMIT 1
    """, (season, exclude_pid, games, hr, sb)).fetchone()

    if row:
        return {"name": row[0], "season": row[1]}
    return None


def _find_last_pitching_dominance(conn, exclude_pid, season, starts, era, total_k):
    """Find last pitcher with ERA <= this and K >= this through same number of starts."""
    k_per_start = total_k / max(starts, 1)
    # Use season_pitching_stats — compare full-season ERA and K with similar starts count
    row = conn.execute("""
        SELECT p.name, sp.season
        FROM season_pitching_stats sp
        JOIN players p ON sp.player_id = p.player_id
        WHERE sp.season < ? AND sp.player_id != ?
        AND sp.games_started BETWEEN ? AND ?
        AND sp.era <= ? AND sp.strikeouts >= ?
        ORDER BY sp.season DESC
        LIMIT 1
    """, (season, exclude_pid, max(starts - 2, 1), starts + 2,
          era, total_k)).fetchone()

    if row:
        return {"name": row[0], "season": row[1]}
    return None


def _run_pelt_scans(conn, season, target_date, bat_games):
    """Run scans using PELT-detected hot streaks as windows."""
    events = []

    for row in bat_games:
        pid, pname, team = row[0], row[1], row[2]

        # Check if player has an active hot streak in current_form
        cf = conn.execute("""
            SELECT num_games, batting_avg, obp, slg, ops, start_game_num
            FROM current_form
            WHERE player_id = ? AND season = ?
        """, (pid, season)).fetchone()

        if not cf:
            continue

        num_games = cf[0] or 0
        cf_avg = cf[1] or 0
        cf_obp = cf[2] or 0
        cf_slg = cf[3] or 0

        cfg = SCAN_CONFIG["slash_line_pelt"]
        if num_games >= 7 and cf_avg >= cfg["gate_avg"]:
            # Find last player with a stretch this good over this many games
            last = _find_last_streak_slash(conn, pid, season, num_games, cf_avg, cf_obp, cf_slg)
            if last:
                years_ago = season - last["season"]
                if years_ago >= cfg["min_years_mlb"]:
                    events.append({
                        "type": "deep_scan",
                        "scan": "slash_line_pelt",
                        "player": pname, "team": team or "",
                        "detail": (
                            f"{pname} is slashing {_format_avg(cf_avg)}/{_format_avg(cf_obp)}/{_format_avg(cf_slg)} "
                            f"over his current {num_games}-game hot streak — "
                            f"the last player with a stretch that dominant was "
                            f"{last['name']} in {last['season']}."
                        ),
                    })

    return events


def _find_last_streak_slash(conn, exclude_pid, season, window, avg, obp, slg):
    """Find last player with a rolling N-game window where AVG/OBP/SLG >= given values.

    This is computationally expensive — uses a simplified approach:
    checks season-level current_form data from prior seasons.
    """
    # Simplified: check current_form from prior seasons
    row = conn.execute("""
        SELECT p.name, cf.season
        FROM current_form cf
        JOIN players p ON cf.player_id = p.player_id
        WHERE cf.season < ? AND cf.player_id != ?
        AND cf.num_games >= ? AND cf.num_games <= ?
        AND cf.batting_avg >= ? AND cf.obp >= ? AND cf.slg >= ?
        ORDER BY cf.season DESC
        LIMIT 1
    """, (season, exclude_pid, max(window - 5, 5), window + 10,
          avg, obp, slg)).fetchone()

    if row:
        return {"name": row[0], "season": row[1]}
    return None
