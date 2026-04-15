import SwiftUI

struct LoadingIndicator: View {
    @State private var baseball1Opacity: Double = 0.0
    @State private var baseball2Opacity: Double = 0.0
    @State private var baseball3Opacity: Double = 0.0
    @State private var showPhrase = false
    @State private var currentPhrase = ""
    @State private var phraseOpacity: Double = 0
    @State private var remainingPhrases: [String] = []

    private let lightBlue = Color(red: 0.45, green: 0.7, blue: 1.0)
    private let deepBlue = Color(red: 0.1, green: 0.25, blue: 0.7)

    private static let phrases = [
        "Digging through the stats...",
        "Bunting the runner over...",
        "Getting signs from the 3rd base coach...",
        "Warming up in the bullpen...",
        "Adjusting my batting gloves...",
        "Hitting the cut-off man...",
        "Relaying the signs...",
        "Tapping home plate...",
        "Breaking in a new glove...",
        "Adjusting my pitch-com...",
        "Writing out the lineup card...",
        "Spitting some sunflower seeds...",
        "Arguing the call...",
        "Dusting off home plate...",
        "Putting on my oven mitts...",
        "Sacrificing...",
    ]

    private enum SparklePhase: CaseIterable {
        case hidden, building, bright, fading
    }

    var body: some View {
        VStack(spacing: 12) {
        ZStack {
            // Sparkle center — builds up, holds, dissolves, repeats
            PhaseAnimator(SparklePhase.allCases) { phase in
                Image(systemName: "sparkle")
                    .font(.system(size: 24, weight: .bold))
                    .foregroundStyle(
                        LinearGradient(
                            colors: [lightBlue, deepBlue],
                            startPoint: .topLeading, endPoint: .bottomTrailing
                        )
                    )
                    .scaleEffect(sparkleScale(for: phase))
                    .opacity(sparkleOpacity(for: phase))
            } animation: { phase in
                sparkleAnimation(for: phase)
            }

            // Baseball top-right
            Image(systemName: "baseball.fill")
                .font(.system(size: 10))
                .foregroundStyle(lightBlue)
                .opacity(baseball1Opacity)
                .offset(x: 12, y: -12)

            // Baseball top-left (smaller)
            Image(systemName: "baseball.fill")
                .font(.system(size: 7))
                .foregroundStyle(lightBlue.opacity(0.7))
                .opacity(baseball2Opacity)
                .offset(x: -10, y: -10)

            // Baseball bottom-right
            Image(systemName: "baseball.fill")
                .font(.system(size: 8))
                .foregroundStyle(lightBlue.opacity(0.85))
                .opacity(baseball3Opacity)
                .offset(x: 10, y: 10)
        }
        .frame(width: 44, height: 44)

        // Baseball phrases — appear after 3s, rotate every 3s
        if showPhrase {
            Text(currentPhrase)
                .font(.system(.subheadline, design: .rounded, weight: .medium))
                .foregroundStyle(.primary)
                .opacity(phraseOpacity)
                .transition(.opacity)
        }
        } // VStack
        .onAppear {
            withAnimation(.easeInOut(duration: 1.8).repeatForever(autoreverses: true).delay(0.3)) {
                baseball1Opacity = 1.0
            }
            withAnimation(.easeInOut(duration: 2.2).repeatForever(autoreverses: true).delay(0.8)) {
                baseball2Opacity = 0.8
            }
            withAnimation(.easeInOut(duration: 2.0).repeatForever(autoreverses: true).delay(1.2)) {
                baseball3Opacity = 0.9
            }
        }
        .task {
            // Show first phrase after 3s
            try? await Task.sleep(for: .seconds(3))
            guard !Task.isCancelled else { return }

            // Shuffle deck — draw without replacement, refill when empty
            remainingPhrases = Self.phrases.shuffled()
            currentPhrase = remainingPhrases.removeFirst()
            showPhrase = true
            withAnimation(.easeIn(duration: 0.4)) { phraseOpacity = 1 }

            // Rotate phrases every 3.4s
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(3.4))
                guard !Task.isCancelled else { break }
                withAnimation(.easeOut(duration: 0.3)) { phraseOpacity = 0 }
                try? await Task.sleep(for: .milliseconds(300))
                guard !Task.isCancelled else { break }
                if remainingPhrases.isEmpty {
                    remainingPhrases = Self.phrases.shuffled()
                }
                currentPhrase = remainingPhrases.removeFirst()
                withAnimation(.easeIn(duration: 0.3)) { phraseOpacity = 1 }
            }
        }
    }

    private func sparkleScale(for phase: SparklePhase) -> CGFloat {
        switch phase {
        case .hidden: 0.1
        case .building: 0.9
        case .bright: 1.0
        case .fading: 0.5
        }
    }

    private func sparkleOpacity(for phase: SparklePhase) -> Double {
        switch phase {
        case .hidden: 0.0
        case .building: 0.85
        case .bright: 1.0
        case .fading: 0.0
        }
    }

    private func sparkleAnimation(for phase: SparklePhase) -> Animation {
        switch phase {
        case .hidden: .easeIn(duration: 0.15)
        case .building: .easeOut(duration: 0.7)
        case .bright: .easeInOut(duration: 0.4)
        case .fading: .easeIn(duration: 0.6)
        }
    }
}
