import SwiftUI

// Track scroll position for drawer collapse gating
private struct ScrollOffsetKey: PreferenceKey {
    nonisolated(unsafe) static var defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) {
        value = nextValue()
    }
}

// Shared navigation destination types
struct ResultsDestination: Hashable { let question: String }
struct PlayerCardDestination: Hashable { let name: String; var alternatives: [String] = [] }
struct TeamCardDestination: Hashable { let code: String }

struct HomeView: View {
    @Environment(AppState.self) private var appState
    @State private var questionText = ""
    @State private var path = NavigationPath()
    @State private var feedExpanded = false
    @State private var feedScrolledToTop = true
    @FocusState private var isInputFocused: Bool
    @State private var lastNameSearchCount: Int = UserDefaults.standard.integer(forKey: "lastNameSearchCount")
    @State private var matchupPills: [String] = []
    @State private var drawerTab: DrawerTab = .notable
    private enum DrawerTab { case notable, leaders }

    private let deepBlue = Color(red: 0.1, green: 0.25, blue: 0.7)
    private let lightBlue = Color(red: 0.45, green: 0.7, blue: 1.0)

    private struct HistoryDestination: Hashable {}

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
                .navigationDestination(for: HistoryDestination.self) { _ in
                    SearchHistoryView(navigationPath: $path)
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
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) {
                    isInputFocused = false
                    UIApplication.shared.sendAction(#selector(UIResponder.resignFirstResponder), to: nil, from: nil, for: nil)
                }
            }
        }
    }

    /// Height of the peeking drawer
    private let peekHeight: CGFloat = 140
    /// Height when fully expanded — near full screen
    private var expandedHeight: CGFloat {
        UIScreen.main.bounds.height * 0.85
    }

    private var drawerHeight: CGFloat {
        feedExpanded ? expandedHeight : peekHeight
    }

    private var mainContent: some View {
        ZStack(alignment: .bottom) {
            // Main search area — centered
            VStack(spacing: 0) {
                // History + Settings buttons
                HStack {
                    if !appState.searchHistory.isEmpty {
                        Button {
                            path.append(HistoryDestination())
                        } label: {
                            Image(systemName: "clock.arrow.circlepath")
                                .font(.system(size: 17))
                                .foregroundStyle(.primary.opacity(0.7))
                        }
                    }
                    Spacer()
                    NavigationLink {
                        SettingsView()
                    } label: {
                        Image(systemName: "gearshape")
                            .font(.system(size: 17))
                            .foregroundStyle(.primary.opacity(0.7))
                    }
                }
                .padding(.horizontal, 24)
                .padding(.top, 8)

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
                            .foregroundStyle(Color(.placeholderText)),
                        axis: .vertical
                    )
                    .font(.system(.body, design: .rounded))
                    .foregroundStyle(.primary)
                    .lineLimit(1...3)
                    .focused($isInputFocused)
                    .autocorrectionDisabled(true)
                    .textInputAutocapitalization(.never)
                    .submitLabel(.search)
                    .onSubmit { submitQuestion() }
                    .onChange(of: questionText) { _, newValue in
                        if newValue.contains("\n") {
                            questionText = newValue.replacingOccurrences(of: "\n", with: "")
                            submitQuestion()
                        }
                    }

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
                .background(Color(.secondarySystemBackground), in: RoundedRectangle(cornerRadius: 16))
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
                        compact: !StoreKitService.shared.isSubscribed,
                        matchupPills: matchupPills
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

                // Reserve space for the drawer
                Color.clear.frame(height: peekHeight + 10)
            }
            .onTapGesture {
                isInputFocused = false
                if feedExpanded {
                    withAnimation(.spring(response: 0.35, dampingFraction: 0.85)) {
                        feedExpanded = false
                    }
                }
            }

            // Notable events drawer
            notableDrawer
                .transition(.move(edge: .bottom))
        }
        .ignoresSafeArea(.keyboard)
        .navigationBarHidden(true)
    }

    private var notableDrawer: some View {
        VStack(spacing: 0) {
            // Drag handle + tab bar — this is the collapse/expand target
            VStack(spacing: 0) {
                Capsule()
                    .fill(Color(.separator))
                    .frame(width: 36, height: 4)
                    .padding(.top, 10)

                HStack(spacing: 0) {
                    drawerTabButton("Events", tab: .notable)
                    drawerTabButton("Leaders", tab: .leaders)
                }
                .padding(.horizontal, 16)
                .padding(.top, 8)
            }
            .contentShape(Rectangle())
            .onTapGesture {
                withAnimation(.spring(response: 0.35, dampingFraction: 0.85)) {
                    if feedExpanded {
                        feedExpanded = false
                    } else {
                        feedExpanded = true
                        NotableEventsFeed.markExpandedTrayToday()
                    }
                }
            }
            .gesture(
                DragGesture(minimumDistance: 10)
                    .onEnded { value in
                        withAnimation(.spring(response: 0.35, dampingFraction: 0.85)) {
                            if value.translation.height < -20 && !feedExpanded {
                                feedExpanded = true
                                NotableEventsFeed.markExpandedTrayToday()
                            } else if value.translation.height > 20 && feedExpanded {
                                feedExpanded = false
                            }
                        }
                    }
            )

            // Tab content
            if drawerTab == .notable {
                DrawerScrollView(
                    isEnabled: feedExpanded,
                    isAtTop: $feedScrolledToTop,
                    onPullDown: {
                        withAnimation(.spring(response: 0.35, dampingFraction: 0.85)) {
                            feedExpanded = false
                        }
                    }
                ) {
                    NotableEventsFeed(
                        onPlayerTap: { name in
                            path.append(PlayerCardDestination(name: name))
                        },
                        onTeamTap: { code in
                            path.append(TeamCardDestination(code: code))
                        },
                        onMatchupTap: { query in
                            path.append(ResultsDestination(question: query))
                        },
                        showHeader: false,
                        matchupPills: $matchupPills,
                        hasExpandedTrayToday: NotableEventsFeed.hasExpandedTray()
                    )
                }
            } else {
                // Leaders tab
                StatLeadersView(
                    onPlayerTap: { name in
                        path.append(PlayerCardDestination(name: name))
                    }
                )
                .scrollDisabled(!feedExpanded)
            }
        }
        .frame(height: drawerHeight)
        .padding(.horizontal, 12)
        .background(
            RoundedRectangle(cornerRadius: 28)
                .fill(Color(.secondarySystemBackground))
                .shadow(color: .black.opacity(0.15), radius: 20, y: 0)
                .shadow(color: .black.opacity(0.10), radius: 8, y: -6)
                .padding(.horizontal, 12)
                .ignoresSafeArea(edges: .bottom)
        )
        // Swipe up anywhere on collapsed drawer to expand
        .highPriorityGesture(
            feedExpanded ? nil :
            DragGesture(minimumDistance: 10)
                .onEnded { value in
                    if value.translation.height < -20 {
                        withAnimation(.spring(response: 0.35, dampingFraction: 0.85)) {
                            feedExpanded = true
                            NotableEventsFeed.markExpandedTrayToday()
                        }
                    }
                }
        )
        .animation(.spring(response: 0.35, dampingFraction: 0.85), value: feedExpanded)
    }

    @ViewBuilder
    private func drawerTabButton(_ title: String, tab: DrawerTab) -> some View {
        Button {
            withAnimation(.easeInOut(duration: 0.2)) {
                drawerTab = tab
            }
        } label: {
            VStack(spacing: 6) {
                Text(title)
                    .font(.system(.subheadline, design: .rounded, weight: drawerTab == tab ? .bold : .medium))
                    .foregroundStyle(drawerTab == tab ? .primary : .secondary)
                Rectangle()
                    .fill(drawerTab == tab ? deepBlue : .clear)
                    .frame(height: 2)
                    .frame(width: CGFloat(title.count) * 8 + 4)
            }
        }
        .buttonStyle(.plain)
        .frame(maxWidth: .infinity)
    }

    private var freeUsageIndicator: some View {
        let remaining = appState.freeQueriesRemaining
        let resetDate = appState.weeklyResetDate
        let formatter: DateFormatter = {
            let f = DateFormatter()
            f.dateFormat = "EEEE" // e.g. "Monday"
            return f
        }()

        return VStack(spacing: 8) {
            if remaining > 0 {
                HStack(spacing: 4) {
                    Text("\(remaining) free search\(remaining == 1 ? "" : "es") remaining")
                        .font(.system(.caption2, design: .rounded))
                        .foregroundStyle(.secondary)
                    Text("·")
                        .foregroundStyle(.quaternary)
                    Text("Resets \(formatter.string(from: resetDate))")
                        .font(.system(.caption2, design: .rounded))
                        .foregroundStyle(.quaternary)
                }
            } else {
                Text("0 free searches remaining · Resets \(formatter.string(from: resetDate))")
                    .font(.system(.caption2, design: .rounded))
                    .foregroundStyle(.secondary)

                Button {
                    appState.showPaywall = true
                } label: {
                    Text("Upgrade for Unlimited")
                        .font(.system(.caption, design: .rounded, weight: .semibold))
                        .foregroundStyle(.white)
                        .padding(.horizontal, 24)
                        .padding(.vertical, 10)
                        .background(deepBlue, in: RoundedRectangle(cornerRadius: 10))
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
