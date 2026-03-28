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
    "player_age": "Age",
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
    "innings_pitched": "IP",
    "quality_starts": "QS",
    "era_plus": "ERA+",
    "k_per_9": "K/9",
    "bb_per_9": "BB/9",
    "k_per_bb": "K/BB",
    "hr_per_9": "HR/9",
    "batters_faced": "BF",
    "position": "Pos",
    "bats": "Bats",
    "throws": "Throws",
    "split": "Split",
    # Common Haiku SQL aliases
    "date": "Date",
    "game_date": "Date",
    "opponent": "Opp",
    "vishome": "H/A",
    "career_hr": "HR",
    "career_hits": "H",
    "career_avg": "AVG",
    "career_ops": "OPS",
    "career_era": "ERA",
    "total_games": "G",
    "total_hr": "HR",
    "total_hits": "H",
    "total_sb": "SB",
    "total_k": "SO",
    "total_wins": "W",
    "total_saves": "SV",
    "k_bb_ratio": "K/BB",
    "sb_pct": "SB%",
    "bb_pct": "BB%",
    "k_pct": "K%",
    "multi_hit_games": "Multi-Hit G",
    "multi_hr_games": "Multi-HR G",
    "ops_improvement": "OPS Chg",
    "ops_change": "OPS Chg",
    "ops_diff": "OPS Diff",
    "avg_improvement": "AVG Chg",
    "avg_change": "AVG Chg",
    "era_improvement": "ERA Chg",
    "era_change": "ERA Chg",
    "hr_diff": "HR Diff",
    "april_avg": "Apr AVG",
    "fielding_pct": "FPCT",
    "errors": "E",
    "assists": "A",
    "putouts": "PO",
    "games_started": "GS",
    "complete_games": "CG",
    "shutouts": "SHO",
    "wild_pitches": "WP",
    "balks": "BK",
    "hit_batters": "HB",
    "so": "SO",
    "bb": "BB",
    "h": "H",
    "hr": "HR",
    "g": "G",
    "ab": "AB",
    "r": "R",
    "avg": "AVG",
}


_RATE_3_COLS = {
    "batting_avg", "obp", "slg", "ops", "iso", "babip",
    "avg", "career_avg", "career_ops", "april_avg", "batting_avg_against",
    "fielding_pct", "sb_pct",
}
_RATE_2_COLS = {
    "era", "whip", "k_per_9", "bb_per_9", "hr_per_9", "k_per_bb",
    "k_bb_ratio", "career_era",
}


def _fmt_val(col: str, val) -> str:
    """Format a single value for display."""
    if val is None or val == "NULL" or str(val).strip() == "":
        return "--"
    lower_col = col.lower()
    if lower_col in _RATE_3_COLS:
        try:
            fv = float(val)
            return f".{int(round(fv * 1000)):03d}" if fv < 1 else f"{fv:.3f}"
        except (ValueError, TypeError):
            return str(val)
    if lower_col in _RATE_2_COLS:
        try:
            return f"{float(val):.2f}"
        except (ValueError, TypeError):
            return str(val)
    if lower_col == "ip_outs":
        try:
            outs = int(val)
            return f"{outs // 3}.{outs % 3}"
        except (ValueError, TypeError):
            return str(val)
    # Improvement/change columns — show with sign
    if "improvement" in lower_col or "change" in lower_col or "diff" in lower_col:
        try:
            fv = float(val)
            sign = "+" if fv > 0 else ""
            # If it looks like a rate stat difference (small number)
            if -1 < fv < 1 and fv != 0:
                return f"{sign}{fv:.3f}"
            return f"{sign}{int(round(fv))}" if fv == int(fv) else f"{sign}{fv:.2f}"
        except (ValueError, TypeError):
            return str(val)
    # Generic: try to clean up float formatting
    try:
        fv = float(val)
        if fv == int(fv) and "." not in str(val):
            return str(int(fv))
    except (ValueError, TypeError):
        pass
    return str(val)


def _display_col_name(col: str) -> str:
    """Convert a SQL column name to a display name."""
    # Check exact match
    if col in _COL_DISPLAY:
        return _COL_DISPLAY[col]
    # Check case-insensitive
    lower = col.lower()
    if lower in _COL_DISPLAY:
        return _COL_DISPLAY[lower]
    # Clean up common SQL alias patterns: snake_case → Title Case
    cleaned = col.replace("_", " ").strip()
    # Short names (<=4 chars) → uppercase (likely abbreviations)
    if len(cleaned) <= 4:
        return cleaned.upper()
    return cleaned.title()


def _format_haiku_result(result_text: str) -> str:
    """
    Convert SqlRunner pipe-delimited output into [LEADERBOARD] or [STATGRID] format.
    Multi-row results with names → [LEADERBOARD] with rank numbers.
    Single-row or aggregate → [STATGRID].
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
    label_cols = set()
    if has_name:
        label_cols.add("name")
    # "season" stays as a stat column unless it's purely a label
    # (i.e., every row is the same season → label, mixed → stat)
    if has_season and multi_row:
        seasons = set(row.get("season", "") for row in data_rows if row.get("season"))
        if len(seasons) > 1:
            pass  # Keep season as a stat column (mixed years)
        else:
            label_cols.add("season")
    elif has_season and not multi_row:
        label_cols.add("season")
    # player_id is always a label
    if "player_id" in columns:
        label_cols.add("player_id")

    stat_cols = [c for c in columns if c not in label_cols]

    # Remove team from stat_cols if we'll use it in the label
    use_team_in_label = "team" in stat_cols and has_name
    if use_team_in_label:
        stat_cols = [c for c in stat_cols if c != "team"]

    if not stat_cols:
        return result_text

    # Build header
    header_names = [_display_col_name(c) for c in stat_cols]
    header = f"HEADER: {', '.join(header_names)}"

    # Use [LEADERBOARD] for multi-row ranked results, [STATGRID] for single/aggregate
    use_leaderboard = multi_row and has_name

    # Build rows
    row_lines = []
    for i, row in enumerate(data_rows):
        # Build label
        label_parts = []
        if has_name:
            name = row.get("name", "")
            team = f" ({row.get('team', '')})" if use_team_in_label and row.get("team") else ""
            label_parts.append(f"{name}{team}")
        if "season" in label_cols and has_season:
            label_parts.append(str(row.get("season", "")))

        label = ", ".join(label_parts) if label_parts else ""

        # Build values
        vals = [_fmt_val(c, row.get(c)) for c in stat_cols]

        if use_leaderboard:
            row_lines.append(f"ROW {i+1}. {label}: {', '.join(vals)}")
        elif label:
            row_lines.append(f"ROW: {label}, {', '.join(vals)}")
        else:
            row_lines.append(f"ROW: {', '.join(vals)}")

    if use_leaderboard:
        parts = []
        parts.append("[TIP]Tap a player name for their full profile.[/TIP]")
        parts.append("[LEADERBOARD]")
        parts.append(header)
        parts.extend(row_lines)
        parts.append("[/LEADERBOARD]")
        return "\n".join(parts)
    else:
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

    if not sql or "OFF_TOPIC" in sql or "NO_DATA" in sql or "NEEDS_CONTEXT" in sql:
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
            if not sql or "OFF_TOPIC" in sql or "NO_DATA" in sql or "NEEDS_CONTEXT" in sql:
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
