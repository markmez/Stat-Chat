"""Notable events feed endpoint."""

import json
import os
import sqlite3
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Query as QueryParam
from pydantic import BaseModel

router = APIRouter()

DB_PATH = os.getenv("DB_PATH", "/data/baseball_stats_full.db")


@router.get("/notable-events")
async def get_notable_events(limit: int = QueryParam(50, le=200)):
    """Return recent notable baseball events, ordered by date and priority.
    Filters out expired matchup previews (past game start time).
    Matchup preview interleaving is handled client-side based on user engagement."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    now_utc = datetime.now(timezone.utc).isoformat()

    # Matchup preview display gate: weekdays noon ET+, weekends 9 AM ET+
    et_now = datetime.now(timezone(timedelta(hours=-4)))
    is_weekend = et_now.weekday() >= 5
    matchup_earliest = 9 if is_weekend else 12
    show_matchup_previews = et_now.hour >= matchup_earliest

    try:
        table_check = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='notable_events'"
        ).fetchone()
        if not table_check:
            return []

        # Check if expires_at column exists
        cols = {row[1] for row in conn.execute("PRAGMA table_info(notable_events)").fetchall()}
        has_expires = "expires_at" in cols

        if has_expires:
            rows = conn.execute("""
                SELECT headline, detail, category, game_date, player_names, team_names,
                       game_context, detection_type
                FROM notable_events
                WHERE expires_at IS NULL OR expires_at = '' OR expires_at > ?
                ORDER BY game_date DESC, priority ASC, id DESC
                LIMIT ?
            """, (now_utc, limit * 2)).fetchall()  # fetch extra to account for filtered previews
        else:
            rows = conn.execute("""
                SELECT headline, detail, category, game_date, player_names, team_names,
                       game_context, detection_type
                FROM notable_events
                ORDER BY game_date DESC, priority ASC, id DESC
                LIMIT ?
            """, (limit * 2,)).fetchall()
    finally:
        conn.close()

    # Build filtered list with detection_type for interleaving
    filtered = []
    for r in rows:
        if r[7] == "matchup_preview" and not show_matchup_previews:
            continue
        filtered.append({
            "headline": r[0],
            "detail": r[1],
            "category": r[2],
            "game_date": r[3],
            "player_names": json.loads(r[4]) if r[4] else [],
            "team_names": json.loads(r[5]) if r[5] else [],
            "game_context": r[6] or "",
            "_type": r[7],  # for interleaving, stripped before return
        })

    # Interleave by detection_type within each game_date group
    # so no two consecutive events share the same type
    interleaved = []
    from itertools import groupby
    for game_date, group in groupby(filtered, key=lambda e: e["game_date"]):
        bucket = list(group)
        # Round-robin by type: pick one from each type in rotation
        by_type = {}
        for e in bucket:
            by_type.setdefault(e["_type"], []).append(e)
        result = []
        # Sort types: matchup previews first, on_this_date last within today's group
        type_order = {"matchup_preview": 0, "on_this_date": 99}
        type_keys = sorted(by_type.keys(), key=lambda t: type_order.get(t, 50))
        while any(by_type.values()):
            for t in type_keys:
                if by_type.get(t):
                    result.append(by_type[t].pop(0))
            # Remove empty types
            by_type = {t: v for t, v in by_type.items() if v}
            type_keys = [t for t in type_keys if t in by_type]
        interleaved.extend(result)

    # Strip internal _type field and apply limit
    for e in interleaved:
        e.pop("_type", None)

    return interleaved[:limit]


class EventTapRequest(BaseModel):
    headline: str
    tap_type: str  # "player", "matchup", "suggestion"
    device_id: str = ""


@router.post("/event-tap")
async def event_tap(req: EventTapRequest):
    """Log a user tapping on a link within a feed event."""
    from services.metering import log_event_tap
    log_event_tap(req.headline, req.tap_type, req.device_id)
    return {"status": "ok"}
