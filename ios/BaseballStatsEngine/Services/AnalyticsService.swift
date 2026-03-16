import Foundation
import Mixpanel

enum AnalyticsService {
    private static let token = "12be0290014587e6017f7bd447315c44"

    static func initialize(distinctId: String) {
        Mixpanel.initialize(token: token, trackAutomaticEvents: false)
        Mixpanel.mainInstance().identify(distinctId: distinctId)
    }

    // MARK: - Query events

    static func trackQuery(text: String, type: QueryType) {
        Mixpanel.mainInstance().track(event: "query", properties: [
            "query_text": text,
            "query_type": type.rawValue
        ])
    }

    enum QueryType: String {
        // Local intercepts
        case localComparison = "local_comparison"
        case localLeaderboard = "local_leaderboard"
        case localThreshold = "local_threshold"
        case localAllTimeThreshold = "local_all_time_threshold"
        case localSuperlative = "local_superlative"
        case localFilteredLeaderboard = "local_filtered_leaderboard"
        case localStreak = "local_streak"
        case localCurrentForm = "local_current_form"
        case localStatLookup = "local_stat_lookup"
        case localCareer = "local_career"
        case localPlatoon = "local_platoon"
        case localHomeAway = "local_home_away"
        case localSeasonLookup = "local_season_lookup"
        case localMonthStats = "local_month_stats"
        case localMilestone = "local_milestone"
        case localTeamRanking = "local_team_ranking"
        case localTeamTotal = "local_team_total"
        case localTeamStats = "local_team_stats"
        case localStatDefinition = "local_stat_definition"
        case localDisambiguation = "local_disambiguation"
        // Backend
        case backendClaude = "backend_claude"
        case backendComparison = "backend_comparison"
        case backendThreshold = "backend_threshold"
        case backendMilestone = "backend_milestone"
        case backendLeaderboard = "backend_leaderboard"
    }

    // MARK: - Player card views

    static func trackPlayerCardView(name: String) {
        Mixpanel.mainInstance().track(event: "player_card_view", properties: [
            "player_name": name
        ])
    }

    // MARK: - Team card views

    static func trackTeamCardView(code: String) {
        Mixpanel.mainInstance().track(event: "team_card_view", properties: [
            "team_code": code
        ])
    }

    // MARK: - Suggestion pill taps

    static func trackSuggestionTap(text: String, source: SuggestionSource) {
        Mixpanel.mainInstance().track(event: "suggestion_tap", properties: [
            "suggestion_text": text,
            "source": source.rawValue
        ])
    }

    enum SuggestionSource: String {
        case animatedPlaceholder = "animated_placeholder"
        case resultPill = "result_pill"
    }

    // MARK: - Session events

    static func trackAppOpen() {
        Mixpanel.mainInstance().track(event: "app_open")
    }

    // MARK: - Paywall events (for future StoreKit integration)

    static func trackPaywallHit(queryCount: Int) {
        Mixpanel.mainInstance().track(event: "paywall_hit", properties: [
            "weekly_query_count": queryCount
        ])
    }

    static func trackSubscription(plan: String) {
        Mixpanel.mainInstance().track(event: "subscription", properties: [
            "plan": plan
        ])
    }
}
