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
    "alltime_passing_rbi": "rbi",
    "alltime_passing_stolen_bases": "stolen_bases",
    "alltime_passing_strikeouts": "strikeouts",
    "alltime_passing_doubles": "doubles",
    "alltime_passing_wins": "wins",
    "alltime_passing_multi_hr": "home_runs",
    "alltime_passing_3_hr": "home_runs",
    "franchise_passing_hits": "hits",
    "franchise_passing_home_runs": "home_runs",
    "franchise_passing_rbi": "rbi",
    "franchise_passing_stolen_bases": "stolen_bases",
    "franchise_passing_doubles": "doubles",
    "franchise_passing_wins": "wins",
    "franchise_passing_strikeouts": "strikeouts",
    "pace_home_runs_50": "home_runs",
    "pace_home_runs_60": "home_runs",
    "pace_home_runs_70": "home_runs",
    "pace_home_runs_80": "home_runs",
    "pace_stolen_bases_50": "stolen_bases",
    "pace_stolen_bases_60": "stolen_bases",
    "pace_stolen_bases_70": "stolen_bases",
    "hitting_streak": "hits",
    "onbase_streak": None,  # mixed (hits + walks + HBP); fall through to keyword scan
    "hr_streak_ended": "home_runs",
    "hot_streak_pelt": "hits",  # OPS-anchored streak; "hits" is the closest lead-in match
    # Pitching dominance streaks: no "By striking out N" lead-in. A quality
    # start is 6+ IP / ≤3 ER and a scoreless streak is about runs, not Ks —
    # the strikeout lead-in implies a false connection. Fall through (None) so
    # the impact emits as its own sentence ("He now has 5 consecutive…").
    "scoreless_streak": None,
    "qs_streak": None,
    "14k_game": "strikeouts",
    "career_p_strikeouts_1000": "strikeouts",
    "career_high": None,  # depends on stat in headline; falls through to keyword scan
    "career_first": None,  # varies (first HR/win/save); fall through to keyword scan
    "historical_scan": None,  # broad family; varies. Fall through to keyword scan.
    "ai_insight": None,      # Sonnet narrative; varies. Fall through to keyword scan.
    "on_this_date": None,    # non-mergeable (see _NON_MERGEABLE_TYPES); catalog entry for completeness.
}


# Detection types that must NEVER get a "By [verb-ing] N," lead-in, even via the
# keyword fallback. Pitching-dominance streaks (quality starts, scoreless
# starts) aren't "caused" by any single box-score stat, and the fallback would
# otherwise latch onto the strikeout count in the game line ("By striking out 6,
# he now has 5 consecutive quality starts" — Ks are irrelevant to a quality
# start). These impacts stand on their own as a sentence.
_NO_LEAD_IN_DETECTION_TYPES = {"qs_streak", "scoreless_streak"}


# Impact clauses where a "By [player verb-ing], ..." lead-in shouldn't
# precede the impact. Two categories:
#
#   1. Achievement-subjected ("that's the first 7+ RBI game...") — the
#      gerund implies the player as subject, but "that's" refers to the
#      game/at-bat. The lead-in produces a dangling participle.
#
#   2. Trajectory observations ("is now on pace for 54 HR this season")
#      — pace is a season-long projection, not a discrete consequence
#      of today's specific action. The lead-in implies a false causal
#      link ("by hitting today's HR he is suddenly on pace for 54"),
#      when really the pace is the season trend that includes today.
#
# In both cases, drop the lead-in and emit the impact as its own sentence.
_NO_LEAD_IN_PREFIX = re.compile(
    r"^(?:"
    r"that's|that was|"
    r"the (?:first|last|most|only)\b|"
    # "the 10 K is a new career high" — the count restates the lead-in
    # ("By striking out 10, the 10 K is…"), so drop the lead-in entirely.
    r"the \d+ \S+ (?:is|are|was|were)\b|"
    r"(?:he\s+)?is\s+(?:now\s+)?on\s+pace\s+for"
    r")",
    re.I,
)


def _impact_should_skip_lead_in(text: str) -> bool:
    return bool(text and _NO_LEAD_IN_PREFIX.match(text.strip()))


# Stat key → lead-in builder. Takes today's count, returns "By [verb-ing] ...".
# Singular forms read more naturally: "By picking up a hit" beats "By collecting 1 hit".
def _lead_in_for_stat(stat, count):
    if not count or count <= 0:
        return None
    if stat == "home_runs":
        return "By going deep" if count == 1 else f"By hitting {count} home runs"
    if stat == "stolen_bases":
        return "By swiping a bag" if count == 1 else f"By swiping {count} bags"
    if stat == "rbi":
        return "By driving in a run" if count == 1 else f"By driving in {count} runs"
    if stat == "hits":
        return "By picking up a hit" if count == 1 else f"By collecting {count} hits"
    if stat == "strikeouts":
        return f"By striking out {count}"
    if stat == "wins":
        return "By picking up the win"
    if stat == "saves":
        return "By converting the save"
    if stat == "doubles":
        return "By hitting a double" if count == 1 else f"By hitting {count} doubles"
    if stat == "triples":
        return "By legging out a triple" if count == 1 else f"By hitting {count} triples"
    if stat == "walks":
        return "By drawing a walk" if count == 1 else f"By drawing {count} walks"
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
    """Convert -ing verbs in a clause to past tense, but only when used as
    the MAIN action (anchored at the start of the impact text). Participles
    that appear mid-clause as continuations — e.g. "...his first 10-K game,
    matching his season high" — must stay in -ing form so the joined
    sentence reads correctly under a lead-in like "By striking out 10, ..."""
    for pres, past in _PRESENT_TO_PAST.items():
        text = re.sub(rf"^(\s*){pres}\b", rf"\1{past}", text)
    return text


# Stat-token shape used inside the restatement patterns below. Matches
# either "{N} {stat}" or "a {homer/triple/double/walk/hit/stolen base}".
# Tight set so the restatement strip doesn't eat richer prose (game
# context like "in the win against Boston") by accident.
_STAT_PART = (
    r"(?:\d+\s+\w+|a\s+(?:homer|triple|double|walk|hit|stolen\s+base))"
)

# Patterns that match the "stat-line restatement" prefix Sonnet/detectors put
# before the actual impact. We strip these so the lead-in carries the stat.
_REDUNDANT_PREFIX_PATTERNS = [
    # "He now has N career stat, passing/reaching/etc."
    re.compile(r"^He now has [\d,]+ career (?:home runs|hits|RBI|strikeouts|stolen bases|doubles|wins|saves|walks)(?:\s+as a [^,]+)?,?\s*", re.I),
    # "He now has N HR/SB/etc and is on pace for M" — keep the "on pace for M" tail
    re.compile(r"^He now has \d+ \w+ and (?=is (?:now )?on pace)", re.I),
    # "That's his Nth career multi-HR games, passing X" — Schwarber-style
    re.compile(r"^That's his \d+(?:st|nd|rd|th) career [\w-]+(?: games)?,\s*", re.I),
    # "He went X-for-Y[ with anything], taking the lead" — strip stat line
    # before a participle pivot. Past-tense forms (took/passed/etc.) included
    # because rule-based detectors emit past tense; without that branch, a
    # restated stat line survives ahead of the impact.
    re.compile(r"^He went \d+-for-\d+(?:\s+with[^,]+(?:,\s+\d+\s+\w+)*)?,\s*(?=taking|passing|reaching|tying|matching|joining|extending|setting|marking|took|passed|reached|tied|matched|joined|extended|set\b|marked)", re.I),
    # Variant of the above where the pivot is introduced by " and PIVOT"
    # with no leading comma. Historical_scan output: "He went 2-for-4 with
    # a homer and 2 RBI and tied Aranda for the AL lead in RBI." Restricted
    # to stat-shaped tokens in the "with" portion so we don't accidentally
    # eat game-context prose ("in the win against Boston").
    re.compile(
        rf"^He went \d+-for-\d+\s+with\s+"
        rf"{_STAT_PART}(?:(?:\s*,\s*|\s+and\s+){_STAT_PART})*"
        rf"\s+and\s+"
        rf"(?=took|passed|reached|tied|matched|joined|extended|set\b|marked|"
        rf"taking|passing|reaching|tying|matching|joining|extending|setting|marking)",
        re.I,
    ),
    re.compile(r"^He threw [\d.]+ (?:scoreless\s+)?(?:IP|innings)(?:\s+with[^,]+)?,\s*(?=taking|passing|reaching|tying|matching|joining|extending|setting|marking|took|passed|reached|tied|matched|joined|extended|set\b|marked)", re.I),
    # "He went 5.2 IP, 2 H, 0 ER, 8 K, W, taking the lead"
    re.compile(r"^He went [\d.]+\s+IP[,\s\d\w]*?,\s*(?=taking|passing|reaching|tying|matching|joining|took|passed|reached|tied|matched|joined)", re.I),
    # "He threw 5.2 IP, 2 ER, 9 K, taking the lead" — historical_scan
    # output uses "threw" instead of "went". Same shape as the line above
    # but with the alternate verb. Without this, multi-comma pitching stat
    # lines from threw-formatted headlines survive ahead of the impact.
    re.compile(r"^He threw [\d.]+\s+(?:IP|innings)[,\s\d\w]*?,\s*(?=taking|passing|reaching|tying|matching|joining|took|passed|reached|tied|matched|joined)", re.I),
    # "He picked up a win, and is now N away" — strip the "picked up a win, and "
    # because the lead-in already says "By picking up the win,"
    re.compile(r"^He (?:picked up a win|earned a win|notched a save|recorded a save|hit (?:a|\d+) (?:homer|home run|home runs)|drove in \d+ runs?|stole (?:a|\d+) base|collected \d+ hits?|struck out \d+),?\s+(?:and\s+)?", re.I),
    # Streak prefixes — keep the "now has N consecutive..." impact. MUST precede
    # the generic stat-line strips below: the generic pitching strip's greedy
    # [^,.]+ would otherwise swallow "now has ..." up to the comma once the
    # streak's "first since X" context is comma-appended to it.
    re.compile(r"^He threw [\d.]+ scoreless (?:IP|innings) with \d+ K and (?=now has)", re.I),
    re.compile(r"^He went [\d.]+ IP,\s*\d+\s*ER,\s*\d+\s*K and (?=now has)", re.I),
    # Generic batting stat-line strip (no pivot required) — fallback after the
    # pivot-aware and streak-specific patterns above.
    re.compile(r"^He went \d+-for-\d+(?:\s+with\s+[^,]+(?:\s+and\s+[^,]+)?)?(?:,\s+\d+\s+\w+(?:\s+\w+)*)*,\s+", re.I),
    # Generic pitching stat-line strip (no pivot required)
    re.compile(r"^He threw [\d.]+ (?:scoreless\s+)?(?:IP|innings)(?:\s+with[^,.]+)?,\s+", re.I),
]


# Detection types whose underlying fact is still ongoing as of latest_date.
# When the impact narrates context for these, prefer present tense ("that's
# the longest…") over past ("that was the longest…").
_ACTIVE_STATE_DETECTION_TYPES = {
    "hitting_streak", "on_base_streak", "hr_streak", "scoreless_streak",
    "qs_streak", "current_form", "season_pace", "pace_home_runs",
    "pace_home_runs_50", "pace_home_runs_60", "pace_home_runs_70", "pace_home_runs_80",
    "pace_stolen_bases_50", "pace_stolen_bases_60", "pace_stolen_bases_70",
}


# After stripping prefixes and past-tensing connector verbs, run this pass
# to clean up common AI-generated grammar slips that the verifier doesn't
# catch (missing auxiliary "is", missing season qualifier, lone "now has X"
# without subject pronoun).
def _normalize_phrasings(text: str) -> str:
    if not text:
        return text
    # "and on pace for N" without preceding "is" — common Sonnet slip.
    # Pattern: word boundary, "and", whitespace, "on pace for", but only when
    # not already preceded by "is".
    text = re.sub(r"(?i)\band on pace for\b", "and is on pace for", text)
    # "now has 9 homers" / "now has 4 HR" without subject pronoun.
    if re.match(r"(?i)^now has\b", text):
        text = "he " + text
    # "on pace for N" without season qualifier — append "this season" so the
    # timeframe is unambiguous. Skip if already qualified. The number group
    # `[\d,]+` handles comma-grouped totals (1,000 hits) which a bare \d
    # would chop at the comma.
    has_qualifier = re.search(
        r"(?i)on pace for\s+[\d,]+[\w\s\-]*?\b(this season|this year|by season's end|on the year|on the season)\b",
        text,
    )
    if not has_qualifier and re.search(r"(?i)\bon pace for\s+\d", text):
        # Find "on pace for N {optional unit}" and append "this season" before
        # the next punctuation/clause boundary. The number regex
        # `\d+(?:,\d+)*` matches integer/comma forms (54, 1,000) without
        # eating a terminal sentence period that follows.
        text = re.sub(
            r"(?i)(on pace for\s+[\d,]+(?:\s+[a-z\-]+){0,3})(?=[\.,;:]|$| and | which )",
            r"\1 this season",
            text,
            count=1,
        )
    # If the impact rides under a "By X-ing N, ..." lead-in (caller will join
    # with comma) and the impact starts with "matched"/"tied"/"extended"/etc.,
    # the participle form ("matching"/"tying"/"extending") reads better.
    # Caller passes in via the `_continuation_form` helper below — leave text
    # unchanged here (transform happens in the merge step).
    return text


# Past-tense verbs that the impact often starts with after stripping. We use
# this to know when to inject "he " as the subject.
_PAST_VERB_STARTERS = {
    "passed", "reached", "took", "tied", "matched", "joined", "extended",
    "set", "marked", "moved", "earned", "picked", "hit", "scored", "drove",
    "stole", "struck", "threw", "gave", "left",
}
_HAS_BE_STARTERS = {"is", "was", "has", "had", "now"}
# Present participles that survive into the impact when an active-state event
# (live streak / ongoing pace) preserves present tense. Without this, an impact
# starting "extending his hitting streak..." gets no subject prepended and
# emerges as a sentence fragment ("By collecting 2 hits, extending his...").
_PRESENT_PARTICIPLE_STARTERS = set(_PRESENT_TO_PAST.keys())

# Sentence-boundary regex: period + whitespace + uppercase letter. Avoids
# splitting on abbreviations like "Jr." / "Sr." / "St." since the next word
# typically starts lowercase ("Jr. for the AL lead").
_SENTENCE_SPLIT = re.compile(r"\.\s+(?=[A-Z])")


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
        if not parts:
            extras = ""
        elif len(parts) == 1:
            extras = " with " + parts[0]
        elif len(parts) == 2:
            extras = " with " + parts[0] + " and " + parts[1]
        else:
            extras = " with " + ", ".join(parts[:-1]) + ", and " + parts[-1]
        return f"{player_name} went {h}-for-{ab}{extras}"

    # Pitching — include W/L/SV in the line
    pitch = conn.execute("""
        SELECT innings_pitched, ip_outs, hits, earned_runs, strikeouts, walks,
               win, loss, save
        FROM game_pitching_logs WHERE player_id = ? AND date = ?
    """, (pid, game_date)).fetchone()
    if pitch:
        ip, ip_outs, h, er, so, bb, w, l, sv = (v or 0 for v in pitch)
        from services.historical_scans import fmt_ip
        ip_display = fmt_ip(ip_outs)
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


# Keywords in headline → stat key (used for historical_scan / generic events).
# Order matters — longer / more specific keywords first to avoid premature
# matches (e.g. "stolen base" before "base").
_HEADLINE_STAT_KEYWORDS = [
    ("stolen base", "stolen_bases"),
    ("strikeouts", "strikeouts"),
    ("strikeout", "strikeouts"),
    ("home runs", "home_runs"),
    ("home run", "home_runs"),
    ("homer", "home_runs"),
    ("multi-HR", "home_runs"),
    ("multi-homer", "home_runs"),
    ("batting average", "hits"),
    ("average", "hits"),
    ("hits", "hits"),
    ("hit", "hits"),
    ("RBI", "rbi"),
    ("doubles", "doubles"),
    ("walks", "walks"),
    ("save", "saves"),
    ("win", "wins"),
]

# Rate-stat keywords. These don't get a "By X" lead-in — instead, their
# impact is embedded as a comma continuation of the stat line itself
# ("Sal Stewart went 1-for-3 with 1 HR, 1 RBI, taking the NL lead in
# slugging (.725), passing CJ Abrams (.716).")
_RATE_STAT_KEYWORDS = ("slugging", "OPS", "OBP", "on-base percentage")

# Bare-abbrev keywords (require word boundaries so " K " doesn't match "Kim")
_HEADLINE_ABBREV_PATTERNS = [
    (re.compile(r"\b\d+\s+K\b"), "strikeouts"),
    (re.compile(r"\b\d+\s+HR\b"), "home_runs"),
    (re.compile(r"\b\d+\s+RBI\b"), "rbi"),
    (re.compile(r"\b\d+\s+SB\b"), "stolen_bases"),
]


def _is_rate_stat_event(headline):
    """Detect headlines anchored on a rate stat (slugging/OPS/OBP)."""
    h_low = headline.lower()
    return any(kw.lower() in h_low for kw in _RATE_STAT_KEYWORDS)


def _extract_rate_stat_continuation(headline, player_name):
    """For rate-stat events, extract the participle-form impact suitable for
    appending to the stat line as a continuation.

    Input:  "Sal Stewart went 1-for-3 with 1 HR, 1 RBI, taking the NL lead
             in slugging (.725), passing CJ Abrams (.716)."
    Output: "taking the NL lead in slugging (.725), passing CJ Abrams (.716)"
    """
    h = headline.strip().rstrip(".!?")
    # Strip player name from front
    if h.startswith(player_name + " "):
        h = h[len(player_name) + 1:].strip()
    # Find the participle pivot and take everything after the comma before it
    pivot_match = re.search(r",\s+(taking|passing|reaching|tying|matching|joining|extending)", h, re.I)
    if pivot_match:
        return h[pivot_match.start() + 1:].strip()  # +1 to skip the comma
    # Fallback: split on first comma, return the rest
    if "," in h:
        return h.split(",", 1)[1].strip()
    return h


def _detect_stat_from_headline(headline):
    """Best-effort: figure out which stat an event is anchored on. Long-form
    keywords ('home runs', 'stolen bases') usually appear in the IMPACT
    clause ('took the lead in stolen bases'); abbreviations ('1 SB', '2 HR')
    usually appear in the BOX SCORE STAT LINE. Check long-form first so we
    detect the event's actual stat, not the player's box-score line."""
    h_low = headline.lower()
    for kw, stat in _HEADLINE_STAT_KEYWORDS:
        if kw.lower() in h_low:
            return stat
    # Fallback: abbrev patterns
    for pat, stat in _HEADLINE_ABBREV_PATTERNS:
        if pat.search(headline):
            return stat
    return None


def _ensure_subject(text, *, active_state: bool = False):
    """Ensure the impact text starts with a subject (he/that/it). If it starts
    with a bare verb or "is/has", prepend "he ". If it starts with a noun
    phrase like 'the first', prepend "that was " (or "that's" for active state).

    `active_state=True` means the underlying fact is still ongoing (active
    hitting/on-base streak, current pace, ranking that hasn't ended). Use
    "that's" instead of "that was" so the tense matches reality.
    """
    if not text:
        return text
    # Slash-line fragment like ".364/.533/.955 with 4 HR over 7 games" —
    # needs a subject + verb to stand alone.
    if re.match(r"^\.?\d+\s*/\s*\.?\d+\s*/\s*\.?\d+", text):
        return "he's hitting " + text
    # Ordinal-rank starts like "18th-longest in the last 100+ years" or
    # "5th most HR ever" — abrupt without a "It's the" intro.
    if re.match(r"^\d+(?:st|nd|rd|th)[\s\-]", text, re.I):
        return "It's the " + text[0].lower() + text[1:]
    first = text.split()[0].lower().rstrip(",.")
    # Already has a subject
    if first in ("he", "she", "they", "that", "this", "it"):
        return text
    if first in _PAST_VERB_STARTERS or first in _HAS_BE_STARTERS:
        return "he " + text[0].lower() + text[1:]
    # Present participle survivor (active-state preserved "extending"/"matching").
    # Use "he's" so tense stays present-progressive — the streak is still alive.
    if first in _PRESENT_PARTICIPLE_STARTERS:
        return "he's " + text[0].lower() + text[1:]
    if first in ("the", "his", "her", "a", "an"):
        first_clause = " ".join(text.split()[:10]).lower()
        if any(f" {v} " in f" {first_clause} " for v in ("is", "was", "has", "had", "are", "were")):
            return text[0].upper() + text[1:]
        # Person predicate ("the first Phillies pitcher to do it") takes "he's";
        # event/stat predicate ("the longest streak since X") takes "that's".
        from services.historical_scans import is_person_referent
        if is_person_referent(text):
            prefix = "he's " if active_state else "he was "
        else:
            prefix = "that's " if active_state else "that was "
        return prefix + text[0].lower() + text[1:]
    return text


def _normalize_for_dedup(text: str) -> str:
    """Strip leading discourse markers ("that's", "that was", "and") so the
    substring dedup can detect content-equivalent impacts that differ only
    in their sentence-position prefix. Without this, Path A's standalone
    "That's the longest …Athletics player…" doesn't match the same chunk
    embedded mid-sentence in Path B as "…, and the longest …Athletics
    player…", and both survive into the merged headline."""
    t = text.lower().strip()
    for prefix in ("that's ", "that was ", "and "):
        if t.startswith(prefix):
            t = t[len(prefix):]
            break
    return t


def _dedupe_by_substring(impacts):
    """Drop impacts whose content is entirely contained in another impact.
    Handles overlap between hot_streak_pelt ('.364/.533/.955 with 4 HR...')
    and an ai_insight that includes the same phrase embedded in richer
    narrative ('extended his red-hot stretch to .364/.533/.955 with 4 HR...').
    Also handles cross-detector overlap where two streak detectors emit
    the same franchise-since claim with different discourse-position
    prefixes ("That's the longest …" vs "…, and the longest …").
    """
    if len(impacts) <= 1:
        return impacts
    kept = []
    for i, a in enumerate(impacts):
        # Drop if any OTHER impact's normalized form contains this one's,
        # after stripping leading discourse markers so position-of-sentence
        # variants ("That's X" vs "and X") don't block the match.
        a_norm = _normalize_for_dedup(a)
        if any(i != j and a_norm in _normalize_for_dedup(b) and a_norm != _normalize_for_dedup(b)
               for j, b in enumerate(impacts)):
            continue
        kept.append(a)
    return kept


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
        sentences = [s.strip() for s in _SENTENCE_SPLIT.split(h) if s.strip()]
        if len(sentences) >= 2:
            impact_text = ". ".join(sentences[1:])
        elif sentences:
            impact_text = "He " + sentences[0]
        else:
            return ""

    # Strip any "He now has N career X, " or "He picked up a win, " restatement
    impact_text = _strip_redundant_prefix(impact_text)
    active_state = detection_type in _ACTIVE_STATE_DETECTION_TYPES
    # Past-tense the connector verbs (skip for active-state events to keep
    # "matching X's run" rather than "matched X's run" when the streak lives).
    if not active_state:
        impact_text = _to_past_tense(impact_text)
    impact_text = _normalize_phrasings(impact_text)
    impact_text = impact_text.strip()
    if not impact_text:
        return ""

    # Build the lead-in based on the event's stat type + today's count
    stat_key = _DETECTION_TYPE_STAT.get(detection_type)
    if not stat_key and detection_type not in _NO_LEAD_IN_DETECTION_TYPES:
        # Fall back to scanning the original headline for stat keywords
        stat_key = _detect_stat_from_headline(headline)
    lead_in = None
    if stat_key:
        count = today_stats.get(stat_key, 0)
        lead_in = _lead_in_for_stat(stat_key, count)

    # Ensure the impact has a subject (he / that / it)
    impact_text = _ensure_subject(impact_text, active_state=active_state)

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


def _extract_raw_impact(headline, player_name, *, preserve_present_tense: bool = False):
    """Extract just the impact clause (no lead-in, no terminal period).
    Used when combining multiple impacts under one shared lead-in.

    `preserve_present_tense=True` skips the past-tense conversion — useful
    for active-state events (current streaks, ongoing pace) where "matching
    Aaron's run" reads better than "matched Aaron's run" because the streak
    is still alive."""
    h = headline.strip()
    if "—" in h:
        impact_text = h.split("—", 1)[1].strip()
    else:
        if h.startswith(player_name + " "):
            h = h[len(player_name) + 1:].strip()
        sentences = [s.strip() for s in _SENTENCE_SPLIT.split(h) if s.strip()]
        if len(sentences) >= 2:
            impact_text = ". ".join(sentences[1:])
        elif sentences:
            impact_text = "He " + sentences[0]
        else:
            return ""
    impact_text = _strip_redundant_prefix(impact_text)
    if not preserve_present_tense:
        impact_text = _to_past_tense(impact_text)
    impact_text = _normalize_phrasings(impact_text)
    impact_text = impact_text.strip().rstrip(".!?,;: ")
    return impact_text


def _strip_subject_pronoun(text):
    """Remove leading 'he '/'that was '/'that ' so an impact can join a list.
    Also lowercases the leading word if it's currently capitalized so the
    joined list reads naturally ('and the 10 K' not 'and The 10 K')."""
    text = text.strip()
    for prefix in ("he ", "He ", "that was ", "That was ", "that ", "That ", "it ", "It "):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    if text and text[0].isupper():
        text = text[0].lower() + text[1:]
    return text


def _merge_player_events(conn, group, player_name, game_date):
    """Combine 2+ events for the same player+date into one card.

    - Lead = deterministic stat line from game logs.
    - Events sharing a stat key collapse into one sentence:
        "By [stat-action], he [impact 1], [impact 2], and [impact 3]."
    - Different stat keys get their own sentence.
    """
    stat_line = _build_stat_line(conn, player_name, game_date)
    today_stats = _today_stats_for_player(conn, player_name, game_date)

    # Pull rate-stat events out separately — they get embedded into the stat
    # line as a continuation rather than getting their own "By X" sentence.
    rate_continuations = []
    non_rate_events = []
    for e in group:
        if _is_rate_stat_event(e["headline"]):
            cont = _extract_rate_stat_continuation(e["headline"], player_name)
            if cont:
                rate_continuations.append(cont)
        else:
            non_rate_events.append(e)

    # Append rate continuations to the stat line directly
    if stat_line and rate_continuations:
        stat_line = stat_line + ", " + ", ".join(rate_continuations)

    # Group remaining events by their relevant stat key (or "_other" if undetectable)
    by_stat = defaultdict(list)
    stat_order = []  # preserve discovery order
    for e in non_rate_events:
        dt = e.get("_type", "")
        if dt in _NO_LEAD_IN_DETECTION_TYPES:
            # Own group key → a standalone sentence with no lead-in. Keeps the
            # quality-/scoreless-start clause and its "first since X" context
            # together; lumping it into "_other" flattened it into a comma-list
            # with unrelated impacts (e.g. a leaderboard pass), leaving
            # "the first ... to do it" with no clear referent.
            key = dt
        else:
            stat = _DETECTION_TYPE_STAT.get(dt)
            if not stat:
                # Scan the IMPACT portion (after stripping stat-line prefix) so
                # we detect the event's anchor stat, not the box-score line.
                raw = _extract_raw_impact(e["headline"], player_name)
                stat = _detect_stat_from_headline(raw)
            key = stat or "_other"
        if key not in by_stat:
            stat_order.append(key)
        by_stat[key].append(e)

    # Push "_other" (comp/historical sentences without a clear stat anchor)
    # to the end. They often start "that was the longest..." and rely on a
    # preceding sentence to establish the subject — if they come first the
    # pronoun has no antecedent.
    stat_order.sort(key=lambda s: 1 if s == "_other" else 0)

    sentences = []
    for stat in stat_order:
        events = by_stat[stat]
        # Tense is decided PER EVENT, not per group. An active-state event
        # (ongoing streak/pace) keeps present tense ("now has 4 scoreless
        # starts"); a completed event goes past ("took the NL lead"). Grouping a
        # completed action with an active streak must not force the completed
        # one into present tense — "taking the NL lead" for yesterday's game is
        # wrong. Mixed tense in one sentence (past action + current state) reads
        # naturally.
        raw_impacts = []
        impact_active = {}  # impact text -> from an active-state event?
        for e in events:
            e_active = e.get("_type", "") in _ACTIVE_STATE_DETECTION_TYPES
            impact = _extract_raw_impact(
                e["headline"], player_name,
                preserve_present_tense=e_active,
            )
            if impact and impact not in raw_impacts:
                raw_impacts.append(impact)
                impact_active[impact] = e_active
        # Drop impacts that are substrings of other impacts (e.g., bare slash
        # line when another impact embeds it in richer narrative)
        raw_impacts = _dedupe_by_substring(raw_impacts)
        if not raw_impacts:
            continue

        # Build lead-in if we have a stat key + count
        lead_in = None
        if stat != "_other":
            count = today_stats.get(stat, 0)
            lead_in = _lead_in_for_stat(stat, count)

        # Combine impacts: first keeps its subject; rest get stripped and joined
        first = _ensure_subject(raw_impacts[0],
                                active_state=impact_active.get(raw_impacts[0], False))
        if len(raw_impacts) == 1:
            body = first
        elif len(raw_impacts) == 2:
            body = first + ", and " + _strip_subject_pronoun(raw_impacts[1])
        else:
            mid = ", ".join(_strip_subject_pronoun(i) for i in raw_impacts[1:-1])
            body = first + ", " + mid + ", and " + _strip_subject_pronoun(raw_impacts[-1])

        if lead_in and not _impact_should_skip_lead_in(body):
            if body and body[0].isupper():
                body = body[0].lower() + body[1:]
            sentence = f"{lead_in}, {body}"
        else:
            sentence = body[0].upper() + body[1:] if body else ""
        if sentence and not sentence.endswith((".", "!", "?")):
            sentence = sentence.rstrip(",;: ") + "."
        sentences.append(sentence)

    # "He also <verb>" for a 2nd+ discrete-action sentence — signals it's an
    # additional accomplishment rather than reading as a disconnected restatement.
    _also_re = re.compile(
        r"^He (took|hit|drove|stole|struck|threw|picked|notched|earned|recorded|"
        r"set|passed|reached|moved|tied|matched|joined|extended|swiped|collected|drew|scored) ")
    for _i in range(1, len(sentences)):
        if _also_re.match(sentences[_i]):
            sentences[_i] = "He also " + sentences[_i][3:]

    if stat_line:
        merged_headline = f"{stat_line}. " + " ".join(sentences)
    elif sentences:
        merged_headline = " ".join(sentences)
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

    # Bucket events for round-robin interleaving. Key insight from
    # feedback-feed-event-date-semantics.md: game-derived events are always
    # dated to YESTERDAY (when the games were played), even though they
    # appear in the feed TODAY. Today's bucket only ever has supplemental
    # types (matchup_preview, on_this_date). If we let "today" be its own
    # bucket, those events clump at the top of the feed with nothing real
    # to interleave against. Re-bucket today's supplemental events into
    # the most recent date that actually has game events.
    SUPPLEMENTAL_TYPES = {"matchup_preview", "on_this_date"}
    real_event_dates = sorted({
        e["game_date"] for e in filtered
        if e["_type"] not in SUPPLEMENTAL_TYPES
    }, reverse=True)
    most_recent_real = real_event_dates[0] if real_event_dates else None

    # Assign each event to a bucket date (may differ from game_date).
    # Supplemental events more recent than the latest real date get
    # re-bucketed into that real date so they interleave with game events
    # rather than forming their own top-of-feed clump.
    buckets: dict = {}
    bucket_order: list = []  # preserve descending date order
    for e in filtered:
        if (e["_type"] in SUPPLEMENTAL_TYPES
                and most_recent_real
                and e["game_date"] > most_recent_real):
            bd = most_recent_real
        else:
            bd = e["game_date"]
        if bd not in buckets:
            buckets[bd] = []
            bucket_order.append(bd)
        buckets[bd].append(e)
    # Re-sort bucket_order in case re-bucketing changed positions
    bucket_order = sorted(set(bucket_order), reverse=True)

    # Interleave by detection_type within each bucket. Goal: each type's
    # items spread roughly evenly across the bucket's length rather than
    # clustering at the start (which is what plain round-robin produces
    # once smaller types deplete — e.g., OTDs ended up paired 1:1 with
    # historical events for a long stretch, reading as one big OTD clump).
    #
    # Algorithm: assign each event a fractional position within its own
    # type (i + 0.5) / count_in_type, then sort all events across types
    # by position. Items in a sparse type get spaced wider; dense types
    # stay closer together. Within-type ordering is preserved (positions
    # are monotonic with index). Matchup previews keep front-pinning by
    # using negative positions; on_this_date stays a normal type but its
    # natural spread now pushes it through the full bucket.
    interleaved = []
    for bucket_date in bucket_order:
        bucket = buckets[bucket_date]
        # Suppress all-supplemental buckets: nothing real to anchor them to
        # (this catches old historical dates that somehow have only OTD).
        if all(e["_type"] in SUPPLEMENTAL_TYPES for e in bucket):
            continue
        by_type: dict = {}
        for e in bucket:
            by_type.setdefault(e["_type"], []).append(e)
        type_order = {"matchup_preview": 0, "on_this_date": 99}
        scored: list = []
        for t, evts in by_type.items():
            n = len(evts)
            pri = type_order.get(t, 50)
            if pri == 0:
                # Front-pin matchup previews: use negative positions so they
                # sort ahead of every other type, in their natural order.
                for i, e in enumerate(evts):
                    scored.append((-1.0 + i * 0.001, pri, e))
            else:
                for i, e in enumerate(evts):
                    pos = (i + 0.5) / n
                    scored.append((pos, pri, e))
        scored.sort(key=lambda x: (x[0], x[1]))
        interleaved.extend([e for (_, _, e) in scored])

    # Strip internal _type field and apply limit. Also normalize terminal
    # punctuation: some detection paths (career_first, certain AI-narrative
    # outputs) build headlines without trailing periods, which surfaces in
    # the UI as bare-looking sentences ("X hit his first career home run").
    # Defensive fix here covers existing rows immediately, without waiting
    # for re-detection.
    for e in interleaved:
        e.pop("_type", None)
        h = e.get("headline", "")
        if h and not h.rstrip().endswith((".", "!", "?", "\"", "'")):
            e["headline"] = h.rstrip().rstrip(",;: ") + "."

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
