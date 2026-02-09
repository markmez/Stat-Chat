import SwiftUI

struct ResultsView: View {
    @Environment(AppState.self) private var appState
    @State private var inputText = ""
    @FocusState private var isInputFocused: Bool
    @State private var resultsContentHeight: CGFloat = 0
    let initialQuestion: String

    private var visibleMessages: [Message] {
        appState.messages.filter { !$0.content.isEmpty || $0.role == .user }
    }

    /// Whether the input area fits inline below results without scrolling
    private func fitsInline(in availableHeight: CGFloat) -> Bool {
        let inputEstimate: CGFloat = 110
        return resultsContentHeight + inputEstimate + 30 < availableHeight
    }

    var body: some View {
        GeometryReader { geometry in
            let available = geometry.size.height
            let inline = fitsInline(in: available)

            ZStack {
                Color(red: 0.06, green: 0.07, blue: 0.12)
                    .ignoresSafeArea()

                VStack(spacing: 0) {
                    ScrollViewReader { proxy in
                        ScrollView {
                            VStack(spacing: 20) {
                                ForEach(visibleMessages) { message in
                                    ResultCard(message: message)
                                        .id(message.id)
                                }

                                if appState.isLoading && appState.currentStreamingText.isEmpty {
                                    LoadingIndicator()
                                        .frame(maxWidth: .infinity, alignment: .leading)
                                        .padding(.horizontal, 20)
                                        .padding(.top, 4)
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
                        .onChange(of: appState.messages.count) {
                            // Scroll to the latest user question so it's visible at the top
                            if let latestUser = visibleMessages.last(where: { $0.role == .user }) {
                                withAnimation(.easeOut(duration: 0.2)) {
                                    proxy.scrollTo(latestUser.id, anchor: .top)
                                }
                            }
                        }
                    }

                    // When results are too tall, pin input at the bottom
                    if !appState.isLoading && !inline {
                        inputAndSuggestions
                            .background(Color(red: 0.06, green: 0.07, blue: 0.12))
                            .overlay(
                                Rectangle()
                                    .frame(height: 0.5)
                                    .foregroundStyle(.white.opacity(0.08)),
                                alignment: .top
                            )
                    }
                }
            }
        }
        .navigationBarTitleDisplayMode(.inline)
        .toolbarBackground(.hidden, for: .navigationBar)
        .toolbar {
            ToolbarItem(placement: .principal) {
                Text("StatChat")
                    .font(.system(.subheadline, design: .rounded, weight: .semibold))
                    .foregroundStyle(.white.opacity(0.6))
            }
        }
        .onAppear {
            if appState.messages.isEmpty && !initialQuestion.isEmpty {
                appState.sendQuestion(initialQuestion)
            }
        }
        .onDisappear {
            appState.clearConversation()
        }
    }

    private var inputAndSuggestions: some View {
        VStack(spacing: 10) {
            HStack(spacing: 12) {
                Image(systemName: "magnifyingglass")
                    .font(.system(size: 15, weight: .medium))
                    .foregroundStyle(.white.opacity(0.3))

                TextField("", text: $inputText, prompt:
                    Text("Ask a follow-up or a new question")
                        .foregroundStyle(.white.opacity(0.3))
                )
                .font(.system(.body, design: .rounded))
                .foregroundStyle(.white)
                .tint(.white)
                .focused($isInputFocused)
                .onSubmit { sendQuestion() }

                if !inputText.isEmpty {
                    Button(action: sendQuestion) {
                        Image(systemName: "arrow.right.circle.fill")
                            .font(.system(size: 22))
                            .foregroundStyle(.white.opacity(0.7))
                    }
                }
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 12)
            .background(.white.opacity(0.06), in: RoundedRectangle(cornerRadius: 14))
            .overlay(
                RoundedRectangle(cornerRadius: 14)
                    .stroke(.white.opacity(0.08), lineWidth: 0.5)
            )
            .padding(.horizontal, 16)

            AnimatedPlaceholder { query in
                inputText = query
            }
        }
        .padding(.top, 10)
        .padding(.bottom, 6)
    }

    private func sendQuestion() {
        let trimmed = inputText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, !appState.isLoading else { return }
        inputText = ""
        appState.sendQuestion(trimmed)
    }
}

private struct ResultsHeightKey: PreferenceKey {
    nonisolated(unsafe) static var defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) {
        value = max(value, nextValue())
    }
}
