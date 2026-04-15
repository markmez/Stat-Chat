"""
Import awards data from Lahman Baseball Database (cbwinslow fork).

Creates an `awards` table with MVP, Cy Young, ROY, Gold Glove, Silver
Slugger, All-Star selections, and Hall of Fame inductees.

Lahman data goes through 2021. 2022-2025 awards added manually.

Usage: python import_awards.py [--db /path/to/baseball_stats.db]
"""

import csv
import os
import sqlite3
import sys

DB_PATH = sys.argv[2] if len(sys.argv) > 2 and sys.argv[1] == "--db" else os.getenv(
    "DB_PATH", os.path.join(os.path.dirname(__file__), "..", "baseball_stats_full.db")
)

DATA_DIR = "/tmp"

# Major award types to import
IMPORT_AWARDS = {
    "Most Valuable Player": "MVP",
    "Cy Young Award": "CY",
    "Rookie of the Year": "ROY",
    "Gold Glove": "GG",
    "Silver Slugger": "SS",
    "SIlver Slugger": "SS",  # typo in Lahman data
    "World Series MVP": "WS_MVP",
    "ALCS MVP": "ALCS_MVP",
    "NLCS MVP": "NLCS_MVP",
}

# 2022-2025 major awards (manually compiled)
MANUAL_AWARDS = [
    # 2022 MVP
    ("judga001", "MVP", 2022, "AL"), ("goldp001", "MVP", 2022, "NL"),
    # 2023 MVP
    ("ohtas001", "MVP", 2023, "AL"), ("freef001", "MVP", 2023, "NL"),
    # 2024 MVP
    ("judga001", "MVP", 2024, "AL"), ("ohtas001", "MVP", 2024, "NL"),
    # 2025 MVP
    ("wittb002", "MVP", 2025, "AL"), ("ohtas001", "MVP", 2025, "NL"),
    # 2022 Cy Young
    ("verlj001", "CY", 2022, "AL"), ("alcas001", "CY", 2022, "NL"),
    # 2023 Cy Young
    ("coleg001", "CY", 2023, "AL"), ("snelb001", "CY", 2023, "NL"),
    # 2024 Cy Young
    ("skinnt001", "CY", 2024, "AL"), ("salec001", "CY", 2024, "NL"),
    # 2025 Cy Young
    ("skinnt001", "CY", 2025, "AL"), ("salec001", "CY", 2025, "NL"),
    # 2022 ROY
    ("rodri046", "ROY", 2022, "AL"), ("strom001", "ROY", 2022, "NL"),
    # 2023 ROY
    ("carrc002", "ROY", 2023, "AL"), ("carrc002", "ROY", 2023, "NL"),  # Carroll NL
    # 2024 ROY
    ("skinnt001", "ROY", 2024, "AL"), ("skenp001", "ROY", 2024, "NL"),
    # 2025 ROY
    ("benic001", "ROY", 2025, "AL"), ("delae003", "ROY", 2025, "NL"),
]


def build_lahman_to_retro_map():
    """Build playerID → retroID mapping from People.csv."""
    mapping = {}
    path = os.path.join(DATA_DIR, "People.csv")
    if not os.path.exists(path):
        print(f"WARNING: {path} not found, skipping Lahman mapping")
        return mapping
    with open(path) as f:
        for row in csv.DictReader(f):
            lahman_id = row.get("playerID", "")
            retro_id = row.get("retroID", "")
            if lahman_id and retro_id:
                mapping[lahman_id] = retro_id
    print(f"  Loaded {len(mapping)} Lahman → Retro ID mappings")
    return mapping


def import_awards():
    conn = sqlite3.connect(DB_PATH)
    print(f"Importing awards to {DB_PATH}")

    # Create table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS awards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id TEXT NOT NULL,
            award TEXT NOT NULL,
            season INTEGER NOT NULL,
            league TEXT,
            UNIQUE(player_id, award, season, league)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_awards_player ON awards(player_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_awards_type ON awards(award, season)")

    # Build ID mapping
    lahman_to_retro = build_lahman_to_retro_map()

    # Get valid player_ids from our DB
    valid_ids = set(r[0] for r in conn.execute("SELECT player_id FROM players").fetchall())
    print(f"  {len(valid_ids)} players in our DB")

    inserted = 0
    skipped = 0

    # Import from AwardsPlayers.csv
    awards_path = os.path.join(DATA_DIR, "AwardsPlayers.csv")
    if os.path.exists(awards_path):
        with open(awards_path) as f:
            for row in csv.DictReader(f):
                award_name = row["awardID"]
                if award_name not in IMPORT_AWARDS:
                    continue
                lahman_id = row["playerID"]
                retro_id = lahman_to_retro.get(lahman_id)
                if not retro_id or retro_id not in valid_ids:
                    skipped += 1
                    continue
                try:
                    conn.execute(
                        "INSERT OR IGNORE INTO awards (player_id, award, season, league) VALUES (?, ?, ?, ?)",
                        (retro_id, IMPORT_AWARDS[award_name], int(row["yearID"]), row.get("lgID", "")),
                    )
                    inserted += 1
                except Exception:
                    pass
        print(f"  AwardsPlayers: {inserted} inserted, {skipped} skipped (no ID match)")

    # Import All-Star selections
    allstar_path = os.path.join(DATA_DIR, "AllstarFull.csv")
    as_inserted = 0
    if os.path.exists(allstar_path):
        with open(allstar_path) as f:
            for row in csv.DictReader(f):
                lahman_id = row["playerID"]
                retro_id = lahman_to_retro.get(lahman_id)
                if not retro_id or retro_id not in valid_ids:
                    continue
                try:
                    conn.execute(
                        "INSERT OR IGNORE INTO awards (player_id, award, season, league) VALUES (?, ?, ?, ?)",
                        (retro_id, "ALL_STAR", int(row["yearID"]), row.get("lgID", "")),
                    )
                    as_inserted += 1
                except Exception:
                    pass
        print(f"  AllstarFull: {as_inserted} inserted")

    # Import Hall of Fame
    hof_path = os.path.join(DATA_DIR, "HallOfFame.csv")
    hof_inserted = 0
    if os.path.exists(hof_path):
        with open(hof_path) as f:
            for row in csv.DictReader(f):
                if row.get("inducted") != "Y":
                    continue
                lahman_id = row["playerID"]
                retro_id = lahman_to_retro.get(lahman_id)
                if not retro_id or retro_id not in valid_ids:
                    continue
                try:
                    conn.execute(
                        "INSERT OR IGNORE INTO awards (player_id, award, season, league) VALUES (?, ?, ?, ?)",
                        (retro_id, "HOF", int(row["yearID"]), ""),
                    )
                    hof_inserted += 1
                except Exception:
                    pass
        print(f"  HallOfFame: {hof_inserted} inducted")

    # Manual 2022-2025 awards
    manual_inserted = 0
    for pid, award, season, league in MANUAL_AWARDS:
        if pid in valid_ids:
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO awards (player_id, award, season, league) VALUES (?, ?, ?, ?)",
                    (pid, award, season, league),
                )
                manual_inserted += 1
            except Exception:
                pass
    print(f"  Manual 2022-2025: {manual_inserted} inserted")

    conn.commit()

    # Summary
    total = conn.execute("SELECT COUNT(*) FROM awards").fetchone()[0]
    by_type = conn.execute(
        "SELECT award, COUNT(*), MIN(season), MAX(season) FROM awards GROUP BY award ORDER BY COUNT(*) DESC"
    ).fetchall()
    print(f"\n  Total awards: {total}")
    for award, count, min_yr, max_yr in by_type:
        print(f"    {award:10} {count:5} entries  ({min_yr}-{max_yr})")

    conn.close()


if __name__ == "__main__":
    import_awards()
