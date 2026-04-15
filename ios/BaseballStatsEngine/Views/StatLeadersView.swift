import SwiftUI

struct StatLeadersView: View {
    var onPlayerTap: ((String) -> Void)?

    @State private var leagueData: [String: BackendService.LeadersResponse] = [:]
    @State private var selectedLeaguePerStat: [String: String] = [:]  // stat → league
    @State private var expandedStats: Set<String> = []

    private let deepBlue = Color.brandDeepBlue
    private let leagues = ["MLB", "AL", "NL"]

    private func dataForStat(_ stat: String, from boards: [BackendService.StatLeaderboard], league: String) -> BackendService.StatLeaderboard? {
        let targetData = leagueData[league]
        // Find this stat in the target league's data
        let allBoards = (targetData?.batting ?? []) + (targetData?.pitching ?? [])
        return allBoards.first { $0.stat == stat }
    }

    private func leagueForStat(_ stat: String) -> String {
        selectedLeaguePerStat[stat] ?? "MLB"
    }

    var body: some View {
        let mlbData = leagueData["MLB"]

        if mlbData != nil && !(mlbData!.batting.isEmpty && mlbData!.pitching.isEmpty) {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    // Batting
                    ForEach(mlbData!.batting, id: \.stat) { board in
                        leaderSection(stat: board.stat, defaultBoard: board)
                    }

                    // Pitching divider
                    if !mlbData!.pitching.isEmpty {
                        HStack(spacing: 8) {
                            Rectangle().fill(deepBlue.opacity(0.2)).frame(height: 1)
                            Text("Pitching")
                                .font(.system(.caption2, design: .rounded, weight: .semibold))
                                .foregroundStyle(.secondary)
                            Rectangle().fill(deepBlue.opacity(0.2)).frame(height: 1)
                        }
                        .padding(.horizontal, 16)
                        .padding(.top, 4)

                        ForEach(mlbData!.pitching, id: \.stat) { board in
                            leaderSection(stat: board.stat, defaultBoard: board)
                        }
                    }

                    Spacer().frame(height: 20)
                }
                .padding(.top, 8)
            }
        } else {
            VStack {
                Spacer()
                ProgressView()
                    .tint(deepBlue)
                Spacer()
            }
            .frame(maxWidth: .infinity)
            .task {
                await loadLeaders(league: "MLB")
            }
        }
    }

    @ViewBuilder
    private func leaderSection(stat: String, defaultBoard: BackendService.StatLeaderboard) -> some View {
        let league = leagueForStat(stat)
        let board = dataForStat(stat, from: [], league: league) ?? defaultBoard
        let isExpanded = expandedStats.contains(stat)
        let displayCount = isExpanded ? board.leaders.count : min(5, board.leaders.count)

        VStack(alignment: .leading, spacing: 4) {
            // Stat header + league selector
            HStack {
                Text(stat)
                    .font(.system(.subheadline, design: .rounded, weight: .bold))
                    .foregroundStyle(.primary)

                Spacer()

                // Inline league pills
                HStack(spacing: 0) {
                    ForEach(leagues, id: \.self) { lg in
                        Button {
                            withAnimation(.easeInOut(duration: 0.15)) {
                                selectedLeaguePerStat[stat] = lg
                            }
                            if leagueData[lg] == nil {
                                Task { await loadLeaders(league: lg) }
                            }
                        } label: {
                            Text(lg)
                                .font(.system(.caption2, design: .rounded, weight: league == lg ? .bold : .regular))
                                .foregroundStyle(league == lg ? deepBlue : .secondary.opacity(0.6))
                                .padding(.horizontal, 6)
                                .padding(.vertical, 2)
                        }
                        .buttonStyle(.plain)
                    }
                }
            }
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
                            expandedStats.remove(stat)
                        } else {
                            expandedStats.insert(stat)
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
                leagueData[league] = result
            }
        } catch {
            // Silent fail
        }
    }
}

#Preview {
    StatLeadersView()
}
