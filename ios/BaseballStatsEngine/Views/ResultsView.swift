import SwiftUI

struct ResultsView: View {
    @Environment(AppState.self) private var appState
    @Environment(\.dismiss) private var dismiss
    @State private var inputText = ""
    @FocusState private var isInputFocused: Bool
    @State private var resultsContentHeight: CGFloat = 0
    @State private var selectedPlayerName: String? = nil
    @State private var selectedTeamCode: String? = nil
    let initialQuestion: String
    @Binding var navigationPath: NavigationPath

    private let deepBlue = Color(red: 0.1, green: 0.25, blue: 0.7)
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
                navigationPath.append(ResultsDestination(question: query))
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
            HStack(spacing: 12) {
                Image(systemName: "magnifyingglass")
                    .font(.system(size: 15, weight: .medium))
                    .foregroundStyle(lightBlue)

                TextField("", text: $inputText, prompt:
                    Text("Ask a follow-up or a new question")
                        .foregroundStyle(Color(uiColor: .placeholderText))
                )
                .font(.system(.body, design: .rounded))
                .foregroundStyle(.primary)
                .focused($isInputFocused)
                .autocorrectionDisabled(true)
                .textInputAutocapitalization(.never)
                .submitLabel(.search)
                .onSubmit { sendQuestion() }

                if !inputText.isEmpty {
                    Button(action: sendQuestion) {
                        Image(systemName: "arrow.right.circle.fill")
                            .font(.system(size: 22))
                            .foregroundStyle(lightBlue)
                    }
                }
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 12)
            .background(Color(uiColor: .secondarySystemBackground), in: RoundedRectangle(cornerRadius: 14))
            .shadow(color: deepBlue.opacity(0.12), radius: 12, y: 4)
            .shadow(color: .black.opacity(0.04), radius: 2, y: 1)
            .padding(.horizontal, 16)

        }
        .padding(.top, 10)
        .padding(.bottom, 6)
    }

    private func sendQuestion() {
        let trimmed = inputText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, !appState.isLoading else { return }
        inputText = ""

        switch PlayerNameMatcher.resolveSearch(trimmed, history: appState) {
        case .player(let name, _):
            selectedPlayerName = name
        case .team(let code):
            selectedTeamCode = code
        case .question(let query):
            appState.sendQuestion(query, isFollowUp: true)
        }
    }
}

private struct ResultsHeightKey: PreferenceKey {
    nonisolated(unsafe) static var defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) {
        value = max(value, nextValue())
    }
}
