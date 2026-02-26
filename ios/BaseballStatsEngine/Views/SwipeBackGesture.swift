import SwiftUI
import UIKit

/// Interactive swipe-from-left-edge gesture using UIKit's UIScreenEdgePanGestureRecognizer.
/// Unlike a SwiftUI DragGesture, this properly coordinates with scroll views via
/// require(toFail:) — no vertical drift during the back swipe.
struct SwipeBackGesture: ViewModifier {
    @Environment(\.dismiss) private var dismiss
    @State private var offset: CGFloat = 0

    func body(content: Content) -> some View {
        content
            .offset(x: offset)
            .shadow(
                color: .black.opacity(offset > 0 ? min(0.18, offset / 400) : 0),
                radius: 12, x: -6, y: 0
            )
            .background(
                EdgeGestureInstaller(offset: $offset, onDismiss: { dismiss() })
                    .frame(width: 0, height: 0)
            )
    }
}

extension View {
    func swipeBack() -> some View {
        modifier(SwipeBackGesture())
    }
}

// MARK: - UIKit edge gesture installer

/// Zero-frame background view that installs a UIScreenEdgePanGestureRecognizer on the nearest
/// ancestor view controller's view. Configures scroll views to defer to the edge gesture.
private struct EdgeGestureInstaller: UIViewRepresentable {
    @Binding var offset: CGFloat
    let onDismiss: () -> Void

    func makeUIView(context: Context) -> UIView {
        let view = UIView(frame: .zero)
        view.isUserInteractionEnabled = false
        context.coordinator.hostView = view
        // Defer to next run loop so the full view hierarchy is established
        DispatchQueue.main.async {
            context.coordinator.install()
        }
        return view
    }

    func updateUIView(_ uiView: UIView, context: Context) {}

    static func dismantleUIView(_ uiView: UIView, coordinator: Coordinator) {
        coordinator.uninstall()
    }

    func makeCoordinator() -> Coordinator {
        Coordinator(offset: $offset, onDismiss: onDismiss)
    }

    final class Coordinator: NSObject {
        private let _offset: Binding<CGFloat>
        private let onDismiss: () -> Void
        weak var hostView: UIView?
        private var edgeGesture: UIScreenEdgePanGestureRecognizer?
        private weak var installedOnView: UIView?

        init(offset: Binding<CGFloat>, onDismiss: @escaping () -> Void) {
            self._offset = offset
            self.onDismiss = onDismiss
        }

        @MainActor
        func install() {
            guard edgeGesture == nil, let host = hostView else { return }

            // Walk up to find the view controller's view (reliable anchor point)
            var target: UIView = host
            var current: UIView? = host.superview
            while let view = current {
                target = view
                if view.next is UIViewController { break }
                current = view.superview
            }

            let gesture = UIScreenEdgePanGestureRecognizer(
                target: self, action: #selector(handlePan(_:))
            )
            gesture.edges = .left
            target.addGestureRecognizer(gesture)
            edgeGesture = gesture
            installedOnView = target

            // Make scroll views defer to the edge gesture so vertical scrolling
            // isn't hijacked during a horizontal back-swipe
            configureScrollViews(in: target, toRequireFailureOf: gesture)
        }

        @MainActor
        func uninstall() {
            if let gesture = edgeGesture, let view = installedOnView {
                view.removeGestureRecognizer(gesture)
            }
            edgeGesture = nil
            installedOnView = nil
        }

        @MainActor
        private func configureScrollViews(in view: UIView, toRequireFailureOf gesture: UIGestureRecognizer) {
            for subview in view.subviews {
                if let scrollView = subview as? UIScrollView {
                    scrollView.panGestureRecognizer.require(toFail: gesture)
                }
                configureScrollViews(in: subview, toRequireFailureOf: gesture)
            }
        }

        @MainActor
        @objc func handlePan(_ gesture: UIScreenEdgePanGestureRecognizer) {
            guard let view = gesture.view else { return }
            let translation = gesture.translation(in: view)
            let velocity = gesture.velocity(in: view)

            switch gesture.state {
            case .changed:
                _offset.wrappedValue = max(0, translation.x)
            case .ended, .cancelled:
                let shouldDismiss = translation.x > 80 || velocity.x > 500
                if shouldDismiss {
                    let screenWidth = UIScreen.main.bounds.width
                    withAnimation(.easeOut(duration: 0.2)) {
                        _offset.wrappedValue = screenWidth
                    }
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) {
                        self.onDismiss()
                    }
                } else {
                    withAnimation(.interactiveSpring(response: 0.3, dampingFraction: 0.85)) {
                        _offset.wrappedValue = 0
                    }
                }
            default:
                break
            }
        }
    }
}
