"""
Historical scan engine for notable events.

Runs specific SQL queries against game logs (1920-2026) to find
Sarah Langs-style historical facts:
  - "Nth player to do X in first N games"
  - "First since [year] to do X" with full historical list
  - Cross-season active streaks
  - Team-level historical rankings

Each scan returns structured facts that Sonnet formats into prose.
"""

import sqlite3
from datetime import date


def _player_name(conn, player_id):
    row = conn.execute("SELECT name FROM players WHERE player_id = ?", (player_id,)).fetchone()
    return row[0] if row else player_id


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


# ---------------------------------------------------------------------------
# Scan: consecutive-game streaks to start a season
# ---------------------------------------------------------------------------

def scan_start_of_season_streaks(conn, season, latest_date):
    """Find players with streaks from the start of the season.

    e.g., "hit in each of the team's first 5 games",
          "reached base in each of first N games",
          "HR in each of first N games"
    """
    facts = []

    streak_types = [
        {
            "name": "hit_every_game",
            "condition": lambda g: g["hits"] > 0,
            "filter": "at_bats > 0",
            "min_games": 5,
            "template": "has hit safely in each of the first {n} games this season",
            "history_condition": "hits > 0",
            "history_filter": "at_bats > 0",
        },
        {
            "name": "reached_base_every_game",
            "condition": lambda g: (g["hits"] + g["walks"] + g["hbp"]) > 0,
            "filter": "(at_bats > 0 OR walks > 0 OR COALESCE(hit_by_pitch, 0) > 0)",
            "min_games": 7,
            "template": "has reached base in each of the first {n} games this season",
            "history_condition": "(hits + walks + COALESCE(hit_by_pitch, 0)) > 0",
            "history_filter": "(at_bats > 0 OR walks > 0 OR COALESCE(hit_by_pitch, 0) > 0)",
        },
        {
            "name": "multi_hit_every_game",
            "condition": lambda g: g["hits"] >= 2,
            "filter": "at_bats > 0",
            "min_games": 4,
            "template": "has had multiple hits in each of the first {n} games this season",
            "history_condition": "hits >= 2",
            "history_filter": "at_bats > 0",
        },
        {
            "name": "hr_every_game",
            "condition": lambda g: g["hr"] > 0,
            "filter": "at_bats > 0",
            "min_games": 3,
            "template": "has homered in each of the first {n} games this season",
            "history_condition": "home_runs > 0",
            "history_filter": "at_bats > 0",
        },
    ]

    for stype in streak_types:
        # Get all players with games this season, ordered by date
        players = conn.execute(f"""
            SELECT DISTINCT player_id FROM game_batting_logs
            WHERE season = ? AND {stype['filter']}
        """, (season,)).fetchall()

        for (pid,) in players:
            games = conn.execute(f"""
                SELECT date, hits, walks, COALESCE(hit_by_pitch, 0) as hbp,
                       home_runs as hr, at_bats
                FROM game_batting_logs
                WHERE player_id = ? AND season = ? AND {stype['filter']}
                ORDER BY date ASC
            """, (pid, season)).fetchall()

            # Check if streak runs from game 1
            streak = 0
            for g in games:
                row = {"hits": g[1], "walks": g[2], "hbp": g[3], "hr": g[4], "at_bats": g[5]}
                if stype["condition"](row):
                    streak += 1
                else:
                    break

            if streak >= stype["min_games"] and streak == len(games):
                # Perfect start — streak covers all games played
                name = _player_name(conn, pid)
                team = _team_display(conn, pid, season)

                # Historical lookup: who else has done this?
                historical = _find_historical_season_start_streak(
                    conn, streak, stype["history_condition"],
                    stype["history_filter"], season, pid
                )

                facts.append({
                    "type": f"season_start_{stype['name']}",
                    "player": name,
                    "team": team,
                    "streak": streak,
                    "text": stype["template"].format(n=streak),
                    "historical": historical,
                })

    return facts


def _find_historical_season_start_streak(conn, streak_len, condition_sql,
                                          filter_sql, exclude_season, exclude_player):
    """Find historical instances of season-opening streaks.

    Efficient approach: for each season, get all games ordered by player+date,
    then walk through in Python to find start-of-season streaks.
    One query per season instead of one per player-season.
    """
    matches = []

    seasons = conn.execute("""
        SELECT DISTINCT season FROM game_batting_logs
        WHERE season < ? AND season >= 1920
        ORDER BY season DESC
    """, (exclude_season,)).fetchall()

    for (szn,) in seasons:
        games = conn.execute(f"""
            SELECT player_id, ({condition_sql}) as met
            FROM game_batting_logs
            WHERE season = ? AND {filter_sql}
            ORDER BY player_id, date ASC
        """, (szn,)).fetchall()

        # Walk through, tracking start-of-season streak per player
        current_pid = None
        current_streak = 0
        broken = False

        for pid, met in games:
            if pid != current_pid:
                # Finalize previous player
                if current_pid and current_streak >= streak_len and current_pid != exclude_player:
                    name = _player_name(conn, current_pid)
                    matches.append({"player": name, "season": szn, "games": current_streak})
                current_pid = pid
                current_streak = 0
                broken = False

            if not broken:
                if met:
                    current_streak += 1
                else:
                    broken = True

        # Finalize last player
        if current_pid and current_streak >= streak_len and current_pid != exclude_player:
            name = _player_name(conn, current_pid)
            matches.append({"player": name, "season": szn, "games": current_streak})

    return matches


# ---------------------------------------------------------------------------
# Scan: cross-season active streaks
# ---------------------------------------------------------------------------

def scan_cross_season_streaks(conn, season, latest_date):
    """Find active streaks that carry over from last season.

    e.g., "Ohtani has the longest active on-base streak in MLB (36 games)"
    """
    facts = []

    streak_checks = [
        {
            "name": "hitting_streak",
            "condition": "hits > 0",
            "filter": "at_bats > 0",
            "min_games": 10,
            "label": "hitting streak",
        },
        {
            "name": "on_base_streak",
            "condition": "(hits + walks + COALESCE(hit_by_pitch, 0)) > 0",
            "filter": "(at_bats > 0 OR walks > 0 OR COALESCE(hit_by_pitch, 0) > 0)",
            "min_games": 15,
            "label": "on-base streak",
        },
    ]

    for check in streak_checks:
        # Get players active this season
        players = conn.execute(f"""
            SELECT DISTINCT player_id FROM game_batting_logs
            WHERE season = ? AND date = ? AND {check['filter']}
        """, (season, latest_date)).fetchall()

        for (pid,) in players:
            # Get current season games (reverse order)
            current_games = conn.execute(f"""
                SELECT ({check['condition']}) as met
                FROM game_batting_logs
                WHERE player_id = ? AND season = ? AND {check['filter']}
                ORDER BY date DESC
            """, (pid, season)).fetchall()

            streak = 0
            for (met,) in current_games:
                if met:
                    streak += 1
                else:
                    break

            # If streak covers ALL current season games, extend into last season
            if streak == len(current_games) and streak > 0:
                prev_games = conn.execute(f"""
                    SELECT ({check['condition']}) as met
                    FROM game_batting_logs
                    WHERE player_id = ? AND season = ? AND {check['filter']}
                    ORDER BY date DESC
                """, (pid, season - 1)).fetchall()

                for (met,) in prev_games:
                    if met:
                        streak += 1
                    else:
                        break

            if streak >= check["min_games"]:
                name = _player_name(conn, pid)
                team = _team_display(conn, pid, season)
                spans_seasons = streak > len(current_games)

                facts.append({
                    "type": f"cross_season_{check['name']}",
                    "player": name,
                    "team": team,
                    "streak": streak,
                    "spans_seasons": spans_seasons,
                    "label": check["label"],
                })

    # Sort by streak length, keep top per type
    by_type = {}
    for f in facts:
        t = f["type"]
        if t not in by_type or f["streak"] > by_type[t]["streak"]:
            by_type[t] = f

    return list(by_type.values())


# ---------------------------------------------------------------------------
# Scan: pitching start-of-season dominance
# ---------------------------------------------------------------------------

def scan_pitching_start_of_season(conn, season, latest_date):
    """Find pitching feats in first N starts of season.

    e.g., "10+ K and 0 BB in first 2 starts — only player since 1900 to do this"
    """
    facts = []

    # Get all starters with 2+ starts this season
    starters = conn.execute("""
        SELECT player_id, COUNT(*) as starts
        FROM game_pitching_logs
        WHERE season = ? AND is_start = 1
        GROUP BY player_id
        HAVING starts >= 2
    """, (season,)).fetchall()

    for pid, num_starts in starters:
        starts = conn.execute("""
            SELECT strikeouts, walks, earned_runs, ip_outs, hits
            FROM game_pitching_logs
            WHERE player_id = ? AND season = ? AND is_start = 1
            ORDER BY date ASC
        """, (pid, season)).fetchall()

        total_k = sum(s[0] or 0 for s in starts)
        total_bb = sum(s[1] or 0 for s in starts)
        total_er = sum(s[2] or 0 for s in starts)
        total_outs = sum(s[3] or 0 for s in starts)
        total_h = sum(s[4] or 0 for s in starts)

        name = _player_name(conn, pid)
        team = _team_display(conn, pid, season)

        # Check: 10+ K and 0 BB in first 2 starts
        if num_starts >= 2 and total_k >= 10 and total_bb == 0:
            first_2 = starts[:2]
            k2 = sum(s[0] or 0 for s in first_2)
            bb2 = sum(s[1] or 0 for s in first_2)
            if k2 >= 10 and bb2 == 0:
                historical = _find_pitchers_with_feat_in_first_n_starts(
                    conn, 2, "SUM(strikeouts) >= 10 AND SUM(walks) = 0",
                    season, pid
                )
                facts.append({
                    "type": "10k_0bb_first_2_starts",
                    "player": name,
                    "team": team,
                    "k": k2,
                    "bb": bb2,
                    "starts": 2,
                    "historical": historical,
                })

        # Check: 0 ER in first 2+ starts with 5+ IP each
        all_scoreless = all((s[2] or 0) == 0 and (s[3] or 0) >= 15 for s in starts)
        if all_scoreless and num_starts >= 2:
            ip_display = f"{total_outs // 3}.{total_outs % 3}"
            historical = _find_pitchers_scoreless_first_n_starts(
                conn, num_starts, season, pid
            )
            facts.append({
                "type": "scoreless_first_n_starts",
                "player": name,
                "team": team,
                "starts": num_starts,
                "ip": ip_display,
                "k": total_k,
                "historical": historical,
            })

    return facts


def _find_pitchers_with_feat_in_first_n_starts(conn, n, having_clause, exclude_season, exclude_pid):
    """Find pitchers with 10+ K and 0 BB in first N starts. One query per season."""
    matches = []
    seasons = conn.execute("""
        SELECT DISTINCT season FROM game_pitching_logs
        WHERE season < ? AND season >= 1920 AND is_start = 1
        ORDER BY season DESC
    """, (exclude_season,)).fetchall()

    for (szn,) in seasons:
        # Get all starts ordered by player + date
        starts = conn.execute("""
            SELECT player_id, strikeouts, walks
            FROM game_pitching_logs
            WHERE season = ? AND is_start = 1
            ORDER BY player_id, date ASC
        """, (szn,)).fetchall()

        # Walk through, track first N starts per player
        current_pid = None
        start_count = 0
        total_k = 0
        total_bb = 0

        for pid, k, bb in starts:
            if pid != current_pid:
                # Finalize previous
                if current_pid and start_count >= n and total_k >= 10 and total_bb == 0 and current_pid != exclude_pid:
                    name = _player_name(conn, current_pid)
                    matches.append({"player": name, "season": szn, "k": total_k})
                current_pid = pid
                start_count = 0
                total_k = 0
                total_bb = 0

            if start_count < n:
                start_count += 1
                total_k += (k or 0)
                total_bb += (bb or 0)

        # Finalize last
        if current_pid and start_count >= n and total_k >= 10 and total_bb == 0 and current_pid != exclude_pid:
            name = _player_name(conn, current_pid)
            matches.append({"player": name, "season": szn, "k": total_k})

    return matches


def _find_pitchers_scoreless_first_n_starts(conn, n, exclude_season, exclude_pid):
    """Find pitchers with N scoreless starts (5+ IP) to open a season. One query per season."""
    matches = []
    seasons = conn.execute("""
        SELECT DISTINCT season FROM game_pitching_logs
        WHERE season < ? AND season >= 1920 AND is_start = 1
        ORDER BY season DESC
    """, (exclude_season,)).fetchall()

    for (szn,) in seasons:
        starts = conn.execute("""
            SELECT player_id, earned_runs, ip_outs
            FROM game_pitching_logs
            WHERE season = ? AND is_start = 1
            ORDER BY player_id, date ASC
        """, (szn,)).fetchall()

        current_pid = None
        start_count = 0
        all_scoreless = True

        for pid, er, outs in starts:
            if pid != current_pid:
                if current_pid and start_count >= n and all_scoreless and current_pid != exclude_pid:
                    name = _player_name(conn, current_pid)
                    matches.append({"player": name, "season": szn})
                current_pid = pid
                start_count = 0
                all_scoreless = True

            if start_count < n:
                start_count += 1
                if (er or 0) > 0 or (outs or 0) < 15:
                    all_scoreless = False

        if current_pid and start_count >= n and all_scoreless and current_pid != exclude_pid:
            name = _player_name(conn, current_pid)
            matches.append({"player": name, "season": szn})

    return matches


# ---------------------------------------------------------------------------
# Scan: team-level historical
# ---------------------------------------------------------------------------

def scan_team_historical(conn, season, latest_date):
    """Find team-level historical facts.

    e.g., "Yankees have allowed 6 runs this season, 3rd fewest through 6 games ever"
    """
    facts = []

    # Get team run totals for the current season
    teams = conn.execute("""
        SELECT team, SUM(earned_runs) as team_er, COUNT(DISTINCT date) as games
        FROM game_pitching_logs
        WHERE season = ? AND is_start = 1
        GROUP BY team
        HAVING games >= 4
    """, (season,)).fetchall()

    for team, team_er, games in teams:
        if team_er is None:
            continue

        # How does this compare historically? Count teams with fewer ER through same # of games
        fewer = conn.execute("""
            SELECT team, season, SUM(earned_runs) as ter
            FROM game_pitching_logs
            WHERE season < ? AND season >= 1920 AND is_start = 1
            GROUP BY team, season
            HAVING COUNT(DISTINCT date) >= ? AND SUM(earned_runs) < ?
            ORDER BY ter ASC
        """, (season, games, team_er)).fetchall()

        if len(fewer) <= 5:  # Top 5 or fewer historically
            rank = len(fewer) + 1
            historical_list = [(t, s, er) for t, s, er in fewer]
            facts.append({
                "type": "team_fewest_er",
                "team": team,
                "er": team_er,
                "games": games,
                "rank": rank,
                "historical": historical_list,
            })

    return facts


# ---------------------------------------------------------------------------
# Main: run all scans
# ---------------------------------------------------------------------------

def run_all_scans(conn, season, latest_date):
    """Run all historical scans and return structured facts."""
    all_facts = []

    print("  Running historical scans...")

    facts = scan_start_of_season_streaks(conn, season, latest_date)
    print(f"    Start-of-season streaks: {len(facts)} facts")
    all_facts.extend(facts)

    facts = scan_cross_season_streaks(conn, season, latest_date)
    print(f"    Cross-season streaks: {len(facts)} facts")
    all_facts.extend(facts)

    facts = scan_pitching_start_of_season(conn, season, latest_date)
    print(f"    Pitching start-of-season: {len(facts)} facts")
    all_facts.extend(facts)

    facts = scan_team_historical(conn, season, latest_date)
    print(f"    Team historical: {len(facts)} facts")
    all_facts.extend(facts)

    print(f"  Total historical facts: {len(all_facts)}")
    return all_facts


def format_facts_for_prompt(facts):
    """Convert structured facts into text for the Sonnet prompt."""
    lines = []
    for f in facts:
        if f["type"].startswith("season_start_"):
            hist = f.get("historical", [])
            if hist:
                hist_str = ", ".join(f"{h['season']} {h['player']}" for h in hist[:5])
                lines.append(f"- {f['player']} ({f['team']}) {f['text']}. "
                           f"Previous players to do this: {hist_str}. "
                           f"({len(hist)} total in history since 1920)")
            else:
                lines.append(f"- {f['player']} ({f['team']}) {f['text']}. "
                           f"No other player has done this since 1920.")

        elif f["type"].startswith("cross_season_"):
            ctx = "dating back to last season" if f["spans_seasons"] else "this season"
            lines.append(f"- {f['player']} ({f['team']}) has the longest active "
                        f"{f['label']} in MLB at {f['streak']} games, {ctx}.")

        elif f["type"] == "10k_0bb_first_2_starts":
            hist = f.get("historical", [])
            if hist:
                hist_str = ", ".join(f"{h['season']} {h['player']}" for h in hist[:5])
                lines.append(f"- {f['player']} ({f['team']}) has {f['k']} K and 0 BB in "
                           f"first {f['starts']} starts. Others to do this since 1920: "
                           f"{hist_str}. ({len(hist)} total)")
            else:
                lines.append(f"- {f['player']} ({f['team']}) has {f['k']} K and 0 BB in "
                           f"first {f['starts']} starts. No other pitcher has done this "
                           f"since 1920.")

        elif f["type"] == "scoreless_first_n_starts":
            hist = f.get("historical", [])
            count = len(hist)
            lines.append(f"- {f['player']} ({f['team']}) has thrown {f['starts']} consecutive "
                        f"scoreless starts (5+ IP each) to open the season ({f['ip']} IP, "
                        f"{f['k']} K). {count} other pitchers have done this since 1920.")

        elif f["type"] == "team_fewest_er":
            hist = f.get("historical", [])
            if hist:
                hist_str = ", ".join(f"{s} {t} ({er} ER)" for t, s, er in hist[:3])
                lines.append(f"- {f['team']} has allowed {f['er']} earned runs through "
                           f"{f['games']} games, ranking #{f['rank']} all-time. "
                           f"Fewer: {hist_str}.")
            else:
                lines.append(f"- {f['team']} has allowed {f['er']} earned runs through "
                           f"{f['games']} games, the fewest in MLB history through "
                           f"{f['games']} games.")

    return "\n".join(lines)
