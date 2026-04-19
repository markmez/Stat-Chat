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


# Below-floor stat-threshold anchor patterns. Sonnet has repeatedly tried to
# anchor on "Nth career 4-RBI game", "matches his career high in RBI" etc.
# even when the threshold falls below the structural detector's floor. We
# filter them out post-generation as a deterministic backstop.
#
# Floors mirror the structural detector's career-high checks:
#   hits: 4+, RBI: 5+, HR: 2+ (multi-HR), K: 10+
_FLOORS = {"hits": 4, "rbi": 5, "hr": 2, "k": 10}

# Each pattern captures the threshold value as group 1.
_FLOOR_PATTERNS = [
    (re.compile(r'(\d+)\+?\s*-?\s*hit(?:s)?(?:\s+(?:game|performance|day|night))', re.I), "hits"),
    (re.compile(r'(\d+)\+?\s*-?\s*RBI(?:\s+(?:game|performance|day|night))?', re.I), "rbi"),
    (re.compile(r'(\d+)\+?\s*-?\s*(?:HR|homer|home\s+run)s?(?:\s+(?:game|performance))?', re.I), "hr"),
    (re.compile(r'(\d+)\+?\s*-?\s*K(?:\s+(?:start|outing|performance))', re.I), "k"),
    (re.compile(r'(\d+)\+?\s*-?\s*strikeout(?:s)?(?:\s+(?:start|outing|performance))', re.I), "k"),
]

# Phrases that signal an anchor framing (not a casual mention of today's stat).
_ANCHOR_CONTEXT = re.compile(
    r"\b(career|matches|ties|matching|tying|his\s+\d+(?:st|nd|rd|th)|across\s+\d+\s+(?:career\s+)?(?:games|starts))\b",
    re.I,
)


def _strip_below_floor_anchors(events):
    """Drop events whose headline anchors on a stat threshold below floor.

    Examples filtered:
      - "his 4th game with 4+ RBI in 328 career games" (4 < 5)
      - "matches his career high in RBI (4)" (4 < 5)
      - "his 3rd career 4-hit game" (this passes — 4 >= 4)
      - "1-hit shutout effort" (this passes — not in anchor context)

    Only filters when the threshold appears in an anchor context (career,
    matches, ties, his Nth, etc.) to avoid catching casual box-score mentions.
    """
    cleaned = []
    for e in events:
        h = e.get("headline", "")
        if not _ANCHOR_CONTEXT.search(h):
            cleaned.append(e)
            continue
        below = False
        for pat, stat in _FLOOR_PATTERNS:
            for m in pat.finditer(h):
                try:
                    val = int(m.group(1))
                except (ValueError, IndexError):
                    continue
                # Only consider this number an anchor threshold if it sits
                # near anchor language. Keep simple: presence of anchor
                # context anywhere in the headline is enough — Sonnet
                # typically anchors and references the threshold close
                # together.
                if val < _FLOORS[stat]:
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

    prompt = f"""You are a baseball analyst writing for a notable events feed in a stats app.
The current date is {date.today().isoformat()}. The current year is {date.today().year}.

Write notable events from the latest games. Think like the best stat-nerd
baseball Twitter account — insightful, punchy, data-driven.

TOOLS — USE THEM. The candidates list below shows player_ids. For EVERY
factual claim you intend to make beyond what's literally in the box-score
line, you MUST call a tool to verify it FIRST. Examples:
- Want to write "his first MLB homer"? Call get_player_career_summary first.
  If career_batting.home_runs > 0, you cannot write that — he has prior HRs.
- Want to write "matches his career high in RBI"? Call get_career_high
  with stat='rbi', scope='game' first. Only assert if today's RBI equals
  career_high.
- Want to write "0.00 ERA through 3 starts" or "X consecutive scoreless
  starts"? Call get_season_aggregates and get_active_streak with
  condition='scoreless_start' first. Only assert what the tools confirm.
- Want to write "first since 2019" or "first Yankee to..."? Call
  get_first_since first. Only assert what the tool returns.

If a tool result contradicts what you wanted to write, REVISE or DROP the
claim. Do not write claims you have not verified through tools. If you
write a claim without backing tool data, the headline will be rejected.

ANCHOR–BOX-SCORE MATCHING: When you reference a STAT THRESHOLD in your
anchor (e.g., "his Nth career 4-hit game", "his 10th 2-HR game", "his
5th 5-RBI game"), today's box-score line MUST literally show that stat
at or above the threshold. The career counter from get_career_high is
NOT today's game — it's a count of past games at that level. You may
only write "today was his Nth X-stat-game" if today actually IS an
X-stat-game per the box score above. Concrete examples:
  - Box score: "3-for-5". You may NOT write "his Nth 4-hit game"
    (today was only 3 hits). You CAN write "his Nth 3+ hit game" if
    you have a tool-verified count for that.
  - Box score: "1 HR". You may NOT write "his 5th 2-HR game"
    (today was only 1 HR).
  - Box score: "4 RBI". You may NOT write "his Nth 5-RBI game"
    (today was only 4 RBI).
The anchor threshold must additionally clear criterion 2's floors
(4+ hits, 2+ HR, 5+ RBI, 10+ K). "His Nth 3-hit game" — even if today
WAS 3 hits — is not a valid anchor; 3 hits is below the floor.

CRITICAL RULES:
1. Every event MUST be about what a player did ON {latest_date} specifically.
   Lead with their game performance, then connect to broader narrative.
   Do NOT use "last night" or "yesterday" — the date context is shown separately.
2. Do NOT claim a player extended or set a streak unless their {latest_date}
   performance continued it. If a pitcher gave up earned runs, they did NOT
   extend a scoreless streak. If a batter went hitless, they did NOT extend
   a hitting streak. Streaks are the rule-based detector's job — you should
   focus on individual-game narratives the rules can't find.
3. Do NOT write about career milestones (approaching or reaching round
   numbers like 1500 K, 500 HR, 3000 hits, etc.) or leaderboard positions
   (leading, tying for the lead, taking the lead in a stat category).
   Both are detected by the rule-based system. If you see one in
   ALREADY-DETECTED, do not repeat it. If you don't see one, it's not
   your job to add it.
3. Use the DB-VERIFIED HISTORICAL CONTEXT — these are confirmed facts from our
   database. Cite them confidently.
4. Do NOT invent historical comparisons beyond what's provided. If the data
   doesn't include a "first since" fact, don't make one up.
5. Do NOT duplicate events already detected (listed under ALREADY-DETECTED).
   If an already-detected event covers a player, you may write about that player
   ONLY if your angle is substantially different.
6. ONLY use biographical facts from the PLAYER CONTEXT section. Do not assume
   team history, rookie status, or career details not listed there.
7. Write each as a single flowing sentence, conversational and punchy.
   If using baseball slang, use it correctly (e.g. "long ball" means home run,
   not a ball hit far). Always include units — "6 innings" not just "6",
   "3 starts" not just "3" — when the number could be ambiguous.
8. Prioritize: historical context, start-of-season milestones, comeback narratives,
   rookie watch, pace projections, cross-category patterns.
9. Output ONLY a JSON array: [{{"headline": "...", "player_names": ["..."], "team_names": ["..."], "opponent": "OPP"}}]
   The "opponent" field must be the team abbreviation the primary player played against
   (from the box score data). This is required for every event.

WHAT MATTERS MOST — this is the single bar every event must clear:

Every event must carry at least ONE of these two things:

1. AN ANCHORED COMPARISON — a specific, previously-held statistical
   marker that today's performance matched or broke. A real anchor is:
   - A specific NUMBER (career high of 11 K; 5 career 6-RBI games).
   - Tied to a specific TIME (set Aug 7, 2024; across his 12-year
     career; in 13 games this season vs a full 2025).
   - Using the SAME STAT TYPE as today's performance (HR matched to
     HR, AVG matched to AVG — never a counting stat compared to a
     rate stat across years).
   Vague phrases are NOT anchors: "sneaky", "torrid", "in his Nth
   season", "continuing his dominance", "a rare combination", or a
   small-sample pace projection.

2. A GENUINELY OUTLIER SINGLE-GAME PERFORMANCE — must clear one of
   these specific bars to qualify on its own:
   - A no-hit bid carried into the 8th inning or later (surface this
     regardless of how the game ended afterward — if the no-hitter was
     still alive entering the 8th, that's the story).
   - A no-hitter or perfect game.
   - 10+ strikeouts in a start.
   - 4+ hits in a game.
   - 2+ HR in a game.
   - 5+ RBI in a game.

   THESE FLOORS ALSO APPLY TO ANCHORS. You may NOT anchor on a
   below-floor stat threshold even via criterion 1. Specifically:
   - Never anchor on "Nth career 4-RBI game" or "matches his career
     high in RBI" when today is 4 RBI. The 5+ RBI floor applies.
   - Never anchor on "Nth career 3-hit game" or "matches his career
     high in hits" when today is 3 hits. The 4+ hits floor applies.
   - Never anchor on "Nth career 1-HR game" — 2+ HR is the floor.
   The structural detector handles below-floor career milestones
   (100 RBI seasons, career first HR, etc.) — those don't need
   AI-insight coverage. Headlines that anchor below floor are
   programmatically filtered out before insertion.

   3 hits, 1 HR, 4 RBI, 8 K — these do NOT qualify on their own
   AND do not qualify as anchors. If the only thing about a game
   is "4 RBI" or "3 hits", skip it entirely.

3. A BREAKOUT TRAJECTORY — a player in their 3rd+ MLB season is on
   pace to substantially EXCEED (not just match) their career high in
   a SEASON counting stat. Example: "Peraza already has 5 homers in
   13 games — his career high is 8, set across a full 2024 season."

   FLOORS — the prior career best you're exceeding must itself clear
   a meaningful bar; otherwise the "breakout" is noise:
   - HR (season): prior best ≥ 5
   - SB (season): prior best ≥ 10
   - RBI (season): prior best ≥ 30
   - Hits (season): prior best ≥ 50

   And the current pace must DWARF the prior best (roughly 1.5x or
   more on a 162-game projection). "Matching" a low total doesn't
   qualify; "blowing past" it does. This applies to SEASON totals
   only — single-game performances belong to criterion 2.

The best events have BOTH a comparison and an outlier (e.g., 11 K AND
career-high match). Any one of the three alone can work. None of the
three means don't write it — better to skip than to surface an event
whose narrative is only "he played fine today."

COUNT ANCHORS — RARITY VS DOMINANCE FRAMING. Before writing any
"Nth career X" anchor, you MUST call get_career_threshold_count to
get the count + total games + rate. Never invent counts. Then
choose framing based on the rate:

(a) RATE < 5% — RARE event, use Nth framing.
    Example: Buxton's 16 four-hit games / 1500 career games = ~1%.
    Write: "his 16th game with 4+ hits in his 12-year career."

(b) RATE >= 25% — DOMINANCE, re-frame as a percentage.
    Example: Skubal hits 10+ K in 18 of 138 starts (13% — actually
    in the judgment-call zone), but if it were 42%, write:
    "Skubal has reached 10+ K in 42% of his career starts." This
    frames frequent occurrence as elite-level dominance, not
    routineness. The percentage frame turns a "common-for-him"
    stat into a moat-of-skill insight.

(c) RATE 5-25% — judgment call. If the threshold itself is elite
    (10+ K in a start, 4+ hits in a game), Nth framing works. If
    the threshold is routine (3 hits, 2 RBI), skip — neither rare
    nor dominant.

NEVER write "his 58th career double-digit K start" without a tool
result confirming the 58. NEVER invent counts. If
get_career_threshold_count gives you 18 in 138 starts (13%), do
NOT round up or guess — write the actual number, or skip the
anchor entirely.

WHAT GREAT LOOKS LIKE — study these, don't paraphrase them:

- "Corbin Carroll went 4-for-5 with 2 home runs and 6 RBI — matches
  his career high in RBI (last reached May 24, 2023) and his first
  multi-homer game since June 2024." (Outlier event clearing 4+ hit,
  2+ HR, 5+ RBI floors + dated anchor.)

- "Will Warren went 7.0 IP, 11 K, 0 BB and 2 ER — the 11 strikeouts
  match his career high set last August, his first double-digit
  strikeout game since." (Outlier event + anchor.)

- "Byron Buxton went 4-for-5 with 2 homers and 2 RBI — the veteran's
  first 4-hit game of 2026 and his 16th game with 4+ hits in his
  12-year career." (Outlier + concrete count-over-career anchor.)

The shared shape in every one: today's specific event → a concrete
personal-history anchor (career high, first since dated event, or
count-over-defined-career-span).

WHAT WEAK INSIGHTS LOOK LIKE — avoid these shapes:

- Career stage without a tied feat. "in his fifth season" isn't an
  anchor; "first time he's gotten off to a hot start in 5 seasons"
  IS an anchor, but only if you can back it with numbers.
- Small-sample MATCHES of low totals. "Matching 2025 HR total (3)"
  when the prior total is so low the match isn't meaningful. (The
  EXCEPTION is the breakout trajectory case in criterion 3 above —
  meaningfully EXCEEDING a low career best is signal, not noise.)
- Routine count anchors. "His 35th career 4-RBI game" is not an
  anchor if the player has done it most seasons of his career — that
  count is evidence of routineness, not rarity.
- Single-game "career firsts" below the structural bar. "First
  3-hit game", "first 4-RBI game", "first 2-HR game" — these are
  below the rule-based detector's threshold for a reason. The
  rule-based system fires Personal Best events at 4+ hits, 5+ RBI,
  3+ HR — anything lower is not a notable career first. Skip.
- Qualitative language without a backing number. "Sneaky power
  start" / "on a torrid pace" / "continuing his dominance" /
  "his sharpest start" / "best outing of the year" / "cleanest
  performance" with no specific figure in the same clause.
  Comparative adjectives ("sharpest", "finest", "cleanest", "most
  dominant", "best since") are forbidden unless the specific stat
  proving the claim AND the prior bar being compared against both
  appear in the same sentence.
- Restating the same fact twice in one insight. If the lead is "his
  first MLB homer," don't also say "entered the day 0-for-career
  in long balls."
- Stat-type mismatches. Don't compare last year's AVG (.205) to this
  year's HR count (3) — the comparison doesn't mean anything.
- Unexplained other-player references. "Matching Riley" is noise
  unless you also say who Riley is and why the comparison matters.

QUALITY THRESHOLD — READ CAREFULLY:
10. Your job is to find things that RULES CAN'T FIND. Standard good
    performances are already covered by our automated system. Only write
    about something if it has a genuine narrative angle:
    - Cross-category connections (same player leading in multiple stats)
    - Career context that makes an otherwise ordinary line interesting
      (hyped rookie's debut stretch, veteran's resurgence)
    - A genuine quirk or pattern you notice in the data
    - A truly extreme single-game performance (0-for-6 with 5 K for an
      MVP candidate, a pitcher shelled for 10 runs)
11. A single slightly-off game after a hot start is NOT notable — it's
    normal regression. 1-for-5 is never notable, even for a .350 hitter.
    A single good game for a normally bad hitter is also not notable.
    Only flag single-game deviations that are genuinely extreme or
    historically unusual. Trend changes (hot streak ending, cold streak
    starting) are the rule-based detector's job, not yours.
12. For PITCHERS: do NOT write about a standard quality start (6 IP, 3 ER).
    The bar is 8+ IP, or 10+ K, or a genuinely unusual narrative. "Picked
    up his first win" is not notable — everyone gets one eventually.
13. For BATTERS: a 2-for-4 night is not notable unless the player is on a
    tear or the stats have broader context. 4+ K in a game for a good
    hitter could be notable.
14. If you can't articulate WHY something is interesting beyond "he played
    well" or "he had an off night," don't include it. Quality over
    quantity — only include items that pass the bar above. Could be
    2 items or 10, depending on the day.
15. NEVER write about a player having a bad game, struggling, slumping,
    having their worst start, giving up lots of runs/hits, or any
    performance where the main story is that they performed poorly.
    If a player got lit up, gave up 7 runs, struck out 4 times, etc. —
    skip them entirely. The feed only celebrates positive performances.
    The ONLY exception: if a bad stat is paired with an unusual positive
    (e.g. "struck out 12 but also hit 2 homers" — the positive is the story).

STYLE RULES:
14. Do NOT pad sentences with empty context. If you don't have a meaningful
    fact, end the sentence. A stat line speaks for itself.
15. Career year/season count is ONLY interesting at extremes: debut, second
    year, or 15+ year veteran.
16. 162-game pace projections are inherently absurd early in the season.
    Note the pace as a fun fact but do NOT editorialize about sustainability.
17. Less is more. A clean stat line with one piece of context beats a
    sentence stuffed with qualifiers.
18. NEVER use dangling references — don't say "the 5 steals" or "the
    3 homers" unless those stats were already mentioned earlier in the
    same sentence. Every stat must be introduced before being referenced.
19. When comparing current stats to a prior period, be explicit about
    what you're comparing. "Matches his second-half pace" is ambiguous —
    does it mean equal totals or similar rate? Say "on a similar pace to"
    or "already has X, which took him until August last year."
20. Over short spans (under ~20 games), convey hot performance with
    batting average, OPS, or another rate stat (".417 over 12 games"),
    not cumulative hit counts ("25 hits through 12 games"). Counting
    stats over small samples sound impressive without being meaningful.
21. Never reference another player by name unless you also explain who
    they are and why the comparison matters in the same sentence. "Tied
    with Riley" is noise; "tied with Atlanta's Austin Riley for the NL
    HR lead" is signal.

DATA SNAPSHOT:
{snapshot}"""

    if dry_run:
        return {"snapshot": snapshot, "prompt_length": len(prompt), "events": []}

    try:
        import anthropic
        from services.insight_tools import TOOLS, execute_tool
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

        messages = [{"role": "user", "content": prompt}]
        max_iterations = 30
        tool_call_count = 0
        final_text = None

        for _ in range(max_iterations):
            response = client.messages.create(
                model="claude-sonnet-4-5-20250929",
                max_tokens=4000,
                tools=TOOLS,
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

        text = final_text
        if "```" in text:
            text = text.split("```json")[-1].split("```")[0].strip()
        events = json.loads(text)
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
