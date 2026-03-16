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
