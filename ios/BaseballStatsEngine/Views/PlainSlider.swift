import SwiftUI

// Replacement for SwiftUI's `Slider` for the Current Hot Streak control.
// SwiftUI's slider is UIKit-backed and animates the thumb size on touch-down,
// which Mark flagged as a wobble/transparency effect. This version uses a
// fixed-size white thumb that doesn't change appearance during drag.
//
// Gesture handling: uses `simultaneousGesture` so the parent ScrollView can
// also receive the touch. Locks per-drag direction on first meaningful
// movement — horizontal drags drive the slider, vertical drags pass through
// to the ScrollView so the page can still scroll over the thumb.
//
// Used by both the player profile (PlayerCardView) and chat search results
// (StatGridView via FORM: metadata) so the streak slider UX is identical
// in both surfaces.
struct PlainSlider: View {
    @Binding var value: Double
    let range: ClosedRange<Double>
    let step: Double
    var trackTint: Color = Color(red: 0.13, green: 0.36, blue: 0.79)  // matches deepBlue
    var isDisabled: Bool = false
    /// Optional floating-chip pair shown above the thumb during active drag.
    /// Pass both to enable; pass nil to hide the chip layer entirely.
    var leftChipText: String? = nil
    var rightChipText: String? = nil
    /// Gradient for the chips. Defaults to a deepBlue → lightBlue gradient
    /// matching the brand palette used elsewhere on the player card.
    var chipGradient: LinearGradient = LinearGradient(
        colors: [
            Color(red: 0.45, green: 0.7, blue: 1.0),     // matches lightBlue
            Color(red: 0.13, green: 0.36, blue: 0.79),   // matches deepBlue
        ],
        startPoint: .leading, endPoint: .trailing
    )

    private let thumbSize: CGFloat = 22
    private let trackHeight: CGFloat = 4
    private let chipHeight: CGFloat = 22
    private let chipGap: CGFloat = 8           // vertical gap between chips and thumb top

    // nil = direction not yet decided for this drag; true = horizontal (slider);
    // false = vertical (scroll, ignore for the rest of this drag).
    @State private var horizontalLock: Bool? = nil
    @State private var isActivelyDragging = false
    @State private var chipPairWidth: CGFloat = 0

    private var chipsEnabled: Bool { leftChipText != nil && rightChipText != nil }
    /// Total view height — extend the frame upward to make room for chips
    /// (they overflow above the slider track when shown).
    private var totalHeight: CGFloat {
        chipsEnabled ? thumbSize + chipGap + chipHeight : thumbSize
    }

    var body: some View {
        GeometryReader { geo in
            let usable = max(0, geo.size.width - thumbSize)
            let thumbCenterX = CGFloat(progress) * usable + thumbSize / 2
            // Clamp the chip-pair X so it stays within the slider's visible bounds.
            let chipLeftX = min(
                max(0, thumbCenterX - chipPairWidth / 2),
                max(0, geo.size.width - chipPairWidth)
            )

            ZStack(alignment: .bottomLeading) {
                // Floating chip pair — only visible during active horizontal drag.
                if chipsEnabled, let l = leftChipText, let r = rightChipText {
                    HStack(spacing: 6) {
                        chip(text: l)
                        chip(text: r)
                    }
                    .fixedSize()
                    .background(
                        GeometryReader { gp in
                            Color.clear.preference(
                                key: ChipPairWidthKey.self, value: gp.size.width
                            )
                        }
                    )
                    .onPreferenceChange(ChipPairWidthKey.self) { chipPairWidth = $0 }
                    .offset(x: chipLeftX, y: -(thumbSize + chipGap))
                    .opacity(isActivelyDragging ? 1 : 0)
                    .animation(.easeOut(duration: 0.15), value: isActivelyDragging)
                    .allowsHitTesting(false)
                }

                // Slider stack — pinned to the bottom of the frame.
                ZStack(alignment: .leading) {
                    // Unfilled track
                    Capsule()
                        .fill(Color(.systemGray5))
                        .frame(height: trackHeight)

                    // Filled portion — extend to thumb center
                    Capsule()
                        .fill(trackTint)
                        .frame(width: max(0, CGFloat(progress) * usable + thumbSize / 2), height: trackHeight)

                    // Thumb — fixed size, no press animation
                    Circle()
                        .fill(Color.white)
                        .frame(width: thumbSize, height: thumbSize)
                        .shadow(color: Color.black.opacity(0.18), radius: 2, x: 0, y: 1)
                        .offset(x: CGFloat(progress) * usable)
                }
                .frame(height: thumbSize)
                .contentShape(Rectangle())
                .simultaneousGesture(
                    DragGesture(minimumDistance: 0)
                        .onChanged { drag in
                            guard !isDisabled, usable > 0 else { return }
                            let dx = abs(drag.translation.width)
                            let dy = abs(drag.translation.height)
                            // Wait for enough motion to decide direction. Don't
                            // commit a value change while undecided — that would
                            // jump the thumb on a vertical-scroll attempt.
                            if horizontalLock == nil {
                                if max(dx, dy) < 6 { return }
                                horizontalLock = dx >= dy
                                if horizontalLock == true {
                                    isActivelyDragging = true
                                }
                            }
                            guard horizontalLock == true else { return }
                            let pct = max(0, min(1, (drag.location.x - thumbSize / 2) / usable))
                            let span = range.upperBound - range.lowerBound
                            let raw = range.lowerBound + Double(pct) * span
                            let stepped = (raw / step).rounded() * step
                            value = max(range.lowerBound, min(range.upperBound, stepped))
                        }
                        .onEnded { _ in
                            horizontalLock = nil
                            isActivelyDragging = false
                        }
                )
                .opacity(isDisabled ? 0.3 : 1)
            }
            .frame(height: totalHeight, alignment: .bottom)
        }
        .frame(height: totalHeight)
        // Suppress any inherited implicit animations from parent state changes.
        .animation(nil, value: value)
    }

    private func chip(text: String) -> some View {
        Text(text)
            .font(.system(.caption, design: .rounded, weight: .semibold))
            .foregroundStyle(.white)
            .padding(.horizontal, 10)
            .padding(.vertical, 3)
            .background(chipGradient, in: Capsule())
            .shadow(color: Color.black.opacity(0.18), radius: 2, x: 0, y: 1)
    }

    private var progress: Double {
        let span = range.upperBound - range.lowerBound
        guard span > 0 else { return 0 }
        return min(1, max(0, (value - range.lowerBound) / span))
    }
}

struct ChipPairWidthKey: PreferenceKey {
    nonisolated(unsafe) static var defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) {
        value = nextValue()
    }
}
