"""
Device-level query quota tracking.

Free tier: FREE_QUERIES_PER_WEEK (default 5) per device per week.
Week resets every Monday. Paid devices bypass the limit.

Uses a small SQLite DB (metering.db) separate from the stats DB.
"""

import os
import sqlite3
from datetime import date, timedelta

METERING_DB_PATH = os.getenv(
    "METERING_DB_PATH",
    os.path.join(os.path.dirname(__file__), "..", "metering.db"),
)
FREE_QUERIES_PER_WEEK = int(os.getenv("FREE_QUERIES_PER_WEEK", "5"))


def init_metering_db() -> None:
    """Create the quota, query_log, event_archive, and event_taps tables if they don't exist."""
    conn = sqlite3.connect(METERING_DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS device_quota (
            device_id       TEXT PRIMARY KEY,
            week_start      TEXT NOT NULL,
            query_count     INTEGER NOT NULL DEFAULT 0,
            is_paid         INTEGER NOT NULL DEFAULT 0,
            paid_expires_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS query_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            query_text      TEXT NOT NULL,
            device_id       TEXT,
            response_type   TEXT NOT NULL,
            timestamp       TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS event_archive (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            headline        TEXT NOT NULL,
            detection_type  TEXT NOT NULL,
            game_date       TEXT NOT NULL,
            archived_at     TEXT NOT NULL,
            UNIQUE(headline, game_date)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS event_taps (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            headline        TEXT NOT NULL,
            tap_type        TEXT NOT NULL,
            device_id       TEXT,
            timestamp       TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def log_query(query_text: str, device_id: str, response_type: str,
              is_followup: bool = False, original_query: str = None) -> None:
    """Log a query with its response type. response_type: 'intercepted', 'haiku', 'sonnet'."""
    conn = sqlite3.connect(METERING_DB_PATH)
    # Add followup columns if they don't exist yet
    try:
        conn.execute("ALTER TABLE query_log ADD COLUMN is_followup INTEGER DEFAULT 0")
    except Exception:
        pass  # Column already exists
    try:
        conn.execute("ALTER TABLE query_log ADD COLUMN original_query TEXT")
    except Exception:
        pass
    conn.execute(
        "INSERT INTO query_log (query_text, device_id, response_type, timestamp, is_followup, original_query) VALUES (?, ?, ?, ?, ?, ?)",
        (query_text, device_id, response_type, _now_iso(), 1 if is_followup else 0, original_query),
    )
    conn.commit()
    conn.close()


def archive_events(events: list[dict]) -> int:
    """Archive notable events. Each dict needs headline, detection_type, game_date.
    Returns count of newly archived events."""
    conn = sqlite3.connect(METERING_DB_PATH)
    ts = _now_iso()
    count = 0
    for e in events:
        try:
            conn.execute(
                "INSERT OR IGNORE INTO event_archive (headline, detection_type, game_date, archived_at) VALUES (?, ?, ?, ?)",
                (e.get("headline", ""), e.get("detection_type", ""), e.get("game_date", ""), ts),
            )
            count += conn.total_changes  # approximate
        except Exception:
            pass
    conn.commit()
    conn.close()
    return count


def log_event_tap(headline: str, tap_type: str, device_id: str) -> None:
    """Log a user tapping on a feed event link."""
    conn = sqlite3.connect(METERING_DB_PATH)
    conn.execute(
        "INSERT INTO event_taps (headline, tap_type, device_id, timestamp) VALUES (?, ?, ?, ?)",
        (headline, tap_type, device_id, _now_iso()),
    )
    conn.commit()
    conn.close()


_SERVER_ERROR_HC_UUID = os.getenv("SERVER_ERROR_HC_UUID", "")


def log_server_error(source: str, error_type: str, error_message: str,
                     context: dict | None = None,
                     device_id: str | None = None) -> None:
    """Log a server-side error to query_log so it surfaces on /admin/dashboard.

    Also pings Healthchecks.io /fail (if SERVER_ERROR_HC_UUID env var set) so
    real alerts go out-of-band — otherwise we're relying on manually checking
    the dashboard.

    Unlike log_client_event, no dedup and no whitelist — every server error
    captured verbatim. Used to replace silently-swallowed exceptions (e.g. the
    knowledge-mode fallback at routers/query.py) so we can diagnose from here
    without shelling into the Lightsail host.
    """
    import json

    conn = sqlite3.connect(METERING_DB_PATH)
    # Add client_context column if it doesn't exist yet (shared with client events)
    try:
        conn.execute("ALTER TABLE query_log ADD COLUMN client_context TEXT")
    except Exception:
        pass

    query_text = f"[server] {source}: {error_type}"
    full_context: dict = {"source": source, "error_type": error_type,
                          "error_message": (error_message or "")[:2000]}
    if isinstance(context, dict):
        full_context.update(context)

    conn.execute(
        "INSERT INTO query_log (query_text, device_id, response_type, timestamp, client_context) "
        "VALUES (?, ?, 'server_error', ?, ?)",
        (query_text, device_id or "server", _now_iso(), json.dumps(full_context)),
    )
    conn.commit()
    conn.close()

    # Fire Healthchecks.io /fail ping so we get an actual alert, not just a
    # dashboard row that only surfaces when someone looks. Swallow network
    # errors — alerting is best-effort and must not cascade into a new error.
    if _SERVER_ERROR_HC_UUID:
        try:
            import requests
            url = f"https://hc-ping.com/{_SERVER_ERROR_HC_UUID}/fail"
            body = f"source: {source}\nerror_type: {error_type}\nmessage: {(error_message or '')[:500]}"
            requests.post(url, data=body, timeout=3)
        except Exception:
            pass


def log_client_event(event_type: str, context: dict, device_id: str,
                     app_version: str | None = None,
                     platform_version: str | None = None) -> bool:
    """Log a client-side event (partial response, decode error, etc.) to query_log.

    Writes with response_type='client_event'. Dedupes within the current minute for
    (device_id, query_text) to prevent flooding. Returns True if inserted, False if deduped.
    """
    import json

    conn = sqlite3.connect(METERING_DB_PATH)
    # Add client_context column if it doesn't exist (same ALTER pattern as is_followup)
    try:
        conn.execute("ALTER TABLE query_log ADD COLUMN client_context TEXT")
    except Exception:
        pass

    # Human-readable headline that groups similar events in the dashboard
    player = None
    if isinstance(context, dict):
        player = context.get("player")
    query_text = f"[client] {event_type}"
    if player:
        query_text = f"{query_text}: {player}"

    now = _now_iso()
    minute_prefix = now[:16]  # "2026-04-16T13:42"
    existing = conn.execute(
        "SELECT 1 FROM query_log WHERE device_id = ? AND response_type = 'client_event' "
        "AND query_text = ? AND timestamp LIKE ? LIMIT 1",
        (device_id, query_text, minute_prefix + "%"),
    ).fetchone()
    if existing:
        conn.close()
        return False

    full_context: dict = dict(context) if isinstance(context, dict) else {"raw": context}
    if app_version:
        full_context["app_version"] = app_version
    if platform_version:
        full_context["platform_version"] = platform_version

    conn.execute(
        "INSERT INTO query_log (query_text, device_id, response_type, timestamp, client_context) "
        "VALUES (?, ?, 'client_event', ?, ?)",
        (query_text, device_id, now, json.dumps(full_context)),
    )
    conn.commit()
    conn.close()
    return True


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def check_quota(device_id: str) -> dict:
    """
    Returns {"allowed": bool, "count": int, "reset": "YYYY-MM-DD"}.
    "reset" is 7 days from when the device's current cycle started.
    """
    conn = sqlite3.connect(METERING_DB_PATH)
    row = conn.execute(
        "SELECT week_start, query_count, is_paid, paid_expires_at "
        "FROM device_quota WHERE device_id = ?",
        (device_id,),
    ).fetchone()
    conn.close()

    today = date.today()

    if row is None:
        reset = (today + timedelta(days=7)).isoformat()
        return {"allowed": True, "count": 0, "reset": reset}

    stored_week, count, is_paid, paid_expires = row

    # Paid subscriber — unlimited unless expired
    if is_paid:
        if paid_expires is None or paid_expires >= today.isoformat():
            reset = (today + timedelta(days=7)).isoformat()
            return {"allowed": True, "count": count, "reset": reset}

    # Check if 7 days have passed since cycle start → reset
    cycle_start = date.fromisoformat(stored_week)
    if (today - cycle_start).days >= 7:
        count = 0

    reset = (cycle_start + timedelta(days=7)).isoformat()

    return {
        "allowed": count < FREE_QUERIES_PER_WEEK,
        "count": count,
        "reset": reset,
    }


def increment_count(device_id: str) -> None:
    """Increment the query count for this device, resetting if 7 days have passed."""
    today = date.today().isoformat()
    conn = sqlite3.connect(METERING_DB_PATH)

    row = conn.execute(
        "SELECT week_start, query_count FROM device_quota WHERE device_id = ?",
        (device_id,),
    ).fetchone()

    if row is None:
        conn.execute(
            "INSERT INTO device_quota (device_id, week_start, query_count) VALUES (?, ?, 1)",
            (device_id, today),
        )
    else:
        stored_week, count = row
        cycle_start = date.fromisoformat(stored_week)
        if (date.today() - cycle_start).days >= 7:
            # New cycle
            conn.execute(
                "UPDATE device_quota SET week_start = ?, query_count = 1 WHERE device_id = ?",
                (today, device_id),
            )
        else:
            conn.execute(
                "UPDATE device_quota SET query_count = ? WHERE device_id = ?",
                (count + 1, device_id),
            )

    conn.commit()
    conn.close()


def mark_paid(device_id: str, expires_at: str) -> None:
    """
    Mark a device as a paid subscriber through the given expiry date.
    expires_at: ISO date string "YYYY-MM-DD"
    """
    week_start = _week_start()
    conn = sqlite3.connect(METERING_DB_PATH)
    conn.execute(
        """
        INSERT INTO device_quota (device_id, week_start, query_count, is_paid, paid_expires_at)
        VALUES (?, ?, 0, 1, ?)
        ON CONFLICT(device_id) DO UPDATE SET is_paid = 1, paid_expires_at = ?
        """,
        (device_id, week_start, expires_at, expires_at),
    )
    conn.commit()
    conn.close()


def _week_start(d: date = None) -> str:
    """ISO date string of the Monday that starts the current week."""
    d = d or date.today()
    monday = d - timedelta(days=d.weekday())
    return monday.isoformat()


def _next_monday() -> str:
    """ISO date string of the next Monday (when the quota resets)."""
    today = date.today()
    days_ahead = (7 - today.weekday()) % 7 or 7
    return (today + timedelta(days=days_ahead)).isoformat()
