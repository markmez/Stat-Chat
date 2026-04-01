"""
AI-powered notable events detection.

After rule-based detection runs, this module compiles a data snapshot
of yesterday's games and current season state, sends it to Claude Sonnet,
and asks it to identify interesting storylines the rules missed.

Cost: ~$0.02-0.04 per run (once per day for all users).
"""

import json
import logging
import os
import sqlite3
from datetime import date

import anthropic

logger = logging.getLogger("statchat.ai_notable")

DB_PATH = os.getenv("DB_PATH", "/data/baseball_stats_full.db")
MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5-20250929")

# Retrosheet team code → display name
RETRO_TO_DISPLAY = {
    "NYA": "Yankees", "NYN": "Mets", "LAN": "Dodgers", "ANA": "Angels",
    "CHN": "Cubs", "CHA": "White Sox", "SFN": "Giants", "SDN": "Padres",
    "SLN": "Cardinals", "KCA": "Royals", "TBA": "Rays", "WAS": "Nationals",
    "BOS": "Red Sox", "HOU": "Astros", "ATL": "Braves", "PHI": "Phillies",
    "TEX": "Rangers", "TOR": "Blue Jays", "BAL": "Orioles", "MIN": "Twins",
    "CLE": "Guardians", "SEA": "Mariners", "MIL": "Brewers", "CIN": "Reds",
    "PIT": "Pirates", "DET": "Tigers", "ARI": "Diamondbacks", "COL": "Rockies",
    "MIA": "Marlins", "OAK": "Athletics",
}


def _team_name(code):
    return RETRO_TO_DISPLAY.get(code, code) if code else ""


def compile_daily_snapshot(db_path=None, season=None):
    """Build a compact text summary of yesterday's data for Sonnet to analyze.

    Returns (snapshot_text, latest_date) or (None, None) if no data.
    """
    if db_path is None:
        db_path = DB_PATH

    conn = sqlite3.connect(db_path)

    if season is None:
        season = date.today().year

    # Find latest game date
    row = conn.execute(
        "SELECT MAX(date) FROM game_batting_logs WHERE season = ?", (season,)
    ).fetchone()
    latest_date = row[0] if row and row[0] else None
    if not latest_date:
        conn.close()
        return None, None

    sections = []

    # --- Section 1: Yesterday's standout batting performances ---
    batting_lines = conn.execute("""
        SELECT p.name, p.team, g.hits, g.at_bats, g.home_runs, g.rbi,
               g.doubles, g.triples, g.walks, g.stolen_bases, g.strikeouts
        FROM game_batting_logs g
        JOIN players p ON g.player_id = p.player_id
        WHERE g.date = ? AND g.season = ? AND g.at_bats >= 1
        ORDER BY (g.hits * 1.0 / MAX(g.at_bats, 1) + g.home_runs * 0.5 + g.rbi * 0.2) DESC
        LIMIT 30
    """, (latest_date, season)).fetchall()

    if batting_lines:
        lines = [f"## Batting Lines — {latest_date}"]
        for name, team, h, ab, hr, rbi, d, t, bb, sb, so in batting_lines:
            team_str = _team_name(team)
            parts = [f"{name} ({team_str}): {h}-for-{ab}"]
            if hr: parts.append(f"{hr} HR")
            if rbi: parts.append(f"{rbi} RBI")
            if d: parts.append(f"{d} 2B")
            if t: parts.append(f"{t} 3B")
            if bb: parts.append(f"{bb} BB")
            if sb: parts.append(f"{sb} SB")
            if so: parts.append(f"{so} K")
            lines.append(", ".join(parts))
        sections.append("\n".join(lines))

    # --- Section 2: Yesterday's standout pitching performances ---
    pitching_lines = conn.execute("""
        SELECT p.name, p.team, g.innings_pitched, g.ip_outs, g.hits, g.earned_runs,
               g.strikeouts, g.walks, g.home_runs, g.is_start, g.win, g.loss, g.save
        FROM game_pitching_logs g
        JOIN players p ON g.player_id = p.player_id
        WHERE g.date = ? AND g.season = ?
        ORDER BY g.ip_outs DESC, g.strikeouts DESC
        LIMIT 20
    """, (latest_date, season)).fetchall()

    if pitching_lines:
        lines = [f"## Pitching Lines — {latest_date}"]
        for name, team, ip, ip_outs, h, er, so, bb, hr, start, w, l, sv in pitching_lines:
            team_str = _team_name(team)
            ip_display = ip or (f"{(ip_outs or 0) // 3}.{(ip_outs or 0) % 3}")
            decision = ""
            if w: decision = " (W)"
            elif l: decision = " (L)"
            elif sv: decision = " (SV)"
            lines.append(f"{name} ({team_str}){decision}: {ip_display} IP, {h or 0} H, {er or 0} ER, {so or 0} K, {bb or 0} BB, {hr or 0} HR")
        sections.append("\n".join(lines))

    # --- Section 3: Active hitting streaks ---
    # Walk backward through game logs to find active streaks
    streak_data = []
    players_with_games = conn.execute("""
        SELECT DISTINCT g.player_id, p.name, p.team
        FROM game_batting_logs g
        JOIN players p ON g.player_id = p.player_id
        WHERE g.season = ? AND g.date = ? AND g.at_bats > 0
    """, (season, latest_date)).fetchall()

    for pid, name, team in players_with_games:
        games = conn.execute("""
            SELECT hits FROM game_batting_logs
            WHERE player_id = ? AND season = ? AND at_bats > 0
            ORDER BY date DESC
        """, (pid, season)).fetchall()
        streak = 0
        for (hits,) in games:
            if hits > 0:
                streak += 1
            else:
                break
        if streak >= 5:
            streak_data.append((streak, name, _team_name(team)))

    # Also check cross-season streaks (carry over from last season)
    for pid, name, team in players_with_games:
        current_games = conn.execute("""
            SELECT hits FROM game_batting_logs
            WHERE player_id = ? AND season = ? AND at_bats > 0
            ORDER BY date DESC
        """, (pid, season)).fetchall()
        current_streak = 0
        for (hits,) in current_games:
            if hits > 0:
                current_streak += 1
            else:
                break
        # If streak extends through all current-season games, check last season
        if current_streak == len(current_games) and current_streak > 0:
            prev_games = conn.execute("""
                SELECT hits FROM game_batting_logs
                WHERE player_id = ? AND season = ? AND at_bats > 0
                ORDER BY date DESC
            """, (pid, season - 1)).fetchall()
            prev_streak = 0
            for (hits,) in prev_games:
                if hits > 0:
                    prev_streak += 1
                else:
                    break
            if prev_streak > 0:
                total = current_streak + prev_streak
                # Update if cross-season streak is longer
                streak_data = [(s, n, t) if n != name else (max(s, total), n, t)
                               for s, n, t in streak_data]

    if streak_data:
        streak_data.sort(reverse=True)
        lines = ["## Active Hitting Streaks"]
        for streak, name, team in streak_data[:10]:
            lines.append(f"{name} ({team}): {streak} games")
        sections.append("\n".join(lines))

    # --- Section 4: Active on-base streaks ---
    ob_streak_data = []
    for pid, name, team in players_with_games:
        games = conn.execute("""
            SELECT hits, walks, COALESCE(hit_by_pitch, 0)
            FROM game_batting_logs
            WHERE player_id = ? AND season = ?
              AND (at_bats > 0 OR walks > 0 OR COALESCE(hit_by_pitch, 0) > 0)
            ORDER BY date DESC
        """, (pid, season)).fetchall()
        streak = 0
        for h, bb, hbp in games:
            if (h + bb + hbp) > 0:
                streak += 1
            else:
                break
        # Check cross-season
        if streak == len(games) and streak > 0:
            prev_games = conn.execute("""
                SELECT hits, walks, COALESCE(hit_by_pitch, 0)
                FROM game_batting_logs
                WHERE player_id = ? AND season = ?
                  AND (at_bats > 0 OR walks > 0 OR COALESCE(hit_by_pitch, 0) > 0)
                ORDER BY date DESC
            """, (pid, season - 1)).fetchall()
            for h, bb, hbp in prev_games:
                if (h + bb + hbp) > 0:
                    streak += 1
                else:
                    break
        if streak >= 8:
            ob_streak_data.append((streak, name, _team_name(team)))

    if ob_streak_data:
        ob_streak_data.sort(reverse=True)
        lines = ["## Active On-Base Streaks"]
        for streak, name, team in ob_streak_data[:10]:
            lines.append(f"{name} ({team}): {streak} games")
        sections.append("\n".join(lines))

    # --- Section 5: Season leaders (batting) ---
    leader_stats = [
        ("home_runs", "HR"), ("rbi", "RBI"), ("hits", "Hits"),
        ("batting_avg", "AVG"), ("stolen_bases", "SB"),
        ("obp", "OBP"), ("slg", "SLG"), ("ops", "OPS"),
    ]
    leader_lines = ["## Season Batting Leaders"]
    for col, label in leader_stats:
        rows = conn.execute(f"""
            SELECT p.name, s.{col}, s.games, p.team
            FROM season_batting_stats s
            JOIN players p ON s.player_id = p.player_id
            WHERE s.season = ? AND s.plate_appearances >= 15
            ORDER BY s.{col} DESC
            LIMIT 5
        """, (season,)).fetchall()
        if rows:
            entries = []
            for name, val, g, team in rows:
                if val is None:
                    continue
                if isinstance(val, float) and val < 2:
                    entries.append(f"{name} (.{int(val*1000):03d})")
                else:
                    entries.append(f"{name} ({val})")
            leader_lines.append(f"{label}: " + ", ".join(entries))
    if len(leader_lines) > 1:
        sections.append("\n".join(leader_lines))

    # --- Section 6: Season leaders (pitching) ---
    pitch_leader_lines = ["## Season Pitching Leaders"]
    pitch_stats = [
        ("earned_run_avg", "ERA", "ASC"), ("strikeouts", "K", "DESC"),
        ("wins", "W", "DESC"), ("saves", "SV", "DESC"),
        ("whip", "WHIP", "ASC"),
    ]
    for col, label, order in pitch_stats:
        qual = "AND s.innings_pitched_outs >= 10" if col in ("earned_run_avg", "whip") else ""
        rows = conn.execute(f"""
            SELECT p.name, s.{col}, p.team
            FROM season_pitching_stats s
            JOIN players p ON s.player_id = p.player_id
            WHERE s.season = ? {qual}
            ORDER BY s.{col} {order}
            LIMIT 5
        """, (season,)).fetchall()
        if rows:
            entries = []
            for name, val, team in rows:
                if val is None:
                    continue
                if isinstance(val, float):
                    entries.append(f"{name} ({val:.2f})")
                else:
                    entries.append(f"{name} ({val})")
            pitch_leader_lines.append(f"{label}: " + ", ".join(entries))
    if len(pitch_leader_lines) > 1:
        sections.append("\n".join(pitch_leader_lines))

    # --- Section 7: PELT current form (hot/cold) ---
    hot_players = conn.execute("""
        SELECT p.name, cf.ops, cf.batting_avg, cf.num_games, cf.home_runs,
               s.ops as season_ops, p.team
        FROM current_form cf
        JOIN players p ON cf.player_id = p.player_id
        JOIN season_batting_stats s ON cf.player_id = s.player_id AND cf.season = s.season
        WHERE cf.season = ? AND cf.num_games >= 5
        ORDER BY cf.ops DESC
        LIMIT 10
    """, (season,)).fetchall()

    if hot_players:
        lines = ["## Hottest Current Stretches (PELT change-point detection)"]
        for name, ops, avg, ng, hr, s_ops, team in hot_players:
            avg_str = f".{int((avg or 0)*1000):03d}"
            lines.append(f"{name} ({_team_name(team)}): {avg_str}/{ops:.3f} OPS over last {ng} games (season OPS: {s_ops:.3f})")
        sections.append("\n".join(lines))

    cold_players = conn.execute("""
        SELECT p.name, cf.ops, cf.batting_avg, cf.num_games,
               s.ops as season_ops, p.team
        FROM current_form cf
        JOIN players p ON cf.player_id = p.player_id
        JOIN season_batting_stats s ON cf.player_id = s.player_id AND cf.season = s.season
        WHERE cf.season = ? AND cf.num_games >= 5
        ORDER BY cf.ops ASC
        LIMIT 10
    """, (season,)).fetchall()

    if cold_players:
        lines = ["## Coldest Current Stretches"]
        for name, ops, avg, ng, s_ops, team in cold_players:
            avg_str = f".{int((avg or 0)*1000):03d}"
            lines.append(f"{name} ({_team_name(team)}): {avg_str}/{ops:.3f} OPS over last {ng} games (season OPS: {s_ops:.3f})")
        sections.append("\n".join(lines))

    # --- Section 8: Season pace projections ---
    pace_lines = ["## 162-Game Pace Projections (min 10 games)"]
    pace_rows = conn.execute("""
        SELECT p.name, s.home_runs, s.rbi, s.hits, s.stolen_bases, s.games, p.team
        FROM season_batting_stats s
        JOIN players p ON s.player_id = p.player_id
        WHERE s.season = ? AND s.games >= 10
        ORDER BY s.home_runs * 162.0 / s.games DESC
        LIMIT 15
    """, (season,)).fetchall()
    for name, hr, rbi, h, sb, g, team in pace_rows:
        hr_pace = int((hr or 0) * 162 / g) if g else 0
        rbi_pace = int((rbi or 0) * 162 / g) if g else 0
        h_pace = int((h or 0) * 162 / g) if g else 0
        sb_pace = int((sb or 0) * 162 / g) if g else 0
        pace_lines.append(f"{name} ({_team_name(team)}, {g}G): {hr_pace} HR, {rbi_pace} RBI, {h_pace} H, {sb_pace} SB pace")
    if len(pace_lines) > 1:
        sections.append("\n".join(pace_lines))

    # --- Section 9: Already-detected rule-based notable events ---
    try:
        existing = conn.execute("""
            SELECT headline, detail, category, detection_type
            FROM notable_events
            ORDER BY priority ASC, game_date DESC
            LIMIT 20
        """).fetchall()
        if existing:
            lines = ["## Already-Detected Notable Events (rules-based — do NOT duplicate these)"]
            for headline, detail, cat, dtype in existing:
                lines.append(f"[{cat}] {headline} {detail}".strip())
            sections.append("\n".join(lines))
    except sqlite3.OperationalError:
        pass  # Table might not exist yet

    # --- Section 10: Career totals for active players ---
    career_lines = ["## Career Totals (players who played yesterday)"]
    career_rows = conn.execute("""
        SELECT p.name, SUM(s.home_runs) as career_hr, SUM(s.hits) as career_h,
               SUM(s.rbi) as career_rbi, COUNT(DISTINCT s.season) as seasons, p.team
        FROM season_batting_stats s
        JOIN players p ON s.player_id = p.player_id
        WHERE s.player_id IN (
            SELECT DISTINCT player_id FROM game_batting_logs
            WHERE date = ? AND season = ?
        )
        GROUP BY s.player_id
        HAVING career_hr >= 50 OR career_h >= 500
        ORDER BY career_hr DESC
        LIMIT 20
    """, (latest_date, season)).fetchall()
    for name, hr, h, rbi, seasons, team in career_rows:
        career_lines.append(f"{name} ({_team_name(team)}): {hr or 0} HR, {h or 0} H, {rbi or 0} RBI over {seasons} seasons")
    if len(career_lines) > 1:
        sections.append("\n".join(career_lines))

    conn.close()

    snapshot = "\n\n".join(sections)
    return snapshot, latest_date


SYSTEM_PROMPT = """You are a baseball analyst for a stats app called StatChat. Your job is to identify interesting, notable, or surprising storylines from today's baseball data that our rule-based detection system missed.

You will receive a comprehensive data snapshot including: yesterday's box scores, active streaks, season leaders, pace projections, hot/cold stretches, career totals, and the events our rules already caught.

IMPORTANT RULES:
1. Do NOT fabricate any statistics. Every number you cite must come directly from the data provided.
2. Do NOT duplicate events already listed in the "Already-Detected Notable Events" section.
3. Focus on INSIGHTS that connect data points — patterns, cross-references, historical significance, surprising combinations. Things like:
   - The same player leading multiple categories
   - A player's current stretch being dramatically different from their season line
   - A player quietly approaching a career milestone
   - An unusual statistical combination in a single game
   - A trend across multiple games (e.g., "3rd straight multi-HR game")
   - Cross-season streaks
   - League-wide trends (lots of shutouts, HR surge, etc.)
4. Return 3-7 events, prioritized by how interesting they'd be to a general baseball fan.
5. Keep each event concise — headline style, not a paragraph.

Return valid JSON array only, no other text. Each element:
{
  "headline": "The main insight in 1-2 sentences",
  "detail": "Optional supporting context (1 sentence or empty string)",
  "category": "Insight" | "Trend" | "Milestone" | "Streak",
  "player_names": ["Player Name"],
  "team_names": ["Team Display Name"]
}"""


def generate_ai_insights(db_path=None, season=None, dry_run=False):
    """Run the AI insight pass. Returns list of events.

    If dry_run=True, returns (snapshot, events) without inserting into DB.
    """
    snapshot, latest_date = compile_daily_snapshot(db_path, season)
    if not snapshot:
        logger.info("No data available for AI insight generation")
        return [] if not dry_run else (None, [])

    logger.info(f"Compiled snapshot: {len(snapshot)} chars for date {latest_date}")

    client = anthropic.Anthropic()

    response = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": f"Here is today's baseball data snapshot. Find 3-7 interesting storylines that our rule-based system missed.\n\n{snapshot}"
        }],
    )

    # Parse response
    text = response.content[0].text.strip()
    # Strip markdown code fences if present
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

    try:
        events = json.loads(text)
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse AI response: {e}\nResponse: {text[:500]}")
        return [] if not dry_run else (snapshot, [])

    # Validate structure
    validated = []
    for evt in events:
        if not isinstance(evt, dict) or "headline" not in evt:
            continue
        validated.append({
            "headline": evt.get("headline", ""),
            "detail": evt.get("detail", ""),
            "category": evt.get("category", "Insight"),
            "game_date": latest_date,
            "player_names": evt.get("player_names", []),
            "team_names": evt.get("team_names", []),
            "detection_type": "ai_insight",
            "priority": 2,
        })

    if dry_run:
        return snapshot, validated

    # Insert into notable_events
    if validated:
        conn = sqlite3.connect(db_path or DB_PATH)
        cursor = conn.cursor()
        inserted = 0
        for e in validated:
            try:
                cursor.execute("""
                    INSERT OR IGNORE INTO notable_events
                    (headline, detail, category, game_date, player_names, team_names,
                     detection_type, priority)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    e["headline"], e["detail"], e["category"], e["game_date"],
                    json.dumps(e.get("player_names", [])),
                    json.dumps(e.get("team_names", [])),
                    e["detection_type"], e["priority"],
                ))
                if cursor.rowcount > 0:
                    inserted += 1
            except sqlite3.IntegrityError:
                pass
        conn.commit()
        conn.close()
        logger.info(f"AI insights: {inserted} new events inserted")

    return validated


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--season", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true",
                        help="Print snapshot + results without inserting into DB")
    args = parser.parse_args()

    if args.dry_run:
        snapshot, events = generate_ai_insights(args.db, args.season, dry_run=True)
        if snapshot:
            print("=" * 60)
            print("DATA SNAPSHOT SENT TO SONNET")
            print("=" * 60)
            print(snapshot)
            print()
        print("=" * 60)
        print(f"AI INSIGHTS ({len(events)} events)")
        print("=" * 60)
        for i, e in enumerate(events, 1):
            print(f"\n{i}. [{e['category']}] {e['headline']}")
            if e['detail']:
                print(f"   {e['detail']}")
            print(f"   Players: {e['player_names']}, Teams: {e['team_names']}")
    else:
        events = generate_ai_insights(args.db, args.season)
        print(f"Generated {len(events)} AI insight events")
