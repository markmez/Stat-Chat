"""Notable events feed endpoint."""

import json
import os
import sqlite3
from datetime import datetime, timezone

from fastapi import APIRouter, Query as QueryParam

router = APIRouter()

DB_PATH = os.getenv("DB_PATH", "/data/baseball_stats_full.db")


@router.get("/notable-events")
async def get_notable_events(limit: int = QueryParam(50, le=200)):
    """Return recent notable baseball events, ordered by date and priority.
    Filters out expired matchup previews (past game start time).
    Matchup preview interleaving is handled client-side based on user engagement."""
    conn = sqlite3.connect(DB_PATH)
    now_utc = datetime.now(timezone.utc).isoformat()
    try:
        table_check = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='notable_events'"
        ).fetchone()
        if not table_check:
            return []

        rows = conn.execute("""
            SELECT headline, detail, category, game_date, player_names, team_names,
                   game_context, expires_at
            FROM notable_events
            WHERE expires_at IS NULL OR expires_at = '' OR expires_at > ?
            ORDER BY game_date DESC, priority ASC, id DESC
            LIMIT ?
        """, (now_utc, limit)).fetchall()
    finally:
        conn.close()

    return [
        {
            "headline": r[0],
            "detail": r[1],
            "category": r[2],
            "game_date": r[3],
            "player_names": json.loads(r[4]) if r[4] else [],
            "team_names": json.loads(r[5]) if r[5] else [],
            "game_context": r[6] or "",
        }
        for r in rows
    ]
