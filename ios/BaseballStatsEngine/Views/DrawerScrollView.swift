import SwiftUI
import UIKit

/// A scroll view that reports when it's at the top, and calls `onPullDown` when
/// the user drags down while already at the top (rubber-band zone). This gives
/// us the Google Maps drawer behavior: scroll normally when content is scrolled
/// down, collapse the drawer when scrolled to top and pulling down.
struct DrawerScrollView<Content: View>: UIViewRepresentable {
    let content: Content
    let isEnabled: Bool
    @Binding var isAtTop: Bool
    var onPullDown: () -> Void

    init(isEnabled: Bool, isAtTop: Binding<Bool>, onPullDown: @escaping () -> Void,
         @ViewBuilder content: () -> Content) {
        self.content = content()
        self.isEnabled = isEnabled
        self._isAtTop = isAtTop
        self.onPullDown = onPullDown
    }

    func makeCoordinator() -> Coordinator {
        Coordinator(parent: self)
    }

    func makeUIView(context: Context) -> UIScrollView {
        let scrollView = UIScrollView()
        scrollView.delegate = context.coordinator
        scrollView.alwaysBounceVertical = true
        scrollView.showsVerticalScrollIndicator = true
        scrollView.backgroundColor = .clear

        let host = UIHostingController(rootView: content)
        host.view.backgroundColor = .clear
        host.view.translatesAutoresizingMaskIntoConstraints = false

        // Size the host to fit its content
        host.sizingOptions = .intrinsicContentSize

        scrollView.addSubview(host.view)

        NSLayoutConstraint.activate([
            host.view.topAnchor.constraint(equalTo: scrollView.contentLayoutGuide.topAnchor),
            host.view.leadingAnchor.constraint(equalTo: scrollView.frameLayoutGuide.leadingAnchor),
            host.view.trailingAnchor.constraint(equalTo: scrollView.frameLayoutGuide.trailingAnchor),
            host.view.bottomAnchor.constraint(equalTo: scrollView.contentLayoutGuide.bottomAnchor),
        ])

        context.coordinator.hostController = host
        return scrollView
    }

    func updateUIView(_ scrollView: UIScrollView, context: Context) {
        scrollView.isScrollEnabled = isEnabled
        context.coordinator.parent = self
        // Only update content if not mid-scroll (layout during deceleration kills momentum)
        if !scrollView.isDecelerating && !scrollView.isDragging {
            context.coordinator.hostController?.rootView = content
            context.coordinator.hostController?.view.invalidateIntrinsicContentSize()
        }
    }

    class Coordinator: NSObject, UIScrollViewDelegate {
        var parent: DrawerScrollView
        var hostController: UIHostingController<Content>?
        private var didTriggerCollapse = false
        private var wasAtTopWhenDragStarted = false
        private var isDragging = false

        init(parent: DrawerScrollView) {
            self.parent = parent
        }

        func scrollViewDidScroll(_ scrollView: UIScrollView) {
            let atTop = scrollView.contentOffset.y <= 0
            if atTop != parent.isAtTop {
                DispatchQueue.main.async {
                    self.parent.isAtTop = atTop
                }
            }

            // Only collapse on pull-down if the user STARTED their drag at the top
            // This prevents momentum from an aggressive scroll-up triggering collapse
            if isDragging && wasAtTopWhenDragStarted
                && scrollView.contentOffset.y < -60 && !didTriggerCollapse {
                didTriggerCollapse = true
                scrollView.contentOffset = .zero
                DispatchQueue.main.async {
                    self.parent.onPullDown()
                }
            }
        }

        func scrollViewWillBeginDragging(_ scrollView: UIScrollView) {
            isDragging = true
            didTriggerCollapse = false
            wasAtTopWhenDragStarted = scrollView.contentOffset.y <= 0
        }

        func scrollViewDidEndDragging(_ scrollView: UIScrollView, willDecelerate decelerate: Bool) {
            isDragging = false
        }
    }
}
