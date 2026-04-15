import SwiftUI

extension Color {
    /// Deep blue — adapts for dark mode (use for text, links, icons)
    static let brandDeepBlue = Color(
        uiColor: UIColor { traits in
            if traits.userInterfaceStyle == .dark {
                return UIColor(red: 0.35, green: 0.55, blue: 1.0, alpha: 1.0)
            } else {
                return UIColor(red: 0.1, green: 0.25, blue: 0.7, alpha: 1.0)
            }
        }
    )

    /// Light blue — adapts for dark mode (use for text, links, icons)
    static let brandLightBlue = Color(
        uiColor: UIColor { traits in
            if traits.userInterfaceStyle == .dark {
                return UIColor(red: 0.55, green: 0.78, blue: 1.0, alpha: 1.0)
            } else {
                return UIColor(red: 0.45, green: 0.7, blue: 1.0, alpha: 1.0)
            }
        }
    )

    /// Fixed deep blue — does NOT adapt (use for pill/gradient backgrounds)
    static let brandDeepBlueFixed = Color(red: 0.1, green: 0.25, blue: 0.7)

    /// Fixed light blue — does NOT adapt (use for pill/gradient backgrounds)
    static let brandLightBlueFixed = Color(red: 0.45, green: 0.7, blue: 1.0)
}
