import Foundation

struct Suggestion: Sendable {
    let id: String
    let text: String
}

@MainActor
final class SuggestionEngine {

    static let shared = SuggestionEngine()

    private(set) var config: SuggestionConfig

    // Cached dynamic leader names (refreshed hourly)
    private var dynamicSuggestions: [Suggestion] = []
    private var dynamicLoadedAt: Date?

    // UserDefaults keys
    private let impressionsKey = "suggestion_impressions"
    private let tappedKey = "suggestion_tapped"
    private let lastResetKey = "suggestion_last_reset"
    private let configVersionKey = "suggestion_config_version"
    private let cachedConfigKey = "suggestion_cached_config"

    private let s3URL = URL(string: "https://stat-chat.s3.us-east-2.amazonaws.com/suggestions_config.json")!

    private init() {
        config = Self.loadBundled()
        if let cached = Self.loadCachedConfig(), cached.version > config.version {
            config = cached
        }
        resetMonthlyIfNeeded()
    }

    // MARK: - Config loading

    private static func loadBundled() -> SuggestionConfig {
        guard let url = Bundle.main.url(forResource: "suggestions_config", withExtension: "json"),
              let data = try? Data(contentsOf: url),
              let config = try? JSONDecoder().decode(SuggestionConfig.self, from: data) else {
            fatalError("suggestions_config.json missing or invalid in bundle")
        }
        return config
    }

    private static func loadCachedConfig() -> SuggestionConfig? {
        guard let data = UserDefaults.standard.data(forKey: "suggestion_cached_config"),
              let config = try? JSONDecoder().decode(SuggestionConfig.self, from: data) else { return nil }
        return config
    }

    func checkForRemoteUpdate() async {
        var request = URLRequest(url: s3URL)
        request.timeoutInterval = 5
        guard let (data, response) = try? await URLSession.shared.data(for: request),
              let http = response as? HTTPURLResponse, http.statusCode == 200,
              let remote = try? JSONDecoder().decode(SuggestionConfig.self, from: data),
              remote.version > config.version else { return }

        config = remote
        UserDefaults.standard.set(data, forKey: cachedConfigKey)

        let storedVersion = UserDefaults.standard.integer(forKey: configVersionKey)
        if remote.version > storedVersion {
            UserDefaults.standard.removeObject(forKey: tappedKey)
            UserDefaults.standard.set(remote.version, forKey: configVersionKey)
        }
    }

    // MARK: - Impression/tap tracking

    private var impressions: [String: Int] {
        get { UserDefaults.standard.dictionary(forKey: impressionsKey) as? [String: Int] ?? [:] }
        set { UserDefaults.standard.set(newValue, forKey: impressionsKey) }
    }

    private var tapped: Set<String> {
        get { Set(UserDefaults.standard.stringArray(forKey: tappedKey) ?? []) }
        set { UserDefaults.standard.set(Array(newValue), forKey: tappedKey) }
    }

    func recordImpression(_ id: String) {
        var imps = impressions
        imps[id, default: 0] += 1
        impressions = imps
    }

    func recordTap(_ id: String) {
        var t = tapped
        t.insert(id)
        tapped = t
    }

    private func shouldShow(_ id: String) -> Bool {
        if tapped.contains(id) { return false }
        if (impressions[id] ?? 0) >= config.algorithm.impressionThreshold { return false }
        return true
    }

    private func resetMonthlyIfNeeded() {
        let now = Date()
        let calendar = Calendar.current
        if let lastReset = UserDefaults.standard.object(forKey: lastResetKey) as? Date {
            let lastMonth = calendar.component(.month, from: lastReset)
            let currentMonth = calendar.component(.month, from: now)
            if lastMonth == currentMonth { return }
        }
        UserDefaults.standard.removeObject(forKey: impressionsKey)
        UserDefaults.standard.set(now, forKey: lastResetKey)
    }

    // MARK: - Build pool

    func buildPool(searchHistory: [String]) -> [Suggestion] {
        let algo = config.algorithm

        // 1. Dynamic tier
        loadDynamicLeadersIfNeeded()
        let dynamic = dynamicSuggestions.filter { shouldShow($0.id) }
        let dynamicPick = Array(dynamic.shuffled().prefix(algo.dynamicSlots))

        // 2. Personalized tier
        let personalized = buildPersonalized(searchHistory: searchHistory)
            .filter { shouldShow($0.id) }
        let personalizedPick = Array(personalized.shuffled().prefix(algo.personalizedSlots))

        // 3. Defaults tier
        let defaults = buildDefaults()
        let defaultsPick = Array(defaults.shuffled().prefix(algo.defaultSlots))

        // Merge — if any tier underperforms, fill from others
        var pool = dynamicPick + personalizedPick + defaultsPick
        let target = algo.poolSize

        if pool.count < target {
            let usedIds = Set(pool.map(\.id))
            let overflow = (dynamic + personalized + defaults)
                .filter { !usedIds.contains($0.id) && shouldShow($0.id) }
                .shuffled()
            pool.append(contentsOf: overflow.prefix(target - pool.count))
        }

        // Last resort fallback for first-time users
        if pool.count < target {
            let usedIds = Set(pool.map(\.id))
            let fallback = config.defaults
                .filter { !usedIds.contains($0.id) }
                .map { Suggestion(id: $0.id, text: $0.text) }
            pool.append(contentsOf: fallback.prefix(target - pool.count))
        }

        return Array(pool.prefix(target)).shuffled()
    }

    // MARK: - Defaults tier

    private func buildDefaults() -> [Suggestion] {
        // Weighted selection: higher weight = more copies in the pool
        var weighted: [Suggestion] = []
        for d in config.defaults where shouldShow(d.id) {
            let copies = max(1, Int(d.weight * 10))
            for _ in 0..<copies {
                weighted.append(Suggestion(id: d.id, text: d.text))
            }
        }
        // Deduplicate after shuffle (pick unique IDs)
        var seen: Set<String> = []
        var result: [Suggestion] = []
        for s in weighted.shuffled() {
            if seen.insert(s.id).inserted {
                result.append(s)
            }
        }
        return result
    }

    // MARK: - Dynamic tier (in-season leaders)

    private func loadDynamicLeadersIfNeeded() {
        if let loadedAt = dynamicLoadedAt, Date().timeIntervalSince(loadedAt) < 3600 {
            return // cache valid for 1 hour
        }

        let db = DatabaseService()

        // Find the most recent season
        guard let seasonResult = try? db.execute(sql: "SELECT MAX(season) FROM season_batting_stats"),
              let seasonRow = seasonResult.rows.first,
              let season = Int(seasonRow[0]) else { return }

        let currentYear = Calendar.current.component(.year, from: Date())
        let seasonLabel = season == currentYear ? "this season" : "last season"

        var suggestions: [Suggestion] = []
        let allQueries = config.dynamicQueries.batting + config.dynamicQueries.pitching

        for query in allQueries {
            let sql = query.sql.replacingOccurrences(of: "{season}", with: String(season))
            guard let result = try? db.execute(sql: sql) else { continue }
            for row in result.rows {
                let name = row[0]
                guard let template = query.templates.randomElement() else { continue }
                let text = template
                    .replacingOccurrences(of: "{player}", with: name)
                    .replacingOccurrences(of: "{seasonLabel}", with: seasonLabel)
                let id = "dyn_\(abs(text.hashValue))"
                suggestions.append(Suggestion(id: id, text: text))
            }
        }

        dynamicSuggestions = suggestions
        dynamicLoadedAt = Date()
    }

    // MARK: - Personalized tier

    private enum Category: String, CaseIterable {
        case streak, comparison, splits, leaderboard, statExplanation, playerLookup, homeAway, milestone
    }

    private func buildPersonalized(searchHistory: [String]) -> [Suggestion] {
        guard !searchHistory.isEmpty else { return [] }

        let searchedPlayers = extractSearchedPlayers(from: searchHistory)
        let usedCategories = detectUsedCategories(in: searchHistory)
        let teamCounts = countTeams(from: searchedPlayers, history: searchHistory)

        let topTeams = teamCounts.sorted { $0.value > $1.value }.prefix(2).map(\.key)
        let searchedNames = Set(searchedPlayers.map { $0.lowercased() })
        var teammates: [String] = []
        for team in topTeams {
            teammates.append(contentsOf: topPlayersForTeam(team, excluding: searchedNames).prefix(3))
        }

        let leagueStars = topLeagueStars(excluding: searchedNames)
        let allPlayers = searchedPlayers + teammates + leagueStars
        let unusedCategories = Set(Category.allCases).subtracting(usedCategories)

        var selected: [Suggestion] = []
        var usedTexts: Set<String> = []

        // Discovery: untried categories
        for category in unusedCategories.shuffled().prefix(3) {
            if let s = generateForCategory(category, players: allPlayers, usedTexts: &usedTexts) {
                selected.append(s)
            }
        }

        // Searched players in untried categories
        for player in searchedPlayers.prefix(4) {
            let playerCats = detectPlayerCategories(player: player, in: searchHistory)
            let untried = Set(Category.allCases).subtracting(playerCats)
            if let cat = untried.shuffled().first,
               let s = generateForPlayer(player, category: cat, allPlayers: allPlayers, usedTexts: &usedTexts) {
                selected.append(s)
            }
        }

        // League stars and teammates
        let discoveryPlayers = (leagueStars.shuffled().prefix(2) + teammates.shuffled().prefix(1))
        for player in discoveryPlayers {
            let cat: Category = [.playerLookup, .splits, .streak, .homeAway].randomElement()!
            if let s = generateForPlayer(player, category: cat, allPlayers: allPlayers, usedTexts: &usedTexts) {
                selected.append(s)
            }
        }

        return selected
    }

    // MARK: - Player/team extraction

    private func extractSearchedPlayers(from history: [String]) -> [String] {
        var players: [String] = []
        var seen: Set<String> = []
        for query in history {
            let cleaned = query.contains("→") ? String(query.split(separator: "→").last ?? Substring(query)) : query
            if let name = PlayerNameMatcher.matchPlayer(cleaned.trimmingCharacters(in: .whitespaces)),
               !seen.contains(name.lowercased()) {
                players.append(name)
                seen.insert(name.lowercased())
            }
        }
        return players
    }

    private func countTeams(from players: [String], history: [String]) -> [String: Int] {
        var counts: [String: Int] = [:]
        let db = DatabaseService()
        for player in players {
            let sanitized = player.replacingOccurrences(of: "'", with: "''")
            if let result = try? db.execute(sql: "SELECT team FROM players WHERE name = '\(sanitized)' LIMIT 1"),
               let row = result.rows.first, !row[0].isEmpty {
                let team = String(row[0].split(separator: "/").last ?? Substring(row[0]))
                counts[team, default: 0] += 1
            }
        }
        for query in history {
            let lower = query.lowercased()
            for (aliases, code) in Self.teamAliases {
                if aliases.contains(where: { lower.contains($0) }) {
                    counts[code, default: 0] += 1
                }
            }
        }
        return counts
    }

    private func topPlayersForTeam(_ teamCode: String, excluding: Set<String>) -> [String] {
        let db = DatabaseService()
        var players: [String] = []

        let batterSql = """
            SELECT p.name FROM season_batting_stats s
            JOIN players p ON s.player_id = p.player_id
            WHERE s.team LIKE '%\(teamCode)%'
              AND s.season = (SELECT MAX(season) FROM season_batting_stats)
              AND s.at_bats >= 100
            ORDER BY s.ops DESC LIMIT 5
            """
        if let result = try? db.execute(sql: batterSql) {
            for row in result.rows where !excluding.contains(row[0].lowercased()) {
                players.append(row[0])
            }
        }

        let pitcherSql = """
            SELECT p.name FROM season_pitching_stats sp
            JOIN players p ON sp.player_id = p.player_id
            WHERE sp.team LIKE '%\(teamCode)%'
              AND sp.season = (SELECT MAX(season) FROM season_pitching_stats)
              AND sp.games_started >= 10
            ORDER BY sp.era ASC LIMIT 2
            """
        if let result = try? db.execute(sql: pitcherSql) {
            for row in result.rows where !excluding.contains(row[0].lowercased()) {
                players.append(row[0])
            }
        }

        return players
    }

    private func topLeagueStars(excluding: Set<String>) -> [String] {
        let db = DatabaseService()
        var stars: [String] = []
        let sql = """
            SELECT p.name FROM season_batting_stats s
            JOIN players p ON s.player_id = p.player_id
            WHERE s.season = (SELECT MAX(season) FROM season_batting_stats)
              AND s.at_bats >= 400
            ORDER BY s.ops DESC LIMIT 15
            """
        if let result = try? db.execute(sql: sql) {
            for row in result.rows where !excluding.contains(row[0].lowercased()) {
                stars.append(row[0])
            }
        }
        return Array(stars.prefix(8))
    }

    // MARK: - Category detection

    private func detectUsedCategories(in history: [String]) -> Set<Category> {
        var used: Set<Category> = []
        for query in history {
            let lower = query.lowercased()
            if ["streak", "hot", "cold", "slump", "fire"].contains(where: { lower.contains($0) }) { used.insert(.streak) }
            if [" vs ", " vs. ", " or ", "compare", "versus"].contains(where: { lower.contains($0) }) { used.insert(.comparison) }
            if ["lefties", "righties", "platoon", "splits", "left-handed", "right-handed"].contains(where: { lower.contains($0) }) { used.insert(.splits) }
            if ["home", "away", "road"].contains(where: { lower.contains($0) }) { used.insert(.homeAway) }
            if ["leaders", "top ", "most ", "best ", "highest", "lowest", "who led"].contains(where: { lower.contains($0) }) { used.insert(.leaderboard) }
            if ["what is", "what's", "explain", "what does"].contains(where: { lower.contains($0) }) { used.insert(.statExplanation) }
            if ["how many times", "has anyone ever", "how many players"].contains(where: { lower.contains($0) }) { used.insert(.milestone) }
            if PlayerNameMatcher.matchPlayer(query) != nil { used.insert(.playerLookup) }
        }
        return used
    }

    private func detectPlayerCategories(player: String, in history: [String]) -> Set<Category> {
        var used: Set<Category> = []
        let playerLower = player.lowercased()
        for query in history {
            let lower = query.lowercased()
            guard lower.contains(playerLower) || lower.contains(player.split(separator: " ").last?.lowercased() ?? "") else { continue }
            if ["streak", "hot", "cold", "slump"].contains(where: { lower.contains($0) }) { used.insert(.streak) }
            if [" vs ", "compare", "versus"].contains(where: { lower.contains($0) }) { used.insert(.comparison) }
            if ["lefties", "righties", "platoon", "splits"].contains(where: { lower.contains($0) }) { used.insert(.splits) }
            if ["home", "away", "road"].contains(where: { lower.contains($0) }) { used.insert(.homeAway) }
            used.insert(.playerLookup)
        }
        return used
    }

    // MARK: - Query generation

    private func generateForCategory(_ category: Category, players: [String], usedTexts: inout Set<String>) -> Suggestion? {
        switch category {
        case .leaderboard:
            let pool = config.defaults.filter { $0.text.lowercased().contains("top ") || $0.text.lowercased().contains("who led") || $0.text.lowercased().contains("who had the") || $0.text.lowercased().contains("lowest") }
            return pickUnused(from: pool.map(\.text), ids: pool.map(\.id), usedTexts: &usedTexts)
        case .milestone:
            let pool = config.defaults.filter { $0.text.lowercased().contains("how many times") || $0.text.lowercased().contains("how many players") || $0.text.lowercased().contains("closest to") }
            return pickUnused(from: pool.map(\.text), ids: pool.map(\.id), usedTexts: &usedTexts)
        case .statExplanation:
            let pool = config.defaults.filter { $0.text.lowercased().contains("what is") || $0.text.lowercased().contains("what does") }
            return pickUnused(from: pool.map(\.text), ids: pool.map(\.id), usedTexts: &usedTexts)
        default:
            if let player = players.randomElement() {
                return generateForPlayer(player, category: category, allPlayers: players, usedTexts: &usedTexts)
            }
            return nil
        }
    }

    private func generateForPlayer(_ player: String, category: Category, allPlayers: [String], usedTexts: inout Set<String>) -> Suggestion? {
        let historical = !isCurrentPlayer(player)
        let lastYear = historical ? lastSeasonYear(for: player) : nil
        let yearStr = lastYear.map { String($0) } ?? "his career"
        let t = config.templates

        let templates: [String]
        switch category {
        case .streak: templates = historical ? t.historical.streak : t.current.streak
        case .splits: templates = historical ? t.historical.splits : t.current.splits
        case .homeAway: templates = historical ? t.historical.homeAway : t.current.homeAway
        case .playerLookup: templates = historical ? t.historical.playerLookup : t.current.playerLookup
        case .comparison:
            let others = allPlayers.filter { $0 != player }
            guard let other = others.randomElement() else { return nil }
            let pool = historical ? t.historical.comparison : t.current.comparison
            guard let template = pool.randomElement() else { return nil }
            let text = template
                .replacingOccurrences(of: "{player1}", with: player)
                .replacingOccurrences(of: "{player2}", with: other)
            if usedTexts.contains(text) { return nil }
            usedTexts.insert(text)
            return Suggestion(id: "p_\(abs(text.hashValue))", text: text)
        case .leaderboard, .milestone, .statExplanation:
            return generateForCategory(category, players: allPlayers, usedTexts: &usedTexts)
        }

        guard let template = templates.randomElement() else { return nil }
        let text = template
            .replacingOccurrences(of: "{player}", with: player)
            .replacingOccurrences(of: "{year}", with: yearStr)
        if usedTexts.contains(text) { return nil }
        usedTexts.insert(text)
        return Suggestion(id: "p_\(abs(text.hashValue))", text: text)
    }

    private func pickUnused(from texts: [String], ids: [String], usedTexts: inout Set<String>) -> Suggestion? {
        let pairs = zip(ids, texts).filter { !usedTexts.contains($0.1) }
        guard let pick = pairs.shuffled().first else { return nil }
        usedTexts.insert(pick.1)
        return Suggestion(id: pick.0, text: pick.1)
    }

    // MARK: - Historical player detection

    private func lastSeasonYear(for player: String) -> Int? {
        let db = DatabaseService()
        let sanitized = player.replacingOccurrences(of: "'", with: "''")
        if let result = try? db.execute(sql: """
            SELECT MAX(season) FROM season_batting_stats s
            JOIN players p ON s.player_id = p.player_id
            WHERE p.name = '\(sanitized)'
            """),
           let row = result.rows.first, let year = Int(row[0]) {
            return year
        }
        if let result = try? db.execute(sql: """
            SELECT MAX(season) FROM season_pitching_stats sp
            JOIN players p ON sp.player_id = p.player_id
            WHERE p.name = '\(sanitized)'
            """),
           let row = result.rows.first, let year = Int(row[0]) {
            return year
        }
        return nil
    }

    private func isCurrentPlayer(_ player: String) -> Bool {
        guard let lastYear = lastSeasonYear(for: player) else { return true }
        let currentYear = Calendar.current.component(.year, from: Date())
        return lastYear >= currentYear - 1
    }

    // MARK: - Team aliases

    private static let teamAliases: [([String], String)] = [
        (["yankees", "yanks", "nyy"], "NYA"),
        (["mets", "nym"], "NYN"),
        (["dodgers", "lad"], "LAN"),
        (["red sox", "boston", "bos"], "BOS"),
        (["cubs", "chc"], "CHN"),
        (["white sox", "chw"], "CHA"),
        (["astros", "houston", "hou"], "HOU"),
        (["braves", "atlanta", "atl"], "ATL"),
        (["phillies", "philadelphia", "phi"], "PHI"),
        (["padres", "san diego", "sdp"], "SDN"),
        (["rangers", "texas", "tex"], "TEX"),
        (["blue jays", "toronto", "tor"], "TOR"),
        (["orioles", "baltimore", "bal"], "BAL"),
        (["twins", "minnesota", "min"], "MIN"),
        (["guardians", "cleveland", "cle"], "CLE"),
        (["mariners", "seattle", "sea"], "SEA"),
        (["rays", "tampa", "tbr"], "TBA"),
        (["angels", "anaheim", "laa"], "ANA"),
        (["giants", "san francisco", "sfg"], "SFN"),
        (["cardinals", "st. louis", "stl"], "SLN"),
        (["brewers", "milwaukee", "mil"], "MIL"),
        (["reds", "cincinnati", "cin"], "CIN"),
        (["pirates", "pittsburgh", "pit"], "PIT"),
        (["royals", "kansas city", "kcr"], "KCA"),
        (["tigers", "detroit", "det"], "DET"),
        (["diamondbacks", "arizona", "ari"], "ARI"),
        (["rockies", "colorado", "col"], "COL"),
        (["nationals", "washington", "wsh"], "WAS"),
        (["marlins", "miami", "mia"], "MIA"),
        (["athletics", "oakland", "oak"], "OAK"),
    ]
}
