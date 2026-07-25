import SwiftUI

struct ContentView: View {
    @StateObject private var model = ArchiveViewModel()

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            header
            Divider()
            selectionSection
            Divider()
            destinationSection
            Divider()
            progressSection
            Spacer(minLength: 0)
            footer
        }
        .padding(20)
        .frame(minWidth: 560, minHeight: 620)
        .task {
            model.restoreSavedFolder()
            await model.loadParks()
        }
    }

    // MARK: - Header

    private var header: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text("Park Tile Archiver").font(.title2).bold()
            Text("Archives historical Disney park map tiles from the version list on GitHub.")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }

    // MARK: - Selection

    private var selectionSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("Park").frame(width: 70, alignment: .leading)
                if model.isLoadingParks {
                    ProgressView().controlSize(.small)
                    Text("Loading from GitHub…").foregroundStyle(.secondary)
                } else {
                    Picker("", selection: $model.selectedParkID) {
                        ForEach(model.parkIDs, id: \.self) { id in
                            Text(id.uppercased()).tag(id)
                        }
                    }
                    .labelsHidden()
                }
            }

            HStack {
                Text("Version").frame(width: 70, alignment: .leading)
                if model.isLoadingVersions {
                    ProgressView().controlSize(.small)
                    Text("Loading…").foregroundStyle(.secondary)
                } else {
                    Picker("", selection: $model.selectedVersionCode) {
                        ForEach(model.versions) { version in
                            Text(version.displayName).tag(version.code)
                        }
                    }
                    .labelsHidden()
                    .disabled(model.versions.isEmpty)
                    Toggle("Show inactive", isOn: $model.showInactiveVersions)
                        .toggleStyle(.checkbox)
                }
            }

            if let park = model.park {
                HStack(spacing: 14) {
                    Text("Zooms").frame(width: 70, alignment: .leading)
                    Stepper(value: $model.minZoom, in: model.zoomRangeAvailable) {
                        Text("from \(model.minZoom)").monospacedDigit()
                    }
                    Stepper(value: $model.maxZoom, in: model.zoomRangeAvailable) {
                        Text("to \(model.maxZoom)").monospacedDigit()
                    }
                    Text("(park allows \(park.minZoom)–\(park.maxZoom))")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }

            estimateBox
        }
    }

    private var estimateBox: some View {
        Group {
            if let plan = model.plan {
                VStack(alignment: .leading, spacing: 4) {
                    HStack {
                        Text("\(plan.totalTiles.formatted()) tiles")
                            .bold()
                            .monospacedDigit()
                        Text("≈ \(Format.bytes(Int64(plan.estimatedBytes)))")
                            .foregroundStyle(.secondary)
                    }

                    if plan.totalTiles > 100_000 {
                        Label(
                            "That is a very large job. Consider lowering the maximum zoom.",
                            systemImage: "exclamationmark.triangle"
                        )
                        .font(.caption)
                        .foregroundStyle(.orange)
                    }

                    if !plan.skippedNoBounds.isEmpty {
                        Text("Skipping zoom(s) with no bounds data: "
                             + plan.skippedNoBounds.map(String.init).joined(separator: ", "))
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }

                    if plan.version.urlOverride != nil {
                        Text("This version uses its own tile server, not the park default.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }

                    Text(plan.exampleURL)
                        .font(.system(.caption2, design: .monospaced))
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .truncationMode(.middle)
                        .textSelection(.enabled)
                }
                .padding(10)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Color.secondary.opacity(0.08), in: RoundedRectangle(cornerRadius: 6))
            }
        }
    }

    // MARK: - Destination

    private var destinationSection: some View {
        HStack {
            Text("Folder").frame(width: 70, alignment: .leading)
            Text(model.destinationDescription)
                .lineLimit(1)
                .truncationMode(.head)
                .foregroundStyle(model.destination == nil ? .secondary : .primary)
                .frame(maxWidth: .infinity, alignment: .leading)
            Button("Choose…") { model.chooseFolder() }
                .disabled(model.isDownloading)
        }
    }

    // MARK: - Progress

    private var progressSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            ProgressView(value: model.progressFraction)
                .progressViewStyle(.linear)

            HStack {
                Text("\(model.summary.completed.formatted()) of \(model.totalTiles.formatted()) tiles")
                    .monospacedDigit()
                Spacer()
                Text("\(Int(model.progressFraction * 100))%").monospacedDigit()
            }
            .font(.callout)

            HStack {
                Text("\(Format.bytes(model.summary.bytes)) of ≈\(Format.bytes(model.estimatedTotalBytes))")
                Spacer()
                if model.isDownloading {
                    Text(String(format: "%.0f tiles/s", model.tilesPerSecond)).monospacedDigit()
                    Text("·")
                    Text("\(Format.duration(model.secondsRemaining)) left").monospacedDigit()
                }
            }
            .font(.caption)
            .foregroundStyle(.secondary)

            HStack(spacing: 16) {
                counter("Downloaded", model.summary.downloaded, .green)
                counter("Already there", model.summary.existing, .secondary)
                counter("No imagery", model.summary.missing, .secondary)
                counter("Failed", model.summary.failed,
                        model.summary.failed > 0 ? .red : .secondary)
            }
            .font(.caption)
        }
    }

    private func counter(_ title: String, _ value: Int, _ tint: Color) -> some View {
        VStack(alignment: .leading, spacing: 1) {
            Text(value.formatted()).bold().monospacedDigit().foregroundStyle(tint)
            Text(title).foregroundStyle(.secondary)
        }
    }

    // MARK: - Footer

    private var footer: some View {
        VStack(alignment: .leading, spacing: 10) {
            if let error = model.errorMessage {
                Label(error, systemImage: "xmark.octagon")
                    .font(.caption)
                    .foregroundStyle(.red)
                    .fixedSize(horizontal: false, vertical: true)
            }

            Text(model.statusMessage)
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)

            HStack {
                Text("Missing tiles are normal — the map is not a perfect rectangle.")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                Spacer()
                if model.isDownloading {
                    Button("Stop") { model.cancelDownload() }
                } else {
                    Button("Download") { model.startDownload() }
                        .keyboardShortcut(.defaultAction)
                        .disabled(!model.canDownload)
                }
            }
        }
    }
}

// The classic form rather than the #Preview macro, so this compiles on
// older Xcode versions too.
struct ContentView_Previews: PreviewProvider {
    static var previews: some View {
        ContentView()
    }
}
