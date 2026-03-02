import Foundation

final class AnthropicService: Sendable {
    private let model = "claude-sonnet-4-5-20250929"
    private let routingModel = "claude-haiku-4-5-20251001"
    private let apiURL = URL(string: "https://api.anthropic.com/v1/messages")!
    private let apiVersion = "2023-06-01"
    private let cacheBetaHeader = "prompt-caching-2024-07-31"

    var apiKey: String? {
        KeychainHelper.load()
    }

    enum ServiceError: LocalizedError {
        case noAPIKey
        case httpError(Int, String)
        case decodingError(String)

        var errorDescription: String? {
            switch self {
            case .noAPIKey: return "No API key configured. Tap the gear icon to add your Anthropic API key."
            case .httpError(let code, let msg): return "API error (\(code)): \(msg)"
            case .decodingError(let msg): return "Failed to parse response: \(msg)"
            }
        }
    }

    // MARK: - Codable request types (deterministic JSON key ordering)

    private struct APIRequest: Encodable {
        let model: String
        let max_tokens: Int
        let system: SystemContent
        let messages: [APIMessage]
        let stream: Bool
    }

    private enum SystemContent: Encodable {
        case plain(String)
        case cached([CachedBlock])

        func encode(to encoder: Encoder) throws {
            var container = encoder.singleValueContainer()
            switch self {
            case .plain(let text):
                try container.encode(text)
            case .cached(let blocks):
                try container.encode(blocks)
            }
        }
    }

    private struct CachedBlock: Encodable {
        let type: String
        let text: String
        let cache_control: CacheControl
    }

    private struct CacheControl: Encodable {
        let type: String
    }

    private struct APIMessage: Encodable {
        let role: String
        let content: String
    }

    // MARK: - Non-streaming calls

    func routeQuery(question: String, history: [(String, String)]) async throws -> String {
        let response = try await callAPI(
            system: .plain(PromptStore.routerPrompt),
            messages: buildMessages(question: question, history: history),
            maxTokens: 256,
            stream: false,
            model: routingModel,
            useCache: false
        )
        return parseNonStreamingResponse(response)
    }

    func generateSQL(question: String, history: [(String, String)]) async throws -> String {
        let response = try await callAPI(
            system: .cached([CachedBlock(
                type: "text",
                text: PromptStore.sqlGenerationPrompt,
                cache_control: CacheControl(type: "ephemeral")
            )]),
            messages: buildMessages(question: question, history: history),
            maxTokens: 1024,
            stream: false,
            model: model,
            useCache: true
        )
        let sql = parseNonStreamingResponse(response)
        return SQLSanitizer.sanitize(sql)
    }

    // MARK: - Streaming calls

    func generateAnswer(
        question: String, sql: String, results: String,
        history: [(String, String)]
    ) -> AsyncThrowingStream<String, Error> {
        let content = "Question: \(question)\n\nSQL executed: \(sql)\n\nResults:\n\(results)"
        return streamAPI(
            system: .cached([CachedBlock(
                type: "text",
                text: PromptStore.answerGenerationPrompt,
                cache_control: CacheControl(type: "ephemeral")
            )]),
            messages: buildMessages(content: content, history: history),
            maxTokens: 1024,
            useCache: true
        )
    }

    func describeStreaks(
        question: String, streakData: String,
        history: [(String, String)]
    ) -> AsyncThrowingStream<String, Error> {
        let content = "Question: \(question)\n\nStreak data:\n\(streakData)"
        return streamAPI(
            system: .plain(PromptStore.streakAnswerPrompt),
            messages: buildMessages(content: content, history: history),
            maxTokens: 1024,
            useCache: false
        )
    }

    func describeCurrentForm(
        question: String, formData: String,
        history: [(String, String)]
    ) -> AsyncThrowingStream<String, Error> {
        let content = "Question: \(question)\n\nCurrent form data:\n\(formData)"
        return streamAPI(
            system: .plain(PromptStore.currentFormAnswerPrompt),
            messages: buildMessages(content: content, history: history),
            maxTokens: 1024,
            useCache: false
        )
    }

    func explainStat(
        question: String,
        history: [(String, String)]
    ) -> AsyncThrowingStream<String, Error> {
        return streamAPI(
            system: .plain(PromptStore.statExplanationPrompt),
            messages: buildMessages(question: question, history: history),
            maxTokens: 512,
            useCache: false
        )
    }

    // MARK: - Message building

    private func buildMessages(question: String, history: [(String, String)]) -> [APIMessage] {
        var messages: [APIMessage] = []
        for (prevQ, prevA) in history {
            messages.append(APIMessage(role: "user", content: prevQ))
            messages.append(APIMessage(role: "assistant", content: prevA))
        }
        messages.append(APIMessage(role: "user", content: question))
        return messages
    }

    private func buildMessages(content: String, history: [(String, String)]) -> [APIMessage] {
        var messages: [APIMessage] = []
        for (prevQ, prevA) in history {
            messages.append(APIMessage(role: "user", content: prevQ))
            messages.append(APIMessage(role: "assistant", content: prevA))
        }
        messages.append(APIMessage(role: "user", content: content))
        return messages
    }

    // MARK: - Non-streaming HTTP

    private func callAPI(
        system: SystemContent,
        messages: [APIMessage],
        maxTokens: Int,
        stream: Bool,
        model: String,
        useCache: Bool
    ) async throws -> Data {
        guard let key = apiKey else { throw ServiceError.noAPIKey }

        var request = URLRequest(url: apiURL)
        request.httpMethod = "POST"
        request.setValue(key, forHTTPHeaderField: "x-api-key")
        request.setValue(apiVersion, forHTTPHeaderField: "anthropic-version")
        request.setValue("application/json", forHTTPHeaderField: "content-type")
        if useCache {
            request.setValue(cacheBetaHeader, forHTTPHeaderField: "anthropic-beta")
        }

        let body = APIRequest(
            model: model,
            max_tokens: maxTokens,
            system: system,
            messages: messages,
            stream: stream
        )
        let encoder = JSONEncoder()
        encoder.outputFormatting = .sortedKeys
        request.httpBody = try encoder.encode(body)

        let (data, response) = try await URLSession.shared.data(for: request)
        if let http = response as? HTTPURLResponse, http.statusCode != 200 {
            let errorBody = String(data: data, encoding: .utf8) ?? "Unknown error"
            throw ServiceError.httpError(http.statusCode, errorBody)
        }
        return data
    }

    private func parseNonStreamingResponse(_ data: Data) -> String {
        guard let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let content = json["content"] as? [[String: Any]],
              let first = content.first,
              let text = first["text"] as? String else {
            return ""
        }
        return text.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    // MARK: - Streaming HTTP (SSE)

    private func streamAPI(
        system: SystemContent,
        messages: [APIMessage],
        maxTokens: Int,
        useCache: Bool
    ) -> AsyncThrowingStream<String, Error> {
        AsyncThrowingStream { continuation in
            Task {
                do {
                    guard let key = self.apiKey else {
                        continuation.finish(throwing: ServiceError.noAPIKey)
                        return
                    }

                    var request = URLRequest(url: self.apiURL)
                    request.httpMethod = "POST"
                    request.setValue(key, forHTTPHeaderField: "x-api-key")
                    request.setValue(self.apiVersion, forHTTPHeaderField: "anthropic-version")
                    request.setValue("application/json", forHTTPHeaderField: "content-type")
                    if useCache {
                        request.setValue(self.cacheBetaHeader, forHTTPHeaderField: "anthropic-beta")
                    }

                    let body = APIRequest(
                        model: self.model,
                        max_tokens: maxTokens,
                        system: system,
                        messages: messages,
                        stream: true
                    )
                    let encoder = JSONEncoder()
                    encoder.outputFormatting = .sortedKeys
                    request.httpBody = try encoder.encode(body)

                    let (bytes, response) = try await URLSession.shared.bytes(for: request)
                    if let http = response as? HTTPURLResponse, http.statusCode != 200 {
                        var errorData = Data()
                        for try await byte in bytes {
                            errorData.append(byte)
                        }
                        let errorBody = String(data: errorData, encoding: .utf8) ?? "Unknown error"
                        continuation.finish(throwing: ServiceError.httpError(http.statusCode, errorBody))
                        return
                    }

                    for try await line in bytes.lines {
                        guard line.hasPrefix("data: ") else { continue }
                        let jsonStr = String(line.dropFirst(6))
                        guard jsonStr != "[DONE]" else { break }

                        guard let data = jsonStr.data(using: .utf8),
                              let event = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                              let type = event["type"] as? String else { continue }

                        if type == "content_block_delta",
                           let delta = event["delta"] as? [String: Any],
                           let text = delta["text"] as? String {
                            continuation.yield(text)
                        }
                    }
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
        }
    }
}
