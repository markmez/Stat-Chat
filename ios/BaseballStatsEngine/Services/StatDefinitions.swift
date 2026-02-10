import Foundation

enum StatDefinitions {
    private static let definitions: [String: String] = [
        // Counting stats
        "G": "Games played",
        "PA": "Plate appearances — every trip to the plate, including walks, HBP, sacrifices",
        "AB": "At bats — plate appearances minus walks, HBP, sacrifices, and catcher interference",
        "H": "Hits",
        "1B": "Singles",
        "2B": "Doubles",
        "3B": "Triples",
        "HR": "Home runs",
        "R": "Runs scored",
        "RBI": "Runs batted in",
        "SB": "Stolen bases",
        "CS": "Caught stealing",
        "BB": "Walks (bases on balls)",
        "SO": "Strikeouts",
        "K": "Strikeouts",
        "HBP": "Hit by pitch",
        "SF": "Sacrifice flies",
        "IBB": "Intentional walks",

        // Rate stats
        "AVG": "Batting average — hits divided by at bats. League average is around .250.",
        "OBP": "On-base percentage — how often a batter reaches base. League average is around .320.",
        "SLG": "Slugging percentage — total bases divided by at bats. Measures power. League average is around .400.",
        "OPS": "On-base plus slugging — OBP + SLG combined. Quick measure of overall hitting. .800+ is very good, .900+ is elite.",
        "ISO": "Isolated power — SLG minus AVG. Measures extra-base hit ability. .200+ is strong power.",
        "BABIP": "Batting average on balls in play — AVG excluding home runs and strikeouts. League average is around .300. Extreme values often regress.",

        // Advanced stats
        "wRC+": "Weighted runs created plus — park- and league-adjusted offensive value. 100 is average, 150+ is elite.",
        "WAR": "Wins above replacement — total value in wins compared to a replacement-level player. 2+ is solid, 5+ is All-Star, 8+ is MVP.",

        // Streak/game log fields
        "Games": "Number of games in this stretch",
        "Dates": "Date range of this stretch",
    ]

    /// Look up a stat definition by its abbreviation (case-insensitive)
    static func lookup(_ stat: String) -> String? {
        let key = stat.trimmingCharacters(in: .whitespaces)
        return definitions[key] ?? definitions[key.uppercased()]
    }
}
