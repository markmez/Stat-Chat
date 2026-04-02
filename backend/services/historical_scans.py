"""
Historical scan engine for notable events.

Uses the pre-computed `historical_index` table for instant lookups.
The index is built once by `build_historical_index.py` and covers 1920-2025.

Each scan computes a current-season stat, then looks up historical
comparisons from the index.
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
        # Compute current-season streaks from game 1
        players = conn.execute("""
            SELECT DISTINCT player_id FROM game_batting_logs
            WHERE season = ? AND at_bats > 0
        """, (season,)).fetchall()

        for (pid,) in players:
            games = conn.execute("""
                SELECT hits, walks, COALESCE(hit_by_pitch, 0), home_runs
                FROM game_batting_logs
                WHERE player_id = ? AND season = ? AND at_bats > 0
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

def scan_cross_season_streaks(conn, season, latest_date):
    """Find active streaks carrying over from last season."""
    facts = []

    checks = [
        {
            "name": "hitting_streak",
            "condition_sql": "hits > 0",
            "filter": "at_bats > 0",
            "min_games": 10,
            "label": "hitting streak",
        },
        {
            "name": "on_base_streak",
            "condition_sql": "(hits + walks + COALESCE(hit_by_pitch, 0)) > 0",
            "filter": "(at_bats > 0 OR walks > 0 OR COALESCE(hit_by_pitch, 0) > 0)",
            "min_games": 15,
            "label": "on-base streak",
        },
    ]

    for check in checks:
        players = conn.execute(f"""
            SELECT DISTINCT player_id FROM game_batting_logs
            WHERE season = ? AND date = ? AND {check['filter']}
        """, (season, latest_date)).fetchall()

        for (pid,) in players:
            # Current season (reverse)
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

            # Extend into last season if streak covers all current games
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

            if streak >= check["min_games"]:
                name = _player_name(conn, pid)
                team = _team_display(conn, pid, season)
                facts.append({
                    "type": f"cross_season_{check['name']}",
                    "player": name,
                    "player_id": pid,
                    "team": team,
                    "streak": streak,
                    "spans_seasons": spans,
                    "label": check["label"],
                })

    # Keep top per type
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
    """Find notable pitching feats in first N starts, with historical lookup."""
    facts = []

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

        name = _player_name(conn, pid)
        team = _team_display(conn, pid, season)

        # Check first 2 starts: 15+ K and 0 BB (rare — ~47 in 100+ years at 10+, much fewer at 15+)
        if num_starts >= 2:
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

        # All starts scoreless with 5+ IP
        all_scoreless = all((s[2] or 0) == 0 and (s[3] or 0) >= 15 for s in starts)
        if all_scoreless and num_starts >= 2:
            total_outs = sum(s[3] or 0 for s in starts)
            total_k = sum(s[0] or 0 for s in starts)
            ip = f"{total_outs // 3}.{total_outs % 3}"

            facts.append({
                "type": "scoreless_first_n_starts",
                "player": name,
                "player_id": pid,
                "team": team,
                "starts": num_starts,
                "ip": ip,
                "k": total_k,
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
        SELECT hits, at_bats, home_runs, rbi, doubles, triples, walks
        FROM game_batting_logs
        WHERE player_id = ? AND date = ? AND season = ?
    """, (player_id, date, season)).fetchone()
    if row and (row[0] or 0) + (row[6] or 0) > 0:
        h, ab, hr, rbi, d, t, bb = row
        parts = [f"{h}-for-{ab}"]
        if hr: parts.append(f"{hr} HR")
        if d: parts.append(f"{d} 2B")
        if rbi: parts.append(f"{rbi} RBI")
        if bb and h == 0: parts.append(f"{bb} BB")  # Show walks when hitless
        return ", ".join(parts), "batting"

    return None, None


def template_facts(conn, facts, season, latest_date):
    """Convert structured facts into templated feed-ready text.
    No Sonnet needed — deterministic copy from DB facts."""
    events = []

    for f in facts:
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

        if f["type"].startswith("start_") and f["type"] != "start_onbase_streak":
            # Batting start-of-season streak
            streak = f["streak"]
            label = f["label"]
            game_intro = f"{player} went {game_line} last night" if game_line else f"{player}"

            if hist_count == 0:
                context = f"no other player has done this in over 100 years"
            elif hist_count <= 10:
                last = hist[0]
                context = f"only {hist_count} players have done this in over 100 years, the last being {last['player']} in {last['season']}"
            else:
                last = hist[0]
                context = f"the last player to do this was {last['player']} in {last['season']}"

            headline = f"{game_intro}, and {label} — {context}."
            events.append({
                "headline": headline,
                "category": "historical",
                "player_names": [player],
                "team_names": [team] if team else [],
            })

        elif f["type"].startswith("cross_season_"):
            streak = f["streak"]
            label = f["label"]
            game_intro = f"{player} went {game_line} last night" if game_line else f"{player}"
            ctx = "dating back to last season" if f["spans_seasons"] else "this season"

            headline = f"{game_intro}, extending the longest active {label} in MLB to {streak} games, {ctx}."
            events.append({
                "headline": headline,
                "category": "historical",
                "player_names": [player],
                "team_names": [team] if team else [],
            })

        elif f["type"] == "10k_0bb_first_2_starts":
            k = f["k"]
            game_intro = f"{player} went {game_line} last night" if game_line else f"{player}"

            if not hist:
                context = f"the only pitcher to do this in over 100 years"
            else:
                last = hist[0]
                context = f"only {len(hist)} pitchers have done this in over 100 years, the last being {last['player']} in {last['season']}"

            headline = f"{game_intro}, reaching {k} K and 0 BB through his first 2 starts — {context}."
            events.append({
                "headline": headline,
                "category": "historical",
                "player_names": [player],
                "team_names": [team] if team else [],
            })

        elif f["type"] == "scoreless_first_n_starts":
            ip = f["ip"]
            k = f["k"]
            starts = f["starts"]
            game_intro = f"{player} went {game_line} last night" if game_line else f"{player}"

            headline = f"{game_intro}, and has now thrown {starts} consecutive scoreless starts to open the season ({ip} IP, {k} K, 0 ER)."
            events.append({
                "headline": headline,
                "category": "historical",
                "player_names": [player],
                "team_names": [team] if team else [],
            })

        elif f["type"] == "team_fewest_er":
            er = f["er"]
            games = f["games"]
            rank = f["rank"]

            if rank == 1:
                headline = f"The {team} have allowed just {er} earned runs through {games} games — the fewest by any team in over 100 years."
            elif rank <= 5:
                hist_str = ", ".join(f"the {h['season']} {h['team']} ({h['er']})"
                                   for h in hist[:3])
                headline = f"The {team} have allowed just {er} earned runs through {games} games, #{rank} all-time behind only {hist_str}."
            else:
                headline = f"The {team} have allowed just {er} earned runs through {games} games, #{rank} all-time."

            events.append({
                "headline": headline,
                "category": "historical",
                "player_names": [],
                "team_names": [team],
            })

    return events
