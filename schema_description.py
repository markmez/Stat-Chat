"""Plain-English schema description for Claude's system prompt."""

SCHEMA_DESCRIPTION = """
You have access to a SQLite database with MLB batting statistics.

Data sources: Retrosheet (retrosheet.org) for season stats, game logs, and platoon splits.
Platoon splits via Chadwick Bureau retrosplits (Open Database License).

## Tables

### players
- player_id (TEXT, primary key) — unique player ID (Retrosheet format, e.g., "judga001")
- name (TEXT) — player's full name (e.g., "Aaron Judge")
- team (TEXT) — most recent team abbreviation (e.g., "NYA", "LAN") — uses Retrosheet abbreviations
- birthdate (TEXT) — date of birth in YYYY-MM-DD format (e.g., "1992-04-26"). Use this to compute age dynamically.
- bats (TEXT) — batting hand: "R" (right), "L" (left), or "B" (both/switch-hitter)
- throws (TEXT) — throwing hand: "R" (right) or "L" (left)

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
- ops_plus (INTEGER) — OPS+ (OPS adjusted for league average). 100 = league average, >100 is above average. Computed as 100 * (player_OBP / league_OBP + player_SLG / league_SLG - 1). No park factors. Use this instead of wRC+ for league-adjusted offense.
- wrc_plus (INTEGER) — weighted runs created plus (wRC+). Always NULL (not available from Retrosheet). Use ops_plus instead.
- war (REAL) — wins above replacement (WAR). Always NULL (not available from Retrosheet). Column kept for compatibility.

### platoon_splits
Available for seasons 1969 and later.
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
- wrc_plus (INTEGER) — NULL (not available from this data source)

### home_away_splits
Home and away batting splits aggregated from game logs.
- player_id (TEXT) — references players table
- season (INTEGER) — year
- split (TEXT) — "home" or "away"
- games (INTEGER) — games played in that split
- plate_appearances (INTEGER) — PA
- at_bats (INTEGER) — AB
- hits, doubles, triples, home_runs, runs, rbi, walks, strikeouts, hit_by_pitch, sacrifice_flies (INTEGER)
- batting_avg, obp, slg, ops, iso, babip (REAL) — rate stats
- For home/away queries, JOIN with home_away_splits using split = 'home' or split = 'away'

### game_batting_logs
Available for seasons 1898 and later.
- player_id (TEXT) — references players table
- season (INTEGER) — year
- date (TEXT) — game date in YYYY-MM-DD format
- opponent (TEXT) — opponent team abbreviation (may be NULL)
- vishome (TEXT) — "H" for home game, "V" for away/visitor game
- plate_appearances, at_bats, hits, doubles, triples, home_runs, runs, rbi, walks, strikeouts (INTEGER)
- batting_avg, obp, slg, ops (REAL) — per-game rates

### league_averages
Per-season league-wide batting averages, computed from all players in season_batting_stats.
- season (INTEGER, primary key) — year
- total_pa, total_ab, total_hits, total_doubles, total_triples, total_hr, total_bb, total_hbp, total_sf, total_so (INTEGER) — league-wide counting totals
- league_avg (REAL) — league batting average
- league_obp (REAL) — league on-base percentage
- league_slg (REAL) — league slugging percentage
- league_ops (REAL) — league OPS
- league_iso (REAL) — league isolated power
- league_babip (REAL) — league BABIP

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

### current_form
Precomputed "current form" for each player-season — the stats from the last detected performance shift to the end of the season. Useful for "how is this player doing lately?" questions.
- player_id (TEXT) — references players table
- season (INTEGER) — year
- form_start_date (TEXT) — date when the current form period starts (YYYY-MM-DD)
- form_start_game_number (INTEGER) — 1-indexed game number where the form starts
- total_season_games (INTEGER) — total games in the player's season
- num_games (INTEGER) — number of games in the form period
- at_bats, hits, doubles, triples, home_runs, runs, rbi, walks, strikeouts, plate_appearances (INTEGER) — form period counting stats
- batting_avg, obp, slg, ops, iso (REAL) — form period rate stats
- season_at_bats, season_hits, season_doubles, season_triples, season_home_runs, season_runs, season_rbi, season_walks, season_strikeouts, season_plate_appearances (INTEGER) — full season counting stats for comparison

## Currently Available Data
- Season batting stats from 1898 to present (aggregated from Retrosheet game logs)
- OPS+ for every player-season (league-adjusted, no park factors — 100 = average)
- League-wide averages per season (league_averages table)
- Game-level batting logs from 1898 to present (Retrosheet) — all players, not limited to qualified batters
- Platoon splits (vs LHP and vs RHP) from 1969 to present (Retrosheet retrosplits)
- Home/away splits aggregated from game logs (home_away_splits table)
- Precomputed streak segments for players with sufficient game logs (streaks table)
- Sensitive fallback streaks for players with no dramatic shifts (streaks_sensitive table)
- Current form data for each player-season (current_form table) — form period stats from last performance shift to end of season
- Note: wRC+ and WAR columns exist but are NULL. Use ops_plus for league-adjusted offense instead.

## Important Notes
- Player names are stored as full names: "Aaron Judge", "Shohei Ohtani", etc.
- Use LIKE with '%' for fuzzy name matching when the user gives a partial name
- Team abbreviations use Retrosheet format: NYA (Yankees), NYN (Mets), LAN (Dodgers), CHN (Cubs), CHA (White Sox), SLN (Cardinals), SFN (Giants), SDN (Padres), TBA (Rays), KCA (Royals), ANA (Angels), WAS (Nationals), etc.
- For rate stats (AVG, OBP, SLG, OPS, OPS+), use the precomputed columns rather than calculating from raw counts
- For OPS+ leaderboards, apply the same PA minimums as other rate stats (>=400 full season, >=200 partial)
- For counting stats (HR, RBI, etc.), use the integer columns directly
- For split queries (vs lefties/righties), JOIN with platoon_splits using split = 'vs_LHP' or split = 'vs_RHP'
- Platoon splits are only available for 1969 and later. If the user asks about splits for earlier years, let them know.
- For home/away split queries, JOIN with home_away_splits using split = 'home' or split = 'away'
- Some historical stats (IBB, SF, HBP) may be NULL or 0 for very old seasons (pre-1955)
- If the user says "last year" or "last season", assume 2024. If they say "this year" or "this season", assume 2025.
"""
