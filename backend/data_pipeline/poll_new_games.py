"""
Lightweight poll: pull today's game logs + season totals, detect new events.

Runs every 15 min during post-game hours. Much faster than the full pipeline —
only pulls today's date, no splits/streaks/play-by-play.

If new games are found since last poll, runs notable event detection + AI insights.

Usage:
    python poll_new_games.py --db /data/baseball_stats_full.db
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta

# Add parent dir for imports
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

DB_PATH = os.getenv("DB_PATH", "/data/baseball_stats_full.db")
MSF_API_KEY = os.getenv("MSF_API_KEY", "")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=DB_PATH)
    args = parser.parse_args()

    if not MSF_API_KEY:
        print("ERROR: MSF_API_KEY not set")
        return

    t0 = time.time()
    conn = sqlite3.connect(args.db)
    conn.execute("PRAGMA journal_mode=WAL")

    # Import pipeline functions
    from pull_live_stats import (
        detect_season, msf_get, find_or_create_player, retro_team,
        safe_int, safe_float, compute_rate_stats,
        pull_season_batting, pull_season_pitching,
        compute_league_averages_and_ops_plus, compute_pitching_league_averages,
        record_last_update,
    )

    # Auto-detect season
    now = datetime.now()
    month = now.month
    year = now.year
    if month >= 3 and month <= 9:
        season_str = f"{year}-regular"
    elif month >= 10:
        season_str = f"{year}-playoff"
    else:
        season_str = f"{year - 1}-regular"

    season_year = detect_season(season_str)
    print(f"Lightweight poll for {season_str}")

    # Determine today's game date(s) to pull
    # Use Eastern time to determine game dates
    # Games ending now are today's date in ET
    eastern_offset = timedelta(hours=-4)  # EDT
    eastern_now = datetime.utcnow() + eastern_offset
    today_str = eastern_now.strftime("%Y%m%d")

    # Also check yesterday if it's before 3 AM ET (late West Coast games)
    dates_to_pull = [today_str]
    if eastern_now.hour < 3:
        yesterday = (eastern_now - timedelta(days=1)).strftime("%Y%m%d")
        dates_to_pull.insert(0, yesterday)

    # Track existing game logs before pull — (player_id, date, game_number)
    existing_batting = set(
        (r[0], r[1], r[2]) for r in conn.execute("""
            SELECT player_id, date, game_number FROM game_batting_logs WHERE season = ?
        """, (season_year,)).fetchall()
    )
    existing_pitching = set(
        (r[0], r[1], r[2]) for r in conn.execute("""
            SELECT player_id, date, game_number FROM game_pitching_logs WHERE season = ?
        """, (season_year,)).fetchall()
    )

    # Pull season totals (fast — single API call each)
    print("  Pulling season totals...")
    pull_season_batting(conn, season_str)
    pull_season_pitching(conn, season_str)

    # Pull game logs for today's date(s) only
    print(f"  Pulling game logs for {dates_to_pull}...")

    # Load existing game_number tracker from current data
    player_date_game_num = {}
    existing = conn.execute("""
        SELECT player_id, date, MAX(game_number) FROM game_batting_logs
        WHERE season = ? GROUP BY player_id, date
    """, (season_year,)).fetchall()
    for pid, dt, gn in existing:
        player_date_game_num[(pid, dt)] = (gn or 0) + 1

    cursor = conn.cursor()
    new_logs = 0
    new_batting_players = set()  # player_ids with genuinely new batting game logs
    new_pitching_players = set()  # player_ids with genuinely new pitching game logs

    for gdate in dates_to_pull:
        game_date_formatted = f"{gdate[:4]}-{gdate[4:6]}-{gdate[6:8]}"
        try:
            data = msf_get(f"{season_str}/date/{gdate}/player_gamelogs.json")
        except Exception as e:
            print(f"    Skipping {gdate}: {e}")
            continue
        if not data:
            continue

        logs = data.get("gamelogs", [])

        for entry in logs:
            player = entry.get("player", {})
            team_info = entry.get("team", {})
            game = entry.get("game", {})
            all_stats = entry.get("stats", {})
            team_abbrev = team_info.get("abbreviation", "")

            # Use request date as game date (not UTC startTime)
            away_team = game.get("awayTeamAbbreviation", "")
            home_team = game.get("homeTeamAbbreviation", "")
            is_home = team_abbrev == home_team
            opponent = away_team if is_home else home_team
            vishome = "H" if is_home else "V"

            # Batting log
            bat = all_stats.get("batting", {})
            if bat and (safe_int(bat.get("atBats")) > 0 or safe_int(bat.get("batterWalks")) > 0 or safe_int(bat.get("hitByPitch")) > 0):
                pid = find_or_create_player(cursor, player, team_abbrev, season_year)

                pkey = (pid, game_date_formatted)
                game_num = player_date_game_num.get(pkey, 0)
                player_date_game_num[pkey] = game_num + 1

                ab = safe_int(bat.get("atBats"))
                h = safe_int(bat.get("hits"))
                doubles = safe_int(bat.get("secondBaseHits"))
                triples = safe_int(bat.get("thirdBaseHits"))
                hr = safe_int(bat.get("homeruns"))
                bb = safe_int(bat.get("batterWalks"))
                so = safe_int(bat.get("batterStrikeouts"))
                hbp = safe_int(bat.get("hitByPitch"))
                sf = safe_int(bat.get("batterSacrificeFlies", 0))
                pa, avg, obp, slg, ops, _, _ = compute_rate_stats(h, ab, bb, hbp, sf, doubles, triples, hr, so)

                is_new_game = (pid, game_date_formatted, game_num) not in existing_batting

                cursor.execute("""
                    INSERT OR REPLACE INTO game_batting_logs
                    (player_id, season, date, game_number, opponent, vishome,
                     plate_appearances, at_bats,
                     hits, doubles, triples, home_runs, runs, rbi, walks, strikeouts,
                     hit_by_pitch, sacrifice_flies,
                     batting_avg, obp, slg, ops)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    pid, season_year, game_date_formatted, game_num, retro_team(opponent), vishome,
                    pa, ab, h, doubles, triples, hr,
                    safe_int(bat.get("runs")),
                    safe_int(bat.get("runsBattedIn")),
                    bb, so, hbp, sf, avg, obp, slg, ops,
                ))
                new_logs += 1
                if is_new_game:
                    new_batting_players.add(pid)

            # Pitching log
            pitch = all_stats.get("pitching", {})
            if pitch and safe_float(pitch.get("inningsPitched"), 0) > 0:
                pid = find_or_create_player(cursor, player, team_abbrev, season_year)

                pkey = (pid, game_date_formatted)
                if pkey not in player_date_game_num:
                    player_date_game_num[pkey] = 0
                game_num = player_date_game_num[pkey]

                is_new_pitch_game = (pid, game_date_formatted, game_num) not in existing_pitching

                ip_raw = safe_float(pitch.get("inningsPitched"), 0)
                ip_whole = int(ip_raw)
                ip_frac = round((ip_raw - ip_whole) * 10)
                ip_outs = ip_whole * 3 + ip_frac
                innings_text = f"{ip_whole}.{ip_frac}"
                h_p = safe_int(pitch.get("hitsAllowed"))
                er = safe_int(pitch.get("earnedRunsAllowed"))
                era = (er * 9.0) / (ip_outs / 3.0) if ip_outs > 0 else None
                misc = all_stats.get("miscellaneous", {})

                cursor.execute("""
                    INSERT OR REPLACE INTO game_pitching_logs
                    (player_id, season, date, game_number, opponent, vishome, is_start,
                     ip_outs, innings_pitched,
                     hits, runs, earned_runs, home_runs, walks, strikeouts, hit_by_pitch,
                     batters_faced, win, loss, save, era)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    pid, season_year, game_date_formatted, game_num, retro_team(opponent), vishome,
                    1 if safe_int(misc.get("gamesStarted")) > 0 else 0,
                    ip_outs, innings_text, h_p,
                    safe_int(pitch.get("runsAllowed")),
                    er,
                    safe_int(pitch.get("homerunsAllowed")),
                    safe_int(pitch.get("pitcherWalks")),
                    safe_int(pitch.get("pitcherStrikeouts")),
                    safe_int(pitch.get("battersHit")),
                    safe_int(pitch.get("totalBattersFaced")),
                    safe_int(pitch.get("wins")),
                    safe_int(pitch.get("losses")),
                    safe_int(pitch.get("saves")),
                    era,
                ))
                if is_new_pitch_game:
                    new_pitching_players.add(pid)

        conn.commit()

    all_new_players = new_batting_players | new_pitching_players
    print(f"  {new_logs} log entries processed, {len(all_new_players)} players with new games")

    # If new games found, run targeted event detection for those players only
    if all_new_players:
        print(f"  Running targeted event detection for {len(all_new_players)} players...")
        try:
            services_parent = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
            if services_parent not in sys.path:
                sys.path.insert(0, services_parent)
            from services.notable_events import detect_for_players
            detect_for_players(args.db, season_year, all_new_players)
        except Exception as e:
            print(f"  Event detection failed: {e}")

        # AI insights (once per game date)
        print("  Running AI insights...")
        try:
            from services.notable_events import _get_latest_date
            from services.ai_notable_events import generate_ai_insights
            latest = _get_latest_date(conn, season_year)
            if latest:
                ai_conn = sqlite3.connect(args.db)
                result = generate_ai_insights(ai_conn, season_year, latest, dry_run=False)
                ai_events = result.get("events", [])
                skipped = result.get("skipped", False)
                if skipped:
                    print(f"  AI insights already exist for {latest}")
                else:
                    print(f"  AI insights: {len(ai_events)} generated")
                ai_conn.close()
        except Exception as e:
            print(f"  AI insights failed: {e}")
    else:
        print("  No new games — skipping event detection")

    conn.close()
    elapsed = time.time() - t0
    print(f"  Poll complete in {elapsed:.1f}s")


if __name__ == "__main__":
    main()
