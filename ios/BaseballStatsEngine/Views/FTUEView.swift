import SwiftUI

struct FTUEView: View {
    let onDismiss: () -> Void

    private let deepBlue = Color.brandDeepBlue
    private let lightBlue = Color(red: 0.45, green: 0.7, blue: 1.0)

    @State private var visibleItems = 0
    @State private var revealTask: Task<Void, Never>?

    private let examples = [
        "Most 4-hit games this year",
        "Players with 30 steals in each of the last 3 years",
        "Pitchers with 200 K and sub-3.00 ERA last season",
    ]

    var body: some View {
        VStack(spacing: 0) {
            Spacer()
            Spacer()

            // Title
            Text("Baseball stats, just ask.")
                .font(.system(size: 26, weight: .bold))
                .foregroundStyle(.white)
                .opacity(visibleItems >= 1 ? 1 : 0)
                .padding(.bottom, 30)

            // Section 1: "Try..."
            VStack(alignment: .leading, spacing: 0) {
                Text("Try...")
                    .font(.system(size: 20, weight: .semibold, design: .rounded))
                    .tracking(0.5)
                    .foregroundStyle(.white.opacity(0.9))
                    .opacity(visibleItems >= 2 ? 1 : 0)
                    .padding(.bottom, 16)

                // Example queries — indented
                VStack(alignment: .leading, spacing: 14) {
                    ForEach(Array(examples.enumerated()), id: \.offset) { idx, example in
                        HStack(alignment: .firstTextBaseline, spacing: 10) {
                            Text("•")
                                .font(.system(size: 16, weight: .medium))
                                .foregroundStyle(.white.opacity(0.45))
                            Text("\"\(example)\"")
                                .font(.system(size: 17, weight: .light, design: .rounded))
                                .tracking(0.5)
                                .foregroundStyle(.white.opacity(0.75))
                                .italic()
                        }
                        .opacity(visibleItems >= idx + 3 ? 1 : 0)
                    }
                }
                .padding(.leading, 14)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 36)
            .padding(.bottom, 32)

            // Section 2: Profile callout
            VStack(alignment: .leading, spacing: 0) {
                Text("Search any player for the full picture")
                    .font(.system(size: 20, weight: .semibold, design: .rounded))
                    .tracking(0.5)
                    .foregroundStyle(.white.opacity(0.9))
                    .opacity(visibleItems >= 6 ? 1 : 0)
                    .padding(.bottom, 16)

                HStack(alignment: .firstTextBaseline, spacing: 10) {
                    Text("•")
                        .font(.system(size: 16, weight: .medium))
                        .foregroundStyle(.white.opacity(0.45))
                    Text("Try \"Judge\" or \"Verlander\" — streaks, splits, matchups, and more")
                        .font(.system(size: 16, weight: .regular, design: .rounded))
                        .tracking(0.3)
                        .foregroundStyle(.white.opacity(0.8))
                        .italic()
                }
                .padding(.leading, 14)
                .opacity(visibleItems >= 7 ? 1 : 0)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 36)

            Spacer()

            // Got it button
            Button {
                onDismiss()
            } label: {
                Text("Got it!")
                    .font(.system(.body, design: .rounded, weight: .semibold))
                    .foregroundStyle(deepBlue)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 14)
                    .background(
                        RoundedRectangle(cornerRadius: 12)
                            .fill(.white)
                    )
            }
            .buttonStyle(.plain)
            .padding(.horizontal, 40)
            .padding(.bottom, 50)
            .opacity(visibleItems >= 8 ? 1 : 0)
            .allowsHitTesting(visibleItems >= 8)
        }
        .contentShape(Rectangle())
        .onTapGesture {
            revealAllImmediately()
        }
        .overlay(alignment: .topTrailing) {
            Button {
                onDismiss()
            } label: {
                Text("Skip")
                    .font(.system(size: 15, weight: .medium, design: .rounded))
                    .foregroundStyle(.white.opacity(0.55))
                    .padding(.horizontal, 16)
                    .padding(.vertical, 10)
            }
            .buttonStyle(.plain)
            .padding(.trailing, 8)
            .padding(.top, 4)
        }
        .onAppear {
            animateSequence()
        }
        .onDisappear {
            revealTask?.cancel()
        }
    }

    private func animateSequence() {
        // (target visibleItems, seconds to dwell before revealing it). Total ~7s,
        // down from ~16s. Driven by a cancellable Task so a tap can skip to the end.
        let steps: [(item: Int, delay: Double)] = [
            (1, 0.8),  // title
            (2, 0.9),  // "Try..."
            (3, 0.9),  // example bullet 1
            (4, 0.9),  // example bullet 2
            (5, 0.9),  // example bullet 3
            (6, 1.0),  // "Search any player..."
            (7, 0.9),  // "Try Judge..."
            (8, 0.8),  // "Got it!" button
        ]
        revealTask = Task { @MainActor in
            for step in steps {
                try? await Task.sleep(for: .seconds(step.delay))
                if Task.isCancelled { return }
                withAnimation(.easeOut(duration: 0.5)) {
                    visibleItems = step.item
                }
            }
        }
    }

    /// Tap anywhere: cancel the timed reveal and show everything at once.
    private func revealAllImmediately() {
        guard visibleItems < 8 else { return }
        revealTask?.cancel()
        withAnimation(.easeOut(duration: 0.35)) {
            visibleItems = 8
        }
    }
}
