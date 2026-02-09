import SwiftUI

struct HomeView: View {
    @Environment(AppState.self) private var appState
    @State private var questionText = ""
    @State private var navigateToResults = false
    @State private var initialQuestion = ""

    private let deepBlue = Color(red: 0.1, green: 0.25, blue: 0.7)
    private let lightBlue = Color(red: 0.45, green: 0.7, blue: 1.0)

    var body: some View {
        ZStack {
            Color(uiColor: .systemBackground)
                .ignoresSafeArea()

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
                                .offset(x: 14, y: -11)

                            Image(systemName: "baseball.fill")
                                .font(.system(size: 10))
                                .foregroundStyle(lightBlue.opacity(0.7))
                                .offset(x: -17, y: -10)

                            Image(systemName: "baseball.fill")
                                .font(.system(size: 10.5))
                                .foregroundStyle(lightBlue.opacity(0.85))
                                .offset(x: 12, y: 13)
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
                VStack(spacing: 0) {
                    AnimatedPlaceholder { query in
                        questionText = query
                    }
                    .padding(.top, 20)
                }

                Spacer()
                Spacer()
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

    private func submitQuestion() {
        let trimmed = questionText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        initialQuestion = trimmed
        questionText = ""
        navigateToResults = true
    }
}
