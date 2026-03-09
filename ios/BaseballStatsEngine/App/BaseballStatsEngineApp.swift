import SwiftUI

@main
struct StatChatApp: App {
    @State private var appState = AppState()
    @State private var showLaunch = true

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
            .tint(Color(red: 0.1, green: 0.25, blue: 0.7))
        }
    }
}
