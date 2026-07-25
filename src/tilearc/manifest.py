"""The ``manifest.json`` written into every archive."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from . import TOOL_NAME, __version__
from .bounds import bounds_to_lonlat
from .plan import JobPlan

MANIFEST_NAME = "manifest.json"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def build_manifest(
    plan: JobPlan,
    *,
    started_at: str,
    finished_at: str | None = None,
    fetched: int = 0,
    missing: int = 0,
    failed: int = 0,
    total_bytes: int = 0,
    tile_urls: dict[str, str] | None = None,
    complete: bool = False,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    park = plan.park
    zoom_range = plan.zoom_range

    per_zoom = {
        str(zp.zoom): {
            "bounds": zp.bounds.as_dict(),
            "tiles": zp.count,
            "width": zp.bounds.width,
            "height": zp.bounds.height,
        }
        for zp in plan.zooms
    }

    # Geographic extent is only meaningful where the grid really is web
    # mercator; for a TMS/Baidu-derived grid it would be a fiction.
    geographic: dict[str, Any] | None = None
    if not park.is_tms and plan.zooms:
        deepest = plan.zooms[-1]
        geographic = {
            "bbox": bounds_to_lonlat(park, deepest.zoom, deepest.bounds),
            "derivedFromZoom": deepest.zoom,
        }

    manifest: dict[str, Any] = {
        "tool": {"name": TOOL_NAME, "version": __version__},
        "manifestVersion": 1,
        "park": {
            "id": park.park_id,
            "label": park.label,
            "yScheme": park.y_scheme,
            "configMinZoom": park.min_zoom,
            "configMaxZoom": park.max_zoom,
        },
        "version": {
            "code": plan.version.code,
            "label": plan.version.label,
            "active": plan.version.active,
            "templateOverridden": bool(plan.version.url),
        },
        "tileTemplate": tile_urls or {},
        "tileExtension": park.tile_extension,
        "modes": plan.modes or None,
        "zoom": {
            "min": zoom_range[0] if zoom_range else None,
            "max": zoom_range[1] if zoom_range else None,
            "levels": [zp.zoom for zp in plan.zooms],
        },
        "bounds": {
            "requestedBbox": plan.bbox.as_list() if plan.bbox else None,
            "byZoom": per_zoom,
            "geographic": geographic,
        },
        "tiles": {
            "requested": plan.total_tiles,
            "fetched": fetched,
            "missing": missing,
            "failed": failed,
        },
        "totalBytes": total_bytes,
        "complete": complete,
        "timestamps": {"started": started_at, "finished": finished_at or utcnow()},
        "notes": list(plan.notes),
    }
    if extra:
        manifest.update(extra)
    return manifest
