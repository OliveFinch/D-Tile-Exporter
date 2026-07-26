"""Border tracing, against synthetic servers whose true shape we control.

The point of tracing over edge-measuring is that it can tell shapes apart, so
most of these define a shape that is *not* a rectangle and check the footprint
comes back as that shape rather than as the box around it.

The TDR-flavoured tests are about the third outcome. A tile probe can come back
present, absent, or refused, and the last two are only distinguishable when the
server bothers to distinguish them -- direct TDR does, the Worker does not.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from tilearc.config import TileBounds
from tilearc.errors import CredentialsError
from tilearc.trace import (
    TraceOptions,
    TraceRefused,
    coverage_payload,
    fill_region,
    group_spans,
    merge_spans,
    trace,
)
from tilearc.urls import TileSource

JPEG = b"\xff\xd8" + b"\x00" * 64 + b"\xff\xd9"
TEMPLATE = "https://tiles.test/{z}/{x}/{y}.jpg"


def source(**kwargs) -> TileSource:
    return TileSource(name="test", template=TEMPLATE, **kwargs)


def run(shape, declared, *, options=None, src=None, zoom=10, handler=None):
    """Trace one zoom of a world where `shape(x, y)` says what exists."""
    calls: list[tuple[int, int]] = []

    def default_handler(request: httpx.Request) -> httpx.Response:
        z, x, y = (int(p) for p in request.url.path.strip("/").removesuffix(".jpg").split("/"))
        calls.append((x, y))
        return (
            httpx.Response(200, content=JPEG) if shape(x, y)
            else httpx.Response(404, content=b"")
        )

    async def go():
        transport = httpx.MockTransport(handler or default_handler)
        async with httpx.AsyncClient(transport=transport) as client:
            return await trace(
                src or source(),
                [(zoom, declared)],
                options or TraceOptions(rps=0, concurrency=4),
                client=client,
            )

    return asyncio.run(go())[0], calls


def tiles_of(result) -> set[tuple[int, int]]:
    """Every tile the trace claims, rebuilt from its runs."""
    claimed = set()
    for y0, y1, x0, x1 in result.runs():
        for y in range(y0, y1 + 1):
            for x in range(x0, x1 + 1):
                claimed.add((x, y))
    return claimed


def truth_of(shape, box: TileBounds) -> set[tuple[int, int]]:
    return {
        (x, y)
        for y in range(box.min_y - 5, box.max_y + 6)
        for x in range(box.min_x - 5, box.max_x + 6)
        if shape(x, y)
    }


# ---------------------------------------------------------------------------
# shapes
# ---------------------------------------------------------------------------


DECLARED = TileBounds(100, 140, 200, 235)


def test_a_rectangle_is_reported_as_one():
    shape = lambda x, y: 104 <= x <= 130 and 203 <= y <= 224
    result, _calls = run(shape, DECLARED)

    assert result.complete
    assert result.rectangle
    assert result.box == TileBounds(104, 130, 203, 224)
    assert result.covered == 27 * 22


def test_an_l_is_not_reported_as_the_box_around_it():
    """The whole reason this exists: HKDL is an L and its box is 15% empty."""
    shape = lambda x, y: (104 <= x <= 130 and 203 <= y <= 212) or (
        104 <= x <= 114 and 203 <= y <= 228
    )
    result, _calls = run(shape, DECLARED)

    assert result.complete
    assert not result.rectangle
    # The box is right, but it is not the footprint.
    assert result.box == TileBounds(104, 130, 203, 228)
    assert result.box.count == 27 * 26
    assert result.covered == len(truth_of(shape, DECLARED))
    assert tiles_of(result) == truth_of(shape, DECLARED)
    assert "irregular" in result.describe()


def test_a_concave_shape_is_not_bridged():
    """A U, open at the top. Taking min/max x per row would fill the gap between
    its arms and claim tiles that are not there -- which is exactly why the fill
    floods inward from outside instead."""
    shape = lambda x, y: (
        104 <= x <= 130 and 203 <= y <= 224 and not (112 <= x <= 122 and 203 <= y <= 215)
    )
    result, _calls = run(shape, DECLARED)

    assert result.complete
    assert tiles_of(result) == truth_of(shape, DECLARED)
    # Rows crossing both arms are two runs, not one bridged run.
    across = [item for item in result.runs() if item[0] <= 210 <= item[1]]
    assert len(across) == 2
    assert sorted(across) == [(203, 215, 104, 111), (203, 215, 123, 130)]


def test_two_separate_blobs_are_both_found():
    shape = lambda x, y: (104 <= x <= 112 and 203 <= y <= 210) or (
        124 <= x <= 132 and 220 <= y <= 228
    )
    result, _calls = run(shape, DECLARED)

    assert len(result.regions) == 2
    assert result.covered == 9 * 8 + 9 * 9
    assert tiles_of(result) == truth_of(shape, DECLARED)


def test_an_empty_zoom_is_an_answer_not_a_failure():
    result, _calls = run(lambda x, y: False, DECLARED)

    assert result.regions == []
    assert result.covered == 0
    assert result.complete           # searched, and there is nothing
    assert result.describe() == "no imagery at this zoom"


def test_the_interior_is_never_swept():
    """Cost must scale with the perimeter, or tracing has no point."""
    shape = lambda x, y: 104 <= x <= 130 and 203 <= y <= 224
    _result, calls = run(shape, DECLARED)

    deep = [
        (x, y) for x, y in calls
        if all(shape(x + dx, y + dy) for dx in (-2, -1, 0, 1, 2) for dy in (-2, -1, 0, 1, 2))
    ]
    assert len(deep) < 0.05 * (27 * 22)


# ---------------------------------------------------------------------------
# the window
# ---------------------------------------------------------------------------


def test_a_border_reaching_the_window_is_not_called_a_measurement():
    """Imagery running past the search margin is a floor, not an answer."""
    shape = lambda x, y: 104 <= x <= 130 and 203 <= y <= 400
    result, _calls = run(shape, DECLARED, options=TraceOptions(rps=0, margin=8))

    assert not result.complete
    assert "maxY" in result.clipped
    assert "search window" in result.describe()


def test_widening_the_window_finds_the_real_edge():
    shape = lambda x, y: 104 <= x <= 130 and 203 <= y <= 250
    result, _calls = run(shape, DECLARED, options=TraceOptions(rps=0, margin=40))

    assert result.complete
    assert result.box == TileBounds(104, 130, 203, 250)


# ---------------------------------------------------------------------------
# refusals -- the TDR-shaped problem
# ---------------------------------------------------------------------------


def test_direct_tdr_treats_403_as_a_rejected_signature_not_a_missing_tile():
    """Going direct, the two are distinguishable, so nothing should be guessed."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, content=b"")

    tdr = source(missing_statuses=frozenset({404}), auth_statuses=frozenset({401, 403}))
    with pytest.raises(CredentialsError, match="CloudFront"):
        run(lambda x, y: True, DECLARED, src=tdr, handler=handler)


def test_a_sustained_outage_aborts_rather_than_drawing_a_border_round_it():
    served = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        served["n"] += 1
        if served["n"] > 6:
            return httpx.Response(503, content=b"")
        return httpx.Response(200, content=JPEG)

    with pytest.raises(TraceRefused, match="refused or timed out"):
        run(
            lambda x, y: True, DECLARED, handler=handler,
            options=TraceOptions(rps=0, retries=0, refusal_limit=4, concurrency=1),
        )


def test_the_audit_catches_a_throttle_that_truncated_the_walk():
    """A refusal answered as 'absent' closes the walk early, tidily, and wrongly.

    This is the Worker's 204 in miniature: refusals are reported as missing, so
    only asking again can tell the difference.
    """
    shape = lambda x, y: 104 <= x <= 130 and 203 <= y <= 224
    served = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        x, y = (int(p) for p in request.url.path.strip("/").removesuffix(".jpg").split("/")[1:])
        served["n"] += 1
        # Answer honestly for a while, then call everything missing -- and
        # recover before the audit runs, which is what a rate limiter does.
        if 12 < served["n"] <= 260 and shape(x, y):
            return httpx.Response(404, content=b"")
        return httpx.Response(200, content=JPEG) if shape(x, y) else httpx.Response(404, content=b"")

    result, _calls = run(shape, DECLARED, handler=handler, options=TraceOptions(rps=0, concurrency=1))

    assert not result.complete
    assert result.flipped, "tiles called absent should have answered on a second ask"
    assert "unreliable" in result.describe()


def test_a_clean_run_passes_the_audit():
    shape = lambda x, y: 104 <= x <= 130 and 203 <= y <= 224
    result, _calls = run(shape, DECLARED)

    assert result.flipped == []
    assert result.dead == []
    assert result.complete


# ---------------------------------------------------------------------------
# geometry helpers
# ---------------------------------------------------------------------------


def test_fill_uses_a_flood_so_a_hole_is_not_split_off():
    """A sealed hole is claimed. The border cannot see it, and that is the deal."""
    shape = lambda x, y: (
        0 <= x <= 20 and 0 <= y <= 20 and not (5 <= x <= 9 and 5 <= y <= 9)
    )
    border = {
        (x, y)
        for y in range(-1, 22)
        for x in range(-1, 22)
        if shape(x, y)
        and any(not shape(x + dx, y + dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1))
    }
    _spans, count = fill_region(border, TileBounds(0, 20, 0, 20))
    assert count == 21 * 21          # the 5x5 hole is inside the outer border


def test_merge_counts_overlapping_regions_once():
    a = [(5, 0, 10)]
    b = [(5, 8, 15)]
    assert merge_spans([a, b]) == [(5, 0, 15)]


def test_group_collapses_identical_consecutive_rows():
    spans = [(y, 0, 9) for y in range(100, 110)] + [(y, 0, 3) for y in range(110, 116)]
    assert group_spans(spans) == [(100, 109, 0, 9), (110, 115, 0, 3)]


def test_group_keeps_a_row_with_two_runs_as_two_entries():
    spans = [(5, 0, 3), (5, 8, 11), (6, 0, 3), (6, 8, 11)]
    assert group_spans(spans) == [(5, 6, 0, 3), (5, 6, 8, 11)]


def test_coverage_payload_omits_runs_for_a_plain_rectangle():
    shape = lambda x, y: 104 <= x <= 130 and 203 <= y <= 224
    result, _calls = run(shape, DECLARED)
    payload = coverage_payload([result])["10"]

    assert payload["shape"] == "rectangle"
    assert payload["tiles"] == 27 * 22
    assert "runs" not in payload         # the box says it all


def test_coverage_payload_carries_runs_for_an_irregular_footprint():
    shape = lambda x, y: (104 <= x <= 130 and 203 <= y <= 212) or (
        104 <= x <= 114 and 203 <= y <= 228
    )
    result, _calls = run(shape, DECLARED)
    payload = coverage_payload([result])["10"]

    assert payload["shape"] == "irregular"
    assert payload["runs"] == [[203, 212, 104, 130], [213, 228, 104, 114]]
