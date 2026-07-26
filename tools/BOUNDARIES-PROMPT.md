# Actual tile boundaries for the Disney park maps

These are the tile ranges the Disney CDNs actually serve, measured directly
against the live servers on 2026-07-26. They are here so Magic Parks Explorer
knows what to expect: which tiles will return imagery, and which will 404.

Use them to skip requests that cannot succeed, and to build bounding regions
for each park and zoom.

They are measurements, not the values in the park config files — the two differ
in places. Nothing below needs any config to change; it is a description of what
the servers do.

## Reading the numbers

- **Server tile space.** These are the values that go straight into the tile
  URL's `{x}` and `{y}`.
- **Shanghai (`shdr`) is `yScheme: "tms"`.** Its Y values below are already TMS
  rows. They go into the URL as-is — do not flip them.
- **Ranges are inclusive** at both ends.
- **Most zooms are a plain rectangle**, fully described by their x and y range.
- **Five zooms are not.** For those the rectangle contains tiles that 404, so
  explicit row runs are given underneath the table. Hong Kong is the strongest
  case: 15% of its bounding box at z19 and z20 is not served.
- **Four declared zooms serve nothing at all** and are marked as such.

## Where this applies in the app

- **Before requesting a tile**, test it against the range for that park and
  zoom. Rectangles are four comparisons; for the five irregular zooms, find the
  row group containing `y` and test `x` against its run.
- **For bounding regions**, the x/y ranges give each zoom's extent directly. The
  irregular zooms have a true extent smaller than their box, so the runs matter
  if the region needs to be tight.
- **For tile counts** — progress, estimates, cache sizing — use the `tiles`
  column rather than multiplying the box out. For the irregular zooms those two
  numbers are not the same.

## Totals

**44 zooms across five parks, 1,097,930 tiles served in total.**

## The boundaries

### Walt Disney World — `wdw` — 575,490 tiles

| zoom | x | y | tiles |
|---|---|---|---|
| z11 | 555–564 | 851–859 | 90 |
| z12 | 1114–1125 | 1706–1715 | 120 |
| z13 | 2228–2251 | 3412–3431 | 480 |
| z14 | 4456–4503 | 6824–6863 | 1,920 |
| z15 | 8928–8987 | 13672–13699 | 1,680 |
| z16 | 17856–17975 | 27344–27399 | 6,720 |
| z17 | 35712–35951 | 54688–54799 | 26,880 |
| z18 | 71424–71903 | 109376–109599 | 107,520 |
| z19 | 143264–143455 | 218752–219199 | 86,016 |
| z20 | 286528–286911 | 437504–438399 | 344,064 |

### Disneyland Resort (California) — `dlr` — 464,168 tiles

| zoom | x | y | tiles |
|---|---|---|---|
| z14 | 2818–2831 | 6549–6560 | 168 |
| z15 | 5636–5663 | 13102–13117 | 448 |
| z16 | 11272–11327 | 26208–26231 | 1,344 |
| z17 | 22544–22655 | 52416–52463 | 5,376 |
| z18 | 45088–45311 | 104832–104927 | 21,504 |
| z19 | 90176–90623 | 209664–209855 | 86,016 |
| z20 | 180352–181247 | 419328–419752 | 349,312 — **not a full rectangle, see runs** |

`dlr` z20 — the box above contains 31,488 tiles that are not served (8% of it). Served rows are:

```
y 419328–419711      x 180352–181247
y 419712–419752      x 180736–180863
```

### Disneyland Paris — `dlp` — 36,014 tiles

| zoom | x | y | tiles |
|---|---|---|---|
| z13 | 4158–4160 | 2816–2819 | 12 |
| z14 | 8316–8321 | 5633–5639 | 42 |
| z15 | 16633–16642 | 11266–11278 | 130 |
| z16 | 33267–33285 | 22532–22556 | 475 |
| z17 | 66535–66571 | 45065–45112 | 1,776 |
| z18 | 133071–133142 | 90130–90224 | 6,774 — **not a full rectangle, see runs** |
| z19 | 266142–266285 | 180261–180449 | 26,805 — **not a full rectangle, see runs** |
| z20 | — | — | **0 — nothing is served at this zoom** |

`dlp` z18 — the box above contains 66 tiles that are not served (1% of it). Served rows are:

```
y 90130              x 133137–133142
y 90131–90224        x 133071–133142
```

`dlp` z19 — the box above contains 411 tiles that are not served (2% of it). Served rows are:

```
y 180261             x 266274–266284
y 180262–180307      x 266142–266284
y 180308–180397      x 266143–266284
y 180398–180449      x 266143–266285
```

### Hong Kong Disneyland — `hkdl` — 9,332 tiles

| zoom | x | y | tiles |
|---|---|---|---|
| z14 | 13380–13383 | 7148–7150 | 12 |
| z15 | 26762–26765 | 14297–14300 | 16 |
| z16 | 53524–53531 | 28594–28601 | 64 |
| z17 | 107048–107063 | 57188–57203 | 256 |
| z18 | 214096–214127 | 114376–114407 | 1,024 |
| z19 | 428207–428242 | 228756–228807 | 1,592 — **not a full rectangle, see runs** |
| z20 | 856414–856485 | 457512–457615 | 6,368 — **not a full rectangle, see runs** |

`hkdl` z19 — the box above contains 280 tiles that are not served (15% of it). Served rows are:

```
y 228756–228769      x 428207–428222
y 228770–228807      x 428207–428242
```

`hkdl` z20 — the box above contains 1,120 tiles that are not served (15% of it). Served rows are:

```
y 457512–457539      x 856414–856445
y 457540–457615      x 856414–856485
```

### Shanghai Disney Resort — `shdr` — 12,926 tiles

| zoom | x | y | tiles |
|---|---|---|---|
| z9 | — | — | **0 — nothing is served at this zoom** |
| z10 | — | — | **0 — nothing is served at this zoom** |
| z11 | — | — | **0 — nothing is served at this zoom** |
| z12 | 825–827 | 220–222 | 9 |
| z13 | 1651–1655 | 441–444 | 20 |
| z14 | 3302–3310 | 882–889 | 72 |
| z15 | 6609–6617 | 1768–1776 | 81 |
| z16 | 13218–13235 | 3536–3553 | 324 |
| z17 | 26447–26459 | 7084–7095 | 156 |
| z18 | 52895–52919 | 14168–14191 | 600 |
| z19 | 105791–105839 | 28336–28383 | 2,352 |
| z20 | 211583–211679 | 56672–56767 | 9,312 |

## How these were measured, and the one limit

Each zoom's coverage border was walked tile by tile rather than sampled or
inferred, and everything inside a closed border is served. Each result was then
audited by re-requesting tiles the walk had recorded as absent, along with tiles
it had seen present as a control — a throttled CDN answers 403, and an `<img>`
load cannot tell that from a 404, so a walk made during a refusal closes early
and looks perfectly tidy. Every figure above comes from a walk that passed that
audit, and the five irregular zooms were each confirmed by two independent runs
agreeing to the tile.

The interior was never probed, only the border. So a region of missing tiles
sealed entirely inside a footprint — with no path to the outside — would be
counted as served, because an outer border cannot see one. None was detected:
every zoom came back as a single region with no border nested inside another.
That is evidence rather than proof, so if the app sees a persistent cluster of
404s well inside one of these ranges, that is what it would look like.

Version codes matter: these footprints are for the current version of each map.
Archived versions of the same park may cover different ground.
