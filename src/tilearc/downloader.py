"""The async download loop: concurrency, rate limiting, backoff, resume.

Politeness is structural here, not advisory:

* a bounded worker pool (default 5), plus a global token bucket so the request
  rate is capped no matter how fast responses come back;
* exponential backoff with jitter on 429/5xx, honouring ``Retry-After``;
* 403/404 (and 204/empty from the TDR worker) recorded as *missing* and never
  retried, on this run or any future one;
* a circuit breaker that aborts rather than grinding through a wall of errors
  caused by expired credentials or a rate-limited host.
"""

from __future__ import annotations

import asyncio
import hashlib
import random
import signal
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

import httpx

from .errors import CredentialsExpiredError, TilearcError
from .plan import JobPlan
from .progress import Progress
from .ratelimit import NullRateLimiter, RateLimiter
from .state import STATUS_DONE, STATUS_FAILED, STATUS_MISSING, JobState, _pack
from .urls import Outcome, TileSource
from .writers.base import TileWriter

#: Above this, warn -- a hobby archive job has no business opening 10+ parallel
#: connections against someone else's production CDN.
CONCURRENCY_WARN_THRESHOLD = 10


@dataclass
class DownloadOptions:
    concurrency: int = 5
    rps: float = 10.0
    retries: int = 5
    timeout: float = 30.0
    backoff_base: float = 1.0
    backoff_max: float = 60.0
    #: Abort after this many consecutive retryable failures across all workers.
    error_streak_limit: int = 50
    #: Abort if the first N responses are all "missing" (see _check_all_missing).
    all_missing_probe: int = 200


@dataclass
class DownloadResult:
    fetched: int = 0
    missing: int = 0
    failed: int = 0
    skipped: int = 0
    total_bytes: int = 0
    interrupted: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.interrupted and self.failed == 0


class _Aborted(Exception):
    """Internal signal: stop the pool and propagate ``reason``."""

    def __init__(self, reason: BaseException) -> None:
        super().__init__(str(reason))
        self.reason = reason


class Downloader:
    def __init__(
        self,
        plan: JobPlan,
        sources: dict[str, TileSource],
        writer: TileWriter,
        state: JobState,
        options: DownloadOptions,
        progress: Progress,
        *,
        log: Callable[[str], None] = lambda _msg: None,
    ) -> None:
        self.plan = plan
        self.sources = sources
        self.writer = writer
        self.state = state
        self.options = options
        self.progress = progress
        self.log = log

        self.result = DownloadResult()
        self._headers: dict[str, dict[str, str]] = {}
        self._stop = asyncio.Event()
        self._abort_reason: BaseException | None = None
        self._error_streak = 0
        self._seen = 0
        self._missing_run = 0
        self._had_success = False

    # -- helpers -----------------------------------------------------------

    def _source(self, mode: str) -> TileSource:
        return self.sources[mode] if mode in self.sources else next(iter(self.sources.values()))

    def _abort(self, reason: BaseException) -> None:
        if self._abort_reason is None:
            self._abort_reason = reason
        self._stop.set()

    def _pending_tiles(self) -> Iterator[tuple[int, int, int, str]]:
        """Yield tiles still to do, skipping anything already terminal."""
        done_by_mode = {mode: self.state.completed(mode) for mode in (self.plan.modes or [""])}
        for z, x, y, mode in self.plan.iter_tiles():
            if _pack(z, x, y) in done_by_mode.get(mode, ()):
                self.result.skipped += 1
                self.progress.update(skipped=1)
                continue
            yield z, x, y, mode

    def _check_all_missing(self) -> None:
        """Catch "everything is 404/204" early instead of after 138k requests.

        For a worker-routed TDR job this is the expired-credential signal: the
        worker turns an upstream 403 into a 204, so bad cookies look exactly
        like a park with no coverage anywhere.
        """
        probe = self.options.all_missing_probe
        if self._had_success or self._seen < probe or self._missing_run < probe:
            return
        source = next(iter(self.sources.values()))
        hint = source.all_missing_hint or (
            "the first {n} tiles were all reported missing. Check the version "
            "code and zoom range -- this usually means the version does not "
            "exist at these coordinates."
        ).format(n=probe)
        error: BaseException
        if source.auth_statuses or source.uses_shared_proxy:
            error = CredentialsExpiredError(hint)
        else:
            error = TilearcError(hint)
        self._abort(error)

    # -- one tile ----------------------------------------------------------

    async def _fetch_tile(
        self, client: httpx.AsyncClient, limiter, z: int, x: int, y: int, mode: str
    ) -> None:
        source = self._source(mode)
        url = source.url(z, x, y)
        headers = self._headers.setdefault(mode, source.request_headers())
        attempts = 0

        while not self._stop.is_set():
            attempts += 1
            await limiter.acquire()
            try:
                response = await client.get(url, headers=headers)
                status = response.status_code
                body = response.content
                outcome = source.classify(status, len(body))
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                status, body, outcome = 0, b"", Outcome.RETRY
                last_error: str | None = f"{type(exc).__name__}: {exc}"
            else:
                last_error = None if outcome != Outcome.RETRY else f"HTTP {status}"

            if outcome == Outcome.OK:
                self._had_success = True
                self._error_streak = 0
                self._missing_run = 0
                self._seen += 1
                digest = hashlib.sha256(body).hexdigest()
                self.writer.write_tile(z, x, y, mode, body)
                self.state.record(
                    z, x, y, mode, STATUS_DONE,
                    size=len(body),
                    etag=response.headers.get("ETag"),
                    sha256=digest,
                    attempts=attempts,
                )
                self.result.fetched += 1
                self.result.total_bytes += len(body)
                self.progress.update(ok=1, nbytes=len(body))
                return

            if outcome == Outcome.MISSING:
                self._error_streak = 0
                self._seen += 1
                self._missing_run += 1
                self.state.record(z, x, y, mode, STATUS_MISSING, attempts=attempts)
                self.result.missing += 1
                self.progress.update(missing=1)
                self._check_all_missing()
                return

            if outcome == Outcome.AUTH:
                self.state.record(z, x, y, mode, STATUS_FAILED, attempts=attempts)
                self._abort(
                    CredentialsExpiredError(
                        f"the tile server rejected our credentials (HTTP {status}) for "
                        f"{source.name}. Refresh the CloudFront signed cookies and "
                        f"re-run; already-archived tiles will not be re-fetched."
                    )
                )
                return

            # -- retryable ----------------------------------------------
            if attempts > self.options.retries:
                self._error_streak += 1
                self.state.record(z, x, y, mode, STATUS_FAILED, attempts=attempts)
                self.result.failed += 1
                self.progress.update(failed=1)
                self.log(f"giving up on {z}/{x}/{y} after {attempts} attempts ({last_error})")
                if self._error_streak >= self.options.error_streak_limit:
                    self._abort(
                        TilearcError(
                            f"{self._error_streak} tiles failed in a row (last: {last_error}). "
                            f"Stopping rather than hammering the server; the job is "
                            f"resumable, so re-run once things look healthy."
                        )
                    )
                return

            delay = self._retry_delay(response if status else None, status, attempts)
            self.log(f"retry {attempts}/{self.options.retries} for {z}/{x}/{y} in {delay:.1f}s")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
                return  # stop was set while waiting
            except asyncio.TimeoutError:
                continue

    def _retry_delay(self, response: httpx.Response | None, status: int, attempts: int) -> float:
        if response is not None and status == 429:
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    return min(float(retry_after), self.options.backoff_max)
                except ValueError:
                    pass
        # Exponential with full jitter, so parallel workers do not resynchronise
        # into a thundering herd after a 429.
        window = min(self.options.backoff_base * (2 ** (attempts - 1)), self.options.backoff_max)
        return random.uniform(window / 2, window)

    # -- pool --------------------------------------------------------------

    async def run(self) -> DownloadResult:
        options = self.options
        limiter = RateLimiter(options.rps) if options.rps > 0 else NullRateLimiter()
        queue: asyncio.Queue[tuple[int, int, int, str] | None] = asyncio.Queue(
            maxsize=options.concurrency * 4
        )

        limits = httpx.Limits(
            max_connections=options.concurrency,
            max_keepalive_connections=options.concurrency,
        )
        client = httpx.AsyncClient(
            limits=limits,
            timeout=httpx.Timeout(options.timeout),
            follow_redirects=True,
            http2=False,
        )

        async def worker() -> None:
            while True:
                item = await queue.get()
                try:
                    if item is None:
                        return
                    if self._stop.is_set():
                        continue
                    await self._fetch_tile(client, limiter, *item)
                finally:
                    queue.task_done()

        async def feeder() -> None:
            for tile in self._pending_tiles():
                if self._stop.is_set():
                    break
                await queue.put(tile)
            for _ in range(options.concurrency):
                await queue.put(None)

        self._install_signal_handler()
        workers = [asyncio.create_task(worker()) for _ in range(options.concurrency)]
        feed = asyncio.create_task(feeder())
        try:
            await feed
            await asyncio.gather(*workers)
        except asyncio.CancelledError:  # pragma: no cover - defensive
            self.result.interrupted = True
            raise
        finally:
            self._remove_signal_handler()
            feed.cancel()
            for task in workers:
                task.cancel()
            await asyncio.gather(feed, *workers, return_exceptions=True)
            await client.aclose()
            self.state.flush()
            self.progress.render(force=True)

        if self._abort_reason is not None:
            raise self._abort_reason
        return self.result

    # -- interrupt handling ------------------------------------------------

    def request_stop(self, message: str | None = None) -> None:
        """Ask the job to wind down: finish in-flight tiles, then save state.

        Safe to call from a signal handler or, via
        ``loop.call_soon_threadsafe``, from another thread -- which is how the
        GUI's Stop button reaches it.
        """
        if self._stop.is_set():
            return
        self.result.interrupted = True
        self._stop.set()
        if message:
            self.log(message)

    def _install_signal_handler(self) -> None:
        self._previous: Any = None
        try:
            loop = asyncio.get_running_loop()
            loop.add_signal_handler(signal.SIGINT, self._on_interrupt)
            self._handler_installed = True
        except Exception:
            # Only the main thread can own signal handlers; a GUI worker
            # thread cannot, and does not need to.
            self._handler_installed = False

    def _remove_signal_handler(self) -> None:
        if getattr(self, "_handler_installed", False):
            try:
                asyncio.get_running_loop().remove_signal_handler(signal.SIGINT)
            except Exception:  # pragma: no cover
                pass

    def _on_interrupt(self) -> None:
        self.request_stop("\ninterrupted -- finishing in-flight tiles and saving state...")
