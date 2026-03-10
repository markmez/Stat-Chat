import SwiftUI

@Observable
@MainActor
final class AppState {
    var messages: [Message] = []
    var isLoading = false
    var currentStreamingText = ""
    var searchHistory: [String] = []
    /// Stores (originalQuery, ambiguousLastName) when disambiguation is pending
    var pendingDisambiguation: (query: String, lastName: String)?

    private let backendService = BackendService()
    private let historyKey = "searchHistory"
    private let maxHistoryItems = 50
    private var currentQueryTask: Task<Void, Never>?
    private var conversationHistory: [(String, String)] = []
    private let maxHistory = 5

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
        PlayerNameMatcher.load()
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

        // Intercept comparison queries — build response from structured data
        let compResult = PlayerNameMatcher.parseComparison(trimmed)
        if let (p1, p2) = compResult {
            let bothLocal = PlayerCardService.hasLocalData(name: p1) && PlayerCardService.hasLocalData(name: p2)
            if bothLocal {
                // Both in local DB — synchronous
                let response: String
                if PlayerCardService.isPitcher(name: p1) && PlayerCardService.isPitcher(name: p2) {
                    response = PlayerCardService.buildPitchingComparison(player1: p1, player2: p2)
                } else {
                    response = PlayerCardService.buildComparison(player1: p1, player2: p2)
                }
                messages.append(Message(role: .user, content: trimmed))
                messages.append(Message(role: .assistant, content: response))
                addToConversationHistory(question: trimmed, answer: "Compared \(p1) and \(p2). \(response)")
                return
            } else {
                // One or both need backend data — async fetch
                messages.append(Message(role: .user, content: trimmed))
                messages.append(Message(role: .assistant, content: ""))
                let loadingIndex = messages.count - 1
                isLoading = true
                currentQueryTask = Task {
                    let response = await PlayerCardService.buildComparisonAsync(player1: p1, player2: p2)
                    guard !Task.isCancelled else { return }
                    messages[loadingIndex] = Message(role: .assistant, content: response)
                    isLoading = false
                    addToConversationHistory(question: trimmed, answer: "Compared \(p1) and \(p2). \(response)")
                }
                return
            }
        }

        // Intercept streak history queries — build response from DB, skip Claude
        if let streak = PlayerNameMatcher.parseStreakQuery(trimmed) {
            let response: String?
            if PlayerCardService.isPitcher(name: streak.name) {
                response = PlayerCardService.buildPitchingStreakList(name: streak.name, performance: streak.performance, season: streak.season)
            } else {
                response = PlayerCardService.buildStreakList(name: streak.name, performance: streak.performance, season: streak.season)
            }
            if let response {
                messages.append(Message(role: .user, content: trimmed))
                messages.append(Message(role: .assistant, content: response))
                addToConversationHistory(question: trimmed, answer: response)
                return
            }
        }

        // Intercept current hot streak queries — build response from DB, skip Claude
        if let playerName = PlayerNameMatcher.parseCurrentForm(trimmed) {
            let response: String?
            if PlayerCardService.isPitcher(name: playerName) {
                response = PlayerCardService.buildPitchingCurrentHotStreak(name: playerName)
            } else {
                response = PlayerCardService.buildCurrentHotStreak(name: playerName)
            }
            if let response {
                messages.append(Message(role: .user, content: trimmed))
                messages.append(Message(role: .assistant, content: response))
                addToConversationHistory(question: trimmed, answer: response)
                return
            }
        }

        // Intercept single-stat lookup queries — "Judge home runs", "Ohtani OPS"
        if let lookup = PlayerNameMatcher.parseSingleStatLookup(trimmed) {
            let response: String?
            if PlayerCardService.isPitcher(name: lookup.name) || PlayerNameMatcher.isPitchingStat(lookup.stat) {
                response = PlayerCardService.buildPitchingSingleStatLookup(name: lookup.name, stat: lookup.stat, season: lookup.season)
            } else {
                response = PlayerCardService.buildSingleStatLookup(name: lookup.name, stat: lookup.stat, season: lookup.season)
            }
            if let response {
                messages.append(Message(role: .user, content: trimmed))
                messages.append(Message(role: .assistant, content: response))
                addToConversationHistory(question: trimmed, answer: response)
                return
            }
        }

        // Intercept career lookup queries — "Judge career stats", "Judge career home runs"
        if let career = PlayerNameMatcher.parseCareerLookup(trimmed) {
            let response: String?
            if PlayerCardService.isPitcher(name: career.name) {
                response = PlayerCardService.buildPitchingCareerLookup(name: career.name, stat: career.stat)
            } else {
                response = PlayerCardService.buildCareerLookup(name: career.name, stat: career.stat)
            }
            if let response {
                messages.append(Message(role: .user, content: trimmed))
                messages.append(Message(role: .assistant, content: response))
                addToConversationHistory(question: trimmed, answer: response)
                return
            }
        }

        // Intercept season lookup queries — build response from DB, skip Claude
        if let (playerName, season) = PlayerNameMatcher.parseSeasonLookup(trimmed) {
            let response: String?
            if PlayerCardService.isPitcher(name: playerName) {
                response = PlayerCardService.buildPitchingSeasonSummary(name: playerName, season: season)
            } else {
                response = PlayerCardService.buildSeasonSummary(name: playerName, season: season)
            }
            if let response {
                messages.append(Message(role: .user, content: trimmed))
                messages.append(Message(role: .assistant, content: response))
                addToConversationHistory(question: trimmed, answer: response)
                return
            }
        }

        // Intercept platoon splits queries — "Judge vs lefties", "Soto splits"
        if let splits = PlayerNameMatcher.parsePlatoonSplits(trimmed) {
            let response: String?
            if PlayerCardService.isPitcher(name: splits.name) {
                response = PlayerCardService.buildPitchingPlatoonSplits(name: splits.name, hand: splits.hand, season: splits.season)
            } else {
                response = PlayerCardService.buildPlatoonSplits(name: splits.name, hand: splits.hand, season: splits.season)
            }
            if let response {
                messages.append(Message(role: .user, content: trimmed))
                messages.append(Message(role: .assistant, content: response))
                addToConversationHistory(question: trimmed, answer: response)
                return
            }
        }

        // Intercept home/away splits queries — "Judge home vs away", "Soto at home"
        if let splits = PlayerNameMatcher.parseHomeAwaySplits(trimmed) {
            let response: String?
            if PlayerCardService.isPitcher(name: splits.name) {
                response = PlayerCardService.buildPitchingHomeAwaySplits(name: splits.name, location: splits.location, season: splits.season)
            } else {
                response = PlayerCardService.buildHomeAwaySplits(name: splits.name, location: splits.location, season: splits.season)
            }
            if let response {
                messages.append(Message(role: .user, content: trimmed))
                messages.append(Message(role: .assistant, content: response))
                addToConversationHistory(question: trimmed, answer: response)
                return
            }
        }

        // Intercept milestone queries — "how many times has someone hit 50 HR?"
        // Always uses backend since milestones span all history.
        if let milestone = PlayerNameMatcher.parseMilestone(trimmed) {
            let isPitching = PlayerNameMatcher.isPitchingStat(milestone.stat)
            // Try local first
            let localResponse = PlayerCardService.buildMilestone(
                stat: milestone.stat, threshold: milestone.threshold,
                since: milestone.since, isPitching: isPitching)
            // If local returned results or query is within local range, use it
            let needsBackend = milestone.since == nil || !PlayerCardService.isLocalSeason(milestone.since!)
            if !needsBackend || !localResponse.contains("No player has reached") {
                messages.append(Message(role: .user, content: trimmed))
                messages.append(Message(role: .assistant, content: localResponse))
                addToConversationHistory(question: trimmed, answer: localResponse)
                return
            }
            // Backend fallback
            messages.append(Message(role: .user, content: trimmed))
            messages.append(Message(role: .assistant, content: ""))
            let loadingIndex = messages.count - 1
            isLoading = true
            currentQueryTask = Task {
                let service = BackendService()
                let resp = try? await service.fetchMilestone(
                    stat: milestone.stat.dbColumn, value: milestone.threshold,
                    since: milestone.since, isPitching: isPitching, limit: 50)
                guard !Task.isCancelled else { return }
                let response = resp.map { PlayerCardService.formatBackendMilestone($0, stat: milestone.stat) }
                    ?? "No data available for that query."
                messages[loadingIndex] = Message(role: .assistant, content: response)
                isLoading = false
                addToConversationHistory(question: trimmed, answer: response)
            }
            return
        }

        // Intercept threshold queries — "who hit 40 home runs?", "players batting over .300"
        if let threshold = PlayerNameMatcher.parseThreshold(trimmed) {
            let isPitching = PlayerNameMatcher.isPitchingStat(threshold.stat)
            if PlayerCardService.isLocalSeason(threshold.season) {
                // Local DB has this season
                let response: String
                if isPitching {
                    response = PlayerCardService.buildPitchingThresholdLeaderboard(
                        stat: threshold.stat, threshold: threshold.threshold,
                        comparison: threshold.comparison, season: threshold.season)
                } else {
                    response = PlayerCardService.buildThresholdLeaderboard(
                        stat: threshold.stat, threshold: threshold.threshold,
                        comparison: threshold.comparison, season: threshold.season)
                }
                messages.append(Message(role: .user, content: trimmed))
                messages.append(Message(role: .assistant, content: response))
                addToConversationHistory(question: trimmed, answer: response)
                return
            }
            // Backend fallback for pre-2016
            messages.append(Message(role: .user, content: trimmed))
            messages.append(Message(role: .assistant, content: ""))
            let loadingIndex = messages.count - 1
            isLoading = true
            currentQueryTask = Task {
                let service = BackendService()
                let resp = try? await service.fetchThreshold(
                    stat: threshold.stat.dbColumn, value: threshold.threshold,
                    comparison: threshold.comparison, season: threshold.season,
                    isPitching: isPitching, limit: 50)
                guard !Task.isCancelled else { return }
                let response = resp.map { PlayerCardService.formatBackendThreshold($0, stat: threshold.stat) }
                    ?? "No data available for that query."
                messages[loadingIndex] = Message(role: .assistant, content: response)
                isLoading = false
                addToConversationHistory(question: trimmed, answer: response)
            }
            return
        }

        // Intercept team ranking queries — "what team hit the most HR?"
        if let teamRanking = PlayerNameMatcher.parseTeamRanking(trimmed) {
            let response = PlayerCardService.buildTeamRanking(
                stat: teamRanking.stat, season: teamRanking.season)
            messages.append(Message(role: .user, content: trimmed))
            messages.append(Message(role: .assistant, content: response))
            addToConversationHistory(question: trimmed, answer: response)
            return
        }

        // Intercept team total queries — "how many HR did the Yankees hit?"
        if let teamTotal = PlayerNameMatcher.parseTeamTotal(trimmed) {
            let response = PlayerCardService.buildTeamTotal(
                teamCode: teamTotal.teamCode, stat: teamTotal.stat, season: teamTotal.season)
            messages.append(Message(role: .user, content: trimmed))
            messages.append(Message(role: .assistant, content: response))
            addToConversationHistory(question: trimmed, answer: response)
            return
        }

        // Intercept team stats queries — "Yankees hitters", "Dodgers OPS leaders"
        if let teamQuery = PlayerNameMatcher.parseTeamStats(trimmed) {
            let response: String
            if let stat = teamQuery.stat, PlayerNameMatcher.isPitchingStat(stat) {
                response = PlayerCardService.buildPitchingTeamStats(
                    teamCode: teamQuery.teamCode, stat: stat, season: teamQuery.season)
            } else {
                response = PlayerCardService.buildTeamStats(
                    teamCode: teamQuery.teamCode, stat: teamQuery.stat, season: teamQuery.season)
            }
            messages.append(Message(role: .user, content: trimmed))
            messages.append(Message(role: .assistant, content: response))
            addToConversationHistory(question: trimmed, answer: response)
            return
        }

        // Intercept leaderboard queries — "HR leaders", "top 5 OPS"
        if let board = PlayerNameMatcher.parseLeaderboard(trimmed) {
            let isPitching = PlayerNameMatcher.isPitchingStat(board.stat)
            // Check if this is within local data range
            let isLocal: Bool
            let scopeStr: String
            var seasonForBackend: Int?
            switch board.scope {
            case .season(let year):
                isLocal = PlayerCardService.isLocalSeason(year)
                scopeStr = "season"
                seasonForBackend = year
            case .career:
                isLocal = false  // career spans all history
                scopeStr = "career"
            case .allTimeSingleSeason:
                isLocal = false  // all-time spans all history
                scopeStr = "all_time"
            }

            if isLocal {
                let response: String
                if isPitching {
                    response = PlayerCardService.buildPitchingLeaderboard(stat: board.stat, scope: board.scope, limit: board.limit)
                } else {
                    response = PlayerCardService.buildLeaderboard(stat: board.stat, scope: board.scope, limit: board.limit)
                }
                messages.append(Message(role: .user, content: trimmed))
                messages.append(Message(role: .assistant, content: response))
                addToConversationHistory(question: trimmed, answer: response)
                return
            }

            // Backend fallback for pre-2016, career, and all-time queries
            messages.append(Message(role: .user, content: trimmed))
            messages.append(Message(role: .assistant, content: ""))
            let loadingIndex = messages.count - 1
            isLoading = true
            currentQueryTask = Task {
                let service = BackendService()
                let resp = try? await service.fetchLeaderboard(
                    stat: board.stat.dbColumn, season: seasonForBackend,
                    scope: scopeStr, limit: board.limit, isPitching: isPitching)
                guard !Task.isCancelled else { return }
                let response = resp.map { PlayerCardService.formatBackendLeaderboard($0, stat: board.stat) }
                    ?? "No data available for that query."
                messages[loadingIndex] = Message(role: .assistant, content: response)
                isLoading = false
                addToConversationHistory(question: trimmed, answer: response)
            }
            return
        }

        // Intercept stat definition queries — "what is OPS?", "explain BABIP"
        if let defn = PlayerNameMatcher.parseStatDefinition(trimmed) {
            let statName = defn.displayName == defn.abbrev ? defn.displayName : defn.displayName.lowercased()
            let response = "**\(defn.abbrev)** — \(defn.definition)\n\n[SUGGEST]\(statName) leaders[/SUGGEST]\n[SUGGEST]career \(statName) leaders[/SUGGEST]"
            messages.append(Message(role: .user, content: trimmed))
            messages.append(Message(role: .assistant, content: response))
            addToConversationHistory(question: trimmed, answer: response)
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
                let answer = try await backendService.ask(
                    question: trimmed,
                    deviceId: Self.deviceId,
                    history: conversationHistory
                ) { [self] chunk in
                    guard !Task.isCancelled, streamingIndex < messages.count else { return }
                    currentStreamingText += chunk
                    messages[streamingIndex] = Message(role: .assistant, content: currentStreamingText)
                }
                guard !Task.isCancelled else { return }
                addToConversationHistory(question: trimmed, answer: answer)
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
        conversationHistory.removeAll()
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

    /// Check if a player name resolves to a pitcher
    private func isPitcherQuery(_ name: String) -> Bool {
        PlayerCardService.isPitcher(name: name)
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
