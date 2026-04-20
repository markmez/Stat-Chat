"""Build the historic_moments table — one-time precompute of the exact date
each player's cumulative career total crossed an iconic threshold.

Uses a hybrid approach:
- season_batting_stats / season_pitching_stats: accurate aggregate totals,
  covers pre-1920 data, used to compute prior-year cumulative.
- game_batting_logs / game_pitching_logs: per-game granularity, used to
  find the exact game within the crossing year.

Applies a Negro League team filter so career totals reflect the pre-2020
MLB-only convention used in public records (Mays 660 HR, etc.).
"""

import sqlite3
import sys
from datetime import datetime


# Iconic thresholds
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

_PITCHING_GAME_COL = {"wins": "win", "saves": "save", "strikeouts": "strikeouts"}


# Negro League team codes (teams not in MLB historically but recognized
# since MLB's Dec 2020 elevation of Negro Leagues 1920-1948 to major-league
# status). We filter these out when computing career totals so public-record
# numbers match (e.g., Mays 660 HR is MLB-only; our 662 includes 2 Negro).
_NEGRO_LEAGUE_TEAMS = frozenset({
    "BIR", "CAG", "KCM", "HOM", "MEM", "PH5", "NY5", "NY6", "NW1", "NW2",
    "BLG", "BLS", "CVB", "PTC", "IN6", "IN7", "IN9", "JAX", "SSN", "SSA",
    "ATN", "CIA", "SNO", "CV9", "WEG", "HIL", "CI1", "CI2", "BRN", "DT1",
    "DT2", "NSH", "HB1", "LOS", "PBG", "TOL", "BAC", "BCR", "CHS", "CLE2",
    "HAR", "IND", "LOU", "NEW", "NYB", "NYC", "PHS", "PIT2", "STL2", "WIL",
})


# Season-level corrections for known Retrosheet gaps. Each entry:
#   (player_id, season, stat) -> correct_value
# Our per-season counts are ~99% accurate; a few iconic careers have small
# per-season shortfalls that cascade into notable milestone-date errors
# (e.g., Mays 1962 HR shown as 47 in Retrosheet, real is 49; this caused
# Mays's 500-HR crossing to come 6 days late and his 600-HR crossing to
# shift 218 days into 1970). Apply corrections when computing cumulative
# totals. Extend as new discrepancies are discovered.
_SEASON_CORRECTIONS = {
    ("maysw101", 1962, "home_runs"): 49,   # real 49, our 47
    ("maysw101", 1962, "rbi"):       141,  # real 141, verify our count
    # Rickey Henderson 1999 hits: real 138 vs our 136
    ("hendr001", 1999, "hits"):      138,
}


def _corrected(player_id, season, stat, raw_value):
    """Apply a manual correction if one exists for this (player, season, stat)."""
    return _SEASON_CORRECTIONS.get((player_id, season, stat), raw_value)


def _format_num(n):
    return f"{n:,}"


def _label(stat):
    return {
        "home_runs": "home run", "hits": "hit", "rbi": "RBI",
        "stolen_bases": "stolen base", "runs": "run", "walks": "walk",
        "doubles": "double",
    }.get(stat, stat.replace("_", " "))


def _pitching_context(stat, threshold):
    if stat == "strikeouts":
        return f"recorded his {_format_num(threshold)}th career strikeout"
    if stat == "wins":
        return f"earned his {_format_num(threshold)}th career win"
    if stat == "saves":
        return f"notched his {_format_num(threshold)}th career save"
    return f"reached {_format_num(threshold)} career {stat}"


def _batting_context(stat, threshold):
    return f"hit his {_format_num(threshold)}th career {_label(stat)}" if stat == "home_runs" \
        else (f"recorded his {_format_num(threshold)}th career {_label(stat)}" if stat == "hits"
              else f"drove in his {_format_num(threshold)}th career run" if stat == "rbi"
              else f"stole his {_format_num(threshold)}th career base" if stat == "stolen_bases"
              else f"reached {_format_num(threshold)} career {_label(stat)}")


def _get_mlb_players_with_career_total(conn, stat, threshold, is_pitching=False):
    """Return (player_id, player_name, seasons_data) for every player whose
    MLB-only career total for `stat` has crossed `threshold` at some point.

    seasons_data is a list of (season, season_total) tuples, in chronological
    order, with Negro League seasons filtered out.
    """
    season_table = "season_pitching_stats" if is_pitching else "season_batting_stats"
    placeholders = ",".join("?" * len(_NEGRO_LEAGUE_TEAMS))
    rows = conn.execute(f"""
        SELECT s.player_id, COALESCE(p.name, s.player_id) AS name,
               s.season, SUM(s.{stat}) AS season_total
        FROM {season_table} s
        LEFT JOIN players p ON p.player_id = s.player_id
        WHERE s.team NOT IN ({placeholders}) AND s.{stat} IS NOT NULL
        GROUP BY s.player_id, s.season
        ORDER BY s.player_id, s.season
    """, list(_NEGRO_LEAGUE_TEAMS)).fetchall()

    # Group by player, applying season corrections
    by_player = {}
    for pid, name, season, season_total in rows:
        corrected = _corrected(pid, season, stat, season_total or 0)
        by_player.setdefault(pid, {"name": name, "seasons": []})
        by_player[pid]["seasons"].append((season, corrected))

    # Keep only players whose cumulative crosses the threshold
    qualifiers = {}
    for pid, info in by_player.items():
        cumul = 0
        for _, season_total in info["seasons"]:
            cumul += season_total
            if cumul >= threshold:
                qualifiers[pid] = info
                break
    return qualifiers


def _find_crossing_game(conn, player_id, stat, prior_cumul, threshold, crossing_season,
                        is_pitching=False):
    """Within crossing_season, find the exact game where cumulative
    (prior_cumul + in_season_running_total) first reaches threshold.

    Returns (date, season_total_at_crossing). If game logs are incomplete
    for the crossing year (missing rows), returns (None, ...) and caller
    should fall back to mid-season estimate.
    """
    game_table = "game_pitching_logs" if is_pitching else "game_batting_logs"
    stat_col = _PITCHING_GAME_COL.get(stat, stat) if is_pitching else stat
    # Filter out Negro League: join to season_*_stats to check team
    placeholders = ",".join("?" * len(_NEGRO_LEAGUE_TEAMS))
    season_table = "season_pitching_stats" if is_pitching else "season_batting_stats"

    # First: was this player's crossing_season an MLB season? (Not Negro League)
    team_row = conn.execute(f"""
        SELECT team FROM {season_table} WHERE player_id = ? AND season = ?
    """, (player_id, crossing_season)).fetchone()
    if team_row and team_row[0] in _NEGRO_LEAGUE_TEAMS:
        return (None, None)

    games = conn.execute(f"""
        SELECT date, game_number, {stat_col}
        FROM {game_table}
        WHERE player_id = ? AND season = ? AND {stat_col} > 0
        ORDER BY date ASC, game_number ASC
    """, (player_id, crossing_season)).fetchall()

    gap = threshold - prior_cumul  # how many more we need within this season
    running = 0
    for date, game_num, val in games:
        running += val or 0
        if running >= gap:
            return (date, prior_cumul + running)
    return (None, prior_cumul + running)  # ran out of games — incomplete logs


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
            context TEXT,
            is_exact INTEGER DEFAULT 1
        )
    """)
    # Ensure is_exact column exists (for pre-existing schema)
    try:
        conn.execute("ALTER TABLE historic_moments ADD COLUMN is_exact INTEGER DEFAULT 1")
    except Exception:
        pass

    conn.execute("DELETE FROM historic_moments")
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_historic_moments_month_day
        ON historic_moments(substr(date, 6))
    """)

    start = datetime.now()
    all_thresholds = (
        [(stat, t, False) for stat, thresholds in _BATTING_THRESHOLDS.items() for t in thresholds]
        + [(stat, t, True) for stat, thresholds in _PITCHING_THRESHOLDS.items() for t in thresholds]
    )

    total_inserted = 0
    for stat, threshold, is_pitching in all_thresholds:
        qualifiers = _get_mlb_players_with_career_total(conn, stat, threshold, is_pitching)
        print(f"  {'pitching ' if is_pitching else ''}{stat} >= {threshold}: "
              f"{len(qualifiers)} qualifying players")
        for pid, info in qualifiers.items():
            # Walk seasons, find crossing year
            cumul = 0
            crossing_season = None
            prior_cumul = 0
            for season, season_total in info["seasons"]:
                if cumul + season_total >= threshold:
                    crossing_season = season
                    prior_cumul = cumul
                    break
                cumul += season_total
            if crossing_season is None:
                continue

            date, _ = _find_crossing_game(conn, pid, stat, prior_cumul, threshold,
                                          crossing_season, is_pitching)
            is_exact = 1 if date else 0
            if date is None:
                # Game log incomplete for crossing year; fall back to July 1
                date = f"{crossing_season}-07-01"

            context = (_pitching_context(stat, threshold) if is_pitching
                       else _batting_context(stat, threshold))
            conn.execute("""
                INSERT INTO historic_moments
                    (player_id, player_name, stat, threshold, date, season, context, is_exact)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (pid, info["name"], stat, threshold, date, crossing_season, context, is_exact))
            total_inserted += 1

    conn.commit()
    elapsed = (datetime.now() - start).total_seconds()
    count = conn.execute("SELECT COUNT(*) FROM historic_moments").fetchone()[0]
    exact_count = conn.execute("SELECT COUNT(*) FROM historic_moments WHERE is_exact=1").fetchone()[0]
    print(f"\nDone in {elapsed:.1f}s. {count} historic moments stored "
          f"({exact_count} exact, {count - exact_count} approximate).")
    conn.close()
    return {"count": count, "exact": exact_count, "seconds": elapsed}


if __name__ == "__main__":
    db = sys.argv[1] if len(sys.argv) > 1 else "/data/baseball_stats_full.db"
    build(db)
