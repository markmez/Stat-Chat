"""Signal Weather anonymous usage pings + admin usage page.

No identifiers by design: the watch app dedups client-side with
calendar-period gates (at most one ping per day / week / month, plus one
install ping ever), so counting rows per period IS exact DAU/WAU/MAU —
the dedup happened on-device, not via a device ID. Payload is period +
app_version only. This keeps Signal Weather's App Store privacy label at
"Usage Data (Product Interaction), not linked to identity" with no
Identifiers declaration. Debug and simulator builds never ping.
"""
import sqlite3

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from services.metering import METERING_DB_PATH
from routers.admin import verify_admin

router = APIRouter()

ALLOWED_PERIODS = {"day", "week", "month", "install"}


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(METERING_DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS sw_pings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL DEFAULT (datetime('now')),
            period TEXT NOT NULL,
            app_version TEXT
        )"""
    )
    return conn


class PingPayload(BaseModel):
    period: str
    app_version: str | None = Field(default=None, max_length=32)


@router.post("/signalweather/ping")
async def signalweather_ping(payload: PingPayload):
    if payload.period not in ALLOWED_PERIODS:
        raise HTTPException(400, "unknown period")
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO sw_pings (period, app_version) VALUES (?, ?)",
            (payload.period, payload.app_version),
        )
        conn.commit()
    finally:
        conn.close()
    return {"status": "ok"}


@router.get("/admin/signalweather", response_class=HTMLResponse)
async def signalweather_dashboard(
    authorization: str | None = Header(default=None),
    key: str | None = None,
):
    verify_admin(authorization, key)
    conn = _connect()
    try:
        installs = conn.execute(
            "SELECT COUNT(*) FROM sw_pings WHERE period='install'"
        ).fetchone()[0]
        dau = conn.execute(
            """SELECT date(ts) d, COUNT(*) FROM sw_pings WHERE period='day'
               GROUP BY d ORDER BY d DESC LIMIT 14"""
        ).fetchall()
        wau = conn.execute(
            """SELECT strftime('%Y-W%W', ts) w, COUNT(*) FROM sw_pings WHERE period='week'
               GROUP BY w ORDER BY w DESC LIMIT 8"""
        ).fetchall()
        mau = conn.execute(
            """SELECT strftime('%Y-%m', ts) m, COUNT(*) FROM sw_pings WHERE period='month'
               GROUP BY m ORDER BY m DESC LIMIT 6"""
        ).fetchall()
        versions = conn.execute(
            """SELECT COALESCE(app_version,'?') v, COUNT(*) FROM sw_pings
               WHERE period='day' AND ts >= datetime('now','-30 days')
               GROUP BY v ORDER BY 2 DESC"""
        ).fetchall()
    finally:
        conn.close()

    def table(title, rows, cols):
        body = "".join(f"<tr><td>{a}</td><td>{b}</td></tr>" for a, b in rows)
        return (
            f"<h2>{title}</h2><table border=0 cellpadding=6 "
            f"style='border-collapse:collapse;background:#1c1c1e;border-radius:8px'>"
            f"<tr><th align=left>{cols[0]}</th><th align=left>{cols[1]}</th></tr>{body}</table>"
        )

    html = (
        "<html><head><title>Signal Weather Usage</title></head>"
        "<body style='font-family:-apple-system,sans-serif;background:#000;color:#eee;padding:24px'>"
        "<h1>Signal Weather</h1>"
        f"<p style='font-size:20px'>Installs (all time): <b>{installs}</b></p>"
        + table("DAU — last 14 days", dau, ("Day", "Users"))
        + table("WAU — last 8 weeks", wau, ("Week", "Users"))
        + table("MAU — last 6 months", mau, ("Month", "Users"))
        + table("App versions (daily pings, 30d)", versions, ("Version", "Pings"))
        + "<p style='color:#888'>Counts are exact uniques — the watch dedups "
        "client-side per calendar period; no device IDs exist.</p>"
        "</body></html>"
    )
    return HTMLResponse(html)
