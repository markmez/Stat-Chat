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

    /// Context from the last locally-resolved result, enabling follow-up queries.
    private var lastResultContext: ResultContext?

    /// Structured metadata about the last result for follow-up resolution.
    struct ResultContext {
        enum ResultType { case leaderboard, comparison, statLookup, seasonLookup, platoonSplits, homeAwaySplits, rispSplits, pitchTypeSplits, countSplits }
        let type: ResultType
        let playerNames: [String]
        let stat: PlayerNameMatcher.StatInfo?
        let season: Int?
        var league: String? = nil
        let originalQuery: String
    }

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

    func sendQuestion(_ question: String, followUpContext: String? = nil) {
        let trimmed = question.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }

        // Paywall gate — check before consuming the query
        resetWeeklyCountIfNeeded()
        if weeklyQueryCount >= 5 && !StoreKitService.shared.isSubscribed && !StoreKitService.shared.products.isEmpty {
            AnalyticsService.trackPaywallHit(queryCount: weeklyQueryCount)
            pendingPaywallQuery = trimmed
            showPaywall = true
            return
        }

        incrementQueryCount()

        // For follow-ups, store with context prefix if the question looks contextual
        if let context = followUpContext, looksContextual(trimmed) {
            addToSearchHistory("\(context) → \(trimmed)")
        } else {
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

        // For contextual follow-ups that couldn't be resolved locally,
        // build a self-contained prompt so Claude has full context.
        let isContextual = looksContextual(trimmed) && !conversationHistory.isEmpty
        let questionForBackend = isContextual ? buildContextualPrompt(for: trimmed) : trimmed
        // When context is baked into the question, don't also send history (avoids duplication)
        let historyForBackend = isContextual ? [(String, String)]() : conversationHistory

        currentQueryTask = Task {
            do {
                let result = try await backendService.ask(
                    question: questionForBackend,
                    deviceId: Self.deviceId,
                    history: historyForBackend,
                    contextual: isContextual
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
                // Pre-compute player/team links so ResultCard doesn't re-scan 24K+ names on every render
                if streamingIndex < messages.count {
                    let raw = messages[streamingIndex].content
                    messages[streamingIndex].processedContent = PlayerNameMatcher.addTeamLinks(to: PlayerNameMatcher.addLinks(to: raw))
                }
                isLoading = false
                currentStreamingText = ""
            } catch {
                guard !Task.isCancelled else { return }
                stopStreamingTimer()
                isLoading = false
                currentStreamingText = ""
                guard streamingIndex < messages.count else { return }
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

    /// The next Monday when free queries reset.
    var weeklyResetDate: Date {
        let calendar = Calendar.current
        let now = Date()
        return calendar.nextDate(after: now, matching: DateComponents(weekday: 2), matchingPolicy: .nextTime) ?? now
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
        let calendar = Calendar.current
        let now = Date()
        if let lastReset = UserDefaults.standard.object(forKey: weekResetKey) as? Date {
            // Reset on Monday (weekday 2)
            let lastMonday = calendar.nextDate(after: lastReset, matching: DateComponents(weekday: 2), matchingPolicy: .nextTime, direction: .backward) ?? lastReset
            let thisMonday = calendar.nextDate(after: now, matching: DateComponents(weekday: 2), matchingPolicy: .nextTime, direction: .backward) ?? now
            if thisMonday > lastMonday {
                UserDefaults.standard.set(0, forKey: weeklyCountKey)
                UserDefaults.standard.set(now, forKey: weekResetKey)
                weeklyQueryCount = 0
            }
        } else {
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

    // MARK: - Contextual Follow-Up Resolution

    /// Build a self-contained prompt for Claude when a contextual follow-up can't be resolved locally.
    /// Includes the original question, the visible results, and the follow-up question.
    private func buildContextualPrompt(for followUp: String) -> String {
        // Find the most recent Q&A pair from visible messages
        var lastQuestion: String?
        var lastAnswer: String?
        for message in messages.reversed() {
            if lastAnswer == nil && message.role == .assistant && !message.content.isEmpty {
                // Strip markup tags from the answer to get what the user actually saw
                lastAnswer = message.content
                    .replacingOccurrences(of: "\\[STATGRID\\]|\\[/STATGRID\\]|\\[LEADERBOARD\\]|\\[/LEADERBOARD\\]|\\[TIP\\].*?\\[/TIP\\]|\\[SUGGEST\\].*?\\[/SUGGEST\\]|\\[SEEALSO\\].*?\\[/SEEALSO\\]|\\[DIDYOUMEAN\\].*?\\[/DIDYOUMEAN\\]", with: "", options: .regularExpression)
                    .trimmingCharacters(in: .whitespacesAndNewlines)
            } else if lastAnswer != nil && lastQuestion == nil && message.role == .user {
                lastQuestion = message.content
                break
            }
        }

        guard let question = lastQuestion, let answer = lastAnswer else {
            return followUp
        }

        // Truncate the answer if it's very long — keep the structure but don't blow up the prompt
        let truncatedAnswer = answer.count > 2000 ? String(answer.prefix(2000)) + "\n..." : answer

        return """
            The user asked: "\(question)"

            The results shown were:
            \(truncatedAnswer)

            The user now asks: "\(followUp)"

            Answer the follow-up question based on the context above. If it's a question about methodology \
            (like "how was this calculated?"), explain using your knowledge of baseball statistics. \
            If it's asking to modify the previous query (like "across all of MLB" or "just the NL"), \
            re-run the query with the requested change.
            """
    }

    /// Phrases that unambiguously reference a previous result set.
    private static let referentialPhrases = [
        "of these", "of those", "of them",
        "among these", "among those", "among them",
        "from that list", "from the list", "from those",
        "in that group", "which one", "which ones",
        "between them", "out of these", "out of those",
        "of the above", "listed above", "players above",
        "same players", "same guys", "those players", "those guys",
    ]

    /// Attempts to resolve a follow-up query using context from the previous result.
    /// Handles: referential phrases + stat, bare "sort by X", bare year re-run, bare stat after leaderboard.
    private func resolveContextualFollowUp(_ question: String) -> String? {
        let lower = question.lowercased()

        // Pattern 1: Referential phrases — "who of these had the highest BABIP?"
        // These explicitly reference a previous result, so always check them.
        let isReferential = Self.referentialPhrases.contains(where: { lower.contains($0) })
        if isReferential {
            if let result = resolveReferentialFollowUp(question) { return result }
        }

        // Remaining patterns only apply to contextual fragments, not standalone queries.
        // "career doubles leaders" or "Top 5 in walks last season" are self-contained.
        guard looksContextual(question) else { return nil }

        // Remaining patterns require stored context from a previous result
        guard let ctx = lastResultContext else { return nil }

        // Pattern 2: "sort by ERA" / "rank by BABIP" / "order by OPS" — re-sort same players by different stat
        let sortPrefixes = ["sort by ", "rank by ", "order by ", "now sort by ", "now rank by "]
        if let prefix = sortPrefixes.first(where: { lower.hasPrefix($0) }) {
            let statPart = String(lower.dropFirst(prefix.count))
            if let stat = PlayerNameMatcher.matchStat(statPart), ctx.playerNames.count >= 2 {
                let isPitching = PlayerNameMatcher.isPitchingStat(stat)
                if let result = PlayerCardService.buildPlayerSubsetLeaderboard(
                    playerNames: ctx.playerNames, stat: stat, season: ctx.season, isPitching: isPitching
                ) {
                    lastResultContext = ResultContext(type: ctx.type, playerNames: ctx.playerNames, stat: stat, season: ctx.season, originalQuery: question)
                    return result
                }
            }
        }

        // Pattern 3: "and in 2024?" / "in 2023?" / "2024?" — re-run with different season
        // Must be a short query that's essentially just a year
        if let year = PlayerNameMatcher.detectSeason(question), lower.count < 20 {
            // No player name in query → it's a contextual year change
            if PlayerNameMatcher.matchPlayer(question) == nil {
                if ctx.type == .leaderboard, let stat = ctx.stat {
                    let isPitching = PlayerNameMatcher.isPitchingStat(stat)
                    let interpretation = "\(year) \(stat.displayName) leaders"
                    if ctx.playerNames.count >= 2 {
                        if let result = PlayerCardService.buildPlayerSubsetLeaderboard(
                            playerNames: ctx.playerNames, stat: stat, season: year, isPitching: isPitching
                        ) {
                            lastResultContext = ResultContext(type: .leaderboard, playerNames: ctx.playerNames, stat: stat, season: year, originalQuery: question)
                            return "[DIDYOUMEAN]\(interpretation)[/DIDYOUMEAN]\n" + result
                        }
                    } else {
                        let response: String
                        if isPitching {
                            response = PlayerCardService.buildPitchingLeaderboard(stat: stat, scope: .season(year), limit: 10, league: nil)
                        } else {
                            response = PlayerCardService.buildLeaderboard(stat: stat, scope: .season(year), limit: 10, league: nil)
                        }
                        lastResultContext = ResultContext(
                            type: .leaderboard,
                            playerNames: extractPlayerNamesFromResponse(response),
                            stat: stat, season: year, originalQuery: question
                        )
                        return "[DIDYOUMEAN]\(interpretation)[/DIDYOUMEAN]\n" + response
                    }
                } else if ctx.type == .statLookup, let stat = ctx.stat, let player = ctx.playerNames.first {
                    let isPitching = PlayerCardService.isPitcher(name: player) || PlayerNameMatcher.isPitchingStat(stat)
                    let interpretation = "\(player) \(stat.displayName) in \(year)"
                    let response: String?
                    if isPitching {
                        response = PlayerCardService.buildPitchingSingleStatLookup(name: player, stat: stat, season: year)
                    } else {
                        response = PlayerCardService.buildSingleStatLookup(name: player, stat: stat, season: year)
                    }
                    if let response {
                        lastResultContext = ResultContext(type: .statLookup, playerNames: [player], stat: stat, season: year, originalQuery: question)
                        return "[DIDYOUMEAN]\(interpretation)[/DIDYOUMEAN]\n" + response
                    }
                } else if ctx.type == .seasonLookup, let player = ctx.playerNames.first {
                    let isPitching = PlayerCardService.isPitcher(name: player)
                    let interpretation = "\(player) in \(year)"
                    let response: String?
                    if isPitching {
                        response = PlayerCardService.buildPitchingSeasonSummary(name: player, season: year)
                    } else {
                        response = PlayerCardService.buildSeasonSummary(name: player, season: year)
                    }
                    if let response {
                        lastResultContext = ResultContext(type: .seasonLookup, playerNames: [player], stat: nil, season: year, originalQuery: question)
                        return "[DIDYOUMEAN]\(interpretation)[/DIDYOUMEAN]\n" + response
                    }
                } else if ctx.type == .comparison, ctx.playerNames.count == 2 {
                    let p1 = ctx.playerNames[0], p2 = ctx.playerNames[1]
                    let isPitching = PlayerCardService.isPitcher(name: p1) && PlayerCardService.isPitcher(name: p2)
                    let interpretation = "\(p1) vs \(p2) in \(year)"
                    let response: String
                    if isPitching {
                        response = PlayerCardService.buildPitchingComparison(player1: p1, player2: p2, season: year)
                    } else {
                        response = PlayerCardService.buildComparison(player1: p1, player2: p2, season: year)
                    }
                    lastResultContext = ResultContext(type: .comparison, playerNames: ctx.playerNames, stat: nil, season: year, originalQuery: question)
                    return "[DIDYOUMEAN]\(interpretation)[/DIDYOUMEAN]\n" + response
                }
            }
        }

        // Pattern 4: League switching — "across all of MLB", "in the NL", "for the AL"
        if ctx.type == .leaderboard, let stat = ctx.stat {
            let leaguePhrases: [(phrase: String, league: String?)] = [
                ("across all of mlb", nil), ("all of mlb", nil), ("across mlb", nil),
                ("for mlb", nil), ("in mlb", nil), ("mlb-wide", nil), ("both leagues", nil),
                ("in the al", "AL"), ("for the al", "AL"), ("al only", "AL"),
                ("in the nl", "NL"), ("for the nl", "NL"), ("nl only", "NL"),
                ("american league", "AL"), ("national league", "NL"),
            ]
            if let match = leaguePhrases.first(where: { lower.contains($0.phrase) }) {
                let newLeague = match.league
                let isPitching = PlayerNameMatcher.isPitchingStat(stat)
                let scope: PlayerNameMatcher.LeaderboardScope
                if let s = ctx.season { scope = .season(s) } else { scope = .career }
                let response: String
                if isPitching {
                    response = PlayerCardService.buildPitchingLeaderboard(stat: stat, scope: scope, limit: 10, league: newLeague)
                } else {
                    response = PlayerCardService.buildLeaderboard(stat: stat, scope: scope, limit: 10, league: newLeague)
                }
                let leagueLabel = newLeague ?? "MLB"
                let seasonLabel = ctx.season.map { "\($0) " } ?? "Career "
                let interpretation = "\(seasonLabel)\(stat.displayName) leaders (\(leagueLabel))"
                lastResultContext = ResultContext(
                    type: .leaderboard,
                    playerNames: extractPlayerNamesFromResponse(response),
                    stat: stat, season: ctx.season, league: newLeague, originalQuery: question
                )
                return "[DIDYOUMEAN]\(interpretation)[/DIDYOUMEAN]\n" + response
            }
        }

        // Pattern 5: "what about BABIP?" / bare stat name — same players, different stat
        // Only when there's no player name and the query is short enough to be contextual
        if lower.count < 30, ctx.playerNames.count >= 2 {
            // Strip "what about" / "how about" / "and" prefix
            let stripped = lower
                .replacingOccurrences(of: "^(what about |how about |and |now |their )", with: "", options: .regularExpression)
                .replacingOccurrences(of: "\\?$", with: "", options: .regularExpression)
                .trimmingCharacters(in: .whitespaces)

            if let stat = PlayerNameMatcher.matchStat(stripped),
               PlayerNameMatcher.matchPlayer(question) == nil {
                let isPitching = PlayerNameMatcher.isPitchingStat(stat)
                let result = PlayerCardService.buildPlayerSubsetLeaderboard(
                    playerNames: ctx.playerNames, stat: stat, season: ctx.season, isPitching: isPitching
                )
                if let result {
                    let seasonLabel = ctx.season.map { "\($0) " } ?? ""
                    let interpretation = "\(seasonLabel)\(stat.displayName) for same players"
                    lastResultContext = ResultContext(type: ctx.type, playerNames: ctx.playerNames, stat: stat, season: ctx.season, originalQuery: question)
                    return "[DIDYOUMEAN]\(interpretation)[/DIDYOUMEAN]\n" + result
                }
            }
        }

        // Pattern 5: "what about [player]?" — substitute player in same query type
        if lower.count < 40, let ctx = lastResultContext {
            let stripped = lower
                .replacingOccurrences(of: "^(what about |how about |and |now |show me |show )", with: "", options: .regularExpression)
                .replacingOccurrences(of: "\\?$", with: "", options: .regularExpression)
                .trimmingCharacters(in: .whitespaces)

            if let newPlayer = PlayerNameMatcher.findPlayerInText(stripped) ?? PlayerNameMatcher.matchPlayer(stripped) {
                let season = ctx.season ?? PlayerNameMatcher.currentCalendarYear
                switch ctx.type {
                case .statLookup:
                    if let stat = ctx.stat {
                        let isPitching = PlayerCardService.isPitcher(name: newPlayer) || PlayerNameMatcher.isPitchingStat(stat)
                        let response: String?
                        if isPitching {
                            response = PlayerCardService.buildPitchingSingleStatLookup(name: newPlayer, stat: stat, season: season)
                        } else {
                            response = PlayerCardService.buildSingleStatLookup(name: newPlayer, stat: stat, season: season)
                        }
                        if let response {
                            lastResultContext = ResultContext(type: .statLookup, playerNames: [newPlayer], stat: stat, season: season, originalQuery: question)
                            return response
                        }
                    }
                case .leaderboard:
                    if let stat = ctx.stat {
                        let isPitching = PlayerCardService.isPitcher(name: newPlayer) || PlayerNameMatcher.isPitchingStat(stat)
                        let response: String?
                        if isPitching {
                            response = PlayerCardService.buildPitchingSingleStatLookup(name: newPlayer, stat: stat, season: season)
                        } else {
                            response = PlayerCardService.buildSingleStatLookup(name: newPlayer, stat: stat, season: season)
                        }
                        if let response {
                            lastResultContext = ResultContext(type: .statLookup, playerNames: [newPlayer], stat: stat, season: season, originalQuery: question)
                            return response
                        }
                    }
                case .seasonLookup:
                    let isPitching = PlayerCardService.isPitcher(name: newPlayer)
                    let response: String?
                    if isPitching {
                        response = PlayerCardService.buildPitchingSeasonSummary(name: newPlayer, season: season)
                    } else {
                        response = PlayerCardService.buildSeasonSummary(name: newPlayer, season: season)
                    }
                    if let response {
                        lastResultContext = ResultContext(type: .seasonLookup, playerNames: [newPlayer], stat: nil, season: season, originalQuery: question)
                        return response
                    }
                default:
                    break
                }
            }
        }

        return nil
    }

    /// Resolves follow-ups with explicit referential phrases ("who of these had the highest BABIP?").
    private func resolveReferentialFollowUp(_ question: String) -> String? {
        // Find the last assistant message with content
        guard let lastAssistant = messages.last(where: { $0.role == .assistant && !$0.content.isEmpty }) else {
            return nil
        }

        // Extract player names from the previous result
        let playerNames = extractPlayerNamesFromResponse(lastAssistant.content)
        guard playerNames.count >= 2 else { return nil }

        // Detect what stat the user is asking about
        guard let stat = PlayerNameMatcher.matchStat(question) else { return nil }

        // Detect season context from previous result or question
        let season = detectSeasonFromContext(previousContent: lastAssistant.content, followUp: question)

        // Query that stat for just those players
        let isPitching = PlayerNameMatcher.isPitchingStat(stat)
        let result = PlayerCardService.buildPlayerSubsetLeaderboard(
            playerNames: playerNames, stat: stat, season: season, isPitching: isPitching
        )
        if let result {
            // Save context so further follow-ups work
            lastResultContext = ResultContext(type: .leaderboard, playerNames: playerNames, stat: stat, season: season, originalQuery: question)
        }
        return result
    }

    /// Extracts player names from a response string containing ROW lines (leaderboard or statgrid format).
    private func extractPlayerNamesFromResponse(_ content: String) -> [String] {
        var names: [String] = []
        var seen = Set<String>()

        for line in content.components(separatedBy: "\n") {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            guard trimmed.hasPrefix("ROW ") || trimmed.hasPrefix("ROW:") else { continue }

            let nameCandidate: String

            if trimmed.hasPrefix("ROW:") {
                // Statgrid/comparison format: "ROW: Aaron Judge, .893, 54, ..."
                // or with year: "ROW: Aaron Judge (2024), .893, 54, ..."
                let afterColon = trimmed.dropFirst(4).trimmingCharacters(in: .whitespaces)
                // Name is everything before the first comma
                if let commaIdx = afterColon.firstIndex(of: ",") {
                    nameCandidate = String(afterColon[afterColon.startIndex..<commaIdx]).trimmingCharacters(in: .whitespaces)
                } else {
                    nameCandidate = afterColon
                }
            } else {
                // Leaderboard format: "ROW 1. Aaron Judge: .893"
                let afterRow = trimmed.dropFirst(4)
                guard let dotIdx = afterRow.firstIndex(of: ".") else { continue }
                let afterDot = afterRow[afterRow.index(after: dotIdx)...].trimmingCharacters(in: .whitespaces)
                if let colonIdx = afterDot.firstIndex(of: ":") {
                    nameCandidate = String(afterDot[afterDot.startIndex..<colonIdx]).trimmingCharacters(in: .whitespaces)
                } else if let commaIdx = afterDot.firstIndex(of: ",") {
                    nameCandidate = String(afterDot[afterDot.startIndex..<commaIdx]).trimmingCharacters(in: .whitespaces)
                } else {
                    nameCandidate = String(afterDot)
                }
            }

            // Strip parenthetical suffix like "(NYY)" or "(2024)"
            let cleanName: String
            if let parenIdx = nameCandidate.firstIndex(of: "(") {
                cleanName = String(nameCandidate[nameCandidate.startIndex..<parenIdx]).trimmingCharacters(in: .whitespaces)
            } else {
                cleanName = nameCandidate
            }

            if !cleanName.isEmpty && !seen.contains(cleanName.lowercased()) {
                names.append(cleanName)
                seen.insert(cleanName.lowercased())
            }
        }

        return names
    }

    /// Detects which season the previous result was about.
    private func detectSeasonFromContext(previousContent: String, followUp: String) -> Int? {
        // Check follow-up first for explicit year
        if let year = PlayerNameMatcher.detectSeason(followUp) { return year }

        // Check previous content for year in title (e.g., "2025 OPS Leaders")
        let yearPattern = /\b(20\d{2}|19\d{2})\b/
        if let match = previousContent.firstMatch(of: yearPattern) {
            return Int(match.1)
        }

        return nil
    }

    private func looksContextual(_ question: String) -> Bool {
        let lower = question.lowercased()
        let words = lower.split(separator: " ")

        // Long questions are likely self-contained
        if words.count >= 8 { return false }

        // Contains a recognized player name → standalone
        for i in 0..<words.count {
            if !PlayerNameMatcher.commonWordLastNames.contains(String(words[i])),
               PlayerNameMatcher.matchPlayer(String(words[i])) != nil { return false }
            if i + 1 < words.count {
                let pair = "\(words[i]) \(words[i + 1])"
                if PlayerNameMatcher.matchPlayer(pair) != nil { return false }
            }
        }

        // Starts with standalone question patterns — these form complete queries
        let standaloneStarters = ["who ", "how many ", "top ", "list ", "compare ", "rank ",
                                  "highest ", "lowest ", "most ", "fewest ", "best ", "worst ",
                                  "what is ", "what are ", "what was ", "what were ",
                                  "show ", "give me "]
        for starter in standaloneStarters {
            if lower.hasPrefix(starter) { return false }
        }

        // Contains "leaders" → likely a standalone leaderboard query ("career doubles leaders")
        if lower.contains("leaders") || lower.contains("leaderboard") { return false }

        // Contains explicit comparison signal → standalone
        let compSignals = [" vs ", " vs. ", " versus ", " compared to "]
        if compSignals.contains(where: { lower.contains($0) }) { return false }

        return true
    }
}
