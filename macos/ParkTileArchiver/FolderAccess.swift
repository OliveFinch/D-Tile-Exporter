import Foundation

/// Remembers the folder the user picked, across launches.
///
/// A sandboxed app loses access to a chosen folder when it quits. A
/// security-scoped bookmark is the sanctioned way to get it back, so the
/// destination does not have to be re-picked every time.
enum FolderAccess {
    private static let defaultsKey = "destinationBookmark"

    static func store(_ url: URL) {
        guard let data = try? url.bookmarkData(
            options: .withSecurityScope,
            includingResourceValuesForKeys: nil,
            relativeTo: nil
        ) else { return }
        UserDefaults.standard.set(data, forKey: defaultsKey)
    }

    /// Returns the remembered folder, or nil if there isn't a usable one.
    static func restore() -> URL? {
        guard let data = UserDefaults.standard.data(forKey: defaultsKey) else { return nil }
        var stale = false
        guard let url = try? URL(
            resolvingBookmarkData: data,
            options: .withSecurityScope,
            relativeTo: nil,
            bookmarkDataIsStale: &stale
        ) else { return nil }
        if stale { store(url) }
        return url
    }

    /// Runs `body` with the sandbox permission for `url` held open.
    static func withAccess<T>(to url: URL, _ body: () async throws -> T) async rethrows -> T {
        let opened = url.startAccessingSecurityScopedResource()
        defer { if opened { url.stopAccessingSecurityScopedResource() } }
        return try await body()
    }
}
