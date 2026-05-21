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
    var onQueryTap: ((String) -> Void)?    // Generic query — e.g. streak game-count links
    var showHeader: Bool = true
    @Binding var matchupPills: [String]
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
                } else if url.host == "query" {
                    let query = url.path.dropFirst().removingPercentEncoding ?? String(url.path.dropFirst())
                    (onQueryTap ?? onMatchupTap)?(query)
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
            let loaded = data.map { NotableEvent(from: $0) }


            // Extract matchup pill strings from "Tonight" events
            let pills = loaded
                .filter { $0.category == "Tonight" && $0.playerNames.count >= 2 }
                .map { event -> String in
                    let batter = event.playerNames[0]
                    let lastName = batter.components(separatedBy: " ").last ?? batter
                    return "\(lastName) tonight"
                }

            // Interleave matchup previews and on-this-date events into the
            // most recent date that has REAL game events.
            //
            // Date convention reminder (feedback-feed-event-date-semantics.md):
            // game-derived events (streak/milestone/rarity/AI insight) are
            // always dated YESTERDAY when the user views the feed TODAY —
            // because they're keyed to the game date, not the publish date.
            // Today's bucket ONLY ever contains supplemental types (matchup
            // preview, on-this-date). If we treat today as its own bucket,
            // those events form a top-of-feed clump with nothing real to
            // interleave against.
            //
            // The backend re-buckets today's MP + OTD into yesterday's bucket
            // (commit 28c6622), but iOS still needs to honor that — preserve
            // OTDs inside the "todayOthers" group rather than filtering them
            // to "older" by their literal game_date.
            let isFirstVisitToday = !Self.hasVisitedToday()
            let previews = loaded.filter { $0.category == "Tonight" }
            let others = loaded.filter { $0.category != "Tonight" }

            // "todayDate" for iOS purposes = the most recent date with a
            // real game event, NOT the gameDate of the first event. Today's
            // OTDs have gameDate=today but should render alongside yesterday's
            // real events, not in their own clump.
            let todayDate = others.first(where: { $0.category != "On This Date" })?.gameDate
                ?? others.first?.gameDate
                ?? ""
            // todayOthers includes OTDs from any date — they belong with the
            // top group regardless of their literal gameDate, since the
            // backend has already placed them in interleave order there.
            let todayOthers = others.filter {
                $0.gameDate == todayDate || $0.category == "On This Date"
            }
            let older = others.filter {
                $0.gameDate != todayDate && $0.category != "On This Date"
            }

            // Always interleave MPs at every 3rd slot. The previous
            // "tray expanded → all at top" branch caused MP clumping at the
            // top of the feed, which mirrored the OTD clumping bug — both
            // surfaces converged on the same shape because the underlying
            // bucketing was wrong. Now that buckets are right (OTDs join
            // todayOthers), MPs just interleave consistently regardless of
            // tray state.
            var merged: [NotableEvent]
            var result: [NotableEvent] = []
            var pi = 0
            // Optionally lead with one MP only when the user has just
            // visited and isn't on first-visit (so they get an immediate
            // matchup tease without a wall of them).
            if !isFirstVisitToday && !previews.isEmpty {
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
                    Text(streakAttributed(for: event))
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

    /// Build the AttributedString for a non-matchup event. For streak events
    /// (category or prose-based detection), the first "N game(s)" span is
    /// linked to `{player} last N games`.
    private func streakAttributed(for event: NotableEvent) -> AttributedString {
        let fullText = event.detail.isEmpty ? event.headline : event.headline + " " + event.detail
        var attr = highlightedText(fullText, playerNames: event.playerNames, teamNames: event.teamNames)
        if let primary = event.playerNames.first, isStreakEvent(event, fullText: fullText) {
            linkifyStreakGameCount(&attr, in: fullText, playerName: primary)
        }
        return attr
    }

    /// Decide whether an event is "streak-shaped" — catches both the Streak
    /// category (Tier 1 detectors + PELT) and historical-scan streak events
    /// (category "historical" but headline mentions a streak/heater/stretch).
    /// Gating on these keywords avoids linkifying game-counts in unrelated
    /// milestone/record events where the number doesn't map to a "last N games"
    /// view.
    private func isStreakEvent(_ event: NotableEvent, fullText: String) -> Bool {
        if event.category == "Streak" { return true }
        let lower = fullText.lowercased()
        return lower.contains("streak") || lower.contains("heater") || lower.contains("stretch")
    }

    /// Find the FIRST "N game" / "N games" / "N-game" span in a streak headline
    /// and link it to a `{player} last N games` query. In-place mutation on the
    /// caller's AttributedString so any existing player/team links are preserved.
    private func linkifyStreakGameCount(_ attr: inout AttributedString, in source: String, playerName: String) {
        // Match a number followed by optional "straight"/"consecutive"/"career" filler,
        // then "game" optionally pluralized. The whitelist keeps us from
        // matching things like "15 home games" that aren't streak-relevant.
        // Word boundary at the end prevents matching "gamer".
        guard let regex = try? NSRegularExpression(
            pattern: #"\b(\d+)(?:[\s-](?:straight|consecutive|career))?[\s-]games?\b"#
        ) else { return }
        let ns = source as NSString
        let match = regex.firstMatch(in: source, range: NSRange(location: 0, length: ns.length))
        guard let match = match, match.numberOfRanges >= 2 else { return }
        let spanNS = match.range(at: 0)
        let numNS = match.range(at: 1)
        let spanText = ns.substring(with: spanNS)
        let gameCount = ns.substring(with: numNS)

        // Convert the NSRange to an AttributedString range by locating the span
        // in the attributed text (AttributedString doesn't share indices with String).
        guard let range = attr.range(of: spanText) else { return }

        let query = "\(playerName) last \(gameCount) games"
        if let encoded = query.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) {
            attr[range].link = URL(string: "statchat://query/\(encoded)")
            attr[range].foregroundColor = deepBlue
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

        // Color + link EVERY occurrence of a name, not just the first — the same
        // player can be mentioned twice in a merged headline (e.g. "...passing
        // Sanchez (80) ... (Sanchez, PHI)"), and both should be tappable. Bold
        // stays on the first occurrence only (the original emphasis behavior).
        func linkAll(_ name: String, urlPrefix: String, boldEligible: Bool) {
            guard !name.isEmpty else { return }
            var searchStart = text.startIndex
            var occurrence = 0
            while let r = text.range(of: name, range: searchStart..<text.endIndex) {
                let lo = text.distance(from: text.startIndex, to: r.lowerBound)
                let hi = text.distance(from: text.startIndex, to: r.upperBound)
                let aLo = result.index(result.startIndex, offsetByCharacters: lo)
                let aHi = result.index(result.startIndex, offsetByCharacters: hi)
                result[aLo..<aHi].foregroundColor = deepBlue
                if boldEligible && occurrence == 0 {
                    result[aLo..<aHi].font = .system(.subheadline, design: .rounded, weight: .bold)
                }
                if let encoded = name.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) {
                    result[aLo..<aHi].link = URL(string: "\(urlPrefix)\(encoded)")
                }
                searchStart = r.upperBound
                occurrence += 1
            }
        }

        for (index, name) in playerNames.enumerated() {
            let boldEligible = boldMode == .all || (boldMode == .firstOnly && index == 0)
            linkAll(name, urlPrefix: "statchat://player/", boldEligible: boldEligible)
        }
        for team in teamNames {
            linkAll(team, urlPrefix: "statchat://team/", boldEligible: false)
        }

        return result
    }
}

#Preview {
    NotableEventsFeed(matchupPills: .constant([]), trayExpanded: true)
}
