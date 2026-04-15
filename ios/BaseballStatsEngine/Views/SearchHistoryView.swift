import SwiftUI

struct SearchHistoryView: View {
    @Environment(AppState.self) private var appState
    @Binding var navigationPath: NavigationPath

    private let deepBlue = Color.brandDeepBlue
    private let lightBlue = Color(red: 0.45, green: 0.7, blue: 1.0)

    var body: some View {
        ScrollView {
            LazyVStack(spacing: 0) {
                ForEach(Array(appState.searchHistory.enumerated()), id: \.element) { index, query in
                    VStack(spacing: 0) {
                        Button {
                            appState.addToSearchHistory(query)
                            if let playerName = PlayerNameMatcher.matchPlayer(query) {
                                navigationPath.append(PlayerCardDestination(name: playerName, source: "search"))
                            } else if let teamCode = PlayerNameMatcher.matchTeamExact(query) {
                                navigationPath.append(TeamCardDestination(code: teamCode))
                            } else {
                                navigationPath.append(ResultsDestination(question: query))
                            }
                        } label: {
                            HStack(spacing: 12) {
                                Image(systemName: "magnifyingglass")
                                    .font(.system(size: 13))
                                    .foregroundStyle(.tertiary)

                                Text(query)
                                    .font(.system(.subheadline, design: .rounded))
                                    .foregroundStyle(.primary)
                                    .lineLimit(2)
                                    .multilineTextAlignment(.leading)

                                Spacer()

                                Button {
                                    withAnimation {
                                        appState.searchHistory.removeAll { $0 == query }
                                        UserDefaults.standard.set(appState.searchHistory, forKey: "searchHistory")
                                    }
                                } label: {
                                    Image(systemName: "xmark")
                                        .font(.system(size: 11, weight: .medium))
                                        .foregroundStyle(.tertiary)
                                }
                                .buttonStyle(.plain)
                            }
                            .padding(.horizontal, 4)
                            .padding(.vertical, 14)
                        }
                        .buttonStyle(.plain)

                        // Thin gradient separator
                        if index < appState.searchHistory.count - 1 {
                            LinearGradient(
                                colors: [lightBlue.opacity(0.4), deepBlue.opacity(0.4)],
                                startPoint: .leading, endPoint: .trailing
                            )
                            .frame(height: 1)
                            .clipShape(Capsule())
                        }
                    }
                }
            }
            .padding(.horizontal, 20)
            .padding(.top, 8)
        }
        .navigationTitle("Recent Searches")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            if !appState.searchHistory.isEmpty {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Clear") {
                        appState.clearSearchHistory()
                    }
                    .font(.system(.body, design: .rounded))
                    .foregroundStyle(.secondary)
                }
            }
        }
    }
}
