"""The token bucket and its adaptive variant.

Time is faked throughout: the point of these tests is the rate *arithmetic*,
and sleeping for real to observe it would make them slow and flaky.
"""

from __future__ import annotations

import pytest

from tilearc import ratelimit
from tilearc.ratelimit import (
    AdaptiveRateLimiter,
    NullRateLimiter,
    RateLimiter,
    build_limiter,
)


class Clock:
    """Stands in for the ``time`` module -- only ``monotonic`` is ever used."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(ratelimit, "time", c)
    return c


# ---------------------------------------------------------------------------
# construction
# ---------------------------------------------------------------------------


def test_rate_must_be_positive():
    with pytest.raises(ValueError):
        RateLimiter(0)


def test_backoff_factor_must_be_a_fraction():
    for bad in (0.0, 1.0, 1.5, -0.5):
        with pytest.raises(ValueError):
            AdaptiveRateLimiter(10, backoff_factor=bad)


def test_build_limiter_picks_the_right_class():
    assert isinstance(build_limiter(0), NullRateLimiter)
    assert isinstance(build_limiter(-1), NullRateLimiter)
    assert isinstance(build_limiter(5, adaptive=True), AdaptiveRateLimiter)

    fixed = build_limiter(5, adaptive=False)
    assert isinstance(fixed, RateLimiter)
    assert not isinstance(fixed, AdaptiveRateLimiter)


# ---------------------------------------------------------------------------
# push-back
# ---------------------------------------------------------------------------


async def test_a_fixed_limiter_ignores_pushback(clock):
    limiter = RateLimiter(20)
    assert await limiter.penalise() is False
    assert limiter.rate == 20


async def test_null_limiter_ignores_pushback():
    assert await NullRateLimiter().penalise() is False


async def test_penalise_halves_the_rate(clock):
    limiter = AdaptiveRateLimiter(20)
    assert limiter.rate == 20
    assert await limiter.penalise() is True
    assert limiter.rate == 10
    assert limiter.penalties == 1
    assert limiter.throttled


async def test_penalise_drops_the_accumulated_burst(clock):
    """Otherwise the next few requests go out at the rate just complained about."""
    limiter = AdaptiveRateLimiter(20)
    assert limiter._tokens > 0
    await limiter.penalise()
    assert limiter._tokens == 0


async def test_a_cluster_of_pushback_counts_once(clock):
    """N workers in flight produce N 429s for one overload event."""
    limiter = AdaptiveRateLimiter(20, cooldown=5.0)

    assert await limiter.penalise() is True
    for _ in range(7):
        clock.advance(0.05)
        assert await limiter.penalise() is False

    assert limiter.rate == 10
    assert limiter.penalties == 1

    clock.advance(6.0)
    assert await limiter.penalise() is True
    assert limiter.rate == 5
    assert limiter.penalties == 2


async def test_rate_never_falls_below_the_floor(clock):
    limiter = AdaptiveRateLimiter(20, min_rate=2.0)
    for _ in range(12):
        clock.advance(10.0)
        await limiter.penalise()
    assert limiter.rate == 2.0


async def test_penalising_at_the_floor_reports_no_further_cut(clock):
    limiter = AdaptiveRateLimiter(4, min_rate=4.0)
    clock.advance(10.0)
    assert await limiter.penalise() is False
    assert limiter.rate == 4.0


async def test_floor_is_clamped_to_the_ceiling(clock):
    """A min_rate above the requested rate must not raise the rate."""
    limiter = AdaptiveRateLimiter(2, min_rate=10.0)
    assert limiter.rate == 2
    assert limiter.min_rate == 2


# ---------------------------------------------------------------------------
# recovery
# ---------------------------------------------------------------------------


async def test_rate_climbs_back_after_quiet(clock):
    limiter = AdaptiveRateLimiter(20, recovery_interval=30.0)
    await limiter.penalise()
    assert limiter.rate == 10

    # Too soon: still inside the recovery interval.
    clock.advance(29.0)
    limiter._tick(clock.now)
    assert limiter.rate == 10

    clock.advance(2.0)
    limiter._tick(clock.now)
    assert limiter.rate == 10 + limiter.recovery_step


async def test_recovery_stops_at_the_ceiling(clock):
    limiter = AdaptiveRateLimiter(20, recovery_interval=30.0)
    await limiter.penalise()
    for _ in range(50):
        clock.advance(31.0)
        limiter._tick(clock.now)
    assert limiter.rate == 20
    assert not limiter.throttled


async def test_fresh_pushback_restarts_the_recovery_clock(clock):
    limiter = AdaptiveRateLimiter(20, recovery_interval=30.0, cooldown=5.0)
    await limiter.penalise()

    clock.advance(20.0)
    await limiter.penalise()  # resets both the penalty and recovery timers
    assert limiter.rate == 5

    clock.advance(20.0)  # 20s since the *second* cut, not 40s since the first
    limiter._tick(clock.now)
    assert limiter.rate == 5


async def test_acquire_still_hands_out_tokens_after_throttling(clock):
    """The bucket must keep working, just more slowly."""
    limiter = AdaptiveRateLimiter(20)
    await limiter.penalise()
    clock.advance(10.0)  # plenty of time to refill
    await limiter.acquire()
    assert limiter.rate == 10
