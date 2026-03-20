"""
Async Anthropic wrapper for the StatChat backend.

Uses:
- Haiku for cheap query routing
- Sonnet for SQL generation and answer generation
- Prompt caching on SQL gen + answer gen system prompts (90% input cost discount)
- Streaming for the final answer so iOS gets a typewriter effect
"""

import os
import re
import json

import anthropic

from prompts import (
    ROUTING_PROMPT,
    SQL_GENERATION_PROMPT,
    ANSWER_GENERATION_PROMPT,
    STREAK_ANSWER_PROMPT,
    STAT_EXPLANATION_PROMPT,
    FOLLOWUP_CLASSIFY_PROMPT,
)

ROUTING_MODEL = os.getenv("ROUTING_MODEL", "claude-haiku-4-5-20251001")
MAIN_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5-20250929")
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
        response = await self.client.messages.create(
            model=ROUTING_MODEL,
            max_tokens=256,
            system=FOLLOWUP_CLASSIFY_PROMPT,
            messages=msgs,
        )
        text = response.content[0].text.strip()
        try:
            result = json.loads(text)
            if result.get("type") == "data" and result.get("rewritten"):
                return result
            elif result.get("type") == "analytical":
                return {"type": "analytical"}
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

    async def explain_stat(self, question: str) -> str:
        """Answer a stat explanation question directly — no SQL needed."""
        response = await self.client.messages.create(
            model=MAIN_MODEL,
            max_tokens=512,
            system=STAT_EXPLANATION_PROMPT,
            messages=[{"role": "user", "content": question}],
        )
        return response.content[0].text.strip()
