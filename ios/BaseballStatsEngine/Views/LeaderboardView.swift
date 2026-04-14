import SwiftUI

struct LeaderboardView: View {
    let grid: StatGridParser.StatGrid
    var onPlayerTap: ((String) -> Void)? = nil
    var onTeamTap: ((String) -> Void)? = nil
    var onDrilldownTap: ((String) -> Void)? = nil

    @State private var visibleCount = 25
    @State private var sortColumn: Int?
    @State private var sortAscending = false
    @State private var initialized = false

    private let deepBlue = Color(red: 0.1, green: 0.25, blue: 0.7)

    /// Whether this leaderboard has stat columns (sortable)
    private var isSortable: Bool { grid.headers.count >= 1 }

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
    /// Whether any rows have a numbered rank (e.g. "1. Aaron Judge")
    private var hasRanks: Bool {
        grid.rows.contains { $0.label.contains(".") && $0.label.first?.isNumber == true }
    }
    /// Compute name column width from longest label using actual font measurement
    private var nameWidth: CGFloat {
        let font = UIFont.systemFont(ofSize: UIFont.preferredFont(forTextStyle: .callout).pointSize,
                                      weight: .medium)
        let longest = grid.rows.map(\.label).max(by: { $0.count < $1.count }) ?? ""
        let measured = (longest as NSString).size(withAttributes: [.font: font])
        // For player names, cap at 148. For short labels (years, pitch types), use measured width.
        return min(148, max(40, ceil(measured.width) + 12))
    }
    private let rankWidth: CGFloat = 30
    private let rankNameGap: CGFloat = 6
    private let nameStatGap: CGFloat = 8
    private let defaultStatColumnWidth: CGFloat = 56
    private let valueGap: CGFloat = 6

    /// Compute per-column widths based on actual content width.
    /// Measures the widest value in each column to avoid wasted space.
    private var columnWidths: [CGFloat] {
        let font = UIFont.monospacedSystemFont(ofSize: UIFont.preferredFont(forTextStyle: .callout).pointSize, weight: .medium)
        let headerFont = UIFont.monospacedSystemFont(ofSize: UIFont.preferredFont(forTextStyle: .caption2).pointSize, weight: .semibold)

        return grid.headers.enumerated().map { idx, header in
            // Measure widest VALUE in this column (not header — headers truncate if needed)
            var valueCandidates: [String] = []
            for row in grid.rows {
                if idx < row.values.count {
                    valueCandidates.append(row.values[idx])
                }
            }
            let widestValue = valueCandidates.max(by: { $0.count < $1.count }) ?? ""
            let valueWidth = (widestValue as NSString).size(withAttributes: [.font: font]).width

            // Header measured at its own (smaller) font
            let headerWidth = (header as NSString).size(withAttributes: [.font: headerFont]).width

            // Use the wider of header vs values, but cap header influence at 80pt
            let effectiveHeader = min(80, headerWidth)
            return max(32, ceil(max(effectiveHeader, valueWidth)) + 8)
        }
    }

    /// Divider spans rank through the last value column
    private var dividerWidth: CGFloat {
        let widths = columnWidths
        let rankSpace: CGFloat = hasRanks ? rankWidth + rankNameGap : 0
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
                Spacer().frame(width: (hasRanks ? rankWidth + rankNameGap : 0) + nameWidth + nameStatGap)
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
                                    .lineLimit(1)
                                    .truncationMode(.tail)
                                if sortColumn == idx {
                                    Image(systemName: sortAscending ? "chevron.up" : "chevron.down")
                                        .font(.system(size: 8, weight: .bold))
                                }
                            }
                            .font(.system(.caption2, design: .monospaced, weight: .semibold))
                            .foregroundStyle(sortColumn == idx ? deepBlue : deepBlue.opacity(0.7))
                            .frame(width: colWidth, alignment: .leading)
                            .padding(.leading, idx > 0 ? valueGap : 0)
                        }
                        .buttonStyle(.plain)
                    } else {
                        Text(header)
                            .lineLimit(1)
                            .truncationMode(.tail)
                            .font(.system(.caption2, design: .monospaced, weight: .semibold))
                            .foregroundStyle(.secondary)
                            .frame(width: colWidth, alignment: .leading)
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
                    // Rank (only shown when rows have numbered ranks)
                    if hasRanks {
                        Text(displayRank)
                            .font(.system(.callout, design: .monospaced, weight: .medium))
                            .foregroundStyle(.secondary)
                            .frame(width: rankWidth, alignment: .trailing)
                    }

                    // Player or team name (tappable — skip in compact/pitch-mix mode and for year labels)
                    let isYearLabel = playerName.count == 4 && Int(playerName) != nil
                    if isCompact || isYearLabel {
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
                        .padding(.leading, hasRanks ? rankNameGap : 0)
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
                        .padding(.leading, hasRanks ? rankNameGap : 0)
                    } else {
                        Text(playerName)
                            .font(.system(.callout, design: .rounded, weight: .medium))
                            .foregroundStyle(.primary)
                            .lineLimit(1)
                            .frame(width: nameWidth, alignment: .leading)
                            .padding(.leading, hasRanks ? rankNameGap : 0)
                    }

                    // Stat values — primary color, or blue+tappable if drilldown
                    let rowWidths = columnWidths
                    ForEach(Array(row.values.enumerated()), id: \.offset) { idx, val in
                        let colWidth = idx < rowWidths.count ? rowWidths[idx] : defaultStatColumnWidth
                        let isDrilldown = idx == 0 && row.drilldownQuery != nil
                        if isDrilldown, let query = row.drilldownQuery {
                            Button {
                                onDrilldownTap?(query)
                            } label: {
                                Text(val)
                                    .font(.system(.callout, design: .monospaced, weight: .medium))
                                    .foregroundStyle(deepBlue)
                                    .lineLimit(1)
                                    .frame(width: colWidth, alignment: .leading)
                                    .padding(.leading, nameStatGap)
                            }
                            .buttonStyle(.plain)
                        } else {
                            Text(val)
                                .font(.system(.callout, design: .monospaced, weight: .medium))
                                .foregroundStyle(.primary)
                                .lineLimit(1)
                                .frame(width: colWidth, alignment: .leading)
                                .padding(.leading, idx == 0 ? nameStatGap : valueGap)
                        }
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
            // Show chevron on first column — data arrives pre-sorted descending by it.
            // This makes the sort affordance visible without changing the order.
            if !initialized && isSortable {
                sortColumn = 0
                sortAscending = false
            }
            initialized = true
        }
    }

    /// Parse "1. Aaron Judge" into ("1.", "Aaron Judge")
    private func parseLabel(_ label: String) -> (rank: String, name: String) {
        if let dotIdx = label.firstIndex(of: ".") {
            let before = String(label[..<dotIdx]).trimmingCharacters(in: .whitespaces)
            // Only treat as rank if the part before the dot is a number
            if Int(before) != nil {
                let rank = String(label[...dotIdx])
                let name = String(label[label.index(after: dotIdx)...]).trimmingCharacters(in: .whitespaces)
                return (rank, name)
            }
        }
        return ("", label)
    }

    /// Parse a display value into a sortable number.
    /// Handles: "58", ".322", "1.96", "2024", "2023–2025", "--", etc.
    private func numericValue(_ str: String) -> Double {
        let cleaned = str.trimmingCharacters(in: .whitespaces)
        if cleaned == "--" || cleaned.isEmpty { return -.infinity }
        // Year ranges: "2023–2025" or "2023-2025" → sort by first year
        if cleaned.contains("–") || (cleaned.count >= 9 && cleaned.contains("-")) {
            let parts = cleaned.components(separatedBy: CharacterSet(charactersIn: "–-"))
            if let first = parts.first, let val = Double(first.trimmingCharacters(in: .whitespaces)) {
                return val
            }
        }
        // Date labels: "4/8", "10/15" → sort as month.day
        if cleaned.contains("/") && cleaned.count <= 5 {
            let parts = cleaned.split(separator: "/")
            if parts.count == 2, let m = Double(parts[0]), let d = Double(parts[1]) {
                return m * 100 + d  // 4/8 → 408, 10/15 → 1015
            }
        }
        // Rate stats displayed without leading zero: ".322" → 0.322
        if cleaned.hasPrefix("."), let val = Double("0" + cleaned) { return val }
        if let val = Double(cleaned) { return val }
        return -.infinity
    }
}
