import SwiftUI

enum AppearanceMode: Int, CaseIterable {
    case system = 0
    case light = 1
    case dark = 2

    var label: String {
        switch self {
        case .system: "System"
        case .light: "Light"
        case .dark: "Dark"
        }
    }

    var colorScheme: ColorScheme? {
        switch self {
        case .system: nil
        case .light: .light
        case .dark: .dark
        }
    }
}

@Observable
@MainActor
final class AppState: SearchHistoryTracking {
    var messages: [Message] = []
    var isLoading = false
    var currentStreamingText = ""
    /// Buffer for smooth streaming: network fills this, display timer drains it
    private var streamingBuffer = ""
    private var displayedLength = 0
    private var streamingTimer: Timer?
    private var streamingComplete = false
    private var streamingMessageIndex: Int?
    var searchHistory: [String] = []
    /// Stores (originalQuery, ambiguousLastName) when disambiguation is pending
    var pendingDisambiguation: (query: String, lastName: String)?
    /// Set by resolveDisambiguation when the corrected query is just a player name — ResultsView observes this to navigate to player card
    var disambiguatedPlayerName: String?
    private(set) var weeklyQueryCount: Int = 0
    var showPaywall = false
    var showUpdateBanner = false
    /// Query that was blocked by the paywall — auto-retried after successful purchase
    var pendingPaywallQuery: String?
    var appearanceMode: AppearanceMode = .system {
        didSet { UserDefaults.standard.set(appearanceMode.rawValue, forKey: appearanceModeKey) }
    }

    private let backendService = BackendService()
    private let historyKey = "searchHistory"
    private let maxHistoryItems = 50
    private let weeklyCountKey = "weeklyQueryCount"
    private let weekResetKey = "weeklyQueryResetDate"
    private let appearanceModeKey = "appearanceMode"
    private var currentQueryTask: Task<Void, Never>?
    private var conversationHistory: [(String, String)] = []
    private let maxHistory = 5

    /// The actual calendar year (e.g. 2026).
    private var currentSeasonYear: Int {
        Calendar.current.component(.year, from: Date())
    }

    /// Whether the current calendar year's data is in the local bundled DB.
    private var isCurrentSeasonLocal: Bool {
        PlayerCardService.isLocalSeason(currentSeasonYear)
    }

    static let deviceId: String = {
        let key = "statchat_device_id"
        if let existing = UserDefaults.standard.string(forKey: key) {
            return existing
        }
        let newId = UUID().uuidString
        UserDefaults.standard.set(newId, forKey: key)
        return newId
    }()

    init() {
        searchHistory = UserDefaults.standard.stringArray(forKey: historyKey) ?? []
        resetWeeklyCountIfNeeded()
        weeklyQueryCount = UserDefaults.standard.integer(forKey: weeklyCountKey)
        appearanceMode = AppearanceMode(rawValue: UserDefaults.standard.integer(forKey: appearanceModeKey)) ?? .system
        PlayerNameMatcher.load()
    }

    func sendQuestion(_ question: String, isFollowUp: Bool = false) {
        let trimmed = question.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }

        // Paywall gate — check before consuming the query
        resetWeeklyCountIfNeeded()
        if weeklyQueryCount >= 1000 && !StoreKitService.shared.isSubscribed {
            AnalyticsService.trackPaywallHit(queryCount: weeklyQueryCount)
            pendingPaywallQuery = trimmed
            showPaywall = true
            return
        }

        incrementQueryCount()

        // Non-follow-up queries get added to history immediately.
        // Follow-ups are deferred until after the backend response, so we can use
        // the Haiku-rewritten standalone query (if available) instead of the raw follow-up text.
        if !isFollowUp {
            addToSearchHistory(trimmed)
        }

        // Stat definitions — hardcoded, zero cost, no DB needed
        if let defn = PlayerNameMatcher.parseStatDefinition(trimmed) {
            let statName = defn.displayName == defn.abbrev ? defn.displayName : defn.displayName.lowercased()
            var response = "**\(defn.abbrev)** — \(defn.definition)"
            if PlayerNameMatcher.matchStat(defn.abbrev) != nil {
                response += "\n\n[SUGGEST]\(statName) leaders[/SUGGEST]\n[SUGGEST]career \(statName) leaders[/SUGGEST]"
            }
            messages.append(Message(role: .user, content: trimmed))
            messages.append(Message(role: .assistant, content: response))
            addToConversationHistory(question: trimmed, answer: response)
            AnalyticsService.trackQuery(text: trimmed, type: .localStatDefinition)
            return
        }

        // Ambiguous last name — show disambiguation with tappable player links
        if let candidates = PlayerNameMatcher.findAmbiguousPlayers(trimmed) {
            let (sorted, dominant) = PlayerNameMatcher.sortByProminence(candidates)

            // If one player is clearly dominant, resolve directly
            if let idx = dominant {
                let chosenName = sorted[idx]
                // Check if query is just a name or has additional context
                let queryWords = trimmed.lowercased().split(separator: " ").map(String.init)
                let nameWords = chosenName.lowercased().split(separator: " ").map(String.init)
                let queryIsJustName = queryWords.allSatisfy { word in
                    nameWords.contains(word) || ["jr.", "jr", "sr.", "sr"].contains(word)
                }
                if queryIsJustName {
                    disambiguatedPlayerName = chosenName
                    return
                }
                // Has additional context — replace and send
                let lower = trimmed.lowercased()
                let ambiguousLast = PlayerNameMatcher.lastNameIndex.first(where: { key, players in
                    players.count > 1 && PlayerNameMatcher.containsWord(key, in: lower)
                })?.key ?? ""
                if !ambiguousLast.isEmpty, let range = lower.range(of: ambiguousLast) {
                    var result = trimmed
                    let startIdx = trimmed.index(trimmed.startIndex, offsetBy: lower.distance(from: lower.startIndex, to: range.lowerBound))
                    let endIdx = trimmed.index(startIdx, offsetBy: ambiguousLast.count)
                    result.replaceSubrange(startIdx..<endIdx, with: chosenName)
                    sendQuestion(result)
                } else {
                    sendQuestion(chosenName)
                }
                return
            }

            // Find which last name was ambiguous
            let lower = trimmed.lowercased()
            let ambiguousLast = PlayerNameMatcher.lastNameIndex.first(where: { key, players in
                players.count > 1 && PlayerNameMatcher.containsWord(key, in: lower)
            })?.key ?? ""

            pendingDisambiguation = (query: trimmed, lastName: ambiguousLast)
            let links = sorted.map { "[\($0)](statchat://player/\($0.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? $0))" }
            let response = "Multiple players match:\n\n" + links.joined(separator: "\n\n")
            messages.append(Message(role: .user, content: trimmed))
            messages.append(Message(role: .assistant, content: response))
            AnalyticsService.trackQuery(text: trimmed, type: .localDisambiguation)
            return
        }

        // Fuzzy match — "Did you mean?" with tappable player links
        let fuzzyMatches = PlayerNameMatcher.fuzzyMatch(trimmed)
        if !fuzzyMatches.isEmpty {
            let (sorted, dominant) = PlayerNameMatcher.sortByProminence(fuzzyMatches)

            if let idx = dominant {
                let chosenName = sorted[idx]
                disambiguatedPlayerName = chosenName
                return
            }

            pendingDisambiguation = (query: trimmed, lastName: "")
            let links = sorted.map { "[\($0)](statchat://player/\($0.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? $0))" }
            let response = "Did you mean?\n\n" + links.joined(separator: "\n\n")
            messages.append(Message(role: .user, content: trimmed))
            messages.append(Message(role: .assistant, content: response))
            return
        }

        messages.append(Message(role: .user, content: trimmed))
        isLoading = true
        currentStreamingText = ""
        streamingBuffer = ""
        displayedLength = 0
        streamingComplete = false

        // Add placeholder assistant message for streaming
        messages.append(Message(role: .assistant, content: ""))
        let streamingIndex = messages.count - 1
        streamingMessageIndex = streamingIndex

        // Start smooth display timer — reveals buffered text at a steady rate
        startStreamingTimer()

        // Only send conversation history for follow-ups from ResultsView.
        // HomeView queries are standalone — stale history would confuse the backend.
        let historyForBackend = isFollowUp ? conversationHistory : [(String, String)]()

        currentQueryTask = Task {
            do {
                let result = try await backendService.ask(
                    question: trimmed,
                    deviceId: Self.deviceId,
                    history: historyForBackend
                ) { [self] chunk in
                    guard !Task.isCancelled, streamingIndex < messages.count else { return }
                    streamingBuffer += chunk
                }
                // Mark streaming as done — timer will finish revealing remaining text
                streamingComplete = true
                // Track after response so we know if it was intercepted
                AnalyticsService.trackQuery(text: trimmed, type: result.intercepted ? .backendIntercept : .backendClaude)
                // Wait for display timer to finish revealing all buffered text
                while displayedLength < streamingBuffer.count {
                    guard !Task.isCancelled else { return }
                    try await Task.sleep(for: .milliseconds(16))
                }
                stopStreamingTimer()
                guard !Task.isCancelled else { return }
                // Final flush — ensure exact full text is shown
                if streamingIndex < messages.count {
                    messages[streamingIndex] = Message(role: .assistant, content: streamingBuffer)
                    currentStreamingText = streamingBuffer
                }
                addToConversationHistory(question: trimmed, answer: result.text)
                // For follow-ups, save the rewritten standalone query to history
                // so replaying from history works without needing the original conversation context
                if isFollowUp {
                    addToSearchHistory(result.rewrittenQuery ?? trimmed)
                }
                // Append contextual SUGGEST pills based on query content
                if streamingIndex < messages.count {
                    let pills = buildFallbackPills(for: trimmed)
                    if !pills.isEmpty {
                        let existing = messages[streamingIndex].content
                        messages[streamingIndex] = Message(role: .assistant, content: existing + "\n\n" + pills)
                    }
                }
                // "Did you mean?" — if query contains a well-known player's common-word last name
                if streamingIndex < messages.count {
                    let didYouMean = buildDidYouMeanLink(for: trimmed)
                    if !didYouMean.isEmpty {
                        let existing = messages[streamingIndex].content
                        messages[streamingIndex] = Message(role: .assistant, content: existing + "\n\n" + didYouMean)
                    }
                }
                isLoading = false
                currentStreamingText = ""
            } catch {
                guard !Task.isCancelled else { return }
                stopStreamingTimer()
                isLoading = false
                currentStreamingText = ""
                guard streamingIndex < messages.count else { return }

                // Quota exceeded from backend — show paywall instead of error
                if case BackendService.ServiceError.quotaExceeded = error {
                    messages.remove(at: streamingIndex)
                    AnalyticsService.trackPaywallHit(queryCount: weeklyQueryCount)
                    pendingPaywallQuery = trimmed
                    showPaywall = true
                    return
                }

                let pills = buildFallbackPills(for: trimmed)
                let friendly = Self.friendlyErrorMessage(error)
                let errorContent = pills.isEmpty
                    ? friendly
                    : friendly + "\n\n" + pills
                messages[streamingIndex] = Message(role: .error, content: errorContent)
            }
        }
    }

    func resolveDisambiguation(with fullName: String) {
        guard let pending = pendingDisambiguation else { return }

        // Build corrected query by replacing the ambiguous part with the chosen full name
        let correctedQuery: String
        if pending.lastName.isEmpty {
            correctedQuery = fullName
        } else {
            // Check if the original query is essentially just the ambiguous name
            // (e.g. "Bobby Witt" → should become "Bobby Witt Jr.", not "Bobby Bobby Witt Jr.")
            let queryWords = pending.query.lowercased()
                .trimmingCharacters(in: .whitespacesAndNewlines)
                .split(separator: " ")
                .map(String.init)
            let nameWords = fullName.lowercased()
                .split(separator: " ")
                .map(String.init)
            let queryIsJustName = queryWords.allSatisfy { word in
                nameWords.contains(word) || ["jr.", "jr", "sr.", "sr", "junior", "senior"].contains(word)
            }

            if queryIsJustName {
                correctedQuery = fullName
            } else {
                // Query has additional context (e.g. "Witt home runs") — replace just the last name
                let lower = pending.query.lowercased()
                if let range = lower.range(of: pending.lastName) {
                    var result = pending.query
                    let startIdx = pending.query.index(pending.query.startIndex, offsetBy: lower.distance(from: lower.startIndex, to: range.lowerBound))
                    let endIdx = pending.query.index(startIdx, offsetBy: pending.lastName.count)
                    result.replaceSubrange(startIdx..<endIdx, with: fullName)
                    correctedQuery = result
                } else {
                    correctedQuery = fullName
                }
            }
        }

        // Remove the disambiguation messages (user question + "Did you mean?" response)
        if messages.count >= 2 {
            messages.removeLast(2)
        }

        pendingDisambiguation = nil

        // If the corrected query is just a player name, navigate directly to player card
        if correctedQuery.lowercased() == fullName.lowercased()
            || PlayerNameMatcher.matchPlayer(correctedQuery) != nil {
            disambiguatedPlayerName = fullName
            return
        }

        // Otherwise re-send with the full name inserted (e.g. "Bobby Witt Jr. home runs")
        sendQuestion(correctedQuery)
    }

    func clearConversation() {
        currentQueryTask?.cancel()
        currentQueryTask = nil
        stopStreamingTimer()
        messages.removeAll()
        isLoading = false
        currentStreamingText = ""
        streamingBuffer = ""
        displayedLength = 0
        conversationHistory.removeAll()
    }

    // MARK: - Smooth streaming display

    /// Starts a timer that reveals buffered text at a steady rate, eliminating visual choppiness.
    private func startStreamingTimer() {
        streamingTimer?.invalidate()
        streamingTimer = Timer.scheduledTimer(withTimeInterval: 0.016, repeats: true) { [weak self] _ in
            Task { @MainActor in
                self?.advanceStreamingDisplay()
            }
        }
    }

    private func stopStreamingTimer() {
        streamingTimer?.invalidate()
        streamingTimer = nil
    }

    private func advanceStreamingDisplay() {
        guard let idx = streamingMessageIndex, idx < messages.count else { return }
        let bufferLength = streamingBuffer.count
        guard displayedLength < bufferLength else { return }

        // Reveal characters at a steady pace: more chars per tick when buffer is large
        let pending = bufferLength - displayedLength
        // Base: 3 chars per tick (~187 chars/sec). Accelerate when buffer grows to avoid falling behind.
        let charsThisTick = pending > 80 ? max(3, pending / 4) : (pending > 20 ? 4 : 3)
        displayedLength = min(displayedLength + charsThisTick, bufferLength)

        let endIndex = streamingBuffer.index(streamingBuffer.startIndex, offsetBy: displayedLength)
        let visibleText = String(streamingBuffer[streamingBuffer.startIndex..<endIndex])
        messages[idx] = Message(role: .assistant, content: visibleText)
        currentStreamingText = visibleText
    }

    private func addToConversationHistory(question: String, answer: String) {
        conversationHistory.append((question, answer))
        if conversationHistory.count > maxHistory {
            conversationHistory = Array(conversationHistory.suffix(maxHistory))
        }
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

    /// Retry the query that was blocked by the paywall (called after successful purchase).
    func retryPendingPaywallQuery() {
        guard let query = pendingPaywallQuery else { return }
        pendingPaywallQuery = nil
        sendQuestion(query)
    }

    /// The date when free queries reset (7 days from first query of this cycle).
    var weeklyResetDate: Date {
        if let lastReset = UserDefaults.standard.object(forKey: weekResetKey) as? Date {
            return lastReset.addingTimeInterval(7 * 24 * 60 * 60)
        }
        return Date().addingTimeInterval(7 * 24 * 60 * 60)
    }

    /// How many free queries remain this week.
    var freeQueriesRemaining: Int {
        max(0, 5 - weeklyQueryCount)
    }

    func incrementQueryCount() {
        resetWeeklyCountIfNeeded()
        weeklyQueryCount += 1
        UserDefaults.standard.set(weeklyQueryCount, forKey: weeklyCountKey)
    }

    private func resetWeeklyCountIfNeeded() {
        let now = Date()
        if let lastReset = UserDefaults.standard.object(forKey: weekResetKey) as? Date {
            // Reset 7 days after the cycle started
            let resetDate = lastReset.addingTimeInterval(7 * 24 * 60 * 60)
            if now >= resetDate {
                UserDefaults.standard.set(0, forKey: weeklyCountKey)
                UserDefaults.standard.set(now, forKey: weekResetKey)
                weeklyQueryCount = 0
            }
        } else {
            // First query ever — start the cycle
            UserDefaults.standard.set(now, forKey: weekResetKey)
        }
    }

    /// Convert raw error messages to user-friendly text.
    private static func friendlyErrorMessage(_ error: Error) -> String {
        let raw = error.localizedDescription

        // SQL errors — hide technical details
        if raw.contains("SQL error") || raw.contains("no such column") || raw.contains("no such table")
            || raw.contains("syntax error") || raw.contains("near \"") {
            return "Sorry, I couldn't process that question. Try rephrasing it."
        }

        // Network errors
        if raw.contains("network") || raw.contains("offline") || raw.contains("internet")
            || raw.contains("timed out") || raw.contains("Could not connect") {
            return "Couldn't reach the server. Check your connection and try again."
        }

        // Server errors
        if raw.contains("Server error") || raw.contains("500") || raw.contains("502")
            || raw.contains("503") || raw.contains("504") {
            return "The server is having trouble right now. Please try again in a moment."
        }

        // Quota exceeded — already user-friendly from ServiceError
        if raw.contains("free queries") {
            return raw
        }

        // Generic fallback
        return "Something went wrong. Please try again."
    }

    // MARK: - Update banner

    private static let appConfigURL = URL(string: "https://stat-chat.s3.us-east-2.amazonaws.com/app_config.json")!

    func checkForUpdate() async {
        var request = URLRequest(url: Self.appConfigURL)
        request.timeoutInterval = 5
        guard let (data, response) = try? await URLSession.shared.data(for: request),
              let http = response as? HTTPURLResponse, http.statusCode == 200,
              let json = try? JSONDecoder().decode(AppConfig.self, from: data) else { return }

        let current = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "0.0.0"
        if current.compare(json.minVersion, options: .numeric) == .orderedAscending {
            showUpdateBanner = true
        }
    }

    private struct AppConfig: Decodable {
        let minVersion: String
        enum CodingKeys: String, CodingKey {
            case minVersion = "min_version"
        }
    }

    /// Build contextual SUGGEST pills for Claude fallthrough responses based on query content.
    /// Check if the query contains a well-known player's last name that's also a common word.
    /// Returns a "Did you mean [Player]?" link if so, empty string otherwise.
    private func buildDidYouMeanLink(for query: String) -> String {
        let words = query.lowercased().split(separator: " ").map(String.init)
        // Only trigger for multi-word queries (single word goes straight to the player)
        guard words.count > 1 else { return "" }

        let commonWords = PlayerNameMatcher.commonWordLastNames
        let minCareerGames = 400

        for word in words {
            let ascii = PlayerNameMatcher.stripDiacritics(word)
            guard commonWords.contains(ascii),
                  let players = PlayerNameMatcher.lastNameIndex[ascii],
                  players.count == 1,
                  let player = players.first else { continue }

            // Only suggest well-known players
            let db = DatabaseService()
            if let result = try? db.execute(sql: """
                SELECT COALESCE(career_games, 0) FROM players
                WHERE name = '\(player.replacingOccurrences(of: "'", with: "''"))'
                """), let row = result.rows.first,
               let games = Int(row[0]), games >= minCareerGames {
                let encoded = player.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? player
                return "Looking for [\(player)](statchat://player/\(encoded))?"
            }
        }
        return ""
    }

    private func buildFallbackPills(for query: String) -> String {
        let lower = query.lowercased()
        var pills: [String] = []

        // Detect player name in query
        let words = lower.split(separator: " ").map(String.init)
        var detectedPlayer: String?
        for i in 0..<words.count {
            // Try two-word names first (more specific)
            if i + 1 < words.count {
                let pair = "\(words[i]) \(words[i + 1])"
                if let match = PlayerNameMatcher.matchPlayer(pair) {
                    detectedPlayer = match
                    break
                }
            }
            // Then single word (last name) — skip common English words
            if !PlayerNameMatcher.commonWordLastNames.contains(words[i]),
               let match = PlayerNameMatcher.matchPlayer(words[i]) {
                detectedPlayer = match
                break
            }
        }

        // Detect stat keyword
        let detectedStat = PlayerNameMatcher.matchStat(lower)

        if let player = detectedPlayer {
            let season = PlayerNameMatcher.detectSeason(lower, defaultToMostRecent: true) ?? currentSeasonYear
            pills.append("[SUGGEST]\(player) \(season)[/SUGGEST]")
            if detectedStat != nil {
                pills.append("[SUGGEST]\(player) splits[/SUGGEST]")
            }
        }

        if let stat = detectedStat {
            let statName = stat.pillName
            pills.append("[SUGGEST]\(statName) leaders[/SUGGEST]")
        }

        return pills.joined(separator: "\n")
    }

    /// Check if a player name resolves to a pitcher
    private func isPitcherQuery(_ name: String) -> Bool {
        PlayerCardService.isPitcher(name: name)
    }

}

