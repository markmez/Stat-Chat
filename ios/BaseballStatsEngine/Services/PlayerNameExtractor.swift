import Foundation

enum PlayerNameExtractor {
    /// Given a row label from a stat grid, returns the clean player name
    /// if it's a real player, or nil if it's a year/career/date range.
    static func extract(_ label: String) -> String? {
        var name = label.trimmingCharacters(in: .whitespaces)
        guard !name.isEmpty else { return nil }

        // Skip 4-digit years (1870–2099)
        if name.count == 4, let num = Int(name), (1870...2099).contains(num) {
            return nil
        }

        // Skip "Career"
        if name.caseInsensitiveCompare("Career") == .orderedSame { return nil }

        // Skip date ranges containing en-dash or em-dash
        if name.contains("\u{2013}") || name.contains("\u{2014}") { return nil }

        // Skip split labels like "vs_LHP", "vs_RHP"
        if name.hasPrefix("vs_") || name.hasPrefix("vs ") { return nil }

        // Skip stat category labels (count splits, situational splits, etc.)
        let skipLabels: Set<String> = [
            "two strikes", "ahead in count", "behind in count", "even count",
            "full count", "first pitch", "home", "away", "day", "night",
            "vs lhp", "vs rhp", "vs left", "vs right", "risp", "bases empty",
            "scoring position", "hot", "cold", "average",
            "0-0", "0-1", "0-2", "1-0", "1-1", "1-2", "2-0", "2-1", "2-2", "3-0", "3-1", "3-2",
        ]
        if skipLabels.contains(name.lowercased()) { return nil }

        // Skip if it looks like a split label (all words are common English, not a name)
        let splitWords: Set<String> = [
            "two", "three", "strikes", "balls", "count", "ahead", "behind", "even",
            "full", "first", "pitch", "scoring", "position", "runners", "bases",
            "empty", "loaded", "with", "in", "on", "no", "outs",
        ]
        let words = name.lowercased().split(separator: " ").map(String.init)
        if words.count >= 2 && words.allSatisfy({ splitWords.contains($0) }) { return nil }

        // Strip leaderboard prefix: "#1 ", "#12 ", etc.
        if name.hasPrefix("#") {
            let rest = name.dropFirst()
            if let spaceIdx = rest.firstIndex(of: " ") {
                let digits = rest[rest.startIndex..<spaceIdx]
                if digits.allSatisfy(\.isNumber) && !digits.isEmpty {
                    name = String(rest[spaceIdx...]).trimmingCharacters(in: .whitespaces)
                }
            }
        }

        // Strip team suffix: "Aaron Judge (NYY)" → "Aaron Judge"
        if let parenIdx = name.firstIndex(of: "("),
           name.hasSuffix(")") {
            name = String(name[name.startIndex..<parenIdx])
                .trimmingCharacters(in: .whitespaces)
        }

        // Final validation: must have at least one letter
        guard name.contains(where: \.isLetter) else { return nil }

        return name.isEmpty ? nil : name
    }
}
