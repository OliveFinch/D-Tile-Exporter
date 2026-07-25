import Foundation

/// What happened to one tile.
enum TileOutcome: Sendable {
    /// Fetched from the server.
    case downloaded(bytes: Int)
    /// Already on disk from an earlier run.
    case existing(bytes: Int)
    /// The server says there is no tile here (403/404, or an empty 200).
    /// Park bounds are rectangles but coverage is not, so this is normal.
    case missing
    /// Still failing after every retry.
    case failed(reason: String)
}

struct DownloadSummary: Sendable {
    var downloaded = 0
    var existing = 0
    var missing = 0
    var failed = 0
    var bytes: Int64 = 0
    var wasCancelled = false

    var completed: Int { downloaded + existing + missing + failed }
}

/// Tracks per-run state that several workers touch.
actor DownloadCollector {
    private(set) var summary = DownloadSummary()
    private var knownMissing: Set<String>

    init(knownMissing: Set<String>) {
        self.knownMissing = knownMissing
    }

    func isKnownMissing(_ path: String) -> Bool { knownMissing.contains(path) }

    func record(_ outcome: TileOutcome, path: String) -> DownloadSummary {
        switch outcome {
        case .downloaded(let bytes):
            summary.downloaded += 1
            summary.bytes += Int64(bytes)
        case .existing(let bytes):
            summary.existing += 1
            summary.bytes += Int64(bytes)
        case .missing:
            summary.missing += 1
            knownMissing.insert(path)
        case .failed:
            summary.failed += 1
        }
        return summary
    }

    func missingPaths() -> [String] { knownMissing.sorted() }

    func markCancelled() { summary.wasCancelled = true }
}

/// Downloads a plan's tiles into a folder, politely.
///
/// `@unchecked` only because `URLSession` predates strict concurrency checking.
/// Every stored property here is a `let`, and all mutable state lives in
/// `DownloadCollector`, which is an actor.
final class TileDownloader: @unchecked Sendable {

    struct Options: Sendable {
        var concurrency = 5
        var requestsPerSecond = 10.0
        var maxRetries = 4
        var requestTimeout: TimeInterval = 30
    }

    private let plan: TilePlan
    private let root: URL
    private let options: Options
    private let session: URLSession
    private let limiter: RateLimiter
    private let onProgress: @Sendable (DownloadSummary) async -> Void

    /// Records tiles the server has no imagery for, so a later run does not
    /// ask again. Without this, every resume would re-probe thousands of
    /// known-absent tiles — exactly the impoliteness worth avoiding.
    private static let missingFileName = "_missing-tiles.json"
    private static let manifestFileName = "manifest.json"

    init(
        plan: TilePlan,
        destinationRoot: URL,
        options: Options = Options(),
        onProgress: @escaping @Sendable (DownloadSummary) async -> Void
    ) {
        self.plan = plan
        self.root = destinationRoot
        self.options = options
        self.onProgress = onProgress
        self.limiter = RateLimiter(requestsPerSecond: options.requestsPerSecond)

        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = options.requestTimeout
        configuration.httpMaximumConnectionsPerHost = options.concurrency
        configuration.waitsForConnectivity = true
        self.session = URLSession(configuration: configuration)
    }

    // MARK: - Run

    func run() async throws -> DownloadSummary {
        try prepareDirectories()
        let collector = DownloadCollector(knownMissing: loadMissingPaths())

        await withTaskGroup(of: Void.self) { group in
            var iterator = plan.makeTileIterator()
            var inFlight = 0

            // Prime the pool, then top it up as each task finishes. This keeps
            // exactly `concurrency` requests outstanding without ever building
            // the full tile list in memory.
            while inFlight < options.concurrency, let tile = iterator.next() {
                group.addTask { await self.process(tile, collector: collector) }
                inFlight += 1
            }

            for await _ in group {
                if Task.isCancelled { break }
                guard let tile = iterator.next() else { continue }
                group.addTask { await self.process(tile, collector: collector) }
            }
        }

        if Task.isCancelled {
            await collector.markCancelled()
        }

        let summary = await collector.summary
        let missing = await collector.missingPaths()
        try? saveMissingPaths(missing)
        try? writeManifest(summary: summary)
        await onProgress(summary)
        return summary
    }

    // MARK: - One tile

    private func process(_ tile: Tile, collector: DownloadCollector) async {
        if Task.isCancelled { return }

        let relative = tile.relativePath
        let destination = root.appendingPathComponent(relative)

        // Resume: anything already on disk, or already known to be absent,
        // costs nothing and is never requested again.
        if let size = existingFileSize(destination), size > 0 {
            let summary = await collector.record(.existing(bytes: size), path: relative)
            await onProgress(summary)
            return
        }
        if await collector.isKnownMissing(relative) {
            let summary = await collector.record(.missing, path: relative)
            await onProgress(summary)
            return
        }

        let outcome = await fetch(tile, to: destination)
        let summary = await collector.record(outcome, path: relative)
        await onProgress(summary)
    }

    private func fetch(_ tile: Tile, to destination: URL) async -> TileOutcome {
        guard let url = plan.url(for: tile) else {
            return .failed(reason: "could not build a URL")
        }

        var request = URLRequest(url: url)
        request.setValue(ConfigService.userAgent, forHTTPHeaderField: "User-Agent")
        request.setValue("image/jpeg,image/*;q=0.8", forHTTPHeaderField: "Accept")

        var attempt = 0
        var lastProblem = "unknown error"

        while true {
            if Task.isCancelled { return .failed(reason: "cancelled") }
            await limiter.acquire()

            var retryAfter: TimeInterval?
            do {
                let (data, response) = try await session.data(for: request)
                let http = response as? HTTPURLResponse
                let status = http?.statusCode ?? 0

                if status == 200 && !data.isEmpty {
                    do {
                        try data.write(to: destination, options: .atomic)
                        return .downloaded(bytes: data.count)
                    } catch {
                        return .failed(reason: "could not write file: \(error.localizedDescription)")
                    }
                }

                // 403/404 mean "no tile here", and an empty 200 means the same.
                // These are expected: the bounds are a rectangle, the drawn map
                // is not.
                if status == 403 || status == 404 || (status == 200 && data.isEmpty) {
                    return .missing
                }

                if status == 429 || status >= 500 {
                    lastProblem = "HTTP \(status)"
                    if let header = http?.value(forHTTPHeaderField: "Retry-After"),
                       let seconds = TimeInterval(header) {
                        retryAfter = min(seconds, 60)
                    }
                } else {
                    // Anything else is not worth hammering.
                    return .missing
                }
            } catch let error as URLError where error.code == .cancelled {
                return .failed(reason: "cancelled")
            } catch {
                lastProblem = error.localizedDescription
            }

            attempt += 1
            if attempt > options.maxRetries {
                return .failed(reason: lastProblem)
            }

            // Exponential backoff with jitter, so parallel workers do not
            // resynchronise into a burst after a 429.
            let window = min(pow(2.0, Double(attempt - 1)), 60)
            let delay = retryAfter ?? Double.random(in: (window / 2)...window)
            try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000))
        }
    }

    // MARK: - Filesystem

    private func existingFileSize(_ url: URL) -> Int? {
        let values = try? url.resourceValues(forKeys: [.fileSizeKey])
        return values?.fileSize
    }

    private func prepareDirectories() throws {
        let manager = FileManager.default
        try manager.createDirectory(at: root, withIntermediateDirectories: true)
        for relative in plan.directoryRelativePaths() {
            try manager.createDirectory(
                at: root.appendingPathComponent(relative),
                withIntermediateDirectories: true
            )
        }
    }

    private func loadMissingPaths() -> Set<String> {
        let url = root.appendingPathComponent(Self.missingFileName)
        guard let data = try? Data(contentsOf: url),
              let list = try? JSONDecoder().decode([String].self, from: data)
        else { return [] }
        return Set(list)
    }

    private func saveMissingPaths(_ paths: [String]) throws {
        guard !paths.isEmpty else { return }
        let data = try JSONEncoder().encode(paths)
        try data.write(to: root.appendingPathComponent(Self.missingFileName), options: .atomic)
    }

    private func writeManifest(summary: DownloadSummary) throws {
        var levels: [String: Any] = [:]
        for level in plan.levels {
            levels[String(level.zoom)] = [
                "minX": level.bounds.minX, "maxX": level.bounds.maxX,
                "minY": level.bounds.minY, "maxY": level.bounds.maxY,
                "tiles": level.count,
            ]
        }

        let manifest: [String: Any] = [
            "tool": ["name": "ParkTileArchiver", "version": "1.0"],
            "park": [
                "id": plan.park.id,
                "label": plan.park.name,
                "yScheme": plan.park.yScheme,
            ],
            "version": [
                "code": plan.version.code,
                "label": plan.version.label ?? "",
                "templateOverridden": plan.version.urlOverride != nil,
            ],
            "tileTemplate": plan.template,
            "zoom": [
                "min": plan.levels.first?.zoom ?? 0,
                "max": plan.levels.last?.zoom ?? 0,
            ],
            "boundsByZoom": levels,
            "tiles": [
                "requested": plan.totalTiles,
                "fetched": summary.downloaded + summary.existing,
                "missing": summary.missing,
                "failed": summary.failed,
            ],
            "totalBytes": summary.bytes,
            "complete": !summary.wasCancelled && summary.failed == 0,
            "finished": ISO8601DateFormatter().string(from: Date()),
        ]

        let data = try JSONSerialization.data(
            withJSONObject: manifest,
            options: [.prettyPrinted, .sortedKeys]
        )
        try data.write(to: root.appendingPathComponent(Self.manifestFileName), options: .atomic)
    }
}
