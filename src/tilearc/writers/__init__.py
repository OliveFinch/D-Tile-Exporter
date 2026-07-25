"""Output backends.

All three share one contract: tiles arrive as ``(z, x, y, mode, data)`` with
``y`` in **server space** (exactly the value that appeared in the tile URL).
Any re-numbering a format requires is the writer's own business -- only the
MBTiles writer needs it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..plan import JobPlan
from .base import TileWriter
from .dirw import DirWriter
from .mbtiles import MBTilesWriter
from .zipw import ZipWriter

FORMATS = ("zip", "dir", "mbtiles")


def default_output(plan: JobPlan, fmt: str) -> Path:
    stem = plan.slug
    if fmt == "zip":
        return Path(f"{stem}.zip")
    if fmt == "mbtiles":
        return Path(f"{stem}.mbtiles")
    return Path(stem)


def build_writer(fmt: str, output: Path, plan: JobPlan) -> TileWriter:
    if fmt == "zip":
        return ZipWriter(output, plan)
    if fmt == "dir":
        return DirWriter(output, plan)
    if fmt == "mbtiles":
        return MBTilesWriter(output, plan)
    raise ValueError(f"unknown output format: {fmt}")


def dump_manifest(manifest: dict[str, Any]) -> bytes:
    return json.dumps(manifest, indent=2, sort_keys=False).encode("utf-8") + b"\n"


__all__ = [
    "FORMATS",
    "TileWriter",
    "DirWriter",
    "ZipWriter",
    "MBTilesWriter",
    "build_writer",
    "default_output",
    "dump_manifest",
]
