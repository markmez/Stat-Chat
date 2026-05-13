import SwiftUI

struct ResultCard: View {
    let message: Message
    var isFirstUser: Bool = false
    var onBack: (() -> Void)? = nil
    var isStreaming: Bool = false
    /// User queries from the conversation — suggestions matching these are suppressed.
    var previousQueries: [String] = []
    var onPlayerTap: ((String) -> Void)? = nil
    var onTeamTap: ((String) -> Void)? = nil
    var onQueryTap: ((String) -> Void)? = nil
    var onDrilldownTap: ((String) -> Void)? = nil
    /// Called when the user taps the edit pencil next to a voice-input query.
    /// The caller pre-fills the input bar with the supplied text so the user
    /// can correct a mistranscription without re-typing from scratch.
    var onEditVoice: ((String) -> Void)? = nil

    private let deepBlue = Color.brandDeepBlue
    private let lightBlue = Color(red: 0.45, green: 0.7, blue: 1.0)

    var body: some View {
        switch message.role {
        case .user:
            userQuery

        case .assistant:
            answerCard

        case .error:
            errorCard
        }
    }

    // User question — with back chevron on the first one and an inline edit
    // pencil for voice-input queries (since transcription may have gotten it
    // wrong and we auto-submit before the user can review).
    //
    // The pencil is rendered as an inline glyph inside the question Text via
    // Text concatenation so it flows with the text — same-line if there's
    // room, wrapping to the next line if not — instead of reserving fixed
    // trailing space on every line. The whole question becomes the tap
    // target for editing; the pencil is the affordance hint.
    private var userQuery: some View {
        HStack(alignment: .top, spacing: 10) {
            if isFirstUser, let onBack {
                Button(action: onBack) {
                    Image(systemName: "chevron.left")
                        .font(.system(size: 22, weight: .medium))
                        .foregroundStyle(Color(red: 0.45, green: 0.7, blue: 1.0))
                }
                .padding(.top, 2)
            }

            if message.inputMethod == "mic", let onEditVoice {
                Button {
                    onEditVoice(message.content)
                } label: {
                    (Text(message.content)
                        .font(.system(.title3, design: .rounded, weight: .semibold))
                        .foregroundColor(.primary)
                     + Text("   Edit")
                        .font(.system(.subheadline, design: .rounded, weight: .medium))
                        .foregroundColor(lightBlue))
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .multilineTextAlignment(.leading)
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Edit voice question")
            } else {
                Text(message.content)
                    .font(.system(.title3, design: .rounded, weight: .semibold))
                    .foregroundStyle(.primary)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .padding(.horizontal, 20)
        .padding(.top, 4)
    }

    // Answer — text on plain background, stat grids in styled cards
    private var answerCard: some View {
        VStack(alignment: .leading, spacing: 4) {
            if message.content.isEmpty {
                Text(" ")
                    .font(.system(.body, design: .rounded))
                    .padding(.horizontal, 20)
            } else {
                let segments = StatGridParser.parse(message.content, isStreaming: isStreaming)
                let grouped = groupedSegments(segments)
                ForEach(Array(grouped.enumerated()), id: \.offset) { idx, group in
                    switch group {
                    case .single(let segment):
                        renderSegment(segment, isFirst: idx == 0)

                    case .suggestions(let queries):
                        if let tap = onQueryTap {
                            FlowLayout(spacing: 8) {
                                ForEach(queries, id: \.self) { query in
                                    Button {
                                        AnalyticsService.trackSuggestionTap(text: query, source: .resultPill)
                                        tap(query)
                                    } label: {
                                        HStack(spacing: 6) {
                                            Image(systemName: "magnifyingglass")
                                                .font(.system(size: 13, weight: .medium))
                                            Text(query)
                                                .font(.system(.subheadline, design: .rounded, weight: .medium))
                                        }
                                        .foregroundStyle(.white)
                                        .padding(.horizontal, 14)
                                        .padding(.vertical, 9)
                                        .background(
                                            LinearGradient(
                                                colors: [lightBlue, deepBlue],
                                                startPoint: .leading, endPoint: .trailing
                                            ),
                                            in: Capsule()
                                        )
                                    }
                                    .buttonStyle(.plain)
                                }
                            }
                            .padding(.horizontal, 20)
                            .padding(.top, 10)
                        }
                    }
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    @ViewBuilder
    private func renderSegment(_ segment: StatGridParser.Segment, isFirst: Bool = false) -> some View {
        switch segment {
        case .text(let text):
            let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
            if !trimmed.isEmpty {
                let isSectionHeader = trimmed.hasPrefix("**") && !isFirst
                // addLinks/addTeamLinks are internally cached — safe to call on every render
                let displayText = isStreaming ? trimmed : PlayerNameMatcher.addTeamLinks(to: PlayerNameMatcher.addLinks(to: trimmed))
                Text(LocalizedStringKey(displayText))
                    .font(.system(.body, design: .rounded))
                    .foregroundStyle(.primary.opacity(0.85))
                    .tint(deepBlue)
                    .textSelection(.enabled)
                    .lineSpacing(3)
                    .padding(.horizontal, 20)
                    .padding(.top, isSectionHeader ? 16 : 0)
                    .environment(\.openURL, OpenURLAction { url in
                        if url.scheme == "statchat",
                           url.host == "player",
                           let name = url.pathComponents.dropFirst().first?.removingPercentEncoding {
                            onPlayerTap?(name)
                            return .handled
                        }
                        if url.scheme == "statchat",
                           url.host == "team",
                           let code = url.pathComponents.dropFirst().first?.removingPercentEncoding {
                            onTeamTap?(code)
                            return .handled
                        }
                        return .systemAction
                    })
            }

        case .statGrid(let grid):
            StatGridView(grid: grid, onPlayerTap: onPlayerTap)
                .padding(.horizontal, 6)
                .padding(.vertical, 6)

        case .leaderboard(let grid):
            LeaderboardView(grid: grid, onPlayerTap: onPlayerTap, onTeamTap: onTeamTap, onDrilldownTap: onDrilldownTap)
                .padding(.horizontal, 6)
                .padding(.vertical, 6)

        case .tip(let text):
            if UserDefaults.standard.integer(forKey: "lastNameSearchCount") < 2 {
                HStack(alignment: .top, spacing: 5) {
                    Image(systemName: "lightbulb.fill")
                        .font(.system(size: 12))
                        .foregroundStyle(.yellow)
                    Group {
                        Text("Tip: ").fontWeight(.medium) +
                        Text(text).italic()
                    }
                    .font(.system(.caption, design: .rounded))
                    .foregroundStyle(.secondary)
                }
                .padding(.horizontal, 20)
            }

        case .context(let text):
            Text(text)
                .font(.system(.subheadline, design: .rounded, weight: .medium))
                .foregroundStyle(.primary.opacity(0.7))
                .padding(.horizontal, 20)
                .padding(.top, 2)

        case .gameLogs(let entries):
            GameLogsResultView(entries: entries)
                .padding(.horizontal, 6)

        case .aiDisclaimer(let text):
            HStack(alignment: .top, spacing: 5) {
                Image(systemName: "sparkle")
                    .font(.system(size: 10, weight: .medium))
                    .foregroundStyle(.secondary.opacity(0.35))
                Text(text)
                    .font(.system(.caption2, design: .rounded))
                    .foregroundStyle(.secondary.opacity(0.45))
                    .italic()
            }
            .padding(.horizontal, 20)
            .padding(.top, 8)

        case .disclaimer(let text):
            Text(text)
                .font(.system(.caption2, design: .rounded))
                .foregroundStyle(.secondary.opacity(0.4))
                .padding(.horizontal, 20)
                .padding(.top, 10)

        case .subtitle(let text):
            Text(text)
                .font(.system(.caption, design: .rounded))
                .italic()
                .foregroundStyle(.secondary)
                .padding(.horizontal, 20)
                .padding(.top, -2)

        case .seeAlso(let names):
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 0) {
                    Text("See also: ")
                        .foregroundStyle(.secondary)
                    ForEach(Array(names.enumerated()), id: \.offset) { idx, name in
                        if idx > 0 {
                            Text(", ")
                                .foregroundStyle(.secondary)
                        }
                        Button {
                            onPlayerTap?(name)
                        } label: {
                            Text(name)
                                .foregroundStyle(deepBlue)
                        }
                        .buttonStyle(.plain)
                    }
                }
                .font(.system(.subheadline, design: .rounded))
            }
            .padding(.horizontal, 20)
            .padding(.top, 10)

        case .didYouMean(let query):
            if let tap = onQueryTap {
                let queries = query.components(separatedBy: "|").map { $0.trimmingCharacters(in: .whitespaces) }
                // Stack vertically so long suggestions ("Pitchers with 200+ strikeouts
                // and sub 3.00 ERA last year") get their own line and wrap cleanly within
                // it instead of overflowing the inline FlowLayout. Comma separators are
                // unnecessary when each item is on its own row.
                VStack(alignment: .leading, spacing: 4) {
                    Text("See also:")
                        .foregroundStyle(.secondary)
                    ForEach(Array(queries.enumerated()), id: \.offset) { _, q in
                        Button { tap(q) } label: {
                            Text(q)
                                .foregroundStyle(deepBlue)
                                .multilineTextAlignment(.leading)
                                .fixedSize(horizontal: false, vertical: true)
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }
                        .buttonStyle(.plain)
                    }
                }
                .font(.system(.subheadline, design: .rounded))
                .padding(.horizontal, 20)
                .padding(.top, 2)
            }

        case .querySuggestion, .partialGrid:
            EmptyView()
        }
    }

    // Group consecutive querySuggestion segments together
    private enum SegmentGroup {
        case single(StatGridParser.Segment)
        case suggestions([String])
    }

    private func groupedSegments(_ segments: [StatGridParser.Segment]) -> [SegmentGroup] {
        let excludedLower = Set(previousQueries.map { $0.lowercased() })

        var result: [SegmentGroup] = []
        var pendingSuggestions: [String] = []

        for segment in segments {
            if case .querySuggestion(let query) = segment {
                // Skip suggestions that match a previous user query
                if !excludedLower.contains(query.lowercased()) {
                    pendingSuggestions.append(query)
                }
            } else {
                if !pendingSuggestions.isEmpty {
                    result.append(.suggestions(pendingSuggestions))
                    pendingSuggestions = []
                }
                result.append(.single(segment))
            }
        }
        if !pendingSuggestions.isEmpty {
            result.append(.suggestions(pendingSuggestions))
        }
        return result
    }

    // Error — plain text, not alarming
    private var errorCard: some View {
        Text(message.content)
            .font(.system(.callout, design: .rounded))
            .foregroundStyle(.secondary)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(16)
            .padding(.horizontal, 16)
    }
}

// MARK: - Flow layout for wrapping pills

/// A horizontal wrapping layout — items flow left-to-right, wrapping to the next line when needed.
/// Layout-value marker so FlowLayout can recognize separator-only views and
/// drop them when they'd otherwise render at the start of a wrapped line —
/// e.g. a bio dot ("  ·  Bats: Right") wrapping shouldn't leave a leading
/// dot orphaned at the new line's left edge.
private struct FlowSeparatorKey: LayoutValueKey {
    static let defaultValue: Bool = false
}

extension View {
    /// Mark a view (typically a small separator like " · ") so FlowLayout
    /// will skip it when it would render at the start of a row.
    func flowSeparator() -> some View {
        layoutValue(key: FlowSeparatorKey.self, value: true)
    }
}

struct FlowLayout: Layout {
    var spacing: CGFloat = 8

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let rows = computeRows(proposal: proposal, subviews: subviews)
        var height: CGFloat = 0
        for (i, row) in rows.enumerated() {
            let rowHeight = row.map(\.size.height).max() ?? 0
            height += rowHeight
            if i > 0 { height += spacing }
        }
        return CGSize(width: proposal.width ?? 0, height: height)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        let rows = computeRows(proposal: proposal, subviews: subviews)
        var y = bounds.minY
        for (i, row) in rows.enumerated() {
            if i > 0 { y += spacing }
            var x = bounds.minX
            let rowHeight = row.map(\.size.height).max() ?? 0
            for item in row {
                item.subview.place(at: CGPoint(x: x, y: y), proposal: ProposedViewSize(item.size))
                x += item.size.width + spacing
            }
            y += rowHeight
        }
    }

    private struct RowItem {
        let subview: LayoutSubview
        let size: CGSize
    }

    private func computeRows(proposal: ProposedViewSize, subviews: Subviews) -> [[RowItem]] {
        let maxWidth = proposal.width ?? .infinity
        var rows: [[RowItem]] = [[]]
        var currentRowWidth: CGFloat = 0

        for subview in subviews {
            let size = subview.sizeThatFits(.unspecified)
            let isSeparator = subview[FlowSeparatorKey.self]
            let needed = currentRowWidth > 0 ? size.width + spacing : size.width
            let willOverflow = currentRowWidth + needed > maxWidth && !rows[rows.count - 1].isEmpty
            if willOverflow {
                rows.append([])
                currentRowWidth = 0
            }
            // Drop separators that would land at the start of a row (the
            // initial row's start, or the start of any wrapped row). The
            // line break itself provides separation.
            if isSeparator && rows[rows.count - 1].isEmpty {
                continue
            }
            rows[rows.count - 1].append(RowItem(subview: subview, size: size))
            currentRowWidth += (currentRowWidth > 0 ? spacing : 0) + size.width
        }
        return rows
    }
}
