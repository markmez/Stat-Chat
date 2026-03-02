import SwiftUI

struct PlayerCardView: View {
    let playerName: String

    @Environment(\.dismiss) private var dismiss
    @State private var playerCard: PlayerCard?
    @State private var isLoading = true
    @State private var expandedSeasons: Set<Int> = []
    @State private var projectionMode: ProjectionMode = .fullSeason
    @State private var formSliderGameNumber: Int? = nil  // nil = auto-detected, represents "last N games"
    @State private var gameLogs: [GameLog]? = nil
    @State private var formProjectionMode: FormProjectionMode = .pace
    @State private var showFormProjection = false
    @State private var splitTab: SplitTab = .platoon
    @State private var priorSeasonTabs: [Int: SplitTab] = [:]

    private let deepBlue = Color(red: 0.1, green: 0.25, blue: 0.7)
    private let lightBlue = Color(red: 0.45, green: 0.7, blue: 1.0)

    enum ProjectionMode: String, CaseIterable {
        case fullSeason = "162 games"
        case gamesMissed = "Account for games missed"
    }

    enum FormProjectionMode: String, CaseIterable {
        case pace = "162-Game Pace"
        case forecast = "Season Forecast"
    }

    enum SplitTab: String, CaseIterable {
        case platoon = "Platoon"
        case homeAway = "Home / Away"
        case streaks = "Hot Streaks"
        case fielding = "Fielding"
    }

    var body: some View {
        ZStack {
            Color(uiColor: .systemBackground)
                .ignoresSafeArea()

            if isLoading {
                LoadingIndicator()
            } else if let card = playerCard {
                ScrollView {
                    VStack(alignment: .leading, spacing: 24) {
                        // Header with back arrow + subtitle
                        VStack(alignment: .leading, spacing: 4) {
                            HStack(alignment: .top, spacing: 10) {
                                Button(action: { dismiss() }) {
                                    Image(systemName: "chevron.left")
                                        .font(.system(size: 22, weight: .medium))
                                        .foregroundStyle(lightBlue)
                                }
                                .padding(.top, 2)

                                Text(card.name)
                                    .font(.system(.title2, design: .rounded, weight: .bold))
                                    .foregroundStyle(.primary)
                            }

                            // Full team name + position + age + handedness
                            HStack(spacing: 0) {
                                Text(card.fullTeamName)
                                if let positions = card.positions {
                                    Text("  \u{00B7}  \(positions)")
                                }
                                if let age = card.age {
                                    Text("  \u{00B7}  Age \(age)")
                                }
                                if let bats = card.bats, let throws_ = card.throws_ {
                                    Text("  \u{00B7}  B/T: \(bats)/\(throws_)")
                                }
                            }
                            .font(.system(.subheadline, design: .rounded))
                            .foregroundStyle(.secondary)
                        }
                        .padding(.horizontal, 20)

                        // Current season
                        if let current = card.seasons.first {
                            VStack(alignment: .leading, spacing: 8) {
                                Text("\(String(current.year)) Season")
                                    .font(.system(.headline, design: .rounded, weight: .semibold))
                                    .foregroundStyle(.primary)
                                    .padding(.horizontal, 20)

                                if let teamLabel = seasonTeamLabel(teamStr: current.team, headerTeam: card.team) {
                                    Text(teamLabel)
                                        .font(.system(.subheadline, design: .rounded))
                                        .foregroundStyle(.secondary)
                                        .padding(.horizontal, 20)
                                }

                                StatGridView(grid: current.stats)
                                    .padding(.horizontal, 6)
                            }

                            // Current form section (includes projection when present)
                            if current.currentForm != nil {
                                currentFormSection(season: current)
                            } else {
                                // Projected stats only when no current form (hot streak has its own projection)
                                projectedStatsSection(season: current)
                            }

                            // Unified splits section (platoon / home-away / hot streaks)
                            splitsSection(
                                season: current,
                                tab: $splitTab
                            )
                        }

                        // Career totals
                        if let career = card.careerTotals {
                            sectionView(title: "Career", grid: career)
                        }

                        // Prior seasons — expandable in place
                        let priorSeasons = Array(card.seasons.dropFirst())
                        if !priorSeasons.isEmpty {
                            VStack(alignment: .leading, spacing: 0) {
                                ForEach(priorSeasons, id: \.year) { season in
                                    let isExpanded = expandedSeasons.contains(season.year)

                                    Button {
                                        withAnimation(.easeInOut(duration: 0.2)) {
                                            if isExpanded {
                                                expandedSeasons.remove(season.year)
                                            } else {
                                                expandedSeasons.insert(season.year)
                                            }
                                        }
                                    } label: {
                                        HStack(spacing: 6) {
                                            Text("\(String(season.year)) Season")
                                                .font(.system(.headline, design: .rounded, weight: .semibold))
                                                .foregroundStyle(.primary)
                                            Image(systemName: isExpanded ? "chevron.up" : "chevron.down")
                                                .font(.system(size: 11, weight: .semibold))
                                                .foregroundStyle(.secondary)
                                        }
                                        .padding(.horizontal, 20)
                                        .padding(.vertical, 10)
                                    }
                                    .buttonStyle(.plain)

                                    if isExpanded {
                                        VStack(alignment: .leading, spacing: 16) {
                                            VStack(alignment: .leading, spacing: 8) {
                                                if let teamLabel = seasonTeamLabel(teamStr: season.team, headerTeam: card.team) {
                                                    Text(teamLabel)
                                                        .font(.system(.subheadline, design: .rounded))
                                                        .foregroundStyle(.secondary)
                                                        .padding(.horizontal, 20)
                                                }

                                                StatGridView(grid: season.stats)
                                                    .padding(.horizontal, 6)
                                            }

                                            splitsSection(
                                                season: season,
                                                tab: Binding(
                                                    get: { priorSeasonTabs[season.year] ?? .platoon },
                                                    set: { priorSeasonTabs[season.year] = $0 }
                                                )
                                            )
                                        }
                                        .padding(.bottom, 8)
                                    }
                                }
                            }
                        }

                        // Bio
                        if card.bio != nil || card.birthdate != nil {
                            VStack(alignment: .leading, spacing: 8) {
                                Text("About")
                                    .font(.system(.headline, design: .rounded, weight: .semibold))
                                    .foregroundStyle(.primary)
                                    .padding(.horizontal, 20)

                                VStack(alignment: .leading, spacing: 8) {
                                    if let birthdate = card.birthdate {
                                        Text("Born: \(birthdate, format: .dateTime.month(.wide).day().year())")
                                            .font(.system(.subheadline, design: .rounded, weight: .medium))
                                            .foregroundStyle(.primary.opacity(0.9))
                                    }

                                    if let bio = card.bio {
                                        Text(bio)
                                            .font(.system(.body, design: .rounded))
                                            .foregroundStyle(.primary.opacity(0.85))
                                            .lineSpacing(3)
                                    }
                                }
                                .padding(.horizontal, 20)
                                .padding(.vertical, 12)
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .background(
                                    RoundedRectangle(cornerRadius: 12)
                                        .fill(Color(uiColor: .secondarySystemBackground))
                                )
                                .padding(.horizontal, 6)
                            }
                        }
                    }
                    .padding(.top, 16)
                    .padding(.bottom, 24)
                }
            }
        }
        .navigationBarBackButtonHidden(true)
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .principal) {
                Button { dismiss() } label: {
                    HStack(spacing: 6) {
                        Text("StatChat")
                            .font(.system(.subheadline, weight: .semibold))
                            .foregroundStyle(
                                LinearGradient(
                                    colors: [lightBlue, deepBlue],
                                    startPoint: .leading, endPoint: .trailing
                                )
                            )

                        ZStack {
                            Image(systemName: "sparkle")
                                .font(.system(size: 12, weight: .bold))
                                .foregroundStyle(
                                    LinearGradient(
                                        colors: [lightBlue, deepBlue],
                                        startPoint: .topLeading, endPoint: .bottomTrailing
                                    )
                                )

                            Image(systemName: "baseball.fill")
                                .font(.system(size: 6))
                                .foregroundStyle(lightBlue)
                                .offset(x: 7.5, y: -7.5)

                            Image(systemName: "baseball.fill")
                                .font(.system(size: 4.5))
                                .foregroundStyle(lightBlue.opacity(0.7))
                                .offset(x: -6.5, y: -6.5)

                            Image(systemName: "baseball.fill")
                                .font(.system(size: 5))
                                .foregroundStyle(lightBlue.opacity(0.85))
                                .offset(x: 6.5, y: 6.5)
                        }
                    }
                }
            }
        }
        .swipeBack()
        .task {
            playerCard = await PlayerCardService.fetch(name: playerName)
            isLoading = false
        }
    }

    private func sectionView(title: String, grid: StatGridParser.StatGrid, compact: Bool = false) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title)
                .font(.system(.headline, design: .rounded, weight: .semibold))
                .foregroundStyle(.primary)
                .padding(.horizontal, 20)

            StatGridView(grid: grid, compactHeaders: compact ? StatGridView.summaryHeaders : nil)
                .padding(.horizontal, 6)
        }
    }

    @ViewBuilder
    private func splitsSection(
        season: SeasonData,
        tab: Binding<SplitTab>
    ) -> some View {
        let hasData = season.platoonSplits != nil || season.homeAwaySplits != nil || season.streaks != nil || season.fieldingStats != nil

        if hasData {
            VStack(alignment: .leading, spacing: 8) {
                // Segmented control
                HStack(spacing: 0) {
                    ForEach(SplitTab.allCases, id: \.self) { t in
                        let available = tabHasData(t, season: season)
                        Button {
                            if available {
                                withAnimation(.easeInOut(duration: 0.15)) {
                                    tab.wrappedValue = t
                                }
                            }
                        } label: {
                            Text(t.rawValue)
                                .font(.system(.subheadline, design: .rounded, weight: .semibold))
                                .padding(.horizontal, 12)
                                .padding(.vertical, 6)
                                .background(
                                    RoundedRectangle(cornerRadius: 8)
                                        .fill(tab.wrappedValue == t
                                              ? deepBlue.opacity(0.12)
                                              : Color.clear)
                                )
                                .foregroundStyle(tab.wrappedValue == t ? deepBlue : .secondary)
                                .opacity(available ? 1.0 : 0.4)
                        }
                        .disabled(!available)
                    }
                }
                .padding(.horizontal, 8)
                .onAppear {
                    // Auto-select first tab that has data if current selection is empty
                    if !tabHasData(tab.wrappedValue, season: season) {
                        for t in SplitTab.allCases {
                            if tabHasData(t, season: season) {
                                tab.wrappedValue = t
                                break
                            }
                        }
                    }
                }

                // Content for selected tab
                if let grid = gridForTab(tab.wrappedValue, season: season) {
                    StatGridView(
                        grid: grid,
                        seasonGames: tab.wrappedValue == .fielding ? nil : season.games
                    )
                    .padding(.horizontal, 6)
                }
            }
        }
    }

    private func tabHasData(_ tab: SplitTab, season: SeasonData) -> Bool {
        gridForTab(tab, season: season) != nil
    }

    private func gridForTab(_ tab: SplitTab, season: SeasonData) -> StatGridParser.StatGrid? {
        switch tab {
        case .platoon: return season.platoonSplits
        case .homeAway: return season.homeAwaySplits
        case .streaks: return season.streaks
        case .fielding: return season.fieldingStats
        }
    }

    private func currentFormSection(season: SeasonData) -> some View {
        guard let form = season.currentForm else { return AnyView(EmptyView()) }

        // Slider controls "last N games" — convert to game number for recompute
        let numGamesShown = formSliderGameNumber ?? form.numGames
        let effectiveGameNumber = form.totalSeasonGames - numGamesShown + 1
        let (formGrid, formNumGames, formStartDate) = recomputeFormStats(
            season: season, fromGameNumber: effectiveGameNumber
        )

        let formattedDate = PlayerCardService.formatDateShort(formStartDate)

        let hasRemainingGames = season.teamGames < 162
        let availableModes: [FormProjectionMode] = hasRemainingGames
            ? FormProjectionMode.allCases
            : [.pace]

        return AnyView(VStack(alignment: .leading, spacing: 8) {
            Text("Current Hot Streak")
                .font(.system(.subheadline, design: .rounded, weight: .semibold))
                .foregroundStyle(.primary)
                .padding(.horizontal, 20)

            VStack(alignment: .leading, spacing: 12) {
                // Subtitle + slider
                VStack(alignment: .leading, spacing: 6) {
                    Text("Since \(formattedDate) (\(formNumGames) games)")
                        .font(.system(.subheadline, design: .rounded))
                        .foregroundStyle(.secondary)
                        .padding(.horizontal, 14)

                    Slider(
                        value: Binding<Double>(
                            get: { Double(numGamesShown) },
                            set: { newValue in
                                formSliderGameNumber = max(10, min(Int(newValue.rounded()), form.totalSeasonGames))
                            }
                        ),
                        in: 10...Double(max(form.totalSeasonGames, 11)),
                        step: 1
                    )
                    .tint(deepBlue)
                    .padding(.horizontal, 14)
                }
                .onAppear {
                    if gameLogs == nil {
                        gameLogs = PlayerCardService.fetchGameLogsForSeason(
                            name: playerName, season: season.year
                        )
                    }
                }

                StatGridView(grid: formGrid)

                // Collapsible projection
                Rectangle()
                    .fill(Color(uiColor: .separator).opacity(0.3))
                    .frame(height: 1)
                    .padding(.horizontal, 14)

                Button {
                    withAnimation(.easeInOut(duration: 0.2)) {
                        showFormProjection.toggle()
                    }
                } label: {
                    HStack(spacing: 6) {
                        Text("Full Season Projection")
                            .font(.system(.subheadline, design: .rounded, weight: .semibold))
                            .foregroundStyle(.primary)
                        Image(systemName: showFormProjection ? "chevron.up" : "chevron.down")
                            .font(.system(size: 11, weight: .semibold))
                            .foregroundStyle(.secondary)
                    }
                    .padding(.horizontal, 14)
                }
                .buttonStyle(.plain)

                if showFormProjection {
                    VStack(alignment: .leading, spacing: 6) {
                        HStack(spacing: 0) {
                            ForEach(availableModes, id: \.self) { mode in
                                Button {
                                    withAnimation(.easeInOut(duration: 0.15)) {
                                        formProjectionMode = mode
                                    }
                                } label: {
                                    Text(mode.rawValue)
                                        .font(.system(.caption, design: .rounded, weight: .medium))
                                        .padding(.horizontal, 10)
                                        .padding(.vertical, 5)
                                        .background(
                                            RoundedRectangle(cornerRadius: 6)
                                                .fill(formProjectionMode == mode
                                                      ? deepBlue.opacity(0.12)
                                                      : Color.clear)
                                        )
                                        .foregroundStyle(formProjectionMode == mode ? deepBlue : .secondary)
                                }
                            }
                        }
                        .padding(.horizontal, 14)

                        let projectedGrid = buildFormProjection(
                            season: season, formGrid: formGrid, formNumGames: formNumGames,
                            effectiveGameNumber: effectiveGameNumber
                        )
                        StatGridView(grid: projectedGrid)
                    }
                }
            }
            .padding(.vertical, 12)
            .background(
                RoundedRectangle(cornerRadius: 12)
                    .fill(Color(uiColor: .secondarySystemBackground))
            )
            .padding(.horizontal, 6)
        })
    }

    /// Recompute form stats from game logs starting at a given game number.
    /// Falls back to precomputed data if game logs aren't loaded yet.
    private func recomputeFormStats(
        season: SeasonData, fromGameNumber: Int
    ) -> (grid: StatGridParser.StatGrid, numGames: Int, startDate: String) {
        guard let form = season.currentForm else {
            return (StatGridParser.StatGrid(headers: [], rows: []), 0, "")
        }

        // If game logs are loaded and slider has been moved, recompute
        if let logs = gameLogs, !logs.isEmpty {
            let startIdx = max(0, fromGameNumber - 1) // 1-indexed to 0-indexed
            let slice = Array(logs[startIdx...])
            guard !slice.isEmpty else {
                return (form.stats, form.numGames, form.formStartDate)
            }

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
            let grid = StatGridParser.StatGrid(
                headers: headers,
                rows: [StatGridParser.StatGrid.Row(label: "", values: values)]
            )
            return (grid, slice.count, slice.first?.date ?? form.formStartDate)
        }

        // Fall back to precomputed data
        return (form.stats, form.numGames, form.formStartDate)
    }

    /// Build a 162-game projection from current form stats.
    /// - pace: Pure extrapolation — streak stats * 162 / streakGames
    /// - forecast: Pre-streak actuals + remaining games at streak pace (mid-season only)
    private func buildFormProjection(
        season: SeasonData, formGrid: StatGridParser.StatGrid, formNumGames: Int,
        effectiveGameNumber: Int
    ) -> StatGridParser.StatGrid {
        guard formNumGames > 0 else { return formGrid }

        let headers = formGrid.headers
        let values = formGrid.rows.first?.values ?? []
        let countingStats: Set<String> = ["G", "AB", "R", "H", "HR", "RBI", "BB", "SO"]

        // Parse form counting values from the grid
        var formCounting: [String: Double] = [:]
        for (idx, header) in headers.enumerated() {
            if countingStats.contains(header), idx < values.count, let v = Double(values[idx]) {
                formCounting[header] = v
            }
        }

        func fmtRate(_ v: Double) -> String {
            let s = String(format: "%.3f", v)
            return s.hasPrefix("0.") ? String(s.dropFirst()) : s
        }

        func rateStatsFrom(counting: [String: Double]) -> [String: String] {
            let ab = counting["AB"] ?? 0
            let h = counting["H"] ?? 0
            let bb = counting["BB"] ?? 0
            let hr = counting["HR"] ?? 0
            // Estimate 2B/3B from season ratios for SLG
            let seasonAB = season.countingValues["AB"] ?? 1
            let ratio2B = seasonAB > 0 ? (season.countingValues["2B"] ?? 0) / seasonAB : 0
            let ratio3B = seasonAB > 0 ? (season.countingValues["3B"] ?? 0) / seasonAB : 0
            let est2B = ab * ratio2B
            let est3B = ab * ratio3B
            let avg = ab > 0 ? h / ab : 0
            let pa = ab + bb
            let obp = pa > 0 ? (h + bb) / pa : 0
            let tb = (h - est2B - est3B - hr) + 2 * est2B + 3 * est3B + 4 * hr
            let slg = ab > 0 ? tb / ab : 0
            return ["AVG": fmtRate(avg), "OBP": fmtRate(obp),
                    "SLG": fmtRate(slg), "OPS": fmtRate(obp + slg)]
        }

        switch formProjectionMode {
        case .pace:
            // Pure extrapolation: streak stats * 162 / streakGames
            var projected: [String] = []
            for (idx, header) in headers.enumerated() {
                guard idx < values.count else { break }
                if countingStats.contains(header) {
                    let raw = formCounting[header] ?? 0
                    let proj = raw * 162.0 / Double(formNumGames)
                    projected.append(String(Int(proj.rounded())))
                } else {
                    projected.append(values[idx])
                }
            }
            return StatGridParser.StatGrid(
                headers: headers,
                rows: [StatGridParser.StatGrid.Row(label: "", values: projected)]
            )

        case .forecast:
            // Pre-streak actuals + remaining games filled at streak pace
            let remaining = max(0, 162 - season.teamGames)

            // Compute pre-streak stats from game logs (slider-aware)
            var preStreakCounting: [String: Double] = [:]
            if let logs = gameLogs, !logs.isEmpty {
                let preStreakEnd = max(0, effectiveGameNumber - 1) // 1-indexed
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

            // Blended = pre-streak + streak + (remaining / streakGames) * streak
            var blended: [String: Double] = [:]
            for stat in countingStats {
                let pre = preStreakCounting[stat] ?? 0
                let form = formCounting[stat] ?? 0
                blended[stat] = pre + form + (Double(remaining) / Double(formNumGames)) * form
            }

            let rates = rateStatsFrom(counting: blended)
            var projected: [String] = []
            for header in headers {
                if countingStats.contains(header) {
                    projected.append(String(Int((blended[header] ?? 0).rounded())))
                } else if let rate = rates[header] {
                    projected.append(rate)
                } else {
                    projected.append("--")
                }
            }
            return StatGridParser.StatGrid(
                headers: headers,
                rows: [StatGridParser.StatGrid.Row(label: "", values: projected)]
            )
        }
    }

    private func projectedStatsSection(season: SeasonData) -> some View {
        let projected = buildProjectedGrid(season: season)

        return VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline, spacing: 12) {
                Text("Projected")
                    .font(.system(.headline, design: .rounded, weight: .semibold))
                    .foregroundStyle(.primary)

                // Tab toggle
                HStack(spacing: 0) {
                    ForEach(ProjectionMode.allCases, id: \.self) { mode in
                        Button {
                            withAnimation(.easeInOut(duration: 0.15)) {
                                projectionMode = mode
                            }
                        } label: {
                            Text(mode.rawValue)
                                .font(.system(.caption, design: .rounded, weight: .medium))
                                .padding(.horizontal, 10)
                                .padding(.vertical, 5)
                                .background(
                                    RoundedRectangle(cornerRadius: 6)
                                        .fill(projectionMode == mode
                                              ? deepBlue.opacity(0.12)
                                              : Color.clear)
                                )
                                .foregroundStyle(projectionMode == mode ? deepBlue : .secondary)
                        }
                    }
                }
            }
            .padding(.horizontal, 20)

            StatGridView(grid: projected)
                .padding(.horizontal, 6)
        }
    }

    private func buildProjectedGrid(season: SeasonData) -> StatGridParser.StatGrid {
        let countingStats = ["G", "AB", "R", "H", "2B", "3B", "HR", "RBI", "SB", "CS",
                             "BB", "IBB", "SO", "HBP"]

        let divisor: Double
        switch projectionMode {
        case .gamesMissed:
            // Project based on team games played so far
            divisor = Double(season.teamGames)
        case .fullSeason:
            // Project based on player's games played
            divisor = Double(season.games)
        }

        guard divisor > 0 else {
            return season.stats
        }

        let headers = season.stats.headers
        let originalValues = season.stats.rows.first?.values ?? []

        var projected: [String] = []
        for (idx, header) in headers.enumerated() {
            guard idx < originalValues.count else { break }
            let original = originalValues[idx]

            if countingStats.contains(header) {
                // Project counting stat: stat * 162 / divisor
                let raw = season.countingValues[header] ?? 0
                let proj = raw * 162.0 / divisor
                projected.append(String(Int(proj.rounded())))
            } else {
                // Rate stats stay as-is
                projected.append(original)
            }
        }

        return StatGridParser.StatGrid(
            headers: headers,
            rows: [StatGridParser.StatGrid.Row(label: "", values: projected)]
        )
    }

    /// Returns a team label for a season, or nil if the team matches the header (no context needed).
    private func seasonTeamLabel(teamStr: String, headerTeam: String) -> String? {
        let isMultiTeam = teamStr.contains("/")
        let lastTeam = teamStr.split(separator: "/").last.map(String.init) ?? teamStr
        let isDifferentTeam = lastTeam != headerTeam

        if isMultiTeam || isDifferentTeam {
            return PlayerCardService.teamDisplayName(teamStr)
        }
        return nil
    }
}
