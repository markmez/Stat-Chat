import SwiftUI

struct PlayerCardView: View {
    let playerName: String

    @Environment(\.dismiss) private var dismiss
    @State private var playerCard: PlayerCard?
    @State private var isLoading = true
    @State private var expandedSeasons: Set<Int> = []
    @State private var projectionMode: ProjectionMode = .fullSeason

    private let deepBlue = Color(red: 0.1, green: 0.25, blue: 0.7)
    private let lightBlue = Color(red: 0.45, green: 0.7, blue: 1.0)

    enum ProjectionMode: String, CaseIterable {
        case fullSeason = "162 games"
        case gamesMissed = "Account for games missed"
    }

    var body: some View {
        ZStack {
            Color(uiColor: .systemBackground)
                .ignoresSafeArea()

            if isLoading {
                LoadingIndicator()
            } else if let card = playerCard {
                ScrollView {
                    VStack(alignment: .leading, spacing: 24) {
                        // Header with back arrow
                        HStack(alignment: .top, spacing: 10) {
                            Button(action: { dismiss() }) {
                                Image(systemName: "chevron.left")
                                    .font(.system(size: 22, weight: .medium))
                                    .foregroundStyle(lightBlue)
                            }
                            .padding(.top, 2)

                            VStack(alignment: .leading, spacing: 4) {
                                Text(card.name)
                                    .font(.system(.title2, design: .rounded, weight: .bold))
                                    .foregroundStyle(.primary)

                                // Full team name + age
                                HStack(spacing: 0) {
                                    Text(card.fullTeamName)
                                    if let age = card.age {
                                        Text("  \u{00B7}  Age \(age)")
                                    }
                                }
                                .font(.system(.subheadline, design: .rounded))
                                .foregroundStyle(.secondary)
                            }
                        }
                        .padding(.horizontal, 20)

                        // Current season
                        if let current = card.seasons.first {
                            sectionView(title: "\(String(current.year)) Season", grid: current.stats)

                            // Projected stats for current season
                            // TODO: Only show when teamGames < 162 (mid-season)
                            projectedStatsSection(season: current)

                            // Current season platoon splits
                            if let splits = current.platoonSplits {
                                sectionView(title: "Platoon Splits", grid: splits)
                            }

                            // Current season streaks
                            if let streaks = current.streaks {
                                sectionView(title: "Notable Streaks", grid: streaks)
                            }
                        }

                        // Career totals
                        if let career = card.careerTotals {
                            sectionView(title: "Career", grid: career)
                        }

                        // Prior seasons — expandable in place
                        let priorSeasons = Array(card.seasons.dropFirst())
                        if !priorSeasons.isEmpty {
                            VStack(alignment: .leading, spacing: 0) {
                                ForEach(priorSeasons, id: \.year) { season in
                                    let isExpanded = expandedSeasons.contains(season.year)

                                    Button {
                                        withAnimation(.easeInOut(duration: 0.2)) {
                                            if isExpanded {
                                                expandedSeasons.remove(season.year)
                                            } else {
                                                expandedSeasons.insert(season.year)
                                            }
                                        }
                                    } label: {
                                        HStack(spacing: 6) {
                                            Text("\(String(season.year)) Season")
                                                .font(.system(.headline, design: .rounded, weight: .semibold))
                                                .foregroundStyle(.primary)
                                            Image(systemName: isExpanded ? "chevron.up" : "chevron.down")
                                                .font(.system(size: 11, weight: .semibold))
                                                .foregroundStyle(.secondary)
                                        }
                                        .padding(.horizontal, 20)
                                        .padding(.vertical, 10)
                                    }
                                    .buttonStyle(.plain)

                                    if isExpanded {
                                        VStack(alignment: .leading, spacing: 16) {
                                            StatGridView(grid: season.stats)
                                                .padding(.horizontal, 6)

                                            if let splits = season.platoonSplits {
                                                sectionView(title: "Platoon Splits", grid: splits)
                                            }

                                            if let streaks = season.streaks {
                                                sectionView(title: "Notable Streaks", grid: streaks)
                                            }
                                        }
                                        .padding(.bottom, 8)
                                    }
                                }
                            }
                        }

                        // Bio
                        if let bio = card.bio {
                            VStack(alignment: .leading, spacing: 8) {
                                Text("About")
                                    .font(.system(.headline, design: .rounded, weight: .semibold))
                                    .foregroundStyle(.primary)
                                    .padding(.horizontal, 20)

                                Text(bio)
                                    .font(.system(.body, design: .rounded))
                                    .foregroundStyle(.primary.opacity(0.85))
                                    .lineSpacing(3)
                                    .padding(.horizontal, 20)
                                    .padding(.vertical, 12)
                                    .frame(maxWidth: .infinity, alignment: .leading)
                                    .background(
                                        RoundedRectangle(cornerRadius: 12)
                                            .fill(Color(uiColor: .secondarySystemBackground))
                                    )
                                    .padding(.horizontal, 6)
                            }
                        }
                    }
                    .padding(.top, 16)
                    .padding(.bottom, 24)
                }
            }
        }
        .navigationBarBackButtonHidden(true)
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .principal) {
                Button { dismiss() } label: {
                    HStack(spacing: 6) {
                        Text("StatChat")
                            .font(.system(.subheadline, weight: .semibold))
                            .foregroundStyle(
                                LinearGradient(
                                    colors: [lightBlue, deepBlue],
                                    startPoint: .leading, endPoint: .trailing
                                )
                            )

                        ZStack {
                            Image(systemName: "sparkle")
                                .font(.system(size: 12, weight: .bold))
                                .foregroundStyle(
                                    LinearGradient(
                                        colors: [lightBlue, deepBlue],
                                        startPoint: .topLeading, endPoint: .bottomTrailing
                                    )
                                )

                            Image(systemName: "baseball.fill")
                                .font(.system(size: 6))
                                .foregroundStyle(lightBlue)
                                .offset(x: 7.5, y: -7.5)

                            Image(systemName: "baseball.fill")
                                .font(.system(size: 4.5))
                                .foregroundStyle(lightBlue.opacity(0.7))
                                .offset(x: -6.5, y: -6.5)

                            Image(systemName: "baseball.fill")
                                .font(.system(size: 5))
                                .foregroundStyle(lightBlue.opacity(0.85))
                                .offset(x: 6.5, y: 6.5)
                        }
                    }
                }
            }
        }
        .gesture(
            DragGesture(minimumDistance: 20, coordinateSpace: .global)
                .onEnded { value in
                    if value.startLocation.x < 40 && value.translation.width > 80 {
                        dismiss()
                    }
                }
        )
        .task {
            playerCard = await PlayerCardService.fetch(name: playerName)
            isLoading = false
        }
    }

    private func sectionView(title: String, grid: StatGridParser.StatGrid) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title)
                .font(.system(.headline, design: .rounded, weight: .semibold))
                .foregroundStyle(.primary)
                .padding(.horizontal, 20)

            StatGridView(grid: grid)
                .padding(.horizontal, 6)
        }
    }

    private func projectedStatsSection(season: SeasonData) -> some View {
        let projected = buildProjectedGrid(season: season)

        return VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline, spacing: 12) {
                Text("Projected")
                    .font(.system(.headline, design: .rounded, weight: .semibold))
                    .foregroundStyle(.primary)

                // Tab toggle
                HStack(spacing: 0) {
                    ForEach(ProjectionMode.allCases, id: \.self) { mode in
                        Button {
                            withAnimation(.easeInOut(duration: 0.15)) {
                                projectionMode = mode
                            }
                        } label: {
                            Text(mode.rawValue)
                                .font(.system(.caption, design: .rounded, weight: .medium))
                                .padding(.horizontal, 10)
                                .padding(.vertical, 5)
                                .background(
                                    RoundedRectangle(cornerRadius: 6)
                                        .fill(projectionMode == mode
                                              ? deepBlue.opacity(0.12)
                                              : Color.clear)
                                )
                                .foregroundStyle(projectionMode == mode ? deepBlue : .secondary)
                        }
                    }
                }
            }
            .padding(.horizontal, 20)

            StatGridView(grid: projected)
                .padding(.horizontal, 6)
        }
    }

    private func buildProjectedGrid(season: SeasonData) -> StatGridParser.StatGrid {
        let countingStats = ["G", "PA", "AB", "R", "H", "2B", "3B", "HR", "RBI", "SB", "CS",
                             "BB", "IBB", "SO", "HBP", "SF"]
        let rateStats: Set<String> = ["AVG", "OBP", "SLG", "OPS", "ISO", "BABIP", "wRC+", "WAR"]

        let divisor: Double
        switch projectionMode {
        case .gamesMissed:
            // Project based on team games played so far
            divisor = Double(season.teamGames)
        case .fullSeason:
            // Project based on player's games played
            divisor = Double(season.games)
        }

        guard divisor > 0 else {
            return season.stats
        }

        let headers = season.stats.headers
        let originalValues = season.stats.rows.first?.values ?? []

        var projected: [String] = []
        for (idx, header) in headers.enumerated() {
            guard idx < originalValues.count else { break }
            let original = originalValues[idx]

            if countingStats.contains(header) {
                // Project counting stat: stat * 162 / divisor
                let raw = season.countingValues[header] ?? 0
                let proj = raw * 162.0 / divisor
                projected.append(String(Int(proj.rounded())))
            } else if header == "WAR" {
                // WAR projects like counting stats but with 1 decimal
                let raw = season.countingValues["WAR"] ?? 0
                let proj = raw * 162.0 / divisor
                projected.append(String(format: "%.1f", proj))
            } else {
                // Rate stats stay as-is
                projected.append(original)
            }
        }

        return StatGridParser.StatGrid(
            headers: headers,
            rows: [StatGridParser.StatGrid.Row(label: "", values: projected)]
        )
    }
}
