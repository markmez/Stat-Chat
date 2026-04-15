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

private enum BoldMode { case firstOnly, all, none }

struct NotableEventsFeed: View {
    var onPlayerTap: ((String) -> Void)?
    var onTeamTap: ((String) -> Void)?
    var onMatchupTap: ((String) -> Void)?  // Query string for matchup preview
    var showHeader: Bool = true
    @Binding var matchupPills: [String]
    var hasExpandedTrayToday: Bool = false
    var trayExpanded: Bool = false
    var refreshTrigger: Bool = false

    @State private var events: [NotableEvent] = []
    @State private var lastLoadTime: Date?
    @State private var seenHeadlines: [String: Set<String>] = Self.loadSeenHeadlines()
    @State private var dwellTimers: [String: Date] = [:]  // headline -> appeared time

    private let deepBlue = Color.brandDeepBlue

    /// Check if user has visited the feed today (Eastern time, daylight hours)
    private static func hasVisitedToday() -> Bool {
        let key = "feedLastVisitDate"
        let stored = UserDefaults.standard.string(forKey: key) ?? ""
        return stored == Self.todayETString()
    }

    /// Mark that the user visited the feed today
    private static func markVisitedToday() {
        UserDefaults.standard.set(Self.todayETString(), forKey: "feedLastVisitDate")
    }

    /// Mark that the user expanded the tray today
    static func markExpandedTrayToday() {
        UserDefaults.standard.set(Self.todayETString(), forKey: "feedLastExpandDate")
    }

    /// Check if user has expanded the tray today
    static func hasExpandedTray() -> Bool {
        let stored = UserDefaults.standard.string(forKey: "feedLastExpandDate") ?? ""
        return stored == Self.todayETString()
    }

    /// Today's date in ET as "YYYY-MM-DD", rolling over at 6 AM ET
    /// (late night games before 6 AM count as "yesterday")
    private static func todayETString() -> String {
        let et = TimeZone(identifier: "America/New_York")!
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = et
        var date = Date()
        let hour = cal.component(.hour, from: date)
        if hour < 6 {
            date = cal.date(byAdding: .day, value: -1, to: date)!
        }
        let fmt = DateFormatter()
        fmt.dateFormat = "yyyy-MM-dd"
        fmt.timeZone = et
        return fmt.string(from: date)
    }

    // MARK: - Scroll depth tracking

    private static func loadSeenHeadlines() -> [String: Set<String>] {
        guard let dict = UserDefaults.standard.dictionary(forKey: "seenEventHeadlines") as? [String: [String]] else { return [:] }
        return dict.mapValues { Set($0) }
    }

    private func saveSeenHeadlines() {
        // Only keep last 3 days
        let cal = Calendar.current
        let cutoff = cal.date(byAdding: .day, value: -3, to: Date())!
        let fmt = DateFormatter()
        fmt.dateFormat = "yyyy-MM-dd"
        let cutoffStr = fmt.string(from: cutoff)
        let recent = seenHeadlines.filter { $0.key >= cutoffStr }
        UserDefaults.standard.set(recent.mapValues { Array($0) }, forKey: "seenEventHeadlines")
    }

    private func markSeen(_ event: NotableEvent) {
        guard !event.headline.isEmpty else { return }
        let wasNew = seenHeadlines[event.gameDate, default: []].insert(event.headline).inserted
        if wasNew { saveSeenHeadlines() }
    }

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
                            .onAppear {
                                if index == 0 && !trayExpanded {
                                    // Collapsed peek: mark first event seen immediately
                                    markSeen(event)
                                } else if trayExpanded {
                                    // Expanded: start dwell timer
                                    dwellTimers[event.headline] = Date()
                                }
                            }
                            .onDisappear {
                                // Check if event was visible for 1.5s+
                                if let appeared = dwellTimers.removeValue(forKey: event.headline),
                                   Date().timeIntervalSince(appeared) >= 1.5 {
                                    markSeen(event)
                                }
                            }
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
            .onChange(of: refreshTrigger) {
                // Reload when returning to home screen after 5+ min in-app
                Task { await loadEvents() }
            }
            .onChange(of: trayExpanded) { _, expanded in
                if !expanded {
                    // Tray collapsed: flush dwell timers, mark any that hit 1.5s
                    let now = Date()
                    for (headline, appeared) in dwellTimers {
                        if now.timeIntervalSince(appeared) >= 1.5,
                           let event = events.first(where: { $0.headline == headline }) {
                            markSeen(event)
                        }
                    }
                    dwellTimers.removeAll()
                }
            }
    }

    private func loadEvents() async {
        do {
            let data = try await BackendService().fetchNotableEvents()
            var loaded = data.map { NotableEvent(from: $0) }


            // Extract matchup pill strings from "Tonight" events
            let pills = loaded
                .filter { $0.category == "Tonight" && $0.playerNames.count >= 2 }
                .map { event -> String in
                    let batter = event.playerNames[0]
                    let lastName = batter.components(separatedBy: " ").last ?? batter
                    return "\(lastName) tonight"
                }

            // Interleave matchup previews based on user engagement today:
            // - First visit today: interleave (every 3rd slot among today's events)
            // - Visited but not expanded tray: first matchup at top, interleave rest
            // - Expanded tray: all matchups at top (user is engaged, wants tonight's games)
            let isFirstVisitToday = !Self.hasVisitedToday()
            let previews = loaded.filter { $0.category == "Tonight" }
            let others = loaded.filter { $0.category != "Tonight" }

            // Find the boundary between today's and older events
            let todayDate = others.first?.gameDate ?? ""
            let todayOthers = others.filter { $0.gameDate == todayDate }
            let older = others.filter { $0.gameDate != todayDate }

            var merged: [NotableEvent]
            if hasExpandedTrayToday {
                // All previews at top, then today's events, then older
                merged = previews + todayOthers + older
            } else if !isFirstVisitToday {
                // First preview at top, interleave rest among today's events
                var result: [NotableEvent] = []
                var pi = 0
                if !previews.isEmpty {
                    result.append(previews[0])
                    pi = 1
                }
                for (i, event) in todayOthers.enumerated() {
                    if pi < previews.count && i > 0 && i % 3 == 2 {
                        result.append(previews[pi])
                        pi += 1
                    }
                    result.append(event)
                }
                while pi < previews.count {
                    result.append(previews[pi])
                    pi += 1
                }
                merged = result + older
            } else {
                // First visit: interleave all among today's events
                var result: [NotableEvent] = []
                var pi = 0
                for (i, event) in todayOthers.enumerated() {
                    if pi < previews.count && i > 0 && i % 3 == 2 {
                        result.append(previews[pi])
                        pi += 1
                    }
                    result.append(event)
                }
                while pi < previews.count {
                    result.append(previews[pi])
                    pi += 1
                }
                merged = result + older
            }

            // Reorder: bubble unseen events to top within each feed group.
            // Today's previews/on-this-date share a group with the most recent
            // game results since they appear together as one feed session.
            let savedSeen = Self.loadSeenHeadlines()
            let allSeenHeadlines = savedSeen.values.reduce(into: Set<String>()) { $0.formUnion($1) }
            let today = Self.todayETString()

            // Find the most recent game date (non-today, has actual game results)
            let latestGameDate = merged.first(where: { $0.gameDate != today && $0.category != "Tonight" })?.gameDate
            // Top group: today + latest game date (if today has events)
            let hasTodayEvents = merged.contains { $0.gameDate == today }
            var topGroupDates: Set<String> = []
            if hasTodayEvents, let gd = latestGameDate {
                topGroupDates = [today, gd]
            } else if hasTodayEvents {
                topGroupDates = [today]
            } else if let gd = latestGameDate {
                topGroupDates = [gd]
            }

            var reordered: [NotableEvent] = []
            var idx = 0

            // First group: combined top dates
            if !topGroupDates.isEmpty {
                var topGroup: [NotableEvent] = []
                while idx < merged.count && topGroupDates.contains(merged[idx].gameDate) {
                    topGroup.append(merged[idx])
                    idx += 1
                }
                let unseenTop = topGroup.filter { !allSeenHeadlines.contains($0.headline) }
                let seenTop = topGroup.filter { allSeenHeadlines.contains($0.headline) }
                reordered.append(contentsOf: unseenTop)
                reordered.append(contentsOf: seenTop)
            }

            // Remaining groups: one per date
            while idx < merged.count {
                let currentDate = merged[idx].gameDate
                var dateGroup: [NotableEvent] = []
                while idx < merged.count && merged[idx].gameDate == currentDate {
                    dateGroup.append(merged[idx])
                    idx += 1
                }
                let seenSet = savedSeen[currentDate] ?? []
                let unseen = dateGroup.filter { !seenSet.contains($0.headline) }
                let seen = dateGroup.filter { seenSet.contains($0.headline) }
                reordered.append(contentsOf: unseen)
                reordered.append(contentsOf: seen)
            }
            merged = reordered

            // Mark as visited today
            Self.markVisitedToday()

            await MainActor.run {
                events = merged
                matchupPills = pills
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
                    // Matchup preview: headline text + inline CTA + optional hint
                    let detailClean = event.detail.replacingOccurrences(
                        of: "\\[MATCHUP_HINT\\].*?\\[/MATCHUP_HINT\\]",
                        with: "", options: .regularExpression)
                    let fullText = event.headline + detailClean
                    let attributed = highlightedTextWithCTA(
                        fullText,
                        playerNames: event.playerNames,
                        teamNames: event.teamNames,
                        ctaText: "matchup preview.",

                        ctaURL: "statchat://matchup/\(event.playerNames[0]) vs \(event.playerNames[1])"
                    )
                    Text(attributed)
                        .font(.system(.callout, design: .rounded))
                        .foregroundStyle(.primary)
                        .lineSpacing(3)

                    // Matchup hint subtext
                    if let hintRange = event.detail.range(of: "\\[MATCHUP_HINT\\](.*?)\\[/MATCHUP_HINT\\]", options: .regularExpression),
                       let capture = event.detail.range(of: "(?<=\\[MATCHUP_HINT\\]).*?(?=\\[/MATCHUP_HINT\\])", options: .regularExpression) {
                        let hint = String(event.detail[capture])
                        Text(hint)
                            .font(.system(.caption, design: .rounded))
                            .italic()
                            .foregroundStyle(.secondary)
                    }
                } else {
                    Text(highlightedText(
                        event.detail.isEmpty ? event.headline : event.headline + " " + event.detail,
                        playerNames: event.playerNames, teamNames: event.teamNames))
                        .font(.system(.callout, design: .rounded))
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
        var result = highlightedText(text, playerNames: playerNames, teamNames: teamNames, boldMode: .none)

        // Make the CTA text a tappable bold link
        if let range = result.range(of: ctaText) {
            result[range].foregroundColor = deepBlue
            result[range].font = .system(.subheadline, design: .rounded, weight: .bold)
            if let encoded = ctaURL.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) {
                result[range].link = URL(string: encoded)
            }
        }

        return result
    }

    /// Build an AttributedString with tappable player/team names.
    /// First player is bold + linked (primary). Subsequent players are linked but not bold.
    /// When allBold is true, ALL player names are bold (used for matchup preview cards).
    private func highlightedText(_ text: String, playerNames: [String], teamNames: [String], boldMode: BoldMode = .firstOnly) -> AttributedString {
        var result = AttributedString(text)

        // Highlight + link player names
        for (index, name) in playerNames.enumerated() {
            if let range = result.range(of: name) {
                result[range].foregroundColor = deepBlue
                if boldMode == .all || (boldMode == .firstOnly && index == 0) {
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
    NotableEventsFeed(matchupPills: .constant([]), trayExpanded: true)
}
