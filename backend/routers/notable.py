"""Notable events feed endpoint."""

import json
import os
import sqlite3
from datetime import date

from fastapi import APIRouter, Query as QueryParam

router = APIRouter()

DB_PATH = os.getenv("DB_PATH", "/data/baseball_stats_full.db")


@router.get("/notable-events")
async def get_notable_events(limit: int = QueryParam(50, le=200)):
    """Return recent notable baseball events, ordered by date and priority.
    Matchup previews are interleaved among today's events only."""
    conn = sqlite3.connect(DB_PATH)
    try:
        # Check if table exists
        table_check = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='notable_events'"
        ).fetchone()
        if not table_check:
            return []

        rows = conn.execute("""
            SELECT headline, detail, category, game_date, player_names, team_names,
                   game_context, detection_type
            FROM notable_events
            ORDER BY game_date DESC, priority ASC, id DESC
            LIMIT ?
        """, (limit,)).fetchall()
    finally:
        conn.close()

    def to_event(r):
        return {
            "headline": r[0],
            "detail": r[1],
            "category": r[2],
            "game_date": r[3],
            "player_names": json.loads(r[4]) if r[4] else [],
            "team_names": json.loads(r[5]) if r[5] else [],
            "game_context": r[6] or "",
        }

    today = date.today().isoformat()

    # Split into three buckets
    today_previews = []
    today_others = []
    older = []

    for r in rows:
        event = to_event(r)
        game_date = r[3]
        detection_type = r[7]

        if game_date == today and detection_type == "matchup_preview":
            today_previews.append(event)
        elif game_date == today:
            today_others.append(event)
        else:
            older.append(event)

    # Interleave previews among today's other events (every 3rd slot)
    today_merged = []
    pi = 0
    for i, event in enumerate(today_others):
        if pi < len(today_previews) and i > 0 and i % 3 == 2:
            today_merged.append(today_previews[pi])
            pi += 1
        today_merged.append(event)

    # If there are still previews left (few today events), append them
    while pi < len(today_previews):
        today_merged.append(today_previews[pi])
        pi += 1

    return (today_merged + older)[:limit]
