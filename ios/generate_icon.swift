#!/usr/bin/env swift

import AppKit
import SwiftUI

// Logo matching the app's HomeView logo: sparkle center + 3 baseballs
struct LogoView: View {
    let size: CGFloat

    private let deepBlue = Color(red: 0.1, green: 0.25, blue: 0.7)
    private let lightBlue = Color(red: 0.45, green: 0.7, blue: 1.0)

    var body: some View {
        ZStack {
            // Background gradient
            RoundedRectangle(cornerRadius: size * 0.22)
                .fill(
                    LinearGradient(
                        colors: [
                            Color(red: 0.06, green: 0.12, blue: 0.45),
                            Color(red: 0.12, green: 0.28, blue: 0.75)
                        ],
                        startPoint: .topLeading, endPoint: .bottomTrailing
                    )
                )

            // Sparkle center
            Image(systemName: "sparkle")
                .font(.system(size: size * 0.437, weight: .bold))
                .foregroundStyle(
                    LinearGradient(
                        colors: [.white, lightBlue],
                        startPoint: .topLeading, endPoint: .bottomTrailing
                    )
                )

            // Baseball top-right
            Image(systemName: "baseball.fill")
                .font(.system(size: size * 0.184))
                .foregroundStyle(.white.opacity(0.9))
                .offset(x: size * 0.196, y: -size * 0.196)

            // Baseball top-left (smaller)
            Image(systemName: "baseball.fill")
                .font(.system(size: size * 0.127))
                .foregroundStyle(.white.opacity(0.65))
                .offset(x: -size * 0.173, y: -size * 0.173)

            // Baseball bottom-right
            Image(systemName: "baseball.fill")
                .font(.system(size: size * 0.138))
                .foregroundStyle(.white.opacity(0.8))
                .offset(x: size * 0.173, y: size * 0.173)
        }
        .frame(width: size, height: size)
    }
}

// Render to PNG on MainActor
@MainActor
func generateIcon() {
    let size: CGFloat = 1024
    let renderer = ImageRenderer(content: LogoView(size: size))
    renderer.scale = 1.0

    guard let nsImage = renderer.nsImage else {
        print("Failed to render image")
        exit(1)
    }

    guard let tiffData = nsImage.tiffRepresentation,
          let bitmap = NSBitmapImageRep(data: tiffData),
          let pngData = bitmap.representation(using: .png, properties: [:]) else {
        print("Failed to convert to PNG")
        exit(1)
    }

    let outputPath = CommandLine.arguments.count > 1
        ? CommandLine.arguments[1]
        : "AppIcon.png"

    do {
        try pngData.write(to: URL(fileURLWithPath: outputPath))
        print("App icon saved to \(outputPath)")
    } catch {
        print("Failed to write file: \(error)")
        exit(1)
    }
}

MainActor.assumeIsolated {
    generateIcon()
}
