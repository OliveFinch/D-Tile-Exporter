"""A global requests/second ceiling.

Concurrency alone does not bound load: four workers against a fast CDN is still
hundreds of requests a second. The token bucket puts an absolute ceiling on the
rate regardless of how many workers are running or how quickly they finish.
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
        self.capacity = float(burst if burst is not None else max(1.0, min(self.rate, 8.0)))
        self._tokens = self.capacity
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: float = 1.0) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.rate)
                self._updated = now
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                deficit = tokens - self._tokens
                wait = deficit / self.rate
            await asyncio.sleep(wait)


class NullRateLimiter:
    """Used by tests and by ``--rps 0``."""

    async def acquire(self, tokens: float = 1.0) -> None:
        return None
