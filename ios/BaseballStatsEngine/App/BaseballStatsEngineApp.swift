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
            .tint(Color(red: 0.1, green: 0.25, blue: 0.7))
        }
    }
}
