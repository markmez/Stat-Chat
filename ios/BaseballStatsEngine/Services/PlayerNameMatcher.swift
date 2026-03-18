import Foundation

/// Result of resolving a search query — used by all search bars for consistent behavior.
enum SearchResult {
    case player(name: String, alternatives: [String])
    case team(code: String)
    case question(String)
}

/// Protocol for search history tracking — AppState conforms to this.
@MainActor
protocol SearchHistoryTracking: AnyObject {
    func addToSearchHistory(_ query: String)
}

enum PlayerNameMatcher {
    nonisolated(unsafe) private(set) static var sortedNames: [String] = []
    nonisolated(unsafe) private(set) static var lastNameIndex: [String: [String]] = [:]
    /// Fast lookup: ASCII-lowercased name → canonical name (for O(1) exact match instead of scanning sortedNames)
    nonisolated(unsafe) private(set) static var nameExactLookup: [String: String] = [:]

    // MARK: - Shared config (loaded from stat_config.json)

    private struct StatConfigFile: Decodable {
        let stat_aliases: [String: StatAliasEntry]
        let pitching_only_stats: [String]
        let common_word_last_names: [String]
        let nickname_aliases: [String: String]
        let disambig_sr_jr_map: [String: [String]]
        let al_teams: [String]
        let nl_teams: [String]

        struct StatAliasEntry: Decodable {
            let aliases: [String]
            let abbrev: String
            let name: String
            let is_rate: Bool
        }
    }

    private static let configFile: StatConfigFile = {
        guard let url = Bundle.main.url(forResource: "stat_config", withExtension: "json"),
              let data = try? Data(contentsOf: url),
              let config = try? JSONDecoder().decode(StatConfigFile.self, from: data) else {
            fatalError("stat_config.json missing or corrupt in app bundle")
        }
        return config
    }()

    // MARK: - Nickname / alias mapping (from shared config)

    private static let nicknameAliases: [String: String] = configFile.nickname_aliases

    /// Sr./Jr. pairs where the base name (without suffix) should trigger disambiguation.
    private static let disambigSrJrMap: [String: [String]] = configFile.disambig_sr_jr_map

    /// The actual calendar year — used as the default season when no year is specified.
    /// This ensures queries like "Judge home runs" resolve to the current year (e.g. 2026),
    /// which may be beyond the local DB range and should fall through to backend.
    static var currentCalendarYear: Int {
        Calendar.current.component(.year, from: Date())
    }

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
        case allTimeSince(Int)
        case career
    }

    /// Maps lowercased aliases to stat info. Built from shared config JSON.
    static let statAliasMap: [String: StatInfo] = {
        var map: [String: StatInfo] = [:]
        for (dbColumn, entry) in configFile.stat_aliases {
            let info = StatInfo(dbColumn: dbColumn, displayAbbrev: entry.abbrev,
                                displayName: entry.name, isRate: entry.is_rate)
            for alias in entry.aliases {
                map[alias] = info
            }
        }
        return map
    }()

    /// Stats that are ONLY pitching (not shared with batting)
    static let pitchingOnlyStats: Set<String> = Set(configFile.pitching_only_stats)

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
        // Handle split phrases like "stolen 60 bases" → "stolen bases"
        // Remove numbers and extra spaces, then retry
        let withoutNumbers = lower.replacingOccurrences(of: "\\d+\\.?\\d*", with: " ", options: .regularExpression)
            .replacingOccurrences(of: "\\s+", with: " ", options: .regularExpression)
            .trimmingCharacters(in: .whitespaces)
        if withoutNumbers != lower {
            for alias in sortedStatAliases {
                if containsWord(alias, in: withoutNumbers) {
                    return statAliasMap[alias]
                }
            }
        }
        return nil
    }

    /// Look up a player's debut year (first season in either batting or pitching stats).
    /// Returns the year after the player's last season.
    /// "Since Ted Williams" (general) → after their career ended.
    private static func lookupPostCareerYear(name: String) -> Int? {
        let db = DatabaseService()
        let sanitized = name.replacingOccurrences(of: "'", with: "''")
        let sql = """
            SELECT MAX(season) FROM (
                SELECT season FROM season_batting_stats s JOIN players p ON s.player_id = p.player_id WHERE p.name = '\(sanitized)'
                UNION ALL
                SELECT season FROM season_pitching_stats sp JOIN players p ON sp.player_id = p.player_id WHERE p.name = '\(sanitized)'
            )
            """
        if let result = try? db.execute(sql: sql),
           let row = result.rows.first, let year = Int(row[0]) {
            return year + 1
        }
        return nil
    }

    /// Returns the year after the player last achieved a stat threshold.
    /// "Closest to .400 since Ted Williams" → Ted hit .406 in 1941 → return 1942.
    /// Falls back to post-career year if the player never achieved the threshold.
    private static func lookupLastThresholdYear(name: String, stat: StatInfo, threshold: Double) -> Int? {
        let db = DatabaseService()
        let sanitized = name.replacingOccurrences(of: "'", with: "''")

        let lowerIsBetter = ["era", "whip", "bb_per_9", "hits_per_9", "hr_per_9"].contains(stat.dbColumn)
        let comparison = lowerIsBetter ? "<=" : ">="
        let table = isPitchingStat(stat) ? "season_pitching_stats" : "season_batting_stats"

        let sql = """
            SELECT MAX(s.season) FROM \(table) s
            JOIN players p ON s.player_id = p.player_id
            WHERE p.name = '\(sanitized)' AND s.\(stat.dbColumn) \(comparison) \(threshold)
            """
        if let result = try? db.execute(sql: sql),
           let row = result.rows.first, let year = Int(row[0]) {
            return year + 1
        }
        // Player never achieved that threshold — fall back to post-career
        return lookupPostCareerYear(name: name)
    }

    /// Extract a season year from input. If `defaultToMostRecent` is true and no explicit year
    /// is found, returns the current calendar year (which may be beyond the local DB range).
    static func detectSeason(_ input: String, defaultToMostRecent: Bool = false) -> Int? {
        let lower = input.lowercased()

        // Explicit 4-digit year (1898-2029)
        if let range = lower.range(of: "\\b(189[89]|19\\d{2}|20[0-2]\\d)\\b", options: .regularExpression),
           let year = Int(lower[range]) {
            return year
        }

        // Relative patterns — use the actual calendar year so "this season" resolves to
        // the real current year (e.g. 2026), not the local DB max (2025).
        let currentYear = currentCalendarYear

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

    /// Detect AL/NL league filter in query text. Returns "AL" or "NL" if found, nil otherwise.
    /// Also returns the input with the league term removed so downstream parsers aren't confused.
    /// Detect AL/NL league filter in query text. Returns league ("AL"/"NL") and cleaned string,
    /// or nil if no league reference found. "(MLB)" is stripped but returns nil league (= all MLB).
    static func detectLeague(_ input: String) -> (league: String?, cleaned: String)? {
        let lower = input.lowercased()

        // "(MLB)" means no league filter — strip it so downstream parsers aren't confused
        if lower.contains("(mlb)") {
            let cleaned = lower.replacingOccurrences(of: "(mlb)", with: "").replacingOccurrences(of: "  ", with: " ").trimmingCharacters(in: .whitespaces)
            return (nil, cleaned)
        }

        // Long phrases first (unambiguous)
        for (phrase, league) in [("american league", "AL"), ("national league", "NL")] {
            if lower.contains(phrase) {
                let cleaned = lower.replacingOccurrences(of: phrase, with: "").replacingOccurrences(of: "  ", with: " ").trimmingCharacters(in: .whitespaces)
                return (league, cleaned)
            }
        }

        // Parenthesized form from suggestion pills: "(AL)" or "(NL)"
        for (token, league) in [("(al)", "AL"), ("(nl)", "NL")] {
            if lower.contains(token) {
                let cleaned = lower.replacingOccurrences(of: token, with: "").replacingOccurrences(of: "  ", with: " ").trimmingCharacters(in: .whitespaces)
                return (league, cleaned)
            }
        }

        // Short codes with word-boundary check to avoid "also", "final", "only", etc.
        let pattern = try! NSRegularExpression(pattern: "\\b(al|nl)\\b")
        if let match = pattern.firstMatch(in: lower, range: NSRange(lower.startIndex..., in: lower)),
           let range = Range(match.range(at: 1), in: lower) {
            let code = String(lower[range]).uppercased()
            let cleaned = lower.replacingCharacters(in: range, with: "").replacingOccurrences(of: "  ", with: " ").trimmingCharacters(in: .whitespaces)
            return (code, cleaned)
        }

        return nil
    }

    /// Strip diacritics: "Acuña" → "Acuna", "Ramírez" → "Ramirez"
    static func stripDiacritics(_ s: String) -> String {
        s.folding(options: .diacriticInsensitive, locale: .current)
    }

    /// First name index for first-name-only searches (e.g., "Gleyber" → "Gleyber Torres")
    nonisolated(unsafe) private(set) static var firstNameIndex: [String: [String]] = [:]

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

        // Deduplicate and sort longest first so "Bobby Witt Jr." matches before "Bobby Witt"
        let unique = Array(Set(names))
        sortedNames = unique.sorted { $0.count > $1.count }

        // Build last name index for fast lookup
        // Skip suffixes like Jr., Sr., II, III, IV, V to find the actual last name
        // Index under both accented and stripped-diacritics keys, and handle hyphens
        let suffixes: Set<String> = ["jr.", "jr", "sr.", "sr", "ii", "iii", "iv", "v"]
        var index: [String: [String]] = [:]
        var fnIndex: [String: [String]] = [:]
        for name in sortedNames {
            let parts = name.split(separator: " ")
            // Walk backwards past any suffix to find the real last name
            var lastIdx = parts.count - 1
            while lastIdx > 0 && suffixes.contains(parts[lastIdx].lowercased()) {
                lastIdx -= 1
            }
            let rawKey = parts[lastIdx].lowercased()
            let asciiKey = stripDiacritics(rawKey)

            // Add under both accented and ASCII keys
            index[rawKey, default: []].append(name)
            if asciiKey != rawKey {
                index[asciiKey, default: []].append(name)
            }

            // Hyphenated names: also index without hyphen (e.g., "crow-armstrong" → "crowarmstrong")
            // and with hyphen replaced by space for multi-word lookup
            if rawKey.contains("-") {
                let noHyphen = rawKey.replacingOccurrences(of: "-", with: "")
                index[noHyphen, default: []].append(name)
            }
            if asciiKey.contains("-") {
                let noHyphen = asciiKey.replacingOccurrences(of: "-", with: "")
                if index[noHyphen] == nil || !index[noHyphen]!.contains(name) {
                    index[noHyphen, default: []].append(name)
                }
            }

            // Build first name index (only for multi-word names)
            if parts.count >= 2 {
                let firstName = stripDiacritics(parts[0].lowercased())
                fnIndex[firstName, default: []].append(name)
            }
        }
        // Cross-link trailing-e variants: "green" ↔ "greene", "brown" ↔ "browne", etc.
        // This ensures searching "Green" also finds players named "Greene" and vice versa.
        let allKeys = Array(index.keys)
        for key in allKeys {
            let variant: String
            if key.hasSuffix("e") {
                variant = String(key.dropLast())
            } else {
                variant = key + "e"
            }
            if let variantPlayers = index[variant] {
                // Merge: add variant's players to this key if not already present
                for player in variantPlayers where !(index[key]?.contains(player) ?? false) {
                    index[key, default: []].append(player)
                }
                // And add this key's players to the variant
                if let keyPlayers = index[key] {
                    for player in keyPlayers where !(index[variant]?.contains(player) ?? false) {
                        index[variant, default: []].append(player)
                    }
                }
            }
        }

        lastNameIndex = index
        firstNameIndex = fnIndex

        // Build exact lookup: ASCII-lowercased → canonical name (first match wins, longest names first)
        var exact: [String: String] = [:]
        for name in sortedNames {
            let key = stripDiacritics(name.lowercased())
            if exact[key] == nil {
                exact[key] = name
            }
        }
        nameExactLookup = exact
    }

    /// For embedded name extraction (queries like "Bobby Witt home runs"), prefer the Jr.
    /// when the base name matches a known Sr./Jr. pair — current stats are more commonly asked about.
    private static func resolveEmbeddedName(_ name: String) -> String {
        let lower = name.lowercased()
        if let candidates = disambigSrJrMap[lower] {
            return candidates[0]  // Jr. is always first in the list
        }
        return name
    }

    /// Find a player name embedded in text. Checks aliases, sortedNames, and lastNameIndex.
    /// For Sr./Jr. pairs, defaults to Jr. (the active player) since embedded queries
    /// are typically about current stats. Direct lookups use matchPlayer() which triggers disambiguation.
    static func findPlayerInText(_ text: String) -> String? {
        let lower = text.lowercased()

        // Check nickname aliases first (longest first)
        for (alias, canonical) in nicknameAliases.sorted(by: { $0.key.count > $1.key.count }) {
            if containsWord(alias, in: lower) {
                return canonical
            }
        }

        // Check full names (longest first, already sorted)
        for name in sortedNames {
            if containsWord(name.lowercased(), in: lower) {
                return resolveEmbeddedName(name)
            }
        }

        // Try unambiguous last name
        for (lastName, players) in lastNameIndex {
            if containsWord(lastName, in: lower) && players.count == 1
                && !commonWordLastNames.contains(lastName) {
                return resolveEmbeddedName(players[0])
            }
        }

        return nil
    }

    /// Normalize suffix variants: "jr" → "jr.", "sr" → "sr.", etc.
    private static func normalizeSuffix(_ input: String) -> String {
        let parts = input.split(separator: " ")
        guard parts.count >= 2 else { return input }
        let last = parts.last!.lowercased()
        // Add period to bare suffixes
        if last == "jr" || last == "sr" {
            return parts.dropLast().joined(separator: " ") + " " + last.capitalized + "."
        }
        return input
    }

    /// If the input is just a player name (full or unambiguous last name), return the canonical name.
    static func matchPlayer(_ input: String) -> String? {
        let trimmed = input.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return nil }
        let lower = trimmed.lowercased()

        // Check nickname/alias map first
        if let canonical = nicknameAliases[lower] {
            return canonical
        }

        // Sr./Jr. pairs — return nil so findAmbiguousPlayers triggers disambiguation
        if disambigSrJrMap[lower] != nil {
            return nil
        }

        // Exact full name match (case-insensitive, accent-insensitive) — O(1) via dictionary
        // Skip single-word names that collide with a last name shared by multiple players
        let ascii = stripDiacritics(lower)
        if let match = nameExactLookup[ascii] {
            let isSingleWord = !match.contains(" ")
            let lookupKey = stripDiacritics(match.split(separator: " ").last?.lowercased() ?? lower)
            if !isSingleWord || (lastNameIndex[lookupKey]?.count ?? 0) <= 1 {
                return match
            }
        }

        // Try with normalized suffix — "Bobby Witt jr" → "Bobby Witt Jr."
        let normalized = normalizeSuffix(trimmed).lowercased()
        let normalizedAscii = stripDiacritics(normalized)
        if normalizedAscii != ascii, let match = nameExactLookup[normalizedAscii] {
            return match
        }

        // "LastName Jr/Sr" pattern — e.g. "Witt Jr" should find "Bobby Witt Jr."
        let suffixPatterns: [(String, String)] = [("jr", "jr."), ("jr.", "jr."), ("sr", "sr."), ("sr.", "sr."),
                                                   ("ii", "ii"), ("iii", "iii")]
        for (suffix, normalizedSuffix) in suffixPatterns {
            if lower.hasSuffix(" \(suffix)") {
                let baseName = stripDiacritics(String(lower.dropLast(suffix.count + 1)))
                // Find players with this last name + suffix
                if let candidates = lastNameIndex[baseName] {
                    let withSuffix = candidates.filter { $0.lowercased().hasSuffix(normalizedSuffix) }
                    if withSuffix.count == 1 {
                        return withSuffix[0]
                    }
                }
            }
        }

        // Last name only — must be unambiguous (exactly one match)
        // Try both accented and ASCII-normalized keys, plus hyphen variants
        let lastNameKey = stripDiacritics(lower).replacingOccurrences(of: " ", with: "-")
        for key in [lower, ascii, lastNameKey, ascii.replacingOccurrences(of: " ", with: "")] {
            if let matches = lastNameIndex[key], matches.count == 1 {
                return matches[0]
            }
        }

        // First name only — must be unambiguous (exactly one match)
        if let matches = firstNameIndex[ascii], matches.count == 1 {
            return matches[0]
        }

        return nil
    }

    /// Match a player name, falling back to prominence-based disambiguation for ambiguous last names.
    /// Used in comparison parsing where we want "Soto" to resolve to Juan Soto (the dominant player).
    /// Match a player name, falling back to prominence-based disambiguation for ambiguous last names.
    /// Returns (matched name, alternative names) — alternatives are other players sharing the last name.
    private static func matchPlayerWithProminence(_ input: String) -> (name: String, alternatives: [String])? {
        // Try exact match first
        if let name = matchPlayer(input) { return (name, []) }
        // If ambiguous last name, pick the most prominent player (current + most games)
        let lower = stripDiacritics(input.trimmingCharacters(in: .whitespacesAndNewlines).lowercased())
        if let candidates = lastNameIndex[lower], candidates.count > 1 {
            let (sorted, _) = sortByProminence(candidates)
            if let first = sorted.first {
                return (first, Array(sorted.dropFirst()))
            }
        }
        return nil
    }

    /// Detect comparison queries like "compare Judge and Ohtani" or "Judge vs Ohtani".
    /// Returns two canonical player names if both resolve unambiguously.
    /// Returns (player1, player2, season, alternatives for disambiguation).
    static func parseComparison(_ input: String) -> (String, String, Int?, [String])? {
        let season = detectSeason(input)
        var cleaned = input.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()

        // Strip year tokens so they don't interfere with player name matching
        if season != nil {
            cleaned = cleaned.replacingOccurrences(of: "\\b(189[89]|19\\d{2}|20[0-2]\\d)\\b", with: "", options: .regularExpression)
                .trimmingCharacters(in: .whitespaces)
            // Also strip relative season phrases
            for phrase in ["this year", "this season", "current season", "last year", "last season",
                          "previous season", "prior season", "two years ago", "2 years ago",
                          "three years ago", "3 years ago"] {
                cleaned = cleaned.replacingOccurrences(of: phrase, with: "")
            }
            cleaned = cleaned.trimmingCharacters(in: .whitespaces)
        }

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

            let m1 = matchPlayerWithProminence(part1)
            let m2 = matchPlayerWithProminence(part2)

            guard !part1.isEmpty, !part2.isEmpty,
                  let r1 = m1,
                  let r2 = m2,
                  r1.name != r2.name else { continue }
            let allAlts = r1.alternatives + r2.alternatives
            return (r1.name, r2.name, season, allAlts)
        }

        // Fallback: find two distinct player names anywhere in the string.
        // Handles cases like "who had the better career mantle or aaron judge"
        // where the preamble can't be cleanly stripped.
        let comparisonSignals = [" vs ", " vs. ", " versus ", " or ", " compared to ", " and ", " better than "]
        let hasComparisonSignal = comparisonSignals.contains(where: { cleaned.contains($0) })
        if hasComparisonSignal {
            if let first = findPlayerInText(cleaned) {
                // Remove the first player's name from the text and search again
                let remaining = cleaned.replacingOccurrences(of: first.lowercased(), with: "")
                if let second = findPlayerInText(remaining), second != first {
                    return (first, second, season, [])
                }
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

        // Find player name
        guard let name = findPlayerInText(lower) else { return nil }
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
        return findPlayerInText(lower)
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

        // Find a player name
        guard let name = findPlayerInText(lower) else { return nil }
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
        guard let name = findPlayerInText(lower) else { return nil }

        let season = detectSeason(lower, defaultToMostRecent: true) ?? currentCalendarYear
        return (name, stat, season)
    }

    // MARK: - Slash line parser

    /// Detect "slash line" queries: "Judge's slash line", "What is Soto's slash line last season"
    static func parseSlashLineLookup(_ input: String) -> (name: String, season: Int)? {
        let lower = input.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard lower.contains("slash line") || lower.contains("slashline") || lower.contains("slash-line") else { return nil }
        guard let name = findPlayerInText(lower) else { return nil }
        let season = detectSeason(lower, defaultToMostRecent: true) ?? currentCalendarYear
        return (name, season)
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

    // MARK: - Unified search resolution

    /// Single entry point for all search bars. Returns a consistent SearchResult
    /// so every view routes the same way. Handles search history and last-name tracking.
    @MainActor
    static func resolveSearch(_ input: String, history: SearchHistoryTracking? = nil) -> SearchResult {
        let trimmed = input.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return .question(trimmed) }

        // Exact player name match
        if let name = matchPlayer(trimmed) {
            trackSearch(trimmed, history: history)
            return .player(name: name, alternatives: [])
        }

        // Exact team name match
        if let code = matchTeamExact(trimmed) {
            trackSearch(trimmed, history: history)
            return .team(code: code)
        }

        // Ambiguous player name → auto-select dominant or route to ResultsView for disambig
        if let ambiguous = findAmbiguousPlayers(trimmed) {
            let (sorted, dominant) = sortByProminence(ambiguous)
            if let idx = dominant {
                trackSearch(sorted[idx], history: history)
                let others = sorted.enumerated().filter { $0.offset != idx }.map(\.element)
                return .player(name: sorted[idx], alternatives: others)
            } else {
                return .question(trimmed)
            }
        }

        // Everything else → ResultsView handles it (fuzzy match, general question)
        trackSearch(trimmed, history: history)
        return .question(trimmed)
    }

    /// Tracks search in history and increments last-name-only counter.
    @MainActor
    private static func trackSearch(_ text: String, history: SearchHistoryTracking?) {
        history?.addToSearchHistory(text)
        if !text.contains(" ") {
            var count = UserDefaults.standard.integer(forKey: "lastNameSearchCount")
            count += 1
            UserDefaults.standard.set(count, forKey: "lastNameSearchCount")
        }
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
        guard let name = findPlayerInText(lower) else { return nil }

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
        guard let name = findPlayerInText(lower) else { return nil }

        let season = detectSeason(lower, defaultToMostRecent: true) ?? currentCalendarYear
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
        guard let name = findPlayerInText(lower) else { return nil }

        let season = detectSeason(lower, defaultToMostRecent: true) ?? currentCalendarYear
        return (name, location, season)
    }

    // MARK: - Pitch type splits parser

    /// Detect queries like "how did X hit against sliders", "X vs fastballs", "X pitch type splits".
    /// Returns player name, optional specific pitch type, and season.
    static func parsePitchTypeSplits(_ input: String) -> (name: String, pitchType: String?, season: Int)? {
        let lower = input.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()

        let pitchTypeMap: [(patterns: [String], dbValue: String)] = [
            (["fastball", "fastballs", "4-seam", "4-seamers", "four-seam", "four seam", "heater", "heaters"], "4-Seam"),
            (["sinker", "sinkers", "two-seam", "two seam", "2-seam"], "Sinker"),
            (["slider", "sliders"], "Slider"),
            (["changeup", "changeups", "change-up", "change up"], "Change"),
            (["curveball", "curveballs", "curve", "curves"], "Curve"),
            (["cutter", "cutters", "cut fastball"], "Cutter"),
            (["sweeper", "sweepers"], "Sweeper"),
            (["splitter", "splitters", "split-finger", "split finger"], "Split"),
        ]

        let generalTriggers = ["pitch type splits", "pitch type", "by pitch type", "by pitch",
                               "pitch splits", "against each pitch"]

        var pitchType: String?
        var hasTrigger = false

        for (patterns, dbValue) in pitchTypeMap {
            if patterns.contains(where: { lower.contains($0) }) {
                pitchType = dbValue
                hasTrigger = true
                break
            }
        }

        if !hasTrigger {
            hasTrigger = generalTriggers.contains(where: { lower.contains($0) })
        }

        // Also catch "against [pitch]" and "vs [pitch]" patterns
        if !hasTrigger {
            let contextTriggers = ["against ", "vs "]
            for trigger in contextTriggers {
                if lower.contains(trigger) {
                    for (patterns, dbValue) in pitchTypeMap {
                        if patterns.contains(where: { lower.contains(trigger + $0) }) {
                            pitchType = dbValue
                            hasTrigger = true
                            break
                        }
                    }
                }
                if hasTrigger { break }
            }
        }

        guard hasTrigger else { return nil }
        guard let name = findPlayerInText(lower) else { return nil }

        let season = detectSeason(lower, defaultToMostRecent: true) ?? currentCalendarYear
        return (name, pitchType, season)
    }

    // MARK: - Count splits parser

    /// Detect queries like "X with two strikes", "X in 3-2 counts", "X ahead in the count".
    /// Returns player name, optional count state filter, and season.
    static func parseCountSplits(_ input: String) -> (name: String, counts: [String]?, season: Int)? {
        let lower = input.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()

        // Specific ball-strike count (e.g. "3-2", "0-2")
        let countRegex = try! NSRegularExpression(pattern: "\\b([0-3]-[0-2])\\b")
        var specificCounts: [String] = []
        let matches = countRegex.matches(in: lower, range: NSRange(lower.startIndex..., in: lower))
        for match in matches {
            if let range = Range(match.range(at: 1), in: lower) {
                specificCounts.append(String(lower[range]))
            }
        }

        // Natural language count groupings
        let twoStrikePatterns = ["two strikes", "2 strikes", "two-strike", "2-strike"]
        let fullCountPatterns = ["full count", "3-2 count"]
        let aheadPatterns = ["ahead in the count", "hitter's count", "hitters count", "batter's count"]
        let behindPatterns = ["behind in the count", "pitcher's count", "pitchers count"]
        let generalTriggers = ["count splits", "by count", "count stats"]

        var counts: [String]?
        var hasTrigger = false

        if !specificCounts.isEmpty {
            counts = specificCounts
            hasTrigger = true
        } else if twoStrikePatterns.contains(where: { lower.contains($0) }) {
            counts = ["0-2", "1-2", "2-2", "3-2"]
            hasTrigger = true
        } else if fullCountPatterns.contains(where: { lower.contains($0) }) {
            counts = ["3-2"]
            hasTrigger = true
        } else if aheadPatterns.contains(where: { lower.contains($0) }) {
            counts = ["1-0", "2-0", "2-1", "3-0", "3-1"]
            hasTrigger = true
        } else if behindPatterns.contains(where: { lower.contains($0) }) {
            counts = ["0-1", "0-2", "1-2"]
            hasTrigger = true
        } else if generalTriggers.contains(where: { lower.contains($0) }) {
            counts = nil  // show all
            hasTrigger = true
        }

        guard hasTrigger else { return nil }
        guard let name = findPlayerInText(lower) else { return nil }

        let season = detectSeason(lower, defaultToMostRecent: true) ?? currentCalendarYear
        return (name, counts, season)
    }

    // MARK: - RISP splits parser

    /// Detect queries like "X with runners in scoring position", "X with RISP".
    /// Returns player name and season.
    static func parseRISPSplits(_ input: String) -> (name: String, season: Int)? {
        let lower = input.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()

        let triggers = ["runners in scoring position", "risp", "scoring position",
                        "runners on base", "men on base", "clutch hitting", "clutch stats",
                        "with runners on"]

        guard triggers.contains(where: { lower.contains($0) }) else { return nil }
        guard let name = findPlayerInText(lower) else { return nil }

        let season = detectSeason(lower, defaultToMostRecent: true) ?? currentCalendarYear
        return (name, season)
    }

    // MARK: - Catch-all player + stat parser

    /// Last-resort parser: any query with a recognizable player name + stat keyword.
    /// Only called after all specific parsers have failed. Catches unusual phrasings like
    /// "Judge's home runs", "tell me Ohtani's ERA", "what was Soto's OPS last year".
    static func parseCatchAllPlayerStat(_ input: String) -> (name: String, stat: StatInfo, season: Int, isCareer: Bool)? {
        let lower = input.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()

        guard let stat = matchStat(lower) else { return nil }
        guard let name = findPlayerInText(lower) else { return nil }

        let isCareer = containsWord("career", in: lower)
        let season = detectSeason(lower, defaultToMostRecent: true) ?? currentCalendarYear
        return (name, stat, season, isCareer)
    }

    // MARK: - Leaderboard parser

    /// Detect queries like "HR leaders", "top 5 OPS", "who hit the most home runs?".
    /// Requires stat keyword + leaderboard trigger, NO player name.
    static func parseLeaderboard(_ input: String) -> (stat: StatInfo, scope: LeaderboardScope, limit: Int, league: String?)? {
        var lower = input.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        let leagueResult = detectLeague(lower)
        if let leagueResult { lower = leagueResult.cleaned }

        let leaderboardTriggers = ["leaders", "leader", "leaderboard", "top ", "most ", "best ", "highest",
                                   "lowest", "who led", "who leads", "who hit the most", "who had the most",
                                   "leading", "closest to", "come closest"]
        guard leaderboardTriggers.contains(where: { lower.contains($0) }) else { return nil }

        // Reject team-aggregate questions — "what team had the highest OPS" asks about teams, not players
        let teamAggregateTriggers = ["what team", "which team", "what teams", "which teams"]
        if teamAggregateTriggers.contains(where: { lower.contains($0) }) { return nil }

        // "closest to .400" → batting average
        // "closest to 60 home runs" → home runs
        var stat: StatInfo?
        var closestToThreshold: Double?
        let isClosestTo = lower.contains("closest to") || lower.contains("come closest")
        if isClosestTo {
            // Try to find a stat from the query
            stat = matchStat(lower)
            // If no explicit stat found but ".400" / ".300" mentioned, it's batting average
            if stat == nil {
                let ratePattern = try? NSRegularExpression(pattern: "\\.\\d{3}")
                if let ratePattern, ratePattern.firstMatch(in: lower, range: NSRange(lower.startIndex..., in: lower)) != nil {
                    stat = statAliasMap["avg"] ?? statAliasMap["batting average"]
                }
            }
            // Extract the target number for threshold lookup
            let numberPattern = try! NSRegularExpression(pattern: "(?:closest to|come closest to)\\s+(\\d+\\.?\\d*|\\.\\d+)")
            if let match = numberPattern.firstMatch(in: lower, range: NSRange(lower.startIndex..., in: lower)),
               let range = Range(match.range(at: 1), in: lower),
               let num = Double(lower[range]) {
                closestToThreshold = num
            }
        } else {
            stat = matchStat(lower)
        }
        guard let stat else { return nil }

        // Check for "since [player name]" or "since [year]" — resolve to allTimeSince scope
        var sinceYear: Int?
        if lower.contains("since ") {
            if let sinceRange = lower.range(of: "since ") {
                let afterSince = String(lower[sinceRange.upperBound...])

                // Check for explicit year after "since" (e.g. "since 2015")
                if let yearRange = afterSince.range(of: "\\b(189[89]|19\\d{2}|20[0-2]\\d)\\b", options: .regularExpression),
                   let year = Int(afterSince[yearRange]) {
                    sinceYear = year
                }

                // Check for player name after "since" (e.g. "since Ted Williams")
                // For "closest to X since [player]", find when the player last achieved X.
                // For general "since [player]", use post-career year.
                if sinceYear == nil {
                    for name in sortedNames {
                        if afterSince.hasPrefix(name.lowercased()) || containsWord(name.lowercased(), in: afterSince) {
                            if isClosestTo, let threshold = closestToThreshold {
                                sinceYear = lookupLastThresholdYear(name: name, stat: stat, threshold: threshold)
                            } else {
                                sinceYear = lookupPostCareerYear(name: name)
                            }
                            break
                        }
                    }
                }
                // Also check last names
                if sinceYear == nil {
                    for (lastName, players) in lastNameIndex where players.count == 1 {
                        if containsWord(lastName, in: afterSince) {
                            if isClosestTo, let threshold = closestToThreshold {
                                sinceYear = lookupLastThresholdYear(name: players[0], stat: stat, threshold: threshold)
                            } else {
                                sinceYear = lookupPostCareerYear(name: players[0])
                            }
                            break
                        }
                    }
                }
            }
        }

        // Check for "over the last N years", "last decade", "this century", "past N years"
        if sinceYear == nil {
            let currentYear = currentCalendarYear
            // "last decade" / "past decade"
            if lower.contains("last decade") || lower.contains("past decade") {
                sinceYear = currentYear - 10
            }
            // "this century" / "21st century"
            else if lower.contains("this century") || lower.contains("21st century") {
                sinceYear = 2000
            }
            // "last/past N years" or "over the last N years"
            else if let range = lower.range(of: "(?:last|past)\\s+(\\d+)\\s+years?", options: .regularExpression) {
                let matched = String(lower[range])
                if let numRange = matched.range(of: "\\d+", options: .regularExpression),
                   let n = Int(matched[numRange]), n > 1, n <= 100 {
                    sinceYear = currentYear - n
                }
            }
        }

        // Only reject player names if not in "since [player]" context
        if sinceYear == nil {
            for name in sortedNames {
                if containsWord(name.lowercased(), in: lower) { return nil }
            }
            for (lastName, players) in lastNameIndex {
                if containsWord(lastName, in: lower) && players.count == 1
                    && !commonWordLastNames.contains(lastName) { return nil }
            }
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
        if let since = sinceYear {
            scope = .allTimeSince(since)
        } else if lower.contains("career") {
            scope = .career
        } else if lower.contains("all time") || lower.contains("all-time") || lower.contains("single season") {
            scope = .allTimeSingleSeason
        } else {
            let season = detectSeason(lower, defaultToMostRecent: true) ?? currentCalendarYear
            scope = .season(season)
        }
        return (stat, scope, limit, leagueResult?.league ?? nil)
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
            if containsWord(lastName, in: lower) && players.count == 1
                && !commonWordLastNames.contains(lastName) { return nil }
        }

        // Try matching via statAliasMap (handles natural language like "batting average")
        if let stat = matchStat(lower),
           let definition = StatDefinitions.lookup(stat.displayAbbrev) {
            return (stat.displayAbbrev, stat.displayName, definition)
        }

        // Try direct abbreviation lookup for stats not in statAliasMap (wRC+, WAR, K, etc.)
        let directAbbrevs = ["war", "wrc+", "woba", "fip", "k", "pa", "sf", "1b"]
        for abbrev in directAbbrevs {
            if containsWord(abbrev, in: lower) {
                let key = abbrev.uppercased()
                let lookupKey = key == "WRC+" ? "wRC+" : (key == "WOBA" ? "wOBA" : (key == "FIP" ? "FIP" : key))
                if let definition = StatDefinitions.lookup(lookupKey) {
                    let display = abbrev == "war" ? "WAR" : (abbrev == "wrc+" ? "wRC+" : (abbrev == "woba" ? "wOBA" : (abbrev == "fip" ? "FIP" : key)))
                    return (display, display, definition)
                }
            }
        }

        return nil
    }

    // MARK: - Threshold parser

    /// Detect queries like "who hit 40 home runs?", "players batting over .300", "who had 100 RBI?".
    /// Requires stat keyword + numeric threshold (league-wide, no player name).
    static func parseThreshold(_ input: String) -> (stat: StatInfo, threshold: Double, comparison: String, season: Int?, league: String?)? {
        var lower = input.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        let leagueResult = detectLeague(lower)
        if let leagueResult { lower = leagueResult.cleaned }

        // Reject if a player name is present — this is league-wide only
        for name in sortedNames {
            if containsWord(name.lowercased(), in: lower) { return nil }
        }
        for (lastName, players) in lastNameIndex {
            if containsWord(lastName, in: lower) && players.count == 1
                && !commonWordLastNames.contains(lastName) { return nil }
        }

        // Reject leaderboard triggers — those go to parseLeaderboard
        let leaderboardWords = ["leaders", "leader", "leaderboard", "top ", "most ", "best ",
                                "highest", "lowest", "who led", "who leads", "leading"]
        if leaderboardWords.contains(where: { lower.contains($0) }) { return nil }

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
        let underPatterns = ["under ", "fewer than ", "less than ", "below ", "no more than ",
                             "or fewer", "or less"]
        let comparison: String
        if underPatterns.contains(where: { lower.contains($0) }) {
            comparison = "<="
        } else {
            comparison = ">="
        }

        // If explicit season ("last season", "2024"), use it. Otherwise nil = all-time.
        let season = detectSeason(lower)
        return (stat, threshold, comparison, season, leagueResult?.league ?? nil)
    }

    // MARK: - Superlative parser

    enum Superlative: String {
        case youngest, oldest, first, last
    }

    /// Detect queries like "youngest player to hit 50 HR", "oldest to win 20 games",
    /// "first player to steal 100 bases", "last player to bat .400".
    static func parseSuperlative(_ input: String) -> (stat: StatInfo, threshold: Double, superlative: Superlative, league: String?)? {
        var lower = input.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        let leagueResult = detectLeague(lower)
        if let leagueResult { lower = leagueResult.cleaned }

        // Detect superlative type
        let superlative: Superlative
        if lower.contains("youngest") || lower.contains("how young") {
            superlative = .youngest
        } else if lower.contains("oldest") || lower.contains("how old") {
            superlative = .oldest
        } else if lower.contains("first player") || lower.contains("first to") || lower.contains("who was the first") || lower.contains("first person") {
            superlative = .first
        } else if lower.contains("last player") || lower.contains("last to") || lower.contains("most recent") ||
                    lower.contains("last time someone") || lower.contains("when was the last") || lower.contains("last person") {
            superlative = .last
        } else {
            return nil
        }

        // Reject if a specific player name is present
        for name in sortedNames {
            if containsWord(name.lowercased(), in: lower) { return nil }
        }
        for (lastName, players) in lastNameIndex {
            if containsWord(lastName, in: lower) && players.count == 1
                && !commonWordLastNames.contains(lastName) { return nil }
        }

        // Must have a stat keyword
        guard let stat = matchStat(lower) else { return nil }

        // Extract numeric threshold (skip years)
        let numberPattern = try! NSRegularExpression(pattern: "(\\d+\\.?\\d*|\\.\\d+)\\+?")
        let matches = numberPattern.matches(in: lower, range: NSRange(lower.startIndex..., in: lower))

        var threshold: Double?
        for match in matches {
            guard let range = Range(match.range(at: 1), in: lower) else { continue }
            let numStr = String(lower[range])
            guard let num = Double(numStr) else { continue }
            let intNum = Int(num)
            if intNum >= 1900 && intNum <= 2099 && !numStr.contains(".") { continue }
            threshold = num
            break
        }

        guard let threshold else { return nil }
        return (stat, threshold, superlative, leagueResult?.league ?? nil)
    }

    // MARK: - Filtered leaderboard parser

    /// Detect queries like "most home runs with a .300+ batting average",
    /// "highest OPS with fewer than 50 strikeouts", "lowest ERA with 200+ innings pitched".
    /// Returns: rankStat (what to sort by), filterStat (what to filter on), threshold, comparison, season.
    static func parseFilteredLeaderboard(_ input: String) -> (rankStat: StatInfo, filterStat: StatInfo, threshold: Double, comparison: String, season: Int?, limit: Int, league: String?)? {
        var lower = input.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        let leagueResult = detectLeague(lower)
        if let leagueResult { lower = leagueResult.cleaned }

        // Must have a leaderboard trigger for the ranking stat
        let leaderboardTriggers = ["most ", "highest ", "lowest ", "best ", "fewest "]
        guard leaderboardTriggers.contains(where: { lower.contains($0) }) else { return nil }

        // Must have "with" or "while" or "among" separating rank stat from filter stat
        let separators = ["with ", "while ", "among players with ", "among those with "]
        guard let separator = separators.first(where: { lower.contains($0) }) else { return nil }

        guard let sepRange = lower.range(of: separator) else { return nil }
        let rankPart = String(lower[lower.startIndex..<sepRange.lowerBound])
        let filterPart = String(lower[sepRange.upperBound...])

        // Reject if a specific player name is present
        for name in sortedNames {
            if containsWord(name.lowercased(), in: lower) { return nil }
        }

        // Extract ranking stat from the first part
        guard let rankStat = matchStat(rankPart) else { return nil }

        // Extract filter stat from the second part
        guard let filterStat = matchStat(filterPart) else { return nil }

        // Must be two different stats
        guard rankStat.dbColumn != filterStat.dbColumn else { return nil }

        // Extract numeric threshold from filter part (skip years)
        let numberPattern = try! NSRegularExpression(pattern: "(\\d+\\.?\\d*|\\.\\d+)\\+?")
        let matches = numberPattern.matches(in: filterPart, range: NSRange(filterPart.startIndex..., in: filterPart))

        var threshold: Double?
        for match in matches {
            guard let range = Range(match.range(at: 1), in: filterPart) else { continue }
            let numStr = String(filterPart[range])
            guard let num = Double(numStr) else { continue }
            let intNum = Int(num)
            if intNum >= 1900 && intNum <= 2099 && !numStr.contains(".") { continue }
            threshold = num
            break
        }

        guard let threshold else { return nil }

        // Determine comparison for filter
        let underPatterns = ["under ", "fewer than ", "less than ", "below ", "no more than ",
                             "or fewer", "or less"]
        let comparison: String
        if underPatterns.contains(where: { filterPart.contains($0) }) {
            comparison = "<="
        } else {
            comparison = ">="
        }

        let season = detectSeason(lower)
        let limit = 10
        return (rankStat, filterStat, threshold, comparison, season, limit, leagueResult?.league ?? nil)
    }

    // MARK: - Milestone parser

    /// Detect cross-season counting queries like "how many times has someone hit 50 HR?"
    /// or "has anyone ever hit 60 home runs?". Returns stat, threshold, and optional year range.
    static func parseMilestone(_ input: String) -> (stat: StatInfo, threshold: Double, since: Int?, league: String?)? {
        var lower = input.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        let leagueResult = detectLeague(lower)
        if let leagueResult { lower = leagueResult.cleaned }

        let milestoneTriggers = ["how many times", "how many players", "how many seasons",
                                 "how often", "has anyone ever", "has anybody ever",
                                 "has anyone", "has a player ever", "ever hit", "ever had",
                                 "ever batted", "ever pitched", "ever thrown", "ever won"]
        guard milestoneTriggers.contains(where: { lower.contains($0) }) else { return nil }

        // Reject if a specific player name is present
        for name in sortedNames {
            if containsWord(name.lowercased(), in: lower) { return nil }
        }
        for (lastName, players) in lastNameIndex {
            if containsWord(lastName, in: lower) && players.count == 1
                && !commonWordLastNames.contains(lastName) { return nil }
        }

        // Must have a stat keyword
        guard let stat = matchStat(lower) else { return nil }

        // Extract numeric threshold (skip years)
        let numberPattern = try! NSRegularExpression(pattern: "(\\d+\\.?\\d*|\\.\\d+)\\+?")
        let matches = numberPattern.matches(in: lower, range: NSRange(lower.startIndex..., in: lower))

        var threshold: Double?
        for match in matches {
            guard let range = Range(match.range(at: 1), in: lower) else { continue }
            let numStr = String(lower[range])
            guard let num = Double(numStr) else { continue }
            let intNum = Int(num)
            if intNum >= 1898 && intNum <= 2099 && !numStr.contains(".") { continue }
            threshold = num
            break
        }

        guard let threshold else { return nil }

        // Check for "since YYYY" constraint
        let since = detectSeason(lower, defaultToMostRecent: false)

        return (stat, threshold, since, leagueResult?.league ?? nil)
    }

    // MARK: - Composite threshold parser (30/30, 40/40, etc.)

    /// Detect queries like "how many 30/30 seasons", "who has gone 40/40", "30-30 club".
    /// Returns the threshold number (e.g. 30 for "30/30").
    static func parseCompositeThreshold(_ input: String) -> Int? {
        let lower = input.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()

        // Detect X/X, X-X, or "X X" patterns for common values
        let compositePattern = try! NSRegularExpression(pattern: "\\b(20|25|30|40|50)[/\\- ](20|25|30|40|50)\\b")
        let matches = compositePattern.matches(in: lower, range: NSRange(lower.startIndex..., in: lower))

        guard let match = matches.first,
              let r1 = Range(match.range(at: 1), in: lower),
              let r2 = Range(match.range(at: 2), in: lower),
              let n1 = Int(lower[r1]),
              let n2 = Int(lower[r2]),
              n1 == n2 else { return nil }

        // Must have a trigger phrase that indicates a question about who/how many
        let triggers = ["how many", "who has", "who have", "most", "players",
                        "seasons", "club", "members", "times", "ever", "history",
                        "list", "all time", "all-time", "has anyone", "has there",
                        "how often", "tell me about"]
        let hasTrigger = triggers.contains(where: { lower.contains($0) })

        // Also allow bare "30/30" or "30/30 seasons" as standalone queries
        let words = lower.split(separator: " ")
        let isShortQuery = words.count <= 4

        guard hasTrigger || isShortQuery else { return nil }

        return n1
    }

    // MARK: - Triple Crown parser

    /// Detect queries about the Triple Crown — "who won the triple crown?", "triple crown winners",
    /// "how many triple crowns have there been?", "has anyone won the triple crown recently?"
    static func parseTripleCrown(_ input: String) -> Bool {
        let lower = input.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        return lower.contains("triple crown")
    }

    // MARK: - Consecutive streak parser (hitting streak, on-base streak)

    struct ConsecutiveStreakQuery {
        enum StreakType { case hit, onbase }
        let type: StreakType
        let playerName: String?
        let season: Int?
    }

    /// Detect queries like "longest hitting streak", "Judge's hit streak", "on-base streak record".
    static func parseConsecutiveStreak(_ input: String) -> ConsecutiveStreakQuery? {
        let lower = input.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()

        // Determine streak type
        let streakType: ConsecutiveStreakQuery.StreakType
        let onBasePatterns = ["on-base streak", "on base streak", "reaching base streak",
                              "onbase streak", "consecutive games reaching base",
                              "consecutive games on base"]
        let hitPatterns = ["hitting streak", "hit streak", "game hitting streak",
                           "game hit streak", "consecutive hit", "consecutive game hit",
                           "consecutive games with a hit"]

        if onBasePatterns.contains(where: { lower.contains($0) }) {
            streakType = .onbase
        } else if hitPatterns.contains(where: { lower.contains($0) }) {
            streakType = .hit
        } else {
            return nil
        }

        // Try to find a player name (use prominence-based matching for ambiguous last names)
        let playerName: String?
        if let name = findPlayerInText(lower) {
            playerName = name
        } else if let result = matchPlayerWithProminence(lower) {
            playerName = result.name
        } else {
            playerName = nil
        }

        // Detect season
        let season = detectSeason(lower, defaultToMostRecent: false)

        return ConsecutiveStreakQuery(type: streakType, playerName: playerName, season: season)
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
            if containsWord(lastName, in: lower) && players.count == 1
                && !commonWordLastNames.contains(lastName) { return nil }
        }

        let stat = matchStat(lower)
        let season = detectSeason(lower, defaultToMostRecent: true) ?? currentCalendarYear
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
            if containsWord(lastName, in: lower) && players.count == 1
                && !commonWordLastNames.contains(lastName) { return nil }
        }

        let season = detectSeason(lower, defaultToMostRecent: true) ?? currentCalendarYear
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

        let season = detectSeason(lower, defaultToMostRecent: true) ?? currentCalendarYear
        return (stat, season)
    }

    // MARK: - Month query parser

    /// Detect queries like "How did Judge hit in September?", "Ohtani's stats in July",
    /// "Judge in April 2025", "Soto September stats", "Judge in May last season".
    /// Returns (playerName, month 1-12, season).
    static func parseMonthQuery(_ input: String) -> (playerName: String, month: Int, season: Int)? {
        let lower = input.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()

        // Must mention a month
        let months: [(names: [String], number: Int)] = [
            (["january", "jan"], 1), (["february", "feb"], 2), (["march", "mar"], 3),
            (["april", "apr"], 4), (["may"], 5), (["june", "jun"], 6),
            (["july", "jul"], 7), (["august", "aug"], 8), (["september", "sept", "sep"], 9),
            (["october", "oct"], 10), (["november", "nov"], 11), (["december", "dec"], 12),
        ]

        var detectedMonth: Int?
        for (names, number) in months {
            for monthName in names {
                if containsWord(monthName, in: lower) {
                    detectedMonth = number
                    break
                }
            }
            if detectedMonth != nil { break }
        }
        guard let month = detectedMonth else { return nil }

        // Exclude leaderboard patterns
        let leaderboardWords = ["leaders", "leader", "leaderboard", "top ", "most ", "best ", "highest", "lowest",
                                "who led", "who leads", "who hit the most", "who had the most", "leading"]
        if leaderboardWords.contains(where: { lower.contains($0) }) { return nil }

        // Exclude career queries
        if containsWord("career", in: lower) { return nil }

        // Find a player name
        guard let name = findPlayerInText(lower) else { return nil }

        let season = detectSeason(lower, defaultToMostRecent: true) ?? currentCalendarYear
        return (name, month, season)
    }

    // MARK: - Fuzzy matching

    /// Find closest player names within edit distance threshold (for "did you mean?" suggestions).
    /// Returns multiple names when a last-name fuzzy match is ambiguous.
    static func fuzzyMatch(_ input: String) -> [String] {
        let trimmed = input.trimmingCharacters(in: .whitespacesAndNewlines)
        guard trimmed.count >= 3 else { return [] }
        let lower = trimmed.lowercased()
        let ascii = stripDiacritics(lower)
        let threshold = ascii.count >= 4 ? 2 : 1

        // Check against full names first (accent-insensitive) — single best match
        var bestMatch: String? = nil
        var bestDistance = Int.max

        for name in sortedNames {
            let nameAscii = stripDiacritics(name.lowercased())
            guard abs(nameAscii.count - ascii.count) <= threshold else { continue }
            let dist = editDistance(ascii, nameAscii)
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
            guard abs(lastName.count - ascii.count) <= threshold else { continue }
            let dist = editDistance(ascii, lastName)
            if dist > 0 && dist <= threshold && dist < bestDistance {
                bestDistance = dist
                bestLastName = lastName
            }
        }

        if let key = bestLastName, let players = lastNameIndex[key] {
            return players
        }

        // Check first names as fuzzy match
        for firstName in firstNameIndex.keys {
            guard abs(firstName.count - ascii.count) <= threshold else { continue }
            let dist = editDistance(ascii, firstName)
            if dist > 0 && dist <= threshold && dist < bestDistance {
                bestDistance = dist
                bestLastName = firstName
            }
        }

        if let key = bestLastName, let players = firstNameIndex[key] {
            return players
        }

        return []
    }

    /// If the input contains an ambiguous last name (matches multiple players), return those player names.
    /// Last names that are extremely common English adjectives/adverbs — these almost never
    /// refer to a player when used in natural language baseball queries like "best hitter",
    /// "most home runs", "good season". Notable player last names (Rice, Young, Hill, Bell,
    /// Park, etc.) are intentionally NOT here.
    /// Last names that are common English words — these should not be treated as player names
    /// unless the full name (first + last) is present. Covers unambiguous DB last names that
    /// collide with verbs, adjectives, nouns, and superlatives used in natural language queries.
    /// When someone types one of these, they almost certainly mean the word, not the player.
    /// The player is still reachable via full name or "see also" disambiguation.
    ///
    /// Common English words that are also unambiguous player last names (1 player in DB).
    /// Loaded from shared stat_config.json. Used by findPlayerInText to avoid false matches.
    ///
    /// NOTE: Multi-player last names like "young" (59 players), "hill" (43), "king" (28) are
    /// already rejected by the `players.count == 1` check.
    ///
    /// EXCLUDED from this list (well-known players whose last names have no baseball meaning):
    /// bench (Johnny Bench), belt (Brandon Belt), story (Trevor Story), penny (Brad Penny),
    /// dye (Jermaine Dye), deer (Rob Deer), duke (Zach Duke), beer (Seth Beer),
    /// steer (Spencer Steer), cave (Jake Cave)
    private static let commonWordLastNames: Set<String> = Set(configFile.common_word_last_names)

    static func findAmbiguousPlayers(_ input: String) -> [String]? {
        let lower = input.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        let ascii = stripDiacritics(lower)

        // If an alias resolves it, it's not ambiguous
        if nicknameAliases[lower] != nil || nicknameAliases[ascii] != nil {
            return nil
        }

        // Also check with normalized suffix
        let normalized = normalizeSuffix(input.trimmingCharacters(in: .whitespacesAndNewlines)).lowercased()
        if normalized != lower && nicknameAliases[normalized] != nil {
            return nil
        }

        // "LastName Jr/Sr" pattern — if it resolves to a single player, not ambiguous
        let suffixPatterns: [(String, String)] = [("jr", "jr."), ("jr.", "jr."), ("sr", "sr."), ("sr.", "sr.")]
        for (suffix, normalizedSuffix) in suffixPatterns {
            if lower.hasSuffix(" \(suffix)") {
                let baseName = stripDiacritics(String(lower.dropLast(suffix.count + 1)))
                if let candidates = lastNameIndex[baseName] {
                    let withSuffix = candidates.filter { $0.lowercased().hasSuffix(normalizedSuffix) }
                    if withSuffix.count == 1 { return nil }
                }
            }
        }

        // Sr./Jr. pairs — trigger disambiguation with the known candidates
        if let candidates = disambigSrJrMap[lower] ?? disambigSrJrMap[ascii] {
            return candidates
        }

        // If an exact full name matches (accent-insensitive), it's not ambiguous
        // (Skip single-word names that collide with last names shared by multiple players)
        if let match = nameExactLookup[ascii] {
            if match.contains(" ") || (lastNameIndex[ascii]?.count ?? 0) <= 1 {
                return nil
            }
        }

        // Also check normalized suffix against full names
        let normalizedAscii = stripDiacritics(normalized)
        if normalizedAscii != ascii, let match = nameExactLookup[normalizedAscii] {
            if match.contains(" ") || (lastNameIndex[normalizedAscii]?.count ?? 0) <= 1 {
                return nil
            }
        }

        // Check for ambiguous last names (accent-insensitive, hyphen-insensitive)
        // Try: exact key, ASCII-stripped key, space→hyphen, spaces removed (for multi-word last names)
        let searchKeys = Set([lower, ascii, ascii.replacingOccurrences(of: " ", with: "-"), ascii.replacingOccurrences(of: " ", with: "")])
        for key in searchKeys {
            if let players = lastNameIndex[key], players.count > 1 {
                if commonWordLastNames.contains(key) { continue }
                // Deduplicate: same player might appear under both accented and ASCII keys
                return Array(Set(players))
            }
        }

        // First name search — if multiple players share this first name, offer disambiguation
        if let players = firstNameIndex[ascii], players.count > 1 {
            return players
        }

        return nil
    }

    /// Sort player names by prominence: current players first, then by career games descending.
    /// Returns (sortedNames, dominantIndex?) — dominantIndex is set when one player is clearly
    /// more relevant (e.g., only current player, or vastly more career games).
    static func sortByProminence(_ names: [String]) -> (sorted: [String], dominantIndex: Int?) {
        guard names.count > 1 else { return (names, names.count == 1 ? 0 : nil) }

        let db = DatabaseService()
        let currentYear = Calendar.current.component(.year, from: Date())

        var infos: [(name: String, lastSeason: Int, totalGames: Int)] = []
        for name in names {
            let sanitized = name.replacingOccurrences(of: "'", with: "''")
            var lastSeason = 0
            var totalGames = 0
            // Batting
            if let result = try? db.execute(sql: """
                SELECT COALESCE(MAX(s.season), 0), COALESCE(SUM(s.games), 0)
                FROM season_batting_stats s JOIN players p ON s.player_id = p.player_id
                WHERE p.name = '\(sanitized)'
                """), let row = result.rows.first {
                lastSeason = max(lastSeason, Int(row[0]) ?? 0)
                totalGames += Int(row[1]) ?? 0
            }
            // Pitching
            if let result = try? db.execute(sql: """
                SELECT COALESCE(MAX(sp.season), 0), COALESCE(SUM(sp.games), 0)
                FROM season_pitching_stats sp JOIN players p ON sp.player_id = p.player_id
                WHERE p.name = '\(sanitized)'
                """), let row = result.rows.first {
                lastSeason = max(lastSeason, Int(row[0]) ?? 0)
                totalGames += Int(row[1]) ?? 0
            }
            infos.append((name, lastSeason, totalGames))
        }

        // Sort: current players first, then by total games descending
        let sorted = infos.sorted { a, b in
            let aCurrent = a.lastSeason >= currentYear - 1
            let bCurrent = b.lastSeason >= currentYear - 1
            if aCurrent != bCurrent { return aCurrent }
            return a.totalGames > b.totalGames
        }

        // Determine if one player is clearly dominant
        var dominantIndex: Int? = nil
        let currentPlayers = sorted.filter { $0.lastSeason >= currentYear - 1 }
        if currentPlayers.count == 1 {
            // Only one current player among historical ones → auto-select
            dominantIndex = 0
        } else if currentPlayers.count >= 2 && currentPlayers[0].totalGames >= currentPlayers[1].totalGames * 3 {
            // Among current players, top one has 3x+ more games → auto-select
            // Handles common names (Ramirez, Diaz) where the star is clearly dominant
            dominantIndex = 0
        } else if sorted.count >= 2 && sorted[0].totalGames >= sorted[1].totalGames * 5 {
            // Top player has 5x+ more games than the runner-up → auto-select
            dominantIndex = 0
        }

        return (sorted.map(\.name), dominantIndex)
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
        // If text already contains statchat:// links, skip to avoid corrupting existing markdown
        if text.contains("statchat://") { return text }
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
