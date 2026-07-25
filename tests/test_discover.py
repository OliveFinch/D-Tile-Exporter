"""Bounds discovery, against synthetic servers whose true extent we control.

Every test here defines exactly which tiles exist, then checks the search finds
that rectangle regardless of how wrong the declared bounds were.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from tilearc.config import TileBounds
from tilearc.discover import (
    ProbeOptions,
    ZoomMeasurement,
    bounds_block,
    discover,
    estimate_requests,
    measurements_to_json,
    patch_config_text,
    sample_points,
)
from tilearc.urls import TileSource

TEMPLATE = "https://tiles.test/{z}/{x}/{y}.jpg"
JPEG = b"\xff\xd8" + b"\x00" * 64 + b"\xff\xd9"


def server(truth: dict[int, TileBounds], *, holes: set[tuple[int, int, int]] | None = None):
    """A transport where tiles exist exactly inside `truth` (minus `holes`)."""
    holes = holes or set()
    calls: list[tuple[int, int, int]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        z, x, y = (int(part) for part in request.url.path.strip("/").removesuffix(".jpg").split("/"))
        calls.append((z, x, y))
        bounds = truth.get(z)
        inside = (
            bounds is not None
            and bounds.min_x <= x <= bounds.max_x
            and bounds.min_y <= y <= bounds.max_y
            and (z, x, y) not in holes
        )
        return httpx.Response(200, content=JPEG) if inside else httpx.Response(404)

    return handler, calls


def run(declared: dict[int, TileBounds], truth, *, options=None, holes=None):
    handler, calls = server(truth, holes=holes)
    source = TileSource(name="t", template=TEMPLATE)
    options = options or ProbeOptions(rps=0, samples=5)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await discover(
                source, sorted(declared.items()), options, client=client
            )

    return asyncio.run(go()), calls


# ---------------------------------------------------------------------------
# the easy case: the config is already right
# ---------------------------------------------------------------------------


def test_correct_bounds_are_confirmed_unchanged():
    truth = {14: TileBounds(100, 119, 200, 219)}
    results, calls = run(dict(truth), truth)

    assert len(results) == 1
    assert results[0].measured == truth[14]
    assert results[0].changed is False
    assert results[0].describe_change() == "unchanged"


def test_confirming_correct_bounds_is_cheap():
    """The common case must not cost much, or nobody will run it."""
    truth = {z: TileBounds(100 * 2**i, 119 * 2**i, 200 * 2**i, 219 * 2**i)
             for i, z in enumerate(range(14, 20))}
    results, calls = run(dict(truth), truth)

    assert all(not m.changed for m in results)
    # Six zooms, verified for well under the pessimistic upper bound.
    assert len(calls) < estimate_requests(6) / 2


# ---------------------------------------------------------------------------
# bounds that are too narrow -- the WDW z12 shape of problem
# ---------------------------------------------------------------------------


def test_finds_content_beyond_a_too_narrow_west_edge():
    truth = {12: TileBounds(1114, 1125, 1706, 1715)}
    declared = {12: TileBounds(1118, 1125, 1706, 1715)}     # minX 4 tiles too high

    results, _calls = run(declared, truth)
    assert results[0].measured == truth[12]
    assert "minX" in results[0].describe_change()
    assert results[0].tile_delta == 4 * 10


@pytest.mark.parametrize(
    "declared,truth",
    [
        (TileBounds(100, 119, 200, 219), TileBounds(80, 119, 200, 219)),    # west
        (TileBounds(100, 119, 200, 219), TileBounds(100, 140, 200, 219)),   # east
        (TileBounds(100, 119, 200, 219), TileBounds(100, 119, 180, 219)),   # north
        (TileBounds(100, 119, 200, 219), TileBounds(100, 119, 200, 250)),   # south
        (TileBounds(100, 119, 200, 219), TileBounds(60, 160, 150, 260)),    # all four
    ],
)
def test_expands_in_every_direction(declared, truth):
    results, _calls = run({15: declared}, {15: truth})
    assert results[0].measured == truth


def test_expansion_is_exponential_not_linear():
    """A far-away true edge must not cost one request per tile."""
    declared = {16: TileBounds(1000, 1010, 2000, 2010)}
    truth = {16: TileBounds(1000, 1400, 2000, 2010)}       # 390 tiles further east

    results, calls = run(declared, truth)
    assert results[0].measured == truth[16]
    assert len(calls) < 400, "should binary-search, not walk"


# ---------------------------------------------------------------------------
# bounds that are too wide
# ---------------------------------------------------------------------------


def test_contracts_when_the_declared_box_is_too_big():
    declared = {14: TileBounds(100, 200, 200, 300)}
    truth = {14: TileBounds(120, 150, 210, 240)}

    results, _calls = run(declared, truth)
    assert results[0].measured == truth[14]
    assert results[0].tile_delta < 0


def test_reports_a_zoom_with_no_tiles_at_all():
    declared = {21: TileBounds(100, 119, 200, 219)}
    results, _calls = run(declared, {})          # nothing exists at z21

    assert results[0].measured is None
    assert "no tiles found" in results[0].notes[0]
    assert results[0].describe_change() == "no tiles found"


# ---------------------------------------------------------------------------
# robustness
# ---------------------------------------------------------------------------


def test_holes_inside_the_map_do_not_shrink_the_bounds():
    """Coverage is not a solid rectangle; a gap must not be read as an edge."""
    truth = {15: TileBounds(100, 139, 200, 239)}
    # Punch out most of the westmost column, leaving one tile.
    holes = {(15, 100, y) for y in range(200, 240)} - {(15, 100, 217)}

    results, _calls = run({15: truth[15]}, truth, holes=holes)
    assert results[0].measured.min_x == 100


def test_a_genuinely_empty_column_is_believed():
    truth = {15: TileBounds(101, 139, 200, 239)}
    declared = {15: TileBounds(100, 139, 200, 239)}       # one column too wide

    results, _calls = run(declared, truth)
    assert results[0].measured.min_x == 101


def test_transient_server_errors_are_retried():
    truth = {14: TileBounds(100, 119, 200, 219)}
    failures = {"left": 6}

    def handler(request: httpx.Request) -> httpx.Response:
        if failures["left"] > 0:
            failures["left"] -= 1
            return httpx.Response(503)
        z, x, y = (int(p) for p in request.url.path.strip("/").removesuffix(".jpg").split("/"))
        bounds = truth[z]
        inside = bounds.min_x <= x <= bounds.max_x and bounds.min_y <= y <= bounds.max_y
        return httpx.Response(200, content=JPEG) if inside else httpx.Response(404)

    source = TileSource(name="t", template=TEMPLATE)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await discover(
                source, [(14, truth[14])],
                ProbeOptions(rps=0, samples=5, retries=5, backoff_base=0.001), client=client,
            )

    results = asyncio.run(go())
    assert results[0].measured == truth[14]
    assert failures["left"] == 0


def test_a_range_request_is_used_to_avoid_downloading_whole_tiles():
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("Range"))
        return httpx.Response(206, content=b"\xff")

    source = TileSource(name="t", template=TEMPLATE)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await discover(
                source, [(14, TileBounds(100, 101, 200, 201))],
                ProbeOptions(rps=0, samples=3, max_expand=2), client=client,
            )

    asyncio.run(go())
    assert seen and all(value == "bytes=0-0" for value in seen)


def test_search_limit_is_reported_rather_than_running_away():
    declared = {16: TileBounds(1000, 1010, 2000, 2010)}
    truth = {16: TileBounds(1000, 99999, 2000, 2010)}

    results, _calls = run(
        declared, truth, options=ProbeOptions(rps=0, samples=3, max_expand=16)
    )
    assert any("search limit" in note for note in results[0].notes)


def test_sample_points_spans_the_range_and_includes_the_ends():
    assert sample_points(10, 20, 5) == [10, 12, 15, 18, 20]
    assert sample_points(10, 12, 9) == [10, 11, 12]      # exhaustive when small
    assert sample_points(7, 7, 5) == [7]


# ---------------------------------------------------------------------------
# writing the result back into a config
# ---------------------------------------------------------------------------


CONFIG = """{
  "parkId": "wdw",
  "name": "Walt Disney World",
  "minZoom": 11,
  "maxZoom": 12,
  "defaultCenter": [-81.567406, 28.386276],
  "boundsByZoom": {
    "11": { "minX": 555, "maxX": 564, "minY": 851, "maxY": 859 },
    "12": { "minX": 1118, "maxX": 1125, "minY": 1706, "maxY": 1715 }
  },
  "trailing": true
}
"""


def measurements():
    return [
        ZoomMeasurement(11, TileBounds(555, 564, 851, 859), TileBounds(555, 564, 851, 859)),
        ZoomMeasurement(12, TileBounds(1118, 1125, 1706, 1715), TileBounds(1114, 1125, 1706, 1715)),
    ]


def test_patch_replaces_only_the_bounds():
    updated = patch_config_text(CONFIG, measurements(), "801755166")
    parsed = json.loads(updated)

    assert parsed["boundsByZoom"]["12"]["minX"] == 1114
    assert parsed["boundsByZoom"]["11"]["minX"] == 555
    # Everything else survives untouched, including formatting quirks.
    assert parsed["trailing"] is True
    assert parsed["defaultCenter"] == [-81.567406, 28.386276]
    assert '"defaultCenter": [-81.567406, 28.386276],' in updated
    assert '"name": "Walt Disney World",' in updated


def test_patch_keeps_the_compact_one_line_style():
    updated = patch_config_text(CONFIG, measurements(), "801755166")
    assert '"12": { "minX": 1114, "maxX": 1125, "minY": 1706, "maxY": 1715 }' in updated


def test_patch_records_provenance():
    updated = patch_config_text(CONFIG, measurements(), "801755166")
    parsed = json.loads(updated)

    assert parsed["boundsMeasured"]["version"] == "801755166"
    assert parsed["boundsMeasured"]["tool"].startswith("tilearc ")
    assert parsed["boundsMeasured"]["at"]


def test_patching_twice_does_not_duplicate_provenance():
    once = patch_config_text(CONFIG, measurements(), "801755166")
    twice = patch_config_text(once, measurements(), "900014458")

    assert twice.count('"boundsMeasured"') == 1
    assert json.loads(twice)["boundsMeasured"]["version"] == "900014458"


def test_patched_config_reloads_through_the_normal_parser(tmp_path):
    from tilearc.config import DirConfigSource, ParkRepository

    park_dir = tmp_path / "wdw"
    park_dir.mkdir()
    (park_dir / "wdw_config.json").write_text(
        patch_config_text(CONFIG, measurements(), "801755166")
    )
    (park_dir / "wdw_dis_servers.json").write_text('[{"code": "1", "active": 1}]')

    park = ParkRepository(DirConfigSource(tmp_path)).park("wdw")
    assert park.bounds_at(12) == TileBounds(1114, 1125, 1706, 1715)


def test_bounds_block_renders_every_measured_zoom():
    block = bounds_block(measurements())
    assert block.startswith('  "boundsByZoom": {')
    assert block.rstrip().endswith("}")
    assert block.count('"minX"') == 2


def test_measurements_to_json_skips_unmeasured_zooms():
    rows = measurements() + [ZoomMeasurement(13, TileBounds(1, 2, 3, 4), None)]
    payload = measurements_to_json(rows)
    assert set(payload) == {"11", "12"}


# ---------------------------------------------------------------------------
# a full park, using WDW's real declared bounds
# ---------------------------------------------------------------------------


def test_wdw_shaped_run_finds_the_z12_west_edge(repo):
    """The one WDW finding that is a real error, reproduced end to end.

    Truth is the declared config with z12's west edge corrected to 1114 (half
    of z13's 2228). Everything else is left exactly as shipped, including the
    per-zoom cropping that `doctor` mistakes for a problem.
    """
    park = repo.park("wdw")
    declared = {z: park.bounds_at(z) for z in range(11, 21)}

    truth = dict(declared)
    correct_z12 = declared[12]
    truth[12] = TileBounds(1114, correct_z12.max_x, correct_z12.min_y, correct_z12.max_y)

    results, _calls = run(declared, truth)
    by_zoom = {m.zoom: m for m in results}

    assert by_zoom[12].measured == truth[12]
    assert by_zoom[12].tile_delta == 4 * 10
    # Every other level is confirmed as-is -- the cropping is real data.
    assert [m.zoom for m in results if m.changed] == [12]


def test_wdw_shaped_run_costs_little_when_only_one_zoom_is_wrong(repo):
    park = repo.park("wdw")
    declared = {z: park.bounds_at(z) for z in range(11, 21)}
    truth = dict(declared)
    truth[12] = TileBounds(1114, 1125, 1706, 1715)

    _results, calls = run(declared, truth)
    assert len(calls) < estimate_requests(10) / 2
