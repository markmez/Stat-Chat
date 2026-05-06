"""
StatChat Backend — FastAPI application entry point.

Run locally:
    cd backend/
    uvicorn main:app --reload

Or from project root:
    uvicorn backend.main:app --reload
"""

import logging
import os
import shutil
import sqlite3
import sys
import urllib.request
from contextlib import asynccontextmanager

_log_handlers = [logging.StreamHandler()]
_api_log_path = os.getenv("API_LOG_PATH", "/data/api.log")
if os.path.isdir(os.path.dirname(_api_log_path)):
    from logging.handlers import RotatingFileHandler
    _log_handlers.append(RotatingFileHandler(
        _api_log_path, maxBytes=50_000_000, backupCount=3
    ))
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    handlers=_log_handlers,
)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

load_dotenv()

# Make the project root importable so backend modules can reach schema_description.py
_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
if _root not in sys.path:
    sys.path.insert(0, _root)

from routers import health, query, player_card, stats, admin, notable, team_card, client_event  # noqa: E402
from services.metering import init_metering_db  # noqa: E402


DB_PATH = os.getenv("DB_PATH", "/data/baseball_stats_full.db")
SEED_DB_PATH = os.getenv("SEED_DB_PATH", "/app/seed_db/baseball_stats_full.db")
DB_DOWNLOAD_URL = os.getenv(
    "DB_DOWNLOAD_URL",
    "https://stat-chat.s3.us-east-2.amazonaws.com/baseball_stats_full.db",
)


def ensure_db():
    """Ensure the database exists on the volume.

    Priority:
    1. Already on volume (from previous run or cron update) — use as-is.
    2. Copy from baked-in seed in Docker image — fast, no network needed.
    3. Download from S3 — fallback if seed is missing (shouldn't happen).
    """
    if os.path.exists(DB_PATH):
        size_mb = os.path.getsize(DB_PATH) // 1_000_000
        print(f"Database found at {DB_PATH} ({size_mb} MB)")
        return

    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

    # Try baked-in seed first (fast local copy)
    if os.path.exists(SEED_DB_PATH):
        print(f"Copying seed database to volume...")
        shutil.copy2(SEED_DB_PATH, DB_PATH)
        size_mb = os.path.getsize(DB_PATH) // 1_000_000
        print(f"Database seeded from image ({size_mb} MB)")
        return

    # Fallback: download from S3
    print(f"Downloading database from {DB_DOWNLOAD_URL} ...")
    urllib.request.urlretrieve(DB_DOWNLOAD_URL, DB_PATH)
    print(f"Database downloaded ({os.path.getsize(DB_PATH) // 1_000_000} MB)")


def enable_wal_mode():
    """Enable WAL journal mode so readers aren't blocked during pipeline writes."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.close()
        print("SQLite WAL mode enabled")
    except Exception as e:
        print(f"Warning: could not enable WAL mode: {e}")


def ensure_indexes():
    """Idempotent index/table creation on startup.

    - idx_players_name: defensive backstop for queries that filter on
      players.name (team_card, stats router, Haiku SQL fallback).
    - career_ranks / career_franchise_ranks: empty placeholders so
      _build_achievements doesn't crash before build_career_ranks.py has run.
      The builder atomically swaps populated tables into place.
    - player_id_aliases: persistent alias table for the player-id merge
      project. Once populated, pull_live_stats consults it on every
      pull so MSF data lands at the canonical id even when the matcher
      would otherwise produce an alias id.
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_players_name ON players(name)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS career_ranks (
                player_id TEXT NOT NULL,
                side TEXT NOT NULL,
                stat TEXT NOT NULL,
                total REAL NOT NULL,
                mlb_rank INTEGER NOT NULL,
                PRIMARY KEY(player_id, side, stat)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS career_franchise_ranks (
                player_id TEXT NOT NULL,
                side TEXT NOT NULL,
                stat TEXT NOT NULL,
                franchise_code TEXT NOT NULL,
                total REAL NOT NULL,
                fran_rank INTEGER NOT NULL,
                PRIMARY KEY(player_id, side, stat, franchise_code)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS player_id_aliases (
                alias_id TEXT PRIMARY KEY,
                canonical_id TEXT NOT NULL,
                reason TEXT,
                created_at TEXT
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_career_ranks_lookup "
            "ON career_ranks(player_id, side)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_career_franchise_ranks_lookup "
            "ON career_franchise_ranks(player_id, side, franchise_code)"
        )
        # Speeds up team-context leaderboards ("most HR in night games", "best
        # OPS in rain games", etc.). Without these, the WHERE filter on
        # daynight/weather scans the full team_game_results table on every
        # all-time query — multi-second latency. Composite index covers the
        # join keys + filter columns the executor uses.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tgr_join "
            "ON team_game_results(date, opponent, is_home, season)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tgr_daynight "
            "ON team_game_results(daynight)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tgr_gametype "
            "ON team_game_results(gametype)"
        )
        conn.commit()
        conn.close()
        print("Indexes and placeholder tables ensured")
    except Exception as e:
        print(f"Warning: could not ensure indexes: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_db()
    enable_wal_mode()
    ensure_indexes()
    init_metering_db()
    yield


app = FastAPI(
    title="StatChat Backend",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(query.router)
app.include_router(player_card.router)
app.include_router(stats.router)
app.include_router(admin.router)
app.include_router(notable.router)
app.include_router(team_card.router)
app.include_router(client_event.router)
from routers import leaders
app.include_router(leaders.router)
from routers import fireside
app.include_router(fireside.router, prefix="/fireside")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
