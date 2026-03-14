import Foundation

struct SuggestionConfig: Codable, Sendable {
    let version: Int
    let algorithm: Algorithm
    let defaults: [DefaultSuggestion]
    let templates: Templates
    let dynamicQueries: DynamicQueries

    struct Algorithm: Codable, Sendable {
        let impressionThreshold: Int
        let poolSize: Int
        let dynamicSlots: Int
        let personalizedSlots: Int
        let defaultSlots: Int
        let displayDuration: Double
        let fadeDuration: Double
    }

    struct DefaultSuggestion: Codable, Sendable {
        let id: String
        let text: String
        let weight: Double
    }

    struct Templates: Codable, Sendable {
        let current: CategoryTemplates
        let historical: CategoryTemplates
    }

    struct CategoryTemplates: Codable, Sendable {
        let streak: [String]
        let comparison: [String]
        let splits: [String]
        let homeAway: [String]
        let playerLookup: [String]
    }

    struct DynamicQueries: Codable, Sendable {
        let batting: [DynamicQuery]
        let pitching: [DynamicQuery]
    }

    struct DynamicQuery: Codable, Sendable {
        let label: String
        let sql: String
        let templates: [String]
    }
}
