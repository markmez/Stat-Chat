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

from services.franchise import get_franchise_codes, get_franchise_name

logger = logging.getLogger("statchat.deep_scans")

DB_PATH = "/data/baseball_stats_full.db"

# ---------------------------------------------------------------------------
# Configuration — printed in sandbox for visibility
# ---------------------------------------------------------------------------

SCAN_CONFIG = {
    "slash_line_season": {
        "description": "OPS through first N games of season",
        "gate": "Dynamic: OPS ≥ 1.200 (≤15g), ≥ 1.100 (≤30g), ≥ 1.000 (≤50g), ≥ .950 (50+g). Games ≥ 10.",
        "interesting_if": "Nobody with OPS this high through same game count in 5+ years",
        "gate_ops": "dynamic",
        "gate_min_games": 10,
        "min_years_mlb": 5,
        "min_years_team": 11,
    },
    "slash_line_pelt": {
        "description": "OPS during PELT-detected hot streak",
        "gate": "Active hot streak in current_form, OPS ≥ 1.000 during streak",
        "interesting_if": "Nobody with a streak OPS this high over same game count in 5+ years",
        "gate_ops": 1.000,
        "min_years_mlb": 5,
        "min_years_team": 11,
    },
    "hr_accumulation": {
        "description": "HR accumulation pace through N games",
        "gate": "HR ≥ 8 through ≤ 30 games, OR HR ≥ 15 through ≤ 50 games",
        "interesting_if": "Nobody with this many HR through this few games in 5+ years",
        "min_years_mlb": 5,
        "min_years_team": 11,
    },
    "sb_accumulation": {
        "description": "SB accumulation pace through N games",
        "gate": "SB ≥ 10 through ≤ 30 games, OR SB ≥ 20 through ≤ 50 games",
        "interesting_if": "Nobody with this many SB through this few games in 5+ years",
        "min_years_mlb": 5,
        "min_years_team": 11,
    },
    "power_speed": {
        "description": "HR + SB combo through N games",
        "gate": "HR ≥ 5 AND SB ≥ 5 through ≤ 25 games",
        "interesting_if": "Nobody with both stats this high through same game count in 5+ years",
        "min_years_mlb": 5,
        "min_years_team": 11,
    },
    "pitching_dominance": {
        "description": "ERA + K rate through first N starts",
        "gate": "Starts ≥ 3, ERA ≤ 1.50, K ≥ 8 per start avg",
        "interesting_if": "Nobody with ERA this low and K rate this high through same starts in 5+ years",
        "gate_era": 1.50,
        "gate_k_per_start": 8,
        "gate_min_starts": 3,
        "min_years_mlb": 5,
        "min_years_team": 11,
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


def _continue_with_context(base: str, continuation: str) -> str:
    """Append a follow-up clause as a separate sentence rather than an
    em-dash continuation. Mirrors the helper in notable_events.py."""
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


def _format_avg(val):
    if val is None: return "--"
    return f".{int(val * 1000):03d}"


def run_deep_scans(conn, season, target_date, cooldowns=None):
    """Run all deep scans for a given date. Returns list of event dicts.

    cooldowns: optional dict of {(player_id, scan_type): (last_fired_date, last_gap_years)}
    Pass the same dict across multiple calls to enforce cooldown across dates.
    Values may also be bare date strings for back-compat; treated as gap=0.
    """
    if cooldowns is None:
        cooldowns = {}
    events = []

    COOLDOWN_DAYS = 5

    def _check_cooldown(pid, scan_type):
        """Time-based cooldown: blocked if fired within last 5 days."""
        key = (pid, scan_type)
        if key not in cooldowns:
            return False
        val = cooldowns[key]
        last_date = val[0] if isinstance(val, tuple) else val
        try:
            from datetime import datetime as _dt
            last = _dt.strptime(last_date, "%Y-%m-%d")
            current = _dt.strptime(target_date, "%Y-%m-%d")
            return (current - last).days < COOLDOWN_DAYS
        except Exception:
            return False

    def _previous_gap(pid, scan_type):
        """Return the 'since' gap in years that previously fired for this
        (player, scan) pair, or 0 if never. Used to gate re-fires to
        require the story to deepen (e.g., from 10yrs-since to 15yrs-since).
        """
        val = cooldowns.get((pid, scan_type))
        if val is None:
            return 0
        if isinstance(val, tuple) and len(val) >= 2:
            return val[1] or 0
        return 0

    def _set_cooldown(pid, scan_type, gap=0):
        """Record the fire. gap = years-since-last shown in this event."""
        cooldowns[(pid, scan_type)] = (target_date, gap)

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

        # Get cumulative stats through this date from game logs
        season_row = conn.execute("""
            SELECT COUNT(*) as games,
                   SUM(at_bats) as ab, SUM(hits) as h, SUM(home_runs) as hr,
                   SUM(walks) as bb, SUM(stolen_bases) as sb,
                   SUM(doubles) as d2b, SUM(triples) as d3b,
                   SUM(COALESCE(hit_by_pitch, 0)) as hbp,
                   SUM(COALESCE(sacrifice_flies, 0)) as sf
            FROM game_batting_logs
            WHERE player_id = ? AND season = ? AND date <= ?
        """, (pid, season, target_date)).fetchone()
        if not season_row or not season_row[1]:
            continue

        games = season_row[0] or 0
        ab = season_row[1] or 0
        hits = season_row[2] or 0
        hr = season_row[3] or 0
        bb = season_row[4] or 0
        sb = season_row[5] or 0
        d2b = season_row[6] or 0
        d3b = season_row[7] or 0
        hbp = season_row[8] or 0
        sf = season_row[9] or 0

        # Compute rate stats
        avg = hits / ab if ab > 0 else 0
        obp_val = (hits + bb + hbp) / (ab + bb + hbp + sf) if (ab + bb + hbp + sf) > 0 else 0
        slg_num = (hits - d2b - d3b - hr) + 2*d2b + 3*d3b + 4*hr
        slg = slg_num / ab if ab > 0 else 0

        # === OPS (season start) ===
        ops = obp_val + slg
        cfg = SCAN_CONFIG["slash_line_season"]
        # Dynamic gate: higher bar early (small samples inflate OPS)
        if games <= 15:
            ops_gate = 1.200
        elif games <= 30:
            ops_gate = 1.100
        elif games <= 50:
            ops_gate = 1.000
        else:
            ops_gate = 0.950
        if games >= cfg["gate_min_games"] and ops >= ops_gate and not _check_cooldown(pid, "ops_season"):
            last_mlb = _find_last_ops_match(conn, pid, season, games, ops)
            ops_display = f"{ops:.3f}"
            last_team = None
            if team:
                team_codes = get_franchise_codes(team)
                last_team = _find_last_ops_match(conn, pid, season, games, ops, team_codes=team_codes)
            mlb_gap = (season - last_mlb["season"]) if last_mlb else None
            team_gap = (season - last_team["season"]) if last_team else None
            mlb_ok = last_mlb and mlb_gap >= cfg["min_years_mlb"]
            team_ok = last_team and team_gap >= cfg["min_years_team"]
            team_scan_fired = False
            prev_gap = _previous_gap(pid, "ops_season")
            # Fire team framing when it's the deeper comparison OR MLB doesn't qualify;
            # but only if the story deepens vs what previously fired for this player.
            if team_ok and (not mlb_ok or team_gap > mlb_gap) and team_gap > prev_gap:
                franchise_name = get_franchise_name(team)
                events.append({
                    "type": "deep_scan",
                    "scan": "slash_line_season",
                    "player": pname, "team": team or "",
                    "secondary_names": [last_team["name"]],
                    "detail": _continue_with_context(
                        f"{pname} is posting a {ops_display} OPS "
                        f"({_format_avg(avg)}/{_format_avg(obp_val)}/{_format_avg(slg)}) "
                        f"through {games} games",
                        f"the last {franchise_name} player to post an OPS "
                        f"that high through {games} games was "
                        f"{last_team['name']} in {last_team['season']}",
                    ),
                })
                _set_cooldown(pid, "ops_season", team_gap)
                team_scan_fired = True
            last_match = last_mlb  # preserve downstream variable name
            if last_match and not team_scan_fired:
                years_ago = season - last_match["season"]
                if years_ago >= cfg["min_years_mlb"] and years_ago > prev_gap:
                    events.append({
                        "type": "deep_scan",
                        "scan": "slash_line_season",
                        "player": pname, "team": team or "",
                        "secondary_names": [last_match["name"]],
                        "detail": _continue_with_context(
                            f"{pname} is posting a {ops_display} OPS "
                            f"({_format_avg(avg)}/{_format_avg(obp_val)}/{_format_avg(slg)}) "
                            f"through {games} games",
                            f"the last player to post an OPS that high through {games} games was "
                            f"{last_match['name']} in {last_match['season']} ({years_ago} years ago)",
                        ),
                    })
                    _set_cooldown(pid, "ops_season", years_ago)
            elif last_match is None and prev_gap < 999:
                # "No precedent" event — infinite gap, fires only once per player
                events.append({
                    "type": "deep_scan",
                    "scan": "slash_line_season",
                    "player": pname, "team": team or "",
                    "detail": _continue_with_context(
                        f"{pname} is posting a {ops_display} OPS "
                        f"({_format_avg(avg)}/{_format_avg(obp_val)}/{_format_avg(slg)}) "
                        f"through {games} games",
                        f"no player in our records has posted an OPS that high through {games} games",
                    ),
                })
                _set_cooldown(pid, "ops_season", 999)

        # === HR ACCUMULATION ===
        cfg_hr = SCAN_CONFIG["hr_accumulation"]
        if ((hr >= 8 and games <= 30) or (hr >= 15 and games <= 50)) and not _check_cooldown(pid, "hr_acc"):
            last_hr_mlb = _find_last_hr_pace(conn, pid, season, games, hr)
            last_hr_team = None
            if team:
                team_codes = get_franchise_codes(team)
                last_hr_team = _find_last_hr_pace(conn, pid, season, games, hr, team_codes=team_codes)
            mlb_gap = (season - last_hr_mlb["season"]) if last_hr_mlb else None
            team_gap = (season - last_hr_team["season"]) if last_hr_team else None
            mlb_ok = last_hr_mlb and mlb_gap >= cfg_hr["min_years_mlb"]
            team_ok = last_hr_team and team_gap >= cfg_hr["min_years_team"]
            hr_team_fired = False
            prev_gap_hr = _previous_gap(pid, "hr_acc")
            if team_ok and (not mlb_ok or team_gap > mlb_gap) and team_gap > prev_gap_hr:
                franchise_name = get_franchise_name(team)
                events.append({
                    "type": "deep_scan",
                    "scan": "hr_accumulation",
                    "player": pname, "team": team or "",
                    "secondary_names": [last_hr_team["name"]],
                    "detail": _continue_with_context(
                        f"{pname} has {hr} HR through {games} games",
                        f"the last {franchise_name} player to hit that many through {games} games was "
                        f"{last_hr_team['name']} ({last_hr_team['value']} HR) in {last_hr_team['season']}",
                    ),
                })
                _set_cooldown(pid, "hr_acc", team_gap)
                hr_team_fired = True
            last_hr = last_hr_mlb
            if last_hr and not hr_team_fired:
                years_ago = season - last_hr["season"]
                if years_ago >= cfg_hr["min_years_mlb"] and years_ago > prev_gap_hr:
                    events.append({
                        "type": "deep_scan",
                        "scan": "hr_accumulation",
                        "player": pname, "team": team or "",
                        "secondary_names": [last_hr["name"]],
                        "detail": _continue_with_context(
                            f"{pname} has {hr} HR through {games} games",
                            f"the last player to hit that many through {games} games was "
                            f"{last_hr['name']} ({last_hr['value']} HR) in {last_hr['season']}",
                        ),
                    })
                    _set_cooldown(pid, "hr_acc", years_ago)
            elif last_hr is None and not hr_team_fired and prev_gap_hr < 999:
                events.append({
                    "type": "deep_scan",
                    "scan": "hr_accumulation",
                    "player": pname, "team": team or "",
                    "detail": _continue_with_context(
                        f"{pname} has {hr} HR through {games} games",
                        "no player in our records has reached that mark this quickly",
                    ),
                })
                _set_cooldown(pid, "hr_acc", 999)

        # === SB ACCUMULATION ===
        cfg_sb = SCAN_CONFIG["sb_accumulation"]
        if ((sb >= 10 and games <= 30) or (sb >= 20 and games <= 50)) and not _check_cooldown(pid, "sb_acc"):
            last_sb_mlb = _find_last_sb_pace(conn, pid, season, games, sb)
            last_sb_team = None
            if team:
                team_codes = get_franchise_codes(team)
                last_sb_team = _find_last_sb_pace(conn, pid, season, games, sb, team_codes=team_codes)
            mlb_gap = (season - last_sb_mlb["season"]) if last_sb_mlb else None
            team_gap = (season - last_sb_team["season"]) if last_sb_team else None
            mlb_ok = last_sb_mlb and mlb_gap >= cfg_sb["min_years_mlb"]
            team_ok = last_sb_team and team_gap >= cfg_sb["min_years_team"]
            sb_team_fired = False
            prev_gap_sb = _previous_gap(pid, "sb_acc")
            if team_ok and (not mlb_ok or team_gap > mlb_gap) and team_gap > prev_gap_sb:
                franchise_name = get_franchise_name(team)
                events.append({
                    "type": "deep_scan",
                    "scan": "sb_accumulation",
                    "player": pname, "team": team or "",
                    "secondary_names": [last_sb_team["name"]],
                    "detail": _continue_with_context(
                        f"{pname} has {sb} SB through {games} games",
                        f"the last {franchise_name} player to steal that many through {games} games was "
                        f"{last_sb_team['name']} ({last_sb_team['value']} SB) in {last_sb_team['season']}",
                    ),
                })
                _set_cooldown(pid, "sb_acc", team_gap)
                sb_team_fired = True
            last_sb = last_sb_mlb
            if last_sb and not sb_team_fired:
                years_ago = season - last_sb["season"]
                if years_ago >= cfg_sb["min_years_mlb"] and years_ago > prev_gap_sb:
                    events.append({
                        "type": "deep_scan",
                        "scan": "sb_accumulation",
                        "player": pname, "team": team or "",
                        "secondary_names": [last_sb["name"]],
                        "detail": _continue_with_context(
                            f"{pname} has {sb} SB through {games} games",
                            f"the last player to steal that many through {games} games was "
                            f"{last_sb['name']} ({last_sb['value']} SB) in {last_sb['season']}",
                        ),
                    })
                    _set_cooldown(pid, "sb_acc", years_ago)

        # === POWER-SPEED COMBO ===
        cfg_ps = SCAN_CONFIG["power_speed"]
        if hr >= 5 and sb >= 5 and games <= 25 and not _check_cooldown(pid, "power_speed"):
            last_ps_mlb = _find_last_power_speed(conn, pid, season, games, hr, sb)
            last_ps_team = None
            if team:
                team_codes = get_franchise_codes(team)
                last_ps_team = _find_last_power_speed(conn, pid, season, games, hr, sb, team_codes=team_codes)
            mlb_gap = (season - last_ps_mlb["season"]) if last_ps_mlb else None
            team_gap = (season - last_ps_team["season"]) if last_ps_team else None
            mlb_ok = last_ps_mlb and mlb_gap >= cfg_ps["min_years_mlb"]
            team_ok = last_ps_team and team_gap >= cfg_ps["min_years_team"]
            ps_team_fired = False
            prev_gap_ps = _previous_gap(pid, "power_speed")
            if team_ok and (not mlb_ok or team_gap > mlb_gap) and team_gap > prev_gap_ps:
                franchise_name = get_franchise_name(team)
                events.append({
                    "type": "deep_scan",
                    "scan": "power_speed",
                    "player": pname, "team": team or "",
                    "secondary_names": [last_ps_team["name"]],
                    "detail": _continue_with_context(
                        f"{pname} has {hr} HR and {sb} SB through {games} games",
                        f"the last {franchise_name} player with that power-speed combo through {games} games was "
                        f"{last_ps_team['name']} ({last_ps_team['hr']} HR, {last_ps_team['sb']} SB) in {last_ps_team['season']}",
                    ),
                })
                _set_cooldown(pid, "power_speed", team_gap)
                ps_team_fired = True
            last_ps = last_ps_mlb
            if last_ps and not ps_team_fired:
                years_ago = season - last_ps["season"]
                if years_ago >= cfg_ps["min_years_mlb"] and years_ago > prev_gap_ps:
                    events.append({
                        "type": "deep_scan",
                        "scan": "power_speed",
                        "player": pname, "team": team or "",
                        "secondary_names": [last_ps["name"]],
                        "detail": _continue_with_context(
                            f"{pname} has {hr} HR and {sb} SB through {games} games",
                            f"the last player with that power-speed combo through {games} games was "
                            f"{last_ps['name']} ({last_ps['hr']} HR, {last_ps['sb']} SB) in {last_ps['season']}",
                        ),
                    })
                    _set_cooldown(pid, "power_speed", years_ago)

    # --- Pitching scans ---
    for row in pitch_games:
        pid, pname, team = row[0], row[1], row[2]
        k_today, bb_today = row[3] or 0, row[4] or 0
        ip_outs_today = row[5] or 0
        is_start = row[10]

        if not is_start:
            continue  # Only starters for now

        # Get cumulative pitching stats through this date
        pitch_season = conn.execute("""
            SELECT SUM(CASE WHEN is_start = 1 THEN 1 ELSE 0 END) as starts,
                   SUM(strikeouts) as k, SUM(walks) as bb,
                   SUM(earned_runs) as er, SUM(ip_outs) as ip_outs
            FROM game_pitching_logs
            WHERE player_id = ? AND season = ? AND date <= ?
        """, (pid, season, target_date)).fetchone()
        if not pitch_season or not pitch_season[0]:
            continue

        starts = pitch_season[0] or 0
        total_k = pitch_season[1] or 0
        total_bb = pitch_season[2] or 0
        total_er = pitch_season[3] or 0
        total_ip_outs = pitch_season[4] or 0
        era = (total_er * 27) / total_ip_outs if total_ip_outs > 0 else 99

        cfg_p = SCAN_CONFIG["pitching_dominance"]
        k_per_start = total_k / max(starts, 1)

        if starts >= cfg_p["gate_min_starts"] and era <= cfg_p["gate_era"] and k_per_start >= cfg_p["gate_k_per_start"] and not _check_cooldown(pid, "pitch_dom"):
            last_dom_mlb = _find_last_pitching_dominance(conn, pid, season, starts, era, total_k)
            last_dom_team = None
            if team:
                team_codes = get_franchise_codes(team)
                last_dom_team = _find_last_pitching_dominance(conn, pid, season, starts, era, total_k, team_codes=team_codes)
            ip_display = f"{total_ip_outs // 3}.{total_ip_outs % 3}"
            mlb_gap = (season - last_dom_mlb["season"]) if last_dom_mlb else None
            team_gap = (season - last_dom_team["season"]) if last_dom_team else None
            mlb_ok = last_dom_mlb and mlb_gap >= cfg_p["min_years_mlb"]
            team_ok = last_dom_team and team_gap >= cfg_p["min_years_team"]
            pd_team_fired = False
            prev_gap_pd = _previous_gap(pid, "pitch_dom")
            if team_ok and (not mlb_ok or team_gap > mlb_gap) and team_gap > prev_gap_pd:
                franchise_name = get_franchise_name(team)
                events.append({
                    "type": "deep_scan",
                    "scan": "pitching_dominance",
                    "player": pname, "team": team or "",
                    "secondary_names": [last_dom_team["name"]],
                    "detail": _continue_with_context(
                        f"{pname} has a {era:.2f} ERA with {total_k} K through {starts} starts "
                        f"({ip_display} IP)",
                        f"the last {franchise_name} pitcher to do that through "
                        f"{starts} starts was {last_dom_team['name']} "
                        f"({last_dom_team['era']:.2f} ERA, {last_dom_team['k']} K) in {last_dom_team['season']}",
                    ),
                })
                _set_cooldown(pid, "pitch_dom", team_gap)
                pd_team_fired = True
            last_dom = last_dom_mlb
            if last_dom and not pd_team_fired:
                years_ago = season - last_dom["season"]
                if years_ago >= cfg_p["min_years_mlb"] and years_ago > prev_gap_pd:
                    events.append({
                        "type": "deep_scan",
                        "scan": "pitching_dominance",
                        "player": pname, "team": team or "",
                        "secondary_names": [last_dom["name"]],
                        "detail": _continue_with_context(
                            f"{pname} has a {era:.2f} ERA with {total_k} K through {starts} starts "
                            f"({ip_display} IP)",
                            f"the last pitcher to do that through {starts} starts was "
                            f"{last_dom['name']} ({last_dom['era']:.2f} ERA, {last_dom['k']} K) in {last_dom['season']}",
                        ),
                    })
                    _set_cooldown(pid, "pitch_dom", years_ago)

    # --- PELT-triggered scans ---
    pelt_events = _run_pelt_scans(conn, season, target_date, bat_games, cooldowns)
    events += pelt_events

    return events


# ---------------------------------------------------------------------------
# Historical comparison queries
# ---------------------------------------------------------------------------

def _team_filter_clause(team_codes, table_alias="numbered", stats_table="season_batting_stats"):
    """Build an EXISTS filter for team scope. Returns (sql_fragment, params)."""
    if not team_codes:
        return "", []
    like_clauses = " OR ".join(["('/' || ss.team || '/') LIKE ?"] * len(team_codes))
    sql = f"""
        AND EXISTS (
            SELECT 1 FROM {stats_table} ss
            WHERE ss.player_id = {table_alias}.player_id
              AND ss.season = {table_alias}.season
              AND ({like_clauses})
        )
    """
    params = [f"%/{c}/%" for c in team_codes]
    return sql, params


def _find_last_ops_match(conn, exclude_pid, season, games, ops, team_codes=None):
    """Find the most recent player with OPS >= the given value through
    the same number of games (first N games of a season).

    Scans one year at a time working backwards to avoid massive window
    function queries. Stops at first match. team_codes: optional list of
    franchise codes to restrict search to players on that franchise.
    """
    # Longer lookback when team-scoped (franchises span many decades)
    lookback = 101 if team_codes else 26
    tf_sql, tf_params = _team_filter_clause(team_codes)
    for check_season in range(season - 1, season - lookback, -1):
        row = conn.execute(f"""
            SELECT p.name, sub.season FROM (
                SELECT player_id, season,
                       CAST(SUM(hits) + SUM(walks) + SUM(COALESCE(hit_by_pitch, 0)) AS REAL) /
                           NULLIF(SUM(at_bats) + SUM(walks) + SUM(COALESCE(hit_by_pitch, 0)) + SUM(COALESCE(sacrifice_flies, 0)), 0)
                       +
                       CAST(SUM(hits) - SUM(doubles) - SUM(triples) - SUM(home_runs)
                            + 2*SUM(doubles) + 3*SUM(triples) + 4*SUM(home_runs) AS REAL) /
                           NULLIF(SUM(at_bats), 0)
                       AS ops_calc
                FROM (
                    SELECT g.player_id, g.season, g.hits, g.at_bats, g.walks,
                           g.hit_by_pitch, g.sacrifice_flies, g.doubles, g.triples, g.home_runs,
                           ROW_NUMBER() OVER (PARTITION BY g.player_id ORDER BY g.date) as gnum
                    FROM game_batting_logs g
                    WHERE g.at_bats > 0 AND g.season = ?
                ) numbered
                WHERE gnum <= ?
                  {tf_sql}
                GROUP BY player_id, season
                HAVING SUM(at_bats) >= ?
            ) sub
            JOIN players p ON sub.player_id = p.player_id
            WHERE sub.player_id != ?
            AND sub.ops_calc >= ?
            LIMIT 1
        """, (check_season, games, *tf_params, max(games * 2, 20), exclude_pid, ops)).fetchone()

        if row:
            return {"name": row[0], "season": row[1]}

    return None


def _find_last_hr_pace(conn, exclude_pid, season, games, hr, team_codes=None):
    """Find last player with >= hr home runs through first N games.
    Scans one year at a time working backwards. team_codes: optional
    franchise codes to restrict to that franchise's history."""
    lookback = 101 if team_codes else 26
    tf_sql, tf_params = _team_filter_clause(team_codes)
    for check_season in range(season - 1, season - lookback, -1):
        row = conn.execute(f"""
            SELECT p.name, sub.season, sub.total_hr FROM (
                SELECT player_id, season, SUM(home_runs) as total_hr
                FROM (
                    SELECT player_id, season, home_runs,
                           ROW_NUMBER() OVER (PARTITION BY player_id ORDER BY date) as gnum
                    FROM game_batting_logs
                    WHERE at_bats > 0 AND season = ?
                ) numbered
                WHERE gnum <= ?
                  {tf_sql}
                GROUP BY player_id, season
                HAVING total_hr >= ?
            ) sub
            JOIN players p ON sub.player_id = p.player_id
            WHERE sub.player_id != ?
            LIMIT 1
        """, (check_season, games, *tf_params, hr, exclude_pid)).fetchone()

        if row:
            return {"name": row[0], "season": row[1], "value": int(row[2])}

    return None


def _find_last_sb_pace(conn, exclude_pid, season, games, sb, team_codes=None):
    """Find last player with >= sb stolen bases through first N games.
    Scans one year at a time working backwards."""
    lookback = 101 if team_codes else 26
    tf_sql, tf_params = _team_filter_clause(team_codes)
    for check_season in range(season - 1, season - lookback, -1):
        row = conn.execute(f"""
            SELECT p.name, sub.season, sub.total_sb FROM (
                SELECT player_id, season, SUM(stolen_bases) as total_sb
                FROM (
                    SELECT player_id, season, stolen_bases,
                           ROW_NUMBER() OVER (PARTITION BY player_id ORDER BY date) as gnum
                    FROM game_batting_logs
                    WHERE season = ?
                ) numbered
                WHERE gnum <= ?
                  {tf_sql}
                GROUP BY player_id, season
                HAVING total_sb >= ?
            ) sub
            JOIN players p ON sub.player_id = p.player_id
            WHERE sub.player_id != ?
            LIMIT 1
        """, (check_season, games, *tf_params, sb, exclude_pid)).fetchone()

        if row:
            return {"name": row[0], "season": row[1], "value": int(row[2])}

    return None


def _find_last_power_speed(conn, exclude_pid, season, games, hr, sb, team_codes=None):
    """Find last player with >= hr HR AND >= sb SB through first N games.
    Scans one year at a time working backwards."""
    lookback = 101 if team_codes else 26
    tf_sql, tf_params = _team_filter_clause(team_codes)
    for check_season in range(season - 1, season - lookback, -1):
        row = conn.execute(f"""
            SELECT p.name, sub.season, sub.total_hr, sub.total_sb FROM (
                SELECT player_id, season,
                       SUM(home_runs) as total_hr, SUM(stolen_bases) as total_sb
                FROM (
                    SELECT player_id, season, home_runs, stolen_bases,
                           ROW_NUMBER() OVER (PARTITION BY player_id ORDER BY date) as gnum
                    FROM game_batting_logs
                    WHERE at_bats > 0 AND season = ?
                ) numbered
                WHERE gnum <= ?
                  {tf_sql}
                GROUP BY player_id, season
                HAVING total_hr >= ? AND total_sb >= ?
            ) sub
            JOIN players p ON sub.player_id = p.player_id
            WHERE sub.player_id != ?
            LIMIT 1
        """, (check_season, games, *tf_params, hr, sb, exclude_pid)).fetchone()

        if row:
            return {"name": row[0], "season": row[1], "hr": int(row[2]), "sb": int(row[3])}

    return None


def _find_last_pitching_dominance(conn, exclude_pid, season, starts, era, total_k, team_codes=None):
    """Find last pitcher with ERA <= this and K >= this through same number of starts.
    Scans one year at a time working backwards."""
    lookback = 101 if team_codes else 26
    tf_sql, tf_params = _team_filter_clause(team_codes, stats_table="season_pitching_stats")
    for check_season in range(season - 1, season - lookback, -1):
        row = conn.execute(f"""
            SELECT p.name, sub.season, sub.era_calc, sub.total_k FROM (
                SELECT player_id, season,
                       CAST(SUM(earned_runs) AS REAL) * 27 / NULLIF(SUM(ip_outs), 0) as era_calc,
                       SUM(strikeouts) as total_k
                FROM (
                    SELECT player_id, season, earned_runs, ip_outs, strikeouts,
                           ROW_NUMBER() OVER (PARTITION BY player_id ORDER BY date) as snum
                    FROM game_pitching_logs
                    WHERE is_start = 1 AND season = ?
                ) numbered
                WHERE snum <= ?
                  {tf_sql}
                GROUP BY player_id, season
                HAVING SUM(ip_outs) > 0
            ) sub
            JOIN players p ON sub.player_id = p.player_id
            WHERE sub.player_id != ?
            AND sub.era_calc <= ? AND sub.total_k >= ?
            LIMIT 1
        """, (check_season, starts, *tf_params, exclude_pid, era, total_k)).fetchone()

        if row:
            return {"name": row[0], "season": row[1], "era": round(row[2], 2), "k": int(row[3])}

    return None


def _run_pelt_scans(conn, season, target_date, bat_games, cooldowns=None):
    """PELT hot-streak feed events are now produced by
    `notable_events.detect_hot_streaks_pelt`, which runs RAW PELT (no
    current_form extension/fallback), enforces a 6-29 game window, applies
    dynamic torrid/locked-in quality gates, and emits MLB + team-since
    comps with a shared progressive cooldown.

    This stub is retained so `run_deep_scans` still calls it safely; if you
    need to add PELT-based deep-scan features that don't belong in the feed
    detector, add them here.
    """
    return []


def _find_last_streak_ops(conn, exclude_pid, season, window, ops, team_codes=None):
    """Find last player with a hot streak OPS >= the given value.
    Uses current_form from prior seasons as proxy for rolling windows.
    team_codes: optional franchise filter."""
    tf_sql = ""
    tf_params = []
    if team_codes:
        like_clauses = " OR ".join(["('/' || ss.team || '/') LIKE ?"] * len(team_codes))
        tf_sql = f"""
            AND EXISTS (
                SELECT 1 FROM season_batting_stats ss
                WHERE ss.player_id = cf.player_id AND ss.season = cf.season
                  AND ({like_clauses})
            )
        """
        tf_params = [f"%/{c}/%" for c in team_codes]
    row = conn.execute(f"""
        SELECT p.name, cf.season
        FROM current_form cf
        JOIN players p ON cf.player_id = p.player_id
        WHERE cf.season < ? AND cf.player_id != ?
        AND cf.num_games >= ?
        AND cf.ops >= ?
        {tf_sql}
        ORDER BY cf.season DESC
        LIMIT 1
    """, (season, exclude_pid, max(window - 5, 5), ops, *tf_params)).fetchone()

    if row:
        return {"name": row[0], "season": row[1]}
    return None
