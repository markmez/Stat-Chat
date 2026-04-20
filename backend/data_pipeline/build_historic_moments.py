"""Build the historic_moments table — one-time precompute of the exact date
each player's cumulative career total crossed an iconic threshold.

For each (player, stat, threshold) we record the earliest game date where
the running cumulative total for that player reached the threshold. Feeds
the On This Date feature in the notable-events feed.

Run via: POST /admin/build-historic-moments
"""

import sqlite3
import sys
from datetime import datetime


# Iconic thresholds — only those historically notable enough to surface yearly.
# Lower values happen too often (1000 hits most seasons) to be interesting.
_BATTING_THRESHOLDS = {
    "home_runs": [500, 600, 700],
    "hits":      [3000],
    "rbi":       [2000, 2500, 3000],
    "stolen_bases": [500, 600],
}
_PITCHING_THRESHOLDS = {
    "strikeouts": [3000, 3500, 4000],
    "wins":       [300, 350],
    "saves":      [400, 500],
}

# Game-log column names (differ from career-total names for some pitching stats)
_PITCHING_GAME_COL = {
    "wins": "win",
    "saves": "save",
    "strikeouts": "strikeouts",
}


def _format_num(n):
    return f"{n:,}"


def _compute_batting_crossings(conn, stat, thresholds):
    """For a batting stat, find the date each player crossed each threshold.
    Uses a window function (SUM ... ROWS UNBOUNDED PRECEDING) to compute
    the running cumulative total per player, then finds the first row where
    cumul >= threshold per (player, threshold).
    """
    print(f"  Computing {stat} crossings for thresholds {thresholds}…")
    rows_inserted = 0
    for threshold in thresholds:
        rows = conn.execute(f"""
            INSERT INTO historic_moments
                (player_id, player_name, stat, threshold, date, season, context)
            SELECT
                x.player_id,
                COALESCE(p.name, x.player_id),
                ?,
                ?,
                MIN(x.date),
                CAST(substr(MIN(x.date), 1, 4) AS INTEGER),
                ?
            FROM (
                SELECT
                    player_id, date, {stat},
                    SUM({stat}) OVER (
                        PARTITION BY player_id ORDER BY date, rowid
                        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                    ) AS cumul
                FROM game_batting_logs
                WHERE {stat} > 0
            ) x
            LEFT JOIN players p ON p.player_id = x.player_id
            WHERE x.cumul >= ?
            GROUP BY x.player_id
        """, (stat, threshold,
              f"hit his {_format_num(threshold)}th career {_label(stat)}",
              threshold)).rowcount
        rows_inserted += rows
        print(f"    {stat} >= {threshold}: {rows} crossings")
    return rows_inserted


def _compute_pitching_crossings(conn, stat, thresholds):
    """Same as batting but against game_pitching_logs."""
    game_col = _PITCHING_GAME_COL.get(stat, stat)
    print(f"  Computing pitching {stat} crossings for thresholds {thresholds}…")
    rows_inserted = 0
    for threshold in thresholds:
        action = _pitching_action(stat, threshold)
        rows = conn.execute(f"""
            INSERT INTO historic_moments
                (player_id, player_name, stat, threshold, date, season, context)
            SELECT
                x.player_id,
                COALESCE(p.name, x.player_id),
                ?,
                ?,
                MIN(x.date),
                CAST(substr(MIN(x.date), 1, 4) AS INTEGER),
                ?
            FROM (
                SELECT
                    player_id, date, {game_col},
                    SUM({game_col}) OVER (
                        PARTITION BY player_id ORDER BY date, rowid
                        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                    ) AS cumul
                FROM game_pitching_logs
                WHERE {game_col} > 0
            ) x
            LEFT JOIN players p ON p.player_id = x.player_id
            WHERE x.cumul >= ?
            GROUP BY x.player_id
        """, (stat, threshold, action, threshold)).rowcount
        rows_inserted += rows
        print(f"    {stat} >= {threshold}: {rows} crossings")
    return rows_inserted


def _label(stat):
    """Map stat key to readable noun."""
    return {
        "home_runs": "home run",
        "hits": "hit",
        "rbi": "RBI",
        "stolen_bases": "stolen base",
        "runs": "run",
        "walks": "walk",
        "doubles": "double",
    }.get(stat, stat.replace("_", " "))


def _pitching_action(stat, threshold):
    """Return a context phrase for pitcher milestones."""
    if stat == "strikeouts":
        return f"recorded his {_format_num(threshold)}th career strikeout"
    if stat == "wins":
        return f"earned his {_format_num(threshold)}th career win"
    if stat == "saves":
        return f"notched his {_format_num(threshold)}th career save"
    return f"reached {_format_num(threshold)} career {stat.replace('_', ' ')}"


def build(db_path):
    """Compute and store all iconic career-milestone crossings with dates."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode = WAL")

    print("Creating historic_moments table…")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS historic_moments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id TEXT NOT NULL,
            player_name TEXT NOT NULL,
            stat TEXT NOT NULL,
            threshold INTEGER NOT NULL,
            date TEXT NOT NULL,
            season INTEGER NOT NULL,
            context TEXT
        )
    """)
    conn.execute("DELETE FROM historic_moments")
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_historic_moments_month_day
        ON historic_moments(substr(date, 6))
    """)

    start = datetime.now()
    total = 0
    for stat, thresholds in _BATTING_THRESHOLDS.items():
        total += _compute_batting_crossings(conn, stat, thresholds)
    for stat, thresholds in _PITCHING_THRESHOLDS.items():
        total += _compute_pitching_crossings(conn, stat, thresholds)

    conn.commit()
    elapsed = (datetime.now() - start).total_seconds()
    count = conn.execute("SELECT COUNT(*) FROM historic_moments").fetchone()[0]
    print(f"\nDone in {elapsed:.1f}s. {count} historic moments stored.")
    conn.close()
    return {"count": count, "seconds": elapsed}


if __name__ == "__main__":
    db = sys.argv[1] if len(sys.argv) > 1 else "/data/baseball_stats_full.db"
    build(db)
