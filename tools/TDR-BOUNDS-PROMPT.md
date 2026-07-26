# Tokyo Disney Resort — the tile ranges that actually exist

Measured against the live server on 2026-07-26 by walking the edge of the
imagery at each zoom. TDR's declared `boundsByZoom` is far wider than what is
served, so requesting the declared range means roughly nine 404s for every tile
that returns an image.

Use these ranges to decide whether to request a tile.

| zoom | x | y | tiles |
|---|---|---|---|
| z15 | 29115–29117 | 12907–12910 | 12 |
| z16 | 58230–58235 | 25814–25821 | 48 |
| z17 | 116460–116471 | 51628–51643 | 192 |
| z18 | 232920–232943 | 103256–103287 | 768 |
| z19 | 465840–465887 | 206512–206575 | 3,072 |
| z20 | 931680–931775 | 413024–413151 | 12,288 |

**16,380 tiles per mode.** Every zoom is a plain rectangle — no gaps, no
odd shapes — so four comparisons per tile is the whole test.

## Things that will trip it up

- **These are server tile coordinates**, `xyz` scheme, the values that go
  straight into the URL. Both ends of every range are inclusive.
- **z15 serves tiles even though the config declares `minZoom: 16`.** If minZoom
  gates fetching, that level is being discarded despite existing.
- **Outside these ranges the server returns nothing**, and through the tile
  proxy that arrives as `204` with an empty body rather than a `404`.
- **Measured on the `daytime` tile set.** `nighttime` is a separate set and is
  assumed to match; that has not been checked.

## A useful property

Each zoom is exactly its parent doubled — z16 is z15 × 2, and so on to z20. TDR
draws the same ground at every level, unlike the other parks, whose coverage
narrows as you zoom in. So one range plus the zoom delta derives the rest, if
that is more convenient than a table:

    minX(z) = 29115 << (z - 15)
    maxX(z) = ((29117 + 1) << (z - 15)) - 1

and the same for y with 12907 and 12910.
