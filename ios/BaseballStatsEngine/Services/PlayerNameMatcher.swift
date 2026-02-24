import Foundation

enum PlayerNameMatcher {
    nonisolated(unsafe) private(set) static var sortedNames: [String] = []

    static func load() {
        let db = DatabaseService()
        guard let result = try? db.execute(sql: "SELECT DISTINCT name FROM players") else { return }
        let names = result.rows.compactMap { $0.first }
        // Sort longest first so "Bobby Witt Jr." matches before "Bobby Witt"
        sortedNames = names.sorted { $0.count > $1.count }
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
