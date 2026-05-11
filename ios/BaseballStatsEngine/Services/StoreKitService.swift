import StoreKit

@Observable
@MainActor
final class StoreKitService {
    static let shared = StoreKitService()

    private(set) var products: [Product] = []
    private(set) var isSubscribed = false
    private(set) var purchaseInProgress = false

    var monthlyProduct: Product? { products.first { $0.id == Self.monthlyID } }
    var yearlyProduct: Product? { products.first { $0.id == Self.yearlyID } }

    private static let monthlyID = "com.statchat.app.monthly"
    private static let yearlyID = "com.statchat.app.yearly"
    private static let productIDs: Set<String> = [monthlyID, yearlyID]
    private static let subscribedKey = "isSubscribedCache"

    private var transactionListener: Task<Void, Never>?

    private init() {
        // Restore cached state immediately so UI doesn't flash
        isSubscribed = UserDefaults.standard.bool(forKey: Self.subscribedKey)
        listenForTransactions()
        Task { await fetchProducts() }
        Task { await updateSubscriptionStatus() }
    }

    // MARK: - Products

    func fetchProducts() async {
        do {
            let storeProducts = try await Product.products(for: Self.productIDs)
            products = storeProducts.sorted { $0.price < $1.price }
        } catch {
            // Products unavailable — buttons will show hardcoded fallback prices
        }
    }

    // MARK: - Purchase

    func purchase(_ product: Product) async -> Bool {
        purchaseInProgress = true
        defer { purchaseInProgress = false }

        do {
            let result = try await product.purchase()
            switch result {
            case .success(let verification):
                let transaction = try checkVerified(verification)
                await transaction.finish()
                await updateSubscriptionStatus()
                // Push the signed JWS to the backend so device_quota.is_paid
                // flips and metering bypasses the 5/week limit.
                await syncEntitlementToBackend()
                let plan = product.id == Self.monthlyID ? "monthly" : "yearly"
                AnalyticsService.trackSubscription(plan: plan)
                return true
            case .userCancelled:
                return false
            case .pending:
                return false
            @unknown default:
                return false
            }
        } catch {
            return false
        }
    }

    // MARK: - Restore

    func restorePurchases() async {
        do {
            try await AppStore.sync()
        } catch {
            // sync failed — fall through to status check
        }
        await updateSubscriptionStatus()
    }

    // MARK: - Subscription Status

    func updateSubscriptionStatus() async {
        var hasEntitlement = false
        for await result in Transaction.currentEntitlements {
            if let transaction = try? checkVerified(result) {
                if Self.productIDs.contains(transaction.productID) {
                    hasEntitlement = true
                    break
                }
            }
        }
        isSubscribed = hasEntitlement
        UserDefaults.standard.set(isSubscribed, forKey: Self.subscribedKey)
        // After local state settles, re-sync with backend so the
        // device_quota.is_paid flag matches reality. Cheap idempotent call
        // — the backend just re-verifies the same JWS and re-UPDATEs the
        // row. Catches the case where a user switched devices, reinstalled,
        // or the backend lost state for any reason.
        if hasEntitlement {
            await syncEntitlementToBackend()
        }
    }

    /// Push every active entitlement's JWS to the backend so it can flip
    /// device_quota.is_paid. The backend cryptographically verifies the JWS
    /// against Apple's cert chain — failures here mean the user can still
    /// use the app subscribed locally, but they'll hit the metering limit
    /// server-side until the next sync succeeds.
    private func syncEntitlementToBackend() async {
        for await result in Transaction.currentEntitlements {
            guard let transaction = try? checkVerified(result),
                  Self.productIDs.contains(transaction.productID) else { continue }
            do {
                _ = try await BackendService().validateReceipt(
                    deviceId: AppState.deviceId,
                    signedTransaction: result.jwsRepresentation,
                    environment: transaction.environment.rawValue
                )
            } catch {
                // Don't escalate — local isSubscribed is the source of
                // truth for UI gating; backend will catch up on next sync.
            }
        }
    }

    // MARK: - Transaction Listener

    private func listenForTransactions() {
        transactionListener = Task { [weak self] in
            for await result in Transaction.updates {
                guard let self else { return }
                if let transaction = try? self.checkVerified(result) {
                    await transaction.finish()
                    await self.updateSubscriptionStatus()
                }
            }
        }
    }

    // MARK: - Verification

    private nonisolated func checkVerified<T>(_ result: VerificationResult<T>) throws -> T {
        switch result {
        case .unverified:
            throw StoreError.failedVerification
        case .verified(let safe):
            return safe
        }
    }

    enum StoreError: Error {
        case failedVerification
    }
}
