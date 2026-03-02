import SwiftUI

@Observable
@MainActor
final class AppState {
    var messages: [Message] = []
    var isLoading = false
    var currentStreamingText = ""
    var showAPIKeySetup = false
    var searchHistory: [String] = []
    /// Stores (originalQuery, ambiguousLastName) when disambiguation is pending
    var pendingDisambiguation: (query: String, lastName: String)?

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

    func sendQuestion(_ question: String, followUpContext: String? = nil) {
        let trimmed = question.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }

        // For follow-ups, store with context prefix if the question looks contextual
        if let context = followUpContext, looksContextual(trimmed) {
            addToSearchHistory("\(context) → \(trimmed)")
        } else {
            addToSearchHistory(trimmed)
        }

        // Intercept comparison queries — build response from DB, skip Claude
        if let (p1, p2) = PlayerNameMatcher.parseComparison(trimmed) {
            let response = PlayerCardService.buildComparison(player1: p1, player2: p2)
            messages.append(Message(role: .user, content: trimmed))
            messages.append(Message(role: .assistant, content: response))
            queryEngine.injectHistory(question: trimmed, answer: "Compared \(p1) and \(p2). \(response)")
            return
        }

        // Intercept streak history queries — build response from DB, skip Claude
        if let streak = PlayerNameMatcher.parseStreakQuery(trimmed),
           let response = PlayerCardService.buildStreakList(name: streak.name, performance: streak.performance, season: streak.season) {
            messages.append(Message(role: .user, content: trimmed))
            messages.append(Message(role: .assistant, content: response))
            queryEngine.injectHistory(question: trimmed, answer: response)
            return
        }

        // Intercept current hot streak queries — build response from DB, skip Claude
        if let playerName = PlayerNameMatcher.parseCurrentForm(trimmed),
           let response = PlayerCardService.buildCurrentHotStreak(name: playerName) {
            messages.append(Message(role: .user, content: trimmed))
            messages.append(Message(role: .assistant, content: response))
            queryEngine.injectHistory(question: trimmed, answer: response)
            return
        }

        // Intercept single-stat lookup queries — "Judge home runs", "Ohtani OPS"
        if let lookup = PlayerNameMatcher.parseSingleStatLookup(trimmed),
           let response = PlayerCardService.buildSingleStatLookup(name: lookup.name, stat: lookup.stat, season: lookup.season) {
            messages.append(Message(role: .user, content: trimmed))
            messages.append(Message(role: .assistant, content: response))
            queryEngine.injectHistory(question: trimmed, answer: response)
            return
        }

        // Intercept career lookup queries — "Judge career stats", "Judge career home runs"
        if let career = PlayerNameMatcher.parseCareerLookup(trimmed),
           let response = PlayerCardService.buildCareerLookup(name: career.name, stat: career.stat) {
            messages.append(Message(role: .user, content: trimmed))
            messages.append(Message(role: .assistant, content: response))
            queryEngine.injectHistory(question: trimmed, answer: response)
            return
        }

        // Intercept season lookup queries — build response from DB, skip Claude
        if let (playerName, season) = PlayerNameMatcher.parseSeasonLookup(trimmed),
           let response = PlayerCardService.buildSeasonSummary(name: playerName, season: season) {
            messages.append(Message(role: .user, content: trimmed))
            messages.append(Message(role: .assistant, content: response))
            queryEngine.injectHistory(question: trimmed, answer: response)
            return
        }

        // Intercept platoon splits queries — "Judge vs lefties", "Soto splits"
        if let splits = PlayerNameMatcher.parsePlatoonSplits(trimmed),
           let response = PlayerCardService.buildPlatoonSplits(name: splits.name, hand: splits.hand, season: splits.season) {
            messages.append(Message(role: .user, content: trimmed))
            messages.append(Message(role: .assistant, content: response))
            queryEngine.injectHistory(question: trimmed, answer: response)
            return
        }

        // Intercept threshold queries — "who hit 40 home runs?", "players batting over .300"
        if let threshold = PlayerNameMatcher.parseThreshold(trimmed) {
            let response = PlayerCardService.buildThresholdLeaderboard(
                stat: threshold.stat, threshold: threshold.threshold,
                comparison: threshold.comparison, season: threshold.season)
            messages.append(Message(role: .user, content: trimmed))
            messages.append(Message(role: .assistant, content: response))
            queryEngine.injectHistory(question: trimmed, answer: response)
            return
        }

        // Intercept team ranking queries — "what team hit the most HR?"
        if let teamRanking = PlayerNameMatcher.parseTeamRanking(trimmed) {
            let response = PlayerCardService.buildTeamRanking(
                stat: teamRanking.stat, season: teamRanking.season)
            messages.append(Message(role: .user, content: trimmed))
            messages.append(Message(role: .assistant, content: response))
            queryEngine.injectHistory(question: trimmed, answer: response)
            return
        }

        // Intercept team total queries — "how many HR did the Yankees hit?"
        if let teamTotal = PlayerNameMatcher.parseTeamTotal(trimmed) {
            let response = PlayerCardService.buildTeamTotal(
                teamCode: teamTotal.teamCode, stat: teamTotal.stat, season: teamTotal.season)
            messages.append(Message(role: .user, content: trimmed))
            messages.append(Message(role: .assistant, content: response))
            queryEngine.injectHistory(question: trimmed, answer: response)
            return
        }

        // Intercept team stats queries — "Yankees hitters", "Dodgers OPS leaders"
        if let teamQuery = PlayerNameMatcher.parseTeamStats(trimmed) {
            let response = PlayerCardService.buildTeamStats(
                teamCode: teamQuery.teamCode, stat: teamQuery.stat, season: teamQuery.season)
            messages.append(Message(role: .user, content: trimmed))
            messages.append(Message(role: .assistant, content: response))
            queryEngine.injectHistory(question: trimmed, answer: response)
            return
        }

        // Intercept leaderboard queries — "HR leaders", "top 5 OPS"
        if let board = PlayerNameMatcher.parseLeaderboard(trimmed) {
            let response = PlayerCardService.buildLeaderboard(stat: board.stat, scope: board.scope, limit: board.limit)
            messages.append(Message(role: .user, content: trimmed))
            messages.append(Message(role: .assistant, content: response))
            queryEngine.injectHistory(question: trimmed, answer: response)
            return
        }

        // Intercept stat definition queries — "what is OPS?", "explain BABIP"
        if let defn = PlayerNameMatcher.parseStatDefinition(trimmed) {
            let statName = defn.displayName == defn.abbrev ? defn.displayName : defn.displayName.lowercased()
            let response = "**\(defn.abbrev)** — \(defn.definition)\n\n[SUGGEST]\(statName) leaders[/SUGGEST]\n[SUGGEST]career \(statName) leaders[/SUGGEST]"
            messages.append(Message(role: .user, content: trimmed))
            messages.append(Message(role: .assistant, content: response))
            queryEngine.injectHistory(question: trimmed, answer: response)
            return
        }

        // Ambiguous last name — show "Did you mean?" with tappable player links
        if let candidates = PlayerNameMatcher.findAmbiguousPlayers(trimmed) {
            // Find which last name was ambiguous
            let lower = trimmed.lowercased()
            let ambiguousLast = PlayerNameMatcher.lastNameIndex.first(where: { key, players in
                players.count > 1 && PlayerNameMatcher.containsWord(key, in: lower)
            })?.key ?? ""

            pendingDisambiguation = (query: trimmed, lastName: ambiguousLast)
            let links = candidates.map { "[\($0)](statchat://player/\($0.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? $0))" }
            let response = "Multiple players match that name. Did you mean:\n\n" + links.joined(separator: "\n\n")
            messages.append(Message(role: .user, content: trimmed))
            messages.append(Message(role: .assistant, content: response))
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
                // Append contextual SUGGEST pills based on query content
                if streamingIndex < messages.count {
                    let pills = buildFallbackPills(for: trimmed)
                    if !pills.isEmpty {
                        let existing = messages[streamingIndex].content
                        messages[streamingIndex] = Message(role: .assistant, content: existing + "\n\n" + pills)
                    }
                }
                isLoading = false
                currentStreamingText = ""
            } catch {
                guard !Task.isCancelled else { return }
                isLoading = false
                currentStreamingText = ""
                guard streamingIndex < messages.count else { return }
                let pills = buildFallbackPills(for: trimmed)
                let errorContent = pills.isEmpty
                    ? error.localizedDescription
                    : error.localizedDescription + "\n\n" + pills
                messages[streamingIndex] = Message(role: .error, content: errorContent)
            }
        }
    }

    func resolveDisambiguation(with fullName: String) {
        guard let pending = pendingDisambiguation else { return }

        // Replace the ambiguous last name with the full name in the original query
        let correctedQuery: String
        if pending.lastName.isEmpty {
            correctedQuery = fullName
        } else {
            // Case-insensitive replacement of the ambiguous last name with the full name
            let lower = pending.query.lowercased()
            if let range = lower.range(of: pending.lastName) {
                var result = pending.query
                let startIdx = pending.query.index(pending.query.startIndex, offsetBy: lower.distance(from: lower.startIndex, to: range.lowerBound))
                let endIdx = pending.query.index(startIdx, offsetBy: pending.lastName.count)
                result.replaceSubrange(startIdx..<endIdx, with: fullName)
                correctedQuery = result
            } else {
                correctedQuery = pending.query
            }
        }

        // Remove the disambiguation messages (user question + "Did you mean?" response)
        if messages.count >= 2 {
            messages.removeLast(2)
        }

        pendingDisambiguation = nil
        sendQuestion(correctedQuery)
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

    /// Build contextual SUGGEST pills for Claude fallthrough responses based on query content.
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
            // Then single word (last name)
            if let match = PlayerNameMatcher.matchPlayer(words[i]) {
                detectedPlayer = match
                break
            }
        }

        // Detect stat keyword
        let detectedStat = PlayerNameMatcher.matchStat(lower)

        if let player = detectedPlayer {
            let season = PlayerNameMatcher.detectSeason(lower, defaultToMostRecent: true) ?? 2025
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

    /// Heuristic: does this question need prior context to make sense?
    /// Short questions without player names or standalone openers are contextual.
    private func looksContextual(_ question: String) -> Bool {
        let lower = question.lowercased()
        let words = lower.split(separator: " ")

        // Long questions are likely self-contained
        if words.count >= 8 { return false }

        // Contains a recognized player name → standalone
        for i in 0..<words.count {
            if PlayerNameMatcher.matchPlayer(String(words[i])) != nil { return false }
            if i + 1 < words.count {
                let pair = "\(words[i]) \(words[i + 1])"
                if PlayerNameMatcher.matchPlayer(pair) != nil { return false }
            }
        }

        // Starts with standalone question patterns
        let standaloneStarters = ["who ", "how many ", "top ", "list ", "compare ", "rank "]
        for starter in standaloneStarters {
            if lower.hasPrefix(starter) { return false }
        }

        return true
    }
}
