"""
Haiku intent mapper — the first fallback after an interceptor miss.

Wild-phrased queries ("AZ diamondbacks stats", "how yanks doin lately",
"judge dingers this yr") usually aren't hard QUERIES — they're hard STRINGS.
The structural engine can execute them; its regex front end just doesn't
recognize the phrasing. This module makes one cheap Haiku call that rewrites
the query into the canonical phrasing the engine's parsers recognize, so the
query re-enters try_intercept and gets a grounded, deterministic, ~zero-cost
answer instead of demoting to Haiku SQL / sql_planner.

Scope discipline (see memory: query-flexibility plan): the mapper normalizes
ENTITIES and PHRASING only. It must never change the stat, the intent, or
the time frame, and never invent qualifiers. A wrong rewrite here would ship
a confidently-wrong structural answer — the prompt is deliberately narrow
and the caller falls through unchanged on any doubt.

Cost: ~$0.002-0.004/query (Haiku, ~1.5K-token prompt). A hit prevents a
~$0.014 Haiku SQL call and possibly a ~$0.02-0.03 / 15-40s planner run.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date

import anthropic

from services.llm import ROUTING_MODEL

logger = logging.getLogger("statchat.intent_mapper")

_client = None


def _get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic()
    return _client


MAPPER_PROMPT = f"""You rewrite a baseball fan's question into the canonical phrasing a structured stats engine recognizes. Today is {date.today().isoformat()}; the current MLB season is {date.today().year}.

YOUR ONLY JOB: normalize entities and phrasing. NEVER change the stat, the intent, the direction (best/worst, most/fewest), or the time frame. NEVER add or drop a qualifier. If unsure how to rewrite, mark it not expressible — a skipped rewrite is harmless, a wrong one is not.

NORMALIZE:
- Team shorthand → full team name: "AZ"/"D-backs"/"Dbacks" → Diamondbacks; "Yanks"/"Bombers" → Yankees; "Nats" → Nationals; "Halos" → Angels; "Sox" → Red Sox or White Sox only if other cues disambiguate (otherwise not expressible).
- Player nicknames/shorthand → full canonical name: "Big Papi" → David Ortiz; "The Kid" → Ken Griffey; "vladdy"/"vlad jr" → Vladimir Guerrero Jr.; "lg jr" → Luis Garcia Jr. Fix obvious typos in names ("Arron Judge" → Aaron Judge). If you don't recognize a nickname, keep the text as-is rather than guessing.
- Slang stats → standard terms: "dingers"/"bombs"/"jacks"/"taters" → home runs; "Ks"/"punchouts" → strikeouts; "ribbies" → RBI; "free passes" → walks; "batting title race" → batting average leaders.
- Casual time words: "this yr" → this season; "rn"/"right now"/"atm" → this season; "last yr" → last season.
- Drop filler that carries no meaning: "hey", "can you tell me", "i wanna know", "pls", emoji.

TARGET SHAPES — rewrite into the closest of these canonical forms (placeholders in caps):
- PLAYER STAT in YEAR — "Aaron Judge home runs in 2025"
- PLAYER stats in YEAR / PLAYER career stats — "Aaron Judge 2024 stats", "Derek Jeter career stats"
- STAT leaders in YEAR — "home run leaders in {date.today().year}", "OPS leaders in 2019"
- players with N+ STAT in YEAR — "players with 40+ home runs in 2024"
- PLAYER vs lefties/righties in YEAR — "Aaron Judge vs lefties in {date.today().year}"
- PLAYER home/road stats in YEAR — "Aaron Judge home stats in {date.today().year}"
- PLAYER with RISP in YEAR — "Aaron Judge with RISP in {date.today().year}"
- PLAYER in MONTH YEAR — "Aaron Judge in June {date.today().year}"
- compare PLAYER and PLAYER — "compare Aaron Judge and Shohei Ohtani"
- PLAYER longest hitting streak — "Aaron Judge longest hitting streak"
- how is PLAYER doing lately — "how is Aaron Judge doing lately"
- PLAYER vs TEAM — "Aaron Judge vs the Red Sox"
- PLAYER last N games — "Aaron Judge last 10 games"
- TEAM record in YEAR / TEAM team stats in YEAR — "Yankees record in {date.today().year}", "Yankees team stats {date.today().year}"
- pitching versions of all of the above ("Gerrit Cole ERA in {date.today().year}", "ERA leaders in {date.today().year}")

If the question already reads like one of the canonical forms, return it unchanged with "changed": false.
If the question doesn't fit any shape (multi-step analysis, strategy, opinions, trades, schedules, injuries), or you can't rewrite it without guessing, return expressible false.

OUTPUT — strict JSON only, no code fences, no prose:
{{"expressible": true, "changed": true, "canonical": "<rewritten question>"}}
or
{{"expressible": false}}

EXAMPLES:
"AZ diamondbacks stats" → {{"expressible": true, "changed": true, "canonical": "Diamondbacks team stats {date.today().year}"}}
"how yanks doin lately" → {{"expressible": true, "changed": true, "canonical": "how are the Yankees doing this season"}}
"judge dingers this yr" → {{"expressible": true, "changed": true, "canonical": "Aaron Judge home runs in {date.today().year}"}}
"stats for lg jr" → {{"expressible": true, "changed": true, "canonical": "Luis Garcia Jr. stats in {date.today().year}"}}
"who leads the al in ribbies rn" → {{"expressible": true, "changed": true, "canonical": "AL RBI leaders in {date.today().year}"}}
"Aaron Judge home runs in 2025" → {{"expressible": true, "changed": false, "canonical": "Aaron Judge home runs in 2025"}}
"should the mets trade for a closer" → {{"expressible": false}}
"""


async def map_to_canonical(question: str) -> str | None:
    """One Haiku call: wild phrasing → canonical engine phrasing.

    Returns the canonical rewrite, or None when the query is already
    canonical, not expressible, unparseable, or anything errors — None
    always means "proceed with the original question unchanged".
    """
    resp = await _get_client().messages.create(
        model=ROUTING_MODEL,
        max_tokens=200,
        system=MAPPER_PROMPT,
        messages=[{"role": "user", "content": question}],
    )
    text = resp.content[0].text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        logger.info("intent_mapper_bad_json question=%r raw=%r", question, text[:120])
        return None
    if not parsed.get("expressible") or not parsed.get("changed"):
        return None
    canonical = (parsed.get("canonical") or "").strip()
    if not canonical or len(canonical) > 200:
        return None
    if canonical.strip().lower() == question.strip().lower():
        return None
    logger.info("intent_mapper_rewrite original=%r canonical=%r", question, canonical)
    return canonical
