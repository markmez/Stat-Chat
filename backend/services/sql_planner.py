"""
Sonnet SQL Planner — multi-step reasoning for complex queries.

Uses Claude tool use to generate and execute SQL queries against our DB,
chaining results until it can answer the question. Single API call with
tool results fed back in the same conversation.

Cost: ~$0.02-0.03 per query. Only called for queries that miss the
interceptor, query engine, and Haiku SQL.
"""

import asyncio
import json
import logging
import os
import sqlite3
from datetime import date

import anthropic

from prompts import VOICE_RULES
from schema_description import SCHEMA_DESCRIPTION
from services.llm import MAIN_MODEL
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

PRESENT_TOOL_DEF = {
    "name": "present_table",
    "description": (
        "Display ONE table to the user alongside your answer. Call at most "
        "once, AFTER your research, with a purpose-built display query: max "
        "10 rows, sorted by the metric that answers the question, only the "
        "entities your answer actually discusses, plus a title naming the "
        "exact slice. Research queries are NEVER shown to the user — if you "
        "don't call this, the answer is prose only, which is often best."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {"type": "string",
                      "description": "Names the exact slice, e.g. 'Blue Jays vs RHP — 2026'"},
            "sql": {"type": "string",
                    "description": "Display SELECT: <=10 rows, sorted, relevant entities only"},
        },
        "required": ["title", "sql"],
    },
}

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

SYSTEM_PROMPT = f"""You are a baseball expert answering a fan's question. Today is {date.today().isoformat()}. The current MLB season is {date.today().year}.

{VOICE_RULES}

You have internal tools to look up specific stats. Use them silently — the fan never sees the seam between what's looked up and what you already know.

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

7) PRESENTATION — VERDICT FIRST, THEN THE RECEIPTS
   Lead with the answer in prose. Research queries are NEVER displayed:
   when your answer names one player, never show the whole roster you
   considered.

   ATTACHING THE RECEIPTS (preferred): end your final answer with ONE
   extra line, on its own line, in exactly this form:
       TABLE: <title> || <one SELECT>
   The line itself is never shown; the query runs server-side and the
   table renders AFTER your prose. Rules: <=10 rows; sorted by the metric
   your reasoning used; ONLY the entities your answer discusses; columns
   = the numbers your prose cited; title names the exact slice ("Blue
   Jays vs RHP — 2026"). Keep the whole line under 400 characters.
   ATTACH THE RECEIPTS whenever your answer names two or more players or
   teams alongside numbers — that comparison card is the house style.
   Skip it only when a table genuinely adds nothing (single fact,
   narrative history). present_table remains available mid-research, but
   the trailing TABLE line is preferred (zero added latency).

   LEADERBOARD COLUMN RULES (when your display query is a ranked list):
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

   NEVER PRESENT APPROXIMATE OR PROXY LEADERBOARDS:
   If the user asked about a specific concept (inside-the-park HRs, walk-off HRs, grand slams, leadoff HRs, pinch-hit HRs, late-and-close performance) that isn't directly available as a column, do NOT write SQL that pulls a different/proxy stat (total HRs, triples, late-game HRs) and present it as a leaderboard for the requested concept. The leaderboard format implies precision — using it for an approximation produces a confidently-wrong answer. Instead:
     • Skip the leaderboard entirely.
     • Just narrate the answer with what you actually know about the concept (career counts of the real top players).
     • If you do run SQL for related context (e.g., total HRs to confirm a player's career length), use it as background — never render it as a leaderboard tagged with a fabricated alias like "Walkoff Approx".
   A clean narrative ("Alex Rodriguez leads with 25 career grand slams, ahead of Lou Gehrig at 23...") beats a leaderboard with the wrong numbers, every time.

== OPERATIONAL RULES ==
1. Use the execute_sql tool — chain queries as needed. Only SELECT.
2. Use actual column/table names from the schema above.
3. "Active roster" approximation: players with a game_batting_log or game_pitching_log entry in the last 14 days.
4. Keep queries efficient — use LIMIT, avoid full table scans.
5. After gathering data, write a concise, natural answer with specific numbers.
6. If a lookup truly doesn't find the answer, fall back to what you already know — without ever signaling the seam to the fan. (See VOICE section above for examples.)
7. Format the final answer for a mobile app feed — concise, no markdown headers, just clean text with player/team names and numbers.
8. Do NOT invent or hallucinate any statistics. Every number must come from a query result or well-established baseball fact.
9. Call tools immediately, with NO preamble text — never write prose before or between tool calls. All prose belongs in your final answer, after data gathering is complete.
"""

MAX_TOOL_ROUNDS = 8  # prevent runaway query chains

# Narration deltas are held back until this many chars accumulate in a round
# with no tool_use block. A round that opens with a tool call (the prompt
# mandates no preamble prose) never streams; a round that opens with a
# sustained run of text is the final narration and streams live from here on.
HOLDBACK_CHARS = 200
# Rolling tail held back during live streaming so the trailing TABLE
# directive (the "receipts" card) is intercepted server-side and never
# reaches the screen. Must exceed the directive's max length (~400).
TAIL_KEEP = 600


def _split_table_directive(text: str):
    """Split a trailing 'TABLE: <title> || <sql>' line off the answer.
    Returns (clean_text, (title, sql) | None)."""
    idx = text.rfind("\nTABLE:")
    if idx < 0:
        if text.startswith("TABLE:"):
            idx = 0
        else:
            return text, None
    line = text[idx:].strip()
    if idx == 0:
        clean = ""
    else:
        clean = text[:idx].rstrip()
    body = line[len("TABLE:"):].strip()
    if "||" not in body:
        return clean, None
    title, sql = body.split("||", 1)
    return clean, (title.strip()[:120], sql.strip())

# System prompt as a cache-marked content block. The planner re-sends the
# full conversation every tool round — the cache breakpoint means rounds 2+
# (and any planner query within the TTL) read the ~10K-token prefix at the
# cached-input rate instead of re-billing it, and skip reprocessing latency.
_SYSTEM_BLOCKS = [{
    "type": "text",
    "text": SYSTEM_PROMPT,
    "cache_control": {"type": "ephemeral"},
}]


def _primary_tool_result(history: list[dict]) -> dict | None:
    """Largest successful tool_result by row count, ties broken by recency.
    Lookup-helper queries (single-row scalar SELECTs that resolve a
    player_id, a team code, etc.) naturally lose this race to the actual
    answer query, which usually returns the most rows."""
    if not history:
        return None
    return max(history, key=lambda tr: (len(tr["rows"]), history.index(tr)))


def _execute_tool_sql(conn: sqlite3.Connection, sql: str) -> dict:
    """Run one tool SQL (sync — call via executor). Returns the tool_result
    content dict, or {"error": ...}."""
    rows = conn.execute(sql).fetchmany(50)
    cols = [desc[0] for desc in conn.execute(sql).description] if rows else []
    return {"columns": cols, "rows": [list(r) for r in rows], "count": len(rows)}


async def plan_and_execute_stream(question: str):
    """Run the insight engine, streaming the final narration.

    Async generator yielding (kind, payload) tuples, in order:
      ("grid", {"columns", "rows"})   — 0 or 1 time, before any text; the
                                        primary tool_result for grid rendering
      ("text", str)                   — narration deltas (first one is the
                                        held-back buffer, then live chunks)
      ("done", {"text", "grounded"})  — always last on success. text is the
                                        full narration; grounded is True iff
                                        at least one SQL returned real rows
      ("failed", None)                — terminal failure with NOTHING emitted;
                                        caller may fall through to knowledge
                                        mode. Never follows an emitted chunk.
    """
    if not ANTHROPIC_API_KEY:
        logger.warning("insight_engine: no API key")
        _log_err("insight_engine", "no_api_key",
                 "ANTHROPIC_API_KEY env var not set on server", question)
        yield ("failed", None)
        return

    client = anthropic.AsyncAnthropic()
    loop = asyncio.get_event_loop()
    # check_same_thread=False: the connection is created on the event-loop
    # thread but each tool SQL runs on an executor worker thread. Access is
    # strictly serial (every run_in_executor call is awaited), so this is safe.
    conn = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    conn.execute("PRAGMA query_only = ON")

    tool_results_history: list[dict] = []
    display_grid: dict | None = None
    messages: list[dict] = [{"role": "user", "content": question}]
    emitted = ""  # all text already yielded to the caller (spans rounds)

    try:
        for round_num in range(MAX_TOOL_ROUNDS):
            buffer = ""
            flushed_this_round = ""
            tool_use_seen = False

            async with client.messages.stream(
                model=MAIN_MODEL,
                max_tokens=4000,
                system=_SYSTEM_BLOCKS,
                tools=[TOOL_DEF, PRESENT_TOOL_DEF],
                messages=messages,
            ) as stream:
                async for ev in stream:
                    if ev.type == "content_block_start" and \
                            getattr(ev.content_block, "type", "") == "tool_use":
                        tool_use_seen = True
                    elif ev.type == "content_block_delta" and \
                            getattr(ev.delta, "type", "") == "text_delta" and \
                            not tool_use_seen:
                        buffer += ev.delta.text
                        # Go live once sustained prose confirms narration, but
                        # always keep a TAIL_KEEP rolling holdback so a
                        # trailing TABLE directive never reaches the screen.
                        if len(buffer) >= HOLDBACK_CHARS and len(buffer) > TAIL_KEEP:
                            part = buffer[:-TAIL_KEEP]
                            yield ("text", part)
                            flushed_this_round += part
                            emitted += part
                            buffer = buffer[-TAIL_KEEP:]
                final_msg = await stream.get_final_message()

            stop_reason = final_msg.stop_reason
            content = final_msg.content

            if stop_reason == "end_turn":
                answer = "".join(
                    b.text for b in content if getattr(b, "type", "") == "text"
                )
                if not answer.strip() and not emitted:
                    _log_err("insight_engine", "empty_answer",
                             "end_turn with no text content", question,
                             {"round": round_num + 1})
                    yield ("failed", None)
                    return
                logger.info("insight_engine: answered in %d rounds", round_num + 1)
                # Receipts: strip a trailing TABLE directive from the full
                # answer, execute it locally (~ms), render AFTER the prose —
                # verdict first, then the evidence card. flushed_this_round
                # is a byte-exact prefix of the CLEAN text because the
                # rolling TAIL_KEEP holdback never emitted the directive.
                answer, _directive = _split_table_directive(answer)
                if _directive:
                    _dt, _dsql = _directive
                    if _dsql.strip().upper().startswith("SELECT"):
                        try:
                            _shown = await loop.run_in_executor(
                                None, _execute_tool_sql, conn, _dsql)
                            if _shown.get("rows"):
                                display_grid = {"columns": _shown["columns"],
                                                "rows": _shown["rows"][:10],
                                                "title": _dt}
                        except Exception as _e:
                            logger.info("receipts directive failed: %s", _e)
                if not emitted:
                    answer = answer.strip()
                    yield ("text", answer)
                    emitted = answer
                else:
                    remainder = answer[len(flushed_this_round):]
                    if remainder:
                        yield ("text", remainder)
                        emitted += remainder
                if display_grid:
                    yield ("grid", display_grid)
                yield ("done", {
                    "text": emitted,
                    "grounded": bool(tool_results_history),
                })
                return

            if stop_reason == "tool_use":
                tool_results = []
                for block in content:
                    if getattr(block, "type", "") != "tool_use":
                        continue
                    tool_id = block.id
                    sql = (block.input or {}).get("sql", "")
                    purpose = (block.input or {}).get("purpose", "")
                    logger.info("insight_engine: step %d — %s", round_num + 1, purpose)

                    if getattr(block, "name", "") == "present_table":
                        title = str((block.input or {}).get("title", ""))[:120]
                        if sql.strip().upper().startswith("SELECT"):
                            try:
                                shown = await loop.run_in_executor(
                                    None, _execute_tool_sql, conn, sql)
                                if shown.get("rows"):
                                    display_grid = {
                                        "columns": shown["columns"],
                                        "rows": shown["rows"][:10],
                                        "title": title,
                                    }
                                    msg = {"status": "table queued for display"}
                                else:
                                    msg = {"error": "display query returned no rows; answer in prose"}
                            except Exception as e:
                                msg = {"error": str(e)}
                        else:
                            msg = {"error": "Only SELECT queries allowed"}
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tool_id,
                            "content": json.dumps(msg, default=str),
                        })
                        continue

                    if not sql.strip().upper().startswith("SELECT"):
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tool_id,
                            "content": json.dumps({"error": "Only SELECT queries allowed"}),
                        })
                        continue
                    try:
                        result_data = await loop.run_in_executor(
                            None, _execute_tool_sql, conn, sql)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tool_id,
                            "content": json.dumps(result_data, default=str),
                        })
                        # Stash for primary-result selection. Skip empty
                        # results and NULL-only single-cell aggregates —
                        # those aren't gridable and shouldn't compete with
                        # real answer rows.
                        cols, rows = result_data["columns"], result_data["rows"]
                        if cols and rows and not (
                                len(rows) == 1 and len(cols) == 1 and rows[0][0] is None):
                            tool_results_history.append(
                                {"columns": cols, "rows": rows})
                    except Exception as e:
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tool_id,
                            "content": json.dumps({"error": str(e)}),
                        })

                # Serialize assistant content blocks back into plain dicts
                assistant_content = []
                for b in content:
                    if getattr(b, "type", "") == "text":
                        assistant_content.append({"type": "text", "text": b.text})
                    elif getattr(b, "type", "") == "tool_use":
                        assistant_content.append({
                            "type": "tool_use", "id": b.id,
                            "name": b.name, "input": b.input,
                        })
                messages.append({"role": "assistant", "content": assistant_content})
                messages.append({"role": "user", "content": tool_results})
                continue

            logger.warning("insight_engine: unexpected stop_reason=%s", stop_reason)
            _log_err("insight_engine", "unexpected_stop_reason",
                     str(stop_reason), question, {"round": round_num + 1})
            if emitted:
                yield ("done", {"text": emitted, "grounded": bool(tool_results_history)})
            else:
                yield ("failed", None)
            return

        logger.warning("insight_engine: hit max rounds (%d)", MAX_TOOL_ROUNDS)
        _log_err("insight_engine", "max_rounds",
                 f"hit {MAX_TOOL_ROUNDS} tool rounds without end_turn", question)
        if emitted:
            yield ("done", {"text": emitted, "grounded": bool(tool_results_history)})
        else:
            yield ("failed", None)

    except Exception as e:
        import traceback as _tb
        tb_str = _tb.format_exc()
        logger.error("insight_engine: error %s\n%s", e, tb_str)
        _log_err("insight_engine", type(e).__name__, str(e), question,
                 {"traceback": tb_str[-1500:]})
        if emitted:
            # Text already reached the user — close out with what we have
            # rather than double-answering via knowledge mode.
            yield ("done", {"text": emitted, "grounded": bool(tool_results_history)})
        else:
            yield ("failed", None)
    finally:
        conn.close()
