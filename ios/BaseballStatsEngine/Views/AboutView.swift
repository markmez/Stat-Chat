import SwiftUI

struct AboutView: View {
    private let deepBlue = Color(red: 0.1, green: 0.25, blue: 0.7)
    private let lightBlue = Color(red: 0.45, green: 0.7, blue: 1.0)

    var body: some View {
        ScrollView {
            VStack(spacing: 28) {
                // App header
                VStack(spacing: 8) {
                    HStack(spacing: 10) {
                        Text("StatChat")
                            .font(.system(size: 28, weight: .bold))
                            .foregroundStyle(
                                LinearGradient(
                                    colors: [lightBlue, deepBlue],
                                    startPoint: .leading, endPoint: .trailing
                                )
                            )

                        Image(systemName: "sparkle")
                            .font(.system(size: 18, weight: .bold))
                            .foregroundStyle(lightBlue)
                    }

                    Text("Baseball stats, answered instantly")
                        .font(.system(.subheadline, design: .rounded))
                        .foregroundStyle(.secondary)
                }
                .padding(.top, 12)

                // AI Disclosure
                infoSection(title: "Powered by AI") {
                    Text("StatChat uses advanced AI to translate your questions into precise database queries. Every stat is computed from real historical data \u{2014} never generated or estimated.")
                }

                // Data Sources
                infoSection(title: "Data Sources") {
                    VStack(alignment: .leading, spacing: 16) {
                        VStack(alignment: .leading, spacing: 6) {
                            Text("Retrosheet")
                                .font(.system(.subheadline, design: .rounded, weight: .semibold))
                                .foregroundStyle(.primary)
                            Text("The information used here was obtained free of charge from and is copyrighted by Retrosheet. Interested parties may contact Retrosheet at www.retrosheet.org.")
                                .font(.system(.caption, design: .rounded))
                                .foregroundStyle(.secondary)
                        }

                        VStack(alignment: .leading, spacing: 6) {
                            Text("Chadwick Baseball Bureau")
                                .font(.system(.subheadline, design: .rounded, weight: .semibold))
                                .foregroundStyle(.primary)
                            Text("Platoon split data provided by the Chadwick Baseball Bureau, available under the Open Database License.")
                                .font(.system(.caption, design: .rounded))
                                .foregroundStyle(.secondary)
                        }
                    }
                }

                // Coverage
                infoSection(title: "Data Coverage") {
                    VStack(alignment: .leading, spacing: 8) {
                        coverageRow("Season stats", "1898 \u{2013} present")
                        coverageRow("Game logs", "2016 \u{2013} present")
                        coverageRow("Platoon splits", "1969 \u{2013} present")
                        coverageRow("Home/away splits", "2016 \u{2013} present")
                        coverageRow("Fielding stats", "2016 \u{2013} present")
                        coverageRow("Streak detection", "2016 \u{2013} present")
                    }
                }

                Spacer(minLength: 40)
            }
            .padding(.horizontal, 20)
        }
        .background(Color(uiColor: .systemBackground))
        .navigationTitle("About")
        .navigationBarTitleDisplayMode(.inline)
    }

    private func infoSection(title: String, @ViewBuilder content: () -> some View) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(title)
                .font(.system(.headline, design: .rounded))
                .foregroundStyle(.primary)

            content()
                .font(.system(.subheadline, design: .rounded))
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(16)
        .background(Color(uiColor: .secondarySystemBackground), in: RoundedRectangle(cornerRadius: 14))
        .overlay(
            RoundedRectangle(cornerRadius: 14)
                .stroke(Color(uiColor: .separator).opacity(0.2), lineWidth: 0.5)
        )
    }

    private func coverageRow(_ label: String, _ range: String) -> some View {
        HStack {
            Text(label)
                .font(.system(.subheadline, design: .rounded))
                .foregroundStyle(.primary)
            Spacer()
            Text(range)
                .font(.system(.subheadline, design: .rounded))
                .foregroundStyle(.secondary)
        }
    }
}
