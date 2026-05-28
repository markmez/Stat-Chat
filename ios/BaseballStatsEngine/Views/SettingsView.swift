import SwiftUI

struct SettingsView: View {
    @Environment(AppState.self) private var appState

    private let store = StoreKitService.shared
    private let deepBlue = Color.brandDeepBlue
    private let lightBlue = Color(red: 0.45, green: 0.7, blue: 1.0)
    private let freeLimit = 5

    @State private var dataFreshness: Date? = nil

    var body: some View {
        ScrollView {
            VStack(spacing: 28) {
                // App header
                HStack(spacing: 10) {
                    Text("StatChat")
                        .font(.system(size: 28, weight: .bold))
                        .foregroundStyle(
                            LinearGradient(
                                colors: [lightBlue, deepBlue],
                                startPoint: .leading, endPoint: .trailing
                            )
                        )

                    Image(systemName: "baseball.fill")
                        .font(.system(size: 18, weight: .bold))
                        .foregroundStyle(lightBlue)
                }
                .padding(.top, 12)

                // Data freshness — its own element between header and first module.
                // Negative vertical padding tightens spacing by ~35% (28pt VStack
                // gap → effective ~18pt above and below).
                if let updated = dataFreshness {
                    Text("Stats updated each morning, most recently \(Self.relativeFreshness(updated))")
                        .font(.system(.caption, design: .rounded))
                        .foregroundStyle(.secondary)
                        .frame(maxWidth: .infinity, alignment: .center)
                        .padding(.vertical, -10)
                }

                // Subscription
                subscriptionSection

                // Appearance
                appearanceSection

                // Where answers come from — verified data + clearly-flagged AI fallback
                infoSection(title: "Our Answer Engine") {
                    VStack(alignment: .leading, spacing: 16) {
                        Text("We use AI to understand your question, and real game data to answer it. The data comes from the sources below, among others. Responses derived from AI are clearly marked.")

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
                            Text("Data provided by the Chadwick Baseball Bureau, available under the Open Database License.")
                                .font(.system(.caption, design: .rounded))
                                .foregroundStyle(.secondary)
                        }

                        VStack(alignment: .leading, spacing: 6) {
                            Text("Lahman Baseball Database")
                                .font(.system(.subheadline, design: .rounded, weight: .semibold))
                                .foregroundStyle(.primary)
                            Text("Data from the Lahman Baseball Database, available under the Creative Commons Attribution-ShareAlike 3.0 license.")
                                .font(.system(.caption, design: .rounded))
                                .foregroundStyle(.secondary)
                        }

                        VStack(alignment: .leading, spacing: 6) {
                            Text("Wikipedia")
                                .font(.system(.subheadline, design: .rounded, weight: .semibold))
                                .foregroundStyle(.primary)
                            Text("Player biographies sourced from Wikipedia, available under the Creative Commons Attribution-ShareAlike 3.0 license.")
                                .font(.system(.caption, design: .rounded))
                                .foregroundStyle(.secondary)
                        }
                    }
                }

                // Support & Contact
                infoSection(title: "Support") {
                    VStack(alignment: .leading, spacing: 12) {
                        Link(destination: URL(string: "https://secondsignalapps.com/support.html")!) {
                            HStack {
                                Image(systemName: "questionmark.circle")
                                    .font(.system(size: 14))
                                Text("Help & Support")
                                    .font(.system(.subheadline, design: .rounded))
                                Spacer()
                                Image(systemName: "arrow.up.right")
                                    .font(.system(size: 11))
                                    .foregroundStyle(.tertiary)
                            }
                            .foregroundStyle(.primary)
                        }

                        Divider()

                        Link(destination: URL(string: "mailto:info@secondsignalapps.com")!) {
                            HStack {
                                Image(systemName: "envelope")
                                    .font(.system(size: 14))
                                Text("Contact Us")
                                    .font(.system(.subheadline, design: .rounded))
                                Spacer()
                                Image(systemName: "arrow.up.right")
                                    .font(.system(size: 11))
                                    .foregroundStyle(.tertiary)
                            }
                            .foregroundStyle(.primary)
                        }

                        Divider()

                        Link(destination: URL(string: "https://secondsignalapps.com/privacy.html")!) {
                            HStack {
                                Image(systemName: "hand.raised")
                                    .font(.system(size: 14))
                                Text("Privacy Policy")
                                    .font(.system(.subheadline, design: .rounded))
                                Spacer()
                                Image(systemName: "arrow.up.right")
                                    .font(.system(size: 11))
                                    .foregroundStyle(.tertiary)
                            }
                            .foregroundStyle(.primary)
                        }

                        Divider()

                        Link(destination: URL(string: "https://www.apple.com/legal/internet-services/itunes/dev/stdeula/")!) {
                            HStack {
                                Image(systemName: "doc.text")
                                    .font(.system(size: 14))
                                Text("Terms of Use")
                                    .font(.system(.subheadline, design: .rounded))
                                Spacer()
                                Image(systemName: "arrow.up.right")
                                    .font(.system(size: 11))
                                    .foregroundStyle(.tertiary)
                            }
                            .foregroundStyle(.primary)
                        }
                    }
                }

                Spacer(minLength: 40)
            }
            .padding(.horizontal, 20)
        }
        .background(Color(uiColor: .systemBackground))
        .navigationTitle("Settings")
        .navigationBarTitleDisplayMode(.inline)
        .task {
            dataFreshness = await BackendService().fetchDataFreshness()
        }
    }

    /// Format "data last updated" in the user's local timezone with a
    /// friendly relative-ish phrasing. "today at 5:13 AM", "yesterday at
    /// 11:30 PM", or an absolute date for anything older.
    private static func relativeFreshness(_ date: Date) -> String {
        let cal = Calendar.current
        let timeFmt = DateFormatter()
        timeFmt.dateStyle = .none
        timeFmt.timeStyle = .short  // honors user's 12h/24h preference
        let time = timeFmt.string(from: date)
        if cal.isDateInToday(date) { return "today at \(time)" }
        if cal.isDateInYesterday(date) { return "yesterday at \(time)" }
        let dateFmt = DateFormatter()
        dateFmt.dateStyle = .medium
        dateFmt.timeStyle = .short
        return dateFmt.string(from: date)
    }

    // MARK: - Subscription

    private var subscriptionSection: some View {
        VStack(alignment: .leading, spacing: 14) {
            if store.isSubscribed {
                // No usage meter needed — unlimited
            } else {
                // Usage meter
                VStack(alignment: .leading, spacing: 8) {
                    HStack(alignment: .firstTextBaseline) {
                        Text("\(min(appState.weeklyQueryCount, freeLimit))")
                            .font(.system(size: 28, weight: .bold, design: .rounded))
                            .foregroundStyle(
                                appState.weeklyQueryCount >= freeLimit ? Color.red : deepBlue
                            )
                        Text("of \(freeLimit) free questions used this week")
                            .font(.system(.subheadline, design: .rounded))
                            .foregroundStyle(.secondary)
                    }

                    // Progress bar
                    GeometryReader { geo in
                        ZStack(alignment: .leading) {
                            Capsule()
                                .fill(Color(uiColor: .tertiarySystemFill))
                                .frame(height: 6)
                            Capsule()
                                .fill(
                                    LinearGradient(
                                        colors: appState.weeklyQueryCount >= freeLimit
                                            ? [.red, .red]
                                            : [lightBlue, deepBlue],
                                        startPoint: .leading, endPoint: .trailing
                                    )
                                )
                                .frame(
                                    width: geo.size.width * min(CGFloat(appState.weeklyQueryCount) / CGFloat(freeLimit), 1.0),
                                    height: 6
                                )
                        }
                    }
                    .frame(height: 6)
                }

                Divider()
            }

            if store.isSubscribed {
                // Subscribed state
                VStack(alignment: .leading, spacing: 12) {
                    HStack(spacing: 8) {
                        Image(systemName: "checkmark.seal.fill")
                            .foregroundStyle(deepBlue)
                        Text("Subscribed")
                            .font(.system(.subheadline, design: .rounded, weight: .semibold))
                            .foregroundStyle(.primary)
                    }

                    Text("You have unlimited questions")
                        .font(.system(.caption, design: .rounded))
                        .foregroundStyle(.secondary)

                    Button {
                        if let url = URL(string: "https://apps.apple.com/account/subscriptions") {
                            UIApplication.shared.open(url)
                        }
                    } label: {
                        Text("Manage Subscription")
                            .font(.system(.caption, design: .rounded))
                            .foregroundStyle(deepBlue)
                    }
                }
            } else {
                // Upgrade CTA
                VStack(alignment: .leading, spacing: 12) {
                    Text("Upgrade for unlimited questions")
                        .font(.system(.subheadline, design: .rounded, weight: .semibold))
                        .foregroundStyle(.primary)

                    // Pricing buttons
                    VStack(spacing: 10) {
                        Button {
                            Task {
                                if let product = store.monthlyProduct {
                                    _ = await store.purchase(product)
                                }
                            }
                        } label: {
                            HStack {
                                Text("Monthly")
                                    .font(.system(.subheadline, design: .rounded, weight: .semibold))
                                Spacer()
                                Text(store.monthlyProduct?.displayPrice ?? "$2.99")
                                    .font(.system(.subheadline, design: .rounded, weight: .semibold))
                                Text("/ mo")
                                    .font(.system(.caption, design: .rounded))
                            }
                            .foregroundStyle(.white)
                            .padding(.horizontal, 18)
                            .padding(.vertical, 12)
                            .background(
                                LinearGradient(
                                    colors: [lightBlue, deepBlue],
                                    startPoint: .leading, endPoint: .trailing
                                ),
                                in: RoundedRectangle(cornerRadius: 10)
                            )
                        }

                        Button {
                            Task {
                                if let product = store.yearlyProduct {
                                    _ = await store.purchase(product)
                                }
                            }
                        } label: {
                            HStack {
                                VStack(alignment: .leading, spacing: 2) {
                                    Text("Yearly")
                                        .font(.system(.subheadline, design: .rounded, weight: .semibold))
                                    Text("Save 44%")
                                        .font(.system(.caption2, design: .rounded))
                                        .foregroundStyle(lightBlue)
                                }
                                Spacer()
                                Text(store.yearlyProduct?.displayPrice ?? "$19.99")
                                    .font(.system(.subheadline, design: .rounded, weight: .semibold))
                                Text("/ yr")
                                    .font(.system(.caption, design: .rounded))
                            }
                            .foregroundStyle(.primary)
                            .padding(.horizontal, 18)
                            .padding(.vertical, 12)
                            .background(Color(uiColor: .tertiarySystemBackground), in: RoundedRectangle(cornerRadius: 10))
                            .overlay(
                                RoundedRectangle(cornerRadius: 10)
                                    .stroke(Color(uiColor: .separator).opacity(0.4), lineWidth: 1)
                            )
                        }
                    }

                    Button {
                        Task { await store.restorePurchases() }
                    } label: {
                        Text("Restore Purchases")
                            .font(.system(.caption, design: .rounded))
                            .foregroundStyle(.secondary)
                    }
                    .frame(maxWidth: .infinity)

                    if store.purchaseInProgress {
                        ProgressView()
                            .frame(maxWidth: .infinity)
                    }
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(16)
        .background(Color(uiColor: .secondarySystemBackground), in: RoundedRectangle(cornerRadius: 14))
        .overlay(
            RoundedRectangle(cornerRadius: 14)
                .stroke(Color(uiColor: .separator).opacity(0.2), lineWidth: 0.5)
        )
    }

    private var appearanceSection: some View {
        @Bindable var state = appState
        return VStack(alignment: .leading, spacing: 10) {
            Text("Appearance")
                .font(.system(.headline, design: .rounded))
                .foregroundStyle(.primary)

            Picker("", selection: $state.appearanceMode) {
                ForEach(AppearanceMode.allCases, id: \.self) { mode in
                    Text(mode.label).tag(mode)
                }
            }
            .pickerStyle(.segmented)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(16)
        .background(Color(uiColor: .secondarySystemBackground), in: RoundedRectangle(cornerRadius: 14))
        .overlay(
            RoundedRectangle(cornerRadius: 14)
                .stroke(Color(uiColor: .separator).opacity(0.2), lineWidth: 0.5)
        )
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
}
