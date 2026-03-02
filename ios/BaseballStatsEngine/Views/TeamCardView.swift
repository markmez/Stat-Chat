import SwiftUI

struct TeamCardView: View {
    let teamCode: String

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
    @State private var searchSuggestions: [String] = []
    @State private var searchPendingQuery: String?
    @State private var searchTeamCode: String? = nil
    @State private var searchQuestion: String? = nil

    private let deepBlue = Color(red: 0.1, green: 0.25, blue: 0.7)
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
            get: { selectedPlayerName != nil },
            set: { if !$0 { selectedPlayerName = nil } }
        )) {
            PlayerCardView(playerName: selectedPlayerName ?? "")
        }
        .navigationDestination(isPresented: Binding(
            get: { searchTeamCode != nil },
            set: { if !$0 { searchTeamCode = nil } }
        )) {
            TeamCardView(teamCode: searchTeamCode ?? "")
        }
        .navigationDestination(isPresented: Binding(
            get: { searchQuestion != nil },
            set: { if !$0 { searchQuestion = nil } }
        )) {
            ResultsView(initialQuestion: searchQuestion ?? "")
        }
        .task {
            teamCard = PlayerCardService.fetchTeamCard(teamCode: teamCode)
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
                                selectedPlayerName = n
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
            selectedPlayerName = name
            return
        }

        // Exact team name match (case-insensitive)
        if let code = PlayerCardService.teamCodeFromFullNameCaseInsensitive(trimmed) {
            searchText = ""
            searchTeamCode = code
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

    // MARK: - Season section (stats + leaders + roster)

    @ViewBuilder
    private func seasonSection(season: TeamSeasonData, expanded: Bool) -> some View {
        VStack(alignment: .leading, spacing: 16) {
            // Team stats grid
            StatGridView(grid: season.stats)
                .padding(.horizontal, 6)

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
        let categories = ["HR", "SB", "H", "AVG", "OBP", "OPS"]
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
                    .overlay(
                        RoundedRectangle(cornerRadius: 12)
                            .stroke(Color(uiColor: .separator).opacity(0.3), lineWidth: 0.5)
                    )
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
            Text("Roster (\(roster.count) players)")
                .font(.system(.headline, design: .rounded, weight: .semibold))
                .foregroundStyle(.primary)
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
                    .overlay(
                        RoundedRectangle(cornerRadius: 12)
                            .stroke(Color(uiColor: .separator).opacity(0.3), lineWidth: 0.5)
                    )
            )
            .padding(.horizontal, 6)
        }
    }
}
