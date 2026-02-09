import Foundation

enum StatGridParser {

    struct StatGrid {
        let headers: [String]
        let rows: [Row]

        struct Row {
            let label: String
            let values: [String]
        }
    }

    enum Segment {
        case text(String)
        case statGrid(StatGrid)
        case partialGrid(String)
    }

    static func parse(_ content: String, isStreaming: Bool) -> [Segment] {
        var segments: [Segment] = []
        var remaining = content

        while let openRange = remaining.range(of: "[STATGRID]") {
            // Text before the tag
            let before = String(remaining[remaining.startIndex..<openRange.lowerBound])
            if !before.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                segments.append(.text(before))
            }

            let afterOpen = String(remaining[openRange.upperBound...])

            if let closeRange = afterOpen.range(of: "[/STATGRID]") {
                // Complete grid block
                let gridContent = String(afterOpen[afterOpen.startIndex..<closeRange.lowerBound])
                if let grid = parseGrid(gridContent) {
                    segments.append(.statGrid(grid))
                } else {
                    segments.append(.text(gridContent))
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

        // Remaining text after last grid
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
            }
        }

        guard !headers.isEmpty, !rows.isEmpty else { return nil }

        // If any row extracted a label, the first header column was the label's name — strip it
        let hasLabels = rows.contains { !$0.label.isEmpty }
        let finalHeaders = hasLabels && headers.count > rows[0].values.count
            ? Array(headers.dropFirst())
            : headers

        return StatGrid(headers: finalHeaders, rows: rows)
    }

    /// Check if a string looks like a stat value (number, rate stat, or rank)
    private static func looksLikeStat(_ value: String) -> Bool {
        guard let first = value.first else { return false }
        return first.isNumber || first == "." || first == "-"
    }
}
