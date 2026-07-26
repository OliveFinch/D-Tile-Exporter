# Task: encode the real tile boundaries in Magic Parks Explorer

The park configs declare tile bounds that don't match what the CDNs actually
serve. Every zoom of every public park has now been measured directly against
the live CDNs, and I want those measurements encoded so the app stops asking
for tiles that don't exist and stops missing tiles that do.

## Where these numbers came from

Each zoom's coverage border was walked tile by tile (Moore-neighbour tracing),
and everything the border encloses is imagery. Not sampled, not inferred from
geometry — walked. Each measurement was then audited by re-requesting tiles the
walk had called absent, plus tiles it had seen present as a control, because a
throttled CDN answers 403 and an `<img>` cannot tell that from a 404; a walk
done during a refusal closes early and looks tidy doing it. Every figure below
comes from a walk that passed that audit, and the irregular ones were each
confirmed by two independent runs agreeing to the tile.

Measured 2026-07-26. **44 zooms, 1,097,930 tiles, ~27 GB at 25 kB/tile.**

## Read this before using the numbers

1. **Coordinates are server tile space** — exactly what goes in the URL.
   Shanghai (`shdr`) is `yScheme: "tms"`, and its Y values below are already
   TMS rows. Do not flip them again.
2. **All ranges are inclusive**, both ends, x and y.
3. **Five zooms are not rectangles.** For those a bounding box over-claims —
   Hong Kong's by 15% at both z19 and z20. They carry explicit row runs instead.
   Everything else is fully described by its box.
4. **Four declared zooms have no imagery at all** and should not be requested.
5. **These are the current version codes' footprints.** Older versions of the
   same park may differ; treat these as the live map, not as universal truth.

## What "live" means here, and its one blind spot

The border was walked; the interior was not probed. Anything the border
encloses is taken to be imagery. That is exact for an L, a C, or a bitten
corner — all verified tile-for-tile against synthetic worlds. It would
over-claim a fully enclosed hole, since an outer border cannot see one. No
holes were detected: every measured zoom came back as a single region with no
nested border. So the footprints below are exact unless one of these maps has
an island of missing tiles sealed entirely inside it, which nothing observed
suggests.

## The data

### Walt Disney World (`wdw`) — 575,490 tiles

| zoom | live tile range | tiles | vs declared |
|---|---|---|---|
| z11 | x 555-564, y 851-859 | 90 | matches |
| z12 | x 1114-1125, y 1706-1715 | 120 | declared 1118-1125, 1706-1715 |
| z13 | x 2228-2251, y 3412-3431 | 480 | matches |
| z14 | x 4456-4503, y 6824-6863 | 1,920 | matches |
| z15 | x 8928-8987, y 13672-13699 | 1,680 | matches |
| z16 | x 17856-17975, y 27344-27399 | 6,720 | matches |
| z17 | x 35712-35951, y 54688-54799 | 26,880 | matches |
| z18 | x 71424-71903, y 109376-109599 | 107,520 | matches |
| z19 | x 143264-143455, y 218752-219199 | 86,016 | matches |
| z20 | x 286528-286911, y 437504-438399 | 344,064 | matches |

### Disneyland Resort (California) (`dlr`) — 464,168 tiles

| zoom | live tile range | tiles | vs declared |
|---|---|---|---|
| z14 | x 2818-2831, y 6549-6560 | 168 | matches |
| z15 | x 5636-5663, y 13102-13117 | 448 | matches |
| z16 | x 11272-11327, y 26208-26231 | 1,344 | matches |
| z17 | x 22544-22655, y 52416-52463 | 5,376 | matches |
| z18 | x 45088-45311, y 104832-104927 | 21,504 | matches |
| z19 | x 90176-90623, y 209664-209855 | 86,016 | matches |
| z20 | see runs below (box 180352-181247, 419328-419752) | 349,312 | **irregular**; declared 180352-181247, 419328-419739 |

`dlr` z20 — bounding box over-claims by 31,488 tiles (8%):
```
y 419328..419711: x 180352-181247
y 419712..419752: x 180736-180863
```

### Disneyland Paris (`dlp`) — 36,014 tiles

| zoom | live tile range | tiles | vs declared |
|---|---|---|---|
| z13 | x 4158-4160, y 2816-2819 | 12 | declared 4156-4161, 2816-2819 |
| z14 | x 8316-8321, y 5633-5639 | 42 | declared 8312-8323, 5632-5639 |
| z15 | x 16633-16642, y 11266-11278 | 130 | declared 16624-16647, 11264-11279 |
| z16 | x 33267-33285, y 22532-22556 | 475 | declared 33248-33295, 22528-22559 |
| z17 | x 66535-66571, y 45065-45112 | 1,776 | declared 66496-66591, 45056-45119 |
| z18 | see runs below (box 133071-133142, 90130-90224) | 6,774 | **irregular**; declared 132992-133183, 90112-90239 |
| z19 | see runs below (box 266142-266285, 180261-180449) | 26,805 | **irregular**; declared 265984-266367, 180224-180479 |
| z20 | **none — do not request** | 0 | declared 531968-532735, 360448-360959, all absent |

`dlp` z18 — bounding box over-claims by 66 tiles (1%):
```
y 90130: x 133137-133142
y 90131..90224: x 133071-133142
```
`dlp` z19 — bounding box over-claims by 411 tiles (2%):
```
y 180261: x 266274-266284
y 180262..180307: x 266142-266284
y 180308..180397: x 266143-266284
y 180398..180449: x 266143-266285
```

### Hong Kong Disneyland (`hkdl`) — 9,332 tiles

| zoom | live tile range | tiles | vs declared |
|---|---|---|---|
| z14 | x 13380-13383, y 7148-7150 | 12 | matches |
| z15 | x 26762-26765, y 14297-14300 | 16 | matches |
| z16 | x 53524-53531, y 28594-28601 | 64 | matches |
| z17 | x 107048-107063, y 57188-57203 | 256 | matches |
| z18 | x 214096-214127, y 114376-114407 | 1,024 | matches |
| z19 | see runs below (box 428207-428242, 228756-228807) | 1,592 | **irregular**; declared 428192-428255, 228752-228815 |
| z20 | see runs below (box 856414-856485, 457512-457615) | 6,368 | **irregular**; declared 856384-856511, 457504-457631 |

`hkdl` z19 — bounding box over-claims by 280 tiles (15%):
```
y 228756..228769: x 428207-428222
y 228770..228807: x 428207-428242
```
`hkdl` z20 — bounding box over-claims by 1,120 tiles (15%):
```
y 457512..457539: x 856414-856445
y 457540..457615: x 856414-856485
```

### Shanghai Disney Resort (`shdr`) — 12,926 tiles

| zoom | live tile range | tiles | vs declared |
|---|---|---|---|
| z9 | **none — do not request** | 0 | declared 103-103, 27-27, all absent |
| z10 | **none — do not request** | 0 | declared 206-206, 55-55, all absent |
| z11 | **none — do not request** | 0 | declared 412-413, 110-111, all absent |
| z12 | x 825-827, y 220-222 | 9 | matches |
| z13 | x 1651-1655, y 441-444 | 20 | matches |
| z14 | x 3302-3310, y 882-889 | 72 | matches |
| z15 | x 6609-6617, y 1768-1776 | 81 | matches |
| z16 | x 13218-13235, y 3536-3553 | 324 | matches |
| z17 | x 26447-26459, y 7084-7095 | 156 | matches |
| z18 | x 52895-52919, y 14168-14191 | 600 | matches |
| z19 | x 105791-105839, y 28336-28383 | 2,352 | matches |
| z20 | x 211583-211679, y 56672-56767 | 9,312 | matches |

## Gotchas that will bite the implementation

**WDW z12 is the one place the declared bounds lose real tiles.** Declared
`minX` is 1118; the imagery starts at **1114**. That's 40 tiles currently
unreachable. The pyramid confirms it independently: z13 starts at 2228, which
halves to 1114, and 1118 would require z13 to start at 2236. Fix this in
`parks/wdw/wdw_config.json`.

**Disneyland Paris z20 does not exist.** The config declares a z20 box of
393,216 tiles and the CDN serves none of them. The same config already sets
`maxZoom: 19`, so the bounds entry is simply stale — drop it.

**Shanghai declares three empty zooms and hides two real ones.** z9, z10 and
z11 are declared and serve nothing. Meanwhile z12 and z13 *do* serve tiles
despite `minZoom: 14`, so if minZoom gates fetching you are throwing away two
usable levels.

**Disneyland Paris is over-declared everywhere**, harmlessly but expensively:
its declared boxes total 524,280 tiles against 36,014 real ones — 93% of what
the config claims does not exist. The measured boxes above are much tighter.

**DLR z20 has a detached-looking southern strip.** Its main block is exactly
z19's box doubled; a separate 128 × 41 strip (~4 km × 1.3 km) hangs about 1.3 km
off the south edge, present at z20 and at no shallower zoom. It is real —
two independent walks agree — so do not "clean it up" as an artifact.

**Disneyland Paris has no `{code}` in its tile template.** It serves one live
map from its own host. One archived DLP version (`jan2026`) is served from a
different host entirely (an R2 bucket) via a per-version `url` override, which
takes precedence over the park's `tileTemplate`.

## What I'd like built

1. Encode the per-zoom footprints as data, not as code — a JSON or TS module
   keyed by park and zoom, holding either a box or a list of runs.
2. A predicate the tile layer can call before requesting: given park, zoom, x,
   y, is this tile expected to exist? For a rectangle that's four comparisons;
   for an irregular zoom, find the run group containing y and test x against it.
   Both are O(1)-ish with a small lookup.
3. Use it to skip requests that would 404, and to size progress/estimates from
   real tile counts rather than box areas.
4. Keep the declared `boundsByZoom` as-is for now, or correct WDW z12 and drop
   DLP z20 if you'd rather have one source of truth — but if you leave them,
   the measured data must win at request time.

Ask me if any zoom's numbers look wrong rather than adjusting them to fit a
pattern; they are measurements, and the irregular ones are irregular on purpose.
