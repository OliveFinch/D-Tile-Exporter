import SwiftUI

@main
struct ParkTileArchiverApp: App {
    var body: some Scene {
        WindowGroup {
            ContentView()
        }
        .windowResizability(.contentMinSize)
        .commands {
            // A download tool has no use for a New Window item.
            CommandGroup(replacing: .newItem) {}
        }
    }
}
