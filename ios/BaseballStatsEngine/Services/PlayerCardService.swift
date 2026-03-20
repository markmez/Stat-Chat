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

struct PitchingSeasonData: Sendable {
    let year: Int
    let team: String
    let games: Int
    let gamesStarted: Int
    let teamGames: Int
    let stats: StatGridParser.StatGrid
    let countingValues: [String: Double]
    let platoonSplits: StatGridParser.StatGrid?
    let homeAwaySplits: StatGridParser.StatGrid?
    let rispSplits: StatGridParser.StatGrid?
    let streaks: StatGridParser.StatGrid?
    let pitchTypeSplits: [StatGridParser.StatGrid]?
    let countSplits: [StatGridParser.StatGrid]?
    let currentForm: PitchingCurrentFormData?
}

struct PitchingCurrentFormData: Sendable {
    let formStartDate: String
    let formStartGameNumber: Int
    let totalSeasonGames: Int
    let numGames: Int
    let role: String
    let stats: StatGridParser.StatGrid
    let countingValues: [String: Double]
    let seasonCountingValues: [String: Double]
}

struct PitchingGameLog: Sendable {
    let date: String
    let ipOuts: Int
    let hits: Int
    let earnedRuns: Int
    let walks: Int
    let strikeouts: Int
    let homeRuns: Int
    let isStart: Bool
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
    let rispSplits: StatGridParser.StatGrid?
    let streaks: StatGridParser.StatGrid?
    let fieldingStats: StatGridParser.StatGrid?
    let pitchTypeSplits: [StatGridParser.StatGrid]?
    let countSplits: [StatGridParser.StatGrid]?
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
    let careerPlatoonSplits: StatGridParser.StatGrid?
    let careerHomeAwaySplits: StatGridParser.StatGrid?
    let platoonSplits: StatGridParser.StatGrid?
    let streaks: StatGridParser.StatGrid?
    let bio: String?
    let isPitcher: Bool
    let isTwoWay: Bool
    let pitchingSeasons: [PitchingSeasonData]?
    let pitchingCareerTotals: StatGridParser.StatGrid?
    let pitchingCareerPlatoonSplits: StatGridParser.StatGrid?
    let pitchingCareerHomeAwaySplits: StatGridParser.StatGrid?
}

// MARK: - Team Card models

struct TeamCard: Sendable {
    let teamCode: String
    let fullName: String
    let seasons: [TeamSeasonData]
}

struct RosterEntry: Sendable {
    let name: String
    let position: String
}

struct TeamSeasonData: Sendable {
    let year: Int
    let stats: StatGridParser.StatGrid
    let pitchingStats: StatGridParser.StatGrid?
    let leaders: [StatLeader]
    let roster: [RosterEntry]
}

struct StatLeader: Sendable {
    let category: String
    let name: String
    let value: String
}

@MainActor
enum PlayerCardService {

    private static let db = DatabaseService()
    private static let backendService = BackendService()

    /// The range of seasons available in the local bundled DB.
    static let localMinYear = 2016
    static let localMaxYear = 2025

    /// Map AL/NL league to Retrosheet team codes for SQL filtering.
    /// The DB has no `league` column — we filter by team membership instead.
    private static let alTeams = "('ANA','ATH','BAL','BOS','CHA','CLE','DET','HOU','KCA','MIN','NYA','SEA','TBA','TEX','TOR')"
    private static let nlTeams = "('ARI','ATL','CHN','CIN','COL','LAN','MIA','MIL','NYN','PHI','PIT','SDN','SFN','SLN','WAS')"

    private static func leagueTeamClause(_ league: String, alias: String) -> String {
        let teams = league == "AL" ? alTeams : nlTeams
        return "\(alias).team IN \(teams)"
    }

    /// Whether a given season is within local DB range.
    static func isLocalSeason(_ year: Int) -> Bool {
        year >= localMinYear && year <= localMaxYear
    }

    /// Whether a player's career might extend beyond local DB range.
    /// Returns true if their earliest local season is at the boundary (2016),
    /// meaning they likely have pre-2016 data we're missing.
    static func playerNeedsBackendForCareer(name: String) -> Bool {
        let sql = """
            SELECT MIN(s.season) FROM season_batting_stats s
            JOIN players p ON s.player_id = p.player_id
            WHERE p.name = '\(sanitize(name))'
            """
        guard let result = try? db.execute(sql: sql),
              let row = result.rows.first,
              let minSeason = Int(row[0]) else {
            return true  // no local data at all → needs backend
        }
        return minSeason < localMinYear
    }

    /// Check if a player has any season data in the local DB.
    static func hasLocalData(name: String) -> Bool {
        let sql = """
            SELECT 1 FROM season_batting_stats s
            JOIN players p ON s.player_id = p.player_id
            WHERE p.name = '\(sanitize(name))'
            LIMIT 1
            """
        if let result = try? db.execute(sql: sql), !result.rows.isEmpty {
            return true
        }
        let pitchSql = """
            SELECT 1 FROM season_pitching_stats sp
            JOIN players p ON sp.player_id = p.player_id
            WHERE p.name = '\(sanitize(name))'
            LIMIT 1
            """
        if let result = try? db.execute(sql: pitchSql), !result.rows.isEmpty {
            return true
        }
        return false
    }

    /// Check if a player is primarily a pitcher
    static func isPitcher(name: String) -> Bool {
        let sql = """
            SELECT p.positions FROM players p
            WHERE p.name = '\(sanitize(name))'
            LIMIT 1
            """
        guard let result = try? db.execute(sql: sql),
              let row = result.rows.first,
              !row[0].isEmpty else { return false }
        let positions = row[0]
        // Primary position (first in slash-separated list) must be "P"
        guard positions.hasPrefix("P") else { return false }
        // Also must have pitching stats
        let pitchSql = """
            SELECT 1 FROM season_pitching_stats sp
            JOIN players p ON sp.player_id = p.player_id
            WHERE p.name = '\(sanitize(name))'
            LIMIT 1
            """
        if let pitchResult = try? db.execute(sql: pitchSql),
           !pitchResult.rows.isEmpty {
            return true
        }
        return false
    }

    /// Check if a player is active (last season within 1 year of current calendar year).
    static func isActivePlayer(name: String) -> Bool {
        let currentYear = Calendar.current.component(.year, from: Date())
        let sql = """
            SELECT COALESCE(last_season, 0) FROM players
            WHERE name = '\(sanitize(name))'
            """
        if let result = try? db.execute(sql: sql),
           let row = result.rows.first,
           let year = Int(row[0]),
           year >= currentYear - 1 {
            return true
        }
        return false
    }

    /// Check if platoon split data exists for a player (1969+ Chadwick or 2025+ MSF).
    static func hasPlayerPlatoonData(name: String) -> Bool {
        guard let recent = mostRecentSeason(name: name) else { return false }
        return recent >= 1969
    }

    /// Find a player's most recent season year (batting or pitching).
    static func mostRecentSeason(name: String) -> Int? {
        let sql = """
            SELECT last_season FROM players
            WHERE name = '\(sanitize(name))' AND last_season > 0
            """
        if let result = try? db.execute(sql: sql),
           let row = result.rows.first,
           let year = Int(row[0]),
           year > 0 {
            return year
        }
        return nil
    }

    /// Find a player's best season year by OPS (batters) or ERA (pitchers).
    /// With backend-only architecture, we don't have season stats locally.
    /// Returns most recent season as a reasonable fallback.
    static func bestSeasonYear(name: String, isPitcher: Bool) -> Int? {
        return mostRecentSeason(name: name)
    }

    // MARK: - Universal season resolution

    /// Resolve the effective season for a player query when no year was specified.
    /// Active players → current year. Inactive players → most recent season in DB.
    static func resolveEffectiveSeason(playerName: String, parsedSeason: Int, seasonExplicit: Bool) -> Int {
        if !seasonExplicit && !isActivePlayer(name: playerName) {
            return mostRecentSeason(name: playerName) ?? parsedSeason
        }
        return parsedSeason
    }

    /// Resolve the effective season for a team query when no year was specified.
    /// Active teams → current year. Defunct/inactive teams → most recent season in DB.
    static func resolveEffectiveTeamSeason(teamCode: String, parsedSeason: Int, seasonExplicit: Bool) -> Int {
        if !seasonExplicit && !isActiveTeam(teamCode: teamCode) {
            return mostRecentTeamSeason(teamCode: teamCode) ?? parsedSeason
        }
        return parsedSeason
    }

    /// Check if a team has data in the current or previous season.
    static func isActiveTeam(teamCode: String) -> Bool {
        let currentYear = Calendar.current.component(.year, from: Date())
        let sql = "SELECT MAX(season) FROM season_batting_stats WHERE team = '\(sanitize(teamCode))'"
        if let result = try? db.execute(sql: sql),
           let row = result.rows.first,
           let year = Int(row[0]) {
            return year >= currentYear - 1
        }
        return false
    }

    /// Find a team's most recent season in the DB.
    static func mostRecentTeamSeason(teamCode: String) -> Int? {
        let sql = "SELECT MAX(season) FROM season_batting_stats WHERE team = '\(sanitize(teamCode))'"
        if let result = try? db.execute(sql: sql),
           let row = result.rows.first,
           let year = Int(row[0]) {
            return year
        }
        return nil
    }

    /// Generate a suggestion pill for a player query based on season resolution.
    /// - `queryLabel`: the query fragment without the year, e.g. "Judge home runs", "Judge vs lefties"
    /// - `careerLabel`: if non-nil, active players get this career pill (e.g. "Judge career HR").
    ///   If nil (splits with no career builder), active players get a previous-year pill instead.
    static func makeSeasonPill(
        name: String, queryLabel: String, careerLabel: String?,
        effectiveSeason: Int, seasonExplicit: Bool, isPitcher: Bool
    ) -> String? {
        guard !seasonExplicit else { return nil }
        if !isActivePlayer(name: name) {
            // Inactive → suggest their best year
            if let bestYear = bestSeasonYear(name: name, isPitcher: isPitcher) {
                return "\n[SUGGEST]\(queryLabel) \(bestYear)[/SUGGEST]"
            }
            return nil
        } else if let careerLabel {
            // Active + career builder available → suggest career
            return "\n[SUGGEST]\(careerLabel)[/SUGGEST]"
        } else {
            // Active + no career builder → suggest previous year
            let prevYear = effectiveSeason - 1
            return "\n[SUGGEST]\(queryLabel) \(prevYear)[/SUGGEST]"
        }
    }

    /// Generate a suggestion pill for a team query based on season resolution.
    static func makeTeamSeasonPill(
        teamCode: String, queryLabel: String,
        effectiveSeason: Int, seasonExplicit: Bool
    ) -> String? {
        guard !seasonExplicit else { return nil }
        let prevYear = effectiveSeason - 1
        return "\n[SUGGEST]\(queryLabel) \(prevYear)[/SUGGEST]"
    }

    /// Detect two-way players: meaningful batting stats (PA >= 130) AND meaningful pitching stats (ip_outs >= 90, ~30 IP).
    /// PA threshold of 130 exceeds what any pitcher would accumulate just from batting in their own starts (~4 PA × 32 GS = 128).
    /// IP threshold of 30 filters out position players doing blowout mop-up duty.
    static func isTwoWayPlayer(name: String) -> Bool {
        let batSql = """
            SELECT 1 FROM season_batting_stats s
            JOIN players p ON s.player_id = p.player_id
            WHERE p.name = '\(sanitize(name))' AND s.plate_appearances >= 130
            LIMIT 1
            """
        guard let batResult = try? db.execute(sql: batSql),
              !batResult.rows.isEmpty else { return false }

        let pitchSql = """
            SELECT 1 FROM season_pitching_stats sp
            JOIN players p ON sp.player_id = p.player_id
            WHERE p.name = '\(sanitize(name))' AND sp.ip_outs >= 90
            LIMIT 1
            """
        guard let pitchResult = try? db.execute(sql: pitchSql),
              !pitchResult.rows.isEmpty else { return false }

        return true
    }

    // All pitching stats — G first for career display, then conventional order
    private static let pitchingAllHeaders = [
        "G", "W", "L", "SV", "GS", "GF", "CG", "QS", "IP", "H", "R", "ER", "HR", "BB", "IBB",
        "SO", "HBP", "WP", "BK", "BF", "SH", "SF", "SB", "CS",
        "ERA", "WHIP", "K/9", "BB/9", "K/BB", "H/9", "HR/9", "BAA", "ERA+"
    ]

    // Stats hidden from display
    private static let pitchingHiddenStats: Set<String> = ["GF", "IBB", "BF", "SF", "SH", "K/BB"]

    // Display headers (filtered)
    private static let pitchingHeaders = pitchingAllHeaders.filter { !pitchingHiddenStats.contains($0) }

    /// Filter full pitching values (aligned to pitchingAllHeaders) down to display set.
    private static func filterPitchingForDisplay(_ values: [String]) -> [String] {
        var filtered: [String] = []
        for (i, h) in pitchingAllHeaders.enumerated() where !pitchingHiddenStats.contains(h) {
            if i < values.count {
                filtered.append(values[i])
            }
        }
        return filtered
    }

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

        // Detect if player is primarily a pitcher or a two-way player
        let playerIsPitcher = isPitcher(name: name)
        let playerIsTwoWay = !playerIsPitcher && isTwoWayPlayer(name: name)

        // Always try the backend first — it has cron-refreshed current season data.
        // The bundled DB is a release-time snapshot and may be missing 2026+ data.
        // Fall back to local only if the backend request fails.
        if let card = await fetchFromBackend(name: name) {
            return card
        }

        // Backend unavailable — fall back to local bundled DB
        let seasons = fetchAllSeasons(name: name)

        // Career totals: use backend if player's career extends before local DB range
        let career: StatGridParser.StatGrid?
        if playerNeedsBackendForCareer(name: name) {
            // Fetch full career from backend, fall back to local
            let backendCareer = await backendCareerGrid(for: name)
            career = backendCareer ?? fetchCareerTotals(name: name)
        } else {
            career = fetchCareerTotals(name: name)
        }
        let splits = fetchPlatoonSplits(name: name)
        let streakGrid = fetchStreaks(name: name)

        // Career splits (only meaningful with multiple seasons)
        let careerPlatoon = fetchCareerPlatoonSplits(name: name)
        let careerHomeAway = fetchCareerHomeAwaySplits(name: name)

        // Fetch pitching stats if pitcher or two-way player
        let pitchingSeasons: [PitchingSeasonData]?
        let pitchingCareer: StatGridParser.StatGrid?
        let pitchingCareerPlatoon: StatGridParser.StatGrid?
        let pitchingCareerHomeAway: StatGridParser.StatGrid?
        if playerIsPitcher || playerIsTwoWay {
            pitchingSeasons = fetchPitchingAllSeasons(name: name)
            pitchingCareer = fetchPitchingCareerTotals(name: name)
            pitchingCareerPlatoon = nil  // No ERA data per split — can't show meaningful pitching stats
            pitchingCareerHomeAway = nil
        } else {
            pitchingSeasons = nil
            pitchingCareer = nil
            pitchingCareerPlatoon = nil
            pitchingCareerHomeAway = nil
        }

        // Use most recent season's team for header — compare batting and pitching years, pick whichever is newer
        let headerTeam: String
        let latestBattingYear = seasons.first?.year ?? 0
        let latestPitchingYear = pitchingSeasons?.first?.year ?? 0
        if latestPitchingYear > latestBattingYear, let latestPitching = pitchingSeasons?.first {
            let parts = latestPitching.team.split(separator: "/")
            headerTeam = String(parts.last ?? Substring(team))
        } else if let latestSeason = seasons.first {
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
            careerPlatoonSplits: careerPlatoon,
            careerHomeAwaySplits: careerHomeAway,
            platoonSplits: splits,
            streaks: streakGrid,
            bio: bio,
            isPitcher: playerIsPitcher,
            isTwoWay: playerIsTwoWay,
            pitchingSeasons: pitchingSeasons,
            pitchingCareerTotals: pitchingCareer,
            pitchingCareerPlatoonSplits: pitchingCareerPlatoon,
            pitchingCareerHomeAwaySplits: pitchingCareerHomeAway
        )
    }

    // MARK: - Backend fallback for historical players

    /// Fetch player card data from the backend for players not in the local DB.
    private static func fetchFromBackend(name: String) async -> PlayerCard? {
        guard let data = try? await backendService.fetchPlayerCard(name: name) else {
            return nil
        }
        // Must have actual season data from backend
        guard !data.batting_seasons.isEmpty || !data.pitching_seasons.isEmpty else {
            return nil
        }

        let info = data.player_info
        let displayName = info?.name ?? name
        let infoTeam = info?.team ?? ""

        // Build per-season splits lookup by year
        var battingSplitsByYear: [Int: BackendService.SeasonSplitsData] = [:]
        for ss in data.season_splits ?? [] {
            battingSplitsByYear[ss.year] = ss
        }
        var pitchingSplitsByYear: [Int: BackendService.PitchingSeasonSplitsData] = [:]
        for pss in data.pitching_season_splits ?? [] {
            pitchingSplitsByYear[pss.year] = pss
        }

        // Convert current form
        let battingCurrentForm: CurrentFormData? = data.current_form.map { cf in
            CurrentFormData(
                formStartDate: cf.form_start_date,
                formStartGameNumber: cf.form_start_game_number,
                totalSeasonGames: cf.total_season_games,
                numGames: cf.num_games,
                stats: convertSplitGrid(cf.stats),
                countingValues: cf.counting_values,
                seasonCountingValues: cf.season_counting_values
            )
        }

        // Convert backend batting seasons to local SeasonData
        let seasons = data.batting_seasons.enumerated().map { (index, s) in
            let values = [
                "\(s.G)", "\(s.AB)", "\(s.R)", "\(s.H)", "\(s.doubles)", "\(s.triples)",
                "\(s.HR)", "\(s.RBI)", "\(s.SB)", "\(s.CS)", "\(s.BB)", "\(s.IBB)",
                "\(s.SO)", "\(s.HBP)", formatRate(s.AVG), formatRate(s.OBP), formatRate(s.SLG), formatRate(s.OPS), s.OPS_plus, formatRate(s.ISO), formatRate(s.BABIP)
            ]
            let grid = StatGridParser.StatGrid(
                headers: allHeaders,
                rows: [StatGridParser.StatGrid.Row(label: "", values: values)]
            )
            let counting: [String: Double] = [
                "G": Double(s.G), "AB": Double(s.AB), "R": Double(s.R), "H": Double(s.H),
                "2B": Double(s.doubles), "3B": Double(s.triples), "HR": Double(s.HR),
                "RBI": Double(s.RBI), "SB": Double(s.SB), "CS": Double(s.CS),
                "BB": Double(s.BB), "IBB": Double(s.IBB), "SO": Double(s.SO), "HBP": Double(s.HBP),
            ]

            // Look up per-season splits from backend
            let ss = battingSplitsByYear[s.year]
            let platoon = ss?.platoon.map { convertSplitGrid($0) }
            let homeAway = ss?.home_away.map { convertSplitGrid($0) }
            let risp = ss?.risp.map { convertSplitGrid($0) }
            let streakGrid = ss?.streaks.map { convertSplitGrid($0) }
            let fieldingGrid = ss?.fielding.map { convertSplitGrid($0) }
            let pitchTypeGrids: [StatGridParser.StatGrid]? = ss?.pitch_type?.map { convertSplitGrid($0) }
            let countGrids: [StatGridParser.StatGrid]? = ss?.count?.map { convertSplitGrid($0) }

            // Current form only for most recent season (index 0)
            let seasonCurrentForm: CurrentFormData? = index == 0 ? battingCurrentForm : nil

            return SeasonData(
                year: s.year, team: s.team, age: s.age, games: s.G, teamGames: 162,
                stats: grid, countingValues: counting,
                platoonSplits: platoon, homeAwaySplits: homeAway, rispSplits: risp,
                streaks: streakGrid, fieldingStats: fieldingGrid,
                pitchTypeSplits: pitchTypeGrids, countSplits: countGrids, currentForm: seasonCurrentForm
            )
        }

        // Convert pitching current form
        let pitchCurrentForm: PitchingCurrentFormData? = data.pitching_current_form.map { pcf in
            PitchingCurrentFormData(
                formStartDate: pcf.form_start_date,
                formStartGameNumber: pcf.form_start_game_number,
                totalSeasonGames: pcf.total_season_games,
                numGames: pcf.num_games,
                role: pcf.role,
                stats: convertSplitGrid(pcf.stats),
                countingValues: pcf.counting_values,
                seasonCountingValues: pcf.season_counting_values
            )
        }

        // Convert backend pitching seasons
        let pitchingSeasons: [PitchingSeasonData]? = data.pitching_seasons.isEmpty ? nil : data.pitching_seasons.enumerated().map { (index, s) in
            let values = [
                "\(s.G)", "\(s.W)", "\(s.L)", "\(s.SV)", "\(s.GS)", "\(s.GF)",
                "\(s.CG)", "\(s.QS)", s.IP, "\(s.H)", "\(s.R)", "\(s.ER)",
                "\(s.HR)", "\(s.BB)", "\(s.IBB)", "\(s.SO)", "\(s.HBP)", "\(s.WP)",
                "\(s.BK)", "\(s.BF)", "\(s.SH)", "\(s.SF)", "\(s.SB_allowed)", "\(s.CS_allowed)",
                s.ERA, s.WHIP, s.K9, s.BB9, s.K_BB, s.H9, s.HR9, formatRate(s.BAA), s.ERA_plus
            ]
            let displayValues = filterPitchingForDisplay(values)
            let grid = StatGridParser.StatGrid(
                headers: pitchingHeaders,
                rows: [StatGridParser.StatGrid.Row(label: "", values: displayValues)]
            )
            let counting: [String: Double] = [
                "W": Double(s.W), "L": Double(s.L), "SV": Double(s.SV),
                "G": Double(s.G), "GS": Double(s.GS), "GF": Double(s.GF),
                "CG": Double(s.CG), "QS": Double(s.QS),
                "SO": Double(s.SO), "BB": Double(s.BB), "IBB": Double(s.IBB),
                "H": Double(s.H), "R": Double(s.R), "ER": Double(s.ER),
                "HR": Double(s.HR), "HBP": Double(s.HBP), "WP": Double(s.WP),
                "BK": Double(s.BK), "BF": Double(s.BF),
                "SH": Double(s.SH), "SF": Double(s.SF),
                "SB": Double(s.SB_allowed), "CS": Double(s.CS_allowed),
                "IP": Double(s.IP) ?? 0,
            ]

            // Look up per-season pitching splits from backend
            let pss = pitchingSplitsByYear[s.year]
            let platoon = pss?.platoon.map { convertSplitGrid($0) }
            let homeAway = pss?.home_away.map { convertSplitGrid($0) }
            let risp = pss?.risp.map { convertSplitGrid($0) }
            let streakGrid = pss?.streaks.map { convertSplitGrid($0) }
            let pitchTypeGrids: [StatGridParser.StatGrid]? = pss?.pitch_type?.map { convertSplitGrid($0) }
            let countGrids: [StatGridParser.StatGrid]? = pss?.count?.map { convertSplitGrid($0) }

            let seasonPitchCurrentForm: PitchingCurrentFormData? = index == 0 ? pitchCurrentForm : nil

            return PitchingSeasonData(
                year: s.year, team: s.team, games: s.G, gamesStarted: s.GS,
                teamGames: 162, stats: grid, countingValues: counting,
                platoonSplits: platoon, homeAwaySplits: homeAway, rispSplits: risp,
                streaks: streakGrid, pitchTypeSplits: pitchTypeGrids, countSplits: countGrids,
                currentForm: seasonPitchCurrentForm
            )
        }

        // Determine header team from most recent season
        let headerTeam: String
        if let latest = seasons.first {
            let parts = latest.team.split(separator: "/")
            headerTeam = String(parts.last ?? Substring(infoTeam))
        } else if let latest = pitchingSeasons?.first {
            let parts = latest.team.split(separator: "/")
            headerTeam = String(parts.last ?? Substring(infoTeam))
        } else {
            headerTeam = infoTeam
        }

        // Compute career totals from backend seasons
        let career = buildCareerTotals(from: seasons)
        let pitchingCareer = pitchingSeasons.flatMap { buildPitchingCareerTotals(from: $0) }

        // Parse birthdate
        var birthDate: Date?
        var dynamicAge: Int?
        if let bdString = info?.birthdate {
            let fmt = DateFormatter()
            fmt.dateFormat = "yyyy-MM-dd"
            if let date = fmt.date(from: bdString) {
                birthDate = date
                dynamicAge = Calendar.current.dateComponents([.year], from: date, to: Date()).year
            }
        }

        let bio = await fetchWikipediaBio(name: displayName)

        // Convert backend split grids to StatGridParser.StatGrid
        let careerPlatoon = data.career_platoon_splits.map { convertSplitGrid($0) }
        let careerHomeAway = data.career_home_away_splits.map { convertSplitGrid($0) }
        let pitchingCareerPlatoon = data.pitching_career_platoon_splits.map { convertSplitGrid($0) }
        let pitchingCareerHomeAway = data.pitching_career_home_away_splits.map { convertSplitGrid($0) }

        // Top-level platoon + streaks from most recent season
        let recentPlatoon = seasons.first?.platoonSplits
        let recentStreaks = seasons.first?.streaks

        return PlayerCard(
            name: displayName,
            team: headerTeam,
            fullTeamName: teamFullName(headerTeam),
            age: dynamicAge,
            birthdate: birthDate,
            positions: info?.positions,
            bats: info?.bats,
            throws_: info?.throws,
            seasons: seasons,
            careerTotals: career,
            careerPlatoonSplits: careerPlatoon,
            careerHomeAwaySplits: careerHomeAway,
            platoonSplits: recentPlatoon,
            streaks: recentStreaks,
            bio: bio,
            isPitcher: data.is_pitcher,
            isTwoWay: data.is_two_way,
            pitchingSeasons: pitchingSeasons,
            pitchingCareerTotals: pitchingCareer,
            pitchingCareerPlatoonSplits: pitchingCareerPlatoon,
            pitchingCareerHomeAwaySplits: pitchingCareerHomeAway
        )
    }

    /// Convert a backend SplitGridData to StatGridParser.StatGrid.
    /// Rate stat headers — values should be .XXX format (no leading zero unless >= 1.000)
    private static let rateHeaders: Set<String> = [
        "AVG", "OBP", "SLG", "OPS", "ISO", "BABIP", "BAA", "FLD%"
    ]
    /// Stats that should stay as decimal (not integers, not rate-formatted)
    private static let decimalHeaders: Set<String> = [
        "ERA", "WHIP", "K/9", "BB/9", "K_BB", "H/9", "HR/9", "K9", "BB9", "H9", "HR9"
    ]

    private static func convertSplitGrid(_ grid: BackendService.SplitGridData) -> StatGridParser.StatGrid {
        let rows = grid.rows.map { row in
            let formatted = zip(grid.headers, row.values).map { header, value in
                formatSplitValue(value, header: header)
            }
            return StatGridParser.StatGrid.Row(label: row.label, values: formatted)
        }
        return StatGridParser.StatGrid(headers: grid.headers, rows: rows)
    }

    /// Format a split grid value based on its header type.
    private static func formatSplitValue(_ value: String, header: String) -> String {
        guard value != "--" else { return value }
        // IP is pre-formatted from ip_outs (e.g. "69.2" = 69⅔ innings) — pass through
        if header == "IP" { return value }
        guard let num = Double(value) else { return value }
        if rateHeaders.contains(header) {
            // Rate stat: 3 decimal places, strip leading zero
            return formatRate(String(format: "%.3f", num))
        } else if decimalHeaders.contains(header) {
            // Decimal stat (ERA, WHIP, etc.): keep appropriate precision
            if header == "ERA" || header == "WHIP" || header == "K_BB" {
                return String(format: "%.2f", num)
            } else {
                return String(format: "%.1f", num)
            }
        } else {
            // Counting stat: integer display
            return "\(Int(num))"
        }
    }

    /// Build career totals grid from an array of SeasonData (for backend-sourced players).
    private static func buildCareerTotals(from seasons: [SeasonData]) -> StatGridParser.StatGrid? {
        guard seasons.count > 1 else { return nil }
        var totals: [String: Double] = [:]
        let countingKeys = ["G", "AB", "R", "H", "2B", "3B", "HR", "RBI", "SB", "CS", "BB", "IBB", "SO", "HBP"]
        for s in seasons {
            for key in countingKeys {
                totals[key, default: 0] += s.countingValues[key] ?? 0
            }
        }
        let ab = totals["AB"] ?? 0
        let h = totals["H"] ?? 0
        let bb = totals["BB"] ?? 0
        let hbp = totals["HBP"] ?? 0
        let sf = 0.0  // not tracked in counting
        let pa = ab + bb + hbp + sf
        let singles = h - (totals["2B"] ?? 0) - (totals["3B"] ?? 0) - (totals["HR"] ?? 0)
        let tb = singles + 2 * (totals["2B"] ?? 0) + 3 * (totals["3B"] ?? 0) + 4 * (totals["HR"] ?? 0)

        let avg = ab > 0 ? h / ab : 0
        let obp = pa > 0 ? (h + bb + hbp) / pa : 0
        let slg = ab > 0 ? tb / ab : 0
        let ops = obp + slg
        let iso = slg - avg

        // BABIP = (H - HR) / (AB - SO - HR + SF)
        let babipDenom = ab - (totals["SO"] ?? 0) - (totals["HR"] ?? 0)
        let babip = babipDenom > 0 ? (h - (totals["HR"] ?? 0)) / babipDenom : 0

        let values = [
            "\(Int(totals["G"] ?? 0))", "\(Int(ab))", "\(Int(totals["R"] ?? 0))",
            "\(Int(h))", "\(Int(totals["2B"] ?? 0))", "\(Int(totals["3B"] ?? 0))",
            "\(Int(totals["HR"] ?? 0))", "\(Int(totals["RBI"] ?? 0))",
            "\(Int(totals["SB"] ?? 0))", "\(Int(totals["CS"] ?? 0))",
            "\(Int(bb))", "\(Int(totals["IBB"] ?? 0))",
            "\(Int(totals["SO"] ?? 0))", "\(Int(hbp))",
            formatRate(String(format: "%.3f", avg)), formatRate(String(format: "%.3f", obp)),
            formatRate(String(format: "%.3f", slg)), formatRate(String(format: "%.3f", ops)),
            "--", formatRate(String(format: "%.3f", iso)), formatRate(String(format: "%.3f", babip)),
        ]
        return StatGridParser.StatGrid(
            headers: allHeaders,
            rows: [StatGridParser.StatGrid.Row(label: "\(seasons.count) Seasons", values: values)]
        )
    }

    /// Build pitching career totals from an array of PitchingSeasonData.
    private static func buildPitchingCareerTotals(from seasons: [PitchingSeasonData]) -> StatGridParser.StatGrid? {
        guard seasons.count > 1 else { return nil }
        // Accumulate all counting stats from per-season data
        let keys = ["W", "L", "SV", "G", "GS", "GF", "CG", "QS",
                     "H", "R", "ER", "HR", "BB", "IBB", "SO", "HBP", "WP", "BK",
                     "BF", "SH", "SF", "SB", "CS"]
        var totals: [String: Int] = [:]
        for key in keys { totals[key] = 0 }
        var totalIPOuts = 0.0

        for s in seasons {
            for key in keys {
                totals[key, default: 0] += Int(s.countingValues[key] ?? 0)
            }
            if let ipVal = s.countingValues["IP"] {
                let whole = Int(ipVal)
                let frac = ipVal - Double(whole)
                totalIPOuts += Double(whole * 3) + (frac * 10).rounded()
            }
        }

        let ip = totalIPOuts / 3.0
        let ipDisplay = "\(Int(totalIPOuts) / 3).\(Int(totalIPOuts) % 3)"
        let h = totals["H"]!, bb = totals["BB"]!, er = totals["ER"]!
        let so = totals["SO"]!, hr = totals["HR"]!, bf = totals["BF"]!
        let hbp = totals["HBP"]!, sh = totals["SH"]!, sf = totals["SF"]!

        let era = ip > 0 ? 9.0 * Double(er) / ip : 0
        let whip = ip > 0 ? Double(bb + h) / ip : 0
        let k9 = ip > 0 ? 9.0 * Double(so) / ip : 0
        let bb9 = ip > 0 ? 9.0 * Double(bb) / ip : 0
        let kbb = bb > 0 ? Double(so) / Double(bb) : 0
        let h9 = ip > 0 ? 9.0 * Double(h) / ip : 0
        let hr9 = ip > 0 ? 9.0 * Double(hr) / ip : 0

        // BAA = H / (BF - BB - HBP - SH - SF)
        let baaDenom = bf - bb - hbp - sh - sf
        let baa = baaDenom > 0 ? Double(h) / Double(baaDenom) : nil

        // QS: only show if all seasons have data (MSF 2026+ doesn't provide QS)
        let hasCompleteQS = seasons.allSatisfy { ($0.countingValues["QS"] ?? 0) > 0 || ($0.countingValues["GS"] ?? 0) == 0 }

        let values = [
            "\(totals["G"]!)", "\(totals["W"]!)", "\(totals["L"]!)", "\(totals["SV"]!)",
            "\(totals["GS"]!)", "\(totals["GF"]!)",
            "\(totals["CG"]!)", hasCompleteQS ? "\(totals["QS"]!)" : "--",
            ipDisplay, "\(h)", "\(totals["R"]!)", "\(er)",
            "\(hr)", "\(bb)", "\(totals["IBB"]!)", "\(so)", "\(hbp)", "\(totals["WP"]!)",
            "\(totals["BK"]!)", "\(bf)", "\(sh)", "\(sf)", "\(totals["SB"]!)", "\(totals["CS"]!)",
            String(format: "%.2f", era), String(format: "%.2f", whip),
            String(format: "%.1f", k9), String(format: "%.1f", bb9),
            String(format: "%.2f", kbb), String(format: "%.1f", h9),
            String(format: "%.1f", hr9),
            baa.map { formatRate(String(format: "%.3f", $0)) } ?? "--",
            "--",
        ]
        let displayValues = filterPitchingForDisplay(values)
        return StatGridParser.StatGrid(
            headers: pitchingHeaders,
            rows: [StatGridParser.StatGrid.Row(label: "\(seasons.count) Seasons", values: displayValues)]
        )
    }

    // MARK: - Comparison builder

    /// Build a structured comparison response for two players (current season + career).
    /// Returns a string with [STATGRID] blocks that StatGridParser can parse.
    static func buildComparison(player1: String, player2: String, season: Int? = nil) -> String {
        let header = "HEADER: " + allHeaders.joined(separator: ", ")

        // Fetch requested season or latest for each player
        let season1 = season.flatMap({ fetchSeasonRow(name: player1, year: $0) }) ?? fetchLatestSeasonRow(name: player1)
        let season2 = season.flatMap({ fetchSeasonRow(name: player2, year: $0) }) ?? fetchLatestSeasonRow(name: player2)

        // Fetch career totals for each player
        let career1 = fetchCareerRow(name: player1)
        let career2 = fetchCareerRow(name: player2)

        let info1 = fetchPlayerInfo(name: player1)
        let info2 = fetchPlayerInfo(name: player2)
        let name1 = info1?.name ?? player1
        let name2 = info2?.name ?? player2

        var parts: [String] = []

        // Current season grid — year shown next to each player name
        if let s1 = season1, let s2 = season2 {
            if s1.year == s2.year {
                parts.append("\(s1.year) Season:\n")
                parts.append("[STATGRID]")
                parts.append(header)
                parts.append("ROW: \(name1), \(s1.values.joined(separator: ", "))")
                parts.append("ROW: \(name2), \(s2.values.joined(separator: ", "))")
                parts.append("[/STATGRID]")
            } else {
                parts.append("Best Seasons:\n")
                parts.append("[STATGRID]")
                parts.append(header)
                parts.append("ROW: \(name1) (\(s1.year)), \(s1.values.joined(separator: ", "))")
                parts.append("ROW: \(name2) (\(s2.year)), \(s2.values.joined(separator: ", "))")
                parts.append("[/STATGRID]")
            }
        }

        // Career grid — only when no specific season was requested
        if season == nil, let c1 = career1, let c2 = career2 {
            parts.append("\nCareer:\n")
            parts.append("[STATGRID]")
            parts.append(header)
            parts.append("ROW: \(name1), \(c1.joined(separator: ", "))")
            parts.append("ROW: \(name2), \(c2.joined(separator: ", "))")
            parts.append("[/STATGRID]")
        }

        if parts.isEmpty {
            return "I don't have enough data to compare these two players."
        }

        if hasPlayerPlatoonData(name: player1) {
            parts.append("\n[SUGGEST]\(name1) vs lefties[/SUGGEST]")
        }
        if hasPlayerPlatoonData(name: player2) {
            parts.append("[SUGGEST]\(name2) vs lefties[/SUGGEST]")
        }

        return parts.joined(separator: "\n")
    }

    /// Async comparison that fetches from backend for players not in local DB.
    static func buildComparisonAsync(player1: String, player2: String, season: Int? = nil) async -> String {
        let header = "HEADER: " + allHeaders.joined(separator: ", ")

        // Fetch data — local first, backend fallback
        let (name1, latest1, career1) = await comparisonData(for: player1, season: season)
        let (name2, latest2, career2) = await comparisonData(for: player2, season: season)

        var parts: [String] = []

        // Best season grid — year shown next to each player name
        if let s1 = latest1, let s2 = latest2 {
            if s1.year == s2.year {
                parts.append("\(s1.year) Season:\n")
                parts.append("[STATGRID]")
                parts.append(header)
                parts.append("ROW: \(name1), \(s1.values.joined(separator: ", "))")
                parts.append("ROW: \(name2), \(s2.values.joined(separator: ", "))")
                parts.append("[/STATGRID]")
            } else {
                parts.append("Best Seasons:\n")
                parts.append("[STATGRID]")
                parts.append(header)
                parts.append("ROW: \(name1) (\(s1.year)), \(s1.values.joined(separator: ", "))")
                parts.append("ROW: \(name2) (\(s2.year)), \(s2.values.joined(separator: ", "))")
                parts.append("[/STATGRID]")
            }
        }

        // Career grid — only when no specific season was requested
        if season == nil, let c1 = career1, let c2 = career2 {
            parts.append("\nCareer:\n")
            parts.append("[STATGRID]")
            parts.append(header)
            parts.append("ROW: \(name1), \(c1.joined(separator: ", "))")
            parts.append("ROW: \(name2), \(c2.joined(separator: ", "))")
            parts.append("[/STATGRID]")
        }

        if parts.isEmpty {
            return "I don't have enough data to compare these two players."
        }

        if hasPlayerPlatoonData(name: player1) {
            parts.append("\n[SUGGEST]\(name1) vs lefties[/SUGGEST]")
        }
        if hasPlayerPlatoonData(name: player2) {
            parts.append("[SUGGEST]\(name2) vs lefties[/SUGGEST]")
        }

        return parts.joined(separator: "\n")
    }

    /// Get comparison data for a single player — tries local DB, falls back to backend.
    private static func comparisonData(for name: String, season: Int? = nil) async -> (label: String, latest: (year: Int, values: [String])?, career: [String]?) {
        // Try local first
        if hasLocalData(name: name) {
            let info = fetchPlayerInfo(name: name)
            let displayName = info?.name ?? name
            let latest = season.flatMap({ fetchSeasonRow(name: name, year: $0) }) ?? fetchLatestSeasonRow(name: name)

            // If player's career extends beyond local range, get career from backend
            if playerNeedsBackendForCareer(name: name) {
                // Use local for latest season, backend for career totals
                let backendCareer = await backendCareerTotals(for: name)
                return (displayName, latest, backendCareer ?? fetchCareerRow(name: name))
            }
            return (displayName, latest, fetchCareerRow(name: name))
        }

        // Backend fallback
        guard let data = try? await backendService.fetchPlayerCard(name: name),
              !data.batting_seasons.isEmpty else {
            return (name, nil, nil)
        }

        let info = data.player_info
        let displayName = info?.name ?? name

        // Latest season → formatted values matching allHeaders order
        let latest: (year: Int, values: [String])? = data.batting_seasons.first.map { s in
            let values = [
                "\(s.G)", "\(s.AB)", "\(s.R)", "\(s.H)", "\(s.doubles)", "\(s.triples)",
                "\(s.HR)", "\(s.RBI)", "\(s.SB)", "\(s.CS)", "\(s.BB)", "\(s.IBB)",
                "\(s.SO)", "\(s.HBP)",
                formatRate(s.AVG), formatRate(s.OBP), formatRate(s.SLG), formatRate(s.OPS),
                s.OPS_plus, formatRate(s.ISO), formatRate(s.BABIP)
            ]
            return (s.year, values)
        }

        // Career totals from all seasons
        guard data.batting_seasons.count > 1 else {
            return (displayName, latest, nil)
        }
        var totG = 0, totAB = 0, totR = 0, totH = 0, tot2B = 0, tot3B = 0
        var totHR = 0, totRBI = 0, totSB = 0, totCS = 0, totBB = 0, totIBB = 0
        var totSO = 0, totHBP = 0
        for s in data.batting_seasons {
            totG += s.G; totAB += s.AB; totR += s.R; totH += s.H
            tot2B += s.doubles; tot3B += s.triples; totHR += s.HR; totRBI += s.RBI
            totSB += s.SB; totCS += s.CS; totBB += s.BB; totIBB += s.IBB
            totSO += s.SO; totHBP += s.HBP
        }
        let ab = Double(totAB), h = Double(totH), bb = Double(totBB), hbp = Double(totHBP)
        let pa = ab + bb + hbp
        let avg = ab > 0 ? h / ab : 0
        let obp = pa > 0 ? (h + bb + hbp) / pa : 0
        let tb = h - Double(tot2B) - Double(tot3B) - Double(totHR) + 2*Double(tot2B) + 3*Double(tot3B) + 4*Double(totHR)
        let slg = ab > 0 ? tb / ab : 0
        let ops = obp + slg
        let iso = slg - avg
        let babipDenom = ab - Double(totSO) - Double(totHR)
        let babip = babipDenom > 0 ? (h - Double(totHR)) / babipDenom : 0

        let career = [
            "\(totG)", "\(totAB)", "\(totR)", "\(totH)", "\(tot2B)", "\(tot3B)",
            "\(totHR)", "\(totRBI)", "\(totSB)", "\(totCS)", "\(totBB)", "\(totIBB)",
            "\(totSO)", "\(totHBP)",
            formatRate(String(format: "%.3f", avg)), formatRate(String(format: "%.3f", obp)),
            formatRate(String(format: "%.3f", slg)), formatRate(String(format: "%.3f", ops)),
            "--", formatRate(String(format: "%.3f", iso)), formatRate(String(format: "%.3f", babip)),
        ]

        return (displayName, latest, career)
    }

    /// Fetch career totals from backend as a StatGrid (for player card display).
    private static func backendCareerGrid(for name: String) async -> StatGridParser.StatGrid? {
        guard let values = await backendCareerTotals(for: name) else { return nil }
        return StatGridParser.StatGrid(
            headers: allHeaders,
            rows: [StatGridParser.StatGrid.Row(label: "Career", values: values)]
        )
    }

    /// Fetch career totals from backend (for players whose career extends beyond local DB).
    private static func backendCareerTotals(for name: String) async -> [String]? {
        guard let data = try? await backendService.fetchPlayerCard(name: name),
              data.batting_seasons.count > 1 else { return nil }

        var totG = 0, totAB = 0, totR = 0, totH = 0, tot2B = 0, tot3B = 0
        var totHR = 0, totRBI = 0, totSB = 0, totCS = 0, totBB = 0, totIBB = 0
        var totSO = 0, totHBP = 0
        for s in data.batting_seasons {
            totG += s.G; totAB += s.AB; totR += s.R; totH += s.H
            tot2B += s.doubles; tot3B += s.triples; totHR += s.HR; totRBI += s.RBI
            totSB += s.SB; totCS += s.CS; totBB += s.BB; totIBB += s.IBB
            totSO += s.SO; totHBP += s.HBP
        }
        let ab = Double(totAB), h = Double(totH), bb = Double(totBB), hbp = Double(totHBP)
        let pa = ab + bb + hbp
        let avg = ab > 0 ? h / ab : 0
        let obp = pa > 0 ? (h + bb + hbp) / pa : 0
        let tb = h - Double(tot2B) - Double(tot3B) - Double(totHR) + 2*Double(tot2B) + 3*Double(tot3B) + 4*Double(totHR)
        let slg = ab > 0 ? tb / ab : 0
        let ops = obp + slg
        let iso = slg - avg
        let babipDenom = ab - Double(totSO) - Double(totHR)
        let babip = babipDenom > 0 ? (h - Double(totHR)) / babipDenom : 0

        return [
            "\(totG)", "\(totAB)", "\(totR)", "\(totH)", "\(tot2B)", "\(tot3B)",
            "\(totHR)", "\(totRBI)", "\(totSB)", "\(totCS)", "\(totBB)", "\(totIBB)",
            "\(totSO)", "\(totHBP)",
            formatRate(String(format: "%.3f", avg)), formatRate(String(format: "%.3f", obp)),
            formatRate(String(format: "%.3f", slg)), formatRate(String(format: "%.3f", ops)),
            "--", formatRate(String(format: "%.3f", iso)), formatRate(String(format: "%.3f", babip)),
        ]
    }

    /// Fetch a specific season's 21 formatted stat values for a player.
    private static func fetchSeasonRow(name: String, year: Int) -> (year: Int, values: [String])? {
        let sql = """
            SELECT s.season,
                   s.games, s.at_bats, s.runs, s.hits,
                   s.doubles, s.triples, s.home_runs, s.rbi, s.stolen_bases, s.caught_stealing,
                   s.walks, s.intentional_walks, s.strikeouts, s.hit_by_pitch,
                   s.batting_avg, s.obp, s.slg, s.ops, s.ops_plus, s.iso, s.babip
            FROM season_batting_stats s
            JOIN players p ON s.player_id = p.player_id
            WHERE p.name = '\(sanitize(name))' AND s.season = \(year)
            LIMIT 1
            """
        guard let result = try? db.execute(sql: sql),
              let row = result.rows.first,
              let yr = Int(row[0]) else { return nil }

        let values = Array(row[1...21])
        let formatted = formatValues(headers: allHeaders, values: values)
        return (yr, formatted)
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
            WHERE p.name = '\(sanitize(name))'
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
            WHERE p.name = '\(sanitize(name))'
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
            let ops = formatRate(String(format: "%.3f", obp + slg))
            formatted.insert(ops, at: 17)
            formatted.insert("--", at: 18) // Career OPS+ not computed
        }

        return formatted
    }

    // MARK: - Player info

    private static func fetchPlayerInfo(name: String) -> (name: String, team: String, birthdate: String?, bats: String?, throws_: String?, positions: String?)? {
        let sql = """
            SELECT p.name, p.team, p.birthdate, p.bats, p.throws, p.positions FROM players p
            WHERE p.name = '\(sanitize(name))'
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
            WHERE p.name = '\(sanitize(name))'
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
            let rispSplits = fetchRISPBattingSplitsForSeason(name: name, season: year)
            let streakGrid = fetchStreaksForSeason(name: name, season: year, performance: "hot")
            let fieldingGrid = fetchFieldingForSeason(name: name, season: year)
            let pitchTypeGrids = fetchPitchTypeBattingSplitsForSeason(name: name, season: year)
            let countGrids = fetchCountBattingSplitsForSeason(name: name, season: year)
            let currentForm = fetchCurrentFormForSeason(name: name, season: year)

            seasons.append(SeasonData(
                year: year, team: team, age: age, games: games, teamGames: teamGames,
                stats: grid, countingValues: counting,
                platoonSplits: splits, homeAwaySplits: homeAwaySplits, rispSplits: rispSplits,
                streaks: streakGrid, fieldingStats: fieldingGrid,
                pitchTypeSplits: pitchTypeGrids, countSplits: countGrids, currentForm: currentForm
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
            WHERE p.name = '\(sanitize(name))'
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
            let ops = formatRate(String(format: "%.3f", obp + slg))
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
            WHERE p.name = '\(sanitize(name))' AND ps.season = \(season)
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
            WHERE p.name = '\(sanitize(name))' AND has.season = \(season)
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

    // MARK: - Per-season RISP batting splits

    private static func fetchRISPBattingSplitsForSeason(name: String, season: Int) -> StatGridParser.StatGrid? {
        let sql = """
            SELECT rs.split, rs.at_bats, rs.hits,
                   rs.doubles, rs.triples, rs.home_runs, rs.rbi,
                   rs.walks, rs.strikeouts,
                   rs.batting_avg, rs.obp, rs.slg, rs.ops, rs.iso, rs.babip
            FROM risp_batting_splits rs
            JOIN players p ON rs.player_id = p.player_id
            WHERE p.name = '\(sanitize(name))' AND rs.season = \(season)
            ORDER BY rs.split DESC
            """
        guard let result = try? db.execute(sql: sql),
              !result.rows.isEmpty else { return nil }

        let headers = ["AB", "H", "2B", "3B", "HR", "RBI", "BB", "SO", "AVG", "OBP", "SLG", "OPS", "ISO", "BABIP"]
        var rows: [StatGridParser.StatGrid.Row] = []
        for row in result.rows.prefix(2) {
            let splitLabel = row[0] == "RISP" ? "RISP" : "Non-RISP"
            let values = formatValues(headers: headers, values: Array(row.dropFirst()))
            rows.append(StatGridParser.StatGrid.Row(label: splitLabel, values: values))
        }

        guard !rows.isEmpty else { return nil }
        return StatGridParser.StatGrid(headers: headers, rows: rows)
    }

    // MARK: - Career platoon splits

    private static func fetchCareerPlatoonSplits(name: String) -> StatGridParser.StatGrid? {
        let sql = """
            SELECT ps.split,
                   SUM(ps.at_bats), SUM(ps.hits),
                   SUM(ps.doubles), SUM(ps.triples), SUM(ps.home_runs),
                   SUM(ps.rbi), SUM(ps.walks), SUM(ps.strikeouts),
                   ROUND(CAST(SUM(ps.hits) AS REAL) / NULLIF(SUM(ps.at_bats), 0), 3),
                   ROUND(CAST(SUM(ps.hits) + SUM(ps.walks) AS REAL) /
                         NULLIF(SUM(ps.plate_appearances), 0), 3),
                   ROUND(CAST(SUM(ps.hits - ps.doubles - ps.triples - ps.home_runs) +
                              2 * SUM(ps.doubles) + 3 * SUM(ps.triples) + 4 * SUM(ps.home_runs) AS REAL) /
                         NULLIF(SUM(ps.at_bats), 0), 3)
            FROM platoon_splits ps
            JOIN players p ON ps.player_id = p.player_id
            WHERE p.name = '\(sanitize(name))'
            GROUP BY ps.split
            HAVING COUNT(DISTINCT ps.season) > 1
            ORDER BY ps.split
            """
        guard let result = try? db.execute(sql: sql),
              !result.rows.isEmpty else { return nil }

        let headers = ["AB", "H", "2B", "3B", "HR", "RBI", "BB", "SO", "AVG", "OBP", "SLG", "OPS", "ISO", "BABIP"]
        var rows: [StatGridParser.StatGrid.Row] = []
        for row in result.rows {
            let splitLabel = row[0] == "vs_LHP" ? "vs LHP" : "vs RHP"
            // Compute OPS, ISO, BABIP in Swift from the SQL values
            let values = Array(row.dropFirst())
            var formatted = formatValues(headers: ["AB", "H", "2B", "3B", "HR", "RBI", "BB", "SO", "AVG", "OBP", "SLG"], values: values)
            let obp = Double(formatted[9]) ?? 0
            let slg = Double(formatted[10]) ?? 0
            let avg = Double(formatted[8]) ?? 0
            formatted.append(formatRate(String(format: "%.3f", obp + slg))) // OPS
            formatted.append(formatRate(String(format: "%.3f", slg - avg))) // ISO
            // BABIP = (H - HR) / (AB - SO - HR)
            let h = Double(values[1]) ?? 0, hr = Double(values[4]) ?? 0
            let ab = Double(values[0]) ?? 0, so = Double(values[7]) ?? 0
            let babipDenom = ab - so - hr
            formatted.append(babipDenom > 0 ? formatRate(String(format: "%.3f", (h - hr) / babipDenom)) : ".000")
            rows.append(StatGridParser.StatGrid.Row(label: splitLabel, values: formatted))
        }

        guard !rows.isEmpty else { return nil }
        return StatGridParser.StatGrid(headers: headers, rows: rows)
    }

    // MARK: - Career home/away splits

    private static func fetchCareerHomeAwaySplits(name: String) -> StatGridParser.StatGrid? {
        let sql = """
            SELECT has.split,
                   SUM(has.games), SUM(has.at_bats), SUM(has.runs), SUM(has.hits),
                   SUM(has.doubles), SUM(has.triples), SUM(has.home_runs),
                   SUM(has.rbi), SUM(has.walks), SUM(has.strikeouts),
                   ROUND(CAST(SUM(has.hits) AS REAL) / NULLIF(SUM(has.at_bats), 0), 3),
                   ROUND(CAST(SUM(has.hits) + SUM(has.walks) + SUM(has.hit_by_pitch) AS REAL) /
                         NULLIF(SUM(has.at_bats) + SUM(has.walks) + SUM(has.hit_by_pitch) + SUM(has.sacrifice_flies), 0), 3),
                   ROUND(CAST(SUM(has.hits - has.doubles - has.triples - has.home_runs) +
                              2 * SUM(has.doubles) + 3 * SUM(has.triples) + 4 * SUM(has.home_runs) AS REAL) /
                         NULLIF(SUM(has.at_bats), 0), 3)
            FROM home_away_splits has
            JOIN players p ON has.player_id = p.player_id
            WHERE p.name = '\(sanitize(name))'
            GROUP BY has.split
            HAVING COUNT(DISTINCT has.season) > 1
            ORDER BY has.split DESC
            """
        guard let result = try? db.execute(sql: sql),
              !result.rows.isEmpty else { return nil }

        let headers = ["G", "AB", "R", "H", "2B", "3B", "HR", "RBI", "BB", "SO", "AVG", "OBP", "SLG", "OPS", "ISO", "BABIP"]
        var rows: [StatGridParser.StatGrid.Row] = []
        for row in result.rows {
            let splitLabel = row[0] == "home" ? "Home" : "Away"
            let values = Array(row.dropFirst())
            var formatted = formatValues(headers: ["G", "AB", "R", "H", "2B", "3B", "HR", "RBI", "BB", "SO", "AVG", "OBP", "SLG"], values: values)
            let obp = Double(formatted[11]) ?? 0
            let slg = Double(formatted[12]) ?? 0
            let avg = Double(formatted[10]) ?? 0
            formatted.append(formatRate(String(format: "%.3f", obp + slg))) // OPS
            formatted.append(formatRate(String(format: "%.3f", slg - avg))) // ISO
            let h = Double(values[3]) ?? 0, hr = Double(values[6]) ?? 0
            let ab = Double(values[1]) ?? 0, so = Double(values[9]) ?? 0
            let babipDenom = ab - so - hr
            formatted.append(babipDenom > 0 ? formatRate(String(format: "%.3f", (h - hr) / babipDenom)) : ".000")
            rows.append(StatGridParser.StatGrid.Row(label: splitLabel, values: formatted))
        }

        guard !rows.isEmpty else { return nil }
        return StatGridParser.StatGrid(headers: headers, rows: rows)
    }

    // MARK: - Career pitching platoon splits

    private static func fetchPitchingCareerPlatoonSplits(name: String) -> StatGridParser.StatGrid? {
        let sql = """
            SELECT pps.split,
                   SUM(pps.at_bats), SUM(pps.hits),
                   SUM(pps.doubles), SUM(pps.triples), SUM(pps.home_runs),
                   SUM(pps.walks), SUM(pps.strikeouts),
                   ROUND(CAST(SUM(pps.hits) AS REAL) / NULLIF(SUM(pps.at_bats), 0), 3),
                   ROUND(CAST(SUM(pps.hits) + SUM(pps.walks) + SUM(pps.hit_by_pitch) AS REAL) /
                         NULLIF(SUM(pps.at_bats) + SUM(pps.walks) + SUM(pps.hit_by_pitch) + SUM(pps.sacrifice_flies), 0), 3),
                   ROUND(CAST(SUM(pps.hits - pps.doubles - pps.triples - pps.home_runs) +
                              2 * SUM(pps.doubles) + 3 * SUM(pps.triples) + 4 * SUM(pps.home_runs) AS REAL) /
                         NULLIF(SUM(pps.at_bats), 0), 3),
                   ROUND(CAST(SUM(pps.hits) + SUM(pps.walks) + SUM(pps.hit_by_pitch) AS REAL) /
                         NULLIF(SUM(pps.at_bats) + SUM(pps.walks) + SUM(pps.hit_by_pitch) + SUM(pps.sacrifice_flies), 0) +
                         CAST(SUM(pps.hits - pps.doubles - pps.triples - pps.home_runs) +
                              2 * SUM(pps.doubles) + 3 * SUM(pps.triples) + 4 * SUM(pps.home_runs) AS REAL) /
                         NULLIF(SUM(pps.at_bats), 0), 3)
            FROM pitching_platoon_splits pps
            JOIN players p ON pps.player_id = p.player_id
            WHERE p.name = '\(sanitize(name))'
            GROUP BY pps.split
            HAVING COUNT(DISTINCT pps.season) > 1
            ORDER BY pps.split
            """
        guard let result = try? db.execute(sql: sql),
              !result.rows.isEmpty else { return nil }

        let headers = ["AB", "H", "2B", "3B", "HR", "BB", "SO", "AVG", "OBP", "SLG", "OPS"]
        var rows: [StatGridParser.StatGrid.Row] = []
        for row in result.rows {
            let splitLabel = row[0] == "vs_LHB" ? "vs LHB" : "vs RHB"
            let values = formatValues(headers: headers, values: Array(row.dropFirst()))
            rows.append(StatGridParser.StatGrid.Row(label: splitLabel, values: values))
        }

        guard !rows.isEmpty else { return nil }
        return StatGridParser.StatGrid(headers: headers, rows: rows)
    }

    // MARK: - Career pitching home/away splits

    private static func fetchPitchingCareerHomeAwaySplits(name: String) -> StatGridParser.StatGrid? {
        let sql = """
            SELECT phas.split,
                   SUM(phas.games), SUM(phas.games_started),
                   CAST(SUM(phas.ip_outs) / 3 AS TEXT) || '.' || CAST(SUM(phas.ip_outs) % 3 AS TEXT),
                   SUM(phas.hits), SUM(phas.earned_runs), SUM(phas.home_runs),
                   SUM(phas.walks), SUM(phas.strikeouts),
                   ROUND(9.0 * CAST(SUM(phas.earned_runs) AS REAL) / NULLIF(SUM(phas.ip_outs) / 3.0, 0), 2),
                   ROUND(CAST(SUM(phas.walks) + SUM(phas.hits) AS REAL) / NULLIF(SUM(phas.ip_outs) / 3.0, 0), 2),
                   ROUND(9.0 * CAST(SUM(phas.strikeouts) AS REAL) / NULLIF(SUM(phas.ip_outs) / 3.0, 0), 1),
                   ROUND(9.0 * CAST(SUM(phas.walks) AS REAL) / NULLIF(SUM(phas.ip_outs) / 3.0, 0), 1),
                   ROUND(CAST(SUM(phas.hits) AS REAL) / NULLIF(SUM(phas.games) * 3, 0), 3)
            FROM pitching_home_away_splits phas
            JOIN players p ON phas.player_id = p.player_id
            WHERE p.name = '\(sanitize(name))'
            GROUP BY phas.split
            HAVING COUNT(DISTINCT phas.season) > 1
            ORDER BY phas.split DESC
            """
        guard let result = try? db.execute(sql: sql),
              !result.rows.isEmpty else { return nil }

        let headers = ["G", "GS", "IP", "H", "ER", "HR", "BB", "SO", "ERA", "WHIP", "K/9", "BB/9", "BAA"]
        var rows: [StatGridParser.StatGrid.Row] = []
        for row in result.rows {
            let splitLabel = row[0] == "home" ? "Home" : "Away"
            let values = formatPitchingValues(headers: headers, values: Array(row.dropFirst()))
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
            WHERE p.name = '\(sanitize(name))' AND sfs.season = \(season) AND sfs.games > 0
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
                fpct = formatRate(String(format: "%.3f", fpctVal))
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

    // MARK: - Per-season pitch type splits (batting)

    private static func fetchPitchTypeBattingSplitsForSeason(name: String, season: Int) -> [StatGridParser.StatGrid]? {
        let sql = """
            SELECT pts.pitch_type, pts.at_bats, pts.hits,
                   pts.doubles, pts.triples, pts.home_runs, pts.rbi,
                   pts.walks, pts.strikeouts,
                   pts.batting_avg, pts.obp, pts.slg, pts.ops
            FROM pitch_type_batting_splits pts
            JOIN players p ON pts.player_id = p.player_id
            WHERE p.name = '\(sanitize(name))' AND pts.season = \(season)
            ORDER BY pts.at_bats DESC
            """
        guard let result = try? db.execute(sql: sql),
              !result.rows.isEmpty else { return nil }

        let headers = ["AB", "H", "2B", "3B", "HR", "RBI", "BB", "SO", "AVG", "OBP", "SLG", "OPS"]
        var grids: [StatGridParser.StatGrid] = []
        for row in result.rows {
            let label = row[0]  // e.g. "4-Seam", "Slider"
            let values = formatValues(headers: headers, values: Array(row[1...]))
            let grid = StatGridParser.StatGrid(
                headers: headers,
                rows: [StatGridParser.StatGrid.Row(label: label, values: values)]
            )
            grids.append(grid)
        }
        return grids.isEmpty ? nil : grids
    }

    // MARK: - Per-season count splits (batting)

    private static func fetchCountBattingSplitsForSeason(name: String, season: Int) -> [StatGridParser.StatGrid]? {
        let sql = """
            SELECT cs.count_state, cs.at_bats, cs.hits,
                   cs.doubles, cs.triples, cs.home_runs, cs.rbi,
                   cs.walks, cs.strikeouts,
                   cs.batting_avg, cs.obp, cs.slg, cs.ops
            FROM count_batting_splits cs
            JOIN players p ON cs.player_id = p.player_id
            WHERE p.name = '\(sanitize(name))' AND cs.season = \(season)
            ORDER BY cs.count_state
            """
        guard let result = try? db.execute(sql: sql),
              !result.rows.isEmpty else { return nil }

        let headers = ["AB", "H", "2B", "3B", "HR", "RBI", "BB", "SO", "AVG", "OBP", "SLG", "OPS"]
        var grids: [StatGridParser.StatGrid] = []
        for row in result.rows {
            let label = row[0]  // e.g. "0-0", "3-2"
            let values = formatValues(headers: headers, values: Array(row[1...]))
            let grid = StatGridParser.StatGrid(
                headers: headers,
                rows: [StatGridParser.StatGrid.Row(label: label, values: values)]
            )
            grids.append(grid)
        }
        return grids.isEmpty ? nil : grids
    }

    // MARK: - Per-season pitch type splits (pitching)

    private static func fetchPitchTypePitchingSplitsForSeason(name: String, season: Int) -> [StatGridParser.StatGrid]? {
        let sql = """
            SELECT pts.pitch_type, pts.at_bats, pts.hits,
                   pts.doubles, pts.triples, pts.home_runs,
                   pts.walks, pts.strikeouts,
                   pts.batting_avg_against, pts.obp_against, pts.slg_against, pts.ops_against
            FROM pitch_type_pitching_splits pts
            JOIN players p ON pts.player_id = p.player_id
            WHERE p.name = '\(sanitize(name))' AND pts.season = \(season)
            ORDER BY pts.at_bats DESC
            """
        guard let result = try? db.execute(sql: sql),
              !result.rows.isEmpty else { return nil }

        let headers = ["AB", "H", "2B", "3B", "HR", "BB", "SO", "AVG", "OBP", "SLG", "OPS"]
        var grids: [StatGridParser.StatGrid] = []
        for row in result.rows {
            let label = row[0]
            let values = formatValues(headers: headers, values: Array(row[1...]))
            let grid = StatGridParser.StatGrid(
                headers: headers,
                rows: [StatGridParser.StatGrid.Row(label: label, values: values)]
            )
            grids.append(grid)
        }
        return grids.isEmpty ? nil : grids
    }

    // MARK: - Per-season count splits (pitching)

    private static func fetchCountPitchingSplitsForSeason(name: String, season: Int) -> [StatGridParser.StatGrid]? {
        let sql = """
            SELECT cs.count_state, cs.at_bats, cs.hits,
                   cs.doubles, cs.triples, cs.home_runs,
                   cs.walks, cs.strikeouts,
                   cs.batting_avg_against, cs.obp_against, cs.slg_against, cs.ops_against
            FROM count_pitching_splits cs
            JOIN players p ON cs.player_id = p.player_id
            WHERE p.name = '\(sanitize(name))' AND cs.season = \(season)
            ORDER BY cs.count_state
            """
        guard let result = try? db.execute(sql: sql),
              !result.rows.isEmpty else { return nil }

        let headers = ["AB", "H", "2B", "3B", "HR", "BB", "SO", "AVG", "OBP", "SLG", "OPS"]
        var grids: [StatGridParser.StatGrid] = []
        for row in result.rows {
            let label = row[0]
            let values = formatValues(headers: headers, values: Array(row[1...]))
            let grid = StatGridParser.StatGrid(
                headers: headers,
                rows: [StatGridParser.StatGrid.Row(label: label, values: values)]
            )
            grids.append(grid)
        }
        return grids.isEmpty ? nil : grids
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
            WHERE p.name = '\(sanitize(name))' AND st.season = \(season) AND st.performance = '\(performance)'
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
                JOIN players p ON ss.player_id = ss.player_id
                WHERE p.name = '\(sanitize(name))' AND ss.season = \(season) AND ss.performance = '\(performance)'
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
                WHERE p.name = '\(sanitize(name))' AND sl.season = \(season) AND sl.performance = '\(performance)'
                ORDER BY sl.ops \(orderDir)
                """
            result = try? db.execute(sql: sql)
        }

        guard let result, !result.rows.isEmpty else { return nil }

        let headers = ["G", "AB", "H", "BB", "SO", "AVG", "OBP", "SLG", "OPS", "HR"]

        // Look up league average OPS for context notes on cold streaks
        var leagueOps: Double?
        if performance == "cold" {
            let leagueSql = "SELECT league_ops FROM league_averages WHERE season = \(season)"
            if let leagueResult = try? db.execute(sql: leagueSql),
               let leagueRow = leagueResult.rows.first {
                leagueOps = Double(leagueRow[0])
            }
        }

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

            var note: String?
            if performance == "cold", let lgOps = leagueOps, let streakOps = Double(row[10]), streakOps > lgOps {
                note = "This \"cold\" streak was still above the \(season) league average OPS of \(formatRate(String(lgOps)))"
            }

            rows.append(StatGridParser.StatGrid.Row(
                label: label,
                values: [games, ab, hits, walks, so, avg, obp, slg, ops, hr],
                note: note
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
            WHERE p.name = '\(sanitize(name))' AND s.season = \(season)
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
            WHERE p.name = '\(sanitize(name))' AND ps.season = \(season)
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
            WHERE p.name = '\(sanitize(name))' AND streaks.season = \(season)
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

        // Only suggest current form for active players (inactive players have no current form data)
        if isActivePlayer(name: name) {
            parts.append("\n[SUGGEST]how is \(displayName) doing lately[/SUGGEST]")
        }
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
                WHERE p.name = '\(sanitize(name))'
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
            if let note = row.note {
                parts.append("NOTE: \(note)")
            }
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
        parts.append("\n[SUGGEST]\(displayName) \(oppositePerf) streaks \(targetSeason)[/SUGGEST]")

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
            WHERE p.name = '\(sanitize(name))'
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
        parts.append("\n[SUGGEST]\(displayName) hot streaks \(season)[/SUGGEST]")

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
                WHERE p.name = '\(sanitize(name))'
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
                WHERE p.name = '\(sanitize(name))'
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
                WHERE p.name = '\(sanitize(name))'
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
            // Only suggest splits if platoon data exists for this player's era
            if mostRecentSeason >= 1969 {
                parts.append("[SUGGEST]\(displayName) vs lefties[/SUGGEST]")
            }

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
            WHERE p.name = '\(sanitize(name))' AND s.season = \(season)
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

    // MARK: - Slash line lookup

    /// Build a slash line response: AVG/OBP/SLG for a player-season.
    static func buildSlashLineLookup(name: String, season: Int) -> String? {
        let sql = """
            SELECT p.name, s.team, s.batting_avg, s.on_base_pct, s.slugging_pct, s.ops
            FROM season_batting_stats s
            JOIN players p ON s.player_id = p.player_id
            WHERE p.name = '\(sanitize(name))' AND s.season = \(season)
            LIMIT 1
            """
        guard let result = try? db.execute(sql: sql),
              let row = result.rows.first,
              row.count >= 6 else { return nil }

        let displayName = row[0]
        let team = row[1]
        let avg = formatRate(row[2])
        let obp = formatRate(row[3])
        let slg = formatRate(row[4])
        let ops = formatRate(row[5])
        let teamDisplay = teamFullName(team)

        return "**\(displayName)**'s slash line in \(season) (\(teamDisplay)):\n\n" +
            "[STATGRID]\nHEADER: AVG, OBP, SLG, OPS\n" +
            "ROW: \(avg), \(obp), \(slg), \(ops)\n[/STATGRID]\n\n" +
            "[TIP]Tap a player name for their full profile.[/TIP]\n\n" +
            "[SUGGEST]\(displayName) last season[/SUGGEST]\n" +
            "[SUGGEST]\(displayName) vs lefties[/SUGGEST]"
    }

    // MARK: - Threshold leaderboard (chat response builder)

    /// Build a filtered leaderboard for "who hit 40 home runs?" or "players batting over .300".
    static func buildThresholdLeaderboard(stat: PlayerNameMatcher.StatInfo, threshold: Double, comparison: String, season: Int, league: String? = nil) -> String {
        let leagueFilter = league.map { " AND \(leagueTeamClause($0, alias: "s"))" } ?? ""
        let leagueLabel = league.map { " (\($0))" } ?? ""

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
            WHERE s.season = \(season) AND s.\(stat.dbColumn) \(comparison) \(threshold)\(paFilter)\(leagueFilter)
            ORDER BY s.\(stat.dbColumn) DESC
            LIMIT 50
            """
        guard let result = try? db.execute(sql: sql),
              !result.rows.isEmpty else {
            let thresholdStr = stat.isRate ? formatRate(String(threshold)) : String(Int(threshold))
            let op = comparison == ">=" ? "at least" : "no more than"
            return "No players had \(op) \(thresholdStr) \(stat.displayAbbrev) in \(season)\(leagueLabel)."
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
                title = "Players Batting Over \(thresholdDisplay) \(stat.displayAbbrev) in \(season)\(leagueLabel)"
            } else {
                title = "Players with \(thresholdDisplay)+ \(stat.displayName) in \(season)\(leagueLabel)"
            }
        } else {
            title = "Players with \(thresholdDisplay) or Fewer \(stat.displayName) in \(season)\(leagueLabel)"
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

        if let league = league {
            let other = league == "AL" ? "NL" : "AL"
            parts.append("[SUGGEST]\(thresholdDisplay)+ \(statName) in \(season) (\(other))[/SUGGEST]")
            parts.append("[SUGGEST]\(thresholdDisplay)+ \(statName) in \(season) (MLB)[/SUGGEST]")
        } else {
            parts.append("[SUGGEST]\(thresholdDisplay)+ \(statName) in \(season) (AL)[/SUGGEST]")
            parts.append("[SUGGEST]\(thresholdDisplay)+ \(statName) in \(season) (NL)[/SUGGEST]")
        }

        return parts.joined(separator: "\n")
    }

    // MARK: - All-time threshold query

    /// Build an all-time threshold response: "who hit 50 home runs?" (no season specified)
    static func buildAllTimeThreshold(stat: PlayerNameMatcher.StatInfo, threshold: Double, comparison: String, isPitching: Bool, league: String? = nil) -> String {
        let table = isPitching ? "season_pitching_stats" : "season_batting_stats"
        let prefix = isPitching ? "sp" : "s"
        let orderDir: String
        if isPitching && (stat.displayAbbrev == "ERA" || stat.displayAbbrev == "WHIP" || stat.displayAbbrev == "BAA") {
            orderDir = comparison == ">=" ? "DESC" : "ASC"
        } else {
            orderDir = comparison == ">=" ? "DESC" : "ASC"
        }

        let badEraFilter = isPitching ? eraDataFilter(prefix: prefix, stat: stat) : ""
        let leagueFilter = league.map { " AND \(leagueTeamClause($0, alias: prefix))" } ?? ""
        let leagueLabel = league.map { " (\($0))" } ?? ""

        let sql = """
            SELECT p.name, \(prefix).\(stat.dbColumn), \(prefix).season
            FROM \(table) \(prefix)
            JOIN players p ON \(prefix).player_id = p.player_id
            WHERE \(prefix).\(stat.dbColumn) \(comparison) \(threshold)\(badEraFilter)\(leagueFilter)
            ORDER BY \(prefix).\(stat.dbColumn) DESC
            """
        guard let result = try? db.execute(sql: sql),
              !result.rows.isEmpty else {
            let thresholdStr = stat.isRate ? formatRate(String(threshold)) : String(Int(threshold))
            let op = comparison == ">=" ? "at least" : "no more than"
            let who = isPitching ? "pitchers" : "players"
            return "No \(who) have had \(op) \(thresholdStr) \(stat.displayAbbrev) in a season."
        }

        let thresholdDisplay = stat.isRate ? formatRate(String(threshold)) : String(Int(threshold))
        let who = isPitching ? "Pitchers" : "Players"
        let title: String
        if comparison == ">=" {
            if stat.isRate {
                title = "\(who) with \(thresholdDisplay)+ \(stat.displayAbbrev) (All-Time)\(leagueLabel)"
            } else {
                title = "\(who) with \(thresholdDisplay)+ \(stat.displayName) (All-Time)\(leagueLabel)"
            }
        } else {
            title = "\(who) with \(thresholdDisplay) or Fewer \(stat.displayName) (All-Time)\(leagueLabel)"
        }

        var parts: [String] = []
        parts.append("**\(title)**\n")
        parts.append("[TIP]Tap a player name for their full profile.[/TIP]")

        parts.append("[LEADERBOARD]")
        parts.append("HEADER: Year, \(stat.displayAbbrev)")
        for (i, row) in result.rows.enumerated() {
            let playerName = row[0]
            let rawValue = row[1]
            let season = row[2]
            let formattedValue = stat.isRate ? formatRate(rawValue) : rawValue
            parts.append("ROW \(i + 1). \(playerName): \(season), \(formattedValue)")
        }
        parts.append("[/LEADERBOARD]")

        let count = result.rows.count
        parts.append("\n\(count) season\(count == 1 ? "" : "s") matched.")

        let currentYear = Calendar.current.component(.year, from: Date())
        let statName = stat.pillName
        parts.append("\n[SUGGEST]\(thresholdDisplay)+ \(statName) this season[/SUGGEST]")
        parts.append("[SUGGEST]\(thresholdDisplay)+ \(statName) last season[/SUGGEST]")

        if let league = league {
            let other = league == "AL" ? "NL" : "AL"
            parts.append("[SUGGEST]\(thresholdDisplay)+ \(statName) all-time (\(other))[/SUGGEST]")
            parts.append("[SUGGEST]\(thresholdDisplay)+ \(statName) all-time (MLB)[/SUGGEST]")
        } else {
            parts.append("[SUGGEST]\(thresholdDisplay)+ \(statName) all-time (AL)[/SUGGEST]")
            parts.append("[SUGGEST]\(thresholdDisplay)+ \(statName) all-time (NL)[/SUGGEST]")
        }

        return parts.joined(separator: "\n")
    }

    // MARK: - Superlative threshold query

    /// Build a superlative+threshold response: "youngest to hit 50 HR", "last player to bat .400"
    static func buildSuperlativeThreshold(stat: PlayerNameMatcher.StatInfo, threshold: Double, superlative: PlayerNameMatcher.Superlative, isPitching: Bool, league: String? = nil) -> String {
        let table = isPitching ? "season_pitching_stats" : "season_batting_stats"
        let prefix = isPitching ? "sp" : "s"

        let orderBy: String
        let ageSelect: String
        switch superlative {
        case .youngest:
            ageSelect = ", \(prefix).season - CAST(SUBSTR(p.birthdate, 1, 4) AS INT) AS age_at_season"
            orderBy = "age_at_season ASC"
        case .oldest:
            ageSelect = ", \(prefix).season - CAST(SUBSTR(p.birthdate, 1, 4) AS INT) AS age_at_season"
            orderBy = "age_at_season DESC"
        case .first:
            ageSelect = ""
            orderBy = "\(prefix).season ASC"
        case .last:
            ageSelect = ""
            orderBy = "\(prefix).season DESC"
        }

        let birthdateFilter = (superlative == .youngest || superlative == .oldest) ? " AND p.birthdate IS NOT NULL" : ""
        let badEraFilter = isPitching ? eraDataFilter(prefix: prefix, stat: stat) : ""
        let leagueFilter = league.map { " AND \(leagueTeamClause($0, alias: prefix))" } ?? ""
        let leagueLabel = league.map { " (\($0))" } ?? ""

        let sql = """
            SELECT p.name, \(prefix).\(stat.dbColumn), \(prefix).season\(ageSelect)
            FROM \(table) \(prefix)
            JOIN players p ON \(prefix).player_id = p.player_id
            WHERE \(prefix).\(stat.dbColumn) >= \(threshold)\(birthdateFilter)\(badEraFilter)\(leagueFilter)
            ORDER BY \(orderBy)
            LIMIT 10
            """
        guard let result = try? db.execute(sql: sql),
              !result.rows.isEmpty else {
            let thresholdStr = stat.isRate ? formatRate(String(threshold)) : String(Int(threshold))
            let who = isPitching ? "pitcher" : "player"
            return "No \(who) has reached \(thresholdStr) \(stat.displayAbbrev) in a season."
        }

        let thresholdDisplay = stat.isRate ? formatRate(String(threshold)) : String(Int(threshold))
        let superlativeLabel: String
        switch superlative {
        case .youngest: superlativeLabel = "Youngest"
        case .oldest: superlativeLabel = "Oldest"
        case .first: superlativeLabel = "First"
        case .last: superlativeLabel = "Most Recent"
        }
        let who = isPitching ? "Pitchers" : "Players"
        let title = "\(superlativeLabel) \(who) with \(thresholdDisplay)+ \(stat.displayName)\(leagueLabel)"

        var parts: [String] = []
        parts.append("**\(title)**\n")
        parts.append("[TIP]Tap a player name for their full profile.[/TIP]")

        parts.append("[LEADERBOARD]")
        let hasAge = superlative == .youngest || superlative == .oldest
        parts.append("HEADER: Year, \(stat.displayAbbrev)\(hasAge ? ", Age" : "")")
        for (i, row) in result.rows.enumerated() {
            let playerName = row[0]
            let rawValue = row[1]
            let season = row[2]
            let formattedValue = stat.isRate ? formatRate(rawValue) : rawValue
            if hasAge, row.count > 3 {
                let age = row[3]
                parts.append("ROW \(i + 1). \(playerName): \(season), \(formattedValue), age \(age)")
            } else {
                parts.append("ROW \(i + 1). \(playerName): \(season), \(formattedValue)")
            }
        }
        parts.append("[/LEADERBOARD]")

        // Suggestion pills for alternate superlatives
        let statName = stat.pillName
        switch superlative {
        case .youngest:
            parts.append("\n[SUGGEST]Oldest player to hit \(thresholdDisplay)+ \(statName)[/SUGGEST]")
            parts.append("[SUGGEST]All players with \(thresholdDisplay)+ \(statName)[/SUGGEST]")
        case .oldest:
            parts.append("\n[SUGGEST]Youngest player to hit \(thresholdDisplay)+ \(statName)[/SUGGEST]")
            parts.append("[SUGGEST]All players with \(thresholdDisplay)+ \(statName)[/SUGGEST]")
        case .first:
            parts.append("\n[SUGGEST]Most recent player with \(thresholdDisplay)+ \(statName)[/SUGGEST]")
            parts.append("[SUGGEST]All players with \(thresholdDisplay)+ \(statName)[/SUGGEST]")
        case .last:
            parts.append("\n[SUGGEST]First player with \(thresholdDisplay)+ \(statName)[/SUGGEST]")
            parts.append("[SUGGEST]All players with \(thresholdDisplay)+ \(statName)[/SUGGEST]")
        }

        if let league = league {
            let other = league == "AL" ? "NL" : "AL"
            parts.append("[SUGGEST]\(superlativeLabel.lowercased()) with \(thresholdDisplay)+ \(statName) (\(other))[/SUGGEST]")
            parts.append("[SUGGEST]\(superlativeLabel.lowercased()) with \(thresholdDisplay)+ \(statName) (MLB)[/SUGGEST]")
        } else {
            parts.append("[SUGGEST]\(superlativeLabel.lowercased()) with \(thresholdDisplay)+ \(statName) (AL)[/SUGGEST]")
            parts.append("[SUGGEST]\(superlativeLabel.lowercased()) with \(thresholdDisplay)+ \(statName) (NL)[/SUGGEST]")
        }

        return parts.joined(separator: "\n")
    }

    // MARK: - Filtered leaderboard query

    /// Build a filtered leaderboard: "most HR with .300+ batting average"
    static func buildFilteredLeaderboard(rankStat: PlayerNameMatcher.StatInfo, filterStat: PlayerNameMatcher.StatInfo, threshold: Double, comparison: String, season: Int?, limit: Int, isPitching: Bool, league: String? = nil) -> String {
        let table = isPitching ? "season_pitching_stats" : "season_batting_stats"
        let prefix = isPitching ? "sp" : "s"
        let seasonFilter = season.map { " AND \(prefix).season = \($0)" } ?? ""
        let leagueFilter = league.map { " AND \(leagueTeamClause($0, alias: prefix))" } ?? ""
        let leagueLabel = league.map { " (\($0))" } ?? ""
        let scopeLabel = season.map { String($0) } ?? "All-Time"

        // For rate ranking stats, order appropriately
        let orderDir: String
        if isPitching && (rankStat.displayAbbrev == "ERA" || rankStat.displayAbbrev == "WHIP" || rankStat.displayAbbrev == "BAA") {
            orderDir = "ASC"
        } else {
            orderDir = "DESC"
        }

        // PA/IP minimum for rate filter stats
        var qualFilter = ""
        if !isPitching && (rankStat.isRate || filterStat.isRate) {
            qualFilter = " AND \(prefix).plate_appearances >= 400"
        } else if isPitching && (rankStat.isRate || filterStat.isRate) {
            qualFilter = " AND \(prefix).ip_outs >= 486"
        }

        // Handle innings_pitched specially — TEXT column, use ip_outs (outs) for numeric comparison
        let filterColumn: String
        let filterThreshold: Double
        let displayColumn: String  // What to SELECT for display
        if filterStat.dbColumn == "innings_pitched" {
            filterColumn = "\(prefix).ip_outs"
            filterThreshold = threshold * 3  // 200 IP = 600 outs
            displayColumn = "\(prefix).innings_pitched"
        } else {
            filterColumn = "\(prefix).\(filterStat.dbColumn)"
            filterThreshold = threshold
            displayColumn = "\(prefix).\(filterStat.dbColumn)"
        }

        // Same for ranking by IP
        let rankColumn: String
        let rankDisplayColumn: String
        if rankStat.dbColumn == "innings_pitched" {
            rankColumn = "\(prefix).ip_outs"
            rankDisplayColumn = "\(prefix).innings_pitched"
        } else {
            rankColumn = "\(prefix).\(rankStat.dbColumn)"
            rankDisplayColumn = "\(prefix).\(rankStat.dbColumn)"
        }

        let badEraFilter = isPitching ? eraDataFilter(prefix: prefix, stat: rankStat, additionalStats: [filterStat]) : ""

        let sql = """
            SELECT p.name, \(rankDisplayColumn), \(displayColumn), \(prefix).season
            FROM \(table) \(prefix)
            JOIN players p ON \(prefix).player_id = p.player_id
            WHERE \(filterColumn) \(comparison) \(filterThreshold)\(seasonFilter)\(qualFilter)\(badEraFilter)\(leagueFilter)
            ORDER BY \(rankColumn) \(orderDir)
            LIMIT \(limit)
            """
        guard let result = try? db.execute(sql: sql),
              !result.rows.isEmpty else {
            let thresholdStr = filterStat.isRate ? formatRate(String(threshold)) : String(Int(threshold))
            let op = comparison == ">=" ? "at least" : "no more than"
            let who = isPitching ? "pitchers" : "players"
            return "No \(who) found with \(op) \(thresholdStr) \(filterStat.displayAbbrev) (\(scopeLabel))."
        }

        let thresholdDisplay = filterStat.isRate ? formatRate(String(threshold)) : String(Int(threshold))
        let filterLabel = comparison == ">=" ? "\(thresholdDisplay)+" : "≤\(thresholdDisplay)"
        let title = "\(rankStat.isRate ? "Highest" : "Most") \(rankStat.displayName) with \(filterLabel) \(filterStat.displayAbbrev) (\(scopeLabel))\(leagueLabel)"

        var parts: [String] = []
        parts.append("**\(title)**\n")
        parts.append("[TIP]Tap a player name for their full profile.[/TIP]")

        parts.append("[LEADERBOARD]")
        let showYear = season == nil
        parts.append("HEADER: \(showYear ? "Year, " : "")\(rankStat.displayAbbrev), \(filterStat.displayAbbrev)")
        for (i, row) in result.rows.enumerated() {
            let playerName = row[0]
            let rankRaw = row[1]
            let filterRaw = row[2]
            let yearStr = row[3]
            let rankFormatted = rankStat.isRate ? formatRate(rankRaw) : rankRaw
            let filterFormatted = filterStat.isRate ? formatRate(filterRaw) : filterRaw
            if showYear {
                parts.append("ROW \(i + 1). \(playerName): \(yearStr), \(rankFormatted), \(filterFormatted)")
            } else {
                parts.append("ROW \(i + 1). \(playerName): \(rankFormatted), \(filterFormatted)")
            }
        }
        parts.append("[/LEADERBOARD]")

        let count = result.rows.count
        parts.append("\n\(count) result\(count == 1 ? "" : "s").")

        // Suggestion pills
        let rankName = rankStat.pillName
        if season != nil {
            parts.append("\n[SUGGEST]\(rankStat.isRate ? "Highest" : "Most") \(rankName) with \(filterLabel) \(filterStat.displayAbbrev) all-time[/SUGGEST]")
        } else {
            let currentYear = Calendar.current.component(.year, from: Date())
            parts.append("\n[SUGGEST]\(rankStat.isRate ? "Highest" : "Most") \(rankName) with \(filterLabel) \(filterStat.displayAbbrev) in \(currentYear)[/SUGGEST]")
        }

        let pillPrefix = rankStat.isRate ? "highest" : "most"
        if let league = league {
            let other = league == "AL" ? "NL" : "AL"
            parts.append("[SUGGEST]\(pillPrefix) \(rankName) with \(filterLabel) \(filterStat.displayAbbrev) (\(other))[/SUGGEST]")
            parts.append("[SUGGEST]\(pillPrefix) \(rankName) with \(filterLabel) \(filterStat.displayAbbrev) (MLB)[/SUGGEST]")
        } else {
            parts.append("[SUGGEST]\(pillPrefix) \(rankName) with \(filterLabel) \(filterStat.displayAbbrev) (AL)[/SUGGEST]")
            parts.append("[SUGGEST]\(pillPrefix) \(rankName) with \(filterLabel) \(filterStat.displayAbbrev) (NL)[/SUGGEST]")
        }

        return parts.joined(separator: "\n")
    }

    // MARK: - Milestone query (chat response builder)

    /// Build a cross-season milestone response: "how many times has someone hit 50 HR?"
    static func buildMilestone(stat: PlayerNameMatcher.StatInfo, threshold: Double, since: Int?, isPitching: Bool, league: String? = nil) -> String {
        let table = isPitching ? "season_pitching_stats" : "season_batting_stats"
        let sinceFilter = since.map { " AND s.season >= \($0)" } ?? ""
        let leagueFilter = league.map { " AND \(leagueTeamClause($0, alias: "s"))" } ?? ""
        let leagueLabel = league.map { " (\($0))" } ?? ""

        // For pitching rate stats like ERA, lower is better
        let lowerIsBetter = ["era", "whip", "bb_per_9", "hits_per_9", "hr_per_9"].contains(stat.dbColumn)
        let comparison = lowerIsBetter ? "<=" : ">="

        let sql = """
            SELECT p.name, s.season, s.\(stat.dbColumn)
            FROM \(table) s
            JOIN players p ON s.player_id = p.player_id
            WHERE s.\(stat.dbColumn) \(comparison) \(threshold)\(sinceFilter)\(leagueFilter)
            ORDER BY s.season DESC, s.\(stat.dbColumn) \(lowerIsBetter ? "ASC" : "DESC")
            """
        guard let result = try? db.execute(sql: sql) else {
            return buildMilestoneEmpty(stat: stat, threshold: threshold, since: since, league: league)
        }

        let rows = result.rows
        if rows.isEmpty {
            return buildMilestoneEmpty(stat: stat, threshold: threshold, since: since, league: league)
        }

        let thresholdDisplay = stat.isRate ? formatRate(String(threshold)) : String(Int(threshold))
        let sinceLabel = since.map { " since \($0)" } ?? ""
        let verb = lowerIsBetter ? "or lower" : "or more"
        let title = "\(thresholdDisplay)+ \(stat.displayName) Seasons\(sinceLabel)\(leagueLabel)"

        var parts: [String] = []
        parts.append("**\(title)**\n")

        let count = rows.count
        parts.append("\(count) time\(count == 1 ? "" : "s") a player has recorded \(thresholdDisplay) \(verb) \(stat.displayAbbrev)\(sinceLabel).\n")

        parts.append("[TIP]Tap a player name for their full profile.[/TIP]")

        parts.append("[LEADERBOARD]")
        parts.append("HEADER: Year, \(stat.displayAbbrev)")
        for (i, row) in rows.enumerated() {
            let playerName = row[0]
            let season = row[1]
            let rawValue = row[2]
            let formattedValue = stat.isRate ? formatRate(rawValue) : rawValue
            parts.append("ROW \(i + 1). \(playerName): \(season), \(formattedValue)")
        }
        parts.append("[/LEADERBOARD]")

        let statName = stat.pillName
        parts.append("\n[SUGGEST]\(statName) leaders[/SUGGEST]")
        parts.append("[SUGGEST]career \(statName) leaders[/SUGGEST]")

        if let league = league {
            let other = league == "AL" ? "NL" : "AL"
            parts.append("[SUGGEST]\(thresholdDisplay)+ \(statName) seasons (\(other))[/SUGGEST]")
            parts.append("[SUGGEST]\(thresholdDisplay)+ \(statName) seasons (MLB)[/SUGGEST]")
        } else {
            parts.append("[SUGGEST]\(thresholdDisplay)+ \(statName) seasons (AL)[/SUGGEST]")
            parts.append("[SUGGEST]\(thresholdDisplay)+ \(statName) seasons (NL)[/SUGGEST]")
        }

        return parts.joined(separator: "\n")
    }

    private static func buildMilestoneEmpty(stat: PlayerNameMatcher.StatInfo, threshold: Double, since: Int?, league: String? = nil) -> String {
        let thresholdDisplay = stat.isRate ? formatRate(String(threshold)) : String(Int(threshold))
        let sinceLabel = since.map { " since \($0)" } ?? ""
        let leagueLabel = league.map { " (\($0))" } ?? ""
        return "No player has reached \(thresholdDisplay) \(stat.displayAbbrev)\(sinceLabel)\(leagueLabel)."
    }

    // MARK: - Backend-backed formatters (for pre-2016 queries)

    /// Format a backend leaderboard response into the same string format as local builders.
    static func formatBackendLeaderboard(_ resp: BackendService.LeaderboardResponse, stat: PlayerNameMatcher.StatInfo) -> String {
        guard !resp.rows.isEmpty else {
            return "No results found for \(stat.displayName)."
        }

        var parts: [String] = []
        parts.append("**\(resp.title)**\n")
        parts.append("[TIP]Tap a player name for their full profile.[/TIP]")

        parts.append("[LEADERBOARD]")
        let hasSeasons = resp.rows.contains(where: { $0.season != nil })
        if hasSeasons {
            parts.append("HEADER: Year, \(stat.displayAbbrev)")
        } else {
            parts.append("HEADER: \(stat.displayAbbrev)")
        }
        for row in resp.rows {
            let formattedValue = stat.isRate ? formatRate(row.value) : row.value
            if let season = row.season {
                parts.append("ROW \(row.rank). \(row.name): \(season), \(formattedValue)")
            } else {
                parts.append("ROW \(row.rank). \(row.name): \(formattedValue)")
            }
        }
        parts.append("[/LEADERBOARD]")

        if let paMin = resp.pa_min {
            parts.append("\n_Min. \(paMin) PA._")
        }

        let statName = stat.pillName
        parts.append("\n[SUGGEST]\(statName) leaders[/SUGGEST]")
        parts.append("[SUGGEST]career \(statName) leaders[/SUGGEST]")

        return parts.joined(separator: "\n")
    }

    /// Format a backend threshold response.
    static func formatBackendThreshold(_ resp: BackendService.LeaderboardResponse, stat: PlayerNameMatcher.StatInfo) -> String {
        guard !resp.rows.isEmpty else {
            return "No players matched that threshold."
        }

        var parts: [String] = []
        parts.append("**\(resp.title)**\n")
        parts.append("[TIP]Tap a player name for their full profile.[/TIP]")

        parts.append("[LEADERBOARD]")
        parts.append("HEADER: \(stat.displayAbbrev)")
        for row in resp.rows {
            let formattedValue = stat.isRate ? formatRate(row.value) : row.value
            parts.append("ROW \(row.rank). \(row.name): \(formattedValue)")
        }
        parts.append("[/LEADERBOARD]")

        let count = resp.count
        parts.append("\n\(count) player\(count == 1 ? "" : "s") matched.")

        return parts.joined(separator: "\n")
    }

    /// Format a backend milestone response.
    static func formatBackendMilestone(_ resp: BackendService.MilestoneResponse, stat: PlayerNameMatcher.StatInfo) -> String {
        guard !resp.rows.isEmpty else {
            return "No player has reached that milestone."
        }

        var parts: [String] = []
        parts.append("**\(resp.title)**\n")

        let count = resp.count
        parts.append("\(count) time\(count == 1 ? "" : "s") this milestone has been reached.\n")

        parts.append("[TIP]Tap a player name for their full profile.[/TIP]")

        parts.append("[LEADERBOARD]")
        parts.append("HEADER: Year, \(stat.displayAbbrev)")
        for row in resp.rows {
            let formattedValue = stat.isRate ? formatRate(row.value) : row.value
            parts.append("ROW \(row.rank). \(row.name): \(row.season ?? 0), \(formattedValue)")
        }
        parts.append("[/LEADERBOARD]")

        let statName = stat.pillName
        parts.append("\n[SUGGEST]\(statName) leaders[/SUGGEST]")
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
                #imageLiteral(resourceName: "simulator_screenshot_BB683322-3664-4F78-80DC-D2EA7EAB435F.png")               SELECT p.name, s.games, s.batting_avg, s.home_runs, s.rbi, s.ops
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
            WHERE p.name = '\(sanitize(name))' AND ps.season = \(season)\(splitFilter)
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

    // MARK: - Home/away splits (chat response builder)

    /// Build a home/away splits response for a batter.
    static func buildHomeAwaySplits(name: String, location: String?, season: Int) -> String? {
        let info = fetchPlayerInfo(name: name)
        let displayName = info?.name ?? name

        var splitFilter = ""
        if let location {
            splitFilter = " AND has.split = '\(location)'"
        }

        let sql = """
            SELECT has.split, has.games, has.at_bats, has.runs, has.hits,
                   has.doubles, has.triples, has.home_runs, has.rbi,
                   has.walks, has.strikeouts,
                   has.batting_avg, has.obp, has.slg, has.ops, has.iso, has.babip
            FROM home_away_splits has
            JOIN players p ON has.player_id = p.player_id
            WHERE p.name = '\(sanitize(name))' AND has.season = \(season)\(splitFilter)
            ORDER BY has.split DESC
            """
        guard let result = try? db.execute(sql: sql),
              !result.rows.isEmpty else { return nil }

        let headers = ["G", "AB", "R", "H", "2B", "3B", "HR", "RBI", "BB", "SO", "AVG", "OBP", "SLG", "OPS", "ISO", "BABIP"]

        let subtitle: String
        if let location {
            subtitle = location == "home" ? "Home" : "Away"
        } else {
            subtitle = "Home vs Away"
        }

        var parts: [String] = []
        parts.append("**\(displayName)** \u{2014} \(season) \(subtitle)\n")

        parts.append("[STATGRID]")
        parts.append("HEADER: " + headers.joined(separator: ", "))
        for row in result.rows.prefix(2) {
            let splitLabel = row[0] == "home" ? "Home" : "Away"
            let values = formatValues(headers: headers, values: Array(row.dropFirst()))
            parts.append("ROW \(splitLabel): " + values.joined(separator: ", "))
        }
        parts.append("[/STATGRID]")

        parts.append("\n[TIP]Tap a player name for their full profile.[/TIP]")
        parts.append("\n[SUGGEST]\(displayName) \(season)[/SUGGEST]")

        return parts.joined(separator: "\n")
    }

    /// Build a home/away splits response for a pitcher.
    static func buildPitchingHomeAwaySplits(name: String, location: String?, season: Int) -> String? {
        let info = fetchPlayerInfo(name: name)
        let displayName = info?.name ?? name

        var splitFilter = ""
        if let location {
            splitFilter = " AND phas.split = '\(location)'"
        }

        let sql = """
            SELECT phas.split, phas.games, phas.games_started, phas.innings_pitched,
                   phas.hits, phas.earned_runs, phas.home_runs, phas.walks, phas.strikeouts,
                   phas.era, phas.whip, phas.k_per_9, phas.bb_per_9, phas.baa
            FROM pitching_home_away_splits phas
            JOIN players p ON phas.player_id = p.player_id
            WHERE p.name = '\(sanitize(name))' AND phas.season = \(season)\(splitFilter)
            ORDER BY phas.split DESC
            """
        guard let result = try? db.execute(sql: sql),
              !result.rows.isEmpty else { return nil }

        let headers = ["G", "GS", "IP", "H", "ER", "HR", "BB", "SO", "ERA", "WHIP", "K/9", "BB/9", "BAA"]

        let subtitle: String
        if let location {
            subtitle = location == "home" ? "Home" : "Away"
        } else {
            subtitle = "Home vs Away"
        }

        var parts: [String] = []
        parts.append("**\(displayName)** \u{2014} \(season) \(subtitle)\n")

        parts.append("[STATGRID]")
        parts.append("HEADER: " + headers.joined(separator: ", "))
        for row in result.rows.prefix(2) {
            let splitLabel = row[0] == "home" ? "Home" : "Away"
            let values = formatPitchingValues(headers: headers, values: Array(row.dropFirst()))
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
    static func buildLeaderboard(stat: PlayerNameMatcher.StatInfo, scope: PlayerNameMatcher.LeaderboardScope, limit: Int, league: String? = nil) -> String {
        switch scope {
        case .season(let season):
            return buildSeasonLeaderboard(stat: stat, season: season, limit: limit, league: league)
        case .allTimeSingleSeason:
            return buildAllTimeSingleSeasonLeaderboard(stat: stat, limit: limit, league: league)
        case .allTimeSince(let year):
            return buildAllTimeSinceLeaderboard(stat: stat, sinceYear: year, limit: limit, league: league)
        case .career:
            if stat.displayAbbrev == "OPS+" {
                return "Career OPS+ leaders require weighted season averaging, which isn't supported yet. Try **career OPS leaders** instead.\n\n[SUGGEST]career ops leaders[/SUGGEST]"
            }
            return buildCareerLeaderboard(stat: stat, limit: limit, league: league)
        }
    }

    private static func buildSeasonLeaderboard(stat: PlayerNameMatcher.StatInfo, season: Int, limit: Int, league: String? = nil) -> String {
        let leagueFilter = league.map { " AND \(leagueTeamClause($0, alias: "s"))" } ?? ""
        let leagueLabel = league.map { " (\($0))" } ?? ""

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
            WHERE s.season = \(season)\(paFilter)\(leagueFilter)
            ORDER BY s.\(stat.dbColumn) DESC
            LIMIT \(limit)
            """
        guard let result = try? db.execute(sql: sql),
              !result.rows.isEmpty else {
            return "No \(stat.displayName) leaders found for \(season)\(leagueLabel)."
        }

        var parts: [String] = []
        parts.append("**\(season) \(stat.displayName) Leaders\(leagueLabel)**\n")
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

        if let league = league {
            let other = league == "AL" ? "NL" : "AL"
            parts.append("[SUGGEST]\(season) \(statName) leaders (\(other))[/SUGGEST]")
            parts.append("[SUGGEST]\(season) \(statName) leaders (MLB)[/SUGGEST]")
        } else {
            parts.append("[SUGGEST]\(season) \(statName) leaders (AL)[/SUGGEST]")
            parts.append("[SUGGEST]\(season) \(statName) leaders (NL)[/SUGGEST]")
        }

        return parts.joined(separator: "\n")
    }

    /// Builds a leaderboard for a specific stat, filtered to only the given player names.
    static func buildPlayerSubsetLeaderboard(playerNames: [String], stat: PlayerNameMatcher.StatInfo, season: Int?, isPitching: Bool) -> String? {
        let sanitized = playerNames.map { $0.replacingOccurrences(of: "'", with: "''") }
        let inClause = sanitized.map { "'\($0)'" }.joined(separator: ", ")

        let table = isPitching ? "season_pitching_stats" : "season_batting_stats"
        let seasonFilter: String
        let seasonLabel: String
        if let season = season {
            seasonFilter = " AND s.season = \(season)"
            seasonLabel = "\(season) "
        } else {
            // Use most recent season for each player
            seasonFilter = " AND s.season = (SELECT MAX(s2.season) FROM \(table) s2 WHERE s2.player_id = s.player_id)"
            seasonLabel = ""
        }

        let lowerIsBetter = ["era", "whip", "bb_per_9", "hits_per_9", "hr_per_9"].contains(stat.dbColumn)
        let sql = """
            SELECT p.name, s.\(stat.dbColumn), s.season
            FROM \(table) s
            JOIN players p ON s.player_id = p.player_id
            WHERE p.name IN (\(inClause))\(seasonFilter)
            ORDER BY s.\(stat.dbColumn) \(lowerIsBetter ? "ASC" : "DESC")
            """

        guard let result = try? db.execute(sql: sql), !result.rows.isEmpty else {
            return nil
        }

        var parts: [String] = []
        parts.append("**\(seasonLabel)\(stat.displayName) for these players:**\n")

        parts.append("[LEADERBOARD]")
        parts.append("HEADER: \(stat.displayAbbrev)")
        for (i, row) in result.rows.enumerated() {
            let name = row[0]
            let rawValue = row[1]
            let formattedValue = stat.isRate ? formatRate(rawValue) : rawValue
            parts.append("ROW \(i + 1). \(name): \(formattedValue)")
        }
        parts.append("[/LEADERBOARD]")

        return parts.joined(separator: "\n")
    }

    private static func buildAllTimeSingleSeasonLeaderboard(stat: PlayerNameMatcher.StatInfo, limit: Int, league: String? = nil) -> String {
        let leagueFilter = league.map { " AND \(leagueTeamClause($0, alias: "s"))" } ?? ""
        let leagueLabel = league.map { " (\($0))" } ?? ""
        let paFilter = stat.isRate ? " WHERE s.plate_appearances >= 400\(leagueFilter)" : (leagueFilter.isEmpty ? "" : " WHERE \(leagueFilter.dropFirst(5))")

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
            return "No all-time \(stat.displayName) leaders found\(leagueLabel)."
        }

        var parts: [String] = []
        parts.append("**All-Time Single Season \(stat.displayName) Leaders\(leagueLabel)**\n")
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

        if let league = league {
            let other = league == "AL" ? "NL" : "AL"
            parts.append("[SUGGEST]all-time single season \(statName) leaders (\(other))[/SUGGEST]")
            parts.append("[SUGGEST]all-time single season \(statName) leaders (MLB)[/SUGGEST]")
        } else {
            parts.append("[SUGGEST]all-time single season \(statName) leaders (AL)[/SUGGEST]")
            parts.append("[SUGGEST]all-time single season \(statName) leaders (NL)[/SUGGEST]")
        }

        return parts.joined(separator: "\n")
    }

    private static func buildAllTimeSinceLeaderboard(stat: PlayerNameMatcher.StatInfo, sinceYear: Int, limit: Int, league: String? = nil) -> String {
        let leagueFilter = league.map { " AND \(leagueTeamClause($0, alias: "s"))" } ?? ""
        let leagueLabel = league.map { " (\($0))" } ?? ""
        let paFilter = stat.isRate ? " AND s.plate_appearances >= 400" : ""

        let sql = """
            SELECT p.name, s.\(stat.dbColumn), s.season
            FROM season_batting_stats s
            JOIN players p ON s.player_id = p.player_id
            WHERE s.season >= \(sinceYear)\(paFilter)\(leagueFilter)
            ORDER BY s.\(stat.dbColumn) DESC
            LIMIT \(limit)
            """
        guard let result = try? db.execute(sql: sql),
              !result.rows.isEmpty else {
            return "No \(stat.displayName) leaders found since \(sinceYear)\(leagueLabel)."
        }

        var parts: [String] = []
        parts.append("**\(stat.displayName) Leaders Since \(sinceYear)\(leagueLabel)**\n")
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
        parts.append("\n[SUGGEST]all-time single season \(statName) leaders[/SUGGEST]")

        if let league = league {
            let other = league == "AL" ? "NL" : "AL"
            parts.append("[SUGGEST]\(statName) leaders since \(sinceYear) (\(other))[/SUGGEST]")
            parts.append("[SUGGEST]\(statName) leaders since \(sinceYear) (MLB)[/SUGGEST]")
        } else {
            parts.append("[SUGGEST]\(statName) leaders since \(sinceYear) (AL)[/SUGGEST]")
            parts.append("[SUGGEST]\(statName) leaders since \(sinceYear) (NL)[/SUGGEST]")
        }

        return parts.joined(separator: "\n")
    }

    private static func buildCareerLeaderboard(stat: PlayerNameMatcher.StatInfo, limit: Int, league: String? = nil) -> String {
        let leagueFilter = league.map { " WHERE \(leagueTeamClause($0, alias: "s"))" } ?? ""
        let leagueLabel = league.map { " (\($0))" } ?? ""
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
            \(leagueFilter)
            GROUP BY p.player_id\(paFilter)
            ORDER BY career_val DESC
            LIMIT \(limit)
            """
        guard let result = try? db.execute(sql: sql),
              !result.rows.isEmpty else {
            return "No career \(stat.displayName) leaders found\(leagueLabel)."
        }

        var parts: [String] = []
        parts.append("**Career \(stat.displayName) Leaders\(leagueLabel)**\n")
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

        if let league = league {
            let other = league == "AL" ? "NL" : "AL"
            parts.append("[SUGGEST]career \(statName) leaders (\(other))[/SUGGEST]")
            parts.append("[SUGGEST]career \(statName) leaders (MLB)[/SUGGEST]")
        } else {
            parts.append("[SUGGEST]career \(statName) leaders (AL)[/SUGGEST]")
            parts.append("[SUGGEST]career \(statName) leaders (NL)[/SUGGEST]")
        }

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
            WHERE p.name = '\(sanitize(name))' AND cf.season = \(season)
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
            WHERE p.name = '\(sanitize(name))' AND g.season = \(season)
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
            WHERE p.name = '\(sanitize(name))'
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

    // MARK: - Pitch type splits (chat response builder)

    /// Build a pitch type splits response for chat queries like "X vs sliders".
    static func buildPitchTypeSplits(name: String, pitchType: String?, season: Int) -> String? {
        let info = fetchPlayerInfo(name: name)
        let displayName = info?.name ?? name

        var filter = ""
        if let pitchType {
            filter = " AND pts.pitch_type = '\(pitchType)'"
        }

        let sql = """
            SELECT pts.pitch_type, pts.at_bats, pts.hits,
                   pts.doubles, pts.triples, pts.home_runs, pts.rbi,
                   pts.walks, pts.strikeouts,
                   pts.batting_avg, pts.obp, pts.slg, pts.ops
            FROM pitch_type_batting_splits pts
            JOIN players p ON pts.player_id = p.player_id
            WHERE p.name = '\(sanitize(name))' AND pts.season = \(season)\(filter)
            ORDER BY pts.at_bats DESC
            """
        guard let result = try? db.execute(sql: sql),
              !result.rows.isEmpty else { return nil }

        let headers = ["AB", "H", "2B", "3B", "HR", "RBI", "BB", "SO", "AVG", "OBP", "SLG", "OPS"]

        let subtitle = pitchType.map { "vs \($0)" } ?? "By Pitch Type"

        var parts: [String] = []
        parts.append("**\(displayName)** \u{2014} \(season) \(subtitle)\n")

        parts.append("[STATGRID]")
        parts.append("HEADER: " + headers.joined(separator: ", "))
        for row in result.rows {
            let label = row[0]
            let values = formatValues(headers: headers, values: Array(row.dropFirst()))
            parts.append("ROW \(label): " + values.joined(separator: ", "))
        }
        parts.append("[/STATGRID]")

        parts.append("\n[SUGGEST]\(displayName) \(season)[/SUGGEST]")
        parts.append("[SUGGEST]\(displayName) vs lefties \(season)[/SUGGEST]")

        return parts.joined(separator: "\n")
    }

    /// Build pitch type splits for a pitcher.
    static func buildPitchingPitchTypeSplits(name: String, pitchType: String?, season: Int) -> String? {
        let info = fetchPlayerInfo(name: name)
        let displayName = info?.name ?? name

        var filter = ""
        if let pitchType {
            filter = " AND pts.pitch_type = '\(pitchType)'"
        }

        let sql = """
            SELECT pts.pitch_type, pts.at_bats, pts.hits,
                   pts.doubles, pts.triples, pts.home_runs,
                   pts.walks, pts.strikeouts,
                   pts.batting_avg_against, pts.obp_against, pts.slg_against, pts.ops_against
            FROM pitch_type_pitching_splits pts
            JOIN players p ON pts.player_id = p.player_id
            WHERE p.name = '\(sanitize(name))' AND pts.season = \(season)\(filter)
            ORDER BY pts.at_bats DESC
            """
        guard let result = try? db.execute(sql: sql),
              !result.rows.isEmpty else { return nil }

        let headers = ["AB", "H", "2B", "3B", "HR", "BB", "SO", "AVG", "OBP", "SLG", "OPS"]

        let subtitle = pitchType.map { "vs \($0)" } ?? "By Pitch Type"

        var parts: [String] = []
        parts.append("**\(displayName)** \u{2014} \(season) \(subtitle) (Pitching)\n")

        parts.append("[STATGRID]")
        parts.append("HEADER: " + headers.joined(separator: ", "))
        for row in result.rows {
            let label = row[0]
            let values = formatPitchingValues(headers: headers, values: Array(row.dropFirst()))
            parts.append("ROW \(label): " + values.joined(separator: ", "))
        }
        parts.append("[/STATGRID]")

        parts.append("\n[SUGGEST]\(displayName) \(season)[/SUGGEST]")

        return parts.joined(separator: "\n")
    }

    // MARK: - Count splits (chat response builder)

    /// Build a count splits response for chat queries like "X with two strikes".
    /// Label for an aggregate count grouping (e.g. "Two Strikes", "Ahead").
    private static func countGroupLabel(_ counts: [String]?) -> String? {
        guard let counts else { return nil }
        let set = Set(counts)
        if set == Set(["0-2", "1-2", "2-2", "3-2"]) { return "Two Strikes" }
        if set == Set(["1-0", "2-0", "2-1", "3-0", "3-1"]) { return "Ahead" }
        if set == Set(["0-1", "0-2", "1-2"]) { return "Behind" }
        return nil
    }

    static func buildCountSplits(name: String, counts: [String]?, season: Int) -> String? {
        let info = fetchPlayerInfo(name: name)
        let displayName = info?.name ?? name

        var filter = ""
        if let counts, !counts.isEmpty {
            let inClause = counts.map { "'\($0)'" }.joined(separator: ", ")
            filter = " AND cs.count_state IN (\(inClause))"
        }

        let sql = """
            SELECT cs.count_state, cs.at_bats, cs.hits,
                   cs.doubles, cs.triples, cs.home_runs, cs.rbi,
                   cs.walks, cs.strikeouts,
                   cs.batting_avg, cs.obp, cs.slg, cs.ops
            FROM count_batting_splits cs
            JOIN players p ON cs.player_id = p.player_id
            WHERE p.name = '\(sanitize(name))' AND cs.season = \(season)\(filter)
            ORDER BY cs.count_state
            """
        guard let result = try? db.execute(sql: sql),
              !result.rows.isEmpty else { return nil }

        let headers = ["AB", "H", "2B", "3B", "HR", "RBI", "BB", "SO", "AVG", "OBP", "SLG", "OPS"]

        let subtitle: String
        let groupLabel = Self.countGroupLabel(counts)
        if let counts, counts.count == 1 {
            subtitle = "in \(counts[0]) Counts"
        } else if let groupLabel {
            subtitle = "With \(groupLabel)"
        } else if let counts, counts.count <= 4 {
            subtitle = "in \(counts.joined(separator: "/")) Counts"
        } else {
            subtitle = "By Count"
        }

        var parts: [String] = []
        parts.append("**\(displayName)** \u{2014} \(season) \(subtitle)\n")

        parts.append("[STATGRID]")
        parts.append("HEADER: " + headers.joined(separator: ", "))

        // When showing a named group with multiple counts, add an aggregate row first
        if let groupLabel, result.rows.count > 1 {
            var totAB = 0, totH = 0, tot2B = 0, tot3B = 0, totHR = 0, totRBI = 0, totBB = 0, totSO = 0
            for row in result.rows {
                totAB += Int(row[1]) ?? 0; totH += Int(row[2]) ?? 0
                tot2B += Int(row[3]) ?? 0; tot3B += Int(row[4]) ?? 0
                totHR += Int(row[5]) ?? 0; totRBI += Int(row[6]) ?? 0
                totBB += Int(row[7]) ?? 0; totSO += Int(row[8]) ?? 0
            }
            let avg = totAB > 0 ? Double(totH) / Double(totAB) : 0
            let sf = 0 // sacrifice flies not in this query
            let hbp = 0
            let pa = totAB + totBB + hbp + sf
            let obp = pa > 0 ? Double(totH + totBB + hbp) / Double(pa) : 0
            let tb = totH - tot2B - tot3B - totHR + 2 * tot2B + 3 * tot3B + 4 * totHR
            let slg = totAB > 0 ? Double(tb) / Double(totAB) : 0
            let ops = obp + slg
            let aggValues = ["\(totAB)", "\(totH)", "\(tot2B)", "\(tot3B)", "\(totHR)", "\(totRBI)",
                             "\(totBB)", "\(totSO)",
                             formatRate(String(format: "%.3f", avg)), formatRate(String(format: "%.3f", obp)),
                             formatRate(String(format: "%.3f", slg)), formatRate(String(format: "%.3f", ops))]
            parts.append("ROW \(groupLabel): " + aggValues.joined(separator: ", "))
        }

        for row in result.rows {
            let label = row[0]
            let values = formatValues(headers: headers, values: Array(row.dropFirst()))
            parts.append("ROW \(label): " + values.joined(separator: ", "))
        }
        parts.append("[/STATGRID]")

        parts.append("\n[SUGGEST]\(displayName) \(season)[/SUGGEST]")
        parts.append("[SUGGEST]\(displayName) vs lefties \(season)[/SUGGEST]")

        return parts.joined(separator: "\n")
    }

    /// Build count splits for a pitcher.
    static func buildPitchingCountSplits(name: String, counts: [String]?, season: Int) -> String? {
        let info = fetchPlayerInfo(name: name)
        let displayName = info?.name ?? name

        var filter = ""
        if let counts, !counts.isEmpty {
            let inClause = counts.map { "'\($0)'" }.joined(separator: ", ")
            filter = " AND cs.count_state IN (\(inClause))"
        }

        let sql = """
            SELECT cs.count_state, cs.at_bats, cs.hits,
                   cs.doubles, cs.triples, cs.home_runs,
                   cs.walks, cs.strikeouts,
                   cs.batting_avg_against, cs.obp_against, cs.slg_against, cs.ops_against
            FROM count_pitching_splits cs
            JOIN players p ON cs.player_id = p.player_id
            WHERE p.name = '\(sanitize(name))' AND cs.season = \(season)\(filter)
            ORDER BY cs.count_state
            """
        guard let result = try? db.execute(sql: sql),
              !result.rows.isEmpty else { return nil }

        let headers = ["AB", "H", "2B", "3B", "HR", "BB", "SO", "AVG", "OBP", "SLG", "OPS"]

        let subtitle: String
        let groupLabel = Self.countGroupLabel(counts)
        if let counts, counts.count == 1 {
            subtitle = "in \(counts[0]) Counts"
        } else if let groupLabel {
            subtitle = "With \(groupLabel)"
        } else if let counts, counts.count <= 4 {
            subtitle = "in \(counts.joined(separator: "/")) Counts"
        } else {
            subtitle = "By Count"
        }

        var parts: [String] = []
        parts.append("**\(displayName)** \u{2014} \(season) \(subtitle) (Pitching)\n")

        parts.append("[STATGRID]")
        parts.append("HEADER: " + headers.joined(separator: ", "))

        // When showing a named group with multiple counts, add an aggregate row first
        if let groupLabel, result.rows.count > 1 {
            var totAB = 0, totH = 0, tot2B = 0, tot3B = 0, totHR = 0, totBB = 0, totSO = 0
            for row in result.rows {
                totAB += Int(row[1]) ?? 0; totH += Int(row[2]) ?? 0
                tot2B += Int(row[3]) ?? 0; tot3B += Int(row[4]) ?? 0
                totHR += Int(row[5]) ?? 0
                totBB += Int(row[6]) ?? 0; totSO += Int(row[7]) ?? 0
            }
            let avg = totAB > 0 ? Double(totH) / Double(totAB) : 0
            let hbp = 0
            let sf = 0
            let pa = totAB + totBB + hbp + sf
            let obp = pa > 0 ? Double(totH + totBB + hbp) / Double(pa) : 0
            let tb = totH - tot2B - tot3B - totHR + 2 * tot2B + 3 * tot3B + 4 * totHR
            let slg = totAB > 0 ? Double(tb) / Double(totAB) : 0
            let ops = obp + slg
            let aggValues = ["\(totAB)", "\(totH)", "\(tot2B)", "\(tot3B)", "\(totHR)",
                             "\(totBB)", "\(totSO)",
                             formatRate(String(format: "%.3f", avg)), formatRate(String(format: "%.3f", obp)),
                             formatRate(String(format: "%.3f", slg)), formatRate(String(format: "%.3f", ops))]
            parts.append("ROW \(groupLabel): " + aggValues.joined(separator: ", "))
        }

        for row in result.rows {
            let label = row[0]
            let values = formatPitchingValues(headers: headers, values: Array(row.dropFirst()))
            parts.append("ROW \(label): " + values.joined(separator: ", "))
        }
        parts.append("[/STATGRID]")

        parts.append("\n[SUGGEST]\(displayName) \(season)[/SUGGEST]")

        return parts.joined(separator: "\n")
    }

    // MARK: - RISP splits (chat response builder)

    /// Build a RISP splits response for chat queries like "X with runners in scoring position".
    static func buildRISPSplits(name: String, season: Int) -> String? {
        let info = fetchPlayerInfo(name: name)
        let displayName = info?.name ?? name

        let sql = """
            SELECT rs.split, rs.at_bats, rs.hits,
                   rs.doubles, rs.triples, rs.home_runs, rs.rbi,
                   rs.walks, rs.strikeouts,
                   rs.batting_avg, rs.obp, rs.slg, rs.ops
            FROM risp_batting_splits rs
            JOIN players p ON rs.player_id = p.player_id
            WHERE p.name = '\(sanitize(name))' AND rs.season = \(season)
            ORDER BY rs.split DESC
            """
        guard let result = try? db.execute(sql: sql),
              !result.rows.isEmpty else { return nil }

        let headers = ["AB", "H", "2B", "3B", "HR", "RBI", "BB", "SO", "AVG", "OBP", "SLG", "OPS"]

        var parts: [String] = []
        parts.append("**\(displayName)** \u{2014} \(season) With Runners in Scoring Position\n")

        parts.append("[STATGRID]")
        parts.append("HEADER: " + headers.joined(separator: ", "))
        for row in result.rows.prefix(2) {
            let splitLabel = row[0] == "RISP" ? "RISP" : "Non-RISP"
            let values = formatValues(headers: headers, values: Array(row.dropFirst()))
            parts.append("ROW \(splitLabel): " + values.joined(separator: ", "))
        }
        parts.append("[/STATGRID]")

        parts.append("\n[SUGGEST]\(displayName) \(season)[/SUGGEST]")
        parts.append("[SUGGEST]\(displayName) vs lefties \(season)[/SUGGEST]")

        return parts.joined(separator: "\n")
    }

    /// Build RISP splits for a pitcher.
    static func buildPitchingRISPSplits(name: String, season: Int) -> String? {
        let info = fetchPlayerInfo(name: name)
        let displayName = info?.name ?? name

        let sql = """
            SELECT rs.split, rs.at_bats, rs.hits,
                   rs.doubles, rs.triples, rs.home_runs,
                   rs.walks, rs.strikeouts,
                   rs.batting_avg_against, rs.obp_against, rs.slg_against, rs.ops_against
            FROM risp_pitching_splits rs
            JOIN players p ON rs.player_id = p.player_id
            WHERE p.name = '\(sanitize(name))' AND rs.season = \(season)
            ORDER BY rs.split DESC
            """
        guard let result = try? db.execute(sql: sql),
              !result.rows.isEmpty else { return nil }

        let headers = ["AB", "H", "2B", "3B", "HR", "BB", "SO", "AVG", "OBP", "SLG", "OPS"]

        var parts: [String] = []
        parts.append("**\(displayName)** \u{2014} \(season) With RISP (Pitching)\n")

        parts.append("[STATGRID]")
        parts.append("HEADER: " + headers.joined(separator: ", "))
        for row in result.rows.prefix(2) {
            let splitLabel = row[0] == "RISP" ? "RISP" : "Non-RISP"
            let values = formatPitchingValues(headers: headers, values: Array(row.dropFirst()))
            parts.append("ROW \(splitLabel): " + values.joined(separator: ", "))
        }
        parts.append("[/STATGRID]")

        parts.append("\n[SUGGEST]\(displayName) \(season)[/SUGGEST]")

        return parts.joined(separator: "\n")
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
            WHERE p.name = '\(sanitize(name))' AND st.performance = 'hot'
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
                WHERE p.name = '\(sanitize(name))' AND ss.performance = 'hot'
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
                WHERE p.name = '\(sanitize(name))' AND sl.performance = 'hot'
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

    // MARK: - Month stats (game log aggregation)

    /// Build a stat summary for a player in a specific month by aggregating game logs.
    /// Returns a formatted STATGRID response, or nil if no data found.
    static func buildMonthStats(name: String, month: Int, season: Int) -> String? {
        let info = fetchPlayerInfo(name: name)
        let displayName = info?.name ?? name
        let monthPad = String(format: "%02d", month)
        let monthNames = ["", "January", "February", "March", "April", "May", "June",
                          "July", "August", "September", "October", "November", "December"]
        let monthName = month >= 1 && month <= 12 ? monthNames[month] : "Month \(month)"

        // Try batting first
        let battingSql = """
            SELECT COUNT(*) as g,
                   SUM(g.at_bats) as ab, SUM(g.hits) as h, SUM(g.doubles) as d2b,
                   SUM(g.triples) as d3b, SUM(g.home_runs) as hr,
                   SUM(g.runs) as r, SUM(g.rbi) as rbi,
                   SUM(g.walks) as bb, SUM(g.strikeouts) as so,
                   SUM(g.plate_appearances) as pa,
                   SUM(g.hit_by_pitch) as hbp, SUM(g.sacrifice_flies) as sf
            FROM game_batting_logs g
            JOIN players p ON g.player_id = p.player_id
            WHERE p.name = '\(sanitize(name))'
              AND g.season = \(season)
              AND substr(g.date, 6, 2) = '\(monthPad)'
            """
        if let result = try? db.execute(sql: battingSql),
           let row = result.rows.first,
           let games = Int(row[0]), games > 0 {
            let ab = Int(row[1]) ?? 0
            let h = Int(row[2]) ?? 0
            let d2b = Int(row[3]) ?? 0
            let d3b = Int(row[4]) ?? 0
            let hr = Int(row[5]) ?? 0
            let r = Int(row[6]) ?? 0
            let rbi = Int(row[7]) ?? 0
            let bb = Int(row[8]) ?? 0
            let so = Int(row[9]) ?? 0
            let pa = Int(row[10]) ?? 0
            let hbp = Int(row[11]) ?? 0
            let sf = Int(row[12]) ?? 0

            // Compute rate stats
            let avg = ab > 0 ? Double(h) / Double(ab) : 0.0
            let obpDenom = ab + bb + hbp + sf
            let obp = obpDenom > 0 ? Double(h + bb + hbp) / Double(obpDenom) : 0.0
            let tb = h + d2b + 2 * d3b + 3 * hr
            let slg = ab > 0 ? Double(tb) / Double(ab) : 0.0
            let ops = obp + slg

            let avgStr = formatRate(String(format: "%.3f", avg))
            let obpStr = formatRate(String(format: "%.3f", obp))
            let slgStr = formatRate(String(format: "%.3f", slg))
            let opsStr = formatRate(String(format: "%.3f", ops))

            var parts: [String] = []
            parts.append("**\(displayName)** \u{2014} \(monthName) \(season)\n")

            parts.append("[STATGRID]")
            parts.append("HEADER: G, AB, R, H, 2B, 3B, HR, RBI, BB, SO, AVG, OBP, SLG, OPS")
            parts.append("ROW: \(games), \(ab), \(r), \(h), \(d2b), \(d3b), \(hr), \(rbi), \(bb), \(so), \(avgStr), \(obpStr), \(slgStr), \(opsStr)")
            parts.append("[/STATGRID]")

            parts.append("\n[SUGGEST]\(displayName) \(season)[/SUGGEST]")
            if isActivePlayer(name: name) {
                parts.append("[SUGGEST]how is \(displayName) doing lately[/SUGGEST]")
            }

            return parts.joined(separator: "\n")
        }

        // Try pitching
        let pitchingSql = """
            SELECT COUNT(*) as g,
                   SUM(g.ip_outs) as ip_outs, SUM(g.hits) as h,
                   SUM(g.earned_runs) as er, SUM(g.walks) as bb,
                   SUM(g.strikeouts) as so, SUM(g.home_runs) as hr,
                   SUM(g.hit_batters) as hb,
                   SUM(g.wins) as w, SUM(g.losses) as l, SUM(g.saves) as sv,
                   SUM(g.games_started) as gs
            FROM game_pitching_logs g
            JOIN players p ON g.player_id = p.player_id
            WHERE p.name = '\(sanitize(name))'
              AND g.season = \(season)
              AND substr(g.date, 6, 2) = '\(monthPad)'
            """
        if let result = try? db.execute(sql: pitchingSql),
           let row = result.rows.first,
           let games = Int(row[0]), games > 0 {
            let ipOuts = Int(row[1]) ?? 0
            let h = Int(row[2]) ?? 0
            let er = Int(row[3]) ?? 0
            let bb = Int(row[4]) ?? 0
            let so = Int(row[5]) ?? 0
            let hr = Int(row[6]) ?? 0
            let w = Int(row[8]) ?? 0
            let l = Int(row[9]) ?? 0
            let sv = Int(row[10]) ?? 0
            let gs = Int(row[11]) ?? 0

            // IP from outs
            let fullInnings = ipOuts / 3
            let remainder = ipOuts % 3
            let ipStr = remainder == 0 ? "\(fullInnings)" : "\(fullInnings).\(remainder)"
            let ipDouble = Double(ipOuts) / 3.0

            // ERA = (ER / IP) * 9
            let era = ipDouble > 0 ? (Double(er) / ipDouble) * 9.0 : 0.0
            // WHIP = (BB + H) / IP
            let whip = ipDouble > 0 ? Double(bb + h) / ipDouble : 0.0
            // K/9 = (SO / IP) * 9
            let kPer9 = ipDouble > 0 ? (Double(so) / ipDouble) * 9.0 : 0.0
            // BB/9 = (BB / IP) * 9
            let bbPer9 = ipDouble > 0 ? (Double(bb) / ipDouble) * 9.0 : 0.0

            let eraStr = String(format: "%.2f", era)
            let whipStr = String(format: "%.2f", whip)
            let kPer9Str = String(format: "%.1f", kPer9)
            let bbPer9Str = String(format: "%.1f", bbPer9)

            var parts: [String] = []
            parts.append("**\(displayName)** \u{2014} \(monthName) \(season)\n")

            parts.append("[STATGRID]")
            parts.append("HEADER: W, L, SV, G, GS, IP, H, ER, BB, SO, HR, ERA, WHIP, K/9, BB/9")
            parts.append("ROW: \(w), \(l), \(sv), \(games), \(gs), \(ipStr), \(h), \(er), \(bb), \(so), \(hr), \(eraStr), \(whipStr), \(kPer9Str), \(bbPer9Str)")
            parts.append("[/STATGRID]")

            parts.append("\n[SUGGEST]\(displayName) \(season)[/SUGGEST]")
            if isActivePlayer(name: name) {
                parts.append("[SUGGEST]how is \(displayName) doing lately[/SUGGEST]")
            }

            return parts.joined(separator: "\n")
        }

        return nil
    }

    // MARK: - Helpers

    // MARK: - Composite threshold (30/30, 40/40, etc.)

    static func buildCompositeThresholdResponse(threshold: Int) -> String {
        // Player ranking — who did it the most times
        let rankSql = """
            SELECT p.name, COUNT(*) as times,
                   GROUP_CONCAT(s.season || ' (' || s.home_runs || '/' || s.stolen_bases || ')', ', ') as seasons
            FROM season_batting_stats s
            JOIN players p ON s.player_id = p.player_id
            WHERE s.home_runs >= \(threshold) AND s.stolen_bases >= \(threshold)
            GROUP BY p.player_id
            ORDER BY times DESC, MAX(s.home_runs + s.stolen_bases) DESC
            """
        // All individual seasons
        let allSql = """
            SELECT p.name, s.season, s.home_runs, s.stolen_bases
            FROM season_batting_stats s
            JOIN players p ON s.player_id = p.player_id
            WHERE s.home_runs >= \(threshold) AND s.stolen_bases >= \(threshold)
            ORDER BY s.season DESC, s.home_runs + s.stolen_bases DESC
            """
        guard let rankResult = try? db.execute(sql: rankSql), !rankResult.rows.isEmpty,
              let allResult = try? db.execute(sql: allSql) else {
            return "No player has ever achieved a \(threshold)/\(threshold) season (HR and SB)."
        }

        let totalSeasons = allResult.rows.count
        let totalPlayers = rankResult.rows.count
        var parts: [String] = []
        parts.append("**\(threshold)/\(threshold) Seasons (HR & SB)**\n")
        parts.append("\(totalSeasons) time\(totalSeasons == 1 ? "" : "s") by \(totalPlayers) player\(totalPlayers == 1 ? "" : "s").\n")
        parts.append("[TIP]Tap a player name for their full profile.[/TIP]")

        // Player ranking
        parts.append("[LEADERBOARD]")
        parts.append("HEADER: Times, Seasons")
        for (i, row) in rankResult.rows.enumerated() {
            let playerName = row[0]
            let times = row[1]
            let seasons = row[2]
            parts.append("ROW \(i + 1). \(playerName): \(times), \(seasons)")
        }
        parts.append("[/LEADERBOARD]")

        // Full list of all individual seasons
        parts.append("\n**All \(threshold)/\(threshold) Seasons**\n")
        parts.append("[LEADERBOARD]")
        parts.append("HEADER: Year, HR, SB")
        for (i, row) in allResult.rows.enumerated() {
            let playerName = row[0]
            let season = row[1]
            let hr = row[2]
            let sb = row[3]
            parts.append("ROW \(i + 1). \(playerName): \(season), \(hr), \(sb)")
        }
        parts.append("[/LEADERBOARD]")

        return parts.joined(separator: "\n")
    }

    // MARK: - Triple Crown

    static func buildTripleCrownResponse() -> String {
        // For each season, find the leader in AVG (min 400 AB), HR, and RBI
        // Check if the same player led in all three
        let sql = """
            WITH avg_leaders AS (
                SELECT s.season, p.name, s.batting_avg,
                       ROW_NUMBER() OVER (PARTITION BY s.season ORDER BY s.batting_avg DESC) as rn
                FROM season_batting_stats s
                JOIN players p ON s.player_id = p.player_id
                WHERE s.at_bats >= 400
            ),
            hr_leaders AS (
                SELECT s.season, p.name, s.home_runs,
                       ROW_NUMBER() OVER (PARTITION BY s.season ORDER BY s.home_runs DESC) as rn
                FROM season_batting_stats s
                JOIN players p ON s.player_id = p.player_id
            ),
            rbi_leaders AS (
                SELECT s.season, p.name, s.rbi,
                       ROW_NUMBER() OVER (PARTITION BY s.season ORDER BY s.rbi DESC) as rn
                FROM season_batting_stats s
                JOIN players p ON s.player_id = p.player_id
            )
            SELECT a.season, a.name, a.batting_avg, h.home_runs, r.rbi
            FROM avg_leaders a
            JOIN hr_leaders h ON a.season = h.season AND a.name = h.name AND h.rn = 1
            JOIN rbi_leaders r ON a.season = r.season AND a.name = r.name AND r.rn = 1
            WHERE a.rn = 1
            ORDER BY a.season DESC
            """
        guard let result = try? db.execute(sql: sql), !result.rows.isEmpty else {
            return "No Triple Crown winners found in the database."
        }

        let count = result.rows.count
        var parts: [String] = []
        parts.append("**Triple Crown Winners**\n")
        parts.append("The Triple Crown is awarded when a player leads their league (or all of MLB) in batting average, home runs, and RBI in the same season. It has happened \(count) time\(count == 1 ? "" : "s") in our records.\n")
        parts.append("[TIP]Tap a player name for their full profile.[/TIP]")

        parts.append("[LEADERBOARD]")
        parts.append("HEADER: Year, AVG, HR, RBI")
        for (i, row) in result.rows.enumerated() {
            let season = row[0]
            let name = row[1]
            let avg = formatRate(row[2])
            let hr = row[3]
            let rbi = row[4]
            parts.append("ROW \(i + 1). \(name): \(season), \(avg), \(hr), \(rbi)")
        }
        parts.append("[/LEADERBOARD]")

        parts.append("\n_Note: Based on overall MLB leaders with min. 400 AB. Historical league-specific Triple Crowns may differ._")

        return parts.joined(separator: "\n")
    }

    // MARK: - Consecutive streak (hitting streak, on-base streak)

    static func buildConsecutiveStreakResponse(type: PlayerNameMatcher.ConsecutiveStreakQuery.StreakType, playerName: String?, season: Int?) -> String {
        let hitCondition: String
        let streakLabel: String
        switch type {
        case .hit:
            hitCondition = "hits > 0"
            streakLabel = "Hitting"
        case .onbase:
            hitCondition = "(hits + walks + COALESCE(hit_by_pitch, 0)) > 0"
            streakLabel = "On-Base"
        }

        let playerFilter: String
        let playerJoin: String
        if let name = playerName {
            playerJoin = "JOIN players p ON g.player_id = p.player_id"
            playerFilter = "AND p.name = '\(sanitize(name))'"
        } else {
            playerJoin = "JOIN players p ON g.player_id = p.player_id"
            playerFilter = ""
        }

        let seasonFilter = season.map { "AND g.season = \($0)" } ?? ""

        let sql = """
            WITH numbered AS (
                SELECT g.player_id, p.name, g.date, g.hits, g.walks, COALESCE(g.hit_by_pitch, 0) as hbp, g.season,
                       ROW_NUMBER() OVER (PARTITION BY g.player_id ORDER BY g.date) as game_num
                FROM game_batting_logs g
                \(playerJoin)
                WHERE 1=1 \(playerFilter) \(seasonFilter)
            ),
            qualifying AS (
                SELECT *, ROW_NUMBER() OVER (PARTITION BY player_id ORDER BY date) as qual_num
                FROM numbered
                WHERE \(hitCondition)
            ),
            streaks AS (
                SELECT player_id, name,
                       COUNT(*) as streak_len,
                       MIN(date) as start_date,
                       MAX(date) as end_date,
                       MIN(season) as season
                FROM qualifying
                GROUP BY player_id, game_num - qual_num
            )
            SELECT name, streak_len, season, start_date, end_date
            FROM streaks
            ORDER BY streak_len DESC
            LIMIT 15
            """

        guard let result = try? db.execute(sql: sql), !result.rows.isEmpty else {
            let scope = playerName ?? "any player"
            return "No \(streakLabel.lowercased()) streak data found for \(scope)."
        }

        var parts: [String] = []
        let scopeLabel: String
        if let name = playerName {
            let info = fetchPlayerInfo(name: name)
            let displayName = info?.name ?? name
            if let season {
                scopeLabel = "\(displayName) \u{2014} \(season)"
            } else {
                scopeLabel = displayName
            }
        } else {
            if let season {
                scopeLabel = "\(season)"
            } else {
                scopeLabel = "Since 2016"
            }
        }

        parts.append("**Longest \(streakLabel) Streaks \u{2014} \(scopeLabel)**\n")
        parts.append("[TIP]Tap a player name for their full profile.[/TIP]")

        parts.append("[LEADERBOARD]")
        parts.append("HEADER: Games, Season, Dates")
        for (i, row) in result.rows.enumerated() {
            let name = row[0]
            let streakLen = row[1]
            let season = row[2]
            let startDate = formatDate(row[3])
            let endDate = formatDate(row[4])
            parts.append("ROW \(i + 1). \(name): \(streakLen), \(season), \(startDate)\u{2013}\(endDate)")
        }
        parts.append("[/LEADERBOARD]")

        return parts.joined(separator: "\n")
    }

    private static func sanitize(_ name: String) -> String {
        name.replacingOccurrences(of: "'", with: "''")
    }

    /// Filter to exclude seasons with missing earned runs data (1903-1908 in Retrosheet).
    /// These show ERA=0.00 with 200+ IP which is bad data, not real.
    /// Only applied when the query involves ERA-related stats.
    private static func eraDataFilter(prefix: String, stat: PlayerNameMatcher.StatInfo, additionalStats: [PlayerNameMatcher.StatInfo] = []) -> String {
        let allStats = [stat] + additionalStats
        let eraRelated = ["era", "earned_runs", "whip", "era_plus"]
        if allStats.contains(where: { eraRelated.contains($0.dbColumn) }) {
            return " AND NOT (\(prefix).earned_runs = 0 AND \(prefix).ip_outs > 0)"
        }
        return ""
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

    static func teamFullName(_ abbreviation: String) -> String {
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
            // Historical franchises
            "CAL": "California Angels", "KC1": "Kansas City Athletics",
            "ML1": "Milwaukee Braves", "BSN": "Boston Braves",
            "BRO": "Brooklyn Dodgers", "NYG": "New York Giants",
            "PHA": "Philadelphia Athletics", "SLA": "St. Louis Browns",
            "WS1": "Washington Senators", "WS2": "Washington Senators (1961)",
            "SE1": "Seattle Pilots", "ML4": "Milwaukee Brewers (AL)",
        ]
        return teams[abbreviation] ?? abbreviation
    }

    /// Convert Retrosheet code to common display abbreviation (e.g., NYA→NYY, LAN→LAD, SFN→SF)
    static func displayAbbreviation(_ code: String) -> String {
        let map: [String: String] = [
            "NYA": "NYY", "NYN": "NYM", "LAN": "LAD", "SFN": "SF", "SDN": "SD",
            "CHA": "CWS", "CHN": "CHC", "TBA": "TB", "KCA": "KC", "SLN": "STL",
            "ANA": "LAA", "WAS": "WSH", "MON": "MTL", "FLO": "FLA", "CAL": "CAL",
            "BSN": "BSN", "BRO": "BRO", "PHA": "PHA", "SLA": "SLB", "WS1": "WSH",
            "ML4": "MIL", "SE1": "SEA", "NYG": "NYG", "PHI": "PHI",
        ]
        return map[code] ?? code
    }

    /// Expand a team string that may contain "/" for multi-team seasons (e.g., "MIA/NYA" → "Miami Marlins / New York Yankees")
    static func teamDisplayName(_ teamStr: String) -> String {
        let parts = teamStr.split(separator: "/")
        if parts.count > 1 {
            return parts.map { teamFullName(String($0)) }.joined(separator: " / ")
        }
        return teamFullName(teamStr)
    }

    // MARK: - Reverse team lookup

    /// Lazily-built reverse map: full team name → Retrosheet code
    private static let reverseTeamMap: [String: String] = {
        // Collect all (code → fullName) pairs from the forward map, then invert.
        // If multiple codes map to the same fullName, the first one wins (fine for reverse lookup).
        let teams: [String: String] = [
            "ARI": "Arizona Diamondbacks", "ATL": "Atlanta Braves",
            "BAL": "Baltimore Orioles", "BOS": "Boston Red Sox",
            "CHN": "Chicago Cubs", "CHA": "Chicago White Sox",
            "CIN": "Cincinnati Reds", "CLE": "Cleveland Guardians",
            "COL": "Colorado Rockies", "DET": "Detroit Tigers",
            "HOU": "Houston Astros", "KCA": "Kansas City Royals",
            "ANA": "Los Angeles Angels", "LAN": "Los Angeles Dodgers",
            "MIA": "Miami Marlins", "MIL": "Milwaukee Brewers",
            "MIN": "Minnesota Twins", "NYN": "New York Mets",
            "NYA": "New York Yankees", "OAK": "Oakland Athletics",
            "PHI": "Philadelphia Phillies", "PIT": "Pittsburgh Pirates",
            "SDN": "San Diego Padres", "SFN": "San Francisco Giants",
            "SEA": "Seattle Mariners", "SLN": "St. Louis Cardinals",
            "TBA": "Tampa Bay Rays", "TEX": "Texas Rangers",
            "TOR": "Toronto Blue Jays", "WAS": "Washington Nationals",
        ]
        var reverse: [String: String] = [:]
        for (code, name) in teams {
            reverse[name] = code
        }
        return reverse
    }()

    /// Returns the Retrosheet team code for a full team name, or nil if not found.
    static func teamCodeFromFullName(_ name: String) -> String? {
        reverseTeamMap[name]
    }

    /// Case-insensitive version — matches "new york yankees" etc.
    static func teamCodeFromFullNameCaseInsensitive(_ input: String) -> String? {
        let lower = input.lowercased()
        for (name, code) in reverseTeamMap {
            if name.lowercased() == lower { return code }
        }
        return nil
    }

    // MARK: - Team card fetch

    static func fetchTeamCard(teamCode: String) -> TeamCard? {
        let fullName = teamFullName(teamCode)

        // Find all seasons this team has data for
        let seasonsSql = """
            SELECT DISTINCT s.season
            FROM season_batting_stats s
            WHERE s.team = '\(sanitize(teamCode))' OR s.team LIKE '\(sanitize(teamCode))/%' OR s.team LIKE '%/\(sanitize(teamCode))'
            ORDER BY s.season DESC
            """
        guard let seasonsResult = try? db.execute(sql: seasonsSql),
              !seasonsResult.rows.isEmpty else { return nil }

        let years = seasonsResult.rows.compactMap { Int($0[0]) }
        guard !years.isEmpty else { return nil }

        var teamSeasons: [TeamSeasonData] = []
        for year in years {
            let teamFilter = "(s.team = '\(sanitize(teamCode))' OR s.team LIKE '\(sanitize(teamCode))/%' OR s.team LIKE '%/\(sanitize(teamCode))')"

            // 2a. Team aggregate stats
            let aggSql = """
                SELECT SUM(s.games), SUM(s.at_bats), SUM(s.runs), SUM(s.hits),
                       SUM(s.doubles), SUM(s.triples), SUM(s.home_runs), SUM(s.rbi),
                       SUM(s.stolen_bases), SUM(s.walks), SUM(s.strikeouts)
                FROM season_batting_stats s
                WHERE \(teamFilter) AND s.season = \(year)
                """
            guard let aggResult = try? db.execute(sql: aggSql),
                  let aggRow = aggResult.rows.first,
                  aggRow.count >= 11 else { continue }

            let _ = aggRow[0], ab = aggRow[1], r = aggRow[2], h = aggRow[3]
            let d = aggRow[4], t = aggRow[5], hr = aggRow[6], rbi = aggRow[7]
            let sb = aggRow[8], bb = aggRow[9], so = aggRow[10]

            let abVal = Double(ab) ?? 0
            let hVal = Double(h) ?? 0
            let bbVal = Double(bb) ?? 0
            let dVal = Double(d) ?? 0
            let tVal = Double(t) ?? 0
            let hrVal = Double(hr) ?? 0

            let avg = abVal > 0 ? hVal / abVal : 0
            // Approximate PA for team OBP (no HBP/SF aggregation readily available at team level)
            let pa = abVal + bbVal
            let obp = pa > 0 ? (hVal + bbVal) / pa : 0
            let tbVal = hVal + dVal + 2 * tVal + 3 * hrVal
            let slg = abVal > 0 ? tbVal / abVal : 0
            let ops = obp + slg

            let teamHeaders = ["R", "H", "2B", "3B", "HR", "RBI", "SB", "BB", "SO", "AVG", "OBP", "SLG", "OPS"]
            let teamValues = [r, h, d, t, hr, rbi, sb, bb, so,
                              formatRate(String(avg)), formatRate(String(obp)),
                              formatRate(String(slg)), formatRate(String(ops))]
            let statsGrid = StatGridParser.StatGrid(
                headers: teamHeaders,
                rows: [StatGridParser.StatGrid.Row(label: "", values: teamValues)]
            )

            // 2a-2. Team pitching aggregate stats
            let pitchingTeamFilterAgg = "(sp.team = '\(sanitize(teamCode))' OR sp.team LIKE '\(sanitize(teamCode))/%' OR sp.team LIKE '%/\(sanitize(teamCode))')"
            let pitchAggSql = """
                SELECT SUM(sp.wins), SUM(sp.losses), SUM(sp.saves),
                       SUM(sp.ip_outs), SUM(sp.hits), SUM(sp.earned_runs),
                       SUM(sp.walks), SUM(sp.strikeouts), SUM(sp.home_runs)
                FROM season_pitching_stats sp
                WHERE \(pitchingTeamFilterAgg) AND sp.season = \(year)
                """
            var pitchingStatsGrid: StatGridParser.StatGrid? = nil
            if let pitchAggResult = try? db.execute(sql: pitchAggSql),
               let pitchAggRow = pitchAggResult.rows.first,
               pitchAggRow.count >= 9,
               let ipOutsVal = Double(pitchAggRow[3]), ipOutsVal > 0 {
                let pSV = pitchAggRow[2]
                let pH = pitchAggRow[4], pER = pitchAggRow[5]
                let pBB = pitchAggRow[6], pSO = pitchAggRow[7], pHR = pitchAggRow[8]

                // Compute ERA: (ER * 9) / (ip_outs / 3)
                let erVal = Double(pER) ?? 0
                let era = (erVal * 9.0) / (ipOutsVal / 3.0)

                // Compute WHIP: (H + BB) / (ip_outs / 3)
                let hitsVal = Double(pH) ?? 0
                let walksVal = Double(pBB) ?? 0
                let whip = (hitsVal + walksVal) / (ipOutsVal / 3.0)

                let pitchHeaders = ["SV", "H", "ER", "HR", "BB", "SO", "ERA", "WHIP"]
                let pitchValues = [pSV, pH, pER, pHR, pBB, pSO,
                                   formatPitchingRate(String(era), decimals: 2),
                                   formatPitchingRate(String(whip), decimals: 2)]
                pitchingStatsGrid = StatGridParser.StatGrid(
                    headers: pitchHeaders,
                    rows: [StatGridParser.StatGrid.Row(label: "", values: pitchValues)]
                )
            }

            // 2b. Leaders (top 3 per category)
            let leaderCategories: [(label: String, col: String, isRate: Bool)] = [
                ("HR", "home_runs", false),
                ("SB", "stolen_bases", false),
                ("H", "hits", false),
                ("AVG", "batting_avg", true),
                ("OBP", "obp", true),
                ("OPS", "ops", true),
            ]
            var leaders: [StatLeader] = []
            for cat in leaderCategories {
                let leaderSql = """
                    SELECT p.name, s.\(cat.col)
                    FROM season_batting_stats s
                    JOIN players p ON s.player_id = p.player_id
                    WHERE \(teamFilter) AND s.season = \(year) AND s.plate_appearances >= 50
                    ORDER BY s.\(cat.col) DESC
                    LIMIT 20
                    """
                if let leaderResult = try? db.execute(sql: leaderSql) {
                    for lRow in leaderResult.rows {
                        let value = cat.isRate ? formatRate(lRow[1]) : lRow[1]
                        leaders.append(StatLeader(category: cat.label, name: lRow[0], value: value))
                    }
                }
            }

            // 2b-2. Pitching leaders
            let pitchingTeamFilter = "(sp.team = '\(sanitize(teamCode))' OR sp.team LIKE '\(sanitize(teamCode))/%' OR sp.team LIKE '%/\(sanitize(teamCode))')"
            let pitchingLeaderCategories: [(label: String, col: String, asc: Bool, minIP: Bool)] = [
                ("W", "wins", false, false),
                ("SV", "saves", false, false),
                ("SO", "strikeouts", false, false),
                ("ERA", "era", true, true),
            ]
            for cat in pitchingLeaderCategories {
                let ipFilter = cat.minIP ? "AND sp.ip_outs >= 54" : ""
                let sortDir = cat.asc ? "ASC" : "DESC"
                let pitchLeaderSql = """
                    SELECT p.name, sp.\(cat.col)
                    FROM season_pitching_stats sp
                    JOIN players p ON sp.player_id = p.player_id
                    WHERE \(pitchingTeamFilter) AND sp.season = \(year) \(ipFilter)
                    ORDER BY sp.\(cat.col) \(sortDir)
                    LIMIT 20
                    """
                if let leaderResult = try? db.execute(sql: pitchLeaderSql) {
                    for lRow in leaderResult.rows {
                        let value = cat.asc ? formatPitchingRate(lRow[1], decimals: 2) : lRow[1]
                        leaders.append(StatLeader(category: cat.label, name: lRow[0], value: value))
                    }
                }
            }

            // 2c. Roster (name + position only)
            let rosterSql = """
                SELECT p.name, p.positions, s.plate_appearances
                FROM season_batting_stats s
                JOIN players p ON s.player_id = p.player_id
                WHERE \(teamFilter) AND s.season = \(year)
                ORDER BY s.plate_appearances DESC
                """
            var positionPlayers: [(name: String, position: String)] = []
            var pitchers: [(name: String, position: String)] = []
            if let rosterResult = try? db.execute(sql: rosterSql) {
                for rRow in rosterResult.rows {
                    let playerName = rRow[0]
                    var pos = rRow[1]
                    let pa = Int(rRow[2]) ?? 0
                    if pos.isEmpty {
                        let posSql = """
                            SELECT sfs.position FROM season_fielding_stats sfs
                            JOIN players p ON sfs.player_id = p.player_id
                            WHERE p.name = '\(sanitize(playerName))' AND sfs.season = \(year)
                            ORDER BY sfs.games DESC LIMIT 1
                            """
                        if let posResult = try? db.execute(sql: posSql),
                           let posRow = posResult.rows.first {
                            pos = posRow[0]
                        }
                    }
                    if pa == 0 && pos.hasPrefix("P") {
                        pitchers.append((name: playerName, position: pos))
                    } else {
                        positionPlayers.append((name: playerName, position: pos))
                    }
                }
            }

            // Sort pitchers by GS DESC, then G DESC
            if !pitchers.isEmpty {
                // Build lookup of GS and G from pitching stats
                let pitcherNameList = pitchers.map { "'\(sanitize($0.name))'" }.joined(separator: ",")
                let pitchSortSql = """
                    SELECT p.name, sp.games_started, sp.games
                    FROM season_pitching_stats sp
                    JOIN players p ON sp.player_id = p.player_id
                    WHERE \(pitchingTeamFilter) AND sp.season = \(year)
                          AND p.name IN (\(pitcherNameList))
                    """
                var pitcherSort: [String: (gs: Int, g: Int)] = [:]
                if let pitchResult = try? db.execute(sql: pitchSortSql) {
                    for pRow in pitchResult.rows {
                        let gs = Int(pRow[1]) ?? 0
                        let g = Int(pRow[2]) ?? 0
                        pitcherSort[pRow[0]] = (gs: gs, g: g)
                    }
                }
                pitchers.sort { a, b in
                    let aStats = pitcherSort[a.name] ?? (gs: 0, g: 0)
                    let bStats = pitcherSort[b.name] ?? (gs: 0, g: 0)
                    if aStats.gs != bStats.gs { return aStats.gs > bStats.gs }
                    return aStats.g > bStats.g
                }
            }

            let rosterEntries = positionPlayers + pitchers
            let roster = rosterEntries.map { RosterEntry(name: $0.name, position: $0.position) }
            teamSeasons.append(TeamSeasonData(
                year: year, stats: statsGrid, pitchingStats: pitchingStatsGrid,
                leaders: leaders, roster: roster
            ))
        }

        guard !teamSeasons.isEmpty else { return nil }
        return TeamCard(teamCode: teamCode, fullName: fullName, seasons: teamSeasons)
    }

    // MARK: - Pitching Season Resolution

    /// Resolve a season for a pitcher — if the requested season has no data, fall back to their most recent.
    private static func resolvePitchingSeason(name: String, requested: Int) -> Int {
        let sql = """
            SELECT sp.season FROM season_pitching_stats sp
            JOIN players p ON sp.player_id = p.player_id
            WHERE p.name = '\(sanitize(name))'
            ORDER BY sp.season DESC LIMIT 1
            """
        guard let result = try? db.execute(sql: sql),
              let row = result.rows.first,
              let latest = Int(row[0]) else { return requested }

        // Check if requested season exists
        let checkSql = """
            SELECT 1 FROM season_pitching_stats sp
            JOIN players p ON sp.player_id = p.player_id
            WHERE p.name = '\(sanitize(name))' AND sp.season = \(requested)
            LIMIT 1
            """
        if let checkResult = try? db.execute(sql: checkSql), checkResult.rows.isEmpty {
            return latest
        }
        return requested
    }

    // MARK: - Pitching Helpers

    private static func formatPitchingRate(_ value: String, decimals: Int = 2) -> String {
        guard let num = Double(value) else { return value }
        return String(format: "%.\(decimals)f", num)
    }

    private static func formatPitchingValues(headers: [String], values: [String]) -> [String] {
        let twoDecStats: Set<String> = ["ERA", "WHIP", "K/BB"]
        let oneDecStats: Set<String> = ["K/9", "BB/9", "H/9", "HR/9"]
        let threeDecStats: Set<String> = ["BAA"]
        var formatted: [String] = []
        for (idx, value) in values.enumerated() {
            if idx < headers.count {
                let h = headers[idx]
                if twoDecStats.contains(h) {
                    formatted.append(formatPitchingRate(value, decimals: 2))
                } else if oneDecStats.contains(h) {
                    formatted.append(formatPitchingRate(value, decimals: 1))
                } else if threeDecStats.contains(h) {
                    formatted.append(formatRate(value))
                } else {
                    formatted.append(value)
                }
            } else {
                formatted.append(value)
            }
        }
        return formatted
    }

    // MARK: - Pitching All Seasons

    private static func fetchPitchingAllSeasons(name: String) -> [PitchingSeasonData] {
        let sql = """
            SELECT sp.season, sp.team,
                   sp.wins, sp.losses, sp.saves, sp.games, sp.games_started, sp.games_finished,
                   sp.complete_games, sp.quality_starts, sp.innings_pitched,
                   sp.hits, sp.runs, sp.earned_runs, sp.home_runs, sp.walks, sp.intentional_walks,
                   sp.strikeouts, sp.hit_by_pitch, sp.wild_pitches, sp.balks,
                   sp.batters_faced, sp.sacrifice_hits, sp.sacrifice_flies,
                   sp.stolen_bases, sp.caught_stealing,
                   sp.era, sp.whip, sp.k_per_9, sp.bb_per_9, sp.k_per_bb,
                   sp.h_per_9, sp.hr_per_9, sp.baa, sp.era_plus
            FROM season_pitching_stats sp
            JOIN players p ON sp.player_id = p.player_id
            WHERE p.name = '\(sanitize(name))'
            ORDER BY sp.season DESC
            """
        guard let result = try? db.execute(sql: sql) else { return [] }

        let countingKeys = ["W", "L", "SV", "G", "GS", "GF", "CG", "QS", "IP",
                            "H", "R", "ER", "HR", "BB", "IBB", "SO", "HBP", "WP", "BK",
                            "BF", "SH", "SF", "SB", "CS"]

        var seasons: [PitchingSeasonData] = []
        for row in result.rows {
            guard let year = Int(row[0]) else { continue }
            let team = row[1]
            let games = Int(row[5]) ?? 0
            let gamesStarted = Int(row[6]) ?? 0

            // Columns 2-34 map to pitchingAllHeaders (33 stats)
            let values = Array(row[2...34])
            var formatted = formatPitchingValues(headers: pitchingAllHeaders, values: values)

            // Replace ERA/WHIP/ERA+ with "--" for seasons with missing earned runs data
            // (Retrosheet gap: some pre-1912 seasons have earned_runs=0 because ERA wasn't tracked)
            let earnedRuns = Int(row[13]) ?? 0  // row[13] = earned_runs (index 2 + 11)
            if earnedRuns == 0 && year < 1912 && games > 0 {
                let eraRelatedHeaders: Set<String> = ["ERA", "WHIP", "ERA+"]
                for (i, header) in pitchingAllHeaders.enumerated() where eraRelatedHeaders.contains(header) {
                    if i < formatted.count { formatted[i] = "--" }
                }
            }

            let displayValues = filterPitchingForDisplay(formatted)
            let grid = StatGridParser.StatGrid(
                headers: pitchingHeaders,
                rows: [StatGridParser.StatGrid.Row(label: "", values: displayValues)]
            )

            // Build counting values dict
            var counting: [String: Double] = [:]
            for (i, key) in countingKeys.enumerated() {
                if key == "IP" {
                    // Use ip_outs for counting, not formatted IP
                    let ipOutsSql = """
                        SELECT sp.ip_outs FROM season_pitching_stats sp
                        JOIN players p ON sp.player_id = p.player_id
                        WHERE p.name = '\(sanitize(name))' AND sp.season = \(year)
                        LIMIT 1
                        """
                    if let ipResult = try? db.execute(sql: ipOutsSql),
                       let ipRow = ipResult.rows.first {
                        counting["IP_OUTS"] = Double(ipRow[0]) ?? 0
                    }
                    counting[key] = Double(row[2 + i]) ?? 0
                } else {
                    counting[key] = Double(row[2 + i]) ?? 0
                }
            }

            let teamGames = fetchTeamGames(team: team, season: year)
            let splits = fetchPitchingPlatoonSplitsForSeason(name: name, season: year)
            let homeAwaySplits = fetchPitchingHomeAwaySplitsForSeason(name: name, season: year)
            let rispSplits = fetchRISPPitchingSplitsForSeason(name: name, season: year)
            let streakGrid = fetchPitchingStreaksForSeason(name: name, season: year, performance: "hot")
            let pitchTypeGrids = fetchPitchTypePitchingSplitsForSeason(name: name, season: year)
            let countGrids = fetchCountPitchingSplitsForSeason(name: name, season: year)
            let currentForm = fetchPitchingCurrentFormForSeason(name: name, season: year)

            seasons.append(PitchingSeasonData(
                year: year, team: team, games: games, gamesStarted: gamesStarted,
                teamGames: teamGames, stats: grid, countingValues: counting,
                platoonSplits: splits, homeAwaySplits: homeAwaySplits, rispSplits: rispSplits,
                streaks: streakGrid, pitchTypeSplits: pitchTypeGrids, countSplits: countGrids,
                currentForm: currentForm
            ))
        }

        return seasons
    }

    // MARK: - Pitching Career Totals

    private static func fetchPitchingCareerTotals(name: String) -> StatGridParser.StatGrid? {
        let sql = """
            SELECT COUNT(DISTINCT sp.season),
                   SUM(sp.wins), SUM(sp.losses), SUM(sp.saves),
                   SUM(sp.games), SUM(sp.games_started), SUM(sp.games_finished),
                   SUM(sp.complete_games), SUM(sp.quality_starts),
                   CAST(SUM(sp.ip_outs) / 3 AS TEXT) || '.' || CAST(SUM(sp.ip_outs) % 3 AS TEXT),
                   SUM(sp.hits), SUM(sp.runs), SUM(sp.earned_runs), SUM(sp.home_runs),
                   SUM(sp.walks), SUM(sp.intentional_walks),
                   SUM(sp.strikeouts), SUM(sp.hit_by_pitch), SUM(sp.wild_pitches), SUM(sp.balks),
                   SUM(sp.batters_faced), SUM(sp.sacrifice_hits), SUM(sp.sacrifice_flies),
                   SUM(sp.stolen_bases), SUM(sp.caught_stealing),
                   ROUND(9.0 * CAST(SUM(sp.earned_runs) AS REAL) / NULLIF(SUM(sp.ip_outs) / 3.0, 0), 2),
                   ROUND(CAST(SUM(sp.walks) + SUM(sp.hits) AS REAL) / NULLIF(SUM(sp.ip_outs) / 3.0, 0), 2),
                   ROUND(9.0 * CAST(SUM(sp.strikeouts) AS REAL) / NULLIF(SUM(sp.ip_outs) / 3.0, 0), 1),
                   ROUND(9.0 * CAST(SUM(sp.walks) AS REAL) / NULLIF(SUM(sp.ip_outs) / 3.0, 0), 1),
                   ROUND(CAST(SUM(sp.strikeouts) AS REAL) / NULLIF(SUM(sp.walks), 0), 2),
                   ROUND(9.0 * CAST(SUM(sp.hits) AS REAL) / NULLIF(SUM(sp.ip_outs) / 3.0, 0), 1),
                   ROUND(9.0 * CAST(SUM(sp.home_runs) AS REAL) / NULLIF(SUM(sp.ip_outs) / 3.0, 0), 1),
                   ROUND(CAST(SUM(sp.hits) AS REAL) / NULLIF(SUM(sp.batters_faced) - SUM(sp.walks) - SUM(sp.hit_by_pitch) - SUM(sp.sacrifice_hits) - SUM(sp.sacrifice_flies), 0), 3)
            FROM season_pitching_stats sp
            JOIN players p ON sp.player_id = p.player_id
            WHERE p.name = '\(sanitize(name))'
            HAVING COUNT(DISTINCT sp.season) > 1
            """
        guard let result = try? db.execute(sql: sql),
              let row = result.rows.first else { return nil }

        let seasons = row[0]
        // row[1..24] = counting stats, row[25..32] = rate stats (no ERA+)
        let values = Array(row.dropFirst())
        var formatted = formatPitchingValues(headers: pitchingAllHeaders.filter { $0 != "ERA+" }, values: values)

        // Append ERA+ as "--" for career
        formatted.append("--")
        let displayValues = filterPitchingForDisplay(formatted)

        return StatGridParser.StatGrid(
            headers: pitchingHeaders,
            rows: [StatGridParser.StatGrid.Row(label: "\(seasons) Seasons", values: displayValues)]
        )
    }

    // MARK: - Per-season pitching platoon splits

    private static func fetchPitchingPlatoonSplitsForSeason(name: String, season: Int) -> StatGridParser.StatGrid? {
        let sql = """
            SELECT pps.split, pps.at_bats, pps.hits,
                   pps.doubles, pps.triples, pps.home_runs,
                   pps.walks, pps.strikeouts,
                   pps.batting_avg_against, pps.obp_against, pps.slg_against, pps.ops_against
            FROM pitching_platoon_splits pps
            JOIN players p ON pps.player_id = p.player_id
            WHERE p.name = '\(sanitize(name))' AND pps.season = \(season)
            ORDER BY pps.split
            """
        guard let result = try? db.execute(sql: sql),
              !result.rows.isEmpty else { return nil }

        let headers = ["AB", "H", "2B", "3B", "HR", "BB", "SO", "AVG", "OBP", "SLG", "OPS"]
        var rows: [StatGridParser.StatGrid.Row] = []
        for row in result.rows.prefix(2) {
            let splitLabel = row[0] == "vs_LHB" ? "vs LHB" : "vs RHB"
            let values = formatValues(headers: headers, values: Array(row.dropFirst()))
            rows.append(StatGridParser.StatGrid.Row(label: splitLabel, values: values))
        }

        guard !rows.isEmpty else { return nil }
        return StatGridParser.StatGrid(headers: headers, rows: rows)
    }

    // MARK: - Per-season pitching home/away splits

    private static func fetchPitchingHomeAwaySplitsForSeason(name: String, season: Int) -> StatGridParser.StatGrid? {
        let sql = """
            SELECT phas.split, phas.games, phas.games_started, phas.innings_pitched,
                   phas.hits, phas.earned_runs, phas.home_runs, phas.walks, phas.strikeouts,
                   phas.era, phas.whip, phas.k_per_9, phas.bb_per_9, phas.baa
            FROM pitching_home_away_splits phas
            JOIN players p ON phas.player_id = p.player_id
            WHERE p.name = '\(sanitize(name))' AND phas.season = \(season)
            ORDER BY phas.split DESC
            """
        guard let result = try? db.execute(sql: sql),
              !result.rows.isEmpty else { return nil }

        let headers = ["G", "GS", "IP", "H", "ER", "HR", "BB", "SO", "ERA", "WHIP", "K/9", "BB/9", "BAA"]
        var rows: [StatGridParser.StatGrid.Row] = []
        for row in result.rows.prefix(2) {
            let splitLabel = row[0] == "home" ? "Home" : "Away"
            let values = formatPitchingValues(headers: headers, values: Array(row.dropFirst()))
            rows.append(StatGridParser.StatGrid.Row(label: splitLabel, values: values))
        }

        guard !rows.isEmpty else { return nil }
        return StatGridParser.StatGrid(headers: headers, rows: rows)
    }

    // MARK: - Per-season RISP pitching splits

    private static func fetchRISPPitchingSplitsForSeason(name: String, season: Int) -> StatGridParser.StatGrid? {
        let sql = """
            SELECT rps.split, rps.at_bats, rps.hits,
                   rps.doubles, rps.triples, rps.home_runs,
                   rps.walks, rps.strikeouts,
                   rps.batting_avg_against, rps.obp_against, rps.slg_against, rps.ops_against
            FROM risp_pitching_splits rps
            JOIN players p ON rps.player_id = p.player_id
            WHERE p.name = '\(sanitize(name))' AND rps.season = \(season)
            ORDER BY rps.split DESC
            """
        guard let result = try? db.execute(sql: sql),
              !result.rows.isEmpty else { return nil }

        let headers = ["AB", "H", "2B", "3B", "HR", "BB", "SO", "AVG", "OBP", "SLG", "OPS"]
        var rows: [StatGridParser.StatGrid.Row] = []
        for row in result.rows.prefix(2) {
            let splitLabel = row[0] == "RISP" ? "RISP" : "Non-RISP"
            let values = formatValues(headers: headers, values: Array(row.dropFirst()))
            rows.append(StatGridParser.StatGrid.Row(label: splitLabel, values: values))
        }

        guard !rows.isEmpty else { return nil }
        return StatGridParser.StatGrid(headers: headers, rows: rows)
    }

    // MARK: - Per-season pitching streaks

    static func fetchPitchingStreaksForSeason(name: String, season: Int, performance: String = "hot") -> StatGridParser.StatGrid? {
        let orderDir = performance == "cold" ? "DESC" : "ASC"  // Lower ERA = hotter for pitchers
        var sql = """
            SELECT ps.start_date, ps.end_date, ps.num_games,
                   ps.innings_pitched, ps.hits, ps.earned_runs, ps.walks, ps.strikeouts,
                   ps.home_runs, ps.era, ps.whip, ps.k_per_9
            FROM pitching_streaks ps
            JOIN players p ON ps.player_id = p.player_id
            WHERE p.name = '\(sanitize(name))' AND ps.season = \(season) AND ps.performance = '\(performance)'
            ORDER BY ps.era \(orderDir)
            """
        var result = try? db.execute(sql: sql)

        // Fall back to sensitive streaks if no rows
        if result == nil || result!.rows.isEmpty {
            sql = """
                SELECT pss.start_date, pss.end_date, pss.num_games,
                       pss.innings_pitched, pss.hits, pss.earned_runs, pss.walks, pss.strikeouts,
                       pss.home_runs, pss.era, pss.whip, pss.k_per_9
                FROM pitching_streaks_sensitive pss
                JOIN players p ON pss.player_id = p.player_id
                WHERE p.name = '\(sanitize(name))' AND pss.season = \(season) AND pss.performance = '\(performance)'
                ORDER BY pss.era \(orderDir)
                """
            result = try? db.execute(sql: sql)
        }

        // Tier 3: sliding window fallback
        if result == nil || result!.rows.isEmpty {
            sql = """
                SELECT psl.start_date, psl.end_date, psl.num_games,
                       psl.innings_pitched, psl.hits, psl.earned_runs, psl.walks, psl.strikeouts,
                       psl.home_runs, psl.era, psl.whip, psl.k_per_9
                FROM pitching_streaks_sliding psl
                JOIN players p ON psl.player_id = p.player_id
                WHERE p.name = '\(sanitize(name))' AND psl.season = \(season) AND psl.performance = '\(performance)'
                ORDER BY psl.era \(orderDir)
                """
            result = try? db.execute(sql: sql)
        }

        guard let result, !result.rows.isEmpty else { return nil }

        let headers = ["G", "IP", "H", "ER", "BB", "SO", "HR", "ERA", "WHIP", "K/9"]

        // Look up league average ERA for context notes on cold streaks
        var leagueEra: Double?
        if performance == "cold" {
            let leagueSql = "SELECT league_era FROM league_pitching_averages WHERE season = \(season)"
            if let leagueResult = try? db.execute(sql: leagueSql),
               let leagueRow = leagueResult.rows.first {
                leagueEra = Double(leagueRow[0])
            }
        }

        var rows: [StatGridParser.StatGrid.Row] = []
        for row in result.rows.prefix(4) {
            let startDate = formatDate(row[0])
            let endDate = formatDate(row[1])
            let label = "\(startDate) \u{2013} \(endDate)"
            let games = row[2]
            let ip = row[3]
            let h = row[4]
            let er = row[5]
            let bb = row[6]
            let so = row[7]
            let hr = row[8]
            let era = formatPitchingRate(row[9], decimals: 2)
            let whip = formatPitchingRate(row[10], decimals: 2)
            let k9 = formatPitchingRate(row[11], decimals: 1)

            var note: String?
            if performance == "cold", let lgEra = leagueEra, let streakEra = Double(row[9]), streakEra < lgEra {
                note = "This \"cold\" streak was still below the \(season) league average ERA of \(formatPitchingRate(String(lgEra), decimals: 2))"
            }

            rows.append(StatGridParser.StatGrid.Row(
                label: label,
                values: [games, ip, h, er, bb, so, hr, era, whip, k9],
                note: note
            ))
        }

        guard !rows.isEmpty else { return nil }
        return StatGridParser.StatGrid(headers: headers, rows: rows)
    }

    // MARK: - Pitching current form

    static func fetchPitchingCurrentFormForSeason(name: String, season: Int) -> PitchingCurrentFormData? {
        let sql = """
            SELECT pcf.form_start_date, pcf.form_start_game_number, pcf.total_season_games, pcf.num_games,
                   pcf.role, pcf.innings_pitched, pcf.hits, pcf.earned_runs, pcf.home_runs,
                   pcf.walks, pcf.strikeouts, pcf.batters_faced,
                   pcf.era, pcf.whip, pcf.k_per_9, pcf.bb_per_9,
                   pcf.season_ip_outs, pcf.season_hits, pcf.season_earned_runs,
                   pcf.season_home_runs, pcf.season_walks, pcf.season_strikeouts,
                   pcf.season_batters_faced, pcf.season_era, pcf.ip_outs
            FROM pitching_current_form pcf
            JOIN players p ON pcf.player_id = p.player_id
            WHERE p.name = '\(sanitize(name))' AND pcf.season = \(season)
            LIMIT 1
            """
        guard let result = try? db.execute(sql: sql),
              let row = result.rows.first,
              row.count >= 24 else { return nil }

        let formStartDate = row[0]
        let formStartGameNumber = Int(row[1]) ?? 1
        let totalSeasonGames = Int(row[2]) ?? 0
        let numGames = Int(row[3]) ?? 0
        let role = row[4]
        let ip = row[5]
        let h = row[6]
        let er = row[7]
        let hr = row[8]
        let bb = row[9]
        let so = row[10]
        // bf at index 11
        let era = formatPitchingRate(row[12], decimals: 2)
        let whip = formatPitchingRate(row[13], decimals: 2)
        let k9 = formatPitchingRate(row[14], decimals: 1)
        let bb9 = formatPitchingRate(row[15], decimals: 1)

        let formHeaders = ["G", "IP", "H", "ER", "HR", "BB", "SO", "ERA", "WHIP", "K/9", "BB/9"]
        let formValues = [String(numGames), ip, h, er, hr, bb, so, era, whip, k9, bb9]
        let grid = StatGridParser.StatGrid(
            headers: formHeaders,
            rows: [StatGridParser.StatGrid.Row(label: "", values: formValues)]
        )

        let ipOuts = Double(row[24]) ?? 0
        let countingValues: [String: Double] = [
            "G": Double(numGames), "IP_OUTS": ipOuts,
            "H": Double(h) ?? 0, "ER": Double(er) ?? 0, "HR": Double(hr) ?? 0,
            "BB": Double(bb) ?? 0, "SO": Double(so) ?? 0
        ]

        let seasonCountingValues: [String: Double] = [
            "IP_OUTS": Double(row[16]) ?? 0, "H": Double(row[17]) ?? 0,
            "ER": Double(row[18]) ?? 0, "HR": Double(row[19]) ?? 0,
            "BB": Double(row[20]) ?? 0, "SO": Double(row[21]) ?? 0,
            "BF": Double(row[22]) ?? 0
        ]

        return PitchingCurrentFormData(
            formStartDate: formStartDate,
            formStartGameNumber: formStartGameNumber,
            totalSeasonGames: totalSeasonGames,
            numGames: numGames,
            role: role,
            stats: grid,
            countingValues: countingValues,
            seasonCountingValues: seasonCountingValues
        )
    }

    // MARK: - Pitching game logs

    static func fetchPitchingGameLogsForSeason(name: String, season: Int) -> [PitchingGameLog] {
        let sql = """
            SELECT g.date, g.ip_outs, g.hits, g.earned_runs, g.walks, g.strikeouts,
                   g.home_runs, g.is_start
            FROM game_pitching_logs g
            JOIN players p ON g.player_id = p.player_id
            WHERE p.name = '\(sanitize(name))' AND g.season = \(season)
            ORDER BY g.date ASC
            """
        guard let result = try? db.execute(sql: sql, maxRows: 0) else { return [] }
        return result.rows.compactMap { row -> PitchingGameLog? in
            guard row.count >= 8 else { return nil }
            return PitchingGameLog(
                date: row[0],
                ipOuts: Int(row[1]) ?? 0,
                hits: Int(row[2]) ?? 0,
                earnedRuns: Int(row[3]) ?? 0,
                walks: Int(row[4]) ?? 0,
                strikeouts: Int(row[5]) ?? 0,
                homeRuns: Int(row[6]) ?? 0,
                isStart: (Int(row[7]) ?? 0) == 1
            )
        }
    }

    // MARK: - Pitching single stat lookup (chat response builder)

    static func buildPitchingSingleStatLookup(name: String, stat: PlayerNameMatcher.StatInfo, season: Int) -> String? {
        let targetSeason = resolvePitchingSeason(name: name, requested: season)

        let sql = """
            SELECT p.name, sp.team, sp.\(stat.dbColumn)
            FROM season_pitching_stats sp
            JOIN players p ON sp.player_id = p.player_id
            WHERE p.name = '\(sanitize(name))' AND sp.season = \(targetSeason)
            LIMIT 1
            """
        guard let result = try? db.execute(sql: sql),
              let row = result.rows.first,
              row.count >= 3 else { return nil }

        let displayName = row[0]
        let team = row[1]
        let rawValue = row[2]

        let formattedValue: String
        if stat.isRate {
            formattedValue = formatPitchingRate(rawValue, decimals: 2)
        } else {
            formattedValue = rawValue
        }

        let sentence: String
        switch stat.displayAbbrev {
        case "W":
            sentence = "**\(displayName)** won **\(formattedValue)** games in \(targetSeason)."
        case "SV":
            sentence = "**\(displayName)** had **\(formattedValue)** saves in \(targetSeason)."
        case "SO":
            sentence = "**\(displayName)** struck out **\(formattedValue)** batters in \(targetSeason)."
        case "ERA":
            sentence = "**\(displayName)** posted a **\(formattedValue) ERA** in \(targetSeason)."
        case "WHIP":
            sentence = "**\(displayName)** posted a **\(formattedValue) WHIP** in \(targetSeason)."
        case "K/9":
            sentence = "**\(displayName)** posted a **\(formatPitchingRate(rawValue, decimals: 1)) K/9** in \(targetSeason)."
        default:
            if stat.isRate {
                sentence = "**\(displayName)** posted a **\(formattedValue) \(stat.displayAbbrev)** in \(targetSeason)."
            } else {
                sentence = "**\(displayName)** had **\(formattedValue) \(stat.displayAbbrev)** in \(targetSeason)."
            }
        }

        let teamDisplay = teamFullName(team)
        let statName = stat.pillName
        return "\(sentence) (\(teamDisplay))\n\n[TIP]Tap a player name for their full profile.[/TIP]\n\n[SUGGEST]\(targetSeason) \(statName) leaders[/SUGGEST]\n[SUGGEST]\(displayName) career[/SUGGEST]"
    }

    // MARK: - Pitching season summary (chat response builder)

    static func buildPitchingSeasonSummary(name: String, season: Int) -> String? {
        let targetSeason = resolvePitchingSeason(name: name, requested: season)
        let info = fetchPlayerInfo(name: name)
        let displayName = info?.name ?? name

        let sql = """
            SELECT sp.team, sp.wins, sp.losses, sp.saves, sp.games, sp.games_started,
                   sp.games_finished, sp.complete_games, sp.quality_starts, sp.innings_pitched,
                   sp.hits, sp.runs, sp.earned_runs, sp.home_runs, sp.walks, sp.intentional_walks,
                   sp.strikeouts, sp.hit_by_pitch, sp.wild_pitches, sp.balks,
                   sp.batters_faced, sp.sacrifice_hits, sp.sacrifice_flies,
                   sp.stolen_bases, sp.caught_stealing,
                   sp.era, sp.whip, sp.k_per_9, sp.bb_per_9, sp.k_per_bb,
                   sp.h_per_9, sp.hr_per_9, sp.baa, sp.era_plus
            FROM season_pitching_stats sp
            JOIN players p ON sp.player_id = p.player_id
            WHERE p.name = '\(sanitize(name))' AND sp.season = \(targetSeason)
            """
        guard let result = try? db.execute(sql: sql),
              let row = result.rows.first,
              row.count >= 34 else { return nil }

        let team = teamFullName(row[0])
        let values = Array(row[1...33])
        let formatted = formatPitchingValues(headers: pitchingAllHeaders, values: values)
        let displayValues = filterPitchingForDisplay(formatted)

        var parts: [String] = []
        parts.append("**\(displayName)** \u{2014} \(targetSeason) Season (\(team))\n")

        parts.append("[STATGRID]")
        parts.append("HEADER: " + pitchingHeaders.joined(separator: ", "))
        parts.append("ROW: " + displayValues.joined(separator: ", "))
        parts.append("[/STATGRID]")

        if isActivePlayer(name: name) {
            parts.append("\n[SUGGEST]how is \(displayName) doing lately[/SUGGEST]")
        }
        parts.append("[SUGGEST]\(displayName) career[/SUGGEST]")

        return parts.joined(separator: "\n")
    }

    // MARK: - Pitching streak list (chat response builder)

    static func buildPitchingStreakList(name: String, performance: String, season: Int?) -> String? {
        let info = fetchPlayerInfo(name: name)
        let displayName = info?.name ?? name

        let targetSeason: Int
        if let s = season {
            targetSeason = resolvePitchingSeason(name: name, requested: s)
        } else {
            let sql = """
                SELECT MAX(ps.season) FROM pitching_streaks ps
                JOIN players p ON ps.player_id = p.player_id
                WHERE p.name = '\(sanitize(name))'
                """
            guard let result = try? db.execute(sql: sql),
                  let row = result.rows.first,
                  let year = Int(row[0]) else { return nil }
            targetSeason = year
        }

        guard let grid = fetchPitchingStreaksForSeason(name: name, season: targetSeason, performance: performance),
              !grid.rows.isEmpty else {
            let label = performance == "cold" ? "cold streaks" : "hot streaks"
            return "No \(label) found for **\(displayName)** in \(targetSeason)."
        }

        let label = performance == "cold" ? "Cold Streaks" : "Hot Streaks"
        var parts: [String] = []
        parts.append("**\(displayName)** \u{2014} \(targetSeason) \(label)\n")

        parts.append("[STATGRID]")
        parts.append("HEADER: " + grid.headers.joined(separator: ", "))
        for row in grid.rows {
            parts.append("ROW \(row.label): " + row.values.joined(separator: ", "))
            if let note = row.note {
                parts.append("NOTE: \(note)")
            }
        }
        parts.append("[/STATGRID]")

        let count = grid.rows.count
        let streakWord = count == 1 ? "streak" : "streaks"
        if let topRow = grid.rows.first {
            let eraIdx = grid.headers.firstIndex(of: "ERA") ?? -1
            let eraValue = eraIdx >= 0 && eraIdx < topRow.values.count ? topRow.values[eraIdx] : ""
            let gIdx = grid.headers.firstIndex(of: "G") ?? -1
            let gValue = gIdx >= 0 && gIdx < topRow.values.count ? topRow.values[gIdx] : ""
            let adjective = performance == "cold" ? "coldest" : "hottest"
            parts.append("\n\(count) \(performance) \(streakWord) detected. The \(adjective) was \(gValue) games (\(topRow.label)) with a \(eraValue) ERA.")
        }

        let oppositePerf = performance == "hot" ? "cold" : "hot"
        parts.append("\n[SUGGEST]\(displayName) \(oppositePerf) streaks \(targetSeason)[/SUGGEST]")

        return parts.joined(separator: "\n")
    }

    // MARK: - Pitching current hot streak (chat response builder)

    static func buildPitchingCurrentHotStreak(name: String) -> String? {
        let info = fetchPlayerInfo(name: name)
        let displayName = info?.name ?? name

        let sql = """
            SELECT pcf.season, pcf.form_start_date, pcf.form_start_game_number,
                   pcf.total_season_games, pcf.num_games, pcf.role,
                   pcf.innings_pitched, pcf.hits, pcf.earned_runs,
                   pcf.walks, pcf.strikeouts, pcf.home_runs,
                   pcf.era, pcf.whip, pcf.k_per_9, pcf.bb_per_9,
                   pcf.season_era, sp.team
            FROM pitching_current_form pcf
            JOIN players p ON pcf.player_id = p.player_id
            LEFT JOIN season_pitching_stats sp ON pcf.player_id = sp.player_id AND pcf.season = sp.season
            WHERE p.name = '\(sanitize(name))'
            ORDER BY pcf.season DESC
            LIMIT 1
            """
        guard let result = try? db.execute(sql: sql),
              let row = result.rows.first,
              row.count >= 18 else { return nil }

        let season = row[0]
        let startDate = formatDate(row[1])
        let startGameNum = row[2]
        let totalGames = row[3]
        let numGames = Int(row[4]) ?? 0
        let _ = row[5]  // role
        let ip = row[6], h = row[7], er = row[8]
        let bb = row[9], so = row[10], hr = row[11]
        let era = formatPitchingRate(row[12], decimals: 2)
        let whip = formatPitchingRate(row[13], decimals: 2)
        let k9 = formatPitchingRate(row[14], decimals: 1)
        let bb9 = formatPitchingRate(row[15], decimals: 1)
        let seasonEra = formatPitchingRate(row[16], decimals: 2)
        let team = row[17]

        let teamGames = fetchTeamGames(team: team, season: Int(season) ?? 0)

        var parts: [String] = []
        parts.append("\(displayName) has been on fire over the last \(numGames) games (since \(startDate)):\n")

        parts.append("[STATGRID]")
        parts.append("HEADER: G, IP, H, ER, HR, BB, SO, ERA, WHIP, K/9, BB/9")
        parts.append("FORM: \(displayName), \(season), \(startGameNum), \(totalGames), \(teamGames)")
        parts.append("ROW: \(numGames), \(ip), \(h), \(er), \(hr), \(bb), \(so), \(era), \(whip), \(k9), \(bb9)")
        parts.append("[/STATGRID]")

        parts.append("\nThat's compared to his \(season) season ERA of \(seasonEra).")
        parts.append("\n[SUGGEST]\(displayName) hot streaks \(season)[/SUGGEST]")

        return parts.joined(separator: "\n")
    }

    // MARK: - Pitching career lookup (chat response builder)

    static func buildPitchingCareerLookup(name: String, stat: PlayerNameMatcher.StatInfo?) -> String? {
        let info = fetchPlayerInfo(name: name)
        let displayName = info?.name ?? name
        let team = info?.team ?? ""
        let teamDisplay = teamFullName(team)

        let mostRecentSeason: Int = {
            let sql = """
                SELECT MAX(sp.season) FROM season_pitching_stats sp
                JOIN players p ON sp.player_id = p.player_id
                WHERE p.name = '\(sanitize(name))'
                """
            if let r = try? db.execute(sql: sql), let row = r.rows.first, let yr = Int(row[0]) {
                return yr
            }
            return 2025
        }()

        if let stat {
            let selectExpr: String
            if stat.isRate {
                guard let formula = careerPitchingRateFormula(for: stat) else { return nil }
                selectExpr = formula
            } else {
                selectExpr = "SUM(sp.\(stat.dbColumn))"
            }

            let sql = """
                SELECT \(selectExpr), COUNT(DISTINCT sp.season)
                FROM season_pitching_stats sp
                JOIN players p ON sp.player_id = p.player_id
                WHERE p.name = '\(sanitize(name))'
                """
            guard let result = try? db.execute(sql: sql),
                  let row = result.rows.first,
                  !row[0].isEmpty else { return nil }

            let seasons = Int(row[1]) ?? 0
            if seasons <= 1 { return nil }

            let formattedValue = stat.isRate ? formatPitchingRate(row[0], decimals: 2) : row[0]

            let sentence: String
            switch stat.displayAbbrev {
            case "W":
                sentence = "**\(displayName)** has **\(formattedValue)** career wins."
            case "SV":
                sentence = "**\(displayName)** has **\(formattedValue)** career saves."
            case "SO":
                sentence = "**\(displayName)** has **\(formattedValue)** career strikeouts."
            case "ERA":
                sentence = "**\(displayName)** has a **\(formattedValue)** career ERA."
            case "WHIP":
                sentence = "**\(displayName)** has a **\(formattedValue)** career WHIP."
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
            guard let careerGrid = fetchPitchingCareerTotals(name: name) else { return nil }

            var parts: [String] = []
            parts.append("**\(displayName)** \u{2014} Career Totals (\(teamDisplay))\n")

            parts.append("[STATGRID]")
            parts.append("HEADER: " + pitchingHeaders.joined(separator: ", "))
            if let careerRow = careerGrid.rows.first {
                parts.append("ROW \(careerRow.label): " + careerRow.values.joined(separator: ", "))
            }
            parts.append("[/STATGRID]")

            parts.append("\n[SUGGEST]\(displayName) \(mostRecentSeason)[/SUGGEST]")
            parts.append("[SUGGEST]\(displayName) vs lefties[/SUGGEST]")

            return parts.joined(separator: "\n")
        }
    }

    // MARK: - Pitching leaderboard (chat response builder)

    static func buildPitchingLeaderboard(stat: PlayerNameMatcher.StatInfo, scope: PlayerNameMatcher.LeaderboardScope, limit: Int, league: String? = nil) -> String {
        switch scope {
        case .season(let season):
            return buildPitchingSeasonLeaderboard(stat: stat, season: season, limit: limit, league: league)
        case .allTimeSingleSeason:
            return buildPitchingAllTimeSingleSeasonLeaderboard(stat: stat, limit: limit, league: league)
        case .allTimeSince(let year):
            return buildPitchingAllTimeSinceLeaderboard(stat: stat, sinceYear: year, limit: limit, league: league)
        case .career:
            return buildPitchingCareerLeaderboard(stat: stat, limit: limit, league: league)
        }
    }

    private static func buildPitchingSeasonLeaderboard(stat: PlayerNameMatcher.StatInfo, season: Int, limit: Int, league: String? = nil) -> String {
        let leagueFilter = league.map { " AND \(leagueTeamClause($0, alias: "sp"))" } ?? ""
        let leagueLabel = league.map { " (\($0))" } ?? ""

        // Rate stats need an IP minimum
        let ipMin: String?
        if stat.isRate {
            let maxGamesSql = "SELECT MAX(games) FROM season_pitching_stats WHERE season = \(season)"
            let maxGames: Int
            if let r = try? db.execute(sql: maxGamesSql), let row = r.rows.first, let val = Int(row[0]) {
                maxGames = val
            } else {
                maxGames = 162
            }
            // 162 IP (486 outs) for full season, 81 IP (243 outs) for partial
            ipMin = maxGames >= 140 ? " AND sp.ip_outs >= 486" : " AND sp.ip_outs >= 243"
        } else {
            ipMin = nil
        }

        let ipFilter = ipMin ?? ""
        let orderDir = (stat.displayAbbrev == "ERA" || stat.displayAbbrev == "WHIP" || stat.displayAbbrev == "BB/9" || stat.displayAbbrev == "H/9" || stat.displayAbbrev == "HR/9" || stat.displayAbbrev == "BAA") ? "ASC" : "DESC"

        let sql = """
            SELECT p.name, sp.\(stat.dbColumn)
            FROM season_pitching_stats sp
            JOIN players p ON sp.player_id = p.player_id
            WHERE sp.season = \(season)\(ipFilter)\(leagueFilter)
            ORDER BY sp.\(stat.dbColumn) \(orderDir)
            LIMIT \(limit)
            """
        guard let result = try? db.execute(sql: sql),
              !result.rows.isEmpty else {
            return "No \(stat.displayName) leaders found for \(season)\(leagueLabel)."
        }

        var parts: [String] = []
        parts.append("**\(season) \(stat.displayName) Leaders\(leagueLabel) (Pitching)**\n")
        parts.append("[TIP]Tap a player name for their full profile.[/TIP]")

        parts.append("[LEADERBOARD]")
        parts.append("HEADER: \(stat.displayAbbrev)")
        for (i, row) in result.rows.enumerated() {
            let playerName = row[0]
            let rawValue = row[1]
            let formattedValue = stat.isRate ? formatPitchingRate(rawValue, decimals: 2) : rawValue
            parts.append("ROW \(i + 1). \(playerName): \(formattedValue)")
        }
        parts.append("[/LEADERBOARD]")

        if ipMin != nil {
            parts.append("\n_Min. qualified IP._")
        }

        let statName = stat.pillName
        parts.append("\n[SUGGEST]career \(statName) leaders[/SUGGEST]")

        if let league = league {
            let other = league == "AL" ? "NL" : "AL"
            parts.append("[SUGGEST]\(season) \(statName) leaders (\(other))[/SUGGEST]")
            parts.append("[SUGGEST]\(season) \(statName) leaders (MLB)[/SUGGEST]")
        } else {
            parts.append("[SUGGEST]\(season) \(statName) leaders (AL)[/SUGGEST]")
            parts.append("[SUGGEST]\(season) \(statName) leaders (NL)[/SUGGEST]")
        }

        return parts.joined(separator: "\n")
    }

    private static func buildPitchingAllTimeSingleSeasonLeaderboard(stat: PlayerNameMatcher.StatInfo, limit: Int, league: String? = nil) -> String {
        let leagueFilter = league.map { " AND \(leagueTeamClause($0, alias: "sp"))" } ?? ""
        let leagueLabel = league.map { " (\($0))" } ?? ""
        let ipFilter = stat.isRate ? " WHERE sp.ip_outs >= 486\(leagueFilter)" : (leagueFilter.isEmpty ? "" : " WHERE \(leagueFilter.dropFirst(5))")
        let orderDir = (stat.displayAbbrev == "ERA" || stat.displayAbbrev == "WHIP" || stat.displayAbbrev == "BB/9" || stat.displayAbbrev == "BAA") ? "ASC" : "DESC"

        let sql = """
            SELECT p.name, sp.\(stat.dbColumn), sp.season
            FROM season_pitching_stats sp
            JOIN players p ON sp.player_id = p.player_id
            \(ipFilter)
            ORDER BY sp.\(stat.dbColumn) \(orderDir)
            LIMIT \(limit)
            """
        guard let result = try? db.execute(sql: sql),
              !result.rows.isEmpty else {
            return "No all-time \(stat.displayName) leaders found\(leagueLabel)."
        }

        var parts: [String] = []
        parts.append("**All-Time Single Season \(stat.displayName) Leaders\(leagueLabel) (Pitching)**\n")
        parts.append("[TIP]Tap a player name for their full profile.[/TIP]")

        parts.append("[LEADERBOARD]")
        parts.append("HEADER: \(stat.displayAbbrev), Year")
        for (i, row) in result.rows.enumerated() {
            let playerName = row[0]
            let rawValue = row[1]
            let season = row[2]
            let formattedValue = stat.isRate ? formatPitchingRate(rawValue, decimals: 2) : rawValue
            parts.append("ROW \(i + 1). \(playerName): \(formattedValue), \(season)")
        }
        parts.append("[/LEADERBOARD]")

        if stat.isRate {
            parts.append("\n_Min. 162 IP._")
        }

        let statName = stat.pillName
        if let league = league {
            let other = league == "AL" ? "NL" : "AL"
            parts.append("\n[SUGGEST]all-time single season \(statName) leaders (\(other))[/SUGGEST]")
            parts.append("[SUGGEST]all-time single season \(statName) leaders (MLB)[/SUGGEST]")
        } else {
            parts.append("\n[SUGGEST]all-time single season \(statName) leaders (AL)[/SUGGEST]")
            parts.append("[SUGGEST]all-time single season \(statName) leaders (NL)[/SUGGEST]")
        }

        return parts.joined(separator: "\n")
    }

    private static func buildPitchingAllTimeSinceLeaderboard(stat: PlayerNameMatcher.StatInfo, sinceYear: Int, limit: Int, league: String? = nil) -> String {
        let leagueFilter = league.map { " AND \(leagueTeamClause($0, alias: "sp"))" } ?? ""
        let leagueLabel = league.map { " (\($0))" } ?? ""
        let ipFilter = stat.isRate ? " AND sp.ip_outs >= 486" : ""
        let orderDir = (stat.displayAbbrev == "ERA" || stat.displayAbbrev == "WHIP" || stat.displayAbbrev == "BB/9" || stat.displayAbbrev == "BAA") ? "ASC" : "DESC"

        let sql = """
            SELECT p.name, sp.\(stat.dbColumn), sp.season
            FROM season_pitching_stats sp
            JOIN players p ON sp.player_id = p.player_id
            WHERE sp.season >= \(sinceYear)\(ipFilter)\(leagueFilter)
            ORDER BY sp.\(stat.dbColumn) \(orderDir)
            LIMIT \(limit)
            """
        guard let result = try? db.execute(sql: sql),
              !result.rows.isEmpty else {
            return "No \(stat.displayName) leaders found since \(sinceYear)\(leagueLabel)."
        }

        var parts: [String] = []
        parts.append("**\(stat.displayName) Leaders Since \(sinceYear)\(leagueLabel) (Pitching)**\n")
        parts.append("[TIP]Tap a player name for their full profile.[/TIP]")

        parts.append("[LEADERBOARD]")
        parts.append("HEADER: \(stat.displayAbbrev), Year")
        for (i, row) in result.rows.enumerated() {
            let playerName = row[0]
            let rawValue = row[1]
            let season = row[2]
            let formattedValue = stat.isRate ? formatPitchingRate(rawValue, decimals: 2) : rawValue
            parts.append("ROW \(i + 1). \(playerName): \(formattedValue), \(season)")
        }
        parts.append("[/LEADERBOARD]")

        if stat.isRate {
            parts.append("\n_Min. 162 IP._")
        }

        let statName = stat.pillName
        if let league = league {
            let other = league == "AL" ? "NL" : "AL"
            parts.append("\n[SUGGEST]\(statName) leaders since \(sinceYear) (\(other))[/SUGGEST]")
            parts.append("[SUGGEST]\(statName) leaders since \(sinceYear) (MLB)[/SUGGEST]")
        } else {
            parts.append("\n[SUGGEST]\(statName) leaders since \(sinceYear) (AL)[/SUGGEST]")
            parts.append("[SUGGEST]\(statName) leaders since \(sinceYear) (NL)[/SUGGEST]")
        }

        return parts.joined(separator: "\n")
    }

    private static func buildPitchingCareerLeaderboard(stat: PlayerNameMatcher.StatInfo, limit: Int, league: String? = nil) -> String {
        let leagueFilter = league.map { " AND \(leagueTeamClause($0, alias: "sp"))" } ?? ""
        let leagueLabel = league.map { " (\($0))" } ?? ""

        let selectExpr: String
        if stat.isRate {
            guard let formula = careerPitchingRateFormula(for: stat) else {
                return "Career \(stat.displayName) leaders are not available."
            }
            selectExpr = "\(formula) as career_val"
        } else {
            selectExpr = "SUM(sp.\(stat.dbColumn)) as career_val"
        }

        let ipFilter = stat.isRate ? "\n            HAVING SUM(sp.ip_outs) >= 486" : ""
        let orderDir = (stat.displayAbbrev == "ERA" || stat.displayAbbrev == "WHIP" || stat.displayAbbrev == "BB/9" || stat.displayAbbrev == "BAA") ? "ASC" : "DESC"

        let sql = """
            SELECT p.name, \(selectExpr)
            FROM season_pitching_stats sp
            JOIN players p ON sp.player_id = p.player_id\(leagueFilter.isEmpty ? "" : "\n            WHERE \(leagueFilter.dropFirst(5))")
            GROUP BY p.player_id\(ipFilter)
            ORDER BY career_val \(orderDir)
            LIMIT \(limit)
            """
        guard let result = try? db.execute(sql: sql),
              !result.rows.isEmpty else {
            return "No career \(stat.displayName) leaders found\(leagueLabel)."
        }

        var parts: [String] = []
        parts.append("**Career \(stat.displayName) Leaders\(leagueLabel) (Pitching)**\n")
        parts.append("[TIP]Tap a player name for their full profile.[/TIP]")

        parts.append("[LEADERBOARD]")
        parts.append("HEADER: \(stat.displayAbbrev)")
        for (i, row) in result.rows.enumerated() {
            let playerName = row[0]
            let rawValue = row[1]
            let formattedValue = stat.isRate ? formatPitchingRate(rawValue, decimals: 2) : rawValue
            parts.append("ROW \(i + 1). \(playerName): \(formattedValue)")
        }
        parts.append("[/LEADERBOARD]")

        if stat.isRate {
            parts.append("\n_Min. 162 IP._")
        }

        let statName = stat.pillName
        if let league = league {
            let other = league == "AL" ? "NL" : "AL"
            parts.append("\n[SUGGEST]career \(statName) leaders (\(other))[/SUGGEST]")
            parts.append("[SUGGEST]career \(statName) leaders (MLB)[/SUGGEST]")
        } else {
            parts.append("\n[SUGGEST]career \(statName) leaders (AL)[/SUGGEST]")
            parts.append("[SUGGEST]career \(statName) leaders (NL)[/SUGGEST]")
        }

        return parts.joined(separator: "\n")
    }

    private static func careerPitchingRateFormula(for stat: PlayerNameMatcher.StatInfo) -> String? {
        switch stat.displayAbbrev {
        case "ERA":
            return "ROUND(9.0 * CAST(SUM(sp.earned_runs) AS REAL) / NULLIF(SUM(sp.ip_outs) / 3.0, 0), 2)"
        case "WHIP":
            return "ROUND(CAST(SUM(sp.walks) + SUM(sp.hits) AS REAL) / NULLIF(SUM(sp.ip_outs) / 3.0, 0), 2)"
        case "K/9":
            return "ROUND(9.0 * CAST(SUM(sp.strikeouts) AS REAL) / NULLIF(SUM(sp.ip_outs) / 3.0, 0), 1)"
        case "BB/9":
            return "ROUND(9.0 * CAST(SUM(sp.walks) AS REAL) / NULLIF(SUM(sp.ip_outs) / 3.0, 0), 1)"
        case "K/BB":
            return "ROUND(CAST(SUM(sp.strikeouts) AS REAL) / NULLIF(SUM(sp.walks), 0), 2)"
        case "H/9":
            return "ROUND(9.0 * CAST(SUM(sp.hits) AS REAL) / NULLIF(SUM(sp.ip_outs) / 3.0, 0), 1)"
        case "HR/9":
            return "ROUND(9.0 * CAST(SUM(sp.home_runs) AS REAL) / NULLIF(SUM(sp.ip_outs) / 3.0, 0), 1)"
        case "BAA":
            return "ROUND(CAST(SUM(sp.hits) AS REAL) / NULLIF(SUM(sp.batters_faced) - SUM(sp.walks) - SUM(sp.hit_by_pitch) - SUM(sp.sacrifice_hits) - SUM(sp.sacrifice_flies), 0), 3)"
        default:
            return nil
        }
    }

    // MARK: - Pitching comparison (chat response builder)

    static func buildPitchingComparison(player1: String, player2: String, season: Int? = nil) -> String {
        let header = "HEADER: " + pitchingHeaders.joined(separator: ", ")

        let season1 = season.flatMap({ fetchPitchingSeasonRow(name: player1, year: $0) }) ?? fetchPitchingLatestSeasonRow(name: player1)
        let season2 = season.flatMap({ fetchPitchingSeasonRow(name: player2, year: $0) }) ?? fetchPitchingLatestSeasonRow(name: player2)

        let career1 = fetchPitchingCareerRow(name: player1)
        let career2 = fetchPitchingCareerRow(name: player2)

        let info1 = fetchPlayerInfo(name: player1)
        let info2 = fetchPlayerInfo(name: player2)
        let label1 = "\(info1?.name ?? player1) (\(info1?.team ?? ""))"
        let label2 = "\(info2?.name ?? player2) (\(info2?.team ?? ""))"

        var parts: [String] = []

        if let s1 = season1, let s2 = season2 {
            let year = s1.year
            parts.append("\(year) Season:\n")
            parts.append("[STATGRID]")
            parts.append(header)
            parts.append("ROW: \(label1), \(s1.values.joined(separator: ", "))")
            parts.append("ROW: \(label2), \(s2.values.joined(separator: ", "))")
            parts.append("[/STATGRID]")
        }

        // Career grid — only when no specific season was requested
        if season == nil, let c1 = career1, let c2 = career2 {
            parts.append("\nCareer:\n")
            parts.append("[STATGRID]")
            parts.append(header)
            parts.append("ROW: \(label1), \(c1.joined(separator: ", "))")
            parts.append("ROW: \(label2), \(c2.joined(separator: ", "))")
            parts.append("[/STATGRID]")
        }

        if parts.isEmpty {
            return "I don't have enough pitching data to compare these two players."
        }

        let name1 = info1?.name ?? player1
        let name2 = info2?.name ?? player2
        parts.append("\n[SUGGEST]\(name1) vs lefties[/SUGGEST]")
        parts.append("[SUGGEST]\(name2) vs lefties[/SUGGEST]")

        return parts.joined(separator: "\n")
    }

    private static func fetchPitchingSeasonRow(name: String, year: Int) -> (year: Int, values: [String])? {
        let sql = """
            SELECT sp.season,
                   sp.wins, sp.losses, sp.saves, sp.games, sp.games_started, sp.games_finished,
                   sp.complete_games, sp.quality_starts, sp.innings_pitched,
                   sp.hits, sp.runs, sp.earned_runs, sp.home_runs, sp.walks, sp.intentional_walks,
                   sp.strikeouts, sp.hit_by_pitch, sp.wild_pitches, sp.balks,
                   sp.batters_faced, sp.sacrifice_hits, sp.sacrifice_flies,
                   sp.stolen_bases, sp.caught_stealing,
                   sp.era, sp.whip, sp.k_per_9, sp.bb_per_9, sp.k_per_bb,
                   sp.h_per_9, sp.hr_per_9, sp.baa, sp.era_plus
            FROM season_pitching_stats sp
            JOIN players p ON sp.player_id = p.player_id
            WHERE p.name = '\(sanitize(name))' AND sp.season = \(year)
            LIMIT 1
            """
        guard let result = try? db.execute(sql: sql),
              let row = result.rows.first,
              let yr = Int(row[0]) else { return nil }

        let values = Array(row[1...33])
        let formatted = formatPitchingValues(headers: pitchingAllHeaders, values: values)
        return (yr, filterPitchingForDisplay(formatted))
    }

    private static func fetchPitchingLatestSeasonRow(name: String) -> (year: Int, values: [String])? {
        let sql = """
            SELECT sp.season,
                   sp.wins, sp.losses, sp.saves, sp.games, sp.games_started, sp.games_finished,
                   sp.complete_games, sp.quality_starts, sp.innings_pitched,
                   sp.hits, sp.runs, sp.earned_runs, sp.home_runs, sp.walks, sp.intentional_walks,
                   sp.strikeouts, sp.hit_by_pitch, sp.wild_pitches, sp.balks,
                   sp.batters_faced, sp.sacrifice_hits, sp.sacrifice_flies,
                   sp.stolen_bases, sp.caught_stealing,
                   sp.era, sp.whip, sp.k_per_9, sp.bb_per_9, sp.k_per_bb,
                   sp.h_per_9, sp.hr_per_9, sp.baa, sp.era_plus
            FROM season_pitching_stats sp
            JOIN players p ON sp.player_id = p.player_id
            WHERE p.name = '\(sanitize(name))'
            ORDER BY sp.season DESC
            LIMIT 1
            """
        guard let result = try? db.execute(sql: sql),
              let row = result.rows.first,
              let year = Int(row[0]) else { return nil }

        let values = Array(row[1...33])
        let formatted = formatPitchingValues(headers: pitchingAllHeaders, values: values)
        return (year, filterPitchingForDisplay(formatted))
    }

    private static func fetchPitchingCareerRow(name: String) -> [String]? {
        let sql = """
            SELECT SUM(sp.wins), SUM(sp.losses), SUM(sp.saves),
                   SUM(sp.games), SUM(sp.games_started), SUM(sp.games_finished),
                   SUM(sp.complete_games), SUM(sp.quality_starts),
                   CAST(SUM(sp.ip_outs) / 3 AS TEXT) || '.' || CAST(SUM(sp.ip_outs) % 3 AS TEXT),
                   SUM(sp.hits), SUM(sp.runs), SUM(sp.earned_runs), SUM(sp.home_runs),
                   SUM(sp.walks), SUM(sp.intentional_walks),
                   SUM(sp.strikeouts), SUM(sp.hit_by_pitch), SUM(sp.wild_pitches), SUM(sp.balks),
                   SUM(sp.batters_faced), SUM(sp.sacrifice_hits), SUM(sp.sacrifice_flies),
                   SUM(sp.stolen_bases), SUM(sp.caught_stealing),
                   ROUND(9.0 * CAST(SUM(sp.earned_runs) AS REAL) / NULLIF(SUM(sp.ip_outs) / 3.0, 0), 2),
                   ROUND(CAST(SUM(sp.walks) + SUM(sp.hits) AS REAL) / NULLIF(SUM(sp.ip_outs) / 3.0, 0), 2),
                   ROUND(9.0 * CAST(SUM(sp.strikeouts) AS REAL) / NULLIF(SUM(sp.ip_outs) / 3.0, 0), 1),
                   ROUND(9.0 * CAST(SUM(sp.walks) AS REAL) / NULLIF(SUM(sp.ip_outs) / 3.0, 0), 1),
                   ROUND(CAST(SUM(sp.strikeouts) AS REAL) / NULLIF(SUM(sp.walks), 0), 2),
                   ROUND(9.0 * CAST(SUM(sp.hits) AS REAL) / NULLIF(SUM(sp.ip_outs) / 3.0, 0), 1),
                   ROUND(9.0 * CAST(SUM(sp.home_runs) AS REAL) / NULLIF(SUM(sp.ip_outs) / 3.0, 0), 1),
                   ROUND(CAST(SUM(sp.hits) AS REAL) / NULLIF(SUM(sp.batters_faced) - SUM(sp.walks) - SUM(sp.hit_by_pitch) - SUM(sp.sacrifice_hits) - SUM(sp.sacrifice_flies), 0), 3)
            FROM season_pitching_stats sp
            JOIN players p ON sp.player_id = p.player_id
            WHERE p.name = '\(sanitize(name))'
            HAVING COUNT(DISTINCT sp.season) > 1
            """
        guard let result = try? db.execute(sql: sql),
              let row = result.rows.first else { return nil }

        // row has 32 values (no ERA+); format them
        var formatted = formatPitchingValues(headers: pitchingAllHeaders.filter { $0 != "ERA+" }, values: row)
        // Append ERA+ as "--" for career
        formatted.append("--")
        return filterPitchingForDisplay(formatted)
    }

    // MARK: - Pitching platoon splits (chat response builder)

    static func buildPitchingPlatoonSplits(name: String, hand: String?, season: Int) -> String? {
        let targetSeason = resolvePitchingSeason(name: name, requested: season)
        let info = fetchPlayerInfo(name: name)
        let displayName = info?.name ?? name

        var splitFilter = ""
        if let hand {
            let splitValue = hand == "LHB" ? "vs_LHB" : "vs_RHB"
            splitFilter = " AND pps.split = '\(splitValue)'"
        }

        let sql = """
            SELECT pps.split, pps.at_bats, pps.hits,
                   pps.doubles, pps.triples, pps.home_runs,
                   pps.walks, pps.strikeouts,
                   pps.batting_avg_against, pps.obp_against, pps.slg_against, pps.ops_against
            FROM pitching_platoon_splits pps
            JOIN players p ON pps.player_id = p.player_id
            WHERE p.name = '\(sanitize(name))' AND pps.season = \(targetSeason)\(splitFilter)
            ORDER BY pps.split
            """
        guard let result = try? db.execute(sql: sql),
              !result.rows.isEmpty else { return nil }

        let headers = ["AB", "H", "2B", "3B", "HR", "BB", "SO", "AVG", "OBP", "SLG", "OPS"]

        let subtitle: String
        if let hand {
            subtitle = hand == "LHB" ? "vs Left-Handed Batters" : "vs Right-Handed Batters"
        } else {
            subtitle = "Platoon Splits"
        }

        var parts: [String] = []
        parts.append("**\(displayName)** \u{2014} \(targetSeason) \(subtitle)\n")

        parts.append("[STATGRID]")
        parts.append("HEADER: " + headers.joined(separator: ", "))
        for row in result.rows.prefix(2) {
            let splitLabel = row[0] == "vs_LHB" ? "vs LHB" : "vs RHB"
            let values = formatValues(headers: headers, values: Array(row.dropFirst()))
            parts.append("ROW \(splitLabel): " + values.joined(separator: ", "))
        }
        parts.append("[/STATGRID]")

        parts.append("\n[TIP]Tap a player name for their full profile.[/TIP]")
        parts.append("\n[SUGGEST]\(displayName) \(targetSeason)[/SUGGEST]")

        return parts.joined(separator: "\n")
    }

    // MARK: - Pitching threshold leaderboard (chat response builder)

    static func buildPitchingThresholdLeaderboard(stat: PlayerNameMatcher.StatInfo, threshold: Double, comparison: String, season: Int, league: String? = nil) -> String {
        let leagueFilter = league.map { " AND \(leagueTeamClause($0, alias: "sp"))" } ?? ""
        let leagueLabel = league.map { " (\($0))" } ?? ""

        let ipMin: String?
        if stat.isRate {
            let maxGamesSql = "SELECT MAX(games) FROM season_pitching_stats WHERE season = \(season)"
            let maxGames: Int
            if let r = try? db.execute(sql: maxGamesSql), let row = r.rows.first, let val = Int(row[0]) {
                maxGames = val
            } else {
                maxGames = 162
            }
            ipMin = maxGames >= 140 ? " AND sp.ip_outs >= 486" : " AND sp.ip_outs >= 243"
        } else {
            ipMin = nil
        }

        let ipFilter = ipMin ?? ""
        let orderDir = (stat.displayAbbrev == "ERA" || stat.displayAbbrev == "WHIP" || stat.displayAbbrev == "BAA") ? "ASC" : "DESC"

        let sql = """
            SELECT p.name, sp.\(stat.dbColumn)
            FROM season_pitching_stats sp
            JOIN players p ON sp.player_id = p.player_id
            WHERE sp.season = \(season) AND sp.\(stat.dbColumn) \(comparison) \(threshold)\(ipFilter)\(leagueFilter)
            ORDER BY sp.\(stat.dbColumn) \(orderDir)
            LIMIT 50
            """
        guard let result = try? db.execute(sql: sql),
              !result.rows.isEmpty else {
            let thresholdStr = stat.isRate ? formatPitchingRate(String(threshold), decimals: 2) : String(Int(threshold))
            let op = comparison == ">=" ? "at least" : "no more than"
            return "No pitchers had \(op) \(thresholdStr) \(stat.displayAbbrev) in \(season)\(leagueLabel)."
        }

        let thresholdDisplay: String
        if stat.isRate {
            thresholdDisplay = formatPitchingRate(String(threshold), decimals: 2)
        } else {
            thresholdDisplay = String(Int(threshold))
        }

        let title: String
        if comparison == "<=" {
            if stat.isRate {
                title = "Pitchers with \(thresholdDisplay) or Better \(stat.displayAbbrev) in \(season)\(leagueLabel)"
            } else {
                title = "Pitchers with \(thresholdDisplay) or Fewer \(stat.displayName) in \(season)\(leagueLabel)"
            }
        } else {
            if stat.isRate {
                title = "Pitchers with \(stat.displayAbbrev) Over \(thresholdDisplay) in \(season)\(leagueLabel)"
            } else {
                title = "Pitchers with \(thresholdDisplay)+ \(stat.displayName) in \(season)\(leagueLabel)"
            }
        }

        var parts: [String] = []
        parts.append("**\(title)**\n")
        parts.append("[TIP]Tap a player name for their full profile.[/TIP]")

        parts.append("[LEADERBOARD]")
        parts.append("HEADER: \(stat.displayAbbrev)")
        for (i, row) in result.rows.enumerated() {
            let playerName = row[0]
            let rawValue = row[1]
            let formattedValue = stat.isRate ? formatPitchingRate(rawValue, decimals: 2) : rawValue
            parts.append("ROW \(i + 1). \(playerName): \(formattedValue)")
        }
        parts.append("[/LEADERBOARD]")

        let count = result.rows.count
        parts.append("\n\(count) pitcher\(count == 1 ? "" : "s") matched.")

        if ipMin != nil {
            parts.append("_Min. qualified IP._")
        }

        let statName = stat.pillName
        parts.append("\n[SUGGEST]\(season) \(statName) leaders[/SUGGEST]")

        if let league = league {
            let other = league == "AL" ? "NL" : "AL"
            parts.append("[SUGGEST]\(season) \(statName) leaders (\(other))[/SUGGEST]")
            parts.append("[SUGGEST]\(season) \(statName) leaders (MLB)[/SUGGEST]")
        } else {
            parts.append("[SUGGEST]\(season) \(statName) leaders (AL)[/SUGGEST]")
            parts.append("[SUGGEST]\(season) \(statName) leaders (NL)[/SUGGEST]")
        }

        return parts.joined(separator: "\n")
    }

    // MARK: - Pitching team stats (chat response builder)

    static func buildPitchingTeamStats(teamCode: String, stat: PlayerNameMatcher.StatInfo?, season: Int) -> String {
        let fullName = teamFullName(teamCode)
        let nickname = teamNickname(teamCode)

        if let stat {
            let ipMin: String?
            if stat.isRate {
                ipMin = " AND sp.ip_outs >= 54"  // ~18 IP minimum for team pitching leaderboards
            } else {
                ipMin = nil
            }
            let ipFilter = ipMin ?? ""
            let orderDir = (stat.displayAbbrev == "ERA" || stat.displayAbbrev == "WHIP" || stat.displayAbbrev == "BAA") ? "ASC" : "DESC"

            let sql = """
                SELECT p.name, sp.\(stat.dbColumn)
                FROM season_pitching_stats sp
                JOIN players p ON sp.player_id = p.player_id
                WHERE (sp.team = '\(teamCode)' OR sp.team LIKE '\(teamCode)/%' OR sp.team LIKE '%/\(teamCode)')
                      AND sp.season = \(season)\(ipFilter)
                ORDER BY sp.\(stat.dbColumn) \(orderDir)
                LIMIT 15
                """
            guard let result = try? db.execute(sql: sql),
                  !result.rows.isEmpty else {
                return "No \(stat.displayName) data found for the \(fullName) in \(season)."
            }

            var parts: [String] = []
            parts.append("**\(fullName)** \u{2014} \(season) \(stat.displayName) Leaders (Pitching)\n")
            parts.append("[TIP]Tap a player name for their full profile.[/TIP]")

            parts.append("[LEADERBOARD]")
            parts.append("HEADER: \(stat.displayAbbrev)")
            for (i, row) in result.rows.enumerated() {
                let playerName = row[0]
                let rawValue = row[1]
                let formattedValue = stat.isRate ? formatPitchingRate(rawValue, decimals: 2) : rawValue
                parts.append("ROW \(i + 1). \(playerName): \(formattedValue)")
            }
            parts.append("[/LEADERBOARD]")

            if ipMin != nil {
                parts.append("\n_Min. 18 IP._")
            }

            let statName = stat.pillName
            parts.append("\n[SUGGEST]\(season) \(statName) leaders[/SUGGEST]")
            parts.append("[SUGGEST]\(nickname) pitchers[/SUGGEST]")

            return parts.joined(separator: "\n")
        } else {
            // Team pitching overview sorted by ERA
            let sql = """
                SELECT p.name, sp.games, sp.innings_pitched, sp.wins, sp.losses, sp.era
                FROM season_pitching_stats sp
                JOIN players p ON sp.player_id = p.player_id
                WHERE (sp.team = '\(teamCode)' OR sp.team LIKE '\(teamCode)/%' OR sp.team LIKE '%/\(teamCode)')
                      AND sp.season = \(season) AND sp.ip_outs >= 54
                ORDER BY sp.era ASC
                LIMIT 15
                """
            guard let result = try? db.execute(sql: sql),
                  !result.rows.isEmpty else {
                return "No pitching data found for the \(fullName) in \(season)."
            }

            var parts: [String] = []
            parts.append("**\(fullName)** \u{2014} \(season) Pitchers\n")
            parts.append("[TIP]Tap a player name for their full profile.[/TIP]")

            parts.append("[LEADERBOARD]")
            parts.append("HEADER: G, IP, W, L, ERA")
            for (i, row) in result.rows.enumerated() {
                let playerName = row[0]
                let g = row[1]
                let ip = row[2]
                let w = row[3]
                let l = row[4]
                let era = formatPitchingRate(row[5], decimals: 2)
                parts.append("ROW \(i + 1). \(playerName): \(g), \(ip), \(w), \(l), \(era)")
            }
            parts.append("[/LEADERBOARD]")

            parts.append("\n_Min. 18 IP._")
            parts.append("\n[SUGGEST]\(nickname) ERA leaders[/SUGGEST]")
            parts.append("[SUGGEST]\(nickname) strikeout leaders[/SUGGEST]")

            return parts.joined(separator: "\n")
        }
    }
}
