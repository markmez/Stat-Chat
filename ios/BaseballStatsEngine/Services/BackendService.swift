import Foundation

final class BackendService: Sendable {
    private let baseURL = URL(string: "https://stat-chat-production.up.railway.app")!

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

    /// Stream an answer from the backend. Calls `onChunk` for each text token.
    /// Returns the full assembled answer.
    func ask(
        question: String,
        deviceId: String,
        history: [(String, String)],
        onChunk: @escaping @MainActor @Sendable (String) -> Void
    ) async throws -> String {
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
                break
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

        return fullText
    }

    // MARK: - Player Card

    /// Structured JSON response from /player-card endpoint.
    struct PlayerCardData: Decodable, Sendable {
        let player_info: PlayerInfoData?
        let batting_seasons: [BattingSeasonData]
        let pitching_seasons: [PitchingSeasonData]
        let is_pitcher: Bool
        let is_two_way: Bool
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
        let G, AB, R, H, doubles, triples, HR, RBI, SB, CS, BB, IBB, SO, HBP: Int
        let AVG, OBP, SLG, OPS, OPS_plus, ISO, BABIP: String
    }

    struct PitchingSeasonData: Decodable, Sendable {
        let year: Int
        let team: String
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

        let (data, response) = try await URLSession.shared.data(from: url)
        if let http = response as? HTTPURLResponse, http.statusCode != 200 {
            let body = String(data: data, encoding: .utf8) ?? "Unknown error"
            throw ServiceError.httpError(http.statusCode, body)
        }
        return try JSONDecoder().decode(PlayerCardData.self, from: data)
    }
}
