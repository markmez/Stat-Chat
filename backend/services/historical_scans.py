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

def scan_cross_season_streaks(conn, season, latest_date):
    """Find active streaks carrying over from last season."""
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
        ("stolen_bases", "SB", "stolen bases", 3, None),  # no game log col
        ("batting_avg", "AVG", "batting average", None, "hits"),
        ("obp", "OBP", "OBP", None, "hits"),
        ("ops", "OPS", "OPS", None, "hits"),
        ("slg", "SLG", "slugging", None, "hits"),
    ]

    # Min PA for rate stats — higher threshold reduces noise from small samples
    min_pa_rate = 30

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

            # Did the leader play on latest_date? (Required for counting stats, not rate stats)
            played = conn.execute("""
                SELECT COUNT(*) FROM game_batting_logs
                WHERE player_id = ? AND season = ? AND date = ?
            """, (leader_pid, season, latest_date)).fetchone()[0]
            if not played and not is_rate:
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

            # For rate stats: leader can change through inaction (other player drops)
            else:
                if leader_val <= runner_up_val:
                    continue

                # Did the leader play on latest_date?
                leader_played_today = conn.execute("""
                    SELECT COUNT(*) FROM game_batting_logs
                    WHERE player_id = ? AND season = ? AND date = ?
                """, (leader_pid, season, latest_date)).fetchone()[0] > 0

                if leader_played_today:
                    # Standard: leader took the lead through their own play
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

    min_ip_rate = 10  # ~3.1 IP minimum for rate stats early season

    for col, abbrev, label, min_val, game_col in pitch_stats:
        is_rate = col in ("era", "whip")
        ip_filter = f"AND sp.ip_outs >= {min_ip_rate * 3}" if is_rate else ""
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

            # Did the leader pitch on latest_date?
            played = conn.execute("""
                SELECT COUNT(*) FROM game_pitching_logs
                WHERE player_id = ? AND season = ? AND date = ?
            """, (leader_pid, season, latest_date)).fetchone()[0]
            if not played and not is_rate:
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
                # Rate stat: leader changed through play
                if played:
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
        SELECT hits, at_bats, home_runs, rbi, doubles, triples, walks
        FROM game_batting_logs
        WHERE player_id = ? AND date = ? AND season = ?
    """, (player_id, date, season)).fetchone()
    if row and (row[0] or 0) + (row[6] or 0) > 0:
        h, ab, hr, rbi, d, t, bb = row
        base = f"{h}-for-{ab}"
        extras = []
        if hr: extras.append(f"{hr} HR")
        if d: extras.append(f"{d} 2B")
        if t: extras.append(f"{t} 3B")
        if rbi: extras.append(f"{rbi} RBI")
        if bb and h == 0: extras.append(f"{bb} BB")
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
            game_intro = f"{player} went {game_line}" if game_line else f"{player}"

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

            headline = f"{game_intro}, and {label} — {context}."

        elif f["type"].startswith("cross_season_"):
            streak = f["streak"]
            label = f["label"]
            game_intro = f"{player} went {game_line}" if game_line else f"{player}"
            ctx = "dating back to last season" if f["spans_seasons"] else "this season"

            headline = f"{game_intro}, extending the longest active {label} in MLB to {streak} games, {ctx}."

        elif f["type"] == "10k_0bb_first_2_starts":
            k = f["k"]
            game_intro = f"{player} went {game_line}" if game_line else f"{player}"

            if not hist:
                context = f"the only pitcher to do this in over 100 years"
            else:
                last = hist[0]
                context = f"only {len(hist)} pitchers have done this in over 100 years, the last being {last['player']} in {last['season']}"
                secondary_names.append(last["player"])

            headline = f"{game_intro}, reaching {k} K and 0 BB through his first 2 starts of the season — {context}."

        elif f["type"] == "scoreless_first_n_starts":
            ip = f["ip"]
            k = f["k"]
            starts = f["starts"]
            game_intro = f"{player} went {game_line}" if game_line else f"{player}"

            headline = f"{game_intro}, and has now thrown {starts} consecutive scoreless starts to open the season ({ip} IP, {k} K, 0 ER)."

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

        elif f["type"] == "team_starter_era":
            era = f["era"]
            er = f["er"]
            ip = f["ip"]
            games = f["games"]
            rank = f["rank"]

            if rank == 1:
                headline = f"The {team}'s starters have a {era:.2f} ERA ({er} ER in {ip} IP) through {games} games — the lowest by any team's starters in over 100 years."
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
                    headline = f"{game_intro}, giving him the most {stat_label} ({val}) in a player's first {cg} career games in over 100 years, passing {passed_str}."
                    secondary_names.extend(t["player"] for t in tied[:3])
                else:
                    headline = f"{game_intro}, giving him the most {stat_label} ({val}) in a player's first {cg} career games in over 100 years."
            else:
                if ahead:
                    ahead_str = ", ".join(f"{a['player']} ({a['value']}, {a['season']})" for a in ahead[:3])
                    headline = f"{game_intro}, giving him {val} {stat_label} in his first {cg} career games — #{rank} all-time, behind only {ahead_str}."
                    secondary_names.extend(a["player"] for a in ahead[:3])
                else:
                    headline = f"{game_intro}, giving him {val} {stat_label} in his first {cg} career games — #{rank} all-time."

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
                           doubles, triples
                    FROM game_batting_logs
                    WHERE player_id = ? AND date = (
                        SELECT MAX(date) FROM game_batting_logs
                        WHERE player_id = ? AND season = ?
                    ) AND season = ?
                    LIMIT 1
                """, (pid, pid, season, season)).fetchone()
                if game_row:
                    h, ab, bb, hbp, hr, d, t = game_row
                    h, ab, bb = h or 0, ab or 0, bb or 0
                    hbp, hr, d, t = hbp or 0, hr or 0, d or 0, t or 0
                    parts = [f"{h} for {ab}"]
                    if bb > 0:
                        parts.append(f"{'a walk' if bb == 1 else f'{bb} walks'}")
                    if hr > 0:
                        parts.append(f"{'a homer' if hr == 1 else f'{hr} homers'}")
                    elif d > 0 or t > 0:
                        xbh_parts = []
                        if d > 0: xbh_parts.append(f"{'a double' if d == 1 else f'{d} doubles'}")
                        if t > 0: xbh_parts.append(f"{'a triple' if t == 1 else f'{t} triples'}")
                        parts.extend(xbh_parts)
                    if len(parts) == 1:
                        game_intro = f"{player} went {parts[0]}"
                    elif len(parts) == 2:
                        game_intro = f"{player} went {parts[0]} with {parts[1]}"
                    else:
                        extras = parts[1:]
                        game_intro = f"{player} went {parts[0]} with {', '.join(extras[:-1])} and {extras[-1]}"
                else:
                    game_intro = f"{player}"
            elif game_line:
                game_intro = f"{player} went {game_line}"
            elif stat == "stolen_bases":
                game_intro = f"{player} swiped a bag"
            elif stat == "saves":
                game_intro = f"{player} earned a save"
            elif stat == "wins":
                game_intro = f"{player} picked up a win"
            else:
                game_intro = f"{player}"

            if isinstance(val, float):
                if val >= 1.0:
                    val_str = f"{val:.3f}"
                else:
                    val_str = f".{int(val * 1000):03d}"
                if runner_up_val >= 1.0:
                    ru_str = f"{runner_up_val:.3f}"
                else:
                    ru_str = f".{int(runner_up_val * 1000):03d}"
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

            headline = f"{game_intro}, {took_verb} the {scope} lead in {stat_label} ({val_str}), passing {runner_up} ({ru_str})."
            secondary_names.append(runner_up)

        elif f["type"] == "leaderboard_tie":
            val = f["value"]
            scope = f["scope"]
            stat_label = f["stat_label"]
            tied_with = f["tied_with"]
            game_intro = f"{player} went {game_line}" if game_line else f"{player}"
            headline = f"{game_intro}, tying {tied_with} for the {scope} lead in {stat_label} ({val})."
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
        events.append({
            "headline": headline,
            "category": "historical",
            "player_names": all_names,
            "team_names": [team] if team else [],
        })
        if pid:
            seen_players.add(pid)

    return events
