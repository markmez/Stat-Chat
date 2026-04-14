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

logger = logging.getLogger("statchat.sql_planner")

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
9. If you can't find data for part of the question, say so clearly and answer what you can.
10. Format your final answer for a mobile app feed — concise, no markdown headers, just clean text with player names and numbers.
11. Do NOT invent or hallucinate any statistics. Every number must come from a query result.
"""

MAX_TOOL_ROUNDS = 8  # prevent runaway query chains


def plan_and_execute(question: str) -> str | None:
    """Run the Sonnet SQL planner on a question.

    Returns the formatted answer text, or None if the planner fails.
    """
    if not ANTHROPIC_API_KEY:
        logger.warning("sql_planner: no API key")
        return None

    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA query_only = ON")

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
                logger.error("sql_planner: API error %s: %s", resp.status_code, resp.text[:200])
                return None

            result = resp.json()
            stop_reason = result.get("stop_reason")
            content = result.get("content", [])

            if stop_reason == "end_turn":
                # Final answer — extract text
                text_parts = [c["text"] for c in content if c.get("type") == "text"]
                answer = "\n".join(text_parts).strip()
                if answer:
                    logger.info("sql_planner: answered in %d rounds", round_num + 1)
                    return answer
                return None

            if stop_reason == "tool_use":
                # Execute each tool call
                tool_results = []
                for block in content:
                    if block.get("type") == "tool_use":
                        tool_id = block["id"]
                        sql = block["input"].get("sql", "")
                        purpose = block["input"].get("purpose", "")
                        logger.info("sql_planner: step %d — %s", round_num + 1, purpose)

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
                logger.warning("sql_planner: unexpected stop_reason=%s", stop_reason)
                return None

        logger.warning("sql_planner: hit max rounds (%d)", MAX_TOOL_ROUNDS)
        return None

    except Exception as e:
        logger.error("sql_planner: error %s", e)
        return None
    finally:
        conn.close()
