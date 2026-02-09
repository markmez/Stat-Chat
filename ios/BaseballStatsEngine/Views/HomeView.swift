import SwiftUI

struct HomeView: View {
    @Environment(AppState.self) private var appState
    @State private var questionText = ""
    @State private var navigateToResults = false
    @State private var initialQuestion = ""
    @State private var historyExpanded = false

    private let deepBlue = Color(red: 0.1, green: 0.25, blue: 0.7)
    private let lightBlue = Color(red: 0.45, green: 0.7, blue: 1.0)

    /// Height of the peeking history card
    private let peekHeight: CGFloat = 160
    /// Height when fully expanded
    private let expandedHeight: CGFloat = 420

    private var cardHeight: CGFloat {
        historyExpanded ? expandedHeight : peekHeight
    }

    var body: some View {
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

                // Sample queries
                AnimatedPlaceholder { query in
                    questionText = query
                }
                .padding(.top, 20)

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
        .navigationDestination(isPresented: $navigateToResults) {
            ResultsView(initialQuestion: initialQuestion)
        }
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                NavigationLink {
                    APIKeySetupView(isInitialSetup: false)
                } label: {
                    Image(systemName: "gearshape")
                        .foregroundStyle(.secondary)
                }
            }
        }
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
                }
                .padding(.horizontal, 20)
            }
            .contentShape(Rectangle())
            .onTapGesture {
                withAnimation(.spring(response: 0.35, dampingFraction: 0.85)) {
                    historyExpanded.toggle()
                }
            }
            .gesture(
                DragGesture(minimumDistance: 10)
                    .onEnded { value in
                        withAnimation(.spring(response: 0.35, dampingFraction: 0.85)) {
                            if value.translation.height < -30 {
                                historyExpanded = true
                            } else if value.translation.height > 30 {
                                historyExpanded = false
                            }
                        }
                    }
            )

            // History items
            ScrollView {
                LazyVStack(spacing: 0) {
                    ForEach(Array(appState.searchHistory.enumerated()), id: \.offset) { _, query in
                        Button {
                            initialQuestion = query
                            navigateToResults = true
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
        .background(
            RoundedRectangle(cornerRadius: 20, style: .continuous)
                .fill(Color(uiColor: .secondarySystemBackground))
                .shadow(color: .black.opacity(0.08), radius: 12, y: -4)
        )
        .animation(.spring(response: 0.35, dampingFraction: 0.85), value: historyExpanded)
    }

    private func submitQuestion() {
        let trimmed = questionText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        initialQuestion = trimmed
        questionText = ""
        navigateToResults = true
    }
}
