"""
Admin endpoints for data management.

POST /admin/refresh — triggers a live data pull from MySportsFeeds.
GET  /admin/freshness — returns when data was last updated.
"""

import os
import sqlite3
import subprocess
import sys
from datetime import date

from fastapi import APIRouter, Header, HTTPException

router = APIRouter(prefix="/admin", tags=["admin"])

DB_PATH = os.getenv("DB_PATH", "/data/baseball_stats_full.db")
ADMIN_KEY = os.getenv("ADMIN_KEY", "")
PIPELINE_SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data_pipeline", "pull_live_stats.py")


def verify_admin(authorization: str | None):
    if not ADMIN_KEY:
        raise HTTPException(503, "ADMIN_KEY not configured on server")
    if authorization != f"Bearer {ADMIN_KEY}":
        raise HTTPException(401, "Invalid admin key")


@router.post("/refresh")
async def refresh_live_data(
    season: str | None = None,
    authorization: str | None = Header(None),
):
    """Trigger a live data refresh from MySportsFeeds."""
    verify_admin(authorization)

    # Auto-detect season if not provided
    if season is None:
        year = date.today().year
        month = date.today().month
        if month < 3 or (month == 3 and date.today().day < 25):
            season = f"{year}-preseason"
        elif month >= 10:
            season = f"{year}-playoff"
        else:
            season = f"{year}-regular"

    cmd = [sys.executable, PIPELINE_SCRIPT, "--season", season, "--db", DB_PATH]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        return {
            "status": "ok" if result.returncode == 0 else "error",
            "season": season,
            "stdout": result.stdout[-2000:] if result.stdout else "",
            "stderr": result.stderr[-1000:] if result.stderr else "",
        }
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "Refresh timed out (5 min limit)")
    except Exception as e:
        raise HTTPException(500, str(e))


@router.get("/schedule")
async def refresh_schedule():
    """Return the current refresh schedule configuration (informational)."""
    year = date.today().year
    month = date.today().month
    if month < 3 or (month == 3 and date.today().day < 25):
        current_phase = "preseason"
        schedule = "Daily at 6:00 AM ET (spring training games)"
    elif month >= 10:
        current_phase = "playoff"
        schedule = "Daily at 6:00 AM ET (postseason)"
    else:
        current_phase = "regular"
        schedule = "Every 4 hours during regular season (6 AM, 10 AM, 2 PM, 6 PM, 10 PM ET)"

    return {
        "current_phase": current_phase,
        "season": f"{year}-{current_phase}",
        "schedule": schedule,
        "note": "Cron jobs configured in Railway. Use POST /admin/refresh to trigger manually.",
    }


@router.get("/freshness")
async def data_freshness():
    """Return when live data was last updated."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT updated_at, season FROM data_freshness WHERE key = 'live_stats'")
        row = cursor.fetchone()
        conn.close()
        if row:
            return {"last_updated": row[0], "season": row[1]}
        return {"last_updated": None, "season": None}
    except Exception:
        return {"last_updated": None, "season": None}
