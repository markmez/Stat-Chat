"""
Sonnet SQL Planner — multi-step reasoning for complex queries.

Uses Claude tool use to generate and execute SQL queries against our DB,
chaining results until it can answer the question. Single API call with
tool results fed back in the same conversation.

Cost: ~$0.02-0.03 per query. Only called for queries that miss the
interceptor, query engine, and Haiku SQL.
"""

import json
import logging
import os
import sqlite3
from datetime import date

import requests

from schema_description import SCHEMA_DESCRIPTION
from services.metering import log_server_error

logger = logging.getLogger("statchat.insight_engine")


def _log_err(source: str, error_type: str, msg: str, question: str, extra: dict | None = None):
    """Mirror insight engine failures to the dashboard so we can diagnose from here."""
    try:
        ctx = {"question": question[:200]}
        if extra:
            ctx.update(extra)
        log_server_error(source=source, error_type=error_type,
                         error_message=msg, context=ctx)
    except Exception:
        pass

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
DB_PATH = os.getenv("DB_PATH", "/data/baseball_stats_full.db")

TOOL_DEF = {
    "name": "execute_sql",
    "description": (
        "Execute a read-only SQL query against the baseball stats SQLite database. "
        "Returns up to 50 rows. Use this to look up stats, find players, check "
        "schedules, and answer questions with real data."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "sql": {
                "type": "string",
                "description": "The SQL SELECT query to execute.",
            },
            "purpose": {
                "type": "string",
                "description": "Brief description of what this query is looking up.",
            },
        },
        "required": ["sql", "purpose"],
    },
}

SYSTEM_PROMPT = f"""You are a baseball stats analyst with access to a SQLite database.
Today is {date.today().isoformat()}. The current MLB season is {date.today().year}.

DATABASE SCHEMA:
{SCHEMA_DESCRIPTION}

RULES:
1. Use the execute_sql tool to query the database. You can make multiple queries.
2. Only use SELECT statements — no modifications.
3. Use actual column and table names from the schema above.
4. The current season is {date.today().year}. Season started late March.
5. Team codes are Retrosheet format (NYA=Yankees, LAN=Dodgers, etc.).
6. For "active roster" approximation: players with a game_batting_log or game_pitching_log entry in the last 14 days.
7. Keep queries efficient — use LIMIT, avoid full table scans.
8. After gathering data, provide a concise, natural answer with specific numbers.
9. If the data isn't in the database, answer from your baseball knowledge instead. Do NOT explain why the database can't answer — the user doesn't know or care about tables, columns, or schemas. Just answer the question naturally.
10. NEVER mention the database, SQL, data sources, table names, column names, or any implementation details. You're a baseball expert talking to a fan.
11. Format your final answer for a mobile app feed — concise, no markdown headers, just clean text with player names and numbers.
12. Do NOT invent or hallucinate any statistics. Every number must come from a query result or well-established baseball fact.
"""

MAX_TOOL_ROUNDS = 8  # prevent runaway query chains


def plan_and_execute(question: str) -> dict | None:
    """Run the insight engine on a question.

    Returns one of:
      None — engine failed (no API key, max rounds, exception, empty answer)
      dict with shape:
        {
          "text": str           — Sonnet's natural-language answer
          "columns": list[str]  — column names of the primary tool_result, or None
          "rows": list[list]    — rows of the primary tool_result, or None
        }

    "Primary tool_result" is the largest successful tool_result by row count.
    Ties broken by recency. Lookup-helper queries (single-row scalar SELECTs
    that resolve a player_id, a team code, etc.) naturally lose this race
    to the actual answer query, which usually returns the most rows.
    Callers can pass `columns`/`rows` through the shared row-to-grid
    formatter to render a [STATGRID]/[LEADERBOARD] above Sonnet's prose
    when the data is gridable; if the formatter returns None (ungridable
    shape — definitions, opinions, narrative answers), prose alone is
    used and nothing is lost.
    """
    if not ANTHROPIC_API_KEY:
        logger.warning("insight_engine: no API key")
        _log_err("insight_engine", "no_api_key",
                 "ANTHROPIC_API_KEY env var not set on server", question)
        return None

    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA query_only = ON")

    # Track tool results across rounds so we can promote the "primary"
    # one (most rows) into the structured-output channel. Each entry:
    # {"columns": [...], "rows": [[...], ...]}
    tool_results_history: list[dict] = []

    messages = [{"role": "user", "content": question}]

    try:
        for round_num in range(MAX_TOOL_ROUNDS):
            # Call Sonnet
            resp = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-sonnet-4-6",
                    "max_tokens": 4000,
                    "system": SYSTEM_PROMPT,
                    "tools": [TOOL_DEF],
                    "messages": messages,
                },
                timeout=60,
            )

            if resp.status_code != 200:
                logger.error("insight_engine: API error %s: %s", resp.status_code, resp.text[:200])
                _log_err("insight_engine", f"api_status_{resp.status_code}",
                         resp.text[:500], question,
                         {"round": round_num + 1, "model": "claude-sonnet-4-6"})
                return None

            result = resp.json()
            stop_reason = result.get("stop_reason")
            content = result.get("content", [])

            if stop_reason == "end_turn":
                # Final answer — extract text
                text_parts = [c["text"] for c in content if c.get("type") == "text"]
                answer = "\n".join(text_parts).strip()
                if answer:
                    logger.info("insight_engine: answered in %d rounds", round_num + 1)
                    # Pick "primary" tool result: largest by row count, ties
                    # broken by recency. Returns None if no eligible result
                    # was captured (pure narrative / definitional answers
                    # where Sonnet ended without DB queries, or only
                    # ran lookup-shaped queries we filtered out).
                    primary: dict | None = None
                    if tool_results_history:
                        primary = max(
                            tool_results_history,
                            key=lambda tr: (len(tr["rows"]),
                                            tool_results_history.index(tr)),
                        )
                    return {
                        "text": answer,
                        "columns": primary["columns"] if primary else None,
                        "rows": primary["rows"] if primary else None,
                    }
                _log_err("insight_engine", "empty_answer",
                         "end_turn with no text content", question,
                         {"round": round_num + 1})
                return None

            if stop_reason == "tool_use":
                # Execute each tool call
                tool_results = []
                for block in content:
                    if block.get("type") == "tool_use":
                        tool_id = block["id"]
                        sql = block["input"].get("sql", "")
                        purpose = block["input"].get("purpose", "")
                        logger.info("insight_engine: step %d — %s", round_num + 1, purpose)

                        # Safety: only allow SELECT
                        if not sql.strip().upper().startswith("SELECT"):
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": tool_id,
                                "content": json.dumps({"error": "Only SELECT queries allowed"}),
                            })
                            continue

                        try:
                            rows = conn.execute(sql).fetchmany(50)
                            # Get column names
                            cols = [desc[0] for desc in conn.execute(sql).description] if rows else []
                            result_data = {
                                "columns": cols,
                                "rows": [list(r) for r in rows],
                                "count": len(rows),
                            }
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": tool_id,
                                "content": json.dumps(result_data, default=str),
                            })
                            # Stash for primary-result selection at end.
                            # Skip empty results (zero-row lookups) and
                            # NULL/None-only single-cell aggregates that
                            # represent "no data found" — those aren't
                            # gridable and shouldn't compete with real
                            # answer rows.
                            if cols and rows:
                                if len(rows) == 1 and len(cols) == 1 and rows[0][0] is None:
                                    pass
                                else:
                                    tool_results_history.append({
                                        "columns": cols,
                                        "rows": [list(r) for r in rows],
                                    })
                        except Exception as e:
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": tool_id,
                                "content": json.dumps({"error": str(e)}),
                            })

                # Add assistant message + tool results to conversation
                messages.append({"role": "assistant", "content": content})
                messages.append({"role": "user", "content": tool_results})
            else:
                logger.warning("insight_engine: unexpected stop_reason=%s", stop_reason)
                _log_err("insight_engine", "unexpected_stop_reason",
                         str(stop_reason), question, {"round": round_num + 1})
                return None

        logger.warning("insight_engine: hit max rounds (%d)", MAX_TOOL_ROUNDS)
        _log_err("insight_engine", "max_rounds",
                 f"hit {MAX_TOOL_ROUNDS} tool rounds without end_turn", question)
        return None

    except Exception as e:
        import traceback as _tb
        tb_str = _tb.format_exc()
        logger.error("insight_engine: error %s\n%s", e, tb_str)
        _log_err("insight_engine", type(e).__name__, str(e), question,
                 {"traceback": tb_str[-1500:]})
        return None
    finally:
        conn.close()
