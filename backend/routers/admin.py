"""
Admin endpoints for data management.

POST /admin/refresh — triggers a live data pull from MySportsFeeds.
GET  /admin/freshness — returns when data was last updated.
"""

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from datetime import date

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel


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
    # Normalize bare-year input ("2026" → "2026-regular") — MSF URLs need the
    # qualified format. Without this the pipeline passes "2026" through and
    # MSF returns 404 on every call. Pre/playoff must be passed explicitly.
    if season and season.isdigit() and len(season) == 4:
        season = f"{season}-regular"
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


@router.post("/backfill-team-game-results")
async def backfill_team_game_results(
    season: str | None = None,
    authorization: str | None = Header(None),
):
    """Populate team_game_results for a single season from MSF games.json.

    Faster than running a full pipeline — hits one MSF endpoint, ~15
    seconds per season. Used to seed the table for prior seasons or to
    repair if data drifts. Pass season as "2025-regular" / "2026-regular".
    """
    verify_admin(authorization)
    if not season:
        season = f"{date.today().year}-regular"
    if season.isdigit() and len(season) == 4:
        season = f"{season}-regular"

    try:
        from data_pipeline.pull_live_stats import pull_team_game_results
        conn = sqlite3.connect(DB_PATH, timeout=30)
        try:
            count = pull_team_game_results(conn, season)
        finally:
            conn.close()
        return {"status": "ok", "season": season, "rows_inserted": count}
    except Exception as e:
        raise HTTPException(500, str(e))


@router.post("/backfill-historical-team-game-results")
async def backfill_historical_team_game_results(
    start: int = 1898,
    end: int = 2024,
    authorization: str | None = Header(None),
):
    """Run Retrosheet gameinfo.csv backfill into team_game_results for a
    season range. Default 1898-2024 (the full Retrosheet coverage).

    Per-season Retrosheet ZIPs (~5MB each) get downloaded to the local
    cache on the server if not already present. ~127 seasons × 5MB = 635MB
    cache after a full run.

    Wraps the standalone script in `data_pipeline/backfill_team_game_results.py`
    so the same code path is reachable via API or CLI.

    Use a smaller range (e.g. start=2024&end=2024) for quick tests; full
    backfill takes a while because of download + extraction per season.
    """
    verify_admin(authorization)
    try:
        from data_pipeline.backfill_team_game_results import (
            backfill_season, ensure_table, load_ballpark_lookup
        )
        conn = sqlite3.connect(DB_PATH, timeout=120)
        conn.execute("PRAGMA journal_mode=WAL")
        ensure_table(conn)
        ballpark_lookup = load_ballpark_lookup()
        per_season = []
        try:
            for season in range(start, end + 1):
                inserted, games = backfill_season(conn, season, ballpark_lookup)
                if games:
                    per_season.append({"season": season, "games": games, "rows": inserted})
        finally:
            conn.close()
        return {
            "status": "ok",
            "range": f"{start}-{end}",
            "seasons_loaded": len(per_season),
            "total_games": sum(s["games"] for s in per_season),
            "total_rows": sum(s["rows"] for s in per_season),
            "per_season": per_season,
        }
    except Exception as e:
        import traceback
        raise HTTPException(500, f"{e}\n{traceback.format_exc()}")


@router.post("/kill-pipeline")
async def kill_pipeline(
    authorization: str | None = Header(None),
):
    """Kill any stuck pull_live_stats.py process and clear the cron lock.

    Use when a cron-invoked pipeline hangs (typically on an MSF call with no
    timeout). The systemd service has PrivateTmp=true, so the lock file at
    /tmp/statchat_pipeline.lock is in the cron's system /tmp and not visible
    from here — but since the lock also has a 90-min stale threshold, the
    next cron will clear it automatically once the process is dead. We just
    need to kill the zombie.
    """
    verify_admin(authorization)

    before = subprocess.run(
        ["pgrep", "-af", "pull_live_stats.py"],
        capture_output=True, text=True
    )
    matches = [line for line in before.stdout.splitlines() if line.strip()]

    if not matches:
        return {"status": "ok", "killed": 0, "message": "No stuck pipeline process found."}

    # SIGTERM first; fall back to SIGKILL for anything that ignores it.
    subprocess.run(["pkill", "-TERM", "-f", "pull_live_stats.py"])
    # Give TERM 3 seconds to take effect before escalating
    import time as _time
    _time.sleep(3)
    still = subprocess.run(["pgrep", "-f", "pull_live_stats.py"], capture_output=True, text=True)
    if still.stdout.strip():
        subprocess.run(["pkill", "-KILL", "-f", "pull_live_stats.py"])

    return {
        "status": "ok",
        "killed": len(matches),
        "processes": matches,
    }


@router.get("/simulate-passing")
async def simulate_passing(
    season: int = 2025,
    start_date: str | None = None,
    end_date: str | None = None,
    key: str | None = None,
    authorization: str | None = Header(None),
):
    """Simulate all-time passing events across a date range."""
    if key:
        authorization = f"Bearer {key}"
    verify_admin(authorization)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = None

    # Get distinct game dates, optionally filtered by range
    date_filter = ""
    params = [season]
    if start_date:
        date_filter += " AND date >= ?"
        params.append(start_date)
    if end_date:
        date_filter += " AND date <= ?"
        params.append(end_date)

    dates = [r[0] for r in conn.execute(f"""
        SELECT DISTINCT date FROM game_batting_logs
        WHERE season = ?{date_filter} ORDER BY date
    """, params).fetchall()]

    # Optimized simulation: build career totals once, then walk through dates
    CONFIGS = [
        ("home_runs",     "season_batting_stats",  "game_batting_logs",   "home runs",    "HR",  75),
        ("hits",          "season_batting_stats",  "game_batting_logs",   "hits",         "H",   150),
        ("rbi",           "season_batting_stats",  "game_batting_logs",   "RBI",          "RBI", 150),
        ("stolen_bases",  "season_batting_stats",  "game_batting_logs",   "stolen bases", "SB",  100),
        ("doubles",       "season_batting_stats",  "game_batting_logs",   "doubles",      "2B",  100),
        ("wins",          "season_pitching_stats", "game_pitching_logs",  "wins",         "W",   75),
        ("strikeouts",    "season_pitching_stats", "game_pitching_logs",  "strikeouts",   "K",   75),
    ]

    all_events = []
    for col, table, game_table, label, abbrev, top_n in CONFIGS:
        # Pre-season career totals (everything before the season)
        pre_career = {}
        for pid, name, total in conn.execute(f"""
            SELECT s.player_id, p.name, SUM(s.{col})
            FROM {table} s JOIN players p ON s.player_id = p.player_id
            WHERE s.season < ?
            GROUP BY s.player_id
        """, (season,)).fetchall():
            pre_career[pid] = (name, total or 0)

        # All game contributions for this season, by date
        gcol = "CASE WHEN win = 1 THEN 1 ELSE 0 END" if col == "wins" else col
        game_contribs = conn.execute(f"""
            SELECT player_id, date, SUM({gcol})
            FROM {game_table}
            WHERE season = ?
            GROUP BY player_id, date
            ORDER BY date
        """, (season,)).fetchall()

        # Group by date
        from collections import defaultdict
        by_date = defaultdict(list)
        for pid, d, val in game_contribs:
            by_date[d].append((pid, val or 0))

        # Walk through dates, accumulating career totals
        running = dict(pre_career)  # pid → (name, running_total)

        # Also need players who only appear in this season
        for pid, d, val in game_contribs:
            if pid not in running:
                name_row = conn.execute("SELECT name FROM players WHERE player_id = ?", (pid,)).fetchone()
                running[pid] = (name_row[0] if name_row else pid, 0)

        # Build sorted all-time list from pre-career + all season data
        # (for rank checking at end of range)
        full_career = {}
        for pid, (name, pre) in running.items():
            full_career[pid] = (name, pre)

        # Walk ALL season dates for accumulation, emit events for filtered range
        all_season_dates = sorted(by_date.keys())
        check_dates = set(dates)

        for d in all_season_dates:
            # Update running totals for today's games
            for pid, val in by_date[d]:
                if pid in running:
                    name, prev = running[pid]
                    running[pid] = (name, prev + val)

            # Only check for passings on requested dates
            if d not in check_dates:
                continue

            # Build today's all-time ranking
            ranked = sorted(running.items(), key=lambda x: -x[1][1])

            if len(ranked) < top_n:
                continue

            cutoff = ranked[top_n - 1][1][1]

            # Check each player who played today
            for pid, val in by_date[d]:
                if val == 0:
                    continue
                name, career_total = running[pid]
                if career_total < cutoff:
                    continue
                career_before = career_total - val

                # Find best person passed
                best_passed = None
                for rank, (rpid, (rname, rtotal)) in enumerate(ranked, 1):
                    if rank > top_n:
                        break
                    if rpid == pid:
                        continue
                    if career_before < rtotal and career_total >= rtotal:
                        if best_passed is None or rank < best_passed[0]:
                            best_passed = (rank, rname, rtotal)

                if best_passed:
                    passed_rank, passed_name, _ = best_passed
                    from services.notable_events import _ordinal
                    headline = (
                        f"{name} now has {career_total} career {label}, "
                        f"passing {passed_name} for {_ordinal(passed_rank)} on the all-time list."
                    )
                    all_events.append({
                        "date": d,
                        "type": f"alltime_passing_{col}",
                        "headline": headline,
                    })

    # Franchise passing — incremental simulation
    from services.franchise import get_franchise_codes, get_franchise_name
    from services.notable_events import _ordinal

    FRANCHISE_TOP_N = 5
    FRANCHISE_APPROACH = 5

    franchise_stats = [
        ("home_runs", "season_batting_stats", "game_batting_logs", "home runs", "HR"),
        ("hits",      "season_batting_stats", "game_batting_logs", "hits",      "H"),
        ("rbi",       "season_batting_stats", "game_batting_logs", "RBI",       "RBI"),
        ("stolen_bases","season_batting_stats","game_batting_logs", "stolen bases","SB"),
        ("doubles",   "season_batting_stats", "game_batting_logs", "doubles",   "2B"),
        ("wins",      "season_pitching_stats","game_pitching_logs","wins",      "W"),
        ("strikeouts","season_pitching_stats","game_pitching_logs","strikeouts","K"),
    ]

    # Get all active teams
    active_teams = [r[0] for r in conn.execute(
        "SELECT DISTINCT team FROM season_batting_stats WHERE season = ?", (season,)
    ).fetchall()]

    for col, table, game_table, label, abbrev in franchise_stats:
        # Get game contributions for the season grouped by player+date
        gcol = "CASE WHEN win = 1 THEN 1 ELSE 0 END" if col == "wins" else col
        game_contribs = conn.execute(f"""
            SELECT g.player_id, g.date, SUM({gcol}) as val
            FROM {game_table} g WHERE g.season = ?
            GROUP BY g.player_id, g.date ORDER BY g.date
        """, (season,)).fetchall()

        by_date_all = {}
        for pid, d, val in game_contribs:
            by_date_all.setdefault(d, []).append((pid, val or 0))

        # For each franchise, build pre-season leaderboard and walk dates
        for team in active_teams:
            franchise_codes = get_franchise_codes(team)
            ph = ",".join(["?"] * len(franchise_codes))
            franchise_name = get_franchise_name(team)

            # Pre-season franchise career totals
            pre = {}
            for pid, name, total in conn.execute(f"""
                SELECT s.player_id, p.name, SUM(s.{col})
                FROM {table} s JOIN players p ON s.player_id = p.player_id
                WHERE s.team IN ({ph}) AND s.season < ?
                GROUP BY s.player_id
            """, (*franchise_codes, season)).fetchall():
                pre[pid] = (name, total or 0)

            # Players on this team this season
            team_players = set(r[0] for r in conn.execute(f"""
                SELECT DISTINCT player_id FROM {table}
                WHERE team = ? AND season = ?
            """, (team, season)).fetchall())

            # Ensure all team players are in the map
            for pid in team_players:
                if pid not in pre:
                    nr = conn.execute("SELECT name FROM players WHERE player_id = ?", (pid,)).fetchone()
                    pre[pid] = (nr[0] if nr else pid, 0)

            running = dict(pre)  # pid → (name, running_total)
            all_season_dates = sorted(by_date_all.keys())
            check_dates = set(dates)

            for d in all_season_dates:
                # Accumulate contributions for players on THIS team
                day_contribs = {}
                for pid, val in by_date_all.get(d, []):
                    if pid in team_players and val > 0:
                        name, prev = running.get(pid, ("", 0))
                        running[pid] = (name, prev + val)
                        day_contribs[pid] = val

                if d not in check_dates or not day_contribs:
                    continue

                # Build ranked list
                ranked = sorted(running.items(), key=lambda x: -x[1][1])

                for pid, contrib in day_contribs.items():
                    name, career_total = running[pid]
                    career_before = career_total - contrib

                    # Find rank
                    player_rank = None
                    for ri, (rpid, _) in enumerate(ranked, 1):
                        if rpid == pid:
                            player_rank = ri
                            break
                    if not player_rank or player_rank > FRANCHISE_TOP_N + 2:
                        continue

                    # Check passing
                    best_passed = None
                    for ri, (rpid, (rname, rtotal)) in enumerate(ranked, 1):
                        if rpid == pid or ri > FRANCHISE_TOP_N:
                            continue
                        if career_before < rtotal and career_total >= rtotal:
                            if not best_passed or ri < best_passed[0]:
                                best_passed = (ri, rname, rtotal)

                    if best_passed:
                        pr, pn, _ = best_passed
                        fn = franchise_name[:-1] if franchise_name.endswith("s") else franchise_name
                        # Check if player has only played for this franchise
                        other = conn.execute(f"""
                            SELECT COUNT(DISTINCT team) FROM {table}
                            WHERE player_id = ? AND team NOT IN ({ph})
                        """, (pid, *franchise_codes)).fetchone()[0]
                        if other == 0:
                            cp = f"{career_total} career {label}"
                            fs = f" in {franchise_name} history"
                        else:
                            cp = f"{career_total} career {label} as a {fn}"
                            fs = " in franchise history"
                        all_events.append({
                            "date": d,
                            "type": f"franchise_passing_{col}",
                            "headline": f"{name} now has {cp}, passing {pn} for {_ordinal(pr)}{fs}.",
                        })

                    # Check approaching record
                    rec_pid, (rec_name, rec_total) = ranked[0]
                    if pid != rec_pid:
                        gap = rec_total - career_total
                        prev_gap = rec_total - career_before
                        if 0 < gap <= FRANCHISE_APPROACH and prev_gap > gap:
                            fn = franchise_name[:-1] if franchise_name.endswith("s") else franchise_name
                            other = conn.execute(f"""
                                SELECT COUNT(DISTINCT team) FROM {table}
                                WHERE player_id = ? AND team NOT IN ({ph})
                            """, (pid, *franchise_codes)).fetchone()[0]
                            if other == 0:
                                cp = f"{career_total} career {label}"
                            else:
                                cp = f"{career_total} career {label} as a {fn}"
                            all_events.append({
                                "date": d,
                                "type": f"franchise_record_approach_{col}",
                                "headline": f"{name} now has {cp}, just {gap} away from {rec_name}'s franchise record of {rec_total}.",
                            })

    # Sort all events by date
    all_events.sort(key=lambda x: x["date"])

    conn.close()

    # Summary by type
    from collections import Counter
    type_counts = Counter(e["type"] for e in all_events if e["type"] != "ERROR")

    # Render as HTML
    from fastapi.responses import HTMLResponse
    date_range = f"{start_date or 'start'} to {end_date or 'end'}" if start_date or end_date else "full season"
    type_summary = "".join(f"<li>{t}: <b>{c}</b></li>" for t, c in sorted(type_counts.items(), key=lambda x: -x[1]))

    rows_html = ""
    for e in all_events:
        stat = e["type"].replace("alltime_passing_", "").replace("franchise_passing_", "F:").replace("franchise_record_approach_", "F→").upper()
        rows_html += f"""<tr>
            <td style="white-space:nowrap;padding:8px 12px;color:#666">{e["date"]}</td>
            <td style="padding:8px 6px"><span style="background:#e8f0fe;color:#1a40b3;padding:2px 8px;border-radius:10px;font-size:12px">{stat}</span></td>
            <td style="padding:8px 12px">{e["headline"]}</td>
        </tr>"""

    html = f"""<!DOCTYPE html>
<html><head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>All-Time Passing Simulation</title>
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif; margin: 0; padding: 20px; background: #f5f5f5; }}
  .card {{ background: white; border-radius: 12px; padding: 20px; margin-bottom: 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
  h1 {{ color: #1a40b3; margin: 0 0 4px; }}
  .subtitle {{ color: #666; margin: 0 0 16px; }}
  table {{ width: 100%; border-collapse: collapse; }}
  tr:nth-child(even) {{ background: #f9f9f9; }}
  tr:hover {{ background: #eef3ff; }}
  ul {{ margin: 8px 0; padding-left: 20px; }}
</style>
</head><body>
<div class="card">
  <h1>All-Time Passing Simulation</h1>
  <p class="subtitle">{season} &middot; {date_range} &middot; {len(all_events)} events across {len(dates)} game dates</p>
  <ul>{type_summary}</ul>
</div>
<div class="card">
  <table>{rows_html}</table>
</div>
</body></html>"""

    return HTMLResponse(html)


@router.post("/redetect")
async def redetect_events(
    clear_date: str | None = None,
    authorization: str | None = Header(None),
):
    """Re-run event detection without pulling new data. Fast (~30s).

    Optional: clear_date=YYYY-MM-DD to delete existing events for that date
    before re-detecting (forces fresh generation with latest code).
    """
    verify_admin(authorization)
    from services.notable_events import detect_all

    if clear_date:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        try:
            # Keep career_first and onbase_streak (these are fine)
            deleted = conn.execute("""
                DELETE FROM notable_events
                WHERE game_date = ? AND detection_type NOT IN ('career_first', 'onbase_streak')
            """, (clear_date,)).rowcount
            conn.commit()
        finally:
            conn.close()
    else:
        deleted = 0

    import io, contextlib
    log_buffer = io.StringIO()
    with contextlib.redirect_stdout(log_buffer):
        count = detect_all(DB_PATH)
    log_output = log_buffer.getvalue()
    return {
        "status": "ok",
        "events_detected": count,
        "events_cleared": deleted,
        "clear_date": clear_date,
        "log": log_output,
    }


@router.post("/rebuild-records")
async def rebuild_records(authorization: str | None = Header(None)):
    """Rebuild team_records and mlb_records tables."""
    verify_admin(authorization)
    import subprocess
    result = subprocess.run(
        ["python3", "data_pipeline/build_records.py", "--db", DB_PATH],
        capture_output=True, text=True, timeout=300,
        cwd=os.path.dirname(os.path.dirname(__file__))
    )
    return {"status": "ok", "stdout": result.stdout, "stderr": result.stderr}


@router.post("/rebuild-prominence")
async def rebuild_prominence(authorization: str | None = Header(None)):
    """Recompute prominence_score for all players."""
    verify_admin(authorization)
    import subprocess
    result = subprocess.run(
        ["python3", "data_pipeline/compute_prominence.py", DB_PATH],
        capture_output=True, text=True, timeout=120,
        cwd=os.path.dirname(os.path.dirname(__file__))
    )
    return {"status": "ok", "stdout": result.stdout, "stderr": result.stderr}


class GradeInsightPayload(BaseModel):
    id: int
    grade: int | None = None  # None = clear the grade
    reason: str | None = None  # None = don't touch reason


@router.post("/grade-insight")
async def grade_insight(
    payload: GradeInsightPayload,
    key: str | None = None,
    authorization: str | None = Header(None),
):
    """Update ai_grade and/or grade_reason on an event_archive row.

    - Grade is any integer 1-10, or None/null to clear.
    - Reason is a free-text field (can be empty string to clear).
    - Skipping an insight = never calling this endpoint = row stays NULL.
      No implicit signal is derived from skips.
    """
    verify_admin(authorization, key)
    if payload.grade is not None and not (1 <= payload.grade <= 10):
        raise HTTPException(400, "grade must be between 1 and 10 (or null)")
    from services.metering import METERING_DB_PATH
    conn = sqlite3.connect(METERING_DB_PATH)
    try:
        # Idempotent column adds — cheap if they already exist
        for stmt in ("ALTER TABLE event_archive ADD COLUMN ai_grade INTEGER",
                     "ALTER TABLE event_archive ADD COLUMN grade_reason TEXT"):
            try:
                conn.execute(stmt)
            except Exception:
                pass
        # Build the update dynamically — only touch fields the caller sent.
        sets = []
        args: list = []
        data = payload.model_dump(exclude_unset=True)
        if "grade" in data:
            sets.append("ai_grade = ?")
            args.append(payload.grade)
        if "reason" in data:
            sets.append("grade_reason = ?")
            args.append(payload.reason)
        if not sets:
            return {"status": "ok", "changed": 0}
        args.append(payload.id)
        conn.execute(f"UPDATE event_archive SET {', '.join(sets)} WHERE id = ?",
                     tuple(args))
        conn.commit()
        return {"status": "ok", "changed": conn.total_changes}
    finally:
        conn.close()


@router.post("/run-metering-sql")
async def run_metering_sql(
    sql: str = "",
    authorization: str | None = Header(None),
):
    """Run SQL against metering.db."""
    verify_admin(authorization)
    if not sql:
        raise HTTPException(400, "No SQL provided")
    from services.metering import METERING_DB_PATH
    conn = sqlite3.connect(METERING_DB_PATH)
    try:
        if sql.strip().upper().startswith("SELECT"):
            rows = conn.execute(sql).fetchall()
            return {"rows": [list(r) for r in rows[:100]]}
        else:
            cur = conn.execute(sql)
            conn.commit()
            return {"status": "ok", "rows_affected": cur.rowcount}
    except Exception as e:
        raise HTTPException(500, str(e))
    finally:
        conn.close()


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
        "compare_years": plan.compare_years,
        "result_preview": (result or "")[:300],
    }


@router.get("/debug-followup")
async def debug_followup(
    q: str = "",
    prior: str = "",
    key: str | None = None,
    authorization: str | None = Header(None),
):
    """Debug: test local follow-up rewriter."""
    verify_admin(authorization, key)
    from routers.query import _extract_prior_context, _local_followup_rewrite
    history = [{"role": "user", "content": prior}, {"role": "assistant", "content": "..."}] if prior else []
    ctx = _extract_prior_context(history)
    rewrite = _local_followup_rewrite(q, history) if history else None
    return {
        "question": q,
        "prior": prior,
        "ctx_player": ctx.get("player"),
        "ctx_stat": ctx.get("stat"),
        "ctx_season": ctx.get("season"),
        "rewrite": rewrite,
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


@router.get("/ai-backtest", response_class=HTMLResponse)
async def ai_backtest_page(
    key: str | None = None,
    authorization: str | None = Header(None),
):
    """HTML page for running AI insight prompt backtests. Doesn't call Sonnet on load."""
    verify_admin(authorization, key)
    key_param = key or ""
    html = f"""<!DOCTYPE html>
<html><head><title>AI Insight Backtest</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: #f5f7fa; color: #1f2937; margin: 0; padding: 24px; }}
  h1 {{ background: linear-gradient(to right, #73B3FF, #1A40B3); -webkit-background-clip: text;
        -webkit-text-fill-color: transparent; margin: 0 0 8px; font-size: 28px; }}
  .sub {{ color: #6b7280; margin-bottom: 20px; font-size: 14px; }}
  .controls {{ display: flex; gap: 12px; align-items: center; margin-bottom: 24px;
              padding: 16px; background: white; border-radius: 12px;
              box-shadow: 0 1px 3px rgba(0,0,0,0.06); }}
  label {{ font-weight: 600; }}
  input[type=number] {{ width: 60px; padding: 6px 10px; border: 1px solid #d1d5db;
                       border-radius: 6px; font-size: 14px; }}
  button {{ background: linear-gradient(135deg, #1A40B3, #73B3FF); color: white;
           border: none; padding: 10px 20px; border-radius: 8px; font-weight: 600;
           cursor: pointer; font-size: 14px; }}
  button:disabled {{ opacity: 0.5; cursor: not-allowed; }}
  .status {{ color: #6b7280; font-size: 13px; margin-left: 8px; }}
  .date-panel {{ background: white; border-radius: 12px; padding: 20px;
                margin-bottom: 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.06); }}
  .date-header {{ font-weight: 700; font-size: 18px; margin-bottom: 12px;
                 color: #1A40B3; }}
  .cols {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }}
  .col h3 {{ margin: 0 0 10px; font-size: 13px; text-transform: uppercase;
            letter-spacing: 0.5px; color: #6b7280; }}
  .col.shipped h3 {{ color: #6b7280; }}
  .col.preview h3 {{ color: #1A40B3; }}
  ul {{ margin: 0; padding-left: 20px; }}
  li {{ margin-bottom: 8px; line-height: 1.45; }}
  .empty {{ color: #9ca3af; font-style: italic; }}
  .err {{ color: #dc2626; font-size: 13px; margin-top: 8px; }}
</style></head>
<body>
  <h1>AI Insight Backtest</h1>
  <div class="sub">Compare shipped headlines (what went out) vs preview headlines (what the current prompt would produce now).</div>
  <div class="controls">
    <label for="days">Days back:</label>
    <input id="days" type="number" min="1" max="7" value="3">
    <button id="run" onclick="runBacktest()">Run backtest</button>
    <span class="status" id="status">Cost: ~$0.15-0.25 per date with prompt caching (first run ~$0.50 to warm cache).</span>
  </div>
  <div id="results"></div>

<script>
const KEY = {json.dumps(key_param)};
async function runBacktest() {{
  const days = document.getElementById('days').value;
  const btn = document.getElementById('run');
  const status = document.getElementById('status');
  const results = document.getElementById('results');
  btn.disabled = true;
  status.textContent = 'Fetching date list…';
  results.innerHTML = '';
  try {{
    const dr = await fetch(`/admin/ai-backtest/dates?days=${{days}}&key=${{encodeURIComponent(KEY)}}`);
    const dd = await dr.json();
    const dates = dd.dates || [];
    if (!dates.length) {{
      status.textContent = 'No game dates found.';
      btn.disabled = false;
      return;
    }}
    const all = [];
    for (let i = 0; i < dates.length; i++) {{
      const d = dates[i];
      status.textContent = `Running ${{d}} (${{i+1}} of ${{dates.length}})… ~35-60s per date.`;
      try {{
        const r = await fetch(`/admin/ai-backtest/run?single_date=${{d}}&key=${{encodeURIComponent(KEY)}}`, {{method: 'POST'}});
        const data = await r.json();
        if (data.status === 'ok' && data.results.length) {{
          all.push(data.results[0]);
          render(all);
        }} else {{
          all.push({{game_date: d, shipped: [], preview: [], error: 'Empty response'}});
          render(all);
        }}
      }} catch (e) {{
        all.push({{game_date: d, shipped: [], preview: [], error: e.message}});
        render(all);
      }}
    }}
    status.textContent = `Done. ${{all.length}} dates.`;
  }} catch (e) {{
    status.textContent = 'Error: ' + e.message;
  }}
  btn.disabled = false;
}}
function render(rows) {{
  const html = rows.map(r => {{
    const shipped = (r.shipped || []).length
      ? '<ul>' + r.shipped.map(h => `<li>${{esc(h)}}</li>`).join('') + '</ul>'
      : '<div class="empty">None shipped on this date.</div>';
    const preview = (r.preview || []).length
      ? '<ul>' + r.preview.map(h => `<li>${{esc(h)}}</li>`).join('') + '</ul>'
      : '<div class="empty">No events generated.</div>';
    const err = r.error ? `<div class="err">Error: ${{esc(r.error)}}</div>` : '';
    const tools = r.tool_calls != null ? ` · ${{r.tool_calls}} tool calls` : '';
    const filt = r.filtered_count ? ` · ${{r.filtered_count}} filtered` : '';
    return `<div class="date-panel">
      <div class="date-header">${{esc(r.game_date)}}${{tools}}${{filt}}</div>
      <div class="cols">
        <div class="col shipped"><h3>Shipped (${{(r.shipped||[]).length}})</h3>${{shipped}}</div>
        <div class="col preview"><h3>Preview — new prompt (${{(r.preview||[]).length}})</h3>${{preview}}</div>
      </div>${{err}}
    </div>`;
  }}).join('');
  document.getElementById('results').innerHTML = html;
}}
function esc(s) {{ return String(s).replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}})[c]); }}
</script>
</body></html>"""
    return HTMLResponse(content=html)


@router.api_route("/ai-backtest/run", methods=["GET", "POST"])
async def ai_backtest_run(
    days: int = 3,
    single_date: str | None = None,
    key: str | None = None,
    authorization: str | None = Header(None),
):
    """Run the current AI insight prompt against historical game dates without inserting.

    If single_date is provided (YYYY-MM-DD), runs only that date.
    Otherwise runs the last `days` distinct game dates.
    Returns side-by-side: shipped vs preview headlines.
    Costs ~$0.02-0.04 per date.
    """
    verify_admin(authorization, key)
    try:
        conn = sqlite3.connect(DB_PATH)
        from services.ai_notable_events import generate_ai_insights

        season = date.today().year
        if single_date:
            target_dates = [single_date]
        else:
            days = max(1, min(days, 7))
            date_rows = conn.execute("""
                SELECT DISTINCT date FROM game_batting_logs
                WHERE season = ? ORDER BY date DESC LIMIT ?
            """, (season, days)).fetchall()
            target_dates = [r[0] for r in date_rows]

        results = []
        for d in target_dates:
            shipped = conn.execute("""
                SELECT headline FROM notable_events
                WHERE detection_type = 'ai_insight' AND game_date = ?
                ORDER BY id ASC
            """, (d,)).fetchall()
            shipped_headlines = [r[0] for r in shipped]

            preview = generate_ai_insights(conn, season, d, preview=True)
            preview_headlines = [e.get("headline", "") for e in preview.get("events", [])]

            results.append({
                "game_date": d,
                "shipped": shipped_headlines,
                "preview": preview_headlines,
                "error": preview.get("error"),
                "tool_calls": preview.get("tool_calls"),
                "filtered_count": preview.get("filtered_count"),
            })

        conn.close()
        return {"status": "ok", "season": season, "days": len(target_dates), "results": results}
    except Exception as e:
        import traceback
        raise HTTPException(500, f"{str(e)}\n{traceback.format_exc()}")


@router.api_route("/ai-backtest/multi", methods=["GET", "POST"])
async def ai_backtest_multi(
    days: int = 3,
    verify: bool = False,
    versions: str | None = None,
    key: str | None = None,
    authorization: str | None = Header(None),
):
    """Run all 5 historical prompt versions against the last N game dates.

    Uses the cheap pre-tool-use snapshot architecture for all variants.
    Cost: ~$0.04 per (date × prompt). 3 days × 5 prompts = ~$0.60.

    Returns a grid: {dates: [...], versions: [...], results: {date: {version: [headlines]}}}.
    """
    verify_admin(authorization, key)
    days = max(1, min(days, 7))
    try:
        conn = sqlite3.connect(DB_PATH)
        from services.ai_notable_events import generate_ai_insights_with_prompt
        from services.historical_prompts import PROMPT_VERSIONS

        season = date.today().year
        date_rows = conn.execute("""
            SELECT DISTINCT date FROM game_batting_logs
            WHERE season = ? ORDER BY date DESC LIMIT ?
        """, (season, days)).fetchall()
        target_dates = [r[0] for r in date_rows]

        if versions:
            wanted = set(v.strip() for v in versions.split(","))
            active = [(n, p) for n, p in PROMPT_VERSIONS if n in wanted]
        else:
            active = list(PROMPT_VERSIONS)
        version_names = [name for name, _ in active]
        results = {}
        for d in target_dates:
            results[d] = {}
            for name, prompt in active:
                r = generate_ai_insights_with_prompt(conn, season, d, prompt, verify=verify)
                results[d][name] = {
                    "headlines": [e.get("headline", "") for e in r.get("events", [])],
                    "error": r.get("error"),
                    "count": len(r.get("events", [])),
                    "verifier_dropped": r.get("verifier_dropped", []),
                    "verifier_dropped_count": r.get("verifier_dropped_count", 0),
                }

        # Also include shipped (production) headlines per date
        shipped = {}
        for d in target_dates:
            rows = conn.execute("""
                SELECT headline FROM notable_events
                WHERE detection_type = 'ai_insight' AND game_date = ?
                ORDER BY id ASC
            """, (d,)).fetchall()
            shipped[d] = [r[0] for r in rows]

        conn.close()
        return {
            "status": "ok",
            "season": season,
            "dates": target_dates,
            "versions": version_names,
            "shipped": shipped,
            "results": results,
        }
    except Exception as e:
        import traceback
        raise HTTPException(500, f"{str(e)}\n{traceback.format_exc()}")


@router.get("/ai-backtest/multi/page", response_class=HTMLResponse)
async def ai_backtest_multi_page(
    key: str | None = None,
    authorization: str | None = Header(None),
):
    """HTML page for multi-prompt comparison. Renders cached result on load."""
    verify_admin(authorization, key)
    key_param = key or ""
    try:
        from services.cached_multi_result import CACHED_RESULT
        cached_json = json.dumps(CACHED_RESULT)
    except Exception:
        cached_json = "null"
    html = f"""<!DOCTYPE html>
<html><head><title>AI Insight Multi-Prompt Comparison</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: #f5f7fa; color: #1f2937; margin: 0; padding: 24px; }}
  h1 {{ background: linear-gradient(to right, #73B3FF, #1A40B3); -webkit-background-clip: text;
        -webkit-text-fill-color: transparent; margin: 0 0 8px; font-size: 28px; }}
  .sub {{ color: #6b7280; margin-bottom: 20px; font-size: 14px; }}
  .controls {{ display: flex; gap: 12px; align-items: center; margin-bottom: 24px;
              padding: 16px; background: white; border-radius: 12px; box-shadow: 0 1px 3px rgba(0,0,0,0.06); }}
  label {{ font-weight: 600; }}
  input[type=number] {{ width: 60px; padding: 6px 10px; border: 1px solid #d1d5db; border-radius: 6px; }}
  button {{ background: linear-gradient(135deg, #1A40B3, #73B3FF); color: white;
           border: none; padding: 10px 20px; border-radius: 8px; font-weight: 600; cursor: pointer; }}
  button:disabled {{ opacity: 0.5; cursor: not-allowed; }}
  .status {{ color: #6b7280; font-size: 13px; margin-left: 8px; }}
  .date-section {{ background: white; border-radius: 12px; padding: 20px;
                  margin-bottom: 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.06); }}
  .date-header {{ font-weight: 700; font-size: 18px; margin-bottom: 12px; color: #1A40B3; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th {{ text-align: left; padding: 8px; background: #f3f4f6; border-bottom: 2px solid #d1d5db;
       font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; color: #6b7280; }}
  td {{ padding: 10px 8px; border-bottom: 1px solid #e5e7eb; vertical-align: top; }}
  td.version {{ font-family: ui-monospace, monospace; font-weight: 600; color: #1A40B3; white-space: nowrap; }}
  td.count {{ text-align: center; font-weight: 600; color: #6b7280; }}
  td.headlines {{ line-height: 1.5; }}
  .headline {{ margin-bottom: 6px; }}
  .empty {{ color: #9ca3af; font-style: italic; font-size: 12px; }}
  .err {{ color: #dc2626; font-size: 12px; }}
  .shipped-row {{ background: #fff7ed; }}
  .shipped-row td.version {{ color: #b45309; }}
  .scroll-wrap {{ overflow-x: auto; }}
  table.player-grid {{ table-layout: fixed; min-width: 1400px; }}
  table.player-grid th {{ position: sticky; top: 0; z-index: 1; }}
  table.player-grid th.player-col, table.player-grid td.player-col {{ width: 140px; }}
  table.player-grid th.shipped-col, table.player-grid td.shipped-col {{ width: 280px; background: #fff7ed; }}
  table.player-grid th.version-col, table.player-grid td.version-col {{ width: 280px; }}
  table.player-grid td {{ vertical-align: top; font-size: 12.5px; }}
  table.player-grid td.player-col {{ font-weight: 600; color: #1f2937; }}
  .layout-toggle {{ margin-left: auto; }}
  .layout-toggle button {{ background: #e5e7eb; color: #1f2937; padding: 6px 12px; font-size: 12px; }}
  .layout-toggle button.active {{ background: linear-gradient(135deg, #1A40B3, #73B3FF); color: white; }}
</style></head>
<body>
  <h1>AI Insight Multi-Prompt Comparison</h1>
  <div class="sub">Compares 5 historical prompt versions side-by-side. Uses cheap snapshot architecture (~$0.60 total for 3 dates).</div>
  <div class="controls">
    <label for="days">Days back:</label>
    <input id="days" type="number" min="1" max="7" value="3">
    <button id="run" onclick="runComparison()">Re-run (~$0.60)</button>
    <span class="status" id="status">Loading cached result…</span>
    <span class="layout-toggle">
      <button id="layout-player" class="active" onclick="setLayout('player')">By player</button>
      <button id="layout-version" onclick="setLayout('version')">By version</button>
    </span>
  </div>
  <div id="results"></div>
<script>
const KEY = {json.dumps(key_param)};
const CACHED = {cached_json};
window.addEventListener('DOMContentLoaded', () => {{
  if (CACHED && CACHED.status === 'ok') {{
    render(CACHED);
    document.getElementById('status').textContent =
      `Showing cached result for dates: ${{CACHED.dates.join(', ')}}. Click "Re-run" for fresh data (~$0.60).`;
  }} else {{
    document.getElementById('status').textContent = 'No cached result. Click "Re-run" to generate (~$0.60).';
  }}
}});
async function runComparison() {{
  const days = document.getElementById('days').value;
  const btn = document.getElementById('run');
  const status = document.getElementById('status');
  const results = document.getElementById('results');
  btn.disabled = true;
  status.textContent = 'Running… 5 prompts × ' + days + ' dates. Sequential, ~5-10s per call. Total ~' + (days * 5 * 8) + 's.';
  results.innerHTML = '';
  try {{
    const r = await fetch(`/admin/ai-backtest/multi?days=${{days}}&key=${{encodeURIComponent(KEY)}}`, {{method: 'POST'}});
    const data = await r.json();
    if (data.status !== 'ok') {{
      status.textContent = 'Error: ' + (data.detail || JSON.stringify(data));
      btn.disabled = false;
      return;
    }}
    render(data);
    status.textContent = 'Done.';
  }} catch (e) {{
    status.textContent = 'Error: ' + e.message;
  }}
  btn.disabled = false;
}}
function esc(s) {{ return String(s).replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}})[c]); }}

let CURRENT_LAYOUT = 'player';
let LAST_DATA = null;

function setLayout(mode) {{
  CURRENT_LAYOUT = mode;
  document.getElementById('layout-player').classList.toggle('active', mode === 'player');
  document.getElementById('layout-version').classList.toggle('active', mode === 'version');
  if (LAST_DATA) render(LAST_DATA);
}}

// Extract first 2-3 capitalized words at start of a headline as the player name
function extractPlayer(headline) {{
  if (!headline) return '???';
  // Match up to 3 capitalized words (allows accents, hyphens, periods like "Jr.")
  const m = headline.match(/^([A-ZÁÉÍÓÚÑ][a-záéíóúñ.'\-]+(?:\s+[A-ZÁÉÍÓÚÑ][a-záéíóúñ.'\-]+){{0,2}})/);
  return m ? m[1] : '???';
}}

function render(data) {{
  LAST_DATA = data;
  if (CURRENT_LAYOUT === 'player') {{
    renderByPlayer(data);
  }} else {{
    renderByVersion(data);
  }}
}}

function renderByPlayer(data) {{
  const html = data.dates.map(d => {{
    const sh = data.shipped[d] || [];

    // Build a player → {{shipped: [], v1: [], v2: [], ...}} map
    const playerMap = new Map();
    const ensure = p => {{
      if (!playerMap.has(p)) {{
        const o = {{shipped: []}};
        for (const v of data.versions) o[v] = [];
        playerMap.set(p, o);
      }}
      return playerMap.get(p);
    }};

    sh.forEach(h => ensure(extractPlayer(h)).shipped.push(h));
    for (const v of data.versions) {{
      const hs = (data.results[d][v] || {{}}).headlines || [];
      hs.forEach(h => ensure(extractPlayer(h))[v].push(h));
    }}

    // Sort: players covered by shipped first, then by total mention count descending
    const players = Array.from(playerMap.keys()).sort((a, b) => {{
      const aShipped = playerMap.get(a).shipped.length > 0;
      const bShipped = playerMap.get(b).shipped.length > 0;
      if (aShipped !== bShipped) return aShipped ? -1 : 1;
      const aCount = data.versions.reduce((s, v) => s + playerMap.get(a)[v].length, 0) + playerMap.get(a).shipped.length;
      const bCount = data.versions.reduce((s, v) => s + playerMap.get(b)[v].length, 0) + playerMap.get(b).shipped.length;
      return bCount - aCount;
    }});

    const headerCells = `<th class="player-col">Player</th><th class="shipped-col">SHIPPED</th>` +
      data.versions.map(v => `<th class="version-col">${{esc(v)}}</th>`).join('');

    const rows = players.map(p => {{
      const o = playerMap.get(p);
      const shipCell = o.shipped.length
        ? o.shipped.map(h => `<div class="headline">${{esc(h)}}</div>`).join('')
        : '<div class="empty">—</div>';
      const versionCells = data.versions.map(v => {{
        const hs = o[v];
        return `<td class="version-col">${{hs.length ? hs.map(h => `<div class="headline">${{esc(h)}}</div>`).join('') : '<div class="empty">—</div>'}}</td>`;
      }}).join('');
      return `<tr>
        <td class="player-col">${{esc(p)}}</td>
        <td class="shipped-col">${{shipCell}}</td>
        ${{versionCells}}
      </tr>`;
    }}).join('');

    return `<div class="date-section">
      <div class="date-header">${{esc(d)}}</div>
      <div class="scroll-wrap">
        <table class="player-grid">
          <thead><tr>${{headerCells}}</tr></thead>
          <tbody>${{rows}}</tbody>
        </table>
      </div>
    </div>`;
  }}).join('');
  document.getElementById('results').innerHTML = html;
}}

function renderByVersion(data) {{
  const html = data.dates.map(d => {{
    const sh = data.shipped[d] || [];
    const shippedRow = `<tr class="shipped-row">
      <td class="version">SHIPPED</td>
      <td class="count">${{sh.length}}</td>
      <td class="headlines">${{sh.length ? sh.map(h => `<div class="headline">${{esc(h)}}</div>`).join('') : '<div class="empty">none</div>'}}</td>
    </tr>`;
    const rows = data.versions.map(v => {{
      const r = data.results[d][v];
      const hs = (r.headlines || []);
      const cell = hs.length ? hs.map(h => `<div class="headline">${{esc(h)}}</div>`).join('') : '<div class="empty">no events</div>';
      const err = r.error ? `<div class="err">${{esc(r.error)}}</div>` : '';
      return `<tr>
        <td class="version">${{esc(v)}}</td>
        <td class="count">${{r.count}}</td>
        <td class="headlines">${{cell}}${{err}}</td>
      </tr>`;
    }}).join('');
    return `<div class="date-section">
      <div class="date-header">${{esc(d)}}</div>
      <table>
        <thead><tr><th>Prompt version</th><th>#</th><th>Headlines</th></tr></thead>
        <tbody>${{shippedRow}}${{rows}}</tbody>
      </table>
    </div>`;
  }}).join('');
  document.getElementById('results').innerHTML = html;
}}
</script>
</body></html>"""
    return HTMLResponse(content=html)


def _simulate_detect_for_date(conn, season, latest_date):
    """Run the structural detectors (not AI insights) against a specific date
    and return the event list WITHOUT inserting. Used by the events sandbox
    to preview what new rules would produce.

    Returns list of event dicts matching the shape of notable_events rows.
    """
    from services import notable_events as NE
    from services import historical_scans as HS

    events = []

    # Dynamic thresholds (same as detect_all)
    gp_row = conn.execute(
        "SELECT MAX(games) FROM season_batting_stats WHERE season = ?", (season,)
    ).fetchone()
    gp = gp_row[0] if gp_row and gp_row[0] else 10
    hit_min = max(8, min(15, int(gp * 0.75)))
    ob_min = max(12, min(25, int(gp * 1.0)))
    hr_min = max(3, min(5, int(gp * 0.3)))

    # Tier 1
    events += NE.detect_hitting_streaks(conn, season, latest_date, min_games=hit_min)
    events += NE.detect_onbase_streaks(conn, season, latest_date, min_games=ob_min)
    events += NE.detect_hr_streaks(conn, season, latest_date, min_games=hr_min)
    events += NE.detect_pitching_streaks(conn, season, latest_date)
    events += NE.detect_season_pace(conn, season, latest_date)

    # Tier 2
    events += NE.detect_career_milestones(conn, season, latest_date)
    events += NE.detect_rarities(conn, season, latest_date)
    try:
        events += NE.detect_hot_streaks_pelt(conn, season, latest_date)
    except Exception as e:
        events.append({"headline": f"[detect_hot_streaks_pelt error: {e}]",
                       "detection_type": "error", "game_date": latest_date,
                       "player_names": [], "team_names": []})

    # Records (_simulate_records_for_date is in this module)
    try:
        records = _simulate_records_for_date(conn, latest_date)
        for r in records:
            events.append({
                "headline": r.get("detail", ""),
                "detail": "",
                "category": r.get("type", "").replace("_", " ").title(),
                "game_date": latest_date,
                "player_names": [r.get("player")] if r.get("player") else [],
                "team_names": [r.get("team")] if r.get("team") else [],
                "detection_type": r.get("type", "record"),
                "priority": 2,
            })
    except Exception as e:
        events.append({"headline": f"[records error: {e}]",
                       "detection_type": "error", "game_date": latest_date,
                       "player_names": [], "team_names": []})

    # All-time + franchise passing
    try:
        events += NE.detect_alltime_passing(conn, season, latest_date)
        events += NE.detect_franchise_passing(conn, season, latest_date)
    except Exception as e:
        events.append({"headline": f"[passing detect error: {e}]",
                       "detection_type": "error", "game_date": latest_date,
                       "player_names": [], "team_names": []})

    # Historical scans
    try:
        facts = []
        facts += HS.scan_start_of_season_streaks(conn, season, latest_date)
        facts += HS.scan_cross_season_streaks(conn, season, latest_date)
        facts += HS.scan_pitching_start_of_season(conn, season, latest_date)
        facts += HS.scan_team_historical(conn, season, latest_date)
        facts += HS.scan_team_starter_era(conn, season, latest_date)
        facts += HS.scan_career_start(conn, season, latest_date)
        facts += HS.scan_debut_youngest(conn, season, latest_date)
        facts += HS.scan_leaderboard_changes(conn, season, latest_date)
        events += HS.template_facts(conn, facts, season, latest_date)
    except Exception as e:
        events.append({"headline": f"[historical scan error: {e}]",
                       "detection_type": "error", "game_date": latest_date,
                       "player_names": [], "team_names": []})

    # Deep scans (OPS/HR/SB/power-speed/pitching dominance/PELT) with
    # MLB-wide + team-context fallback. Cooldowns reset per-date since
    # the sandbox doesn't carry state across dates; production does.
    try:
        from services.deep_scans import run_deep_scans
        deep_events = run_deep_scans(conn, season, latest_date, cooldowns={})
        for e in deep_events:
            events.append({
                "headline": e.get("detail", ""),
                "detail": "",
                "category": "Deep Scan",
                "game_date": latest_date,
                "player_names": [e.get("player")] if e.get("player") else [],
                "team_names": [e.get("team")] if e.get("team") else [],
                "detection_type": f"deep_scan_{e.get('scan', 'unknown')}",
                "priority": 2,
            })
    except Exception as e:
        events.append({"headline": f"[deep scan error: {e}]",
                       "detection_type": "error", "game_date": latest_date,
                       "player_names": [], "team_names": []})

    return events


@router.api_route("/events-sandbox/run", methods=["GET", "POST"])
async def events_sandbox_run(
    days: int = 3,
    key: str | None = None,
    authorization: str | None = Header(None),
):
    """Run structural detectors (not AI insights) for the last N game dates.
    Returns events WITHOUT inserting — just for preview."""
    verify_admin(authorization, key)
    days = max(1, min(days, 7))
    try:
        conn = sqlite3.connect(DB_PATH)
        season = date.today().year
        rows = conn.execute("""
            SELECT DISTINCT date FROM game_batting_logs
            WHERE season = ? ORDER BY date DESC LIMIT ?
        """, (season, days)).fetchall()
        target_dates = [r[0] for r in rows]

        results = []
        for d in target_dates:
            evts = _simulate_detect_for_date(conn, season, d)
            results.append({
                "game_date": d,
                "count": len(evts),
                "events": [
                    {"headline": e.get("headline", ""),
                     "detection_type": e.get("detection_type", "?"),
                     "category": e.get("category", "?"),
                     "player_names": e.get("player_names", [])}
                    for e in evts
                ],
            })
        conn.close()
        return {"status": "ok", "dates": target_dates, "results": results}
    except Exception as e:
        import traceback
        raise HTTPException(500, f"{str(e)}\n{traceback.format_exc()}")


@router.get("/events-sandbox", response_class=HTMLResponse)
async def events_sandbox_page(
    key: str | None = None,
    authorization: str | None = Header(None),
):
    """HTML page that runs the events sandbox and displays results."""
    verify_admin(authorization, key)
    key_param = key or ""
    html = f"""<!DOCTYPE html>
<html><head><title>Events Sandbox</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: #f5f7fa; color: #1f2937; margin: 0; padding: 24px; }}
  h1 {{ background: linear-gradient(to right, #73B3FF, #1A40B3); -webkit-background-clip: text;
        -webkit-text-fill-color: transparent; margin: 0 0 8px; font-size: 28px; }}
  .sub {{ color: #6b7280; margin-bottom: 20px; font-size: 14px; }}
  .controls {{ display: flex; gap: 12px; align-items: center; margin-bottom: 24px;
              padding: 16px; background: white; border-radius: 12px;
              box-shadow: 0 1px 3px rgba(0,0,0,0.06); }}
  input[type=number] {{ width: 60px; padding: 6px 10px; border: 1px solid #d1d5db; border-radius: 6px; }}
  button {{ background: linear-gradient(135deg, #1A40B3, #73B3FF); color: white;
           border: none; padding: 10px 20px; border-radius: 8px; font-weight: 600; cursor: pointer; }}
  button:disabled {{ opacity: 0.5; cursor: not-allowed; }}
  .status {{ color: #6b7280; font-size: 13px; margin-left: 8px; }}
  .date-section {{ background: white; border-radius: 12px; padding: 20px;
                  margin-bottom: 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.06); }}
  .date-header {{ font-weight: 700; font-size: 18px; margin-bottom: 12px; color: #1A40B3; }}
  .event {{ padding: 10px 0; border-bottom: 1px solid #e5e7eb; line-height: 1.45; }}
  .event:last-child {{ border-bottom: none; }}
  .dtype {{ display: inline-block; font-size: 11px; text-transform: uppercase;
           letter-spacing: 0.5px; color: #6b7280; font-weight: 600; margin-right: 8px; }}
</style></head>
<body>
  <h1>Events Sandbox</h1>
  <div class="sub">Structural detectors (no AI insights), re-run with current code. Events NOT inserted.</div>
  <div class="controls">
    <label>Days back:</label>
    <input id="days" type="number" min="1" max="7" value="3">
    <button id="run" onclick="runSandbox()">Run</button>
    <span class="status" id="status">Ready.</span>
  </div>
  <div id="results"></div>
<script>
const KEY = {json.dumps(key_param)};
function esc(s) {{ return String(s).replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}})[c]); }}
async function runSandbox() {{
  const days = document.getElementById('days').value;
  const btn = document.getElementById('run');
  const status = document.getElementById('status');
  const results = document.getElementById('results');
  btn.disabled = true;
  status.textContent = 'Running detectors…';
  results.innerHTML = '';
  try {{
    const r = await fetch(`/admin/events-sandbox/run?days=${{days}}&key=${{encodeURIComponent(KEY)}}`, {{method: 'POST'}});
    const data = await r.json();
    if (data.status !== 'ok') {{
      status.textContent = 'Error: ' + (data.detail || JSON.stringify(data)).slice(0, 400);
      btn.disabled = false;
      return;
    }}
    const html = data.results.map(d => {{
      const evts = d.events.map(e => `<div class="event"><span class="dtype">${{esc(e.detection_type)}}</span> ${{esc(e.headline)}}</div>`).join('');
      return `<div class="date-section">
        <div class="date-header">${{esc(d.game_date)}} — ${{d.count}} events</div>
        ${{evts || '<div class="event">No events.</div>'}}
      </div>`;
    }}).join('');
    results.innerHTML = html;
    status.textContent = `Done. ${{data.results.length}} dates, ${{data.results.reduce((n, r) => n + r.count, 0)}} total events.`;
  }} catch (e) {{
    status.textContent = 'Error: ' + e.message;
  }}
  btn.disabled = false;
}}
window.addEventListener('DOMContentLoaded', runSandbox);
</script>
</body></html>"""
    return HTMLResponse(content=html)


@router.get("/download-live-db")
async def download_live_db(
    key: str | None = None,
    authorization: str | None = Header(None),
):
    """Stream the live SQLite DB file for the admin to grab. Makes a consistent
    snapshot via SQLite's backup API first to avoid torn reads during writes."""
    verify_admin(authorization, key)
    import tempfile
    snap_path = tempfile.mktemp(suffix=".db", dir="/tmp")
    try:
        src = sqlite3.connect(DB_PATH)
        dst = sqlite3.connect(snap_path)
        src.backup(dst)
        src.close()
        dst.close()
    except Exception as e:
        if os.path.exists(snap_path):
            os.remove(snap_path)
        raise HTTPException(500, f"Snapshot failed: {e}")

    from fastapi.responses import FileResponse
    # Caller must clean up; we leave the temp file for the OS to reap (it's in /tmp)
    return FileResponse(snap_path, filename="baseball_stats_live.db",
                        media_type="application/octet-stream")


@router.get("/inspect-retrosheet-columns")
async def inspect_retrosheet_columns(
    season: int = 1987,
    key: str | None = None,
    authorization: str | None = Header(None),
):
    """One-off: download a Retrosheet season zip and list columns in batting.csv.
    Stdlib-only — no pandas dependency on the server."""
    verify_admin(authorization, key)
    import csv
    import io
    import urllib.request
    import zipfile
    try:
        # Retrosheet publishes per-season CSV ZIPs at this URL pattern
        url = f"https://www.retrosheet.org/downloads/{season}/{season}csvs.zip"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = zf.namelist()
            batting_name = next((n for n in names if "batting" in n.lower()), None)
            if not batting_name:
                return {"error": "no batting file in zip", "files": names}
            with zf.open(batting_name) as f:
                reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8"))
                cols = reader.fieldnames
                sample = []
                for i, row in enumerate(reader):
                    if i >= 3:
                        break
                    sample.append(dict(row))
        return {
            "season": season,
            "zip_files": names,
            "batting_file": batting_name,
            "columns": cols,
            "sample_rows": sample,
        }
    except Exception as e:
        import traceback
        raise HTTPException(500, f"{str(e)}\n{traceback.format_exc()}")


@router.post("/build-historic-moments")
async def build_historic_moments(
    key: str | None = None,
    authorization: str | None = Header(None),
):
    """One-time compute of career milestone crossing dates (500 HR, 3000 H,
    etc.) for the On This Date feature. ~30-60s."""
    verify_admin(authorization, key)
    try:
        from data_pipeline.build_historic_moments import build
        result = build(DB_PATH)
        return {"status": "ok", **result}
    except Exception as e:
        import traceback
        raise HTTPException(500, f"{str(e)}\n{traceback.format_exc()}")


@router.post("/build-historical-streaks")
async def build_historical_streaks(
    key: str | None = None,
    authorization: str | None = Header(None),
):
    """One-time compute of all historical hitting + on-base streaks
    >= threshold across the full game-log history. Populates the
    historical_streaks table for feed ranking context."""
    verify_admin(authorization, key)
    try:
        from data_pipeline.build_historical_streaks import build
        counts = build(DB_PATH)
        return {"status": "ok", **counts}
    except Exception as e:
        import traceback
        raise HTTPException(500, f"{str(e)}\n{traceback.format_exc()}")


@router.get("/test-haiku-merge")
async def test_haiku_merge(
    key: str | None = None,
    authorization: str | None = Header(None),
):
    """One-off: take the 7 known multi-event groups and have Haiku merge each.
    Returns originals + Haiku output for human evaluation."""
    verify_admin(authorization, key)

    HAIKU_MERGE_SYSTEM = """You combine 2+ notable-events headlines about the same baseball player on the same day into ONE flowing card.

Format:
- Sentence 1: the player's full stat line for that game (collect all the stats mentioned across the events into one clean line).
- Sentences 2+: each impact, with a "By [stat-action], [past-tense result]" lead-in tying it back to the relevant stat in the line.

Examples of good lead-ins:
- "By striking out 7, he passed Tom Glavine for 29th on the all-time list."
- "By picking up the win, he is now 1 away from 150 career wins."
- "By stealing his 10th base, he took the AL stolen base lead, passing Jose Caballero."
- "By going 2-for-3, he reached 1,000 career hits."
- "By driving in 4 runs, he is now 4 away from 500 career RBI."

STRICT RULES:
1. Use ONLY facts present in the input headlines. Do NOT add commentary, qualitative descriptions, or invented context like "showcased dominant pitching" or "established momentum."
2. Do NOT derive or compute numbers. If the input says "4 away from 500 career RBI," do NOT write "moved to 496 career RBI." Use only numbers explicitly stated in the input.
3. Past tense throughout (no "-ing" verbs in main clauses).
4. Don't restate the stat line beyond the first sentence.
5. Output ONLY the merged card text — no JSON, no commentary, no quotes."""

    groups = [
        ("Chris Sale", "2026-04-18"),
        ("José Ramírez", "2026-04-18"),
        ("Yandy Díaz", "2026-04-18"),
        ("Yordan Alvarez", "2026-04-18"),
        ("Ranger Suarez", "2026-04-17"),
        ("Austin Riley", "2026-04-17"),
        ("Jose Soriano", "2026-04-17"),
    ]

    import anthropic
    from services.ai_notable_events import ANTHROPIC_API_KEY
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    conn = sqlite3.connect(DB_PATH, timeout=10)
    results = []
    try:
        for player, game_date in groups:
            # player_names stored as ASCII-escaped JSON (e.g. "Jos\u00e9");
            # use json.dumps to match the stored form
            search = json.dumps(player)  # → '"Jos\\u00e9 Ram\\u00edrez"'
            rows = conn.execute("""
                SELECT headline FROM notable_events
                WHERE game_date = ? AND player_names LIKE ?
                ORDER BY id ASC
            """, (game_date, f'%{search}%')).fetchall()
            originals = [r[0] for r in rows]
            if len(originals) < 2:
                results.append({"player": player, "date": game_date,
                                "originals": originals, "merged": "(< 2 events found)"})
                continue
            user_msg = f"Player: {player}\nDate: {game_date}\n\nOriginal event headlines:\n"
            for i, h in enumerate(originals, 1):
                user_msg += f"{i}. {h}\n"
            try:
                response = client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=400,
                    system=HAIKU_MERGE_SYSTEM,
                    messages=[{"role": "user", "content": user_msg}],
                )
                merged = response.content[0].text.strip()
            except Exception as e:
                merged = f"ERROR: {e}"
            results.append({"player": player, "date": game_date,
                            "originals": originals, "merged": merged})
    finally:
        conn.close()
    return {"results": results}


@router.get("/ai-backtest/dates")
async def ai_backtest_dates(
    days: int = 3,
    key: str | None = None,
    authorization: str | None = Header(None),
):
    """Return the last N distinct game dates (no Sonnet calls). Used by the
    backtest page to drive sequential per-date fetches."""
    verify_admin(authorization, key)
    days = max(1, min(days, 7))
    conn = sqlite3.connect(DB_PATH)
    season = date.today().year
    rows = conn.execute("""
        SELECT DISTINCT date FROM game_batting_logs
        WHERE season = ? ORDER BY date DESC LIMIT ?
    """, (season, days)).fetchall()
    conn.close()
    return {"dates": [r[0] for r in rows]}


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


def _alert_ts_json(timestamps: list) -> str:
    """JSON-encode a timestamp list for embedding inside a single-quoted HTML
    attribute. Escapes single quotes so the attribute can't break out, even
    though our ISO-8601 timestamps never contain them in practice."""
    import json as _json
    return _json.dumps(timestamps).replace("'", "&#39;")


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

    # All queries ranked by count, tiebroken by recency.
    # latest_context: the client_context JSON from the most recent row with this
    # query_text (NULL if the column doesn't exist yet or nothing was logged).
    # Used by click-to-expand to show error details inline without a follow-up query.
    queries = conn.execute(f"""
        SELECT q1.query_text, COUNT(*) as cnt,
               MAX(q1.timestamp) as last_seen,
               GROUP_CONCAT(DISTINCT q1.response_type) as types,
               (SELECT q2.client_context FROM query_log AS q2
                WHERE q2.query_text = q1.query_text AND q2.client_context IS NOT NULL
                ORDER BY q2.timestamp DESC LIMIT 1) AS latest_context,
               GROUP_CONCAT(DISTINCT COALESCE(q1.input_method, 'keyboard')) as input_methods
        FROM query_log AS q1
        WHERE 1=1{date_filter.replace('timestamp', 'q1.timestamp')}
        GROUP BY q1.query_text
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

    # Alert-card timestamps per category. No 24h window — we ship the actual
    # timestamp list (capped at 1000) and the browser filters against its
    # localStorage ack. The "count" on each card is however many errors are
    # newer than the ack, nothing falls off silently. "1000+" if the cap is
    # exceeded (not realistic at current volumes).
    def _alert_timestamps(rtype: str) -> list:
        rows = conn.execute(
            "SELECT timestamp FROM query_log WHERE response_type = ? "
            "ORDER BY timestamp DESC LIMIT 1000",
            (rtype,),
        ).fetchall()
        return [r[0] for r in rows]

    client_event_ts = _alert_timestamps("client_event")
    server_error_ts = _alert_timestamps("server_error")
    query_engine_error_ts = _alert_timestamps("query_engine_error")

    conn.close()

    # Build breakdown rows. Badge is clickable — same filterBy() as the inline
    # badges on each query row, so you can jump from the cost summary straight
    # into a filtered view of that type (e.g. find a recent haiku response when
    # none appear near the top of the table).
    breakdown_html = ""
    total_cost = 0.0
    for rtype, cnt in breakdown:
        pct = (cnt / total * 100) if total else 0
        cost = cnt * _COST_PER_QUERY.get(rtype, 0)
        total_cost += cost
        css = rtype.replace(' ', '-')
        breakdown_html += f"""
        <tr>
            <td><span class="badge {css}" style="cursor: pointer;" onclick="filterBy('{rtype}'); document.getElementById('qtable').scrollIntoView({{behavior: 'smooth', block: 'start'}});">{rtype}</span></td>
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
    for text, cnt, last_seen, types, latest_context, input_methods in queries:
        # Convert UTC timestamp to Eastern
        ts_display = _to_eastern(last_seen) if last_seen else ""
        # Build type badges (intercepting clicks so the row doesn't also expand)
        type_list = [t.strip() for t in (types or "").split(",")]
        type_badges = " ".join(
            f'<span class="badge {t.replace(" ", "-")}" onclick="event.stopPropagation(); filterBy(\'{t}\')">{t}</span>'
            for t in type_list
        )
        # Input method badges (mic / keyboard). Show only mic visibly to keep
        # the column clean — keyboard is the silent default.
        method_list = sorted({m.strip() for m in (input_methods or "keyboard").split(",") if m.strip()})
        method_badges = ""
        if "mic" in method_list:
            method_badges = '<span class="badge mic" title="Submitted via voice input">mic</span>'
        escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        raw_ts = last_seen or ""
        types_attr = ",".join(type_list)

        # Pretty-print latest_context for the detail row (if any). Attr-escape
        # so it survives round-trip through data-context.
        if latest_context:
            try:
                import json as _json
                parsed = _json.loads(latest_context)
                context_pretty = _json.dumps(parsed, indent=2, ensure_ascii=False)
            except Exception:
                context_pretty = str(latest_context)
            # HTML-escape then put into a data attribute; JS will inject as textContent.
            context_attr = (context_pretty
                            .replace("&", "&amp;").replace('"', "&quot;")
                            .replace("<", "&lt;").replace(">", "&gt;"))
            row_class = "expandable"
            chevron = '<span class="chev">\u203A</span>'
        else:
            context_attr = ""
            row_class = ""
            chevron = ""

        query_rows += f"""
        <tr class="{row_class}" data-count="{cnt}" data-ts="{raw_ts}" data-types="{types_attr}" data-context="{context_attr}">
            <td class="query-text">{chevron}{escaped}</td>
            <td class="count">{cnt}</td>
            <td class="types">{type_badges} {method_badges}</td>
            <td class="timestamp">{ts_display}</td>
        </tr>"""

    # Build events table from archive + tap counts. Also carry ai_grade and
    # grade_reason (idempotently added below) for the inline AI-insight
    # grading UI. NULL grade = ungraded (no implicit signal — a skip is just
    # a skip, not "low quality").
    conn2 = sqlite3.connect(METERING_DB_PATH)
    try:
        conn2.execute("ALTER TABLE event_archive ADD COLUMN ai_grade INTEGER")
    except Exception:
        pass
    try:
        conn2.execute("ALTER TABLE event_archive ADD COLUMN grade_reason TEXT")
    except Exception:
        pass
    conn2.commit()
    event_rows_data = conn2.execute("""
        SELECT e.id, e.headline, e.detection_type, e.game_date,
               COALESCE(t.taps, 0) as taps,
               e.ai_grade, e.grade_reason
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
    for eid, headline, dtype, gdate, taps, ai_grade, grade_reason in event_rows_data:
        escaped_h = headline.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        cat = _event_category(dtype)
        css_cat = cat.lower().replace(" ", "-")

        # Grade + reason cells — interactive only for AI Insight rows, grayed
        # out but visible for everything else so the columns stay aligned.
        if dtype == "ai_insight":
            grade_val = str(ai_grade) if ai_grade is not None else ""
            reason_val = (grade_reason or "").replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")
            grade_options = ['<option value="">—</option>'] + [
                f'<option value="{n}"{" selected" if str(n) == grade_val else ""}>{n}</option>'
                for n in range(1, 11)
            ]
            graded_attr = "1" if ai_grade is not None else "0"
            grade_cell = (
                f'<select class="grade-select" data-id="{eid}" '
                f'onchange="saveGrade(this)">' + "".join(grade_options) + '</select>'
            )
            # Reason cell: read-only display. Click anywhere on it to open an
            # overlay where the user gets real writing space (and can also
            # adjust the grade). Keeps the inline dropdown for quick tweaks
            # while giving room for substantive notes.
            reason_display = reason_val or '<span class="reason-placeholder">click to add note…</span>'
            reason_cell = (
                f'<div class="grade-reason-display" data-id="{eid}" '
                f'onclick="openGradeOverlay(this)" title="Click to edit">{reason_display}</div>'
            )
        else:
            graded_attr = ""
            grade_cell = '<span class="grade-na">—</span>'
            reason_cell = ""

        # Headline attr for overlay display. Attr-escape since it can contain
        # any character the news of the day threw at us.
        headline_attr = (headline.replace("&", "&amp;").replace('"', "&quot;")
                         .replace("<", "&lt;").replace(">", "&gt;"))
        event_rows += f"""
        <tr data-taps="{taps}" data-date="{gdate}" data-etype="{cat}" data-graded="{graded_attr}" data-id="{eid}" data-headline="{headline_attr}">
            <td class="query-text">{escaped_h}</td>
            <td><span class="badge evt-{css_cat}" onclick="filterEvents('{cat}')">{cat}</span></td>
            <td class="count">{taps}</td>
            <td class="timestamp">{gdate}</td>
            <td class="grade-cell">{grade_cell}</td>
            <td class="reason-cell">{reason_cell}</td>
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
  .badge.client_event {{ background: #fef3c7; color: #b45309; }}
  .badge.server_error {{ background: #fee2e2; color: #991b1b; }}
  /* query_engine_error stored with underscores — previous .query-engine-error
     rule never matched because .replace(' ', '-') only touches spaces */
  .badge.query_engine_error {{ background: #fee2e2; color: #7f1d1d; }}
  tr.expandable {{ cursor: pointer; }}
  tr.expandable:active {{ background: #f5f7fa; }}
  tr.expandable .chev {{
    display: inline-block; margin-right: 6px; color: #1A40B3;
    font-size: 13px; transition: transform 0.15s;
  }}
  tr.expandable.open .chev {{ transform: rotate(90deg); }}
  tr.detail-row td {{
    background: #fafbff; padding: 10px 14px; border-bottom: 1px solid #e0e8f5;
  }}
  tr.detail-row pre {{
    font-family: -apple-system, ui-monospace, Menlo, monospace;
    font-size: 11px; color: #1d1d1f;
    white-space: pre-wrap; word-break: break-word;
    max-height: 400px; overflow: auto;
    margin: 0;
  }}
  .grade-select {{
    font-size: 12px; padding: 2px 4px; border: 1px solid #ddd;
    border-radius: 4px; background: #fff; font-family: inherit;
    min-width: 48px;
  }}
  .grade-reason-display {{
    font-size: 12px; padding: 4px 8px; border: 1px solid transparent;
    border-radius: 4px; width: 220px; font-family: inherit;
    cursor: text; white-space: nowrap; overflow: hidden;
    text-overflow: ellipsis; background: #fafafa;
    min-height: 22px; line-height: 1.4;
  }}
  .grade-reason-display:hover {{ border-color: #ddd; background: #fff; }}
  .reason-placeholder {{ color: #bbb; }}
  .grade-na {{ color: #ccc; font-size: 12px; }}
  .grade-cell {{ white-space: nowrap; }}
  .reason-cell {{ white-space: nowrap; }}
  .grade-saved {{ background: #dcfce7; transition: background 0.6s; }}
  .grade-filter-row {{
    display: inline-flex; align-items: center; gap: 6px;
    margin-left: 12px; font-size: 13px; color: #555;
  }}
  /* Grading overlay */
  .overlay-backdrop {{
    display: none; position: fixed; inset: 0;
    background: rgba(20, 20, 30, 0.4); z-index: 1000;
    align-items: center; justify-content: center;
  }}
  .overlay-backdrop.open {{ display: flex; }}
  .overlay-box {{
    background: #fff; border-radius: 12px; padding: 18px 22px;
    width: min(580px, calc(100vw - 32px));
    box-shadow: 0 12px 40px rgba(0, 0, 0, 0.18);
    font-family: -apple-system, system-ui, sans-serif;
  }}
  .overlay-headline {{
    color: #999; font-size: 13px; line-height: 1.4;
    padding: 10px 12px; background: #f5f5f7; border-radius: 8px;
    margin-bottom: 14px; white-space: normal; word-break: break-word;
  }}
  .overlay-row {{
    display: flex; align-items: center; gap: 10px; margin-bottom: 10px;
  }}
  .overlay-row label {{
    font-size: 12px; color: #555; min-width: 60px; text-transform: uppercase;
    letter-spacing: 0.4px; font-weight: 600;
  }}
  .overlay-grade {{
    font-size: 14px; padding: 6px 10px; border: 1px solid #ddd;
    border-radius: 6px; background: #fff; font-family: inherit;
  }}
  .overlay-reason {{
    width: 100%; padding: 10px 12px; border: 1px solid #ddd;
    border-radius: 8px; font-family: inherit; font-size: 14px;
    line-height: 1.5; resize: vertical; min-height: 110px; box-sizing: border-box;
  }}
  .overlay-buttons {{
    display: flex; gap: 8px; justify-content: flex-end; margin-top: 14px;
  }}
  .overlay-buttons button {{
    padding: 7px 16px; border-radius: 6px; font-size: 13px;
    font-family: inherit; cursor: pointer; border: 1px solid #ddd;
    background: #fff;
  }}
  .overlay-buttons .save {{
    background: #1A40B3; color: #fff; border-color: #1A40B3;
  }}
  .overlay-buttons button:active {{ opacity: 0.85; }}
  .badge.evt-ai-insight {{ background: #fef3c7; color: #92400e; }}
  .badge.evt-historical {{ background: #e0e7ff; color: #3730a3; }}
  .badge.evt-streak {{ background: #fce7f3; color: #9d174d; }}
  .badge.evt-milestone {{ background: #d1fae5; color: #065f46; }}
  .badge.evt-matchup {{ background: #ede9fe; color: #5b21b6; }}
  .badge.evt-on-this-date {{ background: #f0f9ff; color: #0c4a6e; }}
  .badge.evt-leader-change {{ background: #fff7ed; color: #9a3412; }}
  .badge.haiku {{ background: #dbeafe; color: #1A40B3; }}
  .badge.sonnet {{ background: #f3e8ff; color: #6b21a8; }}
  .badge.mic {{ background: #ffedd5; color: #9a3412; }}
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
  <div class="stat-card alert-card" data-filter="client_event" data-timestamps='{_alert_ts_json(client_event_ts)}' style="display: none; background: linear-gradient(135deg, #b45309, #f59e0b); cursor: pointer;" onclick="filterBy('client_event')">
    <div class="label">Client Issues</div>
    <div class="value">0</div>
  </div>
  <div class="stat-card alert-card" data-filter="server_error" data-timestamps='{_alert_ts_json(server_error_ts)}' style="display: none; background: linear-gradient(135deg, #991b1b, #ef4444); cursor: pointer;" onclick="filterBy('server_error')">
    <div class="label">Server Errors</div>
    <div class="value">0</div>
  </div>
  <div class="stat-card alert-card" data-filter="query_engine_error" data-timestamps='{_alert_ts_json(query_engine_error_ts)}' style="display: none; background: linear-gradient(135deg, #7f1d1d, #dc2626); cursor: pointer;" onclick="filterBy('query_engine_error')">
    <div class="label">Query Engine Errors</div>
    <div class="value">0</div>
  </div>
</div>
<script>
// Synchronous: decide card visibility AND count BEFORE anything paints.
// Each card ships a JSON array of recent error timestamps (newest first,
// capped at 1000). We filter to "newer than localStorage ack" — that
// filtered length becomes the card's count, and the card hides entirely
// if it's 0. Ack persists across reloads; card only reappears when a
// newer error arrives. No 24h window — nothing falls off silently.
// Force-reset with localStorage.clear() in the console.
(function () {{
  document.querySelectorAll('.alert-card').forEach(function (card) {{
    var timestamps;
    try {{ timestamps = JSON.parse(card.dataset.timestamps || '[]'); }}
    catch (e) {{ timestamps = []; }}
    var acked = localStorage.getItem('ack_' + card.dataset.filter) || '';
    // timestamps are desc-sorted → count how many are newer than ack
    var unacked = 0;
    for (var i = 0; i < timestamps.length; i++) {{
      if (acked && timestamps[i] <= acked) break;
      unacked++;
    }}
    if (unacked > 0) {{
      var valueEl = card.querySelector('.value');
      valueEl.textContent = unacked >= 1000 ? '1000+' : String(unacked);
      card.style.display = '';
    }}
  }});
}})();
</script>

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
  <label class="grade-filter-row">
    <input type="checkbox" id="ungraded-only" onchange="applyUngradedFilter()">
    Ungraded AI insights only
  </label>
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
    <th>Grade</th>
    <th>Reason</th>
  </tr>
  {event_rows}
</table>
<div class="pagination">
  <button id="evt-prev" onclick="changeEvtPage(-1)" disabled>&larr; Prev</button>
  <span class="page-info" id="evt-page-info"></span>
  <button id="evt-next" onclick="changeEvtPage(1)">Next &rarr;</button>
</div>
<!-- Grading overlay: shown when a reason cell is clicked. Fields get populated
     by openGradeOverlay(); Save button calls saveGradeOverlay() which mirrors
     back to the row without a reload. ESC or backdrop click closes. -->
<div id="overlay-backdrop" class="overlay-backdrop">
  <div class="overlay-box" onclick="event.stopPropagation()">
    <div id="overlay-headline" class="overlay-headline"></div>
    <div class="overlay-row">
      <label for="overlay-grade">Grade</label>
      <select id="overlay-grade" class="overlay-grade">
        <option value="">—</option>
        <option value="1">1</option><option value="2">2</option>
        <option value="3">3</option><option value="4">4</option>
        <option value="5">5</option><option value="6">6</option>
        <option value="7">7</option><option value="8">8</option>
        <option value="9">9</option><option value="10">10</option>
      </select>
    </div>
    <textarea id="overlay-reason" class="overlay-reason"
              placeholder="Why this grade? What would make it a 10? Which part was weak (pick vs. write)?"></textarea>
    <div class="overlay-buttons">
      <button onclick="closeGradeOverlay()">Cancel</button>
      <button class="save" onclick="saveGradeOverlay()">Save</button>
    </div>
  </div>
</div>

<script>
const PAGE_SIZE = 30;
// Admin key for authenticated AJAX endpoints (grading). Read from the
// current URL so the same `?key=...` we arrived with is reused.
const ADMIN_KEY = new URLSearchParams(location.search).get('key') || '';
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
  // Hide the matching alert card AND persist the ack in localStorage using
  // the newest timestamp from the card's array. On the next page load, the
  // card's count (derived from "timestamps newer than ack") will be 0 and
  // the card will stay hidden — until a brand-new error arrives with an even
  // newer timestamp, at which point the count becomes > 0 and the card
  // reappears automatically.
  document.querySelectorAll('.alert-card').forEach(card => {{
    if (card.dataset.filter === type) {{
      card.style.display = 'none';
      try {{
        const ts = JSON.parse(card.dataset.timestamps || '[]');
        if (ts.length > 0) {{
          localStorage.setItem('ack_' + type, ts[0]);  // desc-sorted; [0] is newest
        }}
      }} catch (e) {{ /* bad JSON, skip ack */ }}
    }}
  }});
  renderPage();
}}

function clearFilter() {{
  // Clearing the filter removes the table filter only. Alert cards that were
  // hidden when the user clicked stay hidden — viewing them already counted
  // as acknowledgment. A page reload re-evaluates fresh counts and will bring
  // them back if there are still unacknowledged errors in the window.
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

// (Alert-card visibility is decided synchronously by the inline script
// immediately after the .stat-cards block — see above. No deferred logic
// here or cards would flash in before being hidden.)

// Click-to-expand on rows with client_context (server_error, client_event).
// Toggles a detail row beneath showing the pretty-printed JSON.
document.querySelectorAll('#qtable tr.expandable').forEach(row => {{
  row.addEventListener('click', () => {{
    const ctx = row.dataset.context;
    if (!ctx) return;
    // If a detail row already exists right below, toggle it off
    const next = row.nextElementSibling;
    if (next && next.classList.contains('detail-row') && next.dataset.for === row.dataset.ts) {{
      next.remove();
      row.classList.remove('open');
      return;
    }}
    // Collapse any other open detail rows first (one at a time)
    document.querySelectorAll('#qtable tr.detail-row').forEach(d => d.remove());
    document.querySelectorAll('#qtable tr.expandable.open').forEach(r => r.classList.remove('open'));
    // Decode HTML entities that slipped into the data attribute
    const decoded = ctx
      .replace(/&lt;/g, '<').replace(/&gt;/g, '>')
      .replace(/&quot;/g, '"').replace(/&amp;/g, '&');
    const detail = document.createElement('tr');
    detail.className = 'detail-row';
    detail.dataset.for = row.dataset.ts;
    const cell = document.createElement('td');
    cell.colSpan = 4;
    const pre = document.createElement('pre');
    pre.textContent = decoded;
    cell.appendChild(pre);
    detail.appendChild(cell);
    row.classList.add('open');
    row.parentNode.insertBefore(detail, row.nextSibling);
  }});
}});

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
  const ungradedOnly = document.getElementById('ungraded-only');
  if (ungradedOnly && ungradedOnly.checked) {{
    // Only AI insight rows that haven't been graded yet (data-graded=0)
    all = all.filter(r => r.dataset.etype === 'AI Insight' && r.dataset.graded === '0');
  }}
  return all;
}}

function applyUngradedFilter() {{
  evtPage = 0;
  renderEvents();
}}

// Autosave grade on dropdown change. Empty → clears the grade (back to
// NULL, ungraded). Any 1-10 sets the grade and marks the row graded.
function saveGrade(sel) {{
  const id = sel.dataset.id;
  const val = sel.value;
  const body = {{ id: Number(id) }};
  if (val !== '') body.grade = Number(val);
  else body.grade = null;
  fetch('/admin/grade-insight?key=' + encodeURIComponent(ADMIN_KEY), {{
    method: 'POST',
    headers: {{ 'Content-Type': 'application/json' }},
    body: JSON.stringify(body)
  }}).then(r => {{
    if (!r.ok) return;
    const row = sel.closest('tr');
    row.dataset.graded = val === '' ? '0' : '1';
    row.classList.add('grade-saved');
    setTimeout(() => row.classList.remove('grade-saved'), 600);
  }});
}}

// Reason editing now happens in the overlay (openGradeOverlay); the
// inline reason cell is read-only display. saveReason() removed.

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

// Grading overlay — populated on open, cleared on close. Save pushes the
// same /admin/grade-insight endpoint the inline dropdown uses, then
// mirrors the result back into the row so the page stays consistent
// without a full reload.
let overlayCurrentId = null;

function openGradeOverlay(el) {{
  const row = el.closest('tr');
  const id = row.dataset.id;
  const headline = (row.dataset.headline || '')
    .replace(/&lt;/g, '<').replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"').replace(/&amp;/g, '&');
  const gradeSel = row.querySelector('.grade-select');
  const currentGrade = gradeSel ? gradeSel.value : '';
  // Pull current reason from the display div's textContent (handles HTML
  // entities decoded by the browser already). If the display is just the
  // placeholder span, treat as empty.
  const displayEl = row.querySelector('.grade-reason-display');
  const placeholderEl = displayEl ? displayEl.querySelector('.reason-placeholder') : null;
  const currentReason = placeholderEl ? '' : (displayEl ? displayEl.textContent.trim() : '');

  overlayCurrentId = id;
  document.getElementById('overlay-headline').textContent = headline;
  const overlayGrade = document.getElementById('overlay-grade');
  overlayGrade.value = currentGrade;
  document.getElementById('overlay-reason').value = currentReason;
  document.getElementById('overlay-backdrop').classList.add('open');
  // Focus the textarea after the DOM has time to settle
  setTimeout(() => document.getElementById('overlay-reason').focus(), 50);
}}

function closeGradeOverlay() {{
  document.getElementById('overlay-backdrop').classList.remove('open');
  overlayCurrentId = null;
}}

function saveGradeOverlay() {{
  if (overlayCurrentId === null) return;
  const id = overlayCurrentId;
  const gradeRaw = document.getElementById('overlay-grade').value;
  const reason = document.getElementById('overlay-reason').value;
  const body = {{ id: Number(id), reason: reason }};
  body.grade = gradeRaw === '' ? null : Number(gradeRaw);

  fetch('/admin/grade-insight?key=' + encodeURIComponent(ADMIN_KEY), {{
    method: 'POST',
    headers: {{ 'Content-Type': 'application/json' }},
    body: JSON.stringify(body)
  }}).then(r => {{
    if (!r.ok) {{ alert('Save failed (HTTP ' + r.status + ')'); return; }}
    // Mirror back into the row without reloading
    const row = document.querySelector(`#etable tr[data-id="${{id}}"]`);
    if (row) {{
      const sel = row.querySelector('.grade-select');
      if (sel) sel.value = gradeRaw;
      row.dataset.graded = gradeRaw === '' ? '0' : '1';
      const displayEl = row.querySelector('.grade-reason-display');
      if (displayEl) {{
        if (reason) {{
          const escaped = reason
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
          displayEl.innerHTML = escaped;
        }} else {{
          displayEl.innerHTML = '<span class="reason-placeholder">click to add note…</span>';
        }}
      }}
      row.classList.add('grade-saved');
      setTimeout(() => row.classList.remove('grade-saved'), 600);
    }}
    closeGradeOverlay();
  }}).catch(e => alert('Save failed: ' + e));
}}

// Close on backdrop click (not content click) + Escape key
document.getElementById('overlay-backdrop').addEventListener('click', e => {{
  if (e.target.id === 'overlay-backdrop') closeGradeOverlay();
}});
document.addEventListener('keydown', e => {{
  if (e.key === 'Escape' && overlayCurrentId !== null) closeGradeOverlay();
}});
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


def _season_rank_for_game_stat(conn, stat, threshold, season, target_date, is_pitching=False):
    """Return a phrase like 'the Nth N+ stat-game in MLB this season' for a
    career-high event, so a big single-game stat gets season-level context.
    Returns None when the count is too common to be worth mentioning (>25)."""
    table = "game_pitching_logs" if is_pitching else "game_batting_logs"
    row = conn.execute(f"""
        SELECT COUNT(*) FROM {table}
        WHERE season = ? AND {stat} >= ? AND date <= ?
    """, (season, threshold, target_date)).fetchone()
    count = row[0] if row else 0
    if count <= 0 or count > 25:
        return None

    # Stat label for headline
    labels = {
        "home_runs": f"{threshold}-HR",
        "hits": f"{threshold}-hit",
        "rbi": f"{threshold}-RBI",
        "stolen_bases": f"{threshold}-SB",
        "doubles": f"{threshold}-double",
        "strikeouts": f"{threshold}+ K" if is_pitching else f"{threshold}-K",
    }
    what = labels.get(stat, f"{threshold}-{stat}")
    scope = "start" if is_pitching else "game"
    if count == 1:
        return f"the first {what} {scope} in MLB this season"
    return f"the {_ordinal_number(count)} {what} {scope} in MLB this season"


def _season_milestone_last_since(conn, stat_col, threshold, current_season,
                                  label_short, exclude_player_id=None):
    """For a season milestone crossing (e.g., 50+ HR), find the most recent
    year in MLB history that any player hit that threshold. Returns a phrase
    like ', the first 50-HR season since Y's N in YYYY' or ''."""
    row = conn.execute(f"""
        SELECT p.name, s.season, s.{stat_col}
        FROM season_batting_stats s JOIN players p ON s.player_id = p.player_id
        WHERE s.{stat_col} >= ? AND s.season < ? AND s.player_id != ?
        ORDER BY s.season DESC, s.{stat_col} DESC LIMIT 1
    """, (threshold, current_season, exclude_player_id or "")).fetchone()
    if not row:
        return f", the first {threshold}-{label_short} season in MLB history"
    name, yr, val = row
    return f", the first {threshold}-{label_short} season by any player since {name}'s {val} in {yr}"


def _ordinal_number(n):
    """Int → '1st', '2nd', '3rd', '4th', ..."""
    if 10 <= n % 100 <= 20:
        return f"{n}th"
    suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


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
        raise HTTPException(400, "Missing date parameter (use ?date=YYYY-MM-DD)")

    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        events = _simulate_records_for_date(conn, date_str)
        # Deep scans disabled in sandbox (too heavy for browsing historical dates)
        # They run in production via detect_all on the current date only
        return {"date": date_str, "events": events}
    finally:
        conn.close()


@router.get("/records-simulate-all")
async def records_simulate_all(
    key: str | None = None,
    authorization: str | None = Header(None),
):
    """Simulate all dates from March 27 through today with cooldown tracking."""
    verify_admin(authorization, key)
    from datetime import date as _date, timedelta as _td
    from services.deep_scans import run_deep_scans

    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        cooldowns = {}  # Shared across all dates
        all_results = []
        start = _date(_date.today().year, 3, 27)
        end = _date.today()
        d = start
        while d <= end:
            date_str = d.isoformat()
            events = _simulate_records_for_date(conn, date_str)
            try:
                deep_events = run_deep_scans(conn, d.year, date_str, cooldowns)
                events.extend(deep_events)
            except Exception as e:
                events.append({"type": "error", "detail": f"Deep scan error: {e}"})
            if events:
                all_results.append({"date": date_str, "events": events})
            d += _td(days=1)
        return {"results": all_results, "total_events": sum(len(r["events"]) for r in all_results)}
    finally:
        conn.close()


@router.get("/records-simulate-week")
async def records_simulate_week(
    start_date: str = "",
    key: str | None = None,
    authorization: str | None = Header(None),
):
    """Run deep scans for a week starting from start_date. Returns events grouped by date."""
    verify_admin(authorization, key)
    if not start_date:
        raise HTTPException(400, "Missing start_date (YYYY-MM-DD)")
    from datetime import date as _date, timedelta as _td
    from services.deep_scans import run_deep_scans

    conn = sqlite3.connect(DB_PATH, timeout=60)
    try:
        cooldowns = {}
        results = []
        start = _date.fromisoformat(start_date)

        # Get game dates within this week
        end = start + _td(days=6)
        dates = conn.execute("""
            SELECT DISTINCT date FROM game_batting_logs
            WHERE date >= ? AND date <= ? ORDER BY date
        """, (start.isoformat(), end.isoformat())).fetchall()

        for (date_str,) in dates:
            try:
                deep_events = run_deep_scans(conn, int(date_str[:4]), date_str, cooldowns)
                if deep_events:
                    results.append({"date": date_str, "events": deep_events})
            except Exception as e:
                results.append({"date": date_str, "events": [{"type": "error", "detail": str(e)}]})

        total = sum(len(r["events"]) for r in results)

        # Find prev/next week bounds
        prev_start = (start - _td(days=7)).isoformat()
        next_start = (start + _td(days=7)).isoformat()

        return {
            "start_date": start_date,
            "end_date": end.isoformat(),
            "total_events": total,
            "results": results,
            "prev_start": prev_start,
            "next_start": next_start,
        }
    finally:
        conn.close()


def _action_phrase(label, game_val):
    """Convert a stat label + today's value into a natural action phrase."""
    if label == "home runs":
        return "hit a homer" if game_val == 1 else f"hit {game_val} homers"
    if label == "hits":
        return f"went {game_val}-for with {game_val} hits" if game_val > 1 else "singled"
    if label == "RBI":
        return "knocked in a run" if game_val == 1 else f"knocked in {game_val} runs"
    if label == "runs":
        return "scored a run" if game_val == 1 else f"scored {game_val} runs"
    if label == "stolen bases":
        return "stole a base" if game_val == 1 else f"stole {game_val} bases"
    if label == "doubles":
        return "hit a double" if game_val == 1 else f"hit {game_val} doubles"
    if label == "walks":
        return "drew a walk" if game_val == 1 else f"drew {game_val} walks"
    return f"added {game_val} {label}"


def _simulate_records_for_date(conn, target_date):
    """Find all record-related events for a specific date.

    Rules:
    - Player MUST have contributed to the stat on this date (HR>0 for HR records, etc.)
    - Counting stats only (skip rate stats — ERA/WHIP records have no IP minimum)
    - Career and season records against team records
    - Career firsts: HR, win, save only
    - Career highs: single game, min threshold (3+ hits, 2+ HR, 5+ RBI, 3+ R, 10+ K)
    - Milestone thresholds: 50/60 HR seasons, 20/20 30/30 40/40 HR/SB combos
    """
    events = []
    season = int(target_date[:4])

    # Counting stats to check (stat_col, game_log_col, label, min_interesting_game_val)
    BAT_COUNTING = [
        ("home_runs", "home_runs", "home runs", 1),
        ("hits", "hits", "hits", 1),
        ("rbi", "rbi", "RBI", 1),
        ("runs", "runs", "runs", 1),
        ("stolen_bases", "stolen_bases", "stolen bases", 1),
        ("doubles", "doubles", "doubles", 1),
        ("walks", "walks", "walks", 1),
    ]
    PITCH_COUNTING = [
        ("wins", "win", "wins", 1),
        ("strikeouts", "strikeouts", "strikeouts", 1),
        ("saves", "save", "saves", 1),
    ]

    # --- Get all game logs for this date ---
    bat_games = conn.execute("""
        SELECT g.player_id, p.name, p.team,
               g.hits, g.home_runs, g.rbi, g.runs, g.stolen_bases,
               g.doubles, g.walks, g.at_bats
        FROM game_batting_logs g
        JOIN players p ON g.player_id = p.player_id
        WHERE g.date = ?
    """, (target_date,)).fetchall()

    pitch_games = conn.execute("""
        SELECT g.player_id, p.name, p.team,
               g.win, g.strikeouts, g.save, g.hits AS hits_allowed,
               g.earned_runs, g.ip_outs, g.loss
        FROM game_pitching_logs g
        JOIN players p ON g.player_id = p.player_id
        WHERE g.date = ?
    """, (target_date,)).fetchall()

    # Index game stats by player_id
    bat_by_pid = {}
    for row in bat_games:
        pid = row[0]
        bat_by_pid[pid] = {
            "name": row[1], "team": row[2],
            "hits": row[3] or 0, "home_runs": row[4] or 0,
            "rbi": row[5] or 0, "runs": row[6] or 0,
            "stolen_bases": row[7] or 0, "doubles": row[8] or 0,
            "walks": row[9] or 0, "at_bats": row[10] or 0,
        }

    pitch_by_pid = {}
    for row in pitch_games:
        pid = row[0]
        pitch_by_pid[pid] = {
            "name": row[1], "team": row[2],
            "win": row[3] or 0, "strikeouts": row[4] or 0,
            "save": row[5] or 0, "ip_outs": row[8] or 0,
        }

    all_pids = set(bat_by_pid.keys()) | set(pitch_by_pid.keys())

    # --- Get current team for each player ---
    def get_current_team(pid):
        row = conn.execute("""
            SELECT team FROM season_batting_stats
            WHERE player_id = ? AND season = ?
            UNION
            SELECT team FROM season_pitching_stats
            WHERE player_id = ? AND season = ?
            LIMIT 1
        """, (pid, season, pid, season)).fetchone()
        if row and row[0]:
            return row[0].split("/")[0].strip()
        return None

    from services.franchise import get_franchise_codes

    for pid in all_pids:
        bat = bat_by_pid.get(pid, {})
        pitch = pitch_by_pid.get(pid, {})
        pname = bat.get("name") or pitch.get("name")
        team_code = get_current_team(pid)
        if not team_code:
            continue
        team_name = _team_display(team_code)

        # ===== CAREER FIRSTS =====
        # Skip first career win/save in first 3 weeks of season (too much noise)
        from datetime import datetime as _dt, timedelta as _td
        try:
            game_dt = _dt.strptime(target_date, "%Y-%m-%d")
            season_start = _dt(season, 3, 25)  # approximate opening day
            early_season = (game_dt - season_start).days < 21
        except Exception:
            early_season = False

        # First career HR (always interesting)
        if bat.get("home_runs", 0) > 0:
            career_hr_before = conn.execute("""
                SELECT COALESCE(SUM(home_runs), 0) FROM game_batting_logs
                WHERE player_id = ? AND date < ?
            """, (pid, target_date)).fetchone()[0]
            if career_hr_before == 0:
                events.append({
                    "type": "career_first",
                    "player": pname, "team": team_name,
                    "detail": f"{pname} hit his first career home run",
                })

        # First career win (skip early season noise)
        if pitch.get("win", 0) > 0 and not early_season:
            career_w_before = conn.execute("""
                SELECT COALESCE(SUM(win), 0) FROM game_pitching_logs
                WHERE player_id = ? AND date < ?
            """, (pid, target_date)).fetchone()[0]
            if career_w_before == 0:
                events.append({
                    "type": "career_first",
                    "player": pname, "team": team_name,
                    "detail": f"{pname} earned his first career win",
                })

        # First career save — removed, usually flukey, not prospect-driven

        # ===== CAREER HIGHS (single game) =====
        career_high_checks = [
            ("hits", 4), ("home_runs", 3), ("rbi", 5),
            ("stolen_bases", 3),
        ]
        # Build game line for context: "2-for-4, 1 HR, with 6 RBI"
        game_line = ""
        if bat:
            h, ab = bat.get("hits", 0), bat.get("at_bats", 0)
            hr = bat.get("home_runs", 0)
            rbi_val = bat.get("rbi", 0)
            main_parts = [f"{h}-for-{ab}"]
            if hr: main_parts.append(f"{hr} HR")
            if rbi_val:
                game_line = ", ".join(main_parts) + f" with {rbi_val} RBI"
            else:
                game_line = ", ".join(main_parts)

        for stat, min_val in career_high_checks:
            today_val = bat.get(stat, 0)
            if today_val >= min_val:
                prev_row = conn.execute(f"""
                    SELECT {stat}, date FROM game_batting_logs
                    WHERE player_id = ? AND date < ?
                    ORDER BY {stat} DESC LIMIT 1
                """, (pid, target_date)).fetchone()
                prev_high = prev_row[0] if prev_row else 0
                prev_date = prev_row[1] if prev_row else None
                if today_val > (prev_high or 0):
                    _stat_display = {"rbi": "RBI", "home_runs": "home runs",
                                     "hits": "hits", "stolen_bases": "stolen bases",
                                     "doubles": "doubles"}
                    stat_label = _stat_display.get(stat, stat.replace("_", " "))
                    # For hits, just state the threshold — "topping 3 hits" is silly
                    if stat == "hits":
                        context = f"his first career {today_val}-hit game"
                    elif prev_high and prev_date:
                        try:
                            from datetime import datetime as _dt
                            prev_fmt = _dt.strptime(prev_date, "%Y-%m-%d").strftime("%b %-d, %Y")
                        except Exception:
                            prev_fmt = prev_date
                        context = f"a new career high, topping his previous best of {prev_high} set on {prev_fmt}"
                    elif prev_high:
                        context = f"a new career high, topping his previous best of {prev_high}"
                    else:
                        context = f"the first time in his career"

                    # Season-rank context: Nth N+ stat-game in MLB this season
                    season_ctx = _season_rank_for_game_stat(
                        conn, stat, today_val, season, target_date, is_pitching=False
                    )

                    detail = f"{pname} went {game_line}. The {today_val} {stat_label} in a game is {context}"
                    if season_ctx:
                        detail += f" — {season_ctx}"
                    detail += "."
                    events.append({
                        "type": "career_high",
                        "player": pname, "team": team_name,
                        "stat": stat,
                        "detail": detail,
                    })

        # Pitching career highs
        if pitch.get("strikeouts", 0) >= 10:
            prev_k_row = conn.execute("""
                SELECT strikeouts, date FROM game_pitching_logs
                WHERE player_id = ? AND date < ?
                ORDER BY strikeouts DESC LIMIT 1
            """, (pid, target_date)).fetchone()
            prev_k = prev_k_row[0] if prev_k_row else 0
            prev_k_date = prev_k_row[1] if prev_k_row else None
            if pitch["strikeouts"] > (prev_k or 0):
                k = pitch["strikeouts"]
                ip_outs = pitch.get("ip_outs", 0)
                ip_display = f"{ip_outs // 3}.{ip_outs % 3}" if ip_outs else "?"
                if prev_k and prev_k_date:
                    try:
                        from datetime import datetime as _dt
                        prev_fmt = _dt.strptime(prev_k_date, "%Y-%m-%d").strftime("%b %-d, %Y")
                    except Exception:
                        prev_fmt = prev_k_date
                    context = f"a new career high, topping his previous best of {prev_k} set on {prev_fmt}"
                elif prev_k:
                    context = f"a new career high, topping his previous best of {prev_k}"
                else:
                    context = "the first time in his career"

                # Season-rank context: Nth N+ K start in MLB this season
                season_ctx = _season_rank_for_game_stat(
                    conn, "strikeouts", k, season, target_date, is_pitching=True
                )
                detail = f"{pname} struck out {k} in {ip_display} IP. The {k} K is {context}"
                if season_ctx:
                    detail += f" — {season_ctx}"
                detail += "."
                events.append({
                    "type": "career_high",
                    "player": pname, "team": team_name,
                    "stat": "strikeouts",
                    "detail": detail,
                })

        # ===== TEAM RECORD APPROACHES / CROSSINGS (career) =====
        franchise_codes = get_franchise_codes(team_code)

        for stat_col, game_col, label, _ in BAT_COUNTING:
            game_val = bat.get(game_col, 0) if game_col in bat else bat.get(stat_col, 0)
            if game_val <= 0:
                continue  # Must have contributed today

            # Career total through target date using game logs
            career_total = conn.execute(f"""
                SELECT COALESCE(SUM({stat_col}), 0) FROM game_batting_logs
                WHERE player_id = ? AND date <= ?
            """, (pid, target_date)).fetchone()[0]

            # Check records across all franchise codes
            fc_placeholders = ",".join("?" * len(franchise_codes))
            rec = conn.execute(f"""
                SELECT value, player_name, player_id FROM team_records
                WHERE team_code IN ({fc_placeholders}) AND stat = ? AND record_type = 'career'
                ORDER BY value DESC LIMIT 1
            """, franchise_codes + [stat_col]).fetchone()
            if rec and career_total:
                # Skip if the record holder is the current player
                if rec[2] == pid:
                    continue
                diff = int(rec[0]) - int(career_total)
                if 1 <= diff <= 3:
                    events.append({
                        "type": "record_approach",
                        "player": pname, "team": team_name,
                        "detail": f"{pname} {_action_phrase(label, game_val)}, giving him {int(career_total)} career {label} as a member of the {team_name} — {diff} from the franchise record held by {rec[1]} ({int(rec[0])}).",
                    })
                elif diff <= 0 and rec[1] != pname:
                    # Only fire on the day they actually crossed
                    yesterday_total = career_total - game_val
                    if yesterday_total < int(rec[0]):
                        events.append({
                            "type": "record_crossing",
                            "player": pname, "team": team_name,
                            "detail": f"{pname} passed {rec[1]} for the {team_name} career {label} record ({int(career_total)} vs {int(rec[0])})",
                        })

        for stat_col, game_col, label, _ in PITCH_COUNTING:
            game_val = pitch.get(game_col, 0)
            if game_val <= 0:
                continue

            # Career total through target date using game logs
            # game_col is the game log column name (win, strikeouts, save)
            career_total = conn.execute(f"""
                SELECT COALESCE(SUM({game_col}), 0) FROM game_pitching_logs
                WHERE player_id = ? AND date <= ?
            """, (pid, target_date)).fetchone()[0]

            fc_placeholders = ",".join("?" * len(franchise_codes))
            rec = conn.execute(f"""
                SELECT value, player_name, player_id FROM team_records
                WHERE team_code IN ({fc_placeholders}) AND stat = ? AND record_type = 'career'
                ORDER BY value DESC LIMIT 1
            """, franchise_codes + [stat_col]).fetchone()
            if rec and career_total:
                if rec[2] == pid:
                    continue  # Skip own record
                diff = int(rec[0]) - int(career_total)
                if 1 <= diff <= 3:
                    events.append({
                        "type": "record_approach",
                        "player": pname, "team": team_name,
                        "detail": f"{pname} now has {int(career_total)} career {label} with the {team_name} — {diff} from the franchise record held by {rec[1]} ({int(rec[0])}).",
                    })
                elif diff <= 0 and rec[1] != pname:
                    yesterday_total = career_total - game_val
                    if yesterday_total < int(rec[0]):
                        events.append({
                            "type": "record_crossing",
                            "player": pname, "team": team_name,
                            "detail": f"{pname} passed {rec[1]} for the {team_name} career {label} record ({int(career_total)} vs {int(rec[0])})",
                        })

        # ===== SEASON RECORD APPROACHES (only later in season) =====
        # Only check when player has 50+ games (avoid early-season noise)
        season_games = conn.execute("""
            SELECT COUNT(*) FROM game_batting_logs
            WHERE player_id = ? AND season = ? AND date <= ?
        """, (pid, season, target_date)).fetchone()
        if season_games and season_games[0] and season_games[0] >= 50:
            for stat_col, game_col, label, _ in BAT_COUNTING:
                game_val = bat.get(game_col, 0) if game_col in bat else bat.get(stat_col, 0)
                if game_val <= 0:
                    continue
                season_total = conn.execute(f"""
                    SELECT COALESCE(SUM({stat_col}), 0) FROM game_batting_logs
                    WHERE player_id = ? AND season = ? AND date <= ?
                """, (pid, season, target_date)).fetchone()
                if not season_total:
                    continue
                sv = season_total[0]
                fc_placeholders_s = ",".join("?" * len(franchise_codes))
                rec = conn.execute(f"""
                    SELECT value, player_name, season FROM team_records
                    WHERE team_code IN ({fc_placeholders_s}) AND stat = ? AND record_type = 'season'
                    ORDER BY value DESC LIMIT 1
                """, franchise_codes + [stat_col]).fetchone()
                if rec and sv:
                    diff = int(rec[0]) - int(sv)
                    if 1 <= diff <= 3:
                        events.append({
                            "type": "season_record_approach",
                            "player": pname, "team": team_name,
                            "detail": f"{pname} has {int(sv)} {label} — {diff} from {team_name} single-season record ({rec[1]}, {rec[2]}: {int(rec[0])})",
                        })

        # ===== SEASON MILESTONE THRESHOLDS =====
        season_bat = conn.execute("""
            SELECT SUM(home_runs), SUM(stolen_bases), SUM(rbi), SUM(hits), SUM(runs)
            FROM game_batting_logs
            WHERE player_id = ? AND season = ? AND date <= ?
        """, (pid, season, target_date)).fetchone()
        if season_bat and season_bat[0] is not None:
            hr = season_bat[0] or 0
            sb = season_bat[1] or 0
            rbi_total = season_bat[2] or 0
            hits_total = season_bat[3] or 0
            runs_total = season_bat[4] or 0

            def _career_threshold_count(stat_col, threshold, table="season_batting_stats"):
                """How many career seasons has this player reached this threshold?"""
                return conn.execute(f"""
                    SELECT COUNT(*) FROM {table}
                    WHERE player_id = ? AND {stat_col} >= ? AND season < ?
                """, (pid, threshold, season)).fetchone()[0]

            def _ordinal(n):
                if n == 1: return "first"
                if n == 2: return "2nd"
                if n == 3: return "3rd"
                return f"{n}th"

            def _emit_season_milestone(stat_col, season_total, today_val, thresholds,
                                       approach_proximity, action_verb, label_short,
                                       label_full, table="season_batting_stats"):
                """Generic crosses-or-approaches check for a season counting stat."""
                if today_val <= 0:
                    return
                for threshold in sorted(thresholds, reverse=True):
                    diff = threshold - season_total
                    if diff == 0:
                        yesterday = season_total - today_val
                        if yesterday < threshold:
                            prior = _career_threshold_count(stat_col, threshold, table)
                            ctx = (f"the first {threshold}-{label_short} season of his career"
                                   if prior == 0
                                   else f"the {_ordinal(prior + 1)} {threshold}-{label_short} season of his career")
                            events.append({
                                "type": "milestone_crossing",
                                "player": pname, "team": team_name,
                                "detail": f"{pname} {action_verb} his {threshold}th {label_full} of the season — {ctx}.",
                            })
                            return
                    elif threshold in approach_proximity:
                        proximity = approach_proximity[threshold]
                        if 1 <= diff <= proximity:
                            events.append({
                                "type": "milestone_approach",
                                "player": pname, "team": team_name,
                                "detail": f"{pname} has {season_total} {label_short} — {diff} away from {threshold} this season.",
                            })
                            return

            # HR milestones: cross at 20/30/40/50/60/70/80, approach only 50+ (within 3) and 40 (within 2)
            _hr_approach_proximity = {40: 2, 50: 3, 60: 3, 70: 3, 80: 3}
            if bat.get("home_runs", 0) > 0:
                for threshold in [80, 70, 60, 50, 40, 30, 20]:
                    diff = threshold - hr
                    if diff == 0:
                        hr_yesterday = hr - bat.get("home_runs", 0)
                        if hr_yesterday < threshold:
                            prior_times = _career_threshold_count("home_runs", threshold)
                            if prior_times == 0:
                                context = f"the first {threshold}-HR season of his career"
                            else:
                                context = f"the {_ordinal(prior_times + 1)} {threshold}-HR season of his career"
                            # For iconic thresholds (50+), add "last since X" anchor
                            extra = _season_milestone_last_since(
                                conn, "home_runs", threshold, season, "HR", pid
                            ) if threshold >= 50 else ""
                            events.append({
                                "type": "milestone_crossing",
                                "player": pname, "team": team_name,
                                "detail": f"{pname} hit his {threshold}th home run of the season — {context}{extra}.",
                            })
                            break
                    elif threshold in _hr_approach_proximity:
                        proximity = _hr_approach_proximity[threshold]
                        if 1 <= diff <= proximity:
                            events.append({
                                "type": "milestone_approach",
                                "player": pname, "team": team_name,
                                "detail": f"{pname} has {hr} HR — {diff} away from {threshold} this season.",
                            })
                            break

            # SB milestones: cross at 20/30/40/50/60/70/80, approach only 50+ (within 3) and 40 (within 2)
            _sb_approach_proximity = {40: 2, 50: 3, 60: 3, 70: 3, 80: 3}
            if bat.get("stolen_bases", 0) > 0:
                for threshold in [80, 70, 60, 50, 40, 30, 20]:
                    diff = threshold - sb
                    if diff == 0:
                        sb_yesterday = sb - bat.get("stolen_bases", 0)
                        if sb_yesterday < threshold:
                            prior_times = _career_threshold_count("stolen_bases", threshold)
                            if prior_times == 0:
                                context = f"the first {threshold}-steal season of his career"
                            else:
                                context = f"the {_ordinal(prior_times + 1)} {threshold}-steal season of his career"
                            extra = _season_milestone_last_since(
                                conn, "stolen_bases", threshold, season, "SB", pid
                            ) if threshold >= 50 else ""
                            events.append({
                                "type": "milestone_crossing",
                                "player": pname, "team": team_name,
                                "detail": f"{pname} stole his {threshold}th base of the season — {context}{extra}.",
                            })
                            break
                    elif threshold in _sb_approach_proximity:
                        proximity = _sb_approach_proximity[threshold]
                        if 1 <= diff <= proximity:
                            events.append({
                                "type": "milestone_approach",
                                "player": pname, "team": team_name,
                                "detail": f"{pname} has {sb} SB — {diff} away from {threshold} this season.",
                            })
                            break

            # RBI season milestones: 100, 125, 150
            _emit_season_milestone(
                "rbi", rbi_total, bat.get("rbi", 0),
                thresholds=[150, 125, 100],
                approach_proximity={100: 3, 150: 3},
                action_verb="drove in", label_short="RBI", label_full="RBI",
            )

            # Hits season milestones: 150, 175, 200
            _emit_season_milestone(
                "hits", hits_total, bat.get("hits", 0),
                thresholds=[200, 175, 150],
                approach_proximity={200: 3, 175: 3},
                action_verb="collected", label_short="hits", label_full="hit",
            )

            # Runs scored season milestones: 100, 125
            _emit_season_milestone(
                "runs", runs_total, bat.get("runs", 0),
                thresholds=[125, 100],
                approach_proximity={100: 3},
                action_verb="scored", label_short="runs", label_full="run",
            )

            # X/X combos (20/20, 30/30, 40/40) — check if today's game contributed
            if bat.get("home_runs", 0) > 0 or bat.get("stolen_bases", 0) > 0:
                for threshold in [40, 30, 20]:
                    if hr >= threshold and sb >= threshold:
                        hr_yesterday = hr - bat.get("home_runs", 0)
                        sb_yesterday = sb - bat.get("stolen_bases", 0)
                        if hr_yesterday < threshold or sb_yesterday < threshold:
                            events.append({
                                "type": "milestone_crossing",
                                "player": pname, "team": team_name,
                                "detail": f"{pname} joined the {threshold}/{threshold} club ({hr} HR, {sb} SB)!",
                            })
                            break
                    elif hr >= threshold - 3 and sb >= threshold and bat.get("home_runs", 0) > 0:
                        events.append({
                            "type": "milestone_approach",
                            "player": pname, "team": team_name,
                            "detail": f"{pname} has {hr} HR and {sb} SB — {threshold - hr} HR from {threshold}/{threshold}.",
                        })
                        break
                    elif sb >= threshold - 3 and hr >= threshold and bat.get("stolen_bases", 0) > 0:
                        events.append({
                            "type": "milestone_approach",
                            "player": pname, "team": team_name,
                            "detail": f"{pname} has {hr} HR and {sb} SB — {threshold - sb} SB from {threshold}/{threshold}.",
                        })
                        break

            # ===== PITCHING SEASON MILESTONES =====
            season_pitch = conn.execute("""
                SELECT SUM(win), SUM(strikeouts), SUM(save)
                FROM game_pitching_logs
                WHERE player_id = ? AND season = ? AND date <= ?
            """, (pid, season, target_date)).fetchone()
            if season_pitch and season_pitch[0] is not None:
                wins_total = season_pitch[0] or 0
                k_total = season_pitch[1] or 0
                saves_total = season_pitch[2] or 0

                # Wins season milestones: 10, 15, 20
                _emit_season_milestone(
                    "wins", wins_total, pitch.get("win", 0),
                    thresholds=[20, 15, 10],
                    approach_proximity={20: 2},
                    action_verb="picked up", label_short="wins", label_full="win",
                    table="season_pitching_stats",
                )

                # Strikeouts season milestones: 200, 250, 300
                _emit_season_milestone(
                    "strikeouts", k_total, pitch.get("strikeouts", 0),
                    thresholds=[300, 250, 200],
                    approach_proximity={200: 5, 250: 5, 300: 5},
                    action_verb="recorded", label_short="K", label_full="strikeout",
                    table="season_pitching_stats",
                )

                # Saves season milestones: 20, 30, 40
                _emit_season_milestone(
                    "saves", saves_total, pitch.get("save", 0),
                    thresholds=[40, 30, 20],
                    approach_proximity={40: 2},
                    action_verb="notched", label_short="saves", label_full="save",
                    table="season_pitching_stats",
                )

    # No dedup for now — show both career_high and first_threshold when they overlap
    # so we can evaluate the distinction before deciding how to merge them

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
  .badge.record-approach {{ background: #fef3c7; color: #92400e; }}
  .badge.season-record-approach {{ background: #fff7ed; color: #9a3412; }}
  .badge.crossing {{ background: #dcfce7; color: #166534; }}
  .badge.record-crossing {{ background: #dcfce7; color: #166534; }}
  .badge.milestone-approach {{ background: #fef3c7; color: #92400e; }}
  .badge.milestone-crossing {{ background: #dcfce7; color: #166534; }}
  .badge.career-first {{ background: #dbeafe; color: #1A40B3; }}
  .badge.deep-scan {{ background: #fef3c7; color: #78350f; }}
  .event-card.deep-scan {{ border-left-color: #f59e0b; background: #fffbeb; }}
  .badge.error {{ background: #fee2e2; color: #991b1b; }}
  .event-card.error {{ border-left-color: #ef4444; background: #fef2f2; }}
  .badge.career-high {{ background: #f3e8ff; color: #6b21a8; }}
  .badge.holds {{ background: #fce7f3; color: #9d174d; }}
  .event-card {{
    padding: 8px 12px; border-left: 3px solid #1A40B3; margin-bottom: 6px;
    background: #f8f9ff; border-radius: 0 6px 6px 0; font-size: 13px;
  }}
  .event-card.crossing {{ border-left-color: #16a34a; background: #f0fdf4; }}
  .event-card.record-crossing {{ border-left-color: #16a34a; background: #f0fdf4; }}
  .event-card.milestone-crossing {{ border-left-color: #16a34a; background: #f0fdf4; }}
  .event-card.record-approach {{ border-left-color: #d97706; background: #fffbeb; }}
  .event-card.season-record-approach {{ border-left-color: #ea580c; background: #fff7ed; }}
  .event-card.milestone-approach {{ border-left-color: #d97706; background: #fffbeb; }}
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
<h1>Records &amp; Deep Scans Sandbox</h1>

<div class="stat-cards">
  {status_html}
</div>

<details style="margin-bottom:16px;font-size:12px;color:#555">
  <summary style="cursor:pointer;font-weight:600;color:#1A40B3">Detection Rules &amp; Thresholds</summary>
  <div style="margin-top:8px;padding:12px;background:#f5f7fa;border-radius:8px;line-height:1.6">
    <b>Records</b><br>
    &bull; Career firsts: HR (always), Win (after week 3)<br>
    &bull; Career highs: Hits 4+, HR 3+, RBI 5+, SB 3+, K 10+ (pitching)<br>
    &bull; Team record approaches: within 3 (franchise-scoped)<br>
    &bull; Season milestones: HR 20/30/40/50/60/70/80, SB 20/30/40/50/60/70/80<br>
    &bull; Approaching: 40 within 2, 50+ within 3<br>
    &bull; X/X combos: 20/20, 30/30, 40/40<br>
    &bull; All require contribution to the stat on that date<br>
    <br>
    <b>Deep Scans — OPS (season)</b><br>
    &bull; Gate: Dynamic — OPS &ge; 1.200 (&le;15g), &ge; 1.100 (&le;30g), &ge; 1.000 (&le;50g), &ge; .950 (50+g). Games &ge; 10.<br>
    &bull; Checks: last player with OPS this high through same game count (per-year scan)<br>
    &bull; Interesting if: 5+ years MLB, 3+ years team<br>
    <br>
    <b>Deep Scans — OPS (PELT streak)</b><br>
    &bull; Gate: Active hot streak &ge; 7 games, OPS &ge; 1.000 during streak<br>
    &bull; Checks: last player with a streak OPS this high<br>
    <br>
    <b>Deep Scans — HR Accumulation</b><br>
    &bull; Gate: HR &ge; 8 through &le; 30 games, OR HR &ge; 15 through &le; 50 games<br>
    <br>
    <b>Deep Scans — SB Accumulation</b><br>
    &bull; Gate: SB &ge; 10 through &le; 30 games, OR SB &ge; 20 through &le; 50 games<br>
    <br>
    <b>Deep Scans — Power-Speed</b><br>
    &bull; Gate: HR &ge; 5 AND SB &ge; 5 through &le; 25 games<br>
    <br>
    <b>Deep Scans — Pitching Dominance</b><br>
    &bull; Gate: Starts &ge; 3, ERA &le; 1.50, K/start &ge; 8<br>
    <br>
    <b>Cooldown</b>: 5 games after firing before re-check (not yet enforced in sandbox)
  </div>
</details>

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
  <button class="secondary" onclick="simulateAll()">Simulate All Dates</button>
</div>
<div id="sim-results" class="results"></div>

<h2>Deep Scans — Browse by Date</h2>
<div class="search-row">
  <input type="date" id="deep-date" value="2025-04-10">
  <button onclick="scanDeepDate()">Scan Date</button>
  <button class="secondary" id="prev-deep" onclick="navDeepDate(-1)">&larr; Prev Day</button>
  <button class="secondary" id="next-deep" onclick="navDeepDate(1)">Next Day &rarr;</button>
  <span id="deep-info" style="font-size:12px;color:#888"></span>
</div>
<div id="season-results" class="results"></div>

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
    el.innerHTML = renderDateEvents(dt, data.events || []);
  }} catch (e) {{
    el.innerHTML = '<p class="empty">Error: ' + e.message + '</p>';
  }}
}}

async function simulateAll() {{
  const el = document.getElementById('sim-results');
  el.innerHTML = '<p class="loading">Simulating all dates from March 27 (with cooldown tracking)...</p>';
  try {{
    const data = await apiFetch('/admin/records-simulate-all');
    const results = data.results || [];
    let allHtml = '';
    results.forEach(r => {{
      allHtml += renderDateEvents(r.date, r.events);
    }});
    el.innerHTML = '<h3>' + data.total_events + ' total events across ' + results.length + ' game dates</h3>' + allHtml;
  }} catch (e) {{
    el.innerHTML = '<p class="empty">Error: ' + e.message + '</p>';
  }}
}}

function renderDateEvents(dt, events) {{
  if (!events || events.length === 0) return '';
  const dayName = new Date(dt + 'T12:00:00').toLocaleDateString('en-US', {{ weekday: 'short', month: 'short', day: 'numeric' }});
  let html = '<h3 style="margin-top:16px;border-bottom:1px solid #e0e8f5;padding-bottom:4px">' + dayName + ' — ' + events.length + ' event' + (events.length !== 1 ? 's' : '') + '</h3>';
  events.forEach(e => {{
    const cls = e.type.replace(/_/g, '-');
    const badge = '<span class="badge ' + cls + ' event-badge">' + e.type.replace(/_/g, ' ') + '</span>';
    html += '<div class="event-card ' + cls + '">' + badge + esc(e.detail) + '</div>';
  }});
  return html;
}}

let currentDeepDate = '2025-04-10';

async function scanDeepDate(dt) {{
  dt = dt || document.getElementById('deep-date').value;
  if (!dt) return;
  currentDeepDate = dt;
  document.getElementById('deep-date').value = dt;
  const el = document.getElementById('season-results');
  const info = document.getElementById('deep-info');
  el.innerHTML = '<p class="loading">Scanning ' + dt + '...</p>';
  try {{
    const data = await apiFetch('/admin/records-simulate?date_str=' + dt);
    if (!data.events || data.events.length === 0) {{
      el.innerHTML = '<p class="empty">No events for ' + dt + '.</p>';
      info.textContent = dt;
      return;
    }}
    el.innerHTML = renderDateEvents(dt, data.events);
    info.textContent = data.events.length + ' events';
  }} catch (e) {{
    el.innerHTML = '<p class="empty">Error: ' + e.message + '</p>';
  }}
}}

function navDeepDate(direction) {{
  const d = new Date(currentDeepDate + 'T12:00:00');
  d.setDate(d.getDate() + direction);
  const newDate = d.toISOString().slice(0, 10);
  scanDeepDate(newDate);
}}

function esc(s) {{ return (s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }}
function fmt(n) {{ return n != null ? Number(n).toLocaleString() : '--'; }}
</script>
</body>
</html>"""

    return HTMLResponse(content=html)


@router.get("/on-this-date-sandbox", response_class=HTMLResponse)
async def on_this_date_sandbox(
    key: str | None = None,
    authorization: str | None = Header(None),
):
    """Show every On This Date event that would fire on each calendar day
    of the current year. One long scrollable list grouped by month so we
    can scan the whole feed and decide where to tighten/loosen rules.
    """
    verify_admin(authorization, key)

    from services.notable_events import detect_on_this_date
    from datetime import date as _date, timedelta as _timedelta

    conn = sqlite3.connect(DB_PATH, timeout=30)
    season = _date.today().year

    by_month = {m: [] for m in range(1, 13)}
    total_events = 0
    days_with_events = 0
    try:
        d = _date(season, 1, 1)
        end = _date(season, 12, 31)
        while d <= end:
            iso = d.isoformat()
            events = detect_on_this_date(conn, season, iso, target_date=iso)
            if events:
                days_with_events += 1
                total_events += len(events)
                by_month[d.month].append({
                    "date": iso,
                    "month_day": d.strftime("%B %-d"),
                    "events": events,
                })
            d += _timedelta(days=1)
    finally:
        conn.close()

    months = ["January", "February", "March", "April", "May", "June",
              "July", "August", "September", "October", "November", "December"]

    sections = []
    for m in range(1, 13):
        days = by_month[m]
        if not days:
            sections.append(f'<h2 id="m{m}">{months[m-1]}</h2>'
                            f'<p style="color:#999;">No events.</p>')
            continue
        rows = []
        for entry in days:
            event_lines = "".join(
                f'<li>{e["headline"]}</li>' for e in entry["events"])
            n = len(entry['events'])
            rows.append(
                f'<div class="day"><div class="day-header">'
                f'{entry["month_day"]} <span class="count">'
                f'({n} event{"s" if n != 1 else ""})</span></div>'
                f'<ul>{event_lines}</ul></div>')
        sections.append(f'<h2 id="m{m}">{months[m-1]}</h2>' + "\n".join(rows))

    nav_links = " ".join(f'<a href="#m{m}">{months[m-1][:3]}</a>'
                        for m in range(1, 13))

    total_days = (_date(season, 12, 31) - _date(season, 1, 1)).days + 1

    html = f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<title>On This Date Sandbox</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         max-width: 1100px; margin: 24px auto; padding: 0 20px; color: #333; }}
  h1 {{ color: #1A40B3; }}
  h2 {{ margin-top: 36px; padding-bottom: 6px; border-bottom: 2px solid #1A40B3; color: #1A40B3; }}
  .summary {{ background: linear-gradient(135deg, #1A40B3, #73B3FF); color: white;
              padding: 16px 20px; border-radius: 8px; margin-bottom: 20px; }}
  .day {{ margin: 14px 0; }}
  .day-header {{ font-weight: 600; color: #555; margin-bottom: 4px; }}
  .count {{ color: #999; font-weight: normal; font-size: 0.9em; }}
  ul {{ margin: 0; padding-left: 20px; }}
  li {{ margin: 4px 0; line-height: 1.4; }}
  .nav {{ position: sticky; top: 0; background: white; padding: 8px 0; border-bottom: 1px solid #eee; margin-bottom: 16px; z-index: 10; }}
  .nav a {{ margin-right: 12px; color: #1A40B3; text-decoration: none; font-size: 0.9em; }}
</style>
</head><body>
<h1>On This Date — Annual Sandbox</h1>
<div class="summary">
  <div><strong>{total_events}</strong> total events would fire across <strong>{days_with_events}</strong> of {total_days} days in {season}.</div>
  <div style="margin-top:6px;font-size:0.92em;opacity:0.9;">No-hitters, 4+ HR games, 18+ K games, 10+ RBI games, iconic career-threshold crossings (auto-ranked), and hand-curated manual moments. No artificial caps.</div>
</div>
<div class="nav">{nav_links}</div>
{''.join(sections)}
</body></html>"""
    return HTMLResponse(content=html)
