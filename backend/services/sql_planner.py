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

== INFERENCE FRAMEWORK ==
Apply these rules consistently. They mirror how our structural query engine
interprets natural language. Most user questions don't spell out season,
perspective, or thresholds — you must infer them the same way our parsers do.

1) TIME FRAME — default is current season ({date.today().year}) when no scope is given.
   • Explicit YYYY → that season.
   • "this year" / "this season" / "current" / no qualifier → {date.today().year}.
   • "last year" / "previous" / "prior season" → {date.today().year - 1}.
   • "two/three years ago" → {date.today().year - 2} / {date.today().year - 3}.
   • "since YYYY" → range [YYYY, {date.today().year}].
   • "in/over/for the last N years" → rolling [{date.today().year} - N, {date.today().year}].
   • "this decade" → [{date.today().year - (date.today().year % 10)}, {date.today().year}].
   • "last decade" (named, standalone) → [{date.today().year - (date.today().year % 10) - 10}, {date.today().year - (date.today().year % 10) - 1}].
   • "this century" / "21st century" → [2000, {date.today().year}].
   • "career" / "all-time" / "ever" / "in history" → aggregate ALL seasons.
   ALWAYS apply the season filter explicitly in your WHERE — even when defaulting to the current season — so intent is visible and you don't accidentally cross-aggregate.

2) BATTING vs PITCHING perspective — the #1 misinterpretation. Resolve in this order:
   a) Pitching-specific stat (ERA, WHIP, K/9, BB/9, BAA, IP, wins, losses, saves, holds, quality starts, complete games, shutouts) → ALWAYS pitching tables.
   b) "pitcher(s)" as subject ("which pitcher...", "best pitchers with...") → PITCHING.
   c) "against [pitcher(s)]" / "vs pitchers" / "facing pitchers" → BATTING (the subject is the batter facing them).
   d) "against [batter(s)/hitter(s)]" / "vs batters" / "facing hitters" → PITCHING (the subject is the pitcher facing them).
   e) "against [pitch type]" — fastballs, 4-seamers, sliders, curveballs, sinkers, changeups, splitters, sweepers, etc. → BATTING. Pitches are thrown BY pitchers, so the subject FACING them is the batter. Use pitch_type_batting_splits.
   e-bis) When "TEAM" is the subject of "against [pitch type / count / RISP / handedness]", treat the team as the OFFENSE — use BATTING splits. "Team against 4-seamers" = the team's BATTERS facing 4-seamers from any pitcher → pitch_type_batting_splits joined through season_batting_stats. The pitching direction requires EXPLICIT phrasing — "team's pitching against X", "which team THROWS X", "team's pitchers vs X". If the user just says "team [vs|against] [pitch type]" with no possessive or verb pointing to pitching, ALWAYS use BATTING. Worked example: "what team is worst against 4-seam fastballs" → BATTING — find the team with the LOWEST OPS vs 4-Seam in pitch_type_batting_splits, ORDER BY ops ASC. (Worst HITTING, not worst pitching.) "Which team's pitchers throw the worst 4-seamers" → PITCHING.
   f) "team's pitching" / "team's fastball" / "team throws X" / "team strikes out hitters" → PITCHING.
   g) "team hits/bats/connects against X" / "team's batting against X" → BATTING.
   h) Ambiguous bare stats (strikeouts, walks) with no other context:
      • If filter contains a batting-only stat (".300 AVG", "30+ HR") → BATTING.
      • If filter contains a pitching-only stat ("3.00 ERA", "200 IP") → PITCHING.
      • Otherwise default to BATTING and acknowledge the ambiguity in your prose answer.
   The smell test: who is the SUBJECT performing the action — batter or pitcher?

3) IMPLICIT STAT RESOLUTION — when the user says "best" / "worst" / "top" / "leaders" without naming a stat:
   • Batting context → rank by OPS (with PA minimum from section 4).
   • Pitching context, FULL SEASON (season_pitching_stats) → rank by ERA, ASC for "best" (lower is better), DESC for "worst". Apply IP minimum.
   • Pitching context, SPLIT TABLE (pitch_type_pitching_splits, count_pitching_splits, risp_pitching_splits, etc.) → rank by OPS-against (or BAA). ERA is NOT computable from split tables — they don't carry earned_runs or ip_outs, only batter-perspective columns (at_bats, hits, walks, etc.). Do NOT attempt SUM(earned_runs) on a split table; the column doesn't exist there.
   • If the question names a specific stat ("best AVG", "lowest ERA", "most HR"), use that stat instead of the default.
   • Counting stats use DESC for "most/best" and ASC for "fewest/worst" regardless of pitching/batting (e.g., "fewest strikeouts" → ASC).

4) SAMPLE-SIZE GUARDRAILS — REQUIRED for any rate stat (AVG, OBP, SLG, OPS, ISO, BABIP, ERA, WHIP, K/9, BB/9, BAA). Without them, edge cases dominate and rate stats are meaningless.
   • Per-player full season:
     - Batting: HAVING plate_appearances >= 502 (or at_bats >= 400 if PA not available).
     - Pitching: HAVING ip_outs >= 486 (= 162 IP).
   • Per-player in-progress season: prorate to team games played, e.g.,
       HAVING plate_appearances >= ROUND(502 * MAX(team_games_played) / 162.0)
     If you don't have team_games handy, use the simpler proxy:
       HAVING plate_appearances >= 100 (in-progress, early season).
   • Per-team full season:
     - Batting: HAVING SUM(at_bats) >= 1000.
     - Pitching: HAVING SUM(ip_outs) >= 1200.
   • Per-team in-progress (split or full-season aggregate): scale the above by season fraction; for early-to-mid season, use HAVING SUM(at_bats) >= 200 as a minimum floor.
   • For split-table aggregations (vs LHP, vs 4-seamers, with RISP, with 2 strikes): apply the same HAVING on the split's at_bats column. NEVER let small-sample teams or players show up in rate-stat leaderboards — a single player with 12 ABs at .500 must not be the answer.

5) RATE-STAT AGGREGATION FORMULAS — when grouping across players/seasons/splits, NEVER use AVG() on the precomputed per-row rate column (e.g., AVG(ops)). That gives a row-weighted average, not the true aggregate. Recompute from raw components:

   Batting:
     AVG = SUM(hits) / NULLIF(SUM(at_bats), 0)
     OBP = (SUM(hits) + SUM(walks) + SUM(hit_by_pitch))
           / NULLIF(SUM(at_bats) + SUM(walks) + SUM(hit_by_pitch) + SUM(sacrifice_flies), 0)
     SLG = (SUM(hits) + SUM(doubles) + 2*SUM(triples) + 3*SUM(home_runs))
           / NULLIF(SUM(at_bats), 0)
     OPS = OBP + SLG  (inline both expressions)
     ISO = SLG - AVG
   Pitching:
     ERA = 9.0 * SUM(earned_runs) / NULLIF(SUM(ip_outs) / 3.0, 0)
     WHIP = (SUM(walks) + SUM(hits_allowed)) / NULLIF(SUM(ip_outs) / 3.0, 0)
     BAA = SUM(hits_allowed) / NULLIF(SUM(at_bats_against), 0)
     K/9 = 9.0 * SUM(strikeouts) / NULLIF(SUM(ip_outs) / 3.0, 0)
     BB/9 = 9.0 * SUM(walks) / NULLIF(SUM(ip_outs) / 3.0, 0)
   For SPLIT tables (pitch_type_batting_splits, etc.), the SUM columns are the same names — at_bats, hits, doubles, triples, home_runs, walks, hit_by_pitch, sacrifice_flies, strikeouts.
   IMPORTANT — Pitching split tables (pitch_type_pitching_splits, count_pitching_splits, risp_pitching_splits) contain batter-perspective columns ONLY (at_bats, hits, walks, etc.) — they do NOT carry earned_runs or ip_outs. So ERA/WHIP cannot be computed from a split table. For pitching split aggregations, recompute OPS-against / BAA from the same component SUMs (treating "hits" as hits allowed, "at_bats" as at-bats against, etc.). Use the BATTING formulas above — same column names, just remember the perspective.

6) TEAM AGGREGATION PATTERN — per-player split tables (pitch_type_batting_splits, count_batting_splits, risp_batting_splits, and pitching equivalents) do NOT carry team directly. Aggregate by team via JOIN through season stats. Example using OPS (the implicit default for "best/worst" hitting):
     SELECT sbs.team,
            SUM(p.at_bats) AS ab,
            ROUND(1.0 * SUM(p.hits) / NULLIF(SUM(p.at_bats), 0), 3) AS avg,
            ROUND(1.0 * (SUM(p.hits) + SUM(p.walks) + SUM(p.hit_by_pitch))
                  / NULLIF(SUM(p.at_bats) + SUM(p.walks) + SUM(p.hit_by_pitch) + SUM(p.sacrifice_flies), 0), 3) AS obp,
            ROUND(1.0 * (SUM(p.hits) + SUM(p.doubles) + 2*SUM(p.triples) + 3*SUM(p.home_runs))
                  / NULLIF(SUM(p.at_bats), 0), 3) AS slg,
            ROUND(
              1.0 * (SUM(p.hits) + SUM(p.walks) + SUM(p.hit_by_pitch))
                    / NULLIF(SUM(p.at_bats) + SUM(p.walks) + SUM(p.hit_by_pitch) + SUM(p.sacrifice_flies), 0)
              +
              1.0 * (SUM(p.hits) + SUM(p.doubles) + 2*SUM(p.triples) + 3*SUM(p.home_runs))
                    / NULLIF(SUM(p.at_bats), 0)
            , 3) AS ops
     FROM pitch_type_batting_splits p
     JOIN season_batting_stats sbs
       ON p.player_id = sbs.player_id AND p.season = sbs.season
     WHERE p.pitch_type = '4-Seam' AND p.season = {date.today().year}
     GROUP BY sbs.team
     HAVING SUM(p.at_bats) >= 200      -- in-progress season floor
     ORDER BY ops ASC                  -- ASC for "worst", DESC for "best"
     LIMIT 30;
   For pitching direction: JOIN through season_pitching_stats sps the same way; use ERA/WHIP/BAA formulas from section 5.

7) PRESENTATION — LEADERBOARDS (multi-row ranked results)
   For leaderboards, SELECT exactly ONE stat column: the stat the user asked about (or the implicit default — OPS for batting "best/worst", ERA for pitching). No slash line, no supporting stats, no raw counters. This matches the structural query engine's pattern and avoids the trailing-column truncation that plagues narrow iOS leaderboards.
     - "Best/worst" batting (OPS default) → SELECT name (or team), OPS — ONLY.
     - "Best AVG" → SELECT name, AVG — ONLY.
     - "Best ERA" → SELECT name, ERA — ONLY.
     - "Most HR" → SELECT name, HR — ONLY.
   The user came for one number. Show them that one number, big and clean. If they want the full slash line they'll tap into a player card.
   Sample-size constraints (HAVING ... at_bats >= 400, etc.) still apply — they belong in HAVING/WHERE, not SELECT.
   The ORDER BY column MUST also appear in the SELECT (hard rule).
   Team codes: SELECT the raw Retrosheet code (NYA, LAN, KCA). The downstream formatter translates known codes to friendly names. Do NOT write CASE WHEN.

   PRESENTATION — STAT GRIDS (single-row / single-entity detail)
   For single-player or single-team detail queries ("Aaron Judge's 2026 stats", "Yankees season stats"), the full slash line / standard stat set is welcome since there's only one row to display. No truncation risk.

   PRESENTATION — GENERAL
   • In your prose narration, use team NAMES, not codes ("the Royals", not "KCA").
   • Season-aggregate tables already exclude spring training. For game-log tables, add `COALESCE(gametype, 'regular') = 'regular'` to the WHERE.

== OPERATIONAL RULES ==
1. Use the execute_sql tool — chain queries as needed. Only SELECT.
2. Use actual column/table names from the schema above.
3. "Active roster" approximation: players with a game_batting_log or game_pitching_log entry in the last 14 days.
4. Keep queries efficient — use LIMIT, avoid full table scans.
5. After gathering data, write a concise, natural answer with specific numbers.
6. If the data truly isn't in the database, answer from your baseball knowledge. Do NOT explain why the database can't answer — the user doesn't know or care about tables. Just answer naturally.
7. NEVER mention the database, SQL, data sources, table names, column names, or any implementation details. You're a baseball expert talking to a fan.
8. Format the final answer for a mobile app feed — concise, no markdown headers, just clean text with player/team names and numbers.
9. Do NOT invent or hallucinate any statistics. Every number must come from a query result or well-established baseball fact.
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
