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


# Column name → display abbreviation mapping
_COL_DISPLAY = {
    "name": None,  # Used as row label, not a stat column
    "player_id": None,
    "season": None,  # Used as row label when present
    "team": "Team",
    "age": "Age",
    "games": "G",
    "plate_appearances": "PA",
    "at_bats": "AB",
    "hits": "H",
    "doubles": "2B",
    "triples": "3B",
    "home_runs": "HR",
    "runs": "R",
    "rbi": "RBI",
    "stolen_bases": "SB",
    "caught_stealing": "CS",
    "walks": "BB",
    "strikeouts": "SO",
    "hit_by_pitch": "HBP",
    "sacrifice_flies": "SF",
    "intentional_walks": "IBB",
    "batting_avg": "AVG",
    "obp": "OBP",
    "slg": "SLG",
    "ops": "OPS",
    "ops_plus": "OPS+",
    "iso": "ISO",
    "babip": "BABIP",
    "era": "ERA",
    "whip": "WHIP",
    "wins": "W",
    "losses": "L",
    "saves": "SV",
    "earned_runs": "ER",
    "ip_outs": "IP",
    "quality_starts": "QS",
    "era_plus": "ERA+",
    "k_per_9": "K/9",
    "bb_per_9": "BB/9",
    "hr_per_9": "HR/9",
    "batters_faced": "BF",
    "position": "Pos",
    "bats": "Bats",
    "throws": "Throws",
    "split": "Split",
}


def _fmt_val(col: str, val) -> str:
    """Format a single value for display."""
    if val is None or val == "NULL":
        return "--"
    if col in ("batting_avg", "obp", "slg", "ops", "iso", "babip"):
        try:
            return f".{int(round(float(val) * 1000)):03d}" if float(val) < 1 else f"{float(val):.3f}"
        except (ValueError, TypeError):
            return str(val)
    if col in ("era", "whip", "k_per_9", "bb_per_9", "hr_per_9"):
        try:
            return f"{float(val):.2f}"
        except (ValueError, TypeError):
            return str(val)
    if col == "ip_outs":
        try:
            outs = int(val)
            return f"{outs // 3}.{outs % 3}"
        except (ValueError, TypeError):
            return str(val)
    return str(val)


def _format_haiku_result(result_text: str) -> str:
    """
    Convert SqlRunner pipe-delimited output into [STATGRID] format.
    Handles single rows, multi-row leaderboards, and aggregates.
    """
    lines = result_text.strip().split("\n")
    if len(lines) < 3:  # header + separator + at least one data row
        return result_text

    columns = [c.strip() for c in lines[0].split("|")]
    data_rows = []
    for line in lines[2:]:  # skip header + separator
        vals = [v.strip() for v in line.split("|")]
        if len(vals) == len(columns):
            data_rows.append(dict(zip(columns, vals)))

    if not data_rows:
        return result_text

    # Determine row label: name, season, or numbered
    has_name = "name" in columns
    has_season = "season" in columns
    multi_row = len(data_rows) > 1

    # Pick stat columns (everything that's not a label)
    stat_cols = []
    for c in columns:
        display = _COL_DISPLAY.get(c, c)  # Use raw name if not in mapping
        if display is None:  # Skip label columns
            continue
        stat_cols.append(c)

    if not stat_cols:
        return result_text

    # Build header
    header_names = [_COL_DISPLAY.get(c, c) for c in stat_cols]
    header = f"HEADER: {', '.join(header_names)}"

    # Build rows
    row_lines = []
    for i, row in enumerate(data_rows):
        # Build label
        label_parts = []
        if multi_row and has_name:
            rank = f"#{i+1} " if len(data_rows) > 2 else ""
            name = row.get("name", "")
            team = f" ({row.get('team', '')})" if "team" in row and row.get("team") and "team" not in stat_cols else ""
            label_parts.append(f"{rank}{name}{team}")
        elif has_name:
            label_parts.append(row.get("name", ""))
        if has_season and "season" not in stat_cols:
            label_parts.append(str(row.get("season", "")))

        label = ", ".join(label_parts) if label_parts else ""

        # Build values
        vals = [_fmt_val(c, row.get(c)) for c in stat_cols]
        if label:
            row_lines.append(f"ROW: {label}, {', '.join(vals)}")
        else:
            row_lines.append(f"ROW: {', '.join(vals)}")

    grid = f"[STATGRID]\n{header}\n" + "\n".join(row_lines) + "\n[/STATGRID]"
    return grid


async def _try_haiku_sql(question: str):
    """
    Haiku SQL fallback: generate SQL with Haiku, execute it.
    Returns (sql, result_text, is_streak) tuple, or None to fall through to Sonnet.
    Retries once on SQL error (sends error back to Haiku).
    """
    try:
        sql = await llm.generate_sql_haiku(question)
    except Exception as e:
        logger.warning("haiku_sql_gen_error error=%s", e)
        return None

    if not sql or "OFF_TOPIC" in sql or "NO_DATA" in sql:
        return None

    # First attempt
    loop = asyncio.get_event_loop()
    try:
        result_text, is_streak = await loop.run_in_executor(
            None, runner.execute_and_format, sql
        )
    except RuntimeError as e:
        # SQL error — retry once with the error context
        logger.info("haiku_sql_retry error=%s", e)
        try:
            retry_prompt = f"Previous SQL failed with error: {e}\n\nOriginal question: {question}\n\nFix the SQL query."
            sql = await llm.generate_sql_haiku(retry_prompt)
            if not sql or "OFF_TOPIC" in sql or "NO_DATA" in sql:
                return None
            result_text, is_streak = await loop.run_in_executor(
                None, runner.execute_and_format, sql
            )
        except Exception:
            return None  # Both attempts failed, fall through to Sonnet

    if result_text == "No results found.":
        return None  # Empty results — let Sonnet try, it might interpret differently

    return sql, result_text, is_streak


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

    # 2a. Follow-up rewrite — if history is present and question is short,
    # use Haiku to classify as data (rewrite) or analytical (reason about prior answer).
    rewritten_query: str | None = None
    if history and len(question.split()) < 10:
        logger.info("followup_classify question=%r", question)
        try:
            classification = await llm.classify_followup(question, history)
        except Exception as e:
            logger.warning("followup_classify_error error=%s", e)
            classification = {"type": "data", "rewritten": question}

        if classification["type"] == "data":
            rewritten = classification.get("rewritten", question)
            if rewritten != question:
                rewritten_query = rewritten
                logger.info("followup_rewritten original=%r rewritten=%r", question, rewritten)
                # Try interceptor with the rewritten question
                try:
                    intercepted = try_intercept(rewritten)
                except Exception as e:
                    logger.warning("intercept_rewrite_error error=%s", e)
                    intercepted = None
                if intercepted is not None:
                    logger.info("followup_intercepted rewritten=%r", rewritten)
                    yield event({"type": "text", "text": intercepted})
                    done_event = {"type": "done", "intercepted": True}
                    if rewritten_query:
                        done_event["rewritten_query"] = rewritten_query
                    yield event(done_event)
                    increment_count(device_id)
                    return
            # Use rewritten question for the rest of the pipeline
            question = rewritten

        elif classification["type"] == "analytical":
            logger.info("followup_analytical question=%r", question)
            try:
                async for chunk in llm.stream_analytical(question, history):
                    yield event({"type": "text", "text": chunk})
            except Exception as e:
                yield event({"type": "error", "message": str(e)})
                return
            # Analytical follow-ups don't get rewritten queries in history
            yield event({"type": "done"})
            increment_count(device_id)
            return

    # 3. Haiku SQL fallback — cheap SQL generation, no Sonnet needed
    haiku_result = await _try_haiku_sql(question)
    if haiku_result is not None:
        haiku_sql, haiku_result_text, haiku_is_streak = haiku_result
        logger.info("query_haiku_sql question=%r", question)
        formatted = _format_haiku_result(haiku_result_text)
        yield event({"type": "text", "text": formatted})
        done_event = {"type": "done", "haiku_sql": True}
        if rewritten_query:
            done_event["rewritten_query"] = rewritten_query
        yield event(done_event)
        increment_count(device_id)
        return

    # 4. Route the question (falls through to Claude Sonnet)
    logger.info("query_to_claude question=%r", question)
    try:
        route = await llm.route_query(question, history)
    except Exception as e:
        yield event({"type": "error", "message": f"Routing error: {e}"})
        return

    # 4a. Stat explanation — no SQL needed
    if route == "stat_explanation":
        try:
            answer = await llm.explain_stat(question)
            yield event({"type": "text", "text": answer})
            yield event({"type": "done"})
            increment_count(device_id)
        except Exception as e:
            yield event({"type": "error", "message": str(e)})
        return

    # 4b. Generate SQL (routing, simple_lookup, streak_finder, current_form all go here)
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

    # 5. Execute SQL (blocking SQLite call → thread pool)
    try:
        loop = asyncio.get_event_loop()
        result_text, is_streak = await loop.run_in_executor(
            None, runner.execute_and_format, sql
        )
    except RuntimeError as e:
        yield event({"type": "error", "message": f"I had trouble with that query. Could you rephrase? (SQL error: {e})"})
        return

    # 6. Stream the answer
    try:
        async for chunk in llm.stream_answer(question, sql, result_text, history, is_streak=is_streak):
            yield event({"type": "text", "text": chunk})
    except Exception as e:
        yield event({"type": "error", "message": str(e)})
        return

    done_event: dict = {"type": "done"}
    if rewritten_query:
        done_event["rewritten_query"] = rewritten_query
    yield event(done_event)
    increment_count(device_id)
