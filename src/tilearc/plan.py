"""Turning a request into a concrete, countable job."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from .bounds import BBox, ZoomSelection, effective_bounds, iter_bounds, select_zooms
from .config import ParkConfig, TileBounds, VersionEntry
from .errors import TilearcError
from .util import sanitize_component

#: Mean compressed tile size. Derived from the project's own scale reference
#: (e.g. WDW's 575,450 tiles ~ 14 GB); overridable with --bytes-per-tile.
DEFAULT_BYTES_PER_TILE = 25_000


@dataclass(frozen=True)
class ZoomPlan:
    zoom: int
    bounds: TileBounds
    #: Measured coverage as ``(y_from, y_to, x_from, x_to)`` runs, when the real
    #: footprint is known and is not simply ``bounds``. Declared bounds are a
    #: rectangle; the imagery inside one need not be. Hong Kong's z19 box is 15%
    #: empty, and Disneyland Paris declares 14 times the tiles it serves.
    runs: tuple[tuple[int, int, int, int], ...] | None = None

    @property
    def count(self) -> int:
        if self.runs is None:
            return self.bounds.count
        return sum((y1 - y0 + 1) * (x1 - x0 + 1) for y0, y1, x0, x1 in self.runs)

    def iter_xy(self) -> Iterator[tuple[int, int]]:
        if self.runs is None:
            yield from iter_bounds(self.bounds)
            return
        for y0, y1, x0, x1 in self.runs:
            for y in range(y0, y1 + 1):
                for x in range(x0, x1 + 1):
                    yield x, y


@dataclass
class JobPlan:
    park: ParkConfig
    version: VersionEntry
    zooms: list[ZoomPlan]
    modes: list[str]
    bbox: BBox | None = None
    selection: ZoomSelection | None = None
    notes: list[str] = field(default_factory=list)

    # -- identity ----------------------------------------------------------

    @property
    def slug(self) -> str:
        """Filesystem-safe ``{park}_{version}`` stem used inside archives."""
        return f"{sanitize_component(self.park.park_id)}_{sanitize_component(self.version.code)}"

    @property
    def tiles_per_mode(self) -> int:
        return sum(zp.count for zp in self.zooms)

    @property
    def total_tiles(self) -> int:
        return self.tiles_per_mode * max(1, len(self.modes))

    @property
    def zoom_range(self) -> tuple[int, int] | None:
        if not self.zooms:
            return None
        return self.zooms[0].zoom, self.zooms[-1].zoom

    def estimated_bytes(self, bytes_per_tile: int = DEFAULT_BYTES_PER_TILE) -> int:
        return self.total_tiles * bytes_per_tile

    def fingerprint(self) -> str:
        """Stable identity for the resume DB.

        Deliberately covers only what changes *which tiles* are fetched -- not
        output format, concurrency or rate limits -- so tuning politeness knobs
        between runs still resumes cleanly.
        """
        payload = {
            "park": self.park.park_id,
            "version": self.version.code,
            "template_source": "version-url" if self.version.url else "park-template",
            "y_scheme": self.park.y_scheme,
            "modes": sorted(self.modes),
            "bbox": self.bbox.as_list() if self.bbox else None,
            "zooms": {
                str(zp.zoom): {
                    **zp.bounds.as_dict(),
                    **({"runs": [list(r) for r in zp.runs]} if zp.runs else {}),
                }
                for zp in self.zooms
            },
        }
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]

    # -- iteration ---------------------------------------------------------

    def iter_tiles(self) -> Iterator[tuple[int, int, int, str]]:
        """Yield ``(z, x, y, mode)`` in server Y space, coarsest zoom first.

        A generator rather than a list: a full WDW job is 575k tuples and there
        is no reason to hold them all in memory.
        """
        modes = self.modes or [""]
        for mode in modes:
            for zoom_plan in self.zooms:
                for x, y in zoom_plan.iter_xy():
                    yield zoom_plan.zoom, x, y, mode

    def summary_rows(self) -> list[tuple[int, TileBounds, int]]:
        return [(zp.zoom, zp.bounds, zp.count) for zp in self.zooms]


def rehosted_origin(plan: "JobPlan") -> tuple[str, str] | None:
    """``(serving host, the park's own host)`` when a version overrides the URL.

    A version carrying its own ``url`` is served from somewhere other than the
    park's host. Sometimes that is the real source; DLP's ``jan2026`` is not --
    it is a copy already downloaded and re-hosted, so archiving it captures a
    snapshot of the archive rather than of the map.

    Which of those a given override is cannot be decided from here, so this
    only reports the mismatch and leaves the refusing to the caller.
    """
    override = plan.version.url
    if not override:
        return None
    from urllib.parse import urlparse

    park_host = urlparse(plan.park.tile_template or "").hostname or "(none)"
    other_host = urlparse(override).hostname or override
    return other_host, park_host


def load_coverage(path: str | Path, park_id: str) -> tuple[dict[int, dict], dict | None]:
    """Read one park's measured footprints, and which version they came from.

    The file is what ``tools/tile-border-trace.html`` and ``tilearc trace``
    produce: for each zoom, either a box or explicit row runs.

    The version matters as much as the shapes. A park's coverage changes
    between versions -- WDW has ninety-odd, spanning nine years -- so a
    footprint measured against the current map describes the current map. Used
    against an older one it asks for tiles that version never had, and misses
    any ground the old map covered and the new one dropped.
    """
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TilearcError(f"could not read coverage file {path}: {exc}") from exc

    maps = payload.get("maps")
    if not isinstance(maps, dict):
        raise TilearcError(f"{path} has no 'maps' object; is it a coverage file?")
    entry = maps.get(park_id)
    if entry is None:
        raise TilearcError(
            f"{path} has no measured coverage for '{park_id}'. "
            f"It covers: {', '.join(sorted(maps)) or '(nothing)'}"
        )
    zooms = entry.get("zooms") or {}
    out: dict[int, dict] = {}
    for key, value in zooms.items():
        try:
            out[int(key)] = value
        except (TypeError, ValueError):
            continue
    return out, entry.get("measuredAgainst")


def apply_coverage(
    zoom_plans: list[ZoomPlan], coverage: dict[int, dict], notes: list[str]
) -> list[ZoomPlan]:
    """Replace declared rectangles with what was actually measured.

    Both directions matter, and they fail differently. Where the declared box is
    too wide the job asks for tiles nobody drew -- wasteful, and every one comes
    back as a recorded absence. Where it is too narrow the job never asks at
    all, and the archive is quietly short: WDW z12's declared minX is four
    columns east of where its imagery starts.

    A zoom with no measurement keeps its declared bounds and is named in the
    notes, because silently trusting a rectangle is exactly what this is for.
    """
    applied: list[ZoomPlan] = []
    unmeasured: list[int] = []
    widened: list[str] = []
    narrowed = 0
    planned = {zoom_plan.zoom for zoom_plan in zoom_plans}

    for zoom_plan in zoom_plans:
        entry = coverage.get(zoom_plan.zoom)
        if entry is None:
            unmeasured.append(zoom_plan.zoom)
            applied.append(zoom_plan)
            continue

        if entry.get("shape") == "empty" or not entry.get("box"):
            widened.append(f"z{zoom_plan.zoom} serves nothing and was dropped")
            continue

        box = entry["box"]
        measured = TileBounds(box["minX"], box["maxX"], box["minY"], box["maxY"])
        runs = entry.get("runs")
        plan = ZoomPlan(
            zoom=zoom_plan.zoom,
            bounds=measured,
            runs=tuple(tuple(run) for run in runs) if runs else None,
        )
        declared = zoom_plan.bounds
        if (measured.min_x < declared.min_x or measured.max_x > declared.max_x
                or measured.min_y < declared.min_y or measured.max_y > declared.max_y):
            widened.append(
                f"z{zoom_plan.zoom} extends past its declared bounds "
                f"({declared.min_x}-{declared.max_x}, {declared.min_y}-{declared.max_y}"
                f" -> {measured.min_x}-{measured.max_x}, {measured.min_y}-{measured.max_y})"
            )
        if plan.count < zoom_plan.count:
            narrowed += zoom_plan.count - plan.count
        applied.append(plan)

    # Zooms the config's minZoom/maxZoom exclude but the server demonstrably
    # serves. `select_zooms` drops them on the reasonable assumption that the
    # declared range is the map -- Shanghai declares minZoom 14 and serves z12
    # and z13, and Tokyo declares 16 and serves z15. A measurement outranks the
    # assumption it was taken to test.
    for zoom in sorted(set(coverage) - planned):
        entry = coverage[zoom]
        if entry.get("shape") == "empty" or not entry.get("box"):
            continue
        box = entry["box"]
        runs = entry.get("runs")
        applied.append(ZoomPlan(
            zoom=zoom,
            bounds=TileBounds(box["minX"], box["maxX"], box["minY"], box["maxY"]),
            runs=tuple(tuple(run) for run in runs) if runs else None,
        ))
        widened.append(
            f"z{zoom} is outside the declared zoom range but serves "
            f"{entry.get('tiles', 0):,} tile(s), so it was added"
        )
    applied.sort(key=lambda zoom_plan: zoom_plan.zoom)

    if narrowed:
        notes.append(
            f"measured coverage removed {narrowed:,} tile(s) the declared bounds "
            f"claim but the server does not serve"
        )
    for message in widened:
        notes.append("measured coverage: " + message)
    if unmeasured:
        notes.append(
            "no measured coverage for zoom(s) "
            + ", ".join(str(z) for z in unmeasured)
            + "; their declared bounds are used unchecked"
        )
    return applied


def build_plan(
    park: ParkConfig,
    version: VersionEntry,
    *,
    min_zoom: int | None = None,
    max_zoom: int | None = None,
    bbox: BBox | None = None,
    modes: list[str] | None = None,
    allow_tms_bbox: bool = False,
    coverage: dict[int, dict] | None = None,
    coverage_version: dict | None = None,
) -> JobPlan:
    notes: list[str] = []

    if bbox is not None and park.is_tms and not allow_tms_bbox:
        # SHDR's grid is a Baidu-derived scheme; its own config carries a
        # `realCenter` precisely because `defaultCenter` is not a real WGS84
        # coordinate. Treating its tile grid as web mercator produces a
        # confidently wrong rectangle, so refuse rather than mislead.
        raise TilearcError(
            f"--bbox is not supported for '{park.park_id}': its tile grid uses "
            f"yScheme 'tms' and is not aligned to web mercator, so geographic "
            f"coordinates cannot be converted reliably. Use --min-zoom/--max-zoom "
            f"to limit the job instead (or --allow-tms-bbox if you know what the "
            f"grid is and accept the result)."
        )

    selection = select_zooms(park, min_zoom, max_zoom)
    notes.extend(selection.notes)

    zoom_plans: list[ZoomPlan] = []
    clipped_out: list[int] = []
    for zoom in selection.zooms:
        bounds = effective_bounds(park, zoom, bbox)
        if bounds is None:
            clipped_out.append(zoom)
            continue
        zoom_plans.append(ZoomPlan(zoom=zoom, bounds=bounds))

    if coverage is not None:
        # Say this before anything else the coverage does, because it changes
        # what all of it means.
        measured_code = (coverage_version or {}).get("version")
        if measured_code and str(measured_code) != str(version.code):
            label = (coverage_version or {}).get("label") or measured_code
            notes.append(
                f"the coverage was measured against version {measured_code} ({label}), "
                f"not {version.code}. A map's footprint changes between versions, so "
                f"tiles this one never had will be requested and recorded as missing, "
                f"and any ground it covered that the newer map dropped will not be "
                f"asked for at all. Trace {version.code} for a footprint that matches it"
            )
        elif not measured_code:
            notes.append(
                "the coverage file does not say which version it was measured "
                "against, so it cannot be checked against this one"
            )
        if min_zoom is not None or max_zoom is not None:
            coverage = {
                zoom: entry for zoom, entry in coverage.items()
                if (min_zoom is None or zoom >= min_zoom)
                and (max_zoom is None or zoom <= max_zoom)
            }
        zoom_plans = apply_coverage(zoom_plans, coverage, notes)

    if clipped_out:
        notes.append(
            "bbox excluded all tiles at zoom(s): " + ", ".join(str(z) for z in clipped_out)
        )

    if version.url:
        notes.append(
            f"version '{version.code}' overrides the park tile template with its own url"
        )

    return JobPlan(
        park=park,
        version=version,
        zooms=zoom_plans,
        modes=modes or [],
        bbox=bbox,
        selection=selection,
        notes=notes,
    )
