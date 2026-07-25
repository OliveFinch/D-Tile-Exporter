"""Turning a request into a concrete, countable job."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
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

    @property
    def count(self) -> int:
        return self.bounds.count


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
            "zooms": {str(zp.zoom): zp.bounds.as_dict() for zp in self.zooms},
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
                for x, y in iter_bounds(zoom_plan.bounds):
                    yield zoom_plan.zoom, x, y, mode

    def summary_rows(self) -> list[tuple[int, TileBounds, int]]:
        return [(zp.zoom, zp.bounds, zp.count) for zp in self.zooms]


def build_plan(
    park: ParkConfig,
    version: VersionEntry,
    *,
    min_zoom: int | None = None,
    max_zoom: int | None = None,
    bbox: BBox | None = None,
    modes: list[str] | None = None,
    allow_tms_bbox: bool = False,
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
