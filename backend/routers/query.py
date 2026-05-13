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
import time
from typing import Optional

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from services.llm import LLMService
from services.sql_runner import SqlRunner
from services.metering import check_quota, increment_count, log_query, log_server_error
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


_TEAM_CODE_COLUMNS = {"team", "team_code", "team_abbr", "team_abbreviation"}


def _maybe_team_name(col: str, val) -> Optional[str]:
    """If `col` looks like a team-code column and `val` is a known Retrosheet
    code, return the friendly name (e.g., 'KCA' -> 'Kansas City Royals').
    Otherwise return None and the caller falls through to default formatting."""
    if col.lower() not in _TEAM_CODE_COLUMNS:
        return None
    s = str(val).strip()
    if not s or len(s) > 4:
        return None
    try:
        from services.response_builder import _team_full_name
        translated = _team_full_name(s)
        # _team_full_name returns the input unchanged if unknown; only return
        # the translation when it actually mapped.
        return translated if translated != s else None
    except Exception:
        return None


def _fmt_val(col: str, val) -> str:
    """Format a single value for display."""
    if val is None or val == "NULL" or str(val).strip() == "":
        return "--"
    # Team code → friendly name (NYA → Yankees, KCA → Royals). Applied early
    # so the rest of the formatter sees the readable string.
    team_name = _maybe_team_name(col, val)
    if team_name is not None:
        return team_name
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
    Thin wrapper over `_format_structured_rows_to_grid` — parses pipe-delimited
    text into structured rows then delegates. The same core formatting logic
    powers both Haiku (pipe-text in) and Sonnet sql_planner (structured rows
    in via tool-use), so column-cap rules / name-column detection / single-
    aggregate handling stay in one place.
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

    formatted = _format_structured_rows_to_grid(columns, data_rows, total_count, question)
    return formatted if formatted is not None else clean_text


def _format_structured_rows_to_grid(
    columns: list[str],
    data_rows: list[dict],
    total_count: int | None = None,
    question: str = "",
) -> Optional[str]:
    """
    Shared row-to-grid formatter. Used by both Haiku (after parsing pipe text)
    and Sonnet sql_planner (after capturing tool_result rows). Returns:
      - A formatted [LEADERBOARD]/[STATGRID] block when the rows are gridable
      - A bold one-liner ("**1,234** Home Runs") when the result is a single
        scalar aggregate
      - None when the rows are too weird to render as a grid (caller should
        fall back to whatever text it had — narration, raw text, etc.)

    Column-cap rule (max 4 stat columns) and column prioritization
    (deprioritize date/diff/gap cols) live here so every formatter caller
    inherits the same discipline.
    """
    if not data_rows or not columns:
        return None

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
        if c.lower() in ("name", "player_name", "player", "p.name", "player_name_team"):
            name_col = c
            break
    # Fallback: if first column contains player-like values (has spaces, no digits), treat as name
    if not name_col and data_rows:
        first_col = columns[0]
        sample = str(data_rows[0].get(first_col, ""))
        if " " in sample and not sample.replace(" ", "").replace("(", "").replace(")", "").isdigit():
            name_col = first_col

    # Team-aggregation fallback: when there's no player name column but
    # there IS a team column, promote team to the row label. Otherwise
    # 'team' sits in stat_cols and competes with rate stats for the
    # 4-column cap — pushing OPS off the visible grid for team queries
    # like "best team OPS vs 4-seamers". Also translate the code to a
    # readable name right now (NYA → Yankees) since after the rename
    # below the column is called 'name', not 'team', so the formatter's
    # later team-code translation won't fire.
    if not name_col:
        team_col = next((c for c in columns if c.lower() in ("team", "team_code", "team_abbr", "team_abbreviation")), None)
        if team_col and data_rows:
            name_col = team_col
            for row in data_rows:
                raw = row.get(team_col)
                friendly = _maybe_team_name("team", raw)
                if friendly is not None:
                    row[team_col] = friendly
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

    # Ensure "season"/"year" comes AFTER the primary stat, not before
    # Haiku sometimes puts season first in the SELECT which makes it look like the sort column
    season_cols = [c for c in stat_cols if c.lower() in ("season", "year")]
    non_season = [c for c in stat_cols if c.lower() not in ("season", "year")]
    if season_cols and non_season:
        stat_cols = non_season[:1] + season_cols + non_season[1:]

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
                          "diff", "difference", "delta",
                          # Raw counters — useful for context but not the
                          # primary insight. Drop these first when the grid
                          # needs to shed columns so rate stats (OPS, ERA,
                          # etc.) stay visible.
                          "ab", "at_bats", "pa", "plate_appearances",
                          "ip", "ip_outs", "innings_pitched", "g", "games",
                          "tbf", "batters_faced"}
    if multi_row and len(stat_cols) > 4:
        high = [c for c in stat_cols if c.lower() not in _low_priority_cols]
        low = [c for c in stat_cols if c.lower() in _low_priority_cols]
        stat_cols = (high + low)[:4]

    # Remove columns that map to None (label-only columns that slipped through)
    stat_cols = [c for c in stat_cols if _display_col_name(c) is not None]

    if not stat_cols:
        return None

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
            if use_team_in_label and row.get("team"):
                raw_team = row.get("team", "")
                team_display = _maybe_team_name("team", raw_team) or raw_team
                team = f" ({team_display})"
            else:
                team = ""
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
        import traceback as _tb
        tb_str = _tb.format_exc()
        logger.warning("haiku_sql_gen_error error=%s", e)
        try:
            log_server_error(
                source="haiku_sql_gen",
                error_type=type(e).__name__,
                error_message=str(e),
                context={"question": question[:200], "traceback": tb_str[-1500:]},
            )
        except Exception:
            pass
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
        except Exception as retry_e:
            # Both attempts failed — log the retry failure (first SQL error is
            # expected and retry-worthy, but retry failure is a real signal).
            import traceback as _tb
            tb_str = _tb.format_exc()
            try:
                log_server_error(
                    source="haiku_sql_retry",
                    error_type=type(retry_e).__name__,
                    error_message=str(retry_e),
                    context={"question": question[:200],
                             "first_error": str(e)[:300],
                             "sql": (sql or "")[:500],
                             "traceback": tb_str[-1500:]},
                )
            except Exception:
                pass
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
    input_method: str = "keyboard"  # "keyboard" (default) or "mic"


@router.post("/query")
async def query(req: QueryRequest):
    return StreamingResponse(
        _stream(req.question, req.device_id, req.history, req.contextual, req.input_method),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


import re as _re
from services import name_matcher as _nm


_PRE_1898_PITCHING_STATS = {"wins", "complete_games", "win", "W", "CG"}
_PRE_1898_NOTE = "\n[DISCLAIMER]Data begins in 1898. Career totals for pre-1898 pitchers (Cy Young, Pud Galvin, others) may be incomplete.[/DISCLAIMER]"


def _add_pre1898_note(text: str, question: str) -> str:
    """Add a note about pre-1898 data when showing all-time pitching career stats."""
    lower = question.lower()
    # Only for all-time / career queries, not single-season
    is_alltime = any(kw in lower for kw in ["all time", "all-time", "career", "ever", "history"])
    if not is_alltime:
        return text
    # Only wins and complete games — K/ERA records are post-1898
    is_pitching_affected = any(kw in lower for kw in ["wins", "most wins", "complete game", "complete games"])
    if not is_pitching_affected:
        return text
    # Don't double-add
    if "1898" in text:
        return text
    # Add before the last [/LEADERBOARD] or at the end
    if "[/LEADERBOARD]" in text:
        return text.replace("[/LEADERBOARD]", f"[/LEADERBOARD]{_PRE_1898_NOTE}", 1)
    return text + _PRE_1898_NOTE


def _strip_bold_title(text: str, original_question: str = "") -> str:
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
            if not scope and "active" in title_content.lower():
                scope = "Active"
            # Don't show "All-Time" or "Career" — that's the default, not an inference

        # Skip scope display for defaults and explicit mentions
        if scope in ("All-Time", "Career", "Active", "Career (Active)"):
            scope = None
        elif scope and original_question:
            oq = original_question.lower()
            # If the user explicitly mentioned the year/timeframe, don't restate
            if (scope.isdigit() and scope in oq) or \
               ("this season" in oq or "this year" in oq) or \
               ("last season" in oq or "last year" in oq) or \
               (scope.startswith("Since ") and "since" in oq) or \
               ("vs" in oq and _re.search(r'20[012]\d.*vs.*20[012]\d', oq)):
                scope = None

        # Check if next line has a count like "9 matched." or "14 matched."
        count_line = ""
        rest_start = 1
        if len(lines) > 1:
            next_line = lines[1].strip()
            count_match = _re.match(r'^(\d+)\s+(matched|players?)\.?$', next_line)
            if count_match:
                count_line = f"{count_match.group(1)} {count_match.group(2)}"
                rest_start = 2

        # Build context line
        subtitle_parts = []
        if scope:
            if scope.startswith("Since "):
                subtitle_parts.append(f"Showing results since {scope[6:]}")
            else:
                subtitle_parts.append(f"Showing results for {scope}")
        if count_line:
            subtitle_parts.append(count_line)

        rest = "\n".join(lines[rest_start:])

        # Merge any existing SUBTITLE into the context line
        existing_sub = _re.search(r'\[SUBTITLE\](.*?)\[/SUBTITLE\]', rest)
        if existing_sub:
            sub_text = existing_sub.group(1).strip()
            # Shorten verbose phrasing
            sub_text = _re.sub(r'Showing (?:hitters|pitchers|players) on pace for \d+\+ (?:PA|IP)\s*\(', '', sub_text)
            sub_text = sub_text.rstrip(')')
            sub_text = sub_text.strip()
            if sub_text:
                subtitle_parts.append(sub_text)
            rest = rest[:existing_sub.start()] + rest[existing_sub.end():]

        if subtitle_parts:
            return f"[CONTEXT]{' · '.join(subtitle_parts)}[/CONTEXT]\n{rest}"
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
    if not player:
        # Fallback: try match_player_with_prominence on individual words
        # Handles cases like "Soto OPS 2025" where find_player_in_text might miss
        for word in q.split():
            word_clean = word.strip("?.!,")
            if len(word_clean) < 2:
                continue
            result = _nm.match_player_with_prominence(word_clean)
            if result:
                player = result[0]
                break
    if player:
        ctx["player"] = player

    # Extract stat keyword — check common stat words in the prior query.
    # Also record the matched phrase (ctx["stat_phrase"]) so we can do
    # surgical string substitution on follow-up stat swaps without losing
    # other dimensions (splits, scope, etc.) from the prior query.
    lower_q = q.lower()
    stat_keywords = [
        ("home runs", "home runs"), ("homers", "home runs"), ("hr", "home runs"),
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
        if _re.search(r'\b' + _re.escape(keyword) + r'\b', lower_q):
            ctx["stat"] = canonical
            ctx["stat_phrase"] = keyword  # as it appears in the prior query
            break

    return ctx


_TIME_SCOPE_PATTERN = _re.compile(
    r'\b(?:all[- ]?time|career|career stats|lifetime|since \d{4}|in history|'
    r'this season|this year|last season|last year|'
    r'this decade|last decade|in the last decade|'
    r'last \d+ (?:seasons?|years?|games?|game)|past \d+ (?:seasons?|years?|games?|game)|'
    r'over (?:the )?last \d+ (?:seasons?|years?|games?|game)|'
    r'20[012]\d[-–]20[012]\d|20[012]\d)\b',
    flags=_re.IGNORECASE,
)

def _strip_time_scope(query: str) -> str:
    """Remove any existing time/scope reference from a query so we can
    substitute a new one. Used when the follow-up pivots the time dimension
    (career ↔ season ↔ year range ↔ last N games)."""
    cleaned = _TIME_SCOPE_PATTERN.sub('', query)
    return _re.sub(r'\s+', ' ', cleaned).strip()


# Word-to-int for "two years ago" style phrases
_NUM_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}


# Follow-up phrasings that mean "show me more of that list"
_MORE_PHRASES = {
    "what else", "who else", "anyone else", "anybody else",
    "any more", "any more players", "any others", "any other players",
    "show me more", "show more", "give me more", "give me some more",
    "more", "more players", "keep going", "tell me more",
    "next few", "next ones", "next", "the rest",
}


def _get_last_assistant_response(history: list[dict]) -> Optional[str]:
    """Return the text of the most recent assistant message, or None."""
    for msg in reversed(history):
        if msg.get("role") == "assistant":
            c = msg.get("content")
            if isinstance(c, str):
                return c
    return None


def _parse_list_state(assistant_text: Optional[str]) -> Optional[dict]:
    """Parse a [LIST_STATE:...] trailer from a prior assistant response.
    Returns {'state': 'complete'} or {'state': 'truncated', 'limit': N} or None."""
    if not assistant_text:
        return None
    m = _re.search(r'\[LIST_STATE:truncated:(\d+)\]', assistant_text)
    if m:
        return {"state": "truncated", "limit": int(m.group(1))}
    if "[LIST_STATE:complete]" in assistant_text:
        return {"state": "complete"}
    return None


def _local_followup_canned(question: str, history: list[dict]) -> Optional[str]:
    """Intercept follow-ups that deserve a canned response rather than a new
    query. Currently only handles 'what else?' / 'who else?' on a list that
    we know is already complete — replies 'That's the full list' without
    re-running anything."""
    clean = question.strip().lower().rstrip('?.!')
    # Strip common prefixes
    for prefix in ["how about ", "what about ", "and ", "so ", "um "]:
        if clean.startswith(prefix):
            clean = clean[len(prefix):].strip()
            break
    if clean not in _MORE_PHRASES:
        return None
    state = _parse_list_state(_get_last_assistant_response(history))
    if state and state["state"] == "complete":
        return ("That's the full list — those are all the players who qualify.\n\n"
                "_Want to search for something else? Try one of the suggestions below._")
    return None


def _local_followup_rewrite(question: str, history: list[dict]) -> Optional[str]:
    """Try to rewrite a follow-up question locally without calling Haiku.
    Returns the rewritten standalone query, or None to fall through to Haiku.

    Design: mutates `ctx["query"]` (the prior standalone query) along ONE
    dimension at a time, preserving other dimensions. E.g. if prior was
    'Judge HRs career', 'what about vs 4-seamers' → 'Judge HRs career vs 4-seamers';
    'what about last 3 seasons' → 'Judge HRs last 3 seasons' (career stripped)."""
    lower = question.strip().lower()
    ctx = _extract_prior_context(history)

    if not ctx["query"]:
        return None

    player = ctx["player"]
    stat = ctx["stat"]
    season = ctx["season"]

    # Prefix-stripped form for modifier matching. Pattern 1 (swap) still uses
    # raw `lower` since its regex captures "what about X" explicitly.
    clean = lower.rstrip('?.').strip()
    for prefix in ["how about ", "what about ", "and about ", "and ", "how did ", "what did "]:
        if clean.startswith(prefix):
            clean = clean[len(prefix):]
            break

    # --- Pattern 0: "what else" / "who else" on a truncated list ---
    # (The "complete" case is handled upstream by _local_followup_canned,
    # which returns a canned response without re-querying.)
    if clean in _MORE_PHRASES:
        state = _parse_list_state(_get_last_assistant_response(history))
        if state and state["state"] == "truncated":
            prior_limit = state["limit"]
            new_limit = max(prior_limit * 2, 25)  # at least double, floor of 25
            prior_q = ctx["query"]
            # Strip any existing "top N" prefix from prior query
            prior_q = _re.sub(r'^\s*(?:top|best)\s+\d+\s+', '', prior_q, flags=_re.IGNORECASE).strip()
            return f"top {new_limit} {prior_q}"

    # --- Pattern 1: Player swap ---
    # "what about Soto", "and Soto?", "how about Ohtani", "and his?"
    swap_match = _re.match(
        r'^(?:what about|how about|and|how did|what did)\s+(.+?)[\?\.]?$', lower)
    if swap_match:
        name_text = swap_match.group(1).strip().rstrip('?.')
        # (Year swaps like "what about 2023" and time phrases like "what about
        # last year" are handled later via the shared `clean` form in Pattern 7.
        # That path strips only the time dimension from the prior query,
        # preserving splits and other modifiers — unlike rebuilding from
        # player+stat+year which would lose them.)
        if _re.match(r'^20[012]\d$', name_text):
            pass  # fall through to Pattern 7
        # Skip if it's a stat ("and his RBI?", "what about strikeouts?")
        name_text_clean = name_text.replace("his ", "").replace("her ", "")
        stat_match = _nm.match_stat(name_text_clean)
        if stat_match and player:
            # Preserve other dimensions (splits, scope, etc.) by substituting
            # the old stat phrase in the prior query. Fall back to rebuild if
            # we can't identify the old stat phrase.
            prior_stat_phrase = ctx.get("stat_phrase")
            if prior_stat_phrase and _re.search(r'\b' + _re.escape(prior_stat_phrase) + r'\b',
                                                ctx["query"], flags=_re.IGNORECASE):
                return _re.sub(r'\b' + _re.escape(prior_stat_phrase) + r'\b',
                               name_text_clean, ctx["query"], flags=_re.IGNORECASE, count=1)
            season_part = f" {season}" if season else ""
            return f"{player} {name_text_clean}{season_part}"
        # Try as a player name — use prominence first for multi-player names like "De La Cruz"
        prominence = _nm.match_player_with_prominence(name_text)
        if prominence:
            new_player = prominence[0]
        else:
            new_player = _nm.find_player_in_text(name_text) or _nm.match_player(name_text)
        if new_player and stat:
            season_part = f" {season}" if season else ""
            return f"{new_player} {stat}{season_part}"
        elif new_player and player:
            # No stat extracted but we have prior player — use prior query structure
            return ctx["query"].replace(player, new_player)

    # --- Pattern 2: Career ---
    # "career", "for his career", "over her career", "lifetime", "all-time", etc.
    career_phrases = {
        "career", "career stats", "for his career", "for her career",
        "for their career", "in his career", "in her career", "in their career",
        "over his career", "over her career", "over their career",
        "for his whole career", "for her whole career",
        "lifetime", "all-time", "all time", "alltime",
        "his career", "her career", "their career",
    }
    if clean in career_phrases:
        # Stat-less prior (current_form / streak / "doing lately"): rebuild
        # from player so narrative triggers don't survive. _strip_time_scope
        # only strips time phrases — it doesn't strip "doing lately" — so a
        # naive append produces a query that re-routes to current_form.
        if not stat and player:
            return f"{player} career"
        cleaned = _strip_time_scope(ctx["query"])
        if cleaned:
            return f"{cleaned} career"

    # --- Pattern 3: Splits pivot (handedness, home/away, pitch type, RISP, day/night, postseason) ---
    # Map of phrase → canonical split suffix. Additive to the prior query
    # (unlike time scope, these stack: career + vs lefties + in the playoffs).
    splits_patterns = {
        # Handedness (platoon)
        "vs lefties": "vs lefties", "against lefties": "vs lefties",
        "vs left": "vs lefties", "against left": "vs lefties",
        "vs left-handers": "vs lefties", "against left-handers": "vs lefties",
        "vs lhp": "vs lefties", "against lhp": "vs lefties",
        "facing lefties": "vs lefties", "facing left-handers": "vs lefties",
        "facing lhp": "vs lefties",
        "vs righties": "vs righties", "against righties": "vs righties",
        "vs right": "vs righties", "against right": "vs righties",
        "vs right-handers": "vs righties", "against right-handers": "vs righties",
        "vs rhp": "vs righties", "against rhp": "vs righties",
        "facing righties": "vs righties", "facing right-handers": "vs righties",
        "facing rhp": "vs righties",
        # Home/away
        "at home": "at home", "home": "at home",
        "on the road": "on the road", "away": "on the road",
        "in road games": "on the road", "road games": "on the road",
        # Day/night
        "in day games": "in day games", "day games": "in day games", "during the day": "in day games",
        "in night games": "at night", "at night": "at night", "night games": "at night",
        # RISP / clutch
        "with risp": "with RISP", "risp": "with RISP",
        "with runners in scoring position": "with RISP",
        "runners in scoring position": "with RISP",
        "in the clutch": "in clutch", "in clutch": "in clutch",
        # Postseason
        "in the playoffs": "in the playoffs", "the playoffs": "in the playoffs", "playoffs": "in the playoffs",
        "in the postseason": "in the postseason", "postseason": "in the postseason", "the postseason": "in the postseason",
        "in the world series": "in the World Series", "world series": "in the World Series",
        # Pitch types
        "vs fastballs": "vs fastballs", "against fastballs": "vs fastballs",
        "vs 4-seamers": "vs 4-seamers", "against 4-seamers": "vs 4-seamers",
        "vs four-seamers": "vs 4-seamers", "against four-seamers": "vs 4-seamers",
        "vs sinkers": "vs sinkers", "against sinkers": "vs sinkers",
        "vs sliders": "vs sliders", "against sliders": "vs sliders",
        "vs curves": "vs curveballs", "against curves": "vs curveballs",
        "vs curveballs": "vs curveballs", "against curveballs": "vs curveballs",
        "vs changeups": "vs changeups", "against changeups": "vs changeups",
        "vs cutters": "vs cutters", "against cutters": "vs cutters",
        "vs splitters": "vs splitters", "against splitters": "vs splitters",
        "vs sweepers": "vs sweepers", "against sweepers": "vs sweepers",
        "vs knuckleballs": "vs knuckleballs", "against knuckleballs": "vs knuckleballs",
    }
    if clean in splits_patterns:
        split = splits_patterns[clean]
        # When the prior query has no explicit stat (current-form, streak,
        # "how is X doing lately"), the prior query carries narrative triggers
        # like "doing lately" that re-route to current_form even after we
        # append the split. Naive append produces "How is Trout doing
        # lately? vs lefties" — still parsed as current_form, ignoring
        # the split. Rebuild from player+season instead so the split parser
        # actually sees a clean target.
        if not stat and player:
            season_part = f" {season}" if season else ""
            return f"{player} {split}{season_part}".strip()

        # Stat-specific prior — additive append is correct. Strip any prior
        # split of the same category first so we don't end up with "vs lefties
        # vs righties" or "at home on the road".
        base = ctx["query"]
        handedness = {"vs lefties", "vs righties"}
        homeaway = {"at home", "on the road"}
        daynight = {"in day games", "at night"}
        risp_clutch = {"with RISP", "in clutch"}
        postseason = {"in the playoffs", "in the postseason", "in the World Series"}
        pitch_types = {v for k, v in splits_patterns.items() if v.startswith("vs ") and v not in handedness}
        category = None
        if split in handedness: category = handedness
        elif split in homeaway: category = homeaway
        elif split in daynight: category = daynight
        elif split in risp_clutch: category = risp_clutch
        elif split in postseason: category = postseason
        elif split in pitch_types: category = pitch_types
        if category:
            for existing in category:
                base = _re.sub(r'\b' + _re.escape(existing) + r'\b', '', base, flags=_re.IGNORECASE)
            base = _re.sub(r'\s+', ' ', base).strip()
        return f"{base} {split}".strip()

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

    # --- Pattern 6: League filter ---
    # "in the NL", "in the AL", "NL", "AL", "American League", "National League"
    league_match = _re.match(
        r'^(?:in the\s+)?(?:(?:al|american league|a\.l\.)|(?:nl|national league|n\.l\.))[\?\.]?$', clean)
    if league_match:
        query = ctx["query"]
        # Determine which league
        if _re.search(r'(?:^|\b)(?:al|american league|a\.l\.)(?:\b|$)', clean):
            league = "AL"
        else:
            league = "NL"
        # Append league to the prior query
        return f"{query} {league}"

    # --- Pattern 6.5: Compound split + time ---
    # e.g. "against changeups in the last 3 seasons", "vs lefties this year",
    # "at home over the last 5 games". We parse the split prefix, then check
    # the remainder for a known time phrase.
    def _n_from_match(raw: str) -> int:
        return int(raw) if raw.isdigit() else _NUM_WORDS.get(raw, 1)
    _time_suffix_patterns = [
        (r'^(?:in\s+)?(?:the\s+)?(?:last|past)\s+(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+games?$',
         lambda m: f"last {_n_from_match(m.group(1))} games"),
        (r'^(?:in\s+)?(?:the\s+)?(?:last|past)\s+(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+(?:seasons?|years?)$',
         lambda m: f"last {_n_from_match(m.group(1))} seasons"),
        (r'^(?:in|for)\s+(20[012]\d)$', lambda m: m.group(1)),
        (r'^since\s+(20[012]\d)$', lambda m: f"since {m.group(1)}"),
        (r'^(?:in\s+)?(this\s+(?:season|year))$',
         lambda m: "this season"),
        (r'^(?:in\s+)?(last\s+(?:season|year))$',
         lambda m: "last season"),
    ]
    # Try splitting clean into (split_phrase) + (time_phrase) in EITHER order.
    # Split keys iterated longest-first to avoid premature matches
    # ("vs left" before "vs left-handers").
    _sorted_splits = sorted(splits_patterns.keys(), key=len, reverse=True)
    _compound_match = None
    # Pass 1: split at start, time at end — "against changeups in the last 3 seasons"
    for split_key in _sorted_splits:
        if clean.startswith(split_key + " "):
            remainder = clean[len(split_key):].strip()
            for pat, builder in _time_suffix_patterns:
                m = _re.match(pat, remainder)
                if m:
                    ts = builder(m)
                    if ts:
                        _compound_match = (split_key, ts)
                        break
            if _compound_match:
                break
            break  # found split prefix, no matching time — don't try other splits
    # Pass 2: time at start, split at end — "in the last 3 seasons against changeups"
    if not _compound_match:
        for split_key in _sorted_splits:
            if clean.endswith(" " + split_key):
                prefix_text = clean[:-len(split_key)].strip()
                for pat, builder in _time_suffix_patterns:
                    m = _re.match(pat, prefix_text)
                    if m:
                        ts = builder(m)
                        if ts:
                            _compound_match = (split_key, ts)
                            break
                if _compound_match:
                    break
                break
    if _compound_match:
        split_key, time_suffix = _compound_match
        split = splits_patterns[split_key]
        # Stat-less prior: rebuild from player (see Pattern 3 rationale).
        if not stat and player:
            return f"{player} {split} {time_suffix}".strip()
        # Strip time scope from prior, strip same-category split, then append both
        base = _strip_time_scope(ctx["query"])
        handedness = {"vs lefties", "vs righties"}
        homeaway = {"at home", "on the road"}
        daynight = {"in day games", "at night"}
        risp_clutch = {"with RISP", "in clutch"}
        postseason_set = {"in the playoffs", "in the postseason", "in the World Series"}
        pitch_types = {v for k, v in splits_patterns.items() if v.startswith("vs ") and v not in handedness}
        cat = None
        if split in handedness: cat = handedness
        elif split in homeaway: cat = homeaway
        elif split in daynight: cat = daynight
        elif split in risp_clutch: cat = risp_clutch
        elif split in postseason_set: cat = postseason_set
        elif split in pitch_types: cat = pitch_types
        if cat:
            for existing in cat:
                base = _re.sub(r'\b' + _re.escape(existing) + r'\b', '', base, flags=_re.IGNORECASE)
            base = _re.sub(r'\s+', ' ', base).strip()
        return f"{base} {split} {time_suffix}".strip()

    # --- Pattern 7: Time scope pivots (mutually-exclusive with career/ranges) ---
    # All strip any existing time scope from prior query and append the new one.
    # Stat-less prior gets a rebuild from player so narrative triggers like
    # "doing lately" don't override the new time scope. _strip_time_scope only
    # strips time phrases.

    # "since 2010", "after 2010"
    m = _re.match(r'^(?:since|after)\s+(20[012]\d)$', clean)
    if m:
        if not stat and player:
            return f"{player} since {m.group(1)}".strip()
        cleaned = _strip_time_scope(ctx["query"])
        return f"{cleaned} since {m.group(1)}".strip()

    # "in 2023", "for 2023", bare "2023"
    m = _re.match(r'^(?:in|for)?\s*(20[012]\d)$', clean)
    if m and clean in (m.group(1), f"in {m.group(1)}", f"for {m.group(1)}"):
        if not stat and player:
            return f"{player} {m.group(1)}".strip()
        cleaned = _strip_time_scope(ctx["query"])
        return f"{cleaned} {m.group(1)}".strip()

    # "this season" / "this year"
    if clean in ("this season", "this year"):
        if not stat and player:
            return f"{player} this season".strip()
        cleaned = _strip_time_scope(ctx["query"])
        return f"{cleaned} this season".strip()

    # "last season" / "last year"
    if clean in ("last season", "last year"):
        if not stat and player:
            return f"{player} last season".strip()
        cleaned = _strip_time_scope(ctx["query"])
        return f"{cleaned} last season".strip()

    # --- Pattern 8: "last N games" / "over the last 4 games" / "past 5 games" ---
    m = _re.match(r'^(?:over\s+)?(?:the\s+)?(?:last|past)\s+(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+games?$', clean)
    if m:
        n_raw = m.group(1)
        n = int(n_raw) if n_raw.isdigit() else _NUM_WORDS.get(n_raw, 1)
        # For stat-less priors (current_form / streak), rebuild as a current-form
        # query for {player} over last N games — the natural-language version
        # routes through parse_current_form with the slider preset to N.
        if not stat and player:
            return f"how is {player} doing over last {n} games".strip()
        cleaned = _strip_time_scope(ctx["query"])
        return f"{cleaned} last {n} games".strip()

    # --- Pattern 9: "last N seasons" / "last N years" / "over the last 3 seasons" ---
    m = _re.match(r'^(?:over\s+)?(?:the\s+)?(?:last|past)\s+(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+(?:seasons?|years?)$', clean)
    if m:
        n_raw = m.group(1)
        n = int(n_raw) if n_raw.isdigit() else _NUM_WORDS.get(n_raw, 1)
        cleaned = _strip_time_scope(ctx["query"])
        return f"{cleaned} last {n} seasons".strip()

    # --- Pattern 10: "N years ago" / "two years ago" ---
    m = _re.match(r'^(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+years?\s+ago$', clean)
    if m:
        n_raw = m.group(1)
        n = int(n_raw) if n_raw.isdigit() else _NUM_WORDS.get(n_raw, 1)
        from datetime import date as _date
        target_year = _date.today().year - n
        cleaned = _strip_time_scope(ctx["query"])
        return f"{cleaned} {target_year}".strip()

    # --- Pattern 11: "this decade" / "last decade" ---
    if clean in ("this decade", "in this decade", "in the current decade"):
        from datetime import date as _date
        current = _date.today().year
        start = (current // 10) * 10
        cleaned = _strip_time_scope(ctx["query"])
        return f"{cleaned} since {start}".strip()

    if clean in ("last decade", "in the last decade", "the last decade", "in the previous decade"):
        from datetime import date as _date
        current = _date.today().year
        start = (current // 10) * 10 - 10
        end = start + 9
        cleaned = _strip_time_scope(ctx["query"])
        return f"{cleaned} {start}-{end}".strip()

    return None


async def _stream(question: str, device_id: str, history: list[dict], contextual: bool = False, input_method: str = "keyboard"):
    """Core pipeline: quota check → route → SQL → execute → stream answer."""
    original_question = question  # Before any rewriting
    # Wall-clock at handler entry. Every log_query call below passes
    # duration_ms so the dashboard can surface slow queries and we can
    # later prioritize materialization based on real latency × volume.
    t0 = time.time()

    def _elapsed_ms() -> int:
        return int((time.time() - t0) * 1000)

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
        log_query(question, device_id, "sonnet", input_method=input_method, duration_ms=_elapsed_ms())
        return

    # 2. Follow-up rewrite — try local patterns BEFORE interceptor so short
    # follow-ups like "what about Soto" get rewritten, not intercepted as-is.
    rewritten_query: str | None = None
    if history and len(question.split()) < 10:
        # 2a. Canned responses for follow-ups that don't need a new query
        # (e.g., "what else?" on a list we already know is complete).
        canned = _local_followup_canned(question, history)
        if canned is not None:
            logger.info("followup_canned question=%r", question)
            yield event({"type": "text", "text": canned})
            yield event({"type": "done", "intercepted": True})
            increment_count(device_id)
            log_query(question, device_id, "query engine", is_followup=True, duration_ms=_elapsed_ms(),
                      original_query=original_question, input_method=input_method)
            return

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
                intercepted = _strip_bold_title(intercepted, original_question)
                intercepted = _add_pre1898_note(intercepted, original_question)
                yield event({"type": "text", "text": intercepted})
                done_event = {"type": "done", "intercepted": True}
                done_event["rewritten_query"] = local_rewrite
                yield event(done_event)
                increment_count(device_id)
                log_query(local_rewrite, device_id, "query engine", is_followup=True, original_query=original_question, input_method=input_method, duration_ms=_elapsed_ms())
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
                    intercepted = _strip_bold_title(intercepted, original_question)
                    intercepted = _add_pre1898_note(intercepted, original_question)
                    yield event({"type": "text", "text": intercepted})
                    done_event = {"type": "done", "intercepted": True}
                    if rewritten_query:
                        done_event["rewritten_query"] = rewritten_query
                    yield event(done_event)
                    increment_count(device_id)
                    log_query(rewritten, device_id, "intercepted", is_followup=True, original_query=original_question, input_method=input_method, duration_ms=_elapsed_ms())
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
            log_query(question, device_id, "sonnet", input_method=input_method, duration_ms=_elapsed_ms())
            return

    # 2b. Insight query detection — check before interceptor
    def _is_insight_query(q: str) -> bool:
        """Detect queries needing multi-step reasoning, not a single SQL query."""
        lower = q.lower()
        keyword_signals = [
            "optimal", "lineup", "build me", "build a",
            "roster", "draft", "fantasy",
            "pinch hit", "pinch-hit",
            "best at each", "worst at each", "by position",
            "outperforming", "underperforming",
            "if i were", "if you were",
            "strategy", "should i",
            "hot right now", "hottest right now", "on fire right now",
            "coldest right now", "struggling right now",
            "who should i watch", "most exciting",
        ]
        if any(kw in lower for kw in keyword_signals):
            return True
        cross_entity = [
            "across teams", "across the league",
            "best player on each", "worst player on each",
        ]
        if any(ce in lower for ce in cross_entity):
            return True
        # "each team" / "every team" / "per team" — only insight if NOT a simple
        # per-team leaderboard (which the query engine handles)
        if any(ce in lower for ce in ["each team", "every team", "per team"]):
            if not _re.search(r'\b\w+\s*(leaders?|leader)\b.*\b(each|every|per)\s+team\b', lower) \
               and not _re.search(r'\b(each|every|per)\s+team\b.*\b(leaders?|leader)\b', lower):
                return True
        conditionals = ["if .* then", "assuming", "given that",
                        "what would happen", "how would", "simulate", "predict"]
        for pattern in conditionals:
            if _re.search(pattern, lower):
                return True
        return False

    is_insight = _is_insight_query(question)
    if is_insight:
        logger.info("insight_query_detected question=%r", question)

    # 2c. Try local intercept — zero Claude API cost (skip for insight queries)
    if is_insight:
        intercepted = None
    else:
        try:
            intercepted = try_intercept(question)
        except Exception as e:
            logger.error("intercept_error question=%r error=%s type=%s", question, e, type(e).__name__)
            try:
                from services.metering import log_query as _lq
                _lq(question, device_id, "query_engine_error")
            except Exception:
                pass
            intercepted = None
    if intercepted is not None:
        no_count = intercepted.startswith("__NO_COUNT__")
        if no_count:
            intercepted = intercepted.replace("__NO_COUNT__", "", 1)
        logger.info("query_intercepted question=%r no_count=%s", question, no_count)
        intercepted = _strip_bold_title(intercepted, original_question)
        intercepted = _add_pre1898_note(intercepted, original_question)
        yield event({"type": "text", "text": intercepted})
        done_event = {"type": "done", "intercepted": True}
        if rewritten_query:
            done_event["rewritten_query"] = rewritten_query
        yield event(done_event)
        if not no_count:
            increment_count(device_id)
        log_query(question, device_id, "query engine", duration_ms=_elapsed_ms(),
                  is_followup=bool(rewritten_query), original_query=original_question if rewritten_query else None,
                  input_method=input_method)
        return

    # Haiku SQL fallback — skip for insight queries (need insight engine)
    if not is_insight:
        haiku_result = await _try_haiku_sql(question)
    else:
        haiku_result = None
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
        log_query(question, device_id, "haiku", duration_ms=_elapsed_ms(),
                  is_followup=bool(rewritten_query), original_query=original_question if rewritten_query else None,
                  input_method=input_method)
        return

    # 4. Insight engine — multi-step Sonnet reasoning for complex queries
    try:
        import asyncio
        from concurrent.futures import ThreadPoolExecutor
        from services.sql_planner import plan_and_execute

        _executor = ThreadPoolExecutor(max_workers=1)
        loop = asyncio.get_event_loop()
        future = loop.run_in_executor(_executor, plan_and_execute, question)

        # Send heartbeat while insight engine works (keeps nginx alive)
        while not future.done():
            yield event({"type": "text", "text": ""})
            await asyncio.sleep(2)

        insight_result = future.result()
        if insight_result:
            logger.info("query_insight question=%r", question)
            insight_text: str = insight_result.get("text", "") if isinstance(insight_result, dict) else str(insight_result)

            # If sql_planner captured structured rows from its primary
            # tool_result, run them through the shared row-to-grid
            # formatter. Same code path Haiku uses, so column-cap and
            # name-column rules are identical. Returns None when the
            # data isn't gridable (single scalar, weird shape) — in
            # that case we send Sonnet's prose alone, preserving the
            # rich narrative fallback for definitional / opinion /
            # historical-narrative questions.
            grid_block: Optional[str] = None
            if isinstance(insight_result, dict):
                cols = insight_result.get("columns")
                rows = insight_result.get("rows")
                if cols and rows:
                    rows_as_dicts = [dict(zip(cols, r)) for r in rows]
                    grid_block = _format_structured_rows_to_grid(
                        list(cols), rows_as_dicts, total_count=None,
                        question=question,
                    )

            insight_text = _strip_bold_title(insight_text, original_question)
            insight_text = _add_pre1898_note(insight_text, original_question)
            if grid_block:
                payload = grid_block + "\n\n" + insight_text
            else:
                payload = insight_text
            payload += "\n\n[AIDISCLAIMER]Stats verified against our database. Analysis is AI-generated.[/AIDISCLAIMER]"
            yield event({"type": "text", "text": payload})
            done_event = {"type": "done", "insight": True}
            if rewritten_query:
                done_event["rewritten_query"] = rewritten_query
            yield event(done_event)
            increment_count(device_id)
            log_query(question, device_id, "sonnet", duration_ms=_elapsed_ms(),
                      is_followup=bool(rewritten_query), original_query=original_question if rewritten_query else None,
                      input_method=input_method)
            return
    except Exception as e:
        import traceback as _tb
        tb_str = _tb.format_exc()
        logger.warning("insight_engine_error error=%s\n%s", e, tb_str)
        try:
            log_server_error(
                source="insight_engine_wrapper",
                error_type=type(e).__name__,
                error_message=str(e),
                context={"question": question[:200], "traceback": tb_str[-1500:]},
                device_id=device_id,
            )
        except Exception:
            pass

    # 5. Knowledge mode — answer from Claude's baseball knowledge
    # If we got here, nothing else could answer.
    logger.info("knowledge_mode question=%r", question)
    try:
        async for chunk in llm.stream_knowledge(question, history):
            yield event({"type": "text", "text": chunk})
    except Exception as e:
        # Surface the exception: log to dashboard metering DB + gunicorn logger.
        # Previously swallowed silently, masking real Sonnet failures behind the
        # generic "I'm not sure about that" text.
        import traceback as _tb
        tb_str = _tb.format_exc()
        logger.error("knowledge_mode_error question=%r type=%s error=%s\n%s",
                     question, type(e).__name__, e, tb_str)
        try:
            log_server_error(
                source="knowledge_mode",
                error_type=type(e).__name__,
                error_message=str(e),
                context={"question": question[:200], "traceback": tb_str[-1500:]},
                device_id=device_id,
            )
        except Exception:
            pass
        yield event({"type": "text", "text": "I'm not sure about that. Try asking about player stats, leaders, or comparisons."})

    # Disclaimer for knowledge-mode responses — these come from AI, not our verified DB
    yield event({"type": "text", "text": "\n\n[AIDISCLAIMER]Answered by AI from general knowledge, not verified against our database. AI can make mistakes.[/AIDISCLAIMER]"})

    done_event: dict = {"type": "done"}
    if rewritten_query:
        done_event["rewritten_query"] = rewritten_query
    yield event(done_event)
    increment_count(device_id)
    log_query(question, device_id, "sonnet", duration_ms=_elapsed_ms(),
              is_followup=bool(rewritten_query), original_query=original_question if rewritten_query else None,
              input_method=input_method)
