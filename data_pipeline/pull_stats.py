"""
Data pipeline: Pull batting stats from FanGraphs into SQLite.

Pulls both overall season stats (via pybaseball) and platoon splits
(vs LHP / vs RHP, via FanGraphs API directly).

Usage:
    python3 pull_stats.py                  # Pull 2024-2025 (default)
    python3 pull_stats.py 2020 2025        # Pull 2020-2025
    python3 pull_stats.py 1871 2025        # Pull all available history
"""

import json
import re
import sqlite3
import sys
import os
import time

import pandas as pd
import requests
import pybaseball
from pybaseball import batting_stats


DEFAULT_START = 2024
DEFAULT_END = 2025

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "baseball_stats.db")

# FanGraphs API split codes
SPLIT_VS_LHP = 13
SPLIT_VS_RHP = 14


def create_tables(conn):
    """Create the SQLite schema."""
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS players (
            player_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            team TEXT,
            positions TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS season_batting_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id TEXT NOT NULL,
            season INTEGER NOT NULL,
            team TEXT,
            age INTEGER,
            games INTEGER,
            plate_appearances INTEGER,
            at_bats INTEGER,
            hits INTEGER,
            doubles INTEGER,
            triples INTEGER,
            home_runs INTEGER,
            runs INTEGER,
            rbi INTEGER,
            stolen_bases INTEGER,
            caught_stealing INTEGER,
            walks INTEGER,
            strikeouts INTEGER,
            hit_by_pitch INTEGER,
            sacrifice_flies INTEGER,
            intentional_walks INTEGER,
            batting_avg REAL,
            obp REAL,
            slg REAL,
            ops REAL,
            iso REAL,
            babip REAL,
            wrc_plus INTEGER,
            war REAL,
            FOREIGN KEY (player_id) REFERENCES players(player_id),
            UNIQUE(player_id, season, team)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS platoon_splits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id TEXT NOT NULL,
            season INTEGER NOT NULL,
            split TEXT NOT NULL,
            plate_appearances INTEGER,
            at_bats INTEGER,
            hits INTEGER,
            doubles INTEGER,
            triples INTEGER,
            home_runs INTEGER,
            rbi INTEGER,
            walks INTEGER,
            strikeouts INTEGER,
            batting_avg REAL,
            obp REAL,
            slg REAL,
            ops REAL,
            iso REAL,
            babip REAL,
            wrc_plus INTEGER,
            FOREIGN KEY (player_id) REFERENCES players(player_id),
            UNIQUE(player_id, season, split)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS game_batting_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id TEXT NOT NULL,
            season INTEGER NOT NULL,
            date TEXT NOT NULL,
            opponent TEXT,
            plate_appearances INTEGER,
            at_bats INTEGER,
            hits INTEGER,
            doubles INTEGER,
            triples INTEGER,
            home_runs INTEGER,
            runs INTEGER,
            rbi INTEGER,
            walks INTEGER,
            strikeouts INTEGER,
            batting_avg REAL,
            obp REAL,
            slg REAL,
            ops REAL,
            FOREIGN KEY (player_id) REFERENCES players(player_id),
            UNIQUE(player_id, season, date)
        )
    """)

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_stats_player ON season_batting_stats(player_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_stats_season ON season_batting_stats(season)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_stats_player_season ON season_batting_stats(player_id, season)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_splits_player ON platoon_splits(player_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_splits_player_season ON platoon_splits(player_id, season)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_splits_split ON platoon_splits(split)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_gamelogs_player ON game_batting_logs(player_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_gamelogs_player_season ON game_batting_logs(player_id, season)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_gamelogs_date ON game_batting_logs(date)")

    conn.commit()


def pull_season_stats(start_season, end_season):
    """Pull overall season batting stats via pybaseball."""
    print(f"Pulling season batting stats for {start_season}-{end_season}...")
    pybaseball.cache.enable()
    data = batting_stats(start_season, end_season, qual=0)
    print(f"  Pulled {len(data)} player-season rows")
    return data


def pull_splits_from_fangraphs(season, split_code, split_name):
    """Pull split stats directly from FanGraphs API."""
    print(f"  Pulling {split_name} splits for {season}...")
    url = "https://www.fangraphs.com/api/leaders/major-league/data"
    params = {
        "pos": "all",
        "stats": "bat",
        "lg": "all",
        "qual": "0",
        "season": str(season),
        "season1": str(season),
        "month": str(split_code),
        "hand": "",
        "team": "0",
        "pageitems": "10000",
        "pagenum": "1",
        "ind": "1",
        "rost": "0",
        "players": "",
        "type": "8",
        "postseason": "",
        "sortdir": "default",
        "sortstat": "WAR",
    }
    resp = requests.get(url, params=params)
    resp.raise_for_status()
    result = resp.json()
    rows = result["data"] if isinstance(result, dict) else result
    print(f"    Got {len(rows)} rows")
    return rows


def pull_all_splits(start_season, end_season):
    """Pull vs LHP and vs RHP splits for each season."""
    print(f"Pulling platoon splits for {start_season}-{end_season}...")
    all_splits = []
    for season in range(start_season, end_season + 1):
        for split_code, split_name in [(SPLIT_VS_LHP, "vs_LHP"), (SPLIT_VS_RHP, "vs_RHP")]:
            try:
                rows = pull_splits_from_fangraphs(season, split_code, split_name)
                for r in rows:
                    r["_season"] = season
                    r["_split"] = split_name
                all_splits.extend(rows)
            except Exception as e:
                print(f"    Warning: Failed to pull {split_name} for {season}: {e}")
    return all_splits


def get_val(row, key, available_keys, default=None):
    """Safely get a value from a row, returning default for missing/NaN."""
    if key in available_keys:
        val = row.get(key)
        if val is not None and str(val) != "nan":
            return val
    return default


def load_season_stats(conn, data):
    """Load season stats dataframe into SQLite."""
    cursor = conn.cursor()
    available_cols = set(data.columns)
    players_added = set()
    rows_inserted = 0

    for _, row in data.iterrows():
        player_id = str(row.get("IDfg", ""))
        name = row.get("Name", "")
        team = row.get("Team", "")
        season = int(row.get("Season", 0))

        if not player_id or not name:
            continue

        if player_id not in players_added:
            cursor.execute(
                "INSERT OR REPLACE INTO players (player_id, name, team) VALUES (?, ?, ?)",
                (player_id, name, team),
            )
            players_added.add(player_id)

        gv = lambda key, default=None: get_val(row, key, available_cols, default)

        cursor.execute("""
            INSERT OR REPLACE INTO season_batting_stats (
                player_id, season, team, age, games, plate_appearances,
                at_bats, hits, doubles, triples, home_runs, runs, rbi,
                stolen_bases, caught_stealing, walks, strikeouts,
                hit_by_pitch, sacrifice_flies, intentional_walks,
                batting_avg, obp, slg, ops, iso, babip, wrc_plus, war
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            player_id, season, team,
            gv("Age"), gv("G"), gv("PA"),
            gv("AB"), gv("H"),
            gv("2B"), gv("3B"), gv("HR"),
            gv("R"), gv("RBI"),
            gv("SB"), gv("CS"),
            gv("BB"), gv("SO"),
            gv("HBP"), gv("SF"), gv("IBB"),
            gv("AVG"), gv("OBP"), gv("SLG"), gv("OPS"),
            gv("ISO"), gv("BABIP"),
            gv("wRC+"), gv("WAR"),
        ))
        rows_inserted += 1

    conn.commit()
    print(f"  Loaded {rows_inserted} season stat rows for {len(players_added)} players")


def load_splits(conn, splits_data):
    """Load platoon splits into SQLite."""
    cursor = conn.cursor()
    rows_inserted = 0

    for row in splits_data:
        player_id = str(row.get("playerid", row.get("IDfg", "")))
        name = row.get("Name", "")
        season = row.get("_season")
        split = row.get("_split")

        if not player_id or not name or not season:
            continue

        # Ensure player exists in players table
        cursor.execute(
            "INSERT OR IGNORE INTO players (player_id, name, team) VALUES (?, ?, ?)",
            (player_id, name, row.get("Team", "")),
        )

        cursor.execute("""
            INSERT OR REPLACE INTO platoon_splits (
                player_id, season, split, plate_appearances, at_bats,
                hits, doubles, triples, home_runs, rbi, walks, strikeouts,
                batting_avg, obp, slg, ops, iso, babip, wrc_plus
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            player_id, season, split,
            row.get("PA"), row.get("AB"),
            row.get("H"), row.get("2B"), row.get("3B"),
            row.get("HR"), row.get("RBI"),
            row.get("BB"), row.get("SO"),
            row.get("AVG"), row.get("OBP"), row.get("SLG"), row.get("OPS"),
            row.get("ISO"), row.get("BABIP"),
            row.get("wRC+"),
        ))
        rows_inserted += 1

    conn.commit()
    print(f"  Loaded {rows_inserted} split rows")


def get_qualified_player_ids(conn, season, min_pa=400):
    """Get player IDs for batters with enough plate appearances."""
    cursor = conn.cursor()
    cursor.execute(
        "SELECT player_id FROM season_batting_stats WHERE season = ? AND plate_appearances >= ?",
        (season, min_pa),
    )
    return [row[0] for row in cursor.fetchall()]


def pull_game_logs_for_player(player_id, season):
    """Pull game log from FanGraphs API for a single player-season."""
    url = "https://www.fangraphs.com/api/players/game-log"
    params = {
        "playerid": player_id,
        "position": "",
        "type": "0",
        "season": str(season),
    }
    resp = requests.get(url, params=params)
    resp.raise_for_status()
    data = resp.json()
    games = data.get("mlb", [])
    # First row is season totals (date contains "2050"), skip it
    return [g for g in games if "2050" not in str(g.get("Date", ""))]


def pull_and_load_game_logs(conn, start_season, end_season):
    """Pull game logs for qualified batters and load into SQLite."""
    print(f"Pulling game logs for {start_season}-{end_season}...")
    cursor = conn.cursor()
    total_games = 0

    for season in range(start_season, end_season + 1):
        player_ids = get_qualified_player_ids(conn, season)
        print(f"  {season}: {len(player_ids)} qualified batters")

        for i, player_id in enumerate(player_ids):
            try:
                games = pull_game_logs_for_player(player_id, season)
                for g in games:
                    # Parse date from HTML link
                    raw_date = str(g.get("Date", ""))
                    match = re.search(r'date=(\d{4}-\d{2}-\d{2})', raw_date)
                    if not match:
                        continue
                    date = match.group(1)

                    cursor.execute("""
                        INSERT OR REPLACE INTO game_batting_logs (
                            player_id, season, date, opponent,
                            plate_appearances, at_bats, hits, doubles, triples,
                            home_runs, runs, rbi, walks, strikeouts,
                            batting_avg, obp, slg, ops
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        player_id, season, date, g.get("Opp"),
                        g.get("PA"), g.get("AB"), g.get("H"),
                        g.get("2B"), g.get("3B"), g.get("HR"),
                        g.get("R"), g.get("RBI"), g.get("BB"), g.get("SO"),
                        g.get("AVG"), g.get("OBP"), g.get("SLG"), g.get("OPS"),
                    ))
                    total_games += 1

                if (i + 1) % 50 == 0:
                    conn.commit()
                    print(f"    Pulled {i + 1}/{len(player_ids)} players ({total_games} games)...")

                # Rate limiting: small delay between requests
                time.sleep(0.2)

            except Exception as e:
                print(f"    Warning: Failed for player {player_id} in {season}: {e}")

        conn.commit()

    print(f"  Loaded {total_games} game log rows total")


def pull_and_load(start_season, end_season):
    """Pull all data and load into SQLite."""
    conn = sqlite3.connect(DB_PATH)
    create_tables(conn)

    # Season stats via pybaseball
    season_data = pull_season_stats(start_season, end_season)
    load_season_stats(conn, season_data)

    # Platoon splits via FanGraphs API
    splits_data = pull_all_splits(start_season, end_season)
    load_splits(conn, splits_data)

    # Game logs for qualified batters via FanGraphs API
    pull_and_load_game_logs(conn, start_season, end_season)

    conn.close()
    print(f"\nDone! Database saved to: {DB_PATH}")


if __name__ == "__main__":
    start = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_START
    end = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_END
    pull_and_load(start, end)
