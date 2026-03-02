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
        "OPS+": "OPS adjusted for league average. 100 = league average hitter, 150+ is elite. Lets you compare across different seasons.",
        "ISO": "Isolated power — SLG minus AVG. Measures extra-base hit ability. .200+ is strong power.",
        "BABIP": "Batting average on balls in play — AVG excluding home runs and strikeouts. League average is around .300. Extreme values often regress.",

        // Advanced stats
        "wRC+": "Weighted runs created plus — park- and league-adjusted offensive value. 100 is average, 150+ is elite.",
        "WAR": "Wins above replacement — total value in wins compared to a replacement-level player. 2+ is solid, 5+ is All-Star, 8+ is MVP.",

        // Fielding stats
        "PO": "Putouts — outs recorded directly by this fielder (catching a fly ball, stepping on a base, tagging a runner)",
        "A": "Assists — throws that lead to an out being recorded by another fielder",
        "E": "Errors — misplays that allow a batter or runner to advance",
        "DP": "Double plays turned",
        "PB": "Passed balls — pitches the catcher should have caught but didn't, allowing runners to advance",
        "FLD%": "Fielding percentage — (putouts + assists) / (putouts + assists + errors). .980+ is solid, .990+ is excellent.",
        "GS": "Games started",
        "INN": "Innings played at this position",

        // Pitching stats
        "ERA": "Earned run average — earned runs allowed per 9 innings pitched. Lower is better. Sub-3.00 is excellent, sub-2.00 is elite.",
        "WHIP": "Walks plus hits per inning pitched — baserunners allowed per inning. Lower is better. Sub-1.00 is elite, 1.00-1.20 is excellent.",
        "K/9": "Strikeouts per 9 innings — measures a pitcher's ability to miss bats. 9.0+ is very good, 10.0+ is elite.",
        "BB/9": "Walks per 9 innings — measures a pitcher's control. Lower is better. Sub-2.0 is excellent.",
        "K/BB": "Strikeout-to-walk ratio — strikeouts divided by walks. Higher is better. 3.0+ is very good, 4.0+ is elite.",
        "H/9": "Hits allowed per 9 innings. Lower is better. Sub-7.0 is very good.",
        "HR/9": "Home runs allowed per 9 innings. Lower is better. Sub-1.0 is good.",
        "BAA": "Batting average against — opponents' batting average. Lower is better. Sub-.220 is very good.",
        "ERA+": "ERA adjusted for league average and park factors. 100 = league average, higher is better. 150+ is elite.",
        "W": "Wins — games where the pitcher was the pitcher of record when the winning team took the lead for good.",
        "L": "Losses — games where the pitcher was the pitcher of record when the opposing team took the lead for good.",
        "SV": "Saves — a relief pitcher finishes a game won by their team under specific conditions (entered with a lead of 3 or fewer runs, or the tying run was on base/at bat/on deck).",
        "IP": "Innings pitched — each out recorded counts as one-third of an inning. 200+ IP in a season is a workhorse.",
        "QS": "Quality starts — starts where the pitcher went at least 6 innings and allowed 3 or fewer earned runs.",
        "CG": "Complete games — games where the starting pitcher pitched the entire game.",
        "GF": "Games finished — games where the pitcher recorded the final out. Typically a closer/reliever stat.",
        "WP": "Wild pitches — pitches too far from the strike zone for the catcher to handle, allowing runners to advance.",
        "BK": "Balks — illegal pitching motions that allow baserunners to advance one base.",
        "BF": "Batters faced — total number of batters a pitcher has faced (pitching equivalent of plate appearances).",

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
