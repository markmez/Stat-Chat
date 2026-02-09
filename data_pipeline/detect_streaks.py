"""
Streak detection: Uses change-point detection (ruptures PELT) to find
hot and cold streaks in each player's season.

Reads game logs from SQLite, detects performance shifts, and stores
streak segments back into the database.

Usage:
    python3 detect_streaks.py
"""

import sqlite3
import os
import numpy as np
import ruptures as rpt

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "baseball_stats.db")

# PELT parameters
MIN_SEGMENT_SIZE = 7   # Minimum games in a streak segment
PENALTY = 3            # Higher = fewer change points (less sensitive)
ROLLING_WINDOW = 5     # Rolling average window to smooth noise


def create_streaks_table(conn):
    """Create the streaks table."""
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS streaks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id TEXT NOT NULL,
            season INTEGER NOT NULL,
            start_date TEXT NOT NULL,
            end_date TEXT NOT NULL,
            num_games INTEGER NOT NULL,
            batting_avg REAL,
            obp REAL,
            slg REAL,
            ops REAL,
            home_runs INTEGER,
            hits INTEGER,
            at_bats INTEGER,
            walks INTEGER,
            strikeouts INTEGER,
            performance TEXT,
            FOREIGN KEY (player_id) REFERENCES players(player_id)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_streaks_player ON streaks(player_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_streaks_player_season ON streaks(player_id, season)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_streaks_performance ON streaks(performance)")
    conn.commit()


def get_player_seasons(conn):
    """Get all player-season combos that have game logs."""
    cursor = conn.cursor()
    cursor.execute("""
        SELECT DISTINCT player_id, season
        FROM game_batting_logs
        ORDER BY season, player_id
    """)
    return cursor.fetchall()


def get_game_logs(conn, player_id, season):
    """Get game logs for a player-season, ordered by date."""
    cursor = conn.cursor()
    cursor.execute("""
        SELECT date, at_bats, hits, doubles, triples, home_runs,
               walks, strikeouts, plate_appearances
        FROM game_batting_logs
        WHERE player_id = ? AND season = ?
        ORDER BY date ASC
    """, (player_id, season))
    return cursor.fetchall()


def compute_game_ops(games):
    """Compute per-game OPS values from game log rows."""
    ops_values = []
    for g in games:
        date, ab, h, doubles, triples, hr, bb, so, pa = g
        if ab and ab > 0 and pa and pa > 0:
            # SLG = total bases / AB
            tb = (h - doubles - triples - hr) + 2 * doubles + 3 * triples + 4 * hr
            slg = tb / ab
            # OBP = (H + BB) / PA  (simplified — no HBP/SF in game logs)
            obp = (h + bb) / pa
            ops_values.append(obp + slg)
        else:
            ops_values.append(0.0)
    return np.array(ops_values)


def detect_change_points(signal, min_size=MIN_SEGMENT_SIZE, penalty=PENALTY):
    """Run PELT change-point detection on a signal."""
    if len(signal) < min_size * 2:
        # Not enough data for meaningful detection
        return [len(signal)]

    # Smooth with rolling average to reduce game-to-game noise
    smoothed = np.convolve(signal, np.ones(ROLLING_WINDOW) / ROLLING_WINDOW, mode='same')
    smoothed = smoothed.reshape(-1, 1)

    algo = rpt.Pelt(model="l2", min_size=min_size, jump=1)
    algo.fit(smoothed)
    breakpoints = algo.predict(pen=penalty)
    return breakpoints


def compute_segment_stats(games, start_idx, end_idx):
    """Compute aggregate stats for a segment of games."""
    segment = games[start_idx:end_idx]
    total_ab = sum(g[1] or 0 for g in segment)
    total_h = sum(g[2] or 0 for g in segment)
    total_2b = sum(g[3] or 0 for g in segment)
    total_3b = sum(g[4] or 0 for g in segment)
    total_hr = sum(g[5] or 0 for g in segment)
    total_bb = sum(g[6] or 0 for g in segment)
    total_so = sum(g[7] or 0 for g in segment)
    total_pa = sum(g[8] or 0 for g in segment)

    avg = total_h / total_ab if total_ab > 0 else 0
    obp = (total_h + total_bb) / total_pa if total_pa > 0 else 0
    tb = (total_h - total_2b - total_3b - total_hr) + 2 * total_2b + 3 * total_3b + 4 * total_hr
    slg = tb / total_ab if total_ab > 0 else 0
    ops = obp + slg

    return {
        "start_date": segment[0][0],
        "end_date": segment[-1][0],
        "num_games": len(segment),
        "batting_avg": round(avg, 3),
        "obp": round(obp, 3),
        "slg": round(slg, 3),
        "ops": round(ops, 3),
        "home_runs": total_hr,
        "hits": total_h,
        "at_bats": total_ab,
        "walks": total_bb,
        "strikeouts": total_so,
    }


def label_performance(segment_ops, season_ops):
    """Label a segment as hot, cold, or average relative to the season."""
    if season_ops == 0:
        return "average"
    ratio = segment_ops / season_ops
    if ratio >= 1.20:
        return "hot"
    elif ratio <= 0.80:
        return "cold"
    else:
        return "average"


def detect_all_streaks(conn):
    """Run streak detection for all player-seasons and store results."""
    create_streaks_table(conn)
    cursor = conn.cursor()

    # Clear existing streaks
    cursor.execute("DELETE FROM streaks")
    conn.commit()

    player_seasons = get_player_seasons(conn)
    print(f"Running streak detection for {len(player_seasons)} player-seasons...")

    total_streaks = 0
    for i, (player_id, season) in enumerate(player_seasons):
        games = get_game_logs(conn, player_id, season)
        if len(games) < MIN_SEGMENT_SIZE * 2:
            continue

        # Compute per-game OPS
        ops_signal = compute_game_ops(games)
        season_ops = np.mean(ops_signal)

        # Detect change points
        breakpoints = detect_change_points(ops_signal)

        # Build segments
        start_idx = 0
        for end_idx in breakpoints:
            if end_idx > len(games):
                end_idx = len(games)

            stats = compute_segment_stats(games, start_idx, end_idx)
            performance = label_performance(stats["ops"], season_ops)

            cursor.execute("""
                INSERT INTO streaks (
                    player_id, season, start_date, end_date, num_games,
                    batting_avg, obp, slg, ops, home_runs,
                    hits, at_bats, walks, strikeouts, performance
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                player_id, season, stats["start_date"], stats["end_date"],
                stats["num_games"], stats["batting_avg"], stats["obp"],
                stats["slg"], stats["ops"], stats["home_runs"],
                stats["hits"], stats["at_bats"], stats["walks"],
                stats["strikeouts"], performance,
            ))
            total_streaks += 1
            start_idx = end_idx

        if (i + 1) % 100 == 0:
            conn.commit()
            print(f"  Processed {i + 1}/{len(player_seasons)} player-seasons ({total_streaks} streaks)...")

    conn.commit()
    print(f"Done! Detected {total_streaks} streak segments.")


# --- Tier 2: Sensitive streaks (precomputed fallback) ---

SENSITIVE_PENALTY = 1.5
SENSITIVE_MAX_SEGMENT = 30


def create_streaks_sensitive_table(conn):
    """Create the streaks_sensitive table for Tier 2 precomputed fallback."""
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS streaks_sensitive (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id TEXT NOT NULL,
            season INTEGER NOT NULL,
            start_date TEXT NOT NULL,
            end_date TEXT NOT NULL,
            num_games INTEGER NOT NULL,
            batting_avg REAL,
            obp REAL,
            slg REAL,
            ops REAL,
            home_runs INTEGER,
            hits INTEGER,
            at_bats INTEGER,
            walks INTEGER,
            strikeouts INTEGER,
            performance TEXT,
            season_ops REAL,
            FOREIGN KEY (player_id) REFERENCES players(player_id)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_streaks_sens_player ON streaks_sensitive(player_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_streaks_sens_player_season ON streaks_sensitive(player_id, season)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_streaks_sens_performance ON streaks_sensitive(performance)")
    conn.commit()


def detect_sensitive_streaks(conn):
    """Second pass: run PELT with lower penalty (1.5) and keep only 7-30 game segments.

    Only processes player-seasons that had a single segment (no change points)
    in the primary (penalty=3) detection pass.
    """
    create_streaks_sensitive_table(conn)
    cursor = conn.cursor()

    # Clear existing sensitive streaks
    cursor.execute("DELETE FROM streaks_sensitive")
    conn.commit()

    # Find player-seasons with exactly 1 streak segment (no change points detected)
    cursor.execute("""
        SELECT player_id, season, COUNT(*) as seg_count
        FROM streaks
        GROUP BY player_id, season
        HAVING seg_count = 1
    """)
    single_segment_players = cursor.fetchall()
    print(f"Running sensitive streak detection for {len(single_segment_players)} single-segment player-seasons...")

    total_sensitive = 0
    for i, (player_id, season, _) in enumerate(single_segment_players):
        games = get_game_logs(conn, player_id, season)
        if len(games) < MIN_SEGMENT_SIZE * 2:
            continue

        ops_signal = compute_game_ops(games)
        season_ops = float(np.mean(ops_signal))

        # Run PELT with lower penalty
        breakpoints = detect_change_points(ops_signal, min_size=MIN_SEGMENT_SIZE, penalty=SENSITIVE_PENALTY)

        # Build segments, only keep 7-30 game segments
        start_idx = 0
        for end_idx in breakpoints:
            if end_idx > len(games):
                end_idx = len(games)

            num_games = end_idx - start_idx
            if MIN_SEGMENT_SIZE <= num_games <= SENSITIVE_MAX_SEGMENT:
                stats = compute_segment_stats(games, start_idx, end_idx)
                performance = label_performance(stats["ops"], season_ops)

                cursor.execute("""
                    INSERT INTO streaks_sensitive (
                        player_id, season, start_date, end_date, num_games,
                        batting_avg, obp, slg, ops, home_runs,
                        hits, at_bats, walks, strikeouts, performance, season_ops
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    player_id, season, stats["start_date"], stats["end_date"],
                    stats["num_games"], stats["batting_avg"], stats["obp"],
                    stats["slg"], stats["ops"], stats["home_runs"],
                    stats["hits"], stats["at_bats"], stats["walks"],
                    stats["strikeouts"], performance, round(season_ops, 3),
                ))
                total_sensitive += 1

            start_idx = end_idx

        if (i + 1) % 100 == 0:
            conn.commit()
            print(f"  Processed {i + 1}/{len(single_segment_players)} player-seasons ({total_sensitive} sensitive streaks)...")

    conn.commit()
    print(f"Done! Detected {total_sensitive} sensitive streak segments.")


if __name__ == "__main__":
    conn = sqlite3.connect(DB_PATH)
    detect_all_streaks(conn)
    detect_sensitive_streaks(conn)
    conn.close()
