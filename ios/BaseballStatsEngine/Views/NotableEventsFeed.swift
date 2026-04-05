import SwiftUI

struct NotableEvent: Identifiable {
    let id = UUID()
    let headline: String
    let detail: String
    let category: String
    let gameDate: String
    let playerNames: [String]
    let teamNames: [String]
    let gameContext: String  // "April 5 · Dodgers 4 - Astros 3"

    init(headline: String, detail: String, category: String, gameDate: String,
         playerNames: [String], teamNames: [String], gameContext: String = "") {
        self.headline = headline
        self.detail = detail
        self.category = category
        self.gameDate = gameDate
        self.playerNames = playerNames
        self.teamNames = teamNames
        self.gameContext = gameContext
    }

    init(from data: BackendService.NotableEventData) {
        self.headline = data.headline
        self.detail = data.detail
        self.category = data.category
        self.gameDate = data.game_date
        self.playerNames = data.player_names
        self.teamNames = data.team_names
        self.gameContext = data.game_context ?? ""
    }
}

struct NotableEventsFeed: View {
    var onPlayerTap: ((String) -> Void)?
    var onTeamTap: ((String) -> Void)?
    var showHeader: Bool = true

    @State private var events: [NotableEvent] = []
    @State private var hasLoaded = false

    private let deepBlue = Color(red: 0.1, green: 0.25, blue: 0.7)

    var body: some View {
        if !events.isEmpty {
            VStack(alignment: .leading, spacing: 0) {
                if showHeader {
                    // Section header
                    HStack(spacing: 6) {
                        Image(systemName: "flame.fill")
                            .font(.system(size: 12, weight: .semibold))
                            .foregroundStyle(
                                LinearGradient(
                                    colors: [.orange, .red],
                                    startPoint: .top, endPoint: .bottom
                                )
                            )
                        Text("Notable")
                            .font(.system(.subheadline, design: .rounded, weight: .semibold))
                            .foregroundStyle(.secondary)
                    }
                    .padding(.horizontal, 24)
                    .padding(.bottom, 12)
                }

                // Events
                LazyVStack(spacing: 0) {
                    ForEach(Array(events.enumerated()), id: \.element.id) { index, event in
                        eventCard(event, isLast: index == events.count - 1)
                    }
                }
                .padding(.horizontal, 20)
                .padding(.bottom, 20)
            }
            .environment(\.openURL, OpenURLAction { url in
                guard url.scheme == "statchat" else { return .systemAction }
                let name = url.lastPathComponent.removingPercentEncoding ?? url.lastPathComponent
                if url.host == "player" {
                    onPlayerTap?(name)
                } else if url.host == "team" {
                    onTeamTap?(name)
                }
                return .handled
            })
        }

        Color.clear.frame(height: 0)
            .task {
                guard !hasLoaded else { return }
                hasLoaded = true
                await loadEvents()
            }
    }

    private func loadEvents() async {
        do {
            let data = try await BackendService().fetchNotableEvents()
            let loaded = data.map { NotableEvent(from: $0) }
            await MainActor.run {
                events = loaded
            }
        } catch {
            // Silently fail — feed just won't show
        }
    }

    @ViewBuilder
    private func eventCard(_ event: NotableEvent, isLast: Bool) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            VStack(alignment: .leading, spacing: 6) {
                // Game context: "April 5 · Dodgers 4 - Astros 3"
                if !event.gameContext.isEmpty {
                    Text(event.gameContext)
                        .font(.system(.caption2, design: .rounded, weight: .bold))
                        .foregroundStyle(.secondary)
                }

                // Combined text — tweet-style
                Text(highlightedText(
                    event.detail.isEmpty ? event.headline : event.headline + " " + event.detail,
                    playerNames: event.playerNames, teamNames: event.teamNames))
                    .font(.system(.subheadline, design: .rounded))
                    .foregroundStyle(.primary)
                    .lineSpacing(3)
            }
            .padding(.horizontal, 4)
            .padding(.vertical, 14)

            // Gradient separator
            if !isLast {
                LinearGradient(
                    colors: [Color(red: 0.45, green: 0.7, blue: 1.0), Color(red: 0.1, green: 0.25, blue: 0.7)],
                    startPoint: .leading, endPoint: .trailing
                )
                .frame(height: 2)
                .clipShape(Capsule())
            }
        }
    }

    /// Build an AttributedString with tappable player/team names
    private func highlightedText(_ text: String, playerNames: [String], teamNames: [String]) -> AttributedString {
        var result = AttributedString(text)

        // Highlight + link player names
        for name in playerNames {
            if let range = result.range(of: name) {
                result[range].foregroundColor = deepBlue
                result[range].font = .system(.subheadline, design: .rounded, weight: .bold)
                if let encoded = name.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) {
                    result[range].link = URL(string: "statchat://player/\(encoded)")
                }
            }
        }

        // Highlight + link team names
        for team in teamNames {
            if let range = result.range(of: team) {
                result[range].foregroundColor = deepBlue
                result[range].font = .system(.subheadline, design: .rounded, weight: .bold)
                if let encoded = team.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) {
                    result[range].link = URL(string: "statchat://team/\(encoded)")
                }
            }
        }

        return result
    }
}

#Preview {
    NotableEventsFeed()
}
