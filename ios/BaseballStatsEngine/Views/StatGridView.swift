import SwiftUI

struct StatGridView: View {
    let grid: StatGridParser.StatGrid
    var onPlayerTap: ((String) -> Void)? = nil
    var compactHeaders: [String]? = nil

    /// 1-line summary: the 7 key batting stats shown when compact
    static let summaryHeaders = ["G", "AB", "AVG", "OBP", "SLG", "OPS", "HR"]

    @State private var selectedStat: String? = nil
    @State private var isExpanded = false
    @State private var showProjection = false

    private let deepBlue = Color(red: 0.1, green: 0.25, blue: 0.7)

    /// Max columns per row before splitting into stacked rows
    private let maxPerRow = 7

    /// Uniform column width — same for every column across all rows so they align in a true grid
    private let columnWidth: CGFloat = 50

    /// Whether this grid looks like streak data (date-range labels, has G + rate stats, no PA)
    private var isStreakGrid: Bool {
        let headers = Set(grid.headers)
        let hasG = headers.contains("G")
        let rateHits = ["AVG", "OBP", "SLG", "OPS"].filter { headers.contains($0) }.count
        let hasPA = headers.contains("PA")
        let hasDateLabel = grid.rows.contains { $0.label.contains("\u{2013}") || $0.label.contains("–") }
        return hasG && rateHits >= 2 && !hasPA && hasDateLabel
    }

    private static let countingStats: Set<String> = ["G", "AB", "H", "BB", "SO", "HR"]

    /// Project a streak row's values to a 162-game pace
    static func projectTo162(headers: [String], values: [String]) -> [String] {
        guard let gIdx = headers.firstIndex(of: "G"),
              gIdx < values.count,
              let numGames = Double(values[gIdx]),
              numGames > 0 else { return values }

        let scale = 162.0 / numGames
        return zip(headers, values).map { header, value in
            if header == "Perf" { return "—" }
            if countingStats.contains(header), let raw = Double(value) {
                return String(Int((raw * scale).rounded()))
            }
            return value
        }
    }

    /// Whether compact mode is active (compactHeaders provided and grid has more columns than compact set)
    private var isCompactAvailable: Bool {
        guard let compact = compactHeaders else { return false }
        return grid.headers.count > compact.count
    }

    /// Headers to display — compact subset or full grid
    private var displayHeaders: [String] {
        if isCompactAvailable && !isExpanded {
            return compactIndices.map { grid.headers[$0] }
        }
        return grid.headers
    }

    /// Indices into grid.headers that match compactHeaders (preserving compact order, skipping missing)
    private var compactIndices: [Int] {
        guard let compact = compactHeaders else { return [] }
        var indices: [Int] = []
        for header in compact {
            if let idx = grid.headers.firstIndex(of: header) {
                indices.append(idx)
            }
        }
        return indices
    }

    /// Filter a row's values to only the compact columns
    private func compactValues(for row: StatGridParser.StatGrid.Row) -> [String] {
        compactIndices.compactMap { idx in
            idx < row.values.count ? row.values[idx] : nil
        }
    }

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

    /// Headers chunked for display
    private var displayHeaderChunks: [[String]] { chunk(displayHeaders) }

    /// Values for a row, chunked for display
    private func displayValueChunks(for row: StatGridParser.StatGrid.Row) -> [[String]] {
        if isCompactAvailable && !isExpanded {
            return chunk(compactValues(for: row))
        }
        return chunk(row.values)
    }

    /// Map a chunk index + column index back to the header abbreviation
    private func headerForColumn(chunkIdx: Int, colIdx: Int) -> String? {
        let headers = displayHeaders
        let globalIdx = chunkIdx * maxPerRow + colIdx
        guard globalIdx < headers.count else { return nil }
        return headers[globalIdx]
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
                    if let playerName = PlayerNameExtractor.extract(row.label),
                       let tap = onPlayerTap {
                        Button {
                            tap(playerName)
                        } label: {
                            Text(row.label)
                                .font(.system(.callout, design: .rounded, weight: .semibold))
                                .foregroundStyle(deepBlue)
                                .underline(true, color: deepBlue.opacity(0.4))
                        }
                        .padding(.horizontal, 10)
                        .padding(.top, index == 0 ? 8 : 10)
                        .padding(.bottom, 2)
                    } else {
                        Text(row.label)
                            .font(.system(.callout, design: .rounded, weight: .semibold))
                            .foregroundStyle(.primary)
                            .padding(.horizontal, 10)
                            .padding(.top, index == 0 ? 8 : 10)
                            .padding(.bottom, 2)
                    }
                }

                // Stacked stat rows
                let hChunks = displayHeaderChunks
                let vChunks = displayValueChunks(for: row)
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
                .padding(.bottom, showProjection ? 4 : 10)

                // Projected 162-game pace row
                if showProjection && isStreakGrid {
                    let projValues = Self.projectTo162(headers: displayHeaders, values: isCompactAvailable && !isExpanded ? compactValues(for: row) : row.values)
                    let projChunks = chunk(projValues)
                    let hChunksProj = displayHeaderChunks

                    VStack(alignment: .leading, spacing: 0) {
                        Text("162-game pace")
                            .font(.system(.caption, design: .rounded, weight: .medium))
                            .foregroundStyle(.secondary)
                            .padding(.horizontal, 10)
                            .padding(.top, 6)
                            .padding(.bottom, 2)

                        VStack(alignment: .leading, spacing: 6) {
                            ForEach(Array(projChunks.enumerated()), id: \.offset) { chunkIdx, vals in
                                if chunkIdx < hChunksProj.count {
                                    VStack(alignment: .leading, spacing: 1) {
                                        // Repeat headers so projected values are labeled
                                        HStack(spacing: 0) {
                                            ForEach(Array(hChunksProj[chunkIdx].enumerated()), id: \.offset) { _, header in
                                                Text(header)
                                                    .font(.system(.caption2, design: .monospaced, weight: .semibold))
                                                    .foregroundStyle(.secondary.opacity(0.6))
                                                    .frame(width: columnWidth, alignment: .center)
                                            }
                                        }
                                        .padding(.horizontal, 6)

                                        HStack(spacing: 0) {
                                            ForEach(Array(vals.enumerated()), id: \.offset) { _, value in
                                                Text(value)
                                                    .font(.system(.callout, design: .monospaced, weight: .medium))
                                                    .foregroundStyle(.secondary)
                                                    .frame(width: columnWidth, alignment: .center)
                                            }
                                        }
                                        .padding(.horizontal, 6)
                                    }
                                }
                            }
                        }
                    }
                    .padding(.bottom, 10)
                }
            }

            // More / Less toggle
            if isCompactAvailable {
                Button {
                    withAnimation(.easeInOut(duration: 0.2)) {
                        isExpanded.toggle()
                    }
                } label: {
                    HStack(spacing: 4) {
                        Text(isExpanded ? "Less" : "More")
                            .font(.system(.caption, design: .rounded, weight: .medium))
                        Image(systemName: isExpanded ? "chevron.up" : "chevron.down")
                            .font(.system(size: 9, weight: .semibold))
                    }
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, 12)
                    .padding(.bottom, 10)
                }
                .buttonStyle(.plain)
            }

            // Streak projection toggle
            if isStreakGrid {
                Button {
                    withAnimation(.easeInOut(duration: 0.2)) {
                        showProjection.toggle()
                    }
                } label: {
                    HStack(spacing: 4) {
                        Text(showProjection ? "Hide Projection" : "Project Over 162 Game Season")
                            .font(.system(.caption, design: .rounded, weight: .medium))
                        Image(systemName: showProjection ? "chevron.up" : "chevron.down")
                            .font(.system(size: 9, weight: .semibold))
                    }
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, 12)
                    .padding(.bottom, 10)
                }
                .buttonStyle(.plain)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
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
