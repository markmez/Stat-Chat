import SwiftUI

struct StatGridView: View {
    let grid: StatGridParser.StatGrid
    var onPlayerTap: ((String) -> Void)? = nil
    var compactHeaders: [String]? = nil

    /// 1-line summary: the 7 key batting stats shown when compact
    static let summaryHeaders = ["G", "AB", "AVG", "OBP", "SLG", "OPS", "HR"]

    /// Grid with all-empty columns removed (columns where every row is "--" or empty)
    private var filteredGrid: StatGridParser.StatGrid {
        let headers = grid.headers
        guard !headers.isEmpty, !grid.rows.isEmpty else { return grid }

        // Find columns where ALL rows have "--" or are empty
        var emptyColumns = Set<Int>()
        for idx in headers.indices {
            let allEmpty = grid.rows.allSatisfy { row in
                idx >= row.values.count || row.values[idx] == "--" || row.values[idx].isEmpty
            }
            if allEmpty { emptyColumns.insert(idx) }
        }

        guard !emptyColumns.isEmpty else { return grid }

        let keptHeaders = headers.enumerated().compactMap { emptyColumns.contains($0.offset) ? nil : $0.element }
        let keptRows = grid.rows.map { row in
            let keptValues = row.values.enumerated().compactMap { emptyColumns.contains($0.offset) ? nil : $0.element }
            return StatGridParser.StatGrid.Row(label: row.label, values: keptValues)
        }
        return StatGridParser.StatGrid(headers: keptHeaders, rows: keptRows, formMetadata: grid.formMetadata)
    }

    @State private var selectedStat: String? = nil
    @State private var isExpanded = false
    @State private var showProjection = false
    @State private var formSliderGameNumber: Int? = nil
    @State private var formGameLogs: [GameLog]? = nil
    @State private var formShowProjection = false

    private let deepBlue = Color(red: 0.1, green: 0.25, blue: 0.7)

    /// Max columns per row before splitting into stacked rows
    private let maxPerRow = 7

    /// Uniform column width — same for every column across all rows so they align in a true grid
    private let columnWidth: CGFloat = 50

    /// Whether this grid looks like streak data (date-range labels, has G + rate stats, no PA)
    private var isStreakGrid: Bool {
        let headers = Set(filteredGrid.headers)
        let hasG = headers.contains("G")
        let rateHits = ["AVG", "OBP", "SLG", "OPS"].filter { headers.contains($0) }.count
        let hasPA = headers.contains("PA")
        let hasDateLabel = filteredGrid.rows.contains { $0.label.contains("\u{2013}") || $0.label.contains("–") }
        return hasG && rateHits >= 2 && !hasPA && hasDateLabel
    }

    private static let countingStats: Set<String> = ["G", "AB", "R", "H", "BB", "SO", "HR", "RBI"]

    /// Recompute form stats from game logs starting at a given game number.
    static func recomputeFromLogs(_ logs: [GameLog], fromGameNumber: Int) -> StatGridParser.StatGrid? {
        let startIdx = max(0, fromGameNumber - 1)
        guard startIdx < logs.count else { return nil }
        let slice = Array(logs[startIdx...])
        guard !slice.isEmpty else { return nil }

        var totalAB = 0, totalH = 0, total2B = 0, total3B = 0, totalHR = 0
        var totalR = 0, totalRBI = 0, totalBB = 0, totalSO = 0, totalPA = 0
        for g in slice {
            totalAB += g.atBats; totalH += g.hits
            total2B += g.doubles; total3B += g.triples; totalHR += g.homeRuns
            totalR += g.runs; totalRBI += g.rbi
            totalBB += g.walks; totalSO += g.strikeouts; totalPA += g.plateAppearances
        }

        let avg = totalAB > 0 ? Double(totalH) / Double(totalAB) : 0
        let obp = totalPA > 0 ? Double(totalH + totalBB) / Double(totalPA) : 0
        let tb = (totalH - total2B - total3B - totalHR) + 2 * total2B + 3 * total3B + 4 * totalHR
        let slg = totalAB > 0 ? Double(tb) / Double(totalAB) : 0
        let ops = obp + slg

        func fmtRate(_ v: Double) -> String {
            let s = String(format: "%.3f", v)
            return s.hasPrefix("0.") ? String(s.dropFirst()) : s
        }

        let headers = ["G", "AB", "R", "H", "HR", "RBI", "BB", "SO", "AVG", "OBP", "SLG", "OPS"]
        let values = [
            String(slice.count), String(totalAB), String(totalR), String(totalH),
            String(totalHR), String(totalRBI), String(totalBB), String(totalSO),
            fmtRate(avg), fmtRate(obp), fmtRate(slg), fmtRate(ops)
        ]
        return StatGridParser.StatGrid(
            headers: headers,
            rows: [StatGridParser.StatGrid.Row(label: "", values: values)]
        )
    }

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
        return filteredGrid.headers.count > compact.count
    }

    /// Headers to display — compact subset or full grid
    private var displayHeaders: [String] {
        if isCompactAvailable && !isExpanded {
            return compactIndices.map { filteredGrid.headers[$0] }
        }
        return filteredGrid.headers
    }

    /// Indices into filteredGrid.headers that match compactHeaders (preserving compact order, skipping missing)
    private var compactIndices: [Int] {
        guard let compact = compactHeaders else { return [] }
        var indices: [Int] = []
        for header in compact {
            if let idx = filteredGrid.headers.firstIndex(of: header) {
                indices.append(idx)
            }
        }
        return indices
    }

    /// Filter a row's values to only the compact columns (from filteredGrid)
    private func compactValues(for row: StatGridParser.StatGrid.Row) -> [String] {
        compactIndices.compactMap { idx in
            idx < row.values.count ? row.values[idx] : nil
        }
    }

    /// Filtered rows for iteration
    private var displayRows: [StatGridParser.StatGrid.Row] { filteredGrid.rows }

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
            ForEach(Array(displayRows.enumerated()), id: \.offset) { index, row in
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

            // Chat form slider (when FORM: metadata is present)
            if let meta = grid.formMetadata {
                let effectiveGameNumber = formSliderGameNumber ?? meta.autoDetectedGameNumber
                VStack(alignment: .leading, spacing: 4) {
                    HStack {
                        Text("Game \(effectiveGameNumber)")
                            .font(.system(.caption, design: .rounded, weight: .medium))
                            .foregroundStyle(.secondary)
                        Spacer()
                        Text("Game \(meta.totalGames)")
                            .font(.system(.caption, design: .rounded, weight: .medium))
                            .foregroundStyle(.secondary)
                    }

                    Slider(
                        value: Binding<Double>(
                            get: { Double(effectiveGameNumber) },
                            set: { newValue in
                                formSliderGameNumber = max(1, min(Int(newValue.rounded()), meta.totalGames - 6))
                            }
                        ),
                        in: 1...Double(max(meta.totalGames - 6, 2)),
                        step: 1
                    )
                    .tint(deepBlue)
                }
                .padding(.horizontal, 12)
                .padding(.bottom, 10)
                .onAppear {
                    if formGameLogs == nil {
                        formGameLogs = PlayerCardService.fetchGameLogsForSeason(
                            name: meta.playerName, season: meta.season
                        )
                    }
                }

                // Show recomputed stats if slider moved
                if let logs = formGameLogs, formSliderGameNumber != nil {
                    let recomputed = Self.recomputeFromLogs(logs, fromGameNumber: effectiveGameNumber)
                    if let reGrid = recomputed {
                        VStack(alignment: .leading, spacing: 1) {
                            let rHeaders = chunk(reGrid.headers)
                            let rValues = chunk(reGrid.rows.first?.values ?? [])
                            ForEach(Array(rHeaders.enumerated()), id: \.offset) { chunkIdx, hdrs in
                                VStack(alignment: .leading, spacing: 1) {
                                    HStack(spacing: 0) {
                                        ForEach(Array(hdrs.enumerated()), id: \.offset) { _, header in
                                            Text(header)
                                                .font(.system(.caption2, design: .monospaced, weight: .semibold))
                                                .foregroundStyle(.secondary.opacity(0.6))
                                                .frame(width: columnWidth, alignment: .center)
                                        }
                                    }
                                    .padding(.horizontal, 6)

                                    if chunkIdx < rValues.count {
                                        HStack(spacing: 0) {
                                            ForEach(Array(rValues[chunkIdx].enumerated()), id: \.offset) { _, value in
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
                        .padding(.bottom, 10)
                    }
                }

                // Form projection toggle
                Button {
                    withAnimation(.easeInOut(duration: 0.2)) {
                        formShowProjection.toggle()
                    }
                } label: {
                    HStack(spacing: 4) {
                        Text(formShowProjection ? "Hide 162-Game Pace" : "Show 162-Game Pace")
                            .font(.system(.caption, design: .rounded, weight: .medium))
                        Image(systemName: formShowProjection ? "chevron.up" : "chevron.down")
                            .font(.system(size: 9, weight: .semibold))
                    }
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, 12)
                    .padding(.bottom, formShowProjection ? 4 : 10)
                }
                .buttonStyle(.plain)

                if formShowProjection {
                    let sourceValues = {
                        if let logs = formGameLogs, let slider = formSliderGameNumber,
                           let reGrid = Self.recomputeFromLogs(logs, fromGameNumber: slider) {
                            return (reGrid.headers, reGrid.rows.first?.values ?? [])
                        }
                        return (filteredGrid.headers, displayRows.first?.values ?? [])
                    }()
                    let projValues = Self.projectTo162(headers: sourceValues.0, values: sourceValues.1)
                    let projChunks = chunk(projValues)
                    let projHeaders = chunk(sourceValues.0)

                    VStack(alignment: .leading, spacing: 0) {
                        Text("162-game pace")
                            .font(.system(.caption, design: .rounded, weight: .medium))
                            .foregroundStyle(.secondary)
                            .padding(.horizontal, 10)
                            .padding(.bottom, 2)

                        VStack(alignment: .leading, spacing: 6) {
                            ForEach(Array(projChunks.enumerated()), id: \.offset) { chunkIdx, vals in
                                if chunkIdx < projHeaders.count {
                                    VStack(alignment: .leading, spacing: 1) {
                                        HStack(spacing: 0) {
                                            ForEach(Array(projHeaders[chunkIdx].enumerated()), id: \.offset) { _, header in
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
