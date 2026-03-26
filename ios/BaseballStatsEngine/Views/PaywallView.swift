import SwiftUI

struct PaywallView: View {
    @Environment(AppState.self) private var appState
    @Environment(\.dismiss) private var dismiss

    private let store = StoreKitService.shared
    private let deepBlue = Color(red: 0.1, green: 0.25, blue: 0.7)
    private let lightBlue = Color(red: 0.45, green: 0.7, blue: 1.0)

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                Spacer()

                // Header
                VStack(spacing: 16) {
                    ZStack {
                        Image(systemName: "sparkle")
                            .font(.system(size: 44, weight: .bold))
                            .foregroundStyle(
                                LinearGradient(
                                    colors: [lightBlue, deepBlue],
                                    startPoint: .topLeading, endPoint: .bottomTrailing
                                )
                            )

                        Image(systemName: "baseball.fill")
                            .font(.system(size: 18))
                            .foregroundStyle(lightBlue)
                            .offset(x: 20, y: -20)

                        Image(systemName: "baseball.fill")
                            .font(.system(size: 14))
                            .foregroundStyle(lightBlue.opacity(0.7))
                            .offset(x: -16, y: -16)

                        Image(systemName: "baseball.fill")
                            .font(.system(size: 15))
                            .foregroundStyle(lightBlue.opacity(0.85))
                            .offset(x: 16, y: 16)
                    }

                    Text("You've used all 5 free\nsearches this week")
                        .font(.system(size: 22, weight: .bold, design: .rounded))
                        .multilineTextAlignment(.center)
                        .foregroundStyle(.primary)

                    Text("Upgrade for unlimited searches")
                        .font(.system(.subheadline, design: .rounded))
                        .foregroundStyle(.secondary)
                }

                Spacer()

                // Purchase buttons
                VStack(spacing: 12) {
                    // Monthly
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
                        .padding(.horizontal, 20)
                        .padding(.vertical, 14)
                        .background(
                            LinearGradient(
                                colors: [lightBlue, deepBlue],
                                startPoint: .leading, endPoint: .trailing
                            ),
                            in: RoundedRectangle(cornerRadius: 12)
                        )
                    }

                    // Yearly
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
                        .padding(.horizontal, 20)
                        .padding(.vertical, 14)
                        .background(Color(uiColor: .tertiarySystemBackground), in: RoundedRectangle(cornerRadius: 12))
                        .overlay(
                            RoundedRectangle(cornerRadius: 12)
                                .stroke(Color(uiColor: .separator).opacity(0.4), lineWidth: 1)
                        )
                    }

                    // Restore
                    Button {
                        Task { await store.restorePurchases() }
                    } label: {
                        Text("Restore Purchases")
                            .font(.system(.caption, design: .rounded))
                            .foregroundStyle(.secondary)
                    }
                    .padding(.top, 4)

                    if store.purchaseInProgress {
                        ProgressView()
                            .padding(.top, 8)
                    }
                }
                .padding(.horizontal, 24)
                .padding(.bottom, 40)
            }
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button {
                        AnalyticsService.trackPaywallDismiss()
                        dismiss()
                    } label: {
                        Image(systemName: "xmark.circle.fill")
                            .font(.system(size: 24))
                            .symbolRenderingMode(.hierarchical)
                            .foregroundStyle(.secondary)
                    }
                }
            }
        }
        .onChange(of: store.isSubscribed) { _, subscribed in
            if subscribed { dismiss() }
        }
        .interactiveDismissDisabled(store.purchaseInProgress)
    }
}
