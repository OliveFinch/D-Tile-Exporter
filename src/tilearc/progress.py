"""A dependency-free progress bar.

Deliberately not tqdm/rich: this tool has one runtime dependency (httpx) and a
progress bar is not worth a second one.
"""

from __future__ import annotations

import shutil
import sys
import time
from typing import TextIO

from .util import human_bytes, human_duration


class Progress:
    def __init__(
        self,
        total: int,
        *,
        stream: TextIO | None = None,
        enabled: bool = True,
        min_interval: float = 0.1,
    ) -> None:
        self.total = max(0, total)
        self.stream = stream if stream is not None else sys.stderr
        self.enabled = enabled and self.stream.isatty()
        self.min_interval = min_interval
        self.ok = 0
        self.missing = 0
        self.failed = 0
        self.skipped = 0
        self.bytes = 0
        self._start = time.monotonic()
        self._last_render = 0.0
        self._width = 0

    @property
    def done(self) -> int:
        return self.ok + self.missing + self.failed + self.skipped

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._start

    def update(
        self,
        *,
        ok: int = 0,
        missing: int = 0,
        failed: int = 0,
        skipped: int = 0,
        nbytes: int = 0,
    ) -> None:
        self.ok += ok
        self.missing += missing
        self.failed += failed
        self.skipped += skipped
        self.bytes += nbytes
        self.render()

    # -- rendering ---------------------------------------------------------

    def _line(self) -> str:
        done, total = self.done, self.total
        fraction = (done / total) if total else 0.0
        elapsed = max(self.elapsed, 1e-6)
        # Rate and ETA reflect *fetched* tiles; resumed-skip tiles cost nothing
        # and would otherwise inflate the figure into meaninglessness.
        fetched = self.ok + self.missing + self.failed
        rate = fetched / elapsed
        remaining = max(0, total - done)
        eta = remaining / rate if rate > 0.01 else float("inf")

        columns = shutil.get_terminal_size((100, 24)).columns
        stats = (
            f" {done:,}/{total:,} ({fraction * 100:5.1f}%) "
            f"{rate:6.1f}/s eta {human_duration(eta)} "
            f"ok {self.ok:,} missing {self.missing:,} failed {self.failed:,} "
            f"{human_bytes(self.bytes)}"
        )
        bar_width = max(8, min(30, columns - len(stats) - 4))
        filled = int(bar_width * fraction)
        bar = "#" * filled + "-" * (bar_width - filled)
        return f"[{bar}]{stats}"[: max(20, columns - 1)]

    def render(self, force: bool = False) -> None:
        if not self.enabled:
            return
        now = time.monotonic()
        if not force and (now - self._last_render) < self.min_interval:
            return
        self._last_render = now
        line = self._line()
        padding = " " * max(0, self._width - len(line))
        self._width = len(line)
        self.stream.write("\r" + line + padding)
        self.stream.flush()

    def close(self) -> None:
        if self.enabled:
            self.render(force=True)
            self.stream.write("\n")
            self.stream.flush()

    # -- summary -----------------------------------------------------------

    def summary(self) -> str:
        rate = (self.ok + self.missing + self.failed) / max(self.elapsed, 1e-6)
        return (
            f"{self.ok:,} downloaded, {self.missing:,} missing, {self.failed:,} failed"
            + (f", {self.skipped:,} already present" if self.skipped else "")
            + f" -- {human_bytes(self.bytes)} in {human_duration(self.elapsed)} "
            f"({rate:.1f} tiles/s)"
        )
