"""
One-time script to load historical game logs (1920-2015) into the existing DB.
Does NOT touch season stats, splits, or any 2016+ data.
Only inserts game_batting_logs and game_pitching_logs rows.

No pandas dependency — uses only standard library.

Usage:
    python load_historical_gamelogs.py --db /data/baseball_stats_full.db
    python load_historical_gamelogs.py --db /data/baseball_stats_full.db --start 1950 --end 1980
"""

import argparse
import csv
import io
import os
import sqlite3
import sys
import time
import zipfile
from urllib.request import urlopen

RETROSHEET_URL = "https://www.retrosheet.org/downloads/{year}/{year}csvs.zip"


def download_retrosheet_zip(season):
    url = RETROSHEET_URL.format(year=season)
    resp = urlopen(url, timeout=30)
    return zipfile.ZipFile(io.BytesIO(resp.read()))


def read_csv_from_zip(zf, filename):
    """Read a CSV from a ZIP, return list of dicts (like DictReader)."""
    for name in zf.namelist():
        if name.lower().endswith(filename.lower()):
            with zf.open(name) as f:
                text = io.TextIOWrapper(f, encoding="utf-8", errors="replace")
                return list(csv.DictReader(text))
    return None


def safe_int(val, default=0):
    if val is None or val == "":
        return default
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return default


def safe_float(val, default=None):
    if val is None or val == "":
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def format_date(raw):
    s = str(raw).strip()
    if len(s) == 8:
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s


def compute_rate_stats(h, ab, bb, hbp, sf, doubles, triples, hr):
    avg = h / ab if ab > 0 else None
    obp_denom = ab + bb + hbp + sf
    obp = (h + bb + hbp) / obp_denom if obp_denom > 0 else None
    tb = h + doubles + 2 * triples + 3 * hr
    slg = tb / ab if ab > 0 else None
    ops = (obp or 0) + (slg or 0) if obp is not None or slg is not None else None
    return avg, obp, slg, ops


def load_batting_game_logs(conn, start, end):
    print(f"Loading batting game logs for {start}-{end}...")
    cursor = conn.cursor()
    total = 0

    for season in range(start, end + 1):
        t0 = time.time()
        try:
            zf = download_retrosheet_zip(season)
        except Exception as e:
            print(f"  {season}: SKIP ({e})")
            continue

        rows_data = read_csv_from_zip(zf, "batting.csv")
        if rows_data is None:
            print(f"  {season}: no batting.csv")
            continue

        count = 0
        for row in rows_data:
            # Filter to regular season, stat type = value
            if row.get("gametype", "regular") != "regular":
                continue
            if row.get("stattype", "value") != "value":
                continue

            pid = row.get("id", "").strip()
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

            date_str = format_date(row.get("date", ""))
            opp = row.get("opp", "").strip() or None
            vh = row.get("vishome", "").strip().upper() or None

            avg, obp, slg, ops = compute_rate_stats(h, ab, bb, hbp, sf, doubles, triples, hr)

            cursor.execute("""
                INSERT OR IGNORE INTO game_batting_logs (
                    player_id, season, date, opponent, vishome,
                    plate_appearances, at_bats, hits, doubles, triples,
                    home_runs, runs, rbi, walks, strikeouts,
                    batting_avg, obp, slg, ops
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                pid, season, date_str, opp, vh,
                pa, ab, h, doubles, triples, hr, r, rbi, bb, so,
                avg, obp, slg, ops,
            ))
            count += 1

        conn.commit()
        total += count
        elapsed = time.time() - t0
        print(f"  {season}: {count:,} rows ({elapsed:.1f}s)")

    print(f"  Total batting game logs: {total:,}")
    return total


def load_pitching_game_logs(conn, start, end):
    print(f"Loading pitching game logs for {start}-{end}...")
    cursor = conn.cursor()
    total = 0

    for season in range(start, end + 1):
        t0 = time.time()
        try:
            zf = download_retrosheet_zip(season)
        except Exception as e:
            print(f"  {season}: SKIP ({e})")
            continue

        rows_data = read_csv_from_zip(zf, "pitching.csv")
        if rows_data is None:
            print(f"  {season}: no pitching.csv")
            continue

        count = 0
        for row in rows_data:
            if row.get("gametype", "regular") != "regular":
                continue
            if row.get("stattype", "value") != "value":
                continue

            pid = row.get("id", "").strip()
            if not pid:
                continue

            ip_raw = safe_float(row.get("p_ip"), 0)
            if ip_raw == 0:
                continue

            ip_whole = int(ip_raw)
            ip_frac = round((ip_raw - ip_whole) * 10)
            ip_outs = ip_whole * 3 + ip_frac
            innings_text = f"{ip_whole}.{ip_frac}"

            h = safe_int(row.get("p_h"))
            er = safe_int(row.get("p_er"))
            r = safe_int(row.get("p_r"))
            hr = safe_int(row.get("p_hr"))
            bb = safe_int(row.get("p_w"))
            so = safe_int(row.get("p_k"))
            hbp = safe_int(row.get("p_hbp"))
            bf = safe_int(row.get("p_bf"))
            gs = safe_int(row.get("p_gs"))

            # Win/loss/save column names vary by season
            w = safe_int(row.get("p_w_game", row.get("p_wins", 0)))
            l = safe_int(row.get("p_l_game", row.get("p_losses", 0)))
            sv = safe_int(row.get("p_sv"))

            era = (er * 9.0) / (ip_outs / 3.0) if ip_outs > 0 else None

            date_str = format_date(row.get("date", ""))
            opp = row.get("opp", "").strip() or None
            vh = row.get("vishome", "").strip().upper() or None

            cursor.execute("""
                INSERT OR IGNORE INTO game_pitching_logs (
                    player_id, season, date, opponent, vishome, is_start,
                    ip_outs, innings_pitched, hits, runs, earned_runs,
                    home_runs, walks, strikeouts, hit_by_pitch, batters_faced,
                    win, loss, save, era
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                pid, season, date_str, opp, vh, 1 if gs > 0 else 0,
                ip_outs, innings_text, h, r, er, hr, bb, so, hbp, bf,
                w, l, sv, era,
            ))
            count += 1

        conn.commit()
        total += count
        elapsed = time.time() - t0
        print(f"  {season}: {count:,} rows ({elapsed:.1f}s)")

    print(f"  Total pitching game logs: {total:,}")
    return total


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="/data/baseball_stats_full.db")
    parser.add_argument("--start", type=int, default=1920)
    parser.add_argument("--end", type=int, default=2015)
    args = parser.parse_args()

    print(f"Loading historical game logs {args.start}-{args.end} into {args.db}")
    conn = sqlite3.connect(args.db)
    conn.execute("PRAGMA journal_mode=WAL")

    t0 = time.time()
    bat = load_batting_game_logs(conn, args.start, args.end)
    pitch = load_pitching_game_logs(conn, args.start, args.end)
    elapsed = time.time() - t0

    conn.close()
    print(f"\nDone! {bat:,} batting + {pitch:,} pitching game logs in {elapsed:.0f}s")


if __name__ == "__main__":
    main()
