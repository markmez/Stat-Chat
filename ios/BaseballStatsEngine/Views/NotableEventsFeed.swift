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
    var onMatchupTap: ((String) -> Void)?  // Query string for matchup preview
    var showHeader: Bool = true

    @State private var events: [NotableEvent] = []
    @State private var lastLoadTime: Date?

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
                } else if url.host == "matchup" {
                    let query = url.path.dropFirst().removingPercentEncoding ?? String(url.path.dropFirst())
                    onMatchupTap?(query)
                }
                return .handled
            })
        }

        Color.clear.frame(height: 0)
            .task {
                // Load on first appear
                if lastLoadTime == nil {
                    await loadEvents()
                }
            }
            .onReceive(NotificationCenter.default.publisher(for: UIApplication.willEnterForegroundNotification)) { _ in
                // Reload when app returns from background (max every 5 min)
                Task {
                    if let last = lastLoadTime, Date().timeIntervalSince(last) < 300 { return }
                    await loadEvents()
                }
            }
    }

    private func loadEvents() async {
        do {
            let data = try await BackendService().fetchNotableEvents()
            var loaded = data.map { NotableEvent(from: $0) }

            // DEBUG: stub matchup preview cards for simulator testing
            #if DEBUG
            let stubs: [NotableEvent] = [
                NotableEvent(
                    headline: "Tonight Aaron Judge takes on RHP Tanner Houck. Despite the platoon mismatch, Judge has been crushing righties with a 1.050 OPS. See more about this matchup in this ",
                    detail: "matchup preview.",
                    category: "Tonight",
                    gameDate: "2026-04-06",
                    playerNames: ["Aaron Judge", "Tanner Houck"],
                    teamNames: ["Yankees", "Red Sox"],
                    gameContext: "Matchup Preview"
                ),
                NotableEvent(
                    headline: "Tonight Shohei Ohtani faces RHP Max Scherzer. Ohtani is 3-for-8 (.375) career against Scherzer with a home run. See more about this matchup in this ",
                    detail: "matchup preview.",
                    category: "Tonight",
                    gameDate: "2026-04-06",
                    playerNames: ["Shohei Ohtani", "Max Scherzer"],
                    teamNames: ["Dodgers", "Rangers"],
                    gameContext: "Matchup Preview"
                ),
                NotableEvent(
                    headline: "Tonight Kyle Tucker takes on RHP Sonny Gray. Tucker is red hot with a 1.100 OPS over his last 12 games. See more about this matchup in this ",
                    detail: "matchup preview.",
                    category: "Tonight",
                    gameDate: "2026-04-06",
                    playerNames: ["Kyle Tucker", "Sonny Gray"],
                    teamNames: ["Cubs", "Cardinals"],
                    gameContext: "Matchup Preview"
                ),
            ]
            loaded = stubs + loaded
            #endif

            await MainActor.run {
                events = loaded
                lastLoadTime = Date()
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
                if event.category == "Tonight" && event.playerNames.count >= 2 {
                    // Matchup preview: headline text + inline CTA
                    let fullText = event.headline + event.detail
                    let attributed = highlightedTextWithCTA(
                        fullText,
                        playerNames: event.playerNames,
                        teamNames: event.teamNames,
                        ctaText: "matchup preview.",
                        ctaURL: "statchat://matchup/\(event.playerNames[0]) vs \(event.playerNames[1])"
                    )
                    Text(attributed)
                        .font(.system(.subheadline, design: .rounded))
                        .foregroundStyle(.primary)
                        .lineSpacing(3)
                } else {
                    Text(highlightedText(
                        event.detail.isEmpty ? event.headline : event.headline + " " + event.detail,
                        playerNames: event.playerNames, teamNames: event.teamNames))
                        .font(.system(.subheadline, design: .rounded))
                        .foregroundStyle(.primary)
                        .lineSpacing(3)
                }
            }
            .padding(.horizontal, 4)
            .padding(.vertical, 14)

            // Gradient separator (always present, invisible on last item to maintain alignment)
            LinearGradient(
                colors: [Color(red: 0.45, green: 0.7, blue: 1.0), Color(red: 0.1, green: 0.25, blue: 0.7)],
                startPoint: .leading, endPoint: .trailing
            )
            .frame(height: 2)
            .clipShape(Capsule())
            .opacity(isLast ? 0 : 1)
        }
    }

    /// Build an AttributedString with tappable names + inline CTA for matchup previews.
    private func highlightedTextWithCTA(_ text: String, playerNames: [String], teamNames: [String],
                                         ctaText: String, ctaURL: String) -> AttributedString {
        var result = highlightedText(text, playerNames: playerNames, teamNames: teamNames, allBold: true)

        // Make the CTA text a tappable link
        if let range = result.range(of: ctaText) {
            result[range].foregroundColor = deepBlue
            result[range].font = .system(.subheadline, design: .rounded, weight: .semibold)
            if let encoded = ctaURL.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) {
                result[range].link = URL(string: encoded)
            }
        }

        return result
    }

    /// Build an AttributedString with tappable player/team names.
    /// First player is bold + linked (primary). Subsequent players are linked but not bold.
    /// When allBold is true, ALL player names are bold (used for matchup preview cards).
    private func highlightedText(_ text: String, playerNames: [String], teamNames: [String], allBold: Bool = false) -> AttributedString {
        var result = AttributedString(text)

        // Highlight + link player names
        for (index, name) in playerNames.enumerated() {
            if let range = result.range(of: name) {
                result[range].foregroundColor = deepBlue
                if index == 0 || allBold {
                    result[range].font = .system(.subheadline, design: .rounded, weight: .bold)
                }
                if let encoded = name.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) {
                    result[range].link = URL(string: "statchat://player/\(encoded)")
                }
            }
        }

        // Highlight + link team names (not bold)
        for team in teamNames {
            if let range = result.range(of: team) {
                result[range].foregroundColor = deepBlue
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
