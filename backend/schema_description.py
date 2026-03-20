"""Plain-English schema description for Claude's system prompt."""

from datetime import date as _date

_THIS_YEAR = _date.today().year
_LAST_YEAR = _THIS_YEAR - 1

SCHEMA_DESCRIPTION = f"""
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
Home and away batting splits aggregated from game logs. Available for seasons 2016-2025 only.
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
Available for seasons 2016-2025 only (recent data). NOT available for historical seasons before 2016.
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

### season_fielding_stats
Per-position fielding stats aggregated from Retrosheet fielding.csv.
- player_id (TEXT) — references players table
- season (INTEGER) — year
- position (TEXT) — fielding position: "C", "1B", "2B", "3B", "SS", "LF", "CF", "RF", "P"
- games (INTEGER) — games played at this position
- games_started (INTEGER) — games started at this position
- innings (REAL) — innings played (computed from outs / 3)
- putouts (INTEGER) — putouts (PO)
- assists (INTEGER) — assists (A)
- errors (INTEGER) — errors (E)
- double_plays (INTEGER) — double plays turned (DP)
- passed_balls (INTEGER) — passed balls (PB) — primarily relevant for catchers
- fielding_pct (REAL) — fielding percentage: (PO + A) / (PO + A + E)
- For fielding queries, join with season_fielding_stats using player_id and season
- A player may have multiple rows per season (one per position played)

### season_pitching_stats
Per-season aggregated pitching stats from Retrosheet pitching.csv.
- player_id (TEXT) — references players table
- season (INTEGER) — year
- team (TEXT) — team abbreviation
- games (INTEGER) — total games pitched (G)
- games_started (INTEGER) — games started (GS)
- games_finished (INTEGER) — games finished (GF)
- complete_games (INTEGER) — complete games (CG)
- wins (INTEGER) — wins (W)
- losses (INTEGER) — losses (L)
- saves (INTEGER) — saves (SV)
- ip_outs (INTEGER) — raw outs recorded (divide by 3 for IP). Use this for arithmetic.
- innings_pitched (TEXT) — formatted IP display string (e.g., "134.0", "6.1"). Use this for display only.
- hits (INTEGER) — hits allowed (H)
- runs (INTEGER) — runs allowed (R)
- earned_runs (INTEGER) — earned runs (ER)
- home_runs (INTEGER) — home runs allowed (HR)
- walks (INTEGER) — walks/bases on balls (BB)
- intentional_walks (INTEGER) — intentional walks (IBB)
- strikeouts (INTEGER) — strikeouts (SO/K)
- hit_by_pitch (INTEGER) — hit by pitch (HBP)
- wild_pitches (INTEGER) — wild pitches (WP)
- balks (INTEGER) — balks (BK)
- batters_faced (INTEGER) — total batters faced (BF)
- sacrifice_hits (INTEGER) — sacrifice hits allowed (SH)
- sacrifice_flies (INTEGER) — sacrifice flies allowed (SF)
- stolen_bases (INTEGER) — stolen bases allowed (SB)
- caught_stealing (INTEGER) — caught stealing (CS)
- quality_starts (INTEGER) — quality starts: >= 6.0 IP and <= 3 ER (QS)
- era (REAL) — earned run average (ERA = 9 * ER / IP)
- whip (REAL) — walks + hits per inning pitched (WHIP = (BB + H) / IP)
- k_per_9 (REAL) — strikeouts per 9 innings (K/9)
- bb_per_9 (REAL) — walks per 9 innings (BB/9)
- k_per_bb (REAL) — strikeout-to-walk ratio (K/BB)
- h_per_9 (REAL) — hits per 9 innings (H/9)
- hr_per_9 (REAL) — home runs per 9 innings (HR/9)
- baa (REAL) — batting average against (BAA)
- era_plus (INTEGER) — ERA+ (ERA adjusted for league average). 100 = league average, >100 is above average. Computed as 100 * league_ERA / player_ERA. No park factors.

### game_pitching_logs
Per-game pitching logs from Retrosheet pitching.csv. Available for seasons 2016-2025 only.
- player_id (TEXT) — references players table
- season (INTEGER) — year
- date (TEXT) — game date in YYYY-MM-DD format
- opponent (TEXT) — opponent team abbreviation
- vishome (TEXT) — "H" for home game, "V" for away/visitor game
- is_start (INTEGER) — 1 if the pitcher started the game, 0 otherwise
- ip_outs (INTEGER) — outs recorded in this game
- innings_pitched (TEXT) — formatted IP for display (e.g., "6.1")
- hits, runs, earned_runs, home_runs, walks, strikeouts, hit_by_pitch, batters_faced (INTEGER) — per-game counting stats
- win (INTEGER) — 1 if pitcher got the win, 0 otherwise
- loss (INTEGER) — 1 if pitcher got the loss, 0 otherwise
- save (INTEGER) — 1 if pitcher got the save, 0 otherwise
- era (REAL) — per-game ERA (9 * ER / IP for that game)

### pitching_platoon_splits
How batters performed against each pitcher, broken down by batter handedness. From Chadwick Bureau retrosplits.
- player_id (TEXT) — references players table (the pitcher)
- season (INTEGER) — year
- split (TEXT) — "vs_LHB" (vs left-handed batters) or "vs_RHB" (vs right-handed batters)
- plate_appearances, at_bats, hits, doubles, triples, home_runs (INTEGER) — batting stats against
- walks, intentional_walks, strikeouts, hit_by_pitch, sacrifice_hits, sacrifice_flies (INTEGER)
- batting_avg_against (REAL) — opponents' batting average
- obp_against (REAL) — opponents' on-base percentage
- slg_against (REAL) — opponents' slugging percentage
- ops_against (REAL) — opponents' OPS
- For pitching platoon queries, use split = 'vs_LHB' or 'vs_RHB'. Note: these are BATTER hand, not pitcher hand.

### pitching_home_away_splits
Home and away pitching splits aggregated from game logs. Available for seasons 2016-2025 only.
- player_id (TEXT) — references players table
- season (INTEGER) — year
- split (TEXT) — "home" or "away"
- games, games_started (INTEGER) — games pitched / started in that split
- ip_outs (INTEGER) — outs recorded
- innings_pitched (TEXT) — formatted IP for display
- hits, earned_runs, home_runs, walks, strikeouts (INTEGER) — counting stats
- era, whip, k_per_9, bb_per_9, baa (REAL) — rate stats

### pitch_type_batting_splits
Batter performance broken down by the final pitch type of each plate appearance. Available for 2025-2026 — from MySportsFeeds play-by-play data.
- player_id (TEXT) — references players table
- season (INTEGER) — year
- pitch_type (TEXT) — normalized pitch type: "4-Seam", "Sinker", "Cutter", "Slider", "Curveball", "Changeup", "Splitter", "Knuckle", "Sweeper", or other
- plate_appearances, at_bats, hits, doubles, triples, home_runs, rbi, walks, strikeouts, hit_by_pitch, sacrifice_flies (INTEGER)
- batting_avg, obp, slg, ops, iso, babip (REAL) — rate stats

### pitch_type_pitching_splits
How batters performed against each pitcher broken down by the final pitch type of each plate appearance. Available for 2025-2026.
- player_id (TEXT) — references players table (the pitcher)
- season (INTEGER) — year
- pitch_type (TEXT) — normalized pitch type (same values as pitch_type_batting_splits)
- plate_appearances, at_bats, hits, doubles, triples, home_runs, walks, strikeouts, hit_by_pitch, sacrifice_flies (INTEGER)
- batting_avg_against, obp_against, slg_against, ops_against (REAL)

### count_batting_splits
Batter performance broken down by the ball-strike count when the plate appearance ended. Available for 2025-2026.
- player_id (TEXT) — references players table
- season (INTEGER) — year
- count_state (TEXT) — ball-strike count like "0-0", "1-2", "3-2", etc. Format is "balls-strikes".
- plate_appearances, at_bats, hits, doubles, triples, home_runs, rbi, walks, strikeouts, hit_by_pitch, sacrifice_flies (INTEGER)
- batting_avg, obp, slg, ops, iso, babip (REAL)

### count_pitching_splits
How batters performed against each pitcher broken down by the ball-strike count when the PA ended. Available for 2025-2026.
- player_id (TEXT) — references players table (the pitcher)
- season (INTEGER) — year
- count_state (TEXT) — ball-strike count (same format as count_batting_splits)
- plate_appearances, at_bats, hits, doubles, triples, home_runs, walks, strikeouts, hit_by_pitch, sacrifice_flies (INTEGER)
- batting_avg_against, obp_against, slg_against, ops_against (REAL)

### risp_batting_splits
Batter performance with Runners In Scoring Position (2nd and/or 3rd base occupied) vs Non-RISP. Available for 2025-2026.
- player_id (TEXT) — references players table
- season (INTEGER) — year
- split (TEXT) — "RISP" or "Non-RISP"
- plate_appearances, at_bats, hits, doubles, triples, home_runs, rbi, walks, strikeouts, hit_by_pitch, sacrifice_flies (INTEGER)
- batting_avg, obp, slg, ops, iso, babip (REAL)

### risp_pitching_splits
How batters performed against each pitcher with RISP vs Non-RISP. Available for 2025-2026.
- player_id (TEXT) — references players table (the pitcher)
- season (INTEGER) — year
- split (TEXT) — "RISP" or "Non-RISP"
- plate_appearances, at_bats, hits, doubles, triples, home_runs, walks, strikeouts, hit_by_pitch, sacrifice_flies (INTEGER)
- batting_avg_against, obp_against, slg_against, ops_against (REAL)

### league_pitching_averages
Per-season league-wide pitching averages.
- season (INTEGER, primary key) — year
- total_ip_outs, total_er, total_h, total_bb, total_so, total_hr, total_bf (INTEGER) — league totals
- league_era (REAL) — league ERA
- league_whip (REAL) — league WHIP
- league_k_per_9 (REAL) — league K/9
- league_bb_per_9 (REAL) — league BB/9
- league_baa (REAL) — league batting average against

### pitching_streaks
Precomputed pitching performance streaks detected via change-point analysis. Uses per-game ERA as the signal (inverted: low ERA = hot).
- player_id (TEXT) — references players table
- season (INTEGER) — year
- role (TEXT) — "starter" or "reliever"
- start_date, end_date (TEXT) — date range
- num_games (INTEGER) — games in the streak
- ip_outs (INTEGER), innings_pitched (TEXT) — IP raw and formatted
- hits, earned_runs, walks, strikeouts, home_runs (INTEGER) — counting stats
- era, whip, k_per_9 (REAL) — rate stats
- performance (TEXT) — "hot" (ERA <= 70% of season avg), "cold" (ERA >= 140%), or "average"

### pitching_streaks_sensitive
Sensitive pitching streaks for pitchers with no change points at standard threshold. Same schema as pitching_streaks plus season_era for context.

### pitching_streaks_sliding
Sliding window gap-fill pitching streaks. Same schema as pitching_streaks_sensitive.

### pitching_current_form
Current form for each pitcher-season — the tail slice with the lowest ERA (optimistic fan, inverted).
- player_id (TEXT) — references players table
- season (INTEGER) — year
- role (TEXT) — "starter" or "reliever"
- form_start_date (TEXT) — date when form period starts
- form_start_game_number (INTEGER) — 1-indexed game number
- total_season_games (INTEGER) — total games in season
- num_games (INTEGER) — games in form period
- ip_outs (INTEGER), innings_pitched (TEXT) — form period IP
- hits, earned_runs, home_runs, walks, strikeouts, batters_faced (INTEGER) — form period counting stats
- era, whip, k_per_9, bb_per_9 (REAL) — form period rate stats
- season_ip_outs, season_hits, season_earned_runs, season_home_runs, season_walks, season_strikeouts, season_batters_faced (INTEGER) — full season counting stats
- season_era (REAL) — full season ERA for comparison

## Currently Available Data
- Season batting stats from 1898 to present (aggregated from Retrosheet game logs)
- Season pitching stats from 1898 to present (aggregated from Retrosheet pitching.csv)
- OPS+ for every batter-season, ERA+ for every pitcher-season (league-adjusted, no park factors — 100 = average)
- League-wide batting averages (league_averages table) and pitching averages (league_pitching_averages table)
- Game-level batting logs and pitching logs for 2016-2025 ONLY (not available for historical seasons)
- Batting platoon splits (vs LHP/RHP) and pitching platoon splits (vs LHB/RHB) from 1969 to present
- Home/away splits for both batting and pitching — 2016-2025 ONLY (derived from game logs)
- Precomputed batting streaks — 2016-2025 ONLY (streaks, streaks_sensitive, streaks_sliding tables)
- Precomputed pitching streaks — 2016-2025 ONLY (pitching_streaks, pitching_streaks_sensitive, pitching_streaks_sliding tables)
- Current form for batters and pitchers — 2016-2025 ONLY (current_form, pitching_current_form tables)
- Per-position fielding stats (season_fielding_stats table) from 1898 to present — games, innings, putouts, assists, errors, DP, PB, fielding %
- Note: wRC+ and WAR columns exist but are NULL. Use ops_plus for league-adjusted offense instead.
- IMPORTANT: For pre-2016 queries, only use season_batting_stats, season_pitching_stats, season_fielding_stats, platoon_splits (1969+), pitching_platoon_splits (1969+), league_averages, and league_pitching_averages. Do NOT query game logs, home/away splits, streaks, or current form tables for pre-2016 seasons — they will return no data.

## Pitcher Detection
- The `players.positions` field is sorted by games played DESC (primary position first).
- A player is a pitcher if their positions field starts with "P" AND they have rows in season_pitching_stats.
- For players like Shohei Ohtani who play DH and P, the primary position is determined by which role had more games. DH/P = primarily a batter (show batting stats). P/DH = primarily a pitcher (show pitching stats).
- When a user asks about a pitcher, query the pitching tables (season_pitching_stats, game_pitching_logs, etc.) instead of batting tables.
- Shared stat names like "strikeouts" or "home runs" should be resolved based on whether the player is a pitcher or batter.

## Important Notes
- Player names are stored as full names: "Aaron Judge", "Shohei Ohtani", etc.
- Use LIKE with '%' for fuzzy name matching when the user gives a partial name
- Team abbreviations use Retrosheet format: NYA (Yankees), NYN (Mets), LAN (Dodgers), CHN (Cubs), CHA (White Sox), SLN (Cardinals), SFN (Giants), SDN (Padres), TBA (Rays), KCA (Royals), ANA (Angels), WAS (Nationals), etc.
- For rate stats (AVG, OBP, SLG, OPS, OPS+), use the precomputed columns rather than calculating from raw counts
- For OPS+ leaderboards, apply the same PA minimums as other rate stats (>=400 full season, >=200 partial)
- For counting stats (HR, RBI, etc.), use the integer columns directly
- For batting split queries (vs lefties/righties), JOIN with platoon_splits using split = 'vs_LHP' or split = 'vs_RHP'
- For pitching split queries (vs lefties/righties), JOIN with pitching_platoon_splits using split = 'vs_LHB' or split = 'vs_RHB'
- Platoon splits are only available for 1969 and later. If the user asks about splits for earlier years, let them know.
- For home/away batting splits, JOIN with home_away_splits. For pitching home/away, JOIN with pitching_home_away_splits.
- For pitching rate stat leaderboards (ERA, WHIP, etc.), use ip_outs >= 486 (162 IP) for starters, ip_outs >= 150 (50 IP) for all pitchers
- Pitching counting stats: "strikeouts" in pitching context = K (pitcher's strikeouts), different from batter strikeouts
- innings_pitched is a TEXT display field (e.g., "134.0"). For arithmetic, use ip_outs (integer, divide by 3 for IP as decimal)
- Some historical stats (IBB, SF, HBP) may be NULL or 0 for very old seasons (pre-1955)
- If the user says "last year" or "last season", assume {_LAST_YEAR}. If they say "this year" or "this season", assume {_THIS_YEAR}.
"""
