"""
POST /fireside/search-intent — Fireside iOS app's NL search parser.

Claude (Sonnet) parses a natural-language query into a structured intent
JSON. The iOS app then executes the intent deterministically against its
cached Sanity catalog + on-device listening history + cached transcripts.

ISOLATION GUARANTEE:
This router MUST NOT import from services.metering. Fireside data must
never reach StatChat's query_log or admin dashboard. If you need to log
anything, use a Fireside-only mechanism (e.g. add a separate table, or
log on the iOS client).
"""

import logging
import os
from typing import List
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import anthropic

log = logging.getLogger(__name__)

router = APIRouter()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
MODEL = "claude-sonnet-4-6"


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class CatalogHint(BaseModel):
    """Names the iOS app already has cached — used to ground Claude's parsing."""
    series: List[str] = Field(default_factory=list)
    personalities: List[str] = Field(default_factory=list)


class HistoryContext(BaseModel):
    """Lightweight signal about what the user has been listening to.

    iOS sends only series titles + episode titles of recently-played items.
    Backend never sees identifiers, transcripts, or full history.
    """
    recently_played_episode_titles: List[str] = Field(default_factory=list)
    recently_played_series_titles: List[str] = Field(default_factory=list)


class SearchIntentRequest(BaseModel):
    query: str
    catalog_hint: CatalogHint = Field(default_factory=CatalogHint)
    history_context: HistoryContext = Field(default_factory=HistoryContext)


class SearchScope(BaseModel):
    include_history: bool = False
    exclude_history: bool = False
    series_names: List[str] = Field(default_factory=list)
    personality_names: List[str] = Field(default_factory=list)
    use_transcripts: bool = False


class SearchIntent(BaseModel):
    intent: str  # one of: "history", "vibe", "name", "keyword", "anti", "discovery"
    search_terms: List[str] = Field(default_factory=list)
    scope: SearchScope = Field(default_factory=SearchScope)
    ranking_hint: str = ""
    user_intent_summary: str = ""


# ---------------------------------------------------------------------------
# Tool definition — forces Claude to emit a structured intent
# ---------------------------------------------------------------------------

INTENT_TOOL = {
    "name": "return_search_intent",
    "description": (
        "Return the parsed search intent for a Fireside podcast query. "
        "Always call this tool exactly once per request."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "intent": {
                "type": "string",
                "enum": ["history", "vibe", "name", "keyword", "anti", "discovery"],
                "description": (
                    "history = user is recalling something they played before; "
                    "vibe = mood or tone-based ('chill', 'energetic'); "
                    "name = looking for specific series, host, or guest; "
                    "keyword = topical/content search; "
                    "anti = explicitly wants something outside their pattern; "
                    "discovery = open-ended 'show me something new'."
                ),
            },
            "search_terms": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Keywords to match against episode titles, descriptions, "
                    "and transcripts. Empty if intent is purely name-based or anti."
                ),
            },
            "scope": {
                "type": "object",
                "properties": {
                    "include_history": {
                        "type": "boolean",
                        "description": "True for 'what was I listening to' style queries.",
                    },
                    "exclude_history": {
                        "type": "boolean",
                        "description": "True for 'something I haven't heard' / anti-recommendation.",
                    },
                    "series_names": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Series names mentioned in the query. Match against "
                            "the provided catalog_hint.series — return exact names "
                            "from there if a confident match exists."
                        ),
                    },
                    "personality_names": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Host or guest names mentioned. Match against "
                            "catalog_hint.personalities when possible."
                        ),
                    },
                    "use_transcripts": {
                        "type": "boolean",
                        "description": (
                            "True when the query is content-deep enough that "
                            "title+description matching is unlikely to be enough "
                            "(e.g. 'the part where they talked about X')."
                        ),
                    },
                },
                "required": [
                    "include_history", "exclude_history",
                    "series_names", "personality_names", "use_transcripts",
                ],
            },
            "ranking_hint": {
                "type": "string",
                "description": (
                    "One short sentence telling the iOS executor how to rank "
                    "results (e.g. 'prefer recently played episodes mentioning AI')."
                ),
            },
            "user_intent_summary": {
                "type": "string",
                "description": (
                    "One short sentence shown in the UI explaining how we "
                    "interpreted the query (e.g. 'Looking for an AI episode "
                    "you already played'). User-facing copy."
                ),
            },
        },
        "required": ["intent", "search_terms", "scope", "ranking_hint", "user_intent_summary"],
    },
}


def _build_system_prompt(catalog: CatalogHint, history: HistoryContext) -> str:
    series_list = "\n".join(f"  - {s}" for s in catalog.series) or "  (none)"
    personality_list = "\n".join(f"  - {p}" for p in catalog.personalities) or "  (none)"
    recent_eps = "\n".join(f"  - {t}" for t in history.recently_played_episode_titles) or "  (none)"
    recent_series = "\n".join(f"  - {t}" for t in history.recently_played_series_titles) or "  (none)"

    return f"""You are the search intent parser for Fireside, a small-catalog podcast app.

Your only job: take the user's natural-language query and call the
return_search_intent tool exactly once with a structured interpretation.
Never reply in plain text — always use the tool.

CATALOG (small — use these names exactly when matching):
Series:
{series_list}

Personalities (hosts/guests):
{personality_list}

USER'S RECENT LISTENING:
Recently played episodes:
{recent_eps}
Recently played series:
{recent_series}

GUIDELINES:
- "What's that X I was listening to" → intent=history, set include_history=true.
- "What am I up to on [series]" → intent=history, scope.series_names=[matched series], include_history=true.
- "I need a [mood] podcast" → intent=vibe, populate search_terms with mood synonyms ("chill"→["chill","calm","relaxing","mellow"]).
- "Something I wouldn't expect" / "outside my usual" → intent=anti, exclude_history=true.
- Direct mentions of series, host, or guest names → intent=name, populate scope.series_names or scope.personality_names from the catalog above.
- Content/topic queries ("episode about AI") → intent=keyword. Set use_transcripts=true if the topic is unlikely to appear in titles alone.
- If the query is open-ended ("show me something new", "surprise me") → intent=discovery.
- Always populate user_intent_summary as user-facing copy. Keep it short and natural — no jargon.
- Catalog is tiny. Don't invent series or names not in the catalog above. If the user says a name that's not in the catalog, leave scope.series_names / personality_names empty and rely on search_terms instead.
"""


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.post("/search-intent", response_model=SearchIntent)
def search_intent(req: SearchIntentRequest) -> SearchIntent:
    if not ANTHROPIC_API_KEY:
        log.error("fireside.search-intent: ANTHROPIC_API_KEY not set")
        raise HTTPException(status_code=503, detail="search service not configured")

    if not req.query.strip():
        raise HTTPException(status_code=400, detail="query is empty")

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    system_prompt = _build_system_prompt(req.catalog_hint, req.history_context)

    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=system_prompt,
            tools=[INTENT_TOOL],
            tool_choice={"type": "tool", "name": "return_search_intent"},
            messages=[{"role": "user", "content": req.query}],
        )
    except anthropic.APIError as e:
        log.error("fireside.search-intent: anthropic API error: %s", e)
        raise HTTPException(status_code=502, detail="upstream LLM error") from e

    tool_input = None
    for block in resp.content:
        if block.type == "tool_use" and block.name == "return_search_intent":
            tool_input = block.input
            break

    if tool_input is None:
        log.error("fireside.search-intent: no tool_use in response: %s", resp.content)
        raise HTTPException(status_code=502, detail="LLM did not return structured intent")

    try:
        return SearchIntent(**tool_input)
    except Exception as e:
        log.error("fireside.search-intent: invalid tool input: %s — %s", tool_input, e)
        raise HTTPException(status_code=502, detail="LLM returned malformed intent") from e
