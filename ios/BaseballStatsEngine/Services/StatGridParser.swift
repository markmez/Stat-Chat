import Foundation

struct GameLogEntry: Sendable {
    let date: String      // "2026-04-08"
    let line: String      // "2-4, 1 HR, 4 RBI, 1 R, 0 BB, 1 SO, 0 SB"
}

enum StatGridParser {

    struct StatGrid: Sendable {
        let headers: [String]
        let rows: [Row]
        let formMetadata: FormMetadata?
        let footer: [String]

        struct Row: Sendable {
            let label: String
            let values: [String]
            var note: String?
            var drilldownQuery: String?  // If set, the first value is tappable and triggers this query
        }

        struct FormMetadata: Sendable {
            let playerName: String
            let season: Int
            let autoDetectedGameNumber: Int
            let totalGames: Int
            let teamGames: Int
        }

        init(headers: [String], rows: [Row], formMetadata: FormMetadata? = nil, footer: [String] = []) {
            self.headers = headers
            self.rows = rows
            self.formMetadata = formMetadata
            self.footer = footer
        }
    }

    enum Segment {
        case text(String)
        case statGrid(StatGrid)
        case leaderboard(StatGrid)
        case tip(String)
        case aiDisclaimer(String)
        case context(String)
        case gameLogs([GameLogEntry])
        case querySuggestion(String)
        case seeAlso([String])
        case didYouMean(query: String)
        case subtitle(String)
        case partialGrid(String)
    }

    private static let tagTypes = ["STATGRID", "LEADERBOARD", "GAMELOGS", "TIP", "AIDISCLAIMER", "CONTEXT", "SUGGEST", "SEEALSO", "DIDYOUMEAN", "SUBTITLE"]

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
                case "CONTEXT":
                    let trimmed = blockContent.trimmingCharacters(in: .whitespacesAndNewlines)
                    if !trimmed.isEmpty { segments.append(.context(trimmed)) }
                case "GAMELOGS":
                    let lines = blockContent.components(separatedBy: "\n")
                        .map { $0.trimmingCharacters(in: .whitespaces) }
                        .filter { $0.hasPrefix("GAME ") }
                    var entries: [GameLogEntry] = []
                    for line in lines {
                        let content = String(line.dropFirst("GAME ".count))
                        let parts = content.split(separator: "|", maxSplits: 1)
                        if parts.count == 2 {
                            entries.append(GameLogEntry(
                                date: String(parts[0]).trimmingCharacters(in: .whitespaces),
                                line: String(parts[1]).trimmingCharacters(in: .whitespaces)
                            ))
                        }
                    }
                    if !entries.isEmpty { segments.append(.gameLogs(entries)) }
                case "AIDISCLAIMER":
                    let trimmed = blockContent.trimmingCharacters(in: .whitespacesAndNewlines)
                    if !trimmed.isEmpty { segments.append(.aiDisclaimer(trimmed)) }
                case "SUGGEST":
                    let trimmed = blockContent.trimmingCharacters(in: .whitespacesAndNewlines)
                    if !trimmed.isEmpty { segments.append(.querySuggestion(trimmed)) }
                case "SEEALSO":
                    let names = blockContent.split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces) }
                    if !names.isEmpty { segments.append(.seeAlso(names)) }
                case "DIDYOUMEAN":
                    let trimmed = blockContent.trimmingCharacters(in: .whitespacesAndNewlines)
                    if !trimmed.isEmpty { segments.append(.didYouMean(query: trimmed)) }
                case "SUBTITLE":
                    let trimmed = blockContent.trimmingCharacters(in: .whitespacesAndNewlines)
                    if !trimmed.isEmpty { segments.append(.subtitle(trimmed)) }
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

        // Post-process: detect bare HEADER:/ROW: patterns in text segments
        // (Sonnet sometimes outputs grid format without [STATGRID] wrapper)
        if !isStreaming {
            segments = segments.flatMap { segment -> [Segment] in
                guard case .text(let text) = segment else { return [segment] }
                return splitBareGrids(text)
            }
        }

        return segments
    }

    /// Split a text segment into text + grid segments if it contains bare HEADER:/ROW: lines.
    private static func splitBareGrids(_ text: String) -> [Segment] {
        let lines = text.components(separatedBy: .newlines)

        // Check if there's a HEADER: line followed by ROW: lines
        guard let headerIdx = lines.firstIndex(where: { $0.trimmingCharacters(in: .whitespaces).hasPrefix("HEADER:") }) else {
            return [.text(text)]
        }

        // Check there are ROW: lines after it
        let afterHeader = lines[(headerIdx + 1)...]
        let hasRows = afterHeader.contains(where: { $0.trimmingCharacters(in: .whitespaces).hasPrefix("ROW") })
        guard hasRows else { return [.text(text)] }

        // Split: text before HEADER, grid content, text after last ROW
        var segments: [Segment] = []

        // Text before the grid
        let beforeLines = lines[..<headerIdx]
        let beforeText = beforeLines.joined(separator: "\n").trimmingCharacters(in: .whitespacesAndNewlines)
        if !beforeText.isEmpty {
            segments.append(.text(beforeText))
        }

        // Find the last ROW line
        var lastRowIdx = headerIdx
        for (i, line) in lines.enumerated() where i > headerIdx {
            if line.trimmingCharacters(in: .whitespaces).hasPrefix("ROW") {
                lastRowIdx = i
            }
        }

        // Parse the grid content
        let gridLines = lines[headerIdx...lastRowIdx]
        let gridContent = gridLines.joined(separator: "\n")
        if let grid = parseGrid(gridContent) {
            // Determine if it's a leaderboard (has ranked ROW N. patterns)
            let isLeaderboard = gridLines.contains(where: {
                $0.trimmingCharacters(in: .whitespaces).hasPrefix("ROW ") &&
                $0.contains(".")
            })
            segments.append(isLeaderboard ? .leaderboard(grid) : .statGrid(grid))
        } else {
            segments.append(.text(gridContent))
        }

        // Text after the grid
        if lastRowIdx + 1 < lines.count {
            let afterText = lines[(lastRowIdx + 1)...].joined(separator: "\n").trimmingCharacters(in: .whitespacesAndNewlines)
            if !afterText.isEmpty {
                segments.append(.text(afterText))
            }
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
        var footerLines: [String] = []

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
                // Extract [DRILLDOWN]query[/DRILLDOWN] if present
                var drilldown: String?
                var cleanedValues = valuesStr
                if let drillStart = valuesStr.range(of: "[DRILLDOWN]"),
                   let drillEnd = valuesStr.range(of: "[/DRILLDOWN]") {
                    drilldown = String(valuesStr[drillStart.upperBound..<drillEnd.lowerBound])
                    cleanedValues = valuesStr.replacingCharacters(
                        in: drillStart.lowerBound..<drillEnd.upperBound, with: "")
                }
                let parts = cleanedValues.components(separatedBy: ",").map { $0.trimmingCharacters(in: .whitespaces) }
                guard !parts.isEmpty else { continue }
                var row = StatGrid.Row(label: label, values: parts)
                row.drilldownQuery = drilldown
                rows.append(row)
            } else if line.hasPrefix("NOTE:") {
                // Attach note to the most recently parsed row
                if !rows.isEmpty {
                    let noteText = String(line.dropFirst("NOTE:".count)).trimmingCharacters(in: .whitespaces)
                    rows[rows.count - 1].note = noteText
                }
            } else if line.hasPrefix("FOOTER:") {
                let text = String(line.dropFirst("FOOTER:".count)).trimmingCharacters(in: .whitespaces)
                if !text.isEmpty { footerLines.append(text) }
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

        return StatGrid(headers: finalHeaders, rows: rows, formMetadata: formMetadata, footer: footerLines)
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
