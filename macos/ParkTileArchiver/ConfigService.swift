import Foundation

/// Loads park configs and version lists straight from the WDWMap repo.
///
/// Note the host: `raw.githubusercontent.com`, not `github.com`. The
/// `github.com/.../tree/main/parks` URL serves an HTML page, not JSON.
enum ConfigService {

    static let rawBase = "https://raw.githubusercontent.com/OliveFinch/WDWMap/main"
    static let listingAPI = "https://api.github.com/repos/OliveFinch/WDWMap/contents/parks"

    /// Used when the GitHub API listing is unavailable (it is rate-limited to
    /// 60 requests/hour for unauthenticated callers).
    static let fallbackParkIDs = ["wdw", "dlr", "hkdl", "shdr", "dlp", "tdr"]

    static let userAgent =
        "ParkTileArchiver/1.0 (Magic Parks Explorer historical map archiver)"

    private static func get(_ urlString: String, describing what: String) async throws -> Data {
        guard let url = URL(string: urlString) else {
            throw ConfigError.transport("Malformed URL: \(urlString)")
        }
        var request = URLRequest(url: url)
        request.setValue(userAgent, forHTTPHeaderField: "User-Agent")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.cachePolicy = .reloadIgnoringLocalCacheData

        let (data, response): (Data, URLResponse)
        do {
            (data, response) = try await URLSession.shared.data(for: request)
        } catch {
            throw ConfigError.transport(
                "Could not reach GitHub while loading \(what): \(error.localizedDescription)"
            )
        }
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        guard status == 200 else { throw ConfigError.badResponse(what, status) }
        return data
    }

    /// Directory listing of `parks/`, so no park IDs are hardcoded in the app.
    static func parkIDs() async -> [String] {
        struct Entry: Decodable {
            let name: String
            let type: String
        }
        do {
            let data = try await get(listingAPI, describing: "the park list")
            let entries = try JSONDecoder().decode([Entry].self, from: data)
            let ids = entries.filter { $0.type == "dir" }.map(\.name).sorted()
            return ids.isEmpty ? fallbackParkIDs : ids
        } catch {
            return fallbackParkIDs
        }
    }

    static func park(_ id: String) async throws -> ParkConfig {
        let data = try await get(
            "\(rawBase)/parks/\(id)/\(id)_config.json",
            describing: "the \(id) config"
        )
        return try ParkConfig(id: id, jsonData: data)
    }

    static func versions(_ id: String) async throws -> [VersionEntry] {
        let data = try await get(
            "\(rawBase)/parks/\(id)/\(id)_dis_servers.json",
            describing: "the \(id) version list"
        )
        return try JSONDecoder().decode([VersionEntry].self, from: data)
    }
}
