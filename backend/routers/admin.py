"""
Admin endpoints for data management.

POST /admin/refresh — triggers a live data pull from MySportsFeeds.
GET  /admin/freshness — returns when data was last updated.
"""

import asyncio
import os
import sqlite3
import subprocess
import sys
from datetime import date

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import HTMLResponse


async def _run_subprocess(cmd: list[str], timeout: int = 1800) -> subprocess.CompletedProcess:
    """Run a subprocess without blocking the event loop (so queries keep working)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise subprocess.TimeoutExpired(cmd, timeout)
    # Mimic CompletedProcess
    return subprocess.CompletedProcess(
        cmd, proc.returncode, stdout.decode(), stderr.decode()
    )

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
    full_refresh: bool = False,
    authorization: str | None = Header(None),
):
    """Trigger a live data refresh from MySportsFeeds."""
    verify_admin(authorization)

    # Let the pipeline auto-detect season if not explicitly provided.
    # The pipeline has smart Opening Day detection (probes MSF for regular season data).
    cmd = [sys.executable, PIPELINE_SCRIPT, "--db", DB_PATH]
    if season is not None:
        cmd.extend(["--season", season])
    if full_refresh:
        cmd.append("--full-refresh")
    print(f"REFRESH CMD: {cmd}")
    try:
        result = await _run_subprocess(cmd, timeout=1800)
        return {
            "status": "ok" if result.returncode == 0 else "error",
            "season": season or "auto-detected",
            "stdout": result.stdout[-10000:] if result.stdout else "",
            "stderr": result.stderr[-5000:] if result.stderr else "",
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


@router.get("/refresh-log")
async def refresh_log(
    lines: int = 50,
    authorization: str | None = Header(None),
):
    """Read the last N lines of the refresh log."""
    verify_admin(authorization)
    log_path = "/data/refresh.log"
    if not os.path.exists(log_path):
        return {"lines": []}
    try:
        with open(log_path) as f:
            all_lines = f.readlines()
        return {"lines": [l.rstrip() for l in all_lines[-lines:]]}
    except Exception as e:
        raise HTTPException(500, str(e))


@router.get("/poll-timeline")
async def poll_timeline(
    lines: int = 50,
    authorization: str | None = Header(None),
):
    """Read the last N lines of the poll timeline log."""
    verify_admin(authorization)
    log_path = "/data/poll_timeline.log"
    if not os.path.exists(log_path):
        return {"lines": []}
    try:
        with open(log_path) as f:
            all_lines = f.readlines()
        return {"lines": [l.rstrip() for l in all_lines[-lines:]]}
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
        result = await _run_subprocess(cmd, timeout=3600)
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


@router.post("/purge-duplicate-streaks")
async def purge_duplicate_streaks(
    authorization: str | None = Header(None),
):
    """Keep only the latest (highest streak) event per player + type + date."""
    verify_admin(authorization)
    try:
        conn = sqlite3.connect(DB_PATH)
        # For each streak type, keep only the row with the highest id (latest insert)
        # per player_names + detection_type + game_date combo
        streak_types = ("hitting_streak", "onbase_streak", "hr_streak",
                        "pitching_streak", "cross_season_hitting_streak",
                        "cross_season_on_base_streak")
        placeholders = ",".join("?" * len(streak_types))
        cur = conn.execute(f"""
            DELETE FROM notable_events WHERE id NOT IN (
                SELECT MAX(id) FROM notable_events
                WHERE detection_type IN ({placeholders})
                GROUP BY detection_type, game_date, player_names
            ) AND detection_type IN ({placeholders})
        """, streak_types + streak_types)
        conn.commit()
        deleted = cur.rowcount
        conn.close()
        return {"status": "ok", "deleted": deleted}
    except Exception as e:
        raise HTTPException(500, str(e))


@router.post("/fix-duplicate-players")
async def fix_duplicate_players(
    authorization: str | None = Header(None),
):
    """Reset corrupted duplicate player entries to their Retrosheet originals.
    For players with duplicate names, resets the OLDER entry's name/team back
    to what Retrosheet had, undoing any MSF overwrites."""
    verify_admin(authorization)
    try:
        conn = sqlite3.connect(DB_PATH)
        # Find all player_ids that share a name with another player
        dupes = conn.execute("""
            SELECT p1.player_id, p1.name, p1.team,
                   MAX(COALESCE(s.season, sp.season, 0)) as last_active
            FROM players p1
            JOIN players p2 ON p1.name = p2.name AND p1.player_id != p2.player_id
            LEFT JOIN season_batting_stats s ON p1.player_id = s.player_id
            LEFT JOIN season_pitching_stats sp ON p1.player_id = sp.player_id
            GROUP BY p1.player_id
        """).fetchall()

        # For each name, keep the most recently active, reset others
        from collections import defaultdict
        by_name = defaultdict(list)
        for pid, name, team, last_active in dupes:
            by_name[name].append((pid, team, last_active or 0))

        fixed = 0
        for name, entries in by_name.items():
            if len(entries) < 2:
                continue
            entries.sort(key=lambda x: x[2], reverse=True)
            # The first one is the active player — leave it alone
            # Reset all others: clear any MSF name overwrites by keeping the name
            # but restoring the original team from their most recent season stats
            for pid, team, last_active in entries[1:]:
                # Get their original team from their most recent season
                orig = conn.execute("""
                    SELECT team FROM season_batting_stats
                    WHERE player_id = ? ORDER BY season DESC LIMIT 1
                """, (pid,)).fetchone()
                if orig and orig[0] and orig[0] != team:
                    conn.execute("UPDATE players SET team = ? WHERE player_id = ?",
                                (orig[0], pid))
                    fixed += 1

        conn.commit()
        conn.close()
        return {"status": "ok", "fixed": fixed}
    except Exception as e:
        raise HTTPException(500, str(e))


@router.post("/repair-game-logs")
async def repair_game_logs(
    season: str = "2025-regular",
    authorization: str | None = Header(None),
):
    """Re-pull game logs only for a specific season. Runs in background — returns immediately."""
    verify_admin(authorization)
    try:
        pipeline_dir = os.path.dirname(PIPELINE_SCRIPT)
        repair_script = os.path.join(pipeline_dir, "repair_game_logs.py")
        log_file = "/data/repair_game_logs.log"
        cmd = f"{sys.executable} {repair_script} --season {season} --db {DB_PATH} > {log_file} 2>&1 &"
        os.system(cmd)
        return {"status": "started", "log": log_file, "season": season}
    except Exception as e:
        raise HTTPException(500, str(e))


@router.post("/run-sql")
async def run_sql(
    sql: str = "",
    authorization: str | None = Header(None),
):
    """Run a SQL statement against the DB. USE WITH EXTREME CAUTION."""
    verify_admin(authorization)
    if not sql:
        raise HTTPException(400, "No SQL provided")
    try:
        conn = sqlite3.connect(DB_PATH)
        if sql.strip().upper().startswith("SELECT"):
            rows = conn.execute(sql).fetchall()
            conn.close()
            return {"rows": [list(r) for r in rows[:100]]}
        else:
            cur = conn.execute(sql)
            conn.commit()
            conn.close()
            return {"status": "ok", "rows_affected": cur.rowcount}
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


@router.get("/debug-intercept")
async def debug_intercept(
    q: str = "",
    key: str | None = None,
    authorization: str | None = Header(None),
):
    """Debug: run try_intercept and return result or error."""
    verify_admin(authorization, key)
    from services.interceptor import try_intercept
    try:
        result = try_intercept(q)
        return {
            "intercepted": result is not None,
            "result_preview": (result or "")[:500],
            "error": None,
        }
    except Exception as e:
        import traceback
        return {
            "intercepted": False,
            "result_preview": "",
            "error": f"{type(e).__name__}: {e}\n{traceback.format_exc()[-500:]}",
        }


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


@router.post("/seed-event-archive")
async def seed_event_archive(
    key: str | None = None,
    authorization: str | None = Header(None),
):
    """One-time: seed event_archive from current notable_events."""
    verify_admin(authorization, key)
    stats_conn = sqlite3.connect(DB_PATH)
    rows = stats_conn.execute("""
        SELECT headline, detection_type, game_date FROM notable_events
    """).fetchall()
    stats_conn.close()

    from services.metering import archive_events
    events = [{"headline": r[0], "detection_type": r[1], "game_date": r[2]} for r in rows]
    count = archive_events(events)
    return {"status": "ok", "seeded": len(events)}


@router.get("/debug-decompose")
async def debug_decompose(
    q: str = "",
    key: str | None = None,
    authorization: str | None = Header(None),
):
    """Debug: show what the query engine produces for a question."""
    verify_admin(authorization, key)
    from services.query_engine import decompose, execute as qe_execute
    plan = decompose(q)
    error = None
    try:
        result = qe_execute(plan) if plan.is_valid else None
    except Exception as e:
        result = None
        error = f"{type(e).__name__}: {e}"
    return {
        "valid": plan.is_valid,
        "type": plan.query_type,
        "stat": plan.stat.db_column if plan.stat else None,
        "player_name": plan.player_name,
        "game_log_stat": plan.game_log_stat,
        "unexplained": plan.unexplained_words,
        "has_result": result is not None,
        "error": error,
        "streak_length": plan.streak_length,
        "threshold": plan.threshold,
        "result_preview": (result or "")[:300],
    }


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
        result = await _run_subprocess(
            [sys.executable, poll_script, "--db", DB_PATH],
            timeout=120,
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
        result = await _run_subprocess(
            [sys.executable, script, "--db", DB_PATH],
            timeout=600,
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
        result = await _run_subprocess(
            [sys.executable, script, "--db", DB_PATH],
            timeout=3600,
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


BACKFILL_SB_CS_SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                     "data_pipeline", "backfill_sb_cs.py")


@router.post("/backfill-sb-cs")
async def backfill_sb_cs(
    start: int | None = None,
    end: int | None = None,
    dry_run: bool = False,
    key: str | None = None,
    authorization: str | None = Header(None),
):
    """One-time: backfill stolen_bases/caught_stealing in game_batting_logs from Retrosheet."""
    verify_admin(authorization, key)
    cmd = [sys.executable, BACKFILL_SB_CS_SCRIPT, "--db", DB_PATH]
    if start is not None:
        cmd.extend(["--start", str(start)])
    if end is not None:
        cmd.extend(["--end", str(end)])
    if dry_run:
        cmd.append("--dry-run")
    try:
        result = await _run_subprocess(cmd, timeout=3600)
        return {
            "status": "ok" if result.returncode == 0 else "error",
            "stdout": result.stdout[-5000:] if result.stdout else "",
            "stderr": result.stderr[-2000:] if result.stderr else "",
        }
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "Backfill timed out (60 min limit)")
    except Exception as e:
        raise HTTPException(500, str(e))


METERING_DB_PATH = os.getenv(
    "METERING_DB_PATH",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "metering.db"),
)

# Cost estimates per query by response type
_COST_PER_QUERY = {"query engine": 0.0, "intercepted": 0.0, "haiku": 0.002, "sonnet": 0.02, "query_engine_error": 0.0}


def _to_eastern(iso_ts: str) -> str:
    """Convert an ISO UTC timestamp to Eastern Time display string."""
    from datetime import datetime, timezone, timedelta
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        # Eastern = UTC-4 (EDT) or UTC-5 (EST). Use -4 for Apr-Nov.
        eastern = dt.astimezone(timezone(timedelta(hours=-4)))
        return eastern.strftime("%-m/%-d %I:%M %p").lstrip("0")
    except Exception:
        return iso_ts[:16].replace("T", " ")


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    key: str | None = None,
    authorization: str | None = Header(None),
    date_from: str | None = None,
    date_to: str | None = None,
):
    """Admin dashboard showing query analytics."""
    verify_admin(authorization, key)

    conn = sqlite3.connect(METERING_DB_PATH)

    # Migrate legacy "intercepted" rows
    conn.execute("UPDATE query_log SET response_type = 'query engine' WHERE response_type = 'intercepted'")
    conn.commit()

    # Date range filter
    date_filter = ""
    date_params = []
    if date_from:
        date_filter += " AND timestamp >= ?"
        date_params.append(date_from)
    if date_to:
        date_filter += " AND timestamp <= ?"
        date_params.append(date_to + "T23:59:59")

    # All queries ranked by count, tiebroken by recency
    queries = conn.execute(f"""
        SELECT query_text, COUNT(*) as cnt,
               MAX(timestamp) as last_seen,
               GROUP_CONCAT(DISTINCT response_type) as types
        FROM query_log
        WHERE 1=1{date_filter}
        GROUP BY query_text
        ORDER BY cnt DESC, last_seen DESC
        LIMIT 1000
    """, date_params).fetchall()

    # Breakdown by response type
    breakdown = conn.execute(f"""
        SELECT response_type, COUNT(*) as cnt
        FROM query_log
        WHERE 1=1{date_filter}
        GROUP BY response_type
        ORDER BY cnt DESC
    """, date_params).fetchall()

    total = sum(r[1] for r in breakdown)
    conn.close()

    # Build breakdown rows
    breakdown_html = ""
    total_cost = 0.0
    for rtype, cnt in breakdown:
        pct = (cnt / total * 100) if total else 0
        cost = cnt * _COST_PER_QUERY.get(rtype, 0)
        total_cost += cost
        breakdown_html += f"""
        <tr>
            <td><span class="badge {rtype.replace(' ', '-')}">{rtype}</span></td>
            <td>{cnt:,}</td>
            <td>{pct:.1f}%</td>
            <td>${cost:.3f}</td>
        </tr>"""

    breakdown_html += f"""
    <tr class="total-row">
        <td><strong>Total</strong></td>
        <td><strong>{total:,}</strong></td>
        <td><strong>100%</strong></td>
        <td><strong>${total_cost:.3f}</strong></td>
    </tr>"""

    # Build query rows
    query_rows = ""
    for text, cnt, last_seen, types in queries:
        # Convert UTC timestamp to Eastern
        ts_display = _to_eastern(last_seen) if last_seen else ""
        # Build type badges
        type_list = [t.strip() for t in (types or "").split(",")]
        type_badges = " ".join(
            f'<span class="badge {t.replace(" ", "-")}" onclick="filterBy(\'{t}\')">{t}</span>'
            for t in type_list
        )
        escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        raw_ts = last_seen or ""
        types_attr = ",".join(type_list)
        query_rows += f"""
        <tr data-count="{cnt}" data-ts="{raw_ts}" data-types="{types_attr}">
            <td class="query-text">{escaped}</td>
            <td class="count">{cnt}</td>
            <td class="types">{type_badges}</td>
            <td class="timestamp">{ts_display}</td>
        </tr>"""

    # Build events table from archive + tap counts
    conn2 = sqlite3.connect(METERING_DB_PATH)
    event_rows_data = conn2.execute("""
        SELECT e.headline, e.detection_type, e.game_date,
               COALESCE(t.taps, 0) as taps
        FROM event_archive e
        LEFT JOIN (
            SELECT headline, COUNT(*) as taps FROM event_taps GROUP BY headline
        ) t ON e.headline = t.headline
        ORDER BY e.game_date DESC, taps DESC
    """).fetchall()
    conn2.close()

    # Map detection types to clean display categories
    def _event_category(dtype):
        if not dtype:
            return "Other"
        if dtype.startswith("career_"):
            return "Milestone"
        _map = {
            "ai_insight": "AI Insight",
            "historical_scan": "Historical",
            "hitting_streak": "Streak",
            "onbase_streak": "Streak",
            "scoreless_streak": "Streak",
            "hr_streak": "Streak",
            "pitching_streak": "Streak",
            "matchup_preview": "Matchup",
            "on_this_date": "On This Date",
            "leaderboard_change": "Leader Change",
        }
        return _map.get(dtype, dtype.replace("_", " ").title())

    event_rows = ""
    for headline, dtype, gdate, taps in event_rows_data:
        escaped_h = headline.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        cat = _event_category(dtype)
        css_cat = cat.lower().replace(" ", "-")
        event_rows += f"""
        <tr data-taps="{taps}" data-date="{gdate}" data-etype="{cat}">
            <td class="query-text">{escaped_h}</td>
            <td><span class="badge evt-{css_cat}" onclick="filterEvents('{cat}')">{cat}</span></td>
            <td class="count">{taps}</td>
            <td class="timestamp">{gdate}</td>
        </tr>"""

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>StatChat Dashboard</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, system-ui, sans-serif; background: #fff; color: #1d1d1f; padding: 16px; }}
  h1 {{
    font-size: 22px; margin-bottom: 16px; font-weight: 700;
    background: linear-gradient(to right, #73B3FF, #1A40B3);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent;
  }}
  h2 {{ font-size: 15px; margin: 24px 0 8px; color: #1A40B3; font-weight: 600; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th {{ text-align: left; padding: 8px 6px; border-bottom: 2px solid #e0e8f5; color: #1A40B3; font-weight: 600; position: sticky; top: 0; background: #fff; }}
  td {{ padding: 6px; border-bottom: 1px solid #f0f0f0; vertical-align: top; }}
  .count {{ text-align: center; font-variant-numeric: tabular-nums; }}
  .timestamp {{ color: #999; font-size: 11px; white-space: nowrap; }}
  .query-text {{ max-width: 55vw; word-break: break-word; }}
  .types {{ white-space: nowrap; }}
  .badge {{
    display: inline-block; padding: 2px 6px; border-radius: 4px;
    font-size: 10px; font-weight: 600; text-transform: uppercase;
  }}
  .badge.intercepted, .badge.query-engine {{ background: #dcfce7; color: #166534; }}
  .badge.query-engine-error {{ background: #fee2e2; color: #991b1b; }}
  .badge.evt-ai-insight {{ background: #fef3c7; color: #92400e; }}
  .badge.evt-historical {{ background: #e0e7ff; color: #3730a3; }}
  .badge.evt-streak {{ background: #fce7f3; color: #9d174d; }}
  .badge.evt-milestone {{ background: #d1fae5; color: #065f46; }}
  .badge.evt-matchup {{ background: #ede9fe; color: #5b21b6; }}
  .badge.evt-on-this-date {{ background: #f0f9ff; color: #0c4a6e; }}
  .badge.evt-leader-change {{ background: #fff7ed; color: #9a3412; }}
  .badge.haiku {{ background: #dbeafe; color: #1A40B3; }}
  .badge.sonnet {{ background: #f3e8ff; color: #6b21a8; }}
  .breakdown {{ margin-bottom: 24px; }}
  .breakdown table {{ max-width: 500px; }}
  .breakdown td, .breakdown th {{ padding: 8px 12px; }}
  .total-row td {{ border-top: 2px solid #1A40B3; }}
  .stat-cards {{ display: flex; gap: 12px; margin-bottom: 20px; flex-wrap: wrap; }}
  .stat-card {{
    background: linear-gradient(135deg, #1A40B3, #73B3FF);
    border-radius: 10px; padding: 12px 16px; min-width: 100px;
    box-shadow: 0 2px 8px rgba(26, 64, 179, 0.12);
  }}
  .stat-card .label {{ font-size: 11px; color: rgba(255,255,255,0.8); text-transform: uppercase; }}
  .stat-card .value {{ font-size: 24px; font-weight: 700; color: #fff; }}
  th.sortable {{ cursor: pointer; user-select: none; }}
  th.sortable:active {{ opacity: 0.6; }}
  th .arrow {{ font-size: 20px; margin-left: 3px; display: inline-block; transform: translateY(2px); }}
  .types .badge {{ cursor: pointer; }}
  .types .badge:active {{ opacity: 0.6; }}
  .filter-bar {{
    display: none; align-items: center; gap: 8px; margin-bottom: 8px;
    padding: 6px 10px; background: #f5f7fa; border-radius: 6px; font-size: 13px;
  }}
  .filter-bar.active {{ display: flex; }}
  .filter-bar .filter-x {{
    cursor: pointer; font-size: 16px; color: #999; margin-left: 2px;
    line-height: 1; font-weight: 600;
  }}
  .filter-bar .filter-x:active {{ color: #333; }}
  .date-picker {{
    display: flex; align-items: center; gap: 8px; margin-bottom: 16px;
    font-size: 13px; flex-wrap: wrap;
  }}
  .date-picker input {{
    padding: 4px 8px; border: 1px solid #ddd; border-radius: 6px;
    font-size: 13px; font-family: inherit;
  }}
  .date-picker button {{
    padding: 4px 12px; border: 1px solid #1A40B3; border-radius: 6px;
    background: #1A40B3; color: #fff; font-size: 13px; cursor: pointer;
    font-family: inherit;
  }}
  .date-picker button:active {{ opacity: 0.8; }}
  .date-picker .reset {{ background: #fff; color: #1A40B3; }}
  .pagination {{
    display: flex; align-items: center; justify-content: center;
    gap: 12px; padding: 12px 0; font-size: 13px;
  }}
  .pagination button {{
    padding: 6px 14px; border: 1px solid #ddd; border-radius: 6px;
    background: #fff; cursor: pointer; font-size: 13px; font-family: inherit;
  }}
  .pagination button:disabled {{ opacity: 0.3; cursor: default; }}
  .pagination button:not(:disabled):active {{ background: #f0f0f0; }}
  .pagination .page-info {{ color: #888; }}
</style>
</head>
<body>
<h1>StatChat Dashboard</h1>

<div class="stat-cards">
  <div class="stat-card">
    <div class="label">Total Queries</div>
    <div class="value">{total:,}</div>
  </div>
  <div class="stat-card">
    <div class="label">Unique Queries</div>
    <div class="value">{len(queries):,}</div>
  </div>
</div>

<h2>Cost Breakdown by Response Type</h2>
<div class="breakdown">
<table>
  <tr><th>Type</th><th>Count</th><th>%</th><th>Est. Cost</th></tr>
  {breakdown_html}
</table>
</div>

<h2>All Queries</h2>
<div class="date-picker">
  <label>From:</label>
  <input type="date" id="date-from" value="{date_from or ''}">
  <label>To:</label>
  <input type="date" id="date-to" value="{date_to or ''}">
  <button onclick="applyDateRange()">Apply</button>
  <button class="reset" onclick="resetDateRange()">Reset</button>
</div>
<div class="filter-bar" id="filter-bar">
  Showing: <span id="filter-label"></span>
  <span class="filter-x" onclick="clearFilter()">&times;</span>
</div>
<table id="qtable">
  <tr>
    <th>Query</th>
    <th class="sortable" onclick="sortBy('count')" id="th-count">Count<span class="arrow"> &#x25BE;</span></th>
    <th>Type</th>
    <th class="sortable" onclick="sortBy('time')" id="th-time">Last (ET)</th>
  </tr>
  {query_rows}
</table>
<div class="pagination">
  <button id="prev-btn" onclick="changePage(-1)" disabled>&larr; Prev</button>
  <span class="page-info" id="page-info"></span>
  <button id="next-btn" onclick="changePage(1)">Next &rarr;</button>
</div>

<h2>Feed Events</h2>
<div class="date-picker">
  <label>From:</label>
  <input type="date" id="evt-date-from">
  <label>To:</label>
  <input type="date" id="evt-date-to">
  <button onclick="applyEvtDateRange()">Apply</button>
  <button class="reset" onclick="clearEvtDateRange()">Reset</button>
</div>
<div class="filter-bar" id="evt-filter-bar">
  Showing: <span id="evt-filter-label"></span>
  <span class="filter-x" onclick="clearEvtFilter()">&times;</span>
</div>
<table id="etable">
  <tr>
    <th>Event</th>
    <th>Type</th>
    <th class="sortable" onclick="sortEvents('taps')" id="eth-taps">Taps</th>
    <th class="sortable" onclick="sortEvents('date')" id="eth-date">Game Date<span class="arrow"> &#x25BE;</span></th>
  </tr>
  {event_rows}
</table>
<div class="pagination">
  <button id="evt-prev" onclick="changeEvtPage(-1)" disabled>&larr; Prev</button>
  <span class="page-info" id="evt-page-info"></span>
  <button id="evt-next" onclick="changeEvtPage(1)">Next &rarr;</button>
</div>
<script>
const PAGE_SIZE = 30;
let currentSort = 'count';
let currentFilter = null;
let currentPage = 0;

function getVisibleRows() {{
  const all = Array.from(document.querySelectorAll('#qtable tr[data-count]'));
  return currentFilter
    ? all.filter(r => r.dataset.types.split(',').includes(currentFilter))
    : all;
}}

function renderPage() {{
  const visible = getVisibleRows();
  const totalPages = Math.ceil(visible.length / PAGE_SIZE);
  if (currentPage >= totalPages) currentPage = Math.max(0, totalPages - 1);
  const start = currentPage * PAGE_SIZE;
  const end = start + PAGE_SIZE;
  // Hide all, show page
  document.querySelectorAll('#qtable tr[data-count]').forEach(r => r.style.display = 'none');
  visible.forEach((r, i) => {{
    r.style.display = (i >= start && i < end) ? '' : 'none';
  }});
  document.getElementById('page-info').textContent = visible.length > 0
    ? `${{start + 1}}-${{Math.min(end, visible.length)}} of ${{visible.length}}`
    : 'No results';
  document.getElementById('prev-btn').disabled = currentPage === 0;
  document.getElementById('next-btn').disabled = currentPage >= totalPages - 1;
}}

function changePage(delta) {{
  currentPage += delta;
  renderPage();
}}

function sortBy(mode) {{
  currentSort = mode;
  currentPage = 0;
  const table = document.getElementById('qtable');
  const rows = Array.from(table.querySelectorAll('tr[data-count]'));
  rows.sort((a, b) => {{
    if (mode === 'time') return b.dataset.ts.localeCompare(a.dataset.ts);
    const dc = parseInt(b.dataset.count) - parseInt(a.dataset.count);
    return dc !== 0 ? dc : b.dataset.ts.localeCompare(a.dataset.ts);
  }});
  rows.forEach(r => table.appendChild(r));
  document.getElementById('th-count').innerHTML = 'Count' + (mode === 'count' ? '<span class="arrow"> &#x25BE;</span>' : '');
  document.getElementById('th-time').innerHTML = 'Last (ET)' + (mode === 'time' ? '<span class="arrow"> &#x25BE;</span>' : '');
  renderPage();
}}

function filterBy(type) {{
  currentFilter = type;
  currentPage = 0;
  const bar = document.getElementById('filter-bar');
  const label = document.getElementById('filter-label');
  const css = type.replace(' ', '-');
  label.innerHTML = '<span class="badge ' + css + '">' + type + '</span>';
  bar.classList.add('active');
  renderPage();
}}

function clearFilter() {{
  currentFilter = null;
  currentPage = 0;
  document.getElementById('filter-bar').classList.remove('active');
  renderPage();
}}

function applyDateRange() {{
  const from = document.getElementById('date-from').value;
  const to = document.getElementById('date-to').value;
  const url = new URL(window.location);
  if (from) url.searchParams.set('date_from', from); else url.searchParams.delete('date_from');
  if (to) url.searchParams.set('date_to', to); else url.searchParams.delete('date_to');
  window.location = url;
}}

function resetDateRange() {{
  const url = new URL(window.location);
  url.searchParams.delete('date_from');
  url.searchParams.delete('date_to');
  window.location = url;
}}

// Initial render
renderPage();

// --- Events table ---
const EVT_PAGE = 30;
let evtPage = 0;
let evtFilter = null;
let evtSort = 'date';

function getVisibleEvents() {{
  let all = Array.from(document.querySelectorAll('#etable tr[data-taps]'));
  if (evtFilter) all = all.filter(r => r.dataset.etype === evtFilter);
  if (evtDateFrom) all = all.filter(r => r.dataset.date >= evtDateFrom);
  if (evtDateTo) all = all.filter(r => r.dataset.date <= evtDateTo);
  return all;
}}

function renderEvents() {{
  const visible = getVisibleEvents();
  const totalPages = Math.ceil(visible.length / EVT_PAGE);
  if (evtPage >= totalPages) evtPage = Math.max(0, totalPages - 1);
  const start = evtPage * EVT_PAGE;
  const end = start + EVT_PAGE;
  document.querySelectorAll('#etable tr[data-taps]').forEach(r => r.style.display = 'none');
  visible.forEach((r, i) => r.style.display = (i >= start && i < end) ? '' : 'none');
  document.getElementById('evt-page-info').textContent = visible.length > 0
    ? `${{start + 1}}-${{Math.min(end, visible.length)}} of ${{visible.length}}`
    : 'No events';
  document.getElementById('evt-prev').disabled = evtPage === 0;
  document.getElementById('evt-next').disabled = evtPage >= totalPages - 1;
}}

function changeEvtPage(d) {{ evtPage += d; renderEvents(); }}

function sortEvents(mode) {{
  evtSort = mode;
  evtPage = 0;
  const table = document.getElementById('etable');
  const rows = Array.from(table.querySelectorAll('tr[data-taps]'));
  rows.sort((a, b) => {{
    if (mode === 'taps') return parseInt(b.dataset.taps) - parseInt(a.dataset.taps);
    return b.dataset.date.localeCompare(a.dataset.date);
  }});
  rows.forEach(r => table.appendChild(r));
  document.getElementById('eth-date').innerHTML = 'Game Date' + (mode === 'date' ? '<span class="arrow"> &#x25BE;</span>' : '');
  document.getElementById('eth-taps').innerHTML = 'Taps' + (mode === 'taps' ? '<span class="arrow"> &#x25BE;</span>' : '');
  renderEvents();
}}

function filterEvents(type) {{
  evtFilter = type;
  evtPage = 0;
  document.getElementById('evt-filter-bar').classList.add('active');
  const css = type.replace(' ', '-');
  document.getElementById('evt-filter-label').innerHTML = '<span class="badge ' + css + '">' + type + '</span>';
  renderEvents();
}}

function clearEvtFilter() {{
  evtFilter = null;
  evtPage = 0;
  document.getElementById('evt-filter-bar').classList.remove('active');
  renderEvents();
}}

let evtDateFrom = null, evtDateTo = null;

function getVisibleEventsFiltered() {{
  let all = Array.from(document.querySelectorAll('#etable tr[data-taps]'));
  if (evtFilter) all = all.filter(r => r.dataset.etype === evtFilter);
  if (evtDateFrom) all = all.filter(r => r.dataset.date >= evtDateFrom);
  if (evtDateTo) all = all.filter(r => r.dataset.date <= evtDateTo);
  return all;
}}

function applyEvtDateRange() {{
  evtDateFrom = document.getElementById('evt-date-from').value || null;
  evtDateTo = document.getElementById('evt-date-to').value || null;
  evtPage = 0;
  renderEvents();
}}

function clearEvtDateRange() {{
  evtDateFrom = null;
  evtDateTo = null;
  document.getElementById('evt-date-from').value = '';
  document.getElementById('evt-date-to').value = '';
  evtPage = 0;
  renderEvents();
}}

renderEvents();
</script>
</body>
</html>"""

    return HTMLResponse(content=html)


# ---------------------------------------------------------------------------
# Records system endpoints
# ---------------------------------------------------------------------------

RECORDS_SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data_pipeline", "build_records.py",
)

# Team display names — same mapping used in notable_events.py
_RETRO_TO_DISPLAY = {
    "NYA": "Yankees", "NYN": "Mets", "LAN": "Dodgers", "ANA": "Angels",
    "CHN": "Cubs", "CHA": "White Sox", "SFN": "Giants", "SDN": "Padres",
    "SLN": "Cardinals", "KCA": "Royals", "TBA": "Rays", "WAS": "Nationals",
    "BOS": "Red Sox", "HOU": "Astros", "ATL": "Braves", "PHI": "Phillies",
    "TEX": "Rangers", "TOR": "Blue Jays", "BAL": "Orioles", "MIN": "Twins",
    "CLE": "Guardians", "SEA": "Mariners", "MIL": "Brewers", "CIN": "Reds",
    "PIT": "Pirates", "DET": "Tigers", "ARI": "Diamondbacks", "COL": "Rockies",
    "MIA": "Marlins", "OAK": "Athletics", "ATH": "Athletics",
}


def _team_display(code):
    return _RETRO_TO_DISPLAY.get(code, code)


@router.post("/build-records")
async def build_records(
    key: str | None = None,
    authorization: str | None = Header(None),
):
    """Build pre-computed team and MLB records tables."""
    verify_admin(authorization, key)
    try:
        result = await _run_subprocess(
            [sys.executable, RECORDS_SCRIPT, "--db", DB_PATH],
            timeout=3600,
        )
        return {
            "status": "ok" if result.returncode == 0 else "error",
            "stdout": result.stdout[-5000:] if result.stdout else "",
            "stderr": result.stderr[-2000:] if result.stderr else "",
        }
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "Records build timed out (60 min limit)")
    except Exception as e:
        raise HTTPException(500, str(e))


@router.get("/records-lookup")
async def records_lookup(
    name: str = "",
    key: str | None = None,
    authorization: str | None = Header(None),
):
    """Look up a player's career stats and compare against records."""
    verify_admin(authorization, key)
    if not name:
        raise HTTPException(400, "Missing name parameter")

    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        # Find player
        player = conn.execute(
            "SELECT player_id, name, team, positions FROM players WHERE name LIKE ? ORDER BY name LIMIT 10",
            (f"%{name}%",),
        ).fetchall()
        if not player:
            return {"error": f"No player found matching '{name}'", "results": []}

        results = []
        for pid, pname, team, positions in player:
            # Career batting totals
            bat = conn.execute("""
                SELECT SUM(games), SUM(plate_appearances), SUM(at_bats), SUM(hits),
                       SUM(home_runs), SUM(rbi), SUM(runs), SUM(stolen_bases),
                       SUM(doubles), SUM(walks), SUM(strikeouts)
                FROM season_batting_stats WHERE player_id = ?
            """, (pid,)).fetchone()

            # Career pitching totals
            pitch = conn.execute("""
                SELECT SUM(games), SUM(wins), SUM(losses), SUM(saves),
                       SUM(strikeouts), SUM(ip_outs), SUM(earned_runs),
                       SUM(hits), SUM(walks)
                FROM season_pitching_stats WHERE player_id = ?
            """, (pid,)).fetchone()

            career = {}
            if bat and bat[0]:
                career["batting"] = {
                    "games": bat[0], "pa": bat[1], "ab": bat[2], "hits": bat[3],
                    "home_runs": bat[4], "rbi": bat[5], "runs": bat[6],
                    "stolen_bases": bat[7], "doubles": bat[8], "walks": bat[9],
                    "strikeouts": bat[10],
                    "avg": round(bat[3] / bat[2], 3) if bat[2] else None,
                }
            if pitch and pitch[0]:
                ip = round(pitch[5] / 3, 1) if pitch[5] else 0
                career["pitching"] = {
                    "games": pitch[0], "wins": pitch[1], "losses": pitch[2],
                    "saves": pitch[3], "strikeouts": pitch[4], "ip": ip,
                    "era": round(pitch[6] * 9 / (pitch[5] / 3), 2) if pitch[5] else None,
                }

            # Current team — use most recent season entry
            current_team_row = conn.execute("""
                SELECT team FROM season_batting_stats WHERE player_id = ?
                UNION ALL
                SELECT team FROM season_pitching_stats WHERE player_id = ?
                ORDER BY 1 DESC LIMIT 1
            """, (pid, pid)).fetchone()

            # Get all teams this player has played for
            team_rows = conn.execute("""
                SELECT DISTINCT team FROM season_batting_stats WHERE player_id = ?
                UNION
                SELECT DISTINCT team FROM season_pitching_stats WHERE player_id = ?
            """, (pid, pid)).fetchall()
            player_teams = set()
            for (t,) in team_rows:
                if t:
                    for code in t.split("/"):
                        code = code.strip()
                        if code:
                            player_teams.add(code)

            # Compare against team records
            team_records_approaching = []
            for tc in sorted(player_teams):
                records = conn.execute("""
                    SELECT stat, record_type, value, player_name, player_id
                    FROM team_records
                    WHERE team_code = ?
                    ORDER BY stat, record_type, value DESC
                """, (tc,)).fetchall()

                for stat, rtype, val, rec_name, rec_pid in records:
                    # Get this player's value for this stat
                    if rtype == "career":
                        my_val = _get_career_value(conn, pid, stat)
                    else:
                        continue  # Skip season/game for approach detection

                    if my_val is not None and val is not None:
                        diff = val - my_val
                        if 0 < diff <= 10:
                            team_records_approaching.append({
                                "team": tc,
                                "team_name": _team_display(tc),
                                "stat": stat,
                                "record_type": rtype,
                                "record_value": val,
                                "record_holder": rec_name,
                                "my_value": my_val,
                                "diff": diff,
                            })
                        elif diff <= 0 and rec_pid == pid:
                            team_records_approaching.append({
                                "team": tc,
                                "team_name": _team_display(tc),
                                "stat": stat,
                                "record_type": rtype,
                                "record_value": val,
                                "record_holder": rec_name,
                                "my_value": my_val,
                                "diff": 0,
                                "holds_record": True,
                            })

            # Compare against MLB records
            mlb_records_near = []
            mlb_recs = conn.execute("""
                SELECT stat, record_type, value, player_name, player_id
                FROM mlb_records
                ORDER BY stat, record_type
            """).fetchall()
            for stat, rtype, val, rec_name, rec_pid in mlb_recs:
                if rtype == "career":
                    my_val = _get_career_value(conn, pid, stat)
                    if my_val is not None and val is not None:
                        diff = val - my_val
                        if 0 < diff <= 10:
                            mlb_records_near.append({
                                "stat": stat,
                                "record_type": rtype,
                                "record_value": val,
                                "record_holder": rec_name,
                                "my_value": my_val,
                                "diff": diff,
                            })

            # Recent game logs (last 7 days) — find career highs
            recent_highs = _find_recent_career_highs(conn, pid)

            results.append({
                "player_id": pid,
                "name": pname,
                "team": team,
                "positions": positions,
                "career": career,
                "team_records_approaching": team_records_approaching,
                "mlb_records_approaching": mlb_records_near,
                "recent_career_highs": recent_highs,
            })

        return {"results": results}
    finally:
        conn.close()


def _get_career_value(conn, player_id, stat):
    """Get a player's career total for a stat."""
    # Batting stats
    batting_stats = {
        "games": "SUM(games)", "home_runs": "SUM(home_runs)",
        "hits": "SUM(hits)", "rbi": "SUM(rbi)", "runs": "SUM(runs)",
        "stolen_bases": "SUM(stolen_bases)", "doubles": "SUM(doubles)",
        "walks": "SUM(walks)",
    }
    if stat in batting_stats:
        row = conn.execute(
            f"SELECT {batting_stats[stat]} FROM season_batting_stats WHERE player_id = ?",
            (player_id,),
        ).fetchone()
        return row[0] if row and row[0] else None

    # Pitching stats
    pitching_stats = {
        "wins": "SUM(wins)", "strikeouts": "SUM(strikeouts)",
        "saves": "SUM(saves)",
    }
    if stat in pitching_stats:
        row = conn.execute(
            f"SELECT {pitching_stats[stat]} FROM season_pitching_stats WHERE player_id = ?",
            (player_id,),
        ).fetchone()
        return row[0] if row and row[0] else None

    # Pitching games (appearances)
    if stat == "games":
        # Could be batting or pitching — check both
        bat = conn.execute(
            "SELECT SUM(games) FROM season_batting_stats WHERE player_id = ?",
            (player_id,),
        ).fetchone()
        pitch = conn.execute(
            "SELECT SUM(games) FROM season_pitching_stats WHERE player_id = ?",
            (player_id,),
        ).fetchone()
        bv = bat[0] if bat and bat[0] else 0
        pv = pitch[0] if pitch and pitch[0] else 0
        return max(bv, pv) if bv or pv else None

    return None


def _find_recent_career_highs(conn, player_id):
    """Find career highs from last 7 days of game logs."""
    from datetime import datetime, timedelta
    cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")

    highs = []

    # Batting game logs
    recent_bat = conn.execute("""
        SELECT date, hits, home_runs, rbi, runs
        FROM game_batting_logs
        WHERE player_id = ? AND date >= ?
        ORDER BY date DESC
    """, (player_id, cutoff)).fetchall()

    if recent_bat:
        # Career highs from ALL game logs
        career_bat = conn.execute("""
            SELECT MAX(hits), MAX(home_runs), MAX(rbi), MAX(runs)
            FROM game_batting_logs
            WHERE player_id = ? AND date < ?
        """, (player_id, cutoff)).fetchone()

        stat_names = ["hits", "home_runs", "rbi", "runs"]
        for game in recent_bat:
            game_date = game[0]
            for idx, sname in enumerate(stat_names):
                game_val = game[idx + 1]
                career_max = career_bat[idx] if career_bat else 0
                if game_val and career_max is not None and game_val > career_max:
                    highs.append({
                        "date": game_date,
                        "stat": sname,
                        "value": game_val,
                        "previous_high": career_max,
                    })

    # Pitching game logs
    recent_pitch = conn.execute("""
        SELECT date, strikeouts, ip_outs
        FROM game_pitching_logs
        WHERE player_id = ? AND date >= ?
        ORDER BY date DESC
    """, (player_id, cutoff)).fetchall()

    if recent_pitch:
        career_pitch = conn.execute("""
            SELECT MAX(strikeouts), MAX(ip_outs)
            FROM game_pitching_logs
            WHERE player_id = ? AND date < ?
        """, (player_id, cutoff)).fetchone()

        for game in recent_pitch:
            game_date, k, ip_outs = game
            if k and career_pitch and career_pitch[0] is not None and k > career_pitch[0]:
                highs.append({"date": game_date, "stat": "strikeouts", "value": k, "previous_high": career_pitch[0]})
            if ip_outs and career_pitch and career_pitch[1] is not None and ip_outs > career_pitch[1]:
                highs.append({"date": game_date, "stat": "innings_pitched", "value": ip_outs, "previous_high": career_pitch[1]})

    return highs


@router.get("/records-simulate")
async def records_simulate(
    date_str: str = "",
    key: str | None = None,
    authorization: str | None = Header(None),
):
    """Simulate what record events would fire for a given date."""
    verify_admin(authorization, key)
    if not date_str:
        # Use query param name 'date' as alias
        raise HTTPException(400, "Missing date parameter (use ?date=YYYY-MM-DD)")

    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        events = _simulate_records_for_date(conn, date_str)
        return {"date": date_str, "events": events}
    finally:
        conn.close()


def _simulate_records_for_date(conn, target_date):
    """Find all record-related events for a specific date."""
    events = []
    season = int(target_date[:4])

    # Find all players who played on this date (batting)
    bat_players = conn.execute("""
        SELECT DISTINCT g.player_id, p.name
        FROM game_batting_logs g
        JOIN players p ON g.player_id = p.player_id
        WHERE g.date = ?
    """, (target_date,)).fetchall()

    # Find all players who played on this date (pitching)
    pitch_players = conn.execute("""
        SELECT DISTINCT g.player_id, p.name
        FROM game_pitching_logs g
        JOIN players p ON g.player_id = p.player_id
        WHERE g.date = ?
    """, (target_date,)).fetchall()

    all_players = {}
    for pid, name in bat_players + pitch_players:
        all_players[pid] = name

    # Check each player for record approaches and crossings
    for pid, pname in all_players.items():
        # Get this player's teams
        team_rows = conn.execute("""
            SELECT DISTINCT team FROM season_batting_stats WHERE player_id = ?
            UNION
            SELECT DISTINCT team FROM season_pitching_stats WHERE player_id = ?
        """, (pid, pid)).fetchall()
        player_teams = set()
        for (t,) in team_rows:
            if t:
                for code in t.split("/"):
                    code = code.strip()
                    if code:
                        player_teams.add(code)

        # --- Career firsts ---
        # Check if this is player's first career HR
        hr_before = conn.execute("""
            SELECT COALESCE(SUM(home_runs), 0) FROM game_batting_logs
            WHERE player_id = ? AND date < ? AND date > (season || '-03-25')
        """, (pid, target_date)).fetchone()[0]

        hr_today = conn.execute("""
            SELECT COALESCE(home_runs, 0) FROM game_batting_logs
            WHERE player_id = ? AND date = ?
        """, (pid, target_date)).fetchone()

        if hr_today and hr_today[0] and hr_before == 0:
            events.append({
                "type": "career_first",
                "player": pname,
                "stat": "home_run",
                "detail": f"{pname} hit their first career home run",
            })

        # First career win (pitching)
        if any(pid == p[0] for p in pitch_players):
            wins_before = conn.execute("""
                SELECT COALESCE(SUM(win), 0) FROM game_pitching_logs
                WHERE player_id = ? AND date < ? AND date > (season || '-03-25')
            """, (pid, target_date)).fetchone()[0]

            win_today = conn.execute("""
                SELECT COALESCE(win, 0) FROM game_pitching_logs
                WHERE player_id = ? AND date = ?
            """, (pid, target_date)).fetchone()

            if win_today and win_today[0] and wins_before == 0:
                events.append({
                    "type": "career_first",
                    "player": pname,
                    "stat": "win",
                    "detail": f"{pname} earned their first career win",
                })

            # First career save
            saves_before = conn.execute("""
                SELECT COALESCE(SUM(save), 0) FROM game_pitching_logs
                WHERE player_id = ? AND date < ? AND date > (season || '-03-25')
            """, (pid, target_date)).fetchone()[0]

            save_today = conn.execute("""
                SELECT COALESCE(save, 0) FROM game_pitching_logs
                WHERE player_id = ? AND date = ?
            """, (pid, target_date)).fetchone()

            if save_today and save_today[0] and saves_before == 0:
                events.append({
                    "type": "career_first",
                    "player": pname,
                    "stat": "save",
                    "detail": f"{pname} earned their first career save",
                })

        # --- Record approaches and crossings ---
        # Career batting totals up to and including this date
        career_bat = conn.execute("""
            SELECT SUM(home_runs), SUM(hits), SUM(rbi), SUM(runs),
                   SUM(stolen_bases), SUM(doubles), SUM(walks)
            FROM season_batting_stats
            WHERE player_id = ? AND season <= ?
        """, (pid, season)).fetchone()

        if career_bat and career_bat[0] is not None:
            stat_map = {
                "home_runs": career_bat[0], "hits": career_bat[1],
                "rbi": career_bat[2], "runs": career_bat[3],
                "stolen_bases": career_bat[4], "doubles": career_bat[5],
                "walks": career_bat[6],
            }

            for tc in player_teams:
                # Check team records
                for stat, my_val in stat_map.items():
                    if my_val is None:
                        continue
                    rec = conn.execute("""
                        SELECT value, player_name FROM team_records
                        WHERE team_code = ? AND stat = ? AND record_type = 'career'
                        ORDER BY value DESC LIMIT 1
                    """, (tc, stat)).fetchone()
                    if rec:
                        diff = rec[0] - my_val
                        if 0 < diff <= 3:
                            events.append({
                                "type": "record_approach",
                                "player": pname,
                                "team": _team_display(tc),
                                "stat": stat,
                                "record_value": rec[0],
                                "record_holder": rec[1],
                                "current_value": my_val,
                                "diff": diff,
                                "detail": f"{pname} is {diff} {stat.replace('_', ' ')} away from the {_team_display(tc)} career record ({rec[1]}: {rec[0]})",
                            })
                        elif diff <= 0:
                            events.append({
                                "type": "record_crossing",
                                "player": pname,
                                "team": _team_display(tc),
                                "stat": stat,
                                "record_value": rec[0],
                                "record_holder": rec[1],
                                "current_value": my_val,
                                "detail": f"{pname} has surpassed {rec[1]} for the {_team_display(tc)} career {stat.replace('_', ' ')} record ({my_val} vs {rec[0]})",
                            })

        # --- Career highs from today's game ---
        today_bat = conn.execute("""
            SELECT hits, home_runs, rbi, runs
            FROM game_batting_logs WHERE player_id = ? AND date = ?
        """, (pid, target_date)).fetchone()

        if today_bat:
            prior_maxes = conn.execute("""
                SELECT MAX(hits), MAX(home_runs), MAX(rbi), MAX(runs)
                FROM game_batting_logs WHERE player_id = ? AND date < ?
                  AND date > (season || '-03-25')
            """, (pid, target_date)).fetchone()

            stat_names = ["hits", "home_runs", "rbi", "runs"]
            for idx, sname in enumerate(stat_names):
                tv = today_bat[idx]
                pv = prior_maxes[idx] if prior_maxes else 0
                if tv and pv is not None and tv > (pv or 0) and tv >= 3:
                    events.append({
                        "type": "career_high",
                        "player": pname,
                        "stat": sname,
                        "value": tv,
                        "previous_high": pv or 0,
                        "detail": f"{pname} set a career high with {tv} {sname.replace('_', ' ')} (previous: {pv or 0})",
                    })

    return events


@router.get("/records-sandbox", response_class=HTMLResponse)
async def records_sandbox(
    key: str | None = None,
    authorization: str | None = Header(None),
):
    """Admin sandbox page for records testing."""
    verify_admin(authorization, key)

    # Check if records tables exist
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        team_count = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='team_records'"
        ).fetchone()[0]
        mlb_count = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='mlb_records'"
        ).fetchone()[0]
        if team_count and mlb_count:
            tr = conn.execute("SELECT COUNT(*) FROM team_records").fetchone()[0]
            mr = conn.execute("SELECT COUNT(*) FROM mlb_records").fetchone()[0]
            status_html = f'<div class="stat-card"><div class="label">Team Records</div><div class="value">{tr:,}</div></div>'
            status_html += f'<div class="stat-card"><div class="label">MLB Records</div><div class="value">{mr:,}</div></div>'
        else:
            status_html = '<div class="stat-card warn"><div class="label">Status</div><div class="value">Not Built</div></div>'
    except Exception:
        status_html = '<div class="stat-card warn"><div class="label">Status</div><div class="value">Error</div></div>'
    finally:
        conn.close()

    admin_key = key or ""

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Records Sandbox</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, system-ui, sans-serif; background: #fff; color: #1d1d1f; padding: 16px; max-width: 900px; margin: 0 auto; }}
  h1 {{
    font-size: 22px; margin-bottom: 16px; font-weight: 700;
    background: linear-gradient(to right, #73B3FF, #1A40B3);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent;
  }}
  h2 {{ font-size: 15px; margin: 24px 0 8px; color: #1A40B3; font-weight: 600; }}
  h3 {{ font-size: 13px; margin: 16px 0 6px; color: #555; font-weight: 600; }}
  .stat-cards {{ display: flex; gap: 12px; margin-bottom: 20px; flex-wrap: wrap; }}
  .stat-card {{
    background: linear-gradient(135deg, #1A40B3, #73B3FF);
    border-radius: 10px; padding: 12px 16px; min-width: 100px;
    box-shadow: 0 2px 8px rgba(26, 64, 179, 0.12);
  }}
  .stat-card .label {{ font-size: 11px; color: rgba(255,255,255,0.8); text-transform: uppercase; }}
  .stat-card .value {{ font-size: 24px; font-weight: 700; color: #fff; }}
  .stat-card.warn {{ background: linear-gradient(135deg, #b33a1a, #ff7373); }}
  .search-row {{
    display: flex; gap: 8px; margin-bottom: 16px; align-items: center;
  }}
  .search-row input {{
    padding: 8px 12px; border: 1px solid #ddd; border-radius: 8px;
    font-size: 14px; font-family: inherit; flex: 1; max-width: 300px;
  }}
  .search-row input:focus {{ outline: none; border-color: #1A40B3; }}
  button {{
    padding: 8px 16px; border: none; border-radius: 8px;
    background: #1A40B3; color: #fff; font-size: 13px; cursor: pointer;
    font-family: inherit; font-weight: 600;
  }}
  button:active {{ opacity: 0.8; }}
  button.secondary {{
    background: #fff; color: #1A40B3; border: 1px solid #1A40B3;
  }}
  button:disabled {{ opacity: 0.4; cursor: default; }}
  .results {{ margin-top: 12px; }}
  .player-card {{
    border: 1px solid #e0e8f5; border-radius: 10px; padding: 16px;
    margin-bottom: 16px; background: #fafbff;
  }}
  .player-card .player-name {{
    font-size: 16px; font-weight: 700; color: #1A40B3; margin-bottom: 8px;
  }}
  .player-card .meta {{ font-size: 12px; color: #888; margin-bottom: 12px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 12px; margin-bottom: 12px; }}
  th {{ text-align: left; padding: 6px; border-bottom: 2px solid #e0e8f5; color: #1A40B3; font-weight: 600; font-size: 11px; }}
  td {{ padding: 5px 6px; border-bottom: 1px solid #f0f0f0; }}
  .badge {{
    display: inline-block; padding: 2px 6px; border-radius: 4px;
    font-size: 10px; font-weight: 600; text-transform: uppercase;
  }}
  .badge.approach {{ background: #fef3c7; color: #92400e; }}
  .badge.crossing {{ background: #dcfce7; color: #166534; }}
  .badge.career-first {{ background: #dbeafe; color: #1A40B3; }}
  .badge.career-high {{ background: #f3e8ff; color: #6b21a8; }}
  .badge.holds {{ background: #fce7f3; color: #9d174d; }}
  .event-card {{
    padding: 8px 12px; border-left: 3px solid #1A40B3; margin-bottom: 6px;
    background: #f8f9ff; border-radius: 0 6px 6px 0; font-size: 13px;
  }}
  .event-card.crossing {{ border-left-color: #16a34a; background: #f0fdf4; }}
  .event-card.career-first {{ border-left-color: #2563eb; background: #eff6ff; }}
  .event-card.career-high {{ border-left-color: #7c3aed; background: #faf5ff; }}
  .event-card .event-badge {{ margin-right: 6px; }}
  .loading {{ color: #999; font-size: 13px; font-style: italic; }}
  .empty {{ color: #999; font-size: 13px; }}
  .build-section {{
    padding: 12px; background: #f5f7fa; border-radius: 8px; margin-bottom: 20px;
    display: flex; gap: 12px; align-items: center; flex-wrap: wrap;
  }}
  .build-section .status {{ font-size: 12px; color: #666; }}
  #build-output {{
    font-family: monospace; font-size: 11px; background: #1d1d1f; color: #73B3FF;
    padding: 12px; border-radius: 8px; max-height: 200px; overflow-y: auto;
    white-space: pre-wrap; display: none; margin-top: 8px;
  }}
</style>
</head>
<body>
<h1>Records Sandbox</h1>

<div class="stat-cards">
  {status_html}
</div>

<div class="build-section">
  <button onclick="buildRecords()" id="build-btn">Build Records</button>
  <span class="status" id="build-status">Run the build to create/refresh records tables.</span>
</div>
<div id="build-output"></div>

<h2>Player Lookup</h2>
<div class="search-row">
  <input type="text" id="player-input" placeholder="Player name..." onkeydown="if(event.key==='Enter')lookupPlayer()">
  <button onclick="lookupPlayer()">Search</button>
</div>
<div id="lookup-results" class="results"></div>

<h2>Date Simulation</h2>
<div class="search-row">
  <input type="date" id="sim-date">
  <button onclick="simulateDate()">Simulate</button>
</div>
<div id="sim-results" class="results"></div>

<script>
const KEY = '{admin_key}';
const BASE = window.location.origin;

async function apiFetch(path) {{
  const sep = path.includes('?') ? '&' : '?';
  const url = BASE + path + sep + 'key=' + KEY;
  const resp = await fetch(url);
  return resp.json();
}}

async function apiPost(path) {{
  const sep = path.includes('?') ? '&' : '?';
  const url = BASE + path + sep + 'key=' + KEY;
  const resp = await fetch(url, {{ method: 'POST' }});
  return resp.json();
}}

async function buildRecords() {{
  const btn = document.getElementById('build-btn');
  const status = document.getElementById('build-status');
  const output = document.getElementById('build-output');
  btn.disabled = true;
  status.textContent = 'Building... this may take a few minutes.';
  output.style.display = 'block';
  output.textContent = 'Starting build...\\n';
  try {{
    const data = await apiPost('/admin/build-records');
    output.textContent = (data.stdout || '') + '\\n' + (data.stderr || '');
    status.textContent = data.status === 'ok' ? 'Build complete. Reload to see updated counts.' : 'Build failed.';
  }} catch (e) {{
    status.textContent = 'Error: ' + e.message;
    output.textContent = e.message;
  }}
  btn.disabled = false;
}}

async function lookupPlayer() {{
  const name = document.getElementById('player-input').value.trim();
  if (!name) return;
  const el = document.getElementById('lookup-results');
  el.innerHTML = '<p class="loading">Searching...</p>';
  try {{
    const data = await apiFetch('/admin/records-lookup?name=' + encodeURIComponent(name));
    if (data.error) {{
      el.innerHTML = '<p class="empty">' + data.error + '</p>';
      return;
    }}
    if (!data.results || data.results.length === 0) {{
      el.innerHTML = '<p class="empty">No players found.</p>';
      return;
    }}
    el.innerHTML = data.results.map(renderPlayerCard).join('');
  }} catch (e) {{
    el.innerHTML = '<p class="empty">Error: ' + e.message + '</p>';
  }}
}}

function renderPlayerCard(p) {{
  let html = '<div class="player-card">';
  html += '<div class="player-name">' + esc(p.name) + '</div>';
  html += '<div class="meta">' + esc(p.team || '') + ' &middot; ' + esc(p.positions || '') + ' &middot; ' + esc(p.player_id) + '</div>';

  // Career stats
  if (p.career.batting) {{
    const b = p.career.batting;
    html += '<h3>Career Batting</h3><table><tr><th>G</th><th>H</th><th>HR</th><th>RBI</th><th>R</th><th>SB</th><th>2B</th><th>BB</th><th>AVG</th></tr>';
    html += '<tr><td>' + fmt(b.games) + '</td><td>' + fmt(b.hits) + '</td><td>' + fmt(b.home_runs) + '</td><td>' + fmt(b.rbi) + '</td><td>' + fmt(b.runs) + '</td><td>' + fmt(b.stolen_bases) + '</td><td>' + fmt(b.doubles) + '</td><td>' + fmt(b.walks) + '</td><td>' + (b.avg ? b.avg.toFixed(3) : '--') + '</td></tr></table>';
  }}
  if (p.career.pitching) {{
    const pt = p.career.pitching;
    html += '<h3>Career Pitching</h3><table><tr><th>G</th><th>W</th><th>L</th><th>SV</th><th>K</th><th>IP</th><th>ERA</th></tr>';
    html += '<tr><td>' + fmt(pt.games) + '</td><td>' + fmt(pt.wins) + '</td><td>' + fmt(pt.losses) + '</td><td>' + fmt(pt.saves) + '</td><td>' + fmt(pt.strikeouts) + '</td><td>' + fmt(pt.ip) + '</td><td>' + (pt.era != null ? pt.era.toFixed(2) : '--') + '</td></tr></table>';
  }}

  // Team records
  if (p.team_records_approaching && p.team_records_approaching.length > 0) {{
    html += '<h3>Team Records</h3>';
    p.team_records_approaching.forEach(r => {{
      const badge = r.holds_record
        ? '<span class="badge holds event-badge">Holds</span>'
        : '<span class="badge approach event-badge">Approaching</span>';
      const detail = r.holds_record
        ? esc(r.team_name) + ' ' + esc(r.stat.replace(/_/g, ' ')) + ' record: ' + fmt(r.my_value)
        : fmt(r.diff) + ' ' + esc(r.stat.replace(/_/g, ' ')) + ' from ' + esc(r.team_name) + ' record (' + esc(r.record_holder) + ': ' + fmt(r.record_value) + ')';
      html += '<div class="event-card">' + badge + detail + '</div>';
    }});
  }}

  // MLB records
  if (p.mlb_records_approaching && p.mlb_records_approaching.length > 0) {{
    html += '<h3>MLB Records (within 10)</h3>';
    p.mlb_records_approaching.forEach(r => {{
      html += '<div class="event-card">' +
        '<span class="badge approach event-badge">Approaching</span>' +
        fmt(r.diff) + ' ' + esc(r.stat.replace(/_/g, ' ')) + ' from MLB record (' + esc(r.record_holder) + ': ' + fmt(r.record_value) + ')' +
        '</div>';
    }});
  }}

  // Recent career highs
  if (p.recent_career_highs && p.recent_career_highs.length > 0) {{
    html += '<h3>Recent Career Highs (last 7 days)</h3>';
    p.recent_career_highs.forEach(h => {{
      html += '<div class="event-card career-high">' +
        '<span class="badge career-high event-badge">Career High</span>' +
        esc(h.date) + ': ' + fmt(h.value) + ' ' + esc(h.stat.replace(/_/g, ' ')) + ' (prev: ' + fmt(h.previous_high) + ')' +
        '</div>';
    }});
  }}

  if (!p.team_records_approaching?.length && !p.mlb_records_approaching?.length && !p.recent_career_highs?.length) {{
    html += '<p class="empty">No records approaching or recent career highs.</p>';
  }}

  html += '</div>';
  return html;
}}

async function simulateDate() {{
  const dt = document.getElementById('sim-date').value;
  if (!dt) return;
  const el = document.getElementById('sim-results');
  el.innerHTML = '<p class="loading">Simulating...</p>';
  try {{
    const data = await apiFetch('/admin/records-simulate?date_str=' + dt);
    if (!data.events || data.events.length === 0) {{
      el.innerHTML = '<p class="empty">No record events found for ' + dt + '.</p>';
      return;
    }}
    let html = '<h3>' + data.events.length + ' events for ' + dt + '</h3>';
    data.events.forEach(e => {{
      const cls = e.type.replace(/_/g, '-');
      const badge = '<span class="badge ' + cls + ' event-badge">' + e.type.replace(/_/g, ' ') + '</span>';
      html += '<div class="event-card ' + cls + '">' + badge + esc(e.detail) + '</div>';
    }});
    el.innerHTML = html;
  }} catch (e) {{
    el.innerHTML = '<p class="empty">Error: ' + e.message + '</p>';
  }}
}}

function esc(s) {{ return (s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }}
function fmt(n) {{ return n != null ? Number(n).toLocaleString() : '--'; }}
</script>
</body>
</html>"""

    return HTMLResponse(content=html)
