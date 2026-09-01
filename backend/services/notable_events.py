"""
Notable Events Detection Engine

Runs after each cron refresh to scan game logs, streaks, and stats
for notable baseball events. Results stored in `notable_events` table
and served via GET /notable-events.

Tiered detection:
  Tier 1 (high-signal): Active streaks, season pace milestones
  Tier 2 (medium): Career milestones, standout single-game performances, hot streaks
  Tier 3 (backfill): Relaxed thresholds to guarantee 3+ events/day
"""

import json
import sqlite3
import os
import time
from datetime import date, datetime
from services.historical_scans import _get_game_line, fmt_ip, attach_context
from services.qualification import min_pa as _qual_min_pa

# PELT config used by the feed hot-streak detector. Mirrors data_pipeline/detect_streaks.py
# but the detector is run-time, not precomputed — windows are re-detected each cron.
_PELT_MIN_SIZE = 6          # allow 6-game windows (user's "2 series" floor)
_PELT_PENALTY = 3           # primary pass
_PELT_PENALTY_FALLBACK = 1.5  # relaxed pass if no change point found
_PELT_ROLLING_WINDOW = 5
_FEED_STREAK_MIN_GAMES = 6
_FEED_STREAK_MAX_GAMES = 29  # 30+ is a good month, not a streak

DB_PATH = os.getenv("DB_PATH", "/data/baseball_stats_full.db")

# Retrosheet team code → display name
RETRO_TO_DISPLAY = {
    # Current franchises
    "NYA": "Yankees", "NYN": "Mets", "LAN": "Dodgers", "ANA": "Angels",
    "CHN": "Cubs", "CHA": "White Sox", "SFN": "Giants", "SDN": "Padres",
    "SLN": "Cardinals", "KCA": "Royals", "TBA": "Rays", "WAS": "Nationals",
    "BOS": "Red Sox", "HOU": "Astros", "ATL": "Braves", "PHI": "Phillies",
    "TEX": "Rangers", "TOR": "Blue Jays", "BAL": "Orioles", "MIN": "Twins",
    "CLE": "Guardians", "SEA": "Mariners", "MIL": "Brewers", "CIN": "Reds",
    "PIT": "Pirates", "DET": "Tigers", "ARI": "Diamondbacks", "COL": "Rockies",
    "MIA": "Marlins", "OAK": "Athletics", "ATH": "Athletics",
    # Historic franchises / prior team codes
    "BRO": "Brooklyn Dodgers", "BSN": "Boston Braves", "PHA": "Philadelphia Athletics",
    "SLA": "St. Louis Browns", "NY1": "New York Giants", "WS1": "Washington Senators",
    "WS2": "Washington Senators", "KC1": "Kansas City Athletics",
    "MLN": "Milwaukee Braves", "LAA": "LA Angels", "CAL": "California Angels",
    "MON": "Montreal Expos", "FLO": "Florida Marlins",
}


_MANUAL_MOMENTS_CACHE = None
_MANUAL_MOMENTS_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                    "data", "manual_moments.json")

_PERFECT_GAMES_CACHE = None
_PERFECT_GAMES_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                   "data", "perfect_games.json")
# MLB all-time perfect game count (21 modern regular season + Larsen WS + 2 pre-1900)
_TOTAL_PERFECT_GAMES_MLB = 24


# Negro League team codes — mirrors list in build_historic_moments.py. Used
# to filter On This Date entries so obscure Negro League no-hitters don't
# surface with cryptic team codes like "MEM"/"BIR". Drop for now; revisit
# when we have proper team display names for these codes.
_NEGRO_LEAGUE_TEAMS = frozenset({
    "BIR", "CAG", "KCM", "HOM", "MEM", "PH5", "NY5", "NY6", "NW1", "NW2",
    "BLG", "BLS", "CVB", "PTC", "IN6", "IN7", "IN9", "JAX", "SSN", "SSA",
    "ATN", "CIA", "SNO", "CV9", "WEG", "HIL", "CI1", "CI2", "BRN", "DT1",
    "DT2", "NSH", "HB1", "LOS", "PBG", "TOL", "BAC", "BCR", "CHS", "CLE2",
    "HAR", "IND", "LOU", "NEW", "NYB", "NYC", "PHS", "PIT2", "STL2", "WIL",
    # Additional Negro League team codes surfaced in On This Date sandbox
    # (all 1921-1949 with small game counts — verified not MLB franchises):
    "CB1", "CG1", "CNN", "IN4", "SLG", "ML4", "BRG", "CUW", "HAE", "CUE",
    "DAY", "NYL", "CV6", "LS4", "WSW", "CUS", "BFA", "WBS", "IN8", "CCB",
    "HSL", "TLC", "HOE", "LCB",
})


def _load_manual_moments():
    """Hand-curated iconic moments (Jackie Robinson debut, Aaron 715, etc.).
    Loaded once, cached for module lifetime. Returns [] on any error.
    """
    global _MANUAL_MOMENTS_CACHE
    if _MANUAL_MOMENTS_CACHE is not None:
        return _MANUAL_MOMENTS_CACHE
    try:
        with open(_MANUAL_MOMENTS_PATH) as f:
            _MANUAL_MOMENTS_CACHE = json.load(f)
    except Exception as e:
        print(f"  Manual moments load failed (non-fatal): {e}")
        _MANUAL_MOMENTS_CACHE = []
    return _MANUAL_MOMENTS_CACHE


def _load_perfect_games():
    """Hand-curated list of MLB perfect games. Return list of {date, player, opponent}."""
    global _PERFECT_GAMES_CACHE
    if _PERFECT_GAMES_CACHE is not None:
        return _PERFECT_GAMES_CACHE
    try:
        with open(_PERFECT_GAMES_PATH) as f:
            _PERFECT_GAMES_CACHE = json.load(f)
    except Exception as e:
        print(f"  Perfect games load failed (non-fatal): {e}")
        _PERFECT_GAMES_CACHE = []
    return _PERFECT_GAMES_CACHE


def team_display(retro_code):
    """Convert Retrosheet team code to display name."""
    return RETRO_TO_DISPLAY.get(retro_code, retro_code)


def ensure_table(conn):
    """Create the notable_events table if it doesn't exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS notable_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            headline TEXT NOT NULL,
            detail TEXT NOT NULL,
            category TEXT NOT NULL,
            game_date TEXT NOT NULL,
            player_names TEXT,
            team_names TEXT,
            detection_type TEXT NOT NULL,
            priority INTEGER NOT NULL,
            game_context TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(detection_type, game_date, headline)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_notable_date ON notable_events(game_date)
    """)
    # Migrate: add columns if missing
    cols = {row[1] for row in conn.execute("PRAGMA table_info(notable_events)").fetchall()}
    if "game_context" not in cols:
        conn.execute("ALTER TABLE notable_events ADD COLUMN game_context TEXT")
    if "expires_at" not in cols:
        conn.execute("ALTER TABLE notable_events ADD COLUMN expires_at TEXT")
    # Deep-scan cooldown persistence: tracks (player, scan_type) last-fire
    # date + historical "since" gap for the progressive-deepen logic. Without
    # persistence, cooldown state resets every cron invocation and the
    # story-deepen check never kicks in.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS deep_scan_cooldowns (
            player_id TEXT NOT NULL,
            scan_type TEXT NOT NULL,
            last_date TEXT NOT NULL,
            last_gap INTEGER NOT NULL,
            PRIMARY KEY (player_id, scan_type)
        )
    """)
    conn.commit()


def backfill_game_context(conn, season):
    """One-time: fill game_context for existing events that don't have it."""
    rows = conn.execute("""
        SELECT id, player_names, game_date FROM notable_events
        WHERE (game_context IS NULL OR game_context = '') AND player_names IS NOT NULL
    """).fetchall()

    if not rows:
        return 0

    updated = 0
    for row_id, player_names_json, game_date in rows:
        try:
            names = json.loads(player_names_json) if player_names_json else []
            if not names:
                continue
            pid_row = conn.execute(
                "SELECT player_id FROM players WHERE name = ?", (names[0],)
            ).fetchone()
            if not pid_row:
                continue
            context = _get_game_context(conn, pid_row[0], game_date, season)
            if context:
                conn.execute("UPDATE notable_events SET game_context = ? WHERE id = ?",
                             (context, row_id))
                updated += 1
        except:
            continue

    conn.commit()
    return updated


def _get_game_context(conn, player_id, game_date, season):
    """Build game context string like 'April 5 · Dodgers 4 - Astros 3'.

    Looks up the player's team and opponent from game logs, then fetches
    the actual score from the MLB Stats API.
    """
    # Get player's game info (team + opponent)
    game = conn.execute("""
        SELECT g.opponent, g.vishome
        FROM game_batting_logs g
        WHERE g.player_id = ? AND g.date = ? AND g.season = ?
        LIMIT 1
    """, (player_id, game_date, season)).fetchone()

    if not game:
        game = conn.execute("""
            SELECT g.opponent, g.vishome
            FROM game_pitching_logs g
            WHERE g.player_id = ? AND g.date = ? AND g.season = ?
            LIMIT 1
        """, (player_id, game_date, season)).fetchone()

    if not game:
        try:
            from datetime import datetime
            dt = datetime.strptime(game_date, "%Y-%m-%d")
            return dt.strftime("%B %-d")
        except:
            return game_date

    opponent, vishome = game

    # Get player's team from players table
    team_row = conn.execute(
        "SELECT team FROM players WHERE player_id = ?", (player_id,)
    ).fetchone()
    team_code = team_row[0] if team_row else None

    # Format date
    try:
        from datetime import datetime
        dt = datetime.strptime(game_date, "%Y-%m-%d")
        date_str = dt.strftime("%B %-d")
    except:
        date_str = game_date

    if not team_code:
        return date_str

    team_name = team_display(team_code)
    opp_name = team_display(opponent)

    # Fetch actual score from MLB Stats API
    team_runs, opp_runs = _fetch_game_score(game_date, team_code, opponent)
    if team_runs is None:
        return f"{date_str} · {team_name} vs {opp_name}"

    # Winning team listed first
    if team_runs >= opp_runs:
        return f"{date_str} · {team_name} {team_runs} - {opp_name} {opp_runs}"
    else:
        return f"{date_str} · {opp_name} {opp_runs} - {team_name} {team_runs}"


# Cache for MLB Stats API score lookups: {"YYYY-MM-DD": {(away, home): (away_runs, home_runs)}}
_score_cache: dict = {}


def _fetch_game_score(game_date, team_code, opponent_code):
    """Fetch the actual game score from the MLB Stats API.

    Returns (team_runs, opponent_runs) or (None, None) if not found.
    """
    import requests
    import sqlite3 as _sq
    import os as _os
    import time as _time

    def _lookup(cached):
        for (away, home), (away_r, home_r) in cached.items():
            if away == team_code and home == opponent_code:
                return away_r, home_r
            if home == team_code and away == opponent_code:
                return home_r, away_r
        return None, None

    # L1: per-worker in-memory cache
    if game_date in _score_cache:
        return _lookup(_score_cache[game_date])

    # L2: DB-backed shared cache (2026-08-06). The in-memory cache is
    # per-gunicorn-worker, so cold workers each re-hit statsapi at 10s per
    # uncached date — under retry load that serially wedges every worker
    # (the "everything hangs" outage). The DB cache is shared and survives
    # restarts. Non-final dates (no rows) are negative-cached for 10 min
    # via the meta table so in-progress evenings don't re-fetch per request.
    _db = _sq.connect(_os.getenv("DB_PATH", "/data/baseball_stats_full.db"), timeout=5)
    try:
        _db.execute("""CREATE TABLE IF NOT EXISTS game_score_cache (
            game_date TEXT, away TEXT, home TEXT,
            away_runs INTEGER, home_runs INTEGER,
            UNIQUE(game_date, away, home))""")
        _db.execute("""CREATE TABLE IF NOT EXISTS game_score_cache_meta (
            game_date TEXT PRIMARY KEY, fetched_at REAL)""")
        rows = _db.execute(
            "SELECT away, home, away_runs, home_runs FROM game_score_cache WHERE game_date = ?",
            (game_date,)).fetchall()
        if rows:
            cached = {(r[0], r[1]): (r[2], r[3]) for r in rows}
            _score_cache[game_date] = cached
            return _lookup(cached)
        meta = _db.execute(
            "SELECT fetched_at FROM game_score_cache_meta WHERE game_date = ?",
            (game_date,)).fetchone()
        if meta and (_time.time() - meta[0]) < 600:
            _score_cache[game_date] = {}
            return None, None
    except Exception:
        pass

    # L3: fetch from MLB Stats API and persist
    try:
        resp = requests.get(
            "https://statsapi.mlb.com/api/v1/schedule",
            params={"sportId": 1, "date": game_date, "hydrate": "linescore"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        try:
            _db.execute("INSERT OR REPLACE INTO game_score_cache_meta VALUES (?, ?)",
                        (game_date, _time.time()))
            _db.commit()
            _db.close()
        except Exception:
            pass
        return None, None

    from services.daily_games import _team_to_retro

    date_scores = {}
    dates = data.get("dates", [])
    games = dates[0].get("games", []) if dates else []
    for game in games:
        teams = game.get("teams", {})
        away_info = teams.get("away", {})
        home_info = teams.get("home", {})
        away_retro = _team_to_retro(away_info.get("team", {}))
        home_retro = _team_to_retro(home_info.get("team", {}))

        linescore = game.get("linescore", {})
        away_runs = linescore.get("teams", {}).get("away", {}).get("runs")
        home_runs = linescore.get("teams", {}).get("home", {}).get("runs")

        if away_retro and home_retro and away_runs is not None and home_runs is not None:
            date_scores[(away_retro, home_retro)] = (away_runs, home_runs)

    _score_cache[game_date] = date_scores
    try:
        _db.executemany(
            "INSERT OR REPLACE INTO game_score_cache VALUES (?, ?, ?, ?, ?)",
            [(game_date, a, h, ar, hr) for (a, h), (ar, hr) in date_scores.items()])
        _db.execute("INSERT OR REPLACE INTO game_score_cache_meta VALUES (?, ?)",
                    (game_date, _time.time()))
        _db.commit()
        _db.close()
    except Exception:
        pass

    return _lookup(date_scores)


def _get_latest_date(conn, season):
    """Get the most recent game date in the season."""
    row = conn.execute(
        "SELECT MAX(date) FROM game_batting_logs WHERE season = ?", (season,)
    ).fetchone()
    return row[0] if row and row[0] else None


def _ordinal(n):
    """Convert number to ordinal with thousands separators: 1st, 1,552nd, etc."""
    n = int(n)
    suffix = "th" if 11 <= n % 100 <= 13 else ['th','st','nd','rd','th','th','th','th','th','th'][n % 10]
    return f"{n:,}{suffix}"


def _rank_phrase(rank, total, stat, threshold):
    """Build the 'Nth player at the time to reach X' clause.

    rank: 1-indexed order this player crossed the threshold
    total: total players ever to cross it (used for "now N total" tail)
    stat: stat key used to pick a specific noun ("RBI mark" vs "milestone")
    """
    if not rank or rank <= 0:
        return ""
    # Noun for the milestone — makes "2,000th career run" disambiguation clear.
    mark = {
        "rbi": f"the {threshold:,} RBI mark",
        "home_runs": f"the {threshold:,} HR mark",
        "hits": f"the {threshold:,} hit mark",
        "runs": f"the {threshold:,} runs scored mark",
        "stolen_bases": f"the {threshold:,} stolen base mark",
        "strikeouts": f"the {threshold:,} strikeout mark",
        "wins": f"the {threshold:,} win mark",
        "saves": f"the {threshold:,} save mark",
        "season_home_runs": f"a {threshold}-HR season",
    }.get(stat, "the milestone")

    if rank == 1:
        if total and total > 1:
            return f"the first player to reach {mark} (now {total:,} total)"
        return f"the first player to reach {mark}"
    if total and rank == total:
        return f"the {_ordinal(rank)} and most recent player to reach {mark}"
    if total and total > rank:
        return f"the {_ordinal(rank)} player at the time to reach {mark} (now {total:,} total)"
    return f"the {_ordinal(rank)} player to reach {mark}"


def _player_name(conn, player_id):
    """Look up player display name."""
    row = conn.execute(
        "SELECT name FROM players WHERE player_id = ?", (player_id,)
    ).fetchone()
    return row[0] if row else player_id


def _a_or_an(word: str) -> str:
    """Pick "a" or "an" based on the first sound of `word`.

    Simple first-letter vowel check — covers MLB franchise names
    cleanly (Athletics, Astros, Angels, Orioles all start with a
    vowel; everything else with a consonant). Handles the empty/None
    edge by defaulting to "a".

    Edge cases NOT handled (none of which apply to MLB team names):
    - "U" words that begin with a "you" sound ("a uniform")
    - Silent-H words ("an honor")
    """
    if not word:
        return "a"
    return "an" if word[0].lower() in "aeio" else "a"


def _continue_with_context(base: str, continuation: str, *, past_tense: bool = False) -> str:
    """Append a follow-up context clause. Delegates to the shared attach_context
    in historical_scans so person-appositive (comma), event-appositive ("That's"
    / "That was"), and standalone-clause handling stay uniform across detectors.
    past_tense=True is used for "On This Date" context."""
    return attach_context(base, continuation, past=past_tense)


def _player_team_display(conn, player_id, season):
    """Get team display name for a player in a season."""
    row = conn.execute(
        "SELECT team FROM season_batting_stats WHERE player_id = ? AND season = ?",
        (player_id, season)
    ).fetchone()
    if not row:
        row = conn.execute(
            "SELECT team FROM season_pitching_stats WHERE player_id = ? AND season = ?",
            (player_id, season)
        ).fetchone()
    return team_display(row[0]) if row and row[0] else ""


def _player_team_code(conn, player_id, season):
    """Get raw single-team code for franchise lookups. Multi-team season
    ('NYA/BOS') returns first code — consistent with 'primary team' heuristic.
    Returns None if no match."""
    row = conn.execute(
        "SELECT team FROM season_batting_stats WHERE player_id = ? AND season = ?",
        (player_id, season)
    ).fetchone()
    if not row:
        row = conn.execute(
            "SELECT team FROM season_pitching_stats WHERE player_id = ? AND season = ?",
            (player_id, season)
        ).fetchone()
    if row and row[0]:
        return row[0].split("/")[0].strip() or None
    return None


# ---------------------------------------------------------------------------
# Historical comparison engine
# ---------------------------------------------------------------------------

def _historical_context(conn, streak_len, condition_sql, table="game_batting_logs",
                        exclude_season=None, exclude_player=None,
                        at_bat_filter="at_bats > 0",
                        player_team=None,
                        streak_label="hitting",
                        current_date=None,
                        secondary_names=None):
    """For streaks: find the last time someone had a consecutive-game streak
    of this length. Returns context string or empty.

    Returns MLB-wide "the longest {label} streak since X" when streak >= 20,
    AND a franchise-specific "the longest by a {Team} player since X" when
    the team match goes 10+ years deeper than the MLB match (or MLB is
    empty). player_team: current player's team code (for franchise anchor).

    streak_label: noun-phrase fragment naming the streak type ("hitting",
    "on-base", "HR"). Inserted into the phrase so users see a clear subject
    on the second sentence — "That's the longest on-base streak since X"
    instead of the antecedent-ambiguous "The longest since X."

    current_date: optional ISO date string of the current event. Used by the
    cross-season `historical_streaks` lookup (hitting + on-base only) to
    exclude streaks that ended on or after the current event's date. Without
    it, the historical_streaks path falls back to filtering by end_season
    only — which can miss truly recent comparable streaks.

    secondary_names: optional list that the function appends comparison
    player names to (e.g., the "Ramírez" in "longest since Ramírez (39)").
    Callers add these to the event's player_names array so iOS can auto-link
    second/third mentions in the rendered feed text.

    For hitting and on-base streaks, the MLB-wide lookup queries the
    precomputed `historical_streaks` table — that table walks chronologically
    across season boundaries, so cross-season runs (e.g., Ohtani's 53-game
    on-base streak Aug 2025 → Apr 2026) are recognized. The legacy per-season
    game-log scan misses these. Falls back to per-season scan for streak
    types not in historical_streaks (e.g., HR streaks).

    Returned phrase starts lowercase so _continue_with_context prepends
    "That's" for the standalone-sentence form. Comma-join callers can use
    the string as-is without `.lower()` (which would also lowercase player
    names — historic bug).
    """
    # Only add "longest since" context for genuinely rare streaks
    if streak_len < 20:
        return ""

    exclude_season = exclude_season or 0
    exclude_player = exclude_player or ""

    # Map streak_label to historical_streaks.streak_type. None = no
    # precomputed table coverage for this streak type — fall back to scan.
    _HIST_STREAK_TYPE = {"hitting": "hitting", "on-base": "on_base"}
    hist_type = _HIST_STREAK_TYPE.get(streak_label)

    # A streak counts as COMPLETED if it is from a PRIOR season (history is
    # always completed) OR it is current-season with a breaking game after
    # end_date — a game where the player appeared but did not extend it. Only
    # a current-season streak with no break is ACTIVE (ongoing). The break
    # check is staleness-proof (reads the game log, not the snapshot end_date),
    # and the prior-season carve-out prevents a career-end historical streak
    # (no later games in the data, e.g. Shoeless Joe Jackson 1920) from being
    # misread as "active." Active runs must not be cited as a completed prior.
    _cur_season = int(exclude_season or 0)
    _completed_clause = f"""
        AND (historical_streaks.end_season != {_cur_season} OR EXISTS (
            SELECT 1 FROM {table}
            WHERE {table}.player_id = historical_streaks.player_id
              AND {table}.date > historical_streaks.end_date
              AND ({at_bat_filter}) AND NOT ({condition_sql})
        ))"""

    def _scan_active_leader():
        """Another player currently on a LONGER active streak of this type.
        Active = CURRENT-season streak with no breaking game after end_date.
        Returns (pid, length) of the longest such streak, or None. When
        present, the current player is behind an active leader, so we anchor
        on that leader's *current* streak rather than a completed historical
        one. Restricted to the current season so historical career-end
        streaks (false 'active' due to no later games) can't be cited."""
        if not hist_type or not _cur_season:
            return None
        row = conn.execute(f"""
            SELECT player_id, length
            FROM historical_streaks
            WHERE streak_type = ?
              AND length > ?
              AND player_id != ?
              AND end_season = {_cur_season}
              AND NOT EXISTS (
                  SELECT 1 FROM {table}
                  WHERE {table}.player_id = historical_streaks.player_id
                    AND {table}.date > historical_streaks.end_date
                    AND ({at_bat_filter}) AND NOT ({condition_sql})
              )
            ORDER BY length DESC
            LIMIT 1
        """, (hist_type, streak_len, exclude_player)).fetchone()
        return (row[0], row[1]) if row else None

    def _scan_historical_streaks():
        """Cross-season MLB-wide lookup for the most recent COMPLETED streak
        of qualifying length. Returns (end_season, pid, length) or None.

        Filters by end_date when current_date is provided so streaks
        ending on/after the current event don't show up as 'past' runs.
        Falls back to end_season < exclude_season when no current_date,
        which matches the per-season-scan semantics. Requires the streak be
        completed (see _completed_clause) so active runs aren't cited."""
        if not hist_type:
            return None
        if current_date:
            row = conn.execute(f"""
                SELECT player_id, length, end_season
                FROM historical_streaks
                WHERE streak_type = ?
                  AND length >= ?
                  AND player_id != ?
                  AND end_date < ?
                  {_completed_clause}
                ORDER BY end_date DESC, length DESC
                LIMIT 1
            """, (hist_type, streak_len, exclude_player, current_date)).fetchone()
        else:
            row = conn.execute(f"""
                SELECT player_id, length, end_season
                FROM historical_streaks
                WHERE streak_type = ?
                  AND length >= ?
                  AND player_id != ?
                  AND end_season < ?
                  {_completed_clause}
                ORDER BY end_date DESC, length DESC
                LIMIT 1
            """, (hist_type, streak_len, exclude_player, exclude_season)).fetchone()
        if row:
            return (row[2], row[0], row[1])
        return None

    seasons = conn.execute(f"""
        SELECT DISTINCT season FROM {table}
        WHERE season < ?
        ORDER BY season DESC
    """, (exclude_season,)).fetchall()

    def _scan_seasons(team_codes_filter=None):
        """Yield (season, best_pid, best_run) for the first season found with a
        qualifying streak. team_codes_filter: list of codes to restrict to, or None for MLB-wide."""
        for (szn,) in seasons:
            if team_codes_filter:
                placeholders = ",".join("?" * len(team_codes_filter))
                # Scope to players whose season team was exactly one of the
                # franchise codes (exact match excludes 'NYA/BOS' mid-season trades).
                games = conn.execute(f"""
                    SELECT player_id, ({condition_sql}) as met
                    FROM {table}
                    WHERE season = ? AND ({at_bat_filter})
                      AND player_id IN (
                          SELECT player_id FROM {
                              "season_batting_stats" if "batting" in table else "season_pitching_stats"
                          }
                          WHERE season = ? AND team IN ({placeholders})
                      )
                    ORDER BY player_id, date
                """, [szn, szn] + team_codes_filter).fetchall()
            else:
                games = conn.execute(f"""
                    SELECT player_id, ({condition_sql}) as met
                    FROM {table}
                    WHERE season = ? AND ({at_bat_filter})
                    ORDER BY player_id, date
                """, (szn,)).fetchall()

            best_pid = None
            best_run = 0
            current_pid = None
            current_run = 0
            max_run_for_player = 0
            for pid, met in games:
                if pid != current_pid:
                    if current_pid and current_pid != exclude_player and max_run_for_player > best_run:
                        best_run = max_run_for_player
                        best_pid = current_pid
                    current_pid = pid
                    current_run = 0
                    max_run_for_player = 0
                if met:
                    current_run += 1
                    max_run_for_player = max(max_run_for_player, current_run)
                else:
                    current_run = 0
            if current_pid and current_pid != exclude_player and max_run_for_player > best_run:
                best_run = max_run_for_player
                best_pid = current_pid
            if best_run >= streak_len:
                return (szn, best_pid, best_run)
        return None

    # MLB-wide anchor. First check whether another player is currently on a
    # LONGER ACTIVE streak — if so, this player is behind that active leader,
    # and the honest framing anchors on the leader's *current* streak rather
    # than a completed historical one (avoids "longest since X" when X is
    # shorter/active and a longer run is right there). When this player IS the
    # active leader (nobody active is longer), fall through to the most recent
    # COMPLETED streak of qualifying length.
    mlb_phrase = ""
    mlb_year = None
    mlb_length_lopsided = False
    active_leader = _scan_active_leader()
    if active_leader:
        leader_pid, _leader_run = active_leader
        name = _player_name(conn, leader_pid)
        if name:
            mlb_phrase = f"the longest {streak_label} streak since {name}'s current streak."
            if secondary_names is not None:
                secondary_names.append(name)
    else:
        # Prefer the precomputed historical_streaks table (cross-season aware,
        # completed-only); fall back to per-season game-log scan for streak
        # types not in the table (HR streaks).
        mlb_match = _scan_historical_streaks() or _scan_seasons(None)
        if mlb_match:
            mlb_year, mlb_pid, mlb_run = mlb_match
            # If the matched prior streak is >=1.5x the current streak length,
            # the "longest X since Y's MUCH-LONGER" framing reads as deflating
            # rather than useful (e.g. 33-game streak comparing against
            # Ohtani's 53). Drop the MLB-since phrase in that case and let
            # the franchise anchor stand alone.
            mlb_length_lopsided = mlb_run >= streak_len * 1.5
            if not mlb_length_lopsided:
                name = _player_name(conn, mlb_pid)
                # Same-season anchor reads as "earlier this season", not "in 2026".
                _when = "earlier this season" if mlb_year == _cur_season else f"in {mlb_year}"
                mlb_phrase = f"the longest {streak_label} streak since {name} ({mlb_run} games) {_when}."
                if secondary_names is not None and name:
                    secondary_names.append(name)

    # Team
    team_phrase = ""
    if player_team:
        try:
            from services.franchise import get_franchise_codes, get_franchise_name
            codes = get_franchise_codes(player_team)
            franchise_name = get_franchise_name(player_team)
        except Exception:
            codes = []
            franchise_name = None
        if codes and franchise_name:
            team_match = _scan_seasons(codes)
            if team_match:
                t_year, t_pid, t_run = team_match
                t_name = _player_name(conn, t_pid)
                current_year = exclude_season
                team_gap = current_year - t_year
                mlb_gap = (current_year - mlb_year) if mlb_year else 9999
                # Fire team phrase when MLB had no match, the franchise
                # gap is meaningfully deeper than MLB, or MLB got dropped
                # for lopsided length — in which case the franchise is
                # the primary comparable anchor.
                if (
                    (mlb_year is None and team_gap >= 10)
                    or (team_gap - mlb_gap >= 10)
                    or mlb_length_lopsided
                ):
                    article = _a_or_an(franchise_name)
                    if mlb_phrase:
                        # Don't repeat "{streak_label} streak" — MLB phrase
                        # already established the noun.
                        team_phrase = (
                            f"the longest by {article} {franchise_name} player "
                            f"since {t_name}'s {t_run} in {t_year}."
                        )
                    else:
                        # Standalone (MLB empty or dropped) — needs the
                        # full phrase with label.
                        team_phrase = (
                            f"the longest {streak_label} streak by {article} {franchise_name} player "
                            f"since {t_name}'s {t_run} in {t_year}."
                        )
                    if secondary_names is not None and t_name:
                        secondary_names.append(t_name)

    if mlb_phrase and team_phrase:
        return mlb_phrase.rstrip(".") + ", and " + team_phrase
    return mlb_phrase or team_phrase


def _rarity_last_occurrence(conn, condition_sql, table="game_batting_logs",
                            exclude_season=None):
    """For rare single-game events (≤5/yr): find the last time this happened.
    Only returns context if it's been ≥1 full season since last occurrence.
    Returns (season_of_prior_match, "the first since X in Y") or (None, "").
    Legacy return-just-string callers can extract via _rarity_last_occurrence_str.
    """
    exclude_season = exclude_season or 0

    seasons = conn.execute(f"""
        SELECT DISTINCT season FROM {table}
        WHERE season < ?
        ORDER BY season DESC
    """, (exclude_season,)).fetchall()

    for (szn,) in seasons:
        row = conn.execute(f"""
            SELECT p.name
            FROM {table} g
            JOIN players p ON g.player_id = p.player_id
            WHERE g.season = ? AND ({condition_sql})
            LIMIT 1
        """, (szn,)).fetchone()

        if row:
            if szn < exclude_season - 1:
                return (szn, f"the first since {row[0]} in {szn}.")
            return (szn, "")  # Happened last season, not notable enough

    return (None, "the first in recorded history.")


def _rarity_last_occurrence_team(conn, condition_sql, table, team_codes, exclude_season):
    """Team-scoped rarity-since lookup. Find the last season any player on
    this franchise (team_codes) did the thing. Returns (season, "— and the
    first {Franchise} player to do it since X in Y") or (None, "").
    """
    if not team_codes:
        return (None, "")
    stats_table = "season_batting_stats" if "batting" in table else "season_pitching_stats"
    placeholders = ",".join("?" * len(team_codes))

    seasons = conn.execute(f"""
        SELECT DISTINCT season FROM {table}
        WHERE season < ? ORDER BY season DESC
    """, (exclude_season,)).fetchall()

    for (szn,) in seasons:
        params = [szn] + [f"%/{c}/%" for c in team_codes]
        # Subquery scopes condition_sql to game-log columns only, avoiding
        # ambiguity with identically-named columns in season_batting_stats.
        row = conn.execute(f"""
            SELECT p.name
            FROM (
                SELECT g.player_id, g.season FROM {table} g
                WHERE g.season = ? AND ({condition_sql})
            ) matches
            JOIN players p ON p.player_id = matches.player_id
            JOIN {stats_table} ss ON ss.player_id = matches.player_id AND ss.season = matches.season
            WHERE ({" OR ".join(["('/' || ss.team || '/') LIKE ?"] * len(team_codes))})
            LIMIT 1
        """, params).fetchone()
        if row:
            return (szn, row[0])
    return (None, None)


def _rarity_context_combined(conn, condition_sql, table, exclude_season,
                              player_id, min_team_gap=6):
    """Combine MLB and team rarity context for a single-game event.

    Returns a trailing-context string to append to the headline.

    - MLB-since is always shown when it meets the 1-season threshold
    - Team-since is appended only if the team match is at least
      min_team_gap years older than the MLB match (default 6 years).
    - If MLB has no history and team has one, team alone is shown.
    - Purely enrichment: the event firing decision lives upstream and
      does not use this output."""
    mlb_season, mlb_str = _rarity_last_occurrence(conn, condition_sql, table, exclude_season)

    # Look up player's current franchise codes
    team_codes = []
    try:
        row = conn.execute("SELECT team FROM players WHERE player_id = ?", (player_id,)).fetchone()
        if row and row[0]:
            # Current team could be "X/Y" for recent trades — use first code
            primary = row[0].split("/")[0].strip()
            from services.franchise import get_franchise_codes, get_franchise_name
            team_codes = get_franchise_codes(primary)
            franchise_name = get_franchise_name(primary)
    except Exception:
        franchise_name = None

    team_season, team_name = _rarity_last_occurrence_team(
        conn, condition_sql, table, team_codes, exclude_season
    )

    parts = []
    if mlb_str:
        parts.append(mlb_str.rstrip("."))

    # Append team context only if it's meaningfully deeper than MLB
    if team_season is not None and team_name and franchise_name:
        mlb_gap = (exclude_season - mlb_season) if mlb_season is not None else 9999
        team_gap = exclude_season - team_season
        if team_gap - mlb_gap >= min_team_gap:
            parts.append(f"the first {franchise_name} player to do it since {team_name} in {team_season}")

    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0] + "."
    # Combine with ", and " — comma reads less AI-flagged than em-dash
    return ", and ".join(parts) + "."


def _rarity_last_occurrence_str(conn, condition_sql, table="game_batting_logs",
                                 exclude_season=None):
    """Back-compat shim for callers that want just the string."""
    _, s = _rarity_last_occurrence(conn, condition_sql, table, exclude_season)
    return s


def _is_first_this_season(conn, condition_sql, table="game_batting_logs",
                          season=None, before_date=None):
    """For noteworthy events (6-13/yr): check if this is the first occurrence
    this season (before the given date). Returns True/False."""
    row = conn.execute(f"""
        SELECT COUNT(*) FROM {table}
        WHERE season = ? AND date < ? AND ({condition_sql})
    """, (season, before_date)).fetchone()
    return row[0] == 0 if row else True


# ---------------------------------------------------------------------------
# Tier 1: High-signal detectors
# ---------------------------------------------------------------------------

def detect_hitting_streaks(conn, season, latest_date, min_games=8):
    """Find active consecutive-game hitting streaks."""
    events = []

    # Get all players who played on the latest date
    players = conn.execute("""
        SELECT DISTINCT player_id FROM game_batting_logs
        WHERE season = ? AND date = ? AND at_bats > 0
    """, (season, latest_date)).fetchall()

    for (pid,) in players:
        # Walk backwards through game logs
        games = conn.execute("""
            SELECT date, hits, at_bats FROM game_batting_logs
            WHERE player_id = ? AND season = ? AND at_bats > 0
            ORDER BY date DESC
        """, (pid, season)).fetchall()

        streak = 0
        for game_date, hits, ab in games:
            if hits > 0:
                streak += 1
            else:
                break

        if streak >= min_games:
            name = _player_name(conn, pid)
            team = _player_team_display(conn, pid, season)
            team_code = _player_team_code(conn, pid, season)
            game_line, _ = _get_game_line(conn, pid, latest_date, season)
            secondary: list = []
            context = _historical_context(
                conn, streak, "hits > 0",
                exclude_season=season, exclude_player=pid,
                player_team=team_code,
                streak_label="hitting",
                current_date=latest_date,
                secondary_names=secondary,
            )
            # Participle-absolute join — the streak extension is a
            # consequence of the box-score line, not a separate beat. The
            # period-split form ("...went 2-for-4. He extended his streak")
            # reads as two facts when it's really cause→consequence.
            # Sportswriter style: "Judge homered in the 9th, lifting the
            # Yankees to a 3-2 win." The participle modifies the past-tense
            # verb's time frame and reads as past consequence.
            if game_line:
                headline = f"{name} went {game_line}, extending his hitting streak to {streak} straight games"
            else:
                headline = f"{name} extended his hitting streak to {streak} straight games"
            if context:
                headline = _continue_with_context(headline, context)
            else:
                headline += "."
            events.append({
                "headline": headline,
                "detail": "",
                "category": "Streak",
                "game_date": latest_date,
                "player_names": [name] + secondary,
                "team_names": [team] if team else [],
                "detection_type": "hitting_streak",
                "priority": 1,
                "_streak_len": streak,
            })

    # Sort by streak length (longest first)
    events.sort(key=lambda e: e.get("_streak_len", 0), reverse=True)
    for e in events:
        e.pop("_streak_len", None)
    return events


def detect_onbase_streaks(conn, season, latest_date, min_games=12):
    """Find active consecutive-game on-base streaks."""
    events = []

    players = conn.execute("""
        SELECT DISTINCT player_id FROM game_batting_logs
        WHERE season = ? AND date = ? AND (at_bats > 0 OR walks > 0 OR hit_by_pitch > 0)
    """, (season, latest_date)).fetchall()

    for (pid,) in players:
        games = conn.execute("""
            SELECT date, hits, walks, COALESCE(hit_by_pitch, 0) as hbp
            FROM game_batting_logs
            WHERE player_id = ? AND season = ?
                AND (at_bats > 0 OR walks > 0 OR COALESCE(hit_by_pitch, 0) > 0)
            ORDER BY date DESC
        """, (pid, season)).fetchall()

        streak = 0
        for game_date, hits, walks, hbp in games:
            if (hits + walks + hbp) > 0:
                streak += 1
            else:
                break

        if streak >= min_games:
            name = _player_name(conn, pid)
            team = _player_team_display(conn, pid, season)
            team_code = _player_team_code(conn, pid, season)
            game_line, _ = _get_game_line(conn, pid, latest_date, season)
            secondary: list = []
            context = _historical_context(
                conn, streak,
                "(hits + walks + COALESCE(hit_by_pitch, 0)) > 0",
                exclude_season=season, exclude_player=pid,
                at_bat_filter="(at_bats > 0 OR walks > 0 OR COALESCE(hit_by_pitch, 0) > 0)",
                player_team=team_code,
                streak_label="on-base",
                current_date=latest_date,
                secondary_names=secondary,
            )
            # Participle-absolute join — same rationale as hitting_streak.
            if game_line:
                headline = f"{name} went {game_line}, extending his on-base streak to {streak} straight games"
            else:
                headline = f"{name} extended his on-base streak to {streak} straight games"
            if context:
                headline = _continue_with_context(headline, context)
            else:
                headline += "."
            events.append({
                "headline": headline,
                "detail": "",
                "category": "Streak",
                "game_date": latest_date,
                "player_names": [name] + secondary,
                "team_names": [],
                "detection_type": "onbase_streak",
                "priority": 1,
                "_streak_len": streak,
            })

    events.sort(key=lambda e: e.get("_streak_len", 0), reverse=True)
    for e in events:
        e.pop("_streak_len", None)
    return events


def detect_hr_streaks(conn, season, latest_date, min_games=4):
    """Find active consecutive-game HR streaks."""
    events = []

    players = conn.execute("""
        SELECT DISTINCT player_id FROM game_batting_logs
        WHERE season = ? AND date = ? AND at_bats > 0
    """, (season, latest_date)).fetchall()

    for (pid,) in players:
        games = conn.execute("""
            SELECT date, home_runs FROM game_batting_logs
            WHERE player_id = ? AND season = ? AND at_bats > 0
            ORDER BY date DESC
        """, (pid, season)).fetchall()

        streak = 0
        for game_date, hr in games:
            if hr and hr > 0:
                streak += 1
            else:
                break

        if streak >= min_games:
            name = _player_name(conn, pid)
            team = _player_team_display(conn, pid, season)
            # Get today's HR count and season total
            today_hr = conn.execute("""
                SELECT home_runs FROM game_batting_logs
                WHERE player_id = ? AND date = ? AND season = ?
            """, (pid, latest_date, season)).fetchone()
            season_hr = conn.execute("""
                SELECT home_runs FROM season_batting_stats
                WHERE player_id = ? AND season = ?
            """, (pid, season)).fetchone()
            hr_today = today_hr[0] if today_hr else 1
            hr_total = season_hr[0] if season_hr else 0
            team_code = _player_team_code(conn, pid, season)
            game_line, _ = _get_game_line(conn, pid, latest_date, season)
            secondary: list = []
            context = _historical_context(
                conn, streak, "home_runs > 0",
                exclude_season=season, exclude_player=pid,
                player_team=team_code,
                streak_label="HR",
                current_date=latest_date,
                secondary_names=secondary,
            )
            # Build intro with HR number
            ordinal = f"his {_ordinal(hr_total)}" if hr_total else "a"
            intro = f"{name} homered ({ordinal}) and has now gone deep in {streak} straight games"
            if context:
                intro = _continue_with_context(intro, context)
            else:
                intro += "."
            headline = intro
            events.append({
                "headline": headline,
                "detail": "",
                "category": "Streak",
                "game_date": latest_date,
                "player_names": [name] + secondary,
                "team_names": [],
                "detection_type": "hr_streak",
                "priority": 1,
            })

    return events


def detect_streak_endings(conn, season, latest_date):
    """Find significant streaks that ENDED today (player played + failed
    to extend a streak that was at threshold coming in).

    Thresholds (only the most marquee streaks — anything below is too
    routine for an end-event):
      - hitting streak: 30+
      - on-base streak: 45+
      - HR streak: 5+

    For each ending event we count back from yesterday to compute the
    final streak length, surface the player who broke it, and note the
    historical context phrase so the feed has the same comp framing as
    the active-streak events.
    """
    events = []

    # All players who played on latest_date
    players = conn.execute("""
        SELECT DISTINCT player_id FROM game_batting_logs
        WHERE season = ? AND date = ? AND at_bats > 0
    """, (season, latest_date)).fetchall()

    for (pid,) in players:
        # Today's stats — used to detect "did not extend"
        today_row = conn.execute("""
            SELECT hits, COALESCE(walks, 0), COALESCE(hit_by_pitch, 0),
                   COALESCE(home_runs, 0), at_bats
            FROM game_batting_logs
            WHERE player_id = ? AND season = ? AND date = ?
        """, (pid, season, latest_date)).fetchone()
        if not today_row:
            continue
        t_hits, t_walks, t_hbp, t_hr, t_ab = today_row

        # Walk back from BEFORE today to compute prior streaks. Pull all
        # qualifying games once, filter by date in Python.
        prior_bat = conn.execute("""
            SELECT date, hits, COALESCE(walks, 0), COALESCE(hit_by_pitch, 0),
                   COALESCE(home_runs, 0), at_bats
            FROM game_batting_logs
            WHERE player_id = ? AND season = ? AND date < ?
            ORDER BY date DESC
        """, (pid, season, latest_date)).fetchall()

        # 1) Hitting streak that ended today.
        # Today's player must have had at_bats (so they had a shot to extend)
        # AND today's hits == 0.
        if t_ab > 0 and t_hits == 0:
            streak = 0
            for (_d, h, _bb, _hbp, _hr, ab) in prior_bat:
                if ab <= 0:
                    continue  # didn't bat — skip without breaking streak
                if h > 0:
                    streak += 1
                else:
                    break
            if streak >= 30:
                _append_streak_end_event(
                    conn, events, pid, season, latest_date,
                    streak=streak, label="hitting streak", condition_sql="hits > 0",
                    detection_type="hitting_streak_ended",
                )

        # 2) On-base streak that ended today.
        # Today's player must have had a chance to reach base (PA > 0) AND
        # today's H + BB + HBP == 0. Approximate "PA > 0" via at_bats > 0
        # OR walks > 0 OR hbp > 0 — same shape as detect_onbase_streaks.
        had_pa_today = (t_ab > 0) or (t_walks > 0) or (t_hbp > 0)
        if had_pa_today and (t_hits + t_walks + t_hbp) == 0:
            streak = 0
            for (_d, h, bb, hbp, _hr, ab) in prior_bat:
                had_pa = (ab > 0) or (bb > 0) or (hbp > 0)
                if not had_pa:
                    continue
                if (h + bb + hbp) > 0:
                    streak += 1
                else:
                    break
            if streak >= 45:
                _append_streak_end_event(
                    conn, events, pid, season, latest_date,
                    streak=streak, label="on-base streak",
                    condition_sql="(hits + walks + COALESCE(hit_by_pitch, 0)) > 0",
                    detection_type="onbase_streak_ended",
                    at_bat_filter="(at_bats > 0 OR walks > 0 OR COALESCE(hit_by_pitch, 0) > 0)",
                )

        # 3) HR streak that ended today.
        # Player batted (so they had a shot) and didn't homer.
        if t_ab > 0 and t_hr == 0:
            streak = 0
            for (_d, _h, _bb, _hbp, hr, ab) in prior_bat:
                if ab <= 0:
                    continue
                if hr > 0:
                    streak += 1
                else:
                    break
            if streak >= 5:
                _append_streak_end_event(
                    conn, events, pid, season, latest_date,
                    streak=streak, label="HR streak", condition_sql="home_runs > 0",
                    detection_type="hr_streak_ended",
                )

    return events


def _append_streak_end_event(conn, events, pid, season, latest_date, *,
                             streak, label, condition_sql, detection_type,
                             at_bat_filter="at_bats > 0"):
    """Build one streak-end event and append to events list."""
    name = _player_name(conn, pid)
    team = _player_team_display(conn, pid, season)
    team_code = _player_team_code(conn, pid, season)
    game_line, _ = _get_game_line(conn, pid, latest_date, season)

    # Same historical-context machinery as the active-streak detectors.
    # The "longest since X" framing reads cleanly past-tense for an
    # ended streak: "...ending his 25-game on-base streak. That's the
    # longest on-base streak since Y in Z."
    # Derive streak_label by stripping " streak" from `label`
    # ("hitting streak" → "hitting").
    streak_label = label.replace(" streak", "").strip() or "hitting"
    secondary: list = []
    context = _historical_context(
        conn, streak, condition_sql,
        exclude_season=season, exclude_player=pid,
        at_bat_filter=at_bat_filter,
        player_team=team_code,
        streak_label=streak_label,
        current_date=latest_date,
        secondary_names=secondary,
    )

    intro = f"{name} went {game_line}" if game_line else name
    headline = f"{intro}, ending his {streak}-game {label}"
    if context:
        headline = _continue_with_context(headline, context)
    else:
        headline += "."

    events.append({
        "headline": headline,
        "detail": "",
        "category": "Streak",
        "game_date": latest_date,
        "player_names": [name] + secondary,
        "team_names": [team] if team else [],
        "detection_type": detection_type,
        "priority": 1,
    })


def _pitching_streak_context(conn, pid, season, streak_len, condition_sql):
    """Compute MLB + team "first since X" context for a pitching-shape streak.

    streak_len: current active streak length (N consecutive starts matching).
    condition_sql: per-start SQL fragment like 'earned_runs = 0 AND ip_outs >= 15'.

    Returns a trailing-context string or empty. Follows same rules as
    _rarity_context_combined: MLB shown when gap is 2+ years, team
    appended when team gap is 6+ years deeper than MLB. The data decides
    what's rare — we don't pre-filter by streak length. Common streaks
    hit matches in the immediately-prior season and return fast.
    """
    from services.historical_scans import _find_last_consecutive_start_streak
    # MLB
    mlb = _find_last_consecutive_start_streak(conn, pid, season, streak_len, condition_sql)
    # Team
    team_codes = []
    franchise_name = None
    try:
        row = conn.execute("SELECT team FROM players WHERE player_id = ?", (pid,)).fetchone()
        if row and row[0]:
            primary = row[0].split("/")[0].strip()
            from services.franchise import get_franchise_codes, get_franchise_name
            team_codes = get_franchise_codes(primary)
            franchise_name = get_franchise_name(primary)
    except Exception:
        pass
    team_match = (
        _find_last_consecutive_start_streak(conn, pid, season, streak_len, condition_sql, team_codes=team_codes)
        if team_codes else None
    )

    parts = []
    if mlb:
        gap_mlb = season - mlb["season"]
        if gap_mlb >= 2:
            parts.append(f"the first since {mlb['name']} in {mlb['season']}")
    if team_match and franchise_name:
        gap_team = season - team_match["season"]
        gap_mlb = (season - mlb["season"]) if mlb else 9999
        if gap_team - gap_mlb >= 6:
            parts.append(f"the first {franchise_name} pitcher to do it since {team_match['name']} in {team_match['season']}")
    elif not mlb and franchise_name:
        # No MLB match found at all within lookback — just fire team if it has one
        if team_match:
            parts.append(f"the first {franchise_name} pitcher to do it since {team_match['name']} in {team_match['season']}")

    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0] + "."
    return ", and ".join(parts) + "."


def detect_pitching_streaks(conn, season, latest_date):
    """Find active pitching dominance streaks (scoreless starts, quality starts,
    consecutive 10+ K starts). Adds MLB + team-since historical context when
    the streak is long enough to be noteworthy."""
    events = []

    # Get starters who pitched on or near the latest date
    starters = conn.execute("""
        SELECT DISTINCT player_id FROM game_pitching_logs
        WHERE season = ? AND is_start = 1
    """, (season,)).fetchall()

    for (pid,) in starters:
        starts = conn.execute("""
            SELECT date, ip_outs, earned_runs, innings_pitched, strikeouts
            FROM game_pitching_logs
            WHERE player_id = ? AND season = ? AND is_start = 1
            ORDER BY date DESC
        """, (pid, season)).fetchall()

        if not starts:
            continue

        # A scoreless start is essentially a stronger quality start, so a
        # scoreless-start streak makes the parallel quality-start streak a weaker
        # restatement of overlapping games. When both fire for one pitcher, keep
        # the scoreless streak (it carries the historical context) and skip QS.
        scoreless_fired = False

        # Scoreless starts streak (0 ER, 5+ IP)
        scoreless = 0
        for game_date, ip_outs, er, ip_text, so in starts:
            ip = (ip_outs or 0) / 3.0
            if (er is not None and er == 0) and ip >= 5.0:
                scoreless += 1
            else:
                break

        if scoreless >= 2:
            name = _player_name(conn, pid)
            last = starts[0]
            last_ip = fmt_ip(last[1])
            last_so = last[4] or 0
            game_line = f"{name} threw {last_ip} scoreless IP with {last_so} K and "
            headline = f"{game_line}now has {scoreless} consecutive scoreless starts."
            # Historical context only when the streak is rare enough (5+)
            ctx = _pitching_streak_context(conn, pid, season, scoreless,
                                           "earned_runs = 0 AND ip_outs >= 15")
            if ctx:
                headline = _continue_with_context(headline, ctx)
            events.append({
                "headline": headline,
                "detail": "",
                "category": "Streak",
                "game_date": starts[0][0],
                "player_names": [name],
                "team_names": [],
                "detection_type": "scoreless_streak",
                "priority": 1,
            })
            scoreless_fired = True

        # Quality start streak (6+ IP, 3 or fewer ER)
        qs = 0
        for game_date, ip_outs, er, ip_text, so in starts:
            ip = (ip_outs or 0) / 3.0
            if ip >= 6.0 and (er is not None and er <= 3):
                qs += 1
            else:
                break

        if qs >= 4 and not scoreless_fired:
            name = _player_name(conn, pid)
            last = starts[0]
            last_ip = fmt_ip(last[1])
            last_er = last[2] or 0
            last_so = last[4] or 0
            game_line = f"{name} went {last_ip} IP, {last_er} ER, {last_so} K and "
            headline = f"{game_line}now has {qs} consecutive quality starts (6+ IP, ≤3 ER)."
            ctx = _pitching_streak_context(conn, pid, season, qs,
                                           "ip_outs >= 18 AND earned_runs <= 3")
            if ctx:
                headline = _continue_with_context(headline, ctx)
            events.append({
                "headline": headline,
                "detail": "",
                "category": "Streak",
                "game_date": starts[0][0],
                "player_names": [name],
                "team_names": [],
                "detection_type": "qs_streak",
                "priority": 1,
            })

        # 10+ K consecutive starts (new shape — stat-twitter-y)
        k10 = 0
        for game_date, ip_outs, er, ip_text, so in starts:
            if (so or 0) >= 10:
                k10 += 1
            else:
                break

        if k10 >= 3:
            name = _player_name(conn, pid)
            last = starts[0]
            last_ip = fmt_ip(last[1])
            last_so = last[4] or 0
            game_line = f"{name} struck out {last_so} in {last_ip} innings and "
            headline = f"{game_line}now has {k10} consecutive 10+ strikeout starts."
            ctx = _pitching_streak_context(conn, pid, season, k10,
                                           "strikeouts >= 10")
            if ctx:
                headline = _continue_with_context(headline, ctx)
            events.append({
                "headline": headline,
                "detail": "",
                "category": "Streak",
                "game_date": starts[0][0],
                "player_names": [name],
                "team_names": [],
                "detection_type": "k10_streak",
                "priority": 1,
            })

    return events


def detect_season_pace(conn, season, latest_date=None):
    """Fire once per player per threshold when their pace first crosses it.

    Only HR (50/60/70/80) and SB (50/60/70/80). Only after 2 weeks (~14 games).
    Checks notable_events to see if we've already reported this player+threshold.
    """
    events = []
    min_games = 14  # ~2 weeks

    pace_checks = [
        ("home_runs", "HR", [80, 70, 60, 50]),
        ("stolen_bases", "SB", [80, 70, 60, 50]),
    ]

    for stat_col, abbrev, thresholds in pace_checks:
        rows = conn.execute(f"""
            SELECT p.name, s.{stat_col}, s.games, p.team
            FROM season_batting_stats s
            JOIN players p ON s.player_id = p.player_id
            WHERE s.season = ? AND s.games >= ?
            AND s.{stat_col} > 0
            ORDER BY s.{stat_col} DESC
            LIMIT 20
        """, (season, min_games)).fetchall()

        for name, stat_val, games, team_code in rows:
            if not stat_val or games == 0:
                continue

            # Must have contributed to this stat on latest_date
            contributed = conn.execute(f"""
                SELECT {stat_col} FROM game_batting_logs g
                JOIN players p ON g.player_id = p.player_id
                WHERE p.name = ? AND g.date = ? AND g.{stat_col} > 0
            """, (name, latest_date)).fetchone()
            if not contributed:
                continue

            pace = int(stat_val * 162.0 / games)

            for threshold in thresholds:
                if pace >= threshold:
                    # Check if we already fired this player+threshold this season
                    already = conn.execute("""
                        SELECT 1 FROM notable_events
                        WHERE headline LIKE ? AND detection_type = ?
                        AND game_date >= ? LIMIT 1
                    """, (f"%{name}%", f"pace_{stat_col}_{threshold}",
                          f"{season}-01-01")).fetchone()

                    if already:
                        break  # Already reported this threshold, skip lower ones too

                    team = team_display(team_code) if team_code else ""
                    # Get last game line
                    game_row = conn.execute("""
                        SELECT hits, at_bats, home_runs, rbi, stolen_bases
                        FROM game_batting_logs g
                        JOIN players p ON g.player_id = p.player_id
                        WHERE p.name = ? AND g.date = ?
                    """, (name, latest_date)).fetchone()
                    game_intro = ""
                    if game_row:
                        gh, gab, ghr, grbi, gsb = [x or 0 for x in game_row]
                        parts = [f"{gh}-for-{gab}"]
                        if ghr: parts.append(f"{'a homer' if ghr == 1 else f'{ghr} homers'}")
                        if gsb: parts.append(f"{'a stolen base' if gsb == 1 else f'{gsb} steals'}")
                        if grbi: parts.append(f"{grbi} RBI")
                        game_intro = f"{name} went {parts[0]}"
                        if len(parts) > 1:
                            game_intro += f" with {', '.join(parts[1:])}"
                        game_intro += ". "
                    else:
                        game_intro = ""
                    projected = int(stat_val * 162 / games)
                    # Include stat name + "now" in the pace phrase so it
                    # reads as a temporal update ("he is now on pace for
                    # 54 HR this season") and survives the strip when
                    # merged with another event without losing the stat.
                    if game_intro:
                        pace_line = f"{game_intro}He now has {stat_val} {abbrev} and is on pace for {projected} {abbrev} this season."
                    else:
                        pace_line = f"{name} now has {stat_val} {abbrev} and is on pace for {projected} {abbrev} this season."
                    events.append({
                        "headline": pace_line,
                        "detail": "",
                        "category": "Milestone",
                        "game_date": latest_date,
                        "player_names": [name],
                        "team_names": [team] if team else [],
                        "detection_type": f"pace_{stat_col}_{threshold}",
                        "priority": 2,
                    })
                    break  # Only report highest new threshold

    return events


# ---------------------------------------------------------------------------
# Tier 2: Medium-signal detectors
# ---------------------------------------------------------------------------

# Iconic career milestone thresholds — these get the "last since X" anchor.
# Lower thresholds happen too often (1000 hits every year) to be worth it.
_ICONIC_CAREER_THRESHOLDS = {
    "home_runs": {500, 600, 700},
    "hits": {3000},
    "rbi": {2000, 2500, 3000},
    "stolen_bases": {500, 600},
    "strikeouts": {3000, 3500, 4000},  # pitching
    "wins": {300, 350},                 # pitching
    "saves": {400, 500},                # pitching
}


def _last_player_to_cross_career_threshold(conn, stat, threshold, exclude_player_id, is_pitching=False):
    """Find the most recent player to cross this career threshold, plus the
    total number of players who ever have. Returns (rank, name, year) or None."""
    table = "season_pitching_stats" if is_pitching else "season_batting_stats"
    rows = conn.execute(f"""
        SELECT p.name, x.season
        FROM (
            SELECT s.player_id, s.season,
                   SUM(s.{stat}) OVER (
                     PARTITION BY s.player_id ORDER BY s.season
                     ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                   ) AS cumul,
                   COALESCE(
                     SUM(s.{stat}) OVER (
                       PARTITION BY s.player_id ORDER BY s.season
                       ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
                     ), 0
                   ) AS prev_cumul
            FROM {table} s
        ) x
        JOIN players p ON p.player_id = x.player_id
        WHERE x.cumul >= ? AND x.prev_cumul < ? AND x.player_id != ?
        ORDER BY x.season DESC
    """, (threshold, threshold, exclude_player_id)).fetchall()
    if not rows:
        return None
    # Total players who ever crossed = len(rows) + 1 (plus this player)
    rank = len(rows) + 1
    last_name, last_year = rows[0]
    return (rank, last_name, last_year)


def detect_career_milestones(conn, season, latest_date):
    """Find players approaching career milestone numbers.

    Only triggers when the player contributed to the milestone stat in
    their most recent game (e.g., hit a HR → show HR milestone proximity).
    Capped at 5 away from milestone, except for the lower-tier milestones
    in `_LOW_MILESTONE_TIGHT_THRESHOLDS` which fire at 1-away only — at
    that level, "5 away" gets noisy because plenty of players approach
    100 HR / 100 W in a season without it being headline-worthy.
    """
    events = []

    # Lower-tier milestones where the 5-away "approaching" event reads as
    # noise rather than newsworthy. Only fire when the player is exactly
    # 1 away (or has just crossed). Keys are (col, milestone) tuples.
    _LOW_MILESTONE_TIGHT_THRESHOLDS = {
        ("home_runs", 100), ("home_runs", 200),
        ("wins", 100), ("wins", 150),
    }

    # Batting milestones: (career_col, game_log_col, milestones, label, action_template)
    # action_template: lambda(game_value) -> "hit 2 home runs"
    bat_milestones = [
        ("home_runs", "home_runs", [600, 500, 400, 300, 200, 100], "career home runs",
         lambda v: f"hit {v} home run{'s' if v != 1 else ''}"),
        ("hits", "hits", [3000, 2500, 2000, 1500, 1000], "career hits",
         lambda v: f"went {v}-for with {v} hit{'s' if v != 1 else ''}"),
        ("rbi", "rbi", [1500, 1000, 500], "career RBI",
         lambda v: f"drove in {v} run{'s' if v != 1 else ''}"),
    ]

    for col, game_col, milestones, label, action_fn in bat_milestones:
        # Find players who contributed to this stat in their most recent game
        contributors = conn.execute(f"""
            SELECT g.player_id, g.date, g.{game_col}
            FROM game_batting_logs g
            INNER JOIN (
                SELECT player_id, MAX(date) as max_date
                FROM game_batting_logs WHERE season = ?
                GROUP BY player_id
            ) latest ON g.player_id = latest.player_id AND g.date = latest.max_date
            WHERE g.season = ? AND g.{game_col} > 0
        """, (season, season)).fetchall()
        contributor_info = {r[0]: (r[1], r[2]) for r in contributors}  # pid -> (date, stat_val)

        if not contributor_info:
            continue

        # Career totals for those players
        placeholders = ",".join("?" * len(contributor_info))
        rows = conn.execute(f"""
            SELECT s.player_id, p.name, SUM(s.{col}) as career_total
            FROM season_batting_stats s
            JOIN players p ON s.player_id = p.player_id
            WHERE s.player_id IN ({placeholders})
            GROUP BY s.player_id
            ORDER BY career_total DESC
        """, list(contributor_info.keys())).fetchall()

        found = 0
        for pid, name, total in rows:
            if not total or found >= 3:
                break
            game_date, stat_val = contributor_info[pid]
            for m in milestones:
                remaining = m - total
                # Lower-tier milestones (100/200 HR, 100/150 W) only fire at
                # 1-away to avoid noise; everything else uses the standard 5.
                approach_max = 1 if (col, m) in _LOW_MILESTONE_TIGHT_THRESHOLDS else 5
                if 1 <= remaining <= approach_max:
                    # Approaching milestone
                    action = action_fn(stat_val)
                    if col == "hits":
                        game_row = conn.execute("""
                            SELECT hits, at_bats FROM game_batting_logs
                            WHERE player_id = ? AND date = ? AND season = ?
                        """, (pid, game_date, season)).fetchone()
                        if game_row:
                            action = f"collected {game_row[0]} hit{'s' if game_row[0] != 1 else ''}"
                    events.append({
                        "headline": f"{name} {action}, and is now {remaining} away from {m} {label}.",
                        "detail": "",
                        "category": "Milestone",
                        "game_date": game_date,
                        "player_names": [name],
                        "team_names": [],
                        "detection_type": f"career_{col}_{m}",
                        "priority": 2,
                    })
                    found += 1
                elif remaining <= 0 and remaining > -stat_val:
                    # Just crossed milestone
                    action = action_fn(stat_val)
                    if col == "hits":
                        game_row = conn.execute("""
                            SELECT hits, at_bats FROM game_batting_logs
                            WHERE player_id = ? AND date = ? AND season = ?
                        """, (pid, game_date, season)).fetchone()
                        if game_row:
                            action = f"collected {game_row[0]} hit{'s' if game_row[0] != 1 else ''}"
                    headline = f"{name} {action}, reaching {m:,} {label}!"
                    # Iconic-threshold anchor: "Nth player ever; last since X in YEAR"
                    iconic_secondary = []
                    if m in _ICONIC_CAREER_THRESHOLDS.get(col, set()):
                        ctx = _last_player_to_cross_career_threshold(
                            conn, col, m, pid, is_pitching=False
                        )
                        if ctx:
                            rank, last_name, last_year = ctx
                            iconic_secondary.append(last_name)
                            headline = _continue_with_context(
                                headline,
                                f"the {_ordinal(rank)} player ever to reach {m:,} "
                                f"{label.replace('career ', '')}, and the first since "
                                f"{last_name} in {last_year}",
                            )
                    events.append({
                        "headline": headline,
                        "detail": "",
                        "category": "Milestone",
                        "game_date": game_date,
                        "player_names": [name] + iconic_secondary,
                        "team_names": [],
                        "detection_type": f"career_{col}_{m}",
                        "priority": 1,
                    })
                    found += 1
                    break

    # Pitching milestones: (career_col, game_log_col, milestones, label, action_template)
    pitch_milestones = [
        ("strikeouts", "strikeouts", [3000, 2500, 2000, 1500, 1000], "career strikeouts",
         lambda v: f"struck out {v}"),
        ("wins", "win", [200, 150, 100], "career wins",
         lambda v: "picked up a win"),
        ("saves", "save", [400, 300, 200], "career saves",
         lambda v: "recorded a save"),
    ]

    for col, game_col, milestones, label, action_fn in pitch_milestones:
        contributors = conn.execute(f"""
            SELECT g.player_id, g.date, g.{game_col}
            FROM game_pitching_logs g
            INNER JOIN (
                SELECT player_id, MAX(date) as max_date
                FROM game_pitching_logs WHERE season = ?
                GROUP BY player_id
            ) latest ON g.player_id = latest.player_id AND g.date = latest.max_date
            WHERE g.season = ? AND g.{game_col} > 0
        """, (season, season)).fetchall()
        contributor_info = {r[0]: (r[1], r[2]) for r in contributors}

        if not contributor_info:
            continue

        placeholders = ",".join("?" * len(contributor_info))
        rows = conn.execute(f"""
            SELECT s.player_id, p.name, SUM(s.{col}) as career_total
            FROM season_pitching_stats s
            JOIN players p ON s.player_id = p.player_id
            WHERE s.player_id IN ({placeholders})
            GROUP BY s.player_id
            ORDER BY career_total DESC
        """, list(contributor_info.keys())).fetchall()

        found = 0
        for pid, name, total in rows:
            if not total or found >= 3:
                break
            game_date, stat_val = contributor_info[pid]
            for m in milestones:
                remaining = m - total
                # Same lower-tier tightening: 100 W and 150 W only fire at
                # 1-away. (The set is shared with batting; pitching wins
                # share the wins->100/150 entries.)
                approach_max = 1 if (col, m) in _LOW_MILESTONE_TIGHT_THRESHOLDS else 5
                if 1 <= remaining <= approach_max:
                    # Approaching milestone
                    action = action_fn(stat_val)
                    events.append({
                        "headline": f"{name} {action}, and is now {remaining} away from {m} {label}.",
                        "detail": "",
                        "category": "Milestone",
                        "game_date": game_date,
                        "player_names": [name],
                        "team_names": [],
                        "detection_type": f"career_p_{col}_{m}",
                        "priority": 2,
                    })
                    found += 1
                    break
                elif remaining <= 0 and remaining > -stat_val:
                    # Just crossed milestone (total passed m, and today's contribution pushed them over)
                    action = action_fn(stat_val)
                    headline = f"{name} {action}, reaching {m:,} {label}!"
                    iconic_secondary = []
                    if m in _ICONIC_CAREER_THRESHOLDS.get(col, set()):
                        ctx = _last_player_to_cross_career_threshold(
                            conn, col, m, pid, is_pitching=True
                        )
                        if ctx:
                            rank, last_name, last_year = ctx
                            iconic_secondary.append(last_name)
                            headline = _continue_with_context(
                                headline,
                                f"the {_ordinal(rank)} pitcher ever to reach {m:,} "
                                f"{label.replace('career ', '')}, and the first since "
                                f"{last_name} in {last_year}",
                            )
                    events.append({
                        "headline": headline,
                        "detail": "",
                        "category": "Milestone",
                        "game_date": game_date,
                        "player_names": [name] + iconic_secondary,
                        "team_names": [],
                        "detection_type": f"career_p_{col}_{m}",
                        "priority": 1,
                    })
                    found += 1
                    break

    return events


def detect_rarities(conn, season, latest_date):
    """Detect rare and noteworthy single-game performances.

    Two tiers, frequency-validated from 2016-2025 data:

    RARITY (≤5/yr) — always show, with "first since [player] in [year]":
      Cycle (~4.5), 4+ HR (~1.5), 6+ hits (~2), 5+ hits w/ triple (~2),
      8+ RBI (~3), 5+ hits w/ 2+ HR (~3.4), 4+ hits w/ 3+ HR (~5.5),
      No-hitter (~2.4), 1-hitter (~5), 15+ K (~2.8)

    NOTEWORTHY (6-13/yr) — show with "first this season" if applicable:
      2+ triples (~8.5), 14+ K (~9), 3 HR + 5+ RBI (~9.4),
      2-hitter 9IP (~10), 5+ hits w/ HR (~10.5), 7+ RBI (~11),
      3 HR game (~13), Complete game (~25, included by design)
    """
    events = []
    seen = set()  # (player_id, date) dedup

    # --- Helper to process a batch of checks ---
    def _check_batting(checks, category, use_first_since=False, use_first_this_season=False):
        for check in checks:
            rows = conn.execute(f"""
                SELECT p.name, g.player_id, g.date, g.hits, g.home_runs, g.rbi,
                       g.at_bats, g.doubles, g.triples
                FROM game_batting_logs g
                JOIN players p ON g.player_id = p.player_id
                WHERE g.season = ? AND g.date >= (
                    SELECT DISTINCT date FROM game_batting_logs WHERE season = ?
                    ORDER BY date DESC LIMIT 1 OFFSET 1
                )
                AND ({check['condition']})
            """, (season, season)).fetchall()

            for name, pid, game_date, h, hr, rbi, ab, doubles, triples in rows:
                key = (pid, game_date)
                if key in seen:
                    continue
                seen.add(key)

                r = {"h": h or 0, "hr": hr or 0, "rbi": rbi or 0, "ab": ab or 0}
                headline = check["headline"](name, r)

                if use_first_since:
                    context = _rarity_context_combined(
                        conn, check["history_sql"], "game_batting_logs",
                        exclude_season=season, player_id=pid,
                    )
                    if context:
                        headline = _continue_with_context(headline, context)
                elif use_first_this_season:
                    if _is_first_this_season(conn, check["history_sql"],
                                             "game_batting_logs", season, game_date):
                        # Disambiguate the "first this season" subject so users
                        # don't read it as antecedent-ambiguous. "first 3-HR,
                        # 5+ RBI game this season" beats "first this season."
                        label = check.get("label")
                        # "Leaguewide" disambiguates scope. _is_first_this_season
                        # queries the whole table (no player_id filter), so this
                        # is genuinely the first occurrence by any player this
                        # season — not the player's first.
                        suffix = (
                            f"the first {label} leaguewide this season"
                            if label else "the first this season leaguewide"
                        )
                        headline = _continue_with_context(headline, suffix)

                events.append({
                    "headline": headline, "detail": "",
                    "category": category, "game_date": game_date,
                    "player_names": [name], "team_names": [],
                    "detection_type": check["type"],
                    "priority": 1 if category == "Rarity" else 2,
                })

    # ---- RARITY TIER (≤5/yr) — batting ----
    _check_batting([
        {
            "condition": "g.doubles >= 1 AND g.triples >= 1 AND g.home_runs >= 1 AND g.hits >= 4",
            "type": "cycle", "history_sql": "doubles >= 1 AND triples >= 1 AND home_runs >= 1 AND hits >= 4",
            "headline": lambda n, r: f"{n} hit for the cycle, going {r['h']}-for-{r['ab']} with {r['rbi']} RBI.",
        },
        {
            "condition": "g.home_runs >= 4",
            "type": "4hr_game", "history_sql": "home_runs >= 4",
            "headline": lambda n, r: f"{n} hit {r['hr']} home runs, going {r['h']}-for-{r['ab']} with {r['rbi']} RBI.",
        },
        {
            "condition": "g.hits >= 6",
            "type": "6hit_game", "history_sql": "hits >= 6",
            "headline": lambda n, r: f"{n} collected {r['h']} hits, going {r['h']}-for-{r['ab']} with {r['hr']} HR and {r['rbi']} RBI.",
        },
        {
            "condition": "g.hits >= 5 AND g.triples >= 1",
            "type": "5hit_3b_game", "history_sql": "hits >= 5 AND triples >= 1",
            "headline": lambda n, r: f"{n} went {r['h']}-for-{r['ab']} including a triple, with {r['hr']} HR and {r['rbi']} RBI.",
        },
        {
            "condition": "g.rbi >= 8",
            "type": "8rbi_game", "history_sql": "rbi >= 8",
            "headline": lambda n, r: f"{n} drove in {r['rbi']} runs, going {r['h']}-for-{r['ab']} with {r['hr']} home runs.",
        },
        {
            "condition": "g.hits >= 5 AND g.home_runs >= 2",
            "type": "5hit_2hr_game", "history_sql": "hits >= 5 AND home_runs >= 2",
            "headline": lambda n, r: f"{n} went {r['h']}-for-{r['ab']} with {r['hr']} home runs and {r['rbi']} RBI.",
        },
        {
            "condition": "g.hits >= 4 AND g.home_runs >= 3",
            "type": "4hit_3hr_game", "history_sql": "hits >= 4 AND home_runs >= 3",
            "headline": lambda n, r: f"{n} hit {r['hr']} home runs, going {r['h']}-for-{r['ab']} with {r['rbi']} RBI.",
        },
    ], category="Rarity", use_first_since=True)

    # ---- NOTEWORTHY TIER (6-13/yr) — batting ----
    _check_batting([
        {
            "condition": "g.triples >= 2",
            "type": "2_triples", "history_sql": "triples >= 2",
            "label": "2-triple game",
            "headline": lambda n, r: f"{n} hit 2 triples, going {r['h']}-for-{r['ab']}.",
        },
        {
            "condition": "g.home_runs >= 3 AND g.rbi >= 5",
            "type": "3hr_5rbi", "history_sql": "home_runs >= 3 AND rbi >= 5",
            "label": "3-HR, 5+ RBI game",
            "headline": lambda n, r: f"{n} hit {r['hr']} home runs and drove in {r['rbi']} runs.",
        },
        {
            "condition": "g.hits >= 5 AND g.home_runs >= 1",
            "type": "5hit_hr", "history_sql": "hits >= 5 AND home_runs >= 1",
            "label": "5-hit game with a home run",
            "headline": lambda n, r: f"{n} went {r['h']}-for-{r['ab']} with a home run and {r['rbi']} RBI.",
        },
        {
            "condition": "g.rbi >= 7",
            "type": "7rbi_game", "history_sql": "rbi >= 7",
            "label": "7+ RBI game",
            "headline": lambda n, r: f"{n} drove in {r['rbi']} runs, going {r['h']}-for-{r['ab']}.",
        },
        {
            "condition": "g.home_runs >= 3",
            "type": "3hr_game", "history_sql": "home_runs >= 3",
            "label": "3-HR game",
            "headline": lambda n, r: f"{n} hit {r['hr']} home runs, going {r['h']}-for-{r['ab']} with {r['rbi']} RBI.",
        },
    ], category="Rarity", use_first_this_season=True)

    # ---- PITCHING — both tiers ----
    rows = conn.execute("""
        SELECT p.name, g.player_id, g.date, g.innings_pitched, g.strikeouts,
               g.hits, g.walks, g.earned_runs, g.ip_outs
        FROM game_pitching_logs g
        JOIN players p ON g.player_id = p.player_id
        WHERE g.season = ? AND g.date >= (
            SELECT DISTINCT date FROM game_pitching_logs WHERE season = ?
            ORDER BY date DESC LIMIT 1 OFFSET 1
        )
    """, (season, season)).fetchall()

    for name, pid, game_date, ip, so, h, bb, er, ip_outs in rows:
        ip_outs = ip_outs or 0
        so = so or 0
        h = h or 0
        bb = bb or 0
        er = er or 0
        ip_display = fmt_ip(ip_outs)
        key = (pid, game_date)

        # -- RARITY pitching --

        # No-hitter
        if ip_outs >= 27 and h == 0 and key not in seen:
            seen.add(key)
            headline = f"{name} threw a no-hitter over {ip_display} innings, striking out {so}."
            context = _rarity_context_combined(
                conn, "ip_outs >= 27 AND hits = 0", "game_pitching_logs",
                exclude_season=season, player_id=pid,
            )
            if context:
                headline = _continue_with_context(headline, context)
            events.append({"headline": headline, "detail": "", "category": "Rarity", "game_date": game_date,
                           "player_names": [name], "team_names": [], "detection_type": "no_hitter", "priority": 1})
            continue

        # 1-hitter
        if ip_outs >= 27 and h == 1 and key not in seen:
            seen.add(key)
            headline = f"{name} threw a 1-hitter over {ip_display} innings, striking out {so}."
            context = _rarity_context_combined(
                conn, "ip_outs >= 27 AND hits <= 1", "game_pitching_logs",
                exclude_season=season, player_id=pid,
            )
            if context:
                headline = _continue_with_context(headline, context)
            events.append({"headline": headline, "detail": "", "category": "Rarity", "game_date": game_date,
                           "player_names": [name], "team_names": [], "detection_type": "1_hitter", "priority": 1})

        # 15+ K
        if so >= 15 and (pid, game_date, "15k") not in seen:
            seen.add((pid, game_date, "15k"))
            headline = (
                f"{name} struck out {so} in {ip_display} innings, "
                f"allowing {h} hit{'s' if h != 1 else ''} and "
                f"{er} earned run{'s' if er != 1 else ''}."
            )
            context = _rarity_context_combined(
                conn, f"strikeouts >= {so}", "game_pitching_logs",
                exclude_season=season, player_id=pid,
            )
            if context:
                headline = _continue_with_context(headline, context)
            events.append({"headline": headline, "detail": "", "category": "Rarity", "game_date": game_date,
                           "player_names": [name], "team_names": [], "detection_type": "15k_game", "priority": 1})

        # -- NOTEWORTHY pitching --

        # 14+ K (if not already 15+)
        if 14 <= so < 15 and key not in seen:
            seen.add(key)
            headline = (
                f"{name} struck out {so} in {ip_display} innings, "
                f"allowing {h} hit{'s' if h != 1 else ''} and "
                f"{er} earned run{'s' if er != 1 else ''}."
            )
            if _is_first_this_season(conn, "strikeouts >= 14", "game_pitching_logs", season, game_date):
                headline = _continue_with_context(headline, "the first 14+ strikeout game this season")
            events.append({"headline": headline, "detail": "", "category": "Rarity", "game_date": game_date,
                           "player_names": [name], "team_names": [], "detection_type": "14k_game", "priority": 2})

        # 2-hitter (9 IP)
        if ip_outs >= 27 and h == 2 and key not in seen:
            seen.add(key)
            headline = f"{name} threw a 2-hitter over {ip_display} innings, striking out {so}."
            if _is_first_this_season(conn, "ip_outs >= 27 AND hits <= 2", "game_pitching_logs", season, game_date):
                headline = _continue_with_context(headline, "the first 2-hitter this season")
            events.append({"headline": headline, "detail": "", "category": "Rarity", "game_date": game_date,
                           "player_names": [name], "team_names": [], "detection_type": "2_hitter", "priority": 2})

        # Complete game
        if ip_outs >= 27 and key not in seen:
            seen.add(key)
            if er == 0:
                headline = (
                    f"{name} threw a complete game shutout — {ip_display} IP, "
                    f"{so} K, {h} hit{'s' if h != 1 else ''}."
                )
            else:
                headline = f"{name} threw a complete game — {ip_display} IP, {er} ER, {so} K."
            if _is_first_this_season(conn, "ip_outs >= 27", "game_pitching_logs", season, game_date):
                headline = _continue_with_context(headline, "the first complete game this season")
            events.append({"headline": headline, "detail": "", "category": "Rarity", "game_date": game_date,
                           "player_names": [name], "team_names": [], "detection_type": "complete_game", "priority": 2})

    return events


def _raw_pelt_window(conn, player_id, season, latest_date):
    """Run PELT on a player's in-season OPS signal and return the RAW
    last-segment window (from last change point to end of season).

    Unlike `current_form` (which applies a 7-game minimum extension and a
    last-30 fallback), this returns only what PELT actually detects.

    Returns (start_idx, end_idx, stats_dict) or None if:
      - fewer than 2*min_size games,
      - no change point found at either penalty,
      - last segment falls outside [_FEED_STREAK_MIN_GAMES, _FEED_STREAK_MAX_GAMES].
    """
    import numpy as np
    import ruptures as rpt

    games = conn.execute("""
        SELECT date, at_bats, hits, doubles, triples, home_runs,
               walks, strikeouts, plate_appearances, runs, rbi
        FROM game_batting_logs
        WHERE player_id = ? AND season = ?
          AND date <= ?
        ORDER BY date ASC
    """, (player_id, season, latest_date)).fetchall()

    n = len(games)
    if n < _PELT_MIN_SIZE * 2:
        return None

    # Per-game OPS signal (same derivation as detect_streaks.py)
    ops_signal = []
    for g in games:
        ab = g[1] or 0
        h = g[2] or 0
        db = g[3] or 0
        tr = g[4] or 0
        hr = g[5] or 0
        bb = g[6] or 0
        pa = g[8] or 0
        if ab > 0 and pa > 0:
            tb = (h - db - tr - hr) + 2 * db + 3 * tr + 4 * hr
            slg = tb / ab
            obp = (h + bb) / pa
            ops_signal.append(obp + slg)
        else:
            ops_signal.append(0.0)

    signal = np.array(ops_signal)
    smoothed = np.convolve(signal, np.ones(_PELT_ROLLING_WINDOW) / _PELT_ROLLING_WINDOW, mode="same")
    smoothed = smoothed.reshape(-1, 1)

    algo = rpt.Pelt(model="l2", min_size=_PELT_MIN_SIZE, jump=1)
    algo.fit(smoothed)

    def _last_segment_len(bkps):
        # ruptures returns breakpoints as a list of end-indices with the last
        # being len(signal). The last segment is [bkps[-2], bkps[-1]].
        if not bkps:
            return None
        if len(bkps) < 2:
            return None
        return bkps[-1] - bkps[-2]

    # Primary pass
    bkps = algo.predict(pen=_PELT_PENALTY)
    seg_len = _last_segment_len(bkps)
    # If PELT saw no change (single segment == full season), try relaxed penalty
    if seg_len is None or len(bkps) == 1:
        bkps = algo.predict(pen=_PELT_PENALTY_FALLBACK)
        seg_len = _last_segment_len(bkps)
        if seg_len is None or len(bkps) == 1:
            return None

    start_idx = bkps[-2]
    end_idx = bkps[-1]
    num_games = end_idx - start_idx

    if num_games < _FEED_STREAK_MIN_GAMES or num_games > _FEED_STREAK_MAX_GAMES:
        return None

    # Aggregate stats over the window
    segment = games[start_idx:end_idx]
    t_ab = sum(g[1] or 0 for g in segment)
    t_h = sum(g[2] or 0 for g in segment)
    t_2b = sum(g[3] or 0 for g in segment)
    t_3b = sum(g[4] or 0 for g in segment)
    t_hr = sum(g[5] or 0 for g in segment)
    t_bb = sum(g[6] or 0 for g in segment)
    t_pa = sum(g[8] or 0 for g in segment)
    t_r = sum(g[9] or 0 for g in segment)
    t_rbi = sum(g[10] or 0 for g in segment)

    avg = t_h / t_ab if t_ab > 0 else 0.0
    obp = (t_h + t_bb) / t_pa if t_pa > 0 else 0.0
    tb = (t_h - t_2b - t_3b - t_hr) + 2 * t_2b + 3 * t_3b + 4 * t_hr
    slg = tb / t_ab if t_ab > 0 else 0.0
    ops = obp + slg

    return {
        "start_idx": start_idx,
        "end_idx": end_idx,
        "num_games": num_games,
        "start_date": segment[0][0],
        "end_date": segment[-1][0],
        "at_bats": t_ab,
        "hits": t_h,
        "home_runs": t_hr,
        "rbi": t_rbi,
        "runs": t_r,
        "walks": t_bb,
        "plate_appearances": t_pa,
        "batting_avg": avg,
        "obp": obp,
        "slg": slg,
        "ops": ops,
    }


def _pelt_streak_quality(num_games, ops):
    """Bucket a streak window into 'torrid' | 'locked-in' | None.

    Thresholds scale down with window length — a 1.050 OPS across 18 games
    is harder than across 7 games, so the gate relaxes as N grows.
    """
    if num_games < 6 or num_games > 29:
        return None
    if num_games <= 9:
        if ops >= 1.200: return "torrid"
        if ops >= 1.000: return "locked-in"
    elif num_games <= 14:
        if ops >= 1.100: return "torrid"
        if ops >= 0.950: return "locked-in"
    elif num_games <= 19:
        if ops >= 1.050: return "torrid"
        if ops >= 0.920: return "locked-in"
    else:
        if ops >= 1.000: return "torrid"
        if ops >= 0.870: return "locked-in"
    return None


def _find_feed_pelt_comp(conn, exclude_pid, season, window, ops,
                         lookback_years=10, team_codes=None):
    """Find the most recent PRIOR season where a different player had a
    streak of similar length with OPS >= the current OPS.

    Uses `current_form` from prior seasons as the proxy for rolling windows
    (same pattern as deep_scans._find_last_streak_ops) but with a strict
    lookback cap to avoid "hottest since Bonds 2002"-style always-on comps.

    team_codes: optional franchise filter (exact team match excludes mid-
    season trades via the slash-separated team-code convention).

    Returns {"name": str, "season": int} or None.
    """
    # Tolerance: +/-2 games on window length (6-game exact is too restrictive)
    min_games = max(_FEED_STREAK_MIN_GAMES, window - 2)
    max_games = min(_FEED_STREAK_MAX_GAMES, window + 2)
    min_season = season - lookback_years

    tf_sql = ""
    tf_params = []
    if team_codes:
        like_clauses = " OR ".join(["('/' || ss.team || '/') LIKE ?"] * len(team_codes))
        tf_sql = f"""
            AND EXISTS (
                SELECT 1 FROM season_batting_stats ss
                WHERE ss.player_id = cf.player_id AND ss.season = cf.season
                  AND ({like_clauses})
            )
        """
        tf_params = [f"%/{c}/%" for c in team_codes]

    row = conn.execute(f"""
        SELECT p.name, cf.season, cf.num_games
        FROM current_form cf
        JOIN players p ON cf.player_id = p.player_id
        WHERE cf.season < ? AND cf.season >= ?
          AND cf.player_id != ?
          AND cf.num_games BETWEEN ? AND ?
          AND cf.ops >= ?
          {tf_sql}
        ORDER BY cf.season DESC, cf.ops DESC
        LIMIT 1
    """, (season, min_season, exclude_pid, min_games, max_games, ops, *tf_params)).fetchone()

    if row:
        return {"name": row[0], "season": row[1], "num_games": row[2]}
    return None


def _fmt_streak_stats(stats, num_games):
    """Format the stat-list suffix for a hot-streak headline.

    Always leads with OPS. Adds labeled counts (HR/RBI). AVG only on longer
    windows (15+ games) where a 3-digit AVG is meaningful — in small
    samples it's noise.
    """
    ops = stats.get("ops", 0) or 0
    avg = stats.get("batting_avg", 0) or 0
    hr = stats.get("home_runs", 0) or 0
    rbi = stats.get("rbi", 0) or 0

    parts = [f"{_fmt_ops(ops)} OPS"]
    if num_games >= 15:
        parts.append(f"{_fmt_ops(avg)} AVG")
    if hr:
        parts.append(f"{hr} HR")
    if rbi:
        parts.append(f"{rbi} RBI")
    return ", ".join(parts)


def _pelt_streak_headline(name, num_games, quality, stats_phrase, mlb_comp, team_comp, franchise_name):
    """Assemble the final feed headline for a hot-streak event.

    Short windows use "over his last N games" framing; long windows (15+)
    use "N-game heater" framing. Quality label (torrid vs locked-in)
    varies the verb. Historical comps append as em-dashed sentences.
    """
    num_games = int(num_games)
    if num_games < 15:
        if quality == "torrid":
            # "has been torrid" reads as misuse of the adjective. "Torrid"
            # naturally pairs with a noun ("torrid pace", "torrid stretch").
            opener = f"{name} is on a torrid pace over his last {num_games} games"
        else:
            opener = f"{name} has been locked in over his last {num_games} games"
    else:
        if quality == "torrid":
            opener = f"{name} is on a torrid {num_games}-game heater"
        else:
            opener = f"{name} is on an extended {num_games}-game heater"

    sentences = [f"{opener} — {stats_phrase}"]

    # Comparisons are anchored on OPS (see _find_feed_pelt_comp) over a window
    # within ±2 games of the player's own length. State the length explicitly
    # so the reader knows what's being measured — "such a stretch" leaves them
    # guessing. Use the LOWER of the two windows + "+" so the claim ("best
    # OPS over an N+ game stretch") is true for both players regardless of
    # which side has the longer window.
    if mlb_comp:
        n = min(num_games, mlb_comp.get("num_games", num_games))
        sentences.append(
            f"Best OPS over a {n}+ game stretch by any player since {mlb_comp['name']} in {mlb_comp['season']}"
        )
    if team_comp and franchise_name:
        n = min(num_games, team_comp.get("num_games", num_games))
        sentences.append(
            f"Best OPS by {_a_or_an(franchise_name)} {franchise_name} over a {n}+ game stretch since {team_comp['name']} in {team_comp['season']}"
        )

    # Join: first sentence ends at em-dash phrase, rest are separate sentences.
    if len(sentences) == 1:
        return sentences[0] + "."
    return sentences[0] + ". " + ". ".join(sentences[1:]) + "."


def detect_hot_streaks_pelt(conn, season, latest_date=None, cooldowns=None):
    """Feed-only hot-streak detector using RAW PELT windows.

    Differences from the legacy current_form-based version:
      - Uses raw PELT last-segment (no 7-game extension, no last-30 fallback)
      - Enforces 6 <= N <= 29 game window
      - Dynamic torrid/locked-in quality gates by window length
      - OPS-first prose with labeled counts (not a slash line)
      - 10-year MLB historical comp (avoids Bonds 2001-2004 dominance)
      - Team-since comp when deeper than MLB
      - Progressive cooldown (story-deepen rule) via shared cooldowns dict

    Player cards and chat queries still consume `current_form` — this
    detector is intentionally decoupled from that table.
    """
    events = []
    if cooldowns is None:
        cooldowns = {}

    # Candidate pool: players who played on latest_date AND have enough games
    # for PELT to have any hope. 12 is a safe lower bound (2*min_size).
    rows = conn.execute("""
        SELECT DISTINCT p.player_id, p.name, sbs.team
        FROM game_batting_logs g
        JOIN players p ON g.player_id = p.player_id
        LEFT JOIN season_batting_stats sbs
          ON sbs.player_id = p.player_id AND sbs.season = g.season
        WHERE g.season = ? AND g.date = ?
    """, (season, latest_date)).fetchall()

    from datetime import datetime as _dt

    def _on_cooldown(pid, current_gap, days=5):
        """Story-deepens rule: re-fire only if current_gap > previous_gap,
        AND at least `days` have passed since the last fire."""
        val = cooldowns.get((pid, "pelt_feed_streak"))
        if not val:
            return False, 0
        if isinstance(val, tuple) and len(val) >= 2:
            last_date_s, last_gap = val[0], val[1]
        else:
            last_date_s, last_gap = val, 0
        try:
            days_since = (_dt.strptime(latest_date, "%Y-%m-%d") - _dt.strptime(last_date_s, "%Y-%m-%d")).days
        except Exception:
            days_since = 999
        if days_since < days:
            return True, last_gap
        # Past base cooldown: only re-fire if story has deepened (longer gap)
        if current_gap is not None and last_gap is not None and current_gap <= last_gap:
            return True, last_gap
        return False, last_gap

    from services.franchise import get_franchise_codes, get_franchise_name

    for pid, name, team in rows:
        window = _raw_pelt_window(conn, pid, season, latest_date)
        if not window:
            continue
        num_games = window["num_games"]
        ops = window["ops"]

        quality = _pelt_streak_quality(num_games, ops)
        if not quality:
            continue

        # Require today's game to be a reasonable line — avoids "on a tear"
        # next to an 0-for-4. Same floor as legacy detector.
        game_row = conn.execute("""
            SELECT hits, at_bats, home_runs, rbi
            FROM game_batting_logs
            WHERE player_id = ? AND date = ?
        """, (pid, latest_date)).fetchone()
        if game_row:
            gh = game_row[0] or 0
            ghr = game_row[2] or 0
            if gh < 2 and ghr < 1:
                continue

        # Historical comp (MLB, 10-year window)
        mlb_comp = _find_feed_pelt_comp(conn, pid, season, num_games, ops)
        team_comp = None
        franchise_name = None
        if team:
            try:
                team_codes = get_franchise_codes(team)
                franchise_name = get_franchise_name(team)
                team_comp = _find_feed_pelt_comp(
                    conn, pid, season, num_games, ops, team_codes=team_codes
                )
                # Only show team line if it's a DIFFERENT player than the MLB
                # comp and the team gap is at least as deep — otherwise it's
                # redundant or less interesting than the MLB line.
                if team_comp and mlb_comp and team_comp["name"] == mlb_comp["name"]:
                    team_comp = None
            except Exception:
                team_comp = None
                franchise_name = None

        # Progressive cooldown: pick the deepest available gap as "the story"
        mlb_gap = (season - mlb_comp["season"]) if mlb_comp else 0
        team_gap = (season - team_comp["season"]) if team_comp else 0
        current_gap = max(mlb_gap, team_gap)
        on_cd, prev_gap = _on_cooldown(pid, current_gap)
        if on_cd:
            continue

        stats_phrase = _fmt_streak_stats(window, num_games)
        headline = _pelt_streak_headline(
            name, num_games, quality, stats_phrase, mlb_comp, team_comp, franchise_name
        )

        # Register the historical-comparison player(s) so iOS renders them as
        # tappable links. Without this, "Best OPS by any player since Aaron
        # Judge in 2025" leaves "Aaron Judge" as plain text in the feed.
        secondary_names: list[str] = []
        if mlb_comp and mlb_comp.get("name"):
            secondary_names.append(mlb_comp["name"])
        if team_comp and team_comp.get("name") and team_comp["name"] not in secondary_names:
            secondary_names.append(team_comp["name"])

        events.append({
            "headline": headline,
            "detail": "",
            "category": "Streak",
            "game_date": latest_date,
            "player_names": [name] + secondary_names,
            "team_names": [],
            "detection_type": "hot_streak_pelt",
            "priority": 2,
        })
        # Record progressive cooldown
        cooldowns[(pid, "pelt_feed_streak")] = (latest_date, current_gap)

    # Cap at 5 events per run — feed would otherwise flood on hot days
    events.sort(key=lambda e: e.get("priority", 2))
    return events[:5]


# ---------------------------------------------------------------------------
# Tier 3: Backfill detectors (only used when Tiers 1+2 < 3 events)
# ---------------------------------------------------------------------------

def detect_hitting_streaks_relaxed(conn, season, latest_date):
    """Same as detect_hitting_streaks but with a lower threshold (5 games)."""
    return detect_hitting_streaks(conn, season, latest_date, min_games=5)


def detect_league_leaders(conn, season, latest_date=None):
    """Surface current league leaders in key stats."""
    events = []
    min_pa = _qual_min_pa(conn, season)

    leaders = [
        ("home_runs", "home runs", "plate_appearances", min_pa),
        ("batting_avg", "batting average", "plate_appearances", min_pa),
        ("rbi", "RBI", "plate_appearances", min_pa),
        ("stolen_bases", "stolen bases", "plate_appearances", min_pa),
    ]

    for col, label, qual_col, qual_min in leaders:
        row = conn.execute(f"""
            SELECT p.name, s.{col}
            FROM season_batting_stats s
            JOIN players p ON s.player_id = p.player_id
            WHERE s.season = ? AND s.{qual_col} >= ?
            ORDER BY s.{col} DESC
            LIMIT 1
        """, (season, qual_min)).fetchone()

        if row and row[1]:
            name, val = row
            if isinstance(val, float):
                val_str = f".{int(val*1000):03d}"
            else:
                val_str = str(val)
            events.append({
                "headline": f"{name} leads MLB with {val_str} {label}",
                "detail": f"The current leader through early-season action.",
                "category": "Milestone",
                "game_date": latest_date,
                "player_names": [name],
                "team_names": [],
                "detection_type": f"leader_{col}",
                "priority": 3,
            })

    return events


# ---------------------------------------------------------------------------
# Tonight's Matchup Previews
# ---------------------------------------------------------------------------

def _et_today():
    """Today's date in US/Eastern — the feed's clock. The server runs UTC,
    so between 8pm and midnight ET, date.today() is already TOMORROW; using
    it for On This Date / matchup previews published next-day cards early
    (bit us 2026-08-12 when a late-evening redetect posted Aug 13 OTD cards
    on Aug 12). Baseball days are ET days."""
    from zoneinfo import ZoneInfo
    from datetime import datetime as _dt
    return _dt.now(ZoneInfo("America/New_York")).date()


def detect_matchup_previews(conn, season):
    """Generate matchup preview feed cards for tonight's games.

    Selection: pick top 3 pitchers by career ERA (240+ IP, sub-3.50 ERA) or from
    the manual prominence list. Suppress pitchers featured in last 12 days.
    Then find the best opposing batter by career OPS (800+ PA).
    Time-gated: weekdays noon ET+, weekends 9 AM ET+.
    """
    events = []

    try:
        from services.daily_games import get_todays_games
        from services.name_matcher import match_player
    except ImportError:
        return events

    games = get_todays_games()
    if not games:
        return events

    # Load prominence list from config
    import json, os
    config_path = os.path.join(os.path.dirname(__file__), "stat_config.json")
    prominence_list = set()
    try:
        with open(config_path) as f:
            cfg = json.load(f)
        prominence_list = set(cfg.get("pitcher_prominence_list", []))
    except Exception:
        pass

    from datetime import timedelta
    today = _et_today().isoformat()
    suppression_cutoff = (_et_today() - timedelta(days=12)).isoformat()

    # Step 1: Get recently featured pitchers (suppress for 12 days)
    suppressed = set()
    try:
        rows = conn.execute("""
            SELECT player_names FROM notable_events
            WHERE detection_type = 'matchup_preview' AND game_date > ?
        """, (suppression_cutoff,)).fetchall()
        for r in rows:
            names = json.loads(r[0]) if r[0] else []
            if len(names) >= 2:
                suppressed.add(names[1])  # pitcher is second in player_names
    except Exception:
        pass

    # Step 2: Collect all tonight's starters with game info
    starters = []  # (pitcher_name, matched_pitcher, batting_team, pitcher_team, game)
    for game in games:
        away_starter = game.get("away_starter")
        home_starter = game.get("home_starter")
        away_team = game.get("away", "")
        home_team = game.get("home", "")

        if not away_starter or not home_starter:
            continue

        # Home starter faces away batters, away starter faces home batters
        for pitcher_raw, pitcher_team, batting_team in [
            (home_starter, home_team, away_team),
            (away_starter, away_team, home_team),
        ]:
            matched = match_player(pitcher_raw)
            if matched:
                starters.append((pitcher_raw, matched, batting_team, pitcher_team, game))

    if not starters:
        return events

    # Step 3: Bulk career ERA query for all matched pitcher names
    pitcher_names = list(set(s[1] for s in starters))
    placeholders = ",".join("?" * len(pitcher_names))
    career_rows = conn.execute(f"""
        SELECT p.name,
               SUM(s.innings_pitched) as career_ip,
               SUM(s.earned_runs) * 9.0 / NULLIF(SUM(s.innings_pitched), 0) as career_era
        FROM season_pitching_stats s
        JOIN players p ON s.player_id = p.player_id
        WHERE p.name IN ({placeholders})
        GROUP BY p.name
    """, pitcher_names).fetchall()
    career_stats = {r[0]: (r[1], r[2]) for r in career_rows}

    # Step 4: Score and rank pitchers
    # Qualified: career ERA < 3.50 with 240+ IP, OR on prominence list
    def pitcher_score(matched_name):
        ip, era = career_stats.get(matched_name, (0, 99))
        on_prominence = matched_name in prominence_list
        qualified = (ip >= 240 and era < 3.50) or on_prominence
        if not qualified:
            return None
        # Prominence list pitchers without enough IP sort after qualified pitchers
        # but before unqualified ones — use a synthetic ERA of 3.49
        sort_era = era if ip >= 240 else 3.49
        return sort_era

    scored = []
    for pitcher_raw, matched, batting_team, pitcher_team, game in starters:
        score = pitcher_score(matched)
        if score is not None:
            scored.append((score, matched, batting_team, pitcher_team, game))
    # Sort by ERA (best first)
    scored.sort(key=lambda x: x[0])

    # Step 5: Pick top 3, respecting suppression
    selected = []
    for score, matched, batting_team, pitcher_team, game in scored:
        if matched in suppressed:
            continue
        # Avoid two matchups from the same game
        if any(s[4] is game for s in selected):
            continue
        selected.append((score, matched, batting_team, pitcher_team, game))
        if len(selected) >= 3:
            break

    # Step 6: If fewer than 3, relax suppression
    if len(selected) < 3:
        for score, matched, batting_team, pitcher_team, game in scored:
            if any(s[1] == matched for s in selected):
                continue
            if any(s[4] is game for s in selected):
                continue
            selected.append((score, matched, batting_team, pitcher_team, game))
            if len(selected) >= 3:
                break

    # Step 7: For each selected pitcher, find best opposing batter by career OPS (800+ PA)
    # Show first 2 in feed, use 3rd as example text for "try searching" hint
    matchup_data = []  # (batter_name, pitcher_name, batting_team, pitcher_team, game, compelling)
    for _, pitcher_name, batting_team, pitcher_team, game in selected:
        top_batter = conn.execute("""
            SELECT p.name
            FROM season_batting_stats cur
            JOIN players p ON cur.player_id = p.player_id
            JOIN (
                SELECT player_id,
                       (SUM(hits)+SUM(walks)+SUM(hit_by_pitch))*1.0
                           /NULLIF(SUM(at_bats)+SUM(walks)+SUM(hit_by_pitch)+SUM(sacrifice_flies),0)
                       + (SUM(hits)-SUM(doubles)-SUM(triples)-SUM(home_runs)
                          +2*SUM(doubles)+3*SUM(triples)+4*SUM(home_runs))*1.0
                           /NULLIF(SUM(at_bats),0) as career_ops,
                       SUM(plate_appearances) as career_pa
                FROM season_batting_stats
                GROUP BY player_id
                HAVING career_pa >= 800
            ) career ON cur.player_id = career.player_id
            WHERE cur.season = ? AND cur.team = ?
            ORDER BY career.career_ops DESC LIMIT 1
        """, (season, batting_team)).fetchone()

        if not top_batter:
            continue

        batter_name = top_batter[0]

        compelling = _find_compelling_matchup_stat(
            conn, batter_name, pitcher_name, season
        )
        if not compelling:
            compelling = f"{batter_name} faces {pitcher_name} tonight."

        matchup_data.append((batter_name, pitcher_name, batting_team, pitcher_team, game, compelling))

    # Build example hint from 3rd matchup (if available)
    example_hint = ""
    if len(matchup_data) >= 3:
        ex_batter = matchup_data[2][0].split()[-1]  # Last name
        ex_pitcher = matchup_data[2][1].split()[-1]
        example_hint = 'Search any "player vs pitcher" matchup or "player tonight" for other previews.'

    # Generate feed events for first 2 only
    for batter_name, pitcher_name, batting_team, pitcher_team, game, compelling in matchup_data[:2]:
        compelling += " See more in this"

        game_context = "Matchup Preview"

        hint_part = f"[MATCHUP_HINT]{example_hint}[/MATCHUP_HINT]" if example_hint else ""
        events.append({
            "headline": compelling,
            "detail": f" matchup preview.{hint_part}",
            "category": "Tonight",
            "game_date": today,
            "expires_at": game.get("start_time", ""),
            "player_names": [batter_name, pitcher_name],
            "team_names": [team_display(batting_team), team_display(pitcher_team)],
            "detection_type": "matchup_preview",
            "priority": 1,
            "game_context": game_context,
        })

    return events


def _parse_game_time_et(start_time):
    """Parse ISO start time to 'H:MM AM/PM ET' string."""
    if not start_time:
        return ""
    try:
        from datetime import datetime, timedelta, timezone
        dt = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
        et = dt - timedelta(hours=4)
        hour = et.hour
        minute = et.minute
        ampm = "PM" if hour >= 12 else "AM"
        if hour > 12: hour -= 12
        if hour == 0: hour = 12
        return f"{hour}:{minute:02d} {ampm} ET"
    except Exception:
        return ""


def _fmt_ops(val):
    """Format OPS/rate stat: .995 not 0.995, 1.024 stays 1.024."""
    if val < 1.0:
        return f".{int(round(val * 1000)):03d}"
    return f"{val:.3f}"


def _find_compelling_matchup_stat(conn, batter_name, pitcher_name, season):
    """Find one compelling stat for a matchup preview card.

    Tiers (first match wins):
    1. H2H history (any PA)
    2. Pitch mix angle (best/worst pitch matchup from pitcher's arsenal)
    3. Platoon split (.800+ or .650-, 50+ PA)
    4. PELT current form (if exists — player is in a detected streak)
    5. Fallback: season OPS vs season ERA
    """
    try:
        cur = conn.cursor()

        # --- Tier 1: H2H history (even 1 PA) ---
        cur.execute("""
            SELECT SUM(h.plate_appearances), SUM(h.hits), SUM(h.home_runs), SUM(h.at_bats)
            FROM head_to_head h
            JOIN players pb ON h.batter_id = pb.player_id
            JOIN players pp ON h.pitcher_id = pp.player_id
            WHERE pb.name = ? AND pp.name = ?
        """, (batter_name, pitcher_name))
        h2h = cur.fetchone()
        if h2h and h2h[0] and h2h[0] >= 1:
            pa, hits, hr, ab = h2h
            if hr and hr >= 1:
                return f"{batter_name} is {hits}-for-{ab} with {hr} HR in {pa} career PA against {pitcher_name}."
            elif ab and ab > 0:
                return f"{batter_name} is {hits}-for-{ab} in {pa} career PA against {pitcher_name}."

        # --- Tier 2: Pitch mix angle ---
        # Find pitcher's top pitches, check batter's splits against them
        pitcher_mix = None
        for szn in [season, season - 1]:
            cur.execute("""
                SELECT pts.pitch_type, pts.plate_appearances
                FROM pitch_type_pitching_splits pts
                JOIN players p ON pts.player_id = p.player_id
                WHERE p.name = ? AND pts.season = ?
                ORDER BY pts.plate_appearances DESC
            """, (pitcher_name, szn))
            rows = cur.fetchall()
            total_pa = sum(r[1] for r in rows) if rows else 0
            if total_pa >= 50:
                pitcher_mix = rows
                break

        if pitcher_mix:
            total_pa = sum(r[1] for r in pitcher_mix)
            # Check batter's splits against each pitch (use 2025 fallback)
            batter_pitch = {}
            for szn in [season, season - 1]:
                cur.execute("""
                    SELECT pts.pitch_type, pts.ops, pts.plate_appearances, pts.batting_avg
                    FROM pitch_type_batting_splits pts
                    JOIN players p ON pts.player_id = p.player_id
                    WHERE p.name = ? AND pts.season = ?
                """, (batter_name, szn))
                rows = cur.fetchall()
                if rows and sum(r[2] for r in rows) >= 50:
                    batter_pitch = {r[0]: r for r in rows}
                    break

            if batter_pitch:
                # Find most extreme matchup among pitcher's top pitches (>= 10% of mix)
                best_angle = None
                for pitch_type, pitcher_pa in pitcher_mix:
                    mix_pct = pitcher_pa / total_pa
                    if mix_pct < 0.10:
                        continue
                    bp = batter_pitch.get(pitch_type)
                    if not bp or bp[2] < 10:
                        continue
                    ops = bp[1]
                    pct_label = round(mix_pct * 100)
                    if ops >= 0.800:
                        if not best_angle or ops > best_angle[0]:
                            best_angle = (ops, _continue_with_context(
                                f"{batter_name} has hit {_fmt_ops(ops)} OPS against {pitch_type.lower()}s",
                                f"{pct_label}% of {pitcher_name}'s pitch mix",
                            ))
                    elif ops <= 0.650:
                        if not best_angle or ops < best_angle[0]:
                            best_angle = (-ops, _continue_with_context(
                                f"{batter_name} has struggled against {pitch_type.lower()}s ({_fmt_ops(ops)} OPS)",
                                f"{pct_label}% of {pitcher_name}'s pitch mix",
                            ))
                if best_angle:
                    return best_angle[1]

        # --- Tier 3: Platoon split (.800+ or .650-) ---
        cur.execute("SELECT throws FROM players WHERE name = ?", (pitcher_name,))
        hand_row = cur.fetchone()
        if hand_row and hand_row[0]:
            pitcher_hand = hand_row[0]
            split_key = "vs_LHP" if pitcher_hand == "L" else "vs_RHP"
            split = None
            for szn in [season, season - 1]:
                cur.execute("""
                    SELECT ps.ops, ps.home_runs, ps.plate_appearances
                    FROM platoon_splits ps
                    JOIN players p ON ps.player_id = p.player_id
                    WHERE p.name = ? AND ps.season = ? AND ps.split = ?
                """, (batter_name, szn, split_key))
                row = cur.fetchone()
                if row and row[2] and row[2] >= 50:
                    split = row
                    break

            if split and split[0]:
                ops = split[0]
                hand_label = "lefties" if pitcher_hand == "L" else "righties"
                if ops >= 0.800:
                    return _continue_with_context(
                        f"{batter_name} has hit {_fmt_ops(ops)} OPS against {hand_label}",
                        f"{pitcher_name} throws {pitcher_hand}HP",
                    )
                elif ops <= 0.650:
                    return _continue_with_context(
                        f"{batter_name} has hit just {_fmt_ops(ops)} OPS against {hand_label}",
                        f"{pitcher_name} throws {pitcher_hand}HP",
                    )

        # --- Tier 4: PELT current form ---
        cur.execute("""
            SELECT cf.ops, cf.num_games, cf.batting_avg
            FROM current_form cf
            JOIN players p ON cf.player_id = p.player_id
            WHERE p.name = ? AND cf.season = ?
        """, (batter_name, season))
        form = cur.fetchone()
        if form and form[0]:
            return f"{batter_name} is hitting {_fmt_ops(form[0])} OPS over his last {form[1]} games heading into tonight."

        # --- Tier 5: Fallback — season OPS vs season ERA ---
        cur.execute("""
            SELECT s.ops FROM season_batting_stats s
            JOIN players p ON s.player_id = p.player_id
            WHERE p.name = ? AND s.season = ?
            ORDER BY s.plate_appearances DESC LIMIT 1
        """, (batter_name, season))
        batter_ops_row = cur.fetchone()
        cur.execute("""
            SELECT s.era FROM season_pitching_stats s
            JOIN players p ON s.player_id = p.player_id
            WHERE p.name = ? AND s.season = ?
            ORDER BY s.ip_outs DESC LIMIT 1
        """, (pitcher_name, season))
        pitcher_era_row = cur.fetchone()
        if batter_ops_row and pitcher_era_row:
            bops = batter_ops_row[0]
            pera = pitcher_era_row[0]
            if bops and pera:
                return f"{batter_name} ({bops:.3f} OPS) faces {pitcher_name} ({pera:.2f} ERA) tonight."

    except Exception:
        pass

    return None


# ---------------------------------------------------------------------------
# On This Date — historic moments from today's date in past years
# ---------------------------------------------------------------------------

def detect_on_this_date(conn, season, latest_date, target_date=None, attach_date=None):
    """Find historic moments from a given date's month-day in past seasons.

    Very high threshold — no-hitters, 4+ HR games, 20+ K, etc.

    Two date parameters, decoupled so detect_all can show *today's* "on this
    date" history attached to the *latest game date*'s feed bucket:
      - target_date: which calendar day's month-day to look up history for
        (defaults to date.today()).
      - attach_date: which game_date to stamp on the resulting events so they
        appear in that day's feed bucket (defaults to target_date, preserving
        sandbox/backfill behavior where both are the same date).
    """
    events = []

    try:
        today = (target_date or _et_today().isoformat())
        month_day = today[5:]  # "04-09" from "2026-04-09"
    except:
        return events
    stamp_date = attach_date or today

    nl_placeholders = ",".join("?" * len(_NEGRO_LEAGUE_TEAMS))
    nl_params = list(_NEGRO_LEAGUE_TEAMS)

    def _append(headline, player_names, team_names):
        events.append({
            "headline": headline, "detail": "",
            "category": "On This Date", "game_date": stamp_date,
            "player_names": player_names, "team_names": team_names,
            "detection_type": "on_this_date", "priority": 3,
        })

    # --- Perfect games (hand-curated list; emit first, with dedicated phrasing) ---
    perfect_games = _load_perfect_games()
    perfect_game_dates = {pg["date"] for pg in perfect_games}
    for pg in perfect_games:
        if pg["date"][5:] != month_day:
            continue
        yr = pg["date"][:4]
        if int(yr) >= season:
            continue
        headline = (f"On this date in {yr}, {pg['player']} threw a perfect game "
                    f"against the {pg['opponent']}. There have only been "
                    f"{_TOTAL_PERFECT_GAMES_MLB} perfect games in MLB history.")
        _append(headline, [pg["player"]], [pg["opponent"]])

    # --- No-hitters (fame-filtered; excludes perfect games which were emitted above) ---
    # Fame filter: 12+ K OR HoF OR Cy Young OR 4+ AS OR (3+ AS AND still active)
    nohitters = conn.execute(f"""
        SELECT g.date, p.name, g.season, g.innings_pitched, g.strikeouts,
               g.walks, g.opponent
        FROM game_pitching_logs g
        JOIN players p ON g.player_id = p.player_id
        WHERE substr(g.date, 6) = ? AND g.season < ? AND g.ip_outs >= 27 AND g.hits = 0
          AND (
            g.strikeouts >= 12
            OR EXISTS (SELECT 1 FROM awards a WHERE a.player_id = g.player_id AND a.award = 'HOF')
            OR EXISTS (SELECT 1 FROM awards a WHERE a.player_id = g.player_id AND a.award = 'CY')
            OR (SELECT COUNT(*) FROM awards a WHERE a.player_id = g.player_id AND a.award = 'ALL_STAR') >= 4
            OR (
                (SELECT COUNT(*) FROM awards a WHERE a.player_id = g.player_id AND a.award = 'ALL_STAR') >= 3
                AND COALESCE(p.last_season, 0) >= 2024
            )
          )
          AND (g.opponent IS NULL OR g.opponent NOT IN ({nl_placeholders}))
        ORDER BY g.season DESC
    """, [month_day, season] + nl_params).fetchall()

    for date_full, name, yr, ip, so, bb, opp in nohitters:
        if date_full in perfect_game_dates:
            continue  # already emitted as a perfect game
        opp_name = team_display(opp) if opp else ""
        headline = f"On this date in {yr}, {name} threw a no-hitter"
        if so:
            headline += f" with {so} strikeouts"
        if opp_name:
            headline += f" against the {opp_name}"
        headline += "."
        _append(headline, [name], [opp_name] if opp_name else [])

    # --- 4+ HR games (tied MLB single-game record) ---
    total_4hr = conn.execute(f"""
        SELECT COUNT(*) FROM game_batting_logs
        WHERE home_runs >= 4 AND (opponent IS NULL OR opponent NOT IN ({nl_placeholders}))
    """, nl_params).fetchone()[0]
    big_hr = conn.execute(f"""
        SELECT p.name, g.season, g.home_runs, g.hits, g.at_bats, g.rbi, g.opponent
        FROM game_batting_logs g
        JOIN players p ON g.player_id = p.player_id
        WHERE substr(g.date, 6) = ? AND g.season < ? AND g.home_runs >= 4
          AND (g.opponent IS NULL OR g.opponent NOT IN ({nl_placeholders}))
        ORDER BY g.home_runs DESC, g.season DESC
    """, [month_day, season] + nl_params).fetchall()
    for name, yr, hr, h, ab, rbi, opp in big_hr:
        opp_name = team_display(opp) if opp else ""
        headline = f"On this date in {yr}, {name} hit {hr} home runs"
        if opp_name:
            headline += f" against the {opp_name}"
        headline += f", going {h}-for-{ab} with {rbi or 0} RBI"
        headline = _continue_with_context(
            headline,
            f"one of only {total_4hr} games ever to tie the MLB single-game record",
            past_tense=True,
        )
        _append(headline, [name], [opp_name] if opp_name else [])

    # --- 5+ XBH games (ties MLB single-game XBH record; never 6) ---
    total_5xbh = conn.execute(f"""
        SELECT COUNT(*) FROM game_batting_logs
        WHERE (doubles + triples + home_runs) >= 5
          AND (opponent IS NULL OR opponent NOT IN ({nl_placeholders}))
    """, nl_params).fetchone()[0]
    xbh_rows = conn.execute(f"""
        SELECT p.name, g.season, g.doubles, g.triples, g.home_runs,
               g.hits, g.at_bats, g.rbi, g.opponent
        FROM game_batting_logs g
        JOIN players p ON g.player_id = p.player_id
        WHERE substr(g.date, 6) = ? AND g.season < ?
          AND (g.doubles + g.triples + g.home_runs) >= 5
          AND g.home_runs < 4  -- 4-HR games get their own headline above
          AND (g.opponent IS NULL OR g.opponent NOT IN ({nl_placeholders}))
        ORDER BY g.season DESC
    """, [month_day, season] + nl_params).fetchall()
    for name, yr, d, t, hr, h, ab, rbi, opp in xbh_rows:
        opp_name = team_display(opp) if opp else ""
        parts = []
        if d: parts.append(f"{d} double{'s' if d != 1 else ''}")
        if t: parts.append(f"{t} triple{'s' if t != 1 else ''}")
        if hr: parts.append(f"{hr} home run{'s' if hr != 1 else ''}")
        combo = (", ".join(parts[:-1]) + (" and " if len(parts) > 1 else "") + parts[-1]) if parts else ""
        headline = f"On this date in {yr}, {name} had {combo}"
        if opp_name:
            headline += f" against the {opp_name}"
        headline += f", going {h}-for-{ab} with {rbi or 0} RBI"
        headline = _continue_with_context(
            headline,
            f"one of only {total_5xbh} games ever to tie the MLB single-game extra-base hit record",
            past_tense=True,
        )
        _append(headline, [name], [opp_name] if opp_name else [])

    # --- 18+ K games ---
    total_18k = conn.execute(f"""
        SELECT COUNT(*) FROM game_pitching_logs
        WHERE strikeouts >= 18 AND (opponent IS NULL OR opponent NOT IN ({nl_placeholders}))
    """, nl_params).fetchone()[0]
    big_k = conn.execute(f"""
        SELECT p.name, g.season, g.strikeouts, g.innings_pitched, g.ip_outs,
               g.hits, g.earned_runs, g.opponent
        FROM game_pitching_logs g
        JOIN players p ON g.player_id = p.player_id
        WHERE substr(g.date, 6) = ? AND g.season < ? AND g.strikeouts >= 18
          AND (g.opponent IS NULL OR g.opponent NOT IN ({nl_placeholders}))
        ORDER BY g.strikeouts DESC, g.season DESC
    """, [month_day, season] + nl_params).fetchall()
    for name, yr, so, ip, ip_outs, h, er, opp in big_k:
        opp_name = team_display(opp) if opp else ""
        ip_display = fmt_ip(ip_outs)
        headline = f"On this date in {yr}, {name} struck out {so} in {ip_display} innings"
        if opp_name:
            headline += f" against the {opp_name}"
        headline = _continue_with_context(
            headline, f"one of only {total_18k} 18+ K games in MLB history",
            past_tense=True,
        )
        _append(headline, [name], [opp_name] if opp_name else [])

    # --- 10+ RBI games (with extreme-combo callouts) ---
    total_10rbi = conn.execute(f"""
        SELECT COUNT(*) FROM game_batting_logs
        WHERE rbi >= 10 AND (opponent IS NULL OR opponent NOT IN ({nl_placeholders}))
    """, nl_params).fetchone()[0]
    big_rbi = conn.execute(f"""
        SELECT p.name, g.season, g.rbi, g.hits, g.at_bats, g.home_runs, g.opponent
        FROM game_batting_logs g
        JOIN players p ON g.player_id = p.player_id
        WHERE substr(g.date, 6) = ? AND g.season < ? AND g.rbi >= 10
          AND (g.opponent IS NULL OR g.opponent NOT IN ({nl_placeholders}))
        ORDER BY g.rbi DESC, g.season DESC
    """, [month_day, season] + nl_params).fetchall()

    def _combo_count(rbi_min, hr_min, hits_min):
        return conn.execute(f"""
            SELECT COUNT(*) FROM game_batting_logs
            WHERE rbi >= ? AND home_runs >= ? AND hits >= ?
              AND (opponent IS NULL OR opponent NOT IN ({nl_placeholders}))
        """, [rbi_min, hr_min, hits_min] + nl_params).fetchone()[0]

    for name, yr, rbi, h, ab, hr, opp in big_rbi:
        opp_name = team_display(opp) if opp else ""
        hr = hr or 0
        headline = (f"On this date in {yr}, {name} drove in {rbi} runs, "
                    f"going {h}-for-{ab} with {hr} home run{'s' if hr != 1 else ''}")
        if opp_name:
            headline += f" against the {opp_name}"
        # Pick most specific rarity first
        if rbi >= 12 and hr >= 3 and h >= 5:
            n = _combo_count(12, 3, 5)
            rarity = f"one of only {n} games ever with 12+ RBI, 3+ HR, and 5+ hits"
        elif rbi >= 10 and hr >= 3 and h >= 6:
            n = _combo_count(10, 3, 6)
            rarity = f"one of only {n} games ever with 10+ RBI, 3+ HR, and 6+ hits"
        elif rbi >= 11:
            n = _combo_count(11, 0, 0)
            rarity = f"one of only {n} games ever with {rbi}+ RBI"
        else:
            rarity = f"one of only {total_10rbi} 10+ RBI games in MLB history"
        headline = _continue_with_context(headline, rarity, past_tense=True)
        _append(headline, [name], [opp_name] if opp_name else [])

    # Iconic career milestone crossings on this date — from historic_moments table.
    # Each entry is augmented with "Nth player to reach the milestone" using a
    # window function over (stat, threshold) ordered by date.
    try:
        moments = conn.execute("""
            WITH ranked AS (
                SELECT player_name, season, stat, threshold, context, date,
                       ROW_NUMBER() OVER (
                         PARTITION BY stat, threshold ORDER BY date
                       ) AS rank_at_crossing,
                       COUNT(*) OVER (PARTITION BY stat, threshold) AS total_to_date
                FROM historic_moments
            )
            SELECT player_name, season, stat, threshold, context,
                   rank_at_crossing, total_to_date
            FROM ranked
            WHERE substr(date, 6) = ? AND season < ?
            ORDER BY season DESC, threshold DESC
        """, (month_day, season)).fetchall()
        for pname, yr, stat, threshold, context, rank_, total_ in moments:
            rank_phrase = _rank_phrase(rank_, total_, stat, threshold)
            headline = f"On this date in {yr}, {pname} {context}."
            if rank_phrase:
                headline = _continue_with_context(headline, rank_phrase, past_tense=True)
            events.append({
                "headline": headline,
                "detail": "",
                "category": "On This Date",
                "game_date": stamp_date,
                "player_names": [pname],
                "team_names": [],
                "detection_type": "on_this_date",
                "priority": 3,
            })
    except Exception as e:
        # Table may not exist yet; silently skip
        print(f"  On This Date milestones check failed (non-fatal): {e}")

    # Hand-curated iconic non-stat moments (Jackie Robinson debut, Aaron 715,
    # Larsen perfect game, etc). Loaded from JSON, deduped against current year.
    for moment in _load_manual_moments():
        m_date = moment.get("date", "")
        if len(m_date) < 10 or m_date[5:] != month_day:
            continue
        try:
            m_year = int(m_date[:4])
        except ValueError:
            continue
        if m_year >= season:
            continue
        events.append({
            "headline": moment["headline"],
            "detail": "",
            "category": "On This Date",
            "game_date": stamp_date,
            "player_names": moment.get("player_names", []),
            "team_names": moment.get("team_names", []),
            "detection_type": "on_this_date",
            "priority": 3,
        })

    return events


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

DETECTION_LOCK = "/tmp/statchat_detection.lock"


def detect_for_players(db_path, season, player_ids):
    """Targeted event detection for specific players whose games just ended.

    Called by the poll after new game logs are added. Only computes streaks
    and milestones for the given player_ids, not the full slate.
    """
    import time as _time

    if is_detection_locked():
        print("  Detection locked (daily pipeline running) — skipping")
        return 0

    conn = sqlite3.connect(db_path)
    conn.row_factory = None
    ensure_table(conn)

    latest_date = _get_latest_date(conn, season)
    if not latest_date:
        conn.close()
        return 0

    print(f"  Targeted detection for {len(player_ids)} players, latest_date={latest_date}")

    # Players already covered by a richer cross-season streak event from the full
    # pipeline. Skip the breadth-tier hitting/on-base duplicate here, or a poll
    # refresh reintroduces the merge-concatenation bug (the full run's dedup
    # only runs in detect_all). Keyed by (player_name, notable_events type).
    covered_streaks = set()
    for _dt, _key in (("cross_season_streak_hitting", "hitting_streak"),
                      ("cross_season_streak_on_base", "onbase_streak")):
        for (_pn_json,) in conn.execute(
            "SELECT player_names FROM notable_events WHERE game_date = ? AND detection_type = ?",
            (latest_date, _dt)).fetchall():
            try:
                _nm = json.loads(_pn_json)
                if _nm:
                    covered_streaks.add((_nm[0], _key))
            except Exception:
                pass

    events = []

    # Streaks — only for the specific players
    for pid in player_ids:
        name = _player_name(conn, pid)
        if not name:
            continue

        # Hitting streak
        games = conn.execute("""
            SELECT date, hits, at_bats FROM game_batting_logs
            WHERE player_id = ? AND season = ? AND at_bats > 0
            ORDER BY date DESC
        """, (pid, season)).fetchall()

        if games:
            streak = 0
            for gd, hits, ab in games:
                if hits > 0:
                    streak += 1
                else:
                    break

            # On-base streak length (computed up front so the prefer-hitting
            # decision can compare the two run lengths).
            ob_games = conn.execute("""
                SELECT date, hits, walks, COALESCE(hit_by_pitch, 0) as hbp
                FROM game_batting_logs
                WHERE player_id = ? AND season = ?
                    AND (at_bats > 0 OR walks > 0 OR COALESCE(hit_by_pitch, 0) > 0)
                ORDER BY date DESC
            """, (pid, season)).fetchall()
            ob_streak = 0
            for gd, hits, walks, hbp in ob_games:
                if (hits + walks + hbp) > 0:
                    ob_streak += 1
                else:
                    break

            # Prefer hitting when both qualify (more prestigious, same games),
            # UNLESS the on-base streak is >= 10 games longer — then it's a
            # distinct marquee run, so emit on-base instead. Mirrors detect_all.
            hitting_ok = streak >= 8 and (name, "hitting_streak") not in covered_streaks
            onbase_ok = ob_streak >= 12 and (name, "onbase_streak") not in covered_streaks
            emit_onbase = onbase_ok and (not hitting_ok or ob_streak >= streak + 10)
            emit_hitting = hitting_ok and not emit_onbase

            if emit_hitting:
                team = _player_team_display(conn, pid, season)
                team_code = _player_team_code(conn, pid, season)
                secondary: list = []
                context = _historical_context(conn, streak, "hits > 0",
                                              exclude_season=season, exclude_player=pid,
                                              player_team=team_code,
                                              streak_label="hitting",
                                              current_date=latest_date,
                                              secondary_names=secondary)
                headline = f"{name} has hit safely in {streak} straight games"
                if context:
                    # context is already lowercase-leading; don't .lower()
                    # — that would lowercase player names too.
                    headline += f", {context}"
                else:
                    headline += "."
                events.append({
                    "headline": headline, "detail": "", "category": "Streak",
                    "game_date": latest_date, "player_names": [name] + secondary,
                    "team_names": [team] if team else [],
                    "detection_type": "hitting_streak", "priority": 1,
                })

            if emit_onbase:
                team = _player_team_display(conn, pid, season)
                team_code = _player_team_code(conn, pid, season)
                secondary: list = []
                context = _historical_context(
                    conn, ob_streak, "(hits + walks + COALESCE(hit_by_pitch, 0)) > 0",
                    exclude_season=season, exclude_player=pid,
                    player_team=team_code, streak_label="on-base",
                    current_date=latest_date,
                    secondary_names=secondary)
                headline = f"{name} has reached base in {ob_streak} straight games"
                if context:
                    headline += f", {context}"
                else:
                    headline += "."
                events.append({
                    "headline": headline, "detail": "", "category": "Streak",
                    "game_date": latest_date, "player_names": [name] + secondary,
                    "team_names": [team] if team else [],
                    "detection_type": "onbase_streak", "priority": 1,
                })

            # HR streak
            hr_streak = 0
            for gd, hits, ab in games:
                hr = conn.execute("""
                    SELECT home_runs FROM game_batting_logs
                    WHERE player_id = ? AND date = ? AND season = ?
                    LIMIT 1
                """, (pid, gd, season)).fetchone()
                if hr and hr[0] and hr[0] > 0:
                    hr_streak += 1
                else:
                    break
            if hr_streak >= 4:
                team_code = _player_team_code(conn, pid, season)
                secondary: list = []
                context = _historical_context(conn, hr_streak, "home_runs > 0",
                                              exclude_season=season, exclude_player=pid,
                                              player_team=team_code, streak_label="HR",
                                              current_date=latest_date,
                                              secondary_names=secondary)
                headline = f"{name} has homered in {hr_streak} straight games"
                if context:
                    headline += f", {context}"
                else:
                    headline += "."
                events.append({
                    "headline": headline, "detail": "", "category": "Streak",
                    "game_date": latest_date, "player_names": [name] + secondary,
                    "team_names": [],
                    "detection_type": "hr_streak", "priority": 1,
                })

    if not events:
        print("  No notable events for these players")
        conn.close()
        return 0

    # Remove stale streak events for these players on this date, then insert
    streak_types = {"hitting_streak", "onbase_streak", "hr_streak"}
    cursor = conn.cursor()
    for e in events:
        if e["detection_type"] in streak_types:
            cursor.execute("""
                DELETE FROM notable_events
                WHERE detection_type = ? AND game_date = ? AND player_names = ?
            """, (e["detection_type"], e["game_date"],
                  json.dumps(e.get("player_names", []))))

    inserted = 0
    for e in events:
        # Look up game context
        player_names = e.get("player_names", [])
        game_context = None
        if player_names:
            pid_row = conn.execute(
                "SELECT player_id FROM players WHERE name = ?", (player_names[0],)
            ).fetchone()
            if pid_row:
                game_context = _get_game_context(conn, pid_row[0], e["game_date"], season)
        if not game_context:
            try:
                from datetime import datetime
                dt = datetime.strptime(e["game_date"], "%Y-%m-%d")
                game_context = dt.strftime("%B %-d")
            except:
                game_context = e["game_date"]

        try:
            cursor.execute("""
                INSERT OR IGNORE INTO notable_events
                (headline, detail, category, game_date, player_names, team_names,
                 detection_type, priority, game_context, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                e["headline"], e["detail"], e["category"], e["game_date"],
                json.dumps(e.get("player_names", [])),
                json.dumps(e.get("team_names", [])),
                e["detection_type"], e["priority"], game_context,
                e.get("expires_at", ""),
            ))
            if cursor.rowcount > 0:
                inserted += 1
        except sqlite3.IntegrityError:
            pass

    conn.commit()
    print(f"  Inserted {inserted} targeted events")
    conn.close()
    return inserted


def detect_alltime_passing(conn, season, latest_date):
    """Detect when an active player passes someone on an all-time career list.

    Checks HR, Hits, RBI, SB, 2B (batting) and W, K (pitching).
    Fires when today's game pushed a player's career total past someone
    ranked in the top N all-time for that stat.
    """
    events = []

    CONFIGS = [
        # (col, table, game_table, label, abbrev, top_n)
        ("home_runs",     "season_batting_stats",  "game_batting_logs",   "home runs",    "HR",  75),
        ("hits",          "season_batting_stats",  "game_batting_logs",   "hits",         "H",   150),
        ("rbi",           "season_batting_stats",  "game_batting_logs",   "RBI",          "RBI", 150),
        ("stolen_bases",  "season_batting_stats",  "game_batting_logs",   "stolen bases", "SB",  100),
        ("doubles",       "season_batting_stats",  "game_batting_logs",   "doubles",      "2B",  100),
        ("wins",          "season_pitching_stats", "game_pitching_logs",  "wins",         "W",   75),
        ("strikeouts",    "season_pitching_stats", "game_pitching_logs",  "strikeouts",   "K",   75),
    ]

    # Derived game-log stats: (sql_for_leaderboard, game_table, label, abbrev, top_n, trigger_condition)
    DERIVED_CONFIGS = [
        (
            """SELECT g.player_id, p.name, COUNT(*) as career_total
               FROM game_batting_logs g JOIN players p ON g.player_id = p.player_id
               WHERE g.home_runs >= 2
               GROUP BY g.player_id ORDER BY career_total DESC""",
            "game_batting_logs", "multi-HR games", "multi-HR", 50,
            "g.home_runs >= 2",  # trigger: today's game had 2+ HR
        ),
        (
            """SELECT g.player_id, p.name, COUNT(*) as career_total
               FROM game_batting_logs g JOIN players p ON g.player_id = p.player_id
               WHERE g.home_runs >= 3
               GROUP BY g.player_id ORDER BY career_total DESC""",
            "game_batting_logs", "3-HR games", "3-HR", 25,
            "g.home_runs >= 3",  # trigger: today's game had 3+ HR
        ),
    ]

    for col, table, game_table, label, abbrev, top_n in CONFIGS:
        # Build all-time career leaderboard
        all_time = conn.execute(f"""
            SELECT s.player_id, p.name, SUM(s.{col}) as career_total
            FROM {table} s JOIN players p ON s.player_id = p.player_id
            GROUP BY s.player_id
            HAVING career_total > 0
            ORDER BY career_total DESC
        """).fetchall()

        if len(all_time) < top_n:
            continue

        # Build rank lookup: player_id → (rank, name, total)
        rank_map = {}
        for rank, (pid, name, total) in enumerate(all_time, 1):
            rank_map[pid] = (rank, name, total)

        # Threshold: minimum career total to be in the top N
        cutoff_total = all_time[top_n - 1][2]

        # Find active players who played on latest_date and have career totals near the cutoff
        # Check game contribution on latest_date
        game_col = col
        if col == "wins":
            game_col = "win"  # game_pitching_logs uses 'win' not 'wins'

        active_with_games = conn.execute(f"""
            SELECT DISTINCT g.player_id
            FROM {game_table} g
            WHERE g.date = ? AND g.season = ?
        """, (latest_date, season)).fetchall()

        for (pid,) in active_with_games:
            if pid not in rank_map:
                continue
            player_rank, player_name, career_total = rank_map[pid]

            # Only care about players in or near the top N
            if career_total < cutoff_total - 5:
                continue

            # Get today's contribution
            if col == "wins":
                contrib = conn.execute("""
                    SELECT SUM(CASE WHEN win = 1 THEN 1 ELSE 0 END)
                    FROM game_pitching_logs
                    WHERE player_id = ? AND date = ? AND season = ?
                """, (pid, latest_date, season)).fetchone()[0] or 0
            else:
                contrib = conn.execute(f"""
                    SELECT SUM({col}) FROM {game_table}
                    WHERE player_id = ? AND date = ? AND season = ?
                """, (pid, latest_date, season)).fetchone()[0] or 0

            if contrib == 0:
                continue

            career_before = career_total - contrib

            # Find the highest-ranked person this player just passed
            best_passed = None  # (rank, name, total)
            for passed_rank, (passed_pid, passed_name, passed_total) in enumerate(all_time, 1):
                if passed_pid == pid:
                    continue
                if passed_rank > top_n:
                    break
                if career_before < passed_total and career_total >= passed_total:
                    if best_passed is None or passed_rank < best_passed[0]:
                        best_passed = (passed_rank, passed_name, passed_total)

            if best_passed:
                passed_rank, passed_name, _ = best_passed
                game_line, _ = _get_game_line(conn, pid, latest_date, season)
                team = conn.execute(
                    "SELECT team FROM players WHERE player_id = ?", (pid,)
                ).fetchone()
                team_name = team[0] if team else ""

                ordinal = _ordinal(passed_rank)
                if game_line:
                    headline = (
                        f"{player_name} went {game_line}. "
                        f"He now has {career_total:,} career {label}, "
                        f"passing {passed_name} for {ordinal} on the all-time list."
                    )
                else:
                    headline = (
                        f"{player_name} now has {career_total:,} career {label}, "
                        f"passing {passed_name} for {ordinal} on the all-time list."
                    )

                events.append({
                    "headline": headline,
                    "detail": "",
                    "category": "Milestone",
                    "game_date": latest_date,
                    "player_names": [player_name, passed_name],
                    "team_names": [team_name] if team_name else [],
                    "detection_type": f"alltime_passing_{col}",
                    "priority": 1,
                })

            # --- All-time RECORD approach/break (only #1 spot) ---
            # Use verified record totals — our DB starts at 1898, so some
            # all-time leaders have incomplete data (e.g., Cy Young 511 W
            # but we only have 295 from 1898+).
            VERIFIED_RECORDS = {
                "home_runs":    ("Barry Bonds", 762),
                "hits":         ("Pete Rose", 4256),
                "rbi":          ("Hank Aaron", 2297),
                "stolen_bases": ("Rickey Henderson", 1406),
                "doubles":      ("Tris Speaker", 792),  # BBRef: 792; our DB has 793
                "wins":         ("Cy Young", 511),
                "strikeouts":   ("Nolan Ryan", 5714),
            }
            verified = VERIFIED_RECORDS.get(col)
            if verified:
                record_name, record_total = verified
            else:
                record_name, record_total = all_time[0][1], all_time[0][2]
            record_pid = all_time[0][0]  # still need pid to avoid self-check

            if pid != record_pid and contrib > 0:
                gap = record_total - career_total
                prev_gap = record_total - career_before

                if career_total > record_total and career_before <= record_total:
                    # Broke the record
                    game_line, _ = _get_game_line(conn, pid, latest_date, season)
                    player_name = rank_map[pid][1]
                    team = conn.execute(
                        "SELECT team FROM players WHERE player_id = ?", (pid,)
                    ).fetchone()
                    team_name = team[0] if team else ""
                    if game_line:
                        headline = (
                            f"{player_name} went {game_line}. "
                            f"He has set a new all-time record with {career_total:,} career {label}, "
                            f"passing {record_name} ({record_total:,})."
                        )
                    else:
                        headline = (
                            f"{player_name} has set a new all-time record with {career_total:,} career {label}, "
                            f"passing {record_name} ({record_total:,})."
                        )
                    events.append({
                        "headline": headline,
                        "detail": "",
                        "category": "Milestone",
                        "game_date": latest_date,
                        "player_names": [player_name, record_name],
                        "team_names": [team_name] if team_name else [],
                        "detection_type": f"alltime_record_broken_{col}",
                        "priority": 0,
                    })
                elif 0 < gap <= 5 and prev_gap > gap:
                    # Approaching the record
                    game_line, _ = _get_game_line(conn, pid, latest_date, season)
                    player_name = rank_map[pid][1]
                    team = conn.execute(
                        "SELECT team FROM players WHERE player_id = ?", (pid,)
                    ).fetchone()
                    team_name = team[0] if team else ""
                    if game_line:
                        headline = (
                            f"{player_name} went {game_line}. "
                            f"He now has {career_total:,} career {label}, "
                            f"just {gap} away from {record_name}'s all-time record of {record_total:,}."
                        )
                    else:
                        headline = (
                            f"{player_name} now has {career_total:,} career {label}, "
                            f"just {gap} away from {record_name}'s all-time record of {record_total:,}."
                        )
                    events.append({
                        "headline": headline,
                        "detail": "",
                        "category": "Milestone",
                        "game_date": latest_date,
                        "player_names": [player_name, record_name],
                        "team_names": [team_name] if team_name else [],
                        "detection_type": f"alltime_record_approach_{col}",
                        "priority": 0,
                    })

    # --- Derived game-log stats (multi-HR games, 3-HR games) ---
    # These scan game logs, so we only compute counts for triggered players
    # (those who had a qualifying game today), not the full leaderboard.
    for leaderboard_sql, game_table, label, abbrev, top_n, trigger_cond in DERIVED_CONFIGS:
        # Find players who had a qualifying game today (e.g., 2+ HR)
        triggered = conn.execute(f"""
            SELECT g.player_id, p.name
            FROM {game_table} g JOIN players p ON g.player_id = p.player_id
            WHERE g.date = ? AND g.season = ? AND {trigger_cond}
        """, (latest_date, season)).fetchall()

        if not triggered:
            continue

        # Build the full leaderboard once (cached per config), but only
        # if someone was triggered today. This is expensive (~10s) but
        # only runs on days someone actually hit 2+ or 3+ HR.
        all_time_derived = conn.execute(leaderboard_sql).fetchall()
        if len(all_time_derived) < top_n:
            continue

        derived_rank_map = {}
        for rank, (dpid, dname, dtotal) in enumerate(all_time_derived, 1):
            derived_rank_map[dpid] = (rank, dname, dtotal)

        for pid, player_name in triggered:
            if pid not in derived_rank_map:
                continue
            player_rank, _, career_total = derived_rank_map[pid]
            if player_rank > top_n:
                continue

            career_before = career_total - 1

            # Find best person passed
            best_passed = None
            for pr, (ppid, pname, ptotal) in enumerate(all_time_derived, 1):
                if ppid == pid or pr > top_n:
                    break
                if career_before < ptotal and career_total >= ptotal:
                    if best_passed is None or pr < best_passed[0]:
                        best_passed = (pr, pname, ptotal)

            if best_passed:
                passed_rank, passed_name, _ = best_passed
                game_line, _ = _get_game_line(conn, pid, latest_date, season)
                team = conn.execute(
                    "SELECT team FROM players WHERE player_id = ?", (pid,)
                ).fetchone()
                team_name = team[0] if team else ""

                ordinal = _ordinal(passed_rank)
                if game_line:
                    headline = (
                        f"{player_name} went {game_line}. "
                        f"That's his {_ordinal(career_total)} career {label}, "
                        f"passing {passed_name} for {ordinal} on the all-time list."
                    )
                else:
                    headline = (
                        f"{player_name} now has {career_total:,} career {label}, "
                        f"passing {passed_name} for {ordinal} on the all-time list."
                    )

                events.append({
                    "headline": headline,
                    "detail": "",
                    "category": "Milestone",
                    "game_date": latest_date,
                    "player_names": [player_name, passed_name],
                    "team_names": [team_name] if team_name else [],
                    "detection_type": f"alltime_passing_{abbrev.lower().replace('-', '_')}",
                    "priority": 1,
                })

    return events


def detect_franchise_passing(conn, season, latest_date):
    """Detect when a player passes someone on their franchise's all-time career list,
    or approaches within 5 of the franchise record.

    Checks top 5 per franchise for HR, Hits, RBI, SB, 2B (batting) and W, K (pitching).
    """
    from .franchise import get_franchise_codes, get_franchise_name
    events = []

    CONFIGS = [
        ("home_runs",     "season_batting_stats",  "game_batting_logs",   "home runs",    "HR"),
        ("hits",          "season_batting_stats",  "game_batting_logs",   "hits",         "H"),
        ("rbi",           "season_batting_stats",  "game_batting_logs",   "RBI",          "RBI"),
        ("stolen_bases",  "season_batting_stats",  "game_batting_logs",   "stolen bases", "SB"),
        ("doubles",       "season_batting_stats",  "game_batting_logs",   "doubles",      "2B"),
        ("wins",          "season_pitching_stats", "game_pitching_logs",  "wins",         "W"),
        ("strikeouts",    "season_pitching_stats", "game_pitching_logs",  "strikeouts",   "K"),
    ]

    TOP_N = 5
    APPROACH_WITHIN = 5

    for col, table, game_table, label, abbrev in CONFIGS:
        # Get today's contributors with their current team
        if col == "wins":
            contrib_rows = conn.execute(f"""
                SELECT g.player_id, SUM(CASE WHEN g.win = 1 THEN 1 ELSE 0 END) as val,
                       (SELECT team FROM season_pitching_stats
                        WHERE player_id = g.player_id AND season = ? LIMIT 1) as team
                FROM game_pitching_logs g
                WHERE g.date = ? AND g.season = ?
                GROUP BY g.player_id
                HAVING val > 0
            """, (season, latest_date, season)).fetchall()
        else:
            contrib_rows = conn.execute(f"""
                SELECT g.player_id, SUM(g.{col}) as val,
                       (SELECT team FROM season_batting_stats
                        WHERE player_id = g.player_id AND season = ? LIMIT 1) as team
                FROM {game_table} g
                WHERE g.date = ? AND g.season = ?
                GROUP BY g.player_id
                HAVING val > 0
            """, (season, latest_date, season)).fetchall()

        if not contrib_rows:
            continue

        # Group contributors by franchise (only query each franchise once)
        teams_to_check = {}  # team_code → [(pid, contrib)]
        for pid, contrib, team in contrib_rows:
            if not team:
                continue
            team = team.split("/")[0].strip()
            teams_to_check.setdefault(team, []).append((pid, contrib))

        for team, player_contribs in teams_to_check.items():
            franchise_codes = get_franchise_codes(team)
            ph = ",".join(["?"] * len(franchise_codes))
            franchise_name = get_franchise_name(team)

            # Franchise career leaderboard (top 10 for context)
            leaders = conn.execute(f"""
                SELECT s.player_id, p.name, SUM(s.{col}) as career_total
                FROM {table} s JOIN players p ON s.player_id = p.player_id
                WHERE s.team IN ({ph})
                GROUP BY s.player_id
                HAVING career_total > 0
                ORDER BY career_total DESC
                LIMIT 10
            """, franchise_codes).fetchall()

            if len(leaders) < 2:
                continue

            record_holder_pid, record_holder_name, record_total = leaders[0]

            for pid, contrib in player_contribs:
                # Find this player in the leaderboard
                player_entry = None
                for rank, (lpid, lname, ltotal) in enumerate(leaders, 1):
                    if lpid == pid:
                        player_entry = (rank, lname, ltotal)
                        break

                if not player_entry or player_entry[0] > TOP_N + 2:
                    continue

                player_rank, player_name, career_total = player_entry
                career_before = career_total - contrib

                # Check if player has only played for this franchise
                other_teams = conn.execute(f"""
                    SELECT COUNT(DISTINCT team) FROM {table}
                    WHERE player_id = ? AND team NOT IN ({ph})
                """, (pid, *franchise_codes)).fetchone()[0]
                is_lifer = other_teams == 0

                # Build franchise context phrase
                fn = franchise_name[:-1] if franchise_name.endswith("s") else franchise_name
                if is_lifer:
                    career_phrase = f"{career_total:,} career {label}"
                    franchise_suffix = f" in {franchise_name} history"
                else:
                    career_phrase = f"{career_total:,} career {label} as a {fn}"
                    franchise_suffix = " in franchise history"

                # Check for passing someone in top N
                best_passed = None
                for rank, (lpid, lname, ltotal) in enumerate(leaders, 1):
                    if lpid == pid or rank > TOP_N:
                        continue
                    if career_before < ltotal and career_total >= ltotal:
                        if best_passed is None or rank < best_passed[0]:
                            best_passed = (rank, lname, ltotal)

                if best_passed:
                    passed_rank, passed_name, _ = best_passed
                    game_line, _ = _get_game_line(conn, pid, latest_date, season)
                    ordinal = _ordinal(passed_rank)
                    if game_line:
                        headline = (
                            f"{player_name} went {game_line}. "
                            f"He now has {career_phrase}, "
                            f"passing {passed_name} for {ordinal}{franchise_suffix}."
                        )
                    else:
                        headline = (
                            f"{player_name} now has {career_phrase}, "
                            f"passing {passed_name} for {ordinal}{franchise_suffix}."
                        )

                    events.append({
                        "headline": headline,
                        "detail": "",
                        "category": "Milestone",
                        "game_date": latest_date,
                        "player_names": [player_name, passed_name],
                        "team_names": [franchise_name],
                        "detection_type": f"franchise_passing_{col}",
                        "priority": 1,
                    })

                # Check for approaching franchise record (within 5)
                if pid != record_holder_pid:
                    gap = record_total - career_total
                    prev_gap = record_total - career_before
                    if 0 < gap <= APPROACH_WITHIN and prev_gap > gap:
                        game_line, _ = _get_game_line(conn, pid, latest_date, season)
                        if game_line:
                            headline = (
                                f"{player_name} went {game_line}. "
                                f"He now has {career_phrase}, "
                                f"just {gap} away from {record_holder_name}'s franchise record of {record_total:,}."
                            )
                        else:
                            headline = (
                                f"{player_name} now has {career_phrase}, "
                                f"just {gap} away from {record_holder_name}'s franchise record of {record_total:,}."
                            )

                        events.append({
                            "headline": headline,
                            "detail": "",
                            "category": "Milestone",
                            "game_date": latest_date,
                            "player_names": [player_name, record_holder_name],
                            "team_names": [franchise_name],
                            "detection_type": f"franchise_record_approach_{col}",
                            "priority": 1,
                        })

    return events


def _ordinal(n):
    """Convert number to ordinal string: 1 → '1st', 2 → '2nd', etc."""
    if 11 <= (n % 100) <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def is_detection_locked():
    """Check if the full pipeline is running (polls should skip detection)."""
    import os
    if not os.path.exists(DETECTION_LOCK):
        return False
    # Stale lock (older than 30 min) — ignore it
    age = time.time() - os.path.getmtime(DETECTION_LOCK)
    if age > 1800:
        os.remove(DETECTION_LOCK)
        return False
    return True


def _safe_detect(name, fn, *args, **kwargs):
    """Run a detector, catch + log any exception, return the events list (or []).

    Previously, one bad Tier 1/2 detector would crash the entire detect_all()
    call and take out every subsequent detector too — that's how June 18/19/21
    (2026) ended up with only ai_insight/on_this_date/matchup_preview events
    and none of the rule-based stuff. Wrapping each call preserves the rest
    of the run when one detector fails, and log_server_error surfaces the
    failure on /admin/dashboard so it can be diagnosed instead of silently
    swallowed.
    """
    try:
        return fn(*args, **kwargs) or []
    except Exception as e:
        import traceback as _tb
        tb_str = _tb.format_exc()
        print(f"    {name} failed (continuing): {e}\n{tb_str}")
        try:
            from services.metering import log_server_error
            log_server_error(
                source=f"detect_all.{name}",
                error_type=type(e).__name__,
                error_message=str(e),
                context={"traceback": tb_str[-1500:]},
            )
        except Exception as _log_err:
            print(f"    ({name}) Also failed to log the failure: {_log_err}")
        return []


def detect_all(db_path=None, season=None, from_poll=False, force=False, target_date=None):
    """Run all detectors, insert results, prune old events.

    from_poll=True: called from the 15-min poll. Will skip if the daily
    pipeline is running (lock file present).

    force=True: bypass the "no new data since last detection" skip.
    Used by /admin/redetect for manual recovery — always re-runs detection
    even when nothing has changed since the last successful run.

    target_date=YYYY-MM-DD: override the auto-detected latest_date so
    detection runs against a specific past date. Used by /admin/redetect
    to backfill events for a specific game date after a copy/logic fix.
    Implies force=True.
    """
    print("  detect_all build: trend-engine-v1 (9a5ab32+)")
    if from_poll and is_detection_locked():
        print("  Detection locked (daily pipeline running) — skipping")
        return 0

    if db_path is None:
        db_path = DB_PATH

    conn = sqlite3.connect(db_path)
    conn.row_factory = None  # ensure tuples

    ensure_table(conn)

    # Auto-detect season if not provided
    if season is None:
        today = date.today()
        season = today.year

    if target_date:
        latest_date = target_date
        force = True  # Past-date redetection always bypasses the got-data skip
        print(f"  Detection date (override): {latest_date}")
    else:
        latest_date = _get_latest_date(conn, season)
        if not latest_date:
            print(f"  No game logs found for season {season}")
            conn.close()
            return 0
        print(f"  Latest game date: {latest_date}")

    # Got-data skip: if nothing has changed in the underlying game logs
    # since the last successful detection, the heavy pass would just
    # re-derive the same events. Skip and let the next cron try again
    # when MSF actually publishes new data. Cheap probe — the cron's
    # pull_live_stats step always runs, so this only short-circuits the
    # detection itself.
    if not force:
        last_date_row = conn.execute(
            "SELECT updated_at FROM data_freshness WHERE key = ?",
            (f"last_detected_date_{season}",),
        ).fetchone()
        last_count_row = conn.execute(
            "SELECT updated_at FROM data_freshness WHERE key = ?",
            (f"last_detected_count_{season}",),
        ).fetchone()
        current_count = conn.execute(
            "SELECT COUNT(*) FROM game_batting_logs WHERE season = ?", (season,),
        ).fetchone()[0] + conn.execute(
            "SELECT COUNT(*) FROM game_pitching_logs WHERE season = ?", (season,),
        ).fetchone()[0]
        if (last_date_row and last_count_row
                and last_date_row[0] == latest_date
                and int(last_count_row[0]) == current_count):
            print(f"  detect_all SKIP — no new data since last run "
                  f"(latest={latest_date}, count={current_count}). "
                  f"Pass force=True to override.")
            conn.close()
            return 0

    # Don't wipe old events — let them age out via the retention window.
    # INSERT OR IGNORE handles dedup (UNIQUE constraint on headline + date).

    # Backfill game_context for any events that don't have it yet
    backfilled = backfill_game_context(conn, season)
    if backfilled:
        print(f"  Backfilled game_context for {backfilled} events")

    events = []

    # Shared progressive-cooldown dict. Loaded once and passed to both the
    # Tier 2 PELT feed detector and the deep_scans runner so the two share
    # state (a player's PELT feed event suppresses a redundant deep-scan
    # firing and vice versa, and both obey the "story deepens" re-fire rule).
    cd_rows = conn.execute("""
        SELECT player_id, scan_type, last_date, last_gap
        FROM deep_scan_cooldowns
        WHERE last_date >= date(?, '-45 days')
    """, (latest_date,)).fetchall()
    cooldowns = {(pid, st): (ld, lg) for pid, st, ld, lg in cd_rows}

    # Dynamic streak thresholds — higher bar as season progresses
    games_played = conn.execute(
        "SELECT MAX(games) FROM season_batting_stats WHERE season = ?", (season,)
    ).fetchone()
    gp = games_played[0] if games_played and games_played[0] else 10
    hit_streak_min = max(8, min(15, int(gp * 0.75)))
    onbase_streak_min = max(12, min(25, int(gp * 1.0)))
    hr_streak_min = max(3, min(5, int(gp * 0.3)))

    # Tier 1 — each detector wrapped so one crash doesn't take out the rest.
    # Historical June 18/19/21 (2026) incident: an unwrapped detector raised,
    # detect_all bubbled up to pull_live_stats.py which swallowed the print,
    # and no rule-based events fired for 3 days without alerting.
    print(f"  Running Tier 1 detectors... (gp={gp}, hit_min={hit_streak_min}, ob_min={onbase_streak_min}, hr_min={hr_streak_min})")
    events += _safe_detect("hitting_streaks", detect_hitting_streaks, conn, season, latest_date, min_games=hit_streak_min)
    events += _safe_detect("onbase_streaks", detect_onbase_streaks, conn, season, latest_date, min_games=onbase_streak_min)
    events += _safe_detect("hr_streaks", detect_hr_streaks, conn, season, latest_date, min_games=hr_streak_min)
    events += _safe_detect("streak_endings", detect_streak_endings, conn, season, latest_date)
    events += _safe_detect("pitching_streaks", detect_pitching_streaks, conn, season, latest_date)
    events += _safe_detect("season_pace", detect_season_pace, conn, season, latest_date)
    t1_count = len(events)
    print(f"    Tier 1: {t1_count} events")

    # Tier 2
    print("  Running Tier 2 detectors...")
    events += _safe_detect("career_milestones", detect_career_milestones, conn, season, latest_date)
    events += _safe_detect("rarities", detect_rarities, conn, season, latest_date)
    # RETIRED 2026-08-12: detect_hot_streaks_pelt. Its PELT-table windows are
    # season-long segments for most players, which is why hot-streak cards
    # rarely fired. detect_form_edges (trend_engine) finds windows by
    # open-ended change-point on each player's own series — the profile
    # slider's current-form logic, unified. Code kept for rollback.
    t2_count = len(events) - t1_count
    print(f"    Tier 2: {t2_count} events")

    # Trend & discovery engines (2026-08-12) — design log in memory
    # project-trend-discovery-engine.md; deterministic sentences only.
    print("  Running trend & discovery engines...")
    from services.trend_engine import (
        detect_trend_cells, detect_uniqueness_claims, detect_droughts,
        detect_form_edges, detect_history_claims,
    )
    _t0 = len(events)
    events += _safe_detect("trend_cells", detect_trend_cells, conn, season, latest_date)
    events += _safe_detect("uniqueness_claims", detect_uniqueness_claims, conn, season, latest_date)
    events += _safe_detect("hr_droughts", detect_droughts, conn, season, latest_date)
    events += _safe_detect("form_edges", detect_form_edges, conn, season, latest_date)
    events += _safe_detect("history_claims", detect_history_claims, conn, season, latest_date)
    print(f"    Trend & discovery: {len(events) - _t0} events")

    # Records & personal bests
    print("  Running records detection...")
    try:
        from routers.admin import _simulate_records_for_date
        record_events = _simulate_records_for_date(conn, latest_date)
        for re in record_events:
            player_names = [re.get("player", "")] if re.get("player") else []
            team_names = [re.get("team", "")] if re.get("team") else []
            events.append({
                "headline": re["detail"],
                "detail": "",
                "category": re["type"].replace("_", " ").title(),
                "game_date": latest_date,
                "player_names": player_names,
                "team_names": team_names,
                "detection_type": re["type"],
                "priority": 2,
            })
        print(f"    Records: {len(record_events)} events")
    except Exception as e:
        print(f"    Records detection failed: {e}")

    # All-time career list passing
    print("  Running all-time passing detection...")
    try:
        passing_events = detect_alltime_passing(conn, season, latest_date)
        events += passing_events
        print(f"    All-time passing: {len(passing_events)} events")
    except Exception as e:
        print(f"    All-time passing failed: {e}")

    # Franchise career list passing
    print("  Running franchise passing detection...")
    try:
        franchise_events = detect_franchise_passing(conn, season, latest_date)
        events += franchise_events
        print(f"    Franchise passing: {len(franchise_events)} events")
    except Exception as e:
        print(f"    Franchise passing failed: {e}")

    # Tier 3 backfill if needed
    if len(events) < 3:
        print("  Running Tier 3 backfill...")
        events += _safe_detect("hitting_streaks_relaxed", detect_hitting_streaks_relaxed, conn, season, latest_date)
        events += _safe_detect("league_leaders", detect_league_leaders, conn, season, latest_date)
        t3_count = len(events) - t1_count - t2_count
        print(f"    Tier 3: {t3_count} events")

    # Historical scans (DB-verified facts with templates)
    try:
        from services.historical_scans import run_all_scans, template_facts
        print("  Running historical scans...")
        hist_facts = run_all_scans(conn, season, latest_date)
        hist_events = template_facts(conn, hist_facts, season, latest_date)
        for he in hist_events:
            events.append({
                "headline": he["headline"],
                "detail": "",
                "category": he.get("category", "historical"),
                "game_date": latest_date,
                "player_names": he.get("player_names", []),
                "team_names": he.get("team_names", []),
                "detection_type": he.get("detection_type") or "historical_scan",
                "priority": 1,
            })
        print(f"    Historical: {len(hist_events)} events")
    except Exception as e:
        print(f"    Historical scans failed: {e}")

    # Deep scans (team-context fallback, progressive-deepen cooldowns).
    # Persist cooldowns across cron invocations — without persistence the
    # story-deepen logic resets every night.
    try:
        from services.deep_scans import run_deep_scans
        print("  Running deep scans...")
        # `cooldowns` is the shared dict loaded earlier in detect_all — both
        # the PELT feed detector and run_deep_scans mutate it, so persisted
        # state is unified below.
        deep_events = run_deep_scans(conn, season, latest_date, cooldowns=cooldowns)
        for de in deep_events:
            primary = [de.get("player")] if de.get("player") else []
            # Include the historical-comparison player ("the last X player to
            # do this was Y") so iOS renders that name as a tappable link too.
            # Without this, the secondary name appears as plain text in the feed.
            secondary = de.get("secondary_names") or []
            events.append({
                "headline": de.get("detail", ""),
                "detail": "",
                "category": "Deep Scan",
                "game_date": latest_date,
                "player_names": primary + [n for n in secondary if n and n not in primary],
                "team_names": [de.get("team")] if de.get("team") else [],
                "detection_type": f"deep_scan_{de.get('scan', 'unknown')}",
                "priority": 2,
            })
        # Persist updated cooldowns (run_deep_scans mutated the dict in place)
        for (pid, st), val in cooldowns.items():
            if isinstance(val, tuple) and len(val) >= 2:
                last_date_s, last_gap = val[0], val[1]
            else:
                last_date_s, last_gap = val, 0
            conn.execute("""
                INSERT OR REPLACE INTO deep_scan_cooldowns
                    (player_id, scan_type, last_date, last_gap)
                VALUES (?, ?, ?, ?)
            """, (pid, st, last_date_s, int(last_gap or 0)))
        conn.commit()
        print(f"    Deep scans: {len(deep_events)} events")
    except Exception as e:
        import traceback
        print(f"    Deep scans failed (non-fatal): {e}")
        traceback.print_exc()

    # Tonight's matchup previews — wipe previous previews for today first
    # (multiple pipeline runs would otherwise accumulate 3 per run)
    # Matchup previews RETIRED 2026-08-17 (Mark): they solve mid-day
    # freshness for users we don't have yet. detect_matchup_previews kept
    # for potential revival; today's feed bucket now carries OTD only.
    print("  Matchup previews: retired (skipped)")

    # On This Date — look up AND stamp using today's calendar date. Each event
    # in the iOS feed has its own date superheader, so OTD events stamped with
    # latest_date (yesterday's games) read as "May 5 — On this date in 1998..."
    # when the historical content is actually about May 6. Anchoring both to
    # today's calendar makes the superheader match the "this date" in the text.
    print("  Running On This Date...")
    today_iso = date.today().isoformat()
    otd_events = _safe_detect("on_this_date", detect_on_this_date, conn, season,
                              latest_date, target_date=today_iso, attach_date=today_iso)
    events += otd_events
    print(f"    On This Date: {len(otd_events)} events")

    # Remove ALL streak events for latest_date — full recompute replaces them
    # This ensures stale streaks (from bad data) get cleaned up even if the
    # player no longer qualifies for a streak event
    # Wipe ALL events for latest_date — full recompute replaces them.
    # This is the only safe approach: each poll/pipeline run produces a complete
    # set of events for the latest date from the current DB state. Any events
    # from prior runs (which may have been computed on incomplete data) are removed.
    # Events for prior dates are NOT touched — they were computed when that date
    # was latest_date and the data was complete.
    # Trend/discovery engines are TRANSITION-fired with novelty state: they
    # emit a card once and then decline to re-emit ("story already told").
    # The wipe-and-recompute contract deletes anything not re-emitted, so
    # their cards were being erased by the next poll run (2026-08-13..16:
    # a week of cards silently vanished). Exclude them like ai_insight.
    conn.execute("""
        DELETE FROM notable_events WHERE game_date = ? AND detection_type != 'ai_insight'
          AND detection_type NOT LIKE 'trend_%'
          AND detection_type NOT LIKE 'uniqueness_%'
          AND detection_type NOT LIKE 'hr_drought%'
          AND detection_type NOT LIKE 'form_edge%'
          AND detection_type NOT LIKE 'history_claim%'
    """, (latest_date,))
    conn.commit()

    # Suppress breadth-tier notable_events streak events that duplicate a richer
    # cross-season historical scan for the same player+streak-type. Without this
    # the feed merge concatenates both into one headline with conflicting
    # franchise anchors (the historical scan reads the cross-season-aware
    # historical_streaks table; the notable_events scan reads per-season game
    # logs — see the Foxx-vs-Joost bug). The historical scan wins: it counts
    # across seasons and adds "Nth-longest in 100+ years". HR/pitching/ending/
    # PELT streaks have no cross-season counterpart and are untouched.
    _DUP_STREAK_OWNER = {
        "hitting_streak": "cross_season_streak_hitting",
        "onbase_streak": "cross_season_streak_on_base",
    }
    _cross_season_covered = set()
    for e in events:
        if e.get("detection_type") in ("cross_season_streak_hitting", "cross_season_streak_on_base"):
            names = e.get("player_names") or []
            if names:
                _cross_season_covered.add((names[0], e["detection_type"]))
    events = [
        e for e in events
        if not (
            e.get("detection_type") in _DUP_STREAK_OWNER
            and ((e.get("player_names") or [None])[0],
                 _DUP_STREAK_OWNER[e["detection_type"]]) in _cross_season_covered
        )
    ]

    # Prefer hitting over on-base when a player has both: a hitting streak is
    # the more prestigious claim and the on-base run is essentially the same
    # games (on-base length is always >= hitting length), so the feed shouldn't
    # stack two streak sentences for one player. EXCEPTION: if the on-base
    # streak is >= ON_BASE_MARGIN games longer, it's a distinct marquee
    # achievement — keep it and drop the hitting one instead. Marquee
    # on-base-ONLY streaks (no qualifying hitting streak, e.g. a walk-heavy run
    # like Kurtz's 41) are unaffected — there's no hitting event to compare.
    _HITTING_TYPES = {"hitting_streak", "cross_season_streak_hitting"}
    _ONBASE_TYPES = {"onbase_streak", "cross_season_streak_on_base"}
    ON_BASE_MARGIN = 10

    def _streak_games(headline):
        # Streak length is the first "N games" / "N straight games" in the
        # headline (the box-score-derived run), before any "since X (M games)"
        # comparison clause. Returns 0 if not found (→ defaults to prefer-hitting).
        m = re.search(r"\b(\d+)\s+(?:straight\s+)?games\b", headline or "")
        return int(m.group(1)) if m else 0

    _hit_by_player, _ob_by_player = {}, {}
    for e in events:
        dt = e.get("detection_type")
        p = (e.get("player_names") or [None])[0]
        if dt in _HITTING_TYPES:
            _hit_by_player[p] = e
        elif dt in _ONBASE_TYPES:
            _ob_by_player[p] = e
    _drop_ids = set()
    for p, he in _hit_by_player.items():
        oe = _ob_by_player.get(p)
        if not oe:
            continue
        if _streak_games(oe["headline"]) >= _streak_games(he["headline"]) + ON_BASE_MARGIN:
            _drop_ids.add(id(he))   # on-base is the marquee — drop hitting
        else:
            _drop_ids.add(id(oe))   # prefer hitting — drop on-base
    events = [e for e in events if id(e) not in _drop_ids]

    # Suppress hot_streak_pelt for players who already have other events today
    players_with_events = set()
    for e in events:
        if e.get("detection_type") != "hot_streak_pelt" and e.get("game_date") == latest_date:
            for name in e.get("player_names", []):
                players_with_events.add(name)
    events = [
        e for e in events
        if not (e.get("detection_type") == "hot_streak_pelt"
                and any(n in players_with_events for n in e.get("player_names", [])))
    ]

    # Deduplicated insert with game context
    cursor = conn.cursor()
    inserted = 0
    for e in events:
        # Use event's own game_context if provided (e.g. matchup previews set theirs)
        game_context = e.get("game_context")

        # Otherwise look up from game logs
        if not game_context:
            player_names = e.get("player_names", [])
            if player_names:
                first_name = player_names[0]
                pid_row = conn.execute(
                    "SELECT player_id FROM players WHERE name = ?", (first_name,)
                ).fetchone()
                if pid_row:
                    game_context = _get_game_context(conn, pid_row[0], e["game_date"], season)

        if not game_context:
            try:
                from datetime import datetime
                dt = datetime.strptime(e["game_date"], "%Y-%m-%d")
                game_context = dt.strftime("%B %-d")
            except:
                game_context = e["game_date"]

        try:
            cursor.execute("""
                INSERT OR IGNORE INTO notable_events
                (headline, detail, category, game_date, player_names, team_names,
                 detection_type, priority, game_context, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                e["headline"], e["detail"], e["category"], e["game_date"],
                json.dumps(e.get("player_names", [])),
                json.dumps(e.get("team_names", [])),
                e["detection_type"], e["priority"], game_context,
                e.get("expires_at", ""),
            ))
            if cursor.rowcount > 0:
                inserted += 1
        except sqlite3.IntegrityError:
            pass  # Duplicate, skip

    # Prune: keep 7 days, but if fewer than 5 events remain, keep up to 14 days
    cursor.execute("""
        DELETE FROM notable_events WHERE game_date < date(?, '-14 days')
    """, (latest_date,))
    pruned = cursor.rowcount

    # Check if we have enough recent events
    recent_count = cursor.execute("""
        SELECT COUNT(*) FROM notable_events WHERE game_date >= date(?, '-7 days')
    """, (latest_date,)).fetchone()[0]

    if recent_count >= 5:
        # Plenty of recent events — prune the 7-14 day old ones
        cursor.execute("""
            DELETE FROM notable_events WHERE game_date < date(?, '-7 days')
        """, (latest_date,))
        pruned += cursor.rowcount

    conn.commit()

    # Refresh historical_streaks leaderboard if any player broke a
    # ranking-worthy streak today (20+ hitting or 30+ on-base)
    try:
        from services.historical_scans import check_if_historical_streaks_rebuild_needed
        if check_if_historical_streaks_rebuild_needed(conn, season, latest_date):
            print("  Historical streaks leaderboard: rebuild triggered")
            from data_pipeline.build_historical_streaks import build
            conn.close()  # build() opens its own connection
            build(db_path or DB_PATH)
            conn = sqlite3.connect(db_path or DB_PATH)
    except Exception as e:
        print(f"  Historical streaks rebuild check failed (non-fatal): {e}")

    # Refresh historic_moments table if any player crossed an iconic career
    # milestone today (500 HR, 3000 hits, 3000 K, etc.)
    try:
        from services.historical_scans import check_if_historic_moments_rebuild_needed
        if check_if_historic_moments_rebuild_needed(conn, latest_date):
            print("  Historic moments: rebuild triggered (iconic threshold crossed)")
            from data_pipeline.build_historic_moments import build
            conn.close()
            build(db_path or DB_PATH)
            conn = sqlite3.connect(db_path or DB_PATH)
    except Exception as e:
        print(f"  Historic moments rebuild check failed (non-fatal): {e}")

    # Record that we successfully detected for this latest_date + row count.
    # Next run's skip-check compares against these values to avoid re-detecting
    # when nothing has changed.
    try:
        final_count = conn.execute(
            "SELECT COUNT(*) FROM game_batting_logs WHERE season = ?", (season,),
        ).fetchone()[0] + conn.execute(
            "SELECT COUNT(*) FROM game_pitching_logs WHERE season = ?", (season,),
        ).fetchone()[0]
        conn.execute(
            "INSERT OR REPLACE INTO data_freshness (key, updated_at, season) VALUES (?, ?, ?)",
            (f"last_detected_date_{season}", str(latest_date), str(season)),
        )
        conn.execute(
            "INSERT OR REPLACE INTO data_freshness (key, updated_at, season) VALUES (?, ?, ?)",
            (f"last_detected_count_{season}", str(final_count), str(season)),
        )
        conn.commit()
    except Exception as e:
        print(f"  Warning: could not record detection markers: {e}")

    conn.close()

    print(f"  Notable events: {inserted} new, {pruned} pruned, {len(events)} total detected")

    # Archive events permanently (metering.db — not pruned)
    try:
        from services.metering import archive_events
        archived = archive_events(events)
        print(f"  Archived {archived} events")
    except Exception as e:
        print(f"  Archive failed: {e}")

    return len(events)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--season", type=int, default=None)
    args = parser.parse_args()
    detect_all(args.db, args.season)
