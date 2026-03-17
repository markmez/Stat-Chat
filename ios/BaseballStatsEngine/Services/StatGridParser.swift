import Foundation

enum StatGridParser {

    struct StatGrid: Sendable {
        let headers: [String]
        let rows: [Row]
        let formMetadata: FormMetadata?

        struct Row: Sendable {
            let label: String
            let values: [String]
            var note: String?
        }

        struct FormMetadata: Sendable {
            let playerName: String
            let season: Int
            let autoDetectedGameNumber: Int
            let totalGames: Int
            let teamGames: Int
        }

        init(headers: [String], rows: [Row], formMetadata: FormMetadata? = nil) {
            self.headers = headers
            self.rows = rows
            self.formMetadata = formMetadata
        }
    }

    enum Segment {
        case text(String)
        case statGrid(StatGrid)
        case leaderboard(StatGrid)
        case tip(String)
        case querySuggestion(String)
        case seeAlso([String])
        case didYouMean(query: String)
        case partialGrid(String)
    }

    private static let tagTypes = ["STATGRID", "LEADERBOARD", "TIP", "SUGGEST", "SEEALSO", "DIDYOUMEAN"]

    static func parse(_ content: String, isStreaming: Bool) -> [Segment] {
        var segments: [Segment] = []
        var remaining = content

        while true {
            // Find the nearest opening tag of any type
            var earliest: (range: Range<String.Index>, tag: String)?
            for tag in tagTypes {
                if let range = remaining.range(of: "[\(tag)]") {
                    if earliest == nil || range.lowerBound < earliest!.range.lowerBound {
                        earliest = (range, tag)
                    }
                }
            }
            guard let (openRange, tag) = earliest else { break }

            // Text before the tag
            let before = String(remaining[remaining.startIndex..<openRange.lowerBound])
            if !before.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                segments.append(.text(before))
            }

            let afterOpen = String(remaining[openRange.upperBound...])
            let closeTag = "[/\(tag)]"

            if let closeRange = afterOpen.range(of: closeTag) {
                let blockContent = String(afterOpen[afterOpen.startIndex..<closeRange.lowerBound])

                switch tag {
                case "TIP":
                    let trimmed = blockContent.trimmingCharacters(in: .whitespacesAndNewlines)
                    if !trimmed.isEmpty { segments.append(.tip(trimmed)) }
                case "SUGGEST":
                    let trimmed = blockContent.trimmingCharacters(in: .whitespacesAndNewlines)
                    if !trimmed.isEmpty { segments.append(.querySuggestion(trimmed)) }
                case "SEEALSO":
                    let names = blockContent.split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces) }
                    if !names.isEmpty { segments.append(.seeAlso(names)) }
                case "DIDYOUMEAN":
                    let trimmed = blockContent.trimmingCharacters(in: .whitespacesAndNewlines)
                    if !trimmed.isEmpty { segments.append(.didYouMean(query: trimmed)) }
                case "LEADERBOARD":
                    if let grid = parseGrid(blockContent) {
                        segments.append(.leaderboard(grid))
                    } else {
                        segments.append(.text(blockContent))
                    }
                default: // STATGRID
                    if let grid = parseGrid(blockContent) {
                        segments.append(.statGrid(grid))
                    } else {
                        segments.append(.text(blockContent))
                    }
                }
                remaining = String(afterOpen[closeRange.upperBound...])
            } else {
                // No closing tag yet
                if isStreaming {
                    segments.append(.partialGrid(afterOpen))
                } else {
                    segments.append(.text(afterOpen))
                }
                remaining = ""
            }
        }

        // Remaining text after last block
        if !remaining.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            segments.append(.text(remaining))
        }

        return segments
    }

    private static func parseGrid(_ content: String) -> StatGrid? {
        let lines = content.components(separatedBy: .newlines)
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }

        var headers: [String] = []
        var rows: [StatGrid.Row] = []
        var formMetadata: StatGrid.FormMetadata?

        for line in lines {
            if line.hasPrefix("HEADER:") {
                let headerContent = String(line.dropFirst("HEADER:".count))
                headers = headerContent.components(separatedBy: ",").map { $0.trimmingCharacters(in: .whitespaces) }
            } else if line.hasPrefix("ROW:") {
                let rowContent = String(line.dropFirst("ROW:".count))
                let parts = rowContent.components(separatedBy: ",").map { $0.trimmingCharacters(in: .whitespaces) }
                guard !parts.isEmpty else { continue }
                // If first value looks like a stat (number), treat entire row as values (no label)
                if looksLikeStat(parts[0]) {
                    rows.append(StatGrid.Row(label: "", values: parts))
                } else {
                    rows.append(StatGrid.Row(label: parts[0], values: Array(parts.dropFirst())))
                }
            } else if line.hasPrefix("ROW "), let colonIdx = line.firstIndex(of: ":") {
                // Labeled row: "ROW Jun 17 – Jun 25: 8, 35, ..."
                let label = String(line[line.index(line.startIndex, offsetBy: 4)..<colonIdx])
                    .trimmingCharacters(in: .whitespaces)
                let valuesStr = String(line[line.index(after: colonIdx)...])
                let parts = valuesStr.components(separatedBy: ",").map { $0.trimmingCharacters(in: .whitespaces) }
                guard !parts.isEmpty else { continue }
                rows.append(StatGrid.Row(label: label, values: parts))
            } else if line.hasPrefix("NOTE:") {
                // Attach note to the most recently parsed row
                if !rows.isEmpty {
                    let noteText = String(line.dropFirst("NOTE:".count)).trimmingCharacters(in: .whitespaces)
                    rows[rows.count - 1].note = noteText
                }
            } else if line.hasPrefix("FORM:") {
                // Parse FORM: Player Name, season, autoDetectedGameNumber, totalGames[, teamGames]
                let formContent = String(line.dropFirst("FORM:".count))
                let parts = formContent.components(separatedBy: ",").map { $0.trimmingCharacters(in: .whitespaces) }
                if parts.count >= 4,
                   let season = Int(parts[1]),
                   let gameNum = Int(parts[2]),
                   let total = Int(parts[3]) {
                    let tGames = parts.count >= 5 ? (Int(parts[4]) ?? total) : total
                    formMetadata = StatGrid.FormMetadata(
                        playerName: parts[0], season: season,
                        autoDetectedGameNumber: gameNum, totalGames: total,
                        teamGames: tGames
                    )
                }
            }
        }

        guard !headers.isEmpty, !rows.isEmpty else { return nil }

        // If any row extracted a label, the first header column was the label's name — strip it
        let hasLabels = rows.contains { !$0.label.isEmpty }
        let finalHeaders = hasLabels && headers.count > rows[0].values.count
            ? Array(headers.dropFirst())
            : headers

        return StatGrid(headers: finalHeaders, rows: rows, formMetadata: formMetadata)
    }

    /// Check if a string looks like a stat value (number, rate stat, or rank)
    private static func looksLikeStat(_ value: String) -> Bool {
        guard let first = value.first else { return false }
        if first.isNumber {
            // A 4-digit year (1870–2099) is a label, not a stat
            if value.count == 4, let num = Int(value), (1870...2099).contains(num) {
                return false
            }
            return true
        }
        return first == "." || first == "-"
    }
}
