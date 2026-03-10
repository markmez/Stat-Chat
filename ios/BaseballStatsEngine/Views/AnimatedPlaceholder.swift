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
        Group {
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
            }
        }
        .task {
            queries = SampleQuery.personalized(from: searchHistory)
            if queries.isEmpty { queries = SampleQuery.all.shuffled() }
            await startCycling()
        }
    }

    @MainActor
    private func startCycling() async {
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
