"""Zoom selection, the TMS flip, and bbox clipping.

The scale-reference numbers asserted here come from the project's own published
table, and they are the strongest available check that zoom selection and
bounds iteration agree with what the viewer actually shows.
"""

from __future__ import annotations

import pytest

from tilearc.bounds import (
    BBox,
    flip_y,
    iter_bounds,
    select_zooms,
    tiles_for_bbox,
    to_server_y,
    to_xyz_y,
)
from tilearc.config import TileBounds
from tilearc.plan import build_plan

# park -> (full depth, to z18, to z17), from the project's scale reference
SCALE_REFERENCE = {
    "wdw": (575_450, 145_370, 37_850),
    "dlr": (484_008, 28_840, 7_336),
    "tdr": (137_645, 8_683, 2_122),
    "dlp": (131_064, 32_760, 8_184),
    "hkdl": (21_852, 1_372, 348),
    "shdr": (12_897, 1_233, 633),
}


def _count(repo, park_id, max_zoom=None):
    park = repo.park(park_id)
    plan = build_plan(park, repo.version(park_id, "x"), max_zoom=max_zoom)
    return plan.tiles_per_mode


@pytest.mark.parametrize("park_id", sorted(SCALE_REFERENCE))
def test_full_depth_counts_match_scale_reference(repo, park_id):
    assert _count(repo, park_id) == SCALE_REFERENCE[park_id][0]


@pytest.mark.parametrize("park_id", sorted(SCALE_REFERENCE))
def test_to_z18_counts_match_scale_reference(repo, park_id):
    assert _count(repo, park_id, max_zoom=18) == SCALE_REFERENCE[park_id][1]


@pytest.mark.parametrize("park_id", sorted(SCALE_REFERENCE))
def test_to_z17_counts_match_scale_reference(repo, park_id):
    assert _count(repo, park_id, max_zoom=17) == SCALE_REFERENCE[park_id][2]


# ---------------------------------------------------------------------------
# zoom selection: minZoom/maxZoom and boundsByZoom disagree in every direction
# ---------------------------------------------------------------------------


def test_shdr_maxzoom_21_has_no_z21_bounds(repo):
    park = repo.park("shdr")
    assert park.max_zoom == 21
    assert 21 not in park.bounds_by_zoom

    selection = select_zooms(park)
    assert selection.zooms == [14, 15, 16, 17, 18, 19, 20]
    assert selection.missing_bounds == [21]
    assert any("no boundsByZoom" in note for note in selection.notes)


def test_shdr_low_zoom_bounds_below_minzoom_are_excluded(repo):
    park = repo.park("shdr")
    assert {9, 10, 11, 12, 13} <= set(park.bounds_by_zoom)
    assert min(select_zooms(park).zooms) == park.min_zoom == 14


def test_tdr_z15_bounds_below_minzoom_are_excluded(repo):
    park = repo.park("tdr")
    assert 15 in park.bounds_by_zoom
    assert park.min_zoom == 16
    assert select_zooms(park).zooms == [16, 17, 18, 19, 20]


def test_dlp_z20_bounds_above_maxzoom_are_excluded(repo):
    park = repo.park("dlp")
    assert 20 in park.bounds_by_zoom
    assert park.max_zoom == 19
    assert select_zooms(park).zooms == [13, 14, 15, 16, 17, 18, 19]


def test_requesting_beyond_the_park_range_is_reported_not_fatal(repo):
    selection = select_zooms(repo.park("hkdl"), min_zoom=10, max_zoom=25)
    assert selection.zooms == [14, 15, 16, 17, 18, 19, 20]
    assert 10 in selection.out_of_range and 25 in selection.out_of_range
    assert any("outside the park's zoom range" in n for n in selection.notes)


def test_requesting_a_narrow_window(repo):
    assert select_zooms(repo.park("wdw"), min_zoom=15, max_zoom=17).zooms == [15, 16, 17]


# ---------------------------------------------------------------------------
# the TMS flip
# ---------------------------------------------------------------------------


def test_flip_y_is_self_inverse():
    for zoom in range(0, 22):
        for y in (0, 1, 7, (1 << zoom) - 1):
            assert flip_y(zoom, flip_y(zoom, y)) == y


def test_flip_y_values():
    assert flip_y(0, 0) == 0
    assert flip_y(1, 0) == 1
    assert flip_y(10, 0) == 1023
    assert flip_y(17, 7084) == 131_071 - 7084


def test_xyz_park_never_flips(repo):
    park = repo.park("wdw")
    assert to_server_y(park, 18, 109_376) == 109_376
    assert to_xyz_y(park, 18, 109_376) == 109_376


def test_tms_park_flips_only_at_the_geographic_boundary(repo):
    park = repo.park("shdr")
    # to_server_y/to_xyz_y convert *between spaces*, so they flip.
    assert to_server_y(park, 17, 100) == flip_y(17, 100)
    assert to_xyz_y(park, 17, 7084) == flip_y(17, 7084)


def test_shdr_bounds_are_used_verbatim_not_flipped(repo):
    """The critical one: boundsByZoom is already in server space.

    Flipping these would silently download a mirror band from the wrong part of
    the world -- valid JPEGs, wrong map, no error anywhere.
    """
    park = repo.park("shdr")
    plan = build_plan(park, repo.version("shdr", "18"), min_zoom=17, max_zoom=17)
    bounds = plan.zooms[0].bounds

    declared = park.bounds_at(17)
    assert (bounds.min_y, bounds.max_y) == (declared.min_y, declared.max_y) == (7084, 7095)

    ys = {y for _x, y in iter_bounds(bounds)}
    assert ys == set(range(7084, 7096))
    # And emphatically not the flipped band.
    assert flip_y(17, 7084) not in ys


def test_tms_flip_would_have_produced_a_different_band(repo):
    """Documents the size of the mistake the previous test guards against."""
    park = repo.park("shdr")
    declared = park.bounds_at(17)
    flipped_min = flip_y(17, declared.max_y)
    assert abs(flipped_min - declared.min_y) > 100_000


# ---------------------------------------------------------------------------
# bbox
# ---------------------------------------------------------------------------


def test_bbox_parsing():
    box = BBox.parse("-81.60,28.34,-81.51,28.42")
    assert (box.west, box.south, box.east, box.north) == (-81.60, 28.34, -81.51, 28.42)


@pytest.mark.parametrize(
    "text",
    ["1,2,3", "a,b,c,d", "10,0,5,1", "0,10,1,5", "-200,0,0,1", "0,-100,1,0"],
)
def test_bad_bbox_rejected(text):
    with pytest.raises(ValueError):
        BBox.parse(text)


def test_bbox_narrows_a_wdw_job(repo):
    park = repo.park("wdw")
    version = repo.version("wdw", "801755166")
    box = BBox.parse("-81.60,28.34,-81.51,28.42")

    full = build_plan(park, version, min_zoom=15, max_zoom=17)
    clipped = build_plan(park, version, min_zoom=15, max_zoom=17, bbox=box)

    assert 0 < clipped.tiles_per_mode < full.tiles_per_mode
    for zoom_plan in clipped.zooms:
        declared = park.bounds_at(zoom_plan.zoom)
        assert zoom_plan.bounds.min_x >= declared.min_x
        assert zoom_plan.bounds.max_x <= declared.max_x


def test_bbox_tiles_are_in_server_space_for_xyz(repo):
    park = repo.park("wdw")
    box = BBox.parse("-81.60,28.34,-81.51,28.42")
    bounds = tiles_for_bbox(park, 14, box)
    assert isinstance(bounds, TileBounds)
    # WDW's own z14 bounds are 4456..4503 x, 6824..6863 y; the bbox sits inside.
    assert 4456 <= bounds.min_x <= 4503
    assert 6824 <= bounds.min_y <= 6863


def test_bbox_refused_for_tms_park(repo):
    """SHDR's grid is not web mercator -- its config even carries a realCenter."""
    from tilearc.errors import TilearcError

    park = repo.park("shdr")
    with pytest.raises(TilearcError, match="not aligned to web mercator"):
        build_plan(park, repo.version("shdr", "18"), bbox=BBox.parse("121.6,31.1,121.7,31.2"))


def test_bbox_allowed_for_tms_park_when_forced(repo):
    park = repo.park("shdr")
    plan = build_plan(
        park,
        repo.version("shdr", "18"),
        bbox=BBox.parse("121.6,31.1,121.7,31.2"),
        allow_tms_bbox=True,
    )
    assert isinstance(plan.tiles_per_mode, int)


def test_bbox_outside_the_park_yields_nothing(repo):
    park = repo.park("hkdl")
    plan = build_plan(park, repo.version("hkdl", "19"), bbox=BBox.parse("-10,-10,-9,-9"))
    assert plan.tiles_per_mode == 0
    assert any("bbox excluded" in note for note in plan.notes)


# ---------------------------------------------------------------------------
# iteration
# ---------------------------------------------------------------------------


def test_iter_bounds_covers_the_rectangle_once():
    bounds = TileBounds(10, 12, 20, 21)
    tiles = list(iter_bounds(bounds))
    assert len(tiles) == bounds.count == 6
    assert len(set(tiles)) == 6
    assert min(tiles) == (10, 20) and max(tiles) == (12, 21)


def test_intersect():
    assert TileBounds(0, 10, 0, 10).intersect(TileBounds(5, 20, 5, 20)) == TileBounds(5, 10, 5, 10)
    assert TileBounds(0, 1, 0, 1).intersect(TileBounds(5, 6, 5, 6)) is None
