import Foundation

@MainActor
final class QueryEngine {
    private let anthropic = AnthropicService()
    private let database = DatabaseService()
    private var history: [(String, String)] = []
    private let maxHistory = 5

    struct StreamResult {
        let fullText: String
    }

    /// Ask a natural language baseball question. Calls `onChunk` for each streamed token.
    /// Returns the full assembled answer.
    func ask(_ question: String, onChunk: @escaping @MainActor (String) -> Void) async throws -> String {
        // Step 0: Route the query — try local classification first, fall back to Claude
        let route: String
        if let localRoute = classifyLocally(question) {
            route = localRoute
        } else {
            let routeJSON = try await anthropic.routeQuery(question: question, history: history)
            if routeJSON.contains("stat_explanation") {
                route = "stat_explanation"
            } else if routeJSON.contains("current_form") {
                route = "current_form"
            } else if routeJSON.contains("streak_finder") {
                route = "streak_finder"
            } else {
                route = "simple_lookup"
            }
        }

        let fullAnswer: String
        if route == "stat_explanation" {
            if let local = handleLocalStatExplanation(question: question, onChunk: onChunk) {
                fullAnswer = local
            } else {
                // Stat not in local dictionary — fall back to Claude
                let stream = anthropic.explainStat(question: question, history: history)
                fullAnswer = try await collectStream(stream, onChunk: onChunk)
            }
        } else if route == "current_form" {
            fullAnswer = try await handleCurrentFormQuery(question: question, onChunk: onChunk)
        } else if route == "streak_finder" {
            fullAnswer = try await handleStreakQuery(question: question, onChunk: onChunk)
        } else {
            fullAnswer = try await handleSQLQuery(question: question, onChunk: onChunk)
        }
        addToHistory(question: question, answer: fullAnswer)
        return fullAnswer
    }

    // MARK: - Local query classification (skip Claude router for obvious patterns)

    private func classifyLocally(_ question: String) -> String? {
        let q = question.lowercased()
        // Streak patterns
        if q.contains("streak") || q.contains("slump") || q.contains("on fire")
           || q.contains("hot stretch") || q.contains("cold stretch") {
            return "streak_finder"
        }
        // Current form patterns
        if q.contains("lately") || q.contains("recently") || q.contains("current form")
           || q.contains("right now") || q.contains("doing now") {
            return "current_form"
        }
        // Stat explanation patterns
        if q.hasPrefix("what is ") || q.hasPrefix("what does ") || q.hasPrefix("explain ")
           || q.hasPrefix("what's ") || q.hasPrefix("define ") {
            // Only classify as stat_explanation if no player name is detected
            if PlayerNameMatcher.matchStat(q) != nil {
                let hasPlayer = PlayerNameMatcher.sortedNames.contains { name in
                    PlayerNameMatcher.containsWord(name.lowercased(), in: q)
                }
                if !hasPlayer { return "stat_explanation" }
            }
        }
        if (q.contains("how is") || q.contains("how do you")) && q.contains("calculated") {
            return "stat_explanation"
        }
        return nil
    }

    // MARK: - Local stat explanation (zero API cost)

    private func handleLocalStatExplanation(
        question: String,
        onChunk: @escaping @MainActor (String) -> Void
    ) -> String? {
        // Try to match a stat in the question
        let lower = question.lowercased()
        var definition: String?
        var abbrev: String?

        // Try via statAliasMap (handles "batting average", "on-base percentage", etc.)
        if let stat = PlayerNameMatcher.matchStat(lower),
           let defn = StatDefinitions.lookup(stat.displayAbbrev) {
            abbrev = stat.displayAbbrev
            definition = defn
        }

        // Try direct abbreviation lookup for stats not in statAliasMap
        if definition == nil {
            let directAbbrevs = ["war", "wrc+", "k", "pa", "sf", "1b", "fld%"]
            for da in directAbbrevs {
                if PlayerNameMatcher.containsWord(da, in: lower) {
                    let key = da.uppercased()
                    let lookupKey = key == "WRC+" ? "wRC+" : key
                    if let defn = StatDefinitions.lookup(lookupKey) {
                        abbrev = lookupKey
                        definition = defn
                        break
                    }
                }
            }
        }

        guard let abbrev, let definition else { return nil }

        let response = "**\(abbrev)** — \(definition)"
        onChunk(response)
        return response
    }

    // MARK: - Standard SQL query path

    private func handleSQLQuery(
        question: String,
        onChunk: @escaping @MainActor (String) -> Void
    ) async throws -> String {
        let sql = try await anthropic.generateSQL(question: question, history: history)

        if sql.contains("OFF_TOPIC") {
            let msg = "I'm a baseball stats engine — ask me about player stats, leaders, averages, and more!"
            onChunk(msg)
            return msg
        }
        if sql.contains("NO_DATA") {
            let msg = "I don't have the data needed for that question yet. Try asking about 2024 season batting stats!"
            onChunk(msg)
            return msg
        }

        let result: DatabaseService.QueryResult
        do {
            result = try database.execute(sql: sql)
        } catch {
            let msg = "I had trouble with that query. Could you rephrase? (\(error.localizedDescription))"
            onChunk(msg)
            return msg
        }

        let isStreakQuery = sql.lowercased().contains("streaks")

        // Streak fallback: if SQL queried streaks table and got 0 results
        if result.rows.isEmpty && isStreakQuery {
            if let answer = try await handleStreakFallback(sql: sql, question: question, onChunk: onChunk) {
                return answer
            }
        }

        // Format results
        let resultsText: String
        if result.rows.isEmpty {
            resultsText = "No results found."
        } else {
            resultsText = formatTable(columns: result.columns, rows: result.rows)
        }

        // Generate answer (streaming)
        let stream: AsyncThrowingStream<String, Error>
        if isStreakQuery && !result.rows.isEmpty {
            stream = anthropic.describeStreaks(
                question: question, streakData: resultsText, history: history
            )
        } else {
            stream = anthropic.generateAnswer(
                question: question, sql: sql, results: resultsText, history: history
            )
        }

        return try await collectStream(stream, onChunk: onChunk)
    }

    // MARK: - Current form query path

    private func handleCurrentFormQuery(
        question: String,
        onChunk: @escaping @MainActor (String) -> Void
    ) async throws -> String {
        // Extract player name locally — avoid a full SQL generation API call
        let playerName: String
        if let name = extractPlayerNameLocally(from: question) {
            playerName = name
        } else {
            // Local extraction failed — fall back to standard SQL query path
            return try await handleSQLQuery(question: question, onChunk: onChunk)
        }

        // Extract season from question text directly
        let season = String(PlayerNameMatcher.detectSeason(question, defaultToMostRecent: true)
                            ?? 2025)

        // Query current_form table
        let formSQL = """
            SELECT cf.form_start_date, cf.form_start_game_number, cf.total_season_games, cf.num_games,
                   cf.at_bats, cf.hits, cf.doubles, cf.triples, cf.home_runs,
                   cf.runs, cf.rbi, cf.walks, cf.strikeouts, cf.plate_appearances,
                   cf.batting_avg, cf.obp, cf.slg, cf.ops, cf.iso,
                   cf.season_at_bats, cf.season_hits, cf.season_doubles, cf.season_triples,
                   cf.season_home_runs, cf.season_runs, cf.season_rbi,
                   cf.season_walks, cf.season_strikeouts, cf.season_plate_appearances,
                   p.name
            FROM current_form cf
            JOIN players p ON cf.player_id = p.player_id
            WHERE p.name LIKE '%\(playerName)%' AND cf.season = \(season)
            LIMIT 1
            """

        let formResult: DatabaseService.QueryResult
        do {
            formResult = try database.execute(sql: formSQL)
        } catch {
            return try await handleSQLQuery(question: question, onChunk: onChunk)
        }

        guard let row = formResult.rows.first, row.count >= 29 else {
            // No current form data — fall back to regular query
            return try await handleSQLQuery(question: question, onChunk: onChunk)
        }

        // Also fetch season stats for comparison
        let seasonSQL = """
            SELECT s.games, s.at_bats, s.runs, s.hits, s.home_runs, s.rbi,
                   s.walks, s.strikeouts, s.batting_avg, s.obp, s.slg, s.ops
            FROM season_batting_stats s
            JOIN players p ON s.player_id = p.player_id
            WHERE p.name LIKE '%\(playerName)%' AND s.season = \(season)
            LIMIT 1
            """
        let seasonResult = try? database.execute(sql: seasonSQL)
        let seasonRow = seasonResult?.rows.first

        // Build form data text for Claude
        let resolvedName = row[28]
        var formDataText = "Player: \(resolvedName)\n"
        formDataText += "Season: \(season)\n"
        formDataText += "Form start date: \(row[0])\n"
        formDataText += "Form start game number: \(row[1]) (1-indexed)\n"
        formDataText += "Total season games: \(row[2])\n"
        formDataText += "Form period games: \(row[3])\n"
        formDataText += "Form stats: \(row[3]) G, \(row[4]) AB, \(row[9]) R, \(row[5]) H, \(row[8]) HR, \(row[10]) RBI, \(row[11]) BB, \(row[12]) SO, \(row[14]) AVG, \(row[15]) OBP, \(row[16]) SLG, \(row[17]) OPS\n"

        if let sRow = seasonRow, sRow.count >= 12 {
            formDataText += "\nFull season stats: \(sRow[0]) G, \(sRow[1]) AB, \(sRow[2]) R, \(sRow[3]) H, \(sRow[4]) HR, \(sRow[5]) RBI, \(sRow[6]) BB, \(sRow[7]) SO, \(sRow[8]) AVG, \(sRow[9]) OBP, \(sRow[10]) SLG, \(sRow[11]) OPS\n"
        }

        let stream = anthropic.describeCurrentForm(
            question: question, formData: formDataText, history: history
        )
        return try await collectStream(stream, onChunk: onChunk)
    }

    // MARK: - Streak query path (routed by classifier)

    private func handleStreakQuery(
        question: String,
        onChunk: @escaping @MainActor (String) -> Void
    ) async throws -> String {
        let sql = try await anthropic.generateSQL(question: question, history: history)

        if sql.contains("OFF_TOPIC") || sql.contains("NO_DATA") {
            let msg = "I don't have streak data for that query. Try asking about a specific player's streaks in 2024 or 2025."
            onChunk(msg)
            return msg
        }

        let result: DatabaseService.QueryResult
        do {
            result = try database.execute(sql: sql)
        } catch {
            let msg = "I had trouble with that streak query. Could you rephrase? (\(error.localizedDescription))"
            onChunk(msg)
            return msg
        }

        var rows = result.rows
        var columns = result.columns
        var usedFallback = false

        if rows.isEmpty {
            let allStreaks = getAllStreaksForQuery(sql: sql)
            if allStreaks.rows.isEmpty {
                let msg = "I don't have streak data for that player/season. Streak data is available for qualified batters (400+ PA) in 2024-2025."
                onChunk(msg)
                return msg
            }
            rows = allStreaks.rows
            columns = allStreaks.columns
            usedFallback = true
        }

        var streakData = formatTable(columns: columns, rows: rows)

        // Tier 2 fallback: if single segment (no change points), check streaks_sensitive
        if usedFallback || rows.count == 1 {
            if let fallbackText = findSensitiveStreaks(rows: rows, columns: columns) {
                streakData += "\n\n" + fallbackText
            } else if let firstRow = rows.first {
                // Tier 3: sliding window over game logs
                let pidIdx = columns.firstIndex(of: "player_id") ?? 1
                let sIdx = columns.firstIndex(of: "season") ?? 2
                if pidIdx < firstRow.count, sIdx < firstRow.count,
                   let windowText = findSlidingWindowStreaks(playerId: firstRow[pidIdx], season: firstRow[sIdx]) {
                    streakData += "\n\n" + windowText
                }
            }
        }

        let stream = anthropic.describeStreaks(
            question: question, streakData: streakData, history: history
        )
        return try await collectStream(stream, onChunk: onChunk)
    }

    // MARK: - Streak fallback handling

    private func handleStreakFallback(
        sql: String,
        question: String,
        onChunk: @escaping @MainActor (String) -> Void
    ) async throws -> String? {
        let allStreaks = getAllStreaksForQuery(sql: sql)
        guard !allStreaks.rows.isEmpty else { return nil }

        var streakData = formatTable(columns: allStreaks.columns, rows: allStreaks.rows)

        if allStreaks.rows.count == 1 {
            if let fallbackText = findSensitiveStreaks(rows: allStreaks.rows, columns: allStreaks.columns) {
                streakData += "\n\n" + fallbackText
            } else if let firstRow = allStreaks.rows.first {
                // Tier 3: sliding window over game logs
                let pidIdx = allStreaks.columns.firstIndex(of: "player_id") ?? 1
                let sIdx = allStreaks.columns.firstIndex(of: "season") ?? 2
                if pidIdx < firstRow.count, sIdx < firstRow.count,
                   let windowText = findSlidingWindowStreaks(playerId: firstRow[pidIdx], season: firstRow[sIdx]) {
                    streakData += "\n\n" + windowText
                }
            }
        }

        let stream = anthropic.describeStreaks(
            question: question, streakData: streakData, history: history
        )
        return try await collectStream(stream, onChunk: onChunk)
    }

    /// Extract player name and season from SQL, query all streaks without performance filter.
    private func getAllStreaksForQuery(sql: String) -> DatabaseService.QueryResult {
        guard let nameRange = sql.range(of: #"LIKE\s+'%([^%]+)%'"#, options: .regularExpression),
              let innerRange = sql[nameRange].range(of: #"'%([^%]+)%'"#, options: .regularExpression) else {
            return DatabaseService.QueryResult(columns: [], rows: [])
        }
        let nameSlice = sql[innerRange]
        let playerName = String(nameSlice)
            .replacingOccurrences(of: "'%", with: "")
            .replacingOccurrences(of: "%'", with: "")

        let seasonPattern = #"season\s*=\s*(\d{4})"#
        var season = "2024"
        if let seasonRange = sql.range(of: seasonPattern, options: .regularExpression) {
            let match = sql[seasonRange]
            if let digitRange = match.range(of: #"\d{4}"#, options: .regularExpression) {
                season = String(match[digitRange])
            }
        }

        let fallbackSQL = """
            SELECT s.* FROM streaks s
            JOIN players p ON s.player_id = p.player_id
            WHERE p.name LIKE '%\(playerName)%' AND s.season = \(season)
            ORDER BY s.start_date
            """

        do {
            return try database.execute(sql: fallbackSQL)
        } catch {
            return DatabaseService.QueryResult(columns: [], rows: [])
        }
    }

    /// Query precomputed sensitive streaks (Tier 2) for a player-season.
    private func findSensitiveStreaks(rows: [[String]], columns: [String]) -> String? {
        guard let firstRow = rows.first else { return nil }

        let playerIdIdx = columns.firstIndex(of: "player_id") ?? 1
        let seasonIdx = columns.firstIndex(of: "season") ?? 2

        guard playerIdIdx < firstRow.count, seasonIdx < firstRow.count else { return nil }
        let playerId = firstRow[playerIdIdx]
        let season = firstRow[seasonIdx]

        let sql = """
            SELECT * FROM streaks_sensitive
            WHERE player_id = '\(playerId)' AND season = \(season)
            ORDER BY ops DESC
            """

        guard let result = try? database.execute(sql: sql), !result.rows.isEmpty else {
            return nil
        }

        let seasonOpsIdx = result.columns.firstIndex(of: "season_ops") ?? (result.columns.count - 1)
        let seasonOps = result.rows.first.flatMap { seasonOpsIdx < $0.count ? $0[seasonOpsIdx] : nil } ?? "N/A"

        let opsIdx = result.columns.firstIndex(of: "ops") ?? 9
        let startDateIdx = result.columns.firstIndex(of: "start_date") ?? 3
        let endDateIdx = result.columns.firstIndex(of: "end_date") ?? 4
        let numGamesIdx = result.columns.firstIndex(of: "num_games") ?? 5
        let avgIdx = result.columns.firstIndex(of: "batting_avg") ?? 6
        let obpIdx = result.columns.firstIndex(of: "obp") ?? 7
        let slgIdx = result.columns.firstIndex(of: "slg") ?? 8
        let hrIdx = result.columns.firstIndex(of: "home_runs") ?? 10
        let hitsIdx = result.columns.firstIndex(of: "hits") ?? 11
        let abIdx = result.columns.firstIndex(of: "at_bats") ?? 12

        let sorted = result.rows.sorted { a, b in
            (Double(a[opsIdx]) ?? 0) > (Double(b[opsIdx]) ?? 0)
        }
        let hottest = sorted.first!
        let coldest = sorted.last!

        var lines = ["SENSITIVE STREAK FALLBACK (lower-threshold change-point detection, 7-30 game segments):"]
        lines.append("Player season OPS: \(seasonOps)")
        lines.append(
            "Hottest segment: \(hottest[startDateIdx]) to \(hottest[endDateIdx]) (\(hottest[numGamesIdx]) games) — " +
            "\(hottest[avgIdx])/\(hottest[obpIdx])/\(hottest[slgIdx]) (\(hottest[opsIdx]) OPS), " +
            "\(hottest[hrIdx]) HR, \(hottest[hitsIdx]) H in \(hottest[abIdx]) AB"
        )
        if hottest != coldest, sorted.count > 1 {
            lines.append(
                "Coldest segment: \(coldest[startDateIdx]) to \(coldest[endDateIdx]) (\(coldest[numGamesIdx]) games) — " +
                "\(coldest[avgIdx])/\(coldest[obpIdx])/\(coldest[slgIdx]) (\(coldest[opsIdx]) OPS), " +
                "\(coldest[hrIdx]) HR, \(coldest[hitsIdx]) H in \(coldest[abIdx]) AB"
            )
        }
        return lines.joined(separator: "\n")
    }

    /// Tier 3 fallback: sliding window over game logs to find best/worst stretch (7-30 games).
    /// Used when both precomputed streaks and sensitive streaks return nothing.
    private func findSlidingWindowStreaks(playerId: String, season: String) -> String? {
        let sql = """
            SELECT date, at_bats, hits, home_runs, walks, strikeouts,
                   doubles, triples, obp, slg
            FROM game_batting_logs
            WHERE player_id = '\(playerId)' AND season = \(season)
            ORDER BY date
            """
        guard let result = try? database.execute(sql: sql, maxRows: 0),
              result.rows.count >= 7 else { return nil }

        let games = result.rows

        // Compute season OPS from all game logs
        var totalAB = 0, totalH = 0, totalBB = 0, totalHBP = 0, totalSF = 0
        var totalDoubles = 0, totalTriples = 0, totalHR = 0
        for g in games {
            let ab = Int(g[1]) ?? 0
            let h = Int(g[2]) ?? 0
            let hr = Int(g[3]) ?? 0
            let bb = Int(g[4]) ?? 0
            let doubles = Int(g[6]) ?? 0
            let triples = Int(g[7]) ?? 0
            totalAB += ab; totalH += h; totalHR += hr; totalBB += bb
            totalDoubles += doubles; totalTriples += triples
        }
        let seasonOBP = totalAB + totalBB > 0
            ? Double(totalH + totalBB) / Double(totalAB + totalBB) : 0
        let seasonSLG = totalAB > 0
            ? Double(totalH - totalDoubles - totalTriples - totalHR + 2 * totalDoubles + 3 * totalTriples + 4 * totalHR) / Double(totalAB) : 0
        let seasonOPS = seasonOBP + seasonSLG

        struct Window {
            let startIdx: Int
            let endIdx: Int
            let ops: Double
            let ab: Int
            let hits: Int
            let hr: Int
            let bb: Int
            let so: Int
            let avg: Double
            let obp: Double
            let slg: Double
        }

        var best: Window?
        var worst: Window?

        for windowSize in 7...min(30, games.count) {
            for start in 0...(games.count - windowSize) {
                let end = start + windowSize - 1
                var wAB = 0, wH = 0, wHR = 0, wBB = 0, wSO = 0
                var wDoubles = 0, wTriples = 0
                for i in start...end {
                    let g = games[i]
                    wAB += Int(g[1]) ?? 0
                    wH += Int(g[2]) ?? 0
                    wHR += Int(g[3]) ?? 0
                    wBB += Int(g[4]) ?? 0
                    wSO += Int(g[5]) ?? 0
                    wDoubles += Int(g[6]) ?? 0
                    wTriples += Int(g[7]) ?? 0
                }
                guard wAB > 0 else { continue }
                let wOBP = Double(wH + wBB) / Double(wAB + wBB)
                let wSLG = Double(wH - wDoubles - wTriples - wHR + 2 * wDoubles + 3 * wTriples + 4 * wHR) / Double(wAB)
                let wOPS = wOBP + wSLG
                let wAVG = Double(wH) / Double(wAB)

                let w = Window(startIdx: start, endIdx: end, ops: wOPS,
                               ab: wAB, hits: wH, hr: wHR, bb: wBB, so: wSO,
                               avg: wAVG, obp: wOBP, slg: wSLG)

                if best == nil || wOPS > best!.ops { best = w }
                if worst == nil || wOPS < worst!.ops { worst = w }
            }
        }

        guard let hottest = best, let coldest = worst else { return nil }

        // Only report if there's meaningful deviation from season average
        let hotDelta = hottest.ops - seasonOPS
        let coldDelta = seasonOPS - coldest.ops
        guard hotDelta > 0.05 || coldDelta > 0.05 else { return nil }

        func fmt(_ v: Double, _ places: Int = 3) -> String {
            String(format: "%.\(places)f", v)
        }

        var lines = ["SLIDING WINDOW ANALYSIS (best/worst 7-30 game stretches from game logs):"]
        lines.append("Player season OPS: \(fmt(seasonOPS))")

        if hotDelta > 0.05 {
            let startDate = games[hottest.startIdx][0]
            let endDate = games[hottest.endIdx][0]
            let numGames = hottest.endIdx - hottest.startIdx + 1
            lines.append(
                "Hottest stretch: \(startDate) to \(endDate) (\(numGames) games) — " +
                "\(fmt(hottest.avg))/\(fmt(hottest.obp))/\(fmt(hottest.slg)) (\(fmt(hottest.ops)) OPS), " +
                "\(hottest.hr) HR, \(hottest.hits) H in \(hottest.ab) AB"
            )
        }
        if coldDelta > 0.05 {
            let startDate = games[coldest.startIdx][0]
            let endDate = games[coldest.endIdx][0]
            let numGames = coldest.endIdx - coldest.startIdx + 1
            lines.append(
                "Coldest stretch: \(startDate) to \(endDate) (\(numGames) games) — " +
                "\(fmt(coldest.avg))/\(fmt(coldest.obp))/\(fmt(coldest.slg)) (\(fmt(coldest.ops)) OPS), " +
                "\(coldest.hr) HR, \(coldest.hits) H in \(coldest.ab) AB"
            )
        }

        return lines.count > 2 ? lines.joined(separator: "\n") : nil
    }

    // MARK: - Helpers

    private func formatTable(columns: [String], rows: [[String]]) -> String {
        let header = columns.joined(separator: " | ")
        var lines = [header, String(repeating: "-", count: header.count)]
        for row in rows {
            lines.append(row.joined(separator: " | "))
        }
        return lines.joined(separator: "\n")
    }

    private func collectStream(
        _ stream: AsyncThrowingStream<String, Error>,
        onChunk: @escaping @MainActor (String) -> Void
    ) async throws -> String {
        var fullText = ""
        for try await chunk in stream {
            onChunk(chunk)
            fullText += chunk
        }
        return fullText
    }

    private func addToHistory(question: String, answer: String) {
        history.append((question, answer))
        if history.count > maxHistory {
            history = Array(history.suffix(maxHistory))
        }
    }

    /// Inject a Q&A pair into history (for locally-handled queries like comparisons).
    /// Strips internal UI tags so Claude sees clean natural language context.
    func injectHistory(question: String, answer: String) {
        addToHistory(question: question, answer: sanitizeForHistory(answer))
    }

    /// Strip internal rendering tags from intercepted responses so Claude gets clean context.
    private func sanitizeForHistory(_ text: String) -> String {
        var lines = text.components(separatedBy: "\n")

        lines = lines.compactMap { line in
            let trimmed = line.trimmingCharacters(in: .whitespaces)

            // Remove SUGGEST pills, TIP blocks, LEADERBOARD delimiters, HEADER lines, FORM metadata
            if trimmed.hasPrefix("[SUGGEST]") { return nil }
            if trimmed.hasPrefix("[TIP]") { return nil }
            if trimmed == "[LEADERBOARD]" || trimmed == "[/LEADERBOARD]" { return nil }
            if trimmed.hasPrefix("HEADER:") { return nil }
            if trimmed.hasPrefix("FORM:") { return nil }

            // Convert "ROW 1. Name: Value" → "1. Name: Value"
            if trimmed.hasPrefix("ROW ") {
                return String(trimmed.dropFirst(4))
            }

            return line
        }

        // Collapse runs of blank lines
        var result: [String] = []
        for line in lines {
            if line.trimmingCharacters(in: .whitespaces).isEmpty && result.last?.trimmingCharacters(in: .whitespaces).isEmpty == true {
                continue
            }
            result.append(line)
        }

        return result.joined(separator: "\n").trimmingCharacters(in: .whitespacesAndNewlines)
    }

    func clearHistory() {
        history.removeAll()
    }

    // MARK: - Local player name extraction

    /// Extract a player name from a question using PlayerNameMatcher (no API call).
    private func extractPlayerNameLocally(from question: String) -> String? {
        let lower = question.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()

        // Try full name match first (longest names first, word-boundary aware)
        for name in PlayerNameMatcher.sortedNames {
            if PlayerNameMatcher.containsWord(name.lowercased(), in: lower) {
                return name
            }
        }

        // Try unambiguous last name match
        for (lastName, players) in PlayerNameMatcher.lastNameIndex {
            if PlayerNameMatcher.containsWord(lastName, in: lower) && players.count == 1 {
                return players[0]
            }
        }

        return nil
    }
}
