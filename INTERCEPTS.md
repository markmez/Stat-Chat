# Query Intercepts Reference

Local query intercepts bypass the Claude API and answer directly from the SQLite database. They're faster, free, and deterministic.

## Intercept Chain (most specific -> least specific)

| # | Intercept | Parser | Builder | Example |
|---|-----------|--------|---------|---------|
| 1 | Comparison | `parseComparison` | `buildComparison` | "Judge vs Ohtani" |
| 2 | Streak history | `parseStreakQuery` | `buildStreakList` | "Judge's hot streaks 2024" |
| 3 | Current form | `parseCurrentForm` | `buildCurrentHotStreak` | "How has Judge been lately?" |
| 4 | Single-stat lookup | `parseSingleStatLookup` | `buildSingleStatLookup` | "Judge home runs" |
| 5 | Season lookup | `parseSeasonLookup` | `buildSeasonSummary` | "Judge 2024" |
| 6 | Platoon splits | `parsePlatoonSplits` | `buildPlatoonSplits` | "Judge vs lefties" |
| 7 | Leaderboard | `parseLeaderboard` | `buildLeaderboard` | "HR leaders" |
| 8 | Ambiguous names | `findAmbiguousPlayers` | (inline) | "Devers" (multiple matches) |

Non-matching queries fall through to Claude.

---

## 1. Comparison

**Parser**: `PlayerNameMatcher.parseComparison(_ input:) -> (String, String)?`
**Builder**: `PlayerCardService.buildComparison(player1:player2:) -> String`

**Triggers**: Two player names separated by "vs", "and", "compared to", "versus", "to", "with"
**Prefixes stripped**: "how do", "how does", "compare"

**Examples**:
- "Judge vs Ohtani"
- "compare Soto and Judge"
- "how does Ohtani compare to Judge"

**Response**: STATGRID blocks for current season + career (if multi-season).

---

## 2. Streak History

**Parser**: `PlayerNameMatcher.parseStreakQuery(_ input:) -> (name: String, performance: String, season: Int?)?`
**Builder**: `PlayerCardService.buildStreakList(name:performance:season:) -> String?`

**Triggers**: Player name + "hot streaks", "cold streaks", "slumps", "best/worst streaks", etc. Requires plural or explicit season to distinguish from current form.

**Examples**:
- "Judge's hot streaks 2024"
- "Ohtani cold streaks last year"
- "Soto's best streaks"

**Response**: STATGRID with date-ranged streak rows (G, AB, H, BB, SO, AVG, OBP, SLG, OPS, HR). Summary line with count and top streak.

---

## 3. Current Form

**Parser**: `PlayerNameMatcher.parseCurrentForm(_ input:) -> String?`
**Builder**: `PlayerCardService.buildCurrentHotStreak(name:) -> String?`

**Triggers**: Player name + "lately", "recently", "right now", "current form", "been playing", "on fire", "heating up", "how is/has", etc.

**Examples**:
- "How has Judge been playing lately?"
- "Is Ohtani hot right now?"
- "Judge's current form"

**Response**: STATGRID with form-period stats (G, AB, R, H, HR, RBI, BB, SO, AVG, OBP, SLG, OPS) + FORM metadata line for interactive slider. Comparison to full-season line.

---

## 4. Single-Stat Lookup

**Parser**: `PlayerNameMatcher.parseSingleStatLookup(_ input:) -> (name: String, stat: StatInfo, season: Int)?`
**Builder**: `PlayerCardService.buildSingleStatLookup(name:stat:season:) -> String?`

**Triggers**: Player name + stat keyword. Excludes leaderboard words ("leaders", "top", "most", "best", etc.).
**Season**: Defaults to most recent if not specified.

**Examples**:
- "Judge home runs" -> "Aaron Judge hit 58 home runs in 2025."
- "Ohtani's OPS" -> "Shohei Ohtani posted a .986 OPS in 2025."
- "Soto RBI 2024" -> "Juan Soto drove in 109 runs in 2024."
- "Judge batting average" -> "Aaron Judge posted a .310 AVG in 2025."

**Response**: Natural language sentence with stat-specific verbs (hit HR, drove in RBI, stole SB, posted AVG/OPS/OBP/SLG, etc.). Team in parentheses. Player names auto-linked.

---

## 5. Season Lookup

**Parser**: `PlayerNameMatcher.parseSeasonLookup(_ input:) -> (name: String, season: Int)?`
**Builder**: `PlayerCardService.buildSeasonSummary(name:season:) -> String?`

**Triggers**: Player name + explicit season (year, "last year", "this season", etc.). No stat keyword needed.

**Examples**:
- "Judge 2024"
- "How did Soto do last year?"
- "Ohtani this season"

**Response**: Full STATGRID (21 stats), platoon splits grid, and hot streaks grid.

---

## 6. Platoon Splits

**Parser**: `PlayerNameMatcher.parsePlatoonSplits(_ input:) -> (name: String, hand: String?, season: Int)?`
**Builder**: `PlayerCardService.buildPlatoonSplits(name:hand:season:) -> String?`

**Triggers**: Player name + platoon keyword.
- **LHP**: "vs lefties", "against lefties", "vs left-handed", "vs lhp", "facing lefties", "left-handed pitching"
- **RHP**: same pattern with "righties"/"right-handed"/"rhp"
- **Both**: "splits", "platoon splits", "platoon"

**Season**: Defaults to most recent if not specified.

**Examples**:
- "Judge vs lefties" -> vs LHP row only
- "Soto splits" -> both vs LHP and vs RHP rows
- "Ohtani against RHP 2024" -> vs RHP row, 2024 season

**Response**: STATGRID with platoon headers (PA, AB, H, 2B, 3B, HR, RBI, BB, SO, AVG, OBP, SLG, OPS, ISO, BABIP). Player names auto-linked.

---

## 7. Leaderboard

**Parser**: `PlayerNameMatcher.parseLeaderboard(_ input:) -> (stat: StatInfo, season: Int, limit: Int)?`
**Builder**: `PlayerCardService.buildLeaderboard(stat:season:limit:) -> String`

**Triggers**: Stat keyword + leaderboard word. NO player name (rejects if any found).
- **Leaderboard words**: "leaders", "leader", "top", "most", "best", "highest", "lowest", "who led", "who leads", "who hit the most", "who had the most", "leading", "leaderboard"
- **Limit**: Extracted from "top N" (default 10, clamped 1-50)

**Season**: Defaults to most recent if not specified.

**Examples**:
- "HR leaders" -> top 10 HR, most recent season
- "top 5 OPS 2024" -> top 5 OPS with PA minimum
- "who hit the most home runs?" -> top 10 HR
- "best batting average" -> top 10 AVG with PA minimum

**Response**: Numbered markdown list with bold player names (auto-linked). Rate stats show PA minimum footer.

**PA minimums**: Rate stats (AVG, OBP, SLG, OPS, OPS+, ISO, BABIP) require >= 400 PA for full seasons (max games >= 140) or >= 200 PA for partial seasons.

---

## 8. Ambiguous Names

**Parser**: `PlayerNameMatcher.findAmbiguousPlayers(_ input:) -> [String]?`
**Builder**: Inline in AppState (constructs "Did you mean?" message with tappable links)

**Triggers**: Input contains a last name that matches multiple players, and no full name match.

**Examples**:
- "Devers" (if multiple Devers exist)
- "Martinez stats" (if multiple Martinez players)

**Response**: "Multiple players match that name. Did you mean:" + tappable player links. User selection re-runs the original query with the full name substituted.

---

## Stat Alias Map

Used by single-stat lookup and leaderboard intercepts. Longest alias matched first to avoid partial matches.

| Stat | DB Column | Aliases |
|------|-----------|---------|
| HR | home_runs | home runs, homers, dingers, hr, home run, hrs, homer, dinger, taters |
| AVG | batting_avg | batting average, average, avg, ba, batting avg |
| RBI | rbi | runs batted in, rbis, rbi, ribbies |
| OPS | ops | on base plus slugging, ops |
| OPS+ | ops_plus | ops+, ops plus, adjusted ops |
| SB | stolen_bases | stolen bases, steals, sb, stolen base, bags |
| SO | strikeouts | strikeouts, so, ks, k's, strikeout, punchouts |
| BB | walks | bases on balls, walks, bb, walk |
| OBP | obp | on-base percentage, on base percentage, obp, on-base, on base |
| SLG | slg | slugging percentage, slugging, slg |
| R | runs | runs scored, runs |
| H | hits | hits |
| 2B | doubles | doubles, 2b |
| 3B | triples | triples, 3b |
| G | games | games played, games |
| ISO | iso | isolated power, iso |
| BABIP | babip | batting average on balls in play, babip |
| AB | at_bats | at-bats, at bats, ab |
| CS | caught_stealing | caught stealing, cs |
| HBP | hit_by_pitch | hit by pitch, hbp |
| IBB | intentional_walks | intentional walks, ibb |

**Excluded**: bare "k" (too many false positives), bare "r" (matches too many words), bare "h" (use "hits" instead). All matching is word-boundary safe via `containsWord`.
