# Park Tile Archiver (macOS)

A native SwiftUI app that downloads a chosen park version's map tiles into a
folder, in the same `{z}/{x}/{y}.jpg` layout the Disney servers use.

It reads the park list and version lists straight from the WDWMap repo, so
nothing about the parks is baked into the app.

> **Mac only.** SwiftUI does not run on Windows. If a Windows build is ever
> needed, the `tilearc` Python CLI in the repository root already runs on both
> platforms and shares none of this code.

Tokyo Disney Resort is deliberately not offered: it has no public tile
template and needs credentials plus a proxy. The app hides any park whose
config has no `tileTemplate`, which excludes TDR without naming it.

---

## Building it in Xcode, from scratch

You need Xcode from the Mac App Store. Nothing else.

### 1. Create the project

1. Open Xcode → **Create New Project…** (or **File ▸ New ▸ Project…**)
2. Pick the **macOS** tab at the top, choose **App**, click **Next**
3. Fill in:
   - **Product Name:** `ParkTileArchiver`
   - **Team:** None (fine for running on your own Mac)
   - **Organization Identifier:** anything, e.g. `com.yourname`
   - **Interface:** SwiftUI
   - **Language:** Swift
   - **Storage:** None — and leave the test checkboxes unticked
4. **Next**, choose where to save it, **Create**

### 2. Set the deployment target

Click the blue **ParkTileArchiver** project icon at the top of the left
sidebar → the **ParkTileArchiver** target → **General** tab → set
**Minimum Deployments ▸ macOS** to **13.0**.

### 3. Allow network access and folder writing

Still in the target, open the **Signing & Capabilities** tab. Under
**App Sandbox**:

- tick **Outgoing Connections (Client)** — without it every download fails
- set **User Selected File** to **Read/Write** — without it saving fails

If there is no App Sandbox section, click **+ Capability** and add it.

### 4. Add the source files

Xcode has generated `ParkTileArchiverApp.swift` and `ContentView.swift`.
Replace both, and add the rest:

| File | What to do |
|---|---|
| `ParkTileArchiverApp.swift` | replace contents |
| `ContentView.swift` | replace contents |
| `Models.swift` | new file |
| `ConfigService.swift` | new file |
| `TilePlan.swift` | new file |
| `RateLimiter.swift` | new file |
| `TileDownloader.swift` | new file |
| `FolderAccess.swift` | new file |
| `ArchiveViewModel.swift` | new file |

To add one: right-click the yellow **ParkTileArchiver** folder in the sidebar →
**New File from Template…** → **Swift File** → name it exactly as above →
**Create** → paste the contents from `macos/ParkTileArchiver/`.

The fastest route is to drag all nine files from Finder onto that yellow
folder in Xcode, tick **Copy items if needed**, and let it replace the two
generated ones.

### 5. Run

Press **⌘R**. The window appears, loads the park list from GitHub, and you can
pick a park, a version, a zoom range and a folder.

---

## Using it

1. **Park** and **Version** populate from GitHub. Tick *Show inactive* to see
   versions the viewer no longer lists — they usually still work, which is the
   whole point of archiving them.
2. **Zooms** default to the park minimum through z17. Watch the tile count: it
   roughly quadruples per extra zoom level.
3. **Folder** — tiles land in `<chosen folder>/<park>_<version>/{z}/{x}/{y}.jpg`.
4. **Download** — the progress bar shows tiles done, megabytes, rate and time
   remaining.

**Stop** is safe at any point, and pressing **Download** again resumes: tiles
already on disk are skipped, and so are tiles the server said don't exist
(recorded in `_missing-tiles.json`). A `manifest.json` is written alongside.

### Missing tiles are normal

Park bounds are rectangles; the drawn map is not. A 403 or 404 means "no tile
here", so those are counted under **No imagery** rather than treated as errors.
A WDW job legitimately reports thousands of them.

---

## Checking it works

The tile arithmetic is the part worth verifying, and it has known-good answers.
Set the zoom range to the park's full span and compare the tile count:

| Park | Full depth | To z18 | To z17 |
|---|---|---|---|
| wdw | 575,450 | 145,370 | 37,850 |
| dlr | 484,008 | 28,840 | 7,336 |
| dlp | 131,064 | 32,760 | 8,184 |
| hkdl | 21,852 | 1,372 | 348 |
| shdr | 12,897 | 1,233 | 633 |

If those match, zoom selection and bounds iteration are correct. HKDL to z17 is
348 tiles (~9 MB) — a good first real download.

Two more worth eyeballing, because they are the easiest things to get wrong:

- Select **DLP ▸ Jan 2026**. The example URL under the tile count must start
  with `https://pub-…r2.dev/`, **not** `media.disneylandparis.com`. That
  version overrides the park's tile server.
- Select **SHDR** at z17 only. The example URL must end `/17/26447/7084.jpg`.
  SHDR's `yScheme` is `tms`, but its stored bounds are already in server
  space — flipping them would fetch `/124051.jpg`, a mirrored band from the
  wrong part of the world, with no error to warn you.

---

## If Xcode complains

**Sendable / concurrency errors** — the project is written for Swift 5 language
mode, which is the default. If you switched to Swift 6: target → **Build
Settings** → search `Swift Language Version` → set **5**.

**`windowResizability` unavailable** — the deployment target is below macOS 13.
See step 2.

**Downloads all fail instantly** — the sandbox is blocking the network. See
step 3.

**"The file couldn't be saved"** — *User Selected File* is still Read-only, or
the folder was picked before that setting changed. Fix the entitlement, then
re-pick the folder.

---

## What this app does not do

The `tilearc` CLI in the repository root covers these, and runs on Windows too:

- Tokyo Disney Resort, credentials, and the shared-Worker quota guard
- `--bbox` clipping to a geographic area
- zip and MBTiles output
- the `doctor` bounds checker
- archive verification
