import SwiftUI

/// Static, screenshot-friendly card used to generate share images for answers
/// and player profiles. Never displayed live — rasterized by `ShareImage`
/// via `ImageRenderer`, so all colors use the brand "Fixed" variants and the
/// view is forced to light color scheme regardless of the user's appearance.
struct ShareCardView: View {
    enum Content {
        /// Renders the latest assistant answer, plus any prior Q/A pairs in
        /// the conversation (treated as the follow-up thread).
        case answer(messages: [Message])
        case player(card: PlayerCard)
    }

    let content: Content

    /// Fixed render width — share images are screenshot-style portraits.
    static let renderWidth: CGFloat = 380

    /// Row cap for long lists (leaderboards, game logs) in the share image.
    /// Keeps the rendered card in a shareable aspect ratio so iMessage and
    /// Twitter previews don't crop out the header/footer/branding.
    static let shareRowCap = 10

    /// Returns the grid trimmed to the top `shareRowCap` rows plus the count
    /// of rows that were hidden. Hidden count is 0 if no trim was needed.
    static func capLeaderboard(_ grid: StatGridParser.StatGrid) -> (StatGridParser.StatGrid, Int) {
        guard grid.rows.count > shareRowCap else { return (grid, 0) }
        let capped = StatGridParser.StatGrid(
            headers: grid.headers,
            rows: Array(grid.rows.prefix(shareRowCap)),
            formMetadata: grid.formMetadata
        )
        return (capped, grid.rows.count - shareRowCap)
    }

    private let deepBlue = Color.brandDeepBlueFixed
    private let lightBlue = Color.brandLightBlueFixed

    var body: some View {
        VStack(spacing: 0) {
            header
            divider
            bodyContent
                .padding(.horizontal, 20)
                .padding(.vertical, 22)
            divider
            footer
        }
        .frame(width: Self.renderWidth)
        .background(Color.white)
        .environment(\.colorScheme, .light)
    }

    private var divider: some View {
        Rectangle()
            .fill(Color.black.opacity(0.08))
            .frame(height: 1)
    }

    private var header: some View {
        HStack(spacing: 8) {
            Text("StatChat")
                .font(.system(.title3, design: .rounded, weight: .bold))
                .foregroundStyle(
                    LinearGradient(
                        colors: [lightBlue, deepBlue],
                        startPoint: .leading, endPoint: .trailing
                    )
                )

            ZStack {
                Image(systemName: "sparkle")
                    .font(.system(size: 14, weight: .bold))
                    .foregroundStyle(
                        LinearGradient(
                            colors: [lightBlue, deepBlue],
                            startPoint: .topLeading, endPoint: .bottomTrailing
                        )
                    )

                Image(systemName: "baseball.fill")
                    .font(.system(size: 7))
                    .foregroundStyle(lightBlue)
                    .offset(x: 9, y: -9)

                Image(systemName: "baseball.fill")
                    .font(.system(size: 5))
                    .foregroundStyle(lightBlue.opacity(0.7))
                    .offset(x: -7.5, y: -7.5)

                Image(systemName: "baseball.fill")
                    .font(.system(size: 6))
                    .foregroundStyle(lightBlue.opacity(0.85))
                    .offset(x: 7.5, y: 7.5)
            }

            Spacer()
        }
        .padding(.horizontal, 20)
        .padding(.vertical, 14)
    }

    private var footer: some View {
        Text("StatChat Baseball Stats. Ask Like AI, Fast Real Answers.")
            .font(.system(.footnote, design: .rounded, weight: .semibold))
            .foregroundStyle(
                LinearGradient(
                    colors: [lightBlue, deepBlue],
                    startPoint: .leading, endPoint: .trailing
                )
            )
            .multilineTextAlignment(.center)
            .frame(maxWidth: .infinity)
            .padding(.horizontal, 20)
            .padding(.vertical, 16)
    }

    @ViewBuilder
    private var bodyContent: some View {
        switch content {
        case .answer(let messages):
            answerBody(messages: messages)
        case .player(let card):
            playerBody(card: card)
        }
    }

    // MARK: - Answer body

    private struct QAPair {
        let question: String
        let answer: String?
    }

    private func answerBody(messages: [Message]) -> some View {
        let pairs = pairUp(messages.filter { $0.role != .error })
        let lastIndex = pairs.count - 1
        let isThread = pairs.count > 1

        return VStack(alignment: .leading, spacing: 18) {
            ForEach(Array(pairs.enumerated()), id: \.offset) { idx, pair in
                let isLatest = idx == lastIndex
                pairView(
                    question: pair.question,
                    answer: pair.answer,
                    emphasis: isLatest
                )
                if isThread && !isLatest {
                    Rectangle()
                        .fill(Color.black.opacity(0.06))
                        .frame(height: 1)
                }
            }
        }
    }

    private func pairUp(_ messages: [Message]) -> [QAPair] {
        var result: [QAPair] = []
        var pendingQuestion: String? = nil
        for msg in messages {
            switch msg.role {
            case .user:
                if let q = pendingQuestion { result.append(QAPair(question: q, answer: nil)) }
                pendingQuestion = msg.content
            case .assistant:
                if let q = pendingQuestion {
                    result.append(QAPair(question: q, answer: msg.content))
                    pendingQuestion = nil
                }
            case .error:
                break
            }
        }
        if let q = pendingQuestion {
            result.append(QAPair(question: q, answer: nil))
        }
        return result
    }

    @ViewBuilder
    private func pairView(question: String, answer: String?, emphasis: Bool) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(question)
                .font(.system(emphasis ? .title3 : .subheadline,
                              design: .rounded,
                              weight: emphasis ? .bold : .semibold))
                .foregroundStyle(emphasis ? Color.primary : Color.primary.opacity(0.55))
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity, alignment: .leading)

            if let answer, !answer.isEmpty {
                let segments = StatGridParser.parse(answer, isStreaming: false)
                answerSegments(segments, emphasis: emphasis)
            }
        }
    }

    private func moreInAppLine(_ text: String) -> some View {
        Text(text)
            .font(.system(.footnote, design: .rounded, weight: .medium))
            .foregroundStyle(deepBlue.opacity(0.75))
            .padding(.top, 2)
    }

    @ViewBuilder
    private func answerSegments(_ segments: [StatGridParser.Segment], emphasis: Bool) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            ForEach(Array(segments.enumerated()), id: \.offset) { _, segment in
                segmentView(segment, emphasis: emphasis)
            }
        }
    }

    @ViewBuilder
    private func segmentView(_ segment: StatGridParser.Segment, emphasis: Bool) -> some View {
        switch segment {
        case .text(let text):
            let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
            if !trimmed.isEmpty {
                Text(LocalizedStringKey(trimmed))
                    .font(.system(emphasis ? .body : .callout, design: .rounded))
                    .foregroundStyle(.primary.opacity(0.9))
                    .tint(deepBlue)
                    .lineSpacing(3)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }

        case .statGrid(let grid):
            StatGridView(grid: grid)

        case .leaderboard(let grid):
            let (capped, hiddenCount) = Self.capLeaderboard(grid)
            VStack(alignment: .leading, spacing: 6) {
                LeaderboardView(grid: capped)
                if hiddenCount > 0 {
                    moreInAppLine("+\(hiddenCount) more in app")
                }
            }

        case .context(let text):
            Text(text)
                .font(.system(.subheadline, design: .rounded, weight: .medium))
                .foregroundStyle(.primary.opacity(0.7))
                .fixedSize(horizontal: false, vertical: true)

        case .subtitle(let text):
            Text(text)
                .font(.system(.caption, design: .rounded))
                .italic()
                .foregroundStyle(.secondary)

        case .gameLogs(let entries):
            let cappedEntries = Array(entries.prefix(Self.shareRowCap))
            let hidden = entries.count - cappedEntries.count
            VStack(alignment: .leading, spacing: 6) {
                GameLogsResultView(entries: cappedEntries)
                if hidden > 0 {
                    moreInAppLine("+\(hidden) more in app")
                }
            }

        // Skip pills, see-also, drilldown, tips, AI disclaimer — share card
        // shouldn't carry interactive affordances or auxiliary noise.
        case .querySuggestion, .didYouMean, .seeAlso, .tip, .aiDisclaimer,
             .disclaimer, .partialGrid:
            EmptyView()
        }
    }

    // MARK: - Player body

    @ViewBuilder
    private func playerBody(card: PlayerCard) -> some View {
        VStack(alignment: .leading, spacing: 18) {
            playerHeader(card: card)

            if card.isPitcher, let p = card.pitchingSeasons?.first {
                pitcherForm(p, fallbackYear: p.year)
            } else if let b = card.seasons.first {
                batterForm(b, fallbackYear: b.year)
            }
        }
    }

    private func playerHeader(card: PlayerCard) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(card.name)
                .font(.system(.largeTitle, design: .rounded, weight: .heavy))
                .foregroundStyle(.primary)
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity, alignment: .leading)

            let bio = playerBioLine(card: card)
            if !bio.isEmpty {
                Text(bio)
                    .font(.system(.subheadline, design: .rounded, weight: .medium))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private func playerBioLine(card: PlayerCard) -> String {
        var parts: [String] = []
        let teamName = card.fullTeamName.isEmpty ? card.team : card.fullTeamName
        if !teamName.isEmpty { parts.append(teamName) }
        if let age = card.age { parts.append("Age \(age)") }
        var bt: [String] = []
        if let b = card.bats, !b.isEmpty { bt.append("Bats \(b)") }
        if let t = card.throws_, !t.isEmpty { bt.append("Throws \(t)") }
        if !bt.isEmpty { parts.append(bt.joined(separator: " / ")) }
        return parts.joined(separator: " · ")
    }

    @ViewBuilder
    private func batterForm(_ season: SeasonData, fallbackYear: Int) -> some View {
        if let form = season.currentForm {
            formSection(
                title: "CURRENT FORM",
                subtitle: "Last \(form.numGames) games · since \(formattedFormStart(form.formStartDate))",
                grid: form.stats
            )
        } else {
            formSection(
                title: "\(fallbackYear) SEASON",
                subtitle: nil,
                grid: season.stats
            )
        }
    }

    @ViewBuilder
    private func pitcherForm(_ season: PitchingSeasonData, fallbackYear: Int) -> some View {
        if let form = season.currentForm {
            formSection(
                title: "CURRENT FORM",
                subtitle: "Last \(form.numGames) games · since \(formattedFormStart(form.formStartDate))",
                grid: form.stats
            )
        } else {
            formSection(
                title: "\(fallbackYear) SEASON",
                subtitle: nil,
                grid: season.stats
            )
        }
    }

    private func formSection(title: String, subtitle: String?, grid: StatGridParser.StatGrid) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title)
                .font(.system(.caption, design: .rounded, weight: .bold))
                .foregroundStyle(.secondary)
                .tracking(1.2)
            if let subtitle {
                Text(subtitle)
                    .font(.system(.footnote, design: .rounded))
                    .foregroundStyle(.secondary)
            }
            StatGridView(grid: grid)
        }
    }

    private func formattedFormStart(_ raw: String) -> String {
        let parser = DateFormatter()
        parser.dateFormat = "yyyy-MM-dd"
        if let date = parser.date(from: raw) {
            let fmt = DateFormatter()
            fmt.dateFormat = "MMM d"
            return fmt.string(from: date)
        }
        return raw
    }
}
