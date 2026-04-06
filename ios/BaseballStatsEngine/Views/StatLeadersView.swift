import SwiftUI

struct StatLeadersView: View {
    var onPlayerTap: ((String) -> Void)?

    @State private var data: BackendService.LeadersResponse?
    @State private var selectedLeague = "MLB"
    @State private var expandedStats: Set<String> = []

    private let deepBlue = Color(red: 0.1, green: 0.25, blue: 0.7)
    private let lightBlue = Color(red: 0.45, green: 0.7, blue: 1.0)
    private let leagues = ["MLB", "AL", "NL"]

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            // League tabs — Twitter/X style underline
            HStack(spacing: 0) {
                ForEach(leagues, id: \.self) { league in
                    Button {
                        withAnimation(.easeInOut(duration: 0.2)) {
                            selectedLeague = league
                        }
                        Task { await loadLeaders(league: league) }
                    } label: {
                        VStack(spacing: 6) {
                            Text(league)
                                .font(.system(.subheadline, design: .rounded, weight: selectedLeague == league ? .bold : .medium))
                                .foregroundStyle(selectedLeague == league ? .primary : .secondary)
                            Rectangle()
                                .fill(selectedLeague == league ? deepBlue : .clear)
                                .frame(height: 2)
                        }
                        .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.plain)
                }
            }
            .padding(.horizontal, 16)
            .padding(.top, 4)

            if let data = data, !data.batting.isEmpty {
                ScrollView {
                    VStack(alignment: .leading, spacing: 16) {
                        // Batting
                        ForEach(data.batting, id: \.stat) { board in
                            leaderSection(board: board, isBatting: true)
                        }

                        // Pitching divider
                        if !data.pitching.isEmpty {
                            HStack(spacing: 8) {
                                Rectangle().fill(deepBlue.opacity(0.2)).frame(height: 1)
                                Text("Pitching")
                                    .font(.system(.caption2, design: .rounded, weight: .semibold))
                                    .foregroundStyle(.secondary)
                                Rectangle().fill(deepBlue.opacity(0.2)).frame(height: 1)
                            }
                            .padding(.horizontal, 16)
                            .padding(.top, 4)

                            ForEach(data.pitching, id: \.stat) { board in
                                leaderSection(board: board, isBatting: false)
                            }
                        }

                        Spacer().frame(height: 20)
                    }
                    .padding(.top, 12)
                }
            } else {
                VStack {
                    Spacer()
                    ProgressView()
                        .tint(deepBlue)
                    Spacer()
                }
                .frame(maxWidth: .infinity)
            }
        }
        .task {
            if data == nil {
                await loadLeaders(league: selectedLeague)
            }
        }
    }

    @ViewBuilder
    private func leaderSection(board: BackendService.StatLeaderboard, isBatting: Bool) -> some View {
        let isExpanded = expandedStats.contains(board.stat)
        let displayCount = isExpanded ? board.leaders.count : min(5, board.leaders.count)

        VStack(alignment: .leading, spacing: 4) {
            // Stat header
            Text(board.stat)
                .font(.system(.subheadline, design: .rounded, weight: .bold))
                .foregroundStyle(deepBlue)
                .padding(.horizontal, 20)

            // Leader rows
            ForEach(Array(board.leaders.prefix(displayCount).enumerated()), id: \.offset) { idx, leader in
                HStack(spacing: 0) {
                    Text("\(idx + 1)")
                        .font(.system(.caption, design: .monospaced, weight: .medium))
                        .foregroundStyle(.secondary)
                        .frame(width: 22, alignment: .trailing)

                    Button {
                        onPlayerTap?(leader.name)
                    } label: {
                        Text(leader.name)
                            .font(.system(.callout, design: .rounded, weight: .medium))
                            .foregroundStyle(deepBlue)
                            .lineLimit(1)
                    }
                    .buttonStyle(.plain)
                    .padding(.leading, 8)

                    Spacer()

                    Text(leader.value)
                        .font(.system(.callout, design: .monospaced, weight: .semibold))
                        .foregroundStyle(.primary)
                }
                .padding(.horizontal, 20)
                .padding(.vertical, 2)
            }

            // Show more / less
            if board.leaders.count > 5 {
                Button {
                    withAnimation(.easeInOut(duration: 0.2)) {
                        if isExpanded {
                            expandedStats.remove(board.stat)
                        } else {
                            expandedStats.insert(board.stat)
                        }
                    }
                } label: {
                    Text(isExpanded ? "Show less" : "Show more")
                        .font(.system(.caption, design: .rounded, weight: .medium))
                        .foregroundStyle(.secondary)
                }
                .buttonStyle(.plain)
                .padding(.horizontal, 20)
                .padding(.top, 2)
            }
        }
    }

    private func loadLeaders(league: String) async {
        do {
            let result = try await BackendService().fetchLeaders(league: league)
            await MainActor.run {
                data = result
            }
        } catch {
            // Silent fail
        }
    }
}

#Preview {
    StatLeadersView()
}
