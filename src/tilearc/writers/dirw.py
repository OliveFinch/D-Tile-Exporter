"""Plain directory output -- the standard slippy-map layout on disk."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ..manifest import MANIFEST_NAME
from .base import TileWriter


class DirWriter(TileWriter):
    """Writes ``{output}/{park}_{version}/{z}/{x}/{y}.jpg``.

    Tiles are written via a temporary file and an atomic rename so an interrupt
    can never leave a half-written JPEG that a later run would treat as valid.
    """

    def __init__(self, output: Path, plan) -> None:
        super().__init__(output, plan)
        self.root = self.output / plan.slug
        self._made: set[Path] = set()

    def open(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, z: int, x: int, y: int, mode: str) -> Path:
        return self.root / self.tile_relpath(z, x, y, mode)

    def write_tile(self, z: int, x: int, y: int, mode: str, data: bytes) -> None:
        path = self._path(z, x, y, mode)
        parent = path.parent
        if parent not in self._made:
            parent.mkdir(parents=True, exist_ok=True)
            self._made.add(parent)
        tmp = path.with_name(path.name + ".part")
        tmp.write_bytes(data)
        os.replace(tmp, path)

    def has_tile(self, z: int, x: int, y: int, mode: str) -> bool:
        path = self._path(z, x, y, mode)
        try:
            return path.stat().st_size > 0
        except OSError:
            return False

    def finalize(self, manifest: dict[str, Any], *, complete: bool = True) -> Path:
        from . import dump_manifest

        # A directory archive is usable as-is either way; the manifest's own
        # `complete` flag records whether the job finished.
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / MANIFEST_NAME).write_bytes(dump_manifest(manifest))
        return self.root
