"""
Compute prominence_score for all players and store in the players table.

Score formula:
  - Career batting games (as-is)
  - Pitching: starts × 5, saves × 3, other appearances × 1
  - Awards: All-Star/GG/SS × 500, MVP/CY/ROY/HOF × 1000, postseason MVP × 300

Run after any data refresh to keep scores current.
"""

import os
import sqlite3
import sys


DB_PATH = os.getenv(
    "DB_PATH",
    os.path.join(os.path.dirname(__file__), "..", "..", "baseball_stats_full.db"),
)


def compute_prominence(db_path: str = DB_PATH):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    # Ensure column exists
    try:
        cur.execute("ALTER TABLE players ADD COLUMN prominence_score INTEGER DEFAULT 0")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # Column already exists

    # Batch compute: batting games
    cur.execute("""
        CREATE TEMP TABLE _bat_score AS
        SELECT p.player_id, COALESCE(SUM(s.games), 0) AS score
        FROM players p
        LEFT JOIN season_batting_stats s ON s.player_id = p.player_id
        GROUP BY p.player_id
    """)

    # Batch compute: pitching weighted score
    cur.execute("""
        CREATE TEMP TABLE _pitch_score AS
        SELECT p.player_id,
               COALESCE(SUM(sp.games_started), 0) * 5
               + COALESCE(SUM(sp.saves), 0) * 3
               + COALESCE(SUM(CASE WHEN sp.games > sp.games_started
                   THEN sp.games - sp.games_started ELSE 0 END), 0) AS score
        FROM players p
        LEFT JOIN season_pitching_stats sp ON sp.player_id = p.player_id
        GROUP BY p.player_id
    """)

    # Batch compute: awards score
    # All-Star, Gold Glove, Silver Slugger = 500 each
    # MVP, Cy Young, ROY, HOF = 1000 each
    # Postseason MVPs = 300 each
    cur.execute("""
        CREATE TEMP TABLE _award_score AS
        SELECT p.player_id,
               COALESCE(SUM(CASE
                   WHEN a.award IN ('MVP', 'CY', 'ROY', 'HOF') THEN 1000
                   WHEN a.award IN ('ALL_STAR', 'GG', 'SS') THEN 500
                   WHEN a.award IN ('WS_MVP', 'ALCS_MVP', 'NLCS_MVP') THEN 300
                   ELSE 0
               END), 0) AS score
        FROM players p
        LEFT JOIN awards a ON a.player_id = p.player_id
        GROUP BY p.player_id
    """)

    # Combine and update
    cur.execute("""
        UPDATE players SET prominence_score = (
            SELECT COALESCE(b.score, 0) + COALESCE(pi.score, 0) + COALESCE(a.score, 0)
            FROM _bat_score b
            LEFT JOIN _pitch_score pi ON pi.player_id = b.player_id
            LEFT JOIN _award_score a ON a.player_id = b.player_id
            WHERE b.player_id = players.player_id
        )
    """)

    conn.commit()

    # Report top players
    cur.execute("""
        SELECT name, prominence_score, last_season
        FROM players ORDER BY prominence_score DESC LIMIT 20
    """)
    print("Top 20 by prominence:")
    for name, score, last in cur.fetchall():
        print(f"  {score:>6}  {name} (last: {last})")

    # Report De La Cruz specifically
    cur.execute("SELECT name, prominence_score FROM players WHERE name LIKE '%De La Cruz%' ORDER BY prominence_score DESC")
    print("\nDe La Cruz players:")
    for name, score in cur.fetchall():
        print(f"  {score:>6}  {name}")

    cur.execute("DROP TABLE _bat_score")
    cur.execute("DROP TABLE _pitch_score")
    cur.execute("DROP TABLE _award_score")
    conn.commit()
    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else DB_PATH
    compute_prominence(path)
