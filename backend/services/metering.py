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
    """Create the quota table if it doesn't exist. Called at app startup."""
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
    conn.commit()
    conn.close()


def check_quota(device_id: str) -> dict:
    """
    Returns {"allowed": bool, "count": int, "reset": "YYYY-MM-DD"}.
    "reset" is the next Monday when the counter resets.
    """
    week_start = _week_start()
    reset = _next_monday()

    conn = sqlite3.connect(METERING_DB_PATH)
    row = conn.execute(
        "SELECT week_start, query_count, is_paid, paid_expires_at "
        "FROM device_quota WHERE device_id = ?",
        (device_id,),
    ).fetchone()
    conn.close()

    if row is None:
        return {"allowed": True, "count": 0, "reset": reset}

    stored_week, count, is_paid, paid_expires = row

    # Paid subscriber — unlimited unless expired
    if is_paid:
        if paid_expires is None or paid_expires >= date.today().isoformat():
            return {"allowed": True, "count": count, "reset": reset}

    # New week → treat count as 0
    if stored_week != week_start:
        count = 0

    return {
        "allowed": count < FREE_QUERIES_PER_WEEK,
        "count": count,
        "reset": reset,
    }


def increment_count(device_id: str) -> None:
    """Increment the query count for this device, resetting if it's a new week."""
    week_start = _week_start()
    conn = sqlite3.connect(METERING_DB_PATH)

    row = conn.execute(
        "SELECT week_start, query_count FROM device_quota WHERE device_id = ?",
        (device_id,),
    ).fetchone()

    if row is None:
        conn.execute(
            "INSERT INTO device_quota (device_id, week_start, query_count) VALUES (?, ?, 1)",
            (device_id, week_start),
        )
    else:
        stored_week, count = row
        new_count = 1 if stored_week != week_start else count + 1
        conn.execute(
            "UPDATE device_quota SET week_start = ?, query_count = ? WHERE device_id = ?",
            (week_start, new_count, device_id),
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
