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

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "baseball_stats.db")
CACHE_DIR = os.path.join(os.path.dirname(__file__), ".cache")

# Retrosheet CSV download URLs
RETROSHEET_SEASON_URL = "https://www.retrosheet.org/downloads/{year}/{year}csvs.zip"

# Chadwick Bureau retrosplits (GitHub raw)
RETROSPLITS_URL = "https://raw.githubusercontent.com/chadwickbureau/retrosplits/master/splits/batting-platoon-{year}.csv"


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
            throws TEXT
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
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_gamelogs_player ON game_batting_logs(player_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_gamelogs_player_season ON game_batting_logs(player_id, season)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_gamelogs_date ON game_batting_logs(date)")

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

                # Determine primary position from games at each position
                pos_cols = ["g_c", "g_1b", "g_2b", "g_3b", "g_ss", "g_lf", "g_cf", "g_rf", "g_dh", "g_p"]
                pos_names = ["C", "1B", "2B", "3B", "SS", "LF", "CF", "RF", "DH", "P"]
                positions = []
                for col, pos_name in zip(pos_cols, pos_names):
                    if safe_int(row.get(col)) > 0:
                        positions.append(pos_name)
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

            rates = compute_rate_stats(h, ab, bb, hbp, sf, doubles, triples, hr, so)

            cursor.execute("""
                INSERT OR REPLACE INTO game_batting_logs (
                    player_id, season, date, opponent,
                    plate_appearances, at_bats, hits, doubles, triples,
                    home_runs, runs, rbi, walks, strikeouts,
                    batting_avg, obp, slg, ops
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                pid, season, date, opp,
                pa, ab, h, doubles, triples, hr, r, rbi, bb, so,
                rates["avg"], rates["obp"], rates["slg"], rates["ops"],
            ))
            season_rows += 1

        conn.commit()
        total_games += season_rows
        print(f"  {season}: {season_rows} game log rows")

    print(f"  Loaded {total_games} game log rows total")


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

def pull_and_load(start_season, end_season):
    """Pull all data and load into SQLite."""
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

    # Phase 2: Game logs from Retrosheet
    load_game_logs(conn, start_season, end_season)

    # Phase 3: Platoon splits from retrosplits
    load_retrosplits(conn, start_season, end_season)

    # Phase 4: League averages + OPS+
    compute_league_averages(conn)
    compute_ops_plus(conn)

    # Phase 5: Player bio data (birthdate, bats, throws)
    load_bio_data(conn)

    conn.close()
    print(f"\nDone! Database saved to: {DB_PATH}")


if __name__ == "__main__":
    start = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_START
    end = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_END
    pull_and_load(start, end)
