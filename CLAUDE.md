# Project Memory

## Active Project: Baseball Stats Engine
- **Location**: `/Users/markmezrich/Documents/claude/BaseballStatsEngine/`
- **Design doc**: `/Users/markmezrich/Documents/claude/baseball design doc.pdf`
- **What it is**: iOS app (Swift/SwiftUI) that answers natural language baseball questions using real data. Claude translates questions to SQL, SQLite provides ground truth.
- **Current phase**: Phase 1 + Phase 2 + Phase 3 (iOS app) COMPLETE. Data pipeline swapped to Retrosheet (commercially viable). Compiles with zero errors/warnings.

### Data Pipeline (Retrosheet-native)
- `data_pipeline/pull_stats.py` — pulls ALL data from Retrosheet: season stats (batting + pitching), game-level logs, platoon splits (Chadwick Bureau), home/away splits, fielding stats, and player bio data. **2016-2025 data loaded (10 years)** — 3,782 players, 14,173 batting season stats, 8,233 pitching season stats, 661,313 batting game logs, 195,734 pitching game logs, 15,379 platoon splits, 16,391 pitching platoon splits, 27,558 home/away splits, 22,303 fielding stats.
- `data_pipeline/pull_stats_fangraphs.py` — OLD FanGraphs pipeline, preserved for reference only. NOT used.
- `data_pipeline/detect_streaks.py` — change-point detection using ruptures PELT. Batting: **11,469 streaks** (T1) + **6,333 sensitive** (T2) + **7,509 sliding** (T3) + **10,335 current form**. Pitching: **30,764 streaks** + **207 sensitive** + **2,042 sliding** + **6,729 current form**.
- `baseball_stats.db` — **153 MB** SQLite DB, ~20 tables covering batting, pitching, fielding, splits, and streaks. Uses Retrosheet player IDs (e.g., `judga001`). OPS+ and ERA+ computed for all player-seasons. Team abbreviations use Retrosheet format (NYA, LAN, CHA, etc.).
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
- **Key files**: `AppState.swift` (state + local interception), `QueryEngine.swift` (orchestrator + local routing/stat explanations/name extraction), `AnthropicService.swift` (Claude API with SSE streaming, prompt caching, Haiku routing), `DatabaseService.swift` (SQLite C API), `PromptStore.swift` (all prompts), `KeychainHelper.swift` (API key storage), `StatDefinitions.swift` (local stat definitions for zero-cost explanations)
- **Views**: `HomeView` (search + animated sample queries), `ResultsView` (results + follow-up), `ResultCard` (user/assistant/error styling), `APIKeySetupView` (first-launch + settings), `AnimatedPlaceholder`, `LoadingIndicator`
- **Streaming**: SSE parsing via `URLSession.shared.bytes(for:)`, typewriter effect via callback-based `onChunk` pattern
- **Database**: 153MB `baseball_stats.db` bundled in Resources (read-only, 2016-2025, 10 years)
- **Stat grid**: 21 stats (G through BABIP, PA and SF excluded for compact 3-row display). Career rows show "--" for OPS+ (multi-season weighting not implemented).
- **Player card bio**: Dynamic age computed from birthdate (updates on player's birthday). Header shows handedness (Bats R / Throws R). About section shows birth date.
- **Query routing**: `simple_lookup`, `streak_finder`, `current_form`, `stat_explanation` — local `classifyLocally()` handles obvious patterns first, then Claude Haiku classifies the rest. `AppState` intercepts ~25% of queries locally before `QueryEngine`.
- **ResultsView layout**: Follow-up input hidden during loading, appears inline below short results or pinned to bottom for long results

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

### Key technical notes
- Claude Sonnet sometimes wraps SQL in markdown code fences — `SQLSanitizer.swift` strips them with regex
- Using Claude Sonnet (`claude-sonnet-4-5-20250929`) for SQL generation, answer generation, streak/form description
- Using Claude Haiku (`claude-haiku-4-5-20251001`) for query routing only
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
| **Pitching stats** | Doubles addressable questions | DONE (full pipeline + iOS) |
| **Historical data (1898+)** | LLMs get increasingly wrong the further back you go | 10 years bundled (2016-2025); pre-2016 needs backend |
| **Statcast data** (exit velo, launch angle, sprint speed) | LLMs are terrible at this; rich analytical queries | NOT STARTED (2025+ only) |
| **Situational splits** (home/away, by month, RISP) | Beyond platoon; LLMs can't do this reliably | Home/away DONE; month/RISP not started |
| **Predictive/pace features** ("on pace for X") | Unique analytical value, not just lookup | BUILT (162-game projections in PlayerCardView) |
| **Real-time/current season freshness** | LLM training data lags; pipeline can be near-real-time | 2016-2025 loaded |

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

### Database size & bundling strategy
Current DB (2016-2025, 10 years): **153 MB** bundled in-app — fast, works offline. Full historical data (1898+) with game logs would be ~1.8 GB — too big to bundle. Game logs are ~85% of size. Season-level stats alone for all history: ~25 MB.

**Strategy: 10 years bundled, backend for historical.** 153 MB compresses to ~65-75 MB in IPA, well under iOS 200 MB cellular limit. Historical queries (pre-2016) go through the backend server. Backend also needed for API key security.

**Important:** When updating the bundled DB, always copy the rebuilt `baseball_stats.db` from the project root into `ios/BaseballStatsEngine/Resources/baseball_stats.db`. They are separate files — the pipeline writes to the project root, but the app bundles from Resources.

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

### Backend Server Plan (NEXT UP — ready to build)

Target: `backend/` directory in the project root. Python FastAPI app buildable and testable locally against `baseball_stats.db`.

**What gets built (code):**
- FastAPI app structure with `main.py`, `routers/`, `services/`
- `/query` endpoint — proxies to Claude API (routing, SQL gen, answer gen). Streams SSE back to the iOS client.
- SQLite integration — reuses `query_engine.py` logic (import directly or refactor into a service)
- Free tier metering — device UUID tracked in a small SQLite table; 5 queries/week enforced server-side; resets weekly
- StoreKit receipt validation — `/validate-receipt` endpoint hits Apple's App Store Server API to verify subscriptions; marks device as paid in DB
- `Dockerfile` + `railway.toml` / `fly.toml` for deployment
- `requirements.txt` / `pyproject.toml`

**What Mark handles (account/infra):**
- Railway or Fly.io account setup + billing
- Moving `ANTHROPIC_API_KEY` to hosting platform env vars
- Apple Developer account for App Store Server API credentials (needed for server-side receipt validation)
- DNS / custom domain (optional)
- Uploading historical DB to server (future — current 153MB is bundled in-app)

**iOS changes needed after backend exists:**
- Swap `AnthropicService` base URL from `api.anthropic.com` to backend URL
- Pass device UUID in request headers for metering
- Pass StoreKit receipt for subscription validation
- Remove on-device API key (KeychainHelper becomes unnecessary)

### Before public/commercial release
1. **Backend server for API key security + historical data** — POC uses direct Claude API calls with key on-device. Server also needed for historical data too large to bundle.
2. **Implement free tier metering + StoreKit subscription** — track weekly query count, paywall UI, $2.99/month + $19.99/year IAP.
3. **App Store description** — AI disclosure (marketing-friendly), Retrosheet attribution (exact wording), Chadwick Bureau attribution.
4. **In-app About/Data Sources screen** — all three attributions (AI, Retrosheet, Chadwick).
5. ~~Swap to commercially licensed data sources~~ DONE (Retrosheet + Chadwick Bureau)
6. ~~League-adjusted offense metric~~ DONE (OPS+)
7. ~~Add pitching stats~~ DONE (season stats, game logs, streaks, splits, current form, ERA+)
8. ~~Expand data to 10 years~~ DONE (2016-2025, 153 MB bundled)
9. Expand historical data (1898-2015) — via backend server for game logs, bundled for season stats
