import SwiftUI

struct LeaderboardView: View {
    let grid: StatGridParser.StatGrid
    var onPlayerTap: ((String) -> Void)? = nil
    var onTeamTap: ((String) -> Void)? = nil

    @State private var visibleCount = 25
    @State private var sortColumn: Int?
    @State private var sortAscending = false
    @State private var initialized = false

    private let deepBlue = Color(red: 0.1, green: 0.25, blue: 0.7)

    /// Whether this leaderboard has multiple stat columns (sortable)
    private var isSortable: Bool { grid.headers.count >= 2 }

    /// Rows sorted by the active column, or original order if no sort active
    private var sortedRows: [StatGridParser.StatGrid.Row] {
        guard let col = sortColumn else { return grid.rows }
        return grid.rows.sorted { a, b in
            let aVal = col < a.values.count ? numericValue(a.values[col]) : -.infinity
            let bVal = col < b.values.count ? numericValue(b.values[col]) : -.infinity
            return sortAscending ? aVal < bVal : aVal > bVal
        }
    }

    private var visibleRows: ArraySlice<StatGridParser.StatGrid.Row> {
        sortedRows.prefix(visibleCount)
    }

    private var hasMore: Bool {
        sortedRows.count > visibleCount
    }

    /// Fixed name column width — 98% of player names are ≤17 chars.
    /// At .callout rounded ~8pt/char, 17 chars ≈ 136pt. Add a little breathing room.
    /// Compact mode (pitch mix etc.) uses shorter labels, so narrower name + no rank.
    private var isCompact: Bool {
        // Detect pitch mix tables by label pattern: "Sinker (61%)" etc.
        grid.rows.first.map { $0.label.contains("%") } ?? false
    }
    private var nameWidth: CGFloat { isCompact ? 100 : 148 }
    private let rankWidth: CGFloat = 30
    private let rankNameGap: CGFloat = 6
    private let nameStatGap: CGFloat = 8
    private let defaultStatColumnWidth: CGFloat = 56
    private let valueGap: CGFloat = 6

    /// Compute per-column widths based on content.
    /// Date columns and team abbreviations get appropriate widths.
    private var columnWidths: [CGFloat] {
        grid.headers.enumerated().map { idx, header in
            let headerLower = header.lowercased()
            // Date columns need more space ("Sept 27" in monospace)
            if headerLower == "date" {
                return CGFloat(62)
            }
            // Opponent / team columns
            if headerLower == "opp" || headerLower == "team" {
                return CGFloat(36)
            }
            // Year columns
            if headerLower == "year" {
                return CGFloat(42)
            }
            // Age columns
            if headerLower == "age" {
                return CGFloat(32)
            }
            // Default stat width
            return defaultStatColumnWidth
        }
    }

    /// Divider spans rank through the last value column
    private var dividerWidth: CGFloat {
        let widths = columnWidths
        let rankSpace: CGFloat = isCompact ? 0 : rankWidth + rankNameGap
        guard !widths.isEmpty else { return rankSpace + nameWidth }
        var w = rankSpace + nameWidth + nameStatGap + widths[0]
        for i in 1..<widths.count {
            w += valueGap + widths[i]
        }
        return w
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            // Column headers
            HStack(spacing: 0) {
                Spacer().frame(width: (isCompact ? 0 : rankWidth + rankNameGap) + nameWidth + nameStatGap)
                let widths = columnWidths
                ForEach(Array(grid.headers.enumerated()), id: \.offset) { idx, header in
                    let colWidth = idx < widths.count ? widths[idx] : defaultStatColumnWidth
                    if isSortable {
                        Button {
                            withAnimation(.easeInOut(duration: 0.15)) {
                                if sortColumn == idx {
                                    sortAscending.toggle()
                                } else {
                                    sortColumn = idx
                                    sortAscending = false
                                }
                            }
                        } label: {
                            HStack(spacing: 2) {
                                Text(header)
                                if sortColumn == idx {
                                    Image(systemName: sortAscending ? "chevron.up" : "chevron.down")
                                        .font(.system(size: 8, weight: .bold))
                                }
                            }
                            .font(.system(.caption2, design: .monospaced, weight: .semibold))
                            .foregroundStyle(sortColumn == idx ? deepBlue : deepBlue.opacity(0.7))
                            .frame(minWidth: colWidth, alignment: .leading)
                            .padding(.leading, idx > 0 ? valueGap : 0)
                        }
                        .buttonStyle(.plain)
                    } else {
                        Text(header)
                            .font(.system(.caption2, design: .monospaced, weight: .semibold))
                            .foregroundStyle(.secondary)
                            .frame(minWidth: colWidth, alignment: .leading)
                            .padding(.leading, idx > 0 ? valueGap : 0)
                    }
                }
            }
            .padding(.horizontal, 12)
            .padding(.top, 10)
            .padding(.bottom, 4)

            // Rows
            ForEach(Array(visibleRows.enumerated()), id: \.offset) { index, row in
                if index > 0 {
                    Divider()
                        .frame(width: dividerWidth)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(.leading, 12)
                }

                let (_, playerName) = parseLabel(row.label)
                let displayRank = sortColumn != nil ? "\(index + 1)." : parseLabel(row.label).rank

                HStack(spacing: 0) {
                    // Rank (hidden in compact mode)
                    if !isCompact {
                        Text(displayRank)
                            .font(.system(.callout, design: .monospaced, weight: .medium))
                            .foregroundStyle(.secondary)
                            .frame(width: rankWidth, alignment: .trailing)
                    }

                    // Player or team name (tappable — skip in compact/pitch-mix mode)
                    if isCompact {
                        Text(playerName)
                            .font(.system(.callout, design: .rounded, weight: .medium))
                            .foregroundStyle(.primary)
                            .lineLimit(1)
                            .frame(width: nameWidth, alignment: .leading)
                    } else if let teamCode = PlayerCardService.teamCodeFromFullName(playerName),
                       let tap = onTeamTap {
                        Button {
                            tap(teamCode)
                        } label: {
                            Text(playerName)
                                .font(.system(.callout, design: .rounded, weight: .medium))
                                .foregroundStyle(deepBlue)
                                .lineLimit(1)
                        }
                        .buttonStyle(.plain)
                        .frame(width: nameWidth, alignment: .leading)
                        .padding(.leading, isCompact ? 0 : rankNameGap)
                    } else if let extractedName = PlayerNameExtractor.extract(playerName),
                       let tap = onPlayerTap {
                        Button {
                            tap(extractedName)
                        } label: {
                            Text(playerName)
                                .font(.system(.callout, design: .rounded, weight: .medium))
                                .foregroundStyle(deepBlue)
                                .lineLimit(1)
                        }
                        .buttonStyle(.plain)
                        .frame(width: nameWidth, alignment: .leading)
                        .padding(.leading, isCompact ? 0 : rankNameGap)
                    } else {
                        Text(playerName)
                            .font(.system(.callout, design: .rounded, weight: .medium))
                            .foregroundStyle(.primary)
                            .lineLimit(1)
                            .frame(width: nameWidth, alignment: .leading)
                            .padding(.leading, isCompact ? 0 : rankNameGap)
                    }

                    // Stat values — all primary color
                    let rowWidths = columnWidths
                    ForEach(Array(row.values.enumerated()), id: \.offset) { idx, val in
                        let colWidth = idx < rowWidths.count ? rowWidths[idx] : defaultStatColumnWidth
                        Text(val)
                            .font(.system(.callout, design: .monospaced, weight: .medium))
                            .foregroundStyle(.primary)
                            .lineLimit(1)
                            .frame(width: colWidth, alignment: .leading)
                            .padding(.leading, idx == 0 ? nameStatGap : valueGap)
                    }

                    Spacer()
                }
                .padding(.horizontal, 12)
                .padding(.vertical, 4)
            }

            // Footer (e.g. pitch-mix-weighted summary)
            if !grid.footer.isEmpty {
                Divider()
                    .frame(width: dividerWidth)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.leading, 12)
                VStack(alignment: .leading, spacing: 2) {
                    ForEach(grid.footer, id: \.self) { line in
                        Text(line)
                            .font(.system(.caption, design: .rounded))
                            .foregroundStyle(.secondary.opacity(0.7))
                    }
                }
                .padding(.horizontal, 12)
                .padding(.vertical, 8)
            }

            // Show more button
            if hasMore {
                let totalRemaining = sortedRows.count - visibleCount
                let nextBatch = min(25, totalRemaining)
                Button {
                    withAnimation(.easeInOut(duration: 0.2)) {
                        visibleCount = min(visibleCount + 25, sortedRows.count)
                    }
                } label: {
                    HStack(spacing: 4) {
                        Text("Show \(nextBatch) more of \(totalRemaining) remaining")
                            .font(.system(.caption, design: .rounded, weight: .medium))
                        Image(systemName: "chevron.down")
                            .font(.system(size: 9, weight: .semibold))
                    }
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 10)
                }
                .buttonStyle(.plain)
            } else {
                Spacer().frame(height: 10)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: 12)
                .fill(Color(uiColor: .secondarySystemBackground))
                .shadow(color: Color(red: 0.1, green: 0.25, blue: 0.7).opacity(0.10), radius: 10, y: 3)
                .shadow(color: .black.opacity(0.03), radius: 2, y: 1)
        )
        .onAppear {
            // Don't auto-sort — preserve the backend's intentional ordering.
            // User can tap a column header to sort if desired.
            initialized = true
        }
    }

    /// Parse "1. Aaron Judge" into ("1.", "Aaron Judge")
    private func parseLabel(_ label: String) -> (rank: String, name: String) {
        if let dotIdx = label.firstIndex(of: ".") {
            let rank = String(label[...dotIdx])
            let name = String(label[label.index(after: dotIdx)...]).trimmingCharacters(in: .whitespaces)
            return (rank, name)
        }
        return ("", label)
    }

    /// Parse a display value into a sortable number.
    /// Handles: "58", ".322", "1.96", "2024", "--", etc.
    private func numericValue(_ str: String) -> Double {
        let cleaned = str.trimmingCharacters(in: .whitespaces)
        if cleaned == "--" || cleaned.isEmpty { return -.infinity }
        // Rate stats displayed without leading zero: ".322" → 0.322
        if cleaned.hasPrefix("."), let val = Double("0" + cleaned) { return val }
        if let val = Double(cleaned) { return val }
        return -.infinity
    }
}
