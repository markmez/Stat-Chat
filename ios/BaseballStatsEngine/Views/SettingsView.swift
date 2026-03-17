import SwiftUI

struct SettingsView: View {
    @Environment(AppState.self) private var appState

    private let store = StoreKitService.shared
    private let deepBlue = Color(red: 0.1, green: 0.25, blue: 0.7)
    private let lightBlue = Color(red: 0.45, green: 0.7, blue: 1.0)
    private let freeLimit = 5

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

                // Subscription
                subscriptionSection

                // Appearance
                appearanceSection

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
                    }
                }

                Spacer(minLength: 40)
            }
            .padding(.horizontal, 20)
        }
        .background(Color(uiColor: .systemBackground))
        .navigationTitle("Settings")
        .navigationBarTitleDisplayMode(.inline)
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
