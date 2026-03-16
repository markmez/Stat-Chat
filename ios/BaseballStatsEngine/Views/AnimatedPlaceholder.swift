import SwiftUI

struct AnimatedPlaceholder: View {
    var searchHistory: [String] = []
    let onTap: (String) -> Void

    @State private var currentIndex = 0
    @State private var opacity: Double = 0
    @State private var suggestions: [Suggestion] = []

    private var displayDuration: TimeInterval {
        SuggestionEngine.shared.config.algorithm.displayDuration
    }
    private var fadeDuration: TimeInterval {
        SuggestionEngine.shared.config.algorithm.fadeDuration
    }

    var body: some View {
        if !suggestions.isEmpty {
            let current = suggestions[currentIndex % suggestions.count]
            Button {
                SuggestionEngine.shared.recordTap(current.id)
                AnalyticsService.trackSuggestionTap(text: current.text, source: .animatedPlaceholder)
                onTap(current.text)
            } label: {
                Text(current.text)
                    .font(.system(.subheadline, design: .rounded))
                    .foregroundStyle(.tertiary)
                    .opacity(opacity)
                    .lineLimit(1)
            }
            .buttonStyle(.plain)
            .task(id: "cycle") {
                await startCycling()
            }
        } else {
            Color.clear.frame(height: 20)
                .task {
                    suggestions = SuggestionEngine.shared.buildPool(searchHistory: searchHistory)
                }
        }
    }

    @MainActor
    private func startCycling() async {
        try? await Task.sleep(for: .milliseconds(100))
        withAnimation(.easeIn(duration: fadeDuration)) {
            opacity = 1.0
        }

        while !Task.isCancelled {
            // Record impression for the currently visible suggestion
            let current = suggestions[currentIndex % suggestions.count]
            SuggestionEngine.shared.recordImpression(current.id)

            try? await Task.sleep(for: .seconds(displayDuration))
            guard !Task.isCancelled else { break }

            withAnimation(.easeOut(duration: fadeDuration)) {
                opacity = 0
            }
            try? await Task.sleep(for: .seconds(fadeDuration))
            guard !Task.isCancelled else { break }

            currentIndex = (currentIndex + 1) % suggestions.count
            withAnimation(.easeIn(duration: fadeDuration)) {
                opacity = 1.0
            }
        }
    }
}
