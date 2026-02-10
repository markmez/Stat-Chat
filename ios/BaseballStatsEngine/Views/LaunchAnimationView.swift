import SwiftUI

struct LaunchAnimationView: View {
    let onComplete: () -> Void

    @State private var logoOpacity: Double = 0.0
    @State private var baseball1Opacity: Double = 0.0
    @State private var baseball2Opacity: Double = 0.0
    @State private var baseball3Opacity: Double = 0.0
    @State private var backgroundOpacity: Double = 1.0
    @State private var iconScale: CGFloat = 2.0
    @State private var wordmarkOpacity: Double = 0.0
    @State private var taglineOpacity: Double = 0.0

    private let deepBlue = Color(red: 0.1, green: 0.25, blue: 0.7)
    private let lightBlue = Color(red: 0.45, green: 0.7, blue: 1.0)
    private let iconDark = Color(red: 0.06, green: 0.12, blue: 0.45)
    private let iconLight = Color(red: 0.12, green: 0.28, blue: 0.75)

    /// Interpolated color: white when on blue background, blue when on white
    private var iconTint: Double { backgroundOpacity }

    var body: some View {
        ZStack {
            // Background — starts as app icon gradient, fades to system background
            ZStack {
                LinearGradient(
                    colors: [iconDark, iconLight],
                    startPoint: .topLeading, endPoint: .bottomTrailing
                )
                .opacity(backgroundOpacity)

                Color(uiColor: .systemBackground)
                    .opacity(1 - backgroundOpacity)
            }
            .ignoresSafeArea()

            // Logo positioned at its final HomeView location
            // Same layout as HomeView: Spacer, logo group, padding, search area, Spacer
            VStack(spacing: 0) {
                Spacer()

                VStack(spacing: 6) {
                    HStack(spacing: 12) {
                        // Wordmark — hidden during icon phase, fades in during transition
                        Text("StatChat")
                            .font(.system(size: 42, weight: .bold))
                            .foregroundStyle(
                                LinearGradient(
                                    colors: [lightBlue, deepBlue],
                                    startPoint: .leading, endPoint: .trailing
                                )
                            )
                            .opacity(wordmarkOpacity)

                        // Sparkle + baseballs — starts large and white, shrinks to final size and turns blue
                        ZStack {
                            Image(systemName: "sparkle")
                                .font(.system(size: 28, weight: .bold))
                                .foregroundStyle(sparkleColor)
                                .opacity(logoOpacity)

                            Image(systemName: "baseball.fill")
                                .font(.system(size: 14))
                                .foregroundStyle(ballColor(baseOpacity: 1.0))
                                .offset(x: 13, y: -13)
                                .opacity(baseball1Opacity)

                            Image(systemName: "baseball.fill")
                                .font(.system(size: 10))
                                .foregroundStyle(ballColor(baseOpacity: 0.7))
                                .offset(x: -11, y: -11)
                                .opacity(baseball2Opacity)

                            Image(systemName: "baseball.fill")
                                .font(.system(size: 10.5))
                                .foregroundStyle(ballColor(baseOpacity: 0.85))
                                .offset(x: 11, y: 11)
                                .opacity(baseball3Opacity)
                        }
                        .scaleEffect(iconScale)
                    }

                    Text("Baseball stats, answered instantly")
                        .font(.system(.subheadline, design: .rounded))
                        .foregroundStyle(.secondary)
                        .opacity(taglineOpacity)
                }
                .padding(.bottom, 36)

                // Invisible spacer matching HomeView's search field + samples + bottom area
                Color.clear
                    .frame(height: 280)

                Spacer()
            }
        }
        .onAppear {
            runAnimation()
        }
    }

    private var sparkleColor: LinearGradient {
        // Blend from white→lightBlue (on blue bg) to lightBlue→deepBlue (on white bg)
        let startColor = Color(
            red: 1.0 * iconTint + 0.45 * (1 - iconTint),
            green: 1.0 * iconTint + 0.7 * (1 - iconTint),
            blue: 1.0 * iconTint + 1.0 * (1 - iconTint)
        )
        let endColor = Color(
            red: 0.45 * iconTint + 0.1 * (1 - iconTint),
            green: 0.7 * iconTint + 0.25 * (1 - iconTint),
            blue: 1.0 * iconTint + 0.7 * (1 - iconTint)
        )
        return LinearGradient(
            colors: [startColor, endColor],
            startPoint: .topLeading, endPoint: .bottomTrailing
        )
    }

    private func ballColor(baseOpacity: Double) -> Color {
        // White on blue background, lightBlue on white background
        Color(
            red: 1.0 * iconTint + 0.45 * (1 - iconTint),
            green: 1.0 * iconTint + 0.7 * (1 - iconTint),
            blue: 1.0 * iconTint + 1.0 * (1 - iconTint)
        )
        .opacity(baseOpacity)
    }

    private func runAnimation() {
        // Phase 1: Sparkle appears large on blue background (0 – 0.4s)
        withAnimation(.easeOut(duration: 0.4)) {
            logoOpacity = 1.0
        }

        // Phase 2: Baseballs fade in staggered (0.3 – 0.7s)
        withAnimation(.easeOut(duration: 0.35).delay(0.3)) {
            baseball1Opacity = 1.0
        }
        withAnimation(.easeOut(duration: 0.35).delay(0.4)) {
            baseball2Opacity = 1.0
        }
        withAnimation(.easeOut(duration: 0.35).delay(0.5)) {
            baseball3Opacity = 1.0
        }

        // Phase 3: Hold (0.7 – 1.1s), then transition (1.1 – 1.8s)
        // Background fades to white, icon shrinks to home size, colors shift to blue
        withAnimation(.easeInOut(duration: 0.7).delay(1.1)) {
            backgroundOpacity = 0.0
            iconScale = 1.0
        }

        // Wordmark and tagline appear as transition completes
        withAnimation(.easeOut(duration: 0.4).delay(1.4)) {
            wordmarkOpacity = 1.0
        }
        withAnimation(.easeOut(duration: 0.3).delay(1.6)) {
            taglineOpacity = 1.0
        }

        // Phase 4: Dismiss overlay
        DispatchQueue.main.asyncAfter(deadline: .now() + 2.1) {
            onComplete()
        }
    }
}
