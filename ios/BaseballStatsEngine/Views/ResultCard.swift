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

    private let deepBlue = Color(red: 0.1, green: 0.25, blue: 0.7)
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

    // User question — with back chevron on the first one
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

            Text(message.content)
                .font(.system(.title3, design: .rounded, weight: .semibold))
                .foregroundStyle(.primary)

            Spacer()
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
                ForEach(Array(grouped.enumerated()), id: \.offset) { _, group in
                    switch group {
                    case .single(let segment):
                        renderSegment(segment)

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
                                                .font(.system(size: 12, weight: .medium))
                                            Text(query)
                                                .font(.system(.footnote, design: .rounded, weight: .medium))
                                        }
                                        .foregroundStyle(.white)
                                        .padding(.horizontal, 14)
                                        .padding(.vertical, 8)
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
    private func renderSegment(_ segment: StatGridParser.Segment) -> some View {
        switch segment {
        case .text(let text):
            let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
            if !trimmed.isEmpty {
                // addLinks/addTeamLinks are internally cached — safe to call on every render
                let displayText = isStreaming ? trimmed : PlayerNameMatcher.addTeamLinks(to: PlayerNameMatcher.addLinks(to: trimmed))
                Text(LocalizedStringKey(displayText))
                    .font(.system(.body, design: .rounded))
                    .foregroundStyle(.primary.opacity(0.85))
                    .tint(deepBlue)
                    .textSelection(.enabled)
                    .lineSpacing(3)
                    .padding(.horizontal, 20)
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
            LeaderboardView(grid: grid, onPlayerTap: onPlayerTap, onTeamTap: onTeamTap)
                .padding(.horizontal, 6)
                .padding(.vertical, 6)

        case .tip(let text):
            HStack(alignment: .top, spacing: 5) {
                Image(systemName: "lightbulb")
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(.secondary.opacity(0.45))
                Group {
                    Text("Tip: ").fontWeight(.medium) +
                    Text(text).italic()
                }
                .font(.system(.caption, design: .rounded))
                .foregroundStyle(.secondary.opacity(0.55))
            }
            .padding(.horizontal, 20)

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
                HStack(spacing: 6) {
                    Text("Interpreting as:")
                        .font(.system(.caption, design: .rounded))
                        .foregroundStyle(.secondary)
                    Button {
                        tap(query)
                    } label: {
                        Text(query)
                            .font(.system(.caption, design: .rounded, weight: .medium))
                            .foregroundStyle(deepBlue)
                    }
                    .buttonStyle(.plain)
                }
                .padding(.horizontal, 20)
                .padding(.bottom, 2)
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

    // Error
    private var errorCard: some View {
        HStack(spacing: 10) {
            Image(systemName: "exclamationmark.triangle")
                .font(.system(size: 14, weight: .medium))
            Text(message.content)
                .font(.system(.callout, design: .rounded))
        }
        .foregroundStyle(.red.opacity(0.9))
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(16)
        .background(
            RoundedRectangle(cornerRadius: 12)
                .fill(.red.opacity(0.08))
                .overlay(
                    RoundedRectangle(cornerRadius: 12)
                        .stroke(.red.opacity(0.15), lineWidth: 0.5)
                )
        )
        .padding(.horizontal, 16)
    }
}

// MARK: - Flow layout for wrapping pills

/// A horizontal wrapping layout — items flow left-to-right, wrapping to the next line when needed.
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
            let needed = currentRowWidth > 0 ? size.width + spacing : size.width
            if currentRowWidth + needed > maxWidth && !rows[rows.count - 1].isEmpty {
                rows.append([])
                currentRowWidth = 0
            }
            rows[rows.count - 1].append(RowItem(subview: subview, size: size))
            currentRowWidth += (currentRowWidth > 0 ? spacing : 0) + size.width
        }
        return rows
    }
}
