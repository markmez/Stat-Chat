import Foundation

struct Message: Identifiable {
    let id = UUID()
    let role: Role
    let content: String
    let timestamp = Date()
    /// Pre-computed text with player/team links — avoids re-scanning 24K+ names on every render
    var processedContent: String?

    enum Role {
        case user
        case assistant
        case error
    }
}
