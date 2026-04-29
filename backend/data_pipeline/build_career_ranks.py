#!/usr/bin/env python3
"""
Build pre-computed career rank tables.

Replaces the inline `SELECT COUNT(*) + 1 FROM (... HAVING total > ?)`
pattern in player_card._build_achievements with simple indexed lookups.

Two tables:
  - career_ranks(player_id, side, stat, total, mlb_rank)
  - career_franchise_ranks(player_id, side, stat, franchise_code, total, fran_rank)

Both keyed by canonical franchise codes (per services/franchise.FRANCHISE_CANONICAL),
matching team_records.

Run from cron after stats refresh. Idempotent — drops and rebuilds.

Usage:
  python build_career_ranks.py [--db /data/baseball_stats_full.db]
"""

import argparse
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from services.franchise import FRANCHISE_CANONICAL, FRANCHISE_MAP


# Stats and top-N caps must match _BATTING_RANK_STATS / _PITCHING_RANK_STATS in
# routers/player_card.py. The MLB-rank set covers all entries; the franchise-rank
# set is the first 5 (matching the existing `stats[:5]` slice in achievements).
BATTING_MLB_STATS = [
    "home_runs", "hits", "rbi", "stolen_bases", "doubles",
    "runs", "at_bats", "games", "walks",
]
PITCHING_MLB_STATS = ["wins", "strikeouts", "saves", "games"]

BATTING_FRANCHISE_STATS = BATTING_MLB_STATS[:5]
PITCHING_FRANCHISE_STATS = PITCHING_MLB_STATS[:5]


def _assign_dense_ranks(rows):
    """Given rows ordered by total DESC, assign rank using
    'COUNT(*) + 1 WHERE total > ?' semantics (ties share the same rank,
    next rank skips). Yields (player_id, total, rank)."""
    prev_total = None
    current_rank = 0
    for i, (pid, total) in enumerate(rows):
        if total != prev_total:
            current_rank = i + 1
            prev_total = total
        yield pid, total, current_rank


def _build_mlb(conn, side, table, stats):
    cur = conn.cursor()
    inserts = []
    for stat in stats:
        cur.execute(f"""
            SELECT player_id, SUM({stat}) AS total
            FROM {table}
            WHERE {stat} IS NOT NULL
            GROUP BY player_id
            HAVING total > 0
            ORDER BY total DESC
        """)
        rows = cur.fetchall()
        for pid, total, rank in _assign_dense_ranks(rows):
            inserts.append((pid, side, stat, total, rank))
    cur.executemany(
        "INSERT INTO career_ranks_new (player_id, side, stat, total, mlb_rank) VALUES (?, ?, ?, ?, ?)",
        inserts,
    )
    print(f"  career_ranks {side}: {len(inserts)} rows ({len(stats)} stats)")


def _canonical_franchises(conn):
    """Return list of (canonical_code, [all_historical_codes]) for every
    canonical franchise that has any data in season stats."""
    rows = conn.execute(
        "SELECT DISTINCT team FROM season_batting_stats WHERE team IS NOT NULL "
        "UNION SELECT DISTINCT team FROM season_pitching_stats WHERE team IS NOT NULL"
    ).fetchall()
    raw_codes = set()
    for (team_str,) in rows:
        if team_str:
            for t in team_str.split("/"):
                t = t.strip()
                if t:
                    raw_codes.add(t)

    canon_to_codes = {}
    for c in raw_codes:
        canon = FRANCHISE_CANONICAL.get(c, c)
        canon_to_codes.setdefault(canon, set()).update(FRANCHISE_MAP.get(canon, [canon]))
        canon_to_codes[canon].add(c)
    return [(canon, sorted(codes)) for canon, codes in sorted(canon_to_codes.items())]


def _build_franchise(conn, side, table, stats):
    cur = conn.cursor()
    franchises = _canonical_franchises(conn)
    inserts = []
    for canon, codes in franchises:
        ph = ",".join(["?"] * len(codes))
        for stat in stats:
            cur.execute(
                f"""
                SELECT player_id, SUM({stat}) AS total
                FROM {table}
                WHERE team IN ({ph}) AND {stat} IS NOT NULL
                GROUP BY player_id
                HAVING total > 0
                ORDER BY total DESC
                """,
                codes,
            )
            rows = cur.fetchall()
            for pid, total, rank in _assign_dense_ranks(rows):
                inserts.append((pid, side, stat, canon, total, rank))
    cur.executemany(
        "INSERT INTO career_franchise_ranks_new "
        "(player_id, side, stat, franchise_code, total, fran_rank) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        inserts,
    )
    print(f"  career_franchise_ranks {side}: {len(inserts)} rows "
          f"({len(franchises)} franchises × {len(stats)} stats)")


def build(db_path):
    t0 = time.time()
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    # Staging tables — drop any leftovers, recreate
    cur.execute("DROP TABLE IF EXISTS career_ranks_new")
    cur.execute("""
        CREATE TABLE career_ranks_new (
            player_id TEXT NOT NULL,
            side TEXT NOT NULL,
            stat TEXT NOT NULL,
            total REAL NOT NULL,
            mlb_rank INTEGER NOT NULL,
            PRIMARY KEY(player_id, side, stat)
        )
    """)
    cur.execute("DROP TABLE IF EXISTS career_franchise_ranks_new")
    cur.execute("""
        CREATE TABLE career_franchise_ranks_new (
            player_id TEXT NOT NULL,
            side TEXT NOT NULL,
            stat TEXT NOT NULL,
            franchise_code TEXT NOT NULL,
            total REAL NOT NULL,
            fran_rank INTEGER NOT NULL,
            PRIMARY KEY(player_id, side, stat, franchise_code)
        )
    """)

    print("Building career_ranks...")
    _build_mlb(conn, "batting", "season_batting_stats", BATTING_MLB_STATS)
    _build_mlb(conn, "pitching", "season_pitching_stats", PITCHING_MLB_STATS)

    print("Building career_franchise_ranks...")
    _build_franchise(conn, "batting", "season_batting_stats", BATTING_FRANCHISE_STATS)
    _build_franchise(conn, "pitching", "season_pitching_stats", PITCHING_FRANCHISE_STATS)

    # Atomic swap
    cur.execute("DROP TABLE IF EXISTS career_ranks")
    cur.execute("DROP TABLE IF EXISTS career_franchise_ranks")
    cur.execute("ALTER TABLE career_ranks_new RENAME TO career_ranks")
    cur.execute("ALTER TABLE career_franchise_ranks_new RENAME TO career_franchise_ranks")
    cur.execute("CREATE INDEX idx_career_ranks_lookup ON career_ranks(player_id, side)")
    cur.execute("CREATE INDEX idx_career_franchise_ranks_lookup "
                "ON career_franchise_ranks(player_id, side, franchise_code)")
    conn.commit()
    conn.close()

    print(f"Done in {time.time() - t0:.1f}s")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=os.getenv("DB_PATH", "/data/baseball_stats_full.db"))
    args = parser.parse_args()
    build(args.db)
