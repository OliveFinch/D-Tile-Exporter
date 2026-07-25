"""A global requests/second ceiling, with optional self-regulation.

Concurrency alone does not bound load: four workers against a fast CDN is still
hundreds of requests a second. The token bucket puts an absolute ceiling on the
rate regardless of how many workers are running or how quickly they finish.

The ceiling alone is not enough, though. Per-tile backoff (see
``downloader._retry_delay``) makes each *individual* request retreat politely
after a 429, but the bucket carries on issuing tokens at the configured rate
the whole time, so the job as a whole keeps pushing just as hard as before.
That is the pattern that turns a soft rate-limit into a real block.

:class:`AdaptiveRateLimiter` closes that gap with AIMD -- the same
multiplicative-decrease / additive-increase shape TCP uses. Push-back halves
the rate; sustained quiet walks it back up towards the ceiling. The practical
effect is that the configured rate becomes a *maximum* rather than a promise,
so setting it optimistically is no longer a gamble.
"""

from __future__ import annotations

import asyncio
import time


class RateLimiter:
    """Token bucket shared by every worker in a job."""

    def __init__(self, rate_per_second: float, burst: float | None = None) -> None:
        if rate_per_second <= 0:
            raise ValueError("rate_per_second must be positive")
        self.rate = float(rate_per_second)
        # A small burst smooths out scheduling jitter without meaningfully
        # raising the sustained rate.
        self.capacity = float(burst if burst is not None else self._burst_for(self.rate))
        self._explicit_burst = burst is not None
        self._tokens = self.capacity
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    @staticmethod
    def _burst_for(rate: float) -> float:
        return max(1.0, min(rate, 8.0))

    def _tick(self, now: float) -> None:
        """Hook for subclasses to adjust ``self.rate``; called under the lock."""

    async def acquire(self, tokens: float = 1.0) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                self._tick(now)
                self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.rate)
                self._updated = now
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                deficit = tokens - self._tokens
                wait = deficit / self.rate
            await asyncio.sleep(wait)

    async def penalise(self) -> bool:
        """Report server push-back. Fixed limiters ignore it and return False."""
        return False


class AdaptiveRateLimiter(RateLimiter):
    """A token bucket that lowers its own rate when the server pushes back.

    ``rate_per_second`` becomes a ceiling rather than a fixed rate:

    * :meth:`penalise` (called on 429/503) multiplies the current rate by
      ``backoff_factor`` and empties the bucket, so the retreat takes effect on
      the very next request rather than after the accumulated burst drains;
    * after ``recovery_interval`` seconds with no further push-back the rate
      climbs by ``recovery_step``, repeatedly, until it reaches the ceiling.

    ``cooldown`` exists because push-back arrives in clusters: with N workers
    in flight one overload produces N 429s within a few milliseconds, and
    halving per response would collapse the rate to the floor over what is
    really a single event. Only the first penalty in each window counts.
    """

    def __init__(
        self,
        rate_per_second: float,
        burst: float | None = None,
        *,
        min_rate: float = 1.0,
        backoff_factor: float = 0.5,
        recovery_interval: float = 30.0,
        recovery_step: float | None = None,
        cooldown: float = 5.0,
    ) -> None:
        super().__init__(rate_per_second, burst)
        if not 0.0 < backoff_factor < 1.0:
            raise ValueError("backoff_factor must be between 0 and 1")
        self.ceiling = float(rate_per_second)
        #: Never throttle below this -- past some point, stopping beats crawling.
        self.min_rate = max(0.05, min(float(min_rate), self.ceiling))
        self.backoff_factor = float(backoff_factor)
        self.recovery_interval = float(recovery_interval)
        self.recovery_step = float(
            recovery_step if recovery_step is not None else max(self.ceiling / 10.0, 0.5)
        )
        self.cooldown = float(cooldown)
        #: Counts *applied* cuts, not 429s seen -- the cooldown collapses a
        #: cluster of responses into one. Surfaced in the job summary.
        self.penalties = 0
        self._last_penalty: float | None = None
        self._last_recovery = time.monotonic()

    @property
    def throttled(self) -> bool:
        return self.rate < self.ceiling

    def _set_rate(self, rate: float) -> None:
        self.rate = max(self.min_rate, min(self.ceiling, rate))
        if not self._explicit_burst:
            self.capacity = self._burst_for(self.rate)
            self._tokens = min(self._tokens, self.capacity)

    def _tick(self, now: float) -> None:
        """Walk the rate back up once things have been quiet for a while."""
        if self.rate >= self.ceiling:
            return
        if self._last_penalty is not None and now - self._last_penalty < self.recovery_interval:
            return
        if now - self._last_recovery < self.recovery_interval:
            return
        self._last_recovery = now
        self._set_rate(self.rate + self.recovery_step)

    async def penalise(self) -> bool:
        """Halve the rate. Returns True when a cut was actually applied."""
        async with self._lock:
            now = time.monotonic()
            if self._last_penalty is not None and now - self._last_penalty < self.cooldown:
                return False
            self._last_penalty = now
            self._last_recovery = now
            if self.rate <= self.min_rate:
                return False
            self._set_rate(self.rate * self.backoff_factor)
            # Drop the accumulated burst as well: keeping it would let the next
            # few requests go out at the old rate, which is precisely what the
            # server just objected to.
            self._tokens = 0.0
            self.penalties += 1
            return True


class NullRateLimiter:
    """Used by tests and by ``--rps 0``."""

    async def acquire(self, tokens: float = 1.0) -> None:
        return None

    async def penalise(self) -> bool:
        return False


def build_limiter(rps: float, *, adaptive: bool = True):
    """The limiter a job should use for the given rate and adaptive setting."""
    if rps <= 0:
        return NullRateLimiter()
    return AdaptiveRateLimiter(rps) if adaptive else RateLimiter(rps)
