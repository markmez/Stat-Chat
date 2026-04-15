import SwiftUI

struct FTUEView: View {
    let onDismiss: () -> Void

    private let deepBlue = Color.brandDeepBlue
    private let lightBlue = Color(red: 0.45, green: 0.7, blue: 1.0)

    @State private var visibleItems = 0

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
        }
        .onAppear {
            animateSequence()
        }
    }

    private func animateSequence() {
        let basePause = 1.0
        let tryToBullet = 1.4    // Try... → first bullet
        let bulletGap = 2.2      // between each of the 3 bullets
        let toSection2 = 2.0     // 3rd bullet → "Search any player"
        let section2ToBullet = 2.2  // "Search any player" → "Try Judge..."

        // Title
        withAnimation(.easeOut(duration: 0.8).delay(basePause)) {
            visibleItems = 1
        }
        // "Try..."
        let tryTime = basePause + 2.0
        withAnimation(.easeOut(duration: 0.7).delay(tryTime)) {
            visibleItems = 2
        }
        // 3 example bullets
        for i in 0..<examples.count {
            withAnimation(.easeOut(duration: 0.7).delay(tryTime + tryToBullet + Double(i) * bulletGap)) {
                visibleItems = i + 3
            }
        }
        // "Search any player..."
        let section2Time = tryTime + tryToBullet + Double(examples.count - 1) * bulletGap + toSection2
        withAnimation(.easeOut(duration: 0.7).delay(section2Time)) {
            visibleItems = 6
        }
        // "Try Judge..."
        withAnimation(.easeOut(duration: 0.7).delay(section2Time + section2ToBullet)) {
            visibleItems = 7
        }
        // Got it
        withAnimation(.easeOut(duration: 0.7).delay(section2Time + section2ToBullet + 2.2)) {
            visibleItems = 8
        }
    }
}
