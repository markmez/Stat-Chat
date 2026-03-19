"""
Data pipeline: Pull batting stats from Retrosheet into SQLite.

Uses commercially-viable data sources:
- Retrosheet (free, commercial OK) for season stats, game-level batting logs, and player info
- Chadwick Bureau retrosplits (ODbL) for platoon splits (vs LHP/RHP)

For historical expansion, Lahman Database (CC BY-SA 3.0) can be added for 1871-1897 seasons.

Usage:
    python3 pull_stats.py                  # Pull 2024-2025 (default)
    python3 pull_stats.py 2020 2025        # Pull 2020-2025
    python3 pull_stats.py 1898 2025        # Pull all available Retrosheet history

Data attribution:
    The information used here was obtained free of charge from and is copyrighted
    by Retrosheet. Interested parties may contact Retrosheet at www.retrosheet.org.
"""

import io
import os
import sqlite3
import sys
import time
import zipfile

import pandas as pd
import requests


DEFAULT_START = 2024
DEFAULT_END = 2025

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(os.path.dirname(__file__)), "baseball_stats.db"))
CACHE_DIR = os.path.join(os.path.dirname(__file__), ".cache")

# Retrosheet CSV download URLs
RETROSHEET_SEASON_URL = "https://www.retrosheet.org/downloads/{year}/{year}csvs.zip"

# Chadwick Bureau retrosplits (GitHub raw)
RETROSPLITS_URL = "https://raw.githubusercontent.com/chadwickbureau/retrosplits/master/splits/batting-platoon-{year}.csv"
PITCHING_RETROSPLITS_URL = "https://raw.githubusercontent.com/chadwickbureau/retrosplits/master/splits/pitching-platoon-{year}.csv"


def create_tables(conn):
    """Create the SQLite schema."""
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS players (
            player_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            team TEXT,
            positions TEXT,
            birthdate TEXT,
            bats TEXT,
            throws TEXT,
            career_games INTEGER DEFAULT 0,
            last_season INTEGER DEFAULT 0
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
            ops_plus INTEGER,
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
        CREATE TABLE IF NOT EXISTS home_away_splits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id TEXT NOT NULL,
            season INTEGER NOT NULL,
            split TEXT NOT NULL,
            games INTEGER,
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
            hit_by_pitch INTEGER,
            sacrifice_flies INTEGER,
            batting_avg REAL,
            obp REAL,
            slg REAL,
            ops REAL,
            iso REAL,
            babip REAL,
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
            vishome TEXT,
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

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS league_averages (
            season INTEGER PRIMARY KEY,
            total_pa INTEGER,
            total_ab INTEGER,
            total_hits INTEGER,
            total_doubles INTEGER,
            total_triples INTEGER,
            total_hr INTEGER,
            total_bb INTEGER,
            total_hbp INTEGER,
            total_sf INTEGER,
            total_so INTEGER,
            league_avg REAL,
            league_obp REAL,
            league_slg REAL,
            league_ops REAL,
            league_iso REAL,
            league_babip REAL
        )
    """)

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_stats_player ON season_batting_stats(player_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_stats_season ON season_batting_stats(season)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_stats_player_season ON season_batting_stats(player_id, season)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_splits_player ON platoon_splits(player_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_splits_player_season ON platoon_splits(player_id, season)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_splits_split ON platoon_splits(split)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_ha_player ON home_away_splits(player_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_ha_player_season ON home_away_splits(player_id, season)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_gamelogs_player ON game_batting_logs(player_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_gamelogs_player_season ON game_batting_logs(player_id, season)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_gamelogs_date ON game_batting_logs(date)")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS season_fielding_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id TEXT NOT NULL,
            season INTEGER NOT NULL,
            position TEXT NOT NULL,
            games INTEGER,
            games_started INTEGER,
            innings REAL,
            putouts INTEGER,
            assists INTEGER,
            errors INTEGER,
            double_plays INTEGER,
            passed_balls INTEGER,
            fielding_pct REAL,
            FOREIGN KEY (player_id) REFERENCES players(player_id),
            UNIQUE(player_id, season, position)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_fielding_player ON season_fielding_stats(player_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_fielding_player_season ON season_fielding_stats(player_id, season)")

    # --- Pitching tables ---

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS season_pitching_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id TEXT NOT NULL,
            season INTEGER NOT NULL,
            team TEXT,
            games INTEGER,
            games_started INTEGER,
            games_finished INTEGER,
            complete_games INTEGER,
            wins INTEGER,
            losses INTEGER,
            saves INTEGER,
            ip_outs INTEGER,
            innings_pitched TEXT,
            hits INTEGER,
            runs INTEGER,
            earned_runs INTEGER,
            home_runs INTEGER,
            walks INTEGER,
            intentional_walks INTEGER,
            strikeouts INTEGER,
            hit_by_pitch INTEGER,
            wild_pitches INTEGER,
            balks INTEGER,
            batters_faced INTEGER,
            sacrifice_hits INTEGER,
            sacrifice_flies INTEGER,
            stolen_bases INTEGER,
            caught_stealing INTEGER,
            quality_starts INTEGER,
            era REAL,
            whip REAL,
            k_per_9 REAL,
            bb_per_9 REAL,
            k_per_bb REAL,
            h_per_9 REAL,
            hr_per_9 REAL,
            baa REAL,
            era_plus INTEGER,
            FOREIGN KEY (player_id) REFERENCES players(player_id),
            UNIQUE(player_id, season, team)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_pitching_player ON season_pitching_stats(player_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_pitching_season ON season_pitching_stats(season)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_pitching_player_season ON season_pitching_stats(player_id, season)")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS game_pitching_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id TEXT NOT NULL,
            season INTEGER NOT NULL,
            date TEXT NOT NULL,
            opponent TEXT,
            vishome TEXT,
            is_start INTEGER,
            ip_outs INTEGER,
            innings_pitched TEXT,
            hits INTEGER,
            runs INTEGER,
            earned_runs INTEGER,
            home_runs INTEGER,
            walks INTEGER,
            strikeouts INTEGER,
            hit_by_pitch INTEGER,
            batters_faced INTEGER,
            win INTEGER,
            loss INTEGER,
            save INTEGER,
            era REAL,
            FOREIGN KEY (player_id) REFERENCES players(player_id),
            UNIQUE(player_id, season, date)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_pitchlogs_player ON game_pitching_logs(player_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_pitchlogs_player_season ON game_pitching_logs(player_id, season)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_pitchlogs_date ON game_pitching_logs(date)")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pitching_platoon_splits (
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
            walks INTEGER,
            intentional_walks INTEGER,
            strikeouts INTEGER,
            hit_by_pitch INTEGER,
            sacrifice_hits INTEGER,
            sacrifice_flies INTEGER,
            batting_avg_against REAL,
            obp_against REAL,
            slg_against REAL,
            ops_against REAL,
            FOREIGN KEY (player_id) REFERENCES players(player_id),
            UNIQUE(player_id, season, split)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_pitchsplits_player ON pitching_platoon_splits(player_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_pitchsplits_player_season ON pitching_platoon_splits(player_id, season)")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pitching_home_away_splits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id TEXT NOT NULL,
            season INTEGER NOT NULL,
            split TEXT NOT NULL,
            games INTEGER,
            games_started INTEGER,
            ip_outs INTEGER,
            innings_pitched TEXT,
            hits INTEGER,
            earned_runs INTEGER,
            home_runs INTEGER,
            walks INTEGER,
            strikeouts INTEGER,
            era REAL,
            whip REAL,
            k_per_9 REAL,
            bb_per_9 REAL,
            baa REAL,
            FOREIGN KEY (player_id) REFERENCES players(player_id),
            UNIQUE(player_id, season, split)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_pitchha_player ON pitching_home_away_splits(player_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_pitchha_player_season ON pitching_home_away_splits(player_id, season)")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS league_pitching_averages (
            season INTEGER PRIMARY KEY,
            total_ip_outs INTEGER,
            total_er INTEGER,
            total_h INTEGER,
            total_bb INTEGER,
            total_so INTEGER,
            total_hr INTEGER,
            total_bf INTEGER,
            league_era REAL,
            league_whip REAL,
            league_k_per_9 REAL,
            league_bb_per_9 REAL,
            league_baa REAL
        )
    """)

    conn.commit()


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def ensure_cache_dir():
    """Create cache directory if it doesn't exist."""
    os.makedirs(CACHE_DIR, exist_ok=True)


def safe_int(val, default=0):
    """Convert to int, treating NaN/None as default."""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return default
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def compute_rate_stats(h, ab, bb, hbp, sf, doubles, triples, hr, so):
    """Compute AVG, OBP, SLG, OPS, ISO, BABIP from counting stats."""
    avg = h / ab if ab > 0 else None
    obp_denom = ab + bb + hbp + sf
    obp = (h + bb + hbp) / obp_denom if obp_denom > 0 else None
    tb = h + doubles + 2 * triples + 3 * hr
    slg = tb / ab if ab > 0 else None
    ops = (obp or 0) + (slg or 0) if obp is not None or slg is not None else None
    iso = slg - avg if slg is not None and avg is not None else None
    babip_denom = ab - so - hr + sf
    babip = (h - hr) / babip_denom if babip_denom > 0 and h >= hr else None
    return {
        "avg": round(avg, 3) if avg is not None else None,
        "obp": round(obp, 3) if obp is not None else None,
        "slg": round(slg, 3) if slg is not None else None,
        "ops": round(ops, 3) if ops is not None else None,
        "iso": round(iso, 3) if iso is not None else None,
        "babip": round(babip, 3) if babip is not None else None,
    }


def format_date(raw):
    """Convert YYYYMMDD to YYYY-MM-DD."""
    s = str(raw).strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s.replace("/", "-")


# ---------------------------------------------------------------------------
# Retrosheet ZIP download + extraction
# ---------------------------------------------------------------------------

def download_retrosheet_zip(season):
    """Download a Retrosheet season ZIP and return ZipFile object."""
    ensure_cache_dir()
    cache_path = os.path.join(CACHE_DIR, f"retrosheet_{season}.zip")

    if os.path.exists(cache_path):
        return zipfile.ZipFile(cache_path)

    url = RETROSHEET_SEASON_URL.format(year=season)
    print(f"  Downloading Retrosheet {season} from {url}...")
    resp = requests.get(url, timeout=120)
    resp.raise_for_status()

    with open(cache_path, "wb") as f:
        f.write(resp.content)

    return zipfile.ZipFile(cache_path)


def read_csv_from_zip(zf, pattern):
    """Find and read a CSV matching pattern from a ZipFile."""
    for name in zf.namelist():
        if pattern in name.lower():
            with zf.open(name) as f:
                return pd.read_csv(f)
    return None


# ---------------------------------------------------------------------------
# Phase 1: Players + Season Stats (from Retrosheet batting + allplayers)
# ---------------------------------------------------------------------------

def load_players_and_season_stats(conn, start_season, end_season):
    """Load player info and season batting stats from Retrosheet CSVs."""
    print(f"Phase 1: Loading players + season stats for {start_season}-{end_season}...")
    cursor = conn.cursor()
    total_players = 0
    total_stats = 0

    for season in range(start_season, end_season + 1):
        try:
            zf = download_retrosheet_zip(season)
        except Exception as e:
            print(f"  Warning: Failed to download {season}: {e}")
            continue

        # --- Load player info from allplayers.csv ---
        allplayers = read_csv_from_zip(zf, "allplayers.csv")
        if allplayers is not None:
            # De-duplicate: keep last row per player ID (most games = primary team)
            allplayers_dedup = allplayers.sort_values("g", ascending=False).drop_duplicates("id", keep="first")
            for _, row in allplayers_dedup.iterrows():
                pid = str(row.get("id", ""))
                if not pid:
                    continue
                first = str(row.get("first", "")) if pd.notna(row.get("first")) else ""
                last = str(row.get("last", "")) if pd.notna(row.get("last")) else ""
                name = f"{first} {last}".strip()
                team = str(row.get("team", "")) if pd.notna(row.get("team")) else ""

                # Determine positions sorted by games played DESC (primary position first)
                pos_cols = ["g_c", "g_1b", "g_2b", "g_3b", "g_ss", "g_lf", "g_cf", "g_rf", "g_dh", "g_p"]
                pos_names = ["C", "1B", "2B", "3B", "SS", "LF", "CF", "RF", "DH", "P"]
                pos_games = []
                for col, pos_name in zip(pos_cols, pos_names):
                    g = safe_int(row.get(col))
                    if g > 0:
                        pos_games.append((g, pos_name))
                pos_games.sort(key=lambda x: x[0], reverse=True)
                positions = [p for _, p in pos_games]
                pos_str = "/".join(positions) if positions else None

                bats = str(row.get("bat", "")) if pd.notna(row.get("bat")) else None
                throws = str(row.get("throw", "")) if pd.notna(row.get("throw")) else None

                cursor.execute(
                    "INSERT OR REPLACE INTO players (player_id, name, team, positions, bats, throws) VALUES (?, ?, ?, ?, ?, ?)",
                    (pid, name, team, pos_str, bats, throws),
                )
                total_players += 1

        # --- Aggregate batting.csv into season stats ---
        batting = read_csv_from_zip(zf, "batting.csv")
        if batting is not None:
            # Filter to regular season, stat type = value
            if "gametype" in batting.columns:
                batting = batting[batting["gametype"] == "regular"]
            if "stattype" in batting.columns:
                batting = batting[batting["stattype"] == "value"]

            # Aggregate by player ID across all games in the season
            agg = batting.groupby("id").agg(
                team=("team", lambda x: "/".join(dict.fromkeys(x))),
                games=("id", "count"),
                pa=("b_pa", "sum"),
                ab=("b_ab", "sum"),
                r=("b_r", "sum"),
                h=("b_h", "sum"),
                doubles=("b_d", "sum"),
                triples=("b_t", "sum"),
                hr=("b_hr", "sum"),
                rbi=("b_rbi", "sum"),
                sb=("b_sb", "sum"),
                cs=("b_cs", "sum"),
                bb=("b_w", "sum"),
                so=("b_k", "sum"),
                hbp=("b_hbp", "sum"),
                sf=("b_sf", "sum"),
                ibb=("b_iw", "sum"),
                sh=("b_sh", "sum"),
            ).reset_index()

            # Clear stale rows for this season (team strings may have changed)
            cursor.execute("DELETE FROM season_batting_stats WHERE season = ?", (season,))

            season_rows = 0
            for _, row in agg.iterrows():
                pid = str(row["id"])
                ab = safe_int(row["ab"])
                h = safe_int(row["h"])
                doubles = safe_int(row["doubles"])
                triples = safe_int(row["triples"])
                hr = safe_int(row["hr"])
                bb = safe_int(row["bb"])
                so = safe_int(row["so"])
                hbp = safe_int(row["hbp"])
                sf = safe_int(row["sf"])

                rates = compute_rate_stats(h, ab, bb, hbp, sf, doubles, triples, hr, so)

                cursor.execute("""
                    INSERT OR REPLACE INTO season_batting_stats (
                        player_id, season, team, age, games, plate_appearances,
                        at_bats, hits, doubles, triples, home_runs, runs, rbi,
                        stolen_bases, caught_stealing, walks, strikeouts,
                        hit_by_pitch, sacrifice_flies, intentional_walks,
                        batting_avg, obp, slg, ops, iso, babip,
                        ops_plus, wrc_plus, war
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    pid, season, str(row["team"]),
                    None,  # age not available from Retrosheet batting
                    safe_int(row["games"]),
                    safe_int(row["pa"]),
                    ab, h, doubles, triples, hr,
                    safe_int(row["r"]), safe_int(row["rbi"]),
                    safe_int(row["sb"]), safe_int(row["cs"]),
                    bb, so, hbp, sf, safe_int(row["ibb"]),
                    rates["avg"], rates["obp"], rates["slg"], rates["ops"],
                    rates["iso"], rates["babip"],
                    None, None, None,  # OPS+, wRC+, WAR filled later / not available
                ))
                season_rows += 1

            conn.commit()
            total_stats += season_rows
            print(f"  {season}: {season_rows} season stat rows")

        time.sleep(0.5)

    # Update players.team to most recent team (last component if multi-team like "MIA/NYA" → "NYA")
    cursor.execute("""
        SELECT player_id, team FROM season_batting_stats
        WHERE (player_id, season) IN (
            SELECT player_id, MAX(season) FROM season_batting_stats GROUP BY player_id
        )
    """)
    for pid, team_str in cursor.fetchall():
        current_team = team_str.split("/")[-1] if "/" in team_str else team_str
        cursor.execute("UPDATE players SET team = ? WHERE player_id = ?", (current_team, pid))
    conn.commit()

    print(f"  Loaded {total_players} players, {total_stats} season stat rows total")


# ---------------------------------------------------------------------------
# Phase 2: Game-level batting logs (from Retrosheet batting.csv)
# ---------------------------------------------------------------------------

def load_game_logs(conn, start_season, end_season):
    """Load game-level batting logs from Retrosheet CSVs."""
    print(f"Phase 2: Loading game logs for {start_season}-{end_season}...")
    cursor = conn.cursor()
    total_games = 0

    for season in range(start_season, end_season + 1):
        try:
            zf = download_retrosheet_zip(season)
        except Exception as e:
            print(f"  Warning: Failed to download {season}: {e}")
            continue

        batting = read_csv_from_zip(zf, "batting.csv")
        if batting is None:
            continue

        # Filter to regular season, stat type = value
        if "gametype" in batting.columns:
            batting = batting[batting["gametype"] == "regular"]
        if "stattype" in batting.columns:
            batting = batting[batting["stattype"] == "value"]

        season_rows = 0
        for _, row in batting.iterrows():
            pid = str(row.get("id", ""))
            if not pid:
                continue

            ab = safe_int(row.get("b_ab"))
            h = safe_int(row.get("b_h"))
            doubles = safe_int(row.get("b_d"))
            triples = safe_int(row.get("b_t"))
            hr = safe_int(row.get("b_hr"))
            r = safe_int(row.get("b_r"))
            rbi = safe_int(row.get("b_rbi"))
            bb = safe_int(row.get("b_w"))
            so = safe_int(row.get("b_k"))
            pa = safe_int(row.get("b_pa"))
            hbp = safe_int(row.get("b_hbp"))
            sf = safe_int(row.get("b_sf"))

            date = format_date(row.get("date", ""))
            opp = str(row.get("opp", "")) if pd.notna(row.get("opp")) else None
            vishome = str(row.get("vishome", "")).upper() if pd.notna(row.get("vishome")) else None

            rates = compute_rate_stats(h, ab, bb, hbp, sf, doubles, triples, hr, so)

            cursor.execute("""
                INSERT OR REPLACE INTO game_batting_logs (
                    player_id, season, date, opponent, vishome,
                    plate_appearances, at_bats, hits, doubles, triples,
                    home_runs, runs, rbi, walks, strikeouts,
                    batting_avg, obp, slg, ops
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                pid, season, date, opp, vishome,
                pa, ab, h, doubles, triples, hr, r, rbi, bb, so,
                rates["avg"], rates["obp"], rates["slg"], rates["ops"],
            ))
            season_rows += 1

        conn.commit()
        total_games += season_rows
        print(f"  {season}: {season_rows} game log rows")

    print(f"  Loaded {total_games} game log rows total")


# ---------------------------------------------------------------------------
# Phase 2b: Home/Away splits (aggregated from Retrosheet batting.csv)
# ---------------------------------------------------------------------------

def load_home_away_splits(conn, start_season, end_season):
    """Aggregate batting.csv by player + vishome to create home/away splits."""
    print(f"Phase 2b: Loading home/away splits for {start_season}-{end_season}...")
    cursor = conn.cursor()
    total_splits = 0

    for season in range(start_season, end_season + 1):
        try:
            zf = download_retrosheet_zip(season)
        except Exception as e:
            print(f"  Warning: Failed to download {season}: {e}")
            continue

        batting = read_csv_from_zip(zf, "batting.csv")
        if batting is None:
            continue

        # Filter to regular season, stat type = value
        if "gametype" in batting.columns:
            batting = batting[batting["gametype"] == "regular"]
        if "stattype" in batting.columns:
            batting = batting[batting["stattype"] == "value"]

        # Need vishome column
        if "vishome" not in batting.columns:
            print(f"  {season}: no vishome column in batting.csv, skipping")
            continue

        # Normalize vishome to uppercase and drop rows without valid values
        batting["vishome"] = batting["vishome"].astype(str).str.upper()
        batting = batting[batting["vishome"].isin(["H", "V"])]

        # Group by player + vishome
        counting_cols = ["b_pa", "b_ab", "b_h", "b_d", "b_t", "b_hr",
                         "b_r", "b_rbi", "b_w", "b_k", "b_hbp", "b_sf"]
        for col in counting_cols:
            if col in batting.columns:
                batting[col] = batting[col].fillna(0).astype(int)

        grouped = batting.groupby(["id", "vishome"]).agg(
            games=("id", "count"),
            pa=("b_pa", "sum"),
            ab=("b_ab", "sum"),
            h=("b_h", "sum"),
            doubles=("b_d", "sum"),
            triples=("b_t", "sum"),
            hr=("b_hr", "sum"),
            r=("b_r", "sum"),
            rbi=("b_rbi", "sum"),
            bb=("b_w", "sum"),
            so=("b_k", "sum"),
            hbp=("b_hbp", "sum"),
            sf=("b_sf", "sum"),
        ).reset_index()

        season_rows = 0
        for _, row in grouped.iterrows():
            pid = str(row["id"])
            vh = str(row["vishome"])
            split = "home" if vh == "H" else "away"

            games = safe_int(row["games"])
            pa = safe_int(row["pa"])
            ab = safe_int(row["ab"])
            h = safe_int(row["h"])
            doubles = safe_int(row["doubles"])
            triples = safe_int(row["triples"])
            hr = safe_int(row["hr"])
            r = safe_int(row["r"])
            rbi = safe_int(row["rbi"])
            bb = safe_int(row["bb"])
            so = safe_int(row["so"])
            hbp = safe_int(row["hbp"])
            sf = safe_int(row["sf"])

            rates = compute_rate_stats(h, ab, bb, hbp, sf, doubles, triples, hr, so)

            cursor.execute("""
                INSERT OR REPLACE INTO home_away_splits (
                    player_id, season, split, games, plate_appearances, at_bats,
                    hits, doubles, triples, home_runs, runs, rbi,
                    walks, strikeouts, hit_by_pitch, sacrifice_flies,
                    batting_avg, obp, slg, ops, iso, babip
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                pid, season, split, games, pa, ab,
                h, doubles, triples, hr, r, rbi,
                bb, so, hbp, sf,
                rates["avg"], rates["obp"], rates["slg"], rates["ops"],
                rates["iso"], rates["babip"],
            ))
            season_rows += 1

        conn.commit()
        total_splits += season_rows
        print(f"  {season}: {season_rows} home/away split rows")

    print(f"  Loaded {total_splits} home/away split rows total")


# ---------------------------------------------------------------------------
# Phase 2c: Fielding stats (from Retrosheet fielding.csv)
# ---------------------------------------------------------------------------

POS_MAP = {1: "P", 2: "C", 3: "1B", 4: "2B", 5: "3B", 6: "SS", 7: "LF", 8: "CF", 9: "RF"}


def load_fielding_stats(conn, start_season, end_season):
    """Aggregate fielding.csv into per-player, per-position season stats."""
    print(f"Phase 2c: Loading fielding stats for {start_season}-{end_season}...")
    cursor = conn.cursor()
    total_rows = 0

    for season in range(start_season, end_season + 1):
        try:
            zf = download_retrosheet_zip(season)
        except Exception as e:
            print(f"  Warning: Failed to download {season}: {e}")
            continue

        fielding = read_csv_from_zip(zf, "fielding.csv")
        if fielding is None:
            continue

        # Filter to regular season, stat type = value
        if "gametype" in fielding.columns:
            fielding = fielding[fielding["gametype"] == "regular"]
        if "stattype" in fielding.columns:
            fielding = fielding[fielding["stattype"] == "value"]

        # Map position codes to names, drop unmapped (e.g. DH=0)
        fielding = fielding[fielding["d_pos"].isin(POS_MAP.keys())].copy()
        fielding["position"] = fielding["d_pos"].map(POS_MAP)

        # Fill NaN in counting columns
        counting_cols = ["d_gs", "d_ifouts", "d_po", "d_a", "d_e", "d_dp", "d_pb"]
        for col in counting_cols:
            if col in fielding.columns:
                fielding[col] = fielding[col].fillna(0).astype(int)

        # Aggregate by player + position
        grouped = fielding.groupby(["id", "position"]).agg(
            games=("id", "count"),
            gs=("d_gs", "sum"),
            ifouts=("d_ifouts", "sum"),
            po=("d_po", "sum"),
            a=("d_a", "sum"),
            e=("d_e", "sum"),
            dp=("d_dp", "sum"),
            pb=("d_pb", "sum"),
        ).reset_index()

        # Clear stale rows for this season
        cursor.execute("DELETE FROM season_fielding_stats WHERE season = ?", (season,))

        season_rows = 0
        for _, row in grouped.iterrows():
            pid = str(row["id"])
            pos = str(row["position"])
            games = safe_int(row["games"])
            if games == 0:
                continue

            gs = safe_int(row["gs"])
            ifouts = safe_int(row["ifouts"])
            innings = ifouts / 3.0
            po = safe_int(row["po"])
            a = safe_int(row["a"])
            e = safe_int(row["e"])
            dp = safe_int(row["dp"])
            pb = safe_int(row["pb"])
            total = po + a + e
            fpct = round((po + a) / total, 3) if total > 0 else None

            cursor.execute("""
                INSERT OR REPLACE INTO season_fielding_stats (
                    player_id, season, position, games, games_started,
                    innings, putouts, assists, errors, double_plays,
                    passed_balls, fielding_pct
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                pid, season, pos, games, gs,
                round(innings, 1), po, a, e, dp, pb, fpct,
            ))
            season_rows += 1

        conn.commit()
        total_rows += season_rows
        print(f"  {season}: {season_rows} fielding stat rows")

    print(f"  Loaded {total_rows} fielding stat rows total")


# ---------------------------------------------------------------------------
# Pitching: Season stats, game logs, home/away splits
# ---------------------------------------------------------------------------

def format_ip(ip_outs):
    """Format innings pitched from outs: 19 outs → '6.1', 20 → '6.2', 21 → '7.0'."""
    full = ip_outs // 3
    remainder = ip_outs % 3
    return f"{full}.{remainder}"


def compute_pitching_rate_stats(ip_outs, h, er, bb, so, hr, bf):
    """Compute ERA, WHIP, K/9, BB/9, K/BB, H/9, HR/9, BAA from counting stats."""
    ip = ip_outs / 3.0 if ip_outs > 0 else 0
    era = 9.0 * er / ip if ip > 0 else None
    whip = (bb + h) / ip if ip > 0 else None
    k_per_9 = 9.0 * so / ip if ip > 0 else None
    bb_per_9 = 9.0 * bb / ip if ip > 0 else None
    k_per_bb = so / bb if bb > 0 else None
    h_per_9 = 9.0 * h / ip if ip > 0 else None
    hr_per_9 = 9.0 * hr / ip if ip > 0 else None
    # BAA: hits / (batters faced - walks - HBP - SH - SF)
    # Simplified: hits / at_bats_against. We approximate AB = BF - BB - HBP - SH - SF
    # but we don't always have HBP/SH/SF breakdown here, so use BF - BB as rough AB
    ab_approx = bf - bb if bf > bb else bf
    baa = h / ab_approx if ab_approx > 0 else None
    return {
        "era": round(era, 2) if era is not None else None,
        "whip": round(whip, 2) if whip is not None else None,
        "k_per_9": round(k_per_9, 1) if k_per_9 is not None else None,
        "bb_per_9": round(bb_per_9, 1) if bb_per_9 is not None else None,
        "k_per_bb": round(k_per_bb, 2) if k_per_bb is not None else None,
        "h_per_9": round(h_per_9, 1) if h_per_9 is not None else None,
        "hr_per_9": round(hr_per_9, 1) if hr_per_9 is not None else None,
        "baa": round(baa, 3) if baa is not None else None,
    }


def load_pitching_season_stats(conn, start_season, end_season):
    """Aggregate pitching.csv into season pitching stats per player."""
    print(f"Pitching Phase 1: Loading pitching season stats for {start_season}-{end_season}...")
    cursor = conn.cursor()
    total_stats = 0

    for season in range(start_season, end_season + 1):
        try:
            zf = download_retrosheet_zip(season)
        except Exception as e:
            print(f"  Warning: Failed to download {season}: {e}")
            continue

        pitching = read_csv_from_zip(zf, "pitching.csv")
        if pitching is None:
            continue

        if "gametype" in pitching.columns:
            pitching = pitching[pitching["gametype"] == "regular"]
        if "stattype" in pitching.columns:
            pitching = pitching[pitching["stattype"] == "value"]

        # Fill NaN in counting columns and flag columns
        counting_cols = ["p_ipouts", "p_bfp", "p_h", "p_d", "p_t", "p_hr", "p_r", "p_er",
                         "p_w", "p_iw", "p_k", "p_hbp", "p_wp", "p_bk", "p_sh", "p_sf",
                         "p_sb", "p_cs", "p_gs", "p_gf", "p_cg"]
        for col in counting_cols:
            if col in pitching.columns:
                pitching[col] = pitching[col].fillna(0).astype(int)

        flag_cols = ["wp", "lp", "save"]
        for col in flag_cols:
            if col in pitching.columns:
                pitching[col] = pitching[col].fillna(0).astype(int)

        # Compute quality starts per game row: >= 18 outs (6 IP) and <= 3 ER
        pitching["qs"] = ((pitching["p_ipouts"] >= 18) & (pitching["p_er"] <= 3)).astype(int)

        # Aggregate by player ID
        agg = pitching.groupby("id").agg(
            team=("team", lambda x: "/".join(dict.fromkeys(x))),
            games=("id", "count"),
            gs=("p_gs", "sum"),
            gf=("p_gf", "sum"),
            cg=("p_cg", "sum"),
            wins=("wp", "sum"),
            losses=("lp", "sum"),
            saves=("save", "sum"),
            ip_outs=("p_ipouts", "sum"),
            h=("p_h", "sum"),
            r=("p_r", "sum"),
            er=("p_er", "sum"),
            hr=("p_hr", "sum"),
            bb=("p_w", "sum"),
            ibb=("p_iw", "sum"),
            so=("p_k", "sum"),
            hbp=("p_hbp", "sum"),
            wp=("p_wp", "sum"),
            bk=("p_bk", "sum"),
            bf=("p_bfp", "sum"),
            sh=("p_sh", "sum"),
            sf=("p_sf", "sum"),
            sb=("p_sb", "sum"),
            cs=("p_cs", "sum"),
            qs=("qs", "sum"),
        ).reset_index()

        cursor.execute("DELETE FROM season_pitching_stats WHERE season = ?", (season,))

        season_rows = 0
        for _, row in agg.iterrows():
            pid = str(row["id"])
            ip_outs = safe_int(row["ip_outs"])
            h = safe_int(row["h"])
            er = safe_int(row["er"])
            bb = safe_int(row["bb"])
            so = safe_int(row["so"])
            hr = safe_int(row["hr"])
            bf = safe_int(row["bf"])
            hbp = safe_int(row["hbp"])
            sh_val = safe_int(row["sh"])
            sf_val = safe_int(row["sf"])

            rates = compute_pitching_rate_stats(ip_outs, h, er, bb, so, hr, bf)

            # Better BAA: use proper at-bats = BF - BB - HBP - SH - SF
            ab_against = bf - bb - hbp - sh_val - sf_val
            baa = round(h / ab_against, 3) if ab_against > 0 else None

            cursor.execute("""
                INSERT OR REPLACE INTO season_pitching_stats (
                    player_id, season, team, games, games_started, games_finished,
                    complete_games, wins, losses, saves, ip_outs, innings_pitched,
                    hits, runs, earned_runs, home_runs, walks, intentional_walks,
                    strikeouts, hit_by_pitch, wild_pitches, balks, batters_faced,
                    sacrifice_hits, sacrifice_flies, stolen_bases, caught_stealing,
                    quality_starts, era, whip, k_per_9, bb_per_9, k_per_bb,
                    h_per_9, hr_per_9, baa, era_plus
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                pid, season, str(row["team"]),
                safe_int(row["games"]), safe_int(row["gs"]), safe_int(row["gf"]),
                safe_int(row["cg"]), safe_int(row["wins"]), safe_int(row["losses"]),
                safe_int(row["saves"]), ip_outs, format_ip(ip_outs),
                h, safe_int(row["r"]), er, hr, bb, safe_int(row["ibb"]),
                so, hbp, safe_int(row["wp"]), safe_int(row["bk"]), bf,
                sh_val, sf_val, safe_int(row["sb"]), safe_int(row["cs"]),
                safe_int(row["qs"]),
                rates["era"], rates["whip"], rates["k_per_9"], rates["bb_per_9"],
                rates["k_per_bb"], rates["h_per_9"], rates["hr_per_9"],
                baa,
                None,  # ERA+ filled later
            ))
            season_rows += 1

        conn.commit()
        total_stats += season_rows
        print(f"  {season}: {season_rows} pitching season stat rows")

    print(f"  Loaded {total_stats} pitching season stat rows total")


def load_pitching_game_logs(conn, start_season, end_season):
    """Load game-level pitching logs from Retrosheet pitching.csv."""
    print(f"Pitching Phase 2: Loading pitching game logs for {start_season}-{end_season}...")
    cursor = conn.cursor()
    total_games = 0

    for season in range(start_season, end_season + 1):
        try:
            zf = download_retrosheet_zip(season)
        except Exception as e:
            print(f"  Warning: Failed to download {season}: {e}")
            continue

        pitching = read_csv_from_zip(zf, "pitching.csv")
        if pitching is None:
            continue

        if "gametype" in pitching.columns:
            pitching = pitching[pitching["gametype"] == "regular"]
        if "stattype" in pitching.columns:
            pitching = pitching[pitching["stattype"] == "value"]

        season_rows = 0
        for _, row in pitching.iterrows():
            pid = str(row.get("id", ""))
            if not pid:
                continue

            ip_outs = safe_int(row.get("p_ipouts"))
            h = safe_int(row.get("p_h"))
            er = safe_int(row.get("p_er"))
            bb = safe_int(row.get("p_w"))
            so = safe_int(row.get("p_k"))
            hr = safe_int(row.get("p_hr"))
            bf = safe_int(row.get("p_bfp"))
            hbp = safe_int(row.get("p_hbp"))
            r = safe_int(row.get("p_r"))
            is_start = 1 if safe_int(row.get("p_gs")) == 1 else 0
            win = 1 if safe_int(row.get("wp")) == 1 else 0
            loss = 1 if safe_int(row.get("lp")) == 1 else 0
            sv = 1 if safe_int(row.get("save")) == 1 else 0

            date = format_date(row.get("date", ""))
            opp = str(row.get("opp", "")) if pd.notna(row.get("opp")) else None
            vishome = str(row.get("vishome", "")).upper() if pd.notna(row.get("vishome")) else None

            # Per-game ERA
            ip = ip_outs / 3.0
            game_era = round(9.0 * er / ip, 2) if ip > 0 else None

            cursor.execute("""
                INSERT OR REPLACE INTO game_pitching_logs (
                    player_id, season, date, opponent, vishome, is_start,
                    ip_outs, innings_pitched, hits, runs, earned_runs,
                    home_runs, walks, strikeouts, hit_by_pitch, batters_faced,
                    win, loss, save, era
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                pid, season, date, opp, vishome, is_start,
                ip_outs, format_ip(ip_outs), h, r, er,
                hr, bb, so, hbp, bf,
                win, loss, sv, game_era,
            ))
            season_rows += 1

        conn.commit()
        total_games += season_rows
        print(f"  {season}: {season_rows} pitching game log rows")

    print(f"  Loaded {total_games} pitching game log rows total")


def load_pitching_home_away_splits(conn, start_season, end_season):
    """Aggregate pitching.csv by player + vishome to create home/away pitching splits."""
    print(f"Pitching Phase 3: Loading pitching home/away splits for {start_season}-{end_season}...")
    cursor = conn.cursor()
    total_splits = 0

    for season in range(start_season, end_season + 1):
        try:
            zf = download_retrosheet_zip(season)
        except Exception as e:
            print(f"  Warning: Failed to download {season}: {e}")
            continue

        pitching = read_csv_from_zip(zf, "pitching.csv")
        if pitching is None:
            continue

        if "gametype" in pitching.columns:
            pitching = pitching[pitching["gametype"] == "regular"]
        if "stattype" in pitching.columns:
            pitching = pitching[pitching["stattype"] == "value"]

        if "vishome" not in pitching.columns:
            print(f"  {season}: no vishome column in pitching.csv, skipping")
            continue

        pitching["vishome"] = pitching["vishome"].astype(str).str.upper()
        pitching = pitching[pitching["vishome"].isin(["H", "V"])]

        counting_cols = ["p_ipouts", "p_bfp", "p_h", "p_er", "p_hr", "p_w", "p_k",
                         "p_hbp", "p_sh", "p_sf", "p_gs"]
        for col in counting_cols:
            if col in pitching.columns:
                pitching[col] = pitching[col].fillna(0).astype(int)

        grouped = pitching.groupby(["id", "vishome"]).agg(
            games=("id", "count"),
            gs=("p_gs", "sum"),
            ip_outs=("p_ipouts", "sum"),
            h=("p_h", "sum"),
            er=("p_er", "sum"),
            hr=("p_hr", "sum"),
            bb=("p_w", "sum"),
            so=("p_k", "sum"),
            hbp=("p_hbp", "sum"),
            sh=("p_sh", "sum"),
            sf=("p_sf", "sum"),
            bf=("p_bfp", "sum"),
        ).reset_index()

        season_rows = 0
        for _, row in grouped.iterrows():
            pid = str(row["id"])
            vh = str(row["vishome"])
            split = "home" if vh == "H" else "away"

            ip_outs = safe_int(row["ip_outs"])
            h = safe_int(row["h"])
            er = safe_int(row["er"])
            bb = safe_int(row["bb"])
            so = safe_int(row["so"])
            hr = safe_int(row["hr"])
            bf = safe_int(row["bf"])
            hbp = safe_int(row["hbp"])
            sh_val = safe_int(row["sh"])
            sf_val = safe_int(row["sf"])

            rates = compute_pitching_rate_stats(ip_outs, h, er, bb, so, hr, bf)
            ab_against = bf - bb - hbp - sh_val - sf_val
            baa = round(h / ab_against, 3) if ab_against > 0 else None

            cursor.execute("""
                INSERT OR REPLACE INTO pitching_home_away_splits (
                    player_id, season, split, games, games_started,
                    ip_outs, innings_pitched, hits, earned_runs, home_runs,
                    walks, strikeouts, era, whip, k_per_9, bb_per_9, baa
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                pid, season, split,
                safe_int(row["games"]), safe_int(row["gs"]),
                ip_outs, format_ip(ip_outs), h, er, hr,
                bb, so, rates["era"], rates["whip"],
                rates["k_per_9"], rates["bb_per_9"], baa,
            ))
            season_rows += 1

        conn.commit()
        total_splits += season_rows
        print(f"  {season}: {season_rows} pitching home/away split rows")

    print(f"  Loaded {total_splits} pitching home/away split rows total")


def download_pitching_retrosplits(season):
    """Download pitching-platoon CSV for a season from Chadwick Bureau."""
    ensure_cache_dir()
    cache_path = os.path.join(CACHE_DIR, f"retrosplits_pitching_platoon_{season}.csv")

    if os.path.exists(cache_path):
        return pd.read_csv(cache_path)

    url = PITCHING_RETROSPLITS_URL.format(year=season)
    print(f"    Downloading pitching retrosplits {season}...")
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        df = pd.read_csv(io.StringIO(resp.text))
        df.to_csv(cache_path, index=False)
        return df
    except requests.RequestException as e:
        print(f"    Warning: Failed to download pitching platoon splits for {season}: {e}")
        return None


def load_pitching_retrosplits(conn, start_season, end_season):
    """Load pitching platoon splits from Chadwick Bureau retrosplits."""
    effective_start = max(start_season, 1969)
    if effective_start > end_season:
        print(f"Pitching Phase 4: Skipping pitching platoon splits (year range predates 1969)")
        return

    print(f"Pitching Phase 4: Loading pitching retrosplits for {effective_start}-{end_season}...")
    cursor = conn.cursor()
    total_splits = 0

    for season in range(effective_start, end_season + 1):
        df = download_pitching_retrosplits(season)
        if df is None:
            continue

        # Filter to regular season only
        if "PHASE" in df.columns:
            df = df[df["PHASE"] == "R"]

        counting_cols = ["B_PA", "B_AB", "B_H", "B_TB", "B_2B", "B_3B", "B_HR",
                         "B_BB", "B_IBB", "B_SO", "B_HP", "B_SH", "B_SF"]
        for col in counting_cols:
            if col in df.columns:
                df[col] = df[col].fillna(0).astype(int)

        # Group by pitcher + batter hand
        grouped = df.groupby(["RESP_PIT_ID", "RESP_BAT_HAND_CD"]).agg(
            {col: "sum" for col in counting_cols if col in df.columns}
        ).reset_index()

        season_rows = 0
        for _, row in grouped.iterrows():
            pid = str(row.get("RESP_PIT_ID", ""))
            if not pid:
                continue

            bat_hand = str(row.get("RESP_BAT_HAND_CD", ""))
            if bat_hand == "L":
                split = "vs_LHB"
            elif bat_hand == "R":
                split = "vs_RHB"
            else:
                continue

            pa = safe_int(row.get("B_PA"))
            ab = safe_int(row.get("B_AB"))
            h = safe_int(row.get("B_H"))
            doubles = safe_int(row.get("B_2B"))
            triples = safe_int(row.get("B_3B"))
            hr = safe_int(row.get("B_HR"))
            bb = safe_int(row.get("B_BB"))
            ibb = safe_int(row.get("B_IBB"))
            so = safe_int(row.get("B_SO"))
            hbp = safe_int(row.get("B_HP"))
            sh_val = safe_int(row.get("B_SH"))
            sf_val = safe_int(row.get("B_SF"))

            # Compute batting-against rates
            avg_against = round(h / ab, 3) if ab > 0 else None
            obp_denom = ab + bb + hbp + sf_val
            obp_against = round((h + bb + hbp) / obp_denom, 3) if obp_denom > 0 else None
            tb = h + doubles + 2 * triples + 3 * hr
            slg_against = round(tb / ab, 3) if ab > 0 else None
            ops_against = round((obp_against or 0) + (slg_against or 0), 3) if obp_against is not None or slg_against is not None else None

            cursor.execute("""
                INSERT OR REPLACE INTO pitching_platoon_splits (
                    player_id, season, split, plate_appearances, at_bats,
                    hits, doubles, triples, home_runs, walks, intentional_walks,
                    strikeouts, hit_by_pitch, sacrifice_hits, sacrifice_flies,
                    batting_avg_against, obp_against, slg_against, ops_against
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                pid, season, split, pa, ab,
                h, doubles, triples, hr, bb, ibb,
                so, hbp, sh_val, sf_val,
                avg_against, obp_against, slg_against, ops_against,
            ))
            season_rows += 1

        conn.commit()
        total_splits += season_rows
        print(f"  {season}: {season_rows} pitching platoon split rows")

        time.sleep(0.3)

    print(f"  Loaded {total_splits} pitching platoon split rows total")


def compute_league_pitching_averages(conn):
    """Aggregate season_pitching_stats per season into league-wide pitching totals and rates."""
    print("Pitching Phase 5a: Computing league pitching averages...")
    cursor = conn.cursor()

    cursor.execute("""
        SELECT season,
               SUM(ip_outs), SUM(earned_runs), SUM(hits),
               SUM(walks), SUM(strikeouts), SUM(home_runs),
               SUM(batters_faced)
        FROM season_pitching_stats
        GROUP BY season
    """)

    rows_inserted = 0
    for row in cursor.fetchall():
        season = row[0]
        ip_outs, er, h, bb, so, hr, bf = row[1], row[2], row[3], row[4], row[5], row[6], row[7]

        rates = compute_pitching_rate_stats(ip_outs, h, er, bb, so, hr, bf)

        cursor.execute("""
            INSERT OR REPLACE INTO league_pitching_averages (
                season, total_ip_outs, total_er, total_h, total_bb,
                total_so, total_hr, total_bf,
                league_era, league_whip, league_k_per_9, league_bb_per_9, league_baa
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            season, ip_outs, er, h, bb, so, hr, bf,
            rates["era"], rates["whip"], rates["k_per_9"], rates["bb_per_9"], rates["baa"],
        ))
        rows_inserted += 1
        print(f"  {season}: ERA={rates['era']}, WHIP={rates['whip']}, K/9={rates['k_per_9']}")

    conn.commit()
    print(f"  Inserted {rows_inserted} league pitching average rows")


def compute_era_plus(conn):
    """Compute ERA+ for each pitcher-season: 100 * league_ERA / player_ERA."""
    print("Pitching Phase 5b: Computing ERA+...")
    cursor = conn.cursor()

    cursor.execute("SELECT season, league_era FROM league_pitching_averages")
    league = {}
    for row in cursor.fetchall():
        league[row[0]] = row[1]

    cursor.execute("""
        SELECT id, season, era FROM season_pitching_stats
        WHERE era IS NOT NULL AND era > 0
    """)
    updates = []
    for row in cursor.fetchall():
        row_id, season, player_era = row
        if season not in league or league[season] is None or league[season] == 0:
            continue
        era_plus = int(round(100 * league[season] / player_era))
        updates.append((era_plus, row_id))

    cursor.executemany("UPDATE season_pitching_stats SET era_plus = ? WHERE id = ?", updates)
    conn.commit()
    print(f"  Updated {len(updates)} pitcher-seasons with ERA+")


# ---------------------------------------------------------------------------
# Phase 3: Retrosplits — platoon splits (vs LHP / vs RHP)
# ---------------------------------------------------------------------------

def download_retrosplits(season):
    """Download batting-platoon CSV for a season from Chadwick Bureau."""
    ensure_cache_dir()
    cache_path = os.path.join(CACHE_DIR, f"retrosplits_platoon_{season}.csv")

    if os.path.exists(cache_path):
        return pd.read_csv(cache_path)

    url = RETROSPLITS_URL.format(year=season)
    print(f"    Downloading retrosplits {season}...")
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        df = pd.read_csv(io.StringIO(resp.text))
        df.to_csv(cache_path, index=False)
        return df
    except requests.RequestException as e:
        print(f"    Warning: Failed to download platoon splits for {season}: {e}")
        return None


def load_retrosplits(conn, start_season, end_season):
    """Load platoon splits from Chadwick Bureau retrosplits."""
    # Retrosplits only available 1969+
    effective_start = max(start_season, 1969)
    if effective_start > end_season:
        print(f"Phase 3: Skipping platoon splits (year range predates 1969)")
        return

    print(f"Phase 3: Loading retrosplits platoon data for {effective_start}-{end_season}...")
    cursor = conn.cursor()
    total_splits = 0

    for season in range(effective_start, end_season + 1):
        df = download_retrosplits(season)
        if df is None:
            continue

        # Filter to regular season only
        if "PHASE" in df.columns:
            df = df[df["PHASE"] == "R"]

        # Aggregate by batter + pitcher hand
        counting_cols = ["B_PA", "B_AB", "B_H", "B_2B", "B_3B", "B_HR",
                         "B_RBI", "B_BB", "B_IBB", "B_SO", "B_HP", "B_SF"]
        for col in counting_cols:
            if col in df.columns:
                df[col] = df[col].fillna(0).astype(int)

        grouped = df.groupby(["RESP_BAT_ID", "RESP_PIT_HAND_CD"]).agg(
            {col: "sum" for col in counting_cols if col in df.columns}
        ).reset_index()

        season_rows = 0
        for _, row in grouped.iterrows():
            pid = str(row.get("RESP_BAT_ID", ""))
            if not pid:
                continue

            pit_hand = str(row.get("RESP_PIT_HAND_CD", ""))
            if pit_hand == "L":
                split = "vs_LHP"
            elif pit_hand == "R":
                split = "vs_RHP"
            else:
                continue

            pa = safe_int(row.get("B_PA"))
            ab = safe_int(row.get("B_AB"))
            h = safe_int(row.get("B_H"))
            doubles = safe_int(row.get("B_2B"))
            triples = safe_int(row.get("B_3B"))
            hr = safe_int(row.get("B_HR"))
            rbi = safe_int(row.get("B_RBI"))
            bb = safe_int(row.get("B_BB"))
            so = safe_int(row.get("B_SO"))
            hbp = safe_int(row.get("B_HP"))
            sf = safe_int(row.get("B_SF"))

            rates = compute_rate_stats(h, ab, bb, hbp, sf, doubles, triples, hr, so)

            cursor.execute("""
                INSERT OR REPLACE INTO platoon_splits (
                    player_id, season, split, plate_appearances, at_bats,
                    hits, doubles, triples, home_runs, rbi, walks, strikeouts,
                    batting_avg, obp, slg, ops, iso, babip, wrc_plus
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                pid, season, split, pa, ab,
                h, doubles, triples, hr, rbi, bb, so,
                rates["avg"], rates["obp"], rates["slg"], rates["ops"],
                rates["iso"], rates["babip"],
                None,  # wRC+ not available from Retrosheet
            ))
            season_rows += 1

        conn.commit()
        total_splits += season_rows
        print(f"  {season}: {season_rows} split rows")

        time.sleep(0.3)

    print(f"  Loaded {total_splits} platoon split rows total")


# ---------------------------------------------------------------------------
# Phase 4: League averages + OPS+ computation
# ---------------------------------------------------------------------------

def compute_league_averages(conn):
    """Aggregate season_batting_stats per season into league-wide totals and rates."""
    print("Phase 4a: Computing league averages...")
    cursor = conn.cursor()

    cursor.execute("""
        SELECT season,
               SUM(plate_appearances), SUM(at_bats), SUM(hits),
               SUM(doubles), SUM(triples), SUM(home_runs),
               SUM(walks), SUM(hit_by_pitch), SUM(sacrifice_flies),
               SUM(strikeouts)
        FROM season_batting_stats
        GROUP BY season
    """)

    rows_inserted = 0
    for row in cursor.fetchall():
        season = row[0]
        pa, ab, h = row[1], row[2], row[3]
        doubles, triples, hr = row[4], row[5], row[6]
        bb, hbp, sf, so = row[7], row[8], row[9], row[10]

        rates = compute_rate_stats(h, ab, bb, hbp, sf, doubles, triples, hr, so)

        cursor.execute("""
            INSERT OR REPLACE INTO league_averages (
                season, total_pa, total_ab, total_hits,
                total_doubles, total_triples, total_hr,
                total_bb, total_hbp, total_sf, total_so,
                league_avg, league_obp, league_slg, league_ops,
                league_iso, league_babip
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            season, pa, ab, h, doubles, triples, hr,
            bb, hbp, sf, so,
            rates["avg"], rates["obp"], rates["slg"], rates["ops"],
            rates["iso"], rates["babip"],
        ))
        rows_inserted += 1
        print(f"  {season}: OBP={rates['obp']}, SLG={rates['slg']}, OPS={rates['ops']}")

    conn.commit()
    print(f"  Inserted {rows_inserted} league average rows")


def compute_ops_plus(conn):
    """Compute OPS+ for each player-season and UPDATE season_batting_stats."""
    print("Phase 4b: Computing OPS+...")
    cursor = conn.cursor()

    # Load league averages into a dict
    cursor.execute("SELECT season, league_obp, league_slg FROM league_averages")
    league = {}
    for row in cursor.fetchall():
        league[row[0]] = (row[1], row[2])

    # Update each player-season
    cursor.execute("""
        SELECT id, season, obp, slg FROM season_batting_stats
        WHERE obp IS NOT NULL AND slg IS NOT NULL
    """)
    updates = []
    for row in cursor.fetchall():
        row_id, season, player_obp, player_slg = row
        if season not in league:
            continue
        lg_obp, lg_slg = league[season]
        if lg_obp is None or lg_slg is None or lg_obp == 0 or lg_slg == 0:
            continue
        ops_plus = int(round(100 * (player_obp / lg_obp + player_slg / lg_slg - 1)))
        updates.append((ops_plus, row_id))

    cursor.executemany("UPDATE season_batting_stats SET ops_plus = ? WHERE id = ?", updates)
    conn.commit()
    print(f"  Updated {len(updates)} player-seasons with OPS+")


# ---------------------------------------------------------------------------
# Phase 5: Player bio data (birthdate, bats, throws) from Retrosheet biodata
# ---------------------------------------------------------------------------

BIODATA_URL = "https://www.retrosheet.org/downloads/biodata.zip"


def load_bio_data(conn):
    """Load birthdate, bats, throws from Retrosheet biodata.zip into players table."""
    print("Phase 5: Loading player bio data from Retrosheet biodata.zip...")
    ensure_cache_dir()
    cache_path = os.path.join(CACHE_DIR, "biodata.zip")

    if not os.path.exists(cache_path):
        print(f"  Downloading biodata from {BIODATA_URL}...")
        resp = requests.get(BIODATA_URL, timeout=120)
        resp.raise_for_status()
        with open(cache_path, "wb") as f:
            f.write(resp.content)

    zf = zipfile.ZipFile(cache_path)

    # Find the bio file inside the zip
    bio_file = None
    for name in zf.namelist():
        if "biofile" in name.lower() and name.lower().endswith(".csv"):
            bio_file = name
            break

    if bio_file is None:
        print("  Warning: Could not find biofile CSV in biodata.zip")
        return

    with zf.open(bio_file) as f:
        bio_df = pd.read_csv(f)

    print(f"  Read {len(bio_df)} rows from {bio_file}")

    cursor = conn.cursor()
    updated = 0

    for _, row in bio_df.iterrows():
        pid = str(row.get("id", ""))
        if not pid:
            continue

        # Parse birthdate from YYYYMMDD (may be float due to NaN)
        raw_bd = row.get("birthdate")
        birthdate = None
        if pd.notna(raw_bd):
            try:
                bd_str = str(int(float(raw_bd)))
                if len(bd_str) == 8:
                    birthdate = f"{bd_str[:4]}-{bd_str[4:6]}-{bd_str[6:8]}"
            except (ValueError, TypeError):
                pass

        bats = str(row.get("bats", "")).strip() if pd.notna(row.get("bats")) else None
        throws = str(row.get("throws", "")).strip() if pd.notna(row.get("throws")) else None

        if birthdate or bats or throws:
            # Only update fields that have values
            sets = []
            vals = []
            if birthdate:
                sets.append("birthdate = ?")
                vals.append(birthdate)
            if bats:
                sets.append("bats = ?")
                vals.append(bats)
            if throws:
                sets.append("throws = ?")
                vals.append(throws)
            vals.append(pid)
            cursor.execute(f"UPDATE players SET {', '.join(sets)} WHERE player_id = ?", vals)
            if cursor.rowcount > 0:
                updated += 1

    conn.commit()
    print(f"  Updated {updated} players with bio data")


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def pull_and_load(start_season, end_season, skip_game_logs=False):
    """Pull all data and load into SQLite.

    Args:
        skip_game_logs: If True, skip game logs, home/away splits, and pitching
            equivalents. Useful for historical data where streaks aren't needed.
    """
    conn = sqlite3.connect(DB_PATH)
    create_tables(conn)

    # Migrate existing DBs: add ops_plus column if missing
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(season_batting_stats)")
    columns = {row[1] for row in cursor.fetchall()}
    if "ops_plus" not in columns:
        cursor.execute("ALTER TABLE season_batting_stats ADD COLUMN ops_plus INTEGER")
        conn.commit()
        print("  Migrated: added ops_plus column to season_batting_stats")

    # Migrate existing DBs: add vishome column to game_batting_logs if missing
    cursor.execute("PRAGMA table_info(game_batting_logs)")
    gl_columns = {row[1] for row in cursor.fetchall()}
    if "vishome" not in gl_columns:
        cursor.execute("ALTER TABLE game_batting_logs ADD COLUMN vishome TEXT")
        print("  Migrated: added vishome column to game_batting_logs")
    conn.commit()

    # Migrate existing DBs: add bio columns to players if missing
    cursor.execute("PRAGMA table_info(players)")
    player_columns = {row[1] for row in cursor.fetchall()}
    for col in ["birthdate", "bats", "throws"]:
        if col not in player_columns:
            cursor.execute(f"ALTER TABLE players ADD COLUMN {col} TEXT")
            print(f"  Migrated: added {col} column to players")
    conn.commit()

    # Phase 1: Players + season stats from Retrosheet
    load_players_and_season_stats(conn, start_season, end_season)

    if not skip_game_logs:
        # Phase 2: Game logs from Retrosheet
        load_game_logs(conn, start_season, end_season)

        # Phase 2b: Home/away splits from Retrosheet
        load_home_away_splits(conn, start_season, end_season)
    else:
        print("Skipping game logs and home/away splits (--skip-game-logs)")

    # Phase 2c: Fielding stats from Retrosheet
    load_fielding_stats(conn, start_season, end_season)

    # Phase 3: Platoon splits from retrosplits
    load_retrosplits(conn, start_season, end_season)

    # Phase 4: League averages + OPS+
    compute_league_averages(conn)
    compute_ops_plus(conn)

    # Phase 5: Player bio data (birthdate, bats, throws)
    load_bio_data(conn)

    # Pitching phases
    load_pitching_season_stats(conn, start_season, end_season)

    if not skip_game_logs:
        load_pitching_game_logs(conn, start_season, end_season)
        load_pitching_home_away_splits(conn, start_season, end_season)
    else:
        print("Skipping pitching game logs and home/away splits (--skip-game-logs)")

    load_pitching_retrosplits(conn, start_season, end_season)
    compute_league_pitching_averages(conn)
    compute_era_plus(conn)

    # Update computed prominence columns for iOS disambiguation
    print("\nUpdating player prominence columns...")
    cur = conn.cursor()
    cur.execute("""
        UPDATE players SET
            career_games = COALESCE((SELECT SUM(s.games) FROM season_batting_stats s WHERE s.player_id = players.player_id), 0) +
                           COALESCE((SELECT SUM(sp.games) FROM season_pitching_stats sp WHERE sp.player_id = players.player_id), 0),
            last_season = MAX(
                COALESCE((SELECT MAX(s.season) FROM season_batting_stats s WHERE s.player_id = players.player_id), 0),
                COALESCE((SELECT MAX(sp.season) FROM season_pitching_stats sp WHERE sp.player_id = players.player_id), 0)
            )
    """)
    conn.commit()
    print(f"  Updated {cur.rowcount} players")

    conn.close()
    print(f"\nDone! Database saved to: {DB_PATH}")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = [a for a in sys.argv[1:] if a.startswith("--")]
    start = int(args[0]) if len(args) > 0 else DEFAULT_START
    end = int(args[1]) if len(args) > 1 else DEFAULT_END
    skip_gl = "--skip-game-logs" in flags
    pull_and_load(start, end, skip_game_logs=skip_gl)
