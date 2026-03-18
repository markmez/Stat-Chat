import SwiftUI

struct HomeView: View {
    @Environment(AppState.self) private var appState
    @State private var questionText = ""
    @State private var historyExpanded = false
    @State private var path = NavigationPath()
    @FocusState private var isInputFocused: Bool
    @State private var lastNameSearchCount: Int = UserDefaults.standard.integer(forKey: "lastNameSearchCount")

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
    private struct PlayerCardDestination: Hashable { let name: String; var alternatives: [String] = [] }
    private struct TeamCardDestination: Hashable { let code: String }

    var body: some View {
        NavigationStack(path: $path) {
            mainContent
                .navigationDestination(for: ResultsDestination.self) { dest in
                    ResultsView(initialQuestion: dest.question, navigationPath: $path)
                }
                .navigationDestination(for: PlayerCardDestination.self) { dest in
                    PlayerCardView(playerName: dest.name, alternatives: dest.alternatives, navigationPath: $path)
                }
                .navigationDestination(for: TeamCardDestination.self) { dest in
                    TeamCardView(teamCode: dest.code, navigationPath: $path)
                }
        }
        .sheet(isPresented: Binding(
            get: { appState.showPaywall },
            set: { appState.showPaywall = $0 }
        )) {
            PaywallView()
                .environment(appState)
        }
        .onChange(of: path) { _, newPath in
            if newPath.isEmpty {
                isInputFocused = false
                withAnimation(.easeInOut(duration: 0.2)) {
                    historyExpanded = false
                }
                // Delay keyboard dismissal to ensure it fires after the view hierarchy settles
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) {
                    isInputFocused = false
                    UIApplication.shared.sendAction(#selector(UIResponder.resignFirstResponder), to: nil, from: nil, for: nil)
                }
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
                }
                .padding(.bottom, 36)

                // Search field
                HStack(alignment: .top, spacing: 12) {
                    Image(systemName: "magnifyingglass")
                        .font(.system(size: 18, weight: .medium))
                        .foregroundStyle(lightBlue)
                        .padding(.top, 2)

                    TextField("", text: $questionText, prompt:
                        Text("Search by name or ask any question")
                            .foregroundStyle(Color(uiColor: .placeholderText)),
                        axis: .vertical
                    )
                    .font(.system(.body, design: .rounded))
                    .foregroundStyle(.primary)
                    .lineLimit(1...10)
                    .focused($isInputFocused)
                    .autocorrectionDisabled(true)
                    .textInputAutocapitalization(.never)
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
                .shadow(color: deepBlue.opacity(0.12), radius: 12, y: 4)
                .shadow(color: .black.opacity(0.04), radius: 2, y: 1)
                .padding(.horizontal, 24)

                // Sample queries + tip
                VStack(spacing: 12) {
                    if lastNameSearchCount < 2 {
                        HStack(spacing: 6) {
                            Image(systemName: "lightbulb.fill")
                                .font(.system(size: 12))
                                .foregroundStyle(.yellow)
                            Text("You can search for player stats by just last name.")
                                .font(.system(.caption, design: .rounded))
                                .foregroundStyle(.secondary)
                        }
                    }

                    SuggestionPillsView(
                        searchHistory: appState.searchHistory,
                        compact: !StoreKitService.shared.isSubscribed && !appState.searchHistory.isEmpty
                    ) { query in
                        questionText = query
                    }

                    // Free usage indicator
                    if !StoreKitService.shared.isSubscribed {
                        freeUsageIndicator
                    }
                }
                .padding(.top, 16)

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
        .ignoresSafeArea(.keyboard)
        .navigationBarTitleDisplayMode(.inline)
        .toolbarBackground(.automatic, for: .navigationBar)
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                NavigationLink {
                    SettingsView()
                } label: {
                    Image(systemName: "gearshape")
                        .font(.system(size: 15))
                        .foregroundStyle(.secondary)
                }
            }
        }
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

    private var freeUsageIndicator: some View {
        let remaining = appState.freeQueriesRemaining
        let resetDate = appState.weeklyResetDate
        let formatter: DateFormatter = {
            let f = DateFormatter()
            f.dateFormat = "EEEE" // e.g. "Monday"
            return f
        }()

        return VStack(spacing: 4) {
            HStack(spacing: 4) {
                Text("\(remaining) free search\(remaining == 1 ? "" : "es") remaining")
                    .font(.system(.caption2, design: .rounded))
                    .foregroundStyle(remaining == 0 ? .red : .secondary)
                Text("·")
                    .foregroundStyle(.quaternary)
                Text("Resets \(formatter.string(from: resetDate))")
                    .font(.system(.caption2, design: .rounded))
                    .foregroundStyle(.quaternary)
            }

            if remaining == 0 {
                Button {
                    appState.showPaywall = true
                } label: {
                    Text("Upgrade for unlimited")
                        .font(.system(.caption2, design: .rounded, weight: .medium))
                        .foregroundStyle(deepBlue)
                }
            }
        }
        .padding(.top, 6)
    }

    private func submitQuestion() {
        let trimmed = questionText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        questionText = ""

        switch PlayerNameMatcher.resolveSearch(trimmed, history: appState) {
        case .player(let name, let alternatives):
            lastNameSearchCount = UserDefaults.standard.integer(forKey: "lastNameSearchCount")
            path.append(PlayerCardDestination(name: name, alternatives: alternatives))
        case .team(let code):
            path.append(TeamCardDestination(code: code))
        case .question(let query):
            path.append(ResultsDestination(question: query))
        }
    }
}
