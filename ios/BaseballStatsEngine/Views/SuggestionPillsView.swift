import SwiftUI

/// Shows multiple tappable suggestion pills that rotate periodically.
/// Displays 2 rows of gradient capsule pills, fading one out at a time.
struct SuggestionPillsView: View {
    var searchHistory: [String] = []
    var compact: Bool = false
    var matchupPills: [String] = []
    let onTap: (String) -> Void

    @State private var pool: [Suggestion] = []
    @State private var visible: [Suggestion] = []
    @State private var visibleSet: Set<String> = []
    @State private var nextSwapIndex = 0
    @State private var fadingId: String?

    private var maxVisible: Int { compact ? 4 : 6 }
    private let swapInterval: TimeInterval = 4.0
    private let fadeDuration: TimeInterval = 0.5

    private let deepBlue = Color(red: 0.1, green: 0.25, blue: 0.7)
    private let lightBlue = Color(red: 0.45, green: 0.7, blue: 1.0)

    var body: some View {
        if !visible.isEmpty {
            FlowLayout(spacing: 8) {
                ForEach(visible, id: \.id) { suggestion in
                    Button {
                        SuggestionEngine.shared.recordTap(suggestion.id)
                        AnalyticsService.trackSuggestionTap(text: suggestion.text, source: .animatedPlaceholder)
                        onTap(suggestion.text)
                    } label: {
                        Text(suggestion.text)
                            .font(.system(.caption, design: .rounded, weight: .medium))
                            .foregroundStyle(.white)
                            .lineLimit(1)
                            .truncationMode(.tail)
                            .padding(.horizontal, 12)
                            .padding(.vertical, 7)
                            .frame(maxWidth: UIScreen.main.bounds.width - 56)
                            .background(
                                LinearGradient(
                                    colors: [lightBlue, deepBlue],
                                    startPoint: .leading, endPoint: .trailing
                                ),
                                in: Capsule()
                            )
                    }
                    .buttonStyle(.plain)
                    .opacity(fadingId == suggestion.id ? 0 : 1)
                }
            }
            .padding(.horizontal, 24)
            .task(id: "rotate") {
                await rotatePills()
            }
            .onChange(of: matchupPills) { _, newPills in
                // Inject matchup pills when feed loads (may arrive after initial pool build)
                let matchupSuggestions = newPills.map { Suggestion(id: "matchup_\($0)", text: $0) }
                pool.removeAll { $0.id.hasPrefix("matchup_") }
                pool.insert(contentsOf: matchupSuggestions, at: 0)
            }
        } else {
            Color.clear.frame(height: 20)
                .task {
                    pool = SuggestionEngine.shared.buildPool(searchHistory: searchHistory)
                    // Inject matchup preview pills at the front of the pool
                    let matchupSuggestions = matchupPills.map {
                        Suggestion(id: "matchup_\($0)", text: $0)
                    }
                    pool.insert(contentsOf: matchupSuggestions, at: 0)
                    let initial = Array(pool.prefix(maxVisible))
                    visible = initial
                    visibleSet = Set(initial.map(\.id))
                }
        }
    }

    @MainActor
    private func rotatePills() async {
        guard pool.count > maxVisible else { return }

        while !Task.isCancelled {
            try? await Task.sleep(for: .seconds(swapInterval))
            guard !Task.isCancelled else { break }

            let candidates = pool.filter { !visibleSet.contains($0.id) }
            guard let replacement = candidates.randomElement() else { continue }

            let idx = nextSwapIndex % visible.count
            let removed = visible[idx]

            // Fade out the old pill
            withAnimation(.easeOut(duration: fadeDuration)) {
                fadingId = removed.id
            }

            try? await Task.sleep(for: .seconds(fadeDuration))
            guard !Task.isCancelled else { break }

            // Swap content and fade in
            visible[idx] = replacement
            visibleSet.remove(removed.id)
            visibleSet.insert(replacement.id)
            fadingId = replacement.id

            withAnimation(.easeIn(duration: fadeDuration)) {
                fadingId = nil
            }

            SuggestionEngine.shared.recordImpression(replacement.id)
            nextSwapIndex += 1
        }
    }
}
