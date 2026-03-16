import SwiftUI

@main
struct StatChatApp: App {
    @State private var appState = AppState()
    @State private var showLaunch = true

    init() {
        AnalyticsService.initialize(distinctId: AppState.deviceId)
    }

    var body: some Scene {
        WindowGroup {
            ZStack {
                HomeView()

                if showLaunch {
                    LaunchAnimationView {
                        withAnimation(.easeOut(duration: 0.3)) {
                            showLaunch = false
                        }
                    }
                    .transition(.opacity)
                    .zIndex(1)
                }
            }
            .environment(appState)
            .preferredColorScheme(appState.appearanceMode.colorScheme)
            .tint(Color(red: 0.1, green: 0.25, blue: 0.7))
            .task {
                await SuggestionEngine.shared.checkForRemoteUpdate()
            }
            .onAppear {
                AnalyticsService.trackAppOpen()
            }
        }
    }
}
