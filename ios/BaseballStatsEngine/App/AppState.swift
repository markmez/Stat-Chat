import SwiftUI

@Observable
@MainActor
final class AppState {
    var messages: [Message] = []
    var isLoading = false
    var currentStreamingText = ""
    var showAPIKeySetup = false

    private let queryEngine = QueryEngine()

    var hasAPIKey: Bool = KeychainHelper.load() != nil

    func refreshAPIKeyStatus() {
        hasAPIKey = KeychainHelper.load() != nil
    }

    func sendQuestion(_ question: String) {
        let trimmed = question.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }

        messages.append(Message(role: .user, content: trimmed))
        isLoading = true
        currentStreamingText = ""

        // Add placeholder assistant message for streaming
        messages.append(Message(role: .assistant, content: ""))
        let streamingIndex = messages.count - 1

        Task {
            do {
                _ = try await queryEngine.ask(trimmed) { [self] chunk in
                    currentStreamingText += chunk
                    messages[streamingIndex] = Message(role: .assistant, content: currentStreamingText)
                }
                isLoading = false
                currentStreamingText = ""
            } catch {
                isLoading = false
                currentStreamingText = ""
                // Replace the empty placeholder with the error
                messages[streamingIndex] = Message(role: .error, content: error.localizedDescription)
            }
        }
    }

    func clearConversation() {
        messages.removeAll()
        currentStreamingText = ""
        queryEngine.clearHistory()
    }
}
