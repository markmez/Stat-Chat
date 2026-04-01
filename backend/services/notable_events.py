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
            events.append({
                "headline": f"{name} has hit safely in {streak} straight games",
                "detail": f"The longest active hitting streak in MLB." if streak >= 15
                    else f"One of the longest active hitting streaks in MLB.",
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

    # Update detail for #1 to say "longest active"
    if len(events) > 1:
        for e in events[1:]:
            e["detail"] = f"One of the longest active hitting streaks in MLB."

    # Clean up internal field
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
            events.append({
                "headline": f"{name} has reached base in {streak} straight games",
                "detail": "The longest active on-base streak in MLB.",
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
            events.append({
                "headline": f"{name} has homered in {streak} straight games",
                "detail": f"Only a handful of players each year homer in {min_games}+ consecutive games.",
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
                "headline": f"{name} has thrown {scoreless} consecutive scoreless starts",
                "detail": f"Allowing 0 earned runs in 5+ innings each time.",
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
                "headline": f"{name} has {qs} consecutive quality starts",
                "detail": f"At least 6 IP with 3 or fewer earned runs each outing.",
                "category": "Streak",
                "game_date": starts[0][0],
                "player_names": [name],
                "team_names": [],
                "detection_type": "qs_streak",
                "priority": 1,
            })

    return events


def detect_season_pace(conn, season):
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
                        "game_date": str(date.today()),
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
                "game_date": str(date.today()),
                "player_names": [name],
                "team_names": [team] if team else [],
                "detection_type": "pace_pitching_k",
                "priority": 1,
            })

    return events


# ---------------------------------------------------------------------------
# Tier 2: Medium-signal detectors
# ---------------------------------------------------------------------------

def detect_career_milestones(conn, season):
    """Find players approaching career milestone numbers."""
    events = []

    # Batting milestones: (stat_column, milestone_values, label)
    bat_milestones = [
        ("home_runs", [600, 500, 400, 300, 200, 100], "home runs"),
        ("hits", [3000, 2500, 2000, 1500, 1000], "career hits"),
        ("rbi", [1500, 1000, 500], "career RBI"),
        ("stolen_bases", [500, 400, 300], "career stolen bases"),
    ]

    for col, milestones, label in bat_milestones:
        rows = conn.execute(f"""
            SELECT s.player_id, p.name, SUM(s.{col}) as career_total
            FROM season_batting_stats s
            JOIN players p ON s.player_id = p.player_id
            WHERE s.player_id IN (
                SELECT DISTINCT player_id FROM season_batting_stats WHERE season = ?
            )
            GROUP BY s.player_id
            ORDER BY career_total DESC
        """, (season,)).fetchall()

        found = 0
        for pid, name, total in rows:
            if not total or found >= 3:
                break
            for m in milestones:
                remaining = m - total
                if 1 <= remaining <= 10:
                    events.append({
                        "headline": f"{name} is {remaining} away from {m} {label}",
                        "detail": f"Currently at {total} {label}.",
                        "category": "Milestone",
                        "game_date": str(date.today()),
                        "player_names": [name],
                        "team_names": [],
                        "detection_type": f"career_{col}_{m}",
                        "priority": 2,
                    })
                    found += 1
                    break  # Only closest milestone

    # Pitching milestones
    pitch_milestones = [
        ("strikeouts", [3000, 2500, 2000, 1500, 1000], "career strikeouts"),
        ("wins", [200, 150, 100], "career wins"),
        ("saves", [400, 300, 200], "career saves"),
    ]

    for col, milestones, label in pitch_milestones:
        rows = conn.execute(f"""
            SELECT s.player_id, p.name, SUM(s.{col}) as career_total
            FROM season_pitching_stats s
            JOIN players p ON s.player_id = p.player_id
            WHERE s.player_id IN (
                SELECT DISTINCT player_id FROM season_pitching_stats WHERE season = ?
            )
            GROUP BY s.player_id
            ORDER BY career_total DESC
        """, (season,)).fetchall()

        found = 0
        for pid, name, total in rows:
            if not total or found >= 3:
                break
            for m in milestones:
                remaining = m - total
                if 1 <= remaining <= 10:
                    events.append({
                        "headline": f"{name} is {remaining} away from {m} {label}",
                        "detail": f"Currently at {total} {label}.",
                        "category": "Milestone",
                        "game_date": str(date.today()),
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
            headline = f"{name} hit {hr} home runs in a single game"
            detail = f"Went {h}-for-{ab} with {rbi} RBI."
        elif h and h >= 5:
            headline = f"{name} went {h}-for-{ab}"
            detail = f"A {h}-hit game with {hr or 0} HR and {rbi or 0} RBI."
        elif rbi and rbi >= 6:
            headline = f"{name} drove in {rbi} runs"
            detail = f"Went {h}-for-{ab} with {hr or 0} home runs."
        else:
            headline = f"{name} went {h}-for-{ab} with {hr} HR"
            detail = f"Drove in {rbi or 0} runs."

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
            headline = f"{name} threw a {h}-hitter over {ip_display} innings"
            detail = f"Struck out {so or 0} with {bb or 0} walks."
        elif so and so >= 12:
            headline = f"{name} struck out {so} in {ip_display} innings"
            detail = f"Allowed {h or 0} hits and {er or 0} earned runs."
        else:
            headline = f"{name} dominated: {ip_display} IP, {so or 0} K, {er or 0} ER"
            detail = f"Allowed just {h or 0} hits and {bb or 0} walks."

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


def detect_hot_streaks_pelt(conn, season):
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
            "game_date": str(date.today()),
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


def detect_league_leaders(conn, season):
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
                "game_date": str(date.today()),
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

    events = []

    # Tier 1
    print("  Running Tier 1 detectors...")
    events += detect_hitting_streaks(conn, season, latest_date, min_games=8)
    events += detect_onbase_streaks(conn, season, latest_date, min_games=12)
    events += detect_hr_streaks(conn, season, latest_date, min_games=4)
    events += detect_pitching_streaks(conn, season, latest_date)
    events += detect_season_pace(conn, season)
    t1_count = len(events)
    print(f"    Tier 1: {t1_count} events")

    # Tier 2
    print("  Running Tier 2 detectors...")
    events += detect_career_milestones(conn, season)
    events += detect_single_game_performances(conn, season, latest_date)
    events += detect_hot_streaks_pelt(conn, season)
    t2_count = len(events) - t1_count
    print(f"    Tier 2: {t2_count} events")

    # Tier 3 backfill if needed
    if len(events) < 3:
        print("  Running Tier 3 backfill...")
        events += detect_hitting_streaks_relaxed(conn, season, latest_date)
        events += detect_league_leaders(conn, season)
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
