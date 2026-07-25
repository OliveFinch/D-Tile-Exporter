import Foundation

/// A global requests-per-second ceiling shared by every worker.
///
/// Concurrency alone does not bound load: five workers against a fast CDN is
/// still hundreds of requests a second. This puts an absolute ceiling on the
/// rate regardless of how many workers run or how fast responses come back.
actor RateLimiter {
    private let interval: TimeInterval
    private var nextSlot = Date.distantPast

    init(requestsPerSecond: Double) {
        interval = requestsPerSecond > 0 ? 1.0 / requestsPerSecond : 0
    }

    func acquire() async {
        guard interval > 0 else { return }
        let now = Date()
        let slot = max(now, nextSlot)
        nextSlot = slot.addingTimeInterval(interval)

        let delay = slot.timeIntervalSince(now)
        if delay > 0 {
            try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000))
        }
    }
}
