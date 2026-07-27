# Park Tile Archiver — the desktop app

A window with four tabs: **Download**, **Library**, **Verify** and
**Bounds check**. It runs
on macOS and Windows from one codebase, and it drives the same `tilearc` engine
the command line uses, so both agree on every number.

---

## Building it

You need **Python 3.11 or newer**. macOS ships 3.9, which is too old:

```bash
brew install python@3.12        # or grab an installer from python.org
```

Then, from the project folder:

```bash
PYTHON=python3.12 ./build-app.sh
```

That creates a virtual environment, installs everything into it, and builds
**`dist/Park Tile Archiver.app`**. Drag it to your Applications folder and
double-click it like anything else. Use `./build-app.sh --run` to open it
straight after building.

If `python3 --version` already reports 3.11 or newer you can drop the `PYTHON=`
prefix.

The first build takes a few minutes, mostly downloading Qt. Later builds are
quick.

### Running it without building

Useful while changing the code, since there's no build step:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[gui]"
tilearc-gui
```

### On Windows

Same idea from PowerShell, with Python 3.11+ from python.org:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[gui]"
pip install pyinstaller
pyinstaller --clean --noconfirm packaging\ParkTileArchiver.spec
```

You get `dist\ParkTileArchiver\ParkTileArchiver.exe`.

### If the app doesn't start

`build-app.sh` launches the app it just built and fails loudly if it can't
start, so a broken bundle should never reach you silently. If one does, run the
executable inside the bundle directly — that prints the real error, which
double-clicking swallows:

```bash
"dist/Park Tile Archiver.app/Contents/MacOS/ParkTileArchiver"
```

To rule out packaging entirely, run the app straight from source:

```bash
source .venv/bin/activate
tilearc-gui
```

If that works and the bundle doesn't, the problem is PyInstaller, not the app.

### If a "Choose folder…" button beach-balls

macOS draws its file and folder panels in a **separate process**, and the app
sits and waits for one. When that service is wedged — a stale network mount in
the sidebar or an unreachable iCloud item is enough — the panel never opens and
the app beach-balls until the request gives up. Nothing in the app is stuck, and
nothing in the app can unstick it.

Two ways past it.

**Don't use the panel.** Every folder in the app can be typed, pasted or
dragged: drop a folder anywhere on the Download, Library or Verify tab and it's
taken as the destination. That works whatever the system panel is doing.

**Or use Qt's own picker**, which is drawn inside the app and never contacts the
service:

```bash
TILEARC_QT_DIALOGS=1 open "dist/Park Tile Archiver.app"
```

To make that the default, set it for your login shell:

```bash
launchctl setenv TILEARC_QT_DIALOGS 1
```

It's plainer than the macOS panel — no sidebar, no favourites — but it opens
immediately.

If the panel hangs for this app it will hang for others. Restarting the Finder
(⌥-right-click the Finder icon → Relaunch) or ejecting a disconnected network
volume usually clears it.

### Gatekeeper on first launch

The app isn't code-signed, so macOS may refuse a plain double-click the first
time. **Right-click the app → Open**, then confirm. Once only. `build-app.sh`
already clears the quarantine flag, which handles most cases.

---

## Using it

### Park data

The bar at the top shows where park configs are being read from. By default
that's the live repo:

```
https://raw.githubusercontent.com/OliveFinch/WDWMap/main
```

**Use local folder…** points it at a checkout instead — handy for testing
edits to `boundsByZoom` before pushing them. Nothing about the parks is built
into the app; the park list, version lists, tile templates and bounds all come
from those files.

### Download

1. **Park** and **Version** populate from the source above. Tick **Show
   retired** to include versions the viewer no longer lists — they usually
   still work, which is the whole reason for archiving them.
2. **Zoom levels** default to the park minimum through z17. Watch the tile
   count: it roughly quadruples per extra level. Anything over 100,000 tiles
   gets a warning.
3. **Coverage** — leave **Only fetch tiles measured to exist** ticked. See
   below.
4. **Save to** — choose a format and a folder. The folder can be browsed for,
   typed, pasted, or dragged onto the window. The exact path that will be
   written is shown underneath; if the folder doesn't exist yet it says so,
   rather than quietly creating a tree at a mistyped path.
5. **Download**.

**Coverage.** The zoom bounds in the park configs are rectangles; the drawn map
is not, and the rectangles are wrong in both directions. Measured against the
real servers, the declared bounds ask for about 37% more tiles than exist and
miss around 1,700 that do. With the box ticked the job is planned from
`tools/measured-coverage.json` — the measured footprint of every zoom of every
park, including the L-shapes and the notches — instead of from the declared
rectangle. The app finds that file on its own; **Choose…** points it at another.
A park the file doesn't cover falls back to its declared bounds and says so in
the estimate.

**Speed.** *Parallel requests* and *Max requests/second* default to 5 and 10,
which is deliberately gentle — a full-depth park at 10 req/s takes many hours.
Raising both together is what actually speeds things up; the rate is the
binding constraint, and raising it without also raising the parallel requests
just leaves workers waiting. Keep the two roughly in proportion.

Leave **Slow down automatically if the server pushes back** ticked. It turns
the rate into a ceiling rather than a fixed speed: on HTTP 429 or 503 the job
halves its rate and creeps back up once the server settles. That way an
optimistic setting costs you time instead of your access.

The progress bar shows tiles done, bytes, throughput and time remaining, with a
breakdown of downloaded / already there / no imagery / failed.

**Formats.** *Library* is the one to use for archiving more than one version of
a park — see the next section. *Folder of tiles* writes `{z}/{x}/{y}.jpg`
exactly as the Disney servers lay them out. *Zip archive* packs that same
structure into one file. *MBTiles database* produces a single file for map
tools — note it stores rows in TMS order, as that format requires.

**Versions served from somewhere else.** A version with its own tile URL is
served from a host that isn't the park's. Sometimes that is the real source;
DLP's `jan2026` is not — it is a copy already downloaded and re-hosted, so
archiving it captures a snapshot of the archive rather than of the map. The
estimate says so in red and the app asks before starting one.

**If a run gets rate-limited.** "No imagery" is normally permanent — the tile
is not coming back, and re-asking every run would waste tens of thousands of
requests. But a server *refusing* requests also answers 403, and that looks
identical. The app now re-requests one of the tiles after a long run of them:
if it arrives, the run was a refusal, the job stops and says so. To recover,
tick **Re-check tiles previously found to have no imagery** and download again
— tiles already saved are kept.

### Stopping and resuming

**Stop** is safe at any point. It lets in-flight tiles finish, saves state, and
leaves everything resumable. Press **Download** again with the same settings and
it carries on: tiles already on disk are skipped, and so are tiles the server
has already said don't exist.

A zip job that was stopped is deliberately **not** packed. It stays as a
`.parts` staging folder until the job actually finishes — a premature archive
would look complete while the state database marked its tiles done, so the
missing ones could never be added later.

Resume state belongs to one job: one park, one version, one zoom range. Press
Download with different settings and the app finds a resume file tracking
something else, so it asks whether to start over. Starting over discards what
that file remembers, not any tiles: everything already on disk is simply
checked again.

### Library

Archiving every version of a park separately stores the same tile once per
version, and most tiles don't change between versions — a new map usually
redraws a corner of one park. WDW alone is 575,490 tiles a version across some
ninety versions; at 25 kB a tile that is 1.3 TB of mostly the same JPEG.

Choose the **Library** format and every version goes into one tree:

```
library/
  catalogue.sqlite
  wdw/
    47/        <- oldest version archived: the full map
      11/555/851.jpg
    105/       <- only the tiles that differ from 47
      17/35712/54688.jpg
```

Download oldest first. A tile is written only when its bytes differ from what
an earlier version already holds, so a later version's folder contains exactly
what that update changed.

Which means the folders lie: `wdw/105/11/0/0.jpg` not existing does **not**
mean version 105 lacks that tile — it means 105 didn't change it. The catalogue
is what says, for every tile of every version, which folder actually holds the
bytes, and this tab is how you read it.

It lists each park and version with how many tiles it has, how many are stored
in its own folder, how many are shared with an earlier version, and whether the
run finished. Underneath is what not storing the copies has saved. **Where
is…** answers the folder question for one tile: type a park, version and
`z`/`x`/`y` and it gives the path on disk, naming the version that holds it
when that isn't the one you asked about. (The *mode* box is for Tokyo only,
whose map exists twice — `daytime` and `nighttime`.)

The tab points itself at the library a download just wrote to; **Choose library
folder…** opens any other, as does typing a path or dropping a folder on it.

**Parks with no version history.** DLP has no selectable servers — it is always
"current". Filing every download of it under `current` would have each one
overwrite the last, so those go into a folder named for the date instead:
`dlp/2026-07-27/`. Download it again in six months and you get a second
snapshot, with only the tiles that changed stored in it.

### Missing tiles are normal

Park bounds are rectangles; the drawn map is not. A 403 or 404 means "no tile
here", so those are counted under **no imagery** rather than treated as errors.
A full WDW job legitimately reports thousands.

### Verify

Point it at a zip, folder or MBTiles archive. It checks every tile is a
complete, readable image and that the counts match the archive's manifest. Deep
checking is on by default — that is what catches a truncated download.

### Bounds check

Runs the same checks as `tilearc doctor`. The zoom bounds in the park configs
are maintained by hand and have drifted, and this lists what looks wrong:

| Check | Meaning |
|---|---|
| `span` | A zoom is less than 2× its parent's width or height |
| `coverage` | The child rectangle doesn't cover the parent's footprint |
| `aspect` | The width:height ratio inverts — the signature of swapped X/Y |
| `zoom-range` | Bounds outside `minZoom`/`maxZoom`, or a zoom with no bounds |

It never edits a config. A rectangle silently widened would add thousands of
404s to every future job; one silently narrowed would drop part of the map
without telling you.

**These checks guess.** `span` and `coverage` assume a pyramid covers the same
ground at every zoom, and WDW does not — it crops as you zoom in, from about
172 km across at z11 to 13 km at z19. Most of what they report for WDW is that
legitimate cropping, which is why the next button exists.

### Measure from server…

Stops guessing and asks. For each zoom it checks the four declared edges
against the real tile server and walks outward or inward until it finds where
the map actually stops.

It tells you the cost and asks first, changes nothing, and hands back a
`boundsByZoom` block to paste into
`parks/{id}/{id}_config.json`. Measuring all of WDW is about 1,100 requests;
probes ask for one byte per tile, not the whole image.

Once that block is committed to the viewer repo, **both apps pick it up** — the
archiver stops requesting tiles that were never there, and the viewer stops
hiding parts of the map that were.

Tokyo Disney Resort can't be measured here: it needs credentials and a proxy.

---

## Tokyo Disney Resort

Pick Tokyo Disney Resort in the Park list and an extra panel appears. TDR has
no public tile URL, so it needs three things the other parks don't:

- **Map** — the resort is drawn twice, daytime and nighttime. "Both" fetches
  two complete sets, so it doubles the job.
- **Fetch via** — the viewer's Cloudflare Worker (the default, and what the
  live site uses) or direct from the origin. Direct needs all three CloudFront
  signed cookies and the mobile-app User-Agent.
- **Credentials** — leave blank to look in the usual places
  (`./tdr_credentials.json`, the `TILEARC_TDR_*` environment variables, then
  the park config), or point at a file. The line under the box says where the
  credentials were found and when they expire.

**Watch the quota.** The Worker is on Cloudflare's free tier — 100,000
requests a day — and it also serves everyone using the live map. Jobs over
10,000 tiles are refused unless you confirm, because a full TDR run is ~138,000
tiles *per mode* and would take the map down for the rest of the day. Fetching
directly bypasses the Worker entirely and doesn't touch that quota.

**If every tile comes back missing,** the cookies have almost certainly
expired. Through the Worker an upstream 403 arrives as `204 No Content`, which
is indistinguishable from a tile that genuinely isn't there — so dead
credentials look like a successful download of nothing. The app checks expiry
before starting and says so in the panel.

---

## Checking it works

The tile arithmetic has known-good answers. Set the zoom range to a park's full
span and compare:

| Park | Full depth | To z18 | To z17 |
|---|---|---|---|
| wdw | 575,450 | 145,370 | 37,850 |
| dlr | 484,008 | 28,840 | 7,336 |
| dlp | 131,064 | 32,760 | 8,184 |
| hkdl | 21,852 | 1,372 | 348 |
| shdr | 12,897 | 1,233 | 633 |

Two more worth a glance, because they're the easiest things to get wrong:

- **DLP ▸ Jan 2026** — the example URL must start `https://pub-…r2.dev/`, not
  `media.disneylandparis.com`. That version overrides the park's tile server.
- **SHDR at z17 only** — must end `/17/26447/7084.jpg`. SHDR's `yScheme` is
  `tms`, but its stored bounds are already in server space; flipping them would
  fetch `/124051.jpg`, a mirrored band from the wrong part of the world.

Both are covered by automated tests (`tests/test_gui.py`), which run headless:

```bash
pip install -e ".[dev]"
pytest tests/test_gui.py
```

---

## What the app doesn't do

Use the `tilearc` command line for:

- **`--bbox`** clipping to a geographic area.
- Scripting anything, via `--json` on most commands.
