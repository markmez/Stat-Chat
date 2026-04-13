import Foundation

struct Suggestion: Sendable {
    let id: String
    let text: String
}

@MainActor
final class SuggestionEngine {

    static let shared = SuggestionEngine()

    private(set) var config: SuggestionConfig

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

    var tappedIds: Set<String> {
        Set(UserDefaults.standard.stringArray(forKey: tappedKey) ?? [])
    }

    private var tapped: Set<String> {
        get { tappedIds }
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

    // MARK: - Build sequence

    /// Curated pill order. First-time users see these in this exact sequence.
    /// After full cycle, pills randomize. Tapped pills are skipped.
    private static let sequenceOrder: [String] = [
        "d44",  // Most 30/30 seasons all time
        "d88",  // 4+ XBH games last year
        "d32",  // Bobby Witt Jr
        "d79",  // Last player to steal 80 bases
        "d55",  // Most wins with an ERA over 5.00?
        "d48",  // Complete game leaders this season
        "d49",  // Ohtani hot streaks this season
        "d54",  // Lowest ERA with 200+ IP this decade
        "d43",  // Compare Guerrero Jr and Alonso
        "d72",  // Babe Ruth
        "d1",   // Home run leaders this season
        "d2",   // Top 10 in OPS this season
        "d41",  // Compare Soto and Judge this season
        "d4",   // Stolen base leaders this season
        "d6",   // Ohtani home runs this season
        "d45",  // Longest hitting streak all time
        "d12",  // Tucker vs lefties this season
        "d8",   // Closest to .400 since Ted Williams
        "d14",  // Elly De La Cruz this season
        "d17",  // 50+ HR seasons all time
        "d7",   // Judge home vs away this season
        "d21",  // RBI leaders this season
        "d31",  // Gunnar Henderson
        "d10",  // Starter ERA leaders this season
        "d11",  // Compare Lindor and Witt Jr
        "d29",  // Mookie Betts
        "d9",   // Top 5 in batting average this season
        "d80",  // Albies vs sliders this season
        "d90",  // Active home run leaders
        "d13",  // Turner hot streaks this season
        "d22",  // Pitching K leaders this season
        "d51",  // Most home runs while batting .300+?
        "d62",  // Corbin Carroll
        "d19",  // OPS+ leaders this season
        "d53",  // Highest AVG with 30+ HR last year
        "d67",  // Fernando Tatis Jr
        "d23",  // Top 10 in hits this season
        "d42",  // Compare Devers and Ramirez
        "d81",  // Adames with two strikes this season
        "d86",  // Players with 3000 hits and 500 HR
        "d24",  // Who has the best WHIP this season?
        "d5",   // How is Juan Soto doing this season?
        "d52",  // Most stolen bases with under 10 CS?
        "d68",  // Jackson Chourio
        "d83",  // LHH with 30+ HR last year
        "d61",  // Longest hitting streak this season
        "d50",  // Jose Ramirez home vs away this season
        "d34",  // Harper cold streaks this season
        "d20",  // Doubles leaders this season
        "d56",  // Highest OPS with under 50 K
        "d63",  // Julio Rodriguez
        "d91",  // Sub-3 ERA relievers last 3 years
        "d25",  // Most saves this season
        "d82",  // Contreras RISP stats this season
        "d30",  // Freddie Freeman
        "d18",  // 60+ stolen bases in a season, ever
        "d85",  // Switch hitters .800+ OPS
        "d28",  // Wins leaders this season
        "d33",  // Alvarez slash line this season
        "d60",  // Most K in a single season
        "d64",  // Adley Rutschman
        "d87",  // Most 3-hit games in 2025
        "d26",  // Triples leaders this season
        "d73",  // Mickey Mantle
        "d89",  // Best career ERA active players
        "d27",  // Top 5 in walks this season
        "d84",  // Rookie pitchers with ERA under 3
        "d76",  // Ketel Marte
        "d16",  // What does BABIP measure?
        "d65",  // Marcell Ozuna
        "d78",  // Youngest player to hit 40 home runs
        "d74",  // Willie Mays
        "d38",  // What does ISO measure?
        "d35",  // Corey Seager's stats this season
        "d46",  // How many players have hit 40/40?
        "d77",  // Marcus Semien
        "d75",  // Hank Aaron
        "d57",  // Most RBIs in a single game
        "d69",  // Jarren Duran
        "d47",  // Triple crown winners all time
        "d70",  // Matt Olson
        "d59",  // Perfect games since 2010
        "d71",  // CJ Abrams
        "d58",  // Most career grand slams
        "d66",  // Manny Machado
    ]

    /// Legacy entry point for AnimatedPlaceholder
    func buildPool(searchHistory: [String]) -> [Suggestion] {
        buildSequence(searchHistory: searchHistory)
    }

    func buildSequence(searchHistory: [String]) -> [Suggestion] {
        let tappedSet = tappedIds

        // Build ID → display text map
        var idToText: [String: String] = [:]
        for d in config.defaults where passesSeasonFilter(d) {
            idToText[d.id] = displayText(for: d)
        }

        // Build sequence in curated order, then append any unordered pills
        var result: [Suggestion] = []
        var usedIds: Set<String> = []

        for id in Self.sequenceOrder {
            guard let text = idToText[id], !tappedSet.contains(id) else { continue }
            result.append(Suggestion(id: id, text: text))
            usedIds.insert(id)
        }

        // Append any pills not in the sequence order (future additions via S3)
        let remaining = config.defaults
            .filter { passesSeasonFilter($0) && !usedIds.contains($0.id) && !tappedSet.contains($0.id) }
            .map { Suggestion(id: $0.id, text: displayText(for: $0)) }
            .shuffled()
        result.append(contentsOf: remaining)

        return result
    }

    // MARK: - Season awareness

    private var seasonState: SeasonState {
        let now = Date()
        let cal = Calendar.current
        let month = cal.component(.month, from: now)
        let day = cal.component(.day, from: now)

        if month < 3 || (month == 3 && day < 30) { return .offseason }
        if month == 3 || (month == 4 && day <= 12) { return .earlySeason }
        return .inSeason
    }

    private enum SeasonState {
        case offseason, earlySeason, inSeason
    }

    private func displayText(for d: SuggestionConfig.DefaultSuggestion) -> String {
        switch seasonState {
        case .offseason:
            return d.text
        case .earlySeason, .inSeason:
            return d.inSeasonText ?? d.text
        }
    }

    private func passesSeasonFilter(_ d: SuggestionConfig.DefaultSuggestion) -> Bool {
        guard let filter = d.seasonFilter else { return true }
        switch filter {
        case "offseasonOnly":
            return seasonState == .offseason
        case "afterApril12":
            return seasonState == .inSeason
        default:
            return true
        }
    }
}
