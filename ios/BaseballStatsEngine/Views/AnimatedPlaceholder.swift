import SwiftUI

struct AnimatedPlaceholder: View {
    var searchHistory: [String] = []
    let onTap: (String) -> Void

    @State private var currentIndex = 0
    @State private var opacity: Double = 0
    @State private var queries: [String] = []
    private let displayDuration: TimeInterval = 3.5
    private let fadeDuration: TimeInterval = 0.5

    var body: some View {
        if !queries.isEmpty {
            Button {
                onTap(queries[currentIndex % queries.count])
            } label: {
                Text(queries[currentIndex % queries.count])
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
                    queries = SampleQuery.personalized(from: searchHistory)
                    if queries.isEmpty { queries = SampleQuery.all.shuffled() }
                }
        }
    }

    @MainActor
    private func startCycling() async {
        // Small delay to ensure view is laid out before animating
        try? await Task.sleep(for: .milliseconds(100))
        withAnimation(.easeIn(duration: fadeDuration)) {
            opacity = 1.0
        }

        while !Task.isCancelled {
            try? await Task.sleep(for: .seconds(displayDuration))
            guard !Task.isCancelled else { break }

            withAnimation(.easeOut(duration: fadeDuration)) {
                opacity = 0
            }
            try? await Task.sleep(for: .seconds(fadeDuration))
            guard !Task.isCancelled else { break }

            currentIndex = (currentIndex + 1) % queries.count
            withAnimation(.easeIn(duration: fadeDuration)) {
                opacity = 1.0
            }
        }
    }
}
