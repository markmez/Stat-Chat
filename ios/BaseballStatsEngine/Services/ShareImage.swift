import SwiftUI
import UIKit
import UniformTypeIdentifiers

/// Rasterizes a `ShareCardView` to PNG image data via `ImageRenderer` and
/// wraps the result in `ShareableImage`, a `Transferable`/`Identifiable`
/// payload suitable for `UIActivityViewController` (image + URL) or
/// `ShareLink` (via PNG data representation).
@MainActor
enum ShareImage {
    /// App Store URL — mirrored in `UpdateBannerView.appStoreURL`. Keep both
    /// in sync. 404s until the app passes review and goes live, then resolves
    /// automatically with no code change.
    static let appStoreURL = URL(string: "https://apps.apple.com/app/id6763139565")!

    /// The text component included alongside the image in the share payload.
    /// Twitter/X uses this as the tweet draft body; iMessage shows the URL
    /// as a tappable link preview underneath the image.
    static let shareMessage = "StatChat Baseball Stats. Ask Like AI, Fast Real Answers. \(appStoreURL.absoluteString)"

    static func render(_ content: ShareCardView.Content) -> ShareableImage? {
        let view = ShareCardView(content: content)
        let renderer = ImageRenderer(content: view)
        renderer.scale = 3.0
        renderer.proposedSize = .init(width: ShareCardView.renderWidth, height: nil)
        guard let uiImage = renderer.uiImage,
              let pngData = uiImage.pngData() else {
            return nil
        }
        return ShareableImage(uiImage: uiImage, pngData: pngData)
    }
}

struct ShareableImage: Transferable, Identifiable {
    let id = UUID()
    let uiImage: UIImage
    let pngData: Data

    var image: Image { Image(uiImage: uiImage) }

    static var transferRepresentation: some TransferRepresentation {
        DataRepresentation(contentType: .png) { item in
            item.pngData
        } importing: { data in
            ShareableImage(
                uiImage: UIImage(data: data) ?? UIImage(),
                pngData: data
            )
        }
        ProxyRepresentation(exporting: \.image)
    }
}

/// `UIActivityViewController` wrapper. Used over `ShareLink` because we hand
/// off `[UIImage, String]` together — the system distributes them per share
/// target (Twitter gets tweet draft + image, iMessage gets image + link
/// preview), which `ShareLink`'s single-item API doesn't do reliably.
struct ActivityShareView: UIViewControllerRepresentable {
    let activityItems: [Any]

    func makeUIViewController(context: Context) -> UIActivityViewController {
        UIActivityViewController(activityItems: activityItems, applicationActivities: nil)
    }

    func updateUIViewController(_ uiViewController: UIActivityViewController, context: Context) {}
}
