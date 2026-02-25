import Foundation

enum PlayerNameMatcher {
    nonisolated(unsafe) private(set) static var sortedNames: [String] = []
    nonisolated(unsafe) private(set) static var lastNameIndex: [String: [String]] = [:]

    static func load() {
        let db = DatabaseService()
        guard let result = try? db.execute(sql: "SELECT DISTINCT name FROM players", maxRows: 0) else { return }
        let names = result.rows.compactMap { $0.first }
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

        // Try splitting on delimiters (longer first to avoid partial matches)
        let delimiters = [" compared to ", " versus ", " vs. ", " vs ", " and ", " to ", " with "]
        for delimiter in delimiters {
            guard let range = cleaned.range(of: delimiter) else { continue }
            let part1 = String(cleaned[cleaned.startIndex..<range.lowerBound])
                .trimmingCharacters(in: .whitespaces)
            let part2 = String(cleaned[range.upperBound...])
                .trimmingCharacters(in: .whitespaces)

            guard !part1.isEmpty, !part2.isEmpty,
                  let name1 = matchPlayer(part1),
                  let name2 = matchPlayer(part2),
                  name1 != name2 else { continue }
            return (name1, name2)
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
}
