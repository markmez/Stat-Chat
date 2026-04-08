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
    result = qe_execute(plan) if plan.is_valid else None
    return {
        "valid": plan.is_valid,
        "type": plan.query_type,
        "stat": plan.stat.db_column if plan.stat else None,
        "player_name": plan.player_name,
        "game_log_stat": plan.game_log_stat,
        "unexplained": plan.unexplained_words,
        "has_result": result is not None,
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
_COST_PER_QUERY = {"query engine": 0.0, "intercepted": 0.0, "haiku": 0.002, "sonnet": 0.02}


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
async def dashboard(key: str | None = None, authorization: str | None = Header(None)):
    """Admin dashboard showing query analytics."""
    verify_admin(authorization, key)

    conn = sqlite3.connect(METERING_DB_PATH)

    # Migrate legacy "intercepted" rows
    conn.execute("UPDATE query_log SET response_type = 'query engine' WHERE response_type = 'intercepted'")
    conn.commit()

    # All queries ranked by count, tiebroken by recency
    queries = conn.execute("""
        SELECT query_text, COUNT(*) as cnt,
               MAX(timestamp) as last_seen,
               GROUP_CONCAT(DISTINCT response_type) as types
        FROM query_log
        GROUP BY query_text
        ORDER BY cnt DESC, last_seen DESC
        LIMIT 1000
    """).fetchall()

    # Breakdown by response type
    breakdown = conn.execute("""
        SELECT response_type, COUNT(*) as cnt
        FROM query_log
        GROUP BY response_type
        ORDER BY cnt DESC
    """).fetchall()

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
<script>
let currentSort = 'count';
let currentFilter = null;

function sortBy(mode) {{
  currentSort = mode;
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
}}

function filterBy(type) {{
  currentFilter = type;
  const rows = document.querySelectorAll('#qtable tr[data-count]');
  rows.forEach(r => {{
    r.style.display = r.dataset.types.split(',').includes(type) ? '' : 'none';
  }});
  const bar = document.getElementById('filter-bar');
  const label = document.getElementById('filter-label');
  const css = type.replace(' ', '-');
  label.innerHTML = '<span class="badge ' + css + '">' + type + '</span>';
  bar.classList.add('active');
}}

function clearFilter() {{
  currentFilter = null;
  document.querySelectorAll('#qtable tr[data-count]').forEach(r => r.style.display = '');
  document.getElementById('filter-bar').classList.remove('active');
}}
</script>
</body>
</html>"""

    return HTMLResponse(content=html)
