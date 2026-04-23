import SwiftUI

struct TeamCardView: View {
    let teamCode: String
    @Binding var navigationPath: NavigationPath

    @Environment(AppState.self) private var appState
    @Environment(\.dismiss) private var dismiss
    @State private var teamCard: TeamCard?
    @State private var isLoading = true
    @State private var expandedSeasons: Set<Int> = []
    @State private var showAllSeasons = false
    @State private var selectedPlayerName: String? = nil
    /// Tracks visible leader count per season-category (e.g. "2025-HR" → 10)
    @State private var leaderVisibleCounts: [String: Int] = [:]

    // Floating search bar state
    @State private var searchText = ""
    @FocusState private var isSearchFocused: Bool
    @State private var searchTeamCode: String? = nil
    @State private var searchQuestion: String? = nil
    @State private var searchInputMethod: String = "keyboard"
    @State private var voice = VoiceInputService()
    @State private var voiceUsedThisQuery: Bool = false

    private let deepBlue = Color.brandDeepBlue
    private let lightBlue = Color(red: 0.45, green: 0.7, blue: 1.0)

    /// Max prior seasons shown before "Show all" button
    private let maxPriorSeasons = 5

    var body: some View {
        ZStack {
            Color(uiColor: .systemBackground)
                .ignoresSafeArea()

            if isLoading {
                LoadingIndicator()
            } else if let card = teamCard {
                VStack(spacing: 0) {
                ScrollView {
                    VStack(alignment: .leading, spacing: 24) {
                        // Header
                        HStack(alignment: .top, spacing: 10) {
                            Button(action: { dismiss() }) {
                                Image(systemName: "chevron.left")
                                    .font(.system(size: 22, weight: .medium))
                                    .foregroundStyle(lightBlue)
                            }
                            .padding(.top, 2)

                            Text(card.fullName)
                                .font(.system(.title2, design: .rounded, weight: .bold))
                                .foregroundStyle(.primary)
                        }
                        .padding(.horizontal, 20)

                        // Current season (most recent)
                        if let current = card.seasons.first {
                            Text("\(String(current.year)) Season")
                                .font(.system(.headline, design: .rounded, weight: .bold))
                                .foregroundStyle(.primary)
                                .padding(.horizontal, 20)

                            seasonSection(season: current, expanded: true)
                        }

                        // Prior seasons
                        let priorSeasons = Array(card.seasons.dropFirst())
                        if !priorSeasons.isEmpty {
                            let visiblePrior = showAllSeasons ? priorSeasons : Array(priorSeasons.prefix(maxPriorSeasons))

                            VStack(alignment: .leading, spacing: 0) {
                                ForEach(visiblePrior, id: \.year) { season in
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
                                        seasonSection(season: season, expanded: true)
                                            .padding(.bottom, 8)
                                    }
                                }

                                // "Show all N seasons" button
                                if !showAllSeasons && priorSeasons.count > maxPriorSeasons {
                                    Button {
                                        withAnimation(.easeInOut(duration: 0.2)) {
                                            showAllSeasons = true
                                        }
                                    } label: {
                                        Text("Show all \(priorSeasons.count) seasons")
                                            .font(.system(.subheadline, design: .rounded, weight: .medium))
                                            .foregroundStyle(deepBlue)
                                            .padding(.horizontal, 20)
                                            .padding(.vertical, 10)
                                    }
                                    .buttonStyle(.plain)
                                }
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
                Button {
                    UIApplication.shared.sendAction(#selector(UIResponder.resignFirstResponder), to: nil, from: nil, for: nil)
                    navigationPath = NavigationPath()
                } label: {
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
            get: { selectedPlayerName != nil },
            set: { if !$0 { selectedPlayerName = nil } }
        )) {
            PlayerCardView(playerName: selectedPlayerName ?? "", navigationPath: $navigationPath)
        }
        .navigationDestination(isPresented: Binding(
            get: { searchTeamCode != nil },
            set: { if !$0 { searchTeamCode = nil } }
        )) {
            TeamCardView(teamCode: searchTeamCode ?? "", navigationPath: $navigationPath)
        }
        .navigationDestination(isPresented: Binding(
            get: { searchQuestion != nil },
            set: { if !$0 { searchQuestion = nil } }
        )) {
            ResultsView(initialQuestion: searchQuestion ?? "", initialInputMethod: searchInputMethod, navigationPath: $navigationPath)
        }
        .task {
            AnalyticsService.trackTeamCardView(code: teamCode)
            teamCard = await PlayerCardService.fetchTeamCard(teamCode: teamCode)
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
                InlineSearchBar(
                    text: $searchText,
                    placeholder: "Ask any baseball stat question",
                    isFocused: $isSearchFocused,
                    voice: voice,
                    voiceUsedThisQuery: $voiceUsedThisQuery,
                    onSubmit: submitSearch,
                    tint: lightBlue,
                    deepBlue: deepBlue
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
        if voice.isRecording { voice.stopRecording() }
        let inputMethod = voiceUsedThisQuery ? "mic" : "keyboard"
        voiceUsedThisQuery = false
        searchText = ""

        switch PlayerNameMatcher.resolveSearch(trimmed, history: appState) {
        case .player(let name, _):
            selectedPlayerName = name
        case .team(let code):
            searchTeamCode = code
        case .question(let query):
            searchInputMethod = inputMethod
            searchQuestion = query
        }
    }

    // MARK: - Season section (stats + leaders + roster)

    @ViewBuilder
    private func seasonSection(season: TeamSeasonData, expanded: Bool) -> some View {
        VStack(alignment: .leading, spacing: 16) {
            // Team stats grids (batting + pitching)
            VStack(alignment: .leading, spacing: 16) {
                VStack(alignment: .leading, spacing: 8) {
                    Text("Team Batting Totals")
                        .font(.system(.headline, design: .rounded, weight: .semibold))
                        .foregroundStyle(.primary)
                        .padding(.horizontal, 20)

                    StatGridView(grid: season.stats)
                        .padding(.horizontal, 6)
                }

                if let pitchingStats = season.pitchingStats {
                    VStack(alignment: .leading, spacing: 8) {
                        Text("Team Pitching Totals")
                            .font(.system(.headline, design: .rounded, weight: .semibold))
                            .foregroundStyle(.primary)
                            .padding(.horizontal, 20)

                        StatGridView(grid: pitchingStats)
                            .padding(.horizontal, 6)
                    }
                }
            }

            // Leaders
            if !season.leaders.isEmpty {
                leadersSection(leaders: season.leaders, year: season.year)
            }

            // Roster
            if !season.roster.isEmpty {
                rosterSection(roster: season.roster)
            }
        }
    }

    // MARK: - Leaders (mini-leaderboards in 2-column grid)

    private let leaderPageSize = 5

    private func leadersSection(leaders: [StatLeader], year: Int) -> some View {
        let categories = ["HR", "SB", "H", "AVG", "OBP", "OPS", "W", "SV", "SO", "ERA"]
        let columns = [GridItem(.flexible(), spacing: 16), GridItem(.flexible(), spacing: 16)]

        return VStack(alignment: .leading, spacing: 8) {
            Text("Leaders")
                .font(.system(.headline, design: .rounded, weight: .semibold))
                .foregroundStyle(.primary)
                .padding(.horizontal, 20)

            LazyVGrid(columns: columns, alignment: .leading, spacing: 16) {
                ForEach(categories, id: \.self) { category in
                    let catLeaders = leaders.filter { $0.category == category }
                    if !catLeaders.isEmpty {
                        miniLeaderboard(category: category, year: year, entries: catLeaders)
                    }
                }
            }
            .padding(12)
            .background(
                RoundedRectangle(cornerRadius: 12)
                    .fill(Color(uiColor: .secondarySystemBackground))
                    .shadow(color: deepBlue.opacity(0.10), radius: 10, y: 3)
                    .shadow(color: .black.opacity(0.03), radius: 2, y: 1)
            )
            .padding(.horizontal, 6)
        }
    }

    private func miniLeaderboard(category: String, year: Int, entries: [StatLeader]) -> some View {
        let key = "\(year)-\(category)"
        let visibleCount = leaderVisibleCounts[key] ?? leaderPageSize
        let visible = Array(entries.prefix(visibleCount))
        let hasMore = entries.count > visibleCount

        return VStack(alignment: .leading, spacing: 2) {
            Text(category)
                .font(.system(.caption2, design: .monospaced, weight: .semibold))
                .foregroundStyle(.secondary)
                .padding(.bottom, 2)

            ForEach(Array(visible.enumerated()), id: \.offset) { _, leader in
                HStack(spacing: 0) {
                    Button {
                        selectedPlayerName = leader.name
                    } label: {
                        Text(leader.name)
                            .font(.system(.subheadline, design: .rounded, weight: .medium))
                            .foregroundStyle(deepBlue)
                            .lineLimit(1)
                    }
                    .buttonStyle(.plain)

                    Spacer(minLength: 4)

                    Text(leader.value)
                        .font(.system(.subheadline, design: .monospaced, weight: .medium))
                        .foregroundStyle(.primary)
                }
            }

            HStack(spacing: 8) {
                if hasMore {
                    Button {
                        withAnimation(.easeInOut(duration: 0.2)) {
                            leaderVisibleCounts[key] = visibleCount + leaderPageSize
                        }
                    } label: {
                        HStack(spacing: 3) {
                            Text("more")
                                .font(.system(.caption, design: .rounded, weight: .medium))
                            Image(systemName: "chevron.down")
                                .font(.system(size: 8, weight: .semibold))
                        }
                        .foregroundStyle(.secondary)
                    }
                    .buttonStyle(.plain)
                }

                if visibleCount > leaderPageSize {
                    Button {
                        withAnimation(.easeInOut(duration: 0.2)) {
                            leaderVisibleCounts[key] = leaderPageSize
                        }
                    } label: {
                        Image(systemName: "chevron.up")
                            .font(.system(size: 8, weight: .semibold))
                            .foregroundStyle(.secondary)
                    }
                    .buttonStyle(.plain)
                }
            }
            .padding(.top, 2)
        }
    }

    // MARK: - Roster (name + position)

    private func rosterSection(roster: [RosterEntry]) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            VStack(alignment: .leading, spacing: 2) {
                Text("Roster (\(roster.count) players)")
                    .font(.system(.headline, design: .rounded, weight: .semibold))
                    .foregroundStyle(.primary)
                Text("Includes any player who played on the team this year.")
                    .font(.system(.caption, design: .rounded))
                    .foregroundStyle(.secondary)
            }
            .padding(.horizontal, 20)

            VStack(alignment: .leading, spacing: 0) {
                ForEach(Array(roster.enumerated()), id: \.offset) { index, entry in
                    if index > 0 {
                        Divider()
                            .padding(.leading, 12)
                    }

                    HStack(spacing: 0) {
                        Button {
                            selectedPlayerName = entry.name
                        } label: {
                            Text(entry.name)
                                .font(.system(.callout, design: .rounded, weight: .medium))
                                .foregroundStyle(deepBlue)
                                .lineLimit(1)
                        }
                        .buttonStyle(.plain)

                        Spacer(minLength: 8)

                        if !entry.position.isEmpty {
                            Text(entry.position)
                                .font(.system(.callout, design: .monospaced, weight: .medium))
                                .foregroundStyle(.secondary)
                        }
                    }
                    .padding(.horizontal, 12)
                    .padding(.vertical, 4)
                }

                Spacer().frame(height: 10)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(
                RoundedRectangle(cornerRadius: 12)
                    .fill(Color(uiColor: .secondarySystemBackground))
                    .shadow(color: deepBlue.opacity(0.10), radius: 10, y: 3)
                    .shadow(color: .black.opacity(0.03), radius: 2, y: 1)
            )
            .padding(.horizontal, 6)
        }
    }
}
