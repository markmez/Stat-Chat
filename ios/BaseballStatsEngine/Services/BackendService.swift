import Foundation

final class BackendService: Sendable {
    private let baseURL = URL(string: "https://api.secondsignalapps.com")!

    enum ServiceError: LocalizedError {
        case httpError(Int, String)
        case serverError(String)
        case quotaExceeded(count: Int, reset: String)

        var errorDescription: String? {
            switch self {
            case .httpError(let code, let msg):
                return "Server error (\(code)): \(msg)"
            case .serverError(let msg):
                return msg
            case .quotaExceeded(let count, let reset):
                return "You've used all \(count) free queries this week. Resets \(reset)."
            }
        }
    }

    /// Result from a backend query — includes the answer text and whether it was intercepted locally.
    struct QueryResult: Sendable {
        let text: String
        let intercepted: Bool
        /// For follow-up queries: the standalone rewritten query from Haiku (if the backend rewrote it).
        let rewrittenQuery: String?
    }

    /// Stream an answer from the backend. Calls `onChunk` for each text token.
    /// Returns the full assembled answer and whether the backend intercepted it locally.
    func ask(
        question: String,
        deviceId: String,
        history: [(String, String)],
        onChunk: @escaping @MainActor @Sendable (String) -> Void
    ) async throws -> QueryResult {
        let url = baseURL.appendingPathComponent("query")

        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")

        let historyPayload = history.flatMap { q, a in
            [["role": "user", "content": q],
             ["role": "assistant", "content": a]]
        }

        let body: [String: Any] = [
            "question": question,
            "device_id": deviceId,
            "history": historyPayload,
        ]
        request.httpBody = try JSONSerialization.data(withJSONObject: body)

        let (bytes, response) = try await URLSession.shared.bytes(for: request)
        if let http = response as? HTTPURLResponse, http.statusCode != 200 {
            var errorData = Data()
            for try await byte in bytes {
                errorData.append(byte)
            }
            let errorBody = String(data: errorData, encoding: .utf8) ?? "Unknown error"
            throw ServiceError.httpError(http.statusCode, errorBody)
        }

        var fullText = ""
        var wasIntercepted = false
        var rewrittenQuery: String?

        for try await line in bytes.lines {
            guard line.hasPrefix("data: ") else { continue }
            let jsonStr = String(line.dropFirst(6))

            guard let data = jsonStr.data(using: .utf8),
                  let event = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let type = event["type"] as? String else { continue }

            switch type {
            case "text":
                if let text = event["text"] as? String {
                    await onChunk(text)
                    fullText += text
                }
            case "done":
                wasIntercepted = event["intercepted"] as? Bool ?? false
                rewrittenQuery = event["rewritten_query"] as? String
            case "error":
                let message = event["message"] as? String ?? "Unknown server error"
                throw ServiceError.serverError(message)
            case "quota_exceeded":
                let count = event["count"] as? Int ?? 5
                let reset = event["reset"] as? String ?? "next week"
                throw ServiceError.quotaExceeded(count: count, reset: reset)
            default:
                break
            }
        }

        return QueryResult(text: fullText, intercepted: wasIntercepted, rewrittenQuery: rewrittenQuery)
    }

    // MARK: - Player Card

    /// Structured JSON response from /player-card endpoint.
    struct SplitRowData: Decodable, Sendable {
        let label: String
        let values: [String]
    }

    struct SplitGridData: Decodable, Sendable {
        let headers: [String]
        let rows: [SplitRowData]
    }

    struct SeasonSplitsData: Decodable, Sendable {
        let year: Int
        let platoon: SplitGridData?
        let home_away: SplitGridData?
        let risp: SplitGridData?
        let pitch_type: [SplitGridData]?
        let count: [SplitGridData]?
        let streaks: SplitGridData?
        let fielding: SplitGridData?
    }

    struct PitchingSeasonSplitsData: Decodable, Sendable {
        let year: Int
        let platoon: SplitGridData?
        let home_away: SplitGridData?
        let risp: SplitGridData?
        let pitch_type: [SplitGridData]?
        let count: [SplitGridData]?
        let streaks: SplitGridData?
    }

    struct CurrentFormData: Decodable, Sendable {
        let form_start_date: String
        let form_start_game_number: Int
        let total_season_games: Int
        let num_games: Int
        let stats: SplitGridData
        let counting_values: [String: Double]
        let season_counting_values: [String: Double]
    }

    struct PitchingCurrentFormData: Decodable, Sendable {
        let form_start_date: String
        let form_start_game_number: Int
        let total_season_games: Int
        let num_games: Int
        let role: String
        let stats: SplitGridData
        let counting_values: [String: Double]
        let season_counting_values: [String: Double]
    }

    struct PlayerCardData: Decodable, Sendable {
        let player_info: PlayerInfoData?
        let batting_seasons: [BattingSeasonData]
        let pitching_seasons: [PitchingSeasonData]
        let is_pitcher: Bool
        let is_two_way: Bool
        let career_platoon_splits: SplitGridData?
        let career_home_away_splits: SplitGridData?
        let pitching_career_platoon_splits: SplitGridData?
        let pitching_career_home_away_splits: SplitGridData?
        let season_splits: [SeasonSplitsData]?
        let pitching_season_splits: [PitchingSeasonSplitsData]?
        let current_form: CurrentFormData?
        let pitching_current_form: PitchingCurrentFormData?
    }

    struct PlayerInfoData: Decodable, Sendable {
        let name: String
        let team: String
        let birthdate: String?
        let bats: String?
        let `throws`: String?
        let positions: String?
    }

    struct BattingSeasonData: Decodable, Sendable {
        let year: Int
        let team: String
        let age: Int
        let team_games: Int?
        let G, AB, R, H, doubles, triples, HR, RBI, SB, CS, BB, IBB, SO, HBP: Int
        let AVG, OBP, SLG, OPS, OPS_plus, ISO, BABIP: String
    }

    struct PitchingSeasonData: Decodable, Sendable {
        let year: Int
        let team: String
        let team_games: Int?
        let W, L, SV, G, GS, GF, CG, QS: Int
        let IP: String
        let H, R, ER, HR, BB, IBB, SO, HBP, WP, BK, BF, SH, SF, SB_allowed, CS_allowed: Int
        let ERA, WHIP, K9, BB9, K_BB, H9, HR9, BAA, ERA_plus: String
    }

    /// Fetch structured player card data from the backend.
    func fetchPlayerCard(name: String) async throws -> PlayerCardData {
        var components = URLComponents(url: baseURL.appendingPathComponent("player-card"), resolvingAgainstBaseURL: false)!
        components.queryItems = [URLQueryItem(name: "name", value: name)]
        let url = components.url!

        var request = URLRequest(url: url)
        request.timeoutInterval = 15  // Longer timeout — endpoint now returns per-season splits
        let (data, response) = try await URLSession.shared.data(for: request)
        if let http = response as? HTTPURLResponse, http.statusCode != 200 {
            let body = String(data: data, encoding: .utf8) ?? "Unknown error"
            throw ServiceError.httpError(http.statusCode, body)
        }
        return try JSONDecoder().decode(PlayerCardData.self, from: data)
    }

    // MARK: - Game logs (for current form slider)

    struct GameLogData: Decodable, Sendable {
        let date: String
        let at_bats: Int
        let hits: Int
        let doubles: Int
        let triples: Int
        let home_runs: Int
        let runs: Int
        let rbi: Int
        let walks: Int
        let strikeouts: Int
        let plate_appearances: Int
    }

    struct PitchingGameLogData: Decodable, Sendable {
        let date: String
        let ip_outs: Int
        let hits: Int
        let earned_runs: Int
        let walks: Int
        let strikeouts: Int
        let home_runs: Int
        let is_start: Bool
    }

    /// Fetch batting game logs for a player-season (for slider recomputation).
    func fetchBattingGameLogs(name: String, season: Int) async throws -> [GameLogData] {
        var components = URLComponents(url: baseURL.appendingPathComponent("player-card/game-logs"), resolvingAgainstBaseURL: false)!
        components.queryItems = [
            URLQueryItem(name: "name", value: name),
            URLQueryItem(name: "season", value: "\(season)"),
            URLQueryItem(name: "type", value: "batting"),
        ]
        let (data, response) = try await URLSession.shared.data(from: components.url!)
        if let http = response as? HTTPURLResponse, http.statusCode != 200 {
            throw ServiceError.httpError(http.statusCode, String(data: data, encoding: .utf8) ?? "")
        }
        return try JSONDecoder().decode([GameLogData].self, from: data)
    }

    /// Fetch pitching game logs for a player-season (for slider recomputation).
    func fetchPitchingGameLogs(name: String, season: Int) async throws -> [PitchingGameLogData] {
        var components = URLComponents(url: baseURL.appendingPathComponent("player-card/game-logs"), resolvingAgainstBaseURL: false)!
        components.queryItems = [
            URLQueryItem(name: "name", value: name),
            URLQueryItem(name: "season", value: "\(season)"),
            URLQueryItem(name: "type", value: "pitching"),
        ]
        let (data, response) = try await URLSession.shared.data(from: components.url!)
        if let http = response as? HTTPURLResponse, http.statusCode != 200 {
            throw ServiceError.httpError(http.statusCode, String(data: data, encoding: .utf8) ?? "")
        }
        return try JSONDecoder().decode([PitchingGameLogData].self, from: data)
    }

    // MARK: - Stats endpoints (leaderboards, thresholds, milestones)

    struct LeaderboardRow: Decodable, Sendable {
        let rank: Int
        let name: String
        let value: String
        let season: Int?
    }

    struct LeaderboardResponse: Decodable, Sendable {
        let title: String
        let stat: String
        let rows: [LeaderboardRow]
        let count: Int
        let pa_min: Int?
    }

    struct MilestoneResponse: Decodable, Sendable {
        let title: String
        let stat: String
        let count: Int
        let rows: [LeaderboardRow]
    }

    /// Fetch a leaderboard from the backend (for pre-2016 or full historical queries).
    func fetchLeaderboard(stat: String, season: Int?, scope: String, limit: Int, isPitching: Bool) async throws -> LeaderboardResponse {
        var components = URLComponents(url: baseURL.appendingPathComponent("stats/leaderboard"), resolvingAgainstBaseURL: false)!
        var items = [
            URLQueryItem(name: "stat", value: stat),
            URLQueryItem(name: "scope", value: scope),
            URLQueryItem(name: "limit", value: "\(limit)"),
            URLQueryItem(name: "is_pitching", value: isPitching ? "true" : "false"),
        ]
        if let season {
            items.append(URLQueryItem(name: "season", value: "\(season)"))
        }
        components.queryItems = items
        let url = components.url!

        let (data, response) = try await URLSession.shared.data(from: url)
        if let http = response as? HTTPURLResponse, http.statusCode != 200 {
            let body = String(data: data, encoding: .utf8) ?? "Unknown error"
            throw ServiceError.httpError(http.statusCode, body)
        }
        return try JSONDecoder().decode(LeaderboardResponse.self, from: data)
    }

    /// Fetch a threshold leaderboard from the backend.
    func fetchThreshold(stat: String, value: Double, comparison: String, season: Int, isPitching: Bool, limit: Int) async throws -> LeaderboardResponse {
        var components = URLComponents(url: baseURL.appendingPathComponent("stats/threshold"), resolvingAgainstBaseURL: false)!
        components.queryItems = [
            URLQueryItem(name: "stat", value: stat),
            URLQueryItem(name: "value", value: "\(value)"),
            URLQueryItem(name: "comparison", value: comparison),
            URLQueryItem(name: "season", value: "\(season)"),
            URLQueryItem(name: "is_pitching", value: isPitching ? "true" : "false"),
            URLQueryItem(name: "limit", value: "\(limit)"),
        ]
        let url = components.url!

        let (data, response) = try await URLSession.shared.data(from: url)
        if let http = response as? HTTPURLResponse, http.statusCode != 200 {
            let body = String(data: data, encoding: .utf8) ?? "Unknown error"
            throw ServiceError.httpError(http.statusCode, body)
        }
        return try JSONDecoder().decode(LeaderboardResponse.self, from: data)
    }

    /// Fetch a milestone query from the backend.
    func fetchMilestone(stat: String, value: Double, since: Int?, isPitching: Bool, limit: Int) async throws -> MilestoneResponse {
        var components = URLComponents(url: baseURL.appendingPathComponent("stats/milestone"), resolvingAgainstBaseURL: false)!
        var items = [
            URLQueryItem(name: "stat", value: stat),
            URLQueryItem(name: "value", value: "\(value)"),
            URLQueryItem(name: "is_pitching", value: isPitching ? "true" : "false"),
            URLQueryItem(name: "limit", value: "\(limit)"),
        ]
        if let since {
            items.append(URLQueryItem(name: "since", value: "\(since)"))
        }
        components.queryItems = items
        let url = components.url!

        let (data, response) = try await URLSession.shared.data(from: url)
        if let http = response as? HTTPURLResponse, http.statusCode != 200 {
            let body = String(data: data, encoding: .utf8) ?? "Unknown error"
            throw ServiceError.httpError(http.statusCode, body)
        }
        return try JSONDecoder().decode(MilestoneResponse.self, from: data)
    }
}
