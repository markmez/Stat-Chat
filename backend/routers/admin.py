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

    # Let the pipeline auto-detect season if not explicitly provided.
    # The pipeline has smart Opening Day detection (probes MSF for regular season data).
    cmd = [sys.executable, PIPELINE_SCRIPT, "--db", DB_PATH]
    if season is not None:
        cmd.extend(["--season", season])
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        return {
            "status": "ok" if result.returncode == 0 else "error",
            "season": season or "auto-detected",
            "stdout": result.stdout[-2000:] if result.stdout else "",
            "stderr": result.stderr[-1000:] if result.stderr else "",
        }
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "Refresh timed out (30 min limit)")
    except Exception as e:
        raise HTTPException(500, str(e))


@router.post("/redownload-db")
async def redownload_db(
    authorization: str | None = Header(None),
):
    """Force re-download the database from S3."""
    verify_admin(authorization)
    import urllib.request
    db_url = os.getenv("DB_DOWNLOAD_URL", "https://stat-chat.s3.us-east-2.amazonaws.com/baseball_stats_full.db")
    try:
        if os.path.exists(DB_PATH):
            old_size = os.path.getsize(DB_PATH)
            os.remove(DB_PATH)
        else:
            old_size = 0
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        urllib.request.urlretrieve(db_url, DB_PATH)
        new_size = os.path.getsize(DB_PATH)
        return {
            "status": "ok",
            "old_size_mb": old_size // 1_000_000,
            "new_size_mb": new_size // 1_000_000,
        }
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
        "note": "Cron jobs configured on Lightsail. Use POST /admin/refresh to trigger manually.",
    }


@router.get("/volume-usage")
async def volume_usage(authorization: str | None = Header(None)):
    """List files on the volume with sizes."""
    verify_admin(authorization)
    data_dir = os.path.dirname(DB_PATH)
    files = []
    total = 0
    try:
        for f in os.listdir(data_dir):
            path = os.path.join(data_dir, f)
            size = os.path.getsize(path) if os.path.isfile(path) else 0
            files.append({"name": f, "size_mb": round(size / 1_000_000, 1)})
            total += size
    except Exception as e:
        return {"error": str(e)}
    files.sort(key=lambda x: x["size_mb"], reverse=True)
    return {"total_mb": round(total / 1_000_000, 1), "files": files}


@router.delete("/volume-cleanup")
async def volume_cleanup(authorization: str | None = Header(None)):
    """Delete orphaned files on the volume (anything not actively used)."""
    verify_admin(authorization)
    data_dir = os.path.dirname(DB_PATH)
    active_db = os.path.basename(DB_PATH)
    deleted = []
    # Keep: the active DB + its WAL/journal, metering.db, lost+found
    keep = {active_db, f"{active_db}-wal", f"{active_db}-journal", f"{active_db}-shm", "metering.db", "lost+found"}
    try:
        for f in os.listdir(data_dir):
            if f not in keep:
                path = os.path.join(data_dir, f)
                if os.path.isfile(path):
                    size = os.path.getsize(path)
                    os.remove(path)
                    deleted.append({"name": f, "size_mb": round(size / 1_000_000, 1)})
    except Exception as e:
        return {"error": str(e), "deleted": deleted}
    return {"deleted": deleted, "freed_mb": round(sum(d["size_mb"] for d in deleted), 1)}


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


@router.get("/todays-games")
async def todays_games(authorization: str | None = Header(None)):
    """Debug endpoint: show today's games and probable pitchers from MSF."""
    verify_admin(authorization)
    try:
        from services.daily_games import get_todays_games
        parsed = get_todays_games()
        return {
            "date": date.today().isoformat(),
            "game_count": len(parsed),
            "games": parsed,
        }
    except Exception as e:
        return {"error": str(e)}
