import AppKit   // NSOpenPanel
import Foundation
import SwiftUI

@MainActor
final class ArchiveViewModel: ObservableObject {

    // Selection
    @Published var parkIDs: [String] = []
    @Published var selectedParkID: String = "" { didSet { parkChanged() } }
    @Published var park: ParkConfig?
    @Published var versions: [VersionEntry] = []
    @Published var selectedVersionCode: String = "" { didSet { rebuildPlan() } }
    @Published var showInactiveVersions = false { didSet { reloadVersionsList() } }

    @Published var minZoom = 0 { didSet { rebuildPlan() } }
    @Published var maxZoom = 0 { didSet { rebuildPlan() } }

    @Published var destination: URL?
    @Published var plan: TilePlan?

    // Status
    @Published var isLoadingParks = false
    @Published var isLoadingVersions = false
    @Published var isDownloading = false
    @Published var errorMessage: String?
    @Published var statusMessage = "Choose a park to begin."

    // Progress
    @Published var summary = DownloadSummary()
    @Published var tilesPerSecond: Double = 0
    @Published var secondsRemaining: TimeInterval?

    private var downloadTask: Task<Void, Never>?
    private var startedAt: Date?
    private var allVersions: [VersionEntry] = []

    // MARK: - Derived values

    var zoomRangeAvailable: ClosedRange<Int> {
        guard let park else { return 0...0 }
        return park.minZoom...park.maxZoom
    }

    var totalTiles: Int { plan?.totalTiles ?? 0 }

    var estimatedTotalBytes: Int64 {
        guard let plan else { return 0 }
        // Once tiles are arriving, the measured average beats the 25 kB guess.
        let fetched = summary.downloaded + summary.existing
        if fetched > 50 {
            let mean = Double(summary.bytes) / Double(fetched)
            let remaining = max(0, plan.totalTiles - summary.completed)
            return summary.bytes + Int64(mean * Double(remaining))
        }
        return Int64(plan.estimatedBytes)
    }

    var progressFraction: Double {
        guard totalTiles > 0 else { return 0 }
        return min(1, Double(summary.completed) / Double(totalTiles))
    }

    var canDownload: Bool {
        !isDownloading && destination != nil && (plan?.totalTiles ?? 0) > 0
    }

    var destinationDescription: String {
        guard let destination else { return "No folder chosen" }
        guard let plan else { return destination.path }
        return destination.appendingPathComponent(plan.folderName).path
    }

    // MARK: - Loading

    func loadParks() async {
        guard parkIDs.isEmpty else { return }
        isLoadingParks = true
        defer { isLoadingParks = false }

        let ids = await ConfigService.parkIDs()

        // Only offer parks that can actually be fetched. Tokyo Disney Resort
        // has no public tile template — it needs credentials and a proxy — so
        // it drops out here rather than being special-cased by name.
        var usable: [String] = []
        for id in ids {
            if let config = try? await ConfigService.park(id), config.canDownload {
                usable.append(id)
            }
        }

        parkIDs = usable
        if usable.isEmpty {
            errorMessage = "Could not load any parks from GitHub. Check your connection."
        } else if selectedParkID.isEmpty {
            selectedParkID = usable.first ?? ""
        }
    }

    private func parkChanged() {
        guard !selectedParkID.isEmpty else { return }
        Task { await loadPark(selectedParkID) }
    }

    private func loadPark(_ id: String) async {
        isLoadingVersions = true
        defer { isLoadingVersions = false }
        errorMessage = nil

        do {
            let config = try await ConfigService.park(id)
            park = config
            // A sensible default: deep enough to be useful, shallow enough
            // that nobody accidentally starts a 14 GB job.
            minZoom = config.minZoom
            maxZoom = min(17, config.maxZoom)

            allVersions = try await ConfigService.versions(id)
            reloadVersionsList()
            statusMessage = "\(config.name): \(versions.count) version(s) available."
        } catch {
            park = nil
            allVersions = []
            versions = []
            plan = nil
            errorMessage = error.localizedDescription
        }
    }

    private func reloadVersionsList() {
        versions = showInactiveVersions ? allVersions : allVersions.filter(\.active)
        if !versions.contains(where: { $0.code == selectedVersionCode }) {
            selectedVersionCode = versions.first?.code ?? ""
        } else {
            rebuildPlan()
        }
    }

    private func rebuildPlan() {
        guard let park,
              let version = versions.first(where: { $0.code == selectedVersionCode })
        else {
            plan = nil
            return
        }
        guard minZoom <= maxZoom else { plan = nil; return }
        plan = TilePlan.make(park: park, version: version, minZoom: minZoom, maxZoom: maxZoom)
    }

    // MARK: - Folder

    func chooseFolder() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.canCreateDirectories = true
        panel.allowsMultipleSelection = false
        panel.prompt = "Choose"
        panel.message = "Choose where to save the tiles"

        if panel.runModal() == .OK, let url = panel.url {
            destination = url
            FolderAccess.store(url)
        }
    }

    func restoreSavedFolder() {
        if destination == nil { destination = FolderAccess.restore() }
    }

    // MARK: - Download

    func startDownload() {
        guard let plan, let destination else { return }

        let root = destination.appendingPathComponent(plan.folderName)
        isDownloading = true
        errorMessage = nil
        summary = DownloadSummary()
        tilesPerSecond = 0
        secondsRemaining = nil
        startedAt = Date()
        statusMessage = "Downloading to \(root.path)"

        downloadTask = Task { [weak self] in
            guard let self else { return }

            let downloader = TileDownloader(
                plan: plan,
                destinationRoot: root,
                options: TileDownloader.Options()
            ) { [weak self] snapshot in
                await self?.apply(snapshot)
            }

            do {
                let final = try await FolderAccess.withAccess(to: destination) {
                    try await downloader.run()
                }
                await self.finish(final, error: nil)
            } catch {
                await self.finish(nil, error: error)
            }
        }
    }

    func cancelDownload() {
        downloadTask?.cancel()
        statusMessage = "Stopping — finishing the tiles already in flight…"
    }

    private func apply(_ snapshot: DownloadSummary) {
        summary = snapshot
        guard let startedAt else { return }

        // Rate and time remaining reflect tiles actually fetched; tiles skipped
        // on resume are free and would otherwise distort both figures.
        let elapsed = Date().timeIntervalSince(startedAt)
        let fetched = snapshot.downloaded + snapshot.missing + snapshot.failed
        guard elapsed > 0.5, fetched > 0 else { return }

        let rate = Double(fetched) / elapsed
        tilesPerSecond = rate

        let remaining = max(0, totalTiles - snapshot.completed)
        secondsRemaining = rate > 0.01 ? Double(remaining) / rate : nil
    }

    private func finish(_ final: DownloadSummary?, error: Error?) {
        isDownloading = false
        downloadTask = nil
        secondsRemaining = nil

        if let error {
            errorMessage = error.localizedDescription
            statusMessage = "Download stopped."
            return
        }
        guard let final else { return }
        summary = final

        if final.wasCancelled {
            statusMessage = "Stopped. \(final.completed.formatted()) of "
                + "\(totalTiles.formatted()) done — press Download again to carry on."
        } else if final.failed > 0 {
            statusMessage = "Finished with \(final.failed.formatted()) failed tile(s). "
                + "Press Download again to retry just those."
        } else {
            statusMessage = "Done. \(final.downloaded.formatted()) downloaded, "
                + "\(final.existing.formatted()) already present, "
                + "\(final.missing.formatted()) with no imagery."
        }
    }
}

// MARK: - Formatting helpers

enum Format {
    static func bytes(_ value: Int64) -> String {
        let formatter = ByteCountFormatter()
        formatter.countStyle = .file
        return formatter.string(fromByteCount: value)
    }

    static func duration(_ seconds: TimeInterval?) -> String {
        guard let seconds, seconds.isFinite, seconds >= 0 else { return "—" }
        let total = Int(seconds.rounded())
        let hours = total / 3600
        let minutes = (total % 3600) / 60
        let secs = total % 60
        if hours > 0 { return String(format: "%d:%02d:%02d", hours, minutes, secs) }
        return String(format: "%d:%02d", minutes, secs)
    }
}
