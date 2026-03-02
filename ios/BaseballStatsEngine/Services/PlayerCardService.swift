import Foundation

struct CurrentFormData: Sendable {
    let formStartDate: String       // "2024-06-12"
    let formStartGameNumber: Int    // 1-indexed
    let totalSeasonGames: Int
    let numGames: Int
    let stats: StatGridParser.StatGrid
    let countingValues: [String: Double]        // Form period counting stats
    let seasonCountingValues: [String: Double]  // Full season counting stats (for blended projection)
}

struct GameLog: Sendable {
    let date: String
    let atBats: Int
    let hits: Int
    let doubles: Int
    let triples: Int
    let homeRuns: Int
    let runs: Int
    let rbi: Int
    let walks: Int
    let strikeouts: Int
    let plateAppearances: Int
}

struct SeasonData: Sendable {
    let year: Int
    let team: String
    let age: Int
    let games: Int
    let teamGames: Int
    let stats: StatGridParser.StatGrid
    /// Raw counting stat values (G, AB, R, H, 2B, 3B, HR, RBI, SB, CS, BB, IBB, SO, HBP)
    let countingValues: [String: Double]
    let platoonSplits: StatGridParser.StatGrid?
    let homeAwaySplits: StatGridParser.StatGrid?
    let streaks: StatGridParser.StatGrid?
    let fieldingStats: StatGridParser.StatGrid?
    let currentForm: CurrentFormData?
}

struct PlayerCard: Sendable {
    let name: String
    let team: String
    let fullTeamName: String
    let age: Int?
    let birthdate: Date?
    let positions: String?
    let bats: String?
    let throws_: String?
    let seasons: [SeasonData]
    let careerTotals: StatGridParser.StatGrid?
    let platoonSplits: StatGridParser.StatGrid?
    let streaks: StatGridParser.StatGrid?
    let bio: String?
}

@MainActor
enum PlayerCardService {

    private static let db = DatabaseService()

    // All 21 stats in conventional order (PA and SF excluded for compact 3-row display)
    private static let allHeaders = [
        "G", "AB", "R", "H", "2B", "3B", "HR", "RBI", "SB", "CS",
        "BB", "IBB", "SO", "HBP",
        "AVG", "OBP", "SLG", "OPS", "OPS+", "ISO", "BABIP"
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

        // Use most recent season's team for header (players.team can be stale)
        let headerTeam: String
        if let latestSeason = seasons.first {
            let parts = latestSeason.team.split(separator: "/")
            headerTeam = String(parts.last ?? Substring(team))
        } else {
            headerTeam = team
        }
        let fullTeam = teamFullName(headerTeam)

        // Parse birthdate and compute dynamic age
        var birthDate: Date?
        var dynamicAge: Int?
        if let bdString = playerInfo?.birthdate {
            let fmt = DateFormatter()
            fmt.dateFormat = "yyyy-MM-dd"
            if let date = fmt.date(from: bdString) {
                birthDate = date
                dynamicAge = Calendar.current.dateComponents([.year], from: date, to: Date()).year
            }
        }

        // Bio is async (network) — runs after SQL is done
        let bio = await fetchWikipediaBio(name: displayName)

        return PlayerCard(
            name: displayName,
            team: headerTeam,
            fullTeamName: fullTeam,
            age: dynamicAge,
            birthdate: birthDate,
            positions: playerInfo?.positions,
            bats: playerInfo?.bats,
            throws_: playerInfo?.throws_,
            seasons: seasons,
            careerTotals: career,
            platoonSplits: splits,
            streaks: streakGrid,
            bio: bio
        )
    }

    // MARK: - Comparison builder

    /// Build a structured comparison response for two players (current season + career).
    /// Returns a string with [STATGRID] blocks that StatGridParser can parse.
    static func buildComparison(player1: String, player2: String) -> String {
        let header = "HEADER: " + allHeaders.joined(separator: ", ")

        // Fetch current season (latest year) for each player
        let season1 = fetchLatestSeasonRow(name: player1)
        let season2 = fetchLatestSeasonRow(name: player2)

        // Fetch career totals for each player
        let career1 = fetchCareerRow(name: player1)
        let career2 = fetchCareerRow(name: player2)

        let info1 = fetchPlayerInfo(name: player1)
        let info2 = fetchPlayerInfo(name: player2)
        let label1 = "\(info1?.name ?? player1) (\(info1?.team ?? ""))"
        let label2 = "\(info2?.name ?? player2) (\(info2?.team ?? ""))"

        var parts: [String] = []

        // Current season grid
        if let s1 = season1, let s2 = season2 {
            let year = s1.year
            parts.append("\(year) Season:\n")
            parts.append("[STATGRID]")
            parts.append(header)
            parts.append("ROW: \(label1), \(s1.values.joined(separator: ", "))")
            parts.append("ROW: \(label2), \(s2.values.joined(separator: ", "))")
            parts.append("[/STATGRID]")
        }

        // Career grid (only if multi-season data exists for at least one)
        if let c1 = career1, let c2 = career2 {
            parts.append("\nCareer:\n")
            parts.append("[STATGRID]")
            parts.append(header)
            parts.append("ROW: \(label1), \(c1.joined(separator: ", "))")
            parts.append("ROW: \(label2), \(c2.joined(separator: ", "))")
            parts.append("[/STATGRID]")
        }

        if parts.isEmpty {
            return "I don't have enough data to compare these two players."
        }

        let name1 = info1?.name ?? player1
        let name2 = info2?.name ?? player2
        parts.append("\n[SUGGEST]\(name1) vs lefties[/SUGGEST]")
        parts.append("[SUGGEST]\(name2) vs lefties[/SUGGEST]")

        return parts.joined(separator: "\n")
    }

    /// Fetch the latest season's 21 formatted stat values for a player.
    private static func fetchLatestSeasonRow(name: String) -> (year: Int, values: [String])? {
        let sql = """
            SELECT s.season,
                   s.games, s.at_bats, s.runs, s.hits,
                   s.doubles, s.triples, s.home_runs, s.rbi, s.stolen_bases, s.caught_stealing,
                   s.walks, s.intentional_walks, s.strikeouts, s.hit_by_pitch,
                   s.batting_avg, s.obp, s.slg, s.ops, s.ops_plus, s.iso, s.babip
            FROM season_batting_stats s
            JOIN players p ON s.player_id = p.player_id
            WHERE p.name LIKE '%\(sanitize(name))%'
            ORDER BY s.season DESC
            LIMIT 1
            """
        guard let result = try? db.execute(sql: sql),
              let row = result.rows.first,
              let year = Int(row[0]) else { return nil }

        let values = Array(row[1...21])
        let formatted = formatValues(headers: allHeaders, values: values)
        return (year, formatted)
    }

    /// Fetch career aggregate 21 formatted stat values for a player.
    private static func fetchCareerRow(name: String) -> [String]? {
        let sql = """
            SELECT SUM(s.games), SUM(s.at_bats),
                   SUM(s.runs), SUM(s.hits), SUM(s.doubles), SUM(s.triples),
                   SUM(s.home_runs), SUM(s.rbi), SUM(s.stolen_bases), SUM(s.caught_stealing),
                   SUM(s.walks), SUM(s.intentional_walks), SUM(s.strikeouts),
                   SUM(s.hit_by_pitch),
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
                          NULLIF(SUM(s.at_bats) - SUM(s.strikeouts) - SUM(s.home_runs) + SUM(s.sacrifice_flies), 0), 3)
            FROM season_batting_stats s
            JOIN players p ON s.player_id = p.player_id
            WHERE p.name LIKE '%\(sanitize(name))%'
            HAVING COUNT(DISTINCT s.season) > 1
            """
        guard let result = try? db.execute(sql: sql),
              let row = result.rows.first else { return nil }

        // row has 19 values: 14 counting + 5 rate (no OPS or OPS+)
        let headersNoOPSGroup = allHeaders.filter { $0 != "OPS" && $0 != "OPS+" }
        var formatted = formatValues(headers: headersNoOPSGroup, values: row)

        // Insert OPS after SLG (index 16), then OPS+ after OPS
        if formatted.count >= 17 {
            let obp = Double(formatted[15]) ?? 0
            let slg = Double(formatted[16]) ?? 0
            let ops = String(format: "%.3f", obp + slg)
            formatted.insert(ops, at: 17)
            formatted.insert("--", at: 18) // Career OPS+ not computed
        }

        return formatted
    }

    // MARK: - Player info

    private static func fetchPlayerInfo(name: String) -> (name: String, team: String, birthdate: String?, bats: String?, throws_: String?, positions: String?)? {
        let sql = """
            SELECT p.name, p.team, p.birthdate, p.bats, p.throws, p.positions FROM players p
            WHERE p.name LIKE '%\(sanitize(name))%'
            LIMIT 1
            """
        guard let result = try? db.execute(sql: sql),
              let row = result.rows.first,
              row.count >= 2 else { return nil }
        let birthdate = row.count > 2 && !row[2].isEmpty ? row[2] : nil
        let bats = row.count > 3 && !row[3].isEmpty ? row[3] : nil
        let throws_ = row.count > 4 && !row[4].isEmpty ? row[4] : nil
        let positions = row.count > 5 && !row[5].isEmpty ? row[5] : nil
        return (row[0], row[1], birthdate, bats, throws_, positions)
    }

    // MARK: - All seasons

    private static func fetchAllSeasons(name: String) -> [SeasonData] {
        let sql = """
            SELECT s.season, s.team, s.age,
                   s.games, s.at_bats, s.runs, s.hits,
                   s.doubles, s.triples, s.home_runs, s.rbi, s.stolen_bases, s.caught_stealing,
                   s.walks, s.intentional_walks, s.strikeouts, s.hit_by_pitch,
                   s.batting_avg, s.obp, s.slg, s.ops, s.ops_plus, s.iso, s.babip
            FROM season_batting_stats s
            JOIN players p ON s.player_id = p.player_id
            WHERE p.name LIKE '%\(sanitize(name))%'
            ORDER BY s.season DESC
            """
        guard let result = try? db.execute(sql: sql) else { return [] }

        // Counting stat keys matching columns 3-16 (games through hit_by_pitch)
        let countingKeys = ["G", "AB", "R", "H", "2B", "3B", "HR", "RBI", "SB", "CS",
                            "BB", "IBB", "SO", "HBP"]

        var seasons: [SeasonData] = []
        for row in result.rows {
            guard let year = Int(row[0]) else { continue }
            let team = row[1]
            let age = Int(row[2]) ?? 0
            let games = Int(row[3]) ?? 0

            // Columns 3-23 map to allHeaders (21 stats)
            let values = Array(row[3...23])
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

            // Get team max games for this team+season
            let teamGames = fetchTeamGames(team: team, season: year)

            // Per-season splits, streaks, fielding, and current form
            let splits = fetchPlatoonSplitsForSeason(name: name, season: year)
            let homeAwaySplits = fetchHomeAwaySplitsForSeason(name: name, season: year)
            let streakGrid = fetchStreaksForSeason(name: name, season: year, performance: "hot")
            let fieldingGrid = fetchFieldingForSeason(name: name, season: year)
            let currentForm = fetchCurrentFormForSeason(name: name, season: year)

            seasons.append(SeasonData(
                year: year, team: team, age: age, games: games, teamGames: teamGames,
                stats: grid, countingValues: counting,
                platoonSplits: splits, homeAwaySplits: homeAwaySplits,
                streaks: streakGrid, fieldingStats: fieldingGrid, currentForm: currentForm
            ))
        }

        return seasons
    }

    // MARK: - Career totals

    private static func fetchCareerTotals(name: String) -> StatGridParser.StatGrid? {
        let sql = """
            SELECT COUNT(DISTINCT s.season),
                   SUM(s.games), SUM(s.at_bats),
                   SUM(s.runs), SUM(s.hits), SUM(s.doubles), SUM(s.triples),
                   SUM(s.home_runs), SUM(s.rbi), SUM(s.stolen_bases), SUM(s.caught_stealing),
                   SUM(s.walks), SUM(s.intentional_walks), SUM(s.strikeouts),
                   SUM(s.hit_by_pitch),
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
                          NULLIF(SUM(s.at_bats) - SUM(s.strikeouts) - SUM(s.home_runs) + SUM(s.sacrifice_flies), 0), 3)
            FROM season_batting_stats s
            JOIN players p ON s.player_id = p.player_id
            WHERE p.name LIKE '%\(sanitize(name))%'
            HAVING COUNT(DISTINCT s.season) > 1
            """
        guard let result = try? db.execute(sql: sql),
              let row = result.rows.first else { return nil }

        let seasons = row[0]
        // row[1..14] = counting stats (G through HBP, no PA or SF)
        // row[15..17] = AVG, OBP, SLG
        // row[18] = ISO, row[19] = BABIP
        // We need to insert OPS (OBP + SLG) and OPS+ ("--") between SLG and ISO
        let values = Array(row.dropFirst())
        let formatted = formatValues(headers: allHeaders.filter { $0 != "OPS" && $0 != "OPS+" }, values: values)

        // Insert computed OPS after SLG (index 16), then OPS+ after OPS
        var withOPS = formatted
        if withOPS.count >= 17 {
            let obp = Double(withOPS[15]) ?? 0
            let slg = Double(withOPS[16]) ?? 0
            let ops = String(format: "%.3f", obp + slg)
            withOPS.insert(ops, at: 17)
            withOPS.insert("--", at: 18) // Career OPS+ not computed
        }

        return StatGridParser.StatGrid(
            headers: allHeaders,
            rows: [StatGridParser.StatGrid.Row(label: "\(seasons) Seasons", values: withOPS)]
        )
    }

    // MARK: - Per-season platoon splits

    private static func fetchPlatoonSplitsForSeason(name: String, season: Int) -> StatGridParser.StatGrid? {
        let sql = """
            SELECT ps.split, ps.at_bats, ps.hits,
                   ps.doubles, ps.triples, ps.home_runs, ps.rbi,
                   ps.walks, ps.strikeouts,
                   ps.batting_avg, ps.obp, ps.slg, ps.ops, ps.iso, ps.babip
            FROM platoon_splits ps
            JOIN players p ON ps.player_id = p.player_id
            WHERE p.name LIKE '%\(sanitize(name))%' AND ps.season = \(season)
            ORDER BY ps.split
            """
        guard let result = try? db.execute(sql: sql),
              !result.rows.isEmpty else { return nil }

        let headers = ["AB", "H", "2B", "3B", "HR", "RBI", "BB", "SO", "AVG", "OBP", "SLG", "OPS", "ISO", "BABIP"]
        var rows: [StatGridParser.StatGrid.Row] = []
        for row in result.rows.prefix(2) {
            let splitLabel = row[0] == "vs_LHP" ? "vs LHP" : "vs RHP"
            let values = formatValues(headers: headers, values: Array(row.dropFirst()))
            rows.append(StatGridParser.StatGrid.Row(label: splitLabel, values: values))
        }

        guard !rows.isEmpty else { return nil }
        return StatGridParser.StatGrid(headers: headers, rows: rows)
    }

    // MARK: - Per-season home/away splits

    private static func fetchHomeAwaySplitsForSeason(name: String, season: Int) -> StatGridParser.StatGrid? {
        let sql = """
            SELECT has.split, has.games, has.at_bats, has.runs, has.hits,
                   has.doubles, has.triples, has.home_runs, has.rbi,
                   has.walks, has.strikeouts,
                   has.batting_avg, has.obp, has.slg, has.ops, has.iso, has.babip
            FROM home_away_splits has
            JOIN players p ON has.player_id = p.player_id
            WHERE p.name LIKE '%\(sanitize(name))%' AND has.season = \(season)
            ORDER BY has.split DESC
            """
        guard let result = try? db.execute(sql: sql),
              !result.rows.isEmpty else { return nil }

        let headers = ["G", "AB", "R", "H", "2B", "3B", "HR", "RBI", "BB", "SO", "AVG", "OBP", "SLG", "OPS", "ISO", "BABIP"]
        var rows: [StatGridParser.StatGrid.Row] = []
        for row in result.rows.prefix(2) {
            let splitLabel = row[0] == "home" ? "Home" : "Away"
            let values = formatValues(headers: headers, values: Array(row.dropFirst()))
            rows.append(StatGridParser.StatGrid.Row(label: splitLabel, values: values))
        }

        guard !rows.isEmpty else { return nil }
        return StatGridParser.StatGrid(headers: headers, rows: rows)
    }

    // MARK: - Per-season fielding stats

    private static func fetchFieldingForSeason(name: String, season: Int) -> StatGridParser.StatGrid? {
        let sql = """
            SELECT sfs.position, sfs.games, sfs.games_started, sfs.innings,
                   sfs.putouts, sfs.assists, sfs.errors, sfs.double_plays,
                   sfs.passed_balls, sfs.fielding_pct
            FROM season_fielding_stats sfs
            JOIN players p ON sfs.player_id = p.player_id
            WHERE p.name LIKE '%\(sanitize(name))%' AND sfs.season = \(season) AND sfs.games > 0
            ORDER BY sfs.games DESC
            """
        guard let result = try? db.execute(sql: sql),
              !result.rows.isEmpty else { return nil }

        // Check if any row has passed_balls > 0 (catcher)
        let hasPB = result.rows.contains { row in
            row.count > 8 && (Int(row[8]) ?? 0) > 0
        }

        var headers = ["PO", "A", "E", "DP", "FLD%"]
        if hasPB { headers.insert("PB", at: 4) }

        var rows: [StatGridParser.StatGrid.Row] = []
        for row in result.rows {
            let pos = row[0]
            let po = row[4]
            let a = row[5]
            let e = row[6]
            let dp = row[7]
            let pb = row.count > 8 ? row[8] : "0"
            let fpct: String
            if let fpctVal = Double(row[9]) {
                fpct = String(format: "%.3f", fpctVal)
            } else {
                fpct = row[9]
            }

            var values = [po, a, e, dp]
            if hasPB { values.append(pb) }
            values.append(fpct)

            rows.append(StatGridParser.StatGrid.Row(label: pos, values: values))
        }

        guard !rows.isEmpty else { return nil }
        return StatGridParser.StatGrid(headers: headers, rows: rows)
    }

    // MARK: - Per-season streaks

    static func fetchStreaksForSeason(name: String, season: Int, performance: String = "hot") -> StatGridParser.StatGrid? {
        let orderDir = performance == "cold" ? "ASC" : "DESC"
        var sql = """
            SELECT st.start_date, st.end_date, st.num_games,
                   st.at_bats, st.hits, st.walks, st.strikeouts,
                   st.batting_avg, st.obp, st.slg, st.ops, st.home_runs
            FROM streaks st
            JOIN players p ON st.player_id = p.player_id
            WHERE p.name LIKE '%\(sanitize(name))%' AND st.season = \(season) AND st.performance = '\(performance)'
            ORDER BY st.ops \(orderDir)
            """
        var result = try? db.execute(sql: sql)

        // Fall back to sensitive streaks if no rows
        if result == nil || result!.rows.isEmpty {
            sql = """
                SELECT ss.start_date, ss.end_date, ss.num_games,
                       ss.at_bats, ss.hits, ss.walks, ss.strikeouts,
                       ss.batting_avg, ss.obp, ss.slg, ss.ops, ss.home_runs
                FROM streaks_sensitive ss
                JOIN players p ON ss.player_id = p.player_id
                WHERE p.name LIKE '%\(sanitize(name))%' AND ss.season = \(season) AND ss.performance = '\(performance)'
                ORDER BY ss.ops \(orderDir)
                """
            result = try? db.execute(sql: sql)
        }

        // Tier 3: sliding window fallback
        if result == nil || result!.rows.isEmpty {
            sql = """
                SELECT sl.start_date, sl.end_date, sl.num_games,
                       sl.at_bats, sl.hits, sl.walks, sl.strikeouts,
                       sl.batting_avg, sl.obp, sl.slg, sl.ops, sl.home_runs
                FROM streaks_sliding sl
                JOIN players p ON sl.player_id = p.player_id
                WHERE p.name LIKE '%\(sanitize(name))%' AND sl.season = \(season) AND sl.performance = '\(performance)'
                ORDER BY sl.ops \(orderDir)
                """
            result = try? db.execute(sql: sql)
        }

        guard let result, !result.rows.isEmpty else { return nil }

        let headers = ["G", "AB", "H", "BB", "SO", "AVG", "OBP", "SLG", "OPS", "HR"]
        var rows: [StatGridParser.StatGrid.Row] = []
        for row in result.rows.prefix(4) {
            let startDate = formatDate(row[0])
            let endDate = formatDate(row[1])
            let label = "\(startDate) \u{2013} \(endDate)"
            let games = row[2]
            let ab = row[3]
            let hits = row[4]
            let walks = row[5]
            let so = row[6]
            let avg = formatRate(row[7])
            let obp = formatRate(row[8])
            let slg = formatRate(row[9])
            let ops = formatRate(row[10])
            let hr = row[11]
            rows.append(StatGridParser.StatGrid.Row(
                label: label,
                values: [games, ab, hits, walks, so, avg, obp, slg, ops, hr]
            ))
        }

        guard !rows.isEmpty else { return nil }
        return StatGridParser.StatGrid(headers: headers, rows: rows)
    }

    // MARK: - Current hot streak (chat response builder)

    /// Build a structured response for "how has X been playing lately?" queries.
    /// Returns a string with a [STATGRID] block — no Claude call needed.
    /// Build a structured season summary for chat (bypasses Claude).
    /// Returns STATGRID blocks for the season stats, splits, and streaks.
    static func buildSeasonSummary(name: String, season: Int) -> String? {
        let info = fetchPlayerInfo(name: name)
        let displayName = info?.name ?? name

        // Fetch season stats
        let sql = """
            SELECT s.team, s.games, s.at_bats, s.runs, s.hits,
                   s.doubles, s.triples, s.home_runs, s.rbi, s.stolen_bases, s.caught_stealing,
                   s.walks, s.intentional_walks, s.strikeouts, s.hit_by_pitch,
                   s.batting_avg, s.obp, s.slg, s.ops, s.ops_plus, s.iso, s.babip
            FROM season_batting_stats s
            JOIN players p ON s.player_id = p.player_id
            WHERE p.name LIKE '%\(sanitize(name))%' AND s.season = \(season)
            """
        guard let result = try? db.execute(sql: sql),
              let row = result.rows.first,
              row.count >= 22 else { return nil }

        let team = teamFullName(row[0])
        let values = Array(row[1...21])  // games through babip
        let formatted = formatValues(headers: allHeaders, values: values)

        var parts: [String] = []

        // Header text
        parts.append("**\(displayName)** — \(season) Season (\(team))\n")

        // Season stat grid
        parts.append("[STATGRID]")
        parts.append("HEADER: " + allHeaders.joined(separator: ", "))
        parts.append("ROW: " + formatted.joined(separator: ", "))
        parts.append("[/STATGRID]")

        // Platoon splits
        let splitsSql = """
            SELECT ps.split_type,
                   ps.at_bats, ps.hits, ps.doubles, ps.triples, ps.home_runs,
                   ps.walks, ps.strikeouts,
                   ps.batting_avg, ps.obp, ps.slg, ps.ops
            FROM platoon_splits ps
            JOIN players p ON ps.player_id = p.player_id
            WHERE p.name LIKE '%\(sanitize(name))%' AND ps.season = \(season)
            ORDER BY ps.split_type
            """
        if let splitsResult = try? db.execute(sql: splitsSql), !splitsResult.rows.isEmpty {
            parts.append("\n[STATGRID]")
            parts.append("HEADER: AB, H, 2B, 3B, HR, BB, SO, AVG, OBP, SLG, OPS")
            for sRow in splitsResult.rows {
                let label = sRow[0] == "vs_LHP" ? "vs LHP" : "vs RHP"
                let sValues = Array(sRow[1...])
                let sFormatted = formatValues(
                    headers: ["AB", "H", "2B", "3B", "HR", "BB", "SO", "AVG", "OBP", "SLG", "OPS"],
                    values: sValues
                )
                parts.append("ROW \(label): " + sFormatted.joined(separator: ", "))
            }
            parts.append("[/STATGRID]")
        }

        // Hot streaks
        let streaksSql = """
            SELECT start_date, end_date, num_games,
                   batting_avg, obp, slg, ops, home_runs, hits, at_bats, walks, strikeouts
            FROM streaks
            JOIN players p ON streaks.player_id = p.player_id
            WHERE p.name LIKE '%\(sanitize(name))%' AND streaks.season = \(season)
              AND performance = 'hot'
            ORDER BY ops DESC
            LIMIT 3
            """
        if let streaksResult = try? db.execute(sql: streaksSql), !streaksResult.rows.isEmpty {
            parts.append("\n**Notable Hot Streaks**\n")
            parts.append("[STATGRID]")
            parts.append("HEADER: G, AB, H, BB, SO, AVG, OBP, SLG, OPS, HR")
            for sRow in streaksResult.rows {
                let startDate = formatDate(sRow[0])
                let endDate = formatDate(sRow[1])
                let label = "\(startDate) \u{2013} \(endDate)"
                let g = sRow[2]
                let avg = formatRate(sRow[3]), obpVal = formatRate(sRow[4])
                let slgVal = formatRate(sRow[5]), opsVal = formatRate(sRow[6])
                let hr = sRow[7], h = sRow[8], ab = sRow[9], bb = sRow[10], so = sRow[11]
                parts.append("ROW \(label): \(g), \(ab), \(h), \(bb), \(so), \(avg), \(obpVal), \(slgVal), \(opsVal), \(hr)")
            }
            parts.append("[/STATGRID]")
        }

        parts.append("\n[SUGGEST]how is \(displayName) doing lately[/SUGGEST]")
        parts.append("[SUGGEST]\(displayName) career[/SUGGEST]")

        return parts.joined(separator: "\n")
    }

    /// Build a structured response for streak history queries like "Judge's hot streaks 2024".
    /// Returns a string with [STATGRID] blocks — no Claude call needed.
    static func buildStreakList(name: String, performance: String, season: Int?) -> String? {
        let info = fetchPlayerInfo(name: name)
        let displayName = info?.name ?? name

        // If no season specified, find the most recent season with streak data
        let targetSeason: Int
        if let s = season {
            targetSeason = s
        } else {
            let sql = """
                SELECT MAX(st.season) FROM streaks st
                JOIN players p ON st.player_id = p.player_id
                WHERE p.name LIKE '%\(sanitize(name))%'
                """
            guard let result = try? db.execute(sql: sql),
                  let row = result.rows.first,
                  let year = Int(row[0]) else { return nil }
            targetSeason = year
        }

        guard let grid = fetchStreaksForSeason(name: name, season: targetSeason, performance: performance),
              !grid.rows.isEmpty else {
            let label = performance == "cold" ? "cold streaks" : "hot streaks"
            return "No \(label) found for **\(displayName)** in \(targetSeason)."
        }

        let label = performance == "cold" ? "Cold Streaks" : "Hot Streaks"
        var parts: [String] = []
        parts.append("**\(displayName)** — \(targetSeason) \(label)\n")

        parts.append("[STATGRID]")
        parts.append("HEADER: " + grid.headers.joined(separator: ", "))
        for row in grid.rows {
            parts.append("ROW \(row.label): " + row.values.joined(separator: ", "))
        }
        parts.append("[/STATGRID]")

        // Summary line
        let count = grid.rows.count
        let streakWord = count == 1 ? "streak" : "streaks"
        if let topRow = grid.rows.first {
            let opsIdx = grid.headers.firstIndex(of: "OPS") ?? -1
            let opsValue = opsIdx >= 0 && opsIdx < topRow.values.count ? topRow.values[opsIdx] : ""
            let gIdx = grid.headers.firstIndex(of: "G") ?? -1
            let gValue = gIdx >= 0 && gIdx < topRow.values.count ? topRow.values[gIdx] : ""
            let adjective = performance == "cold" ? "coldest" : "hottest"
            parts.append("\n\(count) \(performance) \(streakWord) detected. The \(adjective) was \(gValue) games (\(topRow.label)) with a \(opsValue) OPS.")
        }

        let oppositePerf = performance == "hot" ? "cold" : "hot"
        parts.append("\n[SUGGEST]\(displayName) \(oppositePerf) streaks[/SUGGEST]")

        return parts.joined(separator: "\n")
    }

    static func buildCurrentHotStreak(name: String) -> String? {
        let info = fetchPlayerInfo(name: name)
        let displayName = info?.name ?? name

        // Find the most recent season with current form data
        let sql = """
            SELECT cf.season, cf.form_start_date, cf.form_start_game_number,
                   cf.total_season_games, cf.num_games,
                   cf.at_bats, cf.hits, cf.home_runs, cf.runs, cf.rbi,
                   cf.walks, cf.strikeouts,
                   cf.batting_avg, cf.obp, cf.slg, cf.ops,
                   s.batting_avg, s.obp, s.slg, s.ops,
                   s.team
            FROM current_form cf
            JOIN players p ON cf.player_id = p.player_id
            LEFT JOIN season_batting_stats s ON cf.player_id = s.player_id AND cf.season = s.season
            WHERE p.name LIKE '%\(sanitize(name))%'
            ORDER BY cf.season DESC
            LIMIT 1
            """
        guard let result = try? db.execute(sql: sql),
              let row = result.rows.first,
              row.count >= 21 else { return nil }

        let season = row[0]
        let startDate = formatDate(row[1])
        let startGameNum = row[2]
        let totalGames = row[3]
        let numGames = Int(row[4]) ?? 0
        let ab = row[5], h = row[6], hr = row[7], r = row[8], rbi = row[9]
        let bb = row[10], so = row[11]
        let avg = formatRate(row[12]), obp = formatRate(row[13])
        let slg = formatRate(row[14]), ops = formatRate(row[15])
        let seasonAvg = formatRate(row[16]), seasonOps = formatRate(row[19])
        let team = row[20]

        let teamGames = fetchTeamGames(team: team, season: Int(season) ?? 0)

        var parts: [String] = []
        parts.append("\(displayName) has been on fire over the last \(numGames) games (since \(startDate)):\n")

        parts.append("[STATGRID]")
        parts.append("HEADER: G, AB, R, H, HR, RBI, BB, SO, AVG, OBP, SLG, OPS")
        parts.append("FORM: \(displayName), \(season), \(startGameNum), \(totalGames), \(teamGames)")
        parts.append("ROW: \(numGames), \(ab), \(r), \(h), \(hr), \(rbi), \(bb), \(so), \(avg), \(obp), \(slg), \(ops)")
        parts.append("[/STATGRID]")

        // Brief comparison to full season
        parts.append("\nThat's up from his \(season) season line of \(seasonAvg)/\(seasonOps) (AVG/OPS).")
        parts.append("\n[SUGGEST]\(displayName) hot streaks[/SUGGEST]")

        return parts.joined(separator: "\n")
    }

    // MARK: - Career lookup (chat response builder)

    /// Build a career response for "Judge career stats" or "Judge career home runs".
    /// With stat: single career stat sentence. Without stat: full 21-stat career grid.
    /// Returns nil if only 1 season of data (falls through to season lookup).
    static func buildCareerLookup(name: String, stat: PlayerNameMatcher.StatInfo?) -> String? {
        let info = fetchPlayerInfo(name: name)
        let displayName = info?.name ?? name
        let team = info?.team ?? ""
        let teamDisplay = teamFullName(team)

        // Detect the most recent season for pill suggestions
        let mostRecentSeason: Int = {
            let sql = """
                SELECT MAX(s.season) FROM season_batting_stats s
                JOIN players p ON s.player_id = p.player_id
                WHERE p.name LIKE '%\(sanitize(name))%'
                """
            if let r = try? db.execute(sql: sql), let row = r.rows.first, let yr = Int(row[0]) {
                return yr
            }
            return 2025
        }()

        if let stat {
            // Single career stat
            let selectExpr: String
            if stat.isRate {
                guard let formula = careerRateFormula(for: stat) else { return nil }
                selectExpr = formula
            } else {
                selectExpr = "SUM(s.\(stat.dbColumn))"
            }

            let sql = """
                SELECT \(selectExpr), COUNT(DISTINCT s.season)
                FROM season_batting_stats s
                JOIN players p ON s.player_id = p.player_id
                WHERE p.name LIKE '%\(sanitize(name))%'
                """
            guard let result = try? db.execute(sql: sql),
                  let row = result.rows.first,
                  !row[0].isEmpty else { return nil }

            let seasons = Int(row[1]) ?? 0
            if seasons <= 1 { return nil }

            let formattedValue = stat.isRate ? formatRate(row[0]) : row[0]

            // Build natural language sentence
            let sentence: String
            switch stat.displayAbbrev {
            case "HR":
                sentence = "**\(displayName)** has hit **\(formattedValue)** career home runs."
            case "AVG":
                sentence = "**\(displayName)** has a **\(formattedValue)** career batting average."
            case "RBI":
                sentence = "**\(displayName)** has driven in **\(formattedValue)** career runs."
            case "SB":
                sentence = "**\(displayName)** has stolen **\(formattedValue)** career bases."
            case "H":
                sentence = "**\(displayName)** has **\(formattedValue)** career hits."
            case "R":
                sentence = "**\(displayName)** has scored **\(formattedValue)** career runs."
            default:
                if stat.isRate {
                    sentence = "**\(displayName)** has a **\(formattedValue)** career \(stat.displayAbbrev)."
                } else {
                    sentence = "**\(displayName)** has **\(formattedValue)** career \(stat.displayAbbrev)."
                }
            }

            let statName = stat.pillName
            return "\(sentence) (\(teamDisplay))\n\n[SUGGEST]career \(statName) leaders[/SUGGEST]\n[SUGGEST]\(displayName) \(mostRecentSeason)[/SUGGEST]"
        } else {
            // Full career grid
            guard let careerValues = fetchCareerRow(name: name) else { return nil }

            var parts: [String] = []
            parts.append("**\(displayName)** — Career Totals (\(teamDisplay))\n")

            // Count seasons for the row label
            let seasonCountSql = """
                SELECT COUNT(DISTINCT s.season) FROM season_batting_stats s
                JOIN players p ON s.player_id = p.player_id
                WHERE p.name LIKE '%\(sanitize(name))%'
                """
            let seasonCount: String
            if let r = try? db.execute(sql: seasonCountSql), let row = r.rows.first {
                seasonCount = row[0]
            } else {
                seasonCount = "?"
            }

            parts.append("[STATGRID]")
            parts.append("HEADER: " + allHeaders.joined(separator: ", "))
            parts.append("ROW \(seasonCount) Seasons: " + careerValues.joined(separator: ", "))
            parts.append("[/STATGRID]")

            parts.append("\n[SUGGEST]\(displayName) \(mostRecentSeason)[/SUGGEST]")
            parts.append("[SUGGEST]\(displayName) vs lefties[/SUGGEST]")

            return parts.joined(separator: "\n")
        }
    }

    // MARK: - Single stat lookup (chat response builder)

    /// Build a natural language response for "Judge home runs" or "Ohtani OPS" queries.
    static func buildSingleStatLookup(name: String, stat: PlayerNameMatcher.StatInfo, season: Int) -> String? {
        let sql = """
            SELECT p.name, s.team, s.\(stat.dbColumn)
            FROM season_batting_stats s
            JOIN players p ON s.player_id = p.player_id
            WHERE p.name LIKE '%\(sanitize(name))%' AND s.season = \(season)
            LIMIT 1
            """
        guard let result = try? db.execute(sql: sql),
              let row = result.rows.first,
              row.count >= 3 else { return nil }

        let displayName = row[0]
        let team = row[1]
        let rawValue = row[2]

        // Format the value
        let formattedValue: String
        if stat.isRate {
            formattedValue = formatRate(rawValue)
        } else {
            formattedValue = rawValue
        }

        // Build stat-specific sentence
        let sentence: String
        switch stat.displayAbbrev {
        case "HR":
            sentence = "**\(displayName)** hit **\(formattedValue)** home runs in \(season)."
        case "AVG":
            sentence = "**\(displayName)** posted a **\(formattedValue) AVG** in \(season)."
        case "RBI":
            sentence = "**\(displayName)** drove in **\(formattedValue)** runs in \(season)."
        case "SB":
            sentence = "**\(displayName)** stole **\(formattedValue)** bases in \(season)."
        case "R":
            sentence = "**\(displayName)** scored **\(formattedValue)** runs in \(season)."
        case "H":
            sentence = "**\(displayName)** had **\(formattedValue)** hits in \(season)."
        case "SO":
            sentence = "**\(displayName)** struck out **\(formattedValue)** times in \(season)."
        case "BB":
            sentence = "**\(displayName)** drew **\(formattedValue)** walks in \(season)."
        case "OPS":
            sentence = "**\(displayName)** posted a **\(formattedValue) OPS** in \(season)."
        case "OPS+":
            sentence = "**\(displayName)** posted a **\(formattedValue) OPS+** in \(season)."
        case "OBP":
            sentence = "**\(displayName)** posted a **\(formattedValue) OBP** in \(season)."
        case "SLG":
            sentence = "**\(displayName)** posted a **\(formattedValue) SLG** in \(season)."
        default:
            if stat.isRate {
                sentence = "**\(displayName)** posted a **\(formattedValue) \(stat.displayAbbrev)** in \(season)."
            } else {
                sentence = "**\(displayName)** had **\(formattedValue) \(stat.displayAbbrev)** in \(season)."
            }
        }

        let teamDisplay = teamFullName(team)
        let statName = stat.pillName
        return "\(sentence) (\(teamDisplay))\n\n[TIP]Tap a player name for their full profile.[/TIP]\n\n[SUGGEST]\(season) \(statName) leaders[/SUGGEST]\n[SUGGEST]\(displayName) career \(statName)[/SUGGEST]"
    }

    // MARK: - Threshold leaderboard (chat response builder)

    /// Build a filtered leaderboard for "who hit 40 home runs?" or "players batting over .300".
    static func buildThresholdLeaderboard(stat: PlayerNameMatcher.StatInfo, threshold: Double, comparison: String, season: Int) -> String {
        // Rate stats need a PA minimum
        let paMin: Int?
        if stat.isRate {
            let maxGamesSql = "SELECT MAX(games) FROM season_batting_stats WHERE season = \(season)"
            let maxGames: Int
            if let r = try? db.execute(sql: maxGamesSql), let row = r.rows.first, let val = Int(row[0]) {
                maxGames = val
            } else {
                maxGames = 162
            }
            paMin = maxGames >= 140 ? 400 : 200
        } else {
            paMin = nil
        }

        let paFilter = paMin.map { " AND s.plate_appearances >= \($0)" } ?? ""

        let sql = """
            SELECT p.name, s.\(stat.dbColumn)
            FROM season_batting_stats s
            JOIN players p ON s.player_id = p.player_id
            WHERE s.season = \(season) AND s.\(stat.dbColumn) \(comparison) \(threshold)\(paFilter)
            ORDER BY s.\(stat.dbColumn) DESC
            LIMIT 50
            """
        guard let result = try? db.execute(sql: sql),
              !result.rows.isEmpty else {
            let thresholdStr = stat.isRate ? formatRate(String(threshold)) : String(Int(threshold))
            let op = comparison == ">=" ? "at least" : "no more than"
            return "No players had \(op) \(thresholdStr) \(stat.displayAbbrev) in \(season)."
        }

        // Build title
        let thresholdDisplay: String
        if stat.isRate {
            thresholdDisplay = formatRate(String(threshold))
        } else {
            thresholdDisplay = String(Int(threshold))
        }

        let title: String
        if comparison == ">=" {
            if stat.isRate {
                title = "Players Batting Over \(thresholdDisplay) \(stat.displayAbbrev) in \(season)"
            } else {
                title = "Players with \(thresholdDisplay)+ \(stat.displayName) in \(season)"
            }
        } else {
            title = "Players with \(thresholdDisplay) or Fewer \(stat.displayName) in \(season)"
        }

        var parts: [String] = []
        parts.append("**\(title)**\n")
        parts.append("[TIP]Tap a player name for their full profile.[/TIP]")

        parts.append("[LEADERBOARD]")
        parts.append("HEADER: \(stat.displayAbbrev)")
        for (i, row) in result.rows.enumerated() {
            let playerName = row[0]
            let rawValue = row[1]
            let formattedValue = stat.isRate ? formatRate(rawValue) : rawValue
            parts.append("ROW \(i + 1). \(playerName): \(formattedValue)")
        }
        parts.append("[/LEADERBOARD]")

        let count = result.rows.count
        parts.append("\n\(count) player\(count == 1 ? "" : "s") matched.")

        if let paMin {
            parts.append("_Min. \(paMin) PA._")
        }

        let statName = stat.pillName
        parts.append("\n[SUGGEST]\(season) \(statName) leaders[/SUGGEST]")
        parts.append("[SUGGEST]career \(statName) leaders[/SUGGEST]")

        return parts.joined(separator: "\n")
    }

    // MARK: - Team stats (chat response builder)

    /// Build a team leaderboard for "Yankees hitters" or "Dodgers OPS leaders".
    static func buildTeamStats(teamCode: String, stat: PlayerNameMatcher.StatInfo?, season: Int) -> String {
        let fullName = teamFullName(teamCode)
        let nickname = teamNickname(teamCode)

        if let stat {
            // Team leaderboard for a specific stat
            let paMin: Int?
            if stat.isRate {
                paMin = 50
            } else {
                paMin = nil
            }
            let paFilter = paMin.map { " AND s.plate_appearances >= \($0)" } ?? ""

            let sql = """
                SELECT p.name, s.\(stat.dbColumn)
                FROM season_batting_stats s
                JOIN players p ON s.player_id = p.player_id
                WHERE (s.team = '\(teamCode)' OR s.team LIKE '\(teamCode)/%' OR s.team LIKE '%/\(teamCode)')
                      AND s.season = \(season)\(paFilter)
                ORDER BY s.\(stat.dbColumn) DESC
                LIMIT 15
                """
            guard let result = try? db.execute(sql: sql),
                  !result.rows.isEmpty else {
                return "No \(stat.displayName) data found for the \(fullName) in \(season)."
            }

            var parts: [String] = []
            parts.append("**\(fullName)** — \(season) \(stat.displayName) Leaders\n")
            parts.append("[TIP]Tap a player name for their full profile.[/TIP]")

            parts.append("[LEADERBOARD]")
            parts.append("HEADER: \(stat.displayAbbrev)")
            for (i, row) in result.rows.enumerated() {
                let playerName = row[0]
                let rawValue = row[1]
                let formattedValue = stat.isRate ? formatRate(rawValue) : rawValue
                parts.append("ROW \(i + 1). \(playerName): \(formattedValue)")
            }
            parts.append("[/LEADERBOARD]")

            if let paMin {
                parts.append("\n_Min. \(paMin) PA._")
            }

            let statName = stat.pillName
            parts.append("\n[SUGGEST]\(season) \(statName) leaders[/SUGGEST]")
            parts.append("[SUGGEST]\(nickname) hitters[/SUGGEST]")

            return parts.joined(separator: "\n")
        } else {
            // Team overview sorted by OPS
            let sql = """
                SELECT p.name, s.games, s.batting_avg, s.home_runs, s.rbi, s.ops
                FROM season_batting_stats s
                JOIN players p ON s.player_id = p.player_id
                WHERE (s.team = '\(teamCode)' OR s.team LIKE '\(teamCode)/%' OR s.team LIKE '%/\(teamCode)')
                      AND s.season = \(season) AND s.plate_appearances >= 50
                ORDER BY s.ops DESC
                LIMIT 15
                """
            guard let result = try? db.execute(sql: sql),
                  !result.rows.isEmpty else {
                return "No hitting data found for the \(fullName) in \(season)."
            }

            var parts: [String] = []
            parts.append("**\(fullName)** — \(season) Hitters\n")
            parts.append("[TIP]Tap a player name for their full profile.[/TIP]")

            parts.append("[LEADERBOARD]")
            parts.append("HEADER: G, AVG, HR, RBI, OPS")
            for (i, row) in result.rows.enumerated() {
                let playerName = row[0]
                let g = row[1]
                let avg = formatRate(row[2])
                let hr = row[3]
                let rbi = row[4]
                let ops = formatRate(row[5])
                parts.append("ROW \(i + 1). \(playerName): \(g), \(avg), \(hr), \(rbi), \(ops)")
            }
            parts.append("[/LEADERBOARD]")

            parts.append("\n_Min. 50 PA._")
            parts.append("\n[SUGGEST]\(nickname) home runs[/SUGGEST]")
            parts.append("[SUGGEST]\(nickname) batting average[/SUGGEST]")

            return parts.joined(separator: "\n")
        }
    }

    /// Build a team aggregate total response — e.g. "The Yankees hit 234 home runs in 2024."
    static func buildTeamTotal(teamCode: String, stat: PlayerNameMatcher.StatInfo, season: Int) -> String {
        let fullName = teamFullName(teamCode)
        let nickname = teamNickname(teamCode)

        if stat.isRate {
            // Rate stats → compute from raw components for accuracy
            let (numExpr, denomExpr, label): (String, String, String) = switch stat.dbColumn {
            case "batting_avg":
                ("SUM(s.hits)", "SUM(s.at_bats)", "batting average")
            case "obp":
                ("SUM(s.hits + s.walks + s.hit_by_pitch)", "SUM(s.at_bats + s.walks + s.hit_by_pitch + s.sacrifice_flies)", "on-base percentage")
            case "slg":
                ("SUM(s.hits - s.doubles - s.triples - s.home_runs + 2*s.doubles + 3*s.triples + 4*s.home_runs)", "SUM(s.at_bats)", "slugging percentage")
            case "ops":
                ("CAST(SUM(s.hits + s.walks + s.hit_by_pitch) AS REAL) / SUM(s.at_bats + s.walks + s.hit_by_pitch + s.sacrifice_flies) + CAST(SUM(s.hits - s.doubles - s.triples - s.home_runs + 2*s.doubles + 3*s.triples + 4*s.home_runs) AS REAL) / SUM(s.at_bats)", "1", "OPS")
            default:
                ("SUM(s.\(stat.dbColumn) * s.plate_appearances)", "SUM(s.plate_appearances)", stat.displayName.lowercased())
            }

            let sql: String
            if stat.dbColumn == "ops" {
                // OPS = OBP + SLG, compute directly
                sql = """
                    SELECT CAST(SUM(s.hits + s.walks + s.hit_by_pitch) AS REAL) / SUM(s.at_bats + s.walks + s.hit_by_pitch + s.sacrifice_flies)
                         + CAST(SUM(s.hits - s.doubles - s.triples - s.home_runs + 2*s.doubles + 3*s.triples + 4*s.home_runs) AS REAL) / SUM(s.at_bats)
                    FROM season_batting_stats s
                    WHERE (s.team = '\(teamCode)' OR s.team LIKE '\(teamCode)/%' OR s.team LIKE '%/\(teamCode)')
                          AND s.season = \(season) AND s.plate_appearances >= 1
                    """
            } else {
                sql = """
                    SELECT CAST(\(numExpr) AS REAL) / \(denomExpr)
                    FROM season_batting_stats s
                    WHERE (s.team = '\(teamCode)' OR s.team LIKE '\(teamCode)/%' OR s.team LIKE '%/\(teamCode)')
                          AND s.season = \(season) AND s.plate_appearances >= 1
                    """
            }

            guard let result = try? db.execute(sql: sql),
                  let row = result.rows.first,
                  let value = Double(row[0]) else {
                return "No \(stat.displayName) data found for the \(fullName) in \(season)."
            }
            let formatted = formatRate(String(value))
            return "The **\(fullName)** had a team \(label) of **\(formatted)** in \(season).\n\n[SUGGEST]\(nickname) \(stat.pillName) leaders[/SUGGEST]\n[SUGGEST]\(nickname) hitters[/SUGGEST]"
        } else {
            // Counting stats → SUM
            let sql = """
                SELECT SUM(s.\(stat.dbColumn))
                FROM season_batting_stats s
                WHERE (s.team = '\(teamCode)' OR s.team LIKE '\(teamCode)/%' OR s.team LIKE '%/\(teamCode)')
                      AND s.season = \(season)
                """
            guard let result = try? db.execute(sql: sql),
                  let row = result.rows.first,
                  let total = Int(row[0]) else {
                return "No \(stat.displayName) data found for the \(fullName) in \(season)."
            }

            // Stat-appropriate verb
            let phrase: String = switch stat.dbColumn {
            case "home_runs", "hits", "doubles", "triples":
                "hit **\(total) \(stat.displayName.lowercased())**"
            case "rbi":
                "drove in **\(total) runs**"
            case "runs":
                "scored **\(total) runs**"
            case "stolen_bases":
                "stole **\(total) bases**"
            case "walks":
                "drew **\(total) walks**"
            case "strikeouts":
                "struck out **\(total) times**"
            default:
                "had **\(total) \(stat.displayName.lowercased())**"
            }

            return "The **\(fullName)** \(phrase) in \(season).\n\n[SUGGEST]\(nickname) \(stat.pillName) leaders[/SUGGEST]\n[SUGGEST]\(nickname) hitters[/SUGGEST]"
        }
    }

    /// Build a team ranking — top 10 teams by a stat.
    static func buildTeamRanking(stat: PlayerNameMatcher.StatInfo, season: Int) -> String {
        let limit = 10

        let sql: String
        if stat.isRate {
            // Rate stats: compute from raw components, require minimum PA
            let selectExpr: String = switch stat.dbColumn {
            case "batting_avg":
                "CAST(SUM(s.hits) AS REAL) / SUM(s.at_bats)"
            case "obp":
                "CAST(SUM(s.hits + s.walks + s.hit_by_pitch) AS REAL) / SUM(s.at_bats + s.walks + s.hit_by_pitch + s.sacrifice_flies)"
            case "slg":
                "CAST(SUM(s.hits - s.doubles - s.triples - s.home_runs + 2*s.doubles + 3*s.triples + 4*s.home_runs) AS REAL) / SUM(s.at_bats)"
            case "ops":
                "CAST(SUM(s.hits + s.walks + s.hit_by_pitch) AS REAL) / SUM(s.at_bats + s.walks + s.hit_by_pitch + s.sacrifice_flies) + CAST(SUM(s.hits - s.doubles - s.triples - s.home_runs + 2*s.doubles + 3*s.triples + 4*s.home_runs) AS REAL) / SUM(s.at_bats)"
            case "iso":
                "CAST(SUM(s.hits - s.doubles - s.triples - s.home_runs + 2*s.doubles + 3*s.triples + 4*s.home_runs) AS REAL) / SUM(s.at_bats) - CAST(SUM(s.hits) AS REAL) / SUM(s.at_bats)"
            default:
                "SUM(s.\(stat.dbColumn) * s.plate_appearances) / SUM(s.plate_appearances)"
            }

            sql = """
                SELECT s.team, \(selectExpr) AS team_stat
                FROM season_batting_stats s
                WHERE s.season = \(season) AND s.plate_appearances >= 1
                GROUP BY s.team
                HAVING SUM(s.plate_appearances) >= 100
                ORDER BY team_stat DESC
                LIMIT \(limit)
                """
        } else {
            // Counting stats: SUM
            sql = """
                SELECT s.team, SUM(s.\(stat.dbColumn)) AS team_stat
                FROM season_batting_stats s
                WHERE s.season = \(season)
                GROUP BY s.team
                ORDER BY team_stat DESC
                LIMIT \(limit)
                """
        }

        guard let result = try? db.execute(sql: sql),
              !result.rows.isEmpty else {
            return "No team \(stat.displayName) data found for \(season)."
        }

        var parts: [String] = []
        parts.append("**\(season) Team \(stat.displayName) Rankings**\n")

        parts.append("[LEADERBOARD]")
        parts.append("HEADER: \(stat.displayAbbrev)")
        for (i, row) in result.rows.enumerated() {
            let teamCode = row[0]
            let fullName = teamFullName(teamCode)
            let rawValue = row[1]
            let formattedValue = stat.isRate ? formatRate(rawValue) : rawValue
            parts.append("ROW \(i + 1). \(fullName): \(formattedValue)")
        }
        parts.append("[/LEADERBOARD]")

        let statName = stat.pillName
        parts.append("\n[SUGGEST]\(season) \(statName) leaders[/SUGGEST]")

        return parts.joined(separator: "\n")
    }

    /// Extract the team nickname from a Retrosheet code (e.g., "NYA" → "Yankees").
    private static func teamNickname(_ code: String) -> String {
        let full = teamFullName(code)
        // Last word of full name is typically the nickname
        let parts = full.split(separator: " ")
        if parts.count >= 2 {
            return String(parts.last!)
        }
        return full
    }

    // MARK: - Platoon splits (chat response builder)

    /// Build a STATGRID response for "Judge vs lefties" or "Soto splits" queries.
    static func buildPlatoonSplits(name: String, hand: String?, season: Int) -> String? {
        let info = fetchPlayerInfo(name: name)
        let displayName = info?.name ?? name

        var splitFilter = ""
        if let hand {
            let splitValue = hand == "LHP" ? "vs_LHP" : "vs_RHP"
            splitFilter = " AND ps.split = '\(splitValue)'"
        }

        let sql = """
            SELECT ps.split, ps.plate_appearances, ps.at_bats, ps.hits,
                   ps.doubles, ps.triples, ps.home_runs, ps.rbi,
                   ps.walks, ps.strikeouts,
                   ps.batting_avg, ps.obp, ps.slg, ps.ops, ps.iso, ps.babip
            FROM platoon_splits ps
            JOIN players p ON ps.player_id = p.player_id
            WHERE p.name LIKE '%\(sanitize(name))%' AND ps.season = \(season)\(splitFilter)
            ORDER BY ps.split
            """
        guard let result = try? db.execute(sql: sql),
              !result.rows.isEmpty else { return nil }

        let headers = ["PA", "AB", "H", "2B", "3B", "HR", "RBI", "BB", "SO", "AVG", "OBP", "SLG", "OPS", "ISO", "BABIP"]

        // Header text
        let subtitle: String
        if let hand {
            subtitle = hand == "LHP" ? "vs Left-Handed Pitchers" : "vs Right-Handed Pitchers"
        } else {
            subtitle = "Platoon Splits"
        }

        var parts: [String] = []
        parts.append("**\(displayName)** \u{2014} \(season) \(subtitle)\n")

        parts.append("[STATGRID]")
        parts.append("HEADER: " + headers.joined(separator: ", "))
        for row in result.rows.prefix(2) {
            let splitLabel = row[0] == "vs_LHP" ? "vs LHP" : "vs RHP"
            let values = formatValues(headers: headers, values: Array(row.dropFirst()))
            parts.append("ROW \(splitLabel): " + values.joined(separator: ", "))
        }
        parts.append("[/STATGRID]")

        parts.append("\n[TIP]Tap a player name for their full profile.[/TIP]")
        parts.append("\n[SUGGEST]\(displayName) \(season)[/SUGGEST]")

        return parts.joined(separator: "\n")
    }

    // MARK: - Leaderboard (chat response builder)

    /// Build a leaderboard for the given stat and scope (season, all-time single season, or career).
    /// Returns [LEADERBOARD] block with up to `limit` rows.
    static func buildLeaderboard(stat: PlayerNameMatcher.StatInfo, scope: PlayerNameMatcher.LeaderboardScope, limit: Int) -> String {
        switch scope {
        case .season(let season):
            return buildSeasonLeaderboard(stat: stat, season: season, limit: limit)
        case .allTimeSingleSeason:
            return buildAllTimeSingleSeasonLeaderboard(stat: stat, limit: limit)
        case .career:
            if stat.displayAbbrev == "OPS+" {
                return "Career OPS+ leaders require weighted season averaging, which isn't supported yet. Try **career OPS leaders** instead.\n\n[SUGGEST]career ops leaders[/SUGGEST]"
            }
            return buildCareerLeaderboard(stat: stat, limit: limit)
        }
    }

    private static func buildSeasonLeaderboard(stat: PlayerNameMatcher.StatInfo, season: Int, limit: Int) -> String {
        // Rate stats need a PA minimum
        let paMin: Int?
        if stat.isRate {
            let maxGamesSql = "SELECT MAX(games) FROM season_batting_stats WHERE season = \(season)"
            let maxGames: Int
            if let r = try? db.execute(sql: maxGamesSql), let row = r.rows.first, let val = Int(row[0]) {
                maxGames = val
            } else {
                maxGames = 162
            }
            paMin = maxGames >= 140 ? 400 : 200
        } else {
            paMin = nil
        }

        let paFilter = paMin.map { " AND s.plate_appearances >= \($0)" } ?? ""

        let sql = """
            SELECT p.name, s.\(stat.dbColumn)
            FROM season_batting_stats s
            JOIN players p ON s.player_id = p.player_id
            WHERE s.season = \(season)\(paFilter)
            ORDER BY s.\(stat.dbColumn) DESC
            LIMIT \(limit)
            """
        guard let result = try? db.execute(sql: sql),
              !result.rows.isEmpty else {
            return "No \(stat.displayName) leaders found for \(season)."
        }

        var parts: [String] = []
        parts.append("**\(season) \(stat.displayName) Leaders**\n")
        parts.append("[TIP]Tap a player name for their full profile.[/TIP]")

        parts.append("[LEADERBOARD]")
        parts.append("HEADER: \(stat.displayAbbrev)")
        for (i, row) in result.rows.enumerated() {
            let playerName = row[0]
            let rawValue = row[1]
            let formattedValue = stat.isRate ? formatRate(rawValue) : rawValue
            parts.append("ROW \(i + 1). \(playerName): \(formattedValue)")
        }
        parts.append("[/LEADERBOARD]")

        if let paMin {
            parts.append("\n_Min. \(paMin) PA._")
        }

        let statName = stat.pillName
        parts.append("\n[SUGGEST]all-time single season \(statName) leaders[/SUGGEST]")
        parts.append("[SUGGEST]career \(statName) leaders[/SUGGEST]")

        return parts.joined(separator: "\n")
    }

    private static func buildAllTimeSingleSeasonLeaderboard(stat: PlayerNameMatcher.StatInfo, limit: Int) -> String {
        let paFilter = stat.isRate ? " WHERE s.plate_appearances >= 400" : ""

        let sql = """
            SELECT p.name, s.\(stat.dbColumn), s.season
            FROM season_batting_stats s
            JOIN players p ON s.player_id = p.player_id
            \(paFilter)
            ORDER BY s.\(stat.dbColumn) DESC
            LIMIT \(limit)
            """
        guard let result = try? db.execute(sql: sql),
              !result.rows.isEmpty else {
            return "No all-time \(stat.displayName) leaders found."
        }

        var parts: [String] = []
        parts.append("**All-Time Single Season \(stat.displayName) Leaders**\n")
        parts.append("[TIP]Tap a player name for their full profile.[/TIP]")

        parts.append("[LEADERBOARD]")
        parts.append("HEADER: \(stat.displayAbbrev), Year")
        for (i, row) in result.rows.enumerated() {
            let playerName = row[0]
            let rawValue = row[1]
            let season = row[2]
            let formattedValue = stat.isRate ? formatRate(rawValue) : rawValue
            parts.append("ROW \(i + 1). \(playerName): \(formattedValue), \(season)")
        }
        parts.append("[/LEADERBOARD]")

        if stat.isRate {
            parts.append("\n_Min. 400 PA._")
        }

        let statName = stat.pillName
        parts.append("\n[SUGGEST]career \(statName) leaders[/SUGGEST]")

        return parts.joined(separator: "\n")
    }

    private static func buildCareerLeaderboard(stat: PlayerNameMatcher.StatInfo, limit: Int) -> String {
        let selectExpr: String
        if stat.isRate {
            guard let formula = careerRateFormula(for: stat) else {
                return "Career \(stat.displayName) leaders are not available."
            }
            selectExpr = "\(formula) as career_val"
        } else {
            selectExpr = "SUM(s.\(stat.dbColumn)) as career_val"
        }

        let paFilter = stat.isRate ? "\n            HAVING SUM(s.plate_appearances) >= 400" : ""

        let sql = """
            SELECT p.name, \(selectExpr)
            FROM season_batting_stats s
            JOIN players p ON s.player_id = p.player_id
            GROUP BY p.player_id\(paFilter)
            ORDER BY career_val DESC
            LIMIT \(limit)
            """
        guard let result = try? db.execute(sql: sql),
              !result.rows.isEmpty else {
            return "No career \(stat.displayName) leaders found."
        }

        var parts: [String] = []
        parts.append("**Career \(stat.displayName) Leaders**\n")
        parts.append("[TIP]Tap a player name for their full profile.[/TIP]")

        parts.append("[LEADERBOARD]")
        parts.append("HEADER: \(stat.displayAbbrev)")
        for (i, row) in result.rows.enumerated() {
            let playerName = row[0]
            let rawValue = row[1]
            let formattedValue = stat.isRate ? formatRate(rawValue) : rawValue
            parts.append("ROW \(i + 1). \(playerName): \(formattedValue)")
        }
        parts.append("[/LEADERBOARD]")

        if stat.isRate {
            parts.append("\n_Min. 400 PA._")
        }

        let statName = stat.pillName
        parts.append("\n[SUGGEST]all-time single season \(statName) leaders[/SUGGEST]")

        return parts.joined(separator: "\n")
    }

    private static func careerRateFormula(for stat: PlayerNameMatcher.StatInfo) -> String? {
        switch stat.displayAbbrev {
        case "AVG":
            return "ROUND(CAST(SUM(s.hits) AS REAL) / NULLIF(SUM(s.at_bats), 0), 3)"
        case "OBP":
            return "ROUND(CAST(SUM(s.hits) + SUM(s.walks) + SUM(s.hit_by_pitch) AS REAL) / NULLIF(SUM(s.at_bats) + SUM(s.walks) + SUM(s.hit_by_pitch) + SUM(s.sacrifice_flies), 0), 3)"
        case "SLG":
            return "ROUND(CAST((SUM(s.hits) - SUM(s.doubles) - SUM(s.triples) - SUM(s.home_runs)) + 2 * SUM(s.doubles) + 3 * SUM(s.triples) + 4 * SUM(s.home_runs) AS REAL) / NULLIF(SUM(s.at_bats), 0), 3)"
        case "OPS":
            return "ROUND(CAST(SUM(s.hits) + SUM(s.walks) + SUM(s.hit_by_pitch) AS REAL) / NULLIF(SUM(s.at_bats) + SUM(s.walks) + SUM(s.hit_by_pitch) + SUM(s.sacrifice_flies), 0) + CAST((SUM(s.hits) - SUM(s.doubles) - SUM(s.triples) - SUM(s.home_runs)) + 2 * SUM(s.doubles) + 3 * SUM(s.triples) + 4 * SUM(s.home_runs) AS REAL) / NULLIF(SUM(s.at_bats), 0), 3)"
        case "ISO":
            return "ROUND(CAST(SUM(s.doubles) + 2 * SUM(s.triples) + 3 * SUM(s.home_runs) AS REAL) / NULLIF(SUM(s.at_bats), 0), 3)"
        case "BABIP":
            return "ROUND(CAST(SUM(s.hits) - SUM(s.home_runs) AS REAL) / NULLIF(SUM(s.at_bats) - SUM(s.strikeouts) - SUM(s.home_runs) + SUM(s.sacrifice_flies), 0), 3)"
        default:
            return nil
        }
    }

    // MARK: - Current form

    static func fetchCurrentFormForSeason(name: String, season: Int) -> CurrentFormData? {
        let sql = """
            SELECT cf.form_start_date, cf.form_start_game_number, cf.total_season_games, cf.num_games,
                   cf.at_bats, cf.hits, cf.doubles, cf.triples, cf.home_runs,
                   cf.runs, cf.rbi, cf.walks, cf.strikeouts, cf.plate_appearances,
                   cf.batting_avg, cf.obp, cf.slg, cf.ops, cf.iso,
                   cf.season_at_bats, cf.season_hits, cf.season_doubles, cf.season_triples,
                   cf.season_home_runs, cf.season_runs, cf.season_rbi,
                   cf.season_walks, cf.season_strikeouts, cf.season_plate_appearances
            FROM current_form cf
            JOIN players p ON cf.player_id = p.player_id
            WHERE p.name LIKE '%\(sanitize(name))%' AND cf.season = \(season)
            LIMIT 1
            """
        guard let result = try? db.execute(sql: sql),
              let row = result.rows.first,
              row.count >= 28 else { return nil }

        let formStartDate = row[0]
        let formStartGameNumber = Int(row[1]) ?? 1
        let totalSeasonGames = Int(row[2]) ?? 0
        let numGames = Int(row[3]) ?? 0

        // Form counting stats (indices 4-13)
        let formAB = Int(row[4]) ?? 0
        let formH = Int(row[5]) ?? 0
        let formDoubles = Int(row[6]) ?? 0
        let formTriples = Int(row[7]) ?? 0
        let formHR = Int(row[8]) ?? 0
        let formR = Int(row[9]) ?? 0
        let formRBI = Int(row[10]) ?? 0
        let formBB = Int(row[11]) ?? 0
        let formSO = Int(row[12]) ?? 0
        // PA at index 13

        // Rate stats (indices 14-18)
        let avg = formatRate(row[14])
        let obp = formatRate(row[15])
        let slg = formatRate(row[16])
        let ops = formatRate(row[17])

        let formHeaders = ["G", "AB", "R", "H", "HR", "RBI", "BB", "SO", "AVG", "OBP", "SLG", "OPS"]
        let formValues = [
            String(numGames), String(formAB), String(formR), String(formH),
            String(formHR), String(formRBI), String(formBB), String(formSO),
            avg, obp, slg, ops
        ]
        let grid = StatGridParser.StatGrid(
            headers: formHeaders,
            rows: [StatGridParser.StatGrid.Row(label: "", values: formValues)]
        )

        let countingValues: [String: Double] = [
            "G": Double(numGames), "AB": Double(formAB), "R": Double(formR),
            "H": Double(formH), "2B": Double(formDoubles), "3B": Double(formTriples),
            "HR": Double(formHR), "RBI": Double(formRBI),
            "BB": Double(formBB), "SO": Double(formSO)
        ]

        // Season counting stats (indices 19-27)
        let seasonCountingValues: [String: Double] = [
            "AB": Double(Int(row[19]) ?? 0), "H": Double(Int(row[20]) ?? 0),
            "2B": Double(Int(row[21]) ?? 0), "3B": Double(Int(row[22]) ?? 0),
            "HR": Double(Int(row[23]) ?? 0), "R": Double(Int(row[24]) ?? 0),
            "RBI": Double(Int(row[25]) ?? 0), "BB": Double(Int(row[26]) ?? 0),
            "SO": Double(Int(row[27]) ?? 0)
        ]

        return CurrentFormData(
            formStartDate: formStartDate,
            formStartGameNumber: formStartGameNumber,
            totalSeasonGames: totalSeasonGames,
            numGames: numGames,
            stats: grid,
            countingValues: countingValues,
            seasonCountingValues: seasonCountingValues
        )
    }

    // MARK: - Game logs for slider

    static func fetchGameLogsForSeason(name: String, season: Int) -> [GameLog] {
        let sql = """
            SELECT g.date, g.at_bats, g.hits, g.doubles, g.triples, g.home_runs,
                   g.runs, g.rbi, g.walks, g.strikeouts, g.plate_appearances
            FROM game_batting_logs g
            JOIN players p ON g.player_id = p.player_id
            WHERE p.name LIKE '%\(sanitize(name))%' AND g.season = \(season)
            ORDER BY g.date ASC
            """
        guard let result = try? db.execute(sql: sql, maxRows: 0) else { return [] }
        return result.rows.compactMap { row -> GameLog? in
            guard row.count >= 11 else { return nil }
            return GameLog(
                date: row[0],
                atBats: Int(row[1]) ?? 0,
                hits: Int(row[2]) ?? 0,
                doubles: Int(row[3]) ?? 0,
                triples: Int(row[4]) ?? 0,
                homeRuns: Int(row[5]) ?? 0,
                runs: Int(row[6]) ?? 0,
                rbi: Int(row[7]) ?? 0,
                walks: Int(row[8]) ?? 0,
                strikeouts: Int(row[9]) ?? 0,
                plateAppearances: Int(row[10]) ?? 0
            )
        }
    }

    // MARK: - Platoon splits (all seasons)

    private static func fetchPlatoonSplits(name: String) -> StatGridParser.StatGrid? {
        let sql = """
            SELECT ps.split, ps.plate_appearances, ps.at_bats, ps.hits,
                   ps.doubles, ps.triples, ps.home_runs, ps.rbi,
                   ps.walks, ps.strikeouts,
                   ps.batting_avg, ps.obp, ps.slg, ps.ops, ps.iso, ps.babip
            FROM platoon_splits ps
            JOIN players p ON ps.player_id = p.player_id
            WHERE p.name LIKE '%\(sanitize(name))%'
            ORDER BY ps.season DESC, ps.split
            """
        guard let result = try? db.execute(sql: sql),
              !result.rows.isEmpty else { return nil }

        let headers = ["PA", "AB", "H", "2B", "3B", "HR", "RBI", "BB", "SO", "AVG", "OBP", "SLG", "OPS", "ISO", "BABIP"]

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
            SELECT st.start_date, st.end_date, st.num_games,
                   st.at_bats, st.hits, st.walks, st.strikeouts,
                   st.batting_avg, st.obp, st.slg, st.ops, st.home_runs
            FROM streaks st
            JOIN players p ON st.player_id = p.player_id
            WHERE p.name LIKE '%\(sanitize(name))%' AND st.performance = 'hot'
            ORDER BY st.season DESC, st.ops DESC
            """
        var result = try? db.execute(sql: sql)

        // Fallback to sensitive streaks if no hot rows at all
        if result == nil || result!.rows.isEmpty {
            sql = """
                SELECT ss.start_date, ss.end_date, ss.num_games,
                       ss.at_bats, ss.hits, ss.walks, ss.strikeouts,
                       ss.batting_avg, ss.obp, ss.slg, ss.ops, ss.home_runs
                FROM streaks_sensitive ss
                JOIN players p ON ss.player_id = p.player_id
                WHERE p.name LIKE '%\(sanitize(name))%' AND ss.performance = 'hot'
                ORDER BY ss.season DESC, ss.ops DESC
                """
            result = try? db.execute(sql: sql)
        }

        // Tier 3: sliding window fallback
        if result == nil || result!.rows.isEmpty {
            sql = """
                SELECT sl.start_date, sl.end_date, sl.num_games,
                       sl.at_bats, sl.hits, sl.walks, sl.strikeouts,
                       sl.batting_avg, sl.obp, sl.slg, sl.ops, sl.home_runs
                FROM streaks_sliding sl
                JOIN players p ON sl.player_id = p.player_id
                WHERE p.name LIKE '%\(sanitize(name))%' AND sl.performance = 'hot'
                ORDER BY sl.season DESC, sl.ops DESC
                """
            result = try? db.execute(sql: sql)
        }

        guard let result, !result.rows.isEmpty else { return nil }

        let headers = ["G", "AB", "H", "BB", "SO", "AVG", "OBP", "SLG", "OPS", "HR"]
        var rows: [StatGridParser.StatGrid.Row] = []
        for row in result.rows.prefix(4) {
            let startDate = formatDate(row[0])
            let endDate = formatDate(row[1])
            let label = "\(startDate) \u{2013} \(endDate)"
            let games = row[2]
            let ab = row[3]
            let hits = row[4]
            let walks = row[5]
            let so = row[6]
            let avg = formatRate(row[7])
            let obp = formatRate(row[8])
            let slg = formatRate(row[9])
            let ops = formatRate(row[10])
            let hr = row[11]
            rows.append(StatGridParser.StatGrid.Row(
                label: label,
                values: [games, ab, hits, walks, so, avg, obp, slg, ops, hr]
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

    /// Public date formatter for use in views (e.g., "Jun 12")
    static func formatDateShort(_ dateString: String) -> String {
        formatDate(dateString)
    }

    private static func formatValues(headers: [String], values: [String]) -> [String] {
        let rateStats: Set<String> = ["AVG", "OBP", "SLG", "OPS", "ISO", "BABIP"]
        var formatted: [String] = []
        for (idx, value) in values.enumerated() {
            if idx < headers.count && rateStats.contains(headers[idx]) {
                formatted.append(formatRate(value))
            } else {
                formatted.append(value)
            }
        }
        return formatted
    }

    private static func teamFullName(_ abbreviation: String) -> String {
        let teams: [String: String] = [
            // Standard abbreviations
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
            // Retrosheet abbreviations
            "NYA": "New York Yankees", "NYN": "New York Mets",
            "CHN": "Chicago Cubs", "CHA": "Chicago White Sox",
            "SLN": "St. Louis Cardinals", "SFN": "San Francisco Giants",
            "SDN": "San Diego Padres", "LAN": "Los Angeles Dodgers",
            "TBA": "Tampa Bay Rays", "KCA": "Kansas City Royals",
            "ANA": "Los Angeles Angels", "WAS": "Washington Nationals",
            "FLO": "Florida Marlins", "MON": "Montreal Expos",
            "ATH": "Oakland Athletics",
        ]
        return teams[abbreviation] ?? abbreviation
    }

    /// Expand a team string that may contain "/" for multi-team seasons (e.g., "MIA/NYA" → "Miami Marlins / New York Yankees")
    static func teamDisplayName(_ teamStr: String) -> String {
        let parts = teamStr.split(separator: "/")
        if parts.count > 1 {
            return parts.map { teamFullName(String($0)) }.joined(separator: " / ")
        }
        return teamFullName(teamStr)
    }
}
