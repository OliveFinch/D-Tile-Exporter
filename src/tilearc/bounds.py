"""Zoom selection, Y-scheme handling and bbox clipping.

Y-space conventions -- the single most important thing in this file
--------------------------------------------------------------------
``TileBounds`` values, and every ``(z, x, y)`` tuple that flows through the
downloader and the writers, are in **server space**: ``y`` is literally what
gets substituted into the tile URL.

For ``yScheme: "xyz"`` parks server space *is* XYZ space, so nothing happens.
For ``yScheme: "tms"`` (SHDR only) the stored ``boundsByZoom`` values are
*already* TMS rows, so iterating them needs **no flip at all**. Flipping the
bounds "to be helpful" downloads a mirror-image band from the wrong part of
the world, and because the tiles that come back are valid JPEGs nothing errors.

The flip therefore lives in exactly two places, both of them conversions
between server space and some *other* space:

* :func:`tiles_for_bbox` -- geographic coordinates are inherently XYZ-shaped.
* the MBTiles writer -- the format mandates TMS rows.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .config import ParkConfig, TileBounds


def flip_y(zoom: int, y: int) -> int:
    """Convert between XYZ and TMS row numbering (the operation is its own inverse)."""
    return (1 << zoom) - 1 - y


def to_server_y(config: ParkConfig, zoom: int, y_xyz: int) -> int:
    """XYZ row -> the row this park's server expects in the URL."""
    return flip_y(zoom, y_xyz) if config.is_tms else y_xyz


def to_xyz_y(config: ParkConfig, zoom: int, y_server: int) -> int:
    """The row in the URL -> XYZ row."""
    return flip_y(zoom, y_server) if config.is_tms else y_server


# ---------------------------------------------------------------------------
# web mercator
# ---------------------------------------------------------------------------


def lonlat_to_tile_xyz(lon: float, lat: float, zoom: int) -> tuple[int, int]:
    """Web-mercator lon/lat -> containing XYZ tile."""
    n = 1 << zoom
    lat = max(min(lat, 85.05112878), -85.05112878)
    x = int(math.floor((lon + 180.0) / 360.0 * n))
    lat_rad = math.radians(lat)
    y = int(
        math.floor((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    )
    return max(0, min(n - 1, x)), max(0, min(n - 1, y))


def tile_xyz_to_lonlat(x: int, y: int, zoom: int) -> tuple[float, float]:
    """North-west corner of an XYZ tile."""
    n = 1 << zoom
    lon = x / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * y / n))))
    return lon, lat


def bounds_to_lonlat(config: ParkConfig, zoom: int, bounds: TileBounds) -> list[float]:
    """Geographic extent of a tile rectangle as ``[west, south, east, north]``."""
    ys = [to_xyz_y(config, zoom, bounds.min_y), to_xyz_y(config, zoom, bounds.max_y)]
    top, bottom = min(ys), max(ys)
    west, north = tile_xyz_to_lonlat(bounds.min_x, top, zoom)
    east, south = tile_xyz_to_lonlat(bounds.max_x + 1, bottom + 1, zoom)
    return [west, south, east, north]


# ---------------------------------------------------------------------------
# bbox
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BBox:
    """A geographic bounding box, ``west, south, east, north`` in degrees."""

    west: float
    south: float
    east: float
    north: float

    @classmethod
    def parse(cls, text: str) -> "BBox":
        parts = [p.strip() for p in str(text).split(",")]
        if len(parts) != 4:
            raise ValueError("bbox must be 'minLon,minLat,maxLon,maxLat'")
        try:
            west, south, east, north = (float(p) for p in parts)
        except ValueError as exc:
            raise ValueError(f"bbox has non-numeric components: {text!r}") from exc
        if west > east:
            raise ValueError(f"bbox minLon {west} is east of maxLon {east}")
        if south > north:
            raise ValueError(f"bbox minLat {south} is north of maxLat {north}")
        if not (-180 <= west <= 180 and -180 <= east <= 180):
            raise ValueError("bbox longitudes must be within -180..180")
        if not (-90 <= south <= 90 and -90 <= north <= 90):
            raise ValueError("bbox latitudes must be within -90..90")
        return cls(west, south, east, north)

    def as_list(self) -> list[float]:
        return [self.west, self.south, self.east, self.north]


def tiles_for_bbox(config: ParkConfig, zoom: int, bbox: BBox) -> TileBounds:
    """The tile rectangle covering ``bbox``, returned in **server** Y space."""
    min_x, y_north = lonlat_to_tile_xyz(bbox.west, bbox.north, zoom)
    max_x, y_south = lonlat_to_tile_xyz(bbox.east, bbox.south, zoom)
    # In XYZ, y increases southwards.
    top_xyz, bottom_xyz = min(y_north, y_south), max(y_north, y_south)
    ys = (to_server_y(config, zoom, top_xyz), to_server_y(config, zoom, bottom_xyz))
    return TileBounds(min_x, max_x, min(ys), max(ys))


# ---------------------------------------------------------------------------
# zoom selection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ZoomSelection:
    """Which zooms a job will actually touch, and why the others were dropped."""

    zooms: list[int]
    #: Requested but outside the park's own [minZoom, maxZoom].
    out_of_range: list[int]
    #: Inside the zoom range but with no boundsByZoom entry to iterate.
    missing_bounds: list[int]

    @property
    def notes(self) -> list[str]:
        notes: list[str] = []
        if self.out_of_range:
            notes.append(
                "skipped zoom(s) outside the park's zoom range: "
                + ", ".join(str(z) for z in self.out_of_range)
            )
        if self.missing_bounds:
            notes.append(
                "skipped zoom(s) with no boundsByZoom entry: "
                + ", ".join(str(z) for z in self.missing_bounds)
            )
        return notes


def select_zooms(
    config: ParkConfig,
    min_zoom: int | None = None,
    max_zoom: int | None = None,
) -> ZoomSelection:
    """Intersect the requested zooms with the park's zoom range *and* its bounds keys.

    ``minZoom``/``maxZoom`` and ``boundsByZoom`` disagree in every direction in
    the real configs, so both have to be honoured:

    * SHDR declares ``maxZoom: 21`` but has no z21 bounds -> nothing to iterate.
    * SHDR has z9-z13 bounds below its ``minZoom: 14`` -> not part of the map.
    * TDR has a z15 entry below its ``minZoom: 16`` -> likewise.
    * DLP has a z20 entry above its ``maxZoom: 19`` -> likewise.
    """
    low = config.min_zoom if min_zoom is None else max(min_zoom, config.min_zoom)
    high = config.max_zoom if max_zoom is None else min(max_zoom, config.max_zoom)

    requested = range(
        config.min_zoom if min_zoom is None else min_zoom,
        (config.max_zoom if max_zoom is None else max_zoom) + 1,
    )
    out_of_range = [z for z in requested if z < config.min_zoom or z > config.max_zoom]

    in_range = [z for z in range(low, high + 1)]
    zooms = [z for z in in_range if z in config.bounds_by_zoom]
    missing_bounds = [z for z in in_range if z not in config.bounds_by_zoom]
    return ZoomSelection(zooms=zooms, out_of_range=out_of_range, missing_bounds=missing_bounds)


def effective_bounds(
    config: ParkConfig,
    zoom: int,
    bbox: BBox | None = None,
) -> TileBounds | None:
    """The tile rectangle to iterate at ``zoom``, clipped to ``bbox`` if given."""
    declared = config.bounds_at(zoom)
    if declared is None:
        return None
    if bbox is None:
        return declared
    return declared.intersect(tiles_for_bbox(config, zoom, bbox))


def iter_bounds(bounds: TileBounds):
    """Yield ``(x, y)`` pairs, row-major so nearby tiles are fetched together."""
    for y in range(bounds.min_y, bounds.max_y + 1):
        for x in range(bounds.min_x, bounds.max_x + 1):
            yield x, y
