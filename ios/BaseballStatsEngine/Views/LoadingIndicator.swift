import SwiftUI

struct LoadingIndicator: View {
    @State private var baseball1Opacity: Double = 0.0
    @State private var baseball2Opacity: Double = 0.0
    @State private var baseball3Opacity: Double = 0.0

    private let lightBlue = Color(red: 0.45, green: 0.7, blue: 1.0)
    private let deepBlue = Color(red: 0.1, green: 0.25, blue: 0.7)

    private enum SparklePhase: CaseIterable {
        case hidden, building, bright, fading
    }

    var body: some View {
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
