# Project Memory

## Active Project: Baseball Stats Engine
- **Location**: `/Users/markmezrich/Documents/claude/BaseballStatsEngine/`
- **Design doc**: `/Users/markmezrich/Documents/claude/baseball design doc.pdf`
- **What it is**: iOS app (Swift/SwiftUI) that answers natural language baseball questions using real data. Claude translates questions to SQL, SQLite provides ground truth.
- **Current phase**: Phase 1 + Phase 2 + Phase 3 (iOS app) COMPLETE. Compiles with zero errors/warnings. Next: testing on device, historical data expansion, or commercial data source swap.

### What's been built (all tested & working)
- `data_pipeline/pull_stats.py` — pulls batting stats via pybaseball + platoon splits via FanGraphs API + game logs for qualified batters. **2024-2025 data loaded** (2,924 player-season rows, 1,798 players, 57,762 game log rows for 282 qualified batters). Parameterized for 1871+ later.
- `data_pipeline/detect_streaks.py` — change-point detection using ruptures PELT on per-game OPS. Detects hot/cold/average streaks. **515 streak segments** across 422 player-seasons. Parameters: MIN_SEGMENT_SIZE=7, PENALTY=3, ROLLING_WINDOW=5.
- `baseball_stats.db` — SQLite DB with 6 tables: `players`, `season_batting_stats`, `platoon_splits`, `game_batting_logs`, `streaks`, `streaks_sensitive`. Spot-checked: Judge 2024 = 58 HR, 4 streaks (cold start → dominant middle → Sept slump → hot finish). Ohtani 2024 = 1 segment (consistently elite).
- `schema_description.py` — plain-English schema description for Claude's system prompt (all 6 tables)
- `query_engine.py` — full pipeline: text-to-SQL → answer generation. Has `LLMService` abstraction, conversation history (last 5 exchanges), markdown code fence stripping, `#` comment stripping, two-tier streak detection.
- `cli_poc.py` — interactive terminal CLI. Tested end-to-end: regular stats, splits, comparisons, streaks, off-topic handling all work.
- `data_pipeline/requirements.txt` — pybaseball, anthropic (both installed); ruptures, numpy also installed

### Two-tier streak detection
- **Tier 1 (precomputed, penalty=3)**: Stored in `streaks` table. Catches dramatic performance shifts (e.g., Judge's Sept hot streak at 1.446 OPS). Runs at data pipeline time.
- **Tier 2 (precomputed, penalty=1.5)**: Stored in `streaks_sensitive` table. 335 sensitive streaks for 190 player-seasons with no change points at penalty=3. 7-30 game segments only.
- **Fallback flow**: SQL queries streaks table → 0 "hot"/"cold" rows → check all streaks → if single "average" segment → query `streaks_sensitive` for Tier 2 data.
- **Streak answer prompt** instructs Claude: hot/cold is always relative to that player's own season OPS, never absolute thresholds.
- **Data source agnostic**: PELT only needs game-level batting logs (AB, H, 2B, 3B, HR, BB, PA). Works with any source.

### iOS App (Phase 3 — StatChat)
- **Location**: `ios/`
- **Xcode project**: Generated via XcodeGen (`project.yml`), iOS 17.0+, Swift 6, zero dependencies
- **Architecture**: SwiftUI + @Observable + @MainActor for strict concurrency
- **Key files**: `AppState.swift` (state), `QueryEngine.swift` (orchestrator), `AnthropicService.swift` (Claude API with SSE streaming), `DatabaseService.swift` (SQLite C API), `PromptStore.swift` (all prompts), `KeychainHelper.swift` (API key storage)
- **Views**: `HomeView` (search + animated sample queries), `ResultsView` (results + follow-up), `ResultCard` (user/assistant/error styling), `APIKeySetupView` (first-launch + settings), `AnimatedPlaceholder`, `LoadingIndicator`
- **Streaming**: SSE parsing via `URLSession.shared.bytes(for:)`, typewriter effect via callback-based `onChunk` pattern
- **Database**: 10MB `baseball_stats.db` bundled in Resources (read-only)
- **Query routing**: `simple_lookup`, `streak_finder`, `stat_explanation` — Claude classifies, then dispatches to appropriate handler
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
- **Guaranteed accuracy** — every number from SQL against a real DB, never hallucinated. LLMs still get stats wrong ~5% of the time; fans notice.
- **Streak detection** — strongest differentiator. Game-log-level change-point analysis (PELT) finds hot/cold stretches no LLM can surface from memory.
- **Complex filtered queries** — "Top 10 in OPS with 400+ PA" requires precise SQL filtering, not approximate memory recall.
- **Platoon splits** — vs LHP/RHP data is niche enough that LLMs are unreliable on it.
- **Consistency** — same question, same correct answer every time.

**Commoditizing (LLMs are catching up):**
- Simple stat lookups ("How many HR did Judge hit?") — LLMs are mostly right from training data now
- Basic comparisons and context
- ChatGPT with browsing / Gemini can pull current stats from the web

**High-impact features to widen the gap:**
| Feature | Why it matters | Status |
|---------|---------------|--------|
| **Pitching stats** | Doubles addressable questions | NOT STARTED |
| **Historical data (1871+)** | LLMs get increasingly wrong the further back you go | Pipeline ready, needs data pull |
| **Statcast data** (exit velo, launch angle, sprint speed) | LLMs are terrible at this; rich analytical queries | NOT STARTED (2025+ only) |
| **Situational splits** (home/away, by month, RISP) | Beyond platoon; LLMs can't do this reliably | NOT STARTED |
| **Predictive/pace features** ("on pace for X") | Unique analytical value, not just lookup | NOT STARTED |
| **Real-time/current season freshness** | LLM training data lags; pipeline can be near-real-time | Partial (2024-2025 loaded) |

**Strategic positioning:** The value proposition is shifting from "I can look up stats" (commoditizing) to "I can *analyze* baseball data in ways even the best LLMs can't" (the database + analysis layer is the moat, not the NL interface).

### Commercial Data Strategy (for production/commercial release)
Current dev sources (FanGraphs/pybaseball) are NOT licensed for commercial use. Swap to Lahman (free) + Retrosheet (free) for historical, plus a paid provider for in-season data. Only `pull_stats.py` changes when swapping sources.

### Before public/commercial release
1. **Backend server for API key security** — POC uses direct Claude API calls with key on-device
2. **Swap to commercially licensed data sources**
3. Expand game logs beyond qualified batters, add pitching stats, historical data (1871+)
