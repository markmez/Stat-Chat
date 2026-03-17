import SwiftUI

struct StatGridView: View {
    let grid: StatGridParser.StatGrid
    var onPlayerTap: ((String) -> Void)? = nil
    var compactHeaders: [String]? = nil
    /// When set, enables "Project Over 162 Games" toggle using this as the divisor (player's season games).
    /// Counting stats scale by 162 / seasonGames. Rate stats stay unchanged.
    var seasonGames: Int? = nil
    /// When true, suppresses the default rounded-rect background (for embedding inside a shared container)
    var suppressBackground: Bool = false

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
            return StatGridParser.StatGrid.Row(label: row.label, values: keptValues, note: row.note)
        }
        return StatGridParser.StatGrid(headers: keptHeaders, rows: keptRows, formMetadata: grid.formMetadata)
    }

    @State private var selectedStat: String? = nil
    @State private var isExpanded = false
    @State private var showProjection = false
    /// Slider value: "last N games" (nil = auto-detected from FORM metadata)
    @State private var formSliderNumGames: Int? = nil
    @State private var formGameLogs: [GameLog]? = nil
    /// 0 = 162-Game Pace, 1 = Season Forecast (always visible when form present)
    @State private var formProjectionMode: Int = 0

    private let deepBlue = Color(red: 0.1, green: 0.25, blue: 0.7)

    /// Max columns per row before splitting into stacked rows
    private let maxPerRow = 7

    /// Uniform column width — same for every column across all rows so they align in a true grid
    private let columnWidth: CGFloat = 50

    /// Whether the projection toggle should be available
    private var canProject: Bool {
        grid.formMetadata == nil && (isStreakGrid || seasonGames != nil)
    }

    /// Whether this grid looks like streak data (date-range labels, has G + rate stats, no PA)
    private var isStreakGrid: Bool {
        let headers = Set(filteredGrid.headers)
        let hasG = headers.contains("G")
        let rateHits = ["AVG", "OBP", "SLG", "OPS"].filter { headers.contains($0) }.count
        let hasPA = headers.contains("PA")
        let hasDateLabel = filteredGrid.rows.contains { $0.label.contains("\u{2013}") || $0.label.contains("–") }
        return hasG && rateHits >= 2 && !hasPA && hasDateLabel
    }

    private static let countingStats: Set<String> = ["G", "PA", "AB", "R", "H", "BB", "SO", "HR", "RBI", "2B", "3B", "HBP", "SF"]

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
            if header == "Perf" { return "\u{2014}" }
            if countingStats.contains(header), let raw = Double(value) {
                return String(Int((raw * scale).rounded()))
            }
            return value
        }
    }

    /// Project a row's counting stats to 162-game pace using the player's season games as the divisor.
    static func projectTo162WithScale(headers: [String], values: [String], seasonGames: Int) -> [String] {
        guard seasonGames > 0 else { return values }
        let scale = 162.0 / Double(seasonGames)
        return zip(headers, values).map { header, value in
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

    /// When form metadata is present and slider has been adjusted (or logs are loaded),
    /// returns recomputed rows so the ForEach renders updated values inline.
    private var activeDisplayRows: [StatGridParser.StatGrid.Row] {
        guard let meta = grid.formMetadata,
              let logs = formGameLogs, !logs.isEmpty else {
            return displayRows
        }
        let numGamesShown = formSliderNumGames ?? (meta.totalGames - meta.autoDetectedGameNumber + 1)
        let effectiveGameNumber = meta.totalGames - numGamesShown + 1
        if let reGrid = Self.recomputeFromLogs(logs, fromGameNumber: effectiveGameNumber) {
            return reGrid.rows
        }
        return displayRows
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

    // MARK: - Form helpers

    /// Number of games currently shown by the slider
    private func formNumGamesShown(meta: StatGridParser.StatGrid.FormMetadata) -> Int {
        formSliderNumGames ?? (meta.totalGames - meta.autoDetectedGameNumber + 1)
    }

    /// The 1-indexed game number where the current form window starts
    private func formEffectiveGameNumber(meta: StatGridParser.StatGrid.FormMetadata) -> Int {
        meta.totalGames - formNumGamesShown(meta: meta) + 1
    }

    /// Date string for the start of the current form window
    private func formStartDate(meta: StatGridParser.StatGrid.FormMetadata) -> String {
        if let logs = formGameLogs, !logs.isEmpty {
            let idx = max(0, formEffectiveGameNumber(meta: meta) - 1)
            if idx < logs.count {
                return PlayerCardService.formatDateShort(logs[idx].date)
            }
        }
        return ""
    }

    // MARK: - Projection

    private static func fmtRate(_ v: Double) -> String {
        let s = String(format: "%.3f", v)
        return s.hasPrefix("0.") ? String(s.dropFirst()) : s
    }

    /// Build projection values for chat form grids.
    /// mode 0 = 162-Game Pace, mode 1 = Season Forecast
    private func buildChatFormProjection(
        meta: StatGridParser.StatGrid.FormMetadata,
        currentHeaders: [String],
        currentValues: [String],
        numGames: Int,
        effectiveGameNumber: Int
    ) -> [String] {
        guard numGames > 0 else { return currentValues }

        // Parse counting values from current grid
        var formCounting: [String: Double] = [:]
        for (idx, header) in currentHeaders.enumerated() {
            if Self.countingStats.contains(header), idx < currentValues.count,
               let v = Double(currentValues[idx]) {
                formCounting[header] = v
            }
        }

        if formProjectionMode == 0 {
            // Pace: stat * 162 / numGames
            var projected: [String] = []
            for (idx, header) in currentHeaders.enumerated() {
                guard idx < currentValues.count else { break }
                if Self.countingStats.contains(header) {
                    let raw = formCounting[header] ?? 0
                    let proj = raw * 162.0 / Double(numGames)
                    projected.append(String(Int(proj.rounded())))
                } else {
                    projected.append(currentValues[idx])
                }
            }
            return projected
        } else {
            // Forecast: pre-streak actuals + streak + remaining games at streak pace
            let remaining = max(0, 162 - meta.teamGames)

            var preStreakCounting: [String: Double] = [:]
            if let logs = formGameLogs, !logs.isEmpty {
                let preStreakEnd = max(0, effectiveGameNumber - 1)
                let preSlice = logs.prefix(preStreakEnd)
                var ab = 0, h = 0, hr = 0, r = 0, rbi = 0, bb = 0, so = 0
                for g in preSlice {
                    ab += g.atBats; h += g.hits; hr += g.homeRuns
                    r += g.runs; rbi += g.rbi; bb += g.walks; so += g.strikeouts
                }
                preStreakCounting = [
                    "G": Double(preSlice.count), "AB": Double(ab), "H": Double(h),
                    "HR": Double(hr), "R": Double(r), "RBI": Double(rbi),
                    "BB": Double(bb), "SO": Double(so)
                ]
            }

            // Blended = pre + form + (remaining / formGames) * form
            var blended: [String: Double] = [:]
            for stat in Self.countingStats {
                let pre = preStreakCounting[stat] ?? 0
                let form = formCounting[stat] ?? 0
                blended[stat] = pre + form + (Double(remaining) / Double(numGames)) * form
            }

            // Recompute rate stats from blended counting values
            // Estimate 2B/3B from full season game logs ratios
            let fullAB: Double = {
                guard let logs = formGameLogs else { return 1 }
                return Double(logs.reduce(0) { $0 + $1.atBats })
            }()
            let full2B: Double = {
                guard let logs = formGameLogs else { return 0 }
                return Double(logs.reduce(0) { $0 + $1.doubles })
            }()
            let full3B: Double = {
                guard let logs = formGameLogs else { return 0 }
                return Double(logs.reduce(0) { $0 + $1.triples })
            }()
            let ratio2B = fullAB > 0 ? full2B / fullAB : 0
            let ratio3B = fullAB > 0 ? full3B / fullAB : 0

            let bAB = blended["AB"] ?? 0
            let bH = blended["H"] ?? 0
            let bBB = blended["BB"] ?? 0
            let bHR = blended["HR"] ?? 0
            let est2B = bAB * ratio2B
            let est3B = bAB * ratio3B
            let avg = bAB > 0 ? bH / bAB : 0
            let pa = bAB + bBB
            let obp = pa > 0 ? (bH + bBB) / pa : 0
            let tb = (bH - est2B - est3B - bHR) + 2 * est2B + 3 * est3B + 4 * bHR
            let slg = bAB > 0 ? tb / bAB : 0
            let rates: [String: String] = [
                "AVG": Self.fmtRate(avg), "OBP": Self.fmtRate(obp),
                "SLG": Self.fmtRate(slg), "OPS": Self.fmtRate(obp + slg)
            ]

            var projected: [String] = []
            for header in currentHeaders {
                if Self.countingStats.contains(header) {
                    projected.append(String(Int((blended[header] ?? 0).rounded())))
                } else if let rate = rates[header] {
                    projected.append(rate)
                } else {
                    projected.append("--")
                }
            }
            return projected
        }
    }

    // MARK: - Body

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            // Form subtitle + slider (above the stat grid, matching profile UX)
            if let meta = grid.formMetadata {
                let numGames = formNumGamesShown(meta: meta)
                let dateStr = formStartDate(meta: meta)

                VStack(alignment: .leading, spacing: 6) {
                    if !dateStr.isEmpty {
                        Text("Since \(dateStr) (\(numGames) games)")
                            .font(.system(.subheadline, design: .rounded))
                            .foregroundStyle(.secondary)
                            .padding(.horizontal, 14)
                    } else {
                        Text("Last \(numGames) games")
                            .font(.system(.subheadline, design: .rounded))
                            .foregroundStyle(.secondary)
                            .padding(.horizontal, 14)
                    }

                    Slider(
                        value: Binding<Double>(
                            get: { Double(numGames) },
                            set: { newValue in
                                formSliderNumGames = max(1, min(Int(newValue.rounded()), meta.totalGames))
                            }
                        ),
                        in: 1...Double(max(meta.totalGames, 2)),
                        step: 1
                    )
                    .tint(deepBlue)
                    .padding(.horizontal, 14)
                }
                .padding(.top, 10)
                .padding(.bottom, 6)
                .onAppear {
                    if formGameLogs == nil {
                        formGameLogs = PlayerCardService.fetchGameLogsForSeason(
                            name: meta.playerName, season: meta.season
                        )
                    }
                }
            }

            ForEach(Array(activeDisplayRows.enumerated()), id: \.offset) { index, row in
                // Label above (player name, date range, etc.)
                if !row.label.isEmpty {
                    if index > 0 {
                        Divider()
                            .padding(.top, 4)
                    }
                    if let playerName = PlayerNameExtractor.extract(row.label),
                       let tap = onPlayerTap {
                        HStack(spacing: 0) {
                            Button {
                                tap(playerName)
                            } label: {
                                Text(playerName)
                                    .font(.system(.callout, design: .rounded, weight: .semibold))
                                    .foregroundStyle(deepBlue)
                                    .underline(true, color: deepBlue.opacity(0.4))
                            }
                            // Show any suffix (e.g. year) after the tappable name
                            if row.label != playerName,
                               let parenIdx = row.label.firstIndex(of: "(") {
                                Text(" " + String(row.label[parenIdx...]))
                                    .font(.system(.callout, design: .rounded, weight: .semibold))
                                    .foregroundStyle(.secondary)
                            }
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
                .padding(.bottom, row.note != nil ? 4 : (showProjection ? 4 : 10))

                if let note = row.note {
                    Text(note)
                        .font(.system(.caption, design: .rounded))
                        .foregroundStyle(.secondary)
                        .italic()
                        .padding(.horizontal, 10)
                        .padding(.bottom, showProjection ? 4 : 10)
                }

                // Projected 162-game pace row
                if showProjection && canProject {
                    let rowValues = isCompactAvailable && !isExpanded ? compactValues(for: row) : row.values
                    let projValues = seasonGames.map { Self.projectTo162WithScale(headers: displayHeaders, values: rowValues, seasonGames: $0) }
                        ?? Self.projectTo162(headers: displayHeaders, values: rowValues)
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

            // More / Less toggle (hide for form grids)
            if isCompactAvailable && grid.formMetadata == nil {
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

            // Projection toggle
            if canProject {
                Button {
                    withAnimation(.easeInOut(duration: 0.2)) {
                        showProjection.toggle()
                    }
                } label: {
                    HStack(spacing: 6) {
                        Text(showProjection ? "Full Season Projection" : "Full Season Projection")
                            .font(.system(.subheadline, design: .rounded, weight: .semibold))
                            .foregroundStyle(.primary)
                        Image(systemName: showProjection ? "chevron.up" : "chevron.down")
                            .font(.system(size: 11, weight: .semibold))
                            .foregroundStyle(.secondary)
                    }
                    .padding(.horizontal, 12)
                    .padding(.bottom, 10)
                }
                .buttonStyle(.plain)
            }

            // Chat form projection (always visible when FORM: metadata present)
            if let meta = grid.formMetadata {
                let numGames = formNumGamesShown(meta: meta)
                let effectiveGameNumber = formEffectiveGameNumber(meta: meta)
                let currentRow = activeDisplayRows.first
                let currentHeaders = displayHeaders
                let currentValues = currentRow?.values ?? []

                // Divider
                Rectangle()
                    .fill(Color(uiColor: .separator).opacity(0.3))
                    .frame(height: 1)
                    .padding(.horizontal, 14)

                // Projection tabs
                let hasRemainingGames = meta.teamGames < 162

                VStack(alignment: .leading, spacing: 6) {
                    HStack(spacing: 0) {
                        Button {
                            withAnimation(.easeInOut(duration: 0.15)) {
                                formProjectionMode = 0
                            }
                        } label: {
                            Text("162-Game Pace")
                                .font(.system(.caption, design: .rounded, weight: .medium))
                                .padding(.horizontal, 10)
                                .padding(.vertical, 5)
                                .background(
                                    RoundedRectangle(cornerRadius: 6)
                                        .fill(formProjectionMode == 0
                                              ? deepBlue.opacity(0.12)
                                              : Color.clear)
                                )
                                .foregroundStyle(formProjectionMode == 0 ? deepBlue : .secondary)
                        }
                        .buttonStyle(.plain)

                        if hasRemainingGames {
                            Button {
                                withAnimation(.easeInOut(duration: 0.15)) {
                                    formProjectionMode = 1
                                }
                            } label: {
                                Text("Season Forecast")
                                    .font(.system(.caption, design: .rounded, weight: .medium))
                                    .padding(.horizontal, 10)
                                    .padding(.vertical, 5)
                                    .background(
                                        RoundedRectangle(cornerRadius: 6)
                                            .fill(formProjectionMode == 1
                                                  ? deepBlue.opacity(0.12)
                                                  : Color.clear)
                                    )
                                    .foregroundStyle(formProjectionMode == 1 ? deepBlue : .secondary)
                            }
                            .buttonStyle(.plain)
                        }
                    }
                    .padding(.horizontal, 14)
                    .padding(.top, 8)

                    // Projected stat rows
                    let projValues = buildChatFormProjection(
                        meta: meta,
                        currentHeaders: currentHeaders,
                        currentValues: currentValues,
                        numGames: numGames,
                        effectiveGameNumber: effectiveGameNumber
                    )
                    let projChunks = chunk(projValues)
                    let projHeaderChunks = displayHeaderChunks

                    VStack(alignment: .leading, spacing: 6) {
                        ForEach(Array(projChunks.enumerated()), id: \.offset) { chunkIdx, vals in
                            if chunkIdx < projHeaderChunks.count {
                                VStack(alignment: .leading, spacing: 1) {
                                    HStack(spacing: 0) {
                                        ForEach(Array(projHeaderChunks[chunkIdx].enumerated()), id: \.offset) { _, header in
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
                    .padding(.bottom, 10)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .background {
            if !suppressBackground {
                RoundedRectangle(cornerRadius: 12)
                    .fill(.white)
                    .shadow(color: Color(red: 0.1, green: 0.25, blue: 0.7).opacity(0.10), radius: 10, y: 3)
                    .shadow(color: .black.opacity(0.03), radius: 2, y: 1)
            }
        }
        .overlay {
            if let stat = selectedStat, let definition = StatDefinitions.lookup(stat) {
                // Full-screen dismiss layer so tapping anywhere outside dismisses
                Color.black.opacity(0.01)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .ignoresSafeArea()
                    .contentShape(Rectangle())
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
                .onTapGesture { selectedStat = nil }
            }
        }
        .animation(.easeOut(duration: 0.15), value: selectedStat)
        // Auto-dismiss after a delay so it doesn't get stuck
        .onChange(of: selectedStat) {
            if selectedStat != nil {
                let shown = selectedStat
                DispatchQueue.main.asyncAfter(deadline: .now() + 5) {
                    if selectedStat == shown {
                        selectedStat = nil
                    }
                }
            }
        }
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
                    .fill(.white)
                    .shadow(color: Color(red: 0.1, green: 0.25, blue: 0.7).opacity(0.10), radius: 10, y: 3)
                    .shadow(color: .black.opacity(0.03), radius: 2, y: 1)
            )
    }
}
