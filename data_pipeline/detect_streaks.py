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


def get_game_logs_extended(conn, player_id, season):
    """Get game logs with runs and rbi for current form detection."""
    cursor = conn.cursor()
    cursor.execute("""
        SELECT date, at_bats, hits, doubles, triples, home_runs,
               walks, strikeouts, plate_appearances, runs, rbi
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


def compute_segment_stats_extended(games, start_idx, end_idx):
    """Compute aggregate stats for a segment, including runs, rbi, PA.

    Uses extended game logs (with runs and rbi columns at indices 9, 10).
    """
    segment = games[start_idx:end_idx]
    total_ab = sum(g[1] or 0 for g in segment)
    total_h = sum(g[2] or 0 for g in segment)
    total_2b = sum(g[3] or 0 for g in segment)
    total_3b = sum(g[4] or 0 for g in segment)
    total_hr = sum(g[5] or 0 for g in segment)
    total_bb = sum(g[6] or 0 for g in segment)
    total_so = sum(g[7] or 0 for g in segment)
    total_pa = sum(g[8] or 0 for g in segment)
    total_runs = sum(g[9] or 0 for g in segment)
    total_rbi = sum(g[10] or 0 for g in segment)

    avg = total_h / total_ab if total_ab > 0 else 0
    obp = (total_h + total_bb) / total_pa if total_pa > 0 else 0
    tb = (total_h - total_2b - total_3b - total_hr) + 2 * total_2b + 3 * total_3b + 4 * total_hr
    slg = tb / total_ab if total_ab > 0 else 0
    ops = obp + slg
    iso = slg - avg

    return {
        "start_date": segment[0][0],
        "end_date": segment[-1][0],
        "num_games": len(segment),
        "at_bats": total_ab,
        "hits": total_h,
        "doubles": total_2b,
        "triples": total_3b,
        "home_runs": total_hr,
        "runs": total_runs,
        "rbi": total_rbi,
        "walks": total_bb,
        "strikeouts": total_so,
        "plate_appearances": total_pa,
        "batting_avg": round(avg, 3),
        "obp": round(obp, 3),
        "slg": round(slg, 3),
        "ops": round(ops, 3),
        "iso": round(iso, 3),
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


# --- Tier 3: Sliding window best/worst stretches ---

SLIDING_WINDOW_SIZES = [10, 12, 15, 7]  # Try these window sizes, prefer longer
SLIDING_MIN_DEVIATION = 0.15  # Segment OPS must deviate >= 15% from season average


def create_streaks_sliding_table(conn):
    """Create the streaks_sliding table for Tier 3 sliding window fallback."""
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS streaks_sliding (
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
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_streaks_slid_player ON streaks_sliding(player_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_streaks_slid_player_season ON streaks_sliding(player_id, season)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_streaks_slid_performance ON streaks_sliding(performance)")
    conn.commit()


def find_best_worst_windows(games, season_ops):
    """Slide windows across the season and find the hottest and coldest stretches."""
    best = None  # (ops, start_idx, end_idx, window_size)
    worst = None

    for window_size in SLIDING_WINDOW_SIZES:
        if len(games) < window_size:
            continue

        for start in range(len(games) - window_size + 1):
            end = start + window_size
            stats = compute_segment_stats(games, start, end)
            seg_ops = stats["ops"]

            if best is None or seg_ops > best[0]:
                best = (seg_ops, start, end, window_size)
            if worst is None or seg_ops < worst[0]:
                worst = (seg_ops, start, end, window_size)

    return best, worst


def detect_sliding_streaks(conn):
    """Third pass: sliding window for player-seasons with no useful data in Tier 1 or 2.

    Finds the single hottest and coldest N-game stretches by brute-force scanning.
    """
    create_streaks_sliding_table(conn)
    cursor = conn.cursor()

    # Clear existing
    cursor.execute("DELETE FROM streaks_sliding")
    conn.commit()

    # Find player-seasons with single segment in Tier 1 AND no useful Tier 2 data
    cursor.execute("""
        SELECT t1.player_id, t1.season FROM (
            SELECT player_id, season FROM streaks
            GROUP BY player_id, season HAVING COUNT(*) = 1
        ) t1
        WHERE NOT EXISTS (
            SELECT 1 FROM streaks_sensitive ss
            WHERE ss.player_id = t1.player_id AND ss.season = t1.season
                AND ss.performance <> 'average'
        )
    """)
    no_streak_players = cursor.fetchall()
    print(f"Running sliding window streak detection for {len(no_streak_players)} player-seasons...")

    total_sliding = 0
    for i, (player_id, season) in enumerate(no_streak_players):
        games = get_game_logs(conn, player_id, season)
        if len(games) < MIN_SEGMENT_SIZE * 2:
            continue

        ops_signal = compute_game_ops(games)
        season_ops = float(np.mean(ops_signal))
        if season_ops == 0:
            continue

        best, worst = find_best_worst_windows(games, season_ops)

        for streak_data, label in [(best, "hot"), (worst, "cold")]:
            if streak_data is None:
                continue
            seg_ops, start_idx, end_idx, _ = streak_data
            deviation = abs(seg_ops - season_ops) / season_ops
            if deviation < SLIDING_MIN_DEVIATION:
                continue

            stats = compute_segment_stats(games, start_idx, end_idx)

            cursor.execute("""
                INSERT INTO streaks_sliding (
                    player_id, season, start_date, end_date, num_games,
                    batting_avg, obp, slg, ops, home_runs,
                    hits, at_bats, walks, strikeouts, performance, season_ops
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                player_id, season, stats["start_date"], stats["end_date"],
                stats["num_games"], stats["batting_avg"], stats["obp"],
                stats["slg"], stats["ops"], stats["home_runs"],
                stats["hits"], stats["at_bats"], stats["walks"],
                stats["strikeouts"], label, round(season_ops, 3),
            ))
            total_sliding += 1

        if (i + 1) % 100 == 0:
            conn.commit()
            print(f"  Processed {i + 1}/{len(no_streak_players)} player-seasons ({total_sliding} sliding streaks)...")

    conn.commit()
    print(f"Done! Detected {total_sliding} sliding window streak segments.")


# --- Current Form detection ---

CURRENT_FORM_MIN_GAMES = 14   # Minimum games for a player-season to be eligible
CURRENT_FORM_MIN_SLICE = 10   # Minimum games in the form slice
CURRENT_FORM_MAX_SLICE = 60   # Maximum games to scan back


def create_current_form_table(conn):
    """Create the current_form table."""
    cursor = conn.cursor()
    cursor.execute("DROP TABLE IF EXISTS current_form")
    cursor.execute("""
        CREATE TABLE current_form (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id TEXT NOT NULL,
            season INTEGER NOT NULL,
            form_start_date TEXT NOT NULL,
            form_start_game_number INTEGER NOT NULL,
            total_season_games INTEGER NOT NULL,
            num_games INTEGER NOT NULL,
            at_bats INTEGER,
            hits INTEGER,
            doubles INTEGER,
            triples INTEGER,
            home_runs INTEGER,
            runs INTEGER,
            rbi INTEGER,
            walks INTEGER,
            strikeouts INTEGER,
            plate_appearances INTEGER,
            batting_avg REAL,
            obp REAL,
            slg REAL,
            ops REAL,
            iso REAL,
            season_at_bats INTEGER,
            season_hits INTEGER,
            season_doubles INTEGER,
            season_triples INTEGER,
            season_home_runs INTEGER,
            season_runs INTEGER,
            season_rbi INTEGER,
            season_walks INTEGER,
            season_strikeouts INTEGER,
            season_plate_appearances INTEGER,
            FOREIGN KEY (player_id) REFERENCES players(player_id),
            UNIQUE(player_id, season)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_current_form_player ON current_form(player_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_current_form_player_season ON current_form(player_id, season)")
    conn.commit()


def detect_current_form(conn):
    """Detect current form for all player-seasons.

    Algorithm: find the hottest tail slice — the start point (from the end
    of the season backwards) that maximizes OPS. This is "optimistic fan"
    mode: what recent stretch makes this player look best?

    Scans slice lengths from CURRENT_FORM_MIN_SLICE to CURRENT_FORM_MAX_SLICE
    and picks the one with the highest OPS.
    """
    create_current_form_table(conn)
    cursor = conn.cursor()

    player_seasons = get_player_seasons(conn)
    print(f"Detecting current form for {len(player_seasons)} player-seasons...")

    total_forms = 0
    for i, (player_id, season) in enumerate(player_seasons):
        games = get_game_logs_extended(conn, player_id, season)
        if len(games) < CURRENT_FORM_MIN_GAMES:
            continue

        # Find the tail slice with the highest OPS
        best_start_idx = None
        best_ops = -1.0
        max_slice = min(CURRENT_FORM_MAX_SLICE, len(games))

        for slice_len in range(CURRENT_FORM_MIN_SLICE, max_slice + 1):
            start_idx = len(games) - slice_len
            # Compute OPS for this tail slice
            total_ab = 0
            total_h = 0
            total_2b = 0
            total_3b = 0
            total_hr = 0
            total_bb = 0
            total_pa = 0
            for g in games[start_idx:]:
                ab = g[1] or 0
                h = g[2] or 0
                total_ab += ab
                total_h += h
                total_2b += g[3] or 0
                total_3b += g[4] or 0
                total_hr += g[5] or 0
                total_bb += g[6] or 0
                total_pa += g[8] or 0

            if total_ab > 0 and total_pa > 0:
                tb = (total_h - total_2b - total_3b - total_hr) + 2 * total_2b + 3 * total_3b + 4 * total_hr
                slg = tb / total_ab
                obp = (total_h + total_bb) / total_pa
                ops = obp + slg
            else:
                ops = 0.0

            if ops > best_ops:
                best_ops = ops
                best_start_idx = start_idx

        form_start_idx = best_start_idx if best_start_idx is not None else max(0, len(games) - CURRENT_FORM_MIN_SLICE)

        # Compute form stats
        form_stats = compute_segment_stats_extended(games, form_start_idx, len(games))
        season_stats = compute_segment_stats_extended(games, 0, len(games))

        # form_start_game_number is 1-indexed
        form_start_game_number = form_start_idx + 1

        cursor.execute("""
            INSERT INTO current_form (
                player_id, season, form_start_date, form_start_game_number,
                total_season_games, num_games,
                at_bats, hits, doubles, triples, home_runs,
                runs, rbi, walks, strikeouts, plate_appearances,
                batting_avg, obp, slg, ops, iso,
                season_at_bats, season_hits, season_doubles, season_triples,
                season_home_runs, season_runs, season_rbi,
                season_walks, season_strikeouts, season_plate_appearances
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            player_id, season, form_stats["start_date"], form_start_game_number,
            len(games), form_stats["num_games"],
            form_stats["at_bats"], form_stats["hits"], form_stats["doubles"],
            form_stats["triples"], form_stats["home_runs"],
            form_stats["runs"], form_stats["rbi"], form_stats["walks"],
            form_stats["strikeouts"], form_stats["plate_appearances"],
            form_stats["batting_avg"], form_stats["obp"], form_stats["slg"],
            form_stats["ops"], form_stats["iso"],
            season_stats["at_bats"], season_stats["hits"], season_stats["doubles"],
            season_stats["triples"], season_stats["home_runs"],
            season_stats["runs"], season_stats["rbi"], season_stats["walks"],
            season_stats["strikeouts"], season_stats["plate_appearances"],
        ))
        total_forms += 1

        if (i + 1) % 100 == 0:
            conn.commit()
            print(f"  Processed {i + 1}/{len(player_seasons)} player-seasons ({total_forms} forms)...")

    conn.commit()
    print(f"Done! Detected current form for {total_forms} player-seasons.")


if __name__ == "__main__":
    conn = sqlite3.connect(DB_PATH)
    detect_all_streaks(conn)
    detect_sensitive_streaks(conn)
    detect_sliding_streaks(conn)
    detect_current_form(conn)
    conn.close()
