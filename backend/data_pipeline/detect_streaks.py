"""
Streak detection: Uses change-point detection (ruptures PELT) to find
hot and cold streaks in each player's season.

Reads game logs from SQLite, detects performance shifts, and stores
streak segments back into the database.

Usage:
    python3 detect_streaks.py                      # Process all seasons
    python3 detect_streaks.py --season 2026         # Process only 2026
    python3 detect_streaks.py --season 2026 --db /path/to/db
"""

import argparse
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


def get_player_seasons(conn, season_filter=None):
    """Get all player-season combos that have game logs."""
    cursor = conn.cursor()
    if season_filter is not None:
        cursor.execute("""
            SELECT DISTINCT player_id, season
            FROM game_batting_logs
            WHERE season = ?
            ORDER BY season, player_id
        """, (season_filter,))
    else:
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


def detect_all_streaks(conn, season_filter=None):
    """Run streak detection for all player-seasons and store results."""
    create_streaks_table(conn)
    cursor = conn.cursor()

    # Clear existing streaks (only for target season if filtered)
    if season_filter is not None:
        cursor.execute("DELETE FROM streaks WHERE season = ?", (season_filter,))
    else:
        cursor.execute("DELETE FROM streaks")
    conn.commit()

    player_seasons = get_player_seasons(conn, season_filter)
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


def detect_sensitive_streaks(conn, season_filter=None):
    """Second pass: run PELT with lower penalty (1.5) and keep only 7-30 game segments.

    Only processes player-seasons that had a single segment (no change points)
    in the primary (penalty=3) detection pass.
    """
    create_streaks_sensitive_table(conn)
    cursor = conn.cursor()

    # Clear existing sensitive streaks
    if season_filter is not None:
        cursor.execute("DELETE FROM streaks_sensitive WHERE season = ?", (season_filter,))
    else:
        cursor.execute("DELETE FROM streaks_sensitive")
    conn.commit()

    # Find player-seasons with exactly 1 streak segment (no change points detected)
    if season_filter is not None:
        cursor.execute("""
            SELECT player_id, season, COUNT(*) as seg_count
            FROM streaks
            WHERE season = ?
            GROUP BY player_id, season
            HAVING seg_count = 1
        """, (season_filter,))
    else:
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


def detect_sliding_streaks(conn, season_filter=None):
    """Third pass: sliding window gap-filler for player-seasons missing hot or cold data.

    For each player-season, checks if hot and/or cold streaks exist in Tier 1 or 2.
    If either is missing, runs a brute-force sliding window to find the best/worst
    N-game stretches and fills the gap.
    """
    create_streaks_sliding_table(conn)
    cursor = conn.cursor()

    # Clear existing
    if season_filter is not None:
        cursor.execute("DELETE FROM streaks_sliding WHERE season = ?", (season_filter,))
    else:
        cursor.execute("DELETE FROM streaks_sliding")
    conn.commit()

    player_seasons = get_player_seasons(conn, season_filter)
    print(f"Running sliding window gap-fill for {len(player_seasons)} player-seasons...")

    total_sliding = 0
    skipped = 0
    for i, (player_id, season) in enumerate(player_seasons):
        # Check which performance types are already covered by T1 or T2
        cursor.execute("""
            SELECT DISTINCT performance FROM (
                SELECT performance FROM streaks
                WHERE player_id = ? AND season = ? AND performance IN ('hot', 'cold')
                UNION ALL
                SELECT performance FROM streaks_sensitive
                WHERE player_id = ? AND season = ? AND performance IN ('hot', 'cold')
            )
        """, (player_id, season, player_id, season))
        existing = {row[0] for row in cursor.fetchall()}

        needs_hot = 'hot' not in existing
        needs_cold = 'cold' not in existing
        if not needs_hot and not needs_cold:
            skipped += 1
            continue

        games = get_game_logs(conn, player_id, season)
        if len(games) < MIN_SEGMENT_SIZE * 2:
            continue

        ops_signal = compute_game_ops(games)
        season_ops = float(np.mean(ops_signal))
        if season_ops == 0:
            continue

        best, worst = find_best_worst_windows(games, season_ops)

        for streak_data, label, needed in [(best, "hot", needs_hot), (worst, "cold", needs_cold)]:
            if not needed or streak_data is None:
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

        if (i + 1) % 500 == 0:
            conn.commit()
            print(f"  Processed {i + 1}/{len(player_seasons)} player-seasons ({total_sliding} sliding streaks, {skipped} already covered)...")

    conn.commit()
    print(f"Done! Detected {total_sliding} sliding window streak segments ({skipped} player-seasons already had both hot+cold).")


# --- Current Form detection ---

CURRENT_FORM_MIN_GAMES = 1    # Minimum games for a player-season to be eligible
CURRENT_FORM_MIN_SLICE = 1    # Minimum games in the form slice (early season)
CURRENT_FORM_FULL_SLICE = 6   # Min slice once past early season (2 series — shortest
                              # window that still reads as "he's been on fire")
CURRENT_FORM_EARLY_THRESHOLD = 14  # Below this, use MIN_SLICE; at or above, use FULL_SLICE
CURRENT_FORM_MAX_SLICE = 21   # Max slice cap — past 3 weeks drifts into "hot month"
                              # territory which will be its own detector
# Variance-resistance: the naïve "pick max OPS" was biased toward the floor
# (everyone landed at 10 games because variance peaks there). Instead, find
# the peak OPS across all slices, then pick the LONGEST slice whose OPS is
# within this margin of the peak. Genuinely electric 6-gamers still win;
# merely-close shorter windows yield to longer, more-stable ones.
CURRENT_FORM_VARIANCE_MARGIN = 0.050


def create_current_form_table(conn, drop=True):
    """Create the current_form table."""
    cursor = conn.cursor()
    if drop:
        cursor.execute("DROP TABLE IF EXISTS current_form")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS current_form (
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


def detect_current_form(conn, season_filter=None):
    """Detect current form for all player-seasons.

    Algorithm: find the hottest tail slice — the start point (from the end
    of the season backwards) that maximizes OPS. This is "optimistic fan"
    mode: what recent stretch makes this player look best?

    Scans slice lengths from CURRENT_FORM_MIN_SLICE to CURRENT_FORM_MAX_SLICE
    and picks the one with the highest OPS.
    """
    if season_filter is not None:
        create_current_form_table(conn, drop=False)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM current_form WHERE season = ?", (season_filter,))
        conn.commit()
    else:
        create_current_form_table(conn, drop=True)
        cursor = conn.cursor()

    player_seasons = get_player_seasons(conn, season_filter)
    print(f"Detecting current form for {len(player_seasons)} player-seasons...")

    total_forms = 0
    for i, (player_id, season) in enumerate(player_seasons):
        games = get_game_logs_extended(conn, player_id, season)
        if len(games) < 1:
            continue

        # Early season: use ALL games as current form (no optimization).
        # This gives "here's how the player is doing so far" rather than
        # cherry-picking the best stretch.
        if len(games) < CURRENT_FORM_EARLY_THRESHOLD:
            form_start_idx = 0
            best_start_idx = 0
        else:
            # Full season: two-pass variance-resistant selection.
            # First pass: compute OPS for every candidate slice length.
            # Second pass: take the LONGEST slice whose OPS is within
            # CURRENT_FORM_VARIANCE_MARGIN of the peak. This prevents the
            # naïve max-OPS picker from clustering everyone at the floor
            # (shorter slices have higher variance → more likely to peak).
            min_slice = CURRENT_FORM_FULL_SLICE
            max_slice = min(CURRENT_FORM_MAX_SLICE, len(games))

            slice_ops = {}  # slice_len → OPS
            for slice_len in range(min_slice, max_slice + 1):
                start_idx = len(games) - slice_len
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
                slice_ops[slice_len] = ops

            if slice_ops:
                peak_ops = max(slice_ops.values())
                threshold = peak_ops - CURRENT_FORM_VARIANCE_MARGIN
                # Walk from longest to shortest; first slice within margin wins.
                best_slice_len = min_slice
                for slice_len in range(max_slice, min_slice - 1, -1):
                    if slice_ops[slice_len] >= threshold:
                        best_slice_len = slice_len
                        break
                form_start_idx = len(games) - best_slice_len
            else:
                form_start_idx = max(0, len(games) - min_slice)

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


# ============================================================================
# PITCHING STREAK DETECTION
# ============================================================================

# Pitching PELT parameters
PITCHING_STARTER_WINDOW = 3     # Rolling window for starters
PITCHING_STARTER_MIN_SEG = 3    # Minimum starts in a segment
PITCHING_RELIEVER_WINDOW = 5    # Rolling window for relievers
PITCHING_RELIEVER_MIN_SEG = 5   # Minimum games in a segment
PITCHING_ERA_CAP = 27.0         # Cap for 0-IP appearances (9 ER * 9 innings / 0 IP → use this)


def classify_pitcher(games_started, total_games):
    """Classify pitcher as starter or reliever. >50% GS = starter."""
    if total_games == 0:
        return "reliever"
    return "starter" if games_started > total_games / 2 else "reliever"


def create_pitching_streaks_table(conn):
    """Create the pitching_streaks table."""
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pitching_streaks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id TEXT NOT NULL,
            season INTEGER NOT NULL,
            role TEXT,
            start_date TEXT NOT NULL,
            end_date TEXT NOT NULL,
            num_games INTEGER NOT NULL,
            ip_outs INTEGER,
            innings_pitched TEXT,
            hits INTEGER,
            earned_runs INTEGER,
            walks INTEGER,
            strikeouts INTEGER,
            home_runs INTEGER,
            era REAL,
            whip REAL,
            k_per_9 REAL,
            performance TEXT,
            FOREIGN KEY (player_id) REFERENCES players(player_id)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_pstreaks_player ON pitching_streaks(player_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_pstreaks_player_season ON pitching_streaks(player_id, season)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_pstreaks_performance ON pitching_streaks(performance)")
    conn.commit()


def create_pitching_streaks_sensitive_table(conn):
    """Create the pitching_streaks_sensitive table."""
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pitching_streaks_sensitive (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id TEXT NOT NULL,
            season INTEGER NOT NULL,
            role TEXT,
            start_date TEXT NOT NULL,
            end_date TEXT NOT NULL,
            num_games INTEGER NOT NULL,
            ip_outs INTEGER,
            innings_pitched TEXT,
            hits INTEGER,
            earned_runs INTEGER,
            walks INTEGER,
            strikeouts INTEGER,
            home_runs INTEGER,
            era REAL,
            whip REAL,
            k_per_9 REAL,
            performance TEXT,
            season_era REAL,
            FOREIGN KEY (player_id) REFERENCES players(player_id)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_pstreaks_sens_player ON pitching_streaks_sensitive(player_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_pstreaks_sens_player_season ON pitching_streaks_sensitive(player_id, season)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_pstreaks_sens_performance ON pitching_streaks_sensitive(performance)")
    conn.commit()


def create_pitching_streaks_sliding_table(conn):
    """Create the pitching_streaks_sliding table."""
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pitching_streaks_sliding (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id TEXT NOT NULL,
            season INTEGER NOT NULL,
            role TEXT,
            start_date TEXT NOT NULL,
            end_date TEXT NOT NULL,
            num_games INTEGER NOT NULL,
            ip_outs INTEGER,
            innings_pitched TEXT,
            hits INTEGER,
            earned_runs INTEGER,
            walks INTEGER,
            strikeouts INTEGER,
            home_runs INTEGER,
            era REAL,
            whip REAL,
            k_per_9 REAL,
            performance TEXT,
            season_era REAL,
            FOREIGN KEY (player_id) REFERENCES players(player_id)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_pstreaks_slid_player ON pitching_streaks_sliding(player_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_pstreaks_slid_player_season ON pitching_streaks_sliding(player_id, season)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_pstreaks_slid_performance ON pitching_streaks_sliding(performance)")
    conn.commit()


def get_pitching_player_seasons(conn, season_filter=None):
    """Get all pitcher-season combos that have game logs."""
    cursor = conn.cursor()
    if season_filter is not None:
        cursor.execute("""
            SELECT DISTINCT player_id, season
            FROM game_pitching_logs
            WHERE season = ?
            ORDER BY season, player_id
        """, (season_filter,))
    else:
        cursor.execute("""
            SELECT DISTINCT player_id, season
            FROM game_pitching_logs
            ORDER BY season, player_id
        """)
    return cursor.fetchall()


def get_pitching_game_logs(conn, player_id, season, starts_only=False):
    """Get pitching game logs for a player-season, ordered by date.

    Returns: [(date, ip_outs, hits, earned_runs, walks, strikeouts, home_runs, is_start, batters_faced)]
    """
    cursor = conn.cursor()
    if starts_only:
        cursor.execute("""
            SELECT date, ip_outs, hits, earned_runs, walks, strikeouts,
                   home_runs, is_start, batters_faced
            FROM game_pitching_logs
            WHERE player_id = ? AND season = ? AND is_start = 1
            ORDER BY date ASC
        """, (player_id, season))
    else:
        cursor.execute("""
            SELECT date, ip_outs, hits, earned_runs, walks, strikeouts,
                   home_runs, is_start, batters_faced
            FROM game_pitching_logs
            WHERE player_id = ? AND season = ?
            ORDER BY date ASC
        """, (player_id, season))
    return cursor.fetchall()


def compute_game_era_signal(games):
    """Compute per-game ERA values from pitching game log rows.

    For 0-IP appearances, cap at PITCHING_ERA_CAP (27.0).
    """
    era_values = []
    for g in games:
        ip_outs = g[1] or 0
        er = g[3] or 0
        if ip_outs > 0:
            era_values.append(9.0 * er / (ip_outs / 3.0))
        else:
            era_values.append(PITCHING_ERA_CAP if er > 0 else 0.0)
    return np.array(era_values)


def compute_pitching_segment_stats(games, start_idx, end_idx):
    """Compute aggregate pitching stats for a segment of games."""
    segment = games[start_idx:end_idx]
    total_ip_outs = sum(g[1] or 0 for g in segment)
    total_h = sum(g[2] or 0 for g in segment)
    total_er = sum(g[3] or 0 for g in segment)
    total_bb = sum(g[4] or 0 for g in segment)
    total_so = sum(g[5] or 0 for g in segment)
    total_hr = sum(g[6] or 0 for g in segment)

    ip = total_ip_outs / 3.0 if total_ip_outs > 0 else 0
    era = round(9.0 * total_er / ip, 2) if ip > 0 else None
    whip = round((total_bb + total_h) / ip, 2) if ip > 0 else None
    k_per_9 = round(9.0 * total_so / ip, 1) if ip > 0 else None

    full_ip = total_ip_outs // 3
    remainder = total_ip_outs % 3
    innings_pitched = f"{full_ip}.{remainder}"

    return {
        "start_date": segment[0][0],
        "end_date": segment[-1][0],
        "num_games": len(segment),
        "ip_outs": total_ip_outs,
        "innings_pitched": innings_pitched,
        "hits": total_h,
        "earned_runs": total_er,
        "walks": total_bb,
        "strikeouts": total_so,
        "home_runs": total_hr,
        "era": era,
        "whip": whip,
        "k_per_9": k_per_9,
    }


def label_pitching_performance(segment_era, season_era):
    """Label a pitching segment as hot, cold, or average.

    INVERTED from batting: low ERA = hot, high ERA = cold.
    Asymmetric thresholds: hot <= 70% season avg, cold >= 140% season avg.
    """
    if season_era is None or season_era == 0:
        return "average"
    if segment_era is None:
        return "average"
    ratio = segment_era / season_era
    if ratio <= 0.70:
        return "hot"
    elif ratio >= 1.40:
        return "cold"
    else:
        return "average"


def detect_pitching_streaks(conn, season_filter=None):
    """Run PELT streak detection for all pitcher-seasons."""
    create_pitching_streaks_table(conn)
    cursor = conn.cursor()
    if season_filter is not None:
        cursor.execute("DELETE FROM pitching_streaks WHERE season = ?", (season_filter,))
    else:
        cursor.execute("DELETE FROM pitching_streaks")
    conn.commit()

    player_seasons = get_pitching_player_seasons(conn, season_filter)
    print(f"Running pitching streak detection for {len(player_seasons)} pitcher-seasons...")

    total_streaks = 0
    for i, (player_id, season) in enumerate(player_seasons):
        # Determine role
        all_games = get_pitching_game_logs(conn, player_id, season)
        gs_count = sum(1 for g in all_games if g[7] == 1)
        role = classify_pitcher(gs_count, len(all_games))

        if role == "starter":
            games = get_pitching_game_logs(conn, player_id, season, starts_only=True)
            min_seg = PITCHING_STARTER_MIN_SEG
            window = PITCHING_STARTER_WINDOW
        else:
            games = all_games
            min_seg = PITCHING_RELIEVER_MIN_SEG
            window = PITCHING_RELIEVER_WINDOW

        if len(games) < min_seg * 2:
            continue

        era_signal = compute_game_era_signal(games)
        season_era = float(np.mean(era_signal))

        # Smooth with role-appropriate window
        smoothed = np.convolve(era_signal, np.ones(window) / window, mode='same')
        smoothed = smoothed.reshape(-1, 1)

        algo = rpt.Pelt(model="l2", min_size=min_seg, jump=1)
        algo.fit(smoothed)
        breakpoints = algo.predict(pen=PENALTY)

        start_idx = 0
        for end_idx in breakpoints:
            if end_idx > len(games):
                end_idx = len(games)

            stats = compute_pitching_segment_stats(games, start_idx, end_idx)
            performance = label_pitching_performance(stats["era"], season_era)

            cursor.execute("""
                INSERT INTO pitching_streaks (
                    player_id, season, role, start_date, end_date, num_games,
                    ip_outs, innings_pitched, hits, earned_runs, walks,
                    strikeouts, home_runs, era, whip, k_per_9, performance
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                player_id, season, role,
                stats["start_date"], stats["end_date"], stats["num_games"],
                stats["ip_outs"], stats["innings_pitched"],
                stats["hits"], stats["earned_runs"], stats["walks"],
                stats["strikeouts"], stats["home_runs"],
                stats["era"], stats["whip"], stats["k_per_9"],
                performance,
            ))
            total_streaks += 1
            start_idx = end_idx

        if (i + 1) % 100 == 0:
            conn.commit()
            print(f"  Processed {i + 1}/{len(player_seasons)} pitcher-seasons ({total_streaks} streaks)...")

    conn.commit()
    print(f"Done! Detected {total_streaks} pitching streak segments.")


def detect_pitching_sensitive_streaks(conn, season_filter=None):
    """Second pass: PELT with lower penalty for pitcher-seasons with no change points."""
    create_pitching_streaks_sensitive_table(conn)
    cursor = conn.cursor()
    if season_filter is not None:
        cursor.execute("DELETE FROM pitching_streaks_sensitive WHERE season = ?", (season_filter,))
    else:
        cursor.execute("DELETE FROM pitching_streaks_sensitive")
    conn.commit()

    if season_filter is not None:
        cursor.execute("""
            SELECT player_id, season, role, COUNT(*) as seg_count
            FROM pitching_streaks
            WHERE season = ?
            GROUP BY player_id, season
            HAVING seg_count = 1
        """, (season_filter,))
    else:
        cursor.execute("""
            SELECT player_id, season, role, COUNT(*) as seg_count
            FROM pitching_streaks
            GROUP BY player_id, season
            HAVING seg_count = 1
        """)
    single_segment_pitchers = cursor.fetchall()
    print(f"Running pitching sensitive streak detection for {len(single_segment_pitchers)} single-segment pitcher-seasons...")

    total_sensitive = 0
    for i, (player_id, season, role, _) in enumerate(single_segment_pitchers):
        if role == "starter":
            games = get_pitching_game_logs(conn, player_id, season, starts_only=True)
            min_seg = PITCHING_STARTER_MIN_SEG
            window = PITCHING_STARTER_WINDOW
            max_seg = 15
        else:
            games = get_pitching_game_logs(conn, player_id, season)
            min_seg = PITCHING_RELIEVER_MIN_SEG
            window = PITCHING_RELIEVER_WINDOW
            max_seg = 30

        if len(games) < min_seg * 2:
            continue

        era_signal = compute_game_era_signal(games)
        season_era = float(np.mean(era_signal))

        smoothed = np.convolve(era_signal, np.ones(window) / window, mode='same')
        smoothed = smoothed.reshape(-1, 1)

        algo = rpt.Pelt(model="l2", min_size=min_seg, jump=1)
        algo.fit(smoothed)
        breakpoints = algo.predict(pen=SENSITIVE_PENALTY)

        start_idx = 0
        for end_idx in breakpoints:
            if end_idx > len(games):
                end_idx = len(games)

            num_games = end_idx - start_idx
            if min_seg <= num_games <= max_seg:
                stats = compute_pitching_segment_stats(games, start_idx, end_idx)
                performance = label_pitching_performance(stats["era"], season_era)

                cursor.execute("""
                    INSERT INTO pitching_streaks_sensitive (
                        player_id, season, role, start_date, end_date, num_games,
                        ip_outs, innings_pitched, hits, earned_runs, walks,
                        strikeouts, home_runs, era, whip, k_per_9,
                        performance, season_era
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    player_id, season, role,
                    stats["start_date"], stats["end_date"], stats["num_games"],
                    stats["ip_outs"], stats["innings_pitched"],
                    stats["hits"], stats["earned_runs"], stats["walks"],
                    stats["strikeouts"], stats["home_runs"],
                    stats["era"], stats["whip"], stats["k_per_9"],
                    performance, round(season_era, 2) if season_era else None,
                ))
                total_sensitive += 1

            start_idx = end_idx

        if (i + 1) % 100 == 0:
            conn.commit()
            print(f"  Processed {i + 1}/{len(single_segment_pitchers)} pitcher-seasons ({total_sensitive} sensitive streaks)...")

    conn.commit()
    print(f"Done! Detected {total_sensitive} pitching sensitive streak segments.")


def detect_pitching_sliding_streaks(conn, season_filter=None):
    """Third pass: sliding window gap-filler for pitcher-seasons missing hot or cold."""
    create_pitching_streaks_sliding_table(conn)
    cursor = conn.cursor()
    if season_filter is not None:
        cursor.execute("DELETE FROM pitching_streaks_sliding WHERE season = ?", (season_filter,))
    else:
        cursor.execute("DELETE FROM pitching_streaks_sliding")
    conn.commit()

    player_seasons = get_pitching_player_seasons(conn, season_filter)
    print(f"Running pitching sliding window gap-fill for {len(player_seasons)} pitcher-seasons...")

    total_sliding = 0
    skipped = 0
    for i, (player_id, season) in enumerate(player_seasons):
        cursor.execute("""
            SELECT DISTINCT performance FROM (
                SELECT performance FROM pitching_streaks
                WHERE player_id = ? AND season = ? AND performance IN ('hot', 'cold')
                UNION ALL
                SELECT performance FROM pitching_streaks_sensitive
                WHERE player_id = ? AND season = ? AND performance IN ('hot', 'cold')
            )
        """, (player_id, season, player_id, season))
        existing = {row[0] for row in cursor.fetchall()}

        needs_hot = 'hot' not in existing
        needs_cold = 'cold' not in existing
        if not needs_hot and not needs_cold:
            skipped += 1
            continue

        # Determine role
        all_games = get_pitching_game_logs(conn, player_id, season)
        gs_count = sum(1 for g in all_games if g[7] == 1)
        role = classify_pitcher(gs_count, len(all_games))

        if role == "starter":
            games = get_pitching_game_logs(conn, player_id, season, starts_only=True)
            min_seg = PITCHING_STARTER_MIN_SEG
            window_sizes = [5, 4, 3]
        else:
            games = all_games
            min_seg = PITCHING_RELIEVER_MIN_SEG
            window_sizes = [10, 8, 5]

        if len(games) < min_seg * 2:
            continue

        era_signal = compute_game_era_signal(games)
        season_era = float(np.mean(era_signal))
        if season_era == 0:
            continue

        # Find best (lowest ERA = hot) and worst (highest ERA = cold) windows
        best = None  # lowest ERA
        worst = None  # highest ERA
        for ws in window_sizes:
            if len(games) < ws:
                continue
            for start in range(len(games) - ws + 1):
                end = start + ws
                stats = compute_pitching_segment_stats(games, start, end)
                seg_era = stats["era"]
                if seg_era is None:
                    continue
                if best is None or seg_era < best[0]:
                    best = (seg_era, start, end, ws)
                if worst is None or seg_era > worst[0]:
                    worst = (seg_era, start, end, ws)

        for streak_data, label, needed in [(best, "hot", needs_hot), (worst, "cold", needs_cold)]:
            if not needed or streak_data is None:
                continue
            seg_era, start_idx, end_idx, _ = streak_data
            deviation = abs(seg_era - season_era) / season_era if season_era > 0 else 0
            if deviation < SLIDING_MIN_DEVIATION:
                continue

            stats = compute_pitching_segment_stats(games, start_idx, end_idx)

            cursor.execute("""
                INSERT INTO pitching_streaks_sliding (
                    player_id, season, role, start_date, end_date, num_games,
                    ip_outs, innings_pitched, hits, earned_runs, walks,
                    strikeouts, home_runs, era, whip, k_per_9,
                    performance, season_era
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                player_id, season, role,
                stats["start_date"], stats["end_date"], stats["num_games"],
                stats["ip_outs"], stats["innings_pitched"],
                stats["hits"], stats["earned_runs"], stats["walks"],
                stats["strikeouts"], stats["home_runs"],
                stats["era"], stats["whip"], stats["k_per_9"],
                label, round(season_era, 2),
            ))
            total_sliding += 1

        if (i + 1) % 500 == 0:
            conn.commit()
            print(f"  Processed {i + 1}/{len(player_seasons)} pitcher-seasons ({total_sliding} sliding, {skipped} already covered)...")

    conn.commit()
    print(f"Done! Detected {total_sliding} pitching sliding window streaks ({skipped} already had both hot+cold).")


# --- Pitching Current Form ---

PITCHING_FORM_STARTER_MIN = 3
PITCHING_FORM_STARTER_MAX = 15
PITCHING_FORM_RELIEVER_MIN = 5
PITCHING_FORM_RELIEVER_MAX = 30


def create_pitching_current_form_table(conn, drop=True):
    """Create the pitching_current_form table."""
    cursor = conn.cursor()
    if drop:
        cursor.execute("DROP TABLE IF EXISTS pitching_current_form")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pitching_current_form (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id TEXT NOT NULL,
            season INTEGER NOT NULL,
            role TEXT,
            form_start_date TEXT NOT NULL,
            form_start_game_number INTEGER NOT NULL,
            total_season_games INTEGER NOT NULL,
            num_games INTEGER NOT NULL,
            ip_outs INTEGER,
            innings_pitched TEXT,
            hits INTEGER,
            earned_runs INTEGER,
            home_runs INTEGER,
            walks INTEGER,
            strikeouts INTEGER,
            batters_faced INTEGER,
            era REAL,
            whip REAL,
            k_per_9 REAL,
            bb_per_9 REAL,
            season_ip_outs INTEGER,
            season_hits INTEGER,
            season_earned_runs INTEGER,
            season_home_runs INTEGER,
            season_walks INTEGER,
            season_strikeouts INTEGER,
            season_batters_faced INTEGER,
            season_era REAL,
            FOREIGN KEY (player_id) REFERENCES players(player_id),
            UNIQUE(player_id, season)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_pcform_player ON pitching_current_form(player_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_pcform_player_season ON pitching_current_form(player_id, season)")
    conn.commit()


def detect_pitching_current_form(conn, season_filter=None):
    """Detect current form for all pitcher-seasons.

    Algorithm: find the tail slice that minimizes ERA (optimistic fan, inverted).
    Starters: scan 3-15 starts. Relievers: scan 5-30 games.
    """
    if season_filter is not None:
        create_pitching_current_form_table(conn, drop=False)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM pitching_current_form WHERE season = ?", (season_filter,))
        conn.commit()
    else:
        create_pitching_current_form_table(conn, drop=True)
        cursor = conn.cursor()

    player_seasons = get_pitching_player_seasons(conn, season_filter)
    print(f"Detecting pitching current form for {len(player_seasons)} pitcher-seasons...")

    total_forms = 0
    for i, (player_id, season) in enumerate(player_seasons):
        all_games = get_pitching_game_logs(conn, player_id, season)
        gs_count = sum(1 for g in all_games if g[7] == 1)
        role = classify_pitcher(gs_count, len(all_games))

        if role == "starter":
            games = get_pitching_game_logs(conn, player_id, season, starts_only=True)
            min_slice = PITCHING_FORM_STARTER_MIN
            max_slice = PITCHING_FORM_STARTER_MAX
        else:
            games = all_games
            min_slice = PITCHING_FORM_RELIEVER_MIN
            max_slice = PITCHING_FORM_RELIEVER_MAX

        if len(games) < 1:
            continue

        # Early season: if fewer games than normal minimum, use all games
        if len(games) < min_slice:
            form_start_idx = 0
        else:
            # Find tail slice that minimizes ERA
            best_start_idx = None
            best_era = float('inf')
            actual_max = min(max_slice, len(games))

            for slice_len in range(min_slice, actual_max + 1):
                start_idx = len(games) - slice_len
                total_ip_outs = sum(g[1] or 0 for g in games[start_idx:])
                total_er = sum(g[3] or 0 for g in games[start_idx:])
                ip = total_ip_outs / 3.0
                if ip > 0:
                    era = 9.0 * total_er / ip
                else:
                    era = PITCHING_ERA_CAP

                if era < best_era:
                    best_era = era
                    best_start_idx = start_idx

            form_start_idx = best_start_idx if best_start_idx is not None else max(0, len(games) - min_slice)

        # Compute form stats
        form = compute_pitching_segment_stats(games, form_start_idx, len(games))
        season_stats = compute_pitching_segment_stats(games, 0, len(games))

        # Additional rates for form
        form_ip = (form["ip_outs"] or 0) / 3.0
        form_bb_per_9 = round(9.0 * form["walks"] / form_ip, 1) if form_ip > 0 else None
        total_bf_form = sum(g[8] or 0 for g in games[form_start_idx:])
        total_bf_season = sum(g[8] or 0 for g in games)

        cursor.execute("""
            INSERT INTO pitching_current_form (
                player_id, season, role, form_start_date, form_start_game_number,
                total_season_games, num_games,
                ip_outs, innings_pitched, hits, earned_runs, home_runs,
                walks, strikeouts, batters_faced, era, whip, k_per_9, bb_per_9,
                season_ip_outs, season_hits, season_earned_runs, season_home_runs,
                season_walks, season_strikeouts, season_batters_faced, season_era
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            player_id, season, role,
            form["start_date"], form_start_idx + 1,
            len(games), form["num_games"],
            form["ip_outs"], form["innings_pitched"],
            form["hits"], form["earned_runs"], form["home_runs"],
            form["walks"], form["strikeouts"], total_bf_form,
            form["era"], form["whip"], form["k_per_9"], form_bb_per_9,
            season_stats["ip_outs"], season_stats["hits"],
            season_stats["earned_runs"], season_stats["home_runs"],
            season_stats["walks"], season_stats["strikeouts"],
            total_bf_season, season_stats["era"],
        ))
        total_forms += 1

        if (i + 1) % 100 == 0:
            conn.commit()
            print(f"  Processed {i + 1}/{len(player_seasons)} pitcher-seasons ({total_forms} forms)...")

    conn.commit()
    print(f"Done! Detected pitching current form for {total_forms} pitcher-seasons.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Detect streaks and current form from game logs")
    parser.add_argument("--season", type=int, default=None,
                        help="Process only this season (e.g. 2026). Omit for all seasons.")
    parser.add_argument("--db", default=DB_PATH, help="Path to SQLite database")
    args = parser.parse_args()

    season_filter = args.season
    if season_filter:
        print(f"Processing season {season_filter} only")

    conn = sqlite3.connect(args.db)
    conn.execute("PRAGMA journal_mode=WAL")

    # Batting streaks
    detect_all_streaks(conn, season_filter)
    detect_sensitive_streaks(conn, season_filter)
    detect_sliding_streaks(conn, season_filter)
    detect_current_form(conn, season_filter)

    # Pitching streaks
    detect_pitching_streaks(conn, season_filter)
    detect_pitching_sensitive_streaks(conn, season_filter)
    detect_pitching_sliding_streaks(conn, season_filter)
    detect_pitching_current_form(conn, season_filter)

    conn.close()
