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
