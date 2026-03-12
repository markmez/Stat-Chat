import Foundation

@MainActor
enum SampleQuery {

    enum Category: String, CaseIterable {
        case streak
        case comparison
        case splits
        case leaderboard
        case statExplanation
        case playerLookup
        case homeAway
        case milestone
    }

    // MARK: - Templates for dynamic generation (current players)

    private static let streakTemplates = [
        "Did {player} have any hot streaks last season?",
        "When was {player}'s coldest stretch last season?",
        "Did {player} go on any hot runs last season?",
    ]

    private static let comparisonTemplates = [
        "{player1} vs {player2} last season",
        "Compare {player1} and {player2} last season",
    ]

    private static let splitsTemplates = [
        "How did {player} hit against lefties last season?",
        "{player} platoon splits last season",
        "{player}'s splits vs left-handed pitching last season",
    ]

    private static let homeAwayTemplates = [
        "{player} home vs away splits last season",
        "How did {player} hit at home last season?",
    ]

    private static let playerLookupTemplates = [
        "How did {player} do last season?",
        "What was {player}'s slash line last season?",
        "How did {player} hit in July last season?",
    ]

    // MARK: - Templates for historical players (no longer active)

    private static let historicalStreakTemplates = [
        "Did {player} have any hot streaks in {year}?",
        "When was {player}'s coldest stretch in {year}?",
    ]

    private static let historicalComparisonTemplates = [
        "{player1} vs {player2} career stats",
        "Compare {player1} and {player2}",
    ]

    private static let historicalSplitsTemplates = [
        "How did {player} hit against lefties in his career?",
        "{player} career platoon splits",
    ]

    private static let historicalHomeAwayTemplates = [
        "{player} career home vs away splits",
        "How did {player} hit at home in his career?",
    ]

    private static let historicalPlayerLookupTemplates = [
        "How did {player} do in {year}?",
        "{player} career stats",
        "How did {player} hit in September {year}?",
    ]

    // These don't need player names
    private static let leaderboardQueries = [
        "Who led the league in home runs last season?",
        "Top 10 in OPS last season",
        "Who had the most stolen bases last season?",
        "Who hit the most doubles last season?",
        "Who had the highest OPS+ last season?",
        "Lowest ERA among starters last season",
        "Top 5 in batting average last season",
    ]

    private static let milestoneQueries = [
        "How many times has someone hit 50 home runs?",
        "Who has come closest to hitting .400 since Ted Williams?",
        "How many players have stolen 60 bases in a season?",
    ]

    private static let statExplanationQueries = [
        "What is WAR?",
        "What is OPS+?",
        "What does BABIP measure?",
        "What is ISO?",
    ]

    // MARK: - Dynamic personalization

    /// Build a personalized list of sample queries based on search history.
    static func personalized(from history: [String], count: Int = 12) -> [String] {
        let searchedPlayers = extractSearchedPlayers(from: history)
        let usedCategories = detectUsedCategories(in: history)
        let teamCounts = countTeams(from: searchedPlayers, history: history)

        // Find teammate suggestions for top teams (limited)
        let topTeams = teamCounts.sorted { $0.value > $1.value }.prefix(2).map(\.key)
        let searchedNames = Set(searchedPlayers.map { $0.lowercased() })
        var teammates: [String] = []
        for team in topTeams {
            let stars = topPlayersForTeam(team, excluding: searchedNames)
            teammates.append(contentsOf: stars.prefix(3))
        }

        // Add league-wide stars the user hasn't searched
        let leagueStars = topLeagueStars(excluding: searchedNames)

        // Build the combined player pool: searched players + some teammates + league stars
        let allPlayers = searchedPlayers + teammates + leagueStars
        let unusedCategories = Set(Category.allCases).subtracting(usedCategories)

        var selected: [String] = []
        var usedTexts: Set<String> = []

        // 1. Discovery: one suggestion for each untried category (up to 3)
        for category in unusedCategories.shuffled().prefix(3) {
            if let query = generateForCategory(category, players: allPlayers, searched: searchedPlayers, usedTexts: &usedTexts) {
                selected.append(query)
            }
        }

        // 2. Suggestions using searched players in categories they haven't tried with that player
        for player in searchedPlayers.prefix(4) {
            let playerCategories = detectPlayerCategories(player: player, in: history)
            let untried = Set(Category.allCases).subtracting(playerCategories)
            if let cat = untried.shuffled().first,
               let query = generateForPlayer(player, category: cat, allPlayers: allPlayers, usedTexts: &usedTexts) {
                selected.append(query)
            }
        }

        // 3. League stars the user hasn't looked at (mix with 1 teammate)
        let discoveryPlayers = (leagueStars.shuffled().prefix(2) + teammates.shuffled().prefix(1))
        for player in discoveryPlayers {
            let cat: Category = [.playerLookup, .splits, .streak, .homeAway].randomElement()!
            if let query = generateForPlayer(player, category: cat, allPlayers: allPlayers, usedTexts: &usedTexts) {
                selected.append(query)
            }
        }

        // 4. Fill remaining with variety
        let remaining = count - selected.count
        if remaining > 0 {
            let fillers = generateFillers(count: remaining, players: allPlayers, usedTexts: &usedTexts)
            selected.append(contentsOf: fillers)
        }

        return Array(selected.prefix(count)).shuffled()
    }

    // MARK: - Player/team extraction from history

    private static func extractSearchedPlayers(from history: [String]) -> [String] {
        var players: [String] = []
        var seen: Set<String> = []
        for query in history {
            // Strip context prefix (e.g., "Aaron Judge → how did he do")
            let cleaned = query.contains("→") ? String(query.split(separator: "→").last ?? Substring(query)) : query
            if let name = PlayerNameMatcher.matchPlayer(cleaned.trimmingCharacters(in: .whitespaces)),
               !seen.contains(name.lowercased()) {
                players.append(name)
                seen.insert(name.lowercased())
            }
        }
        return players
    }

    private static func countTeams(from players: [String], history: [String]) -> [String: Int] {
        var counts: [String: Int] = [:]
        let db = DatabaseService()
        for player in players {
            let sanitized = player.replacingOccurrences(of: "'", with: "''")
            if let result = try? db.execute(sql: "SELECT team FROM players WHERE name LIKE '%\(sanitized)%' LIMIT 1"),
               let row = result.rows.first, !row[0].isEmpty {
                // Handle multi-team entries like "NYA/TOR" — use most recent
                let team = String(row[0].split(separator: "/").last ?? Substring(row[0]))
                counts[team, default: 0] += 1
            }
        }
        // Also check team names directly in history
        for query in history {
            let lower = query.lowercased()
            for (aliases, code) in teamAliases {
                if aliases.contains(where: { lower.contains($0) }) {
                    counts[code, default: 0] += 1
                }
            }
        }
        return counts
    }

    /// Top batters by OPS and top pitchers by ERA for a team in the most recent season.
    private static func topPlayersForTeam(_ teamCode: String, excluding: Set<String>) -> [String] {
        let db = DatabaseService()
        var players: [String] = []

        // Top batters by OPS (min 100 AB)
        let batterSql = """
            SELECT p.name FROM season_batting_stats s
            JOIN players p ON s.player_id = p.player_id
            WHERE s.team LIKE '%\(teamCode)%'
              AND s.season = (SELECT MAX(season) FROM season_batting_stats)
              AND s.at_bats >= 100
            ORDER BY s.ops DESC LIMIT 5
            """
        if let result = try? db.execute(sql: batterSql) {
            for row in result.rows {
                let name = row[0]
                if !excluding.contains(name.lowercased()) {
                    players.append(name)
                }
            }
        }

        // Top pitchers by ERA (min 10 GS)
        let pitcherSql = """
            SELECT p.name FROM season_pitching_stats sp
            JOIN players p ON sp.player_id = p.player_id
            WHERE sp.team LIKE '%\(teamCode)%'
              AND sp.season = (SELECT MAX(season) FROM season_pitching_stats)
              AND sp.games_started >= 10
            ORDER BY sp.era ASC LIMIT 2
            """
        if let result = try? db.execute(sql: pitcherSql) {
            for row in result.rows {
                let name = row[0]
                if !excluding.contains(name.lowercased()) {
                    players.append(name)
                }
            }
        }

        return players
    }

    /// Top league-wide stars from the most recent season, excluding already-searched players.
    private static func topLeagueStars(excluding: Set<String>) -> [String] {
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
            for row in result.rows {
                let name = row[0]
                if !excluding.contains(name.lowercased()) {
                    stars.append(name)
                }
            }
        }
        return Array(stars.prefix(8))
    }

    // MARK: - Category detection

    private static func detectUsedCategories(in history: [String]) -> Set<Category> {
        var used: Set<Category> = []
        for query in history {
            let lower = query.lowercased()
            if ["streak", "hot", "cold", "slump", "fire"].contains(where: { lower.contains($0) }) {
                used.insert(.streak)
            }
            if [" vs ", " vs. ", " or ", "compare", "versus"].contains(where: { lower.contains($0) }) {
                used.insert(.comparison)
            }
            if ["lefties", "righties", "platoon", "splits", "left-handed", "right-handed"].contains(where: { lower.contains($0) }) {
                used.insert(.splits)
            }
            if ["home", "away", "road"].contains(where: { lower.contains($0) }) {
                used.insert(.homeAway)
            }
            if ["leaders", "top ", "most ", "best ", "highest", "lowest", "who led"].contains(where: { lower.contains($0) }) {
                used.insert(.leaderboard)
            }
            if ["what is", "what's", "explain", "what does"].contains(where: { lower.contains($0) }) {
                used.insert(.statExplanation)
            }
            if ["how many times", "has anyone ever", "how many players"].contains(where: { lower.contains($0) }) {
                used.insert(.milestone)
            }
            if PlayerNameMatcher.matchPlayer(query) != nil {
                used.insert(.playerLookup)
            }
        }
        return used
    }

    /// Detect which categories have been used for a specific player.
    private static func detectPlayerCategories(player: String, in history: [String]) -> Set<Category> {
        var used: Set<Category> = []
        let playerLower = player.lowercased()
        for query in history {
            let lower = query.lowercased()
            guard lower.contains(playerLower) || lower.contains(player.split(separator: " ").last?.lowercased() ?? "") else { continue }
            if ["streak", "hot", "cold", "slump"].contains(where: { lower.contains($0) }) { used.insert(.streak) }
            if [" vs ", "compare", "versus"].contains(where: { lower.contains($0) }) { used.insert(.comparison) }
            if ["lefties", "righties", "platoon", "splits"].contains(where: { lower.contains($0) }) { used.insert(.splits) }
            if ["home", "away", "road"].contains(where: { lower.contains($0) }) { used.insert(.homeAway) }
            // A plain player name search counts as playerLookup
            used.insert(.playerLookup)
        }
        return used
    }

    // MARK: - Query generation

    private static func generateForCategory(_ category: Category, players: [String], searched: [String], usedTexts: inout Set<String>) -> String? {
        switch category {
        case .leaderboard:
            return pickUnused(from: leaderboardQueries, usedTexts: &usedTexts)
        case .milestone:
            return pickUnused(from: milestoneQueries, usedTexts: &usedTexts)
        case .statExplanation:
            return pickUnused(from: statExplanationQueries, usedTexts: &usedTexts)
        default:
            if let player = players.randomElement() {
                return generateForPlayer(player, category: category, allPlayers: players, usedTexts: &usedTexts)
            }
            return nil
        }
    }

    private static func generateForPlayer(_ player: String, category: Category, allPlayers: [String], usedTexts: inout Set<String>) -> String? {
        let historical = !isCurrentPlayer(player)
        let lastYear = historical ? lastSeasonYear(for: player) : nil
        let yearStr = lastYear.map { String($0) } ?? "his career"

        let templates: [String]
        switch category {
        case .streak: templates = historical ? historicalStreakTemplates : streakTemplates
        case .splits: templates = historical ? historicalSplitsTemplates : splitsTemplates
        case .homeAway: templates = historical ? historicalHomeAwayTemplates : homeAwayTemplates
        case .playerLookup: templates = historical ? historicalPlayerLookupTemplates : playerLookupTemplates
        case .comparison:
            // Need a second player
            let others = allPlayers.filter { $0 != player }
            guard let other = others.randomElement() else { return nil }
            let pool = historical ? historicalComparisonTemplates : comparisonTemplates
            let template = pool.randomElement()!
            let query = template
                .replacingOccurrences(of: "{player1}", with: player)
                .replacingOccurrences(of: "{player2}", with: other)
            if usedTexts.contains(query) { return nil }
            usedTexts.insert(query)
            return query
        case .leaderboard:
            return pickUnused(from: leaderboardQueries, usedTexts: &usedTexts)
        case .milestone:
            return pickUnused(from: milestoneQueries, usedTexts: &usedTexts)
        case .statExplanation:
            return pickUnused(from: statExplanationQueries, usedTexts: &usedTexts)
        }

        guard let template = templates.randomElement() else { return nil }
        let query = template
            .replacingOccurrences(of: "{player}", with: player)
            .replacingOccurrences(of: "{year}", with: yearStr)
        if usedTexts.contains(query) { return nil }
        usedTexts.insert(query)
        return query
    }

    private static func generateFillers(count: Int, players: [String], usedTexts: inout Set<String>) -> [String] {
        var fillers: [String] = []
        let categories = Category.allCases.shuffled()
        var attempts = 0
        while fillers.count < count && attempts < count * 4 {
            let cat = categories[attempts % categories.count]
            if let query = generateForCategory(cat, players: players, searched: players, usedTexts: &usedTexts) {
                fillers.append(query)
            }
            attempts += 1
        }
        return fillers
    }

    private static func pickUnused(from pool: [String], usedTexts: inout Set<String>) -> String? {
        let available = pool.filter { !usedTexts.contains($0) }
        guard let pick = available.randomElement() else { return nil }
        usedTexts.insert(pick)
        return pick
    }

    // MARK: - Historical player detection

    /// Returns the player's most recent season year, or nil if not found.
    private static func lastSeasonYear(for player: String) -> Int? {
        let db = DatabaseService()
        let sanitized = player.replacingOccurrences(of: "'", with: "''")
        if let result = try? db.execute(sql: """
            SELECT MAX(season) FROM season_batting_stats s
            JOIN players p ON s.player_id = p.player_id
            WHERE p.name LIKE '%\(sanitized)%'
            """),
           let row = result.rows.first, let year = Int(row[0]) {
            return year
        }
        // Try pitching
        if let result = try? db.execute(sql: """
            SELECT MAX(season) FROM season_pitching_stats sp
            JOIN players p ON sp.player_id = p.player_id
            WHERE p.name LIKE '%\(sanitized)%'
            """),
           let row = result.rows.first, let year = Int(row[0]) {
            return year
        }
        return nil
    }

    /// A player is "current" if they played within the last 2 years.
    private static func isCurrentPlayer(_ player: String) -> Bool {
        guard let lastYear = lastSeasonYear(for: player) else { return true } // assume current if unknown
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

    // MARK: - Fallback static list (for first-time users with no history)

    static let all: [String] = [
        "Did Marcus Semien have any hot streaks last season?",
        "Who led the league in home runs last season?",
        "What is OPS+?",
        "Who had the most stolen bases last season?",
        "How did Juan Soto do last year?",
        "Top 10 in OPS last season",
        "How did Yordan Alvarez hit against lefties last season?",
        "Compare Lindor and Bobby Witt Jr last season",
        "How many home runs did Ohtani hit last season?",
        "When was Bryce Harper's coldest stretch last season?",
        "Top 5 in batting average last season",
        "Judge home vs away last season",
        "Who has come closest to hitting .400 since Ted Williams?",
        "Trea Turner vs Gunnar Henderson last season",
        "Kyle Tucker's splits vs left-handed pitching last season",
        "How did Juan Soto hit in August last season?",
        "Judge with runners in scoring position last season",
    ]
}
