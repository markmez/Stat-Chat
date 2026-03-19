import SwiftUI

struct UpdateBannerView: View {
    var onDismiss: () -> Void

    private let deepBlue = Color(red: 0.1, green: 0.25, blue: 0.7)
    private let lightBlue = Color(red: 0.45, green: 0.7, blue: 1.0)
    private let iconDark = Color(red: 0.06, green: 0.12, blue: 0.45)
    private let iconLight = Color(red: 0.12, green: 0.28, blue: 0.75)

    // TODO: Replace with actual App Store URL once app ID is available
    private let appStoreURL = URL(string: "https://apps.apple.com/app/statchat")!

    var body: some View {
        ZStack {
            // Background — matches splash screen gradient
            LinearGradient(
                colors: [iconDark, iconLight],
                startPoint: .topLeading, endPoint: .bottomTrailing
            )
            .ignoresSafeArea()

            VStack(spacing: 32) {
                Spacer()

                // Logo icon
                ZStack {
                    Image(systemName: "sparkle")
                        .font(.system(size: 48, weight: .bold))
                        .foregroundStyle(.white)

                    Image(systemName: "baseball.fill")
                        .font(.system(size: 22))
                        .foregroundStyle(.white)
                        .offset(x: 20, y: -20)

                    Image(systemName: "baseball.fill")
                        .font(.system(size: 16))
                        .foregroundStyle(.white.opacity(0.7))
                        .offset(x: -17, y: -17)

                    Image(systemName: "baseball.fill")
                        .font(.system(size: 17))
                        .foregroundStyle(.white.opacity(0.85))
                        .offset(x: 17, y: 17)
                }
                .padding(.bottom, 8)

                // Message
                VStack(spacing: 12) {
                    Text("We've got a big update for you!")
                        .font(.system(size: 24, weight: .bold, design: .rounded))
                        .foregroundStyle(.white)
                        .multilineTextAlignment(.center)

                    Text("Update the app in the App Store now.")
                        .font(.system(.body, design: .rounded))
                        .foregroundStyle(.white.opacity(0.8))
                        .multilineTextAlignment(.center)
                }
                .padding(.horizontal, 32)

                Spacer()

                // Buttons
                VStack(spacing: 14) {
                    Button {
                        UIApplication.shared.open(appStoreURL)
                    } label: {
                        Text("Update")
                            .font(.system(.body, design: .rounded, weight: .semibold))
                            .foregroundStyle(deepBlue)
                            .frame(maxWidth: .infinity)
                            .padding(.vertical, 16)
                            .background(.white, in: RoundedRectangle(cornerRadius: 14))
                    }

                    Button {
                        onDismiss()
                    } label: {
                        Text("Not now")
                            .font(.system(.subheadline, design: .rounded, weight: .medium))
                            .foregroundStyle(.white.opacity(0.7))
                    }
                    .padding(.bottom, 8)
                }
                .padding(.horizontal, 32)
                .padding(.bottom, 24)
            }
        }
    }
}
