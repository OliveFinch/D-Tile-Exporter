# Park Tile Archiver — the desktop app

A window with three tabs: **Download**, **Verify** and **Bounds check**. It runs
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
3. **Save to** — choose a format and a folder. The exact path that will be
   written is shown underneath.
4. **Download**.

The progress bar shows tiles done, bytes, throughput and time remaining, with a
breakdown of downloaded / already there / no imagery / failed.

**Formats.** *Folder of tiles* writes `{z}/{x}/{y}.jpg` exactly as the Disney
servers lay them out. *Zip archive* packs that same structure into one file.
*MBTiles database* produces a single file for map tools — note it stores rows
in TMS order, as that format requires.

### Stopping and resuming

**Stop** is safe at any point. It lets in-flight tiles finish, saves state, and
leaves everything resumable. Press **Download** again with the same settings and
it carries on: tiles already on disk are skipped, and so are tiles the server
has already said don't exist.

A zip job that was stopped is deliberately **not** packed. It stays as a
`.parts` staging folder until the job actually finishes — a premature archive
would look complete while the state database marked its tiles done, so the
missing ones could never be added later.

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

Note that `span` and `coverage` assume a pyramid covers the same ground at every
zoom, and WDW does not — it crops as you zoom in, from about 172 km across at
z11 to 13 km at z19. Most of what they report for WDW is that legitimate
cropping. The one that isn't is **z12**, whose west edge sits ~17 km east of
every other level, so z13 covers ground z12 doesn't.

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

- **Tokyo Disney Resort** — needs credentials and a proxy, plus the
  shared-quota guard. The app hides any park with no tile template, which
  excludes TDR without special-casing it.
- **`--bbox`** clipping to a geographic area.
- Scripting anything, via `--json` on most commands.
