import SwiftUI

struct GameLogsResultView: View {
    let entries: [GameLogEntry]

    @State private var selectedMonth: String = ""
    @State private var expanded = false

    private let deepBlue = Color.brandDeepBlue
    private let lightBlue = Color(red: 0.45, green: 0.7, blue: 1.0)

    /// The most recent year in the game logs (current year context)
    private var latestYear: Int {
        let fmt = DateFormatter()
        fmt.dateFormat = "yyyy-MM-dd"
        let cal = Calendar.current
        var maxYear = 0
        for entry in entries {
            if let date = fmt.date(from: entry.date) {
                let y = cal.component(.year, from: date)
                if y > maxYear { maxYear = y }
            }
        }
        return maxYear > 0 ? maxYear : cal.component(.year, from: Date())
    }

    /// Format a month label — appends 'YY for months in prior years
    private func monthLabel(date: Date) -> String {
        let cal = Calendar.current
        let monthFmt = DateFormatter()
        monthFmt.dateFormat = "MMMM"
        let month = monthFmt.string(from: date)
        let year = cal.component(.year, from: date)
        if year < latestYear {
            let yy = String(format: "%02d", year % 100)
            return "\(month) '\(yy)"
        }
        return month
    }

    private var months: [String] {
        let fmt = DateFormatter()
        fmt.dateFormat = "yyyy-MM-dd"
        var seen = Set<String>()
        var result: [String] = []
        for entry in entries.reversed() { // chronological
            if let date = fmt.date(from: entry.date) {
                let label = monthLabel(date: date)
                if !seen.contains(label) {
                    seen.insert(label)
                    result.append(label)
                }
            }
        }
        return result
    }

    private var filteredEntries: [GameLogEntry] {
        guard !selectedMonth.isEmpty else { return entries }
        let fmt = DateFormatter()
        fmt.dateFormat = "yyyy-MM-dd"
        return entries.filter { entry in
            guard let date = fmt.date(from: entry.date) else { return false }
            return monthLabel(date: date) == selectedMonth
        }
    }

    private var visibleEntries: [GameLogEntry] {
        expanded ? filteredEntries : Array(filteredEntries.prefix(7))
    }

    var body: some View {
        VStack(spacing: 0) {
            // Month pills
            if months.count > 1 {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 0) {
                        monthPill("Recent", isSelected: selectedMonth.isEmpty) {
                            selectedMonth = ""
                            expanded = false
                        }
                        ForEach(months, id: \.self) { month in
                            monthPill(month, isSelected: selectedMonth == month) {
                                selectedMonth = month
                                expanded = false
                            }
                        }
                    }
                    .padding(.horizontal, 12)
                }
                .padding(.top, 8)
            }

            // Game rows
            ForEach(Array(visibleEntries.enumerated()), id: \.offset) { index, entry in
                VStack(spacing: 0) {
                    HStack {
                        Text(formatDate(entry.date))
                            .font(.system(.subheadline, design: .rounded, weight: .semibold))
                            .foregroundStyle(.primary)
                            .fixedSize()
                            .frame(minWidth: 36, alignment: .leading)

                        Text(entry.line)
                            .font(.system(.subheadline, design: .rounded))
                            .foregroundStyle(.primary)

                        Spacer()
                    }
                    .padding(.vertical, 8)
                    .padding(.horizontal, 12)

                    if index < visibleEntries.count - 1 {
                        Divider()
                            .padding(.horizontal, 12)
                    }
                }
            }

            // Show more
            if filteredEntries.count > 7 && !expanded {
                Button {
                    withAnimation { expanded = true }
                } label: {
                    Text("Show more")
                        .font(.system(.caption, design: .rounded, weight: .medium))
                        .foregroundStyle(deepBlue)
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 8)
                }
            }
        }
        .background(
            RoundedRectangle(cornerRadius: 12)
                .fill(Color(.secondarySystemBackground))
                .shadow(color: deepBlue.opacity(0.10), radius: 10, y: 3)
                .shadow(color: .black.opacity(0.03), radius: 2, y: 1)
        )
    }

    @ViewBuilder
    private func monthPill(_ title: String, isSelected: Bool, action: @escaping () -> Void) -> some View {
        Button {
            withAnimation(.easeInOut(duration: 0.15)) { action() }
        } label: {
            Text(title)
                .font(.system(.caption, design: .rounded, weight: .semibold))
                .padding(.horizontal, 12)
                .padding(.vertical, 6)
                .background(
                    isSelected
                    ? AnyShapeStyle(LinearGradient(
                        colors: [lightBlue, deepBlue],
                        startPoint: .leading, endPoint: .trailing))
                    : AnyShapeStyle(Color.clear)
                )
                .clipShape(Capsule())
                .foregroundStyle(isSelected ? .white : .secondary)
        }
    }

    /// Whether game logs span multiple calendar years
    private var spansMultipleYears: Bool {
        guard let first = entries.first?.date.prefix(4),
              let last = entries.last?.date.prefix(4) else { return false }
        return first != last
    }

    private func formatDate(_ dateStr: String) -> String {
        let fmt = DateFormatter()
        fmt.dateFormat = "yyyy-MM-dd"
        guard let date = fmt.date(from: dateStr) else { return dateStr }
        let display = DateFormatter()
        display.dateFormat = spansMultipleYears ? "M/d/yy" : "M/d"
        return display.string(from: date)
    }
}
