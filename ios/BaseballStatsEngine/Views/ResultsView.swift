import SwiftUI

struct ResultsView: View {
    @Environment(AppState.self) private var appState
    @Environment(\.dismiss) private var dismiss
    @State private var inputText = ""
    @FocusState private var isInputFocused: Bool
    @State private var resultsContentHeight: CGFloat = 0
    @State private var selectedPlayerName: String? = nil
    @State private var selectedTeamCode: String? = nil
    @State private var drilldownQuery: String? = nil
    @State private var voice = VoiceInputService()
    @State private var voiceUsedThisQuery: Bool = false
    let initialQuestion: String
    @Binding var navigationPath: NavigationPath

    private let deepBlue = Color.brandDeepBlue
    private let lightBlue = Color(red: 0.45, green: 0.7, blue: 1.0)

    private var visibleMessages: [Message] {
        appState.messages.filter { !$0.content.isEmpty || $0.role == .user }
    }

    private var userQueries: [String] {
        visibleMessages.filter { $0.role == .user }.map(\.content)
    }

    /// Scroll so the latest user question is pinned near the top — only for follow-ups
    private func scrollToLatestQuestion(proxy: ScrollViewProxy) {
        let userMessages = visibleMessages.filter { $0.role == .user }
        if userMessages.count > 1, let latestUser = userMessages.last {
            withAnimation(.easeOut(duration: 0.2)) {
                proxy.scrollTo(latestUser.id, anchor: .top)
            }
        }
    }

    /// Whether the input area fits inline below results without scrolling
    private func fitsInline(in availableHeight: CGFloat) -> Bool {
        let inputEstimate: CGFloat = 110
        return resultsContentHeight + inputEstimate + 30 < availableHeight
    }

    @ViewBuilder
    private func resultCard(for message: Message) -> some View {
        ResultCard(
            message: message,
            isFirstUser: message.id == visibleMessages.first(where: { $0.role == .user })?.id,
            onBack: { dismiss() },
            isStreaming: appState.isLoading && message.id == visibleMessages.last?.id,
            previousQueries: userQueries,
            onPlayerTap: { name in
                if appState.pendingDisambiguation != nil {
                    appState.resolveDisambiguation(with: name)
                } else {
                    selectedPlayerName = name
                }
            },
            onTeamTap: { code in
                selectedTeamCode = code
            },
            onQueryTap: { query in
                appState.sendQuestion(query)
            },
            onDrilldownTap: { query in
                drilldownQuery = query
            }
        )
    }

    var body: some View {
        GeometryReader { geometry in
            let available = geometry.size.height
            let inline = fitsInline(in: available)

            ZStack {
                Color(uiColor: .systemBackground)
                    .ignoresSafeArea()

                VStack(spacing: 0) {
                    ScrollViewReader { proxy in
                        ScrollView {
                            VStack(spacing: 20) {
                                ForEach(visibleMessages) { message in
                                    resultCard(for: message)
                                        .id(message.id)
                                }

                                if appState.isLoading && appState.currentStreamingText.isEmpty {
                                    LoadingIndicator()
                                        .frame(maxWidth: .infinity, alignment: .center)
                                        .padding(.top, 12)
                                        .id("loading")
                                }
                            }
                            .padding(.top, 16)
                            .padding(.bottom, 8)
                            .background(
                                GeometryReader { contentGeo in
                                    Color.clear.preference(
                                        key: ResultsHeightKey.self,
                                        value: contentGeo.size.height
                                    )
                                }
                            )

                            // When results are short, place input inline below them
                            if !appState.isLoading && inline {
                                inputAndSuggestions
                                    .id("inputInline")
                                    .padding(.top, 10)
                            }
                        }
                        .scrollDismissesKeyboard(.interactively)
                        .onPreferenceChange(ResultsHeightKey.self) { height in
                            resultsContentHeight = height
                        }
                        .onChange(of: appState.disambiguatedPlayerName) { _, name in
                            if let name {
                                appState.disambiguatedPlayerName = nil
                                selectedPlayerName = name
                            }
                        }
                        .onChange(of: appState.messages.count) {
                            scrollToLatestQuestion(proxy: proxy)
                        }
                        .onChange(of: appState.isLoading) { _, loading in
                            // Re-scroll when response finishes — content height has changed
                            if !loading {
                                scrollToLatestQuestion(proxy: proxy)
                            }
                        }
                    }

                    // When results are too tall, pin input at the bottom
                    if !appState.isLoading && !inline {
                        inputAndSuggestions
                            .background(Color(uiColor: .systemBackground))
                            .overlay(
                                Rectangle()
                                    .frame(height: 0.5)
                                    .foregroundStyle(Color(uiColor: .separator).opacity(0.3)),
                                alignment: .top
                            )
                    }
                }
            }
        }
        .navigationBarBackButtonHidden(true)
        .swipeBack()
        .navigationBarTitleDisplayMode(.inline)
        .toolbarBackground(.automatic, for: .navigationBar)
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
        .navigationDestination(isPresented: Binding(
            get: { selectedPlayerName != nil },
            set: { if !$0 { selectedPlayerName = nil } }
        )) {
            PlayerCardView(playerName: selectedPlayerName ?? "", navigationPath: $navigationPath)
        }
        .navigationDestination(isPresented: Binding(
            get: { selectedTeamCode != nil },
            set: { if !$0 { selectedTeamCode = nil } }
        )) {
            TeamCardView(teamCode: selectedTeamCode ?? "", navigationPath: $navigationPath)
        }
        .sheet(isPresented: Binding(
            get: { appState.showPaywall },
            set: { appState.showPaywall = $0 }
        )) {
            PaywallView()
                .environment(appState)
        }
        .sheet(isPresented: Binding(
            get: { drilldownQuery != nil },
            set: { if !$0 { drilldownQuery = nil } }
        )) {
            if let query = drilldownQuery {
                DrilldownResultView(query: query, navigationPath: $navigationPath)
            }
        }
        .onChange(of: appState.showPaywall) { _, showing in
            // When paywall dismisses, retry the blocked query if user subscribed
            if !showing && StoreKitService.shared.isSubscribed {
                appState.retryPendingPaywallQuery()
            }
        }
        .onAppear {
            if appState.messages.isEmpty && !initialQuestion.isEmpty {
                appState.sendQuestion(initialQuestion)
            }
        }
        .onDisappear {
            isInputFocused = false
            UIApplication.shared.sendAction(#selector(UIResponder.resignFirstResponder), to: nil, from: nil, for: nil)
            appState.clearConversation()
        }
    }

    private var inputAndSuggestions: some View {
        VStack(spacing: 10) {
            InlineSearchBar(
                text: $inputText,
                placeholder: "Ask a follow-up or a new question",
                isFocused: $isInputFocused,
                voice: voice,
                voiceUsedThisQuery: $voiceUsedThisQuery,
                onSubmit: sendQuestion,
                tint: lightBlue,
                deepBlue: deepBlue
            )
            .padding(.horizontal, 16)
        }
        .padding(.top, 10)
        .padding(.bottom, 6)
    }

    private func sendQuestion() {
        let trimmed = inputText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, !appState.isLoading else { return }
        if voice.isRecording { voice.stopRecording() }
        let submittedViaVoice = voiceUsedThisQuery
        voiceUsedThisQuery = false
        inputText = ""

        if submittedViaVoice {
            let deviceId = AppState.deviceId
            Task.detached {
                await BackendService().logClientEvent(
                    eventType: "voice_input",
                    context: ["query_text": trimmed, "source": "results_followup"],
                    deviceId: deviceId
                )
            }
        }

        // Follow-ups should go to the backend for rewriting, not through resolveSearch
        // which can incorrectly match player names (e.g., "career?" → Carter, "De La Cruz" → Bryan).
        // Only route through resolveSearch if the input looks like a standalone search,
        // not a conversational follow-up.
        let lower = trimmed.lowercased()
        let followUpPrefixes = ["what about", "how about", "and ", "compare", "vs ",
                                "how did", "what did", "how is", "what is", "who led",
                                "who leads", "in the ", "since ", "last ", "this "]
        let isFollowUpPhrase = followUpPrefixes.contains(where: { lower.hasPrefix($0) })
        let wordCount = trimmed.split(separator: " ").count
        if wordCount <= 2 || isFollowUpPhrase {
            appState.sendQuestion(trimmed, isFollowUp: true)
            return
        }

        // Don't pass history — follow-up queries shouldn't add the raw text
        // to search history. The rewritten standalone query gets added later.
        switch PlayerNameMatcher.resolveSearch(trimmed, history: nil) {
        case .player(let name, _):
            selectedPlayerName = name
        case .team(let code):
            selectedTeamCode = code
        case .question(let query):
            appState.sendQuestion(query, isFollowUp: true)
        }
    }
}

// MARK: - Drilldown Result View

struct DrilldownResultView: View {
    let query: String
    @Binding var navigationPath: NavigationPath
    @Environment(\.dismiss) private var dismiss
    @State private var segments: [StatGridParser.Segment] = []
    @State private var isLoading = true

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    if isLoading {
                        LoadingIndicator()
                            .frame(maxWidth: .infinity)
                            .padding(.top, 40)
                    } else {
                        ForEach(Array(segments.enumerated()), id: \.offset) { _, segment in
                            segmentView(segment)
                        }
                    }
                }
                .padding(.horizontal, 12)
            }
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button { dismiss() } label: {
                        Image(systemName: "xmark.circle.fill")
                            .foregroundStyle(.secondary)
                            .font(.title3)
                    }
                }
            }
        }
        .presentationDetents([.medium, .large])
        .task {
            let backend = BackendService()
            do {
                let result = try await backend.ask(
                    question: query,
                    deviceId: AppState.deviceId,
                    history: [],
                    onChunk: { @MainActor _ in }
                )
                segments = StatGridParser.parse(result.text, isStreaming: false)
            } catch {
                segments = [.text("Couldn't load details.")]
            }
            isLoading = false
        }
    }

    @ViewBuilder
    private func segmentView(_ segment: StatGridParser.Segment) -> some View {
        switch segment {
        case .text(let text):
            if !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                Text(LocalizedStringKey(text))
                    .font(.system(.body, design: .rounded))
                    .padding(.vertical, 2)
            }
        case .leaderboard(let grid):
            LeaderboardView(grid: grid)
                .padding(.vertical, 6)
        case .statGrid(let grid):
            StatGridView(grid: grid)
                .padding(.vertical, 6)
        default:
            EmptyView()
        }
    }
}

private struct ResultsHeightKey: PreferenceKey {
    nonisolated(unsafe) static var defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) {
        value = max(value, nextValue())
    }
}
