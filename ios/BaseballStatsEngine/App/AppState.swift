import SwiftUI

@Observable
@MainActor
final class AppState {
    var messages: [Message] = []
    var isLoading = false
    var currentStreamingText = ""
    var showAPIKeySetup = false
    var searchHistory: [String] = []

    private let queryEngine = QueryEngine()
    private let historyKey = "searchHistory"
    private let maxHistoryItems = 50

    var hasAPIKey: Bool = KeychainHelper.load() != nil

    init() {
        searchHistory = UserDefaults.standard.stringArray(forKey: historyKey) ?? []
    }

    func refreshAPIKeyStatus() {
        hasAPIKey = KeychainHelper.load() != nil
    }

    func sendQuestion(_ question: String) {
        let trimmed = question.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }

        addToSearchHistory(trimmed)

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

    private func addToSearchHistory(_ query: String) {
        // Remove duplicate if it exists
        searchHistory.removeAll { $0.lowercased() == query.lowercased() }
        // Insert at front
        searchHistory.insert(query, at: 0)
        // Cap size
        if searchHistory.count > maxHistoryItems {
            searchHistory = Array(searchHistory.prefix(maxHistoryItems))
        }
        // Persist
        UserDefaults.standard.set(searchHistory, forKey: historyKey)
    }

    func clearSearchHistory() {
        searchHistory.removeAll()
        UserDefaults.standard.removeObject(forKey: historyKey)
    }
}
