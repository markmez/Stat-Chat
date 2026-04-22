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
from datetime import date
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
    # Alias the table as `tgr` because subset_clause references `tgr.*`
    # (matching the JOIN alias in _execute_team_context_leaderboard).
    season_filter = ""
    season_params = ()
    if season is not None:
        season_filter = " AND tgr.season = ?"
        season_params = (season,)
    base_filter = "COALESCE(tgr.gametype, 'regular') = 'regular'" + season_filter
    total = conn.execute(
        f"SELECT COUNT(*) FROM team_game_results tgr WHERE {base_filter}",
        season_params).fetchone()[0] or 1
    matched = conn.execute(
        f"SELECT COUNT(*) FROM team_game_results tgr "
        f"WHERE {base_filter} AND ({subset_clause})",
        season_params + tuple(subset_params)).fetchone()[0] or 0
    return max(matched / total, 0.001)  # tiny floor avoids divide-by-zero downstream


def min_pa_subset(conn: sqlite3.Connection, season: Optional[int],
                  subset_clause: str, subset_params: tuple,
                  since_year: Optional[int] = None) -> int:
    """Minimum PA for a team-context subset query.

    Single-season: scale min_pa(season) by subset fraction.
    All-time: scale CAREER_MIN_AB by subset fraction.
    Since-year (range): per-season qualifier × year span × subset fraction × 0.5
        (the 0.5 participation factor reflects that players don't play every
        game and don't stay on one team for the entire window — a strict
        per-game qualifier produces empty leaderboards otherwise).
    Always floored at 30.
    """
    fraction = subset_fraction(conn, subset_clause, subset_params, season)
    if since_year is not None:
        years = max(1, date.today().year - since_year + 1)
        # Use the most recent year's full-season qualifier as the per-year base
        per_year = min_pa(conn, date.today().year)
        return max(30, int(per_year * years * fraction * 0.5))
    if season is None:
        base = CAREER_MIN_AB
    else:
        base = min_pa(conn, season)
    return max(30, int(base * fraction))


def min_ip_outs_subset(conn: sqlite3.Connection, season: Optional[int],
                       subset_clause: str, subset_params: tuple,
                       since_year: Optional[int] = None) -> int:
    """Minimum ip_outs for a team-context subset query.

    See min_pa_subset for scope handling. Pitchers have a similar 0.5
    participation factor for since-year queries.
    """
    fraction = subset_fraction(conn, subset_clause, subset_params, season)
    if since_year is not None:
        years = max(1, date.today().year - since_year + 1)
        per_year = min_ip_outs(conn, date.today().year)
        return max(30, int(per_year * years * fraction * 0.5))
    if season is None:
        base = CAREER_MIN_OUTS
    else:
        base = min_ip_outs(conn, season)
    return max(30, int(base * fraction))
