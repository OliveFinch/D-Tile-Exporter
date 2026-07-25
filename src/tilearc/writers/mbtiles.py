"""MBTiles output.

The MBTiles 1.3 spec stores ``tile_row`` in **TMS** numbering, so this is the
one writer that re-numbers Y. Which direction depends on the park:

* ``yScheme: "xyz"`` -- server rows are XYZ, so they must be flipped.
* ``yScheme: "tms"`` (SHDR) -- server rows are already TMS, so they are stored
  verbatim. Flipping "for consistency" here would mirror the map vertically.

With ``--mode both`` an MBTiles database cannot hold two distinct tile sets at
the same coordinates, so one file per mode is written.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from ..bounds import bounds_to_lonlat, flip_y
from .base import TileWriter

_SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (name TEXT, value TEXT);
CREATE TABLE IF NOT EXISTS tiles (
    zoom_level  INTEGER,
    tile_column INTEGER,
    tile_row    INTEGER,
    tile_data   BLOB
);
CREATE UNIQUE INDEX IF NOT EXISTS tile_index
    ON tiles (zoom_level, tile_column, tile_row);
CREATE UNIQUE INDEX IF NOT EXISTS metadata_name ON metadata (name);
"""


class MBTilesWriter(TileWriter):
    def __init__(self, output: Path, plan) -> None:
        super().__init__(output, plan)
        self._conns: dict[str, sqlite3.Connection] = {}
        self._paths: dict[str, Path] = {}
        self._pending = 0

    # -- plumbing ----------------------------------------------------------

    def _path_for(self, mode: str) -> Path:
        if not mode or len(self.plan.modes) <= 1:
            return self.output
        stem = self.output.stem or self.plan.slug
        return self.output.with_name(f"{stem}_{mode}{self.output.suffix or '.mbtiles'}")

    def _conn(self, mode: str) -> sqlite3.Connection:
        key = mode if len(self.plan.modes) > 1 else ""
        if key not in self._conns:
            path = self._path_for(mode)
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(path, isolation_level=None)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(_SCHEMA)
            self._conns[key] = conn
            self._paths[key] = path
        return self._conns[key]

    def open(self) -> None:
        self.output.parent.mkdir(parents=True, exist_ok=True)
        for mode in self.plan.modes or [""]:
            self._conn(mode)

    def _tile_row(self, z: int, y_server: int) -> int:
        return y_server if self.plan.park.is_tms else flip_y(z, y_server)

    # -- tiles -------------------------------------------------------------

    def write_tile(self, z: int, x: int, y: int, mode: str, data: bytes) -> None:
        self._conn(mode).execute(
            "INSERT INTO tiles (zoom_level, tile_column, tile_row, tile_data) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(zoom_level, tile_column, tile_row) DO UPDATE SET "
            "tile_data = excluded.tile_data",
            (z, x, self._tile_row(z, y), sqlite3.Binary(data)),
        )

    def has_tile(self, z: int, x: int, y: int, mode: str) -> bool:
        row = self._conn(mode).execute(
            "SELECT 1 FROM tiles WHERE zoom_level = ? AND tile_column = ? AND tile_row = ?",
            (z, x, self._tile_row(z, y)),
        ).fetchone()
        return row is not None

    # -- finish ------------------------------------------------------------

    def _metadata(self, manifest: dict[str, Any], mode: str) -> list[tuple[str, str]]:
        plan = self.plan
        zoom_range = plan.zoom_range or (plan.park.min_zoom, plan.park.max_zoom)
        name = f"{plan.park.label} {plan.version.display_label}"
        if mode:
            name += f" ({mode})"

        rows = [
            ("name", name),
            ("format", plan.park.tile_extension),
            ("type", "baselayer"),
            ("version", "1"),
            ("minzoom", str(zoom_range[0])),
            ("maxzoom", str(zoom_range[1])),
            (
                "description",
                f"Historical {plan.park.label} map tiles, version "
                f"{plan.version.code}, archived with tilearc",
            ),
            ("tilearc:manifest", json.dumps(manifest, sort_keys=False)),
            ("tilearc:yScheme", plan.park.y_scheme),
        ]
        # `bounds` is only truthful for a real web-mercator grid.
        if not plan.park.is_tms and plan.zooms:
            deepest = plan.zooms[-1]
            west, south, east, north = bounds_to_lonlat(
                plan.park, deepest.zoom, deepest.bounds
            )
            rows.append(("bounds", f"{west:.6f},{south:.6f},{east:.6f},{north:.6f}"))
            rows.append(
                ("center", f"{(west + east) / 2:.6f},{(south + north) / 2:.6f},{zoom_range[1]}")
            )
        return rows

    def finalize(self, manifest: dict[str, Any], *, complete: bool = True) -> Path:
        for mode in self.plan.modes or [""]:
            conn = self._conn(mode)
            conn.executemany(
                "INSERT INTO metadata (name, value) VALUES (?, ?) "
                "ON CONFLICT(name) DO UPDATE SET value = excluded.value",
                self._metadata(manifest, mode),
            )
            conn.commit()
        self.close()
        paths = sorted(set(self._paths.values()))
        return paths[0] if len(paths) == 1 else self.output

    def close(self) -> None:
        for conn in self._conns.values():
            try:
                conn.commit()
            finally:
                conn.close()
        self._conns.clear()

    def abort(self) -> None:
        self.close()
