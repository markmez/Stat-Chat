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
        // Step 0: Route the query
        let routeJSON = try await anthropic.routeQuery(question: question, history: history)

        let fullAnswer: String
        if routeJSON.contains("stat_explanation") {
            let stream = anthropic.explainStat(question: question, history: history)
            fullAnswer = try await collectStream(stream, onChunk: onChunk)
        } else if routeJSON.contains("streak_finder") {
            fullAnswer = try await handleStreakQuery(question: question, onChunk: onChunk)
        } else {
            fullAnswer = try await handleSQLQuery(question: question, onChunk: onChunk)
        }
        addToHistory(question: question, answer: fullAnswer)
        return fullAnswer
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

    func clearHistory() {
        history.removeAll()
    }
}
