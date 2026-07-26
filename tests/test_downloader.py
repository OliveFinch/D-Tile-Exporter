"""Downloader behaviour, driven entirely by httpx.MockTransport.

No test in this file touches the network.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from tilearc.downloader import Downloader, DownloadOptions
from tilearc.errors import CredentialsExpiredError, TilearcError
from tilearc.plan import build_plan
from tilearc.progress import Progress
from tilearc.state import STATUS_DONE, STATUS_MISSING, JobState
from tilearc.urls import TileSource, build_source
from tilearc.writers.dirw import DirWriter

JPEG = b"\xff\xd8" + b"\x00" * 200 + b"\xff\xd9"


def make_plan(repo, park="hkdl", version="19", **kwargs):
    kwargs.setdefault("min_zoom", 14)
    kwargs.setdefault("max_zoom", 14)
    return build_plan(repo.park(park), repo.version(park, version), **kwargs)


def run_job(plan, handler, tmp_path, *, options=None, source=None, state=None):
    """Wire a Downloader up to a MockTransport and run it to completion."""
    writer = DirWriter(Path(tmp_path) / "out" / plan.slug, plan)
    writer.open()
    state = state or JobState(Path(tmp_path) / "s.sqlite")
    state.bind_job(plan.fingerprint(), {})
    progress = Progress(plan.total_tiles, enabled=False)
    options = options or DownloadOptions(concurrency=2, rps=0, retries=2, backoff_base=0.001)
    source = source or TileSource(name="t", template="https://tiles.test/{z}/{x}/{y}.jpg")

    downloader = Downloader(plan, {"": source}, writer, state, options, progress)

    original_run = downloader.run

    async def patched() -> object:
        transport = httpx.MockTransport(handler)
        real_client = httpx.AsyncClient
        import tilearc.downloader as module

        class Patched(real_client):
            def __init__(self, **kw):
                kw.pop("limits", None)
                kw["transport"] = transport
                super().__init__(**kw)

        module.httpx.AsyncClient = Patched
        try:
            return await original_run()
        finally:
            module.httpx.AsyncClient = real_client

    downloader.run = patched
    try:
        result = asyncio.run(downloader.run())
    finally:
        state.flush()
    return downloader, result, writer, state


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------


def test_all_tiles_downloaded(repo, tmp_path):
    plan = make_plan(repo)
    assert plan.total_tiles == 12

    _dl, result, writer, state = run_job(plan, lambda r: httpx.Response(200, content=JPEG), tmp_path)

    assert result.fetched == 12
    assert result.missing == 0 and result.failed == 0
    assert result.complete
    assert result.total_bytes == 12 * len(JPEG)
    assert state.counts() == {STATUS_DONE: 12}
    assert len(list(writer.root.rglob("*.jpg"))) == 12


def test_written_paths_use_the_slippy_layout(repo, tmp_path):
    plan = make_plan(repo)
    _dl, _r, writer, _s = run_job(plan, lambda r: httpx.Response(200, content=JPEG), tmp_path)
    assert (writer.root / "14" / "13380" / "7148.jpg").is_file()
    assert writer.root.name == "hkdl_19"


def test_requested_urls_match_the_template(repo, tmp_path):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, content=JPEG)

    run_job(make_plan(repo), handler, tmp_path)
    assert "https://tiles.test/14/13380/7148.jpg" in seen
    assert len(seen) == 12


def test_user_agent_is_sent(repo, tmp_path):
    from tilearc import USER_AGENT

    seen = []

    def handler(request):
        seen.append(request.headers.get("user-agent"))
        return httpx.Response(200, content=JPEG)

    plan = make_plan(repo)
    run_job(plan, handler, tmp_path, source=build_source(plan.park, plan.version))
    assert set(seen) == {USER_AGENT}
    assert "tilearc" in USER_AGENT and "archiver" in USER_AGENT


# ---------------------------------------------------------------------------
# missing tiles are normal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [403, 404])
def test_missing_tiles_do_not_fail_the_job(repo, tmp_path, status):
    """Park bounds are rectangles; coverage is not. Gaps are expected."""
    def handler(request):
        if "7148" in str(request.url):
            return httpx.Response(status)
        return httpx.Response(200, content=JPEG)

    _dl, result, _w, state = run_job(make_plan(repo), handler, tmp_path)

    assert result.missing == 4 and result.fetched == 8
    assert result.failed == 0 and result.complete
    assert state.counts() == {STATUS_DONE: 8, STATUS_MISSING: 4}


def test_zero_length_200_is_missing(repo, tmp_path):
    _dl, result, _w, _s = run_job(make_plan(repo), lambda r: httpx.Response(200, content=b""), tmp_path)
    assert result.missing == 12 and result.fetched == 0


def test_worker_204_is_missing(repo, tmp_path):
    source = TileSource(
        name="tdr",
        template="https://w.test/z{z}/{x}_{y}.jpg",
        missing_statuses=frozenset({204, 404}),
        uses_shared_proxy=True,
        all_missing_hint="cookies probably expired",
    )
    plan = make_plan(repo)

    def handler(request):
        return httpx.Response(204) if "7148" in str(request.url) else httpx.Response(200, content=JPEG)

    _dl, result, _w, _s = run_job(plan, handler, tmp_path, source=source)
    assert result.missing == 4 and result.fetched == 8


# ---------------------------------------------------------------------------
# retry and backoff
# ---------------------------------------------------------------------------


def test_500_is_retried_then_succeeds(repo, tmp_path):
    attempts = {}

    def handler(request):
        key = str(request.url)
        attempts[key] = attempts.get(key, 0) + 1
        return httpx.Response(500) if attempts[key] < 2 else httpx.Response(200, content=JPEG)

    _dl, result, _w, _s = run_job(make_plan(repo), handler, tmp_path)
    assert result.fetched == 12 and result.failed == 0
    assert all(count == 2 for count in attempts.values())


def test_429_is_retried(repo, tmp_path):
    attempts = {}

    def handler(request):
        key = str(request.url)
        attempts[key] = attempts.get(key, 0) + 1
        if attempts[key] < 2:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, content=JPEG)

    _dl, result, _w, _s = run_job(make_plan(repo), handler, tmp_path)
    assert result.fetched == 12


def test_retry_after_is_honoured(repo, tmp_path):
    downloader, _r, _w, _s = run_job(
        make_plan(repo), lambda r: httpx.Response(200, content=JPEG), tmp_path
    )
    response = httpx.Response(429, headers={"Retry-After": "7"})
    assert downloader._retry_delay(response, 429, 1) == 7.0
    # ...and clamped to backoff_max.
    huge = httpx.Response(429, headers={"Retry-After": "9999"})
    assert downloader._retry_delay(huge, 429, 1) == downloader.options.backoff_max


def test_backoff_is_exponential_and_jittered(repo, tmp_path):
    downloader, _r, _w, _s = run_job(
        make_plan(repo), lambda r: httpx.Response(200, content=JPEG), tmp_path
    )
    for attempt in range(1, 6):
        window = min(downloader.options.backoff_base * 2 ** (attempt - 1), 60)
        delay = downloader._retry_delay(None, 500, attempt)
        assert window / 2 <= delay <= window


def test_timeouts_are_retried(repo, tmp_path):
    attempts = {}

    def handler(request):
        key = str(request.url)
        attempts[key] = attempts.get(key, 0) + 1
        if attempts[key] < 2:
            raise httpx.ConnectTimeout("slow", request=request)
        return httpx.Response(200, content=JPEG)

    _dl, result, _w, _s = run_job(make_plan(repo), handler, tmp_path)
    assert result.fetched == 12


def test_exhausted_retries_mark_the_tile_failed_not_missing(repo, tmp_path):
    def handler(request):
        return httpx.Response(503) if "7148" in str(request.url) else httpx.Response(200, content=JPEG)

    options = DownloadOptions(concurrency=2, rps=0, retries=1, backoff_base=0.001)
    _dl, result, _w, state = run_job(make_plan(repo), handler, tmp_path, options=options)

    assert result.failed == 4 and result.fetched == 8
    assert not result.complete          # failures mean the archive is incomplete
    counts = state.counts()
    assert counts["failed"] == 4


def test_pushback_lowers_the_rate_and_says_so(repo, tmp_path):
    """429 must slow the whole job down, not just the tile that hit it."""
    def handler(request):
        return httpx.Response(429, headers={"Retry-After": "0"})

    options = DownloadOptions(
        concurrency=2, rps=50, adaptive=True, retries=1, backoff_base=0.001
    )
    _dl, result, _w, _s = run_job(make_plan(repo), handler, tmp_path, options=options)

    assert result.failed == 12
    assert any("pushed back" in w for w in result.warnings)


def test_503_counts_as_pushback_too(repo, tmp_path):
    """A CDN shedding load is telling us the same thing a 429 does."""
    def handler(request):
        return httpx.Response(503)

    options = DownloadOptions(
        concurrency=2, rps=50, adaptive=True, retries=1, backoff_base=0.001
    )
    _dl, result, _w, _s = run_job(make_plan(repo), handler, tmp_path, options=options)

    assert any("pushed back" in w for w in result.warnings)


def test_adaptive_can_be_turned_off(repo, tmp_path):
    def handler(request):
        return httpx.Response(429, headers={"Retry-After": "0"})

    options = DownloadOptions(
        concurrency=2, rps=50, adaptive=False, retries=1, backoff_base=0.001
    )
    _dl, result, _w, _s = run_job(make_plan(repo), handler, tmp_path, options=options)

    assert result.failed == 12
    assert not any("pushed back" in w for w in result.warnings)


def test_a_clean_job_reports_no_throttling(repo, tmp_path):
    options = DownloadOptions(concurrency=2, rps=50, adaptive=True, retries=1)
    _dl, result, _w, _s = run_job(
        make_plan(repo), lambda r: httpx.Response(200, content=JPEG), tmp_path, options=options
    )
    assert result.fetched == 12
    assert not any("pushed back" in w for w in result.warnings)


def test_a_block_partway_through_is_caught(repo, tmp_path):
    """The failure this exists for: fine at first, then refused.

    `_check_all_missing` only fires before the first success, so a job that
    starts working and is then rate-limited used to record every remaining
    tile as permanently missing and report zero failures.
    """
    served = {"n": 0}

    def handler(request):
        served["n"] += 1
        # Two real tiles, then a burst of 403s, then the throttle eases. The
        # verification re-request lands after it eases and comes back with a
        # picture -- which is what gives the "missing" verdicts away as false.
        if served["n"] <= 2:
            return httpx.Response(200, content=JPEG)
        if served["n"] <= 5:
            return httpx.Response(403)
        return httpx.Response(200, content=JPEG)

    plan = make_plan(repo, min_zoom=14, max_zoom=14)
    options = DownloadOptions(concurrency=1, rps=0, retries=1, missing_run_probe=3)

    with pytest.raises(TilearcError) as excinfo:
        run_job(plan, handler, tmp_path, options=options)

    message = str(excinfo.value)
    assert "refusing requests" in message
    assert "--retry-missing" in message


def test_a_genuinely_sparse_park_is_not_mistaken_for_a_block(repo, tmp_path):
    """Long runs of missing are normal: bounds are rectangles, coverage is not."""
    def handler(request):
        # A stable hole: these tiles are absent on the re-request too.
        return httpx.Response(200, content=JPEG) if "7148" in str(request.url) \
            else httpx.Response(404)

    plan = make_plan(repo, min_zoom=14, max_zoom=14)
    options = DownloadOptions(concurrency=1, rps=0, retries=1, missing_run_probe=2)
    _dl, result, _w, _s = run_job(plan, handler, tmp_path, options=options)

    assert result.fetched == 4 and result.missing == 8
    assert result.failed == 0


def test_missing_run_check_can_be_disabled(repo, tmp_path):
    def handler(request):
        return httpx.Response(200, content=JPEG) if "7148" in str(request.url) \
            else httpx.Response(403)

    options = DownloadOptions(concurrency=1, rps=0, retries=1, missing_run_probe=0)
    _dl, result, _w, _s = run_job(make_plan(repo), handler, tmp_path, options=options)
    assert result.missing == 8


def test_a_success_resets_the_missing_run(repo, tmp_path):
    """Alternating hit/miss must never look like a run, however long the job."""
    def handler(request):
        return httpx.Response(404) if "7148" in str(request.url) \
            else httpx.Response(200, content=JPEG)

    options = DownloadOptions(concurrency=1, rps=0, retries=1, missing_run_probe=3)
    _dl, result, _w, _s = run_job(make_plan(repo), handler, tmp_path, options=options)
    assert result.fetched == 8 and result.missing == 4


# ---------------------------------------------------------------------------
# circuit breakers
# ---------------------------------------------------------------------------


def test_auth_status_aborts_immediately(repo, tmp_path):
    source = TileSource(
        name="tdr-direct",
        template="https://o.test/z{z}/{x}_{y}.jpg",
        missing_statuses=frozenset({404}),
        auth_statuses=frozenset({403}),
    )
    with pytest.raises(CredentialsExpiredError, match="rejected our credentials"):
        run_job(make_plan(repo), lambda r: httpx.Response(403), tmp_path, source=source)


def test_everything_missing_on_the_worker_reads_as_expired_cookies(repo, tmp_path):
    source = TileSource(
        name="tdr",
        template="https://w.test/z{z}/{x}_{y}.jpg",
        missing_statuses=frozenset({204, 404}),
        uses_shared_proxy=True,
        all_missing_hint="the CloudFront cookies on the worker have expired",
    )
    plan = make_plan(repo, park="tdr", version="20260122183830", min_zoom=16, max_zoom=16)
    options = DownloadOptions(concurrency=2, rps=0, retries=1, all_missing_probe=50)

    with pytest.raises(CredentialsExpiredError, match="expired"):
        run_job(plan, lambda r: httpx.Response(204), tmp_path, options=options, source=source)


def test_all_missing_probe_does_not_fire_when_tiles_are_arriving(repo, tmp_path):
    """Tiles are iterated row-major, so 7150 is the last row -- successes come first."""
    def handler(request):
        return httpx.Response(404) if "7150" in str(request.url) else httpx.Response(200, content=JPEG)

    options = DownloadOptions(concurrency=1, rps=0, retries=1, all_missing_probe=2)
    _dl, result, _w, _s = run_job(make_plan(repo), handler, tmp_path, options=options)
    assert result.fetched == 8 and result.missing == 4


def test_credentials_are_sent_as_a_cookie_header(repo, tmp_path):
    seen = []

    def handler(request):
        seen.append(request.headers.get("cookie"))
        return httpx.Response(200, content=JPEG)

    source = TileSource(
        name="tdr-direct",
        template="https://o.test/z{z}/{x}_{y}.jpg",
        headers={"User-Agent": "TokyoDisneyResortApp/3.11.8", "Referer": "https://ref.test/"},
        cookies={"CloudFront-Policy": "p", "CloudFront-Signature": "s"},
    )
    run_job(make_plan(repo), handler, tmp_path, source=source)
    assert set(seen) == {"CloudFront-Policy=p; CloudFront-Signature=s"}


def test_long_failure_streak_stops_the_job(repo, tmp_path):
    plan = make_plan(repo, park="wdw", version="47", min_zoom=11, max_zoom=11)
    options = DownloadOptions(
        concurrency=1, rps=0, retries=0, backoff_base=0.001, error_streak_limit=3
    )
    with pytest.raises(TilearcError, match="failed in a row"):
        run_job(plan, lambda r: httpx.Response(500), tmp_path, options=options)


# ---------------------------------------------------------------------------
# resume
# ---------------------------------------------------------------------------


def test_resume_skips_completed_tiles(repo, tmp_path):
    plan = make_plan(repo)
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, content=JPEG)

    state_path = tmp_path / "s.sqlite"
    state = JobState(state_path)
    run_job(plan, handler, tmp_path, state=state)
    state.close()
    assert len(calls) == 12

    calls.clear()
    state = JobState(state_path)
    _dl, result, _w, _s = run_job(plan, handler, tmp_path, state=state)
    state.close()

    assert calls == []                 # nothing re-fetched
    assert result.skipped == 12
    assert result.fetched == 0


def test_resume_does_not_re_probe_missing_tiles(repo, tmp_path):
    """The politeness requirement: known-absent tiles are never asked for twice."""
    plan = make_plan(repo)
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(404)

    state_path = tmp_path / "s.sqlite"
    state = JobState(state_path)
    run_job(plan, handler, tmp_path, state=state)
    state.close()
    assert len(calls) == 12

    calls.clear()
    state = JobState(state_path)
    _dl, result, _w, _s = run_job(plan, handler, tmp_path, state=state)
    state.close()
    assert calls == []
    assert result.skipped == 12


def test_resume_retries_failed_tiles(repo, tmp_path):
    plan = make_plan(repo)
    mode = {"fail": True}

    def handler(request):
        if mode["fail"] and "7148" in str(request.url):
            return httpx.Response(503)
        return httpx.Response(200, content=JPEG)

    options = DownloadOptions(concurrency=2, rps=0, retries=0, backoff_base=0.001)
    state_path = tmp_path / "s.sqlite"

    state = JobState(state_path)
    _dl, first, _w, _s = run_job(plan, handler, tmp_path, options=options, state=state)
    state.close()
    assert first.failed == 4

    mode["fail"] = False
    state = JobState(state_path)
    _dl, second, _w, state2 = run_job(plan, handler, tmp_path, options=options, state=state)
    assert second.fetched == 4          # only the four failures were retried
    assert second.skipped == 8
    assert state2.counts() == {STATUS_DONE: 12}
    state.close()


def test_fingerprint_ignores_politeness_settings_but_not_tile_selection(repo):
    """Tuning concurrency between runs must not invalidate the resume state."""
    a = make_plan(repo, min_zoom=14, max_zoom=14)
    b = make_plan(repo, min_zoom=14, max_zoom=14)
    assert a.fingerprint() == b.fingerprint()

    c = make_plan(repo, min_zoom=14, max_zoom=15)
    assert c.fingerprint() != a.fingerprint()

    d = build_plan(repo.park("hkdl"), repo.version("hkdl", "21"), min_zoom=14, max_zoom=14)
    assert d.fingerprint() != a.fingerprint()


def test_fingerprint_changes_when_a_version_overrides_the_template(repo):
    from tilearc.config import VersionEntry

    park = repo.park("dlp")
    plain = build_plan(park, repo.version("dlp", "current"), min_zoom=13, max_zoom=13)
    override = build_plan(
        park,
        VersionEntry(code="current", url="https://other.test/{z}/{x}/{y}.jpg"),
        min_zoom=13,
        max_zoom=13,
    )
    assert plain.fingerprint() != override.fingerprint()


# ---------------------------------------------------------------------------
# rate limiting
# ---------------------------------------------------------------------------


def test_rate_limiter_enforces_a_ceiling():
    import time

    from tilearc.ratelimit import RateLimiter

    async def drain():
        limiter = RateLimiter(20, burst=1)
        start = time.monotonic()
        for _ in range(6):
            await limiter.acquire()
        return time.monotonic() - start

    # 6 tokens at 20/s from a burst of 1 needs at least 5 refills = 0.25s.
    assert asyncio.run(drain()) >= 0.2


def test_rate_limiter_rejects_nonsense():
    from tilearc.ratelimit import RateLimiter

    with pytest.raises(ValueError):
        RateLimiter(0)
