"""
AI-powered notable events detection using Sonnet.

Runs once per day after the rule-based detection. Compiles a data snapshot
from the DB (yesterday's games, leaders, historical context) and asks Sonnet
to find 5-8 interesting storylines the rules missed.

Cost: ~$0.02-0.04/day (~$1/month). Runs once for ALL users.
"""

import json
import os
import sqlite3
from datetime import date

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

    # Already-detected rule-based events
    existing = conn.execute("""
        SELECT headline, category FROM notable_events
        WHERE detection_type != 'ai_insight'
        ORDER BY game_date DESC, priority ASC
    """).fetchall()
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


def generate_ai_insights(conn, season, latest_date, dry_run=False):
    """Generate AI-powered notable events using Sonnet."""
    # Skip if we already have AI insights for this game date
    existing = conn.execute("""
        SELECT COUNT(*) FROM notable_events
        WHERE detection_type = 'ai_insight' AND game_date = ?
    """, (latest_date,)).fetchone()[0]
    if existing > 0 and not dry_run:
        print(f"  AI insights already exist for {latest_date} ({existing} events), skipping")
        return {"events": [], "skipped": True}

    snapshot = _compile_snapshot(conn, season, latest_date)

    prompt = f"""You are a baseball analyst writing for a notable events feed in a stats app.
The current date is {date.today().isoformat()}. The current year is {date.today().year}.

Write notable events from the latest games. Think like the best stat-nerd
baseball Twitter account — insightful, punchy, data-driven.

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

DATA SNAPSHOT:
{snapshot}"""

    if dry_run:
        return {"snapshot": snapshot, "prompt_length": len(prompt), "events": []}

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model="claude-sonnet-4-5-20250929",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}]
        )
        text = response.content[0].text
        if "```" in text:
            text = text.split("```json")[-1].split("```")[0].strip()
        events = json.loads(text)
    except Exception as e:
        return {"snapshot": snapshot, "events": [], "error": str(e)}

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
