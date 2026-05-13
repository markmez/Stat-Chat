import Foundation

struct Message: Identifiable {
    let id = UUID()
    let role: Role
    let content: String
    let timestamp = Date()
    /// How the user entered this message ("keyboard" or "mic"). Only meaningful
    /// on `.user` messages — drives the pencil-edit affordance for voice queries
    /// where speech recognition may have mistranscribed.
    var inputMethod: String = "keyboard"

    enum Role {
        case user
        case assistant
        case error
    }
}
