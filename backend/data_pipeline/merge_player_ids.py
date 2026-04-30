#!/usr/bin/env python3
"""
Merge fragmented player_ids into a single canonical id per real player.

For each (alias_id, canonical_id) pair, this script:
  1. Discovers every table whose schema mentions `player_id` and re-points
     all alias rows to the canonical id.
  2. Deletes the alias's row from `players` (the canonical row already
     carries the correct current name + team from the MSF feed).
  3. Records the mapping in `player_id_aliases` so future MSF pulls
     redirect to canonical (the pull script consults this table).

Each pair runs in its own transaction, so a failure on one player
doesn't roll back successful merges for other players.

Pre-flight:
- Refuses to run if `season_batting_stats` / `season_pitching_stats`
  show overlapping seasons between alias and canonical (would violate
  the UNIQUE(player_id, season) constraint).
- Refuses to run if `game_*_logs` show overlapping (season, date) tuples.

Dry-run mode reports the rows-per-table that would change without
modifying anything.

Usage:
  python merge_player_ids.py --pairs '[["alias","canon","reason"], ...]'
  python merge_player_ids.py --pairs ... --dry-run
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime


# Tables to skip even if they have a player_id column. Computed/derived
# tables that get rebuilt by their own builders post-merge.
_REBUILD_AFTER_MERGE = {
    "career_ranks",
    "career_franchise_ranks",
    "mlb_records",
    "team_records",
    "historical_streaks",
    "record_progression",
    "historic_moments",
    "historical_index",
}


def _find_player_id_tables(conn):
    """Enumerate tables with a player_id column, excluding rebuildables."""
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    tables = []
    for (name,) in rows:
        if name == "players" or name == "player_id_aliases":
            continue
        if name in _REBUILD_AFTER_MERGE:
            continue
        cols = conn.execute(f"PRAGMA table_info({name})").fetchall()
        if any(c[1] == "player_id" for c in cols):
            tables.append(name)
    return sorted(tables)


def _resolve_unique_overlaps(conn, alias, canonical, dry_run):
    """For each UNIQUE-key conflict between alias and canonical, classify
    as 'identical-duplicate' (delete alias's row, harmless) or 'mismatch'
    (real data divergence — abort). Returns ([deletions_executed],
    [mismatches]).

    Identical duplicates show up regularly during the MSF/Retrosheet
    cutover: the same April 19 game line gets written under both ids
    when the matcher flips mid-pull. Stats match byte-for-byte, the
    alias row is pure redundancy.
    """
    deletions = []
    mismatches = []

    def _compare_and_resolve(table, key_cols):
        # Fetch all alias rows for this table.
        # PRAGMA table_info row shape: (cid, name, type, notnull, dflt, pk).
        # Surrogate INTEGER PRIMARY KEY columns (e.g. id AUTOINCREMENT) must
        # be excluded from comparison — they're per-row unique by design and
        # would falsely flag every duplicate as a mismatch.
        col_info = conn.execute(f"PRAGMA table_info({table})").fetchall()
        cols = [r[1] for r in col_info]
        surrogate_pks = {
            r[1] for r in col_info
            if r[5] and r[2].upper() == "INTEGER"  # pk=1, type=INTEGER
        }
        skip = set(key_cols) | {"player_id"} | surrogate_pks
        non_key_cols = [c for c in cols if c not in skip]
        col_list = ", ".join(non_key_cols) if non_key_cols else "1"

        # Build a JOIN that surfaces (alias_row_values, canonical_row_values)
        # for each conflicting unique key.
        key_match = " AND ".join(
            f"COALESCE(a.{k},0) = COALESCE(c.{k},0)" if k == "game_number"
            else f"a.{k} = c.{k}"
            for k in key_cols if k != "player_id"
        )
        sql = f"""
            SELECT {", ".join(f"a.{k}" for k in key_cols if k != "player_id")},
                   {", ".join(f"a.{c}" for c in non_key_cols) or "1"},
                   {", ".join(f"c.{c}" for c in non_key_cols) or "1"}
            FROM {table} a
            JOIN {table} c ON c.player_id = ? AND {key_match}
            WHERE a.player_id = ?
        """
        rows = conn.execute(sql, (canonical, alias)).fetchall()
        kc = [k for k in key_cols if k != "player_id"]
        for row in rows:
            key_vals = row[:len(kc)]
            n = len(non_key_cols) or 1
            a_vals = row[len(kc):len(kc) + n]
            c_vals = row[len(kc) + n:len(kc) + 2 * n]
            if a_vals == c_vals:
                # Identical duplicate — drop the alias's redundant row.
                if not dry_run:
                    where = " AND ".join(
                        f"{k} IS ?" if v is None else f"{k} = ?"
                        for k, v in zip(kc, key_vals)
                    )
                    params = [v for v in key_vals if v is not None]
                    null_idx = [i for i, v in enumerate(key_vals) if v is None]
                    # SQLite: `col IS NULL` instead of `col = NULL`
                    delete_where = []
                    delete_params = [alias]
                    for k, v in zip(kc, key_vals):
                        if v is None:
                            delete_where.append(f"{k} IS NULL")
                        else:
                            delete_where.append(f"{k} = ?")
                            delete_params.append(v)
                    conn.execute(
                        f"DELETE FROM {table} WHERE player_id = ? AND " +
                        " AND ".join(delete_where),
                        delete_params,
                    )
                deletions.append({
                    "table": table,
                    "key": dict(zip(kc, key_vals)),
                    "stats_match": True,
                })
            else:
                mismatches.append({
                    "table": table,
                    "key": dict(zip(kc, key_vals)),
                    "alias_values": a_vals,
                    "canonical_values": c_vals,
                })

    # All tables with UNIQUE constraints involving player_id need an
    # overlap-resolution pass. Discovered via dry-run failures; if more
    # surface, add them here.
    _compare_and_resolve("season_batting_stats", ["player_id", "season"])
    _compare_and_resolve("season_pitching_stats", ["player_id", "season"])
    _compare_and_resolve(
        "game_batting_logs", ["player_id", "season", "date", "game_number"]
    )
    _compare_and_resolve(
        "game_pitching_logs", ["player_id", "season", "date", "game_number"]
    )
    _compare_and_resolve("current_form", ["player_id", "season"])
    _compare_and_resolve("pitching_current_form", ["player_id", "season"])

    return deletions, mismatches


def _merge_one(conn, alias, canonical, reason, dry_run, tables):
    """Merge a single pair. Caller wraps in transaction."""
    summary = {"alias": alias, "canonical": canonical, "tables": {}}

    # Verify both ids exist (canonical must exist; alias may already be merged)
    canon_exists = conn.execute(
        "SELECT 1 FROM players WHERE player_id = ?", (canonical,)
    ).fetchone()
    if not canon_exists:
        summary["error"] = f"canonical {canonical!r} not found in players"
        return summary

    alias_exists = conn.execute(
        "SELECT 1 FROM players WHERE player_id = ?", (alias,)
    ).fetchone()
    if not alias_exists:
        summary["error"] = f"alias {alias!r} not found in players (already merged?)"
        return summary

    # Resolve UNIQUE-key overlaps. Identical duplicates get dropped from
    # the alias side; mismatches abort the pair.
    deletions, mismatches = _resolve_unique_overlaps(conn, alias, canonical, dry_run)
    if mismatches:
        summary["error"] = "data mismatch on overlapping unique keys"
        summary["mismatches"] = mismatches
        return summary
    if deletions:
        summary["resolved_duplicates"] = len(deletions)

    # Per-table UPDATE pass
    for tbl in tables:
        before = conn.execute(
            f"SELECT COUNT(*) FROM {tbl} WHERE player_id = ?", (alias,)
        ).fetchone()[0]
        if before == 0:
            summary["tables"][tbl] = 0
            continue
        if not dry_run:
            conn.execute(
                f"UPDATE {tbl} SET player_id = ? WHERE player_id = ?",
                (canonical, alias),
            )
        summary["tables"][tbl] = before

    # Players table — drop the alias row. Canonical keeps its existing
    # row (which carries the current MSF name like "Bobby Witt Jr.").
    if not dry_run:
        conn.execute("DELETE FROM players WHERE player_id = ?", (alias,))
        # Record the mapping for future-proofing the pull pipeline.
        conn.execute(
            """INSERT OR REPLACE INTO player_id_aliases
               (alias_id, canonical_id, reason, created_at) VALUES (?, ?, ?, ?)""",
            (alias, canonical, reason, datetime.utcnow().isoformat()),
        )

    summary["status"] = "ok"
    return summary


def merge(db_path, pairs, dry_run=False):
    """Run the merge. `pairs` is a list of [alias, canonical, reason]
    triples. Returns a list of per-pair summaries."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = OFF")

    tables = _find_player_id_tables(conn)
    print(f"Found {len(tables)} player_id tables to update per pair")
    print(f"Skipping (will be rebuilt): {sorted(_REBUILD_AFTER_MERGE)}")

    results = []
    for entry in pairs:
        if len(entry) == 2:
            alias, canonical = entry
            reason = "merged via merge_player_ids"
        else:
            alias, canonical, reason = entry[0], entry[1], entry[2]
        try:
            conn.execute("BEGIN")
            r = _merge_one(conn, alias, canonical, reason, dry_run, tables)
            if r.get("error") or dry_run:
                conn.rollback()
            else:
                conn.commit()
            results.append(r)
            print(f"  {alias} -> {canonical}: {r.get('status', 'ERROR')}")
            if r.get("error"):
                print(f"    {r['error']}")
            if r.get("conflicts"):
                print(f"    conflicts: {r['conflicts']}")
        except Exception as e:
            conn.rollback()
            results.append({"alias": alias, "canonical": canonical, "error": str(e)})
            print(f"  {alias} -> {canonical}: EXCEPTION {e}")

    conn.close()
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=os.getenv("DB_PATH", "/data/baseball_stats_full.db"))
    parser.add_argument("--pairs", required=True,
                        help='JSON array of [alias, canonical, reason] triples')
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    pairs = json.loads(args.pairs)
    results = merge(args.db, pairs, dry_run=args.dry_run)
    print(json.dumps(results, indent=2))
