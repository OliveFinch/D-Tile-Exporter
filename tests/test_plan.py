"""Planning a job from measured coverage instead of declared bounds.

The configs declare a rectangle per zoom. What the servers serve is not always
that rectangle, and it is wrong in both directions: Disneyland Paris declares
fourteen times the tiles it has, while WDW z12 declares four columns fewer than
it serves. The first only wastes requests. The second loses tiles, quietly,
which is why the coverage file exists at all.
"""

from __future__ import annotations

import pytest

from tilearc.config import ParkConfig, TileBounds, VersionEntry


def _park(bounds, *, min_zoom, max_zoom, park_id="wdw", y_scheme="xyz"):
    return ParkConfig(
        park_id=park_id, label="Test Park",
        tile_template="https://cdn/{code}/{z}/{x}/{y}.jpg",
        min_zoom=min_zoom, max_zoom=max_zoom, y_scheme=y_scheme,
        bounds_by_zoom=bounds,
    )




# ---------------------------------------------------------------------------
# planning from measured coverage rather than declared bounds
# ---------------------------------------------------------------------------


def _coverage_file(tmp_path, park_id, zooms):
    import json
    path = tmp_path / "coverage.json"
    path.write_text(json.dumps({"maps": {park_id: {"zooms": zooms}}}))
    return path


def test_coverage_narrows_a_box_that_claims_more_than_the_server_serves(tmp_path):
    """Disneyland Paris declares 14 times the tiles it has."""
    from tilearc.plan import build_plan, load_coverage

    park = _park(bounds={13: TileBounds(4156, 4161, 2816, 2819)}, min_zoom=13, max_zoom=13)
    path = _coverage_file(tmp_path, park.park_id, {
        "13": {"box": {"minX": 4158, "maxX": 4160, "minY": 2816, "maxY": 2819},
               "tiles": 12, "shape": "rectangle"},
    })
    plan = build_plan(park, VersionEntry(code="v"),
                      coverage=load_coverage(path, park.park_id))

    assert plan.total_tiles == 12                 # not the declared 24
    assert plan.zooms[0].bounds == TileBounds(4158, 4160, 2816, 2819)
    assert any("does not serve" in note for note in plan.notes)


def test_coverage_widens_a_box_that_would_have_missed_real_tiles(tmp_path):
    """WDW z12's declared minX is four columns east of where imagery starts."""
    from tilearc.plan import build_plan, load_coverage

    park = _park(bounds={12: TileBounds(1118, 1125, 1706, 1715)}, min_zoom=12, max_zoom=12)
    path = _coverage_file(tmp_path, park.park_id, {
        "12": {"box": {"minX": 1114, "maxX": 1125, "minY": 1706, "maxY": 1715},
               "tiles": 120, "shape": "rectangle"},
    })
    plan = build_plan(park, VersionEntry(code="v"),
                      coverage=load_coverage(path, park.park_id))

    assert plan.total_tiles == 120                # the declared plan fetches 80
    tiles = {(x, y) for _z, x, y, _m in plan.iter_tiles()}
    assert (1114, 1706) in tiles
    assert any("extends past its declared bounds" in note for note in plan.notes)


def test_an_irregular_footprint_is_planned_as_runs_not_a_box(tmp_path):
    """Hong Kong z19 is an L: its box is 15% tiles that do not exist."""
    from tilearc.plan import build_plan, load_coverage

    park = _park(bounds={19: TileBounds(428192, 428255, 228752, 228815)},
                 min_zoom=19, max_zoom=19)
    path = _coverage_file(tmp_path, park.park_id, {
        "19": {"box": {"minX": 428207, "maxX": 428242, "minY": 228756, "maxY": 228807},
               "tiles": 1592, "shape": "irregular",
               "runs": [[228756, 228769, 428207, 428222],
                        [228770, 228807, 428207, 428242]]},
    })
    plan = build_plan(park, VersionEntry(code="v"),
                      coverage=load_coverage(path, park.park_id))

    assert plan.total_tiles == 1592               # not the 1,872 its box holds
    tiles = {(x, y) for _z, x, y, _m in plan.iter_tiles()}
    assert len(tiles) == 1592
    assert (428242, 228756) not in tiles          # in the box, in the notch
    assert (428242, 228770) in tiles              # in the box, in the map


def test_a_zoom_that_serves_nothing_is_dropped(tmp_path):
    """DLP declares a z20 box of 393,216 tiles and serves none of them."""
    from tilearc.plan import build_plan, load_coverage

    park = _park(bounds={19: TileBounds(0, 1, 0, 1), 20: TileBounds(0, 3, 0, 3)},
                 min_zoom=19, max_zoom=20)
    path = _coverage_file(tmp_path, park.park_id, {
        "19": {"box": {"minX": 0, "maxX": 1, "minY": 0, "maxY": 1}, "tiles": 4,
               "shape": "rectangle"},
        "20": {"box": None, "tiles": 0, "shape": "empty"},
    })
    plan = build_plan(park, VersionEntry(code="v"),
                      coverage=load_coverage(path, park.park_id))

    assert [zp.zoom for zp in plan.zooms] == [19]
    assert any("serves nothing" in note for note in plan.notes)


def test_a_zoom_below_the_declared_minimum_is_added_when_it_is_measured(tmp_path):
    """Shanghai declares minZoom 14 and serves z12 and z13 perfectly well."""
    from tilearc.plan import build_plan, load_coverage

    park = _park(bounds={13: TileBounds(1651, 1655, 441, 444),
                         14: TileBounds(3302, 3310, 882, 889)},
                 min_zoom=14, max_zoom=14)
    path = _coverage_file(tmp_path, park.park_id, {
        "13": {"box": {"minX": 1651, "maxX": 1655, "minY": 441, "maxY": 444},
               "tiles": 20, "shape": "rectangle"},
        "14": {"box": {"minX": 3302, "maxX": 3310, "minY": 882, "maxY": 889},
               "tiles": 72, "shape": "rectangle"},
    })
    plan = build_plan(park, VersionEntry(code="v"),
                      coverage=load_coverage(path, park.park_id))

    assert [zp.zoom for zp in plan.zooms] == [13, 14]
    assert plan.total_tiles == 92
    assert any("outside the declared zoom range" in note for note in plan.notes)


def test_an_explicit_zoom_range_still_bounds_what_coverage_adds(tmp_path):
    from tilearc.plan import build_plan, load_coverage

    park = _park(bounds={14: TileBounds(0, 1, 0, 1)}, min_zoom=14, max_zoom=14)
    path = _coverage_file(tmp_path, park.park_id, {
        "12": {"box": {"minX": 0, "maxX": 1, "minY": 0, "maxY": 1}, "tiles": 4,
               "shape": "rectangle"},
        "14": {"box": {"minX": 0, "maxX": 1, "minY": 0, "maxY": 1}, "tiles": 4,
               "shape": "rectangle"},
    })
    plan = build_plan(park, VersionEntry(code="v"), min_zoom=14,
                      coverage=load_coverage(path, park.park_id))

    assert [zp.zoom for zp in plan.zooms] == [14]


def test_a_measured_plan_does_not_resume_from_a_declared_one(tmp_path):
    """Different tile sets must not share resume state."""
    from tilearc.plan import build_plan, load_coverage

    park = _park(bounds={12: TileBounds(1118, 1125, 1706, 1715)}, min_zoom=12, max_zoom=12)
    path = _coverage_file(tmp_path, park.park_id, {
        "12": {"box": {"minX": 1114, "maxX": 1125, "minY": 1706, "maxY": 1715},
               "tiles": 120, "shape": "rectangle"},
    })
    declared = build_plan(park, VersionEntry(code="v"))
    measured = build_plan(park, VersionEntry(code="v"),
                          coverage=load_coverage(path, park.park_id))

    assert declared.fingerprint() != measured.fingerprint()


def test_a_coverage_file_without_this_park_says_so(tmp_path):
    from tilearc.plan import load_coverage
    from tilearc.errors import TilearcError

    path = _coverage_file(tmp_path, "dlr", {})
    with pytest.raises(TilearcError, match="no measured coverage for 'wdw'"):
        load_coverage(path, "wdw")
