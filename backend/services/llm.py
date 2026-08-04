"""
Async Anthropic wrapper for the StatChat backend.

Uses:
- Haiku for cheap query routing
- Sonnet for SQL generation and answer generation
- Prompt caching on SQL gen + answer gen system prompts (90% input cost discount)
- Streaming for the final answer so iOS gets a typewriter effect
"""

import logging
import os
import re
import json

import anthropic

logger = logging.getLogger("statchat.llm")

from prompts import (
    ROUTING_PROMPT,
    SQL_GENERATION_PROMPT,
    HAIKU_SQL_PROMPT,
    ANSWER_GENERATION_PROMPT,
    STREAK_ANSWER_PROMPT,
    STAT_EXPLANATION_PROMPT,
    FOLLOWUP_CLASSIFY_PROMPT,
    KNOWLEDGE_MODE_PROMPT,
)

# Model selection per the Aug 2026 benchmark (see memory: model-selection-
# benchmark-2026-08): Sonnet 5 for multi-round reasoning + narrative tiers
# (sql_planner, knowledge mode, analytical/contextual answers — 30-40% faster
# than Sonnet 4.5 at the same price); Haiku 4.5 stays for single-shot SQL
# generation and classification (3× faster and 4× cheaper than Sonnet there,
# with zero quality gain from upgrading). sql_planner imports MAIN_MODEL from
# here — this is the single source of truth for model IDs.
ROUTING_MODEL = os.getenv("ROUTING_MODEL", "claude-haiku-4-5-20251001")
MAIN_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5")
_CACHE_BETA = "prompt-caching-2024-07-31"


def _cached_system(prompt: str) -> list[dict]:
    """Wrap a prompt string as a cached system content block."""
    return [{"type": "text", "text": prompt, "cache_control": {"type": "ephemeral"}}]


def _build_messages(content: str, history: list[dict]) -> list[dict]:
    """
    Append `content` as the latest user message to the conversation history.
    History should already be in [{role, content}, ...] format.
    We keep the last 10 messages (5 exchanges) for context.
    """
    messages = list(history[-10:])
    messages.append({"role": "user", "content": content})
    return messages


class LLMService:
    def __init__(self) -> None:
        self.client = anthropic.AsyncAnthropic()

    async def route_query(self, question: str, history: list[dict]) -> str:
        """Classify the question. Returns a route string like 'simple_lookup'."""
        msgs = _build_messages(question, history)
        response = await self.client.messages.create(
            model=ROUTING_MODEL,
            max_tokens=256,
            system=ROUTING_PROMPT,
            messages=msgs,
        )
        text = response.content[0].text.strip()
        try:
            return json.loads(text).get("type", "simple_lookup")
        except (json.JSONDecodeError, AttributeError):
            return "simple_lookup"

    async def generate_sql(self, question: str, history: list[dict]) -> str:
        """Translate a question into SQL. Returns raw SQL string."""
        msgs = _build_messages(question, history)
        response = await self.client.messages.create(
            model=MAIN_MODEL,
            max_tokens=1024,
            system=_cached_system(SQL_GENERATION_PROMPT),
            extra_headers={"anthropic-beta": _CACHE_BETA},
            messages=msgs,
        )
        sql = response.content[0].text.strip()
        # Strip markdown code fences and Python-style comments
        sql = re.sub(r"^```(?:sql)?\s*", "", sql)
        sql = re.sub(r"\s*```$", "", sql)
        sql = re.sub(r"#[^\n]*", "", sql)
        return sql.strip()

    async def generate_sql_haiku(self, question: str) -> str:
        """Generate SQL using Haiku (cheap fallback before Sonnet)."""
        response = await self.client.messages.create(
            model=ROUTING_MODEL,
            max_tokens=1024,
            system=_cached_system(HAIKU_SQL_PROMPT),
            extra_headers={"anthropic-beta": _CACHE_BETA},
            messages=[{"role": "user", "content": question}],
        )
        sql = response.content[0].text.strip()
        sql = re.sub(r"^```(?:sql)?\s*", "", sql)
        sql = re.sub(r"\s*```$", "", sql)
        sql = re.sub(r"#[^\n]*", "", sql)
        return sql.strip()

    async def stream_answer(
        self,
        question: str,
        sql: str,
        results: str,
        history: list[dict],
        is_streak: bool = False,
    ):
        """
        Stream the final natural-language answer.
        Yields text chunks as they arrive from the model.
        """
        system = STREAK_ANSWER_PROMPT if is_streak else ANSWER_GENERATION_PROMPT
        content = f"Question: {question}\n\nSQL executed: {sql}\n\nResults:\n{results}"
        msgs = _build_messages(content, history)

        async with self.client.messages.stream(
            model=MAIN_MODEL,
            max_tokens=1024,
            system=_cached_system(system),
            extra_headers={"anthropic-beta": _CACHE_BETA},
            messages=msgs,
        ) as stream:
            async for text in stream.text_stream:
                yield text

    async def stream_contextual(self, prompt: str):
        """
        Stream a response for a contextual follow-up.
        The prompt already contains the original question, results, and follow-up.
        """
        msgs = [{"role": "user", "content": prompt}]
        async with self.client.messages.stream(
            model=MAIN_MODEL,
            max_tokens=1024,
            system=_cached_system(ANSWER_GENERATION_PROMPT),
            extra_headers={"anthropic-beta": _CACHE_BETA},
            messages=msgs,
        ) as stream:
            async for text in stream.text_stream:
                yield text

    async def classify_followup(self, question: str, history: list[dict]) -> dict:
        """Classify a follow-up as 'data' or 'analytical' and rewrite data queries."""
        msgs = _build_messages(question, history)
        logger.info("followup_messages history_len=%d question=%r", len(history), question)
        response = await self.client.messages.create(
            model=ROUTING_MODEL,
            max_tokens=256,
            system=FOLLOWUP_CLASSIFY_PROMPT,
            messages=msgs,
        )
        text = response.content[0].text.strip()
        # Strip markdown code fences if present
        text = re.sub(r'^```(?:json)?\s*', '', text)
        text = re.sub(r'\s*```$', '', text)
        text = text.strip()
        logger.info("followup_raw_response text=%r", text)
        try:
            result = json.loads(text)
            if result.get("type") == "data" and result.get("rewritten"):
                return result
            elif result.get("type") == "analytical":
                return {"type": "analytical"}
            elif result.get("type") == "feedback":
                return {"type": "feedback"}
            return {"type": "data", "rewritten": question}
        except (json.JSONDecodeError, AttributeError):
            return {"type": "data", "rewritten": question}

    async def stream_analytical(self, question: str, history: list[dict]):
        """Stream a response for an analytical follow-up using conversation context."""
        msgs = _build_messages(question, history)
        async with self.client.messages.stream(
            model=MAIN_MODEL,
            max_tokens=1024,
            system=_cached_system(ANSWER_GENERATION_PROMPT),
            extra_headers={"anthropic-beta": _CACHE_BETA},
            messages=msgs,
        ) as stream:
            async for text in stream.text_stream:
                yield text

    async def stream_knowledge(self, question: str, history: list[dict],
                               data_note: str | None = None):
        """Stream a response from Claude's own baseball knowledge — no SQL, no DB.

        data_note: when a bail gate skipped the data tiers, the precise reason
        ("we don't store late-and-close splits") so the narration acknowledges
        the specific limitation instead of hedging generically."""
        system = KNOWLEDGE_MODE_PROMPT
        if data_note:
            system = (KNOWLEDGE_MODE_PROMPT
                      + f"\n\nDATA NOTE: {data_note} Acknowledge this limitation "
                        "naturally in one short clause, then answer from your "
                        "general baseball knowledge.")
        msgs = _build_messages(question, history)
        async with self.client.messages.stream(
            model=MAIN_MODEL,
            max_tokens=1024,
            system=system,
            messages=msgs,
        ) as stream:
            async for text in stream.text_stream:
                yield text

    async def explain_stat(self, question: str) -> str:
        """Answer a stat explanation question directly — no SQL needed."""
        response = await self.client.messages.create(
            model=MAIN_MODEL,
            max_tokens=512,
            system=STAT_EXPLANATION_PROMPT,
            messages=[{"role": "user", "content": question}],
        )
        return response.content[0].text.strip()
