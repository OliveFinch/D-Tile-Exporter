import Foundation

struct ZoomLevel: Identifiable, Sendable {
    let zoom: Int
    let bounds: TileBounds
    var id: Int { zoom }
    var count: Int { bounds.count }
}

/// Everything needed to enumerate and address one version's tiles.
struct TilePlan: Sendable {
    let park: ParkConfig
    let version: VersionEntry
    let levels: [ZoomLevel]
    /// Zooms inside the park's range that have no boundsByZoom entry.
    let skippedNoBounds: [Int]
    /// The resolved template with `{code}` already substituted.
    let template: String

    static let bytesPerTileEstimate = 25_000

    var totalTiles: Int { levels.reduce(0) { $0 + $1.count } }
    var estimatedBytes: Int { totalTiles * Self.bytesPerTileEstimate }

    /// Folder name for this version, e.g. `wdw_801755166`.
    var folderName: String {
        "\(sanitize(park.id))_\(sanitize(version.code))"
    }

    // MARK: - Building

    /// Choose the zooms to download.
    ///
    /// A zoom is included only if it is inside the park's own
    /// `[minZoom, maxZoom]` **and** has a `boundsByZoom` entry. The two
    /// disagree in both directions in the real data:
    ///
    ///   * SHDR declares `maxZoom: 21` but has no z21 bounds, and carries
    ///     z9–z13 bounds below its `minZoom: 14`.
    ///   * DLP has a z20 entry above its `maxZoom: 19`.
    ///
    /// Honouring only one of the two silently downloads the wrong set.
    static func make(
        park: ParkConfig,
        version: VersionEntry,
        minZoom: Int,
        maxZoom: Int
    ) -> TilePlan {
        let low = max(minZoom, park.minZoom)
        let high = min(maxZoom, park.maxZoom)

        var levels: [ZoomLevel] = []
        var skipped: [Int] = []
        if low <= high {
            for zoom in low...high {
                if let bounds = park.boundsByZoom[zoom] {
                    levels.append(ZoomLevel(zoom: zoom, bounds: bounds))
                } else {
                    skipped.append(zoom)
                }
            }
        }

        // A version's own `url` takes precedence over the park template.
        // DLP's `jan2026` uses this to point at an R2 bucket rather than
        // Disney's CDN; ignoring it archives the wrong imagery.
        let base = version.urlOverride ?? park.tileTemplate ?? ""
        // DLP's park template has no `{code}` at all, which is fine — the
        // substitution is simply a no-op there.
        let resolved = base.replacingOccurrences(of: "{code}", with: version.code)

        return TilePlan(
            park: park,
            version: version,
            levels: levels,
            skippedNoBounds: skipped,
            template: resolved
        )
    }

    // MARK: - Addressing

    /// Build a tile URL.
    ///
    /// Y is used exactly as it appears in `boundsByZoom`, with no flip — even
    /// for SHDR, whose `yScheme` is `"tms"`. Those stored values are *already*
    /// TMS rows, so flipping them here would fetch a mirrored band from the
    /// wrong part of the world: valid JPEGs, wrong map, and no error anywhere
    /// to tell you. A flip would only be needed to convert to or from
    /// geographic coordinates, which this app never does.
    func url(for tile: Tile) -> URL? {
        let text = template
            .replacingOccurrences(of: "{z}", with: String(tile.z))
            .replacingOccurrences(of: "{x}", with: String(tile.x))
            .replacingOccurrences(of: "{y}", with: String(tile.y))
        return URL(string: text)
    }

    /// A preview of the URL shape, for display in the UI.
    var exampleURL: String {
        guard let first = levels.first else { return template }
        let tile = Tile(z: first.zoom, x: first.bounds.minX, y: first.bounds.minY)
        return url(for: tile)?.absoluteString ?? template
    }

    // MARK: - Enumeration

    /// Iterate column-major (x outer, y inner) so every tile in a run shares
    /// the same `{z}/{x}/` directory.
    func makeTileIterator() -> TileIterator { TileIterator(levels: levels) }

    struct TileIterator: IteratorProtocol, Sendable {
        private let levels: [ZoomLevel]
        private var levelIndex = 0
        private var xOffset = 0
        private var yOffset = 0

        init(levels: [ZoomLevel]) { self.levels = levels }

        mutating func next() -> Tile? {
            while levelIndex < levels.count {
                let level = levels[levelIndex]
                let bounds = level.bounds
                let x = bounds.minX + xOffset

                if x > bounds.maxX {
                    levelIndex += 1
                    xOffset = 0
                    yOffset = 0
                    continue
                }

                let y = bounds.minY + yOffset
                yOffset += 1
                if bounds.minY + yOffset > bounds.maxY {
                    yOffset = 0
                    xOffset += 1
                }
                return Tile(z: level.zoom, x: x, y: y)
            }
            return nil
        }
    }

    /// Every `{z}/{x}` directory the job will write into. Small enough to
    /// create up front — a full WDW job needs about 1,600 of them — which
    /// keeps the download loop free of filesystem checks.
    func directoryRelativePaths() -> [String] {
        var paths: [String] = []
        for level in levels {
            for x in level.bounds.minX...level.bounds.maxX {
                paths.append("\(level.zoom)/\(x)")
            }
        }
        return paths
    }
}

/// Version codes are opaque upstream strings and are not guaranteed to be
/// safe as a path component.
private func sanitize(_ value: String) -> String {
    let allowed = Set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
    let cleaned = String(value.map { allowed.contains($0) ? $0 : "_" })
        .trimmingCharacters(in: CharacterSet(charactersIn: "._"))
    return cleaned.isEmpty ? "unknown" : cleaned
}
