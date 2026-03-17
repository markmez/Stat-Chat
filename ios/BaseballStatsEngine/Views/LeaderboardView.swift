import SwiftUI

struct LeaderboardView: View {
    let grid: StatGridParser.StatGrid
    var onPlayerTap: ((String) -> Void)? = nil
    var onTeamTap: ((String) -> Void)? = nil

    @State private var visibleCount = 25

    private let deepBlue = Color(red: 0.1, green: 0.25, blue: 0.7)

    private var visibleRows: ArraySlice<StatGridParser.StatGrid.Row> {
        grid.rows.prefix(visibleCount)
    }

    private var hasMore: Bool {
        grid.rows.count > visibleCount
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
            // Column headers — stat abbreviation(s) aligned with values
            HStack(spacing: 0) {
                Spacer().frame(width: rankWidth + rankNameGap + nameWidth + nameStatGap)
                ForEach(Array(grid.headers.enumerated()), id: \.offset) { idx, header in
                    Text(header)
                        .font(.system(.caption2, design: .monospaced, weight: .semibold))
                        .foregroundStyle(.secondary)
                        .frame(minWidth: statColumnWidth, alignment: .leading)
                        .padding(.leading, idx > 0 ? valueGap : 0)
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

                let (rank, playerName) = parseLabel(row.label)

                HStack(spacing: 0) {
                    // Rank
                    Text(rank)
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

                    // Stat values
                    ForEach(Array(row.values.enumerated()), id: \.offset) { idx, val in
                        Text(val)
                            .font(.system(.callout, design: .monospaced, weight: .medium))
                            .foregroundStyle(idx == 0 ? .primary : .secondary)
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
                let remaining = grid.rows.count - visibleCount
                Button {
                    withAnimation(.easeInOut(duration: 0.2)) {
                        visibleCount = min(visibleCount + 25, grid.rows.count)
                    }
                } label: {
                    HStack(spacing: 4) {
                        Text("Show \(remaining) more")
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
                .fill(.white)
                .shadow(color: Color(red: 0.1, green: 0.25, blue: 0.7).opacity(0.10), radius: 10, y: 3)
                .shadow(color: .black.opacity(0.03), radius: 2, y: 1)
        )
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
}
