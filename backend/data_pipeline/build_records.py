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
import os
import sqlite3
import sys
import time

# Import franchise mapping so records can be consolidated per franchise
# (Athletics records span PHA/KC1/OAK/ATH codes, etc.)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from services.franchise import FRANCHISE_MAP, FRANCHISE_CANONICAL


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


def _canonical_franchises_from_teams(team_codes):
    """Given a list of raw team codes, return canonical franchise codes.
    Deduped — one entry per franchise. Teams without a canonical mapping
    (defunct pre-1900 clubs, Federal League, Negro League etc.) pass through
    as-is so their records aren't lost."""
    seen = set()
    result = []
    for tc in team_codes:
        canon = FRANCHISE_CANONICAL.get(tc, tc)
        if canon not in seen:
            seen.add(canon)
            result.append(canon)
    return sorted(result)


def _franchise_team_list(canon):
    """Expand a canonical franchise code to the full list of historical codes
    to include when querying. Single-code franchises return [canon]."""
    return FRANCHISE_MAP.get(canon, [canon])


def _build_team_split_indexes(conn):
    """Pre-normalize the `team` column (which can be slash-separated like
    'NYA/BOS' for mid-season trades) into per-row indexed lookups.

    Without this, querying records per franchise requires OR-chained LIKE
    patterns that can't use indexes. With these temp tables, queries can
    use indexed equality lookups, ~50-100x faster.

    Creates two temp tables:
      season_batting_teams (player_id, season, team)
      season_pitching_teams (player_id, season, team)
    Both indexed on (team, player_id, season).
    """
    print("Pre-normalizing team columns into indexed lookup tables...")
    for kind, src in [
        ("season_batting_teams", "season_batting_stats"),
        ("season_pitching_teams", "season_pitching_stats"),
    ]:
        conn.execute(f"DROP TABLE IF EXISTS {kind}")
        conn.execute(f"""
            CREATE TEMP TABLE {kind} (
                player_id TEXT, season INTEGER, team TEXT
            )
        """)
        rows = conn.execute(f"SELECT player_id, season, team FROM {src} WHERE team IS NOT NULL").fetchall()
        insert_data = []
        for pid, season, team_str in rows:
            for t in team_str.split('/'):
                t = t.strip()
                if t:
                    insert_data.append((pid, season, t))
        conn.executemany(
            f"INSERT INTO {kind} VALUES (?, ?, ?)",
            insert_data
        )
        conn.execute(f"CREATE INDEX idx_{kind}_team ON {kind}(team, player_id, season)")
        print(f"  {kind}: {len(insert_data):,} rows indexed")


def _player_name(conn, player_id):
    """Look up player name from players table."""
    row = conn.execute("SELECT name FROM players WHERE player_id = ?", (player_id,)).fetchone()
    return row[0] if row else player_id


# ---------------------------------------------------------------------------
# Career records (summed across all seasons with that team)
# ---------------------------------------------------------------------------

def _build_career_records(conn, canonical_code, stats_config, table_name, target_table):
    """Build career records for a franchise. canonical_code is the single
    code we store under (e.g. "ATH" for the Athletics); we query all
    historical codes for that franchise (PHA/KC1/OAK/ATH).

    Uses the pre-built season_batting_teams / season_pitching_teams indexed
    lookup tables for fast franchise filtering. Driving JOIN pattern is much
    faster than correlated EXISTS subquery.
    """
    team_codes = _franchise_team_list(canonical_code)
    teams_lookup = "season_batting_teams" if "batting" in table_name else "season_pitching_teams"
    placeholders = ",".join("?" * len(team_codes))

    count = 0
    for stat_name, agg_expr, _ in stats_config:
        order = "ASC" if stat_name in LOWER_IS_BETTER else "DESC"

        # DISTINCT on teams_lookup first dedupes against the (rare) case of
        # a player on multiple franchise codes in same year (not possible
        # for well-formed franchises, but defensive).
        rows = conn.execute(f"""
            SELECT s.player_id, {agg_expr} as val
            FROM (SELECT DISTINCT player_id, season FROM {teams_lookup}
                  WHERE team IN ({placeholders})) t
            JOIN {table_name} s ON s.player_id = t.player_id AND s.season = t.season
            GROUP BY s.player_id
            HAVING val IS NOT NULL
            ORDER BY val {order}
            LIMIT {TOP_N}
        """, team_codes).fetchall()

        for pid, val in rows:
            name = _player_name(conn, pid)
            conn.execute(f"""
                INSERT OR REPLACE INTO {target_table}_new
                ({'' if target_table == 'mlb_records' else 'team_code, '}stat, record_type, value, player_name, player_id, season)
                VALUES ({'' if target_table == 'mlb_records' else '?, '}?, 'career', ?, ?, ?, NULL)
            """, (canonical_code, stat_name, val, name, pid) if target_table == 'team_records' else
                 (stat_name, val, name, pid))
            count += 1

    return count


# ---------------------------------------------------------------------------
# Single-season records
# ---------------------------------------------------------------------------

def _build_season_records(conn, canonical_code, stats_config, table_name, target_table):
    """Build single-season records for a franchise (multi-code if relocated)."""
    team_codes = _franchise_team_list(canonical_code)
    teams_lookup = "season_batting_teams" if "batting" in table_name else "season_pitching_teams"
    placeholders = ",".join("?" * len(team_codes))

    count = 0
    for stat_name, col_expr, qualifier in stats_config:
        order = "ASC" if stat_name in LOWER_IS_BETTER else "DESC"
        where = f"AND ({qualifier})" if qualifier else ""

        rows = conn.execute(f"""
            SELECT player_id, val, season FROM (
                SELECT s.player_id, s.{col_expr} as val, s.season,
                    ROW_NUMBER() OVER (PARTITION BY s.player_id ORDER BY s.{col_expr} {order}) as rn
                FROM (SELECT DISTINCT player_id, season FROM {teams_lookup}
                      WHERE team IN ({placeholders})) t
                JOIN {table_name} s ON s.player_id = t.player_id AND s.season = t.season
                WHERE s.{col_expr} IS NOT NULL
                  {where}
            ) WHERE rn = 1
            ORDER BY val {order}
            LIMIT {TOP_N}
        """, team_codes).fetchall()

        for pid, val, season in rows:
            name = _player_name(conn, pid)
            conn.execute(f"""
                INSERT OR REPLACE INTO {target_table}_new
                ({'' if target_table == 'mlb_records' else 'team_code, '}stat, record_type, value, player_name, player_id, season)
                VALUES ({'' if target_table == 'mlb_records' else '?, '}?, 'season', ?, ?, ?, ?)
            """, (canonical_code, stat_name, val, name, pid, season) if target_table == 'team_records' else
                 (stat_name, val, name, pid, season))
            count += 1

    return count


# ---------------------------------------------------------------------------
# Single-game records
# ---------------------------------------------------------------------------

def _build_game_records(conn, canonical_code, stats_config, log_table, season_table, target_table):
    """Build single-game records for a franchise.

    Game logs don't have a 'team' column — we join through season stats to find
    which team a player was on. We also filter out spring training by requiring
    date > YYYY-03-25 for each season. canonical_code=None means MLB-wide.
    """
    count = 0
    # For franchise-scoped: use driving JOIN from the indexed teams lookup
    # table. For MLB-wide: no filter, just full table aggregate.
    teams_lookup = "season_batting_teams" if "batting" in season_table else "season_pitching_teams"
    if canonical_code is not None:
        team_codes = _franchise_team_list(canonical_code)
        placeholders = ",".join("?" * len(team_codes))
        # Driving JOIN pattern: pre-filter game logs to only (player, season)
        # tuples that were on this franchise. Much faster than EXISTS for
        # 4.8M-row game_logs.
        inner_from = f"""
            (SELECT DISTINCT player_id, season FROM {teams_lookup} WHERE team IN ({placeholders})) t
            JOIN {log_table} g ON g.player_id = t.player_id AND g.season = t.season
        """
        params = tuple(team_codes)
    else:
        inner_from = f"{log_table} g"
        params = ()
    for stat_name, col in stats_config:
        order = "ASC" if stat_name in LOWER_IS_BETTER else "DESC"

        # Phase 1: top 5 players by best game (fast aggregate with GROUP BY)
        top = conn.execute(f"""
            SELECT g.player_id, MAX(g.{col}) as val
            FROM {inner_from}
            WHERE g.{col} IS NOT NULL
              AND g.date > (g.season || '-03-25')
            GROUP BY g.player_id
            HAVING val IS NOT NULL
            ORDER BY val {order}
            LIMIT {TOP_N}
        """, params).fetchall()

        # Phase 2: for each top player, look up a season where that max
        # occurred (separate indexed lookup — much faster than a window
        # function over the full franchise game-log rowset).
        rows = []
        for pid, val in top:
            season_row = conn.execute(f"""
                SELECT season FROM {log_table}
                WHERE player_id = ? AND {col} = ?
                  AND date > (season || '-03-25')
                ORDER BY season DESC LIMIT 1
            """, (pid, val)).fetchone()
            season = season_row[0] if season_row else None
            rows.append((pid, val, season))

        for pid, val, season in rows:
            name = _player_name(conn, pid)
            if target_table == 'team_records':
                conn.execute("""
                    INSERT OR REPLACE INTO team_records_new
                    (team_code, stat, record_type, value, player_name, player_id, season)
                    VALUES (?, ?, 'game', ?, ?, ?, ?)
                """, (canonical_code, stat_name, val, name, pid, season))
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

    # Pre-build indexed lookup tables for franchise filtering — turns
    # OR-chained LIKE patterns (full table scans) into indexed lookups.
    _build_team_split_indexes(conn)

    # Get all distinct team codes, then collapse to canonical franchises
    all_teams = _get_all_single_team_codes(conn)
    canonical_franchises = _canonical_franchises_from_teams(all_teams)
    print(f"Found {len(all_teams)} raw team codes, "
          f"{len(canonical_franchises)} canonical franchises.")

    total = 0
    start = time.time()

    # --------------- Team records (one entry per franchise) ---------------
    for i, canon in enumerate(canonical_franchises):
        team_count = 0

        # Career batting
        team_count += _build_career_records(
            conn, canon, BATTING_CAREER_STATS, "season_batting_stats", "team_records")

        # Career pitching
        team_count += _build_career_records(
            conn, canon, PITCHING_CAREER_STATS, "season_pitching_stats", "team_records")

        # Season batting
        team_count += _build_season_records(
            conn, canon, BATTING_SEASON_STATS, "season_batting_stats", "team_records")

        # Season pitching
        team_count += _build_season_records(
            conn, canon, PITCHING_SEASON_STATS, "season_pitching_stats", "team_records")

        # Game batting
        team_count += _build_game_records(
            conn, canon, BATTING_GAME_STATS, "game_batting_logs",
            "season_batting_stats", "team_records")

        # Game pitching
        team_count += _build_game_records(
            conn, canon, PITCHING_GAME_STATS, "game_pitching_logs",
            "season_pitching_stats", "team_records")

        total += team_count
        if (i + 1) % 10 == 0 or i == len(canonical_franchises) - 1:
            elapsed = time.time() - start
            print(f"  [{i+1}/{len(canonical_franchises)}] {canon}: {team_count} records "
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
