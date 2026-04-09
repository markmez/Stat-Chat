# Project Memory

## Active Project: Baseball Stats Engine
- **Location**: `/Users/markmezrich/Documents/claude/BaseballStatsEngine/`
- **Design doc**: `/Users/markmezrich/Documents/claude/baseball design doc.pdf`
- **What it is**: iOS app (Swift/SwiftUI) that answers natural language baseball questions using real data. Claude translates questions to SQL, SQLite provides ground truth.
- **Current phase**: iOS app + backend deployed with live data. **Backend-only architecture (2026-03-18)** — all stats queries route to backend, iOS bundled DB stripped to players table only (~1.6MB). Backend interceptor handles 29 query types structurally at zero Claude cost. MSF DETAILS tier, cron refresh every 4 hours.

### Data Pipeline (Retrosheet-native)
- `data_pipeline/pull_stats.py` — pulls ALL data from Retrosheet: season stats (batting + pitching), game-level logs, platoon splits (Chadwick Bureau), home/away splits, fielding stats, and player bio data. **2016-2025 data loaded (10 years)** — 3,782 players, 14,173 batting season stats, 8,233 pitching season stats, 661,313 batting game logs, 195,734 pitching game logs, 15,379 platoon splits, 16,391 pitching platoon splits, 27,558 home/away splits, 22,303 fielding stats.
- `data_pipeline/pull_live_stats.py` — pulls current-season stats from **MySportsFeeds** API v2.1. Season batting/pitching totals, daily game logs, home/away splits (derived from game log `vishome` column), platoon splits (derived from play-by-play at-bat handedness data), OPS+/ERA+ computation. Auto-detects season (preseason before Mar 25, regular Apr-Sep, playoff Oct+). Calls `detect_streaks.py --season` after loading game logs. Rate-limit aware (3 retries, 2s delay between daily requests). **Synced in two locations**: `data_pipeline/` and `backend/data_pipeline/` — keep both in sync when editing.
- `data_pipeline/pull_stats_fangraphs.py` — OLD FanGraphs pipeline, preserved for reference only. NOT used.
- `data_pipeline/detect_streaks.py` — change-point detection using ruptures PELT. Supports `--season YYYY` for incremental updates (only processes/replaces that season's data). **Early-season current form**: 1-game minimum — uses all games as "current form" until player reaches 14+ games, then normal tail-slice algorithm kicks in. No date-based switch needed; per-player auto-graduation.
- `baseball_stats.db` — **220 MB** SQLite DB, ~20 tables covering batting, pitching, fielding, splits (platoon + home/away), streaks, and 2026 spring training data. Full historical data 1898-2026. Uses Retrosheet player IDs (e.g., `judga001`). OPS+ and ERA+ computed for all player-seasons. Team abbreviations use Retrosheet format (NYA, LAN, CHA, etc.).
- `schema_description.py` — plain-English schema description for Claude's system prompt (all tables)
- `query_engine.py` — full pipeline: text-to-SQL → answer generation. Data-source agnostic.
- `cli_poc.py` — interactive terminal CLI.
- `data_pipeline/requirements.txt` — anthropic, requests, pandas (pybaseball removed)

### Data Sources (commercially viable)
- **Retrosheet** (retrosheet.org) — game logs, season stats, player info (2016-2025 historical). Free, commercial OK with attribution.
- **Chadwick Bureau retrosplits** (Open Database License) — platoon splits (vs LHP/vs RHP), 1969+.
- **MySportsFeeds** (mysportsfeeds.com) — current-season live stats (2026+). Paid **DETAILS tier** subscription (required for play-by-play endpoint → platoon splits). API key in `.env` and Railway env var `MSF_API_KEY`.
- **OPS+**: Computed from Retrosheet data and live MSF data (league-adjusted, no park factors). 100 = average. Stored in `season_batting_stats.ops_plus`. League averages in `league_averages` table. Early-season note: OPS+ skipped until 100+ total games played.
- **wRC+ and WAR**: Columns kept in DB but always NULL (FanGraphs proprietary). Use OPS+ for league-adjusted offense.
- **Old FanGraphs pipeline**: Backed up as `pull_stats_fangraphs.py`. NOT commercially licensed.

### Three-tier streak detection (batting + pitching)
- **Tier 1 (precomputed, penalty=3)**: `streaks` (11,469 batting) + `pitching_streaks` (30,764 pitching).
- **Tier 2 (precomputed, penalty=1.5)**: `streaks_sensitive` (6,333) + `pitching_streaks_sensitive` (207). Only for player-seasons with single T1 segment.
- **Tier 3 (sliding window)**: `streaks_sliding` (7,509) + `pitching_streaks_sliding` (2,042). Gap-fills missing hot/cold.
- **Current form**: `current_form` (10,335 batting) + `pitching_current_form` (6,729 pitching).
- **Fallback flow**: T1 → T2 → T3 for both batting and pitching.
- **Data source agnostic**: PELT only needs game-level logs.

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
- **Key files**: `AppState.swift` (routing — stat defs + disambiguation only, everything else → backend), `BackendService.swift` (Railway API, SSE streaming), `DatabaseService.swift` (SQLite C API — players table only), `PlayerNameMatcher.swift` (query parsing against local players table), `PlayerCardService.swift` (backend-first player cards), `StatDefinitions.swift` (local stat definitions for zero-cost explanations)
- **Views**: `HomeView` (search + animated sample queries), `ResultsView` (results + follow-up), `ResultCard` (user/assistant/error styling), `AnimatedPlaceholder`, `LoadingIndicator`
- **Streaming**: SSE parsing via `URLSession.shared.bytes(for:)`, typewriter effect via callback-based `onChunk` pattern
- **Database**: **~1.6MB** `baseball_stats.db` bundled in Resources — `players` table only (24,110 rows) for name matching/disambiguation. All stats tables removed (2026-03-18).
- **Stat grid**: 21 stats (G through BABIP, PA and SF excluded for compact 3-row display). Career rows show "--" for OPS+ (multi-season weighting not implemented).
- **Player card bio**: Dynamic age computed from birthdate (updates on player's birthday). Header shows handedness (Bats R / Throws R). About section shows birth date.
- **Query routing (backend-only, 2026-03-18)**: AppState handles only: (1) paywall check, (2) stat definitions (hardcoded, free), (3) player disambiguation (players table). Everything else → backend streaming path. Backend `interceptor.py` handles 29 query types structurally at zero Claude cost. All local stats intercepts removed (~600 lines).
- **Backend interceptor query types (29)**: comparison, streak history, current form, slash line, season count, single stat, career lookup, platoon splits, home/away splits, RISP splits, pitch type splits, count splits, month stats, season lookup, milestone, superlative, filtered leaderboard, threshold, multi-threshold, composite threshold, triple crown, consecutive streak, team ranking, team total, team stats, platoon leaderboard, leaderboard, stat definition, catch-all player stat.
- **Key interceptor techniques**: Multi-threshold splits on ALL separators (with/and/while/plus) via common delimiter. Batting avg inference for "batted/hit .300" queries. Platoon leaderboard context detection: "against pitchers" = batting, "against batters" = pitching. Situational triggers excluded from generic leaderboard parser. Dynamic year references via `date.today().year`.
- **ResultsView layout**: Follow-up input hidden during loading, appears inline below short results or pinned to bottom for long results
- **Dead code note**: `resolveContextualFollowUp`, `resolveReferentialFollowUp`, `extractPlayerNamesFromResponse`, `lastResultContext` in AppState are dead code (referenced local stats tables that no longer exist). `PlayerCardService` build* functions also dead code (no longer called from AppState). SuggestionEngine dynamic queries return empty (stats tables gone), falls back to 50 curated defaults.

### Player name matching & common-word collisions
Two separate code paths for player name resolution, each with different intent:
- **`matchPlayer()`** — direct search (user typed a name in the search bar). Bare last name "Bench" → Johnny Bench. No common-word filtering. Called via `resolveSearch()` from HomeView.
- **`findPlayerInText()`** — embedded name detection (parser scanning a natural language sentence for a player name). Skips last names in `commonWordLastNames` to avoid false matches like "show" → Eric Show.

**`commonWordLastNames`** (~174 words): Comprehensive set of unambiguous player last names (only 1 player in DB) that are also common English words. Loaded from `shared/stat_config.json` (single source of truth). Multi-player last names (judge, hill, young, king, etc.) don't need to be in this set — they're already rejected by the `players.count == 1` check.

**Shared config sync**: `shared/stat_config.json` contains stat aliases, common word last names, nickname aliases, and disambig maps. Both iOS (`PlayerNameMatcher.swift`) and Python (`name_matcher.py`) load from local copies at runtime. After editing, run `shared/sync_config.sh` to copy to `ios/BaseballStatsEngine/Resources/stat_config.json` and `backend/services/stat_config.json`.

**Deciding what goes in `commonWordLastNames`:**
1. Word has a baseball/query meaning (show, score, walk, speed, spring, force, pop, free) → **INCLUDE** — the word almost certainly isn't a player reference
2. Well-known player whose last name has NO baseball meaning (Bench, Belt, Story, Penny, Dye, Deer, Duke, Beer, Steer, Cave) → **EXCLUDE** — matching the player is the only useful answer
3. Obscure player + ambiguous word → **INCLUDE** — harmless, prevents edge cases

Players excluded from `commonWordLastNames` are still reachable via full name. For ambiguous matches, `matchPlayerWithProminence()` picks the most prominent player and `[SEEALSO]` tags show alternatives.

**`matchPlayerWithProminence()`** — fallback for ambiguous last names. Sorts candidates by prominence (current players first, then by total games). Used in `parseComparison` and `parseConsecutiveStreak` so "Soto" resolves to Juan Soto with "See also: Gregory Soto, ..." alternatives.
- **Suggestion CMS**: `SuggestionEngine.swift` + `SuggestionConfig.swift` + `suggestions_config.json`. Bundled JSON config with S3 override (`https://stat-chat.s3.us-east-2.amazonaws.com/suggestions_config.json`). Three tiers: 50 curated defaults (weighted), personalized (from search history/team affinity/templates), dynamic in-season leaders (DB queries, `{seasonLabel}` → "this season"/"last season"). Impression/tap rotation (threshold 6, monthly reset, tapped reset on version bump). OTA updates: upload JSON to S3 with higher `version` number. `SampleQuery.swift` is deleted.

### API Cost Optimization (implemented)
At scale (500K queries/mo), Claude API costs dominate (~$7,500-9,000/mo unoptimized). Five optimizations reduce per-query costs ~55-60%:

**1. Prompt caching on SQL generation & answer generation** — biggest impact
- SQL gen system prompt is ~6,100 tokens (schema + instructions), answer gen is ~1,500 tokens. Both sent with `cache_control: {"type": "ephemeral"}` for 90% input cost discount on cache hits ($0.30/M vs $3/M).
- Requires `anthropic-beta: prompt-caching-2024-07-31` header on cached requests.
- **Critical**: JSON body must be byte-identical for cache hits. Replaced `JSONSerialization.data(withJSONObject:)` (non-deterministic key ordering) with `Encodable` structs + `JSONEncoder(outputFormatting: .sortedKeys)`. `SystemContent` enum encodes as plain string or structured `[CachedBlock]` array.
- Savings: ~$6,300/mo at 375K Claude-routed queries.

**2. Haiku for query routing** — `routingModel = "claude-haiku-4-5-20251001"`
- Routing is a trivial JSON classification (~500 tokens in, ~20 out). Haiku is ~70% cheaper than Sonnet for this.
- `callAPI()` accepts a `model` parameter; `routeQuery()` uses `routingModel`, everything else uses `model` (Sonnet).
- Savings: ~$450/mo.

**3. Local current form name extraction** — eliminates SQL gen call for current_form queries
- `handleCurrentFormQuery()` used to call `anthropic.generateSQL()` just to extract a player name via regex from the SQL output. Now uses `extractPlayerNameLocally()` which searches `PlayerNameMatcher.sortedNames` and `lastNameIndex` with word-boundary matching.
- Season extracted via `PlayerNameMatcher.detectSeason()` instead of SQL regex.
- Falls back to `handleSQLQuery()` if local extraction fails.
- Savings: ~$790/mo (eliminates ~$0.021/query for ~10% of Claude-routed queries).

**4. Local pattern-based routing** — `classifyLocally()` in `QueryEngine.ask()`
- Before calling Claude's router, checks for obvious keyword patterns: streak/slump → `streak_finder`, lately/recently → `current_form`, "what is"/"explain" + stat keyword (no player name) → `stat_explanation`.
- Catches ~30-40% of routed queries, saving the Haiku routing call entirely.
- Savings: ~$200-270/mo.

**5. Local stat explanations** — `handleLocalStatExplanation()` in `QueryEngine`
- When route is `stat_explanation`, looks up from `StatDefinitions` dictionary + `PlayerNameMatcher.statAliasMap`. Returns `**ABBREV** — definition` with zero API cost.
- Falls back to Claude `explainStat()` if stat not in local dictionary.
- Savings: ~$135/mo.

**Total estimated savings**: ~$7,900-8,000/mo at 500K queries → reduced to ~$3,000-3,500/mo.

### Haiku SQL Fallback (VALIDATED, not yet built)
Major unlock: when the 29-parser interceptor misses a query, have Haiku generate SQL directly from the schema instead of routing to Sonnet. Tested 14/15 correct — including complex queries like year-over-year comparisons, cross-table joins, and consecutive-season analysis.

**Current flow**: interceptor miss → Sonnet SQL gen (~$0.02/query)
**New flow**: interceptor miss → Haiku SQL gen (~$0.002/query) → Sonnet only if Haiku fails

- **Test script**: `backend/tests/test_haiku_sql.py` — sends questions to Haiku with schema, runs generated SQL against DB
- **Prorated PA minimums**: `(days_elapsed / total_season_days) * 400` for current-season rate stat queries. Full-season = 400 PA. No arbitrary "partial" bucket.
- **Active player definition**: has a row in season stats for current or previous year
- **Retry on error**: if Haiku SQL fails, send error back to Haiku for one retry before escalating to Sonnet
- **Additional savings**: ~$1,800/mo at 500K queries (assumes 20% interceptor miss rate)
- **Status**: Concept validated, needs implementation + extensive QA. See [full plan](file:///Users/markmezrich/.claude/projects/-Users-markmezrich/memory/project-haiku-sql-fallback.md).

### Key technical notes
- Claude Sonnet sometimes wraps SQL in markdown code fences — `SQLSanitizer.swift` strips them with regex
- Using Claude Sonnet (`claude-sonnet-4-5-20250929`) for SQL generation, answer generation, streak/form description
- Using Claude Haiku (`claude-haiku-4-5-20251001`) for query routing only
- Conversation history (last 5 Q&A pairs) for follow-up questions
- PA minimums for rate stat leaderboards: >=400 full season, prorated for current season (400 × fraction of season elapsed)
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
| **Pitching stats** | Doubles addressable questions | DONE (full pipeline + iOS) |
| **Historical data (1898+)** | LLMs get increasingly wrong the further back you go | 10 years bundled (2016-2025); pre-2016 needs backend |
| **Situational splits** (home/away, by month, RISP) | Beyond platoon; LLMs can't do this reliably | Home/away DONE; platoon DONE (incl. 2026 via play-by-play); career splits DONE; month/RISP not started |
| **Predictive/pace features** ("on pace for X") | Unique analytical value, not just lookup | BUILT (162-game projections in PlayerCardView) |
| **In-season live data feed** | LLM training data lags; real-time stats are table stakes | DONE (MySportsFeeds, every 4 hours) |

**Dropped:** Statcast data — no viable commercial license path.

### Data Expansion Roadmap

We download Retrosheet season ZIPs that contain 7 CSV files. We now use **batting.csv**, **pitching.csv**, **fielding.csv**, **allplayers.csv**, and supplement with **Chadwick Bureau retrosplits**.

#### Retrosheet ZIP contents (per season):
| File | Columns | Rows (2024) | Currently Using |
|------|---------|-------------|-----------------|
| batting.csv | 39 | ~71K game logs | YES — season stats, game logs, home/away splits |
| allplayers.csv | 24 | ~1,500 players | YES — player info, positions, bats/throws |
| pitching.csv | 42 | ~21K game logs | YES — season stats, game logs, home/away splits |
| fielding.csv | 28 | ~67K records | YES — per-position season aggregates |
| gameinfo.csv | 43 | ~2,500 games | **None** |
| plays.csv | 177 | ~193K plate appearances | **None** |
| teamstats.csv | 111 | ~5K team-games | **None** |

#### Expansion phases (ordered by impact and dependency):

**Phase A: Low-hanging fruit from batting.csv**
- Add `b_gdp` (GIDP) to `season_batting_stats` — new counting stat, frequently asked
- ~~Add `bat` (L/R/B batter hand) from allplayers.csv to `players` table~~ DONE — `bats` and `throws` columns added, populated from allplayers.csv + biodata.zip
- Derivable rate stats (BB%, K%, SB%) don't need new columns — Claude computes via SQL on the fly

**~~Phase B: Home/Away splits~~** DONE — `home_away_splits` (27,558 rows) + `pitching_home_away_splits` (15,538 rows)

**~~Phase C: Pitching stats~~** DONE — Full pitching pipeline: `season_pitching_stats` (8,233), `game_pitching_logs` (195,734), `pitching_platoon_splits` (16,391), pitching streaks (3 tiers + current form), ERA+ computed. PlayerCard has pitcher view + two-way player segmented control (Ohtani rule: PA >= 130 + IP >= 30).

**~~Phase D: Fielding stats~~** DONE — `season_fielding_stats` (22,303 rows) with per-position aggregates, fielding pct.

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

### Database strategy (backend-only, 2026-03-18)
- **iOS bundled DB**: ~1.6MB — `players` table only (24,110 rows). All stats tables stripped. Used for name matching/disambiguation only.
- **Backend DB (PRIMARY)**: On Lightsail at `/data/baseball_stats_full.db`. Full historical data 1898-2026, ~26 tables, 4.8M+ game logs back to 1920. Cron-refreshed every 4 hours. **This is the only DB that matters.**
- **Project root DB**: `baseball_stats.db` — STALE local copy from an earlier era. Do NOT use for real work. It's missing historical game logs (only has 2016+) and is out of sync with production.
- **DB changes (schema migrations, backfills, data fixes)**: Run directly on the backend via `POST /admin/run-sql` or deploy migration scripts that execute on the server. Do NOT modify the local DB and upload — that workflow is dead.
- **S3 DB**: `s3://stat-chat/baseball_stats_full.db` — last-resort fallback for cold starts only.

**DANGER: Do NOT re-run `pull_stats.py` (Retrosheet pipeline) without precaution.** It rebuilds from scratch, wiping historical and live data. `pull_live_stats.py` (MSF) is safe — it only inserts/updates current-season rows.

### Stats methodology & discrepancies

**Sacrifice Flies (SF) and pre-1954 stats**: SF wasn't officially tracked until 1954. Our data source (Retrosheet) reconstructs SF for earlier seasons from play-by-play accounts derived from box scores and newspaper records. Baseball Reference does NOT use these reconstructed values — they treat SF as 0 for pre-1954 seasons. This means our career OBP and OPS for pre-1954 players will differ slightly from Baseball Reference. Example: Babe Ruth career OPS is 1.160 in our system vs 1.164 on Baseball Reference. Our number includes estimated SF in the OBP denominator `(AB+BB+HBP+SF)`, while Baseball Reference uses `(AB+BB+HBP)` for those seasons. We consider our approach more accurate since it uses the best available reconstructed data, but users comparing to Baseball Reference may notice small differences for historical players.

**Career rate stat computation**: Career AVG, OBP, SLG, OPS are recomputed from summed raw components across all seasons (e.g., career AVG = total hits / total at-bats). This is mathematically correct but may differ slightly from averaging per-season values because per-season values are stored rounded to 3 decimal places.

**IP qualification rule**: We use the MLB qualification standard: 1.0 inning pitched per scheduled team game. So if a team has played 50 games, pitchers need 50 IP to qualify for rate stat leaderboards. Full season = 162 IP.

**PA qualification for batters**: 400 PA for a full season, prorated by games played for in-progress seasons (e.g., 10 games in = ~25 PA minimum).

### Known data issues
- **Ken Griffey Sr./Jr.**: Both stored as "Ken Griffey" (`grifk001` 1973-1991, `grifk002` 1989-2010). Can't disambiguate in UI since both have identical names. Fix: rename `grifk002` to "Ken Griffey Jr." in the `players` table (and all join tables). Aliases in `PlayerNameMatcher` route "Ken Griffey Jr."/"Sr." to "Ken Griffey" but can't distinguish which one.
- **Bobby Witt Jr. split IDs**: `wittb001` = father (1986-2001), `wittb002` = Jr. data under father's name (2022-2024), `wittjb001` = Jr. from MSF (2025-2026). Jr.'s career is split across two player IDs. Fix: merge `wittb002` data into `wittjb001`.
- **Jazz Chisholm / Jasrado Chisholm Jr.**: `chisj001` = "Jazz Chisholm" (Retrosheet, 2020-2024), `chishj001` = "Jasrado Chisholm Jr." (MSF, 2025-2026). Same player, two IDs. Fix: merge into one ID.
- **Ronald Acuña**: `acunr001` = "Ronald Acuna" (Retrosheet, 2018-2024), `acuñar001` = "Ronald Acuña Jr." (MSF, 2025-2026). Same player, accent mismatch. Fix: merge into one ID.
- **Workaround**: `PlayerNameMatcher` has `nicknameAliases` and `disambigSrJrMap` to handle these at the search layer. Proper fix is merging player IDs in the DB so career stats are unified.

### User-facing error messages
Errors are sanitized in `AppState.friendlyErrorMessage()` before display. The mapping:
| User sees | Underlying cause |
|-----------|-----------------|
| "Sorry, I couldn't process that question. Try rephrasing it." | SQL error — bad column, table, or syntax from generated query |
| "Couldn't reach the server. Check your connection and try again." | Network timeout, offline, connection refused |
| "The server is having trouble right now. Please try again in a moment." | HTTP 500/502/503/504 from backend |
| "You've used all N free queries this week. Resets [date]." | Quota exceeded (passed through from `ServiceError.quotaExceeded`) |
| "Something went wrong. Please try again." | Any other unrecognized error |

### Monetization (decided)
- **Free to download**, 5 free queries per week (resets weekly). All query types count equally — no distinction between local and Claude-handled queries from the user's perspective.
- **$2.99/month** for unlimited queries.
- **$19.99/year** option ($1.67/month effective) — locks users in through the offseason when monthly churn spikes. Clean psychological price point.
- **Rationale**: 5/week is low enough that an excited first-time user burns through them in one session, hitting the paywall while still in the "wow" moment. Weekly reset keeps free users coming back (retention + repeated conversion opportunities) rather than a lifetime bank that runs out and leads to app deletion. $2.99 is still impulse pricing — research shows negligible conversion difference vs $1.99 at the "will I pay at all?" decision point, but 50% more revenue per subscriber. Comp set: FantasyPros basic tier ($2.99), GolfLogix ($49.99/yr), The Athletic ($9.99/mo) — $2.99 is low end of sports enthusiast tools. Not trying to capture serious/analytics fans at a premium tier yet; current stats compete against free alternatives (Baseball Reference, FanGraphs). When Statcast/play-by-play analytics are added (data you can't get free), a premium tier ($5.99+) makes sense.
- **Unit economics**: Post-optimization, blended cost per query is ~$0.005-0.008 for Claude-routed queries, ~25%+ handled locally at zero cost. Free user costs ~$0.02-0.04/week. Paying user at ~100 queries/month costs ~$0.30-0.50/month → 80%+ margin at $2.99.

### Attribution & Disclosure Requirements
- **AI disclosure** (Anthropic policy): Must disclose to users that they're interacting with an AI system. One umbrella statement is sufficient — no per-response labeling. Place in App Store description (marketing-friendly framing, e.g., "Powered by advanced AI to deliver accurate, real-time baseball intelligence") and optionally in an About screen. Do NOT label individual responses as AI vs non-AI — keep the seam invisible.
- **Retrosheet** (required, exact wording): "The information used here was obtained free of charge from and is copyrighted by Retrosheet. Interested parties may contact Retrosheet at www.retrosheet.org." Must be displayed "prominently" — an About/Data Sources screen in-app + App Store description.
- **Chadwick Bureau** (Open Database License): Attribution required, no specific wording mandated. Include alongside Retrosheet notice in About screen.
- **Implementation**: Add an "About" or "Data Sources" screen accessible from settings. Include all three attributions. Mirror in App Store description.

### Backend — DEPLOYED & WORKING
- **Live at** `https://stat-chat-production.up.railway.app`
- **Railway setup**: project `stat-chat`, service `Stat-Chat`, volume mounted at `/data` (5 GB Hobby plan)
- **Deploy method**: GitHub auto-deploy on the Stat-Chat service (root directory `backend/`). Push to GitHub triggers a Railway build. Cron service is NOT connected to GitHub (rarely changes, deploy manually if needed). `deploy.sh` is no longer needed for routine deploys. DB is NOT in the Docker image — the volume has it, refreshed by cron every 4 hours.
- **DB baked into Docker image** at `/app/seed_db/baseball_stats_full.db`. On startup, `ensure_db()` copies to volume if missing (fast local copy). S3 (`https://stat-chat.s3.us-east-2.amazonaws.com/baseball_stats_full.db`) is last-resort fallback only. This fixed a production crash where S3 download failed during SSL read on cold start.
- **Health check**: `/health` verifies DB is queryable (runs `SELECT 1 FROM players`), not just server up. `restartPolicyType = "on_failure"`, timeout 60s.
- **Metering**: `services/metering.py` tracks device UUIDs, 5 free queries/week, weekly Monday reset. `METERING_DB_PATH=/data/metering.db`.
- **SSE event format**: `{"type":"text","text":"..."}`, `{"type":"done"}`, `{"type":"error","message":"..."}`, `{"type":"quota_exceeded","count":N,"reset":"YYYY-MM-DD"}`
- **Dockerfile**: Build context is `backend/`. Copies `data_pipeline/` (pipeline scripts duplicated in `backend/data_pipeline/`), `schema_description.py` (duplicated in `backend/`). When updating pipeline scripts, sync both copies.
- **Admin endpoints**: `POST /admin/refresh` (trigger MSF pipeline), `POST /admin/redownload-db` (force S3 re-download), `GET /admin/freshness`, `GET /admin/schedule`, `GET /admin/volume-usage`, `DELETE /admin/volume-cleanup` (removes orphaned files from volume), `GET /admin/dashboard` (query analytics dashboard)
- **Railway env vars**: `ANTHROPIC_API_KEY`, `DB_PATH`, `FREE_QUERIES_PER_WEEK=1000`, `MSF_API_KEY`, `ADMIN_KEY`

### Live Data Pipeline (MySportsFeeds)
- **Provider**: MySportsFeeds v2.1 API, DETAILS tier subscription (upgraded from STATS for play-by-play access)
- **Auth**: Basic auth (`API_KEY:MYSPORTSFEEDS` base64-encoded)
- **Season format**: `{year}-preseason` (before Mar 25), `{year}-regular` (Apr-Sep), `{year}-playoff` (Oct+). Auto-detected by both `pull_live_stats.py` and `admin.py`.
- **MSF team mapping**: `MSF_TO_RETRO_TEAM` dict maps MSF abbreviations (NYY, LAD) → Retrosheet codes (NYA, LAN)
- **Player matching**: Name-based lookup against existing Retrosheet players. New players get Retrosheet-style IDs (`last5first1001`).
- **Admin endpoints**: See Backend section above for full list.
- **Cron**: Lightsail system cron (`/etc/cron.d/statchat`). Auto-installed on deploy via GitHub Actions. See "Polling & Notable Events Feed" section below for full schedule.
- **Health monitor**: `deploy/healthcheck.sh` — curls `/health` every 5 min, pings Healthchecks.io (`f69f410b-1774-4af4-9bb4-c57136cc59ff`). Alerts after 10-min grace.
- **ADMIN_KEY**: `I9-NNJ-GBen3SZ-wf8JkZX5-_zvvt8Qri2EtTxWUo-I`
- **Pipeline flow**: season batting → league averages + OPS+ → season pitching → pitching averages + ERA+ → daily game logs → home/away splits (from game logs) → platoon splits (from play-by-play) → streak detection (all 8 passes filtered by season) → record freshness timestamp
- **Play-by-play derived splits (2025-2026)**: Pitch type splits (4-Seam, Sinker, Slider, etc.), count splits (all 12 ball-strike counts), RISP splits (runners in scoring position vs non-RISP) — all derived from MSF play-by-play `atBat` data. Tables: `pitch_type_batting_splits`, `pitch_type_pitching_splits`, `count_batting_splits`, `count_pitching_splits`, `risp_batting_splits`, `risp_pitching_splits`. Player card tabs: "By Pitch", "By Count", "RISP".

### Polling & Notable Events Feed

#### Data flow: nightly cascade pipeline
- **Nightly cascade** (`deploy/refresh.sh`): Full MSF pull — season totals, game logs, splits, play-by-play, streaks, integrity check. Creates `/tmp/statchat_detection.lock` + `/tmp/statchat_pipeline.lock` (prevents concurrent runs, 90-min stale timeout). Runs at 11:35 PM, 1:05 AM, 2:35 AM ET (90-min spacing). MSF typically publishes game logs by the 2:35 AM run. Season totals update faster than game logs.
- **Morning catch-alls**: 5:35 AM, 8:00 AM ET — pick up any data MSF published late.
- **Backup**: 10:00 AM ET, 5:00 PM ET (afternoon games), 7:30 PM ET weekends.
- **Weekly reconciliation** (Sunday 6 AM ET): `refresh.sh --full-refresh` — wipes and re-pulls all game logs for full integrity.
- **15-min polls REMOVED** (2026-04-08): Aggressive polling didn't help — MSF game logs aren't available same-night. Reverted to cascade approach which pulls season totals (available within minutes) for leaderboard change detection.

#### Event detection (`detect_all` in `notable_events.py`)
- **Tier 1**: Hitting streaks, on-base streaks, HR streaks, pitching streaks, season pace milestones
- **Dynamic streak thresholds**: Scale with games played — `max(floor, min(ceiling, games_played * rate))`. Hitting: 8→15, on-base: 12→20, HR: 3→5. Prevents routine streaks from flooding mid-season while keeping early-season hot starts noteworthy.
- **No caps on event counts**: If an event meets threshold, it shows. Only matchup previews are capped (top 3). Previously hitting/on-base/HR streaks were capped at 2-3.
- **Tier 2**: Career milestones, single-game rarities, hot streaks (PELT)
- **Tier 3 backfill**: Relaxed hitting streaks, league leaders (only if < 3 events from T1+T2)
- **Historical scans** (`historical_scans.py`): DB-verified "first since" facts — cross-season streaks, career-start stats, leaderboard changes, debut records. **All scans filter to players who played on `latest_date` only** — events are strictly about what happened on that date, not retroactive milestones from earlier games.
- **Rate stat leaderboard changes** (AVG, OBP, SLG, OPS): Complex logic for when leads change through inaction:
  - Leader played today → attribute event to leader ("Rice took the AL lead in OPS")
  - Leader didn't play, leader's team still playing today → HOLD event (wait for game to complete)
  - Leader didn't play, leader's team played but leader had no PAs → attribute to player who LOST lead ("Alvarez dropped below Rice for the AL lead in OPS")
  - Leader didn't play, leader's team has no game today → attribute to player who lost lead
  - Counting stat leads (HR, RBI, etc.) always require the leader to have played — no inaction scenario
- **Matchup previews**: Tonight's games — pitcher-first selection (career ERA < 3.50 / 240+ IP, or `pitcher_prominence_list` in `stat_config.json`). 12-day suppression rotation. Batter by career OPS (800+ PA). **Generated time-agnostic** (overnight pipeline), **display-gated** in feed API: weekdays noon ET+, weekends 9 AM ET+. Capped at 3 per day.
- **On This Date**: Historic performances on today's month-day from past years
- **AI insights** (`ai_notable_events.py`): Sonnet narrative layer — runs once per game date (deduped). Finds connections rules can't. **Prompt rules**: events must be about what happened on latest_date specifically; must NOT claim streak extensions unless the game continued them; streaks are the rule-based detector's job.

#### Streak integrity protections
- **Streak wipe on recompute**: `detect_all` deletes ALL streak events for `latest_date` before inserting new ones. Prevents stale/impossible streaks from persisting when a player no longer qualifies.
- **Lock file**: Daily pipeline creates `/tmp/statchat_detection.lock`, polls check and skip detection while locked. Auto-expires after 30 min. Prevents detection running on partially-loaded data during full pipeline.
- **New game tracking**: Poll tracks which player game logs are genuinely new (not updates to existing). Only triggers detection when new games appear.

#### Event persistence & dedup
- Events persist in `notable_events` table — NOT wiped on each run
- `UNIQUE(detection_type, game_date, headline)` prevents exact duplicate headlines
- Retention: 7-day window (14-day if fewer than 5 events). 50-event display limit.
- Matchup previews: `INSERT OR IGNORE` for non-streak events. Streaks: delete-then-insert per date.

#### Feed API (`/notable-events`)
- Returns events ordered by `game_date DESC`, **interleaved by detection_type** within each date (round-robin so no two consecutive events share the same type)
- **Matchup preview display gate**: hidden before noon ET weekdays / 9 AM ET weekends. Filtered by `expires_at` (game start time) so they disappear after first pitch.
- Game scores fetched from MLB Stats API (`statsapi.mlb.com/schedule` with linescore hydration), cached per date. Winning team listed first.

#### Cron schedule (`deploy/statchat-cron`)
- Nightly cascade: 11:35 PM, 1:05 AM, 2:35 AM ET (90-min spacing, full pipeline)
- Morning catch-alls: 5:35 AM, 8:00 AM ET
- Backup: 10:00 AM ET
- Afternoon: 5:00 PM ET daily, 7:30 PM ET weekends
- Weekly reconciliation: Sunday 6:00 AM ET
- Weekly VACUUM: Sunday 5:00 AM ET
- Health monitor: every 5 min, pings Healthchecks.io
- **Pipeline lock**: `/tmp/statchat_pipeline.lock` prevents concurrent runs (90-min stale timeout)

#### iOS feed display (`NotableEventsFeed.swift`)
- Loads from `/notable-events` on first appear + `willEnterForegroundNotification` (5-min cooldown)
- "Tonight" category events: inline CTA link to matchup preview, both player names linked (not bold), "matchup preview" text bold
- Game context shown as superheader (e.g. "April 5 · Dodgers 4 - Astros 3")
- Gradient separators between events, invisible on last item for alignment
- Matchup pill suggestions ("Judge tonight") extracted from feed events, scattered into suggestion pool

### iOS backend integration (backend-only, 2026-03-18)
- `BackendService.swift`: POST /query (SSE streaming), GET /player-card (structured JSON with career splits), 10s timeout on player card requests
- `deviceId` (UUID in UserDefaults) for metering
- **ALL stats queries → backend**. No local intercepts for stats. Backend `interceptor.py` handles 29 query types structurally (comparisons, leaderboards, streaks, splits, thresholds, multi-threshold, platoon leaderboards, etc.) at zero Claude cost.
- Local DB used ONLY for: player name matching/disambiguation in `PlayerNameMatcher`
- **Git tag** `ios-direct-anthropic-stable` — rollback point to direct Anthropic API

### Force-update banner
- **S3 config**: `https://stat-chat.s3.us-east-2.amazonaws.com/app_config.json` — contains `{ "min_version": "1.0.0" }`. Dormant by default.
- **To trigger**: Edit `app_config.json` in project root, set `min_version` to the version users need, then `aws s3 cp app_config.json s3://stat-chat/app_config.json --content-type application/json`. Users on older versions see a full-screen banner on next launch.
- **To deactivate**: Set `min_version` back to the current shipping version and re-upload.
- **Behavior**: Checked on every app launch (5s timeout, silent failure). Banner is dismissable per session ("Not now"). "Update" button opens App Store (URL placeholder — update `appStoreURL` in `UpdateBannerView.swift` once app ID is available).

### Priority roadmap (in order)
1. ~~**Historical data (pre-2016) via backend**~~ DONE — full 1898-2026 DB, 220 MB
2. ~~**In-season live data feed**~~ DONE — MySportsFeeds DETAILS tier, cron every 4 hours, incl. play-by-play platoon splits
3. ~~**Analytics**~~ DONE — Custom admin dashboard + Mixpanel. See "Admin Query Dashboard" section below.
4. **StoreKit subscription + paywall** — $2.99/month, $19.99/year. `/validate-receipt` endpoint on backend.
5. ~~**About/Data Sources screen**~~ DONE — AboutView with Retrosheet, Chadwick Bureau, AI disclosure.

### Upcoming features
- **"Close & Late" splits** — situational stats for at-bats in 7th inning or later when the batting team is tied, ahead by 1, or the tying run is at least on deck. Requires tracking running score through play-by-play data. MSF play-by-play has inning/half data and runner state; score can be derived by accumulating runs. New tables: `close_late_batting_splits`, `close_late_pitching_splits`. Pipeline addition to `pull_live_stats.py`. iOS player card tab.

### Deep Scans (SCOPED, not built — the killer feed feature)
Pre-defined library of multi-condition historical queries run automatically after each game. Modeled on stat Twitter accounts that go viral with deeply specific comparisons.

**Example patterns (from real stat Twitter, April 7-8 2026):**
- "First Dodgers pitcher to start a season with consecutive 6+ IP 0 ER outings since Maeda 2016" (team + pitching sequence)
- "Second most consecutive K in expansion era: 2024 Estrada 13, 2026 Miller 11, 2023 Alvarado 11" (era-bounded leaderboard)
- "2nd player in expansion era to start career with hits in first 5+ ABs, joining Ted Cox 1977" (career-start sequence + team context)
- "First pitcher since 1900 with 10+ K and 0 BB in each of first 2 starts" (multi-condition + era)
- "Yankees LHB with .400+ BA, .500+ OBP, .860+ SLG in first 6 games: Rice, Berra, Gehrig, Ruth" (multi-threshold + team + the company matters)

**Architecture:**
- `historical_scan_library.py` — scan patterns as structured configs
- Each pattern: condition SQL, lookback era, team-scoped vs MLB, context query
- Run per player per game day in detect_all
- Results templated with team-first-since + era list context

**Key design principles:**
1. Team context is essential — "first Dodger since X" > "first player since X"
2. Era bounding — "since 1900", "expansion era (1961+)"
3. The company matters — listing who else did it, especially if legends
4. Multi-condition — combinations (K + BB, BA + OBP + SLG), not just single stats
5. Game spans — "any 3-game span", "first N games", "first N career ABs"

**TODO:**
1. Define 10-15 scan patterns from real examples
2. Build scan engine (evaluate patterns per player per game)
3. Build historical context queries (team-first-since, era-list)
4. Template results
5. Add to detect_all
6. Test via records sandbox before going live

### Records & Personal Bests (PARTIALLY BUILT)
- **Pre-computed records tables**: `team_records` and `mlb_records` in stats DB. Top 5 all-time per team × stat for career, season, and single-game records. Auto-rebuilt on each pipeline run.
- **Records sandbox**: `/admin/records-sandbox` — player lookup (career stats, record proximity) + date simulation (what events would fire). Used for testing before going live.
- **Milestone additions needed**: 50 HR / 60 HR season thresholds, 20/20 30/30 40/40 HR/SB thresholds
- **Approaching thresholds**: 3 away for team records, 5 for big milestones
- **Personal bests**: Career highs (50+ games min), career firsts (first HR, win, save only), first high-threshold achievement (first 10+ K game, first 4-hit game)
- **"First since" context**: For career-first threshold achievements, check last player on team/league to do it
- **Historic moments table**: Pre-computed from game logs — dates when career totals crossed milestones or passed all-time leaders (e.g., Aaron HR #715 on Apr 8 1974). Also franchise record crossings. Feeds into On This Date.
- **Three surfaces**: Feed events (approaching/crossing), On This Date (historic moments), Leaders tab (franchise record context alongside current leaders — "HR Leaders: Judge 5 — franchise record: 61, Maris 1961")

### Admin Query Dashboard (BUILT, 2026-04-07)
- **URL**: `https://api.secondsignalapps.com/admin/dashboard?key=I9-NNJ-GBen3SZ-wf8JkZX5-_zvvt8Qri2EtTxWUo-I`
- **Query logging**: Every query logged to `query_log` table in `metering.db` with query_text, device_id, response_type, timestamp. Three types: `query engine` ($0), `haiku` (~$0.002/query), `sonnet` (~$0.02/query).
- **Dashboard features**: Blue gradient stat cards (total queries, unique queries), cost breakdown table by response type, full scrollable query list (up to 1,000). Sortable by count (default, tiebroken by recency) or time. Filterable by tapping type badges. Timestamps in Eastern Time.
- **Player card search logging**: `/player-card` endpoint accepts `source=search|link` and `device_id`. Search = logged + metered. Link navigation (default) = no logging, no metering. iOS passes `source: "search"` from HomeView search bar and SearchHistoryView; link taps from ResultsView, NotableEventsFeed, TeamCardView, etc. default to `"link"`.
- **Key files**: `backend/services/metering.py` (query_log table, log_query()), `backend/routers/admin.py` (/admin/dashboard endpoint), `backend/routers/query.py` (log_query calls at all exit points), `backend/routers/player_card.py` (source/device_id params)
- **Styled with app brand colors**: White background, blue gradient (#1A40B3 → #73B3FF) stat cards and title, green/blue/purple type badges.
- **Replaces Mixpanel long-term**. Remaining features needed: daily/weekly volume trend chart, unique users surfacing, paywall/subscription event tracking. Once built, Mixpanel can be dropped.
- **Mixpanel MCP**: Connected via `claude mcp add --transport http --scope user mixpanel https://mcp.mixpanel.com/mcp`. OAuth flow, 25+ tools. "Stat Chat Overview" dashboard (ID 11085633). Being phased out.

### What's explicitly NOT happening
- **Statcast data** — no viable commercial license path. Dropped from roadmap.

### Completed pre-release items
- ~~Commercially licensed data sources~~ DONE (Retrosheet + Chadwick + MSF)
- ~~OPS+~~ DONE (league-adjusted, no park factors)
- ~~Pitching stats~~ DONE (full pipeline + iOS)
- ~~Full historical data~~ DONE (1898-2026, 220 MB bundled)
- ~~Backend server~~ DONE (Railway)
- ~~iOS backend swap~~ DONE
- ~~Live data feed~~ DONE (MSF every 4 hours)
- ~~Home/away splits~~ DONE (Retrosheet 2016-2025, MSF 2026)
- ~~Platoon splits for 2026~~ DONE (MSF play-by-play DETAILS tier)
- ~~Career splits~~ DONE (platoon + home/away, batting + pitching)
