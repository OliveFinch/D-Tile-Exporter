# tilearc

A polite command-line archiver for historical Disney park map tiles, built for
the [Magic Parks Explorer](https://github.com/OliveFinch/WDWMap) viewer.

Disney periodically replaces the live park map with a new version. Older
versions stay reachable by their version code but are undocumented and could
disappear at any time. `tilearc` snapshots one chosen version into a portable
archive so the history survives.

> **This is an archival tool for a fan project, pointed at someone else's
> production servers.** Politeness is built into the defaults rather than left
> to the operator: low concurrency, a global request-rate ceiling, exponential
> backoff, and resume state good enough that a re-run never re-fetches
> anything — including tiles already known to be absent.

There are two front-ends over the same engine:

* **`tilearc`** — the command line, documented below.
* **Park Tile Archiver** — a desktop app for macOS and Windows. Build it with
  `./build-app.sh`; see **[GUI.md](GUI.md)**.

---

## Install

```bash
pip install -e .          # Python 3.11+, one runtime dependency (httpx)
```

## Quick start

Point the tool at a checkout of the viewer repo — park data is never hardcoded.

```bash
export TILEARC_CONFIG_DIR=~/src/WDWMap        # or pass --config-dir / --config-url

tilearc versions --park wdw                                # what's archivable
tilearc estimate --park wdw --version 801755166 --max-zoom 20   # counts + size
tilearc doctor  --park wdw                                 # bounds sanity check
tilearc download --park wdw --version 801755166 --min-zoom 11 --max-zoom 17
tilearc verify  wdw_801755166.zip
```

More:

```bash
tilearc download --park hkdl --version 19 --all-zooms --format mbtiles
tilearc download --park wdw --version 801755166 --bbox -81.60,28.34,-81.51,28.42 --max-zoom 19
tilearc download --park tdr --version 20260122183830 --mode both --max-zoom 17
```

## Commands

| Command | What it does |
|---|---|
| `versions` | List known version codes for a park, with any per-version URL override |
| `estimate` | Exact tile count and projected size. Touches no tile server |
| `doctor` | Report suspicious `boundsByZoom` data. Never rewrites anything |
| `discover` | Measure the real tile bounds by probing the server |
| `download` | Archive one version. Resumable, rate-limited, progress-reporting |
| `verify` | Integrity-check a zip, directory, or mbtiles archive |

Every command takes `--json` for scripting (except `download`).

## Output formats

`--format zip` (default), `dir`, or `mbtiles`.

Zip and directory output use the standard slippy-map layout, so other tools can
read them directly:

```
{park}_{version}/{z}/{x}/{y}.jpg
{park}_{version}/manifest.json
```

`-o` names the artefact itself for every format — `-o out` with `--format dir`
writes the tiles into `out/`, not `out/{park}_{version}/`. Without `-o` the
default is `{park}_{version}` with the appropriate extension.

With `--mode both` (TDR only) a `{mode}/` level is inserted above `{z}`.

`manifest.json` records the park, version code and label, resolved tile
template, zoom range, the bounds actually used, tile counts
(requested/fetched/missing/failed), total bytes, start and finish timestamps,
and the tool version.

## Politeness

| Knob | Default | Notes |
|---|---|---|
| `--concurrency` | 5 | Warns above 10 |
| `--rps` | 10 | Global token bucket; `0` disables |
| `--retries` | 5 | Exponential backoff with jitter, honours `Retry-After` |
| `--max-tiles` | 250,000 | Refuses larger jobs without `--force` |
| `--timeout` | 30s | Per request |

* A descriptive `User-Agent` identifies the tool and links back to this repo.
* Retries apply only to 429/5xx/timeouts. **403 and 404 are not errors** — park
  bounds are rectangular but coverage is not, so gaps are expected. Missing
  tiles are recorded once and never requested again, on this run or any future
  one.
* `download` prints the full plan and asks for confirmation before fetching
  anything (`--yes` to skip, `--dry-run` to stop before it).

### Resume

Job state lives in a small sqlite DB written beside the output
(`{output}.tilearc-state.sqlite`). Re-running the same command picks up exactly
where it stopped. Ctrl-C finishes in-flight tiles, flushes state, and exits 130.

Zip jobs stage tiles in `{output}.parts/` and pack the archive in a final pass.
That is deliberate: an interrupted job leaves a staging directory rather than a
half-written zip, and — importantly — an unfinished job is **never** packed. A
premature archive would look complete while the state DB marked its tiles done,
so the missing ones could never be added on resume.

The state DB is keyed to the tile selection (park, version, zooms, bbox, mode),
not to politeness settings, so you can retune `--concurrency` or `--rps` between
runs and still resume.

## Park data

Configs are read from `parks/{id}/{id}_config.json` and version lists from
`parks/{id}/{id}_dis_servers.json`, via either:

* `--config-dir` / `$TILEARC_CONFIG_DIR` — a local checkout (accepts the repo
  root or the `parks/` directory), or
* `--config-url` / `$TILEARC_CONFIG_URL` — the live site.

### Things that are easy to get wrong

**A version entry may override the park template.** Entries in
`{id}_dis_servers.json` normally look like `{code, label, active}`, but may
carry a `url` that takes precedence over the park's `tileTemplate` — DLP's
`jan2026` points at an R2 bucket rather than Disney's CDN. `tilearc` honours the
override and says so in the plan output.

**`yScheme: "tms"` (SHDR) does not mean "flip the bounds".** The Y written into
a TMS URL is `(2**z - 1) - y_xyz`, but `boundsByZoom` min/max Y are *already
stored in server space*. Iterating them directly needs no flip. The flip happens
in exactly two places, both conversions to or from another coordinate space:
`--bbox` (geographic input) and MBTiles output (the format mandates TMS rows).
Getting this backwards silently downloads the wrong band of the map — valid
JPEGs, wrong imagery, no error anywhere.

**`minZoom`/`maxZoom` and `boundsByZoom` disagree, in both directions.** A zoom
is downloaded only if it is inside the park's zoom range *and* has a bounds
entry. Every park exercises this: SHDR declares `maxZoom: 21` with no z21
bounds and carries z9–z13 bounds below its `minZoom: 14`; TDR has a z15 entry
below `minZoom: 16`; DLP has a z20 entry above `maxZoom: 19`. Skipped zooms are
reported, not silently dropped.

**Version codes are opaque and not filesystem-safe.** WDW uses `47` and
`801755166`, DLP uses `jan2026`, TDR a 14-digit timestamp. They are sanitised
before use in paths, with a short digest appended whenever sanitising changed
the string, so distinct codes can never collide on one path.

**Bounds are hand-maintained and imperfect.** See `doctor` below. `tilearc`
reports problems and leaves the fix to you — it never silently "corrects"
bounds, because a widened rectangle quietly adds thousands of 404s and a
narrowed one quietly omits part of the map.

## `doctor`

Checks each park's `boundsByZoom` and reports, without changing anything:

| Rule | Meaning |
|---|---|
| `span` | A zoom's width/height is less than 2× its parent's. One tile subdivides into four, so this always indicates bad data |
| `coverage` | The child rectangle does not cover the parent's footprint (`min*2 .. max*2+1`). Pinpoints off-by-one data entry |
| `aspect` | The width:height ratio inverts between adjacent zooms — the signature of X and Y swapped on entry. Only reported when both levels are meaningfully non-square |
| `zoom-range` | Bounds keys outside `[minZoom, maxZoom]`, or zooms in range with no bounds entry |

Real findings in the current viewer configs include WDW z12 (x-span 8 where z11
is 10), WDW z15 (y-span shrinks from 40 to 28), WDW z19 (480×224 → 192×448, an
aspect inversion consistent with swapped axes), SHDR z17 (shrinks in both
dimensions), and TDR (systematically one tile short on max X and max Y at every
level — `max*2` where it should be `max*2+1`).

`--strict` exits non-zero when errors are found.

## `discover`

`doctor` guesses from geometry. `discover` asks the server:

```bash
tilearc discover --park wdw --version 801755166           # print the result
tilearc discover --park wdw --version 801755166 --write   # update the config
```

For each zoom it verifies the four declared edges, walking outward if there are
tiles beyond them and inward if the edge is empty. Because it is anchored on
the declared bounds rather than searching from scratch, confirming a correct
config is cheap — measuring all ten WDW zoom levels costs about 1,100 requests,
and probes use `Range: bytes=0-0` so no whole tile is ever downloaded. It prints
the cost and asks before sending anything.

`--write` replaces `boundsByZoom` in the park config, preserving the file's
compact one-line style so the diff shows only what changed, and records a
`boundsMeasured` stamp with the date and the version used. Both this tool and
the viewer read that file, so a measured config fixes both at once.

What it can't tell you is coverage *inside* the rectangle. The bounding box is
measurable; holes within it are normal and stay normal.

## Tokyo Disney Resort

TDR has no public tile template. Tiles come from

```
https://contents-portal.tokyodisneyresort.jp/limited/map-image/{serverId}/{mode}/z{z}/{x}_{y}.jpg
```

— note the `z` prefix on the zoom directory and the `{x}_{y}.jpg` filename — and
require a spoofed mobile-app `User-Agent`, a `Referer`, and three time-limited
CloudFront signed cookies. By default `tilearc` routes through the viewer's
Cloudflare Worker, exactly as the live site does; `--direct` hits the origin
instead and needs the full cookie set.

**Credentials are never committed.** Copy `tdr_credentials.example.json` to
`tdr_credentials.json` (gitignored) or use the `TILEARC_TDR_*` environment
variables. When they lapse you get one clear *"credentials expired"* message
rather than a wall of 403s — including in worker mode, where an upstream 403 is
indistinguishable from a missing tile until you notice that *everything* is
coming back empty.

**`mode`** is `daytime` or `nighttime` — two distinct tile sets for the same
version. `--mode both` fetches each into its own subtree (or, for mbtiles, its
own file, since one database cannot hold two tile sets at the same coordinates).

**Missing tiles arrive as HTTP 204 with an empty body**, not 404, because that
is what the worker returns for an upstream 403/404. 204 and zero-length bodies
are treated as missing.

### Shared quota

The worker runs on Cloudflare's free tier — **100,000 requests/day**, shared
with everyone using the live map. A full TDR job is ~138,000 tiles *per mode*
and would exhaust the day's quota outright.

Worker-routed jobs are therefore capped at **10,000 tiles** (`--worker-max-tiles`),
separately from the general `--max-tiles` cap. Exceeding it requires `--force`
and prints what fraction of the daily quota the run would consume.

## Scale reference

Per version, at ~25 kB/tile. `estimate` reproduces these exactly from the
current configs.

| Park | Full depth | To z18 | To z17 |
|---|---|---|---|
| wdw | 575,450 (~14 GB) | 145,370 (~3.6 GB) | 37,850 (~950 MB) |
| dlr | 484,008 (~12 GB) | 28,840 (~720 MB) | 7,336 (~180 MB) |
| tdr | 137,645/mode (~3.4 GB) | 8,683 (~220 MB) | 2,122 (~53 MB) |
| dlp | 131,064 (~3.3 GB) | 32,760 (~820 MB) | 8,184 (~205 MB) |
| hkdl | 21,852 (~550 MB) | 1,372 (~34 MB) | 348 (~9 MB) |
| shdr | 12,897 (~320 MB) | 1,233 (~31 MB) | 633 (~16 MB) |

WDW alone has ~90 active versions; archiving everything is not a goal. The tool
targets one chosen version per run.

## Tests

```bash
pip install -e ".[dev]"
pytest
```

No test touches the network. HTTP is driven by `httpx.MockTransport`, and the
park fixtures in `tests/fixtures/parks/` are a snapshot of the real viewer
configs (with TDR credentials replaced by placeholders), so the URL-building and
bounds-iteration logic is tested against the actual quirks — including the TMS
handling, the per-version `url` override, and every anomaly `doctor` reports.
See `tests/fixtures/README.md`.

`tests/test_gui.py` builds the desktop app's real widgets under Qt's
`offscreen` platform, so the window is constructed and interrogated without a
display. It also guards the packaging invariants — PyInstaller's entry point
must avoid relative imports, or the bundle builds happily with no Qt in it and
dies on launch. Skipped automatically if the `gui` extra isn't installed.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | General failure, or some tiles failed after retries |
| 2 | Config problem |
| 3 | Credentials missing or expired |
| 4 | Tile cap or shared quota would be exceeded |
| 5 | State DB belongs to a different job |
| 6 | Archive failed verification |
| 130 | Interrupted (state saved; re-run to continue) |
