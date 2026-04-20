"""
AI-powered notable events detection using Sonnet.

Runs once per day after the rule-based detection. Compiles a data snapshot
from the DB (yesterday's games, leaders, historical context) and asks Sonnet
to find 5-8 interesting storylines the rules missed.

Cost: ~$0.02-0.04/day (~$1/month). Runs once for ALL users.
"""

import json
import os
import re
import sqlite3
from datetime import date


# Below-floor stat-threshold anchor filter. Sonnet has tried to anchor on
# "Nth career 4-RBI game", "matches his career high in RBI" etc. even when
# the threshold is below the structural detector's floor. We catch only the
# specific anchor-shaped phrases — NOT box-score mentions of the same number
# elsewhere in the headline.
#
# Floors mirror the structural detector's career-high checks:
#   hits: 4+, RBI: 5+, HR: 2+ (multi-HR), K: 10+
_FLOORS = {"hits": 4, "rbi": 5, "hr": 2, "k": 10}


def _stat_key(s):
    """Normalize a stat phrase ('homers', 'home runs', 'RBI', 'hit') to floor key."""
    s = s.lower().rstrip("s")
    if s in ("hit",): return "hits"
    if s == "rbi": return "rbi"
    if s in ("hr", "homer", "home run"): return "hr"
    if s in ("k", "strikeout"): return "k"
    return None


# Each entry: (compiled regex, threshold-group-index, stat-group-index).
# All three patterns require the threshold to appear INSIDE the anchor
# construct itself — not just anywhere in the headline.
_STAT_ALT = r"(hits?|RBI|HRs?|homers?|home\s+runs?|Ks?|strikeouts?)"
_ANCHOR_FLOOR_PATTERNS = [
    # A. "his Nth (career) K-stat game/start/outing"
    #    Matches: "his 4th career 4-RBI game", "his 2nd 11-K start"
    #    Skips:   "his 20th career multi-homer game" (no numeric threshold)
    (re.compile(
        rf"\b\d+(?:st|nd|rd|th)\s+(?:career\s+)?(\d+)\+?\s*-?\s*{_STAT_ALT}\s+(?:game|start|outing|performance)",
        re.I,
    ), 1, 2),

    # B. "his Nth (career) game/start/outing with K+ stat"
    #    Matches: "his 16th game with 4+ hits", "his 18th career start with 10+ K"
    (re.compile(
        rf"\b\d+(?:st|nd|rd|th)\s+(?:career\s+)?(?:games?|starts?|outings?)\s+with\s+(\d+)\+?\s+{_STAT_ALT}",
        re.I,
    ), 1, 2),

    # C. "matches/ties his career high in stat (K)"
    #    Matches: "matches his career high in RBI (4)"
    #    Note: stat is group 1, threshold is group 2
    (re.compile(
        rf"(?:matches?|ties?|tying|matching)\s+his\s+career\s+high\s+in\s+{_STAT_ALT}\s*\((\d+)\)",
        re.I,
    ), 2, 1),
]


def _strip_below_floor_anchors(events):
    """Drop events whose headline anchors on a stat threshold below the
    structural detector's floor. Only catches threshold values appearing
    INSIDE an anchor-shaped phrase — box-score mentions of the same number
    elsewhere in the headline pass through.

    Drops:
      - "his 4th career 4-RBI game" (4 < 5)
      - "matches his career high in RBI (4)" (4 < 5)
      - "his 16th game with 3+ hits" (3 < 4)

    Keeps:
      - "Bellinger went 3-for-4 with 4 RBI — his 20th career multi-HR game"
        (4 RBI is box-score, not anchor; "20th multi-HR" anchor has no
        numeric threshold)
      - "his 16th game with 4+ hits in his 12-year career" (4 == floor)
      - "his 18th career start with 10+ K" (10 == floor)
    """
    cleaned = []
    for e in events:
        h = e.get("headline", "")
        below = False
        for pat, t_group, s_group in _ANCHOR_FLOOR_PATTERNS:
            for m in pat.finditer(h):
                try:
                    val = int(m.group(t_group))
                except (ValueError, IndexError):
                    continue
                stat_key = _stat_key(m.group(s_group))
                if stat_key and val < _FLOORS[stat_key]:
                    below = True
                    break
            if below:
                break
        if not below:
            cleaned.append(e)
    return cleaned

DB_PATH = os.getenv("DB_PATH", "/data/baseball_stats_full.db")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")


def _compile_snapshot(conn, season, latest_date):
    """Build a text snapshot of current data for Sonnet."""
    sections = []
    sections.append(f"DATE: {date.today().isoformat()}. Current year: {date.today().year}.")
    sections.append(f"MLB season started late March {season}. Latest game date: {latest_date}.")

    team_games = conn.execute("""
        SELECT MAX(games) FROM season_batting_stats WHERE season = ?
    """, (season,)).fetchone()
    if team_games and team_games[0]:
        sections.append(f"Teams have played approximately {team_games[0]} games each.")

    # Already-detected rule-based events (only on/before target date — for backtests)
    existing = conn.execute("""
        SELECT headline, category FROM notable_events
        WHERE detection_type != 'ai_insight' AND game_date <= ?
        ORDER BY game_date DESC, priority ASC
    """, (latest_date,)).fetchall()
    if existing:
        sections.append("\n=== ALREADY-DETECTED EVENTS (do NOT duplicate) ===")
        for headline, category in existing:
            sections.append(f"- [{category}] {headline}")

    # Yesterday's batting standouts
    sections.append(f"\n=== YESTERDAY'S BATTING ({latest_date}) ===")
    bat_rows = conn.execute("""
        SELECT p.name, g.hits, g.at_bats, g.home_runs, g.rbi, g.doubles, g.triples,
               g.walks, g.opponent, p.team
        FROM game_batting_logs g
        JOIN players p ON g.player_id = p.player_id
        WHERE g.date = ? AND g.season = ?
        AND (g.home_runs >= 1 OR g.hits >= 3 OR g.rbi >= 3)
        ORDER BY g.home_runs DESC, g.rbi DESC, g.hits DESC
        LIMIT 20
    """, (latest_date, season)).fetchall()
    for name, h, ab, hr, rbi, d, t, bb, opp, team in bat_rows:
        line = f"- {name} ({team}): {h}-for-{ab}"
        parts = []
        if hr: parts.append(f"{hr} HR")
        if rbi: parts.append(f"{rbi} RBI")
        if d: parts.append(f"{d} 2B")
        if t: parts.append(f"{t} 3B")
        if bb: parts.append(f"{bb} BB")
        if parts: line += ", " + ", ".join(parts)
        line += f" vs {opp}"
        sections.append(line)

    # Season totals for yesterday's players
    sections.append(f"\n=== SEASON TOTALS FOR YESTERDAY'S PLAYERS ===")
    for name, h, ab, hr, rbi, d, t, bb, opp, team in bat_rows:
        season_row = conn.execute("""
            SELECT s.home_runs, s.rbi, s.hits, s.games, s.batting_avg, s.stolen_bases
            FROM season_batting_stats s JOIN players p ON s.player_id = p.player_id
            WHERE p.name = ? AND s.season = ?
        """, (name, season)).fetchone()
        if season_row:
            shr, srbi, sh, sg, savg, ssb = season_row
            sections.append(f"- {name}: {sg} G, {shr} HR, {srbi} RBI, {sh} H, {ssb} SB")

    # Yesterday's pitching standouts
    sections.append(f"\n=== YESTERDAY'S PITCHING ({latest_date}) ===")
    pitch_rows = conn.execute("""
        SELECT p.name, g.innings_pitched, g.ip_outs, g.hits, g.earned_runs,
               g.strikeouts, g.walks, g.is_start, g.win, g.loss, g.save,
               g.opponent, p.team
        FROM game_pitching_logs g
        JOIN players p ON g.player_id = p.player_id
        WHERE g.date = ? AND g.season = ? AND g.is_start = 1
        ORDER BY g.ip_outs DESC, g.strikeouts DESC
        LIMIT 15
    """, (latest_date, season)).fetchall()
    for name, ip, ip_outs, h, er, so, bb, is_start, w, l, sv, opp, team in pitch_rows:
        ip_display = ip or f"{(ip_outs or 0) // 3}.{(ip_outs or 0) % 3}"
        result = ""
        if w: result = " (W)"
        elif l: result = " (L)"
        sections.append(f"- {name} ({team}): {ip_display} IP, {h} H, {er} ER, {so} K, {bb} BB{result} vs {opp}")

    # Season leaders
    sections.append("\n=== SEASON LEADERS ===")
    for label, col, table in [
        ("HR", "home_runs", "season_batting_stats"),
        ("RBI", "rbi", "season_batting_stats"),
        ("SB", "stolen_bases", "season_batting_stats"),
    ]:
        rows = conn.execute(f"""
            SELECT p.name, s.{col}, p.team
            FROM {table} s JOIN players p ON s.player_id = p.player_id
            WHERE s.season = ? AND s.plate_appearances >= 15
            ORDER BY s.{col} DESC LIMIT 5
        """, (season,)).fetchall()
        vals = [f"{name} ({team}) {val}" for name, val, team in rows]
        sections.append(f"{label}: {', '.join(vals)}")

    for label, col, order in [("K", "strikeouts", "DESC"), ("W", "wins", "DESC")]:
        rows = conn.execute(f"""
            SELECT p.name, s.{col}, p.team
            FROM season_pitching_stats s JOIN players p ON s.player_id = p.player_id
            WHERE s.season = ? AND s.games_started >= 1
            ORDER BY s.{col} {order} LIMIT 5
        """, (season,)).fetchall()
        vals = [f"{name} ({team}) {val}" for name, val, team in rows]
        sections.append(f"{label}: {', '.join(vals)}")

    # Historical context from DB
    sections.append("\n=== DB-VERIFIED HISTORICAL CONTEXT ===")
    sections.append("(Verified from our game log database 1920-2026. Use confidently.)")

    hr_leader = conn.execute("""
        SELECT p.name, s.home_runs, s.games, p.team
        FROM season_batting_stats s JOIN players p ON s.player_id = p.player_id
        WHERE s.season = ? ORDER BY s.home_runs DESC LIMIT 1
    """, (season,)).fetchone()
    if hr_leader:
        name, hr, games, team = hr_leader
        pace = int(hr * 162.0 / games) if games > 0 else 0
        sections.append(f"- {name}: {hr} HR in {games} games, on pace for {pace}.")

    cgso = conn.execute("""
        SELECT p.name, g.date, g.strikeouts, g.hits
        FROM game_pitching_logs g JOIN players p ON g.player_id = p.player_id
        WHERE g.season = ? AND g.ip_outs >= 27 AND g.earned_runs = 0
        ORDER BY g.date ASC LIMIT 1
    """, (season,)).fetchone()
    if cgso:
        prev_cgso = conn.execute("""
            SELECT g.date FROM game_pitching_logs g
            WHERE g.season = ? AND g.ip_outs >= 27 AND g.earned_runs = 0
            ORDER BY g.date ASC LIMIT 1
        """, (season - 1,)).fetchone()
        ctx = f"Last year's first CGSO came on {prev_cgso[0]}." if prev_cgso else ""
        sections.append(f"- First CGSO of {season}: {cgso[0]} on {cgso[1]} ({cgso[3]} H, {cgso[2]} K). {ctx}")

    # Player context from DB
    sections.append("\n=== PLAYER CONTEXT (use ONLY these facts) ===")
    key_names = set()
    for row in bat_rows[:10]:
        key_names.add(row[0])
    for row in pitch_rows[:10]:
        key_names.add(row[0])

    for name in sorted(key_names):
        career_bat = conn.execute("""
            SELECT COUNT(DISTINCT s.season), MIN(s.season), MAX(s.season), p.team
            FROM season_batting_stats s JOIN players p ON s.player_id = p.player_id
            WHERE p.name = ?
        """, (name,)).fetchone()
        career_pitch = conn.execute("""
            SELECT COUNT(DISTINCT s.season), MIN(s.season), MAX(s.season), p.team
            FROM season_pitching_stats s JOIN players p ON s.player_id = p.player_id
            WHERE p.name = ?
        """, (name,)).fetchone()

        seasons = max(career_bat[0] if career_bat else 0, career_pitch[0] if career_pitch else 0)
        first = min(career_bat[1] or 9999, career_pitch[1] or 9999)
        team = (career_bat[3] if career_bat and career_bat[3] else
                career_pitch[3] if career_pitch and career_pitch[3] else "")

        if seasons == 0: continue
        elif seasons == 1:
            sections.append(f"- {name} ({team}): Rookie, first MLB season ({first}).")
        elif seasons == 2:
            sections.append(f"- {name} ({team}): 2nd MLB season, debuted {first}.")
        elif seasons <= 5:
            sections.append(f"- {name} ({team}): Young player, {seasons} seasons since {first}.")
        else:
            sections.append(f"- {name} ({team}): Veteran, {seasons} seasons since {first}.")

    return "\n".join(sections)


def _compile_candidates(conn, season, latest_date):
    """Slim snapshot for tool-use mode: today's standout games + player_ids only.

    Sonnet must call tools to verify any other fact (career totals, history, etc).
    """
    sections = []
    sections.append(f"DATE: {date.today().isoformat()}. Current year: {date.today().year}.")
    sections.append(f"MLB season started late March {season}. Latest game date: {latest_date}.")

    team_games = conn.execute(
        "SELECT MAX(games) FROM season_batting_stats WHERE season = ?", (season,)
    ).fetchone()
    if team_games and team_games[0]:
        sections.append(f"Teams have played approximately {team_games[0]} games each.")

    existing = conn.execute("""
        SELECT headline, category FROM notable_events
        WHERE detection_type != 'ai_insight' AND game_date <= ?
        ORDER BY game_date DESC, priority ASC
    """, (latest_date,)).fetchall()
    if existing:
        sections.append("\n=== ALREADY-DETECTED EVENTS (do NOT duplicate) ===")
        for headline, category in existing:
            sections.append(f"- [{category}] {headline}")

    sections.append(f"\n=== CANDIDATE BATTING GAMES ({latest_date}) ===")
    sections.append("Format: player_id | name (team): box-score line vs opponent")
    bat_rows = conn.execute("""
        SELECT g.player_id, p.name, p.team, g.hits, g.at_bats, g.home_runs,
               g.rbi, g.doubles, g.triples, g.walks, g.opponent
        FROM game_batting_logs g JOIN players p ON g.player_id = p.player_id
        WHERE g.date = ? AND g.season = ?
        AND (g.home_runs >= 1 OR g.hits >= 3 OR g.rbi >= 3)
        ORDER BY g.home_runs DESC, g.rbi DESC, g.hits DESC
        LIMIT 25
    """, (latest_date, season)).fetchall()
    for pid, name, team, h, ab, hr, rbi, d, t, bb, opp in bat_rows:
        parts = []
        if hr: parts.append(f"{hr} HR")
        if rbi: parts.append(f"{rbi} RBI")
        if d: parts.append(f"{d} 2B")
        if t: parts.append(f"{t} 3B")
        if bb: parts.append(f"{bb} BB")
        extra = (", " + ", ".join(parts)) if parts else ""
        sections.append(f"- {pid} | {name} ({team}): {h}-for-{ab}{extra} vs {opp}")

    sections.append(f"\n=== CANDIDATE PITCHING STARTS ({latest_date}) ===")
    sections.append("Format: player_id | name (team): IP/H/ER/K/BB result vs opponent")
    pitch_rows = conn.execute("""
        SELECT g.player_id, p.name, p.team, g.innings_pitched, g.ip_outs,
               g.hits, g.earned_runs, g.strikeouts, g.walks, g.win, g.loss, g.opponent
        FROM game_pitching_logs g JOIN players p ON g.player_id = p.player_id
        WHERE g.date = ? AND g.season = ? AND g.is_start = 1
        ORDER BY g.ip_outs DESC, g.strikeouts DESC
        LIMIT 15
    """, (latest_date, season)).fetchall()
    for pid, name, team, ip, ip_outs, h, er, so, bb, w, l, opp in pitch_rows:
        ip_display = ip or f"{(ip_outs or 0) // 3}.{(ip_outs or 0) % 3}"
        result = " (W)" if w else (" (L)" if l else "")
        sections.append(f"- {pid} | {name} ({team}): {ip_display} IP, {h} H, {er} ER, {so} K, {bb} BB{result} vs {opp}")

    return "\n".join(sections)


_SYSTEM_PROMPT = """You are a baseball analyst writing for a notable events feed in a stats app.

Write notable events from the latest games (target date specified in the user message). Think like the best stat-nerd baseball Twitter account — insightful, punchy, data-driven.

TOOLS — USE THEM. The candidates list shows player_ids. For EVERY factual claim you intend to make beyond what's literally in the box-score line, you MUST call a tool to verify it FIRST. Examples:
- Want to write "his first MLB homer"? Call get_player_career_summary first. If career_batting.home_runs > 0, you cannot write that — he has prior HRs.
- Want to write "matches his career high in RBI"? Call get_career_high with stat='rbi', scope='game' first. Only assert if today's RBI equals career_high.
- Want to write "0.00 ERA through 3 starts" or "X consecutive scoreless starts"? Call get_season_aggregates and get_active_streak with condition='scoreless_start' first. Only assert what the tools confirm.
- Want to write "first since 2019" or "first Yankee to..."? Call get_first_since first. Only assert what the tool returns.

If a tool result contradicts what you wanted to write, REVISE or DROP the claim. Do not write claims you have not verified through tools.

ANCHOR–BOX-SCORE MATCHING: When you reference a STAT THRESHOLD in your anchor (e.g., "his Nth career 4-hit game", "his 10th 2-HR game", "his 5th 5-RBI game"), today's box-score line MUST literally show that stat at or above the threshold. The career counter from get_career_high is NOT today's game — it's a count of past games at that level.
  - Box score "3-for-5": you may NOT write "his Nth 4-hit game" (today was 3 hits).
  - Box score "1 HR": you may NOT write "his 5th 2-HR game" (today was 1 HR).
  - Box score "4 RBI": you may NOT write "his Nth 5-RBI game" (today was 4 RBI).

CRITICAL RULES:
1. Every event MUST be about what a player did ON the target date specifically. Lead with their game performance, then connect to broader narrative. Do NOT use "last night" or "yesterday".
2. Do NOT claim a player extended or set a streak unless their target-date performance continued it. If a pitcher gave up earned runs, they did NOT extend a scoreless streak. If a batter went hitless, they did NOT extend a hitting streak. Streaks are the rule-based detector's job.
3. Do NOT write about career milestones (approaching/reaching round numbers like 1500 K, 500 HR, 3000 hits) or leaderboard positions. The rule-based system covers both.
4. Do NOT invent historical comparisons. Only cite facts confirmed by tool calls.
5. Do NOT duplicate events listed under ALREADY-DETECTED. If an already-detected event covers a player, you may write about that player ONLY if your angle is substantially different.
6. Write each as a single flowing sentence, conversational and punchy. Always include units — "6 innings" not just "6", "3 starts" not just "3".
7. Output ONLY a JSON array: [{"headline": "...", "player_names": ["..."], "team_names": ["..."], "opponent": "OPP"}]. The "opponent" field must be the team abbreviation the primary player played against (from the box score). If nothing meets the bar, return an empty array []. NEVER return prose, explanation, or commentary — only the JSON array.

WHAT MATTERS MOST — every event must carry at least ONE of these:

1. AN ANCHORED COMPARISON — a specific previously-held statistical marker that today matched or broke. A real anchor is:
   - A specific NUMBER (career high of 11 K; 5 career 6-RBI games)
   - Tied to a specific TIME (set Aug 7, 2024; across his 12-year career)
   - Same STAT TYPE as today's performance (HR matched to HR, AVG to AVG)
   Vague phrases are NOT anchors: "sneaky", "torrid", "in his Nth season", "continuing his dominance".

2. A GENUINELY OUTLIER SINGLE-GAME PERFORMANCE — must clear one of:
   - No-hit bid into the 8th inning or later
   - No-hitter or perfect game
   - 10+ strikeouts in a start
   - 4+ hits in a game
   - 2+ HR in a game
   - 5+ RBI in a game

   THESE FLOORS ALSO APPLY TO ANCHORS. You may NOT anchor on a below-floor stat threshold even via criterion 1:
   - Never anchor on "Nth career 4-RBI game" or "matches his career high in RBI" when today is 4 RBI (5+ RBI floor)
   - Never anchor on "Nth career 3-hit game" or "matches his career high in hits" when today is 3 hits (4+ hits floor)
   - Never anchor on "Nth career 1-HR game" (2+ HR floor)
   The structural detector handles below-floor career milestones (100 RBI seasons, career first HR). Headlines anchoring below floor are programmatically filtered out.

   3 hits, 1 HR, 4 RBI, 8 K — these do NOT qualify on their own AND do not qualify as anchors. Skip entirely.

3. A BREAKOUT TRAJECTORY — a 3rd+ season player on pace to substantially EXCEED (not match) their career high in a SEASON counting stat. Example: "Peraza already has 5 homers in 13 games — his career high is 8, set across a full 2024 season."
   FLOORS — prior career best must clear: HR ≥ 5, SB ≥ 10, RBI ≥ 30, hits ≥ 50. Current pace must DWARF prior best (~1.5x on 162-game projection). Season totals only — single-game belongs to criterion 2.

The best events have BOTH a comparison and an outlier (e.g., 11 K AND career-high match). Any one of the three alone can work. None means skip.

COUNT ANCHORS — RARITY VS DOMINANCE FRAMING. Before writing any "Nth career X" anchor, you MUST call get_career_threshold_count to get the count + total games + rate. Never invent counts. Then choose framing:

(a) RATE < 5% — RARE event, use Nth framing. Example: Buxton's 16 four-hit games / 1500 career games = ~1%. Write: "his 16th game with 4+ hits in his 12-year career."

(b) RATE >= 25% — DOMINANCE, re-frame as a percentage. Example: "Skubal has reached 10+ K in 42% of his career starts." This frames frequent occurrence as elite-level dominance.

(c) RATE 5-25% — judgment call. If the threshold itself is elite (10+ K, 4+ hits), Nth framing works. If routine (3 hits, 2 RBI), skip — neither rare nor dominant.

NEVER write counts you haven't verified via tool. NEVER round up or guess.

WHAT GREAT LOOKS LIKE — study these:

- "Corbin Carroll went 4-for-5 with 2 home runs and 6 RBI — matches his career high in RBI (last reached May 24, 2023) and his first multi-homer game since June 2024." (Outlier clearing 4+ hit, 2+ HR, 5+ RBI floors + dated anchor.)
- "Will Warren went 7.0 IP, 11 K, 0 BB and 2 ER — the 11 strikeouts match his career high set last August, his first double-digit strikeout game since." (Outlier + anchor.)
- "Byron Buxton went 4-for-5 with 2 homers and 2 RBI — the veteran's first 4-hit game of 2026 and his 16th game with 4+ hits in his 12-year career." (Outlier + count-over-career anchor.)

WHAT WEAK INSIGHTS LOOK LIKE — avoid:

- Career stage without a tied feat. "in his fifth season" isn't an anchor; "first time he's gotten off to a hot start in 5 seasons" IS, but only if backed by numbers.
- Small-sample MATCHES of low totals. "Matching 2025 HR total (3)" when prior is too low to mean anything. (EXCEPTION: criterion 3 breakout — meaningfully EXCEEDING a low career best.)
- Routine count anchors. "His 35th career 4-RBI game" is anti-signal if he does it most seasons.
- Single-game "career firsts" below structural bars. "First 3-hit game", "first 4-RBI game", "first 2-HR game" — Personal Best detector fires at 4+ hits, 5+ RBI, 3+ HR for a reason. Skip.
- Qualitative language without a backing number. "Sneaky power start" / "on a torrid pace" / "his sharpest start" / "best outing of the year" — comparative adjectives forbidden unless the stat AND the prior bar both appear in the same sentence.
- Restating the same fact twice in one insight.
- Stat-type mismatches (last year's AVG vs this year's HR count).
- Unexplained other-player references. "Matching Riley" is noise unless you say who Riley is and why the comparison matters.

QUALITY THRESHOLD:
- Find things RULES CAN'T FIND. Standard good performances are covered automatically.
- A single slightly-off game after a hot start is NOT notable — normal regression.
- For PITCHERS: bar is 8+ IP, or 10+ K, or unusual narrative. NOT a 6-IP/3-ER quality start.
- For BATTERS: 2-for-4 isn't notable without broader context.
- If you can't articulate WHY beyond "he played well", skip.
- NEVER write about a player having a bad game / struggling / slumping. The feed only celebrates positive performances. Exception: bad stat paired with unusual positive ("struck out 12 but hit 2 homers").

STYLE RULES:
- Do NOT pad sentences with empty context. A clean stat line speaks for itself.
- Career year/season count is interesting only at extremes (debut, 2nd year, 15+ year veteran).
- 162-game pace projections: note as fact, don't editorialize about sustainability.
- NEVER use dangling references — every stat must be introduced before being referenced ("the 5 steals" requires earlier mention).
- Be explicit when comparing to prior periods ("on a similar pace to" or "already has X, took until August last year").
- Over short spans (<20 games), use rate stats (".417 over 12 games") not cumulative ("25 hits in 12 games").
- Never name another player without explaining who they are and why the comparison matters."""


def generate_ai_insights(conn, season, latest_date, dry_run=False, preview=False):
    """Generate AI-powered notable events using Sonnet with DB tool access.

    Sonnet receives a slim candidate-games snapshot and must call tools
    (services/insight_tools.py) to verify any factual claim before writing it.

    preview=True calls Sonnet but skips dedup and DB insert.
    """
    if not preview:
        existing = conn.execute("""
            SELECT COUNT(*) FROM notable_events
            WHERE detection_type = 'ai_insight' AND game_date = ?
        """, (latest_date,)).fetchone()[0]
        if existing > 0 and not dry_run:
            print(f"  AI insights already exist for {latest_date} ({existing} events), skipping")
            return {"events": [], "skipped": True}

    snapshot = _compile_candidates(conn, season, latest_date)

    user_message = (
        f"Target game date: {latest_date}. Today is {date.today().isoformat()}.\n\n"
        f"DATA SNAPSHOT:\n{snapshot}"
    )

    if dry_run:
        return {"snapshot": snapshot, "prompt_length": len(_SYSTEM_PROMPT) + len(user_message), "events": []}

    try:
        import anthropic
        from services.insight_tools import TOOLS, execute_tool
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

        # Cache the system prompt + tools (stable across calls; saves ~70-80%
        # input cost on the message-replay loop). Cache hits within 5 min.
        system_blocks = [{
            "type": "text",
            "text": _SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }]
        tools_cached = [dict(t) for t in TOOLS]
        tools_cached[-1] = {**tools_cached[-1], "cache_control": {"type": "ephemeral"}}

        messages = [{"role": "user", "content": user_message}]
        max_iterations = 15
        tool_call_count = 0
        final_text = None

        for _ in range(max_iterations):
            response = client.messages.create(
                model="claude-sonnet-4-5-20250929",
                max_tokens=4000,
                system=system_blocks,
                tools=tools_cached,
                messages=messages,
            )

            if response.stop_reason == "end_turn":
                final_text = next(
                    (b.text for b in response.content if getattr(b, "type", None) == "text"),
                    None,
                )
                break

            if response.stop_reason == "tool_use":
                tool_results = []
                for block in response.content:
                    if getattr(block, "type", None) == "tool_use":
                        tool_call_count += 1
                        result = execute_tool(conn, block.name, dict(block.input))
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps(result),
                        })
                messages.append({"role": "assistant", "content": response.content})
                messages.append({"role": "user", "content": tool_results})
                continue

            # Unexpected stop reason — bail
            return {"snapshot": snapshot, "events": [],
                    "error": f"Unexpected stop_reason: {response.stop_reason}"}

        if final_text is None:
            return {"snapshot": snapshot, "events": [],
                    "error": f"Hit {max_iterations} iterations without end_turn"}

        text = (final_text or "").strip()
        if "```" in text:
            text = text.split("```json")[-1].split("```")[0].strip()
        # Try to extract a JSON array even if Sonnet wrapped it in prose
        if not text.startswith("["):
            m = re.search(r"\[.*\]", text, re.DOTALL)
            text = m.group(0) if m else "[]"
        try:
            events = json.loads(text)
        except json.JSONDecodeError:
            print(f"  AI insights: JSON parse failed, treating as empty. Raw response: {(final_text or '')[:200]!r}")
            events = []
        before_filter = len(events)
        events = _strip_below_floor_anchors(events)
        filtered = before_filter - len(events)
        print(f"  AI insights: {len(events)} events ({filtered} filtered for below-floor anchors), {tool_call_count} tool calls")
    except Exception as e:
        return {"snapshot": snapshot, "events": [], "error": str(e)}

    if preview:
        return {"events": events, "preview": True,
                "tool_calls": tool_call_count, "filtered_count": filtered}

    # Insert into notable_events table with game context
    cursor = conn.cursor()
    inserted = 0
    for e in events:
        # Look up game context using first player + opponent
        game_context = None
        player_names = e.get("player_names", [])
        opponent = e.get("opponent", "")
        if player_names:
            pid_row = conn.execute(
                "SELECT player_id FROM players WHERE name = ?", (player_names[0],)
            ).fetchone()
            if pid_row:
                from services.notable_events import _get_game_context
                game_context = _get_game_context(conn, pid_row[0], latest_date, season)

        if not game_context:
            try:
                from datetime import datetime as dt_cls
                d = dt_cls.strptime(latest_date, "%Y-%m-%d")
                game_context = d.strftime("%B %-d")
            except:
                game_context = latest_date

        try:
            cursor.execute("""
                INSERT OR IGNORE INTO notable_events
                (headline, detail, category, game_date, player_names, team_names,
                 detection_type, priority, game_context)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                e["headline"], "", "Insight", latest_date,
                json.dumps(e.get("player_names", [])),
                json.dumps(e.get("team_names", [])),
                "ai_insight", 2, game_context,
            ))
            if cursor.rowcount > 0:
                inserted += 1
        except sqlite3.IntegrityError:
            pass

    conn.commit()

    # Archive AI insights permanently
    try:
        from services.metering import archive_events
        archive_events([
            {"headline": e["headline"], "detection_type": "ai_insight", "game_date": latest_date}
            for e in events
        ])
    except Exception:
        pass

    return {"events": events, "inserted": inserted}
