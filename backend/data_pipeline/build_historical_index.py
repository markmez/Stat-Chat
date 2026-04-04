"""
One-time: build a historical index table for fast notable events lookups.

Scans all game logs (1920-2025) and pre-computes:
  - Start-of-season batting streaks (hit/OBP/multi-hit/HR in each of first N games)
  - Pitching first-N-starts stats (K, BB, ER totals)
  - Team runs allowed through N games per season

Results stored in `historical_index` table for instant daily lookups.
Run once after loading historical game logs, then never again unless data changes.

Usage:
    python build_historical_index.py --db /data/baseball_stats_full.db
"""

import argparse
import os
import sqlite3
import time

DB_PATH = os.getenv("DB_PATH", "/data/baseball_stats_full.db")


def _player_name(conn, player_id):
    row = conn.execute("SELECT name FROM players WHERE player_id = ?", (player_id,)).fetchone()
    return row[0] if row else player_id


def create_index_table(conn):
    conn.execute("DROP TABLE IF EXISTS historical_index")
    conn.execute("""
        CREATE TABLE historical_index (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_type TEXT NOT NULL,
            player_id TEXT,
            player_name TEXT,
            team TEXT,
            season INTEGER NOT NULL,
            value INTEGER,
            value2 INTEGER,
            detail TEXT
        )
    """)
    conn.commit()
    # Indexes added after all inserts for performance


def add_indexes(conn):
    conn.execute("CREATE INDEX IF NOT EXISTS idx_hist_type ON historical_index(scan_type)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_hist_type_val ON historical_index(scan_type, value)")
    conn.commit()


def build_batting_season_start_streaks(conn):
    """For each player-season, compute how many games from the start of the
    season they maintained various streaks."""
    print("  Building batting start-of-season streaks...")

    streak_types = [
        ("start_hit_streak", lambda h, bb, hbp, hr: h > 0),
        ("start_onbase_streak", lambda h, bb, hbp, hr: (h + bb + hbp) > 0),
        ("start_multi_hit_streak", lambda h, bb, hbp, hr: h >= 2),
        ("start_hr_streak", lambda h, bb, hbp, hr: hr > 0),
    ]

    seasons = conn.execute("""
        SELECT DISTINCT season FROM game_batting_logs
        WHERE season >= 1920 ORDER BY season
    """).fetchall()

    total = 0
    for (szn,) in seasons:
        games = conn.execute("""
            SELECT player_id, hits, walks, COALESCE(hit_by_pitch, 0), home_runs
            FROM game_batting_logs
            WHERE season = ? AND at_bats > 0
            ORDER BY player_id, date ASC
        """, (szn,)).fetchall()

        # Track per-player streaks
        current_pid = None
        streaks = {}  # type_name -> current count
        broken = {}   # type_name -> bool

        for pid, h, bb, hbp, hr in games:
            if pid != current_pid:
                # Save previous player's streaks
                if current_pid:
                    for stype, _ in streak_types:
                        if streaks.get(stype, 0) >= 3:
                            name = _player_name(conn, current_pid)
                            conn.execute("""
                                INSERT INTO historical_index
                                (scan_type, player_id, player_name, season, value)
                                VALUES (?, ?, ?, ?, ?)
                            """, (stype, current_pid, name, szn, streaks[stype]))
                            total += 1

                current_pid = pid
                streaks = {s: 0 for s, _ in streak_types}
                broken = {s: False for s, _ in streak_types}

            for stype, check_fn in streak_types:
                if not broken[stype]:
                    if check_fn(h or 0, bb or 0, hbp or 0, hr or 0):
                        streaks[stype] += 1
                    else:
                        broken[stype] = True

        # Save last player
        if current_pid:
            for stype, _ in streak_types:
                if streaks.get(stype, 0) >= 3:
                    name = _player_name(conn, current_pid)
                    conn.execute("""
                        INSERT INTO historical_index
                        (scan_type, player_id, player_name, season, value)
                        VALUES (?, ?, ?, ?, ?)
                    """, (stype, current_pid, name, szn, streaks[stype]))
                    total += 1

        conn.commit()

    print(f"    {total} batting streak records")


def build_pitching_first_starts(conn):
    """For each pitcher-season, compute stats from first N starts."""
    print("  Building pitching first-starts index...")

    seasons = conn.execute("""
        SELECT DISTINCT season FROM game_pitching_logs
        WHERE season >= 1920 AND is_start = 1 ORDER BY season
    """).fetchall()

    total = 0
    for (szn,) in seasons:
        starts = conn.execute("""
            SELECT player_id, strikeouts, walks, earned_runs, ip_outs, hits
            FROM game_pitching_logs
            WHERE season = ? AND is_start = 1
            ORDER BY player_id, date ASC
        """, (szn,)).fetchall()

        current_pid = None
        start_num = 0
        cum_k = 0
        cum_bb = 0
        cum_er = 0
        cum_outs = 0
        cum_h = 0

        for pid, k, bb, er, outs, h in starts:
            if pid != current_pid:
                # Save previous pitcher's cumulative stats at various start counts
                if current_pid:
                    _save_pitcher_index(conn, current_pid, szn, start_num,
                                       cum_k, cum_bb, cum_er, cum_outs, cum_h)
                    total += 1
                current_pid = pid
                start_num = 0
                cum_k = cum_bb = cum_er = cum_outs = cum_h = 0

            start_num += 1
            cum_k += (k or 0)
            cum_bb += (bb or 0)
            cum_er += (er or 0)
            cum_outs += (outs or 0)
            cum_h += (h or 0)

            # Save snapshot at start 2 and start 3
            if start_num in (2, 3):
                _save_pitcher_index(conn, pid, szn, start_num,
                                    cum_k, cum_bb, cum_er, cum_outs, cum_h)
                total += 1

        if current_pid:
            _save_pitcher_index(conn, current_pid, szn, start_num,
                                cum_k, cum_bb, cum_er, cum_outs, cum_h)
            total += 1

        conn.commit()

    print(f"    {total} pitching records")


def _save_pitcher_index(conn, pid, season, starts, k, bb, er, outs, h):
    name = _player_name(conn, pid)
    # Store cumulative stats through N starts
    detail = f"{starts} GS, {k} K, {bb} BB, {er} ER, {outs} outs, {h} H"

    # Scoreless through N starts (5+ IP each)?
    # We'll store general stats and query at lookup time
    conn.execute("""
        INSERT INTO historical_index
        (scan_type, player_id, player_name, season, value, value2, detail)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (f"pitcher_first_{starts}_starts", pid, name, season, k, bb, detail))


def build_team_runs_allowed(conn):
    """For each team-season, compute total runs allowed through N games."""
    print("  Building team runs allowed index...")

    seasons = conn.execute("""
        SELECT DISTINCT season FROM game_pitching_logs
        WHERE season >= 1920 ORDER BY season
    """).fetchall()

    total = 0
    for (szn,) in seasons:
        # Sum earned runs by team for the season, game by game
        # Use season_pitching_stats for the correct team that season
        team_games = conn.execute("""
            SELECT s.team, g.date, SUM(g.earned_runs) as game_er
            FROM game_pitching_logs g
            JOIN season_pitching_stats s ON g.player_id = s.player_id AND g.season = s.season
            WHERE g.season = ?
            GROUP BY s.team, g.date
            ORDER BY s.team, g.date ASC
        """, (szn,)).fetchall()

        current_team = None
        game_num = 0
        cum_er = 0

        for team, dt, game_er in team_games:
            if team != current_team:
                current_team = team
                game_num = 0
                cum_er = 0

            game_num += 1
            cum_er += (game_er or 0)

            # Save snapshots at key game counts
            if game_num in (5, 6, 7, 8, 10, 15, 20):
                conn.execute("""
                    INSERT INTO historical_index
                    (scan_type, player_id, player_name, team, season, value, value2)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (f"team_er_through_{game_num}", None, None, team, szn, cum_er, game_num))
                total += 1

        conn.commit()

    print(f"    {total} team records")


def build_career_start_batting(conn):
    """For each player, compute cumulative stats through their first N career games.

    Stores: HR, hits, XBH, RBI through first 3, 5, 7, 10, 15, 20 career games.
    Also stores age at debut (from birthdate).
    """
    print("  Building career-start batting index...")

    # Get birthdates for age calculation
    birthdates = {}
    for row in conn.execute("SELECT player_id, birthdate FROM players WHERE birthdate IS NOT NULL"):
        birthdates[row[0]] = row[1]

    # Process one player at a time to avoid loading 5.8M rows into memory
    players = conn.execute("""
        SELECT DISTINCT player_id FROM game_batting_logs
    """).fetchall()

    snapshot_points = [3, 5, 7, 10, 15, 20]
    max_snapshot = max(snapshot_points)
    total = 0

    for (pid,) in players:
        # Get this player's first N career games (across all seasons)
        games = conn.execute("""
            SELECT date, season, hits, home_runs, rbi, doubles, triples
            FROM game_batting_logs
            WHERE player_id = ? AND at_bats > 0
            ORDER BY date ASC
            LIMIT ?
        """, (pid, max_snapshot)).fetchall()

        if not games:
            continue

        debut_date = games[0][0]
        cum_hr = cum_hits = cum_rbi = cum_xbh = 0

        for career_game, (date, season, h, hr, rbi, d, t) in enumerate(games, 1):
            cum_hr += (hr or 0)
            cum_hits += (h or 0)
            cum_rbi += (rbi or 0)
            cum_xbh += (d or 0) + (t or 0) + (hr or 0)

            if career_game in snapshot_points:
                name = _player_name(conn, pid)

                # Calculate age at debut
                age_at_debut = None
                bd = birthdates.get(pid)
                if bd and debut_date:
                    try:
                        from datetime import datetime
                        birth = datetime.strptime(bd, "%Y-%m-%d")
                        debut = datetime.strptime(debut_date, "%Y-%m-%d")
                        age_at_debut = (debut - birth).days // 365
                    except:
                        pass

                # Store HR through N career games
                if cum_hr > 0:
                    conn.execute("""
                        INSERT INTO historical_index
                        (scan_type, player_id, player_name, season, value, value2, detail)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (f"career_first_{career_game}_hr", pid, name, season,
                          cum_hr, age_at_debut,
                          f"{cum_hr} HR in first {career_game} career games"))
                    total += 1

                # Store XBH through N career games
                if cum_xbh > 0:
                    conn.execute("""
                        INSERT INTO historical_index
                        (scan_type, player_id, player_name, season, value, value2, detail)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (f"career_first_{career_game}_xbh", pid, name, season,
                          cum_xbh, age_at_debut,
                          f"{cum_xbh} XBH in first {career_game} career games"))
                    total += 1

                # Store RBI through N career games
                if cum_rbi > 0:
                    conn.execute("""
                        INSERT INTO historical_index
                        (scan_type, player_id, player_name, season, value, value2, detail)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (f"career_first_{career_game}_rbi", pid, name, season,
                          cum_rbi, age_at_debut,
                          f"{cum_rbi} RBI in first {career_game} career games"))
                    total += 1

                # Store hits through N career games
                if cum_hits > 0:
                    conn.execute("""
                        INSERT INTO historical_index
                        (scan_type, player_id, player_name, season, value, value2, detail)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (f"career_first_{career_game}_hits", pid, name, season,
                          cum_hits, age_at_debut,
                          f"{cum_hits} H in first {career_game} career games"))
                    total += 1

        # Commit per player batch
        if total % 50000 == 0 and total > 0:
            conn.commit()

    conn.commit()
    print(f"    {total} career-start batting records")


def build_career_debut_ages(conn):
    """For each player, store their debut game stats with age.

    Enables "youngest player to do X in their debut" queries.
    """
    print("  Building debut game index...")

    birthdates = {}
    for row in conn.execute("SELECT player_id, birthdate FROM players WHERE birthdate IS NOT NULL"):
        birthdates[row[0]] = row[1]

    # Get first career game for each player
    debuts = conn.execute("""
        SELECT g.player_id, MIN(g.date) as debut_date
        FROM game_batting_logs g
        WHERE g.at_bats > 0
        GROUP BY g.player_id
    """).fetchall()

    total = 0
    for pid, debut_date in debuts:
        # Get the debut game stats
        game = conn.execute("""
            SELECT hits, home_runs, rbi, doubles, triples, at_bats, walks
            FROM game_batting_logs
            WHERE player_id = ? AND date = ?
            ORDER BY season ASC LIMIT 1
        """, (pid, debut_date)).fetchone()

        if not game:
            continue

        h, hr, rbi, d, t, ab, bb = game
        xbh = (d or 0) + (t or 0) + (hr or 0)

        # Calculate age at debut
        age_days = None
        bd = birthdates.get(pid)
        if bd and debut_date:
            try:
                from datetime import datetime
                birth = datetime.strptime(bd, "%Y-%m-%d")
                debut = datetime.strptime(debut_date, "%Y-%m-%d")
                age_days = (debut - birth).days
            except:
                continue

        if age_days is None:
            continue

        name = _player_name(conn, pid)
        season = int(debut_date[:4])

        # Store debut with XBH + RBI (for Griffin-type queries)
        if xbh > 0 and (rbi or 0) > 0:
            conn.execute("""
                INSERT INTO historical_index
                (scan_type, player_id, player_name, season, value, value2, detail)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, ("debut_xbh_rbi", pid, name, season,
                  age_days, xbh,
                  f"Age {age_days // 365}y {(age_days % 365) // 30}m, {xbh} XBH, {rbi} RBI in debut"))
            total += 1

        # Store debut with HR
        if (hr or 0) > 0:
            conn.execute("""
                INSERT INTO historical_index
                (scan_type, player_id, player_name, season, value, value2, detail)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, ("debut_hr", pid, name, season,
                  age_days, hr,
                  f"Age {age_days // 365}, {hr} HR in debut"))
            total += 1

    conn.commit()
    print(f"    {total} debut records")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=DB_PATH)
    args = parser.parse_args()

    print(f"Building historical index in {args.db}")
    conn = sqlite3.connect(args.db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-50000")  # 50MB cache

    t0 = time.time()
    create_index_table(conn)
    build_batting_season_start_streaks(conn)
    build_pitching_first_starts(conn)
    build_team_runs_allowed(conn)
    build_career_start_batting(conn)
    build_career_debut_ages(conn)
    add_indexes(conn)

    count = conn.execute("SELECT COUNT(*) FROM historical_index").fetchone()[0]
    elapsed = time.time() - t0
    conn.close()

    print(f"\nDone! {count} records in {elapsed:.0f}s")


if __name__ == "__main__":
    main()
