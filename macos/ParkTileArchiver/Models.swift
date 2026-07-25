import Foundation

// MARK: - Tile geometry

/// An inclusive tile rectangle at one zoom level, in *server* Y space.
///
/// "Server space" means the Y values are exactly what the tile server expects
/// in the URL. For a `yScheme: "tms"` park (SHDR) they are already TMS rows and
/// must NOT be flipped again. See `TilePlan` for why this matters.
struct TileBounds: Equatable, Sendable {
    let minX: Int
    let maxX: Int
    let minY: Int
    let maxY: Int

    var width: Int { maxX - minX + 1 }
    var height: Int { maxY - minY + 1 }
    var count: Int { width * height }
}

struct Tile: Sendable, Hashable {
    let z: Int
    let x: Int
    let y: Int

    /// Standard slippy-map path, matching the layout on the Disney server.
    var relativePath: String { "\(z)/\(x)/\(y).jpg" }
}

// MARK: - Park

struct ParkConfig: Sendable, Identifiable {
    let id: String
    let name: String
    let tileTemplate: String?
    let minZoom: Int
    let maxZoom: Int
    let yScheme: String
    let boundsByZoom: [Int: TileBounds]

    var isTMS: Bool { yScheme.lowercased() == "tms" }

    /// Tokyo Disney Resort has no public template — it needs credentials and a
    /// proxy, which this app deliberately does not handle. Parks without a
    /// template are simply not offered.
    var canDownload: Bool { tileTemplate?.isEmpty == false }
}

struct VersionEntry: Sendable, Identifiable, Hashable {
    let code: String
    let label: String?
    let active: Bool
    /// A per-version template that overrides the park's `tileTemplate`.
    let urlOverride: String?

    var id: String { code }

    var displayName: String {
        if let label, !label.isEmpty { return "\(label) — \(code)" }
        return code
    }
}

// MARK: - Decoding
//
// These files are hand-maintained, so decoding is a little forgiving:
// `active` is written as 1/0 rather than true/false, and a `code` could
// plausibly appear as a number rather than a string.

private struct BoundsDTO: Decodable {
    let minX: Int
    let maxX: Int
    let minY: Int
    let maxY: Int
}

private struct ParkConfigDTO: Decodable {
    let parkId: String?
    let name: String?
    let tileTemplate: String?
    let minZoom: Int?
    let maxZoom: Int?
    let yScheme: String?
    let boundsByZoom: [String: BoundsDTO]
}

enum ConfigError: LocalizedError {
    case emptyBounds(String)
    case badResponse(String, Int)
    case transport(String)

    var errorDescription: String? {
        switch self {
        case .emptyBounds(let park):
            return "\(park): the config has no boundsByZoom entries."
        case .badResponse(let what, let code):
            return "Could not load \(what) (HTTP \(code))."
        case .transport(let message):
            return message
        }
    }
}

extension ParkConfig {
    init(id: String, jsonData: Data) throws {
        let dto = try JSONDecoder().decode(ParkConfigDTO.self, from: jsonData)

        var bounds: [Int: TileBounds] = [:]
        for (key, value) in dto.boundsByZoom {
            guard let zoom = Int(key) else { continue }
            bounds[zoom] = TileBounds(
                minX: value.minX, maxX: value.maxX,
                minY: value.minY, maxY: value.maxY
            )
        }
        guard !bounds.isEmpty else { throw ConfigError.emptyBounds(id) }

        self.id = dto.parkId ?? id
        self.name = dto.name ?? id.uppercased()
        self.tileTemplate = dto.tileTemplate
        self.minZoom = dto.minZoom ?? bounds.keys.min()!
        self.maxZoom = dto.maxZoom ?? bounds.keys.max()!
        self.yScheme = dto.yScheme ?? "xyz"
        self.boundsByZoom = bounds
    }
}

extension VersionEntry: Decodable {
    private enum CodingKeys: String, CodingKey {
        case code, label, active, url
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)

        if let text = try? container.decode(String.self, forKey: .code) {
            code = text
        } else if let number = try? container.decode(Int.self, forKey: .code) {
            code = String(number)
        } else {
            throw DecodingError.dataCorruptedError(
                forKey: .code, in: container,
                debugDescription: "version entry has no usable code"
            )
        }

        label = try? container.decode(String.self, forKey: .label)

        // Written as 1/0 in the viewer's files.
        if let flag = try? container.decode(Bool.self, forKey: .active) {
            active = flag
        } else if let number = try? container.decode(Int.self, forKey: .active) {
            active = number != 0
        } else {
            active = true
        }

        let overrideURL = try? container.decode(String.self, forKey: .url)
        urlOverride = (overrideURL?.isEmpty == false) ? overrideURL : nil
    }
}
