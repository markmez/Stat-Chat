"""
Monthly per-player aggregates.

Materialized rollup of game_batting_logs and game_pitching_logs into one row
per (player_id, season, month). Powers cross-player month-grouped leaderboards
("most HR in a single month all time") at sub-100ms instead of the 20+s a
full game-logs aggregation takes.

Repopulated by the cron pipeline after game logs refresh. Counts/components
only — rate stats are derived at query time so pipeline doesn't have to know
about every possible rate metric.
"""

import logging
import sqlite3

logger = logging.getLogger("statchat.monthly_aggregates")


_BATTING_SCHEMA = """
CREATE TABLE IF NOT EXISTS monthly_batting_aggregates (
    player_id TEXT NOT NULL,
    season INTEGER NOT NULL,
    month INTEGER NOT NULL,
    games INTEGER NOT NULL,
    plate_appearances INTEGER NOT NULL DEFAULT 0,
    at_bats INTEGER NOT NULL DEFAULT 0,
    hits INTEGER NOT NULL DEFAULT 0,
    doubles INTEGER NOT NULL DEFAULT 0,
    triples INTEGER NOT NULL DEFAULT 0,
    home_runs INTEGER NOT NULL DEFAULT 0,
    runs INTEGER NOT NULL DEFAULT 0,
    rbi INTEGER NOT NULL DEFAULT 0,
    walks INTEGER NOT NULL DEFAULT 0,
    strikeouts INTEGER NOT NULL DEFAULT 0,
    hit_by_pitch INTEGER NOT NULL DEFAULT 0,
    sacrifice_flies INTEGER NOT NULL DEFAULT 0,
    stolen_bases INTEGER NOT NULL DEFAULT 0,
    caught_stealing INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (player_id, season, month)
)
"""

_PITCHING_SCHEMA = """
CREATE TABLE IF NOT EXISTS monthly_pitching_aggregates (
    player_id TEXT NOT NULL,
    season INTEGER NOT NULL,
    month INTEGER NOT NULL,
    games_pitched INTEGER NOT NULL,
    starts INTEGER NOT NULL DEFAULT 0,
    ip_outs INTEGER NOT NULL DEFAULT 0,
    hits INTEGER NOT NULL DEFAULT 0,
    runs INTEGER NOT NULL DEFAULT 0,
    earned_runs INTEGER NOT NULL DEFAULT 0,
    home_runs INTEGER NOT NULL DEFAULT 0,
    walks INTEGER NOT NULL DEFAULT 0,
    strikeouts INTEGER NOT NULL DEFAULT 0,
    hit_by_pitch INTEGER NOT NULL DEFAULT 0,
    batters_faced INTEGER NOT NULL DEFAULT 0,
    wins INTEGER NOT NULL DEFAULT 0,
    losses INTEGER NOT NULL DEFAULT 0,
    saves INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (player_id, season, month)
)
"""

# Indexes — leaderboard queries sort by counting stats descending and may
# filter by season range. Composite indexes on (counting_stat) give fast
# top-N scans; (season) helps the since-year branch.
_BATTING_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_mba_season ON monthly_batting_aggregates(season)",
    "CREATE INDEX IF NOT EXISTS idx_mba_hr ON monthly_batting_aggregates(home_runs DESC)",
    "CREATE INDEX IF NOT EXISTS idx_mba_hits ON monthly_batting_aggregates(hits DESC)",
    "CREATE INDEX IF NOT EXISTS idx_mba_rbi ON monthly_batting_aggregates(rbi DESC)",
]

_PITCHING_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_mpa_season ON monthly_pitching_aggregates(season)",
    "CREATE INDEX IF NOT EXISTS idx_mpa_so ON monthly_pitching_aggregates(strikeouts DESC)",
]


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create tables and indexes if missing. Idempotent."""
    conn.execute(_BATTING_SCHEMA)
    conn.execute(_PITCHING_SCHEMA)
    for idx_sql in _BATTING_INDEXES + _PITCHING_INDEXES:
        conn.execute(idx_sql)
    conn.commit()


def rebuild_batting(conn: sqlite3.Connection, since_season: int | None = None) -> int:
    """Repopulate monthly_batting_aggregates from game_batting_logs.

    If since_season is set, only rebuild rows for that season onward. Otherwise
    full rebuild. Returns number of rows written.
    """
    cur = conn.cursor()
    if since_season is not None:
        cur.execute(
            "DELETE FROM monthly_batting_aggregates WHERE season >= ?",
            (since_season,),
        )
        where = "WHERE season >= ?"
        params: tuple = (since_season,)
    else:
        cur.execute("DELETE FROM monthly_batting_aggregates")
        where = ""
        params = ()

    cur.execute(
        f"""
        INSERT INTO monthly_batting_aggregates (
            player_id, season, month, games,
            plate_appearances, at_bats, hits, doubles, triples, home_runs,
            runs, rbi, walks, strikeouts, hit_by_pitch, sacrifice_flies,
            stolen_bases, caught_stealing
        )
        SELECT
            player_id,
            season,
            CAST(strftime('%m', date) AS INTEGER) AS month,
            COUNT(*) AS games,
            COALESCE(SUM(plate_appearances), 0),
            COALESCE(SUM(at_bats), 0),
            COALESCE(SUM(hits), 0),
            COALESCE(SUM(doubles), 0),
            COALESCE(SUM(triples), 0),
            COALESCE(SUM(home_runs), 0),
            COALESCE(SUM(runs), 0),
            COALESCE(SUM(rbi), 0),
            COALESCE(SUM(walks), 0),
            COALESCE(SUM(strikeouts), 0),
            COALESCE(SUM(hit_by_pitch), 0),
            COALESCE(SUM(sacrifice_flies), 0),
            COALESCE(SUM(stolen_bases), 0),
            COALESCE(SUM(caught_stealing), 0)
        FROM game_batting_logs
        {where}
        GROUP BY player_id, season, month
        """,
        params,
    )
    written = cur.rowcount
    conn.commit()
    logger.info("rebuild_batting wrote %s rows since=%s", written, since_season)
    return written


def rebuild_pitching(conn: sqlite3.Connection, since_season: int | None = None) -> int:
    """Repopulate monthly_pitching_aggregates from game_pitching_logs."""
    cur = conn.cursor()
    if since_season is not None:
        cur.execute(
            "DELETE FROM monthly_pitching_aggregates WHERE season >= ?",
            (since_season,),
        )
        where = "WHERE season >= ?"
        params: tuple = (since_season,)
    else:
        cur.execute("DELETE FROM monthly_pitching_aggregates")
        where = ""
        params = ()

    cur.execute(
        f"""
        INSERT INTO monthly_pitching_aggregates (
            player_id, season, month, games_pitched, starts,
            ip_outs, hits, runs, earned_runs, home_runs, walks, strikeouts,
            hit_by_pitch, batters_faced, wins, losses, saves
        )
        SELECT
            player_id,
            season,
            CAST(strftime('%m', date) AS INTEGER) AS month,
            COUNT(*) AS games_pitched,
            COALESCE(SUM(is_start), 0),
            COALESCE(SUM(ip_outs), 0),
            COALESCE(SUM(hits), 0),
            COALESCE(SUM(runs), 0),
            COALESCE(SUM(earned_runs), 0),
            COALESCE(SUM(home_runs), 0),
            COALESCE(SUM(walks), 0),
            COALESCE(SUM(strikeouts), 0),
            COALESCE(SUM(hit_by_pitch), 0),
            COALESCE(SUM(batters_faced), 0),
            COALESCE(SUM(win), 0),
            COALESCE(SUM(loss), 0),
            COALESCE(SUM(save), 0)
        FROM game_pitching_logs
        {where}
        GROUP BY player_id, season, month
        """,
        params,
    )
    written = cur.rowcount
    conn.commit()
    logger.info("rebuild_pitching wrote %s rows since=%s", written, since_season)
    return written


def rebuild_all(conn: sqlite3.Connection, since_season: int | None = None) -> dict:
    """Rebuild both tables. Returns row counts for diagnostics."""
    ensure_schema(conn)
    return {
        "batting": rebuild_batting(conn, since_season),
        "pitching": rebuild_pitching(conn, since_season),
    }
