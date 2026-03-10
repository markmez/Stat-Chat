import SwiftUI

struct HomeView: View {
    @Environment(AppState.self) private var appState
    @State private var questionText = ""
    @State private var historyExpanded = false
    @State private var path = NavigationPath()
    @State private var suggestedPlayers: [String] = []
    @State private var pendingQuery: String?

    private let deepBlue = Color(red: 0.1, green: 0.25, blue: 0.7)
    private let lightBlue = Color(red: 0.45, green: 0.7, blue: 1.0)

    /// Height of the peeking history card
    private let peekHeight: CGFloat = 160
    /// Height when fully expanded
    private let expandedHeight: CGFloat = 420

    private var cardHeight: CGFloat {
        historyExpanded ? expandedHeight : peekHeight
    }

    /// Wrapper types for value-based navigationDestination
    private struct ResultsDestination: Hashable { let question: String }
    private struct PlayerCardDestination: Hashable { let name: String }
    private struct TeamCardDestination: Hashable { let code: String }

    var body: some View {
        NavigationStack(path: $path) {
            mainContent
                .navigationDestination(for: ResultsDestination.self) { dest in
                    ResultsView(initialQuestion: dest.question)
                }
                .navigationDestination(for: PlayerCardDestination.self) { dest in
                    PlayerCardView(playerName: dest.name)
                }
                .navigationDestination(for: TeamCardDestination.self) { dest in
                    TeamCardView(teamCode: dest.code)
                }
        }
    }

    private var mainContent: some View {
        ZStack(alignment: .bottom) {
            Color(uiColor: .systemBackground)
                .ignoresSafeArea()

            // Main content
            VStack(spacing: 0) {
                Spacer()

                // Logo + Wordmark — inline
                VStack(spacing: 6) {
                    HStack(spacing: 12) {
                        Text("StatChat")
                            .font(.system(size: 42, weight: .bold))
                            .foregroundStyle(
                                LinearGradient(
                                    colors: [lightBlue, deepBlue],
                                    startPoint: .leading, endPoint: .trailing
                                )
                            )

                        // AI diamond center + 3 baseballs around it
                        ZStack {
                            Image(systemName: "sparkle")
                                .font(.system(size: 28, weight: .bold))
                                .foregroundStyle(
                                    LinearGradient(
                                        colors: [lightBlue, deepBlue],
                                        startPoint: .topLeading, endPoint: .bottomTrailing
                                    )
                                )

                            Image(systemName: "baseball.fill")
                                .font(.system(size: 14))
                                .foregroundStyle(lightBlue)
                                .offset(x: 13, y: -13)

                            Image(systemName: "baseball.fill")
                                .font(.system(size: 10))
                                .foregroundStyle(lightBlue.opacity(0.7))
                                .offset(x: -11, y: -11)

                            Image(systemName: "baseball.fill")
                                .font(.system(size: 10.5))
                                .foregroundStyle(lightBlue.opacity(0.85))
                                .offset(x: 11, y: 11)
                        }
                    }

                    Text("Baseball stats, answered instantly")
                        .font(.system(.subheadline, design: .rounded))
                        .foregroundStyle(.secondary)
                }
                .padding(.bottom, 36)

                // Search field
                HStack(alignment: .top, spacing: 12) {
                    Image(systemName: "magnifyingglass")
                        .font(.system(size: 18, weight: .medium))
                        .foregroundStyle(lightBlue)
                        .padding(.top, 2)

                    TextField("", text: $questionText, prompt:
                        Text("Ask anything...")
                            .foregroundStyle(Color(uiColor: .placeholderText)),
                        axis: .vertical
                    )
                    .font(.system(.body, design: .rounded))
                    .foregroundStyle(.primary)
                    .lineLimit(1...10)
                    .onSubmit { submitQuestion() }

                    if !questionText.isEmpty {
                        Button {
                            submitQuestion()
                        } label: {
                            Image(systemName: "arrow.right.circle.fill")
                                .font(.system(size: 24))
                                .foregroundStyle(lightBlue)
                        }
                        .padding(.top, 2)
                    }
                }
                .padding(.horizontal, 18)
                .padding(.vertical, 14)
                .frame(minHeight: 120, alignment: .top)
                .background(Color(uiColor: .secondarySystemBackground), in: RoundedRectangle(cornerRadius: 16))
                .overlay(
                    RoundedRectangle(cornerRadius: 16)
                        .stroke(Color(uiColor: .separator).opacity(0.3), lineWidth: 1)
                )
                .padding(.horizontal, 24)

                // "Did you mean?" or sample queries
                if !suggestedPlayers.isEmpty {
                    didYouMeanCard
                        .padding(.top, 14)
                        .transition(.opacity.combined(with: .scale(scale: 0.95)))
                } else {
                    VStack(spacing: 12) {
                        Text("Search for player stats by name or ask any question.")
                            .font(.system(.subheadline, design: .rounded))
                            .foregroundStyle(.secondary)
                            .multilineTextAlignment(.center)
                            .padding(.horizontal, 24)

                        AnimatedPlaceholder { query in
                            questionText = query
                        }
                    }
                    .padding(.top, 20)
                }

                Spacer()

                // Reserve space for the history card
                if !appState.searchHistory.isEmpty {
                    Color.clear.frame(height: peekHeight + 10)
                }
            }

            // History card
            if !appState.searchHistory.isEmpty {
                historyCard
                    .transition(.move(edge: .bottom))
            }
        }
        .navigationBarTitleDisplayMode(.inline)
        .toolbarBackground(.automatic, for: .navigationBar)
    }

    private var historyCard: some View {
        VStack(spacing: 0) {
            // Drag handle + header
            VStack(spacing: 8) {
                Capsule()
                    .fill(Color(uiColor: .separator))
                    .frame(width: 36, height: 4)
                    .padding(.top, 10)

                HStack {
                    Text("Recent")
                        .font(.system(.subheadline, weight: .semibold))
                        .foregroundStyle(.secondary)

                    Spacer()

                    if historyExpanded {
                        Button("Clear") {
                            withAnimation(.spring(response: 0.3)) {
                                appState.clearSearchHistory()
                                historyExpanded = false
                            }
                        }
                        .font(.system(.caption, weight: .medium))
                        .foregroundStyle(.secondary)
                    }

                    NavigationLink {
                        AboutView()
                    } label: {
                        Image(systemName: "info.circle")
                            .font(.system(size: 13))
                            .foregroundStyle(.tertiary)
                    }
                }
                .padding(.horizontal, 20)
            }
            .contentShape(Rectangle())
            .onTapGesture {
                withAnimation(.spring(response: 0.35, dampingFraction: 0.85)) {
                    historyExpanded.toggle()
                }
            }

            // History items
            ScrollView {
                LazyVStack(spacing: 0) {
                    ForEach(appState.searchHistory, id: \.self) { query in
                        Button {
                            let q = query  // capture before mutation
                            appState.addToSearchHistory(q)
                            if let playerName = PlayerNameMatcher.matchPlayer(q) {
                                path.append(PlayerCardDestination(name: playerName))
                            } else if let teamCode = PlayerNameMatcher.matchTeamExact(q) {
                                path.append(TeamCardDestination(code: teamCode))
                            } else {
                                path.append(ResultsDestination(question: q))
                            }
                        } label: {
                            HStack(spacing: 12) {
                                Image(systemName: "clock.arrow.circlepath")
                                    .font(.system(size: 14))
                                    .foregroundStyle(.tertiary)

                                Text(query)
                                    .font(.system(.subheadline, design: .rounded))
                                    .foregroundStyle(.primary)
                                    .lineLimit(1)

                                Spacer()

                                Button {
                                    withAnimation(.easeOut(duration: 0.2)) {
                                        appState.searchHistory.removeAll { $0 == query }
                                        UserDefaults.standard.set(appState.searchHistory, forKey: "searchHistory")
                                    }
                                } label: {
                                    Image(systemName: "xmark")
                                        .font(.system(size: 11, weight: .medium))
                                        .foregroundStyle(.tertiary)
                                }
                            }
                            .padding(.horizontal, 20)
                            .padding(.vertical, 12)
                        }
                        .buttonStyle(.plain)

                        Divider()
                            .padding(.leading, 46)
                    }
                }
            }
            .scrollDisabled(!historyExpanded)
        }
        .frame(height: cardHeight)
        .contentShape(Rectangle())
        .highPriorityGesture(
            historyExpanded ? nil :
            DragGesture(minimumDistance: 8)
                .onEnded { value in
                    withAnimation(.spring(response: 0.35, dampingFraction: 0.85)) {
                        if value.translation.height < -20 {
                            historyExpanded = true
                        } else if value.translation.height > 20 {
                            historyExpanded = false
                        }
                    }
                }
        )
        .simultaneousGesture(
            historyExpanded ?
            DragGesture(minimumDistance: 8)
                .onEnded { value in
                    if value.translation.height > 40 {
                        withAnimation(.spring(response: 0.35, dampingFraction: 0.85)) {
                            historyExpanded = false
                        }
                    }
                }
            : nil
        )
        .background(
            UnevenRoundedRectangle(topLeadingRadius: 20, topTrailingRadius: 20)
                .fill(Color(uiColor: .secondarySystemBackground))
                .shadow(color: .black.opacity(0.08), radius: 12, y: -4)
                .ignoresSafeArea(edges: .bottom)
        )
        .animation(.spring(response: 0.35, dampingFraction: 0.85), value: historyExpanded)
    }

    @ViewBuilder
    private var didYouMeanCard: some View {
        if let query = pendingQuery, !suggestedPlayers.isEmpty {
            VStack(spacing: 6) {
                Text("Did you mean:")
                    .font(.system(.subheadline, design: .rounded))
                    .foregroundStyle(.secondary)

                ForEach(suggestedPlayers, id: \.self) { name in
                    Button {
                        let n = name
                        withAnimation { suggestedPlayers = []; pendingQuery = nil }
                        appState.addToSearchHistory(n)
                        path.append(PlayerCardDestination(name: n))
                    } label: {
                        Text(name)
                            .font(.system(.subheadline, design: .rounded, weight: .semibold))
                            .foregroundStyle(deepBlue)
                    }
                }

                Button {
                    let q = query
                    withAnimation { suggestedPlayers = []; pendingQuery = nil }
                    appState.addToSearchHistory(q)
                    path.append(ResultsDestination(question: q))
                } label: {
                    (Text("Or search \"")
                        .foregroundStyle(.secondary)
                     + Text(query)
                        .foregroundStyle(lightBlue)
                     + Text("\"")
                        .foregroundStyle(.secondary))
                        .font(.system(.subheadline, design: .rounded))
                }
            }
            .padding(.vertical, 14)
            .padding(.horizontal, 20)
            .background(Color(uiColor: .secondarySystemBackground), in: RoundedRectangle(cornerRadius: 14))
            .overlay(
                RoundedRectangle(cornerRadius: 14)
                    .stroke(Color(uiColor: .separator).opacity(0.3), lineWidth: 0.5)
            )
            .padding(.horizontal, 24)
        }
    }

    private func submitQuestion() {
        let trimmed = questionText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        questionText = ""
        suggestedPlayers = []
        pendingQuery = nil

        // Direct-to-profile shortcut: skip Claude if input is just a player name
        if let playerName = PlayerNameMatcher.matchPlayer(trimmed) {
            appState.addToSearchHistory(trimmed)
            path.append(PlayerCardDestination(name: playerName))
        } else if let teamCode = PlayerNameMatcher.matchTeamExact(trimmed) {
            appState.addToSearchHistory(trimmed)
            path.append(TeamCardDestination(code: teamCode))
        } else {
            let fuzzyMatches = PlayerNameMatcher.fuzzyMatch(trimmed)
            if !fuzzyMatches.isEmpty {
                // Don't add misspelling to history — the correction or "search anyway" will
                withAnimation(.easeOut(duration: 0.25)) {
                    suggestedPlayers = fuzzyMatches
                    pendingQuery = trimmed
                }
            } else {
                appState.addToSearchHistory(trimmed)
                path.append(ResultsDestination(question: trimmed))
            }
        }
    }
}
