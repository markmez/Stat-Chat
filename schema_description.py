"""Plain-English schema description for Claude's system prompt."""

SCHEMA_DESCRIPTION = """
You have access to a SQLite database with MLB batting statistics.

## Tables

### players
- player_id (TEXT, primary key) — unique FanGraphs ID
- name (TEXT) — player's full name (e.g., "Aaron Judge")
- team (TEXT) — most recent team abbreviation (e.g., "NYY", "LAD")

### season_batting_stats
- player_id (TEXT) — references players table
- season (INTEGER) — year (e.g., 2024)
- team (TEXT) — team abbreviation for that season
- age (INTEGER) — player's age during that season
- games (INTEGER) — games played (G)
- plate_appearances (INTEGER) — total plate appearances (PA)
- at_bats (INTEGER) — at bats (AB)
- hits (INTEGER) — hits (H)
- doubles (INTEGER) — doubles (2B)
- triples (INTEGER) — triples (3B)
- home_runs (INTEGER) — home runs (HR)
- runs (INTEGER) — runs scored (R)
- rbi (INTEGER) — runs batted in (RBI)
- stolen_bases (INTEGER) — stolen bases (SB)
- caught_stealing (INTEGER) — caught stealing (CS)
- walks (INTEGER) — walks/bases on balls (BB)
- strikeouts (INTEGER) — strikeouts (SO)
- hit_by_pitch (INTEGER) — hit by pitch (HBP)
- sacrifice_flies (INTEGER) — sacrifice flies (SF)
- intentional_walks (INTEGER) — intentional walks (IBB)
- batting_avg (REAL) — batting average (AVG)
- obp (REAL) — on-base percentage (OBP)
- slg (REAL) — slugging percentage (SLG)
- ops (REAL) — on-base plus slugging (OPS)
- iso (REAL) — isolated power (ISO = SLG - AVG)
- babip (REAL) — batting average on balls in play (BABIP)
- wrc_plus (INTEGER) — weighted runs created plus (wRC+), league-adjusted (100 = average)
- war (REAL) — wins above replacement (WAR, FanGraphs version)

### platoon_splits
- player_id (TEXT) — references players table
- season (INTEGER) — year
- split (TEXT) — either "vs_LHP" (vs left-handed pitchers) or "vs_RHP" (vs right-handed pitchers)
- plate_appearances (INTEGER) — PA in that split
- at_bats (INTEGER) — AB in that split
- hits (INTEGER) — hits
- doubles (INTEGER) — doubles
- triples (INTEGER) — triples
- home_runs (INTEGER) — home runs
- rbi (INTEGER) — RBI
- walks (INTEGER) — walks
- strikeouts (INTEGER) — strikeouts
- batting_avg (REAL) — batting average
- obp (REAL) — on-base percentage
- slg (REAL) — slugging percentage
- ops (REAL) — OPS
- iso (REAL) — isolated power
- babip (REAL) — BABIP
- wrc_plus (INTEGER) — wRC+

### game_batting_logs
- player_id (TEXT) — references players table
- season (INTEGER) — year
- date (TEXT) — game date in YYYY-MM-DD format
- opponent (TEXT) — opponent team abbreviation
- plate_appearances, at_bats, hits, doubles, triples, home_runs, runs, rbi, walks, strikeouts (INTEGER)
- batting_avg, obp, slg, ops (REAL) — per-game rates

### streaks
Precomputed performance streaks detected via change-point analysis. Each row is a continuous stretch of games where a player's performance was consistent.
- player_id (TEXT) — references players table
- season (INTEGER) — year
- start_date (TEXT) — first game date of the streak
- end_date (TEXT) — last game date of the streak
- num_games (INTEGER) — number of games in the streak
- batting_avg, obp, slg, ops (REAL) — aggregate stats during the streak
- home_runs, hits, at_bats, walks, strikeouts (INTEGER) — counting stats during the streak
- performance (TEXT) — "hot", "cold", or "average" relative to the player's overall season

### streaks_sensitive
Precomputed sensitive streaks for players who had NO change points in the primary detection (penalty=3). These are subtler performance shifts found with a lower threshold (penalty=1.5), filtered to 7-30 game segments. Use this as a fallback when the streaks table returns only a single "average" segment for a player.
- player_id (TEXT) — references players table
- season (INTEGER) — year
- start_date (TEXT) — first game date of the streak
- end_date (TEXT) — last game date of the streak
- num_games (INTEGER) — number of games in the streak (7-30)
- batting_avg, obp, slg, ops (REAL) — aggregate stats during the streak
- home_runs, hits, at_bats, walks, strikeouts (INTEGER) — counting stats during the streak
- performance (TEXT) — "hot", "cold", or "average" relative to the player's overall season
- season_ops (REAL) — the player's overall season OPS for context

## Currently Available Data
- 2024 and 2025 seasons
- Platoon splits (vs LHP and vs RHP) for both seasons
- Game-level batting logs for qualified batters (400+ PA)
- Precomputed streak segments for qualified batters (streaks table)
- Sensitive fallback streaks for players with no dramatic shifts (streaks_sensitive table)

## Important Notes
- Player names are stored as full names: "Aaron Judge", "Shohei Ohtani", etc.
- Use LIKE with '%' for fuzzy name matching when the user gives a partial name
- Team abbreviations: NYY, LAD, BOS, ATL, HOU, etc.
- For rate stats (AVG, OBP, SLG, OPS), use the precomputed columns rather than calculating from raw counts
- For counting stats (HR, RBI, etc.), use the integer columns directly
- wRC+ of 100 is league average; higher is better
- WAR: 0-1 = replacement level, 2-3 = solid starter, 4-5 = all-star, 6+ = MVP caliber
- For split queries (vs lefties/righties), JOIN with platoon_splits using split = 'vs_LHP' or split = 'vs_RHP'
- If the user says "last year" or "last season", assume 2024. If they say "this year" or "this season", assume 2025.
"""
