"""
Python port of iOS PlayerNameMatcher.swift for the baseball stats backend.

Provides player name matching, stat alias resolution, and query parsing
for local interception of common query patterns (comparisons, leaderboards,
season lookups, splits, streaks, etc.).

Names are loaded from the SQLite database on module import and cached.
"""

import json
import os
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import date
from typing import Optional

DB_PATH = os.getenv(
    "DB_PATH",
    os.path.join(os.path.dirname(__file__), "..", "baseball_stats_full.db"),
)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StatInfo:
    db_column: str       # e.g. "home_runs"
    display_abbrev: str  # e.g. "HR"
    display_name: str    # e.g. "Home Runs"
    is_rate: bool        # True for AVG, OBP, SLG, OPS, OPS+, ISO, BABIP

    @property
    def pill_name(self) -> str:
        """Lowercased display name, but preserves all-caps acronyms."""
        return self.display_name if self.display_name == self.display_abbrev else self.display_name.lower()


# ---------------------------------------------------------------------------
# Load shared config (stat_config.json — single source of truth)
# ---------------------------------------------------------------------------

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "stat_config.json")
with open(_CONFIG_PATH) as _f:
    _config = json.load(_f)

# ---------------------------------------------------------------------------
# Stat alias map (built from shared config)
# ---------------------------------------------------------------------------

stat_alias_map: dict[str, StatInfo] = {}
# Fallback aliases: generic phrases like "best hitter" / "top batter" that should
# only resolve to a stat when no specific stat alias matched elsewhere in the query.
# Keeps "top batters by strikeouts" routing to strikeouts (specific) rather than
# OPS (generic), while still letting "best hitter 2025" default to OPS.
stat_fallback_alias_map: dict[str, StatInfo] = {}
for _db_col, _entry in _config["stat_aliases"].items():
    _info = StatInfo(db_column=_db_col, display_abbrev=_entry["abbrev"],
                     display_name=_entry["name"], is_rate=_entry["is_rate"])
    for _alias in _entry["aliases"]:
        stat_alias_map[_alias] = _info
    for _alias in _entry.get("fallback_aliases", []):
        stat_fallback_alias_map[_alias] = _info

# Pre-sorted longest first for greedy matching
_sorted_stat_aliases: list[str] = sorted(stat_alias_map.keys(), key=len, reverse=True)
_sorted_fallback_aliases: list[str] = sorted(stat_fallback_alias_map.keys(), key=len, reverse=True)

# Stats that are ONLY pitching (not shared with batting)
pitching_only_stats: set[str] = set(_config["pitching_only_stats"])


def is_pitching_stat(stat_or_str) -> bool:
    """Check if a stat is pitching-only. Accepts StatInfo or db_column string."""
    col = stat_or_str.db_column if isinstance(stat_or_str, StatInfo) else stat_or_str
    return col in pitching_only_stats


# ---------------------------------------------------------------------------
# Common word last names, nickname aliases, disambig map (from shared config)
# ---------------------------------------------------------------------------

common_word_last_names: set[str] = set(_config["common_word_last_names"])

nickname_aliases: dict[str, str] = _config["nickname_aliases"]

disambig_sr_jr_map: dict[str, list[str]] = _config["disambig_sr_jr_map"]

# Fame list — all-time greats who should auto-resolve when their last name is searched
fame_list: set[str] = set(_config.get("fame_list", []))


# ---------------------------------------------------------------------------
# Team alias map (all 30 MLB teams)
# ---------------------------------------------------------------------------

team_alias_map: dict[str, str] = {
    # Full names
    "arizona diamondbacks": "ARI", "atlanta braves": "ATL",
    "baltimore orioles": "BAL", "boston red sox": "BOS",
    "chicago cubs": "CHN", "chicago white sox": "CHA",
    "cincinnati reds": "CIN", "cleveland guardians": "CLE",
    "colorado rockies": "COL", "detroit tigers": "DET",
    "houston astros": "HOU", "kansas city royals": "KCA",
    "los angeles angels": "ANA", "los angeles dodgers": "LAN",
    "miami marlins": "MIA", "milwaukee brewers": "MIL",
    "minnesota twins": "MIN", "new york mets": "NYN",
    "new york yankees": "NYA", "oakland athletics": "OAK",
    "philadelphia phillies": "PHI", "pittsburgh pirates": "PIT",
    "san diego padres": "SDN", "san francisco giants": "SFN",
    "seattle mariners": "SEA", "st. louis cardinals": "SLN",
    "st louis cardinals": "SLN", "tampa bay rays": "TBA",
    "texas rangers": "TEX", "toronto blue jays": "TOR",
    "washington nationals": "WAS",
    # Nicknames
    "diamondbacks": "ARI", "d-backs": "ARI", "braves": "ATL",
    "orioles": "BAL", "o's": "BAL", "red sox": "BOS",
    "cubs": "CHN", "white sox": "CHA",
    "reds": "CIN", "guardians": "CLE",
    "rockies": "COL", "tigers": "DET",
    "astros": "HOU", "royals": "KCA",
    "angels": "ANA", "dodgers": "LAN",
    "marlins": "MIA", "brewers": "MIL",
    "twins": "MIN", "mets": "NYN",
    "yankees": "NYA", "yanks": "NYA",
    "athletics": "OAK", "a's": "OAK",
    "phillies": "PHI", "phils": "PHI",
    "pirates": "PIT", "bucs": "PIT",
    "padres": "SDN", "giants": "SFN",
    "mariners": "SEA", "cardinals": "SLN", "cards": "SLN",
    "rays": "TBA", "rangers": "TEX",
    "blue jays": "TOR", "jays": "TOR",
    "nationals": "WAS", "nats": "WAS",
    # Singular nicknames
    "yankee": "NYA", "dodger": "LAN", "met": "NYN",
    "astro": "HOU", "phillie": "PHI", "padre": "SDN",
    "mariner": "SEA", "brewer": "MIL", "cardinal": "SLN",
    "guardian": "CLE", "oriole": "BAL", "pirate": "PIT",
    "brave": "ATL", "marlin": "MIA", "national": "WAS",
    # Unambiguous cities
    "boston": "BOS", "houston": "HOU", "detroit": "DET",
    "atlanta": "ATL", "baltimore": "BAL", "cincinnati": "CIN",
    "cleveland": "CLE", "colorado": "COL", "milwaukee": "MIL",
    "minnesota": "MIN", "oakland": "OAK", "philadelphia": "PHI",
    "pittsburgh": "PIT", "seattle": "SEA", "tampa bay": "TBA",
    "tampa": "TBA", "texas": "TEX", "toronto": "TOR",
    "washington": "WAS", "miami": "MIA", "arizona": "ARI",
    "san diego": "SDN", "san francisco": "SFN",
    # Standard abbreviations -> Retrosheet codes
    "nyy": "NYA", "nym": "NYN", "chc": "CHN", "chw": "CHA",
    "cws": "CHA", "stl": "SLN", "sfg": "SFN", "sf": "SFN",
    "sd": "SDN", "sdp": "SDN", "lad": "LAN", "laa": "ANA",
    "tb": "TBA", "tbr": "TBA", "kc": "KCA", "kcr": "KCA",
    "wsh": "WAS", "wsn": "WAS",
}

_sorted_team_aliases: list[str] = sorted(team_alias_map.keys(), key=len, reverse=True)


# ---------------------------------------------------------------------------
# Stat definitions (for parseStatDefinition — subset used by backend)
# ---------------------------------------------------------------------------

_stat_definitions: dict[str, str] = {
    "HR": "Home Runs -- the number of times a batter hits the ball over the outfield fence (or circles all bases on a batted ball) for an automatic run.",
    "AVG": "Batting Average -- hits divided by at-bats. A .300 AVG is considered excellent.",
    "RBI": "Runs Batted In -- the number of runs that score as a direct result of a batter's plate appearance (excluding errors and double plays).",
    "OPS": "On-Base Plus Slugging -- OBP + SLG. Combines a batter's ability to get on base and hit for power. An OPS over .900 is elite.",
    "OPS+": "Adjusted OPS -- OPS normalized to 100 (league average), adjusted for ballpark. 150 OPS+ means 50% better than average.",
    "SB": "Stolen Bases -- the number of bases a runner advances by running during a pitch without the ball being batted.",
    "SO": "Strikeouts -- the number of times a batter is called out on strikes or swings and misses for the third strike.",
    "BB": "Walks (Bases on Balls) -- the number of times a batter reaches first base by receiving four balls (pitches outside the strike zone).",
    "OBP": "On-Base Percentage -- (Hits + Walks + HBP) / (At Bats + Walks + HBP + Sac Flies). Measures how often a batter reaches base.",
    "SLG": "Slugging Percentage -- Total Bases / At Bats. Measures a batter's power by weighting extra-base hits.",
    "R": "Runs -- the number of times a player crosses home plate to score.",
    "H": "Hits -- the number of times a batter safely reaches base via a batted ball.",
    "2B": "Doubles -- hits where the batter reaches second base.",
    "3B": "Triples -- hits where the batter reaches third base.",
    "G": "Games -- the number of games in which a player appeared.",
    "ISO": "Isolated Power -- SLG minus AVG. Measures raw extra-base-hit power. An ISO over .200 is excellent.",
    "BABIP": "Batting Average on Balls In Play -- (H - HR) / (AB - SO - HR + SF). Measures batting average excluding home runs and strikeouts.",
    "AB": "At Bats -- plate appearances minus walks, hit-by-pitches, sacrifices, and catcher's interference.",
    "CS": "Caught Stealing -- the number of times a runner is tagged out while attempting to steal a base.",
    "HBP": "Hit By Pitch -- the number of times a batter is hit by a pitched ball and awarded first base.",
    "IBB": "Intentional Walks -- walks issued deliberately by the pitching team.",
    "ERA": "Earned Run Average -- (Earned Runs / Innings Pitched) x 9. The average number of earned runs a pitcher allows per nine innings.",
    "WHIP": "Walks plus Hits per Innings Pitched -- (Walks + Hits) / IP. Measures baserunners allowed. A WHIP under 1.00 is elite.",
    "K/9": "Strikeouts per 9 Innings -- (Strikeouts / IP) x 9. Measures a pitcher's ability to miss bats.",
    "BB/9": "Walks per 9 Innings -- (Walks / IP) x 9. Measures a pitcher's control.",
    "K/BB": "Strikeout-to-Walk Ratio -- Strikeouts / Walks. Higher is better; elite pitchers are above 4.0.",
    "H/9": "Hits per 9 Innings -- (Hits Allowed / IP) x 9.",
    "HR/9": "Home Runs per 9 Innings -- (HR Allowed / IP) x 9.",
    "BAA": "Batting Average Against -- Hits Allowed / At Bats Against. The opponent's batting average against the pitcher.",
    "ERA+": "Adjusted ERA -- ERA normalized to 100 (league average), adjusted for ballpark. An ERA+ of 150 means 50% better than average.",
    "W": "Wins -- credited to the pitcher of record when their team takes and holds the lead.",
    "L": "Losses -- charged to the pitcher who gives up the go-ahead run.",
    "SV": "Saves -- credited to a relief pitcher who finishes a game won by their team under specific conditions (entering with a lead of 3 or fewer runs, etc.).",
    "IP": "Innings Pitched -- the number of outs a pitcher records, divided by 3.",
    "QS": "Quality Starts -- a start of 6+ innings with 3 or fewer earned runs.",
    "CG": "Complete Games -- games where the starting pitcher pitched the entire game.",
    "GF": "Games Finished -- the number of games in which a pitcher was the last pitcher for their team.",
    "WP": "Wild Pitches -- pitches so errant that the catcher cannot catch or control them, allowing runners to advance.",
    "BK": "Balks -- illegal pitching motions that allow runners to advance one base.",
    "BF": "Batters Faced -- the total number of plate appearances against a pitcher.",
    "WAR": "Wins Above Replacement -- an estimate of how many more wins a player provides compared to a replacement-level player. Combines batting, baserunning, fielding, and pitching.",
    "wRC+": "Weighted Runs Created Plus -- a park- and league-adjusted measure of total offensive value. 100 is average; higher is better.",
    "wOBA": "Weighted On-Base Average -- a rate stat that weights each offensive event (single, double, HR, walk, etc.) by its run value.",
    "FIP": "Fielding Independent Pitching -- estimates a pitcher's ERA based only on events they control: strikeouts, walks, HBP, and home runs.",
    "K": "Strikeout -- when a batter accumulates three strikes in a plate appearance.",
    "PA": "Plate Appearances -- the total number of times a batter comes to the plate, including walks, HBP, and sacrifices.",
    "SF": "Sacrifice Flies -- fly balls caught for an out that allow a runner on third to score after the catch.",
    "1B": "Singles -- hits where the batter reaches first base safely.",
}


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

def strip_diacritics(s: str) -> str:
    """Strip diacritics: 'Acuña' -> 'Acuna', 'Ramirez' -> 'Ramirez'."""
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def contains_word(word: str, text: str) -> bool:
    """Check if `word` appears in `text` as a whole word (not a substring of another word)."""
    return bool(re.search(rf'\b{re.escape(word)}\b', text))


def _current_calendar_year() -> int:
    return date.today().year


def _get_db_max_season() -> int:
    """Get max season from the database."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT MAX(season) FROM season_batting_stats")
        row = cur.fetchone()
        conn.close()
        if row and row[0]:
            return int(row[0])
    except Exception:
        pass
    return date.today().year


# ---------------------------------------------------------------------------
# Player name loading (module-level cache)
# ---------------------------------------------------------------------------

sorted_names: list[str] = []
last_name_index: dict[str, list[str]] = {}
first_name_index: dict[str, list[str]] = {}
name_exact_lookup: dict[str, str] = {}


def _load_names() -> None:
    """Load player names from the database and build lookup structures."""
    global sorted_names, last_name_index, first_name_index, name_exact_lookup

    names: list[str] = []
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT name FROM players")
        names = [row[0] for row in cur.fetchall() if row[0]]
        conn.close()
    except Exception:
        pass

    # Deduplicate and sort longest first
    unique = list(set(names))
    sorted_names = sorted(unique, key=len, reverse=True)

    # Build last name index
    suffixes = {"jr.", "jr", "sr.", "sr", "ii", "iii", "iv", "v"}
    index: dict[str, list[str]] = {}
    fn_index: dict[str, list[str]] = {}

    for name in sorted_names:
        parts = name.split()
        if not parts:
            continue
        # Walk backwards past suffixes to find the real last name
        last_idx = len(parts) - 1
        while last_idx > 0 and parts[last_idx].lower() in suffixes:
            last_idx -= 1
        raw_key = parts[last_idx].lower()
        ascii_key = strip_diacritics(raw_key)

        # Add under both accented and ASCII keys
        index.setdefault(raw_key, []).append(name)
        if ascii_key != raw_key:
            index.setdefault(ascii_key, []).append(name)

        # Hyphenated names: also index without hyphen
        if "-" in raw_key:
            no_hyphen = raw_key.replace("-", "")
            index.setdefault(no_hyphen, []).append(name)
        if "-" in ascii_key:
            no_hyphen = ascii_key.replace("-", "")
            if no_hyphen not in index or name not in index[no_hyphen]:
                index.setdefault(no_hyphen, []).append(name)

        # Build first name index
        if len(parts) >= 2:
            first = strip_diacritics(parts[0].lower())
            fn_index.setdefault(first, []).append(name)

    # Cross-link trailing-e variants: "green" <-> "greene"
    all_keys = list(index.keys())
    for key in all_keys:
        if key.endswith("e"):
            variant = key[:-1]
        else:
            variant = key + "e"
        if variant in index:
            # Merge variant's players into this key
            for player in index[variant]:
                if player not in index.get(key, []):
                    index.setdefault(key, []).append(player)
            # And this key's players into variant
            for player in index.get(key, []):
                if player not in index.get(variant, []):
                    index.setdefault(variant, []).append(player)

    last_name_index = index
    first_name_index = fn_index

    # Build exact lookup: ASCII-lowered -> canonical name
    # sorted_names is longest first, so accented names win over ASCII versions
    # (e.g., "José Ramírez" over "Jose Ramirez")
    # When duplicates exist with same length, prefer one with more career games
    exact: dict[str, str] = {}
    for name in sorted_names:
        key = strip_diacritics(name.lower())
        if key not in exact:
            exact[key] = name
    name_exact_lookup = exact


# Load on import
_load_names()


# ---------------------------------------------------------------------------
# Helper: DB queries for leaderboard "since" resolution
# ---------------------------------------------------------------------------

def _lookup_post_career_year(name: str) -> Optional[int]:
    """Return the year after a player's last season."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        sanitized = name.replace("'", "''")
        cur.execute(f"""
            SELECT MAX(season) FROM (
                SELECT season FROM season_batting_stats s
                JOIN players p ON s.player_id = p.player_id WHERE p.name = '{sanitized}'
                UNION ALL
                SELECT season FROM season_pitching_stats sp
                JOIN players p ON sp.player_id = p.player_id WHERE p.name = '{sanitized}'
            )
        """)
        row = cur.fetchone()
        conn.close()
        if row and row[0]:
            return int(row[0]) + 1
    except Exception:
        pass
    return None


def _lookup_last_threshold_year(name: str, stat: StatInfo, threshold: float) -> Optional[int]:
    """Return the year after the player last achieved a stat threshold."""
    lower_is_better = stat.db_column in ("era", "whip", "bb_per_9", "hits_per_9", "hr_per_9")
    comparison = "<=" if lower_is_better else ">="
    table = "season_pitching_stats" if is_pitching_stat(stat) else "season_batting_stats"
    sanitized = name.replace("'", "''")
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute(f"""
            SELECT MAX(s.season) FROM {table} s
            JOIN players p ON s.player_id = p.player_id
            WHERE p.name = '{sanitized}' AND s.{stat.db_column} {comparison} {threshold}
        """)
        row = cur.fetchone()
        conn.close()
        if row and row[0]:
            return int(row[0]) + 1
    except Exception:
        pass
    return _lookup_post_career_year(name)


# ---------------------------------------------------------------------------
# Prominence sorting
# ---------------------------------------------------------------------------

def _is_current_player(last_season: int, current_year: int, current_month: int) -> bool:
    """Whether a player should be treated as 'current' for disambiguation.

    Window is roughly 1 year + half a season — covers normal returners
    plus injured players (Tommy John, etc.) who miss a full season:
    - played this year or last year → current
    - played 2 years ago AND we're still pre-August of this year → current
    - otherwise → not current

    Example: in April 2026, a player whose last_season is 2024 is still
    "current" (likely returning from injury). By August 2026, they're not.
    """
    if last_season >= current_year - 1:
        return True
    if last_season == current_year - 2 and current_month <= 7:
        return True
    return False


def _sort_by_prominence(names: list[str]) -> tuple[list[str], Optional[int]]:
    """Sort player names by pre-computed prominence_score from players table.

    Returns (sorted_names, dominant_index).
    Score is computed by the pipeline: career_games + pitching_weight + awards * 500.
    """
    if len(names) <= 1:
        return (names, 0 if names else None)

    today = date.today()
    current_year = today.year
    current_month = today.month
    infos: list[tuple[str, int, int]] = []

    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        for name in names:
            sanitized = name.replace("'", "''")
            cur.execute(f"""
                SELECT COALESCE(last_season, 0), COALESCE(prominence_score, 0)
                FROM players WHERE name = '{sanitized}'
            """)
            row = cur.fetchone()
            if row:
                infos.append((name, int(row[0]), int(row[1])))
            else:
                infos.append((name, 0, 0))
        conn.close()
    except Exception:
        return (names, None)

    # Sort: current players first, then by prominence score descending
    sorted_infos = sorted(infos,
                          key=lambda x: (_is_current_player(x[1], current_year, current_month), x[2]),
                          reverse=True)

    current_players = [i for i in sorted_infos
                       if _is_current_player(i[1], current_year, current_month)]
    dominant_index: Optional[int] = None
    if len(current_players) == 1:
        dominant_index = 0
    elif len(current_players) >= 2 and current_players[0][2] >= current_players[1][2] * 3:
        dominant_index = 0
    elif len(sorted_infos) >= 2 and sorted_infos[0][2] >= sorted_infos[1][2] * 5:
        dominant_index = 0

    return ([i[0] for i in sorted_infos], dominant_index)


# ---------------------------------------------------------------------------
# Core matching functions
# ---------------------------------------------------------------------------

def _resolve_embedded_name(name: str) -> str:
    """For Sr./Jr. pairs, prefer Jr. for embedded name extraction."""
    lower = name.lower()
    if lower in disambig_sr_jr_map:
        return disambig_sr_jr_map[lower][0]
    return name


def _normalize_suffix(input_str: str) -> str:
    """Normalize suffix variants: 'jr' -> 'Jr.', 'sr' -> 'Sr.'."""
    parts = input_str.split()
    if len(parts) < 2:
        return input_str
    last = parts[-1].lower()
    if last in ("jr", "sr"):
        return " ".join(parts[:-1]) + " " + last.capitalize() + "."
    return input_str


def find_player_in_text(text: str) -> Optional[str]:
    """Find a player name embedded in text. Skips common_word_last_names for single last names."""
    lower = text.lower()

    # Check nickname aliases first (longest first)
    for alias in sorted(nickname_aliases.keys(), key=len, reverse=True):
        if contains_word(alias, lower):
            return nickname_aliases[alias]

    # Check full names (longest first, already sorted)
    # Try both exact and accent-stripped matching
    ascii_lower = strip_diacritics(lower)
    for name in sorted_names:
        name_lower = name.lower()
        if contains_word(name_lower, lower) or contains_word(strip_diacritics(name_lower), ascii_lower):
            # Check for duplicate full names — pick most prominent
            ascii_name = strip_diacritics(name_lower)
            last_part = ascii_name.split()[-1] if " " in ascii_name else ascii_name
            candidates = last_name_index.get(last_part, [])
            same_full = [c for c in candidates if strip_diacritics(c.lower()) == ascii_name]
            if len(same_full) > 1:
                sorted_players, dominant = _sort_by_prominence(same_full)
                if dominant is not None:
                    return _resolve_embedded_name(sorted_players[dominant])
            return _resolve_embedded_name(name)

    # Try unambiguous last name
    for last_name, players in last_name_index.items():
        if contains_word(last_name, lower) and len(players) == 1 \
                and last_name not in common_word_last_names \
                and last_name not in _MONTH_WORDS \
                and last_name not in _DURATION_WORDS:
            return _resolve_embedded_name(players[0])

    # Ambiguous last name — pick most prominent (e.g. "Judge" → Aaron Judge over Joe Judge)
    for last_name, players in last_name_index.items():
        if contains_word(last_name, lower) and len(players) > 1 \
                and last_name not in common_word_last_names \
                and last_name not in _MONTH_WORDS \
                and last_name not in _DURATION_WORDS:
            sorted_players, _ = _sort_by_prominence(players)
            if sorted_players:
                return sorted_players[0]

    return None


# Month names are far more often a date reference than a player surname
# (Don August, Lee/Carlos May). A BARE month word must not resolve to the
# player — "OPS leaders in August" is a month leaderboard, not Don August.
# Full names ("Don August stats") still match via the full-name loop above.
_MONTH_WORDS = {
    "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december",
}

# Duration units / window words are far more often a time-window reference
# ("over the last 2 weeks", "best 13 game span") than a surname — bare "weeks"
# must not resolve to Rickie Weeks, nor "span" to Denard Span. Full names still
# match via the full-name loop above.
_DURATION_WORDS = {
    "day", "days", "week", "weeks", "month", "months", "year", "years",
    "span", "spans", "stretch", "stretches",
}


def match_player(text: str) -> Optional[str]:
    """Direct search for player name. No common-word filtering."""
    trimmed = text.strip()
    if not trimmed:
        return None
    # Strip possessive 's
    lower = trimmed.lower()
    if lower.endswith("'s") or lower.endswith("\u2019s"):
        lower = lower[:-2]
    elif lower.endswith("s'") or lower.endswith("s\u2019"):
        lower = lower[:-1]
    # Also try without trailing 's' for possessives like "stantons"
    lower_no_s = lower.rstrip("s") if lower != lower.rstrip("s") else None

    # Check nickname/alias map first
    if lower in nickname_aliases:
        return nickname_aliases[lower]

    # Sr./Jr. pairs -> return None so disambiguation triggers
    if lower in disambig_sr_jr_map:
        return None

    # Exact full name match (case/accent-insensitive)
    ascii_lower = strip_diacritics(lower)
    if ascii_lower in name_exact_lookup:
        match = name_exact_lookup[ascii_lower]
        is_single_word = " " not in match
        lookup_key = strip_diacritics(match.split()[-1].lower())
        if not is_single_word:
            # Check for multiple players with same full name
            candidates = last_name_index.get(lookup_key, [])
            same_full = [c for c in candidates if strip_diacritics(c.lower()) == ascii_lower]
            if len(same_full) > 1:
                # Multiple players with same name — pick most prominent in-place.
                # MUST sort here directly, not via match_player_with_prominence,
                # because that function calls match_player which would re-enter this
                # exact-match branch and recurse forever. RecursionError happens
                # only when a starter tonight has a duplicated name in the DB
                # (rare — surfaced by matchup_preview during a redetect on 5/4).
                sorted_full, _ = _sort_by_prominence(same_full)
                if sorted_full:
                    return sorted_full[0]
            return match
        elif len(last_name_index.get(lookup_key, [])) <= 1:
            return match

    # Try with normalized suffix
    normalized = _normalize_suffix(trimmed).lower()
    normalized_ascii = strip_diacritics(normalized)
    if normalized_ascii != ascii_lower and normalized_ascii in name_exact_lookup:
        return name_exact_lookup[normalized_ascii]

    # "LastName Jr/Sr" pattern
    suffix_patterns = [("jr", "jr."), ("jr.", "jr."), ("sr", "sr."), ("sr.", "sr."),
                       ("ii", "ii"), ("iii", "iii")]
    for suffix, normalized_suffix in suffix_patterns:
        if lower.endswith(f" {suffix}"):
            base_name = strip_diacritics(lower[:-(len(suffix) + 1)])
            candidates = last_name_index.get(base_name, [])
            with_suffix = [c for c in candidates if c.lower().endswith(normalized_suffix)]
            if len(with_suffix) == 1:
                return with_suffix[0]

    # Last name only — must be unambiguous
    last_name_key = strip_diacritics(lower).replace(" ", "-")
    has_last_name_matches = False
    for key in [lower, ascii_lower, last_name_key, ascii_lower.replace(" ", "")]:
        matches = last_name_index.get(key, [])
        if len(matches) == 1:
            return matches[0]
        if matches:
            has_last_name_matches = True

    # First name only — but only if no last name matches exist.
    # If there ARE last name matches (multiple), the user likely meant the
    # last name and should go through prominence-based disambiguation instead
    # of matching a different player by first name (e.g., "Webb" should match
    # Logan Webb by prominence, not Webb Schultz by first name).
    if not has_last_name_matches:
        fn_matches = first_name_index.get(ascii_lower, [])
        if len(fn_matches) == 1:
            return fn_matches[0]

    # Try without trailing 's' for possessives like "stantons" → "stanton"
    if lower_no_s and lower_no_s != lower:
        return match_player(lower_no_s)

    return None


def match_player_with_prominence(text: str) -> Optional[tuple[str, list[str]]]:
    """Match a player name with prominence-based disambiguation.

    Fame list takes priority: if exactly one candidate is on the fame list,
    auto-resolve to them (others become "see also"). If multiple fame list
    candidates match, disambiguate among all candidates with fame-listed
    ones first. Falls back to standard prominence sorting.

    Returns (matched_name, alternatives) or None.
    """
    matched = match_player(text)
    if matched:
        return (matched, [])
    lower = strip_diacritics(text.strip().lower())
    candidates = last_name_index.get(lower, [])
    # For multi-word names like "de la cruz", try the last word ("cruz")
    # but filter to only players whose name actually contains the full phrase
    if not candidates and " " in lower:
        last_word = lower.split()[-1]
        all_with_last = last_name_index.get(last_word, [])
        candidates = [c for c in all_with_last if lower in strip_diacritics(c.lower())]
    if len(candidates) > 1:
        # Check fame list — if exactly one candidate is famous, auto-resolve
        famous = [c for c in candidates if c in fame_list]
        if len(famous) == 1:
            others = [c for c in candidates if c != famous[0]]
            sorted_others, _ = _sort_by_prominence(others)
            return (famous[0], sorted_others)

        # Multiple famous or none — fall back to prominence sort
        # but put fame-listed candidates first within the sort
        sorted_names_list, _ = _sort_by_prominence(candidates)
        if sorted_names_list:
            # Re-sort: fame-listed first, then rest, preserving prominence order within each group
            fame_sorted = [n for n in sorted_names_list if n in fame_list]
            non_fame = [n for n in sorted_names_list if n not in fame_list]
            final = fame_sorted + non_fame
            return (final[0], final[1:])
    return None


def match_stat(input_str: str) -> Optional[StatInfo]:
    """Find a stat keyword in the input string. Longest alias wins."""
    lower = input_str.lower()

    # Special cases where longest-match picks the wrong stat
    # "win/won N games" → wins (not games)
    if re.search(r'\b(?:win|won|winning)\b', lower) and re.search(r'\bgames?\b', lower):
        wins_stat = stat_alias_map.get("wins")
        if wins_stat:
            return wins_stat

    # "hit N HR/home runs" → home runs (not hits)
    if re.search(r'\bhit\b', lower) and re.search(r'\b(?:hr|home run|homer)', lower):
        hr_stat = stat_alias_map.get("home runs") or stat_alias_map.get("hr")
        if hr_stat:
            return hr_stat

    # "hit/bat/batted .300" → batting_avg (not hits/AB).
    # The verb-form rate value pattern is unambiguous: a leading dot signals
    # a rate (.300 = .300 AVG), distinct from a counting threshold (300 hits).
    # Without this rule, "hit" greedy-matches the hits stat and queries like
    # "how many seasons has Judge hit .300" return "0.3+ hits in 11 seasons."
    # Only fires when no longer/more-specific stat keyword (HR, RBI, etc.) is
    # in the query — those win via the existing length-priority alias scan.
    if re.search(r'\b(?:bat|batted|batting|hit|hitting)\s+(?:over|above|under|below|sub)?\s*\.\d{2,3}', lower):
        # Quick check: is there a more specific stat keyword in the query?
        # If yes, defer to the standard alias scan so compound queries like
        # "Judge hit 30 HR with .300 AVG" still resolve to home_runs as the
        # primary stat. Only override when the bare-rate context is the only
        # signal in the query.
        has_specific_keyword = any(kw in lower for kw in (
            "home run", "homer", " hr", "rbi", " sb", "stolen base",
            "strikeout", " k ", "walk", " bb", "double", "triple",
            "innings", "save", "era", "whip", "obp", "slg", " ops"
        ))
        if not has_specific_keyword:
            avg_stat = stat_alias_map.get("batting average")
            if avg_stat:
                return avg_stat

    # "walked" → walks (BB)
    if re.search(r'\bwalked\b', lower):
        walks_stat = stat_alias_map.get("walks") or stat_alias_map.get("bb")
        if walks_stat:
            return walks_stat

    # "K" alone → strikeouts, but only in stat contexts (after numbers or "most")
    if re.search(r'(?:\d+\+?\s*k\b|most k\b|fewest k\b)', lower) and "k/" not in lower:
        k_stat = stat_alias_map.get("strikeouts") or stat_alias_map.get("ks")
        if k_stat:
            return k_stat

    # "(first|last) N games/at-bats/starts" — these are scope markers for
    # the career-game-window leaderboard, not stat asks. Strip them from
    # the alias scan so a longer stat keyword in the same query (OPS, AVG,
    # HR) still wins. Bare "first 50 games" with no other stat will fall
    # through and the leaderboard executor's default-stat rule applies.
    _window_phrase = re.search(
        r'\b(?:first|last)\s+\d+\s+(?:career\s+)?'
        r'(games?|starts?|at[- ]?bats?|abs?|appearances)',
        lower,
    )
    window_skip: set[str] = set()
    if _window_phrase:
        window_skip = {"games", "game", "at-bats", "at bats", "ab",
                        "abs", "starts", "start", "at-bat", "at bat",
                        "appearances", "appearance"}

    for alias in _sorted_stat_aliases:
        if alias in window_skip:
            continue
        if contains_word(alias, lower):
            return stat_alias_map[alias]
    # Handle split phrases like "stolen 60 bases" -> "stolen bases"
    without_numbers = re.sub(r'\d+\.?\d*', ' ', lower)
    without_numbers = re.sub(r'\s+', ' ', without_numbers).strip()
    if without_numbers != lower:
        for alias in _sorted_stat_aliases:
            if alias in window_skip:
                continue
            if contains_word(alias, without_numbers):
                return stat_alias_map[alias]
    # Fallback — generic "best hitter" / "top batter" phrasings default to OPS
    # only if no specific stat was matched above. This keeps "top batters by
    # strikeouts" from getting hijacked into OPS by the longer generic phrase.
    for alias in _sorted_fallback_aliases:
        if contains_word(alias, lower):
            return stat_fallback_alias_map[alias]
    return None


def detect_season(input_str: str, default_to_most_recent: bool = False) -> Optional[int]:
    """Extract a season year from input text."""
    lower = input_str.lower()

    # Explicit 4-digit year (1898-2029)
    m = re.search(r'\b(189[89]|19\d{2}|20[0-2]\d)\b', lower)
    if m:
        return int(m.group(1))

    current_year = _current_calendar_year()

    relative_patterns: list[tuple[list[str], int]] = [
        (["this year", "this season", "current season"], 0),
        (["last year", "last season", "previous season", "prior season"], -1),
        (["two years ago", "2 years ago"], -2),
        (["three years ago", "3 years ago"], -3),
    ]
    for patterns, offset in relative_patterns:
        if any(p in lower for p in patterns):
            return current_year + offset

    if default_to_most_recent:
        return current_year

    return None


def _detect_since_year(lower: str) -> Optional[int]:
    """Detect 'since YYYY', 'this century', 'last decade', 'last N years' patterns.
    Returns the starting year, or None if no range qualifier found."""
    current_year = _current_calendar_year()
    if "this century" in lower or "21st century" in lower:
        return 2000
    if "this decade" in lower:
        return current_year - (current_year % 10)  # 2026 → 2020
    # "in/over/for the last decade" = rolling 10 years
    # "last decade" (standalone) = the prior named decade (2010s)
    if re.search(r'\b(?:in|over|for|during)\s+the\s+last\s+decade', lower):
        return current_year - 10  # rolling 10 years
    if "past decade" in lower:
        return current_year - 10  # rolling 10 years
    if "last decade" in lower:
        return current_year - (current_year % 10) - 10  # named decade: 2026 → 2010
    m = re.search(r'since\s+(\d{4})', lower)
    if m:
        return int(m.group(1))
    m = re.search(r'(?:last|past)\s+(\d+)\s+years?', lower)
    if m:
        n = int(m.group(1))
        if 1 < n <= 100:
            return current_year - n
    return None


def _detect_rookie(lower: str) -> bool:
    """Detect if the query is asking about rookies."""
    rookie_triggers = ["rookie", "rookies", "first year", "first-year"]
    return any(t in lower for t in rookie_triggers)


# Position keyword → fielding position code(s)
_POSITION_MAP = {
    "catcher": ["C"], "catchers": ["C"],
    "first baseman": ["1B"], "first base": ["1B"], "at first": ["1B"],
    "second baseman": ["2B"], "second base": ["2B"], "at second": ["2B"],
    "third baseman": ["3B"], "third base": ["3B"], "at third": ["3B"],
    "shortstop": ["SS"], "shortstops": ["SS"],
    "left fielder": ["LF"], "left field": ["LF"],
    "center fielder": ["CF"], "center field": ["CF"],
    "right fielder": ["RF"], "right field": ["RF"],
    "outfielder": ["LF", "CF", "RF"], "outfielders": ["LF", "CF", "RF"],
    "designated hitter": ["DH"], "dh": ["DH"],
    "pitcher": ["P"], "pitchers": ["P"],
    "infielder": ["1B", "2B", "3B", "SS"], "infielders": ["1B", "2B", "3B", "SS"],
}


def _detect_position(lower: str) -> Optional[list[str]]:
    """Detect position filter in query. Returns list of position codes or None.
    Checks longest keywords first to avoid partial matches."""
    for keyword in sorted(_POSITION_MAP.keys(), key=len, reverse=True):
        if keyword in lower:
            return _POSITION_MAP[keyword]
    return None


def _detect_pitcher_role(lower: str) -> Optional[str]:
    """Detect starter/reliever/closer filter. Returns 'starter', 'reliever', or None."""
    starter_triggers = ["starter", "starters", "starting pitcher", "starting pitchers"]
    reliever_triggers = ["reliever", "relievers", "relief pitcher", "relief pitchers",
                         "closer", "closers", "bullpen", "out of the bullpen"]
    for t in starter_triggers:
        if t in lower:
            return "starter"
    for t in reliever_triggers:
        if t in lower:
            return "reliever"
    return None


@dataclass(frozen=True)
class TeamContextFilter:
    """A team-game-context filter for JOIN against team_game_results.

    Unlike SplitContext (which routes to a specialized split table), this
    filter applies to per-game aggregates derived from game_batting_logs /
    game_pitching_logs JOINed to team_game_results on (date, opponent,
    is_home). Examples: "in extra innings", "in day games", "in rain
    games", "when team had won at least 40 games at the time".

    sql_clause: extra WHERE fragment referencing the `tgr` alias.
                 Use `?` placeholders matched by sql_params.
    """
    label: str
    sql_clause: str
    sql_params: tuple
    consumed_phrases: list


# Fixed-phrase team-context filters. Parametric ones ("team had won 40+
# games") are detected separately in _detect_team_context.
_TEAM_CONTEXT_FIXED = {
    # NOTE: "extra innings" filter removed — Retrosheet's gameinfo.csv stores
    # SCHEDULED innings (always 9), not actual final-inning counts. To
    # support this we'd need plays.csv (not currently loaded). MSF data
    # for 2025+ does have actual innings via score.innings[] array length,
    # but supporting only recent seasons would mislead users for any
    # historical query, so it's all-or-nothing.

    # Day / night — match with or without leading "in"/"during" so phrasings
    # like "most day game home runs" work alongside "in day games".
    "in day games": TeamContextFilter(
        "Day Games", "tgr.daynight = 'day'", (), ["in day games"]),
    "during day games": TeamContextFilter(
        "Day Games", "tgr.daynight = 'day'", (), ["during day games"]),
    "day games": TeamContextFilter(
        "Day Games", "tgr.daynight = 'day'", (), ["day games"]),
    "day game": TeamContextFilter(
        "Day Games", "tgr.daynight = 'day'", (), ["day game"]),
    "in night games": TeamContextFilter(
        "Night Games", "tgr.daynight = 'night'", (), ["in night games"]),
    "during night games": TeamContextFilter(
        "Night Games", "tgr.daynight = 'night'", (), ["during night games"]),
    "night games": TeamContextFilter(
        "Night Games", "tgr.daynight = 'night'", (), ["night games"]),
    "night game": TeamContextFilter(
        "Night Games", "tgr.daynight = 'night'", (), ["night game"]),

    # Weather (free-text match — reasonable hits across MSF + Retrosheet rows)
    "in rain games": TeamContextFilter(
        "Rain Games", "LOWER(tgr.weather) LIKE '%rain%'", (), ["in rain games", "in the rain"]),
    "in cold weather": TeamContextFilter(
        "Cold Weather", "tgr.weather IS NOT NULL AND CAST(SUBSTR(tgr.weather, INSTR(tgr.weather, ',')+1) AS INTEGER) > 0 AND CAST(SUBSTR(tgr.weather, INSTR(tgr.weather, ',')+1) AS INTEGER) < 50",
        (), ["in cold weather", "cold weather games"]),
    "in hot weather": TeamContextFilter(
        "Hot Weather", "tgr.weather IS NOT NULL AND CAST(SUBSTR(tgr.weather, INSTR(tgr.weather, ',')+1) AS INTEGER) >= 85",
        (), ["in hot weather", "hot weather games"]),

    # Team record context (uses wins_before/losses_before computed inline)
    "with team above .500": TeamContextFilter(
        "Team Above .500",
        "(tgr.wins_after - CASE WHEN tgr.result='W' THEN 1 ELSE 0 END) > "
        "(tgr.losses_after - CASE WHEN tgr.result='L' THEN 1 ELSE 0 END)",
        (), ["with team above .500", "team above .500"]),
    "with a winning record": TeamContextFilter(
        "Team Above .500",
        "(tgr.wins_after - CASE WHEN tgr.result='W' THEN 1 ELSE 0 END) > "
        "(tgr.losses_after - CASE WHEN tgr.result='L' THEN 1 ELSE 0 END)",
        (), ["with a winning record", "while team was winning"]),
    "with team below .500": TeamContextFilter(
        "Team Below .500",
        "(tgr.wins_after - CASE WHEN tgr.result='W' THEN 1 ELSE 0 END) < "
        "(tgr.losses_after - CASE WHEN tgr.result='L' THEN 1 ELSE 0 END)",
        (), ["with team below .500", "team below .500"]),
    "with a losing record": TeamContextFilter(
        "Team Below .500",
        "(tgr.wins_after - CASE WHEN tgr.result='W' THEN 1 ELSE 0 END) < "
        "(tgr.losses_after - CASE WHEN tgr.result='L' THEN 1 ELSE 0 END)",
        (), ["with a losing record", "while team was losing"]),
}


def _detect_team_context(lower: str) -> Optional[TeamContextFilter]:
    """Detect a team-game-context filter in the query. Checks fixed phrases
    first, then parametric patterns like "team had won at least N games".

    Returns a TeamContextFilter or None.
    """
    # Fixed phrases — longest match wins to avoid partial overlaps
    for phrase in sorted(_TEAM_CONTEXT_FIXED.keys(), key=len, reverse=True):
        if phrase in lower:
            return _TEAM_CONTEXT_FIXED[phrase]

    # Parametric: "team had won at least N games" / "after team won N" /
    # "when team had won X"
    win_threshold_patterns = [
        r"team\s+had\s+won\s+at\s+least\s+(\d+)\s+games?",
        r"after\s+(?:the\s+)?team\s+(?:had\s+)?won\s+(\d+)\s+games?",
        r"when\s+(?:the\s+)?team\s+(?:had\s+)?won\s+(\d+)\s+games?",
        r"team\s+(?:had\s+)?won\s+(\d+)\+\s+games?",
        r"team\s+with\s+(\d+)\+\s+wins",
    ]
    for pat in win_threshold_patterns:
        m = re.search(pat, lower)
        if m:
            n = int(m.group(1))
            return TeamContextFilter(
                label=f"Team Had Won {n}+",
                sql_clause="(tgr.wins_after - CASE WHEN tgr.result='W' THEN 1 ELSE 0 END) >= ?",
                sql_params=(n,),
                consumed_phrases=[m.group(0)],
            )

    # Symmetric "team had lost at least N"
    loss_threshold_patterns = [
        r"team\s+had\s+lost\s+at\s+least\s+(\d+)\s+games?",
        r"after\s+(?:the\s+)?team\s+(?:had\s+)?lost\s+(\d+)\s+games?",
        r"when\s+(?:the\s+)?team\s+(?:had\s+)?lost\s+(\d+)\s+games?",
    ]
    for pat in loss_threshold_patterns:
        m = re.search(pat, lower)
        if m:
            n = int(m.group(1))
            return TeamContextFilter(
                label=f"Team Had Lost {n}+",
                sql_clause="(tgr.losses_after - CASE WHEN tgr.result='L' THEN 1 ELSE 0 END) >= ?",
                sql_params=(n,),
                consumed_phrases=[m.group(0)],
            )

    return None


@dataclass(frozen=True)
class SplitContext:
    """Describes a split-table filter for leaderboard queries."""
    table: str           # e.g. "count_batting_splits"
    filter_col: Optional[str]  # e.g. "count_state"; None when the table is already a single split (e.g. first_pa_batting_splits)
    filter_values: list  # e.g. ["0-2", "1-2", "2-2", "3-2"]; empty list when filter_col is None
    label: str           # e.g. "With 2 Strikes"
    # Which words in the query this context consumed (for unexplained-word detection)
    consumed_phrases: list
    # True when the split is sourced from a pitching split table (e.g. pitching_tto_splits).
    # Drives the executor's column choice (_against rate cols), sort direction (ASC for rate
    # stats), and qualifier table (season_pitching_stats).
    is_pitching: bool = False
    # Optional cross-side pivot — full query phrase rendered as a [SUGGEST] pill
    # under the leaderboard so users can flip between "best hitters with 2 strikes"
    # and "best pitchers with 2 strikes". Set only when a counterpart split table
    # exists (no first_pa_pitching_splits, so first-PA entries leave this None).
    pivot_phrase: Optional[str] = None


# Game-window qualifier regex shared by parsers that should DECLINE when a
# query includes a "first/last N games|starts|at-bats|appearances|innings"
# clause. Matching parsers don't model the window and would silently drop
# the qualifier, returning stale full-season stats instead. Declining here
# lets the chain fall through to the query engine / Haiku / Sonnet, which
# can handle the window structurally or via SQL. Includes spelled-out
# numbers for voice input.
_GW_NUM = (
    r'(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|'
    r'eleven|twelve|thirteen|fourteen|fifteen|twenty|thirty|forty|fifty|sixty)'
)
_GW_DIRECTION = r'(?:first|last|past|previous|recent|opening|final)'
_GW_NOUN = r'(?:games?|starts?|at[- ]?bats?|appearances?|innings?)'
_GAME_WINDOW_RX = rf'\b{_GW_DIRECTION}\s+{_GW_NUM}\s+(?:career\s+)?{_GW_NOUN}\b'


# Map of split trigger phrases → SplitContext
#
# Note on pitching variants: Stage 2 added explicit "pitchers <phrase>" entries
# that route to the corresponding *_pitching_splits tables. This is "Option 2"
# from the design discussion — explicit phrasing instead of inferred routing.
# The pivot_phrase on each entry feeds a [SUGGEST] pill in the leaderboard so
# users can flip between sides without retyping the whole query.
_SPLIT_CONTEXTS = {
    # Count-based — batting
    "with 2 strikes": SplitContext("count_batting_splits", "count_state", ["0-2", "1-2", "2-2", "3-2"], "With 2 Strikes", ["with 2 strikes", "with two strikes"], pivot_phrase="best pitchers with 2 strikes"),
    "with two strikes": SplitContext("count_batting_splits", "count_state", ["0-2", "1-2", "2-2", "3-2"], "With 2 Strikes", ["with two strikes"], pivot_phrase="best pitchers with 2 strikes"),
    "2-strike": SplitContext("count_batting_splits", "count_state", ["0-2", "1-2", "2-2", "3-2"], "With 2 Strikes", ["2-strike", "two-strike"], pivot_phrase="best pitchers with 2 strikes"),
    "two-strike": SplitContext("count_batting_splits", "count_state", ["0-2", "1-2", "2-2", "3-2"], "With 2 Strikes", ["two-strike"], pivot_phrase="best pitchers with 2 strikes"),
    "full count": SplitContext("count_batting_splits", "count_state", ["3-2"], "Full Count", ["full count"], pivot_phrase="best pitchers in full count"),
    "0-2 count": SplitContext("count_batting_splits", "count_state", ["0-2"], "0-2 Count", ["0-2 count"], pivot_phrase="best pitchers in 0-2 count"),
    "3-2 count": SplitContext("count_batting_splits", "count_state", ["3-2"], "3-2 Count", ["3-2 count"], pivot_phrase="best pitchers in 3-2 count"),
    "3-0 count": SplitContext("count_batting_splits", "count_state", ["3-0"], "3-0 Count", ["3-0 count"], pivot_phrase="best pitchers in 3-0 count"),
    "ahead in the count": SplitContext("count_batting_splits", "count_state", ["0-0", "1-0", "2-0", "3-0", "2-1", "3-1", "3-2"], "Ahead in Count", ["ahead in the count"], pivot_phrase="best pitchers ahead in the count"),
    "behind in the count": SplitContext("count_batting_splits", "count_state", ["0-1", "0-2", "1-2"], "Behind in Count", ["behind in the count"], pivot_phrase="best pitchers behind in the count"),
    # Count-based — pitching (Stage 2)
    "pitchers with 2 strikes": SplitContext("count_pitching_splits", "count_state", ["0-2", "1-2", "2-2", "3-2"], "With 2 Strikes", ["pitchers with 2 strikes", "pitchers with two strikes", "pitcher with 2 strikes", "pitcher with two strikes"], is_pitching=True, pivot_phrase="best hitters with 2 strikes"),
    "pitchers in full count": SplitContext("count_pitching_splits", "count_state", ["3-2"], "Full Count", ["pitchers in full count", "pitcher in full count"], is_pitching=True, pivot_phrase="best hitters in full count"),
    "pitchers in 0-2 count": SplitContext("count_pitching_splits", "count_state", ["0-2"], "0-2 Count", ["pitchers in 0-2 count", "pitcher in 0-2 count", "pitchers in 0-2"], is_pitching=True, pivot_phrase="best hitters in 0-2 count"),
    "pitchers in 3-2 count": SplitContext("count_pitching_splits", "count_state", ["3-2"], "3-2 Count", ["pitchers in 3-2 count", "pitcher in 3-2 count", "pitchers in 3-2"], is_pitching=True, pivot_phrase="best hitters in 3-2 count"),
    "pitchers in 3-0 count": SplitContext("count_pitching_splits", "count_state", ["3-0"], "3-0 Count", ["pitchers in 3-0 count", "pitcher in 3-0 count", "pitchers in 3-0"], is_pitching=True, pivot_phrase="best hitters in 3-0 count"),
    "pitchers ahead in the count": SplitContext("count_pitching_splits", "count_state", ["0-0", "1-0", "2-0", "3-0", "2-1", "3-1", "3-2"], "Ahead in Count", ["pitchers ahead in the count", "pitcher ahead in the count"], is_pitching=True, pivot_phrase="best hitters ahead in the count"),
    "pitchers behind in the count": SplitContext("count_pitching_splits", "count_state", ["0-1", "0-2", "1-2"], "Behind in Count", ["pitchers behind in the count", "pitcher behind in the count"], is_pitching=True, pivot_phrase="best hitters behind in the count"),
    # Pitch type — batting
    "against fastball": SplitContext("pitch_type_batting_splits", "pitch_type", ["4-Seam"], "vs Fastballs", ["against fastball", "against fastballs"], pivot_phrase="best pitchers throwing fastballs"),
    "on fastball": SplitContext("pitch_type_batting_splits", "pitch_type", ["4-Seam"], "vs Fastballs", ["on fastball", "on fastballs"], pivot_phrase="best pitchers throwing fastballs"),
    "vs fastball": SplitContext("pitch_type_batting_splits", "pitch_type", ["4-Seam"], "vs Fastballs", ["vs fastball", "vs fastballs"], pivot_phrase="best pitchers throwing fastballs"),
    "against slider": SplitContext("pitch_type_batting_splits", "pitch_type", ["Slider"], "vs Sliders", ["against slider", "against sliders"], pivot_phrase="best pitchers throwing sliders"),
    "on slider": SplitContext("pitch_type_batting_splits", "pitch_type", ["Slider"], "vs Sliders", ["on slider", "on sliders"], pivot_phrase="best pitchers throwing sliders"),
    "vs slider": SplitContext("pitch_type_batting_splits", "pitch_type", ["Slider"], "vs Sliders", ["vs slider", "vs sliders"], pivot_phrase="best pitchers throwing sliders"),
    "against curve": SplitContext("pitch_type_batting_splits", "pitch_type", ["Curve"], "vs Curveballs", ["against curve", "against curveball", "against curveballs"], pivot_phrase="best pitchers throwing curveballs"),
    "on curve": SplitContext("pitch_type_batting_splits", "pitch_type", ["Curve"], "vs Curveballs", ["on curve", "on curveball"], pivot_phrase="best pitchers throwing curveballs"),
    "vs curve": SplitContext("pitch_type_batting_splits", "pitch_type", ["Curve"], "vs Curveballs", ["vs curve", "vs curveball"], pivot_phrase="best pitchers throwing curveballs"),
    "against changeup": SplitContext("pitch_type_batting_splits", "pitch_type", ["Change"], "vs Changeups", ["against changeup", "against changeups"], pivot_phrase="best pitchers throwing changeups"),
    "on changeup": SplitContext("pitch_type_batting_splits", "pitch_type", ["Change"], "vs Changeups", ["on changeup", "on changeups"], pivot_phrase="best pitchers throwing changeups"),
    "vs changeup": SplitContext("pitch_type_batting_splits", "pitch_type", ["Change"], "vs Changeups", ["vs changeup", "vs changeups"], pivot_phrase="best pitchers throwing changeups"),
    "against sinker": SplitContext("pitch_type_batting_splits", "pitch_type", ["Sinker"], "vs Sinkers", ["against sinker", "against sinkers"], pivot_phrase="best pitchers throwing sinkers"),
    "against cutter": SplitContext("pitch_type_batting_splits", "pitch_type", ["Cutter"], "vs Cutters", ["against cutter", "against cutters"], pivot_phrase="best pitchers throwing cutters"),
    # Pitch type — pitching (Stage 2). Semantically "the pitcher's own X" rather
    # than "vs X" — phrasing differs. "Throwing" is unambiguously a pitcher action.
    "throwing fastballs": SplitContext("pitch_type_pitching_splits", "pitch_type", ["4-Seam"], "Throwing Fastballs", ["throwing fastballs", "throwing fastball", "with their fastball", "with their fastballs"], is_pitching=True, pivot_phrase="best hitters against fastballs"),
    "throwing sliders": SplitContext("pitch_type_pitching_splits", "pitch_type", ["Slider"], "Throwing Sliders", ["throwing sliders", "throwing slider", "with their slider", "with their sliders"], is_pitching=True, pivot_phrase="best hitters against sliders"),
    "throwing curveballs": SplitContext("pitch_type_pitching_splits", "pitch_type", ["Curve"], "Throwing Curveballs", ["throwing curveballs", "throwing curveball", "throwing curves", "throwing curve", "with their curveball", "with their curve"], is_pitching=True, pivot_phrase="best hitters against curveballs"),
    "throwing changeups": SplitContext("pitch_type_pitching_splits", "pitch_type", ["Change"], "Throwing Changeups", ["throwing changeups", "throwing changeup", "with their changeup"], is_pitching=True, pivot_phrase="best hitters against changeups"),
    "throwing sinkers": SplitContext("pitch_type_pitching_splits", "pitch_type", ["Sinker"], "Throwing Sinkers", ["throwing sinkers", "throwing sinker", "with their sinker"], is_pitching=True, pivot_phrase="best hitters against sinkers"),
    "throwing cutters": SplitContext("pitch_type_pitching_splits", "pitch_type", ["Cutter"], "Throwing Cutters", ["throwing cutters", "throwing cutter", "with their cutter"], is_pitching=True, pivot_phrase="best hitters against cutters"),
    # RISP — batting
    "with risp": SplitContext("risp_batting_splits", "split", ["RISP"], "With RISP", ["with risp"], pivot_phrase="best pitchers with risp"),
    "runners in scoring": SplitContext("risp_batting_splits", "split", ["RISP"], "With RISP", ["runners in scoring position", "runners in scoring"], pivot_phrase="best pitchers with risp"),
    # Note: "with runners on" is NOT the same as RISP — removed to prevent false matches.
    # We only have RISP splits, not "any runners on base" splits.
    # RISP — pitching (Stage 2)
    "pitchers with risp": SplitContext("risp_pitching_splits", "split", ["RISP"], "With RISP", ["pitchers with risp", "pitcher with risp", "pitchers with runners in scoring position", "pitchers with runners in scoring"], is_pitching=True, pivot_phrase="best hitters with risp"),
    # Home/Away — batting
    "at home": SplitContext("home_away_splits", "split", ["home"], "At Home", ["at home"], pivot_phrase="best pitchers at home"),
    "on the road": SplitContext("home_away_splits", "split", ["away"], "On the Road", ["on the road"], pivot_phrase="best pitchers on the road"),
    "away from home": SplitContext("home_away_splits", "split", ["away"], "On the Road", ["away from home"], pivot_phrase="best pitchers on the road"),
    # Home/Away — pitching (Stage 2). The pitching_home_away_splits table carries
    # ERA/WHIP/K-9/BAA directly (NOT _against rate columns), so OPS-against
    # queries fall through. ERA-class queries work.
    "pitchers at home": SplitContext("pitching_home_away_splits", "split", ["home"], "At Home", ["pitchers at home", "pitcher at home"], is_pitching=True, pivot_phrase="best hitters at home"),
    "pitchers on the road": SplitContext("pitching_home_away_splits", "split", ["away"], "On the Road", ["pitchers on the road", "pitcher on the road"], is_pitching=True, pivot_phrase="best hitters on the road"),
    "pitchers away from home": SplitContext("pitching_home_away_splits", "split", ["away"], "On the Road", ["pitchers away from home", "pitcher away from home"], is_pitching=True, pivot_phrase="best hitters on the road"),
    # First PA of game (batting). Table is already a single bucket — no IN filter needed.
    # Each entry's `consumed_phrases` strips both the trigger phrase AND the surrounding
    # "of games" / "of the game" / "of a game" tail. Otherwise leftover "games" matches G
    # (games played) as the leaderboard stat.
    **{
        f"{lead}{tail}": SplitContext(
            "first_pa_batting_splits", None, [], "in 1st PA of Game",
            [f"{lead}{tail}", lead],  # strip the long-form first, then bare lead just in case
        )
        for lead in [
            "first at bat", "first at-bat", "1st at bat", "1st at-bat",
            "first plate appearance", "1st plate appearance",
        ]
        for tail in [
            " of games", " of the game", " of a game", " of game",
            "s of the game", "s of a game", "s of games",  # plural lead handled below
            "",
        ]
    },
    "first pa": SplitContext("first_pa_batting_splits", None, [], "in 1st PA of Game", ["first pa"]),
    "1st pa": SplitContext("first_pa_batting_splits", None, [], "in 1st PA of Game", ["1st pa"]),
    "first time up": SplitContext("first_pa_batting_splits", None, [], "in 1st PA of Game", ["first time up"]),
    # Platoon
    "against lefties": SplitContext("platoon_splits", "split", ["vs_LHP"], "vs LHP", ["against lefties"]),
    "against righties": SplitContext("platoon_splits", "split", ["vs_RHP"], "vs RHP", ["against righties"]),
    "vs lefties": SplitContext("platoon_splits", "split", ["vs_LHP"], "vs LHP", ["vs lefties"]),
    "vs righties": SplitContext("platoon_splits", "split", ["vs_RHP"], "vs RHP", ["vs righties"]),
    "against left-handed": SplitContext("platoon_splits", "split", ["vs_LHP"], "vs LHP", ["against left-handed", "against left handed"]),
    "against right-handed": SplitContext("platoon_splits", "split", ["vs_RHP"], "vs RHP", ["against right-handed", "against right handed"]),
    "vs left-handed": SplitContext("platoon_splits", "split", ["vs_LHP"], "vs LHP", ["vs left-handed", "vs left handed"]),
    "vs right-handed": SplitContext("platoon_splits", "split", ["vs_RHP"], "vs RHP", ["vs right-handed", "vs right handed"]),
    # Pitching inning splits — pitching_tto_splits-shaped table with `inning`
    # column holding "1".."9" or "10+". Numeric and word-ordinal triggers are
    # in _detect_split_context's parametric branch below; this map carries
    # the bonus phrasings ("extra innings", "in extras") that don't fit a
    # bare ordinal regex.
    "extra innings": SplitContext("pitching_inning_splits", "inning", ["10+"], "Extra Innings", ["extra innings", "in extras"], is_pitching=True),
    "in extras": SplitContext("pitching_inning_splits", "inning", ["10+"], "Extra Innings", ["in extras"], is_pitching=True),
}


def _detect_split_context(lower: str) -> Optional[SplitContext]:
    """Detect split context in query. Checks longest fixed phrases first,
    then parametric patterns (TTO ordinals)."""
    # Subject pivot: "best pitchers against sliders" naturally reads as a
    # batting context ("against X") but the user wants the pitching side.
    # When the query says "pitcher(s)" with no player name AND the matched
    # phrase is a batting pitch-type / vs-handedness split, pivot to the
    # corresponding pitching split table. Same pitch_type filter values;
    # only table + is_pitching change.
    wants_pitcher_subject = bool(re.search(r'\bpitchers?\b', lower)) and not bool(re.search(r'\bhitters?\b|\bbatters?\b', lower))

    for phrase in sorted(_SPLIT_CONTEXTS.keys(), key=len, reverse=True):
        if phrase in lower:
            ctx = _SPLIT_CONTEXTS[phrase]
            # Pivot a batting pitch-type split into its pitching twin when
            # the subject is clearly pitchers. Label stays as "vs Sliders"
            # — works for both pitcher and hitter framings.
            if (wants_pitcher_subject
                    and not ctx.is_pitching
                    and ctx.table == "pitch_type_batting_splits"):
                return SplitContext(
                    table="pitch_type_pitching_splits",
                    filter_col=ctx.filter_col,
                    filter_values=ctx.filter_values,
                    label=ctx.label,
                    consumed_phrases=ctx.consumed_phrases,
                    is_pitching=True,
                    pivot_phrase=ctx.pivot_phrase,
                )
            return ctx

    # Parametric: "Nth time(s) through (the order)?" — pitching split (pitching_tto_splits).
    # Must include "time through" / "times through" so bare ordinals like
    # "third inning" don't false-match.
    if "time through" in lower or "times through" in lower:
        tto: Optional[str] = None
        consumed_text: Optional[str] = None
        # Word ordinals first
        for word, n in _ORDINAL_WORDS.items():
            m = re.search(rf'\b{word}\s+time(?:s)?\s+through(?:\s+the\s+order|\s+the\s+lineup|\s+order|\s+lineup)?\b', lower)
            if m:
                tto = str(n) if n < 4 else "4+"
                consumed_text = m.group(0)
                break
        # Numeric ordinals if no word match
        if tto is None:
            m = re.search(r'\b(\d+)(?:st|nd|rd|th)\s+time(?:s)?\s+through(?:\s+the\s+order|\s+the\s+lineup|\s+order|\s+lineup)?\b', lower)
            if m:
                n = int(m.group(1))
                tto = str(n) if n < 4 else "4+"
                consumed_text = m.group(0)
        if tto and consumed_text:
            ordinal_label = {"1": "1st", "2": "2nd", "3": "3rd", "4+": "4+"}.get(tto, tto)
            noun = "Times" if tto == "4+" else "Time"
            return SplitContext(
                table="pitching_tto_splits",
                filter_col="tto",
                filter_values=[tto],
                label=f"{ordinal_label} {noun} Through the Order",
                consumed_phrases=[consumed_text],
                is_pitching=True,
            )

    # Parametric: "Nth inning" — pitching split (pitching_inning_splits).
    # Must include the bare word "inning" so generic "first" / "third" with
    # no inning context don't false-match. Word ordinals 1-9 (+ "tenth" → 10+),
    # numeric ordinals "1st".."9th", and the plain "inning N" form all map to
    # the inning column ("1".."9" or "10+").
    if "inning" in lower:
        inning_str: Optional[str] = None
        consumed: Optional[str] = None
        # Word ordinals first (e.g. "seventh inning")
        for word, n in _ORDINAL_WORDS.items():
            m = re.search(rf'\b{word}\s+inning\b', lower)
            if m:
                inning_str = str(n) if n < 10 else "10+"
                consumed = m.group(0)
                break
        # Numeric ordinals: "7th inning"
        if inning_str is None:
            m = re.search(r'\b(\d+)(?:st|nd|rd|th)\s+inning\b', lower)
            if m:
                n = int(m.group(1))
                inning_str = str(n) if n < 10 else "10+"
                consumed = m.group(0)
        # Plain "inning 7" form
        if inning_str is None:
            m = re.search(r'\binning\s+(\d+)\b', lower)
            if m:
                n = int(m.group(1))
                inning_str = str(n) if n < 10 else "10+"
                consumed = m.group(0)
        if inning_str and consumed:
            ordinal_label = {
                "1": "1st", "2": "2nd", "3": "3rd",
                "10+": "Extra",
            }.get(inning_str, f"{inning_str}th")
            label = "Extra Innings" if inning_str == "10+" else f"{ordinal_label} Inning"
            return SplitContext(
                table="pitching_inning_splits",
                filter_col="inning",
                filter_values=[inning_str],
                label=label,
                consumed_phrases=[consumed],
                is_pitching=True,
            )
    return None


def detect_league(input_str: str) -> Optional[tuple[Optional[str], str]]:
    """Detect AL/NL league filter. Returns (league, cleaned_text) or None."""
    lower = input_str.lower()

    # "(MLB)" = no league filter, just strip it
    if "(mlb)" in lower:
        cleaned = lower.replace("(mlb)", "").replace("  ", " ").strip()
        return (None, cleaned)

    # Long phrases first
    for phrase, league in [("american league", "AL"), ("national league", "NL")]:
        if phrase in lower:
            cleaned = lower.replace(phrase, "").replace("  ", " ").strip()
            return (league, cleaned)

    # Parenthesized form
    for token, league in [("(al)", "AL"), ("(nl)", "NL")]:
        if token in lower:
            cleaned = lower.replace(token, "").replace("  ", " ").strip()
            return (league, cleaned)

    # Short codes with word-boundary check
    m = re.search(r'\b(al|nl)\b', lower)
    if m:
        code = m.group(1).upper()
        cleaned = lower[:m.start()] + lower[m.end():]
        cleaned = cleaned.replace("  ", " ").strip()
        return (code, cleaned)

    return None


def match_team(input_str: str) -> Optional[str]:
    """Find a team alias in the input. Returns Retrosheet code."""
    lower = input_str.lower()
    for alias in _sorted_team_aliases:
        if contains_word(alias, lower):
            return team_alias_map[alias]
    return None


def match_team_exact(input_str: str) -> Optional[str]:
    """Exact team name match — entire input must be a team alias."""
    lower = input_str.strip().lower()
    if lower.startswith("the "):
        lower = lower[4:]
    return team_alias_map.get(lower)


# ---------------------------------------------------------------------------
# Pitcher detection
# ---------------------------------------------------------------------------

def is_pitcher(name: str) -> bool:
    """Check if a player is primarily a pitcher.

    Mirrors the player profile's rule (player_card._is_pitcher and _is_two_way):
    the player's `positions` field must start with "P" AND they must have at
    least one row in season_pitching_stats, AND they must not be a two-way
    player by the Ohtani rule (any season with PA >= 130 AND any season with
    ip_outs >= 90, mirrored from player_card._is_two_way).

    Using positions field as the authoritative source (consistent with player
    cards) avoids edge cases like position players who threw a single
    mop-up inning in a blowout being misclassified as pitchers.
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute(
            "SELECT positions FROM players WHERE name = ? LIMIT 1",
            (name,),
        )
        row = cur.fetchone()
        if not row or not row[0] or not row[0].startswith("P"):
            conn.close()
            return False
        cur.execute("""
            SELECT 1 FROM season_pitching_stats sp
            JOIN players p ON sp.player_id = p.player_id
            WHERE p.name = ? LIMIT 1
        """, (name,))
        if cur.fetchone() is None:
            conn.close()
            return False
        # Two-way player (Ohtani rule, mirrored from player_card._is_two_way):
        # any season with PA >= 130 AND any season with ip_outs >= 90.
        cur.execute("""
            SELECT 1 FROM season_batting_stats sb
            JOIN players p ON sb.player_id = p.player_id
            WHERE p.name = ? AND sb.plate_appearances >= 130 LIMIT 1
        """, (name,))
        has_bat = cur.fetchone() is not None
        if has_bat:
            cur.execute("""
                SELECT 1 FROM season_pitching_stats sp
                JOIN players p ON sp.player_id = p.player_id
                WHERE p.name = ? AND sp.ip_outs >= 90 LIMIT 1
            """, (name,))
            if cur.fetchone() is not None:
                conn.close()
                return False  # two-way player
        conn.close()
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Helper: reject if player name is present (used by several parsers)
# ---------------------------------------------------------------------------

def _has_player_name(lower: str) -> bool:
    """Return True if the text contains a recognizable player name."""
    for name in sorted_names:
        if contains_word(name.lower(), lower):
            return True
    for last_name, players in last_name_index.items():
        if contains_word(last_name, lower) and len(players) == 1 \
                and last_name not in common_word_last_names:
            return True
    return False


# ---------------------------------------------------------------------------
# Parser functions
# ---------------------------------------------------------------------------


def parse_tonight_preview(input_str: str) -> Optional[dict]:
    """Detect 'how will Judge do tonight' style queries (player + tonight/today, no pitcher).

    Returns {"name": player_name} or None.
    Does NOT fire if a second player (pitcher) is also mentioned — that's a matchup query.
    """
    lower = input_str.strip().lower()
    # Must contain a tonight/today signal
    if not any(w in lower for w in ["tonight", "today", "this evening", "this game"]):
        return None
    # Strip preambles
    for prefix in ["how will ", "how should ", "how would ", "how does ", "how do ",
                    "what will ", "preview ", "what should i expect from "]:
        if lower.startswith(prefix):
            lower = lower[len(prefix):]
            break
    lower = re.sub(r'\bdo\b\s*', '', lower).strip()
    # Strip trailing time context
    for suffix in [" do tonight", " do today", " tonight", " today",
                   " this evening", " this game"]:
        if lower.endswith(suffix):
            lower = lower[:-len(suffix)]
    lower = lower.strip("?.! ")
    if not lower:
        return None
    # Match a player name
    name = find_player_in_text(lower)
    if not name:
        name = match_player(lower)
    if not name:
        return None
    # Reject if a pitcher is also mentioned (that's parse_matchup's job)
    # Strip both the full resolved name and the parts of the original text that matched
    remaining = lower
    for part in name.lower().split():
        remaining = re.sub(r'\b' + re.escape(part) + r'\b', '', remaining)
    remaining = remaining.strip()
    if remaining:
        second = find_player_in_text(remaining)
        if second and second != name:
            return None
    return {"name": name}


def parse_matchup(input_str: str) -> Optional[dict]:
    """Detect batter-vs-pitcher matchup queries like 'Judge vs Verlander'.
    Returns dict with batter, pitcher, season, or None if not a matchup."""
    season = detect_season(input_str)
    cleaned = input_str.strip().lower()

    # Strip year tokens
    if season is not None:
        cleaned = re.sub(r'\b(189[89]|19\d{2}|20[0-2]\d)\b', '', cleaned).strip()
        for phrase in ["this year", "this season", "current season", "last year", "last season",
                       "previous season", "prior season", "two years ago", "2 years ago",
                       "three years ago", "3 years ago"]:
            cleaned = cleaned.replace(phrase, "")
        cleaned = cleaned.strip()

    # Strip preambles like "how will X do against Y", "how should X do against Y"
    for prefix in ["how will ", "how should ", "how would ", "how does ", "how do ",
                    "what will ", "preview "]:
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):]
            break
    # "judge do against cole" → "judge against cole"
    cleaned = re.sub(r'\bdo\b\s*', '', cleaned).strip()
    # Strip trailing context
    for suffix in [" do tonight", " do today", " tonight", " today",
                   " this game", " do this game"]:
        if cleaned.endswith(suffix):
            cleaned = cleaned[:-len(suffix)]
    cleaned = cleaned.strip("?.!")

    # Try splitting on matchup-style delimiters
    # Strong matchup signals — allow alt-name pitcher resolution
    strong_delimiters = [" against ", " facing ", " matchup with ", " matchup against ", " matchup "]
    # Weak delimiters — only match if primary names have a clear batter/pitcher split
    weak_delimiters = [" versus ", " vs. ", " vs "]
    all_delimiters = strong_delimiters + weak_delimiters
    for delimiter in all_delimiters:
        if delimiter not in cleaned:
            continue
        idx = cleaned.index(delimiter)
        part1 = cleaned[:idx].strip()
        part2 = cleaned[idx + len(delimiter):].strip()

        if not part1 or not part2:
            continue
        m1 = match_player_with_prominence(part1)
        m2 = match_player_with_prominence(part2)
        if m1 and m2 and m1[0] != m2[0]:
            name1, alts1 = m1[0], m1[1]
            name2, alts2 = m2[0], m2[1]
            pitcher1 = is_pitcher(name1)
            pitcher2 = is_pitcher(name2)
            # Exactly one pitcher and one batter
            if pitcher1 and not pitcher2:
                return {"batter": name2, "pitcher": name1, "season": season}
            elif pitcher2 and not pitcher1:
                return {"batter": name1, "pitcher": name2, "season": season}
            # Only check alternatives for strong matchup delimiters
            # ("facing", "against") — not for "vs" which is ambiguous
            if delimiter in strong_delimiters:
                # Both batters — check if an alternative is a pitcher
                if not pitcher1 and not pitcher2:
                    for alt in alts1:
                        if is_pitcher(alt):
                            return {"batter": name2, "pitcher": alt, "season": season}
                    for alt in alts2:
                        if is_pitcher(alt):
                            return {"batter": name1, "pitcher": alt, "season": season}
                # Both pitchers — check alts for a batter
                if pitcher1 and pitcher2:
                    for alt in alts1:
                        if not is_pitcher(alt):
                            return {"batter": alt, "pitcher": name2, "season": season}
                    for alt in alts2:
                        if not is_pitcher(alt):
                            return {"batter": alt, "pitcher": name1, "season": season}
            return None

    return None


def parse_comparison(input_str: str) -> Optional[dict]:
    """Detect comparison queries. Returns dict with name1, name2, season, alternatives."""
    # Year-over-year comparison — not a player comparison
    if re.search(r'20[012]\d\s*(?:vs\.?|versus|compared to|to)\s*20[012]\d', input_str.lower()):
        return None
    season = detect_season(input_str)
    cleaned = input_str.strip().lower()

    # Strip year tokens
    if season is not None:
        cleaned = re.sub(r'\b(189[89]|19\d{2}|20[0-2]\d)\b', '', cleaned).strip()
        for phrase in ["this year", "this season", "current season", "last year", "last season",
                       "previous season", "prior season", "two years ago", "2 years ago",
                       "three years ago", "3 years ago"]:
            cleaned = cleaned.replace(phrase, "")
        cleaned = cleaned.strip()

    # Strip common prefixes
    for prefix in ["how do ", "how does ", "compare "]:
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):]
            break

    # Strip trailing " compare"
    if cleaned.endswith(" compare"):
        cleaned = cleaned[:-len(" compare")]

    # Strip trailing punctuation
    cleaned = cleaned.strip("?.!")

    # Strip preamble before question mark
    if "?" in cleaned:
        after_q = cleaned[cleaned.index("?") + 1:].strip()
        if after_q:
            cleaned = after_q

    # Strip preamble before comma
    if "," in cleaned:
        after_comma = cleaned[cleaned.rindex(",") + 1:].strip()
        if after_comma:
            cleaned = after_comma

    # Try splitting on delimiters
    _comparison_suffixes = ["career", "career stats", "stats", "numbers",
                            "all time", "all-time", "historically"]
    delimiters = [" compared to ", " versus ", " vs. ", " vs ", " or ", " and ", " to ", " with "]
    for delimiter in delimiters:
        if delimiter not in cleaned:
            continue
        idx = cleaned.index(delimiter)
        part1 = cleaned[:idx].strip()
        part2 = cleaned[idx + len(delimiter):].strip()

        # Strip trailing context words that aren't part of a player name
        for suffix in _comparison_suffixes:
            if part2.endswith(f" {suffix}"):
                part2 = part2[:-(len(suffix) + 1)].strip()
            if part1.endswith(f" {suffix}"):
                part1 = part1[:-(len(suffix) + 1)].strip()

        if not part1 or not part2:
            continue
        m1 = match_player_with_prominence(part1)
        m2 = match_player_with_prominence(part2)
        if m1 and m2 and m1[0] != m2[0]:
            all_alts = list(m1[1]) + list(m2[1])
            return {
                "name1": m1[0], "name2": m2[0], "season": season,
                "alternatives1": m1[1], "alternatives2": m2[1],
            }

    # Fallback: find two distinct player names in the string
    comparison_signals = [" vs ", " vs. ", " versus ", " or ", " compared to ", " and ", " better than "]
    has_signal = any(s in cleaned for s in comparison_signals)
    if has_signal:
        first = find_player_in_text(cleaned)
        if first:
            remaining = cleaned.replace(first.lower(), "")
            second = find_player_in_text(remaining)
            if second and second != first:
                return {
                    "name1": first, "name2": second, "season": season,
                    "alternatives1": [], "alternatives2": [],
                }

    return None


def parse_streak_query(input_str: str) -> Optional[dict]:
    """Detect historical streak queries. Returns dict with name, performance, season."""
    lower = input_str.strip().lower()

    # An explicit "{N} game(s)" window is a FIXED-length sliding-window query
    # ("best 13 game span") — the query engine handles those as a rate window.
    # Bail so this PELT-streak-list parser doesn't claim it and drop the N.
    if re.search(r'\b\d+\s*[-]?\s*games?\b', lower):
        return None

    hot_triggers = [
        "hot streaks", "hot streak", "best streaks", "best streak",
        "hottest streak", "hottest stretches", "hottest stretch", "hot runs",
        "best stretch", "best stretches", "best span", "best spans",
        "hot stretch", "hot stretches", "best run",
        # Colloquial hot
        "on a tear", "on a heater", "heater", "on a roll", "red hot", "red-hot",
        "scorching", "torrid", "in a groove", "hot spell", "tear",
        "when was", "when did", "get hot",
    ]
    cold_triggers = [
        "cold streaks", "cold streak", "worst streaks", "worst streak",
        "coldest streak", "cold stretches", "cold stretch", "slumps", "slump",
        "worst stretch", "worst stretches", "worst span", "worst spans",
        "biggest slump", "worst run",
        # Colloquial cold
        "skid", "skids", "in a funk", "funk", "in a rut", "rut",
        "rough patch", "rough stretch", "dry spell", "cold spell", "cold snap",
        "when was", "when did", "get cold",
    ]
    _cold_kw = ("cold", "worst", "coldest", "slump", "skid", "funk", "rut",
                "rough patch", "rough stretch", "dry spell", "snap")
    _hot_kw = ("hot", "best", "hottest", "tear", "heater", "roll", "scorching",
               "torrid", "groove")

    is_cold = any(t in lower for t in cold_triggers) and any(w in lower for w in _cold_kw)
    is_hot = any(t in lower for t in hot_triggers) and any(w in lower for w in _hot_kw)

    if not (is_hot or is_cold):
        return None
    performance = "cold" if is_cold else "hot"

    has_plural = any(w in lower for w in ("streaks", "stretches", "runs", "slumps", "spans"))
    has_explicit_year = bool(re.search(r'20[12][0-9]', lower))
    has_current = any(p in lower for p in ("this year", "this season", "right now", "currently"))
    has_career = any(p in lower for p in ("career", "all time", "all-time", "ever"))
    past_tense_patterns = ["last year", "last season", "previous season", "prior season",
                           "two years ago", "2 years ago", "three years ago", "3 years ago"]
    has_past_tense = any(p in lower for p in past_tense_patterns)

    # Bare present-tense "best streak" with no time context is ambiguous — defer
    # to current_form. "this year"/"this season"/"career" give it context.
    if (not has_plural and not has_explicit_year and not has_past_tense
            and not has_current and not has_career and performance == "hot"):
        return None

    # Detect season. career → None + career flag (all-time scope in the builder).
    target_season: Optional[int] = None
    m = re.search(r'20[12][0-9]', lower)
    if m:
        target_season = int(m.group())
    elif not has_career:
        current_year = _get_db_max_season()
        for patterns, offset in [
            (["this year", "this season"], 0),
            (["last year", "last season", "previous season", "prior season"], -1),
            (["two years ago", "2 years ago"], -2),
            (["three years ago", "3 years ago"], -3),
        ]:
            if any(p in lower for p in patterns):
                target_season = current_year + offset
                break

    name = find_player_in_text(lower)
    if not name:
        return None
    return {"name": name, "performance": performance, "season": target_season,
            "career": has_career}


def parse_best_month(input_str: str) -> Optional[dict]:
    """'{player} best/worst month [this year/career]' → that player's best or
    worst calendar month by OPS (hitters) / ERA (pitchers). Returns
    {name, performance, season, career} or None. Only fires with a player, so
    the all-time month leaderboard ("best OPS month ever") is unaffected."""
    lower = input_str.strip().lower()
    if "month" not in lower:
        return None
    # An explicit "{N} game(s)" window is a stretch, not a calendar month.
    if re.search(r'\b\d+\s*[-]?\s*games?\b', lower):
        return None
    if any(k in lower for k in ("worst month", "coldest month")):
        performance = "worst"
    elif any(k in lower for k in ("best month", "hottest month", "top month", "biggest month")):
        performance = "best"
    else:
        return None
    name = find_player_in_text(input_str)
    if not name:
        return None
    if any(p in lower for p in ("career", "all time", "all-time", "ever")):
        return {"name": name, "performance": performance, "season": None, "career": True}
    season = detect_season(lower)
    # Bare "best month" (no time context) → career default (with a season see-also).
    return {"name": name, "performance": performance, "season": season,
            "career": season is None}


def parse_leaderboard_window(input_str: str) -> Optional[dict]:
    """'who had the best 10-game stretch this year' / 'most HR in a 10 game
    stretch' (NO specific player) → a leaderboard of players' best/worst N-game
    windows. Season-scoped (a multi-player all-time window scan is too expensive).
    Returns {stat, n, sort_asc, season, is_pitching} or None."""
    lower = input_str.strip().lower()
    if not (re.search(r'\b(?:stretch|span)\b', lower)
            or re.search(r'\b(?:in|over|across)\s+(?:a|any|their)?\s*\d+[\s-]?games?\b', lower)):
        return None
    m = re.search(r'(\d+)[\s-]*games?\b', lower)
    if not m:
        return None
    n = int(m.group(1))
    if n < 2 or n > 162:
        return None
    # A specific player → single-player route (query engine), not a leaderboard.
    if find_player_in_text(input_str):
        return None
    # Needs a leaderboard signal.
    if not any(t in lower for t in ("who ", "who'", "leaders", "leader", "best",
                                    "worst", "most", "fewest", "highest",
                                    "lowest", "top ", "league")):
        return None
    sort_asc = any(w in lower for w in ("worst", "fewest", "lowest", "least"))
    stripped = re.sub(r'\b(?:in|over|across|during)?\s*(?:a|any|their)?\s*'
                      r'\d+[\s-]*games?\b', ' ', lower)
    stripped = re.sub(r'\b(?:stretch|span)\b', ' ', stripped)
    stat = match_stat(stripped)
    if stat and stat.db_column == "games":
        stat = None
    is_pitching = bool(re.search(r'\bpitchers?\b', lower)) or \
        (stat is not None and is_pitching_stat(stat))
    season = detect_season(lower) or _get_db_max_season()
    return {"stat": stat, "n": n, "sort_asc": sort_asc, "season": season,
            "is_pitching": is_pitching}


def parse_current_form(input_str: str) -> Optional[str]:
    """Detect current form queries. Returns canonical player name."""
    lower = input_str.strip().lower()

    triggers = [
        "lately", "recently", "right now", "current form", "current streak",
        "hot streak", "hot right now", "been playing", "been doing",
        "doing lately", "doing recently", "playing lately", "playing recently",
        "been hitting", "hitting lately", "on fire", "heating up", "locked in",
        "how is", "how has", "how's",
    ]
    if not any(t in lower for t in triggers):
        return None

    return find_player_in_text(lower)


def parse_season_lookup(input_str: str) -> Optional[dict]:
    """Detect season lookup queries. Returns dict with name, season."""
    lower = input_str.strip().lower()

    # Reject game-log-style queries — these need the query engine, not a season summary
    if re.search(r'games?\s+with\s+\d|multi[- ]?(hit|homer|hr)|(\d+)\+?\s*[- ]?\s*hit\s+games?|\d+\+\s+\w+\s+games?', lower):
        return None

    # Reject "first/last/past/previous/recent N games|starts|at-bats|appearances|innings"
    # — these are game-window qualifiers we don't model in a season summary.
    # Letting them through claims the query with stale full-season stats and
    # silently drops the qualifier. Decline so the chain falls to the
    # query engine or Haiku SQL.
    if re.search(_GAME_WINDOW_RX, lower):
        return None

    # Reject year-over-year comparisons — "Betts 2023 vs 2024" or "Soto last year vs this year"
    if re.search(r'(?:18|19|20)\d{2}\s*(?:vs\.?|versus|compared to|to)\s*(?:18|19|20)\d{2}', lower):
        return None
    if re.search(r'(?:last|this)\s+(?:year|season)\s+(?:vs\.?|versus|compared to)\s+(?:last|this)\s+(?:year|season)', lower):
        return None

    # Reject cross-season analytical questions — these should fall through to Claude
    cross_season_patterns = [
        "how many seasons", "how many times", "how many years",
        "how often", "has.*ever", "did.*ever", "in how many",
        "throughout his career", "over his career", "across.*seasons",
        "each season", "every season", "per season",
    ]
    for pat in cross_season_patterns:
        if re.search(pat, lower):
            return None

    # Reject "player single-season max" queries — those want the BEST single
    # season across the player's career, not the most recent season. Handled
    # in query_engine.decompose() (preferred) and parse_player_single_season_max
    # (fallback). Without this guard, "Most home runs Hank Aaron ever hit in
    # a season" silently matches here and returns Aaron's final-season stat
    # line.
    _has_super = bool(re.search(r'\b(?:most|best|highest|fewest|worst|lowest|peak|top|least)\b', lower))
    _has_explicit_year = bool(re.search(r'\b(?:19|20)\d{2}\b', lower)) or any(
        p in lower for p in ("this year", "this season", "current season",
                              "last year", "last season", "previous season", "prior season",
                              "two years ago", "three years ago"))
    _has_career_total = bool(re.search(
        r'\bcareer\s+(?:total|totals|stat|stats|statistics|number|numbers|sum|sums)\b', lower))
    if _has_super and (
        re.search(r'\bever\b', lower)
        or re.search(r'\bin (?:a|one|any|a single) season\b', lower)
        or re.search(r'\b(?:best|highest|peak|top|worst|lowest)\b(?:\s+\w+){0,3}\s+(?:season|year)\b', lower)
        or re.search(r'\bcareer[- ]?(?:high|best|worst|low|lowest|highest)\b', lower)
        # Bare possessive: "Maddux's lowest WHIP", "Trout's best OPS" — no
        # explicit "ever" or "in a season", but superlative + no specific
        # year + no career-total phrasing = single-season max intent.
        or (not _has_explicit_year and not _has_career_total)
    ):
        return None

    # Reject narrative / interrogative / opinion shapes. These ask about
    # WHY / HOW / a player's story — not their stat line. Returning a
    # season summary drops the actual question and ships an answer the
    # user didn't ask for. Decline so the chain falls through to Sonnet's
    # sql_planner, which can handle narrative analysis.
    narrative_triggers = [
        r'\bwhy\s+(?:is|are|was|were|does|do|did|has|have|had)\b',
        r'\bexplain\b',
        r'\btell\s+me\s+about\b',
        r'\b(?:how\s+come|how\s+did)\b',
        r'\bwhat\s+(?:makes|made)\b',
        r'\bdescribe\b',
        r'\bcompare\b',  # "compare X and Y" — handled by parse_comparison if both names present
        r'\bbreakdown\b',
        r'\bbreak\s+down\b',
    ]
    for pat in narrative_triggers:
        if re.search(pat, lower):
            return None

    current_year = _get_db_max_season()

    target_season: Optional[int] = None
    # Any season the DB could hold (1876-2029) — NOT just 2020s. The old
    # pattern (20[2][0-9]) silently dropped historical years: "David Ortiz
    # stats in 2004" matched no year and shipped his 2016 (final) season.
    m = re.search(r'\b(?:18[7-9]\d|19\d{2}|20[0-2]\d)\b', lower)
    if m:
        target_season = int(m.group())
    else:
        for patterns, offset in [
            (["this year", "this season", "doing this", "current season"], 0),
            (["last year", "last season", "previous season", "prior season"], -1),
            (["two years ago", "2 years ago"], -2),
            (["three years ago", "3 years ago"], -3),
        ]:
            if any(p in lower for p in patterns):
                target_season = current_year + offset
                break

    name = find_player_in_text(lower)
    if not name:
        return None

    # If no season specified, default to most recent season for that player
    if target_season is None:
        target_season = current_year

    # Unexplained-words guard: a season lookup is "X [year]". A leftover
    # qualifier (vs the Red Sox, at Fenway, in wins, postseason, leadoff, in
    # extra innings) means we'd silently ship a full-season line that ignores
    # it — bail to the query engine / Haiku instead.
    if _residual_qualifier_words(lower, name):
        return None

    return {"name": name, "season": target_season}


def parse_season_count(input_str: str) -> Optional[dict]:
    """Detect 'how many seasons has X hit a triple' / 'how often has X hit 30 HR'.
    Returns dict with name, stat (db column), threshold (minimum value, default 1)."""
    lower = input_str.strip().lower()

    # Must contain a cross-season counting phrase OR "N+ stat seasons" pattern
    has_counting_phrase = bool(re.search(r'how many (seasons?|times?|years?)|how often|in how many|has.*ever|did.*ever', lower))

    # "Ohtani 30+ HR seasons", "Judge .300 seasons", "Soto 100 RBI years"
    # "30+ HR seasons", ".300 batting average seasons", ".300 seasons"
    n_stat_seasons_match = re.search(r'(\.?\d+\.?\d*)\+?\s+(\w[\w\s]*?)\s+(?:seasons?|years?)\s*$', lower)
    if not n_stat_seasons_match:
        # Bare rate: ".300 seasons" (no stat word, infer batting average)
        n_stat_seasons_match = re.search(r'(\.\d{3})\+?\s+(?:seasons?|years?)\s*$', lower)

    if not has_counting_phrase and not n_stat_seasons_match:
        return None

    name = find_player_in_text(lower)
    if not name:
        return None

    # Verb-form rate values ("hit .300", "batted .300", "hit over .300")
    # must override whatever match_stat would return. "hit" alone is an
    # alias for the hits stat, but "hit .300" semantically means ".300
    # batting average." Without this guard, match_stat resolves to hits
    # first and the query becomes "0.3+ hits in N seasons" — never the
    # user's intent. The leading dot in the regex is what disambiguates a
    # rate value (.300) from a counting threshold (30).
    verb_rate = re.search(
        r'(?:batted|batting|hitting|hit|bat)\s+(?:over|above|under|below|sub)?\s*\.\d{2,3}',
        lower,
    )
    if verb_rate:
        stat_info = match_stat("batting average")
    else:
        stat_info = match_stat(lower)
        if not stat_info:
            # Bare rate values ".300 seasons" — no verb to anchor on, infer
            # batting average from the rate-format value.
            if re.search(r'\.\d{3}\s+(?:seasons?|years?)', lower):
                stat_info = match_stat("batting average")
            if not stat_info:
                return None

    # Check for an explicit threshold — "hit 30 home runs", "batted .300", "stolen 40 bases"
    threshold = 1
    if n_stat_seasons_match:
        threshold = float(n_stat_seasons_match.group(1))
        if threshold == int(threshold):
            threshold = int(threshold)
    else:
        # Try number anywhere near context: "hit 30", "batted .300", "stolen 40", "bat .300"
        m = re.search(r'(?:hit|hitting|batted|batting|bat|stolen|had|threw|pitched|struck out|walked|over|above)\s+(\.?\d+\.?\d*)', lower)
        if m:
            threshold = float(m.group(1))
            if threshold == int(threshold):
                threshold = int(threshold)

    if _residual_qualifier_words(lower, name):
        return None
    return {"name": name, "stat": stat_info.db_column, "stat_abbrev": stat_info.display_abbrev,
            "stat_name": stat_info.display_name, "threshold": threshold, "is_rate": stat_info.is_rate}


def parse_single_stat_lookup(input_str: str) -> Optional[dict]:
    """Detect queries like 'Judge home runs'. Returns dict with name, stat, season."""
    lower = input_str.strip().lower()

    # Reject game-log-style queries — handled by query engine
    if re.search(r'games?\s+with\s+\d|multi[- ]?(hit|homer|hr)|(\d+)\+?\s*[- ]?\s*hit\s+games?|\d+\+\s+\w+\s+games?', lower):
        return None

    # Reject "game logs" / "game log" — not a stat lookup
    if re.search(r'\bgame\s*logs?\b', lower):
        return None

    # Reject game-window qualifiers ("Stanton past 5 games", "Will Warren
    # first seven starts"). Without this guard, the qualifier is silently
    # dropped and the user gets a full-season stat line — see _GAME_WINDOW_RX
    # comment for the full pattern shape.
    if re.search(_GAME_WINDOW_RX, lower):
        return None

    # Reject platoon-filtered queries — handled by platoon parser
    if re.search(r'\bvs\s+(?:lefties|righties|left|right|lhp|rhp)\b', lower):
        return None

    # Exclude cross-season queries (handled by parse_season_count)
    cross_season_patterns = [
        "how many seasons", "how many times", "how many years",
        "how often", r"has.*ever", r"did.*ever", "in how many",
        "throughout his career", "over his career", r"across.*seasons",
        "each season", "every season", "per season",
    ]
    for pat in cross_season_patterns:
        if re.search(pat, lower):
            return None

    # Exclude leaderboard patterns
    leaderboard_words = ["leaders", "leader", "leaderboard", "top ", "most ", "best ", "highest", "lowest",
                         "fewest", "worst", "least",
                         "who led", "who leads", "who hit the most", "who had the most", "leading"]
    if any(w in lower for w in leaderboard_words):
        return None

    # Exclude career queries
    if contains_word("career", lower):
        return None

    stat = match_stat(lower)
    if not stat:
        return None

    name = find_player_in_text(lower)
    if not name:
        return None

    season = detect_season(lower, default_to_most_recent=True) or _current_calendar_year()
    if _residual_qualifier_words(lower, name):
        return None
    return {"name": name, "stat": stat, "season": season}


def parse_slash_line_lookup(input_str: str) -> Optional[dict]:
    """Detect slash line queries. Returns dict with name, season."""
    lower = input_str.strip().lower()
    if not any(s in lower for s in ("slash line", "slashline", "slash-line")):
        return None
    name = find_player_in_text(lower)
    if not name:
        return None
    season = detect_season(lower, default_to_most_recent=True) or _current_calendar_year()
    return {"name": name, "season": season,
            "consumed": ["slash line", "slashline", "slash-line"]}


def parse_career_lookup(input_str: str) -> Optional[dict]:
    """Detect career lookup queries. Returns dict with name, stat (optional)."""
    lower = input_str.strip().lower()

    if not contains_word("career", lower):
        return None

    # Exclude leaderboard patterns
    leaderboard_words = ["leaders", "leader", "leaderboard", "top ", "most ", "best ", "highest", "lowest",
                         "fewest", "worst", "least",
                         "who led", "who leads", "who hit the most", "who had the most", "leading"]
    if any(w in lower for w in leaderboard_words):
        return None

    # Exclude comparison patterns
    comparison_words = [" vs ", " vs. ", " versus ", " compared to ", " or ", " better than ", " and "]
    if any(w in lower for w in comparison_words):
        return None

    name = find_player_in_text(lower)
    if not name:
        return None

    stat = match_stat(lower)
    if _residual_qualifier_words(lower, name):
        return None
    return {"name": name, "stat": stat}


def parse_platoon_splits(input_str: str) -> Optional[dict]:
    """Detect platoon split queries. Returns dict with name, hand, season."""
    lower = input_str.strip().lower()

    lhp_triggers = ["vs lefties", "against lefties", "vs left-handed", "vs lhp",
                     "versus lefties", "facing lefties", "left-handed pitching",
                     "vs. lefties", "against left-handed", "against lhp",
                     "versus left-handed", "facing left-handed"]
    rhp_triggers = ["vs righties", "against righties", "vs right-handed", "vs rhp",
                     "versus righties", "facing righties", "right-handed pitching",
                     "vs. righties", "against right-handed", "against rhp",
                     "versus right-handed", "facing right-handed"]
    both_triggers = ["platoon splits", "platoon", "splits"]
    # Mixed / abbreviated phrasings that mean "show me both splits":
    # "righty left splits", "lefty righty splits", "rhp lhp", "L/R splits",
    # bare "righty splits" / "lefty splits". A common casual asker doesn't
    # use "vs" or "against" and just names the handedness they care about.
    # For now, treat all of these as the "both sides" case, same as the
    # bare "platoon splits" trigger — the response builder shows both rows.
    both_mixed_re = re.compile(
        r'\b(?:'
        r'(?:righty|lefty|rhp|lhp|righties|lefties)\s+'
        r'(?:righty|lefty|rhp|lhp|righties|lefties)\s+splits'
        r'|(?:righty|lefty|rhp|lhp)\s+splits'
        r'|(?:right|left)\s*/\s*(?:left|right)\s+splits'
        r'|l\s*/\s*r\s+splits'
        r'|r\s*/\s*l\s+splits'
        r')\b'
    )
    has_lhp = any(t in lower for t in lhp_triggers)
    has_rhp = any(t in lower for t in rhp_triggers)
    has_both = any(t in lower for t in both_triggers) or both_mixed_re.search(lower) is not None

    if has_lhp:
        hand = "LHP"
    elif has_rhp:
        hand = "RHP"
    elif has_both:
        hand = None
    else:
        return None

    name = find_player_in_text(lower)
    if not name:
        return None

    # Career scope override — "career vs lefties" / "all-time OPS against
    # lefties" / "lifetime vs LHP" must aggregate across every season the
    # player has platoon data for, NOT silently default to current season
    # (2026-07-06 bug: "Judge career OPS against lefties" was silently
    # dropping "career" and returning his 2026 platoon number in 71 PA).
    is_career = ("career" in lower or "all time" in lower
                 or "all-time" in lower or "lifetime" in lower)
    if is_career:
        season = None
    else:
        season = detect_season(lower, default_to_most_recent=True) or _current_calendar_year()
    mixed_words = ["righty", "lefty", "righties", "lefties",
                   "rhp", "lhp", "right", "left"]
    return {"name": name, "hand": hand, "season": season,
            "consumed": lhp_triggers + rhp_triggers + both_triggers + mixed_words +
            (["career", "all time", "all-time", "lifetime"] if is_career else [])}


def parse_platoon_leaderboard(input_str: str) -> Optional[dict]:
    """Detect platoon leaderboard queries like 'most HR against lefties'.
    Returns dict with stat, hand, is_pitching, season, limit."""
    lower = input_str.strip().lower()
    league_result = detect_league(lower)
    if league_result:
        lower = league_result[1]

    leaderboard_triggers = ["leaders", "leader", "leaderboard", "top ", "most ", "best ",
                            "highest", "lowest", "who led", "who leads", "who hit the most",
                            "who had the most", "leading", "fewest"]
    if not any(t in lower for t in leaderboard_triggers):
        return None

    # Must have a platoon qualifier
    lhp_triggers = ["vs lefties", "against lefties", "vs left-handed", "vs lhp",
                     "versus lefties", "facing lefties", "left-handed pitching",
                     "against left-handed", "against lhp", "left-handed batters",
                     "left-handed hitters",
                     # Abbreviation forms common in stat-Twitter
                     "vs lhb", "against lhb", "facing lhb", "versus lhb"]
    rhp_triggers = ["vs righties", "against righties", "vs right-handed", "vs rhp",
                     "versus righties", "facing righties", "right-handed pitching",
                     "against right-handed", "against rhp", "right-handed batters",
                     "right-handed hitters",
                     # Abbreviation forms common in stat-Twitter
                     "vs rhb", "against rhb", "facing rhb", "versus rhb"]

    has_lhp = any(t in lower for t in lhp_triggers)
    has_rhp = any(t in lower for t in rhp_triggers)
    if not has_lhp and not has_rhp:
        return None

    hand = "LHP" if has_lhp else "RHP"

    # Reject if there's a specific player name — that's a player split, not a leaderboard
    if _has_player_name(lower):
        return None

    # Strip the platoon qualifier before matching the stat, so "batting average
    # against righties" matches AVG, not BAA ("batting average against")
    stat_text = lower
    all_platoon_triggers = lhp_triggers + rhp_triggers
    for trigger in sorted(all_platoon_triggers, key=len, reverse=True):
        stat_text = stat_text.replace(trigger, " ")
    stat = match_stat(stat_text)
    if not stat:
        return None

    # Detect pitching context: pitching-specific stat, or "pitcher" NOT preceded
    # by "against/vs/facing ... pitcher" (which describes the opponent, not the subject)
    pitching_stats = {"era", "earned_run_avg", "wins", "losses", "saves", "whip",
                      "k_per_9", "bb_per_9", "h_per_9", "hr_per_9", "k_per_bb",
                      "innings_pitched", "quality_starts", "complete_games",
                      "shutouts", "batting_avg_against"}
    # "against left-handed pitchers" = batters facing pitchers, NOT pitching context
    # "against left-handed batters/hitters" = pitchers facing batters, IS pitching context
    pitcher_is_subject = bool(re.search(r'\bpitcher', lower)) and \
        not re.search(r'(?:against|vs\.?|versus|facing)\b.*\bpitcher', lower)
    batter_is_opponent = bool(re.search(r'(?:against|vs\.?|versus|facing)\b.*\b(?:batter|hitter)', lower))
    is_pitching = pitcher_is_subject or batter_is_opponent or stat.db_column in pitching_stats

    limit = 50
    m = re.search(r'top\s+(\d+)', lower)
    if m:
        limit = max(1, min(int(m.group(1)), 50))

    season = detect_season(lower, default_to_most_recent=True) or _current_calendar_year()
    return {
        "stat": stat, "hand": hand, "is_pitching": is_pitching,
        "season": season, "limit": limit,
        "league": league_result[0] if league_result else None,
    }


def parse_home_away_splits(input_str: str) -> Optional[dict]:
    """Detect home/away split queries. Returns dict with name, location, season."""
    lower = input_str.strip().lower()

    home_triggers = ["at home", "home splits", "home stats", "home numbers",
                     "at home field", "home games"]
    away_triggers = ["on the road", "road splits", "road stats", "away splits",
                     "away stats", "away games", "road numbers", "road games"]
    both_triggers = ["home vs away", "home and away", "home/away", "home away splits",
                     "home away", "home vs. away", "home or away"]

    has_both = any(t in lower for t in both_triggers)
    has_home = any(t in lower for t in home_triggers)
    has_away = any(t in lower for t in away_triggers)

    if has_both:
        location = None
    elif has_home:
        location = "home"
    elif has_away:
        location = "away"
    else:
        return None

    name = find_player_in_text(lower)
    if not name:
        return None

    season = detect_season(lower, default_to_most_recent=True) or _current_calendar_year()
    return {"name": name, "location": location, "season": season,
            "consumed": home_triggers + away_triggers + both_triggers}


def parse_risp_splits(input_str: str) -> Optional[dict]:
    """Detect RISP split queries. Returns dict with name, season."""
    lower = input_str.strip().lower()

    triggers = ["runners in scoring position", "risp", "scoring position",
                "runners on base", "men on base", "clutch hitting", "clutch stats",
                "with runners on"]

    if not any(t in lower for t in triggers):
        return None
    name = find_player_in_text(lower)
    if not name:
        return None

    season = detect_season(lower, default_to_most_recent=True) or _current_calendar_year()
    return {"name": name, "season": season, "consumed": triggers}


def parse_pitch_type_splits(input_str: str) -> Optional[dict]:
    """Detect pitch type split queries. Returns dict with name, pitch_type, season."""
    lower = input_str.strip().lower()

    pitch_type_map: list[tuple[list[str], str]] = [
        (["fastball", "fastballs", "4-seam", "4-seamers", "four-seam", "four seam", "heater", "heaters"], "4-Seam"),
        (["sinker", "sinkers", "two-seam", "two seam", "2-seam"], "Sinker"),
        (["slider", "sliders"], "Slider"),
        (["changeup", "changeups", "change-up", "change up"], "Change"),
        (["curveball", "curveballs", "curve", "curves"], "Curve"),
        (["cutter", "cutters", "cut fastball"], "Cutter"),
        (["sweeper", "sweepers"], "Sweeper"),
        (["splitter", "splitters", "split-finger", "split finger"], "Split"),
    ]

    general_triggers = ["pitch type splits", "pitch type", "by pitch type", "by pitch",
                        "pitch splits", "against each pitch"]

    pitch_type: Optional[str] = None
    has_trigger = False

    for patterns, db_value in pitch_type_map:
        if any(p in lower for p in patterns):
            pitch_type = db_value
            has_trigger = True
            break

    if not has_trigger:
        has_trigger = any(t in lower for t in general_triggers)

    # Also catch "against [pitch]" and "vs [pitch]" patterns
    if not has_trigger:
        for trigger in ("against ", "vs "):
            if trigger in lower:
                for patterns, db_value in pitch_type_map:
                    if any((trigger + p) in lower for p in patterns):
                        pitch_type = db_value
                        has_trigger = True
                        break
            if has_trigger:
                break

    if not has_trigger:
        return None
    name = find_player_in_text(lower)
    if not name:
        return None

    season = detect_season(lower, default_to_most_recent=True) or _current_calendar_year()
    _pt_consumed = [p for pats, _ in pitch_type_map for p in pats] + general_triggers + ["against", "vs", "versus", "by pitch"]
    return {"name": name, "pitch_type": pitch_type, "season": season,
            "consumed": _pt_consumed}


def parse_count_splits(input_str: str) -> Optional[dict]:
    """Detect count split queries. Returns dict with name, counts, season."""
    lower = input_str.strip().lower()

    # Specific ball-strike count
    specific_counts = re.findall(r'\b([0-3]-[0-2])\b', lower)

    two_strike_patterns = ["two strikes", "2 strikes", "two-strike", "2-strike"]
    full_count_patterns = ["full count", "3-2 count"]
    ahead_patterns = ["ahead in the count", "hitter's count", "hitters count", "batter's count"]
    behind_patterns = ["behind in the count", "pitcher's count", "pitchers count"]
    general_triggers = ["count splits", "by count", "count stats"]

    counts: Optional[list[str]] = None
    has_trigger = False

    if specific_counts:
        counts = specific_counts
        has_trigger = True
    elif any(p in lower for p in two_strike_patterns):
        counts = ["0-2", "1-2", "2-2", "3-2"]
        has_trigger = True
    elif any(p in lower for p in full_count_patterns):
        counts = ["3-2"]
        has_trigger = True
    elif any(p in lower for p in ahead_patterns):
        counts = ["1-0", "2-0", "2-1", "3-0", "3-1"]
        has_trigger = True
    elif any(p in lower for p in behind_patterns):
        counts = ["0-1", "0-2", "1-2"]
        has_trigger = True
    elif any(t in lower for t in general_triggers):
        counts = None  # show all
        has_trigger = True

    if not has_trigger:
        return None
    name = find_player_in_text(lower)
    if not name:
        return None

    season = detect_season(lower, default_to_most_recent=True) or _current_calendar_year()
    _ct_consumed = (two_strike_patterns + full_count_patterns + ahead_patterns
                    + behind_patterns + general_triggers + (specific_counts or [])
                    + ["count", "the count"])
    return {"name": name, "counts": counts, "season": season,
            "consumed": _ct_consumed}


# Word → digit lookup for ordinal matching across the next three parsers.
# "first" through "tenth" — anything past tenth is rare enough we accept only digits.
_ORDINAL_WORDS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
    "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
}


def parse_first_pa(input_str: str) -> Optional[dict]:
    """Detect 'first PA / first AB of game' batting queries. Returns dict
    with name, season — used to query first_pa_batting_splits."""
    lower = input_str.strip().lower()

    # Must reference "first" (or "1st") AND a PA-equivalent term.
    # Acceptable: "first at bat", "first AB", "first plate appearance",
    # "first PA", "1st at bat", "first time up".
    triggers = [
        "first at bat", "first at-bat", "first ab",
        "first plate appearance", "first pa",
        "first time up",
        "1st at bat", "1st at-bat", "1st ab", "1st pa",
    ]
    if not any(t in lower for t in triggers):
        return None

    name = find_player_in_text(lower)
    if not name:
        return None

    season = detect_season(lower, default_to_most_recent=True) or _current_calendar_year()
    return {"name": name, "season": season}


def parse_pitching_inning(input_str: str) -> Optional[dict]:
    """Detect 'Nth inning' pitcher queries. Returns dict with name, inning
    (string "1".."9" or "10+"), season — used to query pitching_inning_splits.
    """
    lower = input_str.strip().lower()

    inning: Optional[str] = None

    # Word ordinals: "first inning", "seventh inning", etc.
    for word, n in _ORDINAL_WORDS.items():
        if re.search(rf'\b{word}\s+inning\b', lower):
            inning = str(n) if n < 10 else "10+"
            break

    # Numeric ordinals: "1st inning", "7th inning", "10th inning"
    if inning is None:
        m = re.search(r'\b(\d+)(?:st|nd|rd|th)\s+inning\b', lower)
        if m:
            n = int(m.group(1))
            inning = str(n) if n < 10 else "10+"

    # Plain "inning N" / "inning 7" forms
    if inning is None:
        m = re.search(r'\binning\s+(\d+)\b', lower)
        if m:
            n = int(m.group(1))
            inning = str(n) if n < 10 else "10+"

    if inning is None:
        return None

    name = find_player_in_text(lower)
    if not name:
        return None

    season = detect_season(lower, default_to_most_recent=True) or _current_calendar_year()
    return {"name": name, "inning": inning, "season": season}


def parse_pitching_tto(input_str: str) -> Optional[dict]:
    """Detect 'Nth time through the order' pitcher queries. Returns dict
    with name, tto ("1"/"2"/"3"/"4+"), season — used to query
    pitching_tto_splits."""
    lower = input_str.strip().lower()

    tto: Optional[str] = None

    # Must reference "time through" or "times through" — otherwise things
    # like "third inning" would falsely match the ordinal alone.
    if "time through" not in lower and "times through" not in lower:
        return None

    # Word ordinals: "first time through", "third time through", etc.
    for word, n in _ORDINAL_WORDS.items():
        if re.search(rf'\b{word}\s+time(?:s)?\s+through\b', lower):
            tto = str(n) if n < 4 else "4+"
            break

    # Numeric ordinals: "1st time through", "3rd time through", "4th time through"
    if tto is None:
        m = re.search(r'\b(\d+)(?:st|nd|rd|th)\s+time(?:s)?\s+through\b', lower)
        if m:
            n = int(m.group(1))
            tto = str(n) if n < 4 else "4+"

    if tto is None:
        return None

    name = find_player_in_text(lower)
    if not name:
        return None

    season = detect_season(lower, default_to_most_recent=True) or _current_calendar_year()
    return {"name": name, "tto": tto, "season": season}


# ---------------------------------------------------------------------------
# Unexplained-words guard (shared) — the broad fix for "parser stopped reading
# the rest of the query." A simple parser declares the substrings it consumed
# (player name, stat phrase, season, its trigger words); anything meaningful
# left over means it ignored a qualifier and should bail to the query engine /
# Haiku rather than return a confident partial answer. Filler is TRUE filler
# only — scope qualifiers (after/before/vs/with/without/over/under/most…),
# numbers, and stray stat words are deliberately NOT filler, so leaving one
# triggers a bail.
# ---------------------------------------------------------------------------
# Mirrors query_engine._STOP_WORDS (the proven set that already lets the query
# engine claim all 116 audit queries without over-flagging) plus a few query-
# phrasing words. Deliberately does NOT contain situational/scope CONTENT words
# (risp, lefties, righties, losses, outs, after, vs, versus, against, before)
# — leaving one of those is exactly the "ignored qualifier" signal we bail on.
_GUARD_FILLER = {
    "jr", "sr", "junior", "senior",  # name suffixes consumed by alias layer
    "the", "a", "an", "in", "of", "by", "for", "to", "and", "or", "with",
    "who", "what", "which", "how", "many", "much", "did", "do", "does", "has",
    "had", "have", "is", "was", "were", "are", "been", "be",
    "that", "this", "than", "then", "from", "at", "on", "it", "its",
    "all", "any", "each", "every", "some", "no", "not",
    "player", "players", "pitcher", "pitchers", "hitter", "hitters",
    "batter", "batters", "team", "teams",
    "time", "times", "season", "seasons", "year", "years",
    "hit", "got", "get", "scored", "threw", "pitched", "batted",
    "ever", "just", "also", "over", "under", "least", "more", "most",
    "them", "their", "those", "these", "there",
    "game", "games", "played", "play", "playing", "hitting", "batting",
    "won", "win", "winning", "stole", "stolen", "stealing", "bases",
    "pas", "abs", "plate", "appearance", "appearances",
    "hits", "runs", "multiple", "allowed", "fewer", "thrown", "throwing",
    "pitching", "drove", "driven", "struck", "walked", "walking", "given",
    "gave", "during", "when", "where", "only", "ago", "back", "since", "sub",
    "among", "between", "across", "around", "about", "within", "per", "into",
    "both", "either", "but", "so", "yet", "while", "above", "below", "without",
    "like", "really", "actually", "currently", "recently", "appeared",
    "appearing", "recorded", "posted", "put", "league", "led", "leading",
    "leads", "leader", "leaders", "outing", "outings", "start", "starts",
    "baseman", "basemen", "fielder", "fielders", "qualified", "qualifying",
    "qualify",
    # query-phrasing extras (not in the engine's set, but pure filler here)
    "stats", "stat", "statline", "line", "numbers", "number", "s",
    "his", "her", "show", "me", "please", "whats", "career",
    # relative-season words (season parsers resolve these; not qualifiers)
    "last", "previous", "prior", "ago", "current", "doing", "two", "three",
}


def _strip_consumed(text: str, phrases: list) -> str:
    """Remove each consumed phrase AND its individual word tokens. The token
    pass matters because find_player_in_text returns the CANONICAL name
    ("Aaron Judge") while the query may contain only a partial ("judge") — so
    we strip both the full string and each name token.

    Also strips a trailing period from each token before the word-boundary
    match. Without this, "Luis Garcia Jr." (canonical form) → token "jr."
    fails to match "jr" in the query text — the "." after \b\bjr\b\b\. never
    aligns. Result: "luis garcia jr platoon splits" leaves "jr" as a
    residual qualifier and the parser silently bails, even though the name
    resolved correctly.
    """
    for phrase in phrases:
        if not phrase:
            continue
        p = str(phrase).lower()
        # Multi-word phrases, and ones with punctuation/digits ("vs.", "0-2"),
        # are removed as a contiguous substring — safe, they don't occur inside
        # other words. A single ALPHABETIC word is NOT substring-replaced:
        # text.replace("in"," ") would corrupt "doing"→"do g" (and which words it
        # hits depends on set-iteration order / PYTHONHASHSEED). Single alphabetic
        # words are handled only by the word-boundary token pass below.
        if " " in p or not p.isalpha():
            text = text.replace(p, " ")
            # Also try a version with trailing periods stripped so
            # "luis garcia jr." (canonical) matches "luis garcia jr" (query).
            p_no_period = re.sub(r'\.$| \.', ' ', p).strip()
            if p_no_period != p:
                text = text.replace(p_no_period, " ")
        for tok in p.split():
            # Strip trailing period so canonical "Jr." matches user's "jr"
            tok_norm = tok.rstrip(".,")
            if len(tok_norm) >= 2:
                text = re.sub(rf'\b{re.escape(tok_norm)}\b', ' ', text)
    return text


def _unexplained_words(lower: str, consumed: list) -> list:
    """Return meaningful words left in `lower` after removing the parser's
    consumed substrings and true filler. Non-empty → the parser ignored
    something and should bail."""
    text = _strip_consumed(lower, consumed)
    toks = re.findall(r"[a-z0-9.+]+", text)
    # Numbers (years, thresholds like "30", rate values like ".300") are NOT
    # ignored-qualifier signals — they're handled by season/threshold logic, and
    # flagging them would wrongly bail legit "30 HR seasons" queries. The guard
    # targets ignored qualifier WORDS (after / RISP / with / losses / vs …).
    return [t for t in toks
            if t and t not in _GUARD_FILLER and not re.fullmatch(r"[\d.]+", t)]


# Stat aliases, longest-first, for stripping a (possibly multi-word) stat phrase
# out of a query before the qualifier-word check. Single-char aliases (g, h, r,
# k) are excluded — too collision-prone with stray letters.
_STAT_ALIASES_DESC = None


def _residual_qualifier_words(lower: str, name, extra_consumed=None) -> list:
    """Like _unexplained_words but also strips EVERY stat alias (word-boundary)
    so a multi-word stat phrase ("home runs") is fully consumed. Used by the
    stat-bearing parsers (single stat, career, season count, catch-all) — what
    remains is genuine ignored-qualifier words."""
    global _STAT_ALIASES_DESC
    if _STAT_ALIASES_DESC is None:
        _STAT_ALIASES_DESC = sorted(
            (a for a in stat_alias_map.keys() if len(a) >= 2), key=len, reverse=True)
    text = _strip_consumed(lower, [name] + list(extra_consumed or []))
    for alias in _STAT_ALIASES_DESC:
        text = re.sub(rf'\b{re.escape(alias)}\b', ' ', text)
    toks = re.findall(r"[a-z0-9.+]+", text)
    # Strip trailing periods before the filler check: a name suffix the
    # alias layer already resolved can leave a bare "jr." / "sr." token
    # ("Ken Griffey Jr. career stats" → alias consumes "ken griffey jr",
    # the orphan "jr." must not read as an ignored qualifier and bail the
    # parser). Suffix words are in _GUARD_FILLER for the same reason.
    return [t for t in toks
            if t and t.rstrip(".") not in _GUARD_FILLER
            and not re.fullmatch(r"[\d.]+", t)]


_MONTH_NAME_MAP = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sept": 9, "sep": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}


_TW_UNIT_DAYS = {"day": 1, "week": 7, "month": 30, "year": 365}
_TW_RE = re.compile(
    r'\b((?:in|over|during|for|within)\s+)?(the\s+)?(?:last|past)\s+'
    r'(\d+)?\s*(day|week|month|year)s?\b')


def _trailing_window_since(lower: str):
    """Trailing rolling window: "(over/in the) last/past N {day|week|month|year}".

    Returns (since_date_iso, matched_text) or (None, None). Days/weeks/months
    always count. A YEAR counts as a rolling window only with an explicit number
    ("last 2 years") or an in/over/the wrapper ("over the last year") — bare
    "last year"/"last season" stays the previous SEASON (detect_season owns it).
    """
    from datetime import date as _date, timedelta
    m = _TW_RE.search(lower)
    if not m:
        return None, None
    prep, the_, num, unit = m.group(1), m.group(2), m.group(3), m.group(4)
    if unit == "year" and not num and not (prep or the_):
        return None, None
    n = int(num) if num else 1
    since = (_date.today() - timedelta(days=n * _TW_UNIT_DAYS[unit])).isoformat()
    return since, m.group(0).strip()


def _detect_date_bounds(lower: str):
    """Sub-season date bounds. Returns (since_date, end_date, consumed_text).
    'after/since/from [date]' → lower bound; 'before/through/until [date]' →
    upper bound; 'in the last N days' → lower bound. Month-based dates and
    'last N days' only — bare years (since 2020) are multi-season scope and
    are intentionally NOT matched here (the query engine owns those)."""
    import calendar
    from datetime import date as _date, timedelta
    today = _date.today()
    since = end = None
    consumed = []

    _tw_since, _tw_text = _trailing_window_since(lower)
    if _tw_since:
        since = _tw_since
        consumed.append(_tw_text)

    # "since/after the all-star break" → second half (since the ASG date).
    # "break" distinguishes the date reference from awards ("all-star
    # selections/appearances"), which should NOT be claimed here.
    if since is None and ("all-star break" in lower or "all star break" in lower):
        ym = re.search(r'\b(20\d{2})\b', lower)
        yr = int(ym.group(1)) if ym else (today.year if today.month >= 7 else today.year - 1)
        _ASG = {2021: "2021-07-13", 2022: "2022-07-19", 2023: "2023-07-11",
                2024: "2024-07-16", 2025: "2025-07-15", 2026: "2026-07-14"}
        since = _ASG.get(yr, f"{yr}-07-15")
        consumed.append("all-star break" if "all-star break" in lower else "all star break")
        consumed.append("since after the second half")

    def _resolve(month_str, day, year, is_end):
        month = _MONTH_NAME_MAP.get(month_str)
        if not month:
            return None
        yr = year if year is not None else (today.year if month <= today.month else today.year - 1)
        last = calendar.monthrange(yr, month)[1]
        d = day if day is not None else (last if is_end else 1)
        d = max(1, min(d, last))
        return f"{yr:04d}-{month:02d}-{d:02d}"

    def _find(quals, is_end):
        q = "|".join(quals)
        patterns = [
            (rf'\b(?:{q})\s+([a-z]+)\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\s*,?\s*(\d{{4}})\b', True, True),
            (rf'\b(?:{q})\s+([a-z]+)\.?\s+(\d{{4}})\b', False, True),
            (rf'\b(?:{q})\s+([a-z]+)\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b', True, False),
            (rf'\b(?:{q})\s+([a-z]+)\b', False, False),
        ]
        for pat, has_day, has_year in patterns:
            mm = re.search(pat, lower)
            if not mm or mm.group(1) not in _MONTH_NAME_MAP:
                continue
            day = int(mm.group(2)) if has_day else None
            year = int(mm.group(3 if (has_day and has_year) else 2)) if has_year else None
            resolved = _resolve(mm.group(1), day, year, is_end)
            if resolved:
                return resolved, mm.group(0)
        return None, None

    if since is None:
        since, c = _find(["since", "after", "from", "between"], is_end=False)
        if c:
            consumed.append(c)
    # "to"/"and" close a range ("from June 1 to July 31", "between June 1 and
    # July 31"); the patterns require a month, so a stray "to"/"and" won't fire.
    end, c = _find(["before", "through", "thru", "until", "to", "and"], is_end=True)
    if c:
        consumed.append(c)
    return since, end, " ".join(consumed)


def parse_player_date_range(input_str: str) -> Optional[dict]:
    """Single-player stats over a date range — 'Austin Wells stats after June 30
    2025', 'Soto in the last 30 days'. NON-GREEDY: requires an explicit date
    bound, and bails (unexplained-words guard) if anything meaningful is left
    over, so it never returns a partial answer. Runs before parse_month_query
    so 'after June 30' isn't misread as the month June."""
    lower = input_str.strip().lower()

    # Leaderboards / comparisons aren't single-player lookups.
    if any(w in lower for w in [
        "leaders", "leader", "leaderboard", "top ", "most ", "best ", "highest",
        "lowest", "fewest", "worst", "least", "who led", "who leads", "leading",
        " vs ", " versus ", " compared to ", " or "]):
        return None
    if contains_word("career", lower):
        return None

    since_date, end_date, date_text = _detect_date_bounds(lower)
    if since_date is None and end_date is None:
        return None

    name = find_player_in_text(lower)
    if not name:
        return None

    # Bail if a meaningful qualifier remains after removing the player, the date
    # phrase, AND any stat phrase (a specific stat like "home runs" just narrows
    # the line — we still show the full grid — so it must be consumed, not
    # flagged). Genuine leftovers ("vs lefties", "with RISP") route elsewhere.
    if _residual_qualifier_words(lower, name, extra_consumed=[date_text]):
        return None

    return {"name": name, "since_date": since_date, "end_date": end_date}


def parse_month_query(input_str: str) -> Optional[dict]:
    """Detect month queries. Returns dict with player_name, month, season."""
    lower = input_str.strip().lower()

    months: list[tuple[list[str], int]] = [
        (["january", "jan"], 1), (["february", "feb"], 2), (["march", "mar"], 3),
        (["april", "apr"], 4), (["may"], 5), (["june", "jun"], 6),
        (["july", "jul"], 7), (["august", "aug"], 8), (["september", "sept", "sep"], 9),
        (["october", "oct"], 10), (["november", "nov"], 11), (["december", "dec"], 12),
    ]

    detected_month: Optional[int] = None
    matched_month_word: Optional[str] = None
    for names, number in months:
        for month_name in names:
            if contains_word(month_name, lower):
                detected_month = number
                matched_month_word = month_name
                break
        if detected_month is not None:
            break

    if detected_month is None:
        return None

    # Exclude leaderboard patterns
    leaderboard_words = ["leaders", "leader", "leaderboard", "top ", "most ", "best ", "highest", "lowest",
                         "fewest", "worst", "least",
                         "who led", "who leads", "who hit the most", "who had the most", "leading"]
    if any(w in lower for w in leaderboard_words):
        return None

    if contains_word("career", lower):
        return None

    name = find_player_in_text(lower)
    if not name:
        return None

    season = detect_season(lower, default_to_most_recent=True) or _current_calendar_year()

    # Unexplained-words guard: a month query is "X in June [year]". A date
    # qualifier or day number ("after June 30", "since June") means this is a
    # date slice, not a month aggregate — bail so parse_player_date_range / the
    # query engine handles it. Consume player, month word, year, and a matched
    # stat; if anything meaningful remains, decline.
    _stat = match_stat(lower)
    consumed = [name, matched_month_word or "", str(season)]
    if _stat is not None:
        for alias in [getattr(_stat, "display", ""), getattr(_stat, "db_column", "")]:
            if alias:
                consumed.append(alias.replace("_", " "))
    leftover = _unexplained_words(lower, consumed)
    leftover = [w for w in leftover if match_stat(w) is None]
    if leftover:
        return None

    return {"player_name": name, "month": detected_month, "season": season}


def parse_leaderboard(input_str: str) -> Optional[dict]:
    """Detect leaderboard queries. Returns dict with stat, scope, limit, league."""
    lower = input_str.strip().lower()
    league_result = detect_league(lower)
    if league_result:
        lower = league_result[1]

    leaderboard_triggers = ["leaders", "leader", "leaderboard", "top ", "most ", "best ", "greatest ", "highest",
                            "lowest", "worst", "fewest",
                            "who led", "who leads", "who hit the most", "who had the most",
                            "who walked the most", "who struck out the most", "who stole the most",
                            "leading", "closest to", "come closest"]
    if not any(t in lower for t in leaderboard_triggers):
        return None

    # Reject team-aggregate questions
    team_aggregate_triggers = ["what team", "which team", "what teams", "which teams"]
    if any(t in lower for t in team_aggregate_triggers):
        return None

    # Reject "player single-season max" queries — when a specific player is
    # named AND the single-season-max pattern is present, the query is about
    # THAT player's career-best season, not an all-time leaderboard. Handled
    # by parse_player_single_season_max (step 25b in interceptor). Without
    # this guard, "Most home runs Hank Aaron ever hit in a season" matches
    # here and returns the all-time HR leaderboard, ignoring Aaron.
    has_single_season_max_phrase = bool(
        re.search(r'\bever\b', lower)
        or re.search(r'\bin (?:a|one|any|a single) season\b', lower)
        or re.search(r'\b(?:best|highest|peak|top|worst|lowest)\b(?:\s+\w+){0,3}\s+(?:season|year)\b', lower)
        or re.search(r'\bcareer[- ]?(?:high|best|worst|low|lowest|highest)\b', lower)
    )
    if has_single_season_max_phrase and find_player_in_text(lower):
        return None

    # Detect split context (count, pitch type, RISP, home/away, platoon)
    # This is handled by the split leaderboard builder, not a bail-out.
    split_context = _detect_split_context(lower)

    # Bail on qualifiers we truly can't handle (no split table, no builder)
    _truly_unhandled = [
        # Per-game queries (need game logs, not season totals)
        "in a game", "in one game", "in a single game", "per game",
        "game log", "single game",
        # Year-over-year / comparison
        "improved", "improvement", "decline", "drop", "increase",
        "from 20", "compared to",
        # Date range / half season
        "first half", "second half", "before the break", "after the break",
        # Conditional / multi-stat filters not handled by filtered_leaderboard
        "without", "while also", "with under ", "with fewer",
        # Count / frequency queries
        "how many player", "how many pitcher", "how many batter",
        "how many times",
        # Multi-game event counts
        "multi-hit", "multi hit", "multi-homer", "multi homer",
        "multi-hr", "multi hr", "multi home run",
        # Month-filtered leaderboards (need game logs + date filtering)
        "in january", "in february", "in march", "in april", "in may",
        "in june", "in july", "in august", "in september", "in october",
        "in jan ", "in feb ", "in mar ", "in apr ", "in jun ",
        "in jul ", "in aug ", "in sept ", "in oct ",
        # Game situations we don't have tables for
        "in the clutch", "close and late", "high leverage",
        "bases loaded", "bases empty",
        "day game", "night game",
    ]
    if any(t in lower for t in _truly_unhandled):
        return None

    # "closest to .400" -> batting average
    stat: Optional[StatInfo] = None
    closest_to_threshold: Optional[float] = None
    is_closest_to = "closest to" in lower or "come closest" in lower

    if is_closest_to:
        stat = match_stat(lower)
        if stat is None and re.search(r'\.\d{3}', lower):
            stat = stat_alias_map.get("avg") or stat_alias_map.get("batting average")
        m = re.search(r'(?:closest to|come closest to)\s+(\d+\.?\d*|\.\d+)', lower)
        if m:
            closest_to_threshold = float(m.group(1))
    else:
        stat = match_stat(lower)

    if stat is None:
        return None

    # Check for "since [player name]" or "since [year]"
    since_year: Optional[int] = None
    if "since " in lower:
        since_idx = lower.index("since ")
        after_since = lower[since_idx + 6:]

        # Check for explicit year after "since"
        m = re.search(r'\b(189[89]|19\d{2}|20[0-2]\d)\b', after_since)
        if m:
            since_year = int(m.group(1))

        # Check for player name after "since"
        if since_year is None:
            for name in sorted_names:
                name_lower = name.lower()
                if after_since.startswith(name_lower) or contains_word(name_lower, after_since):
                    if is_closest_to and closest_to_threshold is not None:
                        since_year = _lookup_last_threshold_year(name, stat, closest_to_threshold)
                    else:
                        since_year = _lookup_post_career_year(name)
                    break

            # Also check last names
            if since_year is None:
                for last_name, players in last_name_index.items():
                    if len(players) == 1 and contains_word(last_name, after_since):
                        if is_closest_to and closest_to_threshold is not None:
                            since_year = _lookup_last_threshold_year(players[0], stat, closest_to_threshold)
                        else:
                            since_year = _lookup_post_career_year(players[0])
                        break

    # Check for "over the last N years", "last decade", etc.
    if since_year is None:
        current_year = _current_calendar_year()
        if "last decade" in lower or "past decade" in lower:
            since_year = current_year - 10
        elif "this century" in lower or "21st century" in lower:
            since_year = 2000
        else:
            m = re.search(r'(?:last|past)\s+(\d+)\s+years?', lower)
            if m:
                n = int(m.group(1))
                if 1 < n <= 100:
                    since_year = current_year - n

    # Also check for decade/century/N-year range patterns
    if since_year is None:
        since_year = _detect_since_year(lower)

    # Only reject player names if not in "since [player]" context
    if since_year is None:
        if _has_player_name(lower):
            return None

    # Extract limit from "top N" pattern
    limit = 50
    m = re.search(r'top\s+(\d+)', lower)
    if m:
        limit = max(1, min(int(m.group(1)), 50))

    # Determine scope
    if since_year is not None:
        scope = f"all_time_since_{since_year}"
    elif "career" in lower:
        scope = "career"
    elif ("all time" in lower or "all-time" in lower or "single season" in lower
          or "in a season" in lower or "in a year" in lower or "ever" in lower
          or "in history" in lower or "record" in lower):
        scope = "all_time"
    else:
        # Check for explicit season first
        season = detect_season(lower, default_to_most_recent=False)
        if season is None:
            # Past tense ("who led", "who had the most") → last completed season
            past_tense = any(p in lower for p in ["who led", "who had", "who hit the most"])
            if past_tense:
                season = _current_calendar_year() - 1
            else:
                season = _current_calendar_year()
        scope = f"season_{season}"

    rookie = _detect_rookie(lower)
    position = _detect_position(lower)
    pitcher_role = _detect_pitcher_role(lower)

    # Detect sort direction — "worst" and "fewest" mean ascending for counting stats
    sort_asc = any(t in lower for t in ["worst", "fewest"])

    return {
        "stat": stat, "scope": scope, "limit": limit,
        "league": league_result[0] if league_result else None,
        "pitcher_role": pitcher_role, "sort_asc": sort_asc,
        "rookie": rookie, "position": position,
        "split_context": split_context,
    }


def parse_stat_definition(input_str: str) -> Optional[dict]:
    """Detect stat definition queries. Returns dict with abbrev, display_name, definition."""
    lower = input_str.strip().lower()

    triggers = ["what is ", "what's ", "what are ", "what does ", "what do ",
                "explain ", "define ", "meaning of ", "definition of ",
                "tell me about ", "describe ", "how is ", "how do you calculate "]
    suffix_triggers = [" mean", " meaning", " stand for", " measure",
                       " calculated", " definition"]

    has_trigger = any(t in lower for t in triggers) or any(t in lower for t in suffix_triggers)
    if not has_trigger:
        return None

    # Reject if the query has clear player-context markers — possessive
    # pronouns ("his/her/their") or possessive name forms ("Judges OPS",
    # "Judge's OPS") strongly imply a player-stat lookup, not a definition.
    # Without this, queries like "what's Judges OPS in his first at bat of
    # any game" match "what's" and then match_stat picks "G" out of "game".
    if re.search(r'\b(his|her|their|whose)\b', lower):
        return None
    if re.search(r"\b\w+(?:'s|s')\s", lower):
        return None

    # Reject if a player name is present (also catches non-possessive forms)
    if _has_player_name(lower):
        return None

    # Reject overly long queries — real stat definitions are short ("what is
    # OPS", "explain BABIP calculation"). 8+ word queries tend to be
    # situational lookups that happen to contain a trigger phrase.
    if len(lower.split()) > 8:
        return None

    # Try matching via stat_alias_map
    stat = match_stat(lower)
    if stat:
        definition = _stat_definitions.get(stat.display_abbrev)
        if definition:
            return {"abbrev": stat.display_abbrev, "display_name": stat.display_name,
                    "definition": definition}

    # Try direct abbreviation lookup
    direct_abbrevs = ["war", "wrc+", "woba", "fip", "k", "pa", "sf", "1b"]
    for abbrev in direct_abbrevs:
        if contains_word(abbrev, lower):
            key = abbrev.upper()
            if key == "WRC+":
                lookup_key = "wRC+"
            elif key == "WOBA":
                lookup_key = "wOBA"
            else:
                lookup_key = key
            definition = _stat_definitions.get(lookup_key)
            if definition:
                display = {"war": "WAR", "wrc+": "wRC+", "woba": "wOBA", "fip": "FIP"}.get(abbrev, key)
                return {"abbrev": display, "display_name": display, "definition": definition}

    return None


def _extract_threshold(text: str, skip_years: bool = True,
                       stat: Optional['StatInfo'] = None) -> Optional[float]:
    """Extract a numeric threshold from text, skipping 4-digit years.

    Normalizes whole-number values for rate stats stored on a 0-1 scale:
    - "800 OPS" → .800, "350 OBP" → .350, "500 SLG" → .500
    - Only applies to AVG, OBP, SLG, OPS, ISO, BABIP, BAA (not ERA, K/9, OPS+, ERA+)
    - Also handles batting-average verb shorthand: "hitting 300" → .300
    """
    # Stats stored on a 0-1 scale where 3-digit whole numbers mean ÷1000
    _sub_one_stats = {"batting_avg", "obp", "slg", "ops", "iso", "babip",
                      "batting_avg_against"}

    _avg_verb = re.search(r'(?:batted|hit|batting|hitting|bat)\b', text) is not None
    for m in re.finditer(r'(\d+\.?\d*|\.\d+)\+?', text):
        num_str = m.group(1)
        try:
            num = float(num_str)
        except ValueError:
            continue
        if skip_years:
            int_num = int(num)
            if 1900 <= int_num <= 2099 and "." not in num_str:
                # Check if this number is directly adjacent to a stat keyword —
                # "2000 strikeouts" is a threshold, not a year
                after = text[m.end():].lstrip("+").lstrip()
                stat_adjacent = stat and any(
                    after.startswith(kw) for kw in [
                        stat.display_name.lower(), stat.display_abbrev.lower(),
                        stat.db_column.replace("_", " "),
                    ]
                )
                if not stat_adjacent:
                    continue
        # "hitting 300" → .300  (whole-number batting avg shorthand)
        # Only apply when no specific stat is provided, or stat IS a sub-1 rate stat.
        # "200 hit club" has stat=hits (counting), so "hit" here is a noun, not a verb.
        if _avg_verb and 200 <= num <= 400 and "." not in num_str:
            if stat is None or (stat and stat.db_column in _sub_one_stats):
                return num / 1000
        # "800 OPS", "350 OBP", etc. → divide by 1000 for sub-1 rate stats
        if (stat and stat.db_column in _sub_one_stats
                and 100 <= num <= 999 and "." not in num_str):
            return num / 1000
        return num
    return None


def parse_threshold(input_str: str) -> Optional[dict]:
    """Detect threshold queries. Returns dict with stat, threshold, comparison, season, league."""
    lower = input_str.strip().lower()
    league_result = detect_league(lower)
    if league_result:
        lower = league_result[1]

    if _has_player_name(lower):
        return None

    # Reject qualifiers we can't handle — let Haiku generate the SQL
    _threshold_bail = [
        "in a game", "in one game", "in a single game", "per game",
        "improved", "decline", "drop", "from 20", "compared to",
        "first half", "second half", "before the break", "after the break",
        "without", "while also",
        "how many player", "how many pitcher", "how many batter",
        "multi-hit", "multi hit", "multi-homer", "multi homer",
        "multi-hr", "multi hr",
    ]
    if any(t in lower for t in _threshold_bail):
        return None

    # Reject leaderboard triggers
    leaderboard_words = ["leaders", "leader", "leaderboard", "top ", "most ", "best ",
                         "highest", "lowest", "who led", "who leads", "leading"]
    if any(w in lower for w in leaderboard_words):
        return None

    stat = match_stat(lower)
    if not stat:
        # "batted .300" / "hit over .300" / "hitting 300" — infer batting average
        if re.search(r'(?:batted|hit|batting|hitting)\s+(?:over\s+|above\s+|at least\s+)?\.?\d', lower) or \
           re.search(r'(?:batted|hit|batting|hitting)\s+(?:over\s+|above\s+|at least\s+)?\d{3}\b', lower):
            stat = stat_alias_map.get("batting average") or stat_alias_map.get("avg")
        if not stat:
            return None

    threshold = _extract_threshold(lower, stat=stat)
    if threshold is None:
        return None

    # Reject compound thresholds (e.g. "hit .300 with 30 HR") — two or more
    # non-year numbers means multiple stat filters, too complex for this parser.
    non_year_nums = [
        float(m.group(1))
        for m in re.finditer(r'(\d+\.?\d*|\.\d+)\+?', lower)
        if not (1900 <= int(float(m.group(1))) <= 2099 and "." not in m.group(1))
    ]
    if len(non_year_nums) > 1:
        return None

    under_patterns = ["under ", "fewer than ", "less than ", "below ", "no more than ",
                      "or fewer", "or less"]
    comparison = "<=" if any(p in lower for p in under_patterns) else ">="

    # Detect rookie filter
    rookie = _detect_rookie(lower)

    # Detect since_year BEFORE detect_season so "since 2000" isn't treated as season=2000
    since_year = _detect_since_year(lower)

    # Only look for a specific season if no since_year was found
    season = detect_season(lower) if since_year is None else None

    position = _detect_position(lower)

    return {
        "stat": stat, "threshold": threshold, "comparison": comparison,
        "season": season, "league": league_result[0] if league_result else None,
        "since_year": since_year, "rookie": rookie, "position": position,
    }


def parse_milestone(input_str: str) -> Optional[dict]:
    """Detect milestone queries. Returns dict with stat, threshold, since, league."""
    lower = input_str.strip().lower()
    league_result = detect_league(lower)
    if league_result:
        lower = league_result[1]

    # "how many players" is a count query (handled separately), not a milestone
    milestone_triggers = ["how many times", "how many seasons",
                          "how often", "has anyone ever", "has anybody ever",
                          "has anyone", "has a player ever", "ever hit", "ever had",
                          "ever batted", "ever pitched", "ever thrown", "ever won"]
    if not any(t in lower for t in milestone_triggers):
        return None

    if _has_player_name(lower):
        return None

    stat = match_stat(lower)
    if not stat:
        return None

    # Extract threshold (skip years, but use 1898 as lower bound)
    threshold: Optional[float] = None
    for m in re.finditer(r'(\d+\.?\d*|\.\d+)\+?', lower):
        num_str = m.group(1)
        try:
            num = float(num_str)
        except ValueError:
            continue
        int_num = int(num)
        if 1898 <= int_num <= 2099 and "." not in num_str:
            continue
        threshold = num
        break

    if threshold is None:
        return None

    # Reject compound queries (e.g. "has anyone hit 300 with 30 sbs")
    non_year_nums = [
        float(m.group(1))
        for m in re.finditer(r'(\d+\.?\d*|\.\d+)\+?', lower)
        if not (1900 <= int(float(m.group(1))) <= 2099 and "." not in m.group(1))
    ]
    if len(non_year_nums) > 1:
        return None

    since = _detect_since_year(lower) or detect_season(lower, default_to_most_recent=False)
    return {
        "stat": stat, "threshold": threshold, "since": since,
        "league": league_result[0] if league_result else None,
    }


def parse_filtered_leaderboard(input_str: str) -> Optional[dict]:
    """Detect filtered leaderboard queries. Returns dict with rank_stat, filter_stat, etc."""
    lower = input_str.strip().lower()
    league_result = detect_league(lower)
    if league_result:
        lower = league_result[1]

    leaderboard_triggers = ["most ", "highest ", "lowest ", "best ", "fewest "]
    if not any(t in lower for t in leaderboard_triggers):
        return None

    separators = ["with ", "while ", "among players with ", "among those with "]
    separator = None
    for s in separators:
        if s in lower:
            separator = s
            break
    if not separator:
        return None

    sep_idx = lower.index(separator)
    rank_part = lower[:sep_idx]
    filter_part = lower[sep_idx + len(separator):]

    if _has_player_name(lower):
        return None

    # If the rank_part contains a threshold number, this is a compound threshold
    # query (e.g. "players who hit .300 with 30 HR"), not a filtered leaderboard.
    if re.search(r'(?:over|above|more than|at least)?\s*\.?\d+', rank_part) and \
            _extract_threshold(rank_part) is not None:
        return None

    rank_stat = match_stat(rank_part)
    if not rank_stat:
        return None

    filter_stat = match_stat(filter_part)
    if not filter_stat:
        return None

    if rank_stat.db_column == filter_stat.db_column:
        return None

    threshold = _extract_threshold(filter_part)
    if threshold is None:
        return None

    under_patterns = ["under ", "fewer than ", "less than ", "below ", "no more than ",
                      "or fewer", "or less"]
    comparison = "<=" if any(p in filter_part for p in under_patterns) else ">="

    season = detect_season(lower)
    return {
        "rank_stat": rank_stat, "filter_stat": filter_stat,
        "threshold": threshold, "comparison": comparison,
        "season": season, "limit": 10,
        "league": league_result[0] if league_result else None,
    }


def parse_superlative(input_str: str) -> Optional[dict]:
    """Detect superlative queries. Returns dict with stat, threshold, superlative, league."""
    lower = input_str.strip().lower()
    league_result = detect_league(lower)
    if league_result:
        lower = league_result[1]

    if "youngest" in lower or "how young" in lower:
        superlative = "youngest"
    elif "oldest" in lower or "how old" in lower:
        superlative = "oldest"
    elif any(p in lower for p in ("first player", "first to", "who was the first", "first person")):
        superlative = "first"
    elif any(p in lower for p in ("last player", "last to", "most recent",
                                   "last time someone", "when was the last", "last person")):
        superlative = "last"
    else:
        return None

    if _has_player_name(lower):
        return None

    stat = match_stat(lower)
    if not stat:
        return None

    threshold = _extract_threshold(lower)
    if threshold is None:
        return None

    since_year = _detect_since_year(lower)

    return {
        "stat": stat, "threshold": threshold, "superlative": superlative,
        "league": league_result[0] if league_result else None,
        "since_year": since_year,
    }


def parse_multi_threshold(input_str: str) -> Optional[dict]:
    """Detect compound threshold queries like '.300 with 30 HR' or '200 K and sub-3.00 ERA'.
    Returns dict with filters (list of {stat, threshold, comparison}), season, is_pitching."""
    lower = input_str.strip().lower()
    league_result = detect_league(lower)
    if league_result:
        lower = league_result[1]

    if _has_player_name(lower):
        return None

    # Split on all separators to get individual conditions
    # e.g. "pitchers with 200+ K and sub-3.00 ERA" → ["pitchers", "200+ K", "sub-3.00 ERA"]
    separators = [" with ", " and ", " while ", " plus "]
    if not any(s in lower for s in separators):
        return None

    # Replace all separators with a common delimiter, then split
    temp = lower
    for s in separators:
        temp = temp.replace(s, " |SEP| ")
    parts = [p.strip() for p in temp.split("|SEP|") if p.strip()]
    if len(parts) < 2:
        return None

    under_patterns = ["under ", "fewer than ", "less than ", "below ",
                      "sub-", "sub ", "no more than ", "or fewer", "or less"]

    filters = []
    for part in parts:
        part = part.strip()

        # Determine comparison direction
        comparison = ">="
        for up in under_patterns:
            if up in part:
                comparison = "<="
                break

        stat = match_stat(part)
        if not stat:
            # "batted .300" / "hit over .300" / "hitting 300" — infer batting average
            if re.search(r'(?:batted|hit|batting|hitting)\s+(?:over\s+|above\s+|at least\s+)?\.?\d', part) or \
               re.search(r'(?:batted|hit|batting|hitting)\s+(?:over\s+|above\s+|at least\s+)?\d{3}\b', part):
                stat = stat_alias_map.get("batting average") or stat_alias_map.get("avg")
            if not stat:
                continue

        threshold = _extract_threshold(part, stat=stat)
        if threshold is None:
            continue

        filters.append({"stat": stat, "threshold": threshold, "comparison": comparison})

    if len(filters) < 2:
        return None

    # Detect pitching context
    pitching_stats = {"earned_run_avg", "wins", "losses", "saves", "whip",
                      "k_per_9", "bb_per_9", "innings_pitched", "quality_starts",
                      "complete_games", "batting_avg_against"}
    is_pitching = "pitcher" in lower or any(f["stat"].db_column in pitching_stats for f in filters)

    # Detect rookie filter
    rookie = _detect_rookie(lower)

    # Detect since_year BEFORE detect_season so "since 2000" isn't treated as season=2000
    since_year = _detect_since_year(lower)
    season = detect_season(lower) if since_year is None else None

    return {
        "filters": filters, "season": season, "is_pitching": is_pitching,
        "league": league_result[0] if league_result else None,
        "since_year": since_year, "rookie": rookie,
    }


def parse_composite_threshold(input_str: str) -> Optional[int]:
    """Detect 30/30, 40/40 type queries. Returns the threshold number."""
    lower = input_str.strip().lower()

    m = re.search(r'\b(20|25|30|40|50)[/\- ](20|25|30|40|50)\b', lower)
    if not m:
        return None
    n1, n2 = int(m.group(1)), int(m.group(2))
    if n1 != n2:
        return None

    triggers = ["how many", "who has", "who have", "most", "players",
                "seasons", "club", "members", "times", "ever", "history",
                "list", "all time", "all-time", "has anyone", "has there",
                "how often", "tell me about"]
    has_trigger = any(t in lower for t in triggers)

    words = lower.split()
    is_short_query = len(words) <= 4

    if not has_trigger and not is_short_query:
        return None

    return n1


def parse_triple_crown(input_str: str) -> bool:
    """Detect triple crown queries."""
    return "triple crown" in input_str.strip().lower()


def parse_consecutive_streak(input_str: str) -> Optional[dict]:
    """Detect consecutive streak queries. Returns dict with type, player_name, season."""
    lower = input_str.strip().lower()

    # "How many hitting streaks" is a counting query — let the query engine handle it
    if re.search(r'how many|how common|how often|count of', lower):
        return None

    # "active/current hitting streak" needs trailing streak logic — let the query engine handle it
    if any(w in lower for w in ["active", "current"]):
        return None

    on_base_patterns = ["on-base streak", "on base streak", "reaching base streak",
                        "onbase streak", "consecutive games reaching base",
                        "consecutive games on base"]
    hit_patterns = ["hitting streak", "hit streak", "game hitting streak",
                    "game hit streak", "consecutive hit", "consecutive game hit",
                    "consecutive games with a hit"]

    if any(p in lower for p in on_base_patterns):
        streak_type = "onbase"
    elif any(p in lower for p in hit_patterns):
        streak_type = "hit"
    else:
        return None

    # Try to find a player name
    player_name = find_player_in_text(lower)
    if not player_name:
        result = match_player_with_prominence(lower)
        player_name = result[0] if result else None

    season = detect_season(lower, default_to_most_recent=False)

    _cs_consumed = (on_base_patterns + hit_patterns
                    + ["game", "games", "consecutive", "streak", "reaching", "base", "in a row"])
    return {"type": streak_type, "player_name": player_name, "season": season,
            "consumed": _cs_consumed}


# "{player} vs/against {team}" markers. Used by parse_player_vs_team AND by
# parse_team_stats (to bail when the query is really a player-vs-team split —
# _has_player_name misses ambiguous surnames like "Judge").
_VS_TOKENS = (" vs ", " vs. ", " v. ", " versus ", " against ", " facing ")


def parse_player_vs_team(input_str: str) -> Optional[dict]:
    """Detect "{player} vs/against {team}" → that player's line vs the opponent.

    Returns {"name", "opponent_code", "season"} or None. season is None for a
    career split (no year given), else the explicit/relative year. Uses
    find_player_in_text (resolves ambiguous surnames by prominence) so "Judge
    vs the Red Sox" works, where _has_player_name would miss "Judge".
    """
    padded = " " + input_str.strip().lower() + " "
    if not any(tok in padded for tok in _VS_TOKENS):
        return None
    name = find_player_in_text(input_str)
    if not name:
        return None
    opponent_code = match_team(input_str)
    if not opponent_code:
        return None
    season = detect_season(input_str)
    # Consumed = every alias of the matched team + the vs/against markers, so a
    # trailing unsupported qualifier ("...on Tuesdays") is flagged and bails.
    _vt_consumed = [a for a, c in team_alias_map.items() if c == opponent_code] + \
                   ["vs", "vs.", "v.", "versus", "against", "facing"]
    return {"name": name, "opponent_code": opponent_code, "season": season,
            "consumed": _vt_consumed}


def parse_team_stats(input_str: str) -> Optional[dict]:
    """Detect team stat queries. Returns dict with team_code, stat, season."""
    lower = input_str.strip().lower()

    team_code = match_team(lower)
    if not team_code:
        return None

    if _has_player_name(lower):
        return None

    # "{player} vs the {team}" is a player-vs-opponent split, not a team query.
    # _has_player_name misses ambiguous surnames, so check vs/against + player
    # explicitly and bail (parse_player_vs_team handles it upstream).
    if any(tok in f" {lower} " for tok in _VS_TOKENS) and find_player_in_text(input_str):
        return None

    stat = match_stat(lower)
    season = detect_season(lower, default_to_most_recent=True) or _current_calendar_year()
    return {"team_code": team_code, "stat": stat, "season": season}


def parse_team_total(input_str: str) -> Optional[dict]:
    """Detect team total queries. Returns dict with team_code, stat, season."""
    lower = input_str.strip().lower()

    team_code = match_team(lower)
    if not team_code:
        return None

    stat = match_stat(lower)
    if not stat:
        return None

    total_signals = ["how many", "total", "combined", "as a team",
                     "did the", "do the", "did they", "do they"]
    if not any(s in lower for s in total_signals):
        return None

    if _has_player_name(lower):
        return None

    season = detect_season(lower, default_to_most_recent=True) or _current_calendar_year()
    return {"team_code": team_code, "stat": stat, "season": season}


def parse_team_ranking(input_str: str) -> Optional[dict]:
    """Detect team ranking queries. Returns dict with stat, season."""
    lower = input_str.strip().lower()

    triggers = ["what team", "which team", "what teams", "which teams",
                "team with the most", "team with the highest", "team with the lowest",
                "team with the fewest", "rank teams", "team rankings",
                "teams by"]
    if not any(t in lower for t in triggers):
        return None

    stat = match_stat(lower)
    if not stat:
        return None

    season = detect_season(lower, default_to_most_recent=True) or _current_calendar_year()
    return {"stat": stat, "season": season}


def parse_team_record(input_str: str) -> Optional[dict]:
    """Detect team W-L record queries.

    Triggers: "Yankees record", "what's the Mets record", "how are the Cubs
    doing", "Dodgers W-L", "Phillies wins this season".

    Returns {"team_code": str, "season": int} or None.
    """
    lower = input_str.strip().lower()

    team_code = match_team(lower)
    if not team_code:
        return None

    if _has_player_name(lower):
        return None

    # Trigger words/phrases. Be specific to avoid hijacking other team queries.
    record_triggers = [
        " record", "w-l", "w/l", "win-loss", "wins and losses",
        "how are the", "how have the", "how did the",
        "are the", " standing", " standings",
    ]
    # Bare "wins" / "losses" only counts if no other stat keyword present.
    has_record_trigger = any(t in lower for t in record_triggers)
    if not has_record_trigger:
        # Fall back to bare "wins"/"losses" — but only when the team is the
        # subject (avoids "most home runs in Yankees wins" kind of queries).
        if not re.search(r"\b(wins|losses)\b", lower):
            return None
        # Reject if a non-W/L stat is also mentioned (avoids "Mets HR in wins")
        if match_stat(lower) not in (None, "wins", "losses"):
            return None

    season = detect_season(lower, default_to_most_recent=True) or _current_calendar_year()
    return {"team_code": team_code, "season": season}


def parse_team_game_score(input_str: str) -> Optional[dict]:
    """Detect single-game score lookup queries.

    Triggers: "Yankees score yesterday", "did the Mets win last night",
    "what was the Dodgers game tonight", "Yankees vs Red Sox last night".

    Returns {"team_code": str, "opponent_code": str|None, "date_keyword": str}
    where date_keyword is one of: 'yesterday', 'last night', 'tonight',
    'today', or a specific YYYY-MM-DD date.
    """
    lower = input_str.strip().lower()

    team_code = match_team(lower)
    if not team_code:
        return None

    if _has_player_name(lower):
        return None

    # Date keyword required — otherwise this is just a generic team query.
    date_kw = None
    for kw in ("yesterday", "last night", "tonight", "today"):
        if kw in lower:
            date_kw = kw
            break
    # Also accept explicit dates like "april 19" or "2026-04-19"
    if date_kw is None:
        m = re.search(r"\b(20\d{2})-(\d{2})-(\d{2})\b", lower)
        if m:
            date_kw = m.group(0)
    if date_kw is None:
        # Score-context triggers without a date: "what was the score of the
        # last yankees game", "did the mets win". Only accept if no
        # ambiguity with broader stats queries.
        score_triggers = [" score", " win ", " won ", " lost ", " win?", " won?"]
        if not any(t in lower for t in score_triggers):
            return None
        date_kw = "last_game"  # most recent game by this team

    # Optional opponent (for "Yankees vs Red Sox" style). Split on the vs
    # marker and re-match each half — match_team scans the full string and
    # may pick the most-specific name regardless of position, so we need
    # explicit pre/post split to assign subject vs opponent correctly.
    opponent_code = None
    vs_match = re.search(r"\b(?:vs\.?|versus|against)\b", lower)
    if vs_match:
        pre = lower[:vs_match.start()]
        post = lower[vs_match.end():]
        pre_team = match_team(pre)
        post_team = match_team(post)
        if pre_team and post_team and pre_team != post_team:
            team_code = pre_team
            opponent_code = post_team
        elif post_team and post_team != team_code:
            opponent_code = post_team

    return {
        "team_code": team_code,
        "opponent_code": opponent_code,
        "date_keyword": date_kw,
    }


def parse_single_game_extreme(input_str: str) -> Optional[dict]:
    """Detect per-game extreme queries: 'most K in one game', 'most HR in a single game'.
    Returns dict with stat, season, is_pitching, position."""
    lower = input_str.strip().lower()

    game_triggers = ["in a game", "in one game", "in a single game"]
    if not any(t in lower for t in game_triggers):
        return None

    leaderboard_triggers = ["most ", "best ", "highest", "record"]
    if not any(t in lower for t in leaderboard_triggers):
        return None

    stat = match_stat(lower)
    if not stat:
        return None

    # Detect pitching context
    pitching_context = any(w in lower for w in ["pitched", "pitching", "pitcher", "pitchers"])
    is_pitching = is_pitching_stat(stat) or pitching_context

    season = detect_season(lower)
    position = _detect_position(lower)

    return {
        "stat": stat, "season": season, "is_pitching": is_pitching,
        "position": position,
    }


def parse_count_query(input_str: str) -> Optional[dict]:
    """Detect count queries: 'how many players hit 30 HR in 2025'.
    Returns dict with stat, threshold, season, is_pitching."""
    lower = input_str.strip().lower()

    count_triggers = ["how many player", "how many pitcher", "how many batter",
                      "how many hitter"]
    if not any(t in lower for t in count_triggers):
        return None

    stat = match_stat(lower)
    if not stat:
        return None

    # Handle word numbers and implicit threshold
    threshold = _extract_threshold(lower, stat=stat)
    if threshold is None:
        # Check for word numbers: "at least one", "hit a home run"
        _word_numbers = [
            ("fifty", 50), ("forty", 40), ("thirty", 30), ("twenty", 20),
            ("ten", 10), ("five", 5), ("four", 4), ("three", 3), ("two", 2), ("one", 1),
        ]
        for word, num in _word_numbers:
            if re.search(rf'\b{word}\b', lower):
                threshold = float(num)
                break
    if threshold is None:
        # "how many players hit a home run" — implicit threshold of 1
        threshold = 1.0

    pitching_context = any(w in lower for w in ["pitched", "pitching", "pitcher", "pitchers"])
    is_pitching = is_pitching_stat(stat) or pitching_context

    season = detect_season(lower)
    position = _detect_position(lower)

    return {
        "stat": stat, "threshold": threshold, "season": season,
        "is_pitching": is_pitching, "position": position,
    }


def parse_player_game_window(input_str: str) -> Optional[dict]:
    """Detect player + game-window queries like:
    - 'most hits in first 13 games of any Stanton season'
    - 'Judge stats in his last 20 games'
    - 'how did Ohtani do in his first 5 games this year'
    - 'Soto first 10 games 2025'

    Returns dict with name, window_type ('first' or 'last'), n_games, stat (optional),
    season (optional, None = all seasons), or None.
    """
    lower = input_str.strip().lower()
    # Strip possessives for player matching
    # "stanton's" → "stanton", "stantons" → "stanton"
    cleaned = re.sub(r"'s\b|\u2019s\b", "", lower)
    cleaned = re.sub(r"s'\b|s\u2019\b", "s", cleaned)
    # Also handle bare possessive without apostrophe: "stantons seasons" → "stanton seasons"
    # Try to find player both with and without trailing 's' stripped from each word
    cleaned_no_s = re.sub(r'(\w+)s\b', r'\1', cleaned)

    # Normalize spelled-out numbers ("past five games" → "past 5 games"). Voice
    # recognition often produces words instead of digits.
    _word_to_digit = {
        "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
        "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
        "eleven": "11", "twelve": "12", "thirteen": "13", "fourteen": "14", "fifteen": "15",
        "twenty": "20", "thirty": "30", "forty": "40", "fifty": "50", "sixty": "60",
    }
    cleaned = re.sub(
        r'\b(' + '|'.join(_word_to_digit.keys()) + r')\b',
        lambda m: _word_to_digit[m.group(1)],
        cleaned,
    )
    cleaned_no_s = re.sub(
        r'\b(' + '|'.join(_word_to_digit.keys()) + r')\b',
        lambda m: _word_to_digit[m.group(1)],
        cleaned_no_s,
    )

    # Detect "first/last N games|starts|appearances". For pitchers, "starts"
    # and "appearances" are valid synonyms — game_pitching_logs is one row
    # per appearance, so the same window math applies. ("at-bats" is NOT
    # included here: it implies per-PA aggregation we don't have a table
    # for; queries with that phrasing should fall through to Haiku/Sonnet.)
    window_match = re.search(
        r'\b(first|last|past|previous|recent|opening|final)\s+(\d+)\s*'
        r'(?:career\s+)?'
        r'(games?|starts?|appearances?)\b',
        cleaned,
    )
    if not window_match:
        return None

    window_type = "first" if window_match.group(1) in ("first", "opening") else "last"
    n_games = int(window_match.group(2))
    window_noun_raw = window_match.group(3)
    # Normalize for display: "starts" → "Starts", "games" → "Games", etc.
    if window_noun_raw.startswith("start"):
        window_noun = "Starts"
    elif window_noun_raw.startswith("appearance"):
        window_noun = "Appearances"
    else:
        window_noun = "Games"
    if n_games < 1 or n_games > 162:
        return None

    # Find player name — try possessive-stripped version first (more likely to match
    # full names like "McMahon" vs "McMahons"), then original
    player = find_player_in_text(cleaned_no_s)
    if not player:
        player = find_player_in_text(cleaned)
    if not player:
        for prefix in ["most ", "best ", "how did ", "how many ", "what were "]:
            for text in [cleaned_no_s, cleaned]:
                if text.startswith(prefix):
                    player = find_player_in_text(text[len(prefix):])
                    if player:
                        break
            if player:
                break
    if not player:
        return None

    # Detect stat (optional — None means show full stat line)
    # Strip the window phrase before matching to avoid "games"/"starts" being matched as a stat
    stat_text = re.sub(
        r'\b(first|last|past|previous|recent|opening|final)\s+\d+\s*'
        r'(?:career\s+)?(?:games?|starts?|appearances?)\b',
        '', cleaned,
    )
    stat = match_stat(stat_text)
    # "games" as a stat is almost never what the user means here
    if stat and stat.db_column == "games":
        stat = None

    # Detect season (None = compare across all seasons)
    season = detect_season(cleaned, default_to_most_recent=False)

    # "any season" / "any of his seasons" / "each season" = all seasons comparison
    if any(p in lower for p in ["any season", "any of", "each season", "every season", "per season"]):
        season = None

    return {
        "name": player,
        "window_type": window_type,
        "n_games": n_games,
        "window_noun": window_noun,
        "stat": stat,
        "season": season,
    }


def parse_player_single_season_max(input_str: str) -> Optional[dict]:
    """Detect "[player]'s career-best single-season [stat]" queries.

    Returns dict with name, stat, direction ("max" or "min"), or None.

    Matches phrasings like:
      - "Most home runs Hank Aaron ever hit in a season"
      - "Most HR Hank Aaron ever hit"
      - "What's the most HR Aaron Judge ever had"
      - "Best HR season Babe Ruth ever had"
      - "Aaron Judge's career high in HR"
      - "Aaron Judge's career-best OPS"
      - "Lowest ERA Pedro Martinez ever had"
      - "Fewest strikeouts Greg Maddux ever had in a season"

    NOT matched (handled by other parsers):
      - "Hank Aaron career home runs" → career total (parse_career_lookup)
      - "Hank Aaron home runs" → most recent season (parse_single_stat_lookup)
    """
    lower = input_str.strip().lower()

    # Need a superlative — "most" / "best" / "highest" / "fewest" / "worst" / "lowest" / "career high"
    has_high_super = bool(re.search(r'\b(?:most|best|highest|peak|top)\b', lower))
    has_low_super = bool(re.search(r'\b(?:fewest|worst|lowest|least)\b', lower))
    has_career_super = bool(re.search(r'\bcareer[- ]?(?:high|best|worst|low|lowest|highest)\b', lower))
    if not (has_high_super or has_low_super or has_career_super):
        return None

    # Need single-season-max context — at least one of these signals:
    #   "ever", "in a season", "in one season", "in any season", "in a single season",
    #   "best season", "highest season", "peak season", "career high", "career best"
    has_ever = bool(re.search(r'\bever\b', lower))
    has_in_season_phrase = bool(re.search(r'\bin (?:a|one|any|a single) season\b', lower))
    has_best_season_phrase = bool(re.search(r'\b(?:best|highest|peak|top|worst|lowest)\b(?:\s+\w+){0,3}\s+(?:season|year)\b', lower))
    if not (has_ever or has_in_season_phrase or has_best_season_phrase or has_career_super):
        return None

    # Must NOT be a career-total query (career SUM, not career MAX)
    # "career home runs" / "career stats" → career total via parse_career_lookup
    # But "career high HR" / "career best HR" → single-season max (this parser).
    if contains_word("career", lower) and not has_career_super:
        return None

    # Find player and stat
    name = find_player_in_text(lower)
    if not name:
        return None
    stat = match_stat(lower)
    if not stat:
        return None

    # Direction: "lowest/fewest/worst/least" → MIN.
    # "best" alone is ambiguous — defer to stat's "lower is better" property
    # if available. Default: MAX.
    direction = "min" if has_low_super else "max"
    # For lower-is-better rate stats (ERA/WHIP/BAA/etc.), "best" actually means MIN.
    lower_is_better = stat.db_column in (
        "era", "whip", "bb_per_9", "hits_per_9", "hr_per_9", "baa",
        "batting_avg_against", "obp_against", "slg_against", "ops_against",
    )
    if lower_is_better:
        if "best" in lower or "lowest" in lower:
            direction = "min"
        elif "worst" in lower or "highest" in lower:
            direction = "max"

    return {"name": name, "stat": stat, "direction": direction}


def parse_catch_all_player_stat(input_str: str) -> Optional[dict]:
    """Last-resort parser: player name + stat keyword. Returns dict with name, stat, season, is_career."""
    lower = input_str.strip().lower()

    # Reject "player single-season max" patterns — handled by parse_player_single_season_max.
    # Belt-and-suspenders in case wiring order changes.
    if re.search(r'\b(?:most|best|highest|fewest|worst|lowest|peak|top|least)\b', lower) and (
        re.search(r'\bever\b', lower)
        or re.search(r'\bin (?:a|one|any|a single) season\b', lower)
        or re.search(r'\b(?:best|highest|peak|top|worst|lowest)\b(?:\s+\w+){0,3}\s+(?:season|year)\b', lower)
        or re.search(r'\bcareer[- ]?(?:high|best|worst|low|lowest|highest)\b', lower)
    ):
        return None

    # Reject queries where the player name is context, not the subject.
    # Questions starting with interrogative words are asking about the stat broadly,
    # not about the specific player mentioned.
    # (re is already imported at module level — the redundant local import
    # below was creating a function-scope `re` that shadowed the module-level
    # one and tripped UnboundLocalError on the guard above.)
    if re.match(r'^(who|what|how|when|has anyone|has any|have any|is there|are there|which)\b', lower):
        return None
    # "closest to", "since [player]" — comparative/historical
    if re.search(r'\bclosest\b|\bsince\b', lower):
        return None
    # "game logs" — not a stat lookup
    if re.search(r'\bgame\s*logs?\b', lower):
        return None
    # Game-log-style queries: "games with 4+ hits", "multi-homer games" — not a single-stat lookup
    if re.search(r'games?\s+with\s+\d|multi[- ]?(hit|homer|hr)|(\d+)\+?\s*[- ]?\s*hit\s+games?|\d+\+\s+\w+\s+games?', lower):
        return None

    # Reject game-window qualifiers — "Stanton past 5 games", "Will Warren
    # first seven starts". The catch-all is the LAST resort before falling
    # to the query engine / Haiku / Sonnet, so a greedy player+stat match
    # here would silently drop the qualifier and ship a wrong full-season
    # answer. Decline.
    if re.search(_GAME_WINDOW_RX, lower):
        return None

    stat = match_stat(lower)
    if not stat:
        return None
    name = find_player_in_text(lower)
    if not name:
        return None

    is_career = contains_word("career", lower)
    season = detect_season(lower, default_to_most_recent=True) or _current_calendar_year()
    if _residual_qualifier_words(lower, name):
        return None
    return {"name": name, "stat": stat, "season": season, "is_career": is_career}


def parse_perfect_games(input_str: str) -> Optional[dict]:
    """Detect perfect-games queries. Answered from a hand-curated JSON list.

    Returns one of:
      {"mode": "all"}                — full list
      {"mode": "since", "year": Y}   — games from year Y onward
      {"mode": "in", "year": Y}      — single year
      {"mode": "latest"}             — most recent one
      {"mode": "count"}              — just the total count
    Or None.
    """
    lower = input_str.strip().lower().rstrip("?.!")
    # Require the literal phrase "perfect game(s)"
    if not re.search(r'\bperfect\s+games?\b', lower):
        return None
    # Exclude queries that add a player name or other narrow qualifier we can't
    # answer from the flat list (e.g., "has Verlander thrown a perfect game").
    # If a player name is present, bail out and let another parser or Claude take it.
    if _has_player_name(lower):
        return None

    # "most recent" / "last" / "latest"
    if re.search(r'\b(most\s+recent|latest|last)\b', lower):
        return {"mode": "latest"}

    # "how many"
    if re.search(r'\bhow\s+many\b', lower):
        return {"mode": "count"}

    # "since YYYY"
    m = re.search(r'\bsince\s+(\d{4})\b', lower)
    if m:
        return {"mode": "since", "year": int(m.group(1))}

    # "in YYYY"
    m = re.search(r'\bin\s+(\d{4})\b', lower)
    if m:
        return {"mode": "in", "year": int(m.group(1))}

    # "all", "ever", "all time", "all-time", "in history"
    if re.search(r'\b(all|ever|all[-\s]?time|in\s+history)\b', lower):
        return {"mode": "all"}

    # Bare "perfect games" — default to all-time list
    return {"mode": "all"}
# v2 - force reimport
