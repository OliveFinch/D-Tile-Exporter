"""Zip output.

Tiles are staged in ``{output}.parts/`` and packed into the zip at the end,
rather than appended to the archive as they arrive.

That costs a second pass over the data, and it is worth it. A zip interrupted
mid-append has no valid central directory, so an archive killed by Ctrl-C on a
14 GB WDW job could be unreadable -- and unreadable means unresumable, which is
the one outcome this tool must never produce. A staging directory is just files
on disk: whatever survived is still there, and the state DB agrees with it.
"""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path
from typing import Any

from ..manifest import MANIFEST_NAME
from .base import TileWriter
from .dirw import DirWriter


class ZipWriter(TileWriter):
    def __init__(self, output: Path, plan, *, keep_staging: bool = False) -> None:
        super().__init__(output, plan)
        self.staging = self.output.with_name(self.output.name + ".parts")
        self.keep_staging = keep_staging
        self._inner = DirWriter(self.staging, plan)

    @property
    def staging_root(self) -> Path:
        return self._inner.root

    def open(self) -> None:
        self._inner.open()

    def write_tile(self, z: int, x: int, y: int, mode: str, data: bytes) -> None:
        self._inner.write_tile(z, x, y, mode, data)

    def has_tile(self, z: int, x: int, y: int, mode: str) -> bool:
        return self._inner.has_tile(z, x, y, mode)

    def finalize(self, manifest: dict[str, Any], *, complete: bool = True) -> Path:
        from . import dump_manifest

        root = self.staging_root
        root.mkdir(parents=True, exist_ok=True)
        (root / MANIFEST_NAME).write_bytes(dump_manifest(manifest))

        if not complete:
            # Packing now would produce a zip that looks finished but is not,
            # and the next run would skip everything already inside it -- so the
            # missing tiles would never be added. Leave the staging directory as
            # the resume state instead.
            return root

        tmp = self.output.with_name(self.output.name + ".tmp")
        if tmp.exists():
            tmp.unlink()
        self.output.parent.mkdir(parents=True, exist_ok=True)

        # JPEGs are already compressed; ZIP_STORED keeps packing fast and the
        # archive readable by anything.
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
            for path in sorted(root.rglob("*")):
                if not path.is_file() or path.suffix == ".part":
                    continue
                arcname = f"{self.plan.slug}/{path.relative_to(root).as_posix()}"
                if path.name == MANIFEST_NAME:
                    archive.writestr(arcname, path.read_bytes())
                else:
                    archive.write(path, arcname)

        tmp.replace(self.output)
        if not self.keep_staging:
            shutil.rmtree(root.parent, ignore_errors=True)
        return self.output

    def abort(self) -> None:
        # Staging is the resume state; leaving it in place is the whole point.
        return None
