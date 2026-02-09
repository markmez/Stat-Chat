import SwiftUI

struct StatGridView: View {
    let grid: StatGridParser.StatGrid

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
                                    ForEach(Array(headers.enumerated()), id: \.offset) { _, header in
                                        Text(header)
                                            .font(.system(.caption2, design: .monospaced, weight: .semibold))
                                            .foregroundStyle(.secondary)
                                            .frame(width: columnWidth, alignment: .center)
                                    }
                                }
                                .padding(.horizontal, 6)
                            }

                            if chunkIdx < vChunks.count {
                                HStack(spacing: 0) {
                                    ForEach(Array(vChunks[chunkIdx].enumerated()), id: \.offset) { _, value in
                                        Text(value)
                                            .font(.system(.callout, design: .monospaced, weight: .medium))
                                            .foregroundStyle(.primary)
                                            .frame(width: columnWidth, alignment: .center)
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
