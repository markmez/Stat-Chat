"""MLB qualification rules — single source of truth.

Batting: 3.1 PA per scheduled team game (502 PA / 162 games).
Pitching: 1.0 IP per scheduled team game (162 IP / 162 games).
"""

import sqlite3


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
