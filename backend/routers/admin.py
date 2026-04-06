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


def verify_admin(authorization: str | None, key: str | None = None):
    if not ADMIN_KEY:
        raise HTTPException(503, "ADMIN_KEY not configured on server")
    # Accept either Authorization header or ?key= query param
    if key == ADMIN_KEY:
        return
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


HISTORICAL_SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data_pipeline", "load_historical_gamelogs.py")


@router.post("/load-historical-gamelogs")
async def load_historical_gamelogs(
    start: int = 1920,
    end: int = 2015,
    authorization: str | None = Header(None),
):
    """One-time: load historical game logs from Retrosheet (1920-2015)."""
    verify_admin(authorization)

    cmd = [sys.executable, HISTORICAL_SCRIPT, "--db", DB_PATH,
           "--start", str(start), "--end", str(end)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        return {
            "status": "ok" if result.returncode == 0 else "error",
            "range": f"{start}-{end}",
            "stdout": result.stdout[-3000:] if result.stdout else "",
            "stderr": result.stderr[-1000:] if result.stderr else "",
        }
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "Historical load timed out (60 min limit)")
    except Exception as e:
        raise HTTPException(500, str(e))


@router.delete("/clear-matchup-previews")
async def clear_matchup_previews(
    authorization: str | None = Header(None),
):
    """Delete all matchup_preview events for today so they can be regenerated."""
    verify_admin(authorization)
    try:
        conn = sqlite3.connect(DB_PATH)
        today = date.today().isoformat()
        cur = conn.execute(
            "DELETE FROM notable_events WHERE detection_type = 'matchup_preview' AND game_date = ?",
            (today,)
        )
        conn.commit()
        deleted = cur.rowcount
        conn.close()
        return {"status": "ok", "deleted": deleted}
    except Exception as e:
        raise HTTPException(500, str(e))


@router.post("/refresh-game-contexts")
async def refresh_game_contexts(
    authorization: str | None = Header(None),
):
    """Clear and recompute all game_context values for recent events."""
    verify_admin(authorization)
    try:
        conn = sqlite3.connect(DB_PATH)
        # Clear all game contexts so backfill recomputes them
        conn.execute("UPDATE notable_events SET game_context = NULL")
        conn.commit()
        from services.notable_events import backfill_game_context
        season = date.today().year
        updated = backfill_game_context(conn, season)
        conn.close()
        return {"status": "ok", "updated": updated}
    except Exception as e:
        raise HTTPException(500, str(e))


@router.post("/detect-notable")
async def detect_notable(
    authorization: str | None = Header(None),
):
    """Re-run just the notable events detection (no stats pull or streak detection)."""
    verify_admin(authorization)
    try:
        from services.notable_events import detect_all
        count = detect_all(DB_PATH)
        return {"status": "ok", "events_detected": count}
    except Exception as e:
        raise HTTPException(500, str(e))


@router.api_route("/ai-notable", methods=["GET", "POST"])
async def ai_notable(
    dry_run: bool = True,
    key: str | None = None,
    authorization: str | None = Header(None),
):
    """Run AI-powered notable events detection. dry_run=true returns snapshot only."""
    verify_admin(authorization, key)
    try:
        conn = sqlite3.connect(DB_PATH)
        from services.notable_events import _get_latest_date
        from services.ai_notable_events import generate_ai_insights

        season = date.today().year
        latest_date = _get_latest_date(conn, season)
        if not latest_date:
            conn.close()
            return {"status": "error", "message": "No game logs found"}

        result = generate_ai_insights(conn, season, latest_date, dry_run=dry_run)
        conn.close()
        return {"status": "ok", "dry_run": dry_run, **result}
    except Exception as e:
        import traceback
        raise HTTPException(500, f"{str(e)}\n{traceback.format_exc()}")


@router.post("/poll")
async def poll_new_games(
    key: str | None = None,
    authorization: str | None = Header(None),
):
    """Trigger a lightweight poll for new games."""
    verify_admin(authorization, key)
    poll_script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "data_pipeline", "poll_new_games.py")
    try:
        result = subprocess.run(
            [sys.executable, poll_script, "--db", DB_PATH],
            capture_output=True, text=True, timeout=120,
        )
        return {
            "status": "ok" if result.returncode == 0 else "error",
            "stdout": result.stdout[-2000:] if result.stdout else "",
            "stderr": result.stderr[-500:] if result.stderr else "",
        }
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "Poll timed out")
    except Exception as e:
        raise HTTPException(500, str(e))


@router.get("/debug-player")
async def debug_player(
    name: str,
    key: str | None = None,
    authorization: str | None = Header(None),
):
    """Debug: show game log rows vs season stats for a player."""
    verify_admin(authorization, key)
    conn = sqlite3.connect(DB_PATH)
    player = conn.execute("SELECT player_id, name, team FROM players WHERE name LIKE ?",
                          (f"%{name}%",)).fetchone()
    if not player:
        conn.close()
        return {"error": f"Player not found: {name}"}
    pid = player[0]

    season_stats = conn.execute("""
        SELECT season, games, plate_appearances, at_bats, hits, home_runs, rbi
        FROM season_batting_stats WHERE player_id = ? ORDER BY season DESC LIMIT 3
    """, (pid,)).fetchall()

    game_logs = conn.execute("""
        SELECT date, season, plate_appearances, at_bats, hits, home_runs, rbi
        FROM game_batting_logs WHERE player_id = ? AND season >= 2026
        ORDER BY date ASC
    """, (pid,)).fetchall()

    conn.close()
    return {
        "player_id": pid,
        "name": player[1],
        "team": player[2],
        "season_stats": [
            {"season": r[0], "games": r[1], "pa": r[2], "ab": r[3], "h": r[4], "hr": r[5], "rbi": r[6]}
            for r in season_stats
        ],
        "game_log_count": len(game_logs),
        "game_logs": [
            {"date": r[0], "season": r[1], "pa": r[2], "ab": r[3], "h": r[4], "hr": r[5], "rbi": r[6]}
            for r in game_logs
        ],
    }


@router.post("/fix-doubleheader-schema")
async def fix_doubleheader_schema(
    key: str | None = None,
    authorization: str | None = Header(None),
):
    """Run schema migration to fix doubleheader data loss."""
    verify_admin(authorization, key)
    script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "data_pipeline", "fix_doubleheader_schema.py")
    try:
        result = subprocess.run(
            [sys.executable, script, "--db", DB_PATH],
            capture_output=True, text=True, timeout=600,
        )
        return {
            "status": "ok" if result.returncode == 0 else "error",
            "stdout": result.stdout[-3000:] if result.stdout else "",
            "stderr": result.stderr[-1000:] if result.stderr else "",
        }
    except Exception as e:
        raise HTTPException(500, str(e))


@router.post("/build-historical-index")
async def build_historical_index(
    key: str | None = None,
    authorization: str | None = Header(None),
):
    """One-time: build historical index table for fast lookups."""
    verify_admin(authorization, key)
    script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "data_pipeline", "build_historical_index.py")
    try:
        result = subprocess.run(
            [sys.executable, script, "--db", DB_PATH],
            capture_output=True, text=True, timeout=3600,
        )
        return {
            "status": "ok" if result.returncode == 0 else "error",
            "stdout": result.stdout[-3000:] if result.stdout else "",
            "stderr": result.stderr[-1000:] if result.stderr else "",
        }
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "Index build timed out (60 min limit)")


@router.api_route("/historical-scans", methods=["GET", "POST"])
async def historical_scans(
    as_of: str | None = None,
    key: str | None = None,
    authorization: str | None = Header(None),
):
    """Run historical scans. Optional as_of=YYYY-MM-DD to simulate a past date."""
    verify_admin(authorization, key)
    try:
        conn = sqlite3.connect(DB_PATH)
        from services.notable_events import _get_latest_date
        from services.historical_scans import run_all_scans

        season = date.today().year
        latest_date = as_of or _get_latest_date(conn, season)
        if not latest_date:
            conn.close()
            return {"status": "error", "message": "No game logs found"}

        facts = run_all_scans(conn, season, latest_date)
        from services.historical_scans import template_facts
        templated = template_facts(conn, facts, season, latest_date)
        conn.close()
        return {
            "status": "ok",
            "latest_date": latest_date,
            "num_facts": len(facts),
            "templated_events": templated,
        }
    except Exception as e:
        import traceback
        raise HTTPException(500, f"{str(e)}\n{traceback.format_exc()}")
