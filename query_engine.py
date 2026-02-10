"""
Query engine: translates natural language questions to SQL via Claude,
executes against SQLite, and generates natural language answers.

Includes a query router that classifies questions and routes streak
queries to specialized handling.

The LLM service is abstracted behind a simple interface so it can be
swapped for a backend server later.
"""

import json
import re
import sqlite3
import os
import anthropic
import numpy as np
import ruptures as rpt

from schema_description import SCHEMA_DESCRIPTION


DB_PATH = os.path.join(os.path.dirname(__file__), "baseball_stats.db")
MODEL = "claude-sonnet-4-5-20250929"


# --- LLM Service Layer (swap this out for a backend later) ---

class LLMService:
    """Abstraction over Claude API. Replace this class to route through a backend."""

    def __init__(self):
        self.client = anthropic.Anthropic()  # Uses ANTHROPIC_API_KEY env var

    def route_query(self, question: str, history: list = None) -> dict:
        """Classify a question into a query type."""
        messages = []
        if history:
            for prev_q, prev_answer in history:
                messages.append({"role": "user", "content": prev_q})
                messages.append({"role": "assistant", "content": prev_answer})
        messages.append({"role": "user", "content": question})

        response = self.client.messages.create(
            model=MODEL,
            max_tokens=256,
            system=ROUTER_PROMPT,
            messages=messages,
        )
        text = response.content[0].text.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"type": "simple_lookup"}

    def generate_sql(self, question: str, history: list = None) -> str:
        """Translate a natural language question into SQL, with conversation context."""
        messages = []
        if history:
            for prev_q, prev_answer in history:
                messages.append({"role": "user", "content": prev_q})
                messages.append({"role": "assistant", "content": prev_answer})
        messages.append({"role": "user", "content": question})

        response = self.client.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=SQL_GENERATION_PROMPT,
            messages=messages,
        )
        sql = response.content[0].text.strip()
        # Strip markdown code fences if Claude adds them
        sql = re.sub(r'^```(?:sql)?\s*', '', sql)
        sql = re.sub(r'\s*```$', '', sql)
        # Strip Python-style # comments (Claude sometimes uses these instead of SQL -- comments)
        sql = re.sub(r'#[^\n]*', '', sql)
        return sql.strip()

    def generate_answer(self, question: str, sql: str, results: str, history: list = None) -> str:
        """Generate a natural language answer from SQL results, with conversation context."""
        messages = []
        if history:
            for prev_q, prev_answer in history:
                messages.append({"role": "user", "content": prev_q})
                messages.append({"role": "assistant", "content": prev_answer})
        messages.append({
            "role": "user",
            "content": f"Question: {question}\n\nSQL executed: {sql}\n\nResults:\n{results}",
        })

        response = self.client.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=ANSWER_GENERATION_PROMPT,
            messages=messages,
        )
        return response.content[0].text.strip()

    def describe_streaks(self, question: str, streak_data: str, history: list = None) -> str:
        """Generate a natural language description of streak data."""
        messages = []
        if history:
            for prev_q, prev_answer in history:
                messages.append({"role": "user", "content": prev_q})
                messages.append({"role": "assistant", "content": prev_answer})
        messages.append({
            "role": "user",
            "content": f"Question: {question}\n\nStreak data:\n{streak_data}",
        })

        response = self.client.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=STREAK_ANSWER_PROMPT,
            messages=messages,
        )
        return response.content[0].text.strip()


# --- Prompts ---

ROUTER_PROMPT = """You classify baseball questions into query types. Given a question, return a JSON object with the type.

Types:
- "simple_lookup": Standard stat questions, leaderboards, comparisons. Anything about counting stats, averages, splits, or player comparisons.
- "streak_finder": Questions about hot streaks, cold streaks, slumps, when a player was on fire, best/worst stretches, performance over time within a season.

Return ONLY valid JSON, nothing else. Examples:
- "What was Judge's OPS?" → {"type": "simple_lookup"}
- "Compare Soto and Judge" → {"type": "simple_lookup"}
- "Who led the league in HR?" → {"type": "simple_lookup"}
- "When was Judge on a hot streak?" → {"type": "streak_finder"}
- "Did Ohtani have any slumps in 2024?" → {"type": "streak_finder"}
- "What was Judge's best stretch in 2024?" → {"type": "streak_finder"}
- "How did Judge do against lefties?" → {"type": "simple_lookup"}

If unsure, default to "simple_lookup".
"""

SQL_GENERATION_PROMPT = f"""You are a baseball statistics SQL expert. Given a natural language question about baseball stats, generate a SQLite query to answer it.

{SCHEMA_DESCRIPTION}

Rules:
- Output ONLY the SQL query, nothing else. No explanation, no markdown, no code fences.
- If the question is not about baseball statistics, output exactly: SELECT 'OFF_TOPIC'
- Use JOINs between players and season_batting_stats as needed.
- For player name lookups, use LIKE with '%' for flexibility (e.g., WHERE p.name LIKE '%Judge%').
- Always alias tables: players AS p, season_batting_stats AS s.
- Format numbers nicely: use ROUND() for decimals, PRINTF() for batting averages (3 decimal places).
- For "league leaders" or "top" queries, use ORDER BY ... DESC LIMIT 10 unless a specific number is requested.
- For leaderboard/ranking queries on rate stats (AVG, OBP, SLG, OPS, ISO, BABIP), add a minimum plate appearances filter: WHERE plate_appearances >= 400 for a full season, or >= 200 for partial/current seasons. This avoids small sample size noise. Counting stats (HR, RBI, SB, etc.) don't need this filter.
- When the user asks for a player's "stats" without specifying a year, use UNION ALL to return (1) their most recent season row AND (2) a career totals row aggregated across all available seasons. For career totals, SUM the counting stats and recalculate rate stats from sums (e.g., CAST(SUM(hits) AS REAL)/SUM(at_bats) for AVG). Use 'Career' as the season value. Only include the career row if the player has more than one season of data.
- For questions about stats we don't have data for, return SELECT 'NO_DATA' as answer.
"""

ANSWER_GENERATION_PROMPT = """You are a knowledgeable baseball analyst. Given a user's question, the SQL that was run, and the results, provide a clear, concise answer.

Rules:
- Be conversational but accurate. You're talking to a baseball fan.
- STAT GRID FORMAT: When your answer includes 3 or more stats for a player, or stats for multiple players, present them in a stat grid block. Wrap the grid in [STATGRID] and [/STATGRID] tags. Use HEADER: for column names and ROW: for each player. Separate values with commas. Example:

[STATGRID]
HEADER: G, AB, H, HR, RBI, AVG, OBP, SLG, OPS
ROW: 158, 526, 169, 58, 144, .322, .458, .701, 1.159
[/STATGRID]

For single-player grids, do NOT include the player name in the ROW — it's already in your commentary. For comparisons or leaderboards with multiple players, start each ROW with the player name. For leaderboards, include a Rank column:

[STATGRID]
HEADER: Rank, Player, HR
ROW: 1, Aaron Judge (NYY), 58
ROW: 2, Shohei Ohtani (LAD), 54
[/STATGRID]

Only include stats relevant to the question — don't dump every column. Commentary text goes OUTSIDE the [STATGRID] block, before or after it.
- When results include both a specific season and a "Career" row, start each ROW with the year or "Career" as a label — just like player names in comparisons. Do NOT put year/season as a stat column in the HEADER. Example:

[STATGRID]
HEADER: G, AB, H, HR, RBI, AVG, OBP, SLG, OPS
ROW: 2024, 157, 550, 168, 30, 87, .305, .395, .538, .933
ROW: Career, 500, 1800, 550, 100, 250, .306, .390, .535, .925
[/STATGRID]
- For simple single-stat answers (e.g., "Judge hit 58 home runs"), just state the number — no grid needed.
- If the results are empty, say you don't have data for that query and suggest what might work.
- Keep answers short. Resist the urge to narrate or editorialize.
- Don't mention SQL or databases — just answer naturally as if you looked it up.
- If the result is 'OFF_TOPIC', politely redirect: "I'm a baseball stats engine — ask me about player stats!"
"""

STREAK_ANSWER_PROMPT = """You are a knowledgeable baseball analyst describing player performance streaks.

You'll receive pre-detected streak segments for a player's season, identified by change-point analysis. Each segment has dates, number of games, and stats.

Rules:
- CRITICAL: Only present the type of streak the user asked about. If they asked about cold streaks or slumps, ONLY discuss cold data. If they asked about hot streaks, ONLY discuss hot data. Do NOT mention or present the opposite type at all — no "on the flip side", no "conversely", no bonus hot streak info on a cold streak question. If the question is general ("any streaks?"), show the full picture.
- Present each streak's stats in a stat grid block using [STATGRID] and [/STATGRID] tags. Always use the EXACT dates and numbers from the data — never paraphrase dates vaguely like "mid April" when you have exact dates. Example:

[STATGRID]
HEADER: Dates, Games, AVG, OBP, SLG, OPS, HR
ROW: Sept 13 – Sept 28, 12, .360, .469, .760, 1.229, 5
[/STATGRID]

Commentary and context go OUTSIDE the grid block.
- Label streaks in plain language: "hot streak", "cold stretch", "slump", "dominant run", etc.
- IMPORTANT: "hot" and "cold" are defined relative to THAT PLAYER'S own season average, NOT league average or any absolute threshold. A player with a .650 season OPS can still have hot streaks (periods where they hit well above their own .650 norm) and cold streaks (periods well below it). Never reference absolute OPS thresholds like ".750" or ".800" — everything is relative to the individual.
- If only one segment is returned covering the whole season (labeled "average"), this means no major performance shifts were detected. BUT you may also receive "SENSITIVE STREAK FALLBACK" data showing subtler stretches. When this fallback data is present:
  - Briefly note the player was fairly consistent overall without any dramatic swings.
  - Present ONLY the streak type that matches what the user asked about. If they asked about cold streaks, show ONLY the coldest stretch with its exact dates, games, and stats. If they asked about hot streaks, show ONLY the hottest stretch. Do NOT mention the other type.
  - Use natural language like "That said, he did have a relatively cold stretch..." or "That said, he did have a relatively hot stretch..."
  - Compare the segment OPS to the player's season OPS (provided in the data) to show how much they deviated from their own norm.
  - Never mention "sensitive analysis", "methodology", "change-point detection", or any technical language. Just talk about the stretches naturally as a baseball analyst would.
- Keep it concise. Present the data clearly, add minimal commentary.
"""


# --- Query Execution ---

class QueryEngine:
    """Handles the full question → SQL → answer pipeline."""

    MAX_HISTORY = 5  # Keep last 5 exchanges for context

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self.llm = LLMService()
        self.history = []  # List of (question, answer) tuples

    def ask(self, question: str) -> str:
        """Answer a natural language baseball question."""
        # Step 0: Route the query
        route = self.llm.route_query(question, self.history)
        query_type = route.get("type", "simple_lookup")

        if query_type == "streak_finder":
            answer = self._handle_streak_query(question)
        else:
            answer = self._handle_sql_query(question)

        self._add_to_history(question, answer)
        return answer

    def _handle_sql_query(self, question: str) -> str:
        """Handle standard text-to-SQL queries."""
        # Step 1: Generate SQL
        sql = self.llm.generate_sql(question, self.history)

        # Handle off-topic / no-data
        if "OFF_TOPIC" in sql:
            return "I'm a baseball stats engine — ask me about player stats, leaders, averages, and more!"
        if "NO_DATA" in sql:
            return "I don't have the data needed for that question yet. Try asking about 2024 season batting stats!"

        # Step 2: Execute SQL
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(sql)
            columns = [desc[0] for desc in cursor.description] if cursor.description else []
            rows = cursor.fetchall()
        except Exception as e:
            return f"I had trouble with that query. Could you rephrase? (Error: {e})"

        # Streak fallback: if SQL queried streaks table with a performance filter
        # and got 0 results, get all streaks + sliding window best/worst stretches
        is_streak_query = "streaks" in sql.lower()
        if not rows and is_streak_query:
            all_rows = self._get_all_streaks_for_query(conn, sql)
            if all_rows:
                streak_columns = ["id", "player_id", "season", "start_date", "end_date",
                                  "num_games", "batting_avg", "obp", "slg", "ops",
                                  "home_runs", "hits", "at_bats", "walks", "strikeouts",
                                  "performance"]
                header = " | ".join(streak_columns)
                lines = [header, "-" * len(header)]
                for row in all_rows:
                    lines.append(" | ".join(str(v) for v in row))
                streak_data = "\n".join(lines)

                # Add sliding window fallback for single-segment players
                if len(all_rows) == 1:
                    fallback = self._find_best_worst_stretches(conn, all_rows[0])
                    if fallback:
                        streak_data += "\n\n" + fallback

                conn.close()
                return self.llm.describe_streaks(question, streak_data, self.history)

        conn.close()

        # Format results
        if not rows:
            results = "No results found."
        else:
            header = " | ".join(columns)
            lines = [header, "-" * len(header)]
            for row in rows[:50]:
                lines.append(" | ".join(str(v) for v in row))
            results = "\n".join(lines)

        # Step 3: Generate answer
        # Use streak-specific prompt when the query hit the streaks table
        if is_streak_query and rows:
            return self.llm.describe_streaks(question, results, self.history)
        return self.llm.generate_answer(question, sql, results, self.history)

    def _handle_streak_query(self, question: str) -> str:
        """Handle streak finder queries using precomputed streak data."""
        # Use Claude to generate SQL against the streaks table
        sql = self.llm.generate_sql(question, self.history)

        if "OFF_TOPIC" in sql or "NO_DATA" in sql:
            return "I don't have streak data for that query. Try asking about a specific player's streaks in 2024 or 2025."

        # Execute SQL
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(sql)
            columns = [desc[0] for desc in cursor.description] if cursor.description else []
            rows = cursor.fetchall()
        except Exception as e:
            return f"I had trouble with that streak query. Could you rephrase? (Error: {e})"

        # If filtered query returned no rows (e.g. asked for "hot" but player had none),
        # check if the player has ANY streak data — if so, they just had no change points.
        used_fallback = False
        if not rows:
            all_rows = self._get_all_streaks_for_query(conn, sql)
            if not all_rows:
                conn.close()
                return "I don't have streak data for that player/season. Streak data is available for qualified batters (400+ PA) in 2024-2025."
            rows = all_rows
            columns = ["id", "player_id", "season", "start_date", "end_date", "num_games",
                        "batting_avg", "obp", "slg", "ops", "home_runs", "hits",
                        "at_bats", "walks", "strikeouts", "performance"]
            used_fallback = True

        # Format streak data
        header = " | ".join(columns)
        lines = [header, "-" * len(header)]
        for row in rows:
            lines.append(" | ".join(str(v) for v in row))
        streak_data = "\n".join(lines)

        # Fallback: if only 1 segment (no change points), find best/worst stretches via sliding window
        if used_fallback or len(rows) == 1:
            fallback = self._find_best_worst_stretches(conn, rows[0])
            if fallback:
                streak_data += "\n\n" + fallback

        conn.close()

        # Have Claude describe the streaks
        return self.llm.describe_streaks(question, streak_data, self.history)

    def _get_all_streaks_for_query(self, conn, original_sql: str) -> list:
        """When a filtered streak query returns 0 rows, try to get ALL streaks for that player/season.

        Extracts the player name and season from the original SQL, then queries
        all streak segments without any performance filter.
        """
        try:
            # Extract player name from LIKE '%Name%' pattern
            name_match = re.search(r"LIKE\s+'%([^%]+)%'", original_sql)
            if not name_match:
                return []
            player_name = name_match.group(1)

            # Extract season (4-digit year near 'season')
            season_match = re.search(r"season\s*=\s*(\d{4})", original_sql)
            season = int(season_match.group(1)) if season_match else 2024

            cursor = conn.cursor()
            cursor.execute("""
                SELECT s.* FROM streaks s
                JOIN players p ON s.player_id = p.player_id
                WHERE p.name LIKE ? AND s.season = ?
                ORDER BY s.start_date
            """, (f"%{player_name}%", season))
            return cursor.fetchall()
        except Exception:
            return []

    # Fallback PELT parameters — more sensitive than the precomputed streaks
    FALLBACK_PENALTY = 1.5
    FALLBACK_MIN_SEGMENT = 7
    FALLBACK_MAX_SEGMENT = 30
    FALLBACK_ROLLING_WINDOW = 5

    def _find_best_worst_stretches(self, conn, streak_row) -> str:
        """Re-run PELT with a lower penalty to find subtler streaks.

        Used as a fallback when the precomputed streaks (penalty=3) found no
        change points. Returns the hottest and coldest segments between
        7-30 games.
        """
        # Extract player_id and season from the streak row (SELECT s.* format)
        player_id = None
        season = None
        for val in streak_row:
            if isinstance(val, str) and not player_id and not val.startswith("20") and val not in ("hot", "cold", "average"):
                player_id = val
            elif isinstance(val, int) and not season and 2020 <= val <= 2030:
                season = val
        if not player_id or not season:
            return ""

        cursor = conn.cursor()
        cursor.execute("""
            SELECT date, at_bats, hits, doubles, triples, home_runs,
                   walks, plate_appearances
            FROM game_batting_logs
            WHERE player_id = ? AND season = ?
            ORDER BY date ASC
        """, (player_id, season))
        games = cursor.fetchall()

        if len(games) < self.FALLBACK_MIN_SEGMENT * 2:
            return ""

        # Compute per-game OPS signal
        ops_values = []
        for g in games:
            date, ab, h, d, t, hr, bb, pa = g
            if ab and ab > 0 and pa and pa > 0:
                tb = (h - d - t - hr) + 2 * d + 3 * t + 4 * hr
                slg = tb / ab
                obp = (h + bb) / pa
                ops_values.append(obp + slg)
            else:
                ops_values.append(0.0)

        signal = np.array(ops_values)
        season_ops = np.mean(signal)

        # Smooth and run PELT with lower penalty
        smoothed = np.convolve(signal, np.ones(self.FALLBACK_ROLLING_WINDOW) / self.FALLBACK_ROLLING_WINDOW, mode='same')
        smoothed = smoothed.reshape(-1, 1)

        algo = rpt.Pelt(model="l2", min_size=self.FALLBACK_MIN_SEGMENT, jump=1)
        algo.fit(smoothed)
        breakpoints = algo.predict(pen=self.FALLBACK_PENALTY)

        # Build segments and find the hottest/coldest in the 7-30 game range
        segments = []
        start_idx = 0
        for end_idx in breakpoints:
            if end_idx > len(games):
                end_idx = len(games)
            num_games = end_idx - start_idx
            if self.FALLBACK_MIN_SEGMENT <= num_games <= self.FALLBACK_MAX_SEGMENT:
                seg = self._compute_segment(games, start_idx, end_idx)
                seg["season_ops"] = season_ops
                segments.append(seg)
            start_idx = end_idx

        if not segments:
            return ""

        # Find hottest and coldest by OPS deviation from season average
        hottest = max(segments, key=lambda s: s["ops"])
        coldest = min(segments, key=lambda s: s["ops"])

        def fmt(val):
            return f"{val:.3f}"

        lines = [f"SENSITIVE STREAK FALLBACK (lower-threshold change-point detection, {self.FALLBACK_MIN_SEGMENT}-{self.FALLBACK_MAX_SEGMENT} game segments):"]
        lines.append(f"Player season OPS: {fmt(season_ops)}")
        lines.append(
            f"Hottest segment: {hottest['start_date']} to {hottest['end_date']} ({hottest['num_games']} games) — "
            f"{fmt(hottest['avg'])}/{fmt(hottest['obp'])}/{fmt(hottest['slg'])} ({fmt(hottest['ops'])} OPS), "
            f"{hottest['hr']} HR, {hottest['hits']} H in {hottest['ab']} AB"
        )
        if coldest is not hottest:
            lines.append(
                f"Coldest segment: {coldest['start_date']} to {coldest['end_date']} ({coldest['num_games']} games) — "
                f"{fmt(coldest['avg'])}/{fmt(coldest['obp'])}/{fmt(coldest['slg'])} ({fmt(coldest['ops'])} OPS), "
                f"{coldest['hr']} HR, {coldest['hits']} H in {coldest['ab']} AB"
            )
        return "\n".join(lines)

    @staticmethod
    def _compute_segment(games, start_idx, end_idx) -> dict:
        """Compute aggregate stats for a segment of games."""
        seg = games[start_idx:end_idx]
        ab = sum(g[1] or 0 for g in seg)
        h = sum(g[2] or 0 for g in seg)
        d = sum(g[3] or 0 for g in seg)
        t = sum(g[4] or 0 for g in seg)
        hr = sum(g[5] or 0 for g in seg)
        bb = sum(g[6] or 0 for g in seg)
        pa = sum(g[7] or 0 for g in seg)
        avg = round(h / ab, 3) if ab > 0 else 0
        obp = round((h + bb) / pa, 3) if pa > 0 else 0
        tb = (h - d - t - hr) + 2 * d + 3 * t + 4 * hr
        slg = round(tb / ab, 3) if ab > 0 else 0
        return {
            "start_date": seg[0][0], "end_date": seg[-1][0],
            "num_games": len(seg), "avg": avg, "obp": obp, "slg": slg,
            "ops": round(obp + slg, 3), "hr": hr, "hits": h, "ab": ab, "bb": bb,
        }

    def _add_to_history(self, question: str, answer: str):
        """Track conversation history, keeping only the last N exchanges."""
        self.history.append((question, answer))
        if len(self.history) > self.MAX_HISTORY:
            self.history = self.history[-self.MAX_HISTORY:]
