#!/usr/bin/env python3
"""
Build pre-computed team and MLB records tables.

Creates two tables in the stats DB:
  - team_records: top 5 all-time for each team + stat + record_type
  - mlb_records: top 5 all-time MLB-wide for each stat + record_type

Record types: career (summed across seasons), season (single best season),
              game (single best game from game logs).

Usage:
  python build_records.py --db /data/baseball_stats_full.db
"""

import argparse
import sqlite3
import time


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TOP_N = 5  # how many records per group

BATTING_CAREER_STATS = [
    ("games", "SUM(games)", None),
    ("home_runs", "SUM(home_runs)", None),
    ("hits", "SUM(hits)", None),
    ("rbi", "SUM(rbi)", None),
    ("runs", "SUM(runs)", None),
    ("stolen_bases", "SUM(stolen_bases)", None),
    ("doubles", "SUM(doubles)", None),
    ("walks", "SUM(walks)", None),
]

PITCHING_CAREER_STATS = [
    ("wins", "SUM(wins)", None),
    ("strikeouts", "SUM(strikeouts)", None),
    ("saves", "SUM(saves)", None),
    ("games", "SUM(games)", None),  # appearances
]

BATTING_SEASON_STATS = [
    ("home_runs", "home_runs", None),
    ("hits", "hits", None),
    ("rbi", "rbi", None),
    ("runs", "runs", None),
    ("stolen_bases", "stolen_bases", None),
    ("batting_avg", "batting_avg", "plate_appearances >= 400"),
    ("ops", "ops", "plate_appearances >= 400"),
]

PITCHING_SEASON_STATS = [
    ("wins", "wins", None),
    ("strikeouts", "strikeouts", None),
    ("saves", "saves", None),
    ("era", "era", "ip_outs >= 486"),       # 162 IP = 486 outs
    ("whip", "whip", "ip_outs >= 486"),
]

BATTING_GAME_STATS = [
    ("home_runs", "home_runs"),
    ("hits", "hits"),
    ("rbi", "rbi"),
    ("runs", "runs"),
]
# Note: game_batting_logs does NOT have a stolen_bases column

PITCHING_GAME_STATS = [
    ("strikeouts", "strikeouts"),
    ("innings_pitched", "ip_outs"),   # stored as ip_outs (total outs pitched)
]

# Rate stats where lower is better
LOWER_IS_BETTER = {"era", "whip"}


def create_tables(conn):
    """Create temp tables, build into them, then swap at the end.

    This avoids needing an exclusive lock during the build — DROP + rename
    only happens at the very end in a quick transaction.
    """
    conn.execute("DROP TABLE IF EXISTS team_records_new")
    conn.execute("DROP TABLE IF EXISTS mlb_records_new")

    conn.execute("""
        CREATE TABLE team_records_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            team_code TEXT NOT NULL,
            stat TEXT NOT NULL,
            record_type TEXT NOT NULL,
            value REAL,
            player_name TEXT,
            player_id TEXT,
            season INTEGER,
            UNIQUE(team_code, stat, record_type, player_id)
        )
    """)

    conn.execute("""
        CREATE TABLE mlb_records_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stat TEXT NOT NULL,
            record_type TEXT NOT NULL,
            value REAL,
            player_name TEXT,
            player_id TEXT,
            season INTEGER,
            UNIQUE(stat, record_type, player_id)
        )
    """)

    conn.commit()
    print("Created staging tables (team_records_new, mlb_records_new).")


def swap_tables(conn):
    """Atomically swap staging tables into production names."""
    conn.execute("DROP TABLE IF EXISTS team_records")
    conn.execute("DROP TABLE IF EXISTS mlb_records")
    conn.execute("ALTER TABLE team_records_new RENAME TO team_records")
    conn.execute("ALTER TABLE mlb_records_new RENAME TO mlb_records")
    conn.execute("CREATE INDEX idx_team_records_team ON team_records(team_code)")
    conn.execute("CREATE INDEX idx_team_records_player ON team_records(player_id)")
    conn.execute("CREATE INDEX idx_mlb_records_player ON mlb_records(player_id)")
    conn.commit()
    print("Swapped staging tables to production.")


def _get_all_single_team_codes(conn):
    """Get all distinct single-team codes (no slash combos).
    A player with team='NYA/BOS' counts for both NYA and BOS."""
    rows = conn.execute(
        "SELECT DISTINCT team FROM season_batting_stats UNION "
        "SELECT DISTINCT team FROM season_pitching_stats"
    ).fetchall()

    teams = set()
    for (team_str,) in rows:
        if team_str:
            for t in team_str.split("/"):
                t = t.strip()
                if t:
                    teams.add(t)
    return sorted(teams)


def _player_name(conn, player_id):
    """Look up player name from players table."""
    row = conn.execute("SELECT name FROM players WHERE player_id = ?", (player_id,)).fetchone()
    return row[0] if row else player_id


# ---------------------------------------------------------------------------
# Career records (summed across all seasons with that team)
# ---------------------------------------------------------------------------

def _build_career_records(conn, team_code, stats_config, table_name, target_table):
    """Build career records for a team from a stats table.

    For multi-team seasons (e.g. 'NYA/BOS'), we include the player for EACH
    team in the slash-separated string. This means their full-season stats count
    toward every team they played for that year. Acceptable for records purposes.
    """
    count = 0
    for stat_name, agg_expr, _ in stats_config:
        order = "ASC" if stat_name in LOWER_IS_BETTER else "DESC"

        # Match team_code anywhere in the slash-separated team field
        rows = conn.execute(f"""
            SELECT s.player_id, {agg_expr} as val
            FROM {table_name} s
            WHERE ('/' || s.team || '/') LIKE ?
            GROUP BY s.player_id
            HAVING val IS NOT NULL
            ORDER BY val {order}
            LIMIT {TOP_N}
        """, (f"%/{team_code}/%",)).fetchall()

        for pid, val in rows:
            name = _player_name(conn, pid)
            conn.execute(f"""
                INSERT OR REPLACE INTO {target_table}_new
                ({'' if target_table == 'mlb_records' else 'team_code, '}stat, record_type, value, player_name, player_id, season)
                VALUES ({'' if target_table == 'mlb_records' else '?, '}?, 'career', ?, ?, ?, NULL)
            """, (team_code, stat_name, val, name, pid) if target_table == 'team_records' else
                 (stat_name, val, name, pid))
            count += 1

    return count


# ---------------------------------------------------------------------------
# Single-season records
# ---------------------------------------------------------------------------

def _build_season_records(conn, team_code, stats_config, table_name, target_table):
    """Build single-season records for a team."""
    count = 0
    for stat_name, col_expr, qualifier in stats_config:
        order = "ASC" if stat_name in LOWER_IS_BETTER else "DESC"
        where = f"AND ({qualifier})" if qualifier else ""

        # Get each player's best season only (UNIQUE constraint allows one per player)
        rows = conn.execute(f"""
            SELECT player_id, val, season FROM (
                SELECT s.player_id, s.{col_expr} as val, s.season,
                    ROW_NUMBER() OVER (PARTITION BY s.player_id ORDER BY s.{col_expr} {order}) as rn
                FROM {table_name} s
                WHERE ('/' || s.team || '/') LIKE ?
                  AND s.{col_expr} IS NOT NULL
                  {where}
            ) WHERE rn = 1
            ORDER BY val {order}
            LIMIT {TOP_N}
        """, (f"%/{team_code}/%",)).fetchall()

        for pid, val, season in rows:
            name = _player_name(conn, pid)
            conn.execute(f"""
                INSERT OR REPLACE INTO {target_table}_new
                ({'' if target_table == 'mlb_records' else 'team_code, '}stat, record_type, value, player_name, player_id, season)
                VALUES ({'' if target_table == 'mlb_records' else '?, '}?, 'season', ?, ?, ?, ?)
            """, (team_code, stat_name, val, name, pid, season) if target_table == 'team_records' else
                 (stat_name, val, name, pid, season))
            count += 1

    return count


# ---------------------------------------------------------------------------
# Single-game records
# ---------------------------------------------------------------------------

def _build_game_records(conn, team_code, stats_config, log_table, season_table, target_table):
    """Build single-game records for a team.

    Game logs don't have a 'team' column — we join through season stats to find
    which team a player was on. We also filter out spring training by requiring
    date > YYYY-03-25 for each season.
    """
    count = 0
    for stat_name, col in stats_config:
        order = "ASC" if stat_name in LOWER_IS_BETTER else "DESC"

        # Join game logs to season stats to get the team. Filter spring training.
        # For multi-team seasons, include if ANY team matches.
        if team_code is not None:
            team_filter = "AND ('/' || ss.team || '/') LIKE ?"
            params = (f"%/{team_code}/%",)
        else:
            team_filter = ""
            params = ()

        # Use a subquery to get the best game per player, then pick the
        # actual season from that specific game row.
        rows = conn.execute(f"""
            SELECT sub.player_id, sub.val, g2.season
            FROM (
                SELECT g.player_id, MAX(g.{col}) as val
                FROM {log_table} g
                JOIN {season_table} ss ON g.player_id = ss.player_id AND g.season = ss.season
                WHERE g.{col} IS NOT NULL
                  AND g.date > (g.season || '-03-25')
                  {team_filter}
                GROUP BY g.player_id
                HAVING val IS NOT NULL
                ORDER BY val {order}
                LIMIT {TOP_N}
            ) sub
            JOIN {log_table} g2 ON g2.player_id = sub.player_id AND g2.{col} = sub.val
              AND g2.date > (g2.season || '-03-25')
            GROUP BY sub.player_id
            ORDER BY sub.val {order}
        """, params).fetchall()

        for pid, val, season in rows:
            name = _player_name(conn, pid)
            if target_table == 'team_records':
                conn.execute("""
                    INSERT OR REPLACE INTO team_records_new
                    (team_code, stat, record_type, value, player_name, player_id, season)
                    VALUES (?, ?, 'game', ?, ?, ?, ?)
                """, (team_code, stat_name, val, name, pid, season))
            else:
                conn.execute("""
                    INSERT OR REPLACE INTO mlb_records_new
                    (stat, record_type, value, player_name, player_id, season)
                    VALUES (?, 'game', ?, ?, ?, ?)
                """, (stat_name, val, name, pid, season))
            count += 1

    return count


# ---------------------------------------------------------------------------
# MLB-wide career records
# ---------------------------------------------------------------------------

def _build_mlb_career_records(conn, stats_config, table_name):
    """Build MLB-wide career records (no team filter)."""
    count = 0
    for stat_name, agg_expr, _ in stats_config:
        order = "ASC" if stat_name in LOWER_IS_BETTER else "DESC"

        rows = conn.execute(f"""
            SELECT player_id, {agg_expr} as val
            FROM {table_name}
            GROUP BY player_id
            HAVING val IS NOT NULL
            ORDER BY val {order}
            LIMIT {TOP_N}
        """).fetchall()

        for pid, val in rows:
            name = _player_name(conn, pid)
            conn.execute("""
                INSERT OR REPLACE INTO mlb_records_new
                (stat, record_type, value, player_name, player_id, season)
                VALUES (?, 'career', ?, ?, ?, NULL)
            """, (stat_name, val, name, pid))
            count += 1

    return count


def _build_mlb_season_records(conn, stats_config, table_name):
    """Build MLB-wide single-season records (no team filter)."""
    count = 0
    for stat_name, col_expr, qualifier in stats_config:
        order = "ASC" if stat_name in LOWER_IS_BETTER else "DESC"
        where = f"AND ({qualifier})" if qualifier else ""

        rows = conn.execute(f"""
            SELECT player_id, {col_expr} as val, season
            FROM {table_name}
            WHERE {col_expr} IS NOT NULL
              {where}
            ORDER BY val {order}
            LIMIT {TOP_N}
        """).fetchall()

        for pid, val, season in rows:
            name = _player_name(conn, pid)
            conn.execute("""
                INSERT OR REPLACE INTO mlb_records_new
                (stat, record_type, value, player_name, player_id, season)
                VALUES (?, 'season', ?, ?, ?, ?)
            """, (stat_name, val, name, pid, season))
            count += 1

    return count


# ---------------------------------------------------------------------------
# Main build
# ---------------------------------------------------------------------------

def build_all(db_path):
    """Build all records tables."""
    conn = sqlite3.connect(db_path, timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")

    create_tables(conn)

    # Get all distinct single-team codes
    all_teams = _get_all_single_team_codes(conn)
    print(f"Found {len(all_teams)} distinct team codes.")

    total = 0
    start = time.time()

    # --------------- Team records ---------------
    for i, team in enumerate(all_teams):
        team_count = 0

        # Career batting
        team_count += _build_career_records(
            conn, team, BATTING_CAREER_STATS, "season_batting_stats", "team_records")

        # Career pitching
        team_count += _build_career_records(
            conn, team, PITCHING_CAREER_STATS, "season_pitching_stats", "team_records")

        # Season batting
        team_count += _build_season_records(
            conn, team, BATTING_SEASON_STATS, "season_batting_stats", "team_records")

        # Season pitching
        team_count += _build_season_records(
            conn, team, PITCHING_SEASON_STATS, "season_pitching_stats", "team_records")

        # Game batting
        team_count += _build_game_records(
            conn, team, BATTING_GAME_STATS, "game_batting_logs",
            "season_batting_stats", "team_records")

        # Game pitching
        team_count += _build_game_records(
            conn, team, PITCHING_GAME_STATS, "game_pitching_logs",
            "season_pitching_stats", "team_records")

        total += team_count
        if (i + 1) % 10 == 0 or i == len(all_teams) - 1:
            elapsed = time.time() - start
            print(f"  [{i+1}/{len(all_teams)}] {team}: {team_count} records "
                  f"(total: {total}, {elapsed:.1f}s)")

    conn.commit()
    print(f"\nTeam records: {total} total.")

    # --------------- MLB-wide records ---------------
    mlb_count = 0
    print("\nBuilding MLB-wide records...")

    mlb_count += _build_mlb_career_records(conn, BATTING_CAREER_STATS, "season_batting_stats")
    mlb_count += _build_mlb_career_records(conn, PITCHING_CAREER_STATS, "season_pitching_stats")
    print(f"  Career: {mlb_count}")

    mlb_count += _build_mlb_season_records(conn, BATTING_SEASON_STATS, "season_batting_stats")
    mlb_count += _build_mlb_season_records(conn, PITCHING_SEASON_STATS, "season_pitching_stats")
    print(f"  + Season: {mlb_count}")

    mlb_count += _build_game_records(
        conn, None, BATTING_GAME_STATS, "game_batting_logs",
        "season_batting_stats", "mlb_records")
    mlb_count += _build_game_records(
        conn, None, PITCHING_GAME_STATS, "game_pitching_logs",
        "season_pitching_stats", "mlb_records")
    print(f"  + Game: {mlb_count}")

    conn.commit()

    # Swap staging tables to production names
    print("\nSwapping tables...")
    swap_tables(conn)
    conn.close()

    elapsed = time.time() - start
    print(f"\nDone. {total} team records + {mlb_count} MLB records in {elapsed:.1f}s.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build pre-computed records tables")
    parser.add_argument("--db", default="/data/baseball_stats_full.db",
                        help="Path to the stats database")
    args = parser.parse_args()
    build_all(args.db)
