import Foundation

enum PlayerNameMatcher {
    nonisolated(unsafe) private(set) static var sortedNames: [String] = []
    nonisolated(unsafe) private(set) static var lastNameIndex: [String: [String]] = [:]

    // MARK: - Stat alias infrastructure

    struct StatInfo: Sendable {
        let dbColumn: String      // e.g. "home_runs"
        let displayAbbrev: String // e.g. "HR"
        let displayName: String   // e.g. "Home Runs"
        let isRate: Bool          // true for AVG, OBP, SLG, OPS, OPS+, ISO, BABIP

        /// Lowercased display name for pill text, but preserves all-caps acronyms (RBI, OPS, BABIP).
        var pillName: String {
            displayName == displayAbbrev ? displayName : displayName.lowercased()
        }
    }

    enum LeaderboardScope: Sendable {
        case season(Int)
        case allTimeSingleSeason
        case career
    }

    /// Maps lowercased aliases to stat info. Built from tuples, longest aliases first for matching.
    static let statAliasMap: [String: StatInfo] = {
        let entries: [(aliases: [String], dbColumn: String, abbrev: String, name: String, isRate: Bool)] = [
            (["home runs", "homers", "dingers", "hr", "home run", "hrs", "homer", "dinger", "taters"],
             "home_runs", "HR", "Home Runs", false),
            (["batting average", "average", "avg", "ba", "batting avg"],
             "batting_avg", "AVG", "Batting Average", true),
            (["runs batted in", "rbis", "rbi", "ribbies"],
             "rbi", "RBI", "RBI", false),
            (["on base plus slugging", "ops"],
             "ops", "OPS", "OPS", true),
            (["ops+", "ops plus", "adjusted ops"],
             "ops_plus", "OPS+", "OPS+", true),
            (["stolen bases", "steals", "sb", "stolen base", "bags"],
             "stolen_bases", "SB", "Stolen Bases", false),
            (["strikeouts", "ks", "k's", "strikeout", "punchouts"],
             "strikeouts", "SO", "Strikeouts", false),
            (["bases on balls", "walks", "bb", "walk"],
             "walks", "BB", "Walks", false),
            (["on-base percentage", "on base percentage", "obp", "on-base", "on base"],
             "obp", "OBP", "On-Base Percentage", true),
            (["slugging percentage", "slugging", "slg"],
             "slg", "SLG", "Slugging Percentage", true),
            (["runs scored", "runs"],
             "runs", "R", "Runs", false),
            (["hits"],
             "hits", "H", "Hits", false),
            (["doubles", "2b"],
             "doubles", "2B", "Doubles", false),
            (["triples", "3b"],
             "triples", "3B", "Triples", false),
            (["games played", "games"],
             "games", "G", "Games", false),
            (["isolated power", "iso", "power"],
             "iso", "ISO", "Isolated Power", true),
            (["batting average on balls in play", "babip"],
             "babip", "BABIP", "BABIP", true),
            (["at-bats", "at bats", "ab"],
             "at_bats", "AB", "At Bats", false),
            (["caught stealing", "cs"],
             "caught_stealing", "CS", "Caught Stealing", false),
            (["hit by pitch", "hbp"],
             "hit_by_pitch", "HBP", "Hit By Pitch", false),
            (["intentional walks", "ibb"],
             "intentional_walks", "IBB", "Intentional Walks", false),
            // --- Pitching stats ---
            (["earned run average", "era"],
             "era", "ERA", "ERA", true),
            (["walks and hits per innings pitched", "whip"],
             "whip", "WHIP", "WHIP", true),
            (["strikeouts per 9 innings", "k/9", "k per 9", "strikeouts per nine"],
             "k_per_9", "K/9", "K/9", true),
            (["walks per 9 innings", "bb/9", "bb per 9", "walks per nine"],
             "bb_per_9", "BB/9", "BB/9", true),
            (["strikeout to walk ratio", "k/bb", "k per bb", "strikeout walk ratio"],
             "k_per_bb", "K/BB", "K/BB", true),
            (["hits per 9 innings", "h/9", "h per 9", "hits per nine"],
             "h_per_9", "H/9", "H/9", true),
            (["home runs per 9 innings", "hr/9", "hr per 9"],
             "hr_per_9", "HR/9", "HR/9", true),
            (["batting average against", "baa", "opponents batting average", "opponent avg"],
             "baa", "BAA", "BAA", true),
            (["era+", "era plus", "adjusted era"],
             "era_plus", "ERA+", "ERA+", true),
            (["wins", "w"],
             "wins", "W", "Wins", false),
            (["losses"],
             "losses", "L", "Losses", false),
            (["saves", "sv"],
             "saves", "SV", "Saves", false),
            (["innings pitched", "ip", "innings"],
             "innings_pitched", "IP", "Innings Pitched", false),
            (["quality starts", "qs"],
             "quality_starts", "QS", "Quality Starts", false),
            (["complete games", "cg"],
             "complete_games", "CG", "Complete Games", false),
            (["games finished", "gf"],
             "games_finished", "GF", "Games Finished", false),
            (["wild pitches", "wp"],
             "wild_pitches", "WP", "Wild Pitches", false),
            (["balks", "bk"],
             "balks", "BK", "Balks", false),
            (["batters faced", "bf"],
             "batters_faced", "BF", "Batters Faced", false),
        ]
        var map: [String: StatInfo] = [:]
        for entry in entries {
            let info = StatInfo(dbColumn: entry.dbColumn, displayAbbrev: entry.abbrev,
                                displayName: entry.name, isRate: entry.isRate)
            for alias in entry.aliases {
                map[alias] = info
            }
        }
        return map
    }()

    /// Stats that are ONLY pitching (not shared with batting)
    static let pitchingOnlyStats: Set<String> = [
        "era", "whip", "k_per_9", "bb_per_9", "k_per_bb", "h_per_9", "hr_per_9",
        "baa", "era_plus", "wins", "losses", "saves", "innings_pitched",
        "quality_starts", "complete_games", "games_finished", "wild_pitches",
        "balks", "batters_faced"
    ]

    /// Check if a StatInfo represents a pitching-only stat
    static func isPitchingStat(_ stat: StatInfo) -> Bool {
        pitchingOnlyStats.contains(stat.dbColumn)
    }

    /// All stat aliases sorted longest first (for greedy matching).
    private static let sortedStatAliases: [String] = {
        Array(statAliasMap.keys).sorted { $0.count > $1.count }
    }()

    /// Find a stat keyword in the input string. Returns the first match (longest alias wins).
    static func matchStat(_ input: String) -> StatInfo? {
        let lower = input.lowercased()
        for alias in sortedStatAliases {
            if containsWord(alias, in: lower) {
                return statAliasMap[alias]
            }
        }
        return nil
    }

    /// Extract a season year from input. If `defaultToMostRecent` is true and no explicit year
    /// is found, returns the max season from the DB.
    static func detectSeason(_ input: String, defaultToMostRecent: Bool = false) -> Int? {
        let lower = input.lowercased()

        // Explicit 4-digit year (1898-2029)
        if let range = lower.range(of: "\\b(189[89]|19\\d{2}|20[0-2]\\d)\\b", options: .regularExpression),
           let year = Int(lower[range]) {
            return year
        }

        // Relative patterns
        let db = DatabaseService()
        let currentYear: Int = {
            if let result = try? db.execute(sql: "SELECT MAX(season) FROM season_batting_stats"),
               let row = result.rows.first, let year = Int(row[0]) {
                return year
            }
            return 2025
        }()

        let relativePatterns: [(patterns: [String], offset: Int)] = [
            (["this year", "this season", "current season"], 0),
            (["last year", "last season", "previous season", "prior season"], -1),
            (["two years ago", "2 years ago"], -2),
            (["three years ago", "3 years ago"], -3),
        ]
        for (patterns, offset) in relativePatterns {
            if patterns.contains(where: { lower.contains($0) }) {
                return currentYear + offset
            }
        }

        if defaultToMostRecent {
            return currentYear
        }

        return nil
    }

    static func load() {
        // Load names from bundled all_players.json (full historical, 22K+ players)
        // Falls back to local DB if JSON not found
        var names: [String] = []
        if let url = Bundle.main.url(forResource: "all_players", withExtension: "json"),
           let data = try? Data(contentsOf: url),
           let players = try? JSONSerialization.jsonObject(with: data) as? [[String: Any]] {
            names = players.compactMap { $0["name"] as? String }
        } else {
            let db = DatabaseService()
            if let result = try? db.execute(sql: "SELECT DISTINCT name FROM players", maxRows: 0) {
                names = result.rows.compactMap { $0.first }
            }
        }

        // Sort longest first so "Bobby Witt Jr." matches before "Bobby Witt"
        sortedNames = names.sorted { $0.count > $1.count }

        // Build last name index for fast lookup
        // Skip suffixes like Jr., Sr., II, III, IV, V to find the actual last name
        let suffixes: Set<String> = ["jr.", "jr", "sr.", "sr", "ii", "iii", "iv", "v"]
        var index: [String: [String]] = [:]
        for name in sortedNames {
            let parts = name.split(separator: " ")
            // Walk backwards past any suffix to find the real last name
            var lastIdx = parts.count - 1
            while lastIdx > 0 && suffixes.contains(parts[lastIdx].lowercased()) {
                lastIdx -= 1
            }
            let key = parts[lastIdx].lowercased()
            index[key, default: []].append(name)
        }
        lastNameIndex = index
    }

    /// If the input is just a player name (full or unambiguous last name), return the canonical name.
    static func matchPlayer(_ input: String) -> String? {
        let trimmed = input.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return nil }
        let lower = trimmed.lowercased()

        // Exact full name match (case-insensitive)
        if let match = sortedNames.first(where: { $0.lowercased() == lower }) {
            return match
        }

        // Last name only — must be unambiguous (exactly one match)
        if let matches = lastNameIndex[lower], matches.count == 1 {
            return matches[0]
        }

        return nil
    }

    /// Detect comparison queries like "compare Judge and Ohtani" or "Judge vs Ohtani".
    /// Returns two canonical player names if both resolve unambiguously.
    static func parseComparison(_ input: String) -> (String, String)? {
        var cleaned = input.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()

        // Strip common prefixes
        for prefix in ["how do ", "how does ", "compare "] {
            if cleaned.hasPrefix(prefix) {
                cleaned = String(cleaned.dropFirst(prefix.count))
                break
            }
        }

        // Strip trailing " compare" (from "how does X compare to Y")
        if cleaned.hasSuffix(" compare") {
            cleaned = String(cleaned.dropLast(" compare".count))
        }

        // Strip trailing punctuation
        cleaned = cleaned.trimmingCharacters(in: CharacterSet(charactersIn: "?.!"))

        // Strip preamble before a question mark (e.g. "who had the better peak career? mantle or judge")
        if let qIndex = cleaned.firstIndex(of: "?") {
            let afterQ = String(cleaned[cleaned.index(after: qIndex)...]).trimmingCharacters(in: .whitespaces)
            if !afterQ.isEmpty {
                cleaned = afterQ
            }
        }

        // Strip preamble before a comma (e.g. "who had the better career, rizzuto or mantle")
        if let commaIndex = cleaned.lastIndex(of: ",") {
            let afterComma = String(cleaned[cleaned.index(after: commaIndex)...]).trimmingCharacters(in: .whitespaces)
            if !afterComma.isEmpty {
                cleaned = afterComma
            }
        }

        // Try splitting on delimiters (longer first to avoid partial matches)
        let delimiters = [" compared to ", " versus ", " vs. ", " vs ", " or ", " and ", " to ", " with "]
        for delimiter in delimiters {
            guard let range = cleaned.range(of: delimiter) else { continue }
            let part1 = String(cleaned[cleaned.startIndex..<range.lowerBound])
                .trimmingCharacters(in: .whitespaces)
            let part2 = String(cleaned[range.upperBound...])
                .trimmingCharacters(in: .whitespaces)

            let m1 = matchPlayer(part1)
            let m2 = matchPlayer(part2)

            guard !part1.isEmpty, !part2.isEmpty,
                  let name1 = m1,
                  let name2 = m2,
                  name1 != name2 else { continue }
            return (name1, name2)
        }

        // Fallback: find two distinct player names anywhere in the string.
        // Handles cases like "who had the better career mantle or aaron judge"
        // where the preamble can't be cleanly stripped.
        let comparisonSignals = [" vs ", " vs. ", " versus ", " or ", " compared to ", " and ", " better than "]
        let hasComparisonSignal = comparisonSignals.contains(where: { cleaned.contains($0) })
        if hasComparisonSignal {
            var found: [String] = []
            var used: Set<String> = []
            // Check full names first (longer names first since sortedNames is sorted by length desc)
            for name in sortedNames {
                let lower = name.lowercased()
                if containsWord(lower, in: cleaned), !used.contains(lower) {
                    found.append(name)
                    used.insert(lower)
                    if found.count == 2 { break }
                }
            }
            // Try last names if we don't have two yet
            if found.count < 2 {
                for (lastName, players) in lastNameIndex {
                    if players.count == 1, containsWord(lastName, in: cleaned) {
                        let fullName = players[0]
                        if !used.contains(fullName.lowercased()) {
                            found.append(fullName)
                            used.insert(fullName.lowercased())
                            if found.count == 2 { break }
                        }
                    }
                }
            }
            if found.count == 2 {
                return (found[0], found[1])
            }
        }

        return nil
    }

    /// Detect historical streak queries like "Judge's hot streaks last year" or "Ohtani cold streaks 2024".
    /// Returns (canonicalName, "hot"/"cold", optionalSeason). Must be checked BEFORE parseCurrentForm
    /// because both trigger on "hot streak" — this catches plural "streaks" or past-tense season references.
    static func parseStreakQuery(_ input: String) -> (name: String, performance: String, season: Int?)? {
        let lower = input.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()

        // Determine hot vs cold
        let hotTriggers = [
            "hot streaks", "hot streak", "best streaks", "best streak",
            "hottest streak", "hottest stretches", "hot runs",
            "when was", "when did", "get hot"
        ]
        let coldTriggers = [
            "cold streaks", "cold streak", "worst streaks", "worst streak",
            "coldest streak", "cold stretches", "slumps", "slump",
            "when was", "when did", "get cold"
        ]

        // Check cold first (more specific phrases like "cold streak" before generic "when was")
        let isCold = coldTriggers.contains(where: { lower.contains($0) })
            && (lower.contains("cold") || lower.contains("worst") || lower.contains("coldest") || lower.contains("slump"))
        let isHot = hotTriggers.contains(where: { lower.contains($0) })
            && (lower.contains("hot") || lower.contains("best") || lower.contains("hottest"))

        guard isHot || isCold else { return nil }
        let performance = isCold ? "cold" : "hot"

        // Distinguish from current-form queries: this parser owns plural "streaks", explicit seasons,
        // and past-tense references. If it's singular "hot streak" with no season context and present-tense
        // wording (e.g. "Judge's hot streak right now"), let parseCurrentForm handle it.
        let hasPlural = lower.contains("streaks") || lower.contains("stretches")
            || lower.contains("runs") || lower.contains("slumps")
        let hasExplicitYear = lower.range(of: "20[12][0-9]", options: .regularExpression) != nil
        let pastTensePatterns = ["last year", "last season", "previous season", "prior season",
                                 "two years ago", "2 years ago", "three years ago", "3 years ago"]
        let hasPastTense = pastTensePatterns.contains(where: { lower.contains($0) })

        // If singular "streak" with no season reference, defer to parseCurrentForm for "hot" queries
        if !hasPlural && !hasExplicitYear && !hasPastTense && performance == "hot" {
            return nil
        }

        // Detect season (reuse same logic as parseSeasonLookup)
        var targetSeason: Int?
        if let range = lower.range(of: "20[12][0-9]", options: .regularExpression),
           let year = Int(lower[range]) {
            targetSeason = year
        } else {
            let db = DatabaseService()
            let currentYear: Int = {
                if let result = try? db.execute(sql: "SELECT MAX(season) FROM season_batting_stats"),
                   let row = result.rows.first, let year = Int(row[0]) {
                    return year
                }
                return 2025
            }()
            let relativePatterns: [(patterns: [String], offset: Int)] = [
                (["this year", "this season"], 0),
                (["last year", "last season", "previous season", "prior season"], -1),
                (["two years ago", "2 years ago"], -2),
                (["three years ago", "3 years ago"], -3),
            ]
            for (patterns, offset) in relativePatterns {
                if patterns.contains(where: { lower.contains($0) }) {
                    targetSeason = currentYear + offset
                    break
                }
            }
        }

        // Find player name — full name first (word-boundary aware)
        var playerName: String?
        for name in sortedNames {
            if containsWord(name.lowercased(), in: lower) {
                playerName = name
                break
            }
        }

        // Try last name — unambiguous only
        if playerName == nil {
            for (lastName, players) in lastNameIndex {
                if containsWord(lastName, in: lower) && players.count == 1 {
                    playerName = players[0]
                    break
                }
            }
        }

        guard let name = playerName else { return nil }
        return (name, performance, targetSeason)
    }

    /// Detect current hot streak queries like "how has Judge been playing lately?" or "is Ohtani hot right now?"
    /// Returns the canonical player name if one resolves unambiguously.
    static func parseCurrentForm(_ input: String) -> String? {
        let lower = input.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()

        // Check for trigger phrases
        let triggers = [
            "lately", "recently", "right now", "current form", "current streak",
            "hot streak", "hot right now", "been playing", "been doing",
            "doing lately", "doing recently", "playing lately", "playing recently",
            "been hitting", "hitting lately", "on fire", "heating up", "locked in",
            "how is", "how has", "how's"
        ]
        guard triggers.contains(where: { lower.contains($0) }) else { return nil }

        // Try to find a player name in the input
        // First try: exact match against known full names (word-boundary aware)
        for name in sortedNames {
            if containsWord(name.lowercased(), in: lower) {
                return name
            }
        }

        // Second try: last name match (word-boundary aware to avoid "rea" in "streak", etc.)
        for (lastName, players) in lastNameIndex {
            if containsWord(lastName, in: lower) && players.count == 1 {
                return players[0]
            }
        }

        return nil
    }

    /// Detect season lookup queries like "How did Judge do last year?" or "Soto 2024 stats".
    /// Returns (canonicalName, season) if the query is a general season stats lookup.
    static func parseSeasonLookup(_ input: String) -> (name: String, season: Int)? {
        let lower = input.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()

        // Determine the "current" year from the DB (max season)
        let db = DatabaseService()
        let currentYear: Int = {
            if let result = try? db.execute(sql: "SELECT MAX(season) FROM season_batting_stats"),
               let row = result.rows.first, let year = Int(row[0]) {
                return year
            }
            return 2025
        }()

        // Detect target season first (needed for disambiguation)
        var targetSeason: Int?
        if let range = lower.range(of: "20[2][0-9]", options: .regularExpression),
           let year = Int(lower[range]) {
            targetSeason = year
        } else {
            let relativePatterns: [(patterns: [String], offset: Int)] = [
                (["this year", "this season", "doing this", "current season"], 0),
                (["last year", "last season", "previous season", "prior season"], -1),
                (["two years ago", "2 years ago"], -2),
                (["three years ago", "3 years ago"], -3),
            ]
            for (patterns, offset) in relativePatterns {
                if patterns.contains(where: { lower.contains($0) }) {
                    targetSeason = currentYear + offset
                    break
                }
            }
        }
        guard let season = targetSeason else { return nil }

        // Find a player name — try full name first
        var playerName: String?
        for name in sortedNames {
            if containsWord(name.lowercased(), in: lower) {
                playerName = name
                break
            }
        }

        // Try last name — unambiguous only
        if playerName == nil {
            for (lastName, players) in lastNameIndex {
                if containsWord(lastName, in: lower) && players.count == 1 {
                    playerName = players[0]
                    break
                }
            }
        }

        guard let name = playerName else { return nil }
        return (name, season)
    }

    // MARK: - Single-stat lookup parser

    /// Detect queries like "Judge home runs", "Ohtani's OPS", "Soto RBI 2024".
    /// Requires player name + stat keyword, excludes leaderboard words.
    static func parseSingleStatLookup(_ input: String) -> (name: String, stat: StatInfo, season: Int)? {
        let lower = input.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()

        // Exclude leaderboard patterns — those belong to parseLeaderboard
        let leaderboardWords = ["leaders", "leader", "leaderboard", "top ", "most ", "best ", "highest", "lowest",
                                "who led", "who leads", "who hit the most", "who had the most", "leading"]
        if leaderboardWords.contains(where: { lower.contains($0) }) { return nil }

        // Exclude career queries — those belong to parseCareerLookup
        if containsWord("career", in: lower) { return nil }

        // Must have a stat keyword
        guard let stat = matchStat(lower) else { return nil }

        // Must have a player name
        var playerName: String?
        for name in sortedNames {
            if containsWord(name.lowercased(), in: lower) {
                playerName = name
                break
            }
        }
        if playerName == nil {
            for (lastName, players) in lastNameIndex {
                if containsWord(lastName, in: lower) && players.count == 1 {
                    playerName = players[0]
                    break
                }
            }
        }
        guard let name = playerName else { return nil }

        let season = detectSeason(lower, defaultToMostRecent: true) ?? 2025
        return (name, stat, season)
    }

    // MARK: - Team alias infrastructure

    /// Maps lowercased team names/nicknames/abbreviations to Retrosheet team codes.
    static let teamAliasMap: [String: String] = [
        // Full names
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
        // Nicknames
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
        // Singular nicknames (common in "best yankee", "top dodger" patterns)
        "yankee": "NYA", "dodger": "LAN", "met": "NYN",
        "astro": "HOU", "phillie": "PHI", "padre": "SDN",
        "mariner": "SEA", "brewer": "MIL", "cardinal": "SLN",
        "guardian": "CLE", "oriole": "BAL", "pirate": "PIT",
        "brave": "ATL", "marlin": "MIA", "national": "WAS",
        // Unambiguous cities
        "boston": "BOS", "houston": "HOU", "detroit": "DET",
        "atlanta": "ATL", "baltimore": "BAL", "cincinnati": "CIN",
        "cleveland": "CLE", "colorado": "COL", "milwaukee": "MIL",
        "minnesota": "MIN", "oakland": "OAK", "philadelphia": "PHI",
        "pittsburgh": "PIT", "seattle": "SEA", "tampa bay": "TBA",
        "tampa": "TBA", "texas": "TEX", "toronto": "TOR",
        "washington": "WAS", "miami": "MIA", "arizona": "ARI",
        "san diego": "SDN", "san francisco": "SFN",
        // Standard abbreviations → Retrosheet codes
        "nyy": "NYA", "nym": "NYN", "chc": "CHN", "chw": "CHA",
        "cws": "CHA", "stl": "SLN", "sfg": "SFN", "sf": "SFN",
        "sd": "SDN", "sdp": "SDN", "lad": "LAN", "laa": "ANA",
        "tb": "TBA", "tbr": "TBA", "kc": "KCA", "kcr": "KCA",
        "wsh": "WAS", "wsn": "WAS",
    ]

    /// All team aliases sorted longest first (for greedy matching).
    private static let sortedTeamAliases: [String] = {
        Array(teamAliasMap.keys).sorted { $0.count > $1.count }
    }()

    /// Find a team alias in the input string. Returns Retrosheet code if found.
    static func matchTeam(_ input: String) -> String? {
        let lower = input.lowercased()
        for alias in sortedTeamAliases {
            if containsWord(alias, in: lower) {
                return teamAliasMap[alias]
            }
        }
        return nil
    }

    /// Exact team name match — returns Retrosheet code only if the entire input is a team alias
    /// (optionally preceded by "the"). Used for direct-to-profile shortcuts.
    static func matchTeamExact(_ input: String) -> String? {
        var lower = input.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        if lower.hasPrefix("the ") { lower = String(lower.dropFirst(4)) }
        return teamAliasMap[lower]
    }

    // MARK: - Career lookup parser

    /// Detect queries like "Judge career stats", "Judge career home runs", "Judge career OPS".
    /// Requires "career" keyword + player name, optional stat keyword.
    static func parseCareerLookup(_ input: String) -> (name: String, stat: StatInfo?)? {
        let lower = input.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()

        // Must contain "career"
        guard containsWord("career", in: lower) else { return nil }

        // Exclude leaderboard patterns — those go to parseLeaderboard with career scope
        let leaderboardWords = ["leaders", "leader", "leaderboard", "top ", "most ", "best ", "highest", "lowest",
                                "who led", "who leads", "who hit the most", "who had the most", "leading"]
        if leaderboardWords.contains(where: { lower.contains($0) }) { return nil }

        // Exclude comparison patterns — those go to parseComparison or backend
        let comparisonWords = [" vs ", " vs. ", " versus ", " compared to ", " or ", " better than ", " and "]
        if comparisonWords.contains(where: { lower.contains($0) }) { return nil }

        // Must have a player name
        var playerName: String?
        for name in sortedNames {
            if containsWord(name.lowercased(), in: lower) {
                playerName = name
                break
            }
        }
        if playerName == nil {
            for (lastName, players) in lastNameIndex {
                if containsWord(lastName, in: lower) && players.count == 1 {
                    playerName = players[0]
                    break
                }
            }
        }
        guard let name = playerName else { return nil }

        // Optional stat keyword
        let stat = matchStat(lower)

        return (name, stat)
    }

    // MARK: - Platoon splits parser

    /// Detect queries like "Judge vs lefties", "Soto splits", "Ohtani against RHP 2024".
    /// Returns (name, hand, season) where hand is "LHP", "RHP", or nil (both).
    static func parsePlatoonSplits(_ input: String) -> (name: String, hand: String?, season: Int)? {
        let lower = input.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()

        let lhpTriggers = ["vs lefties", "against lefties", "vs left-handed", "vs lhp",
                           "versus lefties", "facing lefties", "left-handed pitching",
                           "vs. lefties", "against left-handed", "against lhp",
                           "versus left-handed", "facing left-handed"]
        let rhpTriggers = ["vs righties", "against righties", "vs right-handed", "vs rhp",
                           "versus righties", "facing righties", "right-handed pitching",
                           "vs. righties", "against right-handed", "against rhp",
                           "versus right-handed", "facing right-handed"]
        let bothTriggers = ["platoon splits", "platoon", "splits"]

        var hand: String?
        let hasLHP = lhpTriggers.contains(where: { lower.contains($0) })
        let hasRHP = rhpTriggers.contains(where: { lower.contains($0) })
        let hasBoth = bothTriggers.contains(where: { lower.contains($0) })

        if hasLHP {
            hand = "LHP"
        } else if hasRHP {
            hand = "RHP"
        } else if hasBoth {
            hand = nil
        } else {
            return nil
        }

        // Must have a player name
        var playerName: String?
        for name in sortedNames {
            if containsWord(name.lowercased(), in: lower) {
                playerName = name
                break
            }
        }
        if playerName == nil {
            for (lastName, players) in lastNameIndex {
                if containsWord(lastName, in: lower) && players.count == 1 {
                    playerName = players[0]
                    break
                }
            }
        }
        guard let name = playerName else { return nil }

        let season = detectSeason(lower, defaultToMostRecent: true) ?? 2025
        return (name, hand, season)
    }

    // MARK: - Home/away splits parser

    /// Detect queries like "Judge home vs away", "Soto at home", "Ohtani road splits".
    /// Returns player name, location filter (home/away/nil for both), and season.
    static func parseHomeAwaySplits(_ input: String) -> (name: String, location: String?, season: Int)? {
        let lower = input.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()

        let homeTriggers = ["at home", "home splits", "home stats", "home numbers",
                            "at home field", "home games"]
        let awayTriggers = ["on the road", "road splits", "road stats", "away splits",
                            "away stats", "away games", "road numbers", "road games"]
        let bothTriggers = ["home vs away", "home and away", "home/away", "home away splits",
                            "home away", "home vs. away", "home or away"]

        var location: String?
        let hasBoth = bothTriggers.contains(where: { lower.contains($0) })
        let hasHome = homeTriggers.contains(where: { lower.contains($0) })
        let hasAway = awayTriggers.contains(where: { lower.contains($0) })

        if hasBoth {
            location = nil
        } else if hasHome {
            location = "home"
        } else if hasAway {
            location = "away"
        } else {
            return nil
        }

        // Must have a player name
        var playerName: String?
        for name in sortedNames {
            if containsWord(name.lowercased(), in: lower) {
                playerName = name
                break
            }
        }
        if playerName == nil {
            for (lastName, players) in lastNameIndex {
                if containsWord(lastName, in: lower) && players.count == 1 {
                    playerName = players[0]
                    break
                }
            }
        }
        guard let name = playerName else { return nil }

        let season = detectSeason(lower, defaultToMostRecent: true) ?? 2025
        return (name, location, season)
    }

    // MARK: - Leaderboard parser

    /// Detect queries like "HR leaders", "top 5 OPS", "who hit the most home runs?".
    /// Requires stat keyword + leaderboard trigger, NO player name.
    static func parseLeaderboard(_ input: String) -> (stat: StatInfo, scope: LeaderboardScope, limit: Int)? {
        let lower = input.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()

        let leaderboardTriggers = ["leaders", "leader", "leaderboard", "top ", "most ", "best ", "highest",
                                   "lowest", "who led", "who leads", "who hit the most", "who had the most",
                                   "leading"]
        guard leaderboardTriggers.contains(where: { lower.contains($0) }) else { return nil }

        // Reject team-aggregate questions — "what team had the highest OPS" asks about teams, not players
        let teamAggregateTriggers = ["what team", "which team", "what teams", "which teams"]
        if teamAggregateTriggers.contains(where: { lower.contains($0) }) { return nil }

        // Must have a stat keyword
        guard let stat = matchStat(lower) else { return nil }

        // Reject if any player name is found — that's a single-stat query
        for name in sortedNames {
            if containsWord(name.lowercased(), in: lower) { return nil }
        }
        for (lastName, players) in lastNameIndex {
            if containsWord(lastName, in: lower) && players.count == 1 { return nil }
        }

        // Extract limit from "top N" pattern (default 50 for pagination)
        var limit = 50
        if let range = lower.range(of: "top\\s+(\\d+)", options: .regularExpression) {
            let matched = lower[range]
            if let numRange = matched.range(of: "\\d+", options: .regularExpression),
               let num = Int(matched[numRange]) {
                limit = max(1, min(num, 50))
            }
        }

        let scope: LeaderboardScope
        if lower.contains("career") {
            scope = .career
        } else if lower.contains("all time") || lower.contains("all-time") || lower.contains("single season") {
            scope = .allTimeSingleSeason
        } else {
            let season = detectSeason(lower, defaultToMostRecent: true) ?? 2025
            scope = .season(season)
        }
        return (stat, scope, limit)
    }

    // MARK: - Stat definition parser

    /// Detect queries like "what is OPS?", "explain BABIP", "what does AVG mean?".
    /// Returns (abbreviation, definition) if a definition trigger + stat match is found.
    static func parseStatDefinition(_ input: String) -> (abbrev: String, displayName: String, definition: String)? {
        let lower = input.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()

        let triggers = ["what is ", "what's ", "what are ", "what does ", "what do ",
                        "explain ", "define ", "meaning of ", "definition of ",
                        "tell me about ", "describe ", "how is ", "how do you calculate "]
        let suffixTriggers = [" mean", " meaning", " stand for", " measure",
                              " calculated", " definition"]

        let hasTrigger = triggers.contains(where: { lower.contains($0) })
            || suffixTriggers.contains(where: { lower.contains($0) })
        guard hasTrigger else { return nil }

        // Reject if a player name is present — "what is Judge's OPS" is a single-stat query
        for name in sortedNames {
            if containsWord(name.lowercased(), in: lower) { return nil }
        }
        for (lastName, players) in lastNameIndex {
            if containsWord(lastName, in: lower) && players.count == 1 { return nil }
        }

        // Try matching via statAliasMap (handles natural language like "batting average")
        if let stat = matchStat(lower),
           let definition = StatDefinitions.lookup(stat.displayAbbrev) {
            return (stat.displayAbbrev, stat.displayName, definition)
        }

        // Try direct abbreviation lookup for stats not in statAliasMap (wRC+, WAR, K, etc.)
        let directAbbrevs = ["war", "wrc+", "k", "pa", "sf", "1b"]
        for abbrev in directAbbrevs {
            if containsWord(abbrev, in: lower) {
                let key = abbrev.uppercased()
                if let definition = StatDefinitions.lookup(key == "WRC+" ? "wRC+" : key) {
                    let display = abbrev == "war" ? "WAR" : (abbrev == "wrc+" ? "wRC+" : key)
                    return (display, display, definition)
                }
            }
        }

        return nil
    }

    // MARK: - Threshold parser

    /// Detect queries like "who hit 40 home runs?", "players batting over .300", "who had 100 RBI?".
    /// Requires stat keyword + numeric threshold (league-wide, no player name).
    static func parseThreshold(_ input: String) -> (stat: StatInfo, threshold: Double, comparison: String, season: Int)? {
        let lower = input.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()

        // Reject if a player name is present — this is league-wide only
        for name in sortedNames {
            if containsWord(name.lowercased(), in: lower) { return nil }
        }
        for (lastName, players) in lastNameIndex {
            if containsWord(lastName, in: lower) && players.count == 1 { return nil }
        }

        // Reject leaderboard triggers — those go to parseLeaderboard
        let leaderboardWords = ["leaders", "leader", "leaderboard"]
        if leaderboardWords.contains(where: { containsWord($0, in: lower) }) { return nil }

        // Must have a stat keyword
        guard let stat = matchStat(lower) else { return nil }

        // Extract numeric threshold (skip 4-digit years 1900-2099)
        let numberPattern = try! NSRegularExpression(pattern: "(\\d+\\.?\\d*|\\.\\d+)\\+?")
        let matches = numberPattern.matches(in: lower, range: NSRange(lower.startIndex..., in: lower))

        var threshold: Double?
        for match in matches {
            guard let range = Range(match.range(at: 1), in: lower) else { continue }
            let numStr = String(lower[range])
            guard let num = Double(numStr) else { continue }
            // Skip 4-digit years (1900-2099)
            let intNum = Int(num)
            if intNum >= 1900 && intNum <= 2099 && !numStr.contains(".") { continue }
            threshold = num
            break
        }

        guard let threshold else { return nil }

        // Determine comparison operator
        let underPatterns = ["under ", "fewer than ", "less than ", "below ", "no more than "]
        let comparison: String
        if underPatterns.contains(where: { lower.contains($0) }) {
            comparison = "<="
        } else {
            comparison = ">="
        }

        let season = detectSeason(lower, defaultToMostRecent: true) ?? 2025
        return (stat, threshold, comparison, season)
    }

    // MARK: - Team stats parser

    /// Detect queries like "Yankees hitters", "Dodgers OPS leaders", "Mets home runs 2024".
    /// Requires team alias match, no player name.
    static func parseTeamStats(_ input: String) -> (teamCode: String, stat: StatInfo?, season: Int)? {
        let lower = input.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()

        // Must have a team alias
        guard let teamCode = matchTeam(lower) else { return nil }

        // Reject if a player name is present
        for name in sortedNames {
            if containsWord(name.lowercased(), in: lower) { return nil }
        }
        for (lastName, players) in lastNameIndex {
            if containsWord(lastName, in: lower) && players.count == 1 { return nil }
        }

        let stat = matchStat(lower)
        let season = detectSeason(lower, defaultToMostRecent: true) ?? 2025
        return (teamCode, stat, season)
    }

    // MARK: - Team total parser

    /// Detect aggregate team queries like "how many home runs did the Yankees hit?", "Yankees total RBI".
    /// Requires team + stat + aggregate phrasing. No player name.
    static func parseTeamTotal(_ input: String) -> (teamCode: String, stat: StatInfo, season: Int)? {
        let lower = input.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()

        // Must have a team alias
        guard let teamCode = matchTeam(lower) else { return nil }

        // Must have a recognized stat
        guard let stat = matchStat(lower) else { return nil }

        // Must have aggregate phrasing
        let totalSignals = [
            "how many", "total", "combined", "as a team",
            "did the", "do the", "did they", "do they"
        ]
        let hasTotal = totalSignals.contains { lower.contains($0) }
        guard hasTotal else { return nil }

        // Reject if a player name is present
        for name in sortedNames {
            if containsWord(name.lowercased(), in: lower) { return nil }
        }
        for (lastName, players) in lastNameIndex {
            if containsWord(lastName, in: lower) && players.count == 1 { return nil }
        }

        let season = detectSeason(lower, defaultToMostRecent: true) ?? 2025
        return (teamCode, stat, season)
    }

    // MARK: - Team ranking parser

    /// Detect team-ranking queries like "what team hit the most home runs?", "which team had the highest OPS?".
    /// Returns stat + season. No specific team required.
    static func parseTeamRanking(_ input: String) -> (stat: StatInfo, season: Int)? {
        let lower = input.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()

        // Must have a team-ranking signal
        let teamRankingTriggers = [
            "what team", "which team", "what teams", "which teams",
            "team with the most", "team with the highest", "team with the lowest",
            "team with the fewest", "rank teams", "team rankings",
            "teams by"
        ]
        guard teamRankingTriggers.contains(where: { lower.contains($0) }) else { return nil }

        // Must have a stat keyword
        guard let stat = matchStat(lower) else { return nil }

        let season = detectSeason(lower, defaultToMostRecent: true) ?? 2025
        return (stat, season)
    }

    // MARK: - Fuzzy matching

    /// Find closest player names within edit distance threshold (for "did you mean?" suggestions).
    /// Returns multiple names when a last-name fuzzy match is ambiguous.
    static func fuzzyMatch(_ input: String) -> [String] {
        let trimmed = input.trimmingCharacters(in: .whitespacesAndNewlines)
        guard trimmed.count >= 3 else { return [] }
        let lower = trimmed.lowercased()
        let threshold = lower.count >= 4 ? 2 : 1

        // Check against full names first — single best match
        var bestMatch: String? = nil
        var bestDistance = Int.max

        for name in sortedNames {
            let nameLower = name.lowercased()
            guard abs(nameLower.count - lower.count) <= threshold else { continue }
            let dist = editDistance(lower, nameLower)
            if dist > 0 && dist <= threshold && dist < bestDistance {
                bestDistance = dist
                bestMatch = name
                if dist == 1 { break }
            }
        }

        if let match = bestMatch { return [match] }

        // Check against last names — return all players sharing the best-matching last name
        var bestLastName: String? = nil
        bestDistance = Int.max

        for lastName in lastNameIndex.keys {
            guard abs(lastName.count - lower.count) <= threshold else { continue }
            let dist = editDistance(lower, lastName)
            if dist > 0 && dist <= threshold && dist < bestDistance {
                bestDistance = dist
                bestLastName = lastName
            }
        }

        if let key = bestLastName, let players = lastNameIndex[key] {
            return players
        }

        return []
    }

    /// If the input contains an ambiguous last name (matches multiple players), return those player names.
    static func findAmbiguousPlayers(_ input: String) -> [String]? {
        let lower = input.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()

        // If a full name matches, it's not ambiguous
        for name in sortedNames {
            if containsWord(name.lowercased(), in: lower) { return nil }
        }

        // Check for ambiguous last names
        for (lastName, players) in lastNameIndex {
            if containsWord(lastName, in: lower) && players.count > 1 {
                return players
            }
        }
        return nil
    }

    /// Check if `word` appears in `text` as a whole word (not a substring of another word).
    static func containsWord(_ word: String, in text: String) -> Bool {
        guard let range = text.range(of: word) else { return false }
        let before = range.lowerBound == text.startIndex || !text[text.index(before: range.lowerBound)].isLetter
        let after = range.upperBound == text.endIndex || !text[range.upperBound].isLetter
        return before && after
    }

    private static func editDistance(_ a: String, _ b: String) -> Int {
        let aChars = Array(a)
        let bChars = Array(b)
        let m = aChars.count
        let n = bChars.count
        guard m > 0 else { return n }
        guard n > 0 else { return m }

        var prev = Array(0...n)
        var curr = Array(repeating: 0, count: n + 1)

        for i in 1...m {
            curr[0] = i
            for j in 1...n {
                if aChars[i - 1] == bChars[j - 1] {
                    curr[j] = prev[j - 1]
                } else {
                    curr[j] = 1 + min(prev[j - 1], prev[j], curr[j - 1])
                }
            }
            (prev, curr) = (curr, prev)
        }

        return prev[n]
    }

    static func addLinks(to text: String) -> String {
        guard !sortedNames.isEmpty else { return text }
        var result = text
        for name in sortedNames {
            // Skip if name not present (fast path)
            guard result.range(of: name) != nil else { continue }
            // Replace with markdown link, word-boundary aware
            let escaped = NSRegularExpression.escapedPattern(for: name)
            let pattern = "(?<![\\[\\w])" + escaped + "(?![\\]\\w])"
            let link = "[\(name)](statchat://player/\(name.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? name))"
            if let regex = try? NSRegularExpression(pattern: pattern) {
                result = regex.stringByReplacingMatches(
                    in: result,
                    range: NSRange(result.startIndex..., in: result),
                    withTemplate: link
                )
            }
        }
        return result
    }

    /// Wrap team full names with `statchat://team/CODE` markdown links.
    /// Apply AFTER `addLinks(to:)` so player links are already in place.
    static func addTeamLinks(to text: String) -> String {
        // All 30 team full names sorted longest first (avoid partial matches)
        let teamNames: [(name: String, code: String)] = [
            ("Arizona Diamondbacks", "ARI"), ("Atlanta Braves", "ATL"),
            ("Baltimore Orioles", "BAL"), ("Boston Red Sox", "BOS"),
            ("Chicago Cubs", "CHN"), ("Chicago White Sox", "CHA"),
            ("Cincinnati Reds", "CIN"), ("Cleveland Guardians", "CLE"),
            ("Colorado Rockies", "COL"), ("Detroit Tigers", "DET"),
            ("Houston Astros", "HOU"), ("Kansas City Royals", "KCA"),
            ("Los Angeles Angels", "ANA"), ("Los Angeles Dodgers", "LAN"),
            ("Miami Marlins", "MIA"), ("Milwaukee Brewers", "MIL"),
            ("Minnesota Twins", "MIN"), ("New York Mets", "NYN"),
            ("New York Yankees", "NYA"), ("Oakland Athletics", "OAK"),
            ("Philadelphia Phillies", "PHI"), ("Pittsburgh Pirates", "PIT"),
            ("San Diego Padres", "SDN"), ("San Francisco Giants", "SFN"),
            ("Seattle Mariners", "SEA"), ("St. Louis Cardinals", "SLN"),
            ("Tampa Bay Rays", "TBA"), ("Texas Rangers", "TEX"),
            ("Toronto Blue Jays", "TOR"), ("Washington Nationals", "WAS"),
        ].sorted { $0.name.count > $1.name.count }

        var result = text
        for (name, code) in teamNames {
            guard result.range(of: name) != nil else { continue }
            let escaped = NSRegularExpression.escapedPattern(for: name)
            // Don't re-link if already inside a markdown link
            let pattern = "(?<![\\[\\w])" + escaped + "(?![\\]\\w])"
            let link = "[\(name)](statchat://team/\(code))"
            if let regex = try? NSRegularExpression(pattern: pattern) {
                result = regex.stringByReplacingMatches(
                    in: result,
                    range: NSRange(result.startIndex..., in: result),
                    withTemplate: link
                )
            }
        }
        return result
    }
}
