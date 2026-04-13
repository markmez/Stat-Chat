import SwiftUI

/// Shows 3 tappable suggestion pills that rotate one at a time with fade transitions.
/// Pills follow a curated sequence, then randomize after full cycle.
struct SuggestionPillsView: View {
    var searchHistory: [String] = []
    let onTap: (String) -> Void

    @State private var allPills: [Suggestion] = []
    @State private var visible: [Suggestion] = []
    @State private var nextIndex = 3  // next pill to pull from allPills
    @State private var nextSwapSlot = 0  // which of the 3 visible slots to swap next
    @State private var fadingId: String?
    @State private var shownIds: Set<String> = []  // pills shown this session

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
                            .font(.system(.subheadline, design: .rounded, weight: .medium))
                            .foregroundStyle(.white)
                            .lineLimit(1)
                            .truncationMode(.tail)
                            .padding(.horizontal, 14)
                            .padding(.vertical, 9)
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
            .onAppear {
                fadingId = nil
            }
            .task(id: "rotate") {
                await rotatePills()
            }
        } else {
            // Reserve height matching loaded pills to prevent logo shift
            Color.clear.frame(height: 70)
                .task {
                    allPills = SuggestionEngine.shared.buildSequence(searchHistory: searchHistory)
                    guard allPills.count >= 3 else { return }
                    visible = Array(allPills.prefix(3))
                    shownIds = Set(visible.map(\.id))
                    nextIndex = 3
                }
        }
    }

    @MainActor
    private func rotatePills() async {
        guard allPills.count > 3 else { return }

        while !Task.isCancelled {
            try? await Task.sleep(for: .seconds(swapInterval))
            guard !Task.isCancelled else { break }

            // Find the next pill to show
            let replacement = nextPill()
            guard let replacement else { continue }

            let slot = nextSwapSlot % visible.count
            let removed = visible[slot]

            // Fade out
            withAnimation(.easeOut(duration: fadeDuration)) {
                fadingId = removed.id
            }

            try? await Task.sleep(for: .seconds(fadeDuration))
            guard !Task.isCancelled else { break }

            // Swap and fade in
            visible[slot] = replacement
            shownIds.insert(replacement.id)
            fadingId = replacement.id

            withAnimation(.easeIn(duration: fadeDuration)) {
                fadingId = nil
            }

            SuggestionEngine.shared.recordImpression(replacement.id)
            nextSwapSlot += 1
        }
    }

    private func nextPill() -> Suggestion? {
        let visibleIds = Set(visible.map(\.id))

        // Walk through the sequence, skipping currently visible pills
        while nextIndex < allPills.count {
            let candidate = allPills[nextIndex]
            nextIndex += 1
            if !visibleIds.contains(candidate.id) {
                return candidate
            }
        }

        // Full cycle complete — reshuffle and restart, excluding tapped
        let tapped = SuggestionEngine.shared.tappedIds
        allPills = allPills.filter { !tapped.contains($0.id) }.shuffled()
        shownIds.removeAll()
        nextIndex = 0

        while nextIndex < allPills.count {
            let candidate = allPills[nextIndex]
            nextIndex += 1
            if !visibleIds.contains(candidate.id) {
                return candidate
            }
        }

        return nil
    }
}
