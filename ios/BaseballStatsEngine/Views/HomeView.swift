import SwiftUI

struct HomeView: View {
    @Environment(AppState.self) private var appState
    @State private var questionText = ""
    @State private var navigateToResults = false
    @State private var initialQuestion = ""

    var body: some View {
        ZStack {
            // Gradient background
            LinearGradient(
                colors: [Color(red: 0.06, green: 0.07, blue: 0.12),
                         Color(red: 0.08, green: 0.05, blue: 0.14)],
                startPoint: .top, endPoint: .bottom
            )
            .ignoresSafeArea()

            VStack(spacing: 0) {
                Spacer()

                // Wordmark
                VStack(spacing: 6) {
                    Text("StatChat")
                        .font(.system(size: 42, weight: .bold, design: .rounded))
                        .foregroundStyle(
                            LinearGradient(
                                colors: [.white, .white.opacity(0.7)],
                                startPoint: .leading, endPoint: .trailing
                            )
                        )

                    Text("MLB stats, answered instantly")
                        .font(.system(.subheadline, design: .rounded))
                        .foregroundStyle(.white.opacity(0.4))
                }
                .padding(.bottom, 36)

                // Search field
                HStack(alignment: .top, spacing: 12) {
                    Image(systemName: "magnifyingglass")
                        .font(.system(size: 18, weight: .medium))
                        .foregroundStyle(.white.opacity(0.4))
                        .padding(.top, 2)

                    TextField("", text: $questionText, prompt:
                        Text("Ask anything...")
                            .foregroundStyle(.white.opacity(0.3)),
                        axis: .vertical
                    )
                    .font(.system(.body, design: .rounded))
                    .foregroundStyle(.white)
                    .tint(.white)
                    .lineLimit(1...10)
                    .onSubmit { submitQuestion() }

                    if !questionText.isEmpty {
                        Button {
                            submitQuestion()
                        } label: {
                            Image(systemName: "arrow.right.circle.fill")
                                .font(.system(size: 24))
                                .foregroundStyle(.white.opacity(0.8))
                        }
                        .padding(.top, 2)
                    }
                }
                .padding(.horizontal, 18)
                .padding(.vertical, 14)
                .frame(minHeight: 120, alignment: .top)
                .background(.white.opacity(0.08), in: RoundedRectangle(cornerRadius: 16))
                .overlay(
                    RoundedRectangle(cornerRadius: 16)
                        .stroke(.white.opacity(0.1), lineWidth: 1)
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
                        .foregroundStyle(.white.opacity(0.4))
                }
            }
        }
        .toolbarBackground(.hidden, for: .navigationBar)
    }

    private func submitQuestion() {
        let trimmed = questionText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        initialQuestion = trimmed
        questionText = ""
        navigateToResults = true
    }
}
