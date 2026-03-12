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
    @State private var pitchTypeIndex: Int = 0
    @State private var countSplitIndex: Int = 0
    @State private var priorPitchTypeIndex: [Int: Int] = [:]
    @State private var priorCountSplitIndex: [Int: Int] = [:]
    @State private var selectedTeamCode: String? = nil

    // Two-way player state
    @State private var twoWayTab: Int = 0  // 0 = Batting, 1 = Pitching

    // Pitching-specific state
    @State private var pitchingFormSliderGameNumber: Int? = nil
    @State private var pitchingGameLogs: [PitchingGameLog]? = nil
    @State private var showPitchingFormProjection = false
    @State private var careerStartYear: Int? = nil   // nil = full career
    @State private var careerEndYear: Int? = nil
    @State private var pitchingCareerStartYear: Int? = nil
    @State private var pitchingCareerEndYear: Int? = nil
    @State private var careerRangeExpanded = false
    @State private var pitchingCareerRangeExpanded = false

    // Floating search bar state
    @State private var searchText = ""
    @FocusState private var isSearchFocused: Bool
    @State private var searchSuggestions: [String] = []
    @State private var searchPendingQuery: String?
    @State private var searchPlayerName: String? = nil
    @State private var searchQuestion: String? = nil

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
        case homeAway = "Home\nAway"
        case risp = "RISP"
        case streaks = "Streaks"
        case byPitch = "By Pitch"
        case byCount = "By Count"
    }

    var body: some View {
        ZStack {
            Color(uiColor: .systemBackground)
                .ignoresSafeArea()

            if isLoading {
                LoadingIndicator()
            } else if let card = playerCard {
                VStack(spacing: 0) {
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
                                Button {
                                    if let code = PlayerCardService.teamCodeFromFullName(card.fullTeamName) {
                                        selectedTeamCode = code
                                    }
                                } label: {
                                    Text(card.fullTeamName)
                                        .foregroundStyle(deepBlue)
                                }
                                .buttonStyle(.plain)
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

                        if card.isTwoWay {
                            Picker("", selection: $twoWayTab) {
                                Text("Batting").tag(0)
                                Text("Pitching").tag(1)
                            }
                            .pickerStyle(.segmented)
                            .padding(.horizontal, 20)
                        }

                        if card.isTwoWay, twoWayTab == 1,
                           let pitchingSeasons = card.pitchingSeasons, !pitchingSeasons.isEmpty {
                            pitcherCardContent(card: card, pitchingSeasons: pitchingSeasons)
                        } else if card.isPitcher, let pitchingSeasons = card.pitchingSeasons, !pitchingSeasons.isEmpty {
                            pitcherCardContent(card: card, pitchingSeasons: pitchingSeasons)
                        } else {
                            batterCardContent(card: card)
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
                    .padding(.bottom, 80)
                }
                .scrollDismissesKeyboard(.interactively)

                floatingSearchBar
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
        .navigationDestination(isPresented: Binding(
            get: { selectedTeamCode != nil },
            set: { if !$0 { selectedTeamCode = nil } }
        )) {
            TeamCardView(teamCode: selectedTeamCode ?? "")
        }
        .navigationDestination(isPresented: Binding(
            get: { searchPlayerName != nil },
            set: { if !$0 { searchPlayerName = nil } }
        )) {
            PlayerCardView(playerName: searchPlayerName ?? "")
        }
        .navigationDestination(isPresented: Binding(
            get: { searchQuestion != nil },
            set: { if !$0 { searchQuestion = nil } }
        )) {
            ResultsView(initialQuestion: searchQuestion ?? "")
        }
        .task {
            playerCard = await PlayerCardService.fetch(name: playerName)
            isLoading = false
        }
    }

    // MARK: - Floating search bar

    private var floatingSearchBar: some View {
        VStack(spacing: 0) {
            // Fade gradient so scrolled content doesn't look clipped
            LinearGradient(
                colors: [Color(uiColor: .systemBackground).opacity(0), Color(uiColor: .systemBackground)],
                startPoint: .top, endPoint: .bottom
            )
            .frame(height: 16)

            VStack(spacing: 6) {
                // "Did you mean?" suggestions
                if let query = searchPendingQuery, !searchSuggestions.isEmpty {
                    VStack(spacing: 4) {
                        Text("Did you mean:")
                            .font(.system(.caption, design: .rounded))
                            .foregroundStyle(.secondary)

                        ForEach(searchSuggestions, id: \.self) { name in
                            Button {
                                let n = name
                                withAnimation { searchSuggestions = []; searchPendingQuery = nil }
                                searchText = ""
                                searchPlayerName = n
                            } label: {
                                Text(name)
                                    .font(.system(.subheadline, design: .rounded, weight: .semibold))
                                    .foregroundStyle(deepBlue)
                            }
                        }

                        Button {
                            let q = query
                            withAnimation { searchSuggestions = []; searchPendingQuery = nil }
                            searchText = ""
                            searchQuestion = q
                        } label: {
                            (Text("Or search \"")
                                .foregroundStyle(.secondary)
                             + Text(query)
                                .foregroundStyle(lightBlue)
                             + Text("\"")
                                .foregroundStyle(.secondary))
                                .font(.system(.caption, design: .rounded))
                        }
                    }
                    .padding(.bottom, 6)
                }

                // Search input
                HStack(spacing: 12) {
                    Image(systemName: "magnifyingglass")
                        .font(.system(size: 15, weight: .medium))
                        .foregroundStyle(lightBlue)

                    TextField("", text: $searchText, prompt:
                        Text("Search player stats or ask a question")
                            .foregroundStyle(Color(uiColor: .placeholderText))
                    )
                    .font(.system(.body, design: .rounded))
                    .foregroundStyle(.primary)
                    .focused($isSearchFocused)
                    .onSubmit { submitSearch() }

                    if !searchText.isEmpty {
                        Button(action: submitSearch) {
                            Image(systemName: "arrow.right.circle.fill")
                                .font(.system(size: 22))
                                .foregroundStyle(lightBlue)
                        }
                    }
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 12)
                .background(Color(uiColor: .secondarySystemBackground), in: RoundedRectangle(cornerRadius: 14))
                .overlay(
                    RoundedRectangle(cornerRadius: 14)
                        .stroke(Color(uiColor: .separator).opacity(0.3), lineWidth: 0.5)
                )
                .padding(.horizontal, 16)
            }
            .padding(.bottom, 6)
            .background(Color(uiColor: .systemBackground))
        }
    }

    private func submitSearch() {
        let trimmed = searchText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        searchSuggestions = []
        searchPendingQuery = nil

        // Exact player name match
        if let name = PlayerNameMatcher.matchPlayer(trimmed) {
            searchText = ""
            searchPlayerName = name
            return
        }

        // Exact team name match (case-insensitive)
        if let code = PlayerCardService.teamCodeFromFullNameCaseInsensitive(trimmed) {
            searchText = ""
            selectedTeamCode = code
            return
        }

        // Fuzzy player name match → "Did you mean?"
        let fuzzy = PlayerNameMatcher.fuzzyMatch(trimmed)
        if !fuzzy.isEmpty {
            withAnimation(.easeOut(duration: 0.25)) {
                searchSuggestions = fuzzy
                searchPendingQuery = trimmed
            }
            return
        }

        // Fall through to Claude
        searchText = ""
        searchQuestion = trimmed
    }

    /// Whether a player's most recent season is recent enough to auto-expand (current or previous calendar year).
    private var isRecentPlayer: Bool {
        let currentYear = Calendar.current.component(.year, from: Date())
        let battingYear = playerCard?.seasons.first?.year ?? 0
        let pitchingYear = playerCard?.pitchingSeasons?.first?.year ?? 0
        return max(battingYear, pitchingYear) >= currentYear - 1
    }

    /// Whether projections make sense (only for an in-progress season — current year, team hasn't played 162 yet).
    private func isCurrentSeason(_ season: SeasonData) -> Bool {
        let currentYear = Calendar.current.component(.year, from: Date())
        return season.year == currentYear && season.teamGames < 162
    }

    // MARK: - Batter Card Content

    @ViewBuilder
    private func batterCardContent(card: PlayerCard) -> some View {
        if isRecentPlayer {
            // Recent player: show most recent season expanded with all details
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
                } else if isCurrentSeason(current) {
                    // Projections only for in-progress current season
                    projectedStatsSection(season: current)
                }

                // Unified splits section
                splitsSection(
                    season: current,
                    tab: $splitTab,
                    pitchIdx: $pitchTypeIndex,
                    countIdx: $countSplitIndex
                )

                // Fielding (collapsed)
                fieldingSection(season: current)
            }

            // Career totals + 162-game pace
            careerWithPaceSection(card: card)

            // Career splits
            careerSplitsSection(card: card)

            // Prior seasons — expandable in place
            let priorSeasons = Array(card.seasons.dropFirst())
            expandableSeasonsSection(seasons: priorSeasons, card: card)
        } else {
            // Historical player: career + pace, all seasons collapsible
            careerWithPaceSection(card: card)

            careerSplitsSection(card: card)

            expandableSeasonsSection(seasons: card.seasons, card: card)
        }
    }

    @ViewBuilder
    private func expandableSeasonsSection(seasons: [SeasonData], card: PlayerCard) -> some View {
        let priorSeasons = seasons
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
                                ),
                                pitchIdx: Binding(
                                    get: { priorPitchTypeIndex[season.year] ?? 0 },
                                    set: { priorPitchTypeIndex[season.year] = $0 }
                                ),
                                countIdx: Binding(
                                    get: { priorCountSplitIndex[season.year] ?? 0 },
                                    set: { priorCountSplitIndex[season.year] = $0 }
                                )
                            )

                            fieldingSection(season: season)
                        }
                        .padding(.bottom, 8)
                    }
                }
            }
        }
    }

    // MARK: - Pitcher Card Content

    @ViewBuilder
    private func pitcherCardContent(card: PlayerCard, pitchingSeasons: [PitchingSeasonData]) -> some View {
        if isRecentPlayer {
            // Recent pitcher: show most recent season expanded
            if let current = pitchingSeasons.first {
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

                // Pitching current form section
                if current.currentForm != nil {
                    pitchingFormSection(season: current)
                }

                // Pitching splits section
                pitchingSplitsSection(
                    season: current, tab: $splitTab,
                    pitchIdx: $pitchTypeIndex, countIdx: $countSplitIndex
                )
            }

            // Pitching career totals + 162-game pace
            pitchingCareerWithPaceSection(card: card, pitchingSeasons: pitchingSeasons)

            // Pitching career splits
            pitchingCareerSplitsSection(card: card)

            // Prior pitching seasons — expandable
            expandablePitchingSeasonsSection(seasons: Array(pitchingSeasons.dropFirst()), card: card)
        } else {
            // Historical pitcher: career + pace, all seasons collapsible
            pitchingCareerWithPaceSection(card: card, pitchingSeasons: pitchingSeasons)

            pitchingCareerSplitsSection(card: card)

            expandablePitchingSeasonsSection(seasons: pitchingSeasons, card: card)
        }
    }

    @ViewBuilder
    private func expandablePitchingSeasonsSection(seasons: [PitchingSeasonData], card: PlayerCard) -> some View {
        if !seasons.isEmpty {
            VStack(alignment: .leading, spacing: 0) {
                ForEach(seasons, id: \.year) { season in
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

                            pitchingSplitsSection(
                                season: season,
                                tab: Binding(
                                    get: { priorSeasonTabs[season.year] ?? .platoon },
                                    set: { priorSeasonTabs[season.year] = $0 }
                                ),
                                pitchIdx: Binding(
                                    get: { priorPitchTypeIndex[season.year] ?? 0 },
                                    set: { priorPitchTypeIndex[season.year] = $0 }
                                ),
                                countIdx: Binding(
                                    get: { priorCountSplitIndex[season.year] ?? 0 },
                                    set: { priorCountSplitIndex[season.year] = $0 }
                                )
                            )
                        }
                        .padding(.bottom, 8)
                    }
                }
            }
        }
    }

    // MARK: - Pitching Splits Section

    @ViewBuilder
    private func pitchingSplitsSection(
        season: PitchingSeasonData,
        tab: Binding<SplitTab>,
        pitchIdx: Binding<Int>? = nil,
        countIdx: Binding<Int>? = nil
    ) -> some View {
        let hasData = season.platoonSplits != nil || season.homeAwaySplits != nil || season.streaks != nil
            || (season.pitchTypeSplits != nil && !season.pitchTypeSplits!.isEmpty)
            || (season.countSplits != nil && !season.countSplits!.isEmpty)

        if hasData {
            VStack(alignment: .leading, spacing: 8) {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 0) {
                        ForEach(SplitTab.allCases, id: \.self) { t in
                            let available = pitchingTabHasData(t, season: season)
                            Button {
                                if available {
                                    withAnimation(.easeInOut(duration: 0.15)) {
                                        tab.wrappedValue = t
                                    }
                                }
                            } label: {
                                Text(t.rawValue)
                                    .font(.system(.subheadline, design: .rounded, weight: .semibold))
                                    .multilineTextAlignment(.center)
                                    .padding(.horizontal, 10)
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
                }
                .onAppear {
                    if !pitchingTabHasData(tab.wrappedValue, season: season) {
                        for t in SplitTab.allCases {
                            if pitchingTabHasData(t, season: season) {
                                tab.wrappedValue = t
                                break
                            }
                        }
                    }
                }

                if tab.wrappedValue == .byPitch, let grids = season.pitchTypeSplits, !grids.isEmpty {
                    splitArrayContent(grids: grids, index: pitchIdx ?? $pitchTypeIndex)
                } else if tab.wrappedValue == .byCount, let grids = season.countSplits, !grids.isEmpty {
                    splitArrayContent(grids: grids, index: countIdx ?? $countSplitIndex)
                } else if let grid = pitchingGridForTab(tab.wrappedValue, season: season) {
                    StatGridView(grid: grid, seasonGames: season.games)
                        .padding(.horizontal, 6)
                }
            }
        }
    }

    private func pitchingTabHasData(_ tab: SplitTab, season: PitchingSeasonData) -> Bool {
        switch tab {
        case .byPitch: return season.pitchTypeSplits != nil && !(season.pitchTypeSplits!.isEmpty)
        case .byCount: return season.countSplits != nil && !(season.countSplits!.isEmpty)
        default: return pitchingGridForTab(tab, season: season) != nil
        }
    }

    private func pitchingGridForTab(_ tab: SplitTab, season: PitchingSeasonData) -> StatGridParser.StatGrid? {
        switch tab {
        case .platoon: return season.platoonSplits
        case .homeAway: return season.homeAwaySplits
        case .risp: return season.rispSplits
        case .streaks: return season.streaks
        case .byPitch, .byCount: return nil  // Handled separately with sub-selectors
        }
    }

    // MARK: - Pitching Form Section

    private func pitchingFormSection(season: PitchingSeasonData) -> some View {
        guard let form = season.currentForm else { return AnyView(EmptyView()) }

        let numGamesShown = pitchingFormSliderGameNumber ?? form.numGames
        let effectiveGameNumber = form.totalSeasonGames - numGamesShown + 1
        let (formGrid, formNumGames, formStartDate) = recomputePitchingFormStats(
            season: season, fromGameNumber: effectiveGameNumber
        )

        let formattedDate = PlayerCardService.formatDateShort(formStartDate)

        return AnyView(VStack(alignment: .leading, spacing: 8) {
            Text("Current Hot Streak")
                .font(.system(.subheadline, design: .rounded, weight: .semibold))
                .foregroundStyle(.primary)
                .padding(.horizontal, 20)

            VStack(alignment: .leading, spacing: 12) {
                VStack(alignment: .leading, spacing: 6) {
                    HStack(spacing: 4) {
                        Text("Since \(formattedDate) (\(formNumGames) games)")
                            .font(.system(.subheadline, design: .rounded))
                            .foregroundStyle(.secondary)

                        if pitchingFormSliderGameNumber != nil {
                            Button {
                                withAnimation(.easeInOut(duration: 0.15)) {
                                    pitchingFormSliderGameNumber = nil
                                }
                            } label: {
                                HStack(spacing: 3) {
                                    Image(systemName: "arrow.counterclockwise")
                                        .font(.system(size: 12, weight: .semibold))
                                    Text("Reset")
                                        .font(.system(.caption2, design: .rounded, weight: .medium))
                                }
                                .foregroundStyle(deepBlue.opacity(0.7))
                            }
                            .buttonStyle(.plain)
                            .transition(.opacity)
                        }
                    }
                    .padding(.horizontal, 14)

                    ZStack {
                        Slider(
                            value: Binding<Double>(
                                get: { Double(numGamesShown) },
                                set: { newValue in
                                    pitchingFormSliderGameNumber = max(1, min(Int(newValue.rounded()), form.totalSeasonGames))
                                }
                            ),
                            in: 1...Double(max(form.totalSeasonGames, 2)),
                            step: 1
                        )
                        .tint(deepBlue)

                        if pitchingFormSliderGameNumber != nil {
                            GeometryReader { geo in
                                let range = Double(max(form.totalSeasonGames, 2)) - 1
                                let pct = range > 0 ? (Double(form.numGames) - 1) / range : 0
                                let trackWidth = geo.size.width - 28
                                let xPos = 14 + trackWidth * pct

                                Rectangle()
                                    .fill(deepBlue.opacity(0.5))
                                    .frame(width: 2, height: 14)
                                    .position(x: xPos, y: geo.size.height / 2)
                                    .allowsHitTesting(false)
                            }
                        }
                    }
                    .padding(.horizontal, 14)
                }
                .onAppear {
                    if pitchingGameLogs == nil {
                        pitchingGameLogs = PlayerCardService.fetchPitchingGameLogsForSeason(
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
                        showPitchingFormProjection.toggle()
                    }
                } label: {
                    HStack(spacing: 6) {
                        Text("Full Season Projection")
                            .font(.system(.subheadline, design: .rounded, weight: .semibold))
                            .foregroundStyle(.primary)
                        Image(systemName: showPitchingFormProjection ? "chevron.up" : "chevron.down")
                            .font(.system(size: 11, weight: .semibold))
                            .foregroundStyle(.secondary)
                    }
                    .padding(.horizontal, 14)
                }
                .buttonStyle(.plain)

                if showPitchingFormProjection {
                    let projectedGrid = buildPitchingFormProjection(
                        season: season, formGrid: formGrid, formNumGames: formNumGames
                    )
                    StatGridView(grid: projectedGrid)
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

    private func recomputePitchingFormStats(
        season: PitchingSeasonData, fromGameNumber: Int
    ) -> (grid: StatGridParser.StatGrid, numGames: Int, startDate: String) {
        guard let form = season.currentForm else {
            return (StatGridParser.StatGrid(headers: [], rows: []), 0, "")
        }

        if let logs = pitchingGameLogs, !logs.isEmpty {
            let startIdx = max(0, fromGameNumber - 1)
            let slice = Array(logs[startIdx...])
            guard !slice.isEmpty else {
                return (form.stats, form.numGames, form.formStartDate)
            }

            var totalIPOuts = 0, totalH = 0, totalER = 0, totalBB = 0, totalSO = 0, totalHR = 0
            for g in slice {
                totalIPOuts += g.ipOuts
                totalH += g.hits; totalER += g.earnedRuns
                totalBB += g.walks; totalSO += g.strikeouts; totalHR += g.homeRuns
            }

            let ip = Double(totalIPOuts) / 3.0
            let era = ip > 0 ? 9.0 * Double(totalER) / ip : 0
            let whip = ip > 0 ? Double(totalBB + totalH) / ip : 0
            let k9 = ip > 0 ? 9.0 * Double(totalSO) / ip : 0

            func fmtIP(_ outs: Int) -> String {
                "\(outs / 3).\(outs % 3)"
            }

            let headers = ["G", "IP", "H", "ER", "BB", "SO", "HR", "ERA", "WHIP", "K/9"]
            let values = [
                String(slice.count), fmtIP(totalIPOuts),
                String(totalH), String(totalER), String(totalBB), String(totalSO), String(totalHR),
                String(format: "%.2f", era), String(format: "%.2f", whip), String(format: "%.1f", k9)
            ]
            let grid = StatGridParser.StatGrid(
                headers: headers,
                rows: [StatGridParser.StatGrid.Row(label: "", values: values)]
            )
            return (grid, slice.count, slice.first?.date ?? form.formStartDate)
        }

        return (form.stats, form.numGames, form.formStartDate)
    }

    private func buildPitchingFormProjection(
        season: PitchingSeasonData, formGrid: StatGridParser.StatGrid, formNumGames: Int
    ) -> StatGridParser.StatGrid {
        guard formNumGames > 0 else { return formGrid }

        let headers = formGrid.headers
        let values = formGrid.rows.first?.values ?? []
        // Project counting stats by IP pace to a full-season workload
        // Estimate total IP from form, then scale to ~200 IP (starter) or ~70 IP (reliever)
        let targetGames = season.gamesStarted > season.games / 2 ? 32 : 70 // starter vs reliever
        let scale = Double(targetGames) / Double(formNumGames)

        let countingStats: Set<String> = ["G", "IP", "H", "ER", "BB", "SO", "HR"]

        var projected: [String] = []
        for (idx, header) in headers.enumerated() {
            guard idx < values.count else { break }
            if countingStats.contains(header) {
                if header == "IP" {
                    // Parse IP string, scale ip_outs
                    let parts = values[idx].split(separator: ".")
                    let whole = Int(parts.first ?? "0") ?? 0
                    let frac = parts.count > 1 ? (Int(parts[1]) ?? 0) : 0
                    let outs = whole * 3 + frac
                    let projOuts = Int((Double(outs) * scale).rounded())
                    projected.append("\(projOuts / 3).\(projOuts % 3)")
                } else if let v = Double(values[idx]) {
                    projected.append(String(Int((v * scale).rounded())))
                } else {
                    projected.append(values[idx])
                }
            } else {
                // Rate stats stay as-is
                projected.append(values[idx])
            }
        }

        return StatGridParser.StatGrid(
            headers: headers,
            rows: [StatGridParser.StatGrid.Row(label: "", values: projected)]
        )
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
        tab: Binding<SplitTab>,
        pitchIdx: Binding<Int>? = nil,
        countIdx: Binding<Int>? = nil
    ) -> some View {
        let hasData = season.platoonSplits != nil || season.homeAwaySplits != nil || season.streaks != nil
            || (season.pitchTypeSplits != nil && !season.pitchTypeSplits!.isEmpty)
            || (season.countSplits != nil && !season.countSplits!.isEmpty)

        if hasData {
            VStack(alignment: .leading, spacing: 8) {
                // Scrollable tab bar
                ScrollView(.horizontal, showsIndicators: false) {
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
                                    .multilineTextAlignment(.center)
                                    .padding(.horizontal, 10)
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
                }
                .onAppear {
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
                if tab.wrappedValue == .byPitch, let grids = season.pitchTypeSplits, !grids.isEmpty {
                    splitArrayContent(grids: grids, index: pitchIdx ?? $pitchTypeIndex)
                } else if tab.wrappedValue == .byCount, let grids = season.countSplits, !grids.isEmpty {
                    splitArrayContent(grids: grids, index: countIdx ?? $countSplitIndex)
                } else if let grid = gridForTab(tab.wrappedValue, season: season) {
                    StatGridView(
                        grid: grid,
                        seasonGames: season.games
                    )
                    .padding(.horizontal, 6)
                }
            }
        }
    }

    @ViewBuilder
    private func splitArrayContent(grids: [StatGridParser.StatGrid], index: Binding<Int>) -> some View {
        let safeIndex = min(max(index.wrappedValue, 0), grids.count - 1)
        let grid = grids[safeIndex]

        VStack(alignment: .leading, spacing: 8) {
            ScrollViewReader { proxy in
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 16) {
                        ForEach(0..<grids.count, id: \.self) { i in
                            let label = grids[i].rows.first?.label ?? ""
                            Button {
                                withAnimation(.easeInOut(duration: 0.15)) {
                                    index.wrappedValue = i
                                }
                            } label: {
                                VStack(spacing: 4) {
                                    Text(label)
                                        .font(.system(.subheadline, design: .rounded, weight: i == safeIndex ? .semibold : .regular))
                                        .foregroundStyle(i == safeIndex ? deepBlue : .secondary)
                                    Rectangle()
                                        .fill(i == safeIndex ? deepBlue : Color.clear)
                                        .frame(height: 2)
                                }
                            }
                            .id(i)
                        }
                    }
                    .padding(.horizontal, 20)
                }
                .onChange(of: index.wrappedValue) { _, newVal in
                    withAnimation { proxy.scrollTo(newVal, anchor: .center) }
                }
            }

            StatGridView(grid: grid)
                .padding(.horizontal, 6)
        }
    }

    @ViewBuilder
    private func fieldingSection(season: SeasonData) -> some View {
        if let grid = season.fieldingStats {
            DisclosureGroup {
                StatGridView(grid: grid)
                    .padding(.horizontal, 6)
                    .padding(.top, 4)
            } label: {
                Text("Fielding")
                    .font(.system(.subheadline, design: .rounded, weight: .semibold))
                    .foregroundStyle(.secondary)
            }
            .padding(.horizontal, 16)
        }
    }

    @ViewBuilder
    private func careerSplitsSection(card: PlayerCard) -> some View {
        let hasPlatoon = card.careerPlatoonSplits != nil
        let hasHomeAway = card.careerHomeAwaySplits != nil

        if hasPlatoon || hasHomeAway {
            VStack(alignment: .leading, spacing: 8) {
                Text("Career Splits")
                    .font(.system(.headline, design: .rounded, weight: .semibold))
                    .padding(.horizontal, 20)
                    .padding(.top, 4)

                if let grid = card.careerPlatoonSplits {
                    Text("Platoon")
                        .font(.system(.subheadline, design: .rounded, weight: .medium))
                        .foregroundStyle(.secondary)
                        .padding(.horizontal, 20)
                    StatGridView(grid: grid)
                        .padding(.horizontal, 6)
                }

                if let grid = card.careerHomeAwaySplits {
                    Text("Home / Away")
                        .font(.system(.subheadline, design: .rounded, weight: .medium))
                        .foregroundStyle(.secondary)
                        .padding(.horizontal, 20)
                    StatGridView(grid: grid)
                        .padding(.horizontal, 6)
                }
            }
        }
    }

    @ViewBuilder
    private func pitchingCareerSplitsSection(card: PlayerCard) -> some View {
        let hasPlatoon = card.pitchingCareerPlatoonSplits != nil
        let hasHomeAway = card.pitchingCareerHomeAwaySplits != nil

        if hasPlatoon || hasHomeAway {
            VStack(alignment: .leading, spacing: 8) {
                Text("Career Splits")
                    .font(.system(.headline, design: .rounded, weight: .semibold))
                    .padding(.horizontal, 20)
                    .padding(.top, 4)

                if let grid = card.pitchingCareerPlatoonSplits {
                    Text("Platoon")
                        .font(.system(.subheadline, design: .rounded, weight: .medium))
                        .foregroundStyle(.secondary)
                        .padding(.horizontal, 20)
                    StatGridView(grid: grid)
                        .padding(.horizontal, 6)
                }

                if let grid = card.pitchingCareerHomeAwaySplits {
                    Text("Home / Away")
                        .font(.system(.subheadline, design: .rounded, weight: .medium))
                        .foregroundStyle(.secondary)
                        .padding(.horizontal, 20)
                    StatGridView(grid: grid)
                        .padding(.horizontal, 6)
                }
            }
        }
    }

    private func tabHasData(_ tab: SplitTab, season: SeasonData) -> Bool {
        switch tab {
        case .byPitch: return season.pitchTypeSplits != nil && !(season.pitchTypeSplits!.isEmpty)
        case .byCount: return season.countSplits != nil && !(season.countSplits!.isEmpty)
        default: return gridForTab(tab, season: season) != nil
        }
    }

    private func gridForTab(_ tab: SplitTab, season: SeasonData) -> StatGridParser.StatGrid? {
        switch tab {
        case .platoon: return season.platoonSplits
        case .homeAway: return season.homeAwaySplits
        case .risp: return season.rispSplits
        case .streaks: return season.streaks
        case .byPitch, .byCount: return nil  // Handled separately with sub-selectors
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
                    HStack(spacing: 4) {
                        Text("Since \(formattedDate) (\(formNumGames) games)")
                            .font(.system(.subheadline, design: .rounded))
                            .foregroundStyle(.secondary)

                        if formSliderGameNumber != nil {
                            Button {
                                withAnimation(.easeInOut(duration: 0.15)) {
                                    formSliderGameNumber = nil
                                }
                            } label: {
                                HStack(spacing: 3) {
                                    Image(systemName: "arrow.counterclockwise")
                                        .font(.system(size: 12, weight: .semibold))
                                    Text("Reset")
                                        .font(.system(.caption2, design: .rounded, weight: .medium))
                                }
                                .foregroundStyle(deepBlue.opacity(0.7))
                            }
                            .buttonStyle(.plain)
                            .transition(.opacity)
                        }
                    }
                    .padding(.horizontal, 14)

                    ZStack {
                        Slider(
                            value: Binding<Double>(
                                get: { Double(numGamesShown) },
                                set: { newValue in
                                    formSliderGameNumber = max(1, min(Int(newValue.rounded()), form.totalSeasonGames))
                                }
                            ),
                            in: 1...Double(max(form.totalSeasonGames, 2)),
                            step: 1
                        )
                        .tint(deepBlue)

                        // Tick mark at detected position — only visible when slider has moved away
                        if formSliderGameNumber != nil {
                            GeometryReader { geo in
                                let range = Double(max(form.totalSeasonGames, 2)) - 1
                                let pct = range > 0 ? (Double(form.numGames) - 1) / range : 0
                                let trackWidth = geo.size.width - 28
                                let xPos = 14 + trackWidth * pct

                                Rectangle()
                                    .fill(deepBlue.opacity(0.5))
                                    .frame(width: 2, height: 14)
                                    .position(x: xPos, y: geo.size.height / 2)
                                    .allowsHitTesting(false)
                            }
                        }
                    }
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

    // MARK: - Career + 162-Game Pace (combined container)

    @ViewBuilder
    private func careerWithPaceSection(card: PlayerCard) -> some View {
        let seasons = card.seasons
        if let career = card.careerTotals {
            if seasons.count > 1 {
                let years = seasons.map(\.year).sorted()
                let minYear = years.first!
                let maxYear = years.last!
                let startYear = careerStartYear ?? minYear
                let endYear = careerEndYear ?? maxYear
                let isFullCareer = careerStartYear == nil && careerEndYear == nil
                let sourceGrid: StatGridParser.StatGrid? = isFullCareer
                    ? career
                    : buildBattingTotalsFromSeasons(seasons.filter { $0.year >= startYear && $0.year <= endYear })

                VStack(alignment: .leading, spacing: 8) {
                    Text("Career")
                        .font(.system(.headline, design: .rounded, weight: .semibold))
                        .foregroundStyle(.primary)
                        .padding(.horizontal, 20)

                    VStack(alignment: .leading, spacing: 8) {
                        StatGridView(grid: career, suppressBackground: true)

                        if let sourceGrid {
                            let games = extractGames(from: sourceGrid)
                            if games > 0 {
                                let projected = buildCareerProjectedGrid(career: sourceGrid)

                                Rectangle()
                                    .fill(Color(uiColor: .separator).opacity(0.3))
                                    .frame(height: 1)
                                    .padding(.horizontal, 14)

                                paceHeaderWithDropdown(
                                    years: years,
                                    startYear: startYear, endYear: endYear,
                                    minYear: minYear, maxYear: maxYear,
                                    isExpanded: $careerRangeExpanded,
                                    onStartChange: { y in
                                        withAnimation(.easeInOut(duration: 0.15)) {
                                            careerStartYear = y == minYear && (careerEndYear == nil || careerEndYear == maxYear) ? nil : y
                                            if y > endYear { careerEndYear = y }
                                        }
                                    },
                                    onEndChange: { y in
                                        withAnimation(.easeInOut(duration: 0.15)) {
                                            careerEndYear = y == maxYear && (careerStartYear == nil || careerStartYear == minYear) ? nil : y
                                            if y < startYear { careerStartYear = y }
                                        }
                                    },
                                    onReset: {
                                        withAnimation(.easeInOut(duration: 0.15)) {
                                            careerStartYear = nil
                                            careerEndYear = nil
                                            careerRangeExpanded = false
                                        }
                                    }
                                )

                                StatGridView(grid: projected, suppressBackground: true)
                            }
                        }
                    }
                    .padding(.vertical, 12)
                    .background(
                        RoundedRectangle(cornerRadius: 12)
                            .fill(Color(uiColor: .secondarySystemBackground))
                    )
                    .padding(.horizontal, 6)
                }
            } else {
                sectionView(title: "Career", grid: career)
            }
        }
    }

    @ViewBuilder
    private func pitchingCareerWithPaceSection(card: PlayerCard, pitchingSeasons: [PitchingSeasonData]) -> some View {
        if let career = card.pitchingCareerTotals {
            if pitchingSeasons.count > 1 {
                let years = pitchingSeasons.map(\.year).sorted()
                let minYear = years.first!
                let maxYear = years.last!
                let startYear = pitchingCareerStartYear ?? minYear
                let endYear = pitchingCareerEndYear ?? maxYear
                let isFullCareer = pitchingCareerStartYear == nil && pitchingCareerEndYear == nil
                let sourceGrid: StatGridParser.StatGrid? = isFullCareer
                    ? career
                    : buildPitchingTotalsFromSeasons(pitchingSeasons.filter { $0.year >= startYear && $0.year <= endYear })

                VStack(alignment: .leading, spacing: 8) {
                    Text("Career")
                        .font(.system(.headline, design: .rounded, weight: .semibold))
                        .foregroundStyle(.primary)
                        .padding(.horizontal, 20)

                    VStack(alignment: .leading, spacing: 8) {
                        StatGridView(grid: career, suppressBackground: true)

                        if let sourceGrid {
                            let games = extractGames(from: sourceGrid)
                            if games > 0 {
                                let projected = buildPitchingCareerProjectedGrid(career: sourceGrid)

                                Rectangle()
                                    .fill(Color(uiColor: .separator).opacity(0.3))
                                    .frame(height: 1)
                                    .padding(.horizontal, 14)

                                paceHeaderWithDropdown(
                                    years: years,
                                    startYear: startYear, endYear: endYear,
                                    minYear: minYear, maxYear: maxYear,
                                    isExpanded: $pitchingCareerRangeExpanded,
                                    onStartChange: { y in
                                        withAnimation(.easeInOut(duration: 0.15)) {
                                            pitchingCareerStartYear = y == minYear && (pitchingCareerEndYear == nil || pitchingCareerEndYear == maxYear) ? nil : y
                                            if y > endYear { pitchingCareerEndYear = y }
                                        }
                                    },
                                    onEndChange: { y in
                                        withAnimation(.easeInOut(duration: 0.15)) {
                                            pitchingCareerEndYear = y == maxYear && (pitchingCareerStartYear == nil || pitchingCareerStartYear == minYear) ? nil : y
                                            if y < startYear { pitchingCareerStartYear = y }
                                        }
                                    },
                                    onReset: {
                                        withAnimation(.easeInOut(duration: 0.15)) {
                                            pitchingCareerStartYear = nil
                                            pitchingCareerEndYear = nil
                                            pitchingCareerRangeExpanded = false
                                        }
                                    }
                                )

                                StatGridView(grid: projected, suppressBackground: true)
                            }
                        }
                    }
                    .padding(.vertical, 12)
                    .background(
                        RoundedRectangle(cornerRadius: 12)
                            .fill(Color(uiColor: .secondarySystemBackground))
                    )
                    .padding(.horizontal, 6)
                }
            } else {
                sectionView(title: "Career", grid: career)
            }
        }
    }

    // MARK: - Pace Header with Inline Dropdown

    @ViewBuilder
    private func paceHeaderWithDropdown(
        years: [Int],
        startYear: Int, endYear: Int,
        minYear: Int, maxYear: Int,
        isExpanded: Binding<Bool>,
        onStartChange: @escaping (Int) -> Void,
        onEndChange: @escaping (Int) -> Void,
        onReset: @escaping () -> Void
    ) -> some View {
        let isFullRange = startYear == minYear && endYear == maxYear
        let yearDesc = isFullRange
            ? "\(minYear)-\(maxYear)"
            : (startYear == endYear ? "\(startYear)" : "\(startYear)-\(endYear)")

        // Header line with inline dropdown
        HStack(alignment: .center, spacing: 8) {
            Text("162-Game Pace")
                .font(.system(.subheadline, design: .rounded, weight: .semibold))
                .foregroundStyle(.primary)

            HStack(alignment: .center, spacing: 5) {
                Text("Based on:")
                    .font(.system(.caption2, design: .rounded))
                    .foregroundStyle(.secondary)

                Button {
                    withAnimation(.easeInOut(duration: 0.2)) {
                        isExpanded.wrappedValue.toggle()
                    }
                } label: {
                    HStack(spacing: 5) {
                        Text(yearDesc)
                            .font(.system(.caption, design: .rounded, weight: .semibold))
                        Image(systemName: isExpanded.wrappedValue ? "chevron.up" : "chevron.down")
                            .font(.system(size: 9, weight: .bold))
                    }
                    .foregroundStyle(.white)
                    .padding(.horizontal, 10)
                    .padding(.vertical, 5)
                    .background(
                        LinearGradient(
                            colors: [lightBlue, deepBlue],
                            startPoint: .leading, endPoint: .trailing
                        ),
                        in: Capsule()
                    )
                }
                .buttonStyle(.plain)
            }
            .offset(y: 1.5)

            Spacer()
        }
        .padding(.horizontal, 14)
        .overlay(alignment: .topLeading) {
            if isExpanded.wrappedValue {
                // Dismiss tap area behind the floating picker
                Color.black.opacity(0.001)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .fixedSize(horizontal: false, vertical: false)
                    .onTapGesture {
                        withAnimation(.easeInOut(duration: 0.2)) {
                            isExpanded.wrappedValue = false
                        }
                    }
                    .ignoresSafeArea()

                VStack(spacing: 0) {
                    HStack {
                        Text("From")
                            .font(.system(.subheadline, design: .rounded))
                            .foregroundStyle(.secondary)
                        Spacer()
                        Menu {
                            ForEach(years.filter { $0 <= endYear }, id: \.self) { year in
                                Button {
                                    onStartChange(year)
                                } label: {
                                    if year == startYear {
                                        Label(String(year), systemImage: "checkmark")
                                    } else {
                                        Text(String(year))
                                    }
                                }
                            }
                        } label: {
                            HStack(spacing: 4) {
                                Text(String(startYear))
                                    .font(.system(.subheadline, design: .rounded, weight: .semibold))
                                Image(systemName: "chevron.up.chevron.down")
                                    .font(.system(size: 10, weight: .semibold))
                            }
                            .foregroundStyle(deepBlue)
                        }
                    }
                    .padding(.horizontal, 14)
                    .padding(.vertical, 8)

                    Divider().padding(.horizontal, 14)

                    HStack {
                        Text("To")
                            .font(.system(.subheadline, design: .rounded))
                            .foregroundStyle(.secondary)
                        Spacer()
                        Menu {
                            ForEach(years.filter { $0 >= startYear }, id: \.self) { year in
                                Button {
                                    onEndChange(year)
                                } label: {
                                    if year == endYear {
                                        Label(String(year), systemImage: "checkmark")
                                    } else {
                                        Text(String(year))
                                    }
                                }
                            }
                        } label: {
                            HStack(spacing: 4) {
                                Text(String(endYear))
                                    .font(.system(.subheadline, design: .rounded, weight: .semibold))
                                Image(systemName: "chevron.up.chevron.down")
                                    .font(.system(size: 10, weight: .semibold))
                            }
                            .foregroundStyle(deepBlue)
                        }
                    }
                    .padding(.horizontal, 14)
                    .padding(.vertical, 8)

                    if !isFullRange {
                        Divider().padding(.horizontal, 14)

                        Button {
                            onReset()
                        } label: {
                            HStack(spacing: 4) {
                                Image(systemName: "arrow.counterclockwise")
                                    .font(.system(size: 11, weight: .semibold))
                                Text("All Seasons")
                                    .font(.system(.caption, design: .rounded, weight: .medium))
                            }
                            .foregroundStyle(deepBlue.opacity(0.7))
                            .frame(maxWidth: .infinity)
                            .padding(.vertical, 6)
                        }
                        .buttonStyle(.plain)
                    }
                }
                .background(
                    RoundedRectangle(cornerRadius: 8)
                        .fill(Color(uiColor: .tertiarySystemBackground))
                        .shadow(color: .black.opacity(0.15), radius: 8, y: 4)
                )
                .overlay(
                    RoundedRectangle(cornerRadius: 8)
                        .stroke(Color(uiColor: .separator).opacity(0.3), lineWidth: 0.5)
                )
                .padding(.horizontal, 14)
                .offset(y: 32)
                .zIndex(10)
                .transition(.opacity.combined(with: .scale(scale: 0.95, anchor: .topLeading)))
            }
        }
        .zIndex(isExpanded.wrappedValue ? 10 : 0)
    }

    // MARK: - Build totals from season subsets

    private func buildBattingTotalsFromSeasons(_ seasons: [SeasonData]) -> StatGridParser.StatGrid? {
        guard !seasons.isEmpty else { return nil }
        let countingKeys = ["G", "AB", "R", "H", "2B", "3B", "HR", "RBI", "SB", "CS", "BB", "IBB", "SO", "HBP"]
        var totals: [String: Double] = [:]
        for s in seasons {
            for key in countingKeys {
                totals[key, default: 0] += s.countingValues[key] ?? 0
            }
        }
        let ab = totals["AB"] ?? 0
        let h = totals["H"] ?? 0
        let bb = totals["BB"] ?? 0
        let hbp = totals["HBP"] ?? 0
        let pa = ab + bb + hbp
        let singles = h - (totals["2B"] ?? 0) - (totals["3B"] ?? 0) - (totals["HR"] ?? 0)
        let tb = singles + 2 * (totals["2B"] ?? 0) + 3 * (totals["3B"] ?? 0) + 4 * (totals["HR"] ?? 0)

        let avg = ab > 0 ? h / ab : 0
        let obp = pa > 0 ? (h + bb + hbp) / pa : 0
        let slg = ab > 0 ? tb / ab : 0
        let ops = obp + slg
        let iso = slg - avg
        let babipDenom = ab - (totals["SO"] ?? 0) - (totals["HR"] ?? 0)
        let babip = babipDenom > 0 ? (h - (totals["HR"] ?? 0)) / babipDenom : 0

        let values = [
            "\(Int(totals["G"] ?? 0))", "\(Int(ab))", "\(Int(totals["R"] ?? 0))",
            "\(Int(h))", "\(Int(totals["2B"] ?? 0))", "\(Int(totals["3B"] ?? 0))",
            "\(Int(totals["HR"] ?? 0))", "\(Int(totals["RBI"] ?? 0))",
            "\(Int(totals["SB"] ?? 0))", "\(Int(totals["CS"] ?? 0))",
            "\(Int(bb))", "\(Int(totals["IBB"] ?? 0))",
            "\(Int(totals["SO"] ?? 0))", "\(Int(hbp))",
            formatRate(avg), formatRate(obp), formatRate(slg), formatRate(ops),
            "--", formatRate(iso), formatRate(babip),
        ]
        let headers = ["G", "AB", "R", "H", "2B", "3B", "HR", "RBI", "SB", "CS",
                        "BB", "IBB", "SO", "HBP", "AVG", "OBP", "SLG", "OPS", "OPS+", "ISO", "BABIP"]
        return StatGridParser.StatGrid(
            headers: headers,
            rows: [StatGridParser.StatGrid.Row(label: "", values: values)]
        )
    }

    private func buildPitchingTotalsFromSeasons(_ seasons: [PitchingSeasonData]) -> StatGridParser.StatGrid? {
        guard !seasons.isEmpty else { return nil }
        var w = 0, l = 0, sv = 0, g = 0, gs = 0, cg = 0, qs = 0
        var h = 0, r = 0, er = 0, hr = 0, bb = 0, so = 0, hbp = 0, wp = 0, bk = 0
        var totalIPOuts = 0.0

        for s in seasons {
            w += Int(s.countingValues["W"] ?? 0)
            l += Int(s.countingValues["L"] ?? 0)
            sv += Int(s.countingValues["SV"] ?? 0)
            g += s.games
            gs += s.gamesStarted
            so += Int(s.countingValues["SO"] ?? 0)
            bb += Int(s.countingValues["BB"] ?? 0)
            h += Int(s.countingValues["H"] ?? 0)
            er += Int(s.countingValues["ER"] ?? 0)
            hr += Int(s.countingValues["HR"] ?? 0)
            hbp += Int(s.countingValues["HBP"] ?? 0)
            wp += Int(s.countingValues["WP"] ?? 0)
            bk += Int(s.countingValues["BK"] ?? 0)
            cg += Int(s.countingValues["CG"] ?? 0)
            qs += Int(s.countingValues["QS"] ?? 0)
            if let ipVal = s.countingValues["IP"] {
                let whole = Int(ipVal)
                let frac = ipVal - Double(whole)
                totalIPOuts += Double(whole * 3) + (frac * 10).rounded()
            }
        }

        let ip = totalIPOuts / 3.0
        let ipDisplay = "\(Int(totalIPOuts) / 3).\(Int(totalIPOuts) % 3)"
        let era = ip > 0 ? 9.0 * Double(er) / ip : 0
        let whip = ip > 0 ? Double(bb + h) / ip : 0
        let k9 = ip > 0 ? 9.0 * Double(so) / ip : 0
        let bb9 = ip > 0 ? 9.0 * Double(bb) / ip : 0
        let h9 = ip > 0 ? 9.0 * Double(h) / ip : 0
        let hr9 = ip > 0 ? 9.0 * Double(hr) / ip : 0

        // Use the display headers (filtered set matching what PlayerCardService uses)
        let headers = ["W", "L", "SV", "G", "GS", "CG", "QS", "IP", "H", "R", "ER",
                        "HR", "BB", "SO", "HBP", "WP", "BK", "SB", "CS",
                        "ERA", "WHIP", "K/9", "BB/9", "H/9", "HR/9", "BAA", "ERA+"]
        let values = [
            "\(w)", "\(l)", "\(sv)", "\(g)", "\(gs)",
            "\(cg)", "\(qs)", ipDisplay, "\(h)", "\(r)", "\(er)",
            "\(hr)", "\(bb)", "\(so)", "\(hbp)", "\(wp)",
            "\(bk)", "0", "0",
            String(format: "%.2f", era), String(format: "%.2f", whip),
            String(format: "%.1f", k9), String(format: "%.1f", bb9),
            String(format: "%.1f", h9), String(format: "%.1f", hr9), ".000", "--",
        ]
        return StatGridParser.StatGrid(
            headers: headers,
            rows: [StatGridParser.StatGrid.Row(label: "", values: values)]
        )
    }

    private func formatRate(_ value: Double) -> String {
        let str = String(format: "%.3f", value)
        if str.hasPrefix("0.") { return String(str.dropFirst()) }
        if str.hasPrefix("-0.") { return "-" + String(str.dropFirst(2)) }
        return str
    }

    // MARK: - Career projection helpers

    private func extractGames(from grid: StatGridParser.StatGrid) -> Double {
        guard let gIdx = grid.headers.firstIndex(of: "G"),
              let values = grid.rows.first?.values,
              gIdx < values.count,
              let g = Double(values[gIdx]) else { return 0 }
        return g
    }

    private func buildCareerProjectedGrid(career: StatGridParser.StatGrid) -> StatGridParser.StatGrid {
        let countingStats: Set<String> = ["G", "AB", "R", "H", "2B", "3B", "HR", "RBI", "SB", "CS",
                                           "BB", "IBB", "SO", "HBP"]
        let games = extractGames(from: career)
        guard games > 0 else { return career }
        let factor = 162.0 / games

        let headers = career.headers
        let originalValues = career.rows.first?.values ?? []

        var projected: [String] = []
        for (idx, header) in headers.enumerated() {
            guard idx < originalValues.count else { break }
            let original = originalValues[idx]

            if countingStats.contains(header), let val = Double(original) {
                projected.append(String(Int((val * factor).rounded())))
            } else {
                projected.append(original)
            }
        }

        return StatGridParser.StatGrid(
            headers: headers,
            rows: [StatGridParser.StatGrid.Row(label: "", values: projected)]
        )
    }

    private func buildPitchingCareerProjectedGrid(career: StatGridParser.StatGrid) -> StatGridParser.StatGrid {
        let countingStats: Set<String> = ["W", "L", "SV", "G", "GS", "CG", "QS", "H", "R", "ER",
                                           "HR", "BB", "SO", "HBP", "WP", "BK", "SB", "CS"]
        let games = extractGames(from: career)
        guard games > 0 else { return career }
        let factor = 162.0 / games

        let headers = career.headers
        let originalValues = career.rows.first?.values ?? []

        var projected: [String] = []
        for (idx, header) in headers.enumerated() {
            guard idx < originalValues.count else { break }
            let original = originalValues[idx]

            if header == "IP" {
                let parts = original.split(separator: ".")
                let whole = Int(parts[0]) ?? 0
                let thirds = parts.count > 1 ? (Int(parts[1]) ?? 0) : 0
                let totalOuts = Double(whole * 3 + thirds) * factor
                let projWhole = Int(totalOuts) / 3
                let projThirds = Int(totalOuts) % 3
                projected.append("\(projWhole).\(projThirds)")
            } else if countingStats.contains(header), let val = Double(original) {
                projected.append(String(Int((val * factor).rounded())))
            } else {
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
