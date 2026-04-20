"""Notable events feed endpoint."""

import json
import os
import re
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


# === Catalogs for deterministic merge ===========================================
# Map detection_type → the stat key in today's game log most relevant to that
# event. Used to build the "By [verb-ing] N [stat]," lead-in.
_DETECTION_TYPE_STAT = {
    "career_hits_1000": "hits",
    "career_hits_2000": "hits",
    "career_hits_3000": "hits",
    "career_rbi_500": "rbi",
    "career_rbi_1000": "rbi",
    "career_home_runs_100": "home_runs",
    "career_home_runs_200": "home_runs",
    "career_home_runs_300": "home_runs",
    "career_home_runs_400": "home_runs",
    "career_home_runs_500": "home_runs",
    "career_home_runs_600": "home_runs",
    "career_p_wins_100": "wins",
    "career_p_wins_150": "wins",
    "career_p_wins_200": "wins",
    "alltime_passing_hits": "hits",
    "alltime_passing_home_runs": "home_runs",
    "alltime_passing_strikeouts": "strikeouts",
    "alltime_passing_doubles": "doubles",
    "alltime_passing_multi_hr": "home_runs",
    "franchise_passing_hits": "hits",
    "franchise_passing_home_runs": "home_runs",
    "pace_home_runs_50": "home_runs",
    "pace_home_runs_60": "home_runs",
    "pace_home_runs_70": "home_runs",
    "pace_home_runs_80": "home_runs",
    "pace_stolen_bases_50": "stolen_bases",
    "pace_stolen_bases_60": "stolen_bases",
    "pace_stolen_bases_70": "stolen_bases",
    "hitting_streak": "hits",
    "scoreless_streak": None,  # pitcher streak; uses generic lead-in
    "qs_streak": None,
}


# Stat key → lead-in builder. Takes today's count, returns "By [verb-ing] ...".
def _lead_in_for_stat(stat, count):
    if not count or count <= 0:
        return None
    if stat == "home_runs":
        return f"By going deep" if count == 1 else f"By hitting {count} home runs"
    if stat == "stolen_bases":
        return f"By swiping a bag" if count == 1 else f"By swiping {count} bags"
    if stat == "rbi":
        return f"By driving in {count} run" + ("" if count == 1 else "s")
    if stat == "hits":
        return f"By collecting {count} hit" + ("" if count == 1 else "s")
    if stat == "strikeouts":
        return f"By striking out {count}"
    if stat == "wins":
        return "By picking up the win"
    if stat == "saves":
        return "By converting the save"
    if stat == "doubles":
        return f"By hitting {count} double" + ("" if count == 1 else "s")
    return None


# Present-participle → past-tense conversions for the "passing/reaching/taking"
# verbs that appear in impact phrases.
_PRESENT_TO_PAST = {
    "passing": "passed",
    "reaching": "reached",
    "taking": "took",
    "tying": "tied",
    "extending": "extended",
    "joining": "joined",
    "marking": "marked",
    "matching": "matched",
    "setting": "set",
}


def _to_past_tense(text):
    """Convert -ing verbs in a clause to past tense, when used as the main
    action of a statement (not as a subordinate clause)."""
    for pres, past in _PRESENT_TO_PAST.items():
        text = re.sub(rf"\b{pres}\b", past, text)
    return text


# Patterns that match the "stat-line restatement" prefix Sonnet/detectors put
# before the actual impact. We strip these so the lead-in carries the stat.
_REDUNDANT_PREFIX_PATTERNS = [
    # "He now has N career stat, passing/reaching/etc."
    re.compile(r"^He now has [\d,]+ career (?:home runs|hits|RBI|strikeouts|stolen bases|doubles|wins|saves|walks)(?:\s+as a [^,]+)?,?\s*", re.I),
    # "He now has N HR/SB/etc and is on pace for M" — keep the "on pace for M" tail
    re.compile(r"^He now has \d+ \w+ and (?=is on pace)", re.I),
    # "That's his Nth career multi-HR games, passing X" — Schwarber-style
    re.compile(r"^That's his \d+(?:st|nd|rd|th) career [\w-]+(?: games)?,\s*", re.I),
    # "He went X-for-Y[ with anything], taking the lead" — strip stat line
    # before a participle pivot (taking/passing/reaching/etc.)
    re.compile(r"^He went \d+-for-\d+(?:\s+with[^,]+(?:,\s+\d+\s+\w+)*)?,\s*(?=taking|passing|reaching|tying|matching|joining|extending|setting|marking)", re.I),
    re.compile(r"^He threw [\d.]+ (?:scoreless\s+)?(?:IP|innings)(?:\s+with[^,]+)?,\s*(?=taking|passing|reaching|tying|matching|joining|extending|setting|marking)", re.I),
    # "He went 5.2 IP, 2 H, 0 ER, 8 K, W, taking the lead"
    re.compile(r"^He went [\d.]+\s+IP[,\s\d\w]*?,\s*(?=taking|passing|reaching|tying|matching|joining)", re.I),
    # "He picked up a win, and is now N away" — strip the "picked up a win, and "
    # because the lead-in already says "By picking up the win,"
    re.compile(r"^He (?:picked up a win|earned a win|notched a save|recorded a save|hit (?:a|\d+) (?:homer|home run|home runs)|drove in \d+ runs?|stole (?:a|\d+) base|collected \d+ hits?|struck out \d+),?\s+(?:and\s+)?", re.I),
    # Generic batting stat-line strip (no pivot required) — runs LAST as a
    # fallback after the pivot-aware patterns above.
    re.compile(r"^He went \d+-for-\d+(?:\s+with\s+[^,]+(?:\s+and\s+[^,]+)?)?(?:,\s+\d+\s+\w+(?:\s+\w+)*)*,\s+", re.I),
    # Generic pitching stat-line strip (no pivot required)
    re.compile(r"^He threw [\d.]+ (?:scoreless\s+)?(?:IP|innings)(?:\s+with[^,.]+)?,\s+", re.I),
]


# Past-tense verbs that the impact often starts with after stripping. We use
# this to know when to inject "he " as the subject.
_PAST_VERB_STARTERS = {
    "passed", "reached", "took", "tied", "matched", "joined", "extended",
    "set", "marked", "moved", "earned", "picked", "hit", "scored", "drove",
    "stole", "struck", "threw", "gave", "left",
}
_HAS_BE_STARTERS = {"is", "was", "has", "had"}


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

    # Pitching — include W/L/SV in the line
    pitch = conn.execute("""
        SELECT innings_pitched, ip_outs, hits, earned_runs, strikeouts, walks,
               win, loss, save
        FROM game_pitching_logs WHERE player_id = ? AND date = ?
    """, (pid, game_date)).fetchone()
    if pitch:
        ip, ip_outs, h, er, so, bb, w, l, sv = (v or 0 for v in pitch)
        ip_display = ip if ip else f"{ip_outs // 3}.{ip_outs % 3}"
        outcome = ""
        if w: outcome = ", W"
        elif l: outcome = ", L"
        elif sv: outcome = ", SV"
        return (f"{player_name} went {ip_display} IP, {h} H, {er} ER, "
                f"{so} K, {bb} BB{outcome}")
    return ""


def _today_stats_for_player(conn, player_name, game_date):
    """Fetch today's batting + pitching counts for a player. Returns dict."""
    pid_row = conn.execute(
        "SELECT player_id FROM players WHERE name = ?", (player_name,)
    ).fetchone()
    if not pid_row:
        return {}
    pid = pid_row[0]
    out = {}
    bat = conn.execute("""
        SELECT hits, at_bats, home_runs, rbi, doubles, triples, walks, stolen_bases, runs
        FROM game_batting_logs WHERE player_id = ? AND date = ?
    """, (pid, game_date)).fetchone()
    if bat:
        out.update(dict(zip(
            ["hits", "at_bats", "home_runs", "rbi", "doubles", "triples",
             "walks", "stolen_bases", "runs"],
            [v or 0 for v in bat]
        )))
    pitch = conn.execute("""
        SELECT win, loss, save, strikeouts, ip_outs, earned_runs, hits AS h_allowed
        FROM game_pitching_logs WHERE player_id = ? AND date = ?
    """, (pid, game_date)).fetchone()
    if pitch:
        out["wins"] = pitch[0] or 0
        out["losses"] = pitch[1] or 0
        out["saves"] = pitch[2] or 0
        out["strikeouts"] = pitch[3] or 0
        out["ip_outs"] = pitch[4] or 0
        out["earned_runs_allowed"] = pitch[5] or 0
    return out


def _strip_redundant_prefix(s):
    """Remove stat-line restatement that often precedes the actual impact."""
    for pat in _REDUNDANT_PREFIX_PATTERNS:
        new_s = pat.sub("", s)
        if new_s != s:
            s = new_s.strip()
            # Re-strip in case multiple patterns apply
            for inner in _REDUNDANT_PREFIX_PATTERNS:
                s = inner.sub("", s).strip()
            break
    return s


# Keywords in headline → stat key (used for historical_scan / generic events)
_HEADLINE_STAT_KEYWORDS = [
    ("stolen base", "stolen_bases"),
    ("strikeouts", "strikeouts"),
    ("strikeout", "strikeouts"),
    ("home runs", "home_runs"),
    ("home run", "home_runs"),
    ("homer", "home_runs"),
    ("multi-HR", "home_runs"),
    ("multi-homer", "home_runs"),
    ("hits", "hits"),
    ("hit", "hits"),
    ("RBI", "rbi"),
    ("doubles", "doubles"),
    ("walks", "walks"),
    ("save", "saves"),
    ("win", "wins"),
]


def _detect_stat_from_headline(headline):
    """Best-effort: figure out which stat an event is anchored on by scanning
    its headline. Used when detection_type doesn't map directly."""
    h_low = headline.lower()
    for kw, stat in _HEADLINE_STAT_KEYWORDS:
        if kw.lower() in h_low:
            return stat
    return None


def _ensure_subject(text):
    """Ensure the impact text starts with a subject (he/that/it). If it starts
    with a bare verb or "is/has", prepend "he ". If it starts with a noun
    phrase like 'the first', prepend "that was "."""
    if not text:
        return text
    first = text.split()[0].lower().rstrip(",.")
    # Already has a subject
    if first in ("he", "she", "they", "that", "this", "it"):
        return text
    if first in _PAST_VERB_STARTERS or first in _HAS_BE_STARTERS:
        return "he " + text[0].lower() + text[1:]
    if first in ("the", "his", "her", "a", "an"):
        return "that was " + text[0].lower() + text[1:]
    return text


def _format_impact(headline, player_name, detection_type, today_stats):
    """Turn an event headline into a single follow-up sentence:

      "By [stat-action], [past-tense impact]."

    Falls back to a generic "He ..." form if the lead-in can't be built.
    """
    h = headline.strip()

    # AI-insight shape: take the part after "—"
    if "—" in h:
        impact_text = h.split("—", 1)[1].strip()
    else:
        # Strip "{player_name} " from the front
        if h.startswith(player_name + " "):
            h = h[len(player_name) + 1:].strip()
        # If multi-sentence, drop the first (stat line lead) and use the rest
        sentences = [s.strip() for s in h.split(". ") if s.strip()]
        if len(sentences) >= 2:
            impact_text = ". ".join(sentences[1:])
        elif sentences:
            impact_text = "He " + sentences[0]
        else:
            return ""

    # Strip any "He now has N career X, " or "He picked up a win, " restatement
    impact_text = _strip_redundant_prefix(impact_text)
    # Past-tense the connector verbs
    impact_text = _to_past_tense(impact_text)
    impact_text = impact_text.strip()
    if not impact_text:
        return ""

    # Build the lead-in based on the event's stat type + today's count
    stat_key = _DETECTION_TYPE_STAT.get(detection_type)
    if not stat_key:
        # Fall back to scanning the original headline for stat keywords
        stat_key = _detect_stat_from_headline(headline)
    lead_in = None
    if stat_key:
        count = today_stats.get(stat_key, 0)
        lead_in = _lead_in_for_stat(stat_key, count)

    # Ensure the impact has a subject (he / that / it)
    impact_text = _ensure_subject(impact_text)

    if lead_in:
        # After comma, the impact should start lowercase
        if impact_text and impact_text[0].isupper():
            impact_text = impact_text[0].lower() + impact_text[1:]
        sentence = f"{lead_in}, {impact_text}"
    else:
        # No lead-in: the impact stands on its own — capitalize first word
        sentence = impact_text[0].upper() + impact_text[1:] if impact_text else ""

    if sentence and not sentence.endswith((".", "!", "?")):
        sentence = sentence.rstrip(",;: ") + "."
    return sentence


def _merge_player_events(conn, group, player_name, game_date):
    """Combine 2+ events for the same player+date into one card.

    Lead = deterministic stat line from game logs.
    Followups = "By [stat-action], [past-tense impact]." per event.
    Falls back to first event's headline if no stat line is buildable.
    """
    stat_line = _build_stat_line(conn, player_name, game_date)
    today_stats = _today_stats_for_player(conn, player_name, game_date)

    impacts = []
    for e in group:
        sentence = _format_impact(
            e["headline"], player_name, e.get("_type", ""), today_stats
        )
        if sentence and sentence not in impacts:  # dedup identical follow-ups
            impacts.append(sentence)

    if stat_line:
        merged_headline = f"{stat_line}. " + " ".join(impacts)
    elif impacts:
        merged_headline = group[0]["headline"]
        if not merged_headline.endswith((".", "!", "?")):
            merged_headline += "."
        if len(impacts) > 1:
            merged_headline += " " + " ".join(impacts[1:])
    else:
        merged_headline = group[0]["headline"]

    base = dict(group[0])
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
            # Use first player name as the merge key (primary subject).
            # Events with secondary players ("Sale passed Glavine") still merge
            # under "Sale". Events with no player_names (some matchup
            # previews, team events) are skipped.
            if pn and t not in _NON_MERGEABLE_TYPES:
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
