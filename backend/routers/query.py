"""
POST /query — the main endpoint.

Accepts a question + device_id + conversation history.
Returns a Server-Sent Events stream.

SSE event format:
  data: {"type": "text", "text": "..."}        ← streaming answer chunk
  data: {"type": "done"}                        ← finished successfully
  data: {"type": "error", "message": "..."}    ← error, stream ends
  data: {"type": "quota_exceeded", "count": N, "reset": "YYYY-MM-DD"}
"""

import json
import asyncio
import logging

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from services.llm import LLMService
from services.sql_runner import SqlRunner
from services.metering import check_quota, increment_count
from services.interceptor import try_intercept

logger = logging.getLogger("statchat.query")

router = APIRouter()
llm = LLMService()
runner = SqlRunner()


class QueryRequest(BaseModel):
    question: str
    device_id: str
    history: list[dict] = []  # [{role: "user"|"assistant", content: "..."}]
    contextual: bool = False  # True when iOS sends an enriched contextual follow-up prompt


@router.post("/query")
async def query(req: QueryRequest):
    return StreamingResponse(
        _stream(req.question, req.device_id, req.history, req.contextual),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _stream(question: str, device_id: str, history: list[dict], contextual: bool = False):
    """Core pipeline: quota check → route → SQL → execute → stream answer."""

    def event(data: dict) -> str:
        return f"data: {json.dumps(data)}\n\n"

    # 1. Quota check
    quota = check_quota(device_id)
    if not quota["allowed"]:
        yield event({
            "type": "quota_exceeded",
            "count": quota["count"],
            "reset": quota["reset"],
        })
        return

    # 1a. Contextual follow-up — iOS already built a self-contained prompt with
    # the original question, results, and follow-up. Skip SQL generation and let
    # Claude answer directly (these are analytical, not data-retrieval questions).
    if contextual:
        logger.info("query_contextual question_len=%d", len(question))
        try:
            async for chunk in llm.stream_contextual(question):
                yield event({"type": "text", "text": chunk})
        except Exception as e:
            yield event({"type": "error", "message": str(e)})
            return
        yield event({"type": "done"})
        increment_count(device_id)
        return

    # 2. Try local intercept first — zero Claude API cost
    try:
        intercepted = try_intercept(question)
    except Exception as e:
        logger.warning("intercept_error question=%r error=%s", question, e)
        intercepted = None
    if intercepted is not None:
        logger.info("query_intercepted question=%r", question)
        yield event({"type": "text", "text": intercepted})
        yield event({"type": "done", "intercepted": True})
        increment_count(device_id)
        return

    # 3. Route the question (falls through to Claude)
    logger.info("query_to_claude question=%r", question)
    try:
        route = await llm.route_query(question, history)
    except Exception as e:
        yield event({"type": "error", "message": f"Routing error: {e}"})
        return

    # 3a. Stat explanation — no SQL needed
    if route == "stat_explanation":
        try:
            answer = await llm.explain_stat(question)
            yield event({"type": "text", "text": answer})
            yield event({"type": "done"})
            increment_count(device_id)
        except Exception as e:
            yield event({"type": "error", "message": str(e)})
        return

    # 3b. Generate SQL (routing, simple_lookup, streak_finder, current_form all go here)
    try:
        sql = await llm.generate_sql(question, history)
    except Exception as e:
        yield event({"type": "error", "message": f"SQL generation error: {e}"})
        return

    if "OFF_TOPIC" in sql:
        yield event({"type": "text", "text": "I'm a baseball stats engine — ask me about player stats, leaders, averages, and more!"})
        yield event({"type": "done"})
        increment_count(device_id)
        return

    if "NO_DATA" in sql:
        # Let Claude explain what the stat is and suggest alternatives
        no_data_result = "NO_DATA — this stat is not stored in our database and cannot be derived from available columns."
        try:
            async for chunk in llm.stream_answer(question, sql, no_data_result, history):
                yield event({"type": "text", "text": chunk})
        except Exception as e:
            yield event({"type": "text", "text": "I don't have data for that stat in my database. Try asking about batting stats, pitching stats, or streaks from 2016–2025."})
        yield event({"type": "done"})
        increment_count(device_id)
        return

    # 4. Execute SQL (blocking SQLite call → thread pool)
    try:
        loop = asyncio.get_event_loop()
        result_text, is_streak = await loop.run_in_executor(
            None, runner.execute_and_format, sql
        )
    except RuntimeError as e:
        yield event({"type": "error", "message": f"I had trouble with that query. Could you rephrase? (SQL error: {e})"})
        return

    # 5. Stream the answer
    try:
        async for chunk in llm.stream_answer(question, sql, result_text, history, is_streak=is_streak):
            yield event({"type": "text", "text": chunk})
    except Exception as e:
        yield event({"type": "error", "message": str(e)})
        return

    yield event({"type": "done"})
    increment_count(device_id)
