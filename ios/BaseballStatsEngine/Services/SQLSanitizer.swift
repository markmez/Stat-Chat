import Foundation

enum SQLSanitizer {
    /// Strip markdown code fences and Python-style # comments from Claude's SQL output.
    static func sanitize(_ sql: String) -> String {
        var result = sql.trimmingCharacters(in: .whitespacesAndNewlines)

        // Strip markdown code fences: ```sql ... ``` or ``` ... ```
        if let range = result.range(of: #"^```(?:sql)?\s*"#, options: .regularExpression) {
            result.removeSubrange(range)
        }
        if let range = result.range(of: #"\s*```$"#, options: .regularExpression) {
            result.removeSubrange(range)
        }

        // Strip Python-style # comments (Claude sometimes uses these instead of SQL -- comments)
        result = result.replacingOccurrences(of: #"#[^\n]*"#, with: "", options: .regularExpression)

        return result.trimmingCharacters(in: .whitespacesAndNewlines)
    }
}
