"""Notable events feed endpoint."""

import json
import os
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Query as QueryParam
from pydantic import BaseModel

router = APIRouter()

DB_PATH = os.getenv("DB_PATH", "/data/baseball_stats_full.db")


# Detection types that should NOT be merged with others (multi-player or
# non-game events)
_NON_MERGEABLE_TYPES = {"matchup_preview", "on_this_date"}


def _build_stat_line(conn, player_name, game_date):
    """Build a deterministic batting/pitching stat-line lead sentence for a
    player's game on a given date. Returns '' if no game data found."""
    pid_row = conn.execute(
        "SELECT player_id FROM players WHERE name = ?", (player_name,)
    ).fetchone()
    if not pid_row:
        return ""
    pid = pid_row[0]

    # Batting (preferred if player had ABs)
    bat = conn.execute("""
        SELECT hits, at_bats, home_runs, rbi, doubles, triples, walks, stolen_bases
        FROM game_batting_logs WHERE player_id = ? AND date = ?
    """, (pid, game_date)).fetchone()
    if bat and (bat[1] or 0) > 0:
        h, ab, hr, rbi, d, t, bb, sb = (v or 0 for v in bat)
        parts = []
        if hr: parts.append(f"{hr} HR")
        if rbi: parts.append(f"{rbi} RBI")
        if d: parts.append(f"{d} 2B")
        if t: parts.append(f"{t} 3B")
        if bb: parts.append(f"{bb} BB")
        if sb: parts.append(f"{sb} SB")
        extras = " with " + ", ".join(parts) if parts else ""
        return f"{player_name} went {h}-for-{ab}{extras}"

    # Pitching
    pitch = conn.execute("""
        SELECT innings_pitched, ip_outs, hits, earned_runs, strikeouts, walks
        FROM game_pitching_logs WHERE player_id = ? AND date = ?
    """, (pid, game_date)).fetchone()
    if pitch:
        ip, ip_outs, h, er, so, bb = (v or 0 for v in pitch)
        ip_display = ip if ip else f"{ip_outs // 3}.{ip_outs % 3}"
        return (f"{player_name} threw {ip_display} IP with {so} K and {bb} BB, "
                f"allowing {h} hits and {er} earned runs")
    return ""


_VERB_STARTERS = {"matches", "ties", "tied", "sets", "set", "marks",
                  "marked", "reaches", "reached", "joins", "joined",
                  "extends", "extended", "passes", "passed",
                  "matching", "tying", "setting", "marking", "reaching"}
_NOUN_STARTERS = {"the", "his", "her", "a", "an"}


def _extract_impact(headline, player_name):
    """Pull the 'impact' portion out of an event headline so it can follow a
    stat-line lead. Strips the leading 'Player went X-for-Y' or similar.
    Returns a sentence-cased string ending in '.'."""
    h = headline.strip()
    impact = ""
    if "—" in h:
        # AI-insight shape: "{player} did X — {impact}"
        impact = h.split("—", 1)[1].strip()
    elif h.startswith(player_name + " "):
        # Structural shape: "{player} {action}" — strip name, add "He"
        impact = "He " + h[len(player_name) + 1:].strip()
    else:
        impact = h
    if not impact:
        return ""

    # Smooth standalone fragments by adding subject/connector
    first_word = impact.split()[0].lower().rstrip(",")
    if first_word in _VERB_STARTERS:
        impact = "That " + impact
    elif first_word in _NOUN_STARTERS and not impact.lower().startswith("his career"):
        impact = "That was " + impact[0].lower() + impact[1:]

    impact = impact[0].upper() + impact[1:]
    if not impact.endswith((".", "!", "?")):
        impact = impact.rstrip(",;: ") + "."
    return impact


def _merge_player_events(conn, group, player_name, game_date):
    """Combine 2+ events for the same player+date into one card.

    Lead = deterministic stat line from game logs.
    Followups = each event's 'impact' part as its own sentence.
    Falls back to first event's headline if no stat line is buildable.
    """
    stat_line = _build_stat_line(conn, player_name, game_date)
    impacts = [_extract_impact(e["headline"], player_name) for e in group]
    impacts = [i for i in impacts if i]

    if stat_line:
        merged_headline = f"{stat_line}. " + " ".join(impacts)
    elif impacts:
        # No stat-line buildable (rare). Use first event's full headline + rest as followups.
        merged_headline = group[0]["headline"]
        if not merged_headline.endswith((".", "!", "?")):
            merged_headline += "."
        if len(impacts) > 1:
            merged_headline += " " + " ".join(impacts[1:])
    else:
        merged_headline = group[0]["headline"]

    base = dict(group[0])  # use first event as the template (priority, type, etc.)
    base["headline"] = merged_headline
    return base


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

    # Merge multi-event groups for the same player + game_date into a single
    # card. Reduces "3 thin cards about Judge" to one rich narrative.
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        merge_groups = defaultdict(list)
        merge_keys_in_order = []  # preserve insertion order for stable output
        non_mergeable_indices = set()

        for idx, e in enumerate(filtered):
            pn = e.get("player_names", [])
            t = e.get("_type")
            if len(pn) == 1 and t not in _NON_MERGEABLE_TYPES:
                key = (pn[0], e["game_date"])
                if key not in merge_groups:
                    merge_keys_in_order.append((key, idx))
                merge_groups[key].append(e)
            else:
                non_mergeable_indices.add(idx)

        # Build the post-merge list, preserving original ordering
        post_merge = []
        seen_groups = set()
        for idx, e in enumerate(filtered):
            if idx in non_mergeable_indices:
                post_merge.append(e)
                continue
            pn = e.get("player_names", [])
            key = (pn[0], e["game_date"])
            if key in seen_groups:
                continue  # already merged at this group's first occurrence
            seen_groups.add(key)
            group = merge_groups[key]
            if len(group) >= 2:
                post_merge.append(_merge_player_events(conn, group, pn[0], e["game_date"]))
            else:
                post_merge.append(e)
        filtered = post_merge
    finally:
        conn.close()

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
