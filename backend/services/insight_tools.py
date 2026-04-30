"""
Tool executor for AI insight generation.

Sonnet uses these narrow read-only tools to verify any factual claim
before writing it. Replaces the pre-computed snapshot pattern that
allowed hallucinated "first MLB", "0.00 ERA through 3 starts" etc.

Each tool returns a JSON-serializable dict. Failures return
{"error": "..."} so Sonnet can react in-loop instead of crashing.
"""

import sqlite3


# ---------- Tool implementations ----------

# Split-ID groups: these represent the SAME player whose career is split
# across multiple player_id rows (Retrosheet vs MSF, parent/child name
# overlap, etc.). When any tool needs a "career total" for a player_id in
# one of these groups, it must aggregate across ALL ids in the group —
# otherwise we get false "first career HR" claims for established veterans.
#
# Source: long-standing player ID issues catalogued in CLAUDE.md and
# memory/baseball-stats.md. The proper fix is merging IDs in the players
# table; this is the tactical workaround until that ships.
_SPLIT_ID_GROUPS = [
    # Bobby Witt Jr.: Jr. data under father's Retrosheet name 2022-24,
    # then MSF assigned a fresh id for 2025-26.
    {"wittb002", "wittjb001"},
    # Jazz Chisholm Jr.: Retrosheet "Jazz Chisholm" through 2024,
    # MSF "Jasrado Chisholm Jr." for 2025-26.
    {"chisj001", "chishj001"},
    # Ronald Acuña Jr.: Retrosheet "Ronald Acuna" through 2024,
    # MSF "Ronald Acuña Jr." (with accent) for 2025-26.
    {"acunr001", "acuñar001"},
]
_SPLIT_ID_LOOKUP = {pid: group for group in _SPLIT_ID_GROUPS for pid in group}


def _aliased_player_ids(player_id: str) -> list:
    """Return all player_ids that represent the same real-world player.
    Always includes the input id; adds split-group siblings if any."""
    group = _SPLIT_ID_LOOKUP.get(player_id)
    if group:
        return sorted(group)
    return [player_id]


def get_player_career_summary(conn: sqlite3.Connection, player_id: str) -> dict:
    """Career totals + season count + debut year + current team.

    Aggregates across known split-id siblings (`_SPLIT_ID_GROUPS`) so
    veteran players with fragmented ids return correct career totals.
    """
    ids = _aliased_player_ids(player_id)
    placeholders = ",".join("?" * len(ids))

    bat = conn.execute(f"""
        SELECT COALESCE(SUM(home_runs),0), COALESCE(SUM(hits),0),
               COALESCE(SUM(rbi),0), COALESCE(SUM(stolen_bases),0),
               COALESCE(SUM(runs),0), COALESCE(SUM(walks),0),
               COUNT(DISTINCT season), MIN(season), MAX(season)
        FROM season_batting_stats WHERE player_id IN ({placeholders})
    """, ids).fetchone()

    pitch = conn.execute(f"""
        SELECT COALESCE(SUM(wins),0), COALESCE(SUM(losses),0),
               COALESCE(SUM(saves),0), COALESCE(SUM(strikeouts),0),
               COALESCE(SUM(games_started),0),
               COUNT(DISTINCT season), MIN(season), MAX(season)
        FROM season_pitching_stats WHERE player_id IN ({placeholders})
    """, ids).fetchone()

    name_row = conn.execute(
        "SELECT name, team FROM players WHERE player_id = ?", (player_id,)
    ).fetchone()
    if not name_row:
        return {"error": f"Player {player_id} not found"}

    bat_seasons = bat[6] if bat else 0
    pitch_seasons = pitch[5] if pitch else 0
    seasons_played = max(bat_seasons or 0, pitch_seasons or 0)
    debut = min(bat[7] or 9999, pitch[6] or 9999) if (bat or pitch) else None
    last = max(bat[8] or 0, pitch[7] or 0) if (bat or pitch) else None

    return {
        "player_id": player_id,
        "name": name_row[0],
        "current_team": name_row[1],
        "seasons_played": seasons_played,
        "debut_season": debut if debut and debut < 9999 else None,
        "last_season": last if last else None,
        "career_batting": {
            "home_runs": bat[0], "hits": bat[1], "rbi": bat[2],
            "stolen_bases": bat[3], "runs": bat[4], "walks": bat[5],
        } if bat else {},
        "career_pitching": {
            "wins": pitch[0], "losses": pitch[1], "saves": pitch[2],
            "strikeouts": pitch[3], "games_started": pitch[4],
        } if pitch else {},
    }


def get_career_high(conn: sqlite3.Connection, player_id: str, stat: str, scope: str) -> dict:
    """Max single-game or single-season value for a stat.

    scope: 'game' or 'season'
    Returns the max value, when set (date/season), and how many times reached.
    """
    BAT_GAME_STATS = {"hits", "home_runs", "rbi", "runs", "stolen_bases",
                      "doubles", "triples", "walks"}
    PITCH_GAME_STATS = {"strikeouts", "ip_outs", "earned_runs", "walks"}
    BAT_SEASON_STATS = {"home_runs", "hits", "rbi", "stolen_bases", "runs",
                        "walks", "doubles", "triples", "batting_avg", "ops"}
    PITCH_SEASON_STATS = {"wins", "saves", "strikeouts", "era", "innings_pitched",
                          "games_started"}

    if scope == "game":
        if stat in BAT_GAME_STATS:
            table = "game_batting_logs"
        elif stat in PITCH_GAME_STATS:
            table = "game_pitching_logs"
        else:
            return {"error": f"Unknown game stat: {stat}"}
        max_row = conn.execute(
            f"SELECT MAX({stat}) FROM {table} WHERE player_id = ?", (player_id,)
        ).fetchone()
        if not max_row or max_row[0] is None:
            return {"player_id": player_id, "stat": stat, "scope": scope,
                    "career_high": None, "times_reached": 0}
        max_val = max_row[0]
        first_set = conn.execute(
            f"SELECT date, season FROM {table} WHERE player_id = ? AND {stat} = ? ORDER BY date ASC LIMIT 1",
            (player_id, max_val)
        ).fetchone()
        times = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE player_id = ? AND {stat} = ?",
            (player_id, max_val)
        ).fetchone()[0]
        return {
            "player_id": player_id, "stat": stat, "scope": "game",
            "career_high": max_val, "times_reached": times,
            "first_set_date": first_set[0] if first_set else None,
            "first_set_season": first_set[1] if first_set else None,
        }

    elif scope == "season":
        if stat in BAT_SEASON_STATS:
            table = "season_batting_stats"
        elif stat in PITCH_SEASON_STATS:
            table = "season_pitching_stats"
        else:
            return {"error": f"Unknown season stat: {stat}"}
        max_row = conn.execute(
            f"SELECT MAX({stat}), season FROM {table} WHERE player_id = ?",
            (player_id,)
        ).fetchone()
        if not max_row or max_row[0] is None:
            return {"player_id": player_id, "stat": stat, "scope": scope,
                    "career_high": None}
        return {
            "player_id": player_id, "stat": stat, "scope": "season",
            "career_high": max_row[0], "set_season": max_row[1],
        }
    else:
        return {"error": f"scope must be 'game' or 'season', got '{scope}'"}


def get_season_aggregates(conn: sqlite3.Connection, player_id: str, season: int) -> dict:
    """Current season totals + games played + pace projections."""
    bat = conn.execute("""
        SELECT games, home_runs, hits, rbi, stolen_bases, runs, walks,
               at_bats, batting_avg, on_base_pct, slugging_pct, ops, plate_appearances
        FROM season_batting_stats WHERE player_id = ? AND season = ?
    """, (player_id, season)).fetchone()

    pitch = conn.execute("""
        SELECT games, games_started, wins, losses, saves, strikeouts, walks,
               innings_pitched, earned_runs, era, whip
        FROM season_pitching_stats WHERE player_id = ? AND season = ?
    """, (player_id, season)).fetchone()

    out = {"player_id": player_id, "season": season}
    if bat:
        gp = bat[0] or 0
        out["batting"] = {
            "games": gp, "home_runs": bat[1], "hits": bat[2], "rbi": bat[3],
            "stolen_bases": bat[4], "runs": bat[5], "walks": bat[6],
            "at_bats": bat[7], "batting_avg": bat[8], "obp": bat[9],
            "slg": bat[10], "ops": bat[11], "plate_appearances": bat[12],
            "pace_162": {
                "home_runs": int((bat[1] or 0) * 162.0 / gp) if gp else None,
                "rbi": int((bat[3] or 0) * 162.0 / gp) if gp else None,
                "stolen_bases": int((bat[4] or 0) * 162.0 / gp) if gp else None,
            },
        }
    if pitch:
        gs = pitch[1] or 0
        out["pitching"] = {
            "games": pitch[0], "games_started": gs, "wins": pitch[2],
            "losses": pitch[3], "saves": pitch[4], "strikeouts": pitch[5],
            "walks": pitch[6], "innings_pitched": pitch[7],
            "earned_runs": pitch[8], "era": pitch[9], "whip": pitch[10],
        }
    return out


def get_active_streak(conn: sqlite3.Connection, player_id: str, condition: str) -> dict:
    """Current active consecutive-game streak meeting a condition.

    Conditions:
      hit              — 1+ hit
      on_base          — 1+ H or BB or HBP
      hr               — 1+ HR
      multi_hit        — 2+ H
      scoreless_start  — pitching start with 0 ER
      quality_start    — pitching start with 6+ IP and ≤3 ER
    """
    BAT_CONDS = {
        "hit": "hits >= 1",
        "on_base": "(hits + walks + COALESCE(hbp, 0)) >= 1",
        "hr": "home_runs >= 1",
        "multi_hit": "hits >= 2",
    }
    PITCH_CONDS = {
        "scoreless_start": "is_start = 1 AND earned_runs = 0",
        "quality_start": "is_start = 1 AND ip_outs >= 18 AND earned_runs <= 3",
    }
    if condition in BAT_CONDS:
        table, where_extra = "game_batting_logs", BAT_CONDS[condition]
        is_start_filter = ""
    elif condition in PITCH_CONDS:
        table = "game_pitching_logs"
        where_extra = PITCH_CONDS[condition]
        is_start_filter = " AND is_start = 1" if "is_start" in where_extra else ""
    else:
        return {"error": f"Unknown condition: {condition}"}

    rows = conn.execute(f"""
        SELECT date, ({where_extra}) AS hit_cond
        FROM {table}
        WHERE player_id = ?{is_start_filter}
        ORDER BY date DESC
    """, (player_id,)).fetchall()

    streak_len, streak_start = 0, None
    for date, hit in rows:
        if hit:
            streak_len += 1
            streak_start = date
        else:
            break

    return {
        "player_id": player_id, "condition": condition,
        "streak_length": streak_len,
        "streak_start_date": streak_start,
        "last_game_date": rows[0][0] if rows else None,
    }


def get_career_threshold_count(conn: sqlite3.Connection, player_id: str,
                                stat: str, threshold: int) -> dict:
    """Count of career games (or starts) where stat >= threshold, plus total games.

    Use this to verify any 'Nth career X' anchor and to compute the rate
    so you can choose the right framing (rare vs dominance).
    """
    BAT_GAME_STATS = {"hits", "home_runs", "rbi", "runs", "stolen_bases",
                      "doubles", "triples", "walks"}
    PITCH_START_STATS = {"strikeouts", "ip_outs", "earned_runs"}
    PITCH_GAME_STATS = {"strikeouts", "ip_outs", "earned_runs", "walks"}

    if stat in BAT_GAME_STATS:
        table, start_filter = "game_batting_logs", ""
    elif stat in PITCH_START_STATS:
        # Default to starts-only for pitcher counting stats — that's what
        # "Nth double-digit K start" framing means.
        table, start_filter = "game_pitching_logs", " AND is_start = 1"
    elif stat in PITCH_GAME_STATS:
        table, start_filter = "game_pitching_logs", ""
    else:
        return {"error": f"Unknown stat: {stat}"}

    row = conn.execute(f"""
        SELECT
          SUM(CASE WHEN {stat} >= ? THEN 1 ELSE 0 END) AS at_threshold,
          COUNT(*) AS total
        FROM {table}
        WHERE player_id = ?{start_filter}
    """, (threshold, player_id)).fetchone()

    at = row[0] or 0
    total = row[1] or 0
    rate = round(at / total * 100, 1) if total else 0.0

    return {
        "player_id": player_id,
        "stat": stat,
        "threshold": threshold,
        "games_at_threshold": at,
        "total_career_games": total,
        "rate_pct": rate,
        "scope": "starts" if start_filter else "games",
    }


def get_first_since(conn: sqlite3.Connection, stat: str, threshold: int,
                    scope: str = "mlb", team_code: str = None,
                    before_season: int = None) -> dict:
    """Most recent prior season-total threshold occurrence.

    scope: 'mlb' (any team) or 'team' (filter to team_code's franchise)
    Useful for "first Yankees pitcher since X to ..." style anchors.
    """
    BAT_STATS = {"home_runs", "hits", "rbi", "stolen_bases", "runs", "walks"}
    PITCH_STATS = {"wins", "saves", "strikeouts"}
    if stat in BAT_STATS:
        table = "season_batting_stats"
    elif stat in PITCH_STATS:
        table = "season_pitching_stats"
    else:
        return {"error": f"Unknown stat: {stat}"}

    where = [f"s.{stat} >= ?"]
    params = [threshold]
    if before_season is not None:
        where.append("s.season < ?")
        params.append(before_season)
    if scope == "team" and team_code:
        where.append("s.team = ?")
        params.append(team_code)

    row = conn.execute(f"""
        SELECT s.season, p.name, s.team, s.{stat}
        FROM {table} s JOIN players p ON s.player_id = p.player_id
        WHERE {' AND '.join(where)}
        ORDER BY s.season DESC LIMIT 1
    """, params).fetchone()

    if not row:
        return {"stat": stat, "threshold": threshold, "scope": scope,
                "team_code": team_code, "found": False}
    return {
        "stat": stat, "threshold": threshold, "scope": scope,
        "team_code": team_code, "found": True,
        "season": row[0], "player_name": row[1], "team": row[2], "value": row[3],
    }


# ---------- Tool registry for Anthropic SDK ----------

TOOLS = [
    {
        "name": "get_player_career_summary",
        "description": (
            "Look up a player's career totals (batting + pitching), debut year, "
            "seasons played, and current team. Use this to verify any 'first MLB', "
            "'rookie', 'Nth season', or 'career first' claim before writing it. "
            "If career_batting.home_runs > 0, the player has hit a HR before — "
            "do NOT write 'first MLB homer'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"player_id": {"type": "string"}},
            "required": ["player_id"],
        },
    },
    {
        "name": "get_career_high",
        "description": (
            "Look up a player's career-high value for a single stat, at single-game "
            "or single-season scope. Returns the max value, when it was first set, "
            "and how many times reached. Use this to verify any 'career high', "
            "'matches his best', or 'never done before' claim."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "player_id": {"type": "string"},
                "stat": {"type": "string", "description": (
                    "Game stats: hits, home_runs, rbi, runs, stolen_bases, "
                    "doubles, triples, walks, strikeouts, ip_outs, earned_runs. "
                    "Season stats: home_runs, hits, rbi, stolen_bases, runs, "
                    "walks, batting_avg, ops, wins, saves, strikeouts, era, "
                    "innings_pitched, games_started."
                )},
                "scope": {"type": "string", "enum": ["game", "season"]},
            },
            "required": ["player_id", "stat", "scope"],
        },
    },
    {
        "name": "get_season_aggregates",
        "description": (
            "Look up a player's current-season totals, games played, ERA/AVG/OPS, "
            "and 162-game pace projections. Use this to verify any current-season "
            "stat claim (e.g., '0.00 ERA through 3 starts', 'on pace for 60 HR'). "
            "If pitching.era > 0.00 and you wrote '0.00 ERA', you're hallucinating."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "player_id": {"type": "string"},
                "season": {"type": "integer"},
            },
            "required": ["player_id", "season"],
        },
    },
    {
        "name": "get_active_streak",
        "description": (
            "Look up a player's current consecutive-game streak for a given "
            "condition. streak_length is the number of consecutive games (most "
            "recent first) meeting the condition. Use this to verify any 'N "
            "consecutive', 'X-game streak', or 'extended his streak' claim."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "player_id": {"type": "string"},
                "condition": {
                    "type": "string",
                    "enum": ["hit", "on_base", "hr", "multi_hit",
                             "scoreless_start", "quality_start"],
                },
            },
            "required": ["player_id", "condition"],
        },
    },
    {
        "name": "get_career_threshold_count",
        "description": (
            "Count of career games (or starts, for pitcher counting stats) "
            "where the stat met or exceeded the threshold, plus total career "
            "games. Returns rate_pct so you can choose the right framing. "
            "REQUIRED before writing any 'Nth career X-stat-game' anchor — "
            "use the rate to decide rare-vs-dominance framing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "player_id": {"type": "string"},
                "stat": {"type": "string", "description": (
                    "Game/start stat: hits, home_runs, rbi, runs, stolen_bases, "
                    "doubles, triples, walks, strikeouts, ip_outs, earned_runs."
                )},
                "threshold": {"type": "integer"},
            },
            "required": ["player_id", "stat", "threshold"],
        },
    },
    {
        "name": "get_first_since",
        "description": (
            "Find the most recent prior season-total occurrence of a stat at or "
            "above a threshold. Use this to anchor 'first since X' or 'first "
            "Yankees player to ...' claims. Returns the most recent player+season "
            "that hit the threshold, optionally filtered by team."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "stat": {"type": "string"},
                "threshold": {"type": "integer"},
                "scope": {"type": "string", "enum": ["mlb", "team"]},
                "team_code": {"type": "string"},
                "before_season": {"type": "integer"},
            },
            "required": ["stat", "threshold"],
        },
    },
]


# ---------- Dispatcher ----------

_DISPATCH = {
    "get_player_career_summary": lambda conn, args: get_player_career_summary(
        conn, args["player_id"]),
    "get_career_high": lambda conn, args: get_career_high(
        conn, args["player_id"], args["stat"], args["scope"]),
    "get_season_aggregates": lambda conn, args: get_season_aggregates(
        conn, args["player_id"], args["season"]),
    "get_active_streak": lambda conn, args: get_active_streak(
        conn, args["player_id"], args["condition"]),
    "get_career_threshold_count": lambda conn, args: get_career_threshold_count(
        conn, args["player_id"], args["stat"], args["threshold"]),
    "get_first_since": lambda conn, args: get_first_since(
        conn, args["stat"], args["threshold"],
        args.get("scope", "mlb"), args.get("team_code"),
        args.get("before_season")),
}


def execute_tool(conn: sqlite3.Connection, name: str, args: dict) -> dict:
    """Run a tool by name. Returns {"error": "..."} on unknown tool or failure."""
    fn = _DISPATCH.get(name)
    if not fn:
        return {"error": f"Unknown tool: {name}"}
    try:
        return fn(conn, args)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
