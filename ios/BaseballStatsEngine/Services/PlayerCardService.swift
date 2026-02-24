import Foundation

struct SeasonData: Sendable {
    let year: Int
    let team: String
    let age: Int
    let games: Int
    let teamGames: Int
    let stats: StatGridParser.StatGrid
    /// Raw counting stat values (G, PA, AB, R, H, 2B, 3B, HR, RBI, SB, CS, BB, IBB, SO, HBP, SF) + WAR
    let countingValues: [String: Double]
    let platoonSplits: StatGridParser.StatGrid?
    let streaks: StatGridParser.StatGrid?
}

struct PlayerCard: Sendable {
    let name: String
    let team: String
    let fullTeamName: String
    let age: Int?
    let seasons: [SeasonData]
    let careerTotals: StatGridParser.StatGrid?
    let platoonSplits: StatGridParser.StatGrid?
    let streaks: StatGridParser.StatGrid?
    let bio: String?
}

@MainActor
enum PlayerCardService {

    private static let db = DatabaseService()

    // All 24 stats in conventional order
    private static let allHeaders = [
        "G", "PA", "AB", "R", "H", "2B", "3B", "HR", "RBI", "SB", "CS",
        "BB", "IBB", "SO", "HBP", "SF",
        "AVG", "OBP", "SLG", "OPS", "ISO", "BABIP", "wRC+", "WAR"
    ]

    static func fetch(name: String) async -> PlayerCard {
        let playerInfo = fetchPlayerInfo(name: name)
        let team = playerInfo?.team ?? ""
        let displayName = playerInfo?.name ?? name

        // Fetch stats (all synchronous SQL)
        let seasons = fetchAllSeasons(name: name)
        let career = fetchCareerTotals(name: name)
        let splits = fetchPlatoonSplits(name: name)
        let streakGrid = fetchStreaks(name: name)

        let currentAge = seasons.first?.age
        let fullTeam = teamFullName(team)

        // Bio is async (network) — runs after SQL is done
        let bio = await fetchWikipediaBio(name: displayName)

        return PlayerCard(
            name: displayName,
            team: team,
            fullTeamName: fullTeam,
            age: currentAge,
            seasons: seasons,
            careerTotals: career,
            platoonSplits: splits,
            streaks: streakGrid,
            bio: bio
        )
    }

    // MARK: - Player info

    private static func fetchPlayerInfo(name: String) -> (name: String, team: String)? {
        let sql = """
            SELECT p.name, p.team FROM players p
            WHERE p.name LIKE '%\(sanitize(name))%'
            LIMIT 1
            """
        guard let result = try? db.execute(sql: sql),
              let row = result.rows.first,
              row.count >= 2 else { return nil }
        return (row[0], row[1])
    }

    // MARK: - All seasons

    private static func fetchAllSeasons(name: String) -> [SeasonData] {
        let sql = """
            SELECT s.season, s.team, s.age,
                   s.games, s.plate_appearances, s.at_bats, s.runs, s.hits,
                   s.doubles, s.triples, s.home_runs, s.rbi, s.stolen_bases, s.caught_stealing,
                   s.walks, s.intentional_walks, s.strikeouts, s.hit_by_pitch, s.sacrifice_flies,
                   s.batting_avg, s.obp, s.slg, s.ops, s.iso, s.babip, s.wrc_plus, s.war
            FROM season_batting_stats s
            JOIN players p ON s.player_id = p.player_id
            WHERE p.name LIKE '%\(sanitize(name))%'
            ORDER BY s.season DESC
            """
        guard let result = try? db.execute(sql: sql) else { return [] }

        // Counting stat keys matching columns 3-18 (games through sacrifice_flies) + war at column 26
        let countingKeys = ["G", "PA", "AB", "R", "H", "2B", "3B", "HR", "RBI", "SB", "CS",
                            "BB", "IBB", "SO", "HBP", "SF"]

        var seasons: [SeasonData] = []
        for row in result.rows {
            guard let year = Int(row[0]) else { continue }
            let team = row[1]
            let age = Int(row[2]) ?? 0
            let games = Int(row[3]) ?? 0

            // Columns 3-26 map to allHeaders (24 stats)
            let values = Array(row[3...26])
            let formatted = formatValues(headers: allHeaders, values: values)
            let grid = StatGridParser.StatGrid(
                headers: allHeaders,
                rows: [StatGridParser.StatGrid.Row(label: "", values: formatted)]
            )

            // Build counting values dict for projections
            var counting: [String: Double] = [:]
            for (i, key) in countingKeys.enumerated() {
                counting[key] = Double(row[3 + i]) ?? 0
            }
            counting["WAR"] = Double(row[26]) ?? 0

            // Get team max games for this team+season
            let teamGames = fetchTeamGames(team: team, season: year)

            // Per-season splits and streaks
            let splits = fetchPlatoonSplitsForSeason(name: name, season: year)
            let streakGrid = fetchStreaksForSeason(name: name, season: year)

            seasons.append(SeasonData(
                year: year, team: team, age: age, games: games, teamGames: teamGames,
                stats: grid, countingValues: counting,
                platoonSplits: splits, streaks: streakGrid
            ))
        }

        return seasons
    }

    // MARK: - Career totals

    private static func fetchCareerTotals(name: String) -> StatGridParser.StatGrid? {
        let sql = """
            SELECT COUNT(DISTINCT s.season),
                   SUM(s.games), SUM(s.plate_appearances), SUM(s.at_bats),
                   SUM(s.runs), SUM(s.hits), SUM(s.doubles), SUM(s.triples),
                   SUM(s.home_runs), SUM(s.rbi), SUM(s.stolen_bases), SUM(s.caught_stealing),
                   SUM(s.walks), SUM(s.intentional_walks), SUM(s.strikeouts),
                   SUM(s.hit_by_pitch), SUM(s.sacrifice_flies),
                   ROUND(CAST(SUM(s.hits) AS REAL) / NULLIF(SUM(s.at_bats), 0), 3),
                   ROUND((CAST(SUM(s.hits) + SUM(s.walks) + SUM(s.hit_by_pitch) AS REAL) /
                          NULLIF(SUM(s.at_bats) + SUM(s.walks) + SUM(s.hit_by_pitch) + SUM(s.sacrifice_flies), 0)), 3),
                   ROUND(CAST(SUM(s.hits - s.doubles - s.triples - s.home_runs) +
                              2 * SUM(s.doubles) + 3 * SUM(s.triples) + 4 * SUM(s.home_runs) AS REAL) /
                          NULLIF(SUM(s.at_bats), 0), 3),
                   ROUND(CAST(SUM(s.hits - s.doubles - s.triples - s.home_runs) +
                              2 * SUM(s.doubles) + 3 * SUM(s.triples) + 4 * SUM(s.home_runs) AS REAL) /
                          NULLIF(SUM(s.at_bats), 0) -
                          CAST(SUM(s.hits) AS REAL) / NULLIF(SUM(s.at_bats), 0), 3),
                   ROUND(CAST(SUM(s.hits) - SUM(s.home_runs) AS REAL) /
                          NULLIF(SUM(s.at_bats) - SUM(s.strikeouts) - SUM(s.home_runs) + SUM(s.sacrifice_flies), 0), 3),
                   ROUND(AVG(s.wrc_plus)),
                   ROUND(SUM(s.war), 1)
            FROM season_batting_stats s
            JOIN players p ON s.player_id = p.player_id
            WHERE p.name LIKE '%\(sanitize(name))%'
            HAVING COUNT(DISTINCT s.season) > 1
            """
        guard let result = try? db.execute(sql: sql),
              let row = result.rows.first else { return nil }

        let seasons = row[0]
        // row[1..16] = counting stats (G through SF)
        // row[17..19] = AVG, OBP, SLG
        // row[20] = ISO, row[21] = BABIP, row[22] = wRC+, row[23] = WAR
        // We need to insert OPS (OBP + SLG) between SLG and ISO
        var values = Array(row.dropFirst())
        let formatted = formatValues(headers: allHeaders.filter { $0 != "OPS" }, values: values)

        // Insert computed OPS after SLG (index 18 in the 23-element array = position after SLG in formatted)
        var withOPS = formatted
        if withOPS.count >= 19 {
            let obp = Double(withOPS[17]) ?? 0
            let slg = Double(withOPS[18]) ?? 0
            let ops = String(format: "%.3f", obp + slg)
            withOPS.insert(ops, at: 19)
        }

        return StatGridParser.StatGrid(
            headers: allHeaders,
            rows: [StatGridParser.StatGrid.Row(label: "\(seasons) Seasons", values: withOPS)]
        )
    }

    // MARK: - Per-season platoon splits

    private static func fetchPlatoonSplitsForSeason(name: String, season: Int) -> StatGridParser.StatGrid? {
        let sql = """
            SELECT ps.split, ps.plate_appearances, ps.at_bats, ps.hits,
                   ps.doubles, ps.triples, ps.home_runs, ps.rbi,
                   ps.walks, ps.strikeouts,
                   ps.batting_avg, ps.obp, ps.slg, ps.ops, ps.iso, ps.babip, ps.wrc_plus
            FROM platoon_splits ps
            JOIN players p ON ps.player_id = p.player_id
            WHERE p.name LIKE '%\(sanitize(name))%' AND ps.season = \(season)
            ORDER BY ps.split
            """
        guard let result = try? db.execute(sql: sql),
              !result.rows.isEmpty else { return nil }

        let headers = ["PA", "AB", "H", "2B", "3B", "HR", "RBI", "BB", "SO", "AVG", "OBP", "SLG", "OPS", "ISO", "BABIP", "wRC+"]
        var rows: [StatGridParser.StatGrid.Row] = []
        for row in result.rows.prefix(2) {
            let splitLabel = row[0] == "vs_LHP" ? "vs LHP" : "vs RHP"
            let values = formatValues(headers: headers, values: Array(row.dropFirst()))
            rows.append(StatGridParser.StatGrid.Row(label: splitLabel, values: values))
        }

        guard !rows.isEmpty else { return nil }
        return StatGridParser.StatGrid(headers: headers, rows: rows)
    }

    // MARK: - Per-season streaks

    private static func fetchStreaksForSeason(name: String, season: Int) -> StatGridParser.StatGrid? {
        var sql = """
            SELECT st.start_date, st.end_date, st.num_games, st.performance,
                   st.at_bats, st.hits, st.walks, st.strikeouts,
                   st.batting_avg, st.obp, st.slg, st.ops, st.home_runs
            FROM streaks st
            JOIN players p ON st.player_id = p.player_id
            WHERE p.name LIKE '%\(sanitize(name))%' AND st.season = \(season) AND st.performance != 'average'
            ORDER BY st.start_date
            """
        var result = try? db.execute(sql: sql)

        if result == nil || result!.rows.isEmpty {
            sql = """
                SELECT ss.start_date, ss.end_date, ss.num_games, ss.performance,
                       ss.at_bats, ss.hits, ss.walks, ss.strikeouts,
                       ss.batting_avg, ss.obp, ss.slg, ss.ops, ss.home_runs
                FROM streaks_sensitive ss
                JOIN players p ON ss.player_id = p.player_id
                WHERE p.name LIKE '%\(sanitize(name))%' AND ss.season = \(season) AND ss.performance != 'average'
                ORDER BY ss.start_date
                """
            result = try? db.execute(sql: sql)
        }

        guard let result, !result.rows.isEmpty else { return nil }

        let headers = ["G", "AB", "H", "BB", "SO", "AVG", "OBP", "SLG", "OPS", "HR", "Perf"]
        var rows: [StatGridParser.StatGrid.Row] = []
        for row in result.rows.prefix(4) {
            let startDate = formatDate(row[0])
            let endDate = formatDate(row[1])
            let label = "\(startDate) \u{2013} \(endDate)"
            let games = row[2]
            let performance = row[3].capitalized
            let ab = row[4]
            let hits = row[5]
            let walks = row[6]
            let so = row[7]
            let avg = formatRate(row[8])
            let obp = formatRate(row[9])
            let slg = formatRate(row[10])
            let ops = formatRate(row[11])
            let hr = row[12]
            rows.append(StatGridParser.StatGrid.Row(
                label: label,
                values: [games, ab, hits, walks, so, avg, obp, slg, ops, hr, performance]
            ))
        }

        guard !rows.isEmpty else { return nil }
        return StatGridParser.StatGrid(headers: headers, rows: rows)
    }

    // MARK: - Platoon splits (all seasons)

    private static func fetchPlatoonSplits(name: String) -> StatGridParser.StatGrid? {
        let sql = """
            SELECT ps.split, ps.plate_appearances, ps.at_bats, ps.hits,
                   ps.doubles, ps.triples, ps.home_runs, ps.rbi,
                   ps.walks, ps.strikeouts,
                   ps.batting_avg, ps.obp, ps.slg, ps.ops, ps.iso, ps.babip, ps.wrc_plus
            FROM platoon_splits ps
            JOIN players p ON ps.player_id = p.player_id
            WHERE p.name LIKE '%\(sanitize(name))%'
            ORDER BY ps.season DESC, ps.split
            """
        guard let result = try? db.execute(sql: sql),
              !result.rows.isEmpty else { return nil }

        let headers = ["PA", "AB", "H", "2B", "3B", "HR", "RBI", "BB", "SO", "AVG", "OBP", "SLG", "OPS", "ISO", "BABIP", "wRC+"]

        // Take only the most recent season's splits (first 2 rows max)
        var rows: [StatGridParser.StatGrid.Row] = []
        var seenSplits = 0
        for row in result.rows {
            guard seenSplits < 2 else { break }
            let splitLabel = row[0] == "vs_LHP" ? "vs LHP" : "vs RHP"
            let values = formatValues(headers: headers, values: Array(row.dropFirst()))
            rows.append(StatGridParser.StatGrid.Row(label: splitLabel, values: values))
            seenSplits += 1
        }

        guard !rows.isEmpty else { return nil }
        return StatGridParser.StatGrid(headers: headers, rows: rows)
    }

    // MARK: - Streaks

    private static func fetchStreaks(name: String) -> StatGridParser.StatGrid? {
        // Try primary streaks table first, then fallback to sensitive
        var sql = """
            SELECT st.start_date, st.end_date, st.num_games, st.performance,
                   st.at_bats, st.hits, st.walks, st.strikeouts,
                   st.batting_avg, st.obp, st.slg, st.ops, st.home_runs
            FROM streaks st
            JOIN players p ON st.player_id = p.player_id
            WHERE p.name LIKE '%\(sanitize(name))%' AND st.performance != 'average'
            ORDER BY st.season DESC, st.start_date
            """
        var result = try? db.execute(sql: sql)

        // Fallback to sensitive streaks if no hot/cold found
        if result == nil || result!.rows.isEmpty {
            sql = """
                SELECT ss.start_date, ss.end_date, ss.num_games, ss.performance,
                       ss.at_bats, ss.hits, ss.walks, ss.strikeouts,
                       ss.batting_avg, ss.obp, ss.slg, ss.ops, ss.home_runs
                FROM streaks_sensitive ss
                JOIN players p ON ss.player_id = p.player_id
                WHERE p.name LIKE '%\(sanitize(name))%' AND ss.performance != 'average'
                ORDER BY ss.season DESC, ss.start_date
                """
            result = try? db.execute(sql: sql)
        }

        guard let result, !result.rows.isEmpty else { return nil }

        let headers = ["G", "AB", "H", "BB", "SO", "AVG", "OBP", "SLG", "OPS", "HR", "Perf"]
        var rows: [StatGridParser.StatGrid.Row] = []
        for row in result.rows.prefix(4) {
            let startDate = formatDate(row[0])
            let endDate = formatDate(row[1])
            let label = "\(startDate) \u{2013} \(endDate)"
            let games = row[2]
            let performance = row[3].capitalized
            let ab = row[4]
            let hits = row[5]
            let walks = row[6]
            let so = row[7]
            let avg = formatRate(row[8])
            let obp = formatRate(row[9])
            let slg = formatRate(row[10])
            let ops = formatRate(row[11])
            let hr = row[12]
            rows.append(StatGridParser.StatGrid.Row(
                label: label,
                values: [games, ab, hits, walks, so, avg, obp, slg, ops, hr, performance]
            ))
        }

        guard !rows.isEmpty else { return nil }
        return StatGridParser.StatGrid(headers: headers, rows: rows)
    }

    // MARK: - Wikipedia bio

    private static func fetchWikipediaBio(name: String) async -> String? {
        let encoded = name.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? name
        let urlString = "https://en.wikipedia.org/api/rest_v1/page/summary/\(encoded)"
        guard let url = URL(string: urlString) else { return nil }

        do {
            let (data, response) = try await URLSession.shared.data(from: url)
            guard let httpResponse = response as? HTTPURLResponse,
                  httpResponse.statusCode == 200 else { return nil }
            let json = try JSONSerialization.jsonObject(with: data) as? [String: Any]
            return json?["extract"] as? String
        } catch {
            return nil
        }
    }

    // MARK: - Team games

    private static func fetchTeamGames(team: String, season: Int) -> Int {
        // Use season-wide max — reliably returns 162 for complete seasons,
        // and the current progress for mid-season data
        let sql = """
            SELECT MAX(games) FROM season_batting_stats
            WHERE season = \(season)
            """
        guard let result = try? db.execute(sql: sql),
              let row = result.rows.first,
              let val = Int(row[0]) else { return 162 }
        return min(val, 162)
    }

    // MARK: - Helpers

    private static func sanitize(_ name: String) -> String {
        name.replacingOccurrences(of: "'", with: "''")
    }

    private static func formatRate(_ value: String) -> String {
        guard let num = Double(value) else { return value }
        let str = String(format: "%.3f", num)
        // Baseball convention: .302 not 0.302, but 1.052 stays
        if str.hasPrefix("0.") { return String(str.dropFirst()) }
        if str.hasPrefix("-0.") { return "-" + String(str.dropFirst(2)) }
        return str
    }

    private static func formatDate(_ dateString: String) -> String {
        let parts = dateString.split(separator: "-")
        guard parts.count == 3,
              let month = Int(parts[1]),
              let day = Int(parts[2]) else { return dateString }

        let monthNames = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
                          "Jul", "Aug", "Sept", "Oct", "Nov", "Dec"]
        guard month >= 1 && month <= 12 else { return dateString }
        return "\(monthNames[month]) \(day)"
    }

    private static func formatValues(headers: [String], values: [String]) -> [String] {
        let rateStats: Set<String> = ["AVG", "OBP", "SLG", "OPS", "ISO", "BABIP"]
        var formatted: [String] = []
        for (idx, value) in values.enumerated() {
            if idx < headers.count && headers[idx] == "wRC+" {
                if let num = Double(value) {
                    formatted.append(String(Int(num.rounded())))
                } else {
                    formatted.append(value)
                }
            } else if idx < headers.count && rateStats.contains(headers[idx]) {
                formatted.append(formatRate(value))
            } else {
                formatted.append(value)
            }
        }
        return formatted
    }

    private static func teamFullName(_ abbreviation: String) -> String {
        let teams: [String: String] = [
            "ARI": "Arizona Diamondbacks", "ATL": "Atlanta Braves",
            "BAL": "Baltimore Orioles", "BOS": "Boston Red Sox",
            "CHC": "Chicago Cubs", "CHW": "Chicago White Sox",
            "CIN": "Cincinnati Reds", "CLE": "Cleveland Guardians",
            "COL": "Colorado Rockies", "DET": "Detroit Tigers",
            "HOU": "Houston Astros", "KCR": "Kansas City Royals",
            "LAA": "Los Angeles Angels", "LAD": "Los Angeles Dodgers",
            "MIA": "Miami Marlins", "MIL": "Milwaukee Brewers",
            "MIN": "Minnesota Twins", "NYM": "New York Mets",
            "NYY": "New York Yankees", "OAK": "Oakland Athletics",
            "PHI": "Philadelphia Phillies", "PIT": "Pittsburgh Pirates",
            "SDP": "San Diego Padres", "SFG": "San Francisco Giants",
            "SEA": "Seattle Mariners", "STL": "St. Louis Cardinals",
            "TBR": "Tampa Bay Rays", "TEX": "Texas Rangers",
            "TOR": "Toronto Blue Jays", "WSN": "Washington Nationals",
            // Common alternates
            "KC": "Kansas City Royals", "SD": "San Diego Padres",
            "SF": "San Francisco Giants", "TB": "Tampa Bay Rays",
            "WSH": "Washington Nationals", "CWS": "Chicago White Sox",
            "LAE": "Los Angeles Angels",
        ]
        return teams[abbreviation] ?? abbreviation
    }
}
