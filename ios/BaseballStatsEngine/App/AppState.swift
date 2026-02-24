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
    private var currentQueryTask: Task<Void, Never>?

    var hasAPIKey: Bool = KeychainHelper.load() != nil

    init() {
        searchHistory = UserDefaults.standard.stringArray(forKey: historyKey) ?? []
        PlayerNameMatcher.load()
    }

    func refreshAPIKeyStatus() {
        hasAPIKey = KeychainHelper.load() != nil
    }

    func sendQuestion(_ question: String) {
        let trimmed = question.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }

        addToSearchHistory(trimmed)

        // Intercept comparison queries — build response from DB, skip Claude
        if let (p1, p2) = PlayerNameMatcher.parseComparison(trimmed) {
            let response = PlayerCardService.buildComparison(player1: p1, player2: p2)
            messages.append(Message(role: .user, content: trimmed))
            messages.append(Message(role: .assistant, content: response))
            // Inject into query engine history so Claude has context for follow-ups
            queryEngine.injectHistory(question: trimmed, answer: "Compared \(p1) and \(p2). \(response)")
            return
        }

        messages.append(Message(role: .user, content: trimmed))
        isLoading = true
        currentStreamingText = ""

        // Add placeholder assistant message for streaming
        messages.append(Message(role: .assistant, content: ""))
        let streamingIndex = messages.count - 1

        currentQueryTask = Task {
            do {
                _ = try await queryEngine.ask(trimmed) { [self] chunk in
                    guard !Task.isCancelled, streamingIndex < messages.count else { return }
                    currentStreamingText += chunk
                    messages[streamingIndex] = Message(role: .assistant, content: currentStreamingText)
                }
                guard !Task.isCancelled else { return }
                isLoading = false
                currentStreamingText = ""
            } catch {
                guard !Task.isCancelled else { return }
                isLoading = false
                currentStreamingText = ""
                guard streamingIndex < messages.count else { return }
                messages[streamingIndex] = Message(role: .error, content: error.localizedDescription)
            }
        }
    }

    func clearConversation() {
        currentQueryTask?.cancel()
        currentQueryTask = nil
        messages.removeAll()
        isLoading = false
        currentStreamingText = ""
        queryEngine.clearHistory()
    }

    func addToSearchHistory(_ query: String) {
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
