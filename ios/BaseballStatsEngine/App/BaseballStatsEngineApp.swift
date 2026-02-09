import SwiftUI

@main
struct StatChatApp: App {
    @State private var appState = AppState()

    var body: some Scene {
        WindowGroup {
            NavigationStack {
                if appState.hasAPIKey {
                    HomeView()
                } else {
                    APIKeySetupView(isInitialSetup: true)
                }
            }
            .environment(appState)
            .preferredColorScheme(.dark)
        }
    }
}
