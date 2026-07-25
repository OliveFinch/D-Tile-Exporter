from __future__ import annotations

from pathlib import Path
from typing import Any

from ..plan import JobPlan


class TileWriter:
    """Interface for output backends."""

    #: True when the backend can tell, on its own, which tiles it already holds.
    #: (All three can; the state DB is still authoritative for *missing* tiles,
    #: which by definition leave nothing on disk.)
    verifies_existing = True

    def __init__(self, output: Path, plan: JobPlan) -> None:
        self.output = Path(output)
        self.plan = plan

    def open(self) -> None:
        raise NotImplementedError

    def write_tile(self, z: int, x: int, y: int, mode: str, data: bytes) -> None:
        raise NotImplementedError

    def has_tile(self, z: int, x: int, y: int, mode: str) -> bool:
        return False

    def finalize(self, manifest: dict[str, Any], *, complete: bool = True) -> Path:
        """Flush, write the manifest, and return the artefact path.

        ``complete=False`` means the job was interrupted or aborted. Backends
        that build their artefact in a final pass must not do that pass -- the
        partly-filled staging area is the resume state.
        """
        raise NotImplementedError

    def abort(self) -> None:
        """Called on interrupt: leave everything in a resumable state."""
        return None

    # -- helpers -----------------------------------------------------------

    def tile_relpath(self, z: int, x: int, y: int, mode: str) -> str:
        """Standard slippy-map layout, with a mode directory only when needed."""
        prefix = f"{mode}/" if mode else ""
        return f"{prefix}{z}/{x}/{y}.{self.plan.park.tile_extension}"
