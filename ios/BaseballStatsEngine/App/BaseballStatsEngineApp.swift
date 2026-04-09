import SwiftUI

@main
struct StatChatApp: App {
    @State private var appState = AppState()
    @State private var showLaunch = true
    @State private var showFTUE = !UserDefaults.standard.bool(forKey: "ftue_completed")

    init() {
        AnalyticsService.initialize(distinctId: AppState.deviceId)
        // Start transaction listener early so renewals/revocations are caught
        _ = StoreKitService.shared
    }

    var body: some Scene {
        WindowGroup {
            ZStack {
                HomeView()

                if showLaunch {
                    LaunchAnimationView(holdForFTUE: showFTUE) {
                        if showFTUE {
                            // Splash done but FTUE needed — keep launch overlay
                            // (splash stays on blue, FTUE content appears)
                        } else {
                            withAnimation(.easeOut(duration: 0.3)) {
                                showLaunch = false
                            }
                        }
                    }
                    .transition(.opacity)
                    .zIndex(1)
                }

                if showFTUE && showLaunch {
                    FTUEView {
                        UserDefaults.standard.set(true, forKey: "ftue_completed")
                        showFTUE = false
                        withAnimation(.easeOut(duration: 0.7)) {
                            showLaunch = false
                        }
                    }
                    .transition(.opacity)
                    .zIndex(1.5)
                }

                if appState.showUpdateBanner {
                    UpdateBannerView {
                        withAnimation(.easeOut(duration: 0.3)) {
                            appState.showUpdateBanner = false
                        }
                    }
                    .transition(.opacity)
                    .zIndex(2)
                }
            }
            .environment(appState)
            .preferredColorScheme(appState.appearanceMode.colorScheme)
            .tint(Color(red: 0.1, green: 0.25, blue: 0.7))
            .task {
                await SuggestionEngine.shared.checkForRemoteUpdate()
                await appState.checkForUpdate()
            }
            .onAppear {
                AnalyticsService.trackAppOpen()
            }
        }
    }
}
