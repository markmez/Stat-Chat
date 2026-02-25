import SwiftUI
import UIKit

/// Interactive swipe-from-left-edge gesture that slides the view right with the finger,
/// with a left-edge shadow for depth. Horizontal only — no vertical drift.
struct SwipeBackGesture: ViewModifier {
    @Environment(\.dismiss) private var dismiss
    @State private var offset: CGFloat = 0
    @State private var isDragging = false

    func body(content: Content) -> some View {
        content
            .offset(x: offset)
            .shadow(
                color: .black.opacity(offset > 0 ? min(0.18, offset / 400) : 0),
                radius: 12, x: -6, y: 0
            )
            .simultaneousGesture(
                DragGesture(minimumDistance: 12, coordinateSpace: .global)
                    .onChanged { value in
                        guard value.startLocation.x < 30 else {
                            // Started outside the edge zone — ignore entirely
                            return
                        }
                        isDragging = true
                        offset = max(0, value.translation.width)
                    }
                    .onEnded { value in
                        guard isDragging else { return }
                        isDragging = false

                        let shouldDismiss = value.translation.width > 80
                            || value.predictedEndTranslation.width > 300

                        if shouldDismiss {
                            let screenWidth = UIScreen.main.bounds.width
                            withAnimation(.easeOut(duration: 0.2)) {
                                offset = screenWidth
                            }
                            DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) {
                                dismiss()
                            }
                        } else {
                            withAnimation(.interactiveSpring(response: 0.3, dampingFraction: 0.85)) {
                                offset = 0
                            }
                        }
                    }
            )
    }
}

extension View {
    func swipeBack() -> some View {
        modifier(SwipeBackGesture())
    }
}
