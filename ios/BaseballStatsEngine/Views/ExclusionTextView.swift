import SwiftUI
import UIKit

/// SwiftUI wrapper around UITextView that supports `textContainer.exclusionPaths`,
/// letting text wrap around a reserved bottom-right region (e.g. for an inline
/// mic button on HomeView). SwiftUI's TextField can't do this — its padding
/// applies uniformly to all lines.
struct ExclusionTextView: UIViewRepresentable {
    @Binding var text: String
    let placeholder: String
    /// Bottom-right region (in points) that text should wrap around.
    let exclusionSize: CGSize
    var onSubmit: () -> Void

    func makeUIView(context: Context) -> UITextView {
        let tv = ExclusionUITextView()
        tv.delegate = context.coordinator
        // Match SwiftUI's `.font(.system(.body, design: .rounded))`
        let bodyDescriptor = UIFontDescriptor.preferredFontDescriptor(withTextStyle: .body)
        if let rounded = bodyDescriptor.withDesign(.rounded) {
            tv.font = UIFont(descriptor: rounded, size: 0)
        } else {
            tv.font = .preferredFont(forTextStyle: .body)
        }
        tv.backgroundColor = .clear
        tv.textColor = .label
        tv.textContainer.lineFragmentPadding = 0
        tv.textContainerInset = .zero
        tv.autocorrectionType = .no
        tv.autocapitalizationType = .none
        tv.spellCheckingType = .no
        tv.returnKeyType = .search
        tv.isScrollEnabled = true
        tv.exclusionSize = exclusionSize
        return tv
    }

    func updateUIView(_ tv: UITextView, context: Context) {
        if tv.text != text {
            tv.text = text
        }
        if let custom = tv as? ExclusionUITextView {
            custom.exclusionSize = exclusionSize
            custom.refreshExclusionPaths()
        }
    }

    func makeCoordinator() -> Coordinator { Coordinator(self) }

    final class Coordinator: NSObject, UITextViewDelegate {
        var parent: ExclusionTextView
        init(_ parent: ExclusionTextView) { self.parent = parent }

        func textViewDidChange(_ textView: UITextView) {
            if parent.text != textView.text {
                parent.text = textView.text
            }
        }

        func textView(_ textView: UITextView,
                      shouldChangeTextIn range: NSRange,
                      replacementText text: String) -> Bool {
            // Treat Return as Submit (matches `.submitLabel(.search) + .onSubmit`)
            if text == "\n" {
                parent.onSubmit()
                return false
            }
            return true
        }
    }
}

private final class ExclusionUITextView: UITextView {
    var exclusionSize: CGSize = .zero
    private var lastAppliedRect: CGRect = .null

    // Don't dictate our own height — let SwiftUI's container drive sizing
    // (otherwise UITextView with isScrollEnabled=true defaults to a tall
    // intrinsic size that blows up the parent layout).
    override var intrinsicContentSize: CGSize {
        return CGSize(width: UIView.noIntrinsicMetric, height: UIView.noIntrinsicMetric)
    }

    override func layoutSubviews() {
        super.layoutSubviews()
        refreshExclusionPaths()
    }

    func refreshExclusionPaths() {
        guard exclusionSize.width > 0, exclusionSize.height > 0, bounds.width > 0 else {
            if !textContainer.exclusionPaths.isEmpty {
                textContainer.exclusionPaths = []
            }
            return
        }
        let rect = CGRect(
            x: max(0, bounds.width - exclusionSize.width),
            y: max(0, bounds.height - exclusionSize.height),
            width: exclusionSize.width,
            height: exclusionSize.height
        )
        // Avoid layout loops — only re-apply if rect changed meaningfully
        if rect != lastAppliedRect {
            lastAppliedRect = rect
            textContainer.exclusionPaths = [UIBezierPath(rect: rect)]
        }
    }
}
