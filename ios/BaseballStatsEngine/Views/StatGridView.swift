import SwiftUI

struct StatGridView: View {
    let grid: StatGridParser.StatGrid

    @State private var selectedStat: String? = nil

    private let deepBlue = Color(red: 0.1, green: 0.25, blue: 0.7)

    /// Max columns per row before splitting into stacked rows
    private let maxPerRow = 7

    /// Uniform column width — same for every column across all rows so they align in a true grid
    private let columnWidth: CGFloat = 50

    /// Split an array into chunks of maxPerRow
    private func chunk<T>(_ array: [T]) -> [[T]] {
        guard array.count > maxPerRow else { return [array] }
        var result: [[T]] = []
        var start = array.startIndex
        while start < array.endIndex {
            let end = min(start + maxPerRow, array.endIndex)
            result.append(Array(array[start..<end]))
            start = end
        }
        return result
    }

    /// Split headers into chunks that fit without scrolling
    private var headerChunks: [[String]] { chunk(grid.headers) }

    /// Split a row's values to match header chunks
    private func valueChunks(for row: StatGridParser.StatGrid.Row) -> [[String]] { chunk(row.values) }

    /// Map a chunk index + column index back to the header abbreviation
    private func headerForColumn(chunkIdx: Int, colIdx: Int) -> String? {
        let globalIdx = chunkIdx * maxPerRow + colIdx
        guard globalIdx < grid.headers.count else { return nil }
        return grid.headers[globalIdx]
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            ForEach(Array(grid.rows.enumerated()), id: \.offset) { index, row in
                // Label above (player name, date range, etc.)
                if !row.label.isEmpty {
                    if index > 0 {
                        Divider()
                            .padding(.top, 4)
                    }
                    Text(row.label)
                        .font(.system(.callout, design: .rounded, weight: .semibold))
                        .foregroundStyle(.primary)
                        .padding(.horizontal, 10)
                        .padding(.top, index == 0 ? 8 : 10)
                        .padding(.bottom, 2)
                }

                // Stacked stat rows
                let hChunks = headerChunks
                let vChunks = valueChunks(for: row)
                let showHeaders = index == 0 || !row.label.isEmpty

                VStack(alignment: .leading, spacing: 6) {
                    ForEach(Array(hChunks.enumerated()), id: \.offset) { chunkIdx, headers in
                        VStack(alignment: .leading, spacing: 1) {
                            if showHeaders {
                                HStack(spacing: 0) {
                                    ForEach(Array(headers.enumerated()), id: \.offset) { colIdx, header in
                                        Text(header)
                                            .font(.system(.caption2, design: .monospaced, weight: .semibold))
                                            .foregroundStyle(.secondary)
                                            .frame(width: columnWidth, alignment: .center)
                                            .contentShape(Rectangle())
                                            .onTapGesture {
                                                selectedStat = header
                                            }
                                    }
                                }
                                .padding(.horizontal, 6)
                            }

                            if chunkIdx < vChunks.count {
                                HStack(spacing: 0) {
                                    ForEach(Array(vChunks[chunkIdx].enumerated()), id: \.offset) { colIdx, value in
                                        Text(value)
                                            .font(.system(.callout, design: .monospaced, weight: .medium))
                                            .foregroundStyle(.primary)
                                            .frame(width: columnWidth, alignment: .center)
                                            .contentShape(Rectangle())
                                            .onTapGesture {
                                                if let header = headerForColumn(chunkIdx: chunkIdx, colIdx: colIdx) {
                                                    selectedStat = header
                                                }
                                            }
                                    }
                                }
                                .padding(.horizontal, 6)
                            }
                        }
                    }
                }
                .padding(.top, row.label.isEmpty && index == 0 ? 10 : 4)
                .padding(.bottom, 10)
            }
        }
        .background(
            RoundedRectangle(cornerRadius: 12)
                .fill(Color(uiColor: .secondarySystemBackground))
                .overlay(
                    RoundedRectangle(cornerRadius: 12)
                        .stroke(Color(uiColor: .separator).opacity(0.3), lineWidth: 0.5)
                )
        )
        .overlay {
            if let stat = selectedStat, let definition = StatDefinitions.lookup(stat) {
                ZStack {
                    // Dismiss background
                    Color.black.opacity(0.01)
                        .onTapGesture { selectedStat = nil }

                    VStack(alignment: .leading, spacing: 4) {
                        Text(stat)
                            .font(.system(.headline, design: .rounded, weight: .bold))
                            .foregroundStyle(.primary)
                        Text(definition)
                            .font(.system(.subheadline, design: .rounded))
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .padding(14)
                    .frame(maxWidth: 280, alignment: .leading)
                    .background(
                        RoundedRectangle(cornerRadius: 12)
                            .fill(.ultraThinMaterial)
                            .shadow(color: .black.opacity(0.15), radius: 12, y: 4)
                    )
                }
            }
        }
        .animation(.easeOut(duration: 0.15), value: selectedStat)
    }
}

/// View for a partial stat grid still being streamed
struct PartialStatGridView: View {
    let content: String

    var body: some View {
        Text(content.trimmingCharacters(in: .whitespacesAndNewlines))
            .font(.system(.callout, design: .monospaced))
            .foregroundStyle(.primary.opacity(0.6))
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(8)
            .background(
                RoundedRectangle(cornerRadius: 12)
                    .fill(Color(uiColor: .secondarySystemBackground))
                    .overlay(
                        RoundedRectangle(cornerRadius: 12)
                            .stroke(Color(uiColor: .separator).opacity(0.3), lineWidth: 0.5)
                    )
            )
    }
}
