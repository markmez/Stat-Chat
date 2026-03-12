#!/usr/bin/env python3
"""Backfill RISP splits from MSF play-by-play data for 2025 and 2026.
One-time script — after this, pull_live_stats.py handles RISP in the normal pipeline.
"""
import os, sys, time, base64, requests, sqlite3

# --- Config ---
DB_PATH = os.environ.get("DB_PATH", "baseball_stats.db")
MSF_KEY = os.environ.get("MSF_API_KEY", "")
if not MSF_KEY:
    print("Set MSF_API_KEY env var"); sys.exit(1)

AUTH = base64.b64encode(f"{MSF_KEY}:MYSPORTSFEEDS".encode()).decode()
HEADERS = {"Authorization": f"Basic {AUTH}"}
BASE = "https://api.mysportsfeeds.com/v2.1/pull/mlb"

HIT_RESULTS = {"SINGLE", "DOUBLE", "TRIPLE", "HOMERUN"}
OUT_RESULTS = {"FLYOUT", "GROUNDOUT", "LINEOUT", "POP_OUT", "FORCEOUT",
               "FIELDERS_CHOICE", "DOUBLE_PLAY", "TRIPLE_PLAY",
               "SACRIFICE_FLY", "SACRIFICE_BUNT", "BUNT_GROUNDOUT",
               "BUNT_POP_OUT", "BUNT_LINEOUT"}

def msf_get(endpoint):
    url = f"{BASE}/{endpoint}"
    for attempt in range(4):
        try:
            r = requests.get(url, headers=HEADERS, timeout=180)
            if r.status_code == 429:
                print(f"    Rate limited, waiting 5s...")
                time.sleep(5)
                continue
            if r.status_code == 500:
                wait = 5 * (attempt + 1)
                print(f"    Server error 500, retrying in {wait}s...")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except requests.exceptions.ReadTimeout:
            print(f"    Timeout, retrying...")
            time.sleep(5)
    return None

def empty_bat_stats():
    return {"pa": 0, "ab": 0, "h": 0, "2b": 0, "3b": 0, "hr": 0,
            "rbi": 0, "bb": 0, "so": 0, "hbp": 0, "sf": 0}

def accumulate(bucket, result):
    is_hit = result in HIT_RESULTS
    is_out = result in OUT_RESULTS or result == "STRIKEOUT"
    is_ab = is_hit or is_out
    bucket["pa"] += 1
    if is_ab: bucket["ab"] += 1
    if is_hit: bucket["h"] += 1
    if result == "DOUBLE": bucket["2b"] += 1
    elif result == "TRIPLE": bucket["3b"] += 1
    elif result == "HOMERUN": bucket["hr"] += 1
    if result == "WALK": bucket["bb"] += 1
    if result == "STRIKEOUT": bucket["so"] += 1
    if result == "HIT_BY_PITCH": bucket["hbp"] += 1
    if result == "SACRIFICE_FLY": bucket["sf"] += 1

def compute_rate_stats(h, ab, bb, hbp, sf, doubles, triples, hr, so):
    pa = ab + bb + hbp + sf
    avg = h / ab if ab > 0 else None
    obp_denom = ab + bb + hbp + sf
    obp = (h + bb + hbp) / obp_denom if obp_denom > 0 else None
    singles = h - doubles - triples - hr
    tb = singles + 2 * doubles + 3 * triples + 4 * hr
    slg = tb / ab if ab > 0 else None
    ops = (obp or 0) + (slg or 0) if obp is not None and slg is not None else None
    iso = (slg or 0) - (avg or 0) if slg is not None and avg is not None else None
    babip_denom = ab - so - hr + sf
    babip = (h - hr) / babip_denom if babip_denom > 0 else None
    return pa, avg, obp, slg, ops, iso, babip

def process_season(conn, season_str, season_year):
    print(f"\nProcessing RISP splits for {season_str}...")

    # Get completed games
    schedule = msf_get(f"{season_str}/games.json?status=final")
    if not schedule:
        print("  Failed to fetch schedule"); return
    games = [g["schedule"]["id"] for g in schedule.get("games", [])
             if g.get("schedule", {}).get("playedStatus") == "COMPLETED"]
    print(f"  Found {len(games)} completed games")

    bat_risp = {}
    pitch_risp = {}

    for i, gid in enumerate(games):
        if i > 0:
            time.sleep(2)
        try:
            pbp = msf_get(f"{season_str}/games/{gid}/playbyplay.json")
        except Exception as e:
            print(f"    Game {gid}: error {e}")
            continue
        if not pbp:
            continue

        for ab in pbp.get("atBats", []):
            all_plays = ab.get("atBatPlay", [])
            for play in all_plays:
                if not isinstance(play, dict):
                    continue
                bu = play.get("batterUp", {})
                if not bu or not isinstance(bu, dict) or not bu.get("result"):
                    continue

                result = bu["result"]
                batter_info = bu.get("battingPlayer", {})
                batter_id = batter_info.get("id")
                batter_name = f"{batter_info.get('firstName', '')} {batter_info.get('lastName', '')}".strip()

                ps = play.get("playStatus", {})
                pitcher_info = ps.get("pitcher", {})
                pitcher_id = pitcher_info.get("id")
                pitcher_name = f"{pitcher_info.get('firstName', '')} {pitcher_info.get('lastName', '')}".strip()

                if not batter_id or not pitcher_id:
                    continue

                has_risp = ps.get("secondBaseRunner") is not None or ps.get("thirdBaseRunner") is not None
                risp_label = "RISP" if has_risp else "Non-RISP"

                br_key = (batter_id, batter_name, risp_label)
                if br_key not in bat_risp:
                    bat_risp[br_key] = empty_bat_stats()
                accumulate(bat_risp[br_key], result)

                pr_key = (pitcher_id, pitcher_name, risp_label)
                if pr_key not in pitch_risp:
                    pitch_risp[pr_key] = empty_bat_stats()
                accumulate(pitch_risp[pr_key], result)

                break

        if (i + 1) % 50 == 0:
            print(f"    Processed {i + 1}/{len(games)} games...")

    print(f"  Processed all {len(games)} games: {len(bat_risp)} batting RISP, {len(pitch_risp)} pitching RISP splits")

    # Create tables and insert
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS risp_batting_splits (
            player_id TEXT NOT NULL, season INTEGER NOT NULL, split TEXT NOT NULL,
            plate_appearances INTEGER, at_bats INTEGER, hits INTEGER,
            doubles INTEGER, triples INTEGER, home_runs INTEGER,
            rbi INTEGER, walks INTEGER, strikeouts INTEGER,
            hit_by_pitch INTEGER, sacrifice_flies INTEGER,
            batting_avg REAL, obp REAL, slg REAL, ops REAL, iso REAL, babip REAL,
            UNIQUE(player_id, season, split)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS risp_pitching_splits (
            player_id TEXT NOT NULL, season INTEGER NOT NULL, split TEXT NOT NULL,
            plate_appearances INTEGER, at_bats INTEGER, hits INTEGER,
            doubles INTEGER, triples INTEGER, home_runs INTEGER,
            walks INTEGER, strikeouts INTEGER, hit_by_pitch INTEGER, sacrifice_flies INTEGER,
            batting_avg_against REAL, obp_against REAL, slg_against REAL, ops_against REAL,
            UNIQUE(player_id, season, split)
        )
    """)

    cursor.execute("DELETE FROM risp_batting_splits WHERE season = ?", (season_year,))
    cursor.execute("DELETE FROM risp_pitching_splits WHERE season = ?", (season_year,))

    def resolve_player(name):
        cursor.execute("SELECT player_id FROM players WHERE name = ? LIMIT 1", (name,))
        row = cursor.fetchone()
        return row[0] if row else None

    bat_count = 0
    for (msf_id, name, risp_label), stats in bat_risp.items():
        pid = resolve_player(name)
        if not pid:
            continue
        h, ab, bb, hbp, sf = stats["h"], stats["ab"], stats["bb"], stats["hbp"], stats["sf"]
        doubles, triples, hr, so = stats["2b"], stats["3b"], stats["hr"], stats["so"]
        pa_calc, avg, obp, slg, ops, iso, babip = compute_rate_stats(h, ab, bb, hbp, sf, doubles, triples, hr, so)
        cursor.execute("""
            INSERT OR REPLACE INTO risp_batting_splits
            (player_id, season, split, plate_appearances, at_bats,
             hits, doubles, triples, home_runs, rbi, walks, strikeouts,
             hit_by_pitch, sacrifice_flies, batting_avg, obp, slg, ops, iso, babip)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (pid, season_year, risp_label, pa_calc, ab, h, doubles, triples, hr, stats["rbi"], bb, so,
              hbp, sf, avg, obp, slg, ops, iso, babip))
        bat_count += 1

    pitch_count = 0
    for (msf_id, name, risp_label), stats in pitch_risp.items():
        pid = resolve_player(name)
        if not pid:
            continue
        h, ab, bb, hbp, sf = stats["h"], stats["ab"], stats["bb"], stats["hbp"], stats["sf"]
        doubles, triples, hr, so = stats["2b"], stats["3b"], stats["hr"], stats["so"]
        avg_against = h / ab if ab > 0 else None
        obp_denom = ab + bb + hbp + sf
        obp_against = (h + bb + hbp) / obp_denom if obp_denom > 0 else None
        singles = h - doubles - triples - hr
        tb = singles + 2 * doubles + 3 * triples + 4 * hr
        slg_against = tb / ab if ab > 0 else None
        ops_against = (obp_against or 0) + (slg_against or 0) if obp_against is not None and slg_against is not None else None
        cursor.execute("""
            INSERT OR REPLACE INTO risp_pitching_splits
            (player_id, season, split, plate_appearances, at_bats,
             hits, doubles, triples, home_runs, walks, strikeouts,
             hit_by_pitch, sacrifice_flies,
             batting_avg_against, obp_against, slg_against, ops_against)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (pid, season_year, risp_label, stats["pa"], ab, h, doubles, triples, hr, bb, so, hbp, sf,
              avg_against, obp_against, slg_against, ops_against))
        pitch_count += 1

    conn.commit()
    print(f"  RISP: {bat_count} batting + {pitch_count} pitching")


if __name__ == "__main__":
    conn = sqlite3.connect(DB_PATH)
    t0 = time.time()
    process_season(conn, "2026-preseason", 2026)
    process_season(conn, "2025-regular", 2025)
    conn.close()
    print(f"\nDone in {time.time() - t0:.1f}s")
