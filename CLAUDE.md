# Project Memory

## Active Project: Baseball Stats Engine
- **Location**: `/Users/markmezrich/Documents/claude/BaseballStatsEngine/`
- **Design doc**: `/Users/markmezrich/Documents/claude/baseball design doc.pdf`
- **What it is**: iOS app (Swift/SwiftUI) that answers natural language baseball questions using real data. Claude translates questions to SQL, SQLite provides ground truth.
- **Current phase**: Phase 1 + Phase 2 + Phase 3 (iOS app) COMPLETE. Data pipeline swapped to Retrosheet (commercially viable). Compiles with zero errors/warnings.

### Data Pipeline (Retrosheet-native)
- `data_pipeline/pull_stats.py` — pulls ALL data from Retrosheet: season stats (aggregated from game logs), game-level batting logs, platoon splits (via Chadwick Bureau retrosplits), and player bio data (birthdate, bats, throws from `biodata.zip`). **2024-2025 data loaded** (2,925 players, 1,799 with bio data, 2,924 season stat rows, 142,822 game log rows for ALL players, 2,602 platoon split rows).
- `data_pipeline/pull_stats_fangraphs.py` — OLD FanGraphs pipeline, preserved for reference only. NOT used.
- `data_pipeline/detect_streaks.py` — change-point detection using ruptures PELT on per-game OPS. **2,347 streak segments** (Tier 1) + **1,258 sensitive streaks** (Tier 2) + **2,137 current form entries**. Also detects "current form" — the stats from the last performance shift to end of season.
- `baseball_stats.db` — SQLite DB, 9 tables: `players` (with `birthdate`, `bats`, `throws`), `season_batting_stats`, `league_averages`, `platoon_splits`, `game_batting_logs`, `streaks`, `streaks_sensitive`, `streaks_sliding`, `current_form`. Uses Retrosheet player IDs (e.g., `judga001`). OPS+ computed for all player-seasons. Team abbreviations use Retrosheet format (NYA, LAN, CHA, etc.).
- `schema_description.py` — plain-English schema description for Claude's system prompt (all tables)
- `query_engine.py` — full pipeline: text-to-SQL → answer generation. Data-source agnostic.
- `cli_poc.py` — interactive terminal CLI.
- `data_pipeline/requirements.txt` — anthropic, requests, pandas (pybaseball removed)

### Data Sources (commercially viable)
- **Retrosheet** (retrosheet.org) — game logs, season stats, player info. Free, commercial OK with attribution.
- **Chadwick Bureau retrosplits** (Open Database License) — platoon splits (vs LHP/vs RHP), 1969+.
- **OPS+**: Computed from Retrosheet data (league-adjusted, no park factors). 100 = average. Stored in `season_batting_stats.ops_plus`. League averages in `league_averages` table.
- **wRC+ and WAR**: Columns kept in DB but always NULL (FanGraphs proprietary). Use OPS+ for league-adjusted offense.
- **Old FanGraphs pipeline**: Backed up as `pull_stats_fangraphs.py`. NOT commercially licensed.

### Two-tier streak detection
- **Tier 1 (precomputed, penalty=3)**: Stored in `streaks` table. 2,347 segments.
- **Tier 2 (precomputed, penalty=1.5)**: Stored in `streaks_sensitive` table. 1,258 sensitive streaks.
- **Fallback flow**: streaks table → if single "average" segment → query `streaks_sensitive`.
- **Data source agnostic**: PELT only needs game-level batting logs.

### Current Form detection
- **`current_form` table**: 2,137 entries. Stores the "current form" for each player-season — stats from the last PELT change point to end of season.
- **Algorithm**: PELT penalty=3 → take last change point. If none, try penalty=1.5. If still none, default to last 30 games (or half season if <60 games). Minimum 7-game slice.
- **Player card**: "Current Form" section with stat grid, interactive slider (recomputes from game logs), and two projection modes (form pace, blended).
- **Chat**: `current_form` query route for "how is X doing lately?" questions. Claude returns stat grid with FORM: metadata line for slider support.
- **StatGridView**: Detects FORM: metadata in parsed grids and shows slider + projection toggle in chat responses too.

### iOS App (Phase 3 — StatChat)
- **Location**: `ios/`
- **Xcode project**: Generated via XcodeGen (`project.yml`), iOS 17.0+, Swift 6, zero dependencies
- **Architecture**: SwiftUI + @Observable + @MainActor for strict concurrency
- **Key files**: `AppState.swift` (state), `QueryEngine.swift` (orchestrator), `AnthropicService.swift` (Claude API with SSE streaming), `DatabaseService.swift` (SQLite C API), `PromptStore.swift` (all prompts), `KeychainHelper.swift` (API key storage)
- **Views**: `HomeView` (search + animated sample queries), `ResultsView` (results + follow-up), `ResultCard` (user/assistant/error styling), `APIKeySetupView` (first-launch + settings), `AnimatedPlaceholder`, `LoadingIndicator`
- **Streaming**: SSE parsing via `URLSession.shared.bytes(for:)`, typewriter effect via callback-based `onChunk` pattern
- **Database**: 24MB `baseball_stats.db` bundled in Resources (read-only)
- **Stat grid**: 21 stats (G through BABIP, PA and SF excluded for compact 3-row display). Career rows show "--" for OPS+ (multi-season weighting not implemented).
- **Player card bio**: Dynamic age computed from birthdate (updates on player's birthday). Header shows handedness (Bats R / Throws R). About section shows birth date.
- **Query routing**: `simple_lookup`, `streak_finder`, `current_form`, `stat_explanation` — Claude classifies, then dispatches
- **ResultsView layout**: Follow-up input hidden during loading, appears inline below short results or pinned to bottom for long results

### Key technical notes
- Claude Sonnet sometimes wraps SQL in markdown code fences — `SQLSanitizer.swift` strips them with regex
- Using Claude Sonnet (`claude-sonnet-4-5-20250929`) for all LLM calls
- Conversation history (last 5 Q&A pairs) for follow-up questions
- PA minimums for rate stat leaderboards: >=400 full season, >=200 partial
- `hasAPIKey` must be a stored property (not computed) for SwiftUI reactivity
- `INFOPLIST_KEY_UILaunchScreen_Generation: YES` required in project.yml to avoid iPhone 7 layout

### Differentiators vs. General LLMs (ChatGPT, Gemini, Claude direct)

**Current moat (built today):**
- **Guaranteed accuracy** — every number from SQL against a real DB, never hallucinated
- **Streak detection** — game-log-level change-point analysis (PELT) finds hot/cold stretches no LLM can surface
- **Complex filtered queries** — "Top 10 in OPS with 400+ PA" requires precise SQL filtering
- **Platoon splits** — vs LHP/RHP data is niche enough that LLMs are unreliable on it
- **Consistency** — same question, same correct answer every time

**High-impact features to widen the gap:**
| Feature | Why it matters | Status |
|---------|---------------|--------|
| **OPS+** | League-adjusted offense metric, replaces wRC+ | DONE |
| **Pitching stats** | Doubles addressable questions | NOT STARTED |
| **Historical data (1898+)** | LLMs get increasingly wrong the further back you go | Pipeline ready, just run with wider year range |
| **Statcast data** (exit velo, launch angle, sprint speed) | LLMs are terrible at this; rich analytical queries | NOT STARTED (2025+ only) |
| **Situational splits** (home/away, by month, RISP) | Beyond platoon; LLMs can't do this reliably | NOT STARTED |
| **Predictive/pace features** ("on pace for X") | Unique analytical value, not just lookup | BUILT (162-game projections in PlayerCardView) |
| **Real-time/current season freshness** | LLM training data lags; pipeline can be near-real-time | Partial (2024-2025 loaded) |

### Data Expansion Roadmap

We download Retrosheet season ZIPs that contain 7 CSV files. We currently only use **batting.csv**, **allplayers.csv**, and supplement with **Chadwick Bureau retrosplits**. The remaining files are untapped.

#### Retrosheet ZIP contents (per season):
| File | Columns | Rows (2024) | Currently Using |
|------|---------|-------------|-----------------|
| batting.csv | 39 | ~71K game logs | 18 of 39 cols |
| allplayers.csv | 24 | ~1,500 players | 17 of 24 cols (incl. bat, throw) |
| pitching.csv | 42 | ~21K game logs | **None** |
| fielding.csv | 28 | ~67K records | **None** |
| gameinfo.csv | 43 | ~2,500 games | **None** |
| plays.csv | 177 | ~193K plate appearances | **None** |
| teamstats.csv | 111 | ~5K team-games | **None** |

#### Expansion phases (ordered by impact and dependency):

**Phase A: Low-hanging fruit from batting.csv**
- Add `b_gdp` (GIDP) to `season_batting_stats` — new counting stat, frequently asked
- ~~Add `bat` (L/R/B batter hand) from allplayers.csv to `players` table~~ DONE — `bats` and `throws` columns added, populated from allplayers.csv + biodata.zip
- Derivable rate stats (BB%, K%, SB%) don't need new columns — Claude computes via SQL on the fly

**Phase B: Home/Away splits**
- `vishome` flag already in batting.csv game logs
- Add to `game_batting_logs` table, create new `home_away_splits` table (or extend platoon_splits with a `context` dimension)
- Enables: "Judge's home vs road stats", "Best road OPS", day/night with gameinfo.csv join
- Design decision: separate table per split type, or unified splits table with a `split_type` column?

**Phase C: Pitching stats (mirrors batting system)**
- `pitching.csv` has 42 cols: IP (as outs), H, R, ER, HR, BB, K, HBP, WP, BK, etc.
- New tables: `season_pitching_stats` (aggregated), `game_pitching_logs` (per-game)
- Computed fields: ERA, WHIP, K/9, BB/9, K/BB, H/9
- Streak detection extends naturally (PELT on per-game ERA or K/9)
- Schema description + PromptStore + iOS stat grid all need pitching equivalents
- Design decision: separate PlayerCard view for pitchers, or unified card with batting/pitching tabs?

**Phase D: Fielding stats**
- `fielding.csv`: putouts, assists, errors, double plays, catcher-specific stats (PB, SB/CS allowed)
- New table: `season_fielding_stats` (player-position-season aggregates)
- Computed: fielding percentage, range factor
- Lower priority — niche audience, but completes the picture

**Phase E: Play-by-play analytics (advanced)**
- `plays.csv`: 177 columns per plate appearance — pitch counts, event outcomes, base states
- Enables truly advanced metrics: K%, contact rate, chase rate, situational (RISP, 2 outs)
- Largest engineering effort; may need summary/rollup tables rather than raw storage
- This is the biggest analytical moat vs general LLMs

#### Unused batting.csv columns of note:
- `b_gdp` — grounded into double play (3,278 in 2024). Worth adding to season stats.
- `b_roe` — reached on error (1,598 in 2024). Marginal value.
- `vishome` — home/away flag. Key for Phase B.
- `dh`, `ph`, `pr` — role flags. Marginal standalone value.

#### Unused allplayers.csv columns of note:
- ~~`bat` (L/R/B) — batter handedness~~ DONE (stored as `bats` in players table)
- ~~`throw` (L/R) — throw hand~~ DONE (stored as `throws` in players table)
- `first_g`, `last_g` — career date range. Could enable "active in year X" queries.

### Database size & bundling strategy
Current DB (2024-2025 only): **37 MB** bundled in-app — fast, works offline. But full historical data (1898+) with game logs would be **~1.8 GB** — way too big to bundle. Game logs are ~90% of the size (~9.4M rows). Season-level stats alone for all history would only be ~25 MB.

**Plan: Bundle recent, backend for historical.** Bundle the last 5-10 years of data (~100-150 MB) for speed. Historical queries (pre-2015 or so) go through the backend server. This keeps the app fast for the most common queries while still supporting "who led the league in HR in 1961?" via the server. The backend is already needed for API key security, so this piggybacks on that infrastructure.

**Important:** When updating the bundled DB, always copy the rebuilt `baseball_stats.db` from the project root into `ios/BaseballStatsEngine/Resources/baseball_stats.db`. They are separate files — the pipeline writes to the project root, but the app bundles from Resources.

### Before public/commercial release
1. **Backend server for API key security + historical data** — POC uses direct Claude API calls with key on-device. Server also needed for historical data too large to bundle.
2. ~~Swap to commercially licensed data sources~~ DONE (Retrosheet + Chadwick Bureau)
3. ~~League-adjusted offense metric~~ DONE (OPS+)
4. ~~Add pitching stats~~ DONE (season stats, game logs, streaks, splits, current form, ERA+)
5. Expand historical data (1898+) — via backend server for game logs, bundled for season stats
