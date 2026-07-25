# Test fixtures

`parks/` holds a snapshot of the real `parks/{id}/` data from the Magic Parks
Explorer viewer repo, so the bounds and URL logic is tested against the actual
quirks rather than idealised data. The tile counts these produce match the
project's published scale reference exactly (WDW full depth 575,450; DLR
484,008; TDR 137,645/mode; DLP 131,064; HKDL 21,852; SHDR 12,897).

Two deliberate edits to `tdr/tdr_config.json`:

* **The CloudFront cookies are placeholders.** The real signed cookies are
  credentials and are never committed here. `proxyUrl` and `cookieExpires` are
  likewise fixture values.
* `rotationAreas` was dropped -- it is viewer rendering data and made up most
  of the file.

Everything else is verbatim. Refresh with a re-run of the snapshot step if the
viewer's configs change; the anomaly tests are written against the values below
and will fail loudly if the source data is corrected upstream (which is the
point -- `doctor` exists to get these fixed).

## Known anomalies these fixtures encode

| Park | Anomaly |
|---|---|
| wdw | z12 x-span 8 < z11's 10 |
| wdw | z15 y-span 28 < z14's 40 |
| wdw | z19 is 192x448 vs z18's 480x224 -- aspect inverts, X/Y likely swapped |
| dlr | z20 maxY 419739 overshoots the 2x grid (419711) |
| hkdl | z15 is one tile short on max X and max Y |
| shdr | z17 shrinks in *both* dimensions vs z16 (18x18 -> 13x12) |
| shdr | maxZoom 21 but no z21 bounds; z9-z13 bounds below minZoom 14 |
| dlp | z20 bounds entry above maxZoom 19; park template has no `{code}` |
| dlp | version `jan2026` overrides the template with an R2 bucket URL |
| tdr | z15 entry below minZoom 16 |
| tdr | every level is one tile short on max X/Y (`max*2` instead of `max*2+1`) |
