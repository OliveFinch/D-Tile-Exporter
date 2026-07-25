"""Keeping work off the UI thread.

Two shapes are needed:

* :class:`FunctionTask` for short blocking calls (fetching a config, verifying
  an archive) -- fire and forget onto the global thread pool.
* :class:`DownloadWorker` for the long job, which needs live progress and a
  Stop button. It owns an asyncio loop on its own ``QThread`` and reaches the
  running downloader through ``call_soon_threadsafe``.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

from tilearc.downloader import Downloader
from tilearc.job import JobRequest, run_job
from tilearc.progress import Progress


class _TaskSignals(QObject):
    done = Signal(object)
    failed = Signal(str)


#: In-flight tasks, so Python cannot collect one while the pool is still
#: running it. Callers do not have to hold a reference themselves.
_LIVE_TASKS: set["FunctionTask"] = set()


class FunctionTask(QRunnable):
    """Runs ``fn()`` on the thread pool and reports back via signals."""

    def __init__(self, fn: Callable[[], Any]) -> None:
        super().__init__()
        self._fn = fn
        self.signals = _TaskSignals()
        # Lifetime is managed by _LIVE_TASKS; letting Qt delete the C++ object
        # underneath a live Python reference is a crash waiting to happen.
        self.setAutoDelete(False)

    @Slot()
    def run(self) -> None:
        try:
            try:
                result = self._fn()
            except Exception as exc:
                self._emit(self.signals.failed, str(exc) or exc.__class__.__name__)
            else:
                self._emit(self.signals.done, result)
        finally:
            _LIVE_TASKS.discard(self)

    @staticmethod
    def _emit(signal, payload) -> None:
        try:
            signal.emit(payload)
        except RuntimeError:
            # The receiving widget was destroyed while this work was in flight
            # -- the window closed mid-load. There is nobody left to tell.
            pass


def run_async(
    fn: Callable[[], Any],
    on_done: Callable[[Any], None],
    on_error: Callable[[str], None],
) -> FunctionTask:
    """Start ``fn`` in the background and deliver the result on the UI thread."""
    task = FunctionTask(fn)
    task.signals.done.connect(on_done)
    task.signals.failed.connect(on_error)
    _LIVE_TASKS.add(task)
    QThreadPool.globalInstance().start(task)
    return task


class SignalProgress(Progress):
    """A `Progress` that emits snapshots instead of drawing a terminal bar."""

    def __init__(self, total: int, emit: Callable[[dict], None]) -> None:
        super().__init__(total, enabled=False)
        self._emit = emit
        self._last_emit = 0.0

    def update(self, **kwargs: int) -> None:
        super().update(**kwargs)
        now = time.monotonic()
        if now - self._last_emit >= 0.1:
            self._last_emit = now
            self._emit(self.snapshot())

    def snapshot(self) -> dict:
        return {
            "total": self.total,
            "done": self.done,
            "ok": self.ok,
            "missing": self.missing,
            "failed": self.failed,
            "skipped": self.skipped,
            "bytes": self.bytes,
            "elapsed": self.elapsed,
        }


class DownloadWorker(QObject):
    """Runs one download job. Move it onto a `QThread` and call `run`."""

    progressed = Signal(dict)
    logged = Signal(str)
    resumed = Signal(dict)
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, request: JobRequest) -> None:
        super().__init__()
        self._request = request
        self._loop: asyncio.AbstractEventLoop | None = None
        self._downloader: Downloader | None = None
        self._stop_requested = False

    @Slot()
    def run(self) -> None:  # pragma: no cover - exercised through the UI
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)

        progress = SignalProgress(self._request.plan.total_tiles, self.progressed.emit)
        try:
            outcome = loop.run_until_complete(
                run_job(
                    self._request,
                    progress,
                    log=self.logged.emit,
                    on_resume=self.resumed.emit,
                    on_downloader=self._capture_downloader,
                )
            )
        except Exception as exc:
            self.failed.emit(str(exc) or exc.__class__.__name__)
        else:
            self.progressed.emit(progress.snapshot())
            self.finished.emit(outcome)
        finally:
            self._loop = None
            self._downloader = None
            asyncio.set_event_loop(None)
            loop.close()

    def _capture_downloader(self, downloader: Downloader) -> None:
        self._downloader = downloader
        # Stop may have been pressed during setup, before there was anything
        # to stop.
        if self._stop_requested:
            self.stop()

    @Slot()
    def stop(self) -> None:
        """Ask the job to wind down. Safe to call from the UI thread."""
        self._stop_requested = True
        loop, downloader = self._loop, self._downloader
        if loop is None or downloader is None:
            return
        try:
            loop.call_soon_threadsafe(downloader.request_stop)
        except RuntimeError:
            pass  # the loop already finished
