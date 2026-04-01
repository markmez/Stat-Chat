import SwiftUI

struct SearchHistoryView: View {
    @Environment(AppState.self) private var appState
    @Binding var navigationPath: NavigationPath

    var body: some View {
        List {
            ForEach(appState.searchHistory, id: \.self) { query in
                Button {
                    appState.addToSearchHistory(query)
                    if let playerName = PlayerNameMatcher.matchPlayer(query) {
                        navigationPath.append(PlayerCardDestination(name: playerName))
                    } else if let teamCode = PlayerNameMatcher.matchTeamExact(query) {
                        navigationPath.append(TeamCardDestination(code: teamCode))
                    } else {
                        navigationPath.append(ResultsDestination(question: query))
                    }
                } label: {
                    HStack {
                        Text(query)
                            .font(.system(.body, design: .rounded))
                            .foregroundStyle(.primary)
                            .lineLimit(2)
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
                }
            }
        }
        .listStyle(.plain)
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
