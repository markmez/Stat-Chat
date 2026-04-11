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
from typing import Optional

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from services.llm import LLMService
from services.sql_runner import SqlRunner
from services.metering import check_quota, increment_count, log_query
from services.interceptor import try_intercept

logger = logging.getLogger("statchat.query")

router = APIRouter()
llm = LLMService()
runner = SqlRunner()


# Column name → display abbreviation mapping
_COL_DISPLAY = {
    "name": None,  # Used as row label, not a stat column
    "player_id": None,
    "season": "Year",
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


_RATE_3_EXACT = {
    "batting_avg", "obp", "slg", "ops", "iso", "babip",
    "avg", "career_avg", "career_ops", "april_avg", "batting_avg_against",
    "fielding_pct", "sb_pct",
}
# Substrings that indicate a .XXX rate stat column (for Haiku aliases like ops_2024)
_RATE_3_PATTERNS = ["avg", "obp", "slg", "ops", "iso", "babip", "baa", "fielding_pct"]
_RATE_2_EXACT = {
    "era", "whip", "k_per_9", "bb_per_9", "hr_per_9", "k_per_bb",
    "k_bb_ratio", "career_era",
}
_RATE_2_PATTERNS = ["era", "whip", "k_per_9", "bb_per_9", "hr_per_9", "k_per_bb", "k_bb"]


def _is_rate_3(col: str) -> bool:
    lower = col.lower()
    if lower in _RATE_3_EXACT:
        return True
    return any(p in lower for p in _RATE_3_PATTERNS)


def _is_rate_2(col: str) -> bool:
    lower = col.lower()
    if lower in _RATE_2_EXACT:
        return True
    return any(p in lower for p in _RATE_2_PATTERNS)


def _fmt_val(col: str, val) -> str:
    """Format a single value for display."""
    if val is None or val == "NULL" or str(val).strip() == "":
        return "--"
    lower_col = col.lower()
    if _is_rate_3(col):
        try:
            fv = float(val)
            # Leading dot only when < 1 (e.g., .321), full number when >= 1 (e.g., 1.024)
            return f".{int(round(fv * 1000)):03d}" if fv < 1 else f"{fv:.3f}"
        except (ValueError, TypeError):
            return str(val)
    if _is_rate_2(col):
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
    # Date columns — shorten to M/D
    if lower_col in ("date", "game_date", "start_date", "end_date"):
        s = str(val).strip()
        if len(s) == 10 and s[4] == "-":  # YYYY-MM-DD
            try:
                m, d = int(s[5:7]), int(s[8:10])
                return f"{m}/{d}"
            except ValueError:
                pass
    # Generic: try to clean up float formatting
    try:
        fv = float(val)
        if fv == int(fv) and "." not in str(val):
            return str(int(fv))
    except (ValueError, TypeError):
        pass
    return str(val)


def _display_col_name(col: str) -> Optional[str]:
    """Convert a SQL column name to a display name. Returns None for label-only columns."""
    import re as _re
    # Check exact match
    if col in _COL_DISPLAY:
        return _COL_DISPLAY[col]
    # Check case-insensitive
    lower = col.lower()
    if lower in _COL_DISPLAY:
        return _COL_DISPLAY[lower]
    # Year-suffixed stat columns: "ops_2024" → "OPS '24", "avg_2025" → "AVG '25"
    m = _re.match(r'^(.+?)_(\d{4})$', lower)
    if m:
        base, year = m.group(1), m.group(2)
        base_display = _COL_DISPLAY.get(base, base.upper())
        if base_display is None:
            base_display = base.upper()
        return f"{base_display} '{year[2:]}"
    # Clean up common SQL alias patterns: snake_case → Title Case
    cleaned = col.replace("_", " ").strip()
    # Short names (<=4 chars) → uppercase (likely abbreviations)
    if len(cleaned) <= 4:
        return cleaned.upper()
    # Join consecutive numbers with a dash: "40 47" → "40-47"
    import re as _re2
    cleaned = _re2.sub(r'(\d+)\s+(\d+)', r'\1-\2', cleaned)
    # Title case, then uppercase known stat abbreviations
    result = cleaned.title()
    for word, abbrev in [("Hr", "HR"), ("Rbi", "RBI"), ("Sb", "SB"), ("So", "SO"),
                          ("Bb", "BB"), ("Avg", "AVG"), ("Obp", "OBP"), ("Slg", "SLG"),
                          ("Ops", "OPS"), ("Era", "ERA"), ("Whip", "WHIP"), ("Ip", "IP")]:
        result = result.replace(word, abbrev)
    return result


def _format_haiku_result(result_text: str, question: str = "") -> str:
    """
    Convert SqlRunner pipe-delimited output into [LEADERBOARD] or [STATGRID] format.
    Multi-row results with names → [LEADERBOARD] with rank numbers.
    Single-row or aggregate → [STATGRID].
    """
    # Extract total count metadata if present
    total_count = None
    clean_text = result_text
    if "__TOTAL_COUNT__:" in result_text:
        parts = result_text.rsplit("__TOTAL_COUNT__:", 1)
        clean_text = parts[0].strip()
        try:
            total_count = int(parts[1].strip())
        except ValueError:
            pass

    lines = clean_text.strip().split("\n")
    if len(lines) < 3:  # header + separator + at least one data row
        return clean_text

    columns = [c.strip() for c in lines[0].split("|")]
    data_rows = []
    for line in lines[2:]:  # skip header + separator
        vals = [v.strip() for v in line.split("|")]
        if len(vals) == len(columns):
            data_rows.append(dict(zip(columns, vals)))

    if not data_rows:
        return clean_text

    # Single aggregate result (COUNT, SUM, etc.) — format as a clean answer
    if len(data_rows) == 1 and len(columns) == 1:
        val = list(data_rows[0].values())[0]
        try:
            num = int(float(val))
            col_name = _display_col_name(columns[0]) or columns[0].replace("_", " ").title()
            return f"**{num:,}** {col_name}"
        except (ValueError, TypeError):
            pass

    # Fix Haiku concatenating name + year: "Tony Gwynn, 1994" → split into separate columns
    import re as _re
    # Find the name column (case-insensitive, multiple aliases)
    name_col = None
    for c in columns:
        if c.lower() in ("name", "player_name", "player"):
            name_col = c
            break
    has_name = name_col is not None
    # Normalize name column to "name" for downstream processing
    if has_name and name_col != "name":
        idx = columns.index(name_col)
        columns[idx] = "name"
        for row in data_rows:
            row["name"] = row.pop(name_col, "")

    # Strip team abbreviations from name column: "Aaron Judge (NYY)" → "Aaron Judge"
    if has_name:
        for row in data_rows:
            name_val = str(row.get("name", ""))
            # "Name (TEAM)" pattern
            m = _re.match(r'^(.+?)\s*\([A-Z]{2,3}\)$', name_val)
            if m:
                row["name"] = m.group(1).strip()
                continue
            # "Name, TEAM" pattern (but not "Name, 1994" which is year)
            m = _re.match(r'^(.+?),\s*([A-Z]{2,3})$', name_val)
            if m:
                row["name"] = m.group(1).strip()

    if has_name and not any(c.lower() == "season" for c in columns):
        # Check if name values contain ", YYYY" pattern
        sample = [row.get(name_col, "") for row in data_rows[:5]]
        if sample and all(_re.search(r',\s*\d{4}$', str(s)) for s in sample if s):
            columns.append("season")
            for row in data_rows:
                name_val = str(row.get("name", ""))
                m = _re.match(r'^(.+?),\s*(\d{4})$', name_val)
                if m:
                    row["name"] = m.group(1).strip()
                    row["season"] = m.group(2)

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
    # Redundant season/year columns: season_2024, season_2025, year_2024, year etc.
    # These just repeat year info that's already obvious from the query context.
    import re as _re
    for c in columns:
        lower_c = c.lower()
        if _re.match(r'^(?:season|year)(?:_\d{4})?$', lower_c) and c != "season":
            label_cols.add(c)

    stat_cols = [c for c in columns if c not in label_cols]

    # Strip columns where every row has the same value (no information)
    if multi_row and len(data_rows) > 1:
        constant_cols = set()
        for c in stat_cols:
            vals = set(str(row.get(c, "")) for row in data_rows)
            if len(vals) == 1:
                constant_cols.add(c)
        if constant_cols:
            stat_cols = [c for c in stat_cols if c not in constant_cols]

    # Remove team from stat_cols if we'll use it in the label
    use_team_in_label = "team" in stat_cols and has_name
    if use_team_in_label:
        stat_cols = [c for c in stat_cols if c != "team"]

    # Limit to 4 stat columns max for display — iOS leaderboard overflows otherwise.
    # Deprioritize computed/derived columns (gap, diff, shortfall) and date columns.
    _low_priority_cols = {"date", "game_date", "start_date", "end_date",
                          "gap", "gap_to_400", "gap_from_400", "shortfall",
                          "diff", "difference", "delta"}
    if multi_row and len(stat_cols) > 4:
        high = [c for c in stat_cols if c.lower() not in _low_priority_cols]
        low = [c for c in stat_cols if c.lower() in _low_priority_cols]
        stat_cols = (high + low)[:4]

    # Remove columns that map to None (label-only columns that slipped through)
    stat_cols = [c for c in stat_cols if _display_col_name(c) is not None]

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
        # Show season as its own column, not merged into the name label
        # (handled by stat_cols if season is in columns)

        label = ", ".join(label_parts) if label_parts else ""

        # Build values
        vals = [_fmt_val(c, row.get(c)) for c in stat_cols]

        if use_leaderboard:
            row_lines.append(f"ROW {i+1}. {label}: {', '.join(vals)}")
        elif label:
            row_lines.append(f"ROW: {label}, {', '.join(vals)}")
        else:
            row_lines.append(f"ROW: {', '.join(vals)}")

    # Build title with count only when there's a meaningful total
    # (more rows exist than displayed). Don't show "50 results" for a LIMIT 50 leaderboard.
    title = ""
    if total_count and total_count > len(data_rows):
        title = f"**{total_count} results**\n\n"

    # Pagination note if results were capped
    pagination = ""
    if total_count and total_count > len(data_rows):
        pagination = f"\n\nShowing 1-{len(data_rows)} of {total_count}."

    if use_leaderboard:
        parts = []
        if title:
            parts.append(title)
        parts.append("[TIP]Tap a player name for their full profile.[/TIP]")
        parts.append("[LEADERBOARD]")
        parts.append(header)
        parts.extend(row_lines)
        parts.append("[/LEADERBOARD]")
        if pagination:
            parts.append(pagination)
        return "\n".join(parts)
    else:
        parts = []
        if title:
            parts.append(title)
        parts.append(f"[STATGRID]\n{header}\n" + "\n".join(row_lines) + "\n[/STATGRID]")
        if pagination:
            parts.append(pagination)
        return "\n".join(parts)


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

    # If Haiku returned a bare COUNT(*), unwrap and re-execute to get examples
    if _is_bare_count(result_text):
        examples_sql = _unwrap_count_sql(sql)
        if examples_sql:
            try:
                examples_text, _ = await loop.run_in_executor(
                    None, runner.execute_and_format, examples_sql
                )
                if examples_text and examples_text != "No results found.":
                    # Prepend the count as a title, examples follow
                    result_text = result_text.strip() + "\n" + examples_text
            except Exception:
                pass  # Keep the count-only result

    return sql, result_text, is_streak


def _is_bare_count(result_text: str) -> bool:
    """Check if result is a single aggregate number (COUNT, SUM, etc.)."""
    lines = result_text.strip().split("\n")
    if "__TOTAL_COUNT__:" in result_text:
        lines = result_text.split("__TOTAL_COUNT__:")[0].strip().split("\n")
    # header + separator + one data row = 3 lines
    if len(lines) != 3:
        return False
    cols = [c.strip() for c in lines[0].split("|")]
    vals = [v.strip() for v in lines[2].split("|")]
    if len(cols) != 1 or len(vals) != 1:
        return False
    try:
        int(float(vals[0]))
        return True
    except (ValueError, TypeError):
        return False


def _unwrap_count_sql(sql: str) -> Optional[str]:
    """Strip COUNT(*) from SQL to get the underlying query with examples.
    Returns modified SQL with LIMIT 25, or None if can't unwrap."""
    import re

    # Strip trailing semicolons
    sql_clean = sql.strip().rstrip(';').strip()

    # Pattern 1: SELECT COUNT(*) [AS alias] FROM (subquery) [alias]
    m = re.match(
        r'SELECT\s+COUNT\s*\(\s*\*\s*\)(?:\s+AS\s+\w+)?\s+FROM\s*\((.*)\)\s*\w*\s*$',
        sql_clean, re.IGNORECASE | re.DOTALL)
    if m:
        inner = m.group(1).strip()
        inner = re.sub(r'\s+LIMIT\s+\d+\s*$', '', inner, flags=re.IGNORECASE)
        return inner + " LIMIT 25"

    # Pattern 2: SELECT COUNT(*) [AS alias] FROM table/join WHERE ...
    m = re.match(
        r'SELECT\s+COUNT\s*\(\s*\*\s*\)(?:\s+AS\s+\w+)?\s+FROM\s+(.+)',
        sql_clean, re.IGNORECASE | re.DOTALL)
    if m:
        rest = m.group(1).strip()
        rest = re.sub(r'\s+LIMIT\s+\d+\s*$', '', rest, flags=re.IGNORECASE)
        return f"SELECT * FROM {rest} LIMIT 25"

    return None


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


import re as _re
from services import name_matcher as _nm


def _strip_bold_title(text: str) -> str:
    """Strip the bold **title** line from a response and convert scope info to subtitle.

    Before: **Switch-Hitting Players with 20+ HR (2025)**\n9 matched.\n...
    After:  [SUBTITLE]2025 · 9 matched[/SUBTITLE]\n...

    Before: **Aaron Judge** hit **53** home runs in 2025.
    After:  (unchanged — sentence responses don't start with a full bold title)
    """
    lines = text.split("\n")
    if not lines:
        return text

    first = lines[0].strip()

    # Only strip lines that are ENTIRELY a bold title: **...** with no other text
    # Don't touch sentence responses like "**Aaron Judge** hit **53** home runs..."
    if first.startswith("**") and first.endswith("**") and first.count("**") == 2:
        title_content = first[2:-2]

        # Extract scope from parentheses: "... (2025)" or "... (All-Time)"
        scope_match = _re.search(r'\(([^)]+)\)\s*$', title_content)
        if scope_match:
            scope = scope_match.group(1)
        else:
            # Try extracting year from title like "2026 ERA Leaders"
            year_match = _re.search(r'\b(20[012]\d)\b', title_content)
            scope = year_match.group(1) if year_match else None
            if not scope and "career" in title_content.lower():
                scope = "Career"
            elif not scope and "all-time" in title_content.lower():
                scope = "All-Time"
            elif not scope and "active" in title_content.lower():
                scope = "Active"

        # Check if next line has a count like "9 matched." or "14 matched."
        count_line = ""
        rest_start = 1
        if len(lines) > 1:
            next_line = lines[1].strip()
            count_match = _re.match(r'^(\d+)\s+matched\.?$', next_line)
            if count_match:
                count_line = f"{count_match.group(1)} matched"
                rest_start = 2

        # Build subtitle
        subtitle_parts = []
        if scope:
            subtitle_parts.append(scope)
        if count_line:
            subtitle_parts.append(count_line)

        rest = "\n".join(lines[rest_start:])
        if subtitle_parts:
            return f"[SUBTITLE]{' · '.join(subtitle_parts)}[/SUBTITLE]\n{rest}"
        return rest

    return text


def _extract_prior_context(history: list[dict]) -> dict:
    """Extract player name, stat, and season from the prior Q&A exchange."""
    ctx = {"player": None, "stat": None, "season": None, "query": None}
    # Find the last user message
    for msg in reversed(history):
        if msg.get("role") == "user":
            ctx["query"] = msg["content"]
            break
    if not ctx["query"]:
        return ctx

    q = ctx["query"]

    # Extract season
    year_match = _re.search(r'\b(20[12]\d)\b', q)
    if year_match:
        ctx["season"] = year_match.group(1)
    elif "this season" in q.lower() or "this year" in q.lower():
        from datetime import date
        ctx["season"] = str(date.today().year)
    elif "last season" in q.lower() or "last year" in q.lower():
        from datetime import date
        ctx["season"] = str(date.today().year - 1)

    # Extract player name
    player = _nm.find_player_in_text(q)
    if player:
        ctx["player"] = player

    # Extract stat keyword — check common stat words in the prior query
    lower_q = q.lower()
    stat_keywords = [
        ("home runs", "home runs"), ("hr", "home runs"), ("homers", "home runs"),
        ("rbi", "RBI"), ("runs batted in", "RBI"),
        ("batting average", "batting average"), ("avg", "batting average"),
        ("ops", "OPS"), ("obp", "OBP"), ("slg", "SLG"),
        ("stolen bases", "stolen bases"), ("steals", "stolen bases"),
        ("hits", "hits"), ("doubles", "doubles"), ("triples", "triples"),
        ("runs", "runs"), ("walks", "walks"),
        ("strikeouts", "strikeouts"),
        ("era", "ERA"), ("whip", "WHIP"), ("wins", "wins"),
        ("saves", "saves"), ("innings pitched", "innings pitched"),
    ]
    for keyword, canonical in stat_keywords:
        if keyword in lower_q:
            ctx["stat"] = canonical
            break

    return ctx


def _local_followup_rewrite(question: str, history: list[dict]) -> Optional[str]:
    """Try to rewrite a follow-up question locally without calling Haiku.
    Returns the rewritten standalone query, or None to fall through to Haiku."""
    lower = question.strip().lower()
    ctx = _extract_prior_context(history)

    if not ctx["query"]:
        return None

    player = ctx["player"]
    stat = ctx["stat"]
    season = ctx["season"]

    # --- Pattern 1: Player swap ---
    # "what about Soto", "and Soto?", "how about Ohtani", "and his?"
    swap_match = _re.match(
        r'^(?:what about|how about|and|how did|what did)\s+(.+?)[\?\.]?$', lower)
    if swap_match:
        name_text = swap_match.group(1).strip().rstrip('?.')
        # Skip if it's a year ("what about 2023?")
        if _re.match(r'^20[012]\d$', name_text):
            # Year swap
            if player and stat:
                return f"{player} {stat} {name_text}"
            elif player:
                return f"{player} {name_text}"
            return None
        # Skip if it's a stat ("and his RBI?", "what about strikeouts?")
        name_text_clean = name_text.replace("his ", "").replace("her ", "")
        stat_match = _nm.match_stat(name_text_clean)
        if stat_match and player:
            season_part = f" {season}" if season else ""
            return f"{player} {name_text_clean}{season_part}"
        # Try as a player name (use prominence for ambiguous last names like "Soto")
        new_player = _nm.find_player_in_text(name_text) or _nm.match_player(name_text)
        if not new_player:
            prominence = _nm.match_player_with_prominence(name_text)
            if prominence:
                new_player = prominence[0]
        if new_player and stat:
            season_part = f" {season}" if season else ""
            return f"{new_player} {stat}{season_part}"
        elif new_player and player:
            # No stat extracted but we have prior player — use prior query structure
            return ctx["query"].replace(player, new_player)

    # --- Pattern 2: Career ---
    if lower in ("career", "career?", "career stats", "career stats?"):
        if player:
            if stat:
                return f"{player} career {stat}"
            return f"{player} career stats"

    # --- Pattern 3: Splits pivot ---
    # "vs lefties", "vs righties", "at home", "on the road", "away"
    splits_patterns = {
        "vs lefties": "vs lefties", "against lefties": "vs lefties",
        "vs left": "vs lefties", "against left": "vs lefties",
        "vs righties": "vs righties", "against righties": "vs righties",
        "vs right": "vs righties", "against right": "vs righties",
        "at home": "home vs away", "home": "home vs away",
        "on the road": "home vs away", "away": "home vs away",
    }
    clean = lower.rstrip('?.').strip()
    # Strip leading "how about" / "what about"
    for prefix in ["how about ", "what about ", "and "]:
        if clean.startswith(prefix):
            clean = clean[len(prefix):]
    if clean in splits_patterns and player:
        split = splits_patterns[clean]
        season_part = f" {season}" if season else ""
        return f"{player} {split}{season_part}"

    # --- Pattern 4: Comparison ---
    # "compare him to Ohtani", "compare to Soto", "him vs Ohtani"
    compare_match = _re.match(
        r'^(?:compare (?:him|her|them) to|compare to|him vs|vs)\s+(.+?)[\?\.]?$', lower)
    if compare_match and player:
        other_text = compare_match.group(1).strip()
        other_player = _nm.find_player_in_text(other_text) or _nm.match_player(other_text)
        if not other_player:
            prominence = _nm.match_player_with_prominence(other_text)
            if prominence:
                other_player = prominence[0]
        if other_player:
            season_part = f" {season}" if season else ""
            return f"{player} vs {other_player}{season_part}"

    # --- Pattern 5: "who led the league?" ---
    if _re.match(r'^who (?:led|leads|won)\b', lower) and stat:
        season_part = f" {season}" if season else ""
        return f"most {stat}{season_part}"

    return None


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
        log_query(question, device_id, "sonnet")
        return

    # 2. Follow-up rewrite — try local patterns BEFORE interceptor so short
    # follow-ups like "what about Soto" get rewritten, not intercepted as-is.
    rewritten_query: str | None = None
    if history and len(question.split()) < 10:
        local_rewrite = _local_followup_rewrite(question, history)
        if local_rewrite is None:
            ctx = _extract_prior_context(history)
            logger.info("followup_local_miss question=%r ctx_player=%r ctx_stat=%r ctx_season=%r",
                        question, ctx["player"], ctx["stat"], ctx["season"])
        if local_rewrite:
            rewritten_query = local_rewrite
            logger.info("followup_local_rewrite original=%r rewritten=%r", question, local_rewrite)
            try:
                intercepted = try_intercept(local_rewrite)
            except Exception as e:
                logger.warning("intercept_rewrite_error error=%s", e)
                intercepted = None
            if intercepted is not None:
                logger.info("followup_local_intercepted rewritten=%r", local_rewrite)
                intercepted = _strip_bold_title(intercepted)
                yield event({"type": "text", "text": intercepted})
                done_event = {"type": "done", "intercepted": True}
                done_event["rewritten_query"] = local_rewrite
                yield event(done_event)
                increment_count(device_id)
                log_query(local_rewrite, device_id, "query engine")
                return
            # Local rewrite didn't intercept — use it as the question for the rest of pipeline
            question = local_rewrite

    if history and len(question.split()) < 10 and not rewritten_query:
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
                    intercepted = _strip_bold_title(intercepted)
                    yield event({"type": "text", "text": intercepted})
                    done_event = {"type": "done", "intercepted": True}
                    if rewritten_query:
                        done_event["rewritten_query"] = rewritten_query
                    yield event(done_event)
                    increment_count(device_id)
                    log_query(rewritten, device_id, "intercepted")
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
            log_query(question, device_id, "sonnet")
            return

    # 2b. Try local intercept — zero Claude API cost
    # Runs after follow-up rewrite so the question may already be rewritten.
    try:
        intercepted = try_intercept(question)
    except Exception as e:
        logger.warning("intercept_error question=%r error=%s type=%s", question, e, type(e).__name__)
        intercepted = None
    if intercepted is not None:
        no_count = intercepted.startswith("__NO_COUNT__")
        if no_count:
            intercepted = intercepted.replace("__NO_COUNT__", "", 1)
        logger.info("query_intercepted question=%r no_count=%s", question, no_count)
        intercepted = _strip_bold_title(intercepted)
        yield event({"type": "text", "text": intercepted})
        done_event = {"type": "done", "intercepted": True}
        if rewritten_query:
            done_event["rewritten_query"] = rewritten_query
        yield event(done_event)
        if not no_count:
            increment_count(device_id)
        log_query(question, device_id, "query engine")
        return

    # 3. Haiku SQL fallback — cheap SQL generation, no Sonnet needed
    haiku_result = await _try_haiku_sql(question)
    if haiku_result is not None:
        haiku_sql, haiku_result_text, haiku_is_streak = haiku_result
        logger.info("query_haiku_sql question=%r", question)
        formatted = _format_haiku_result(haiku_result_text, question=question)
        # Guard: never send more than one container — iOS renders each as a separate card
        for tag in ["[LEADERBOARD]", "[STATGRID]"]:
            first = formatted.find(tag)
            if first >= 0:
                end_tag = tag.replace("[", "[/")
                first_end = formatted.find(end_tag, first)
                if first_end >= 0:
                    second = formatted.find(tag, first_end)
                    if second >= 0:
                        formatted = formatted[:first_end + len(end_tag)]
        yield event({"type": "text", "text": formatted})
        done_event = {"type": "done", "haiku_sql": True}
        if rewritten_query:
            done_event["rewritten_query"] = rewritten_query
        yield event(done_event)
        increment_count(device_id)
        log_query(question, device_id, "haiku")
        return

    # 4. Knowledge mode — answer from Claude's baseball knowledge
    # If we got here, neither the interceptor, query engine, nor Haiku could answer.
    # Answer from Sonnet's own knowledge rather than trying SQL (which produces
    # worse results for questions that got this far in the pipeline).
    logger.info("knowledge_mode question=%r", question)
    try:
        async for chunk in llm.stream_knowledge(question, history):
            yield event({"type": "text", "text": chunk})
    except Exception as e:
        yield event({"type": "text", "text": "I'm not sure about that. Try asking about player stats, leaders, or comparisons."})

    # Disclaimer for knowledge-mode responses — these come from AI, not our verified DB
    yield event({"type": "text", "text": "\n\n[AIDISCLAIMER]Answered by AI from general knowledge, not verified against our database. AI can make mistakes.[/AIDISCLAIMER]"})

    done_event: dict = {"type": "done"}
    if rewritten_query:
        done_event["rewritten_query"] = rewritten_query
    yield event(done_event)
    increment_count(device_id)
    log_query(question, device_id, "sonnet")
