"""MLB qualification rules — single source of truth.

Single-season:
  Batting: 3.1 PA per scheduled team game (502 PA / 162 games).
  Pitching: 1.0 IP per scheduled team game (162 IP / 162 games).

Career / all-time (matches the existing _career_rate_formulas in
query_engine.py):
  Batting: 5000 AB.
  Pitching: 3000 outs (1000 IP).
"""

import sqlite3
from typing import Optional, Tuple

# Career rate-stat thresholds — must stay in sync with _career_rate_formulas
# in query_engine.py (used for non-subset career leaderboards).
CAREER_MIN_AB = 5000
CAREER_MIN_OUTS = 3000


def _team_games(conn: sqlite3.Connection, season: int) -> int:
    """Best estimate of how many games teams have played this season."""
    row = conn.execute("""
        SELECT MAX(g) FROM (
            SELECT COUNT(DISTINCT date) as g FROM game_batting_logs
            WHERE season = ? GROUP BY player_id
        )
    """, (season,)).fetchone()
    return int(row[0]) if row and row[0] else 1


def min_pa(conn: sqlite3.Connection, season: int) -> int:
    """Minimum plate appearances for batting rate stat qualification."""
    games = _team_games(conn, season)
    return max(30, int(3.1 * games))


def min_ip_outs(conn: sqlite3.Connection, season: int) -> int:
    """Minimum ip_outs for pitching rate stat qualification."""
    games = _team_games(conn, season)
    return max(30, games * 3)  # 1 IP = 3 outs per team game


def subset_fraction(conn: sqlite3.Connection, subset_clause: str,
                    subset_params: tuple, season: Optional[int] = None) -> float:
    """Fraction of team_game_results rows that match a team-context filter.

    Used to scale qualification thresholds for subset queries (e.g., day
    games are ~37% of MLB games, so the day-game qualifier is 37% of the
    full-season / career rule). Returns a value in (0, 1].
    """
    season_filter = ""
    season_params = ()
    if season is not None:
        season_filter = " AND season = ?"
        season_params = (season,)
    base_filter = "COALESCE(gametype, 'regular') = 'regular'" + season_filter
    total = conn.execute(
        f"SELECT COUNT(*) FROM team_game_results WHERE {base_filter}",
        season_params).fetchone()[0] or 1
    matched = conn.execute(
        f"SELECT COUNT(*) FROM team_game_results "
        f"WHERE {base_filter} AND ({subset_clause})",
        season_params + tuple(subset_params)).fetchone()[0] or 0
    return max(matched / total, 0.001)  # tiny floor avoids divide-by-zero downstream


def min_pa_subset(conn: sqlite3.Connection, season: Optional[int],
                  subset_clause: str, subset_params: tuple) -> int:
    """Minimum PA for a team-context subset query.

    Single-season: scale min_pa(season) by subset fraction.
    All-time: scale CAREER_MIN_AB by subset fraction.
    Always floored at 30.
    """
    fraction = subset_fraction(conn, subset_clause, subset_params, season)
    if season is None:
        base = CAREER_MIN_AB
    else:
        base = min_pa(conn, season)
    return max(30, int(base * fraction))


def min_ip_outs_subset(conn: sqlite3.Connection, season: Optional[int],
                       subset_clause: str, subset_params: tuple) -> int:
    """Minimum ip_outs for a team-context subset query.

    Single-season: scale min_ip_outs(season) by subset fraction.
    All-time: scale CAREER_MIN_OUTS by subset fraction.
    Always floored at 30.
    """
    fraction = subset_fraction(conn, subset_clause, subset_params, season)
    if season is None:
        base = CAREER_MIN_OUTS
    else:
        base = min_ip_outs(conn, season)
    return max(30, int(base * fraction))
