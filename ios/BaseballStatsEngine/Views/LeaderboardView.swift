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
    private let nameWidth: CGFloat = 148
    private let rankWidth: CGFloat = 30
    private let rankNameGap: CGFloat = 6
    private let nameStatGap: CGFloat = 8
    private let statColumnWidth: CGFloat = 56
    private let valueGap: CGFloat = 6

    /// Divider spans rank through the last value column
    private var dividerWidth: CGFloat {
        var w = rankWidth + rankNameGap + nameWidth + nameStatGap + statColumnWidth
        for _ in 1..<grid.headers.count {
            w += valueGap + statColumnWidth
        }
        return w
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            // Column headers
            HStack(spacing: 0) {
                Spacer().frame(width: rankWidth + rankNameGap + nameWidth + nameStatGap)
                ForEach(Array(grid.headers.enumerated()), id: \.offset) { idx, header in
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
                            .frame(minWidth: statColumnWidth, alignment: .leading)
                            .padding(.leading, idx > 0 ? valueGap : 0)
                        }
                        .buttonStyle(.plain)
                    } else {
                        Text(header)
                            .font(.system(.caption2, design: .monospaced, weight: .semibold))
                            .foregroundStyle(.secondary)
                            .frame(minWidth: statColumnWidth, alignment: .leading)
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
                    // Rank
                    Text(displayRank)
                        .font(.system(.callout, design: .monospaced, weight: .medium))
                        .foregroundStyle(.secondary)
                        .frame(width: rankWidth, alignment: .trailing)

                    // Player or team name (tappable)
                    if let teamCode = PlayerCardService.teamCodeFromFullName(playerName),
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
                        .padding(.leading, rankNameGap)
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
                        .padding(.leading, rankNameGap)
                    } else {
                        Text(playerName)
                            .font(.system(.callout, design: .rounded, weight: .medium))
                            .foregroundStyle(.primary)
                            .lineLimit(1)
                            .frame(width: nameWidth, alignment: .leading)
                            .padding(.leading, rankNameGap)
                    }

                    // Stat values — all primary color
                    ForEach(Array(row.values.enumerated()), id: \.offset) { idx, val in
                        Text(val)
                            .font(.system(.callout, design: .monospaced, weight: .medium))
                            .foregroundStyle(.primary)
                            .lineLimit(1)
                            .fixedSize(horizontal: true, vertical: false)
                            .frame(minWidth: statColumnWidth, alignment: .leading)
                            .padding(.leading, idx == 0 ? nameStatGap : valueGap)
                    }

                    Spacer()
                }
                .padding(.horizontal, 12)
                .padding(.vertical, 4)
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
            if !initialized && isSortable {
                sortColumn = 0
                sortAscending = false
                initialized = true
            }
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
