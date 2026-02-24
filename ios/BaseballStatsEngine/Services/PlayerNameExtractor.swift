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
