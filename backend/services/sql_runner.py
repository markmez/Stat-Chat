"""
SQLite execution layer for the StatChat backend.

Runs generated SQL against baseball_stats.db, formats results as text,
and handles streak fallback (T1 all-streaks → T2 sensitive precomputed).

Note: live PELT fallback (numpy/ruptures) is intentionally excluded here —
the precomputed streaks_sensitive and streaks_sliding tables cover it.
"""

import os
import re
import sqlite3

DB_PATH = os.getenv(
    "DB_PATH",
    os.path.join(os.path.dirname(__file__), "..", "..", "baseball_stats.db"),
)

MAX_ROWS = 50

_STREAK_COLS = [
    "id", "player_id", "season", "start_date", "end_date",
    "num_games", "batting_avg", "obp", "slg", "ops",
    "home_runs", "hits", "at_bats", "walks", "strikeouts", "performance",
]

_STREAK_SENSITIVE_COLS = _STREAK_COLS + ["season_ops"]


class SqlRunner:
    """Executes SQL and returns (result_text, is_streak_query)."""

    def execute_and_format(self, sql: str) -> tuple[str, bool]:
        """
        Execute `sql` against the stats DB.
        Returns (formatted_text, is_streak_query).
        Raises RuntimeError on SQL errors (caller converts to user-facing error).
        """
        is_streak = "streaks" in sql.lower()

        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute(sql)
            columns = [desc[0] for desc in cursor.description] if cursor.description else []
            rows = cursor.fetchall()
        except Exception as e:
            raise RuntimeError(str(e)) from e

        # Streak fallback: filtered query returned nothing → try all streaks
        if not rows and is_streak:
            fallback = self._streak_fallback(conn, sql)
            conn.close()
            return (fallback or "No streak data found for that player/season."), True

        conn.close()

        if not rows:
            return "No results found.", False

        return _format_rows(columns, rows[:MAX_ROWS]), is_streak

    # ------------------------------------------------------------------
    # Streak fallback helpers
    # ------------------------------------------------------------------

    def _streak_fallback(self, conn: sqlite3.Connection, original_sql: str) -> str:
        """
        When a filtered streak query returns 0 rows, attempt:
          1. T1 all-streaks (remove performance filter)
          2. T2 sensitive precomputed streaks
        Returns formatted text or empty string if nothing found.
        """
        name_match = re.search(r"LIKE\s+'%([^%]+)%'", original_sql, re.IGNORECASE)
        if not name_match:
            return ""
        player_name = name_match.group(1)

        season_match = re.search(r"season\s*=\s*(\d{4})", original_sql, re.IGNORECASE)
        season = int(season_match.group(1)) if season_match else 2024

        cursor = conn.cursor()

        # T1: all streaks for this player/season, no performance filter
        cursor.execute(
            """
            SELECT s.* FROM streaks s
            JOIN players p ON s.player_id = p.player_id
            WHERE p.name LIKE ? AND s.season = ?
            ORDER BY s.start_date
            """,
            (f"%{player_name}%", season),
        )
        rows = cursor.fetchall()
        if rows:
            return _format_rows(_STREAK_COLS, rows)

        # T2: sensitive precomputed (lower-penalty change-point, 7-30 game segments)
        try:
            cursor.execute(
                """
                SELECT s.* FROM streaks_sensitive s
                JOIN players p ON s.player_id = p.player_id
                WHERE p.name LIKE ? AND s.season = ?
                ORDER BY s.start_date
                """,
                (f"%{player_name}%", season),
            )
            rows = cursor.fetchall()
            if rows:
                return _format_rows(_STREAK_SENSITIVE_COLS, rows)
        except Exception:
            pass

        return ""


def _format_rows(columns: list[str], rows: list) -> str:
    header = " | ".join(columns)
    lines = [header, "-" * len(header)]
    for row in rows:
        lines.append(" | ".join("NULL" if v is None else str(v) for v in row))
    return "\n".join(lines)
