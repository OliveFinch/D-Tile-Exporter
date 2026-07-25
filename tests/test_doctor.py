"""Bounds sanity checks, asserted against the real anomalies in the fixtures."""

from __future__ import annotations

from tilearc.doctor import check_park, worst_severity


def findings_for(repo, park_id, rule=None, zoom=None):
    out = check_park(repo.park(park_id))
    if rule:
        out = [f for f in out if f.rule == rule]
    if zoom is not None:
        out = [f for f in out if f.zoom == zoom]
    return out


# ---------------------------------------------------------------------------
# WDW
# ---------------------------------------------------------------------------


def test_wdw_z12_x_span_is_narrower_than_z11(repo):
    """z11 spans 10 tiles across; z12 spans 8, where it should span >= 20."""
    hits = findings_for(repo, "wdw", rule="span", zoom=12)
    x_hits = [f for f in hits if f.detail["axis"] == "x"]
    assert len(x_hits) == 1
    assert x_hits[0].detail["parentSpan"] == 10
    assert x_hits[0].detail["childSpan"] == 8
    assert x_hits[0].detail["expectedMin"] == 20
    assert x_hits[0].severity == "error"


def test_wdw_z15_y_span_shrinks_against_z14(repo):
    hits = [f for f in findings_for(repo, "wdw", rule="span", zoom=15) if f.detail["axis"] == "y"]
    assert len(hits) == 1
    assert hits[0].detail["parentSpan"] == 40
    assert hits[0].detail["childSpan"] == 28


def test_wdw_z19_aspect_ratio_inverts(repo):
    """z18 is 480x224 (wide); z19 is 192x448 (tall) -- X/Y look swapped."""
    hits = findings_for(repo, "wdw", rule="aspect", zoom=19)
    assert len(hits) == 1
    assert hits[0].detail["parent"] == "480x224"
    assert hits[0].detail["child"] == "192x448"
    assert "swapped" in hits[0].message


def test_wdw_z19_x_span_also_flagged(repo):
    hits = [f for f in findings_for(repo, "wdw", rule="span", zoom=19) if f.detail["axis"] == "x"]
    assert hits and hits[0].detail["childSpan"] == 192


def test_wdw_z20_is_not_flagged_for_aspect(repo):
    """z20 keeps z19's (wrong) shape, so only z19 is the inversion point."""
    assert findings_for(repo, "wdw", rule="aspect", zoom=20) == []


def test_near_square_levels_do_not_count_as_inversions(repo):
    """z11 is 10x9 and z12 is 8x10 -- a wobble across 1.0, not swapped axes.

    Without a significance threshold these drown out the one real inversion.
    """
    assert findings_for(repo, "wdw", rule="aspect", zoom=12) == []
    assert findings_for(repo, "wdw", rule="aspect", zoom=13) == []
    assert [f.zoom for f in findings_for(repo, "wdw", rule="aspect")] == [19]


def test_aspect_significance_threshold():
    from tilearc.doctor import _is_significant_inversion

    assert _is_significant_inversion(2.14, 0.43) is True    # WDW z18 -> z19
    assert _is_significant_inversion(1.11, 0.80) is False   # WDW z11 -> z12
    assert _is_significant_inversion(2.0, 3.0) is False     # no inversion at all


# ---------------------------------------------------------------------------
# TDR: systematically one tile short on max X/Y
# ---------------------------------------------------------------------------


def test_tdr_is_one_tile_short_at_every_level(repo):
    hits = findings_for(repo, "tdr", rule="coverage")
    assert {f.zoom for f in hits} == {16, 17, 18, 19, 20}
    for finding in hits:
        assert set(finding.detail["shortfalls"]) == {"maxX", "maxY"}
        expected = finding.detail["expected"]
        actual = finding.detail["actual"]
        # max was entered as parent*2 instead of parent*2+1.
        assert expected["maxX"] - actual["maxX"] == 1
        assert expected["maxY"] - actual["maxY"] == 1


def test_tdr_span_rule_also_catches_the_off_by_one(repo):
    spans = findings_for(repo, "tdr", rule="span", zoom=17)
    assert {f.detail["axis"] for f in spans} == {"x", "y"}
    assert all(f.detail["childSpan"] == f.detail["parentSpan"] * 2 - 1 for f in spans)


def test_tdr_z15_below_minzoom_is_reported(repo):
    hits = findings_for(repo, "tdr", rule="zoom-range", zoom=15)
    assert len(hits) == 1
    assert hits[0].severity == "info"
    assert "never be downloaded" in hits[0].message


# ---------------------------------------------------------------------------
# SHDR
# ---------------------------------------------------------------------------


def test_shdr_z17_shrinks_in_both_dimensions(repo):
    hits = findings_for(repo, "shdr", rule="span", zoom=17)
    assert {f.detail["axis"] for f in hits} == {"x", "y"}
    by_axis = {f.detail["axis"]: f.detail for f in hits}
    assert (by_axis["x"]["parentSpan"], by_axis["x"]["childSpan"]) == (18, 13)
    assert (by_axis["y"]["parentSpan"], by_axis["y"]["childSpan"]) == (18, 12)


def test_shdr_missing_z21_bounds_is_a_warning(repo):
    hits = findings_for(repo, "shdr", rule="zoom-range", zoom=21)
    assert len(hits) == 1
    assert hits[0].severity == "warning"
    assert "no boundsByZoom entry" in hits[0].message


def test_shdr_low_zooms_reported_as_out_of_range(repo):
    hits = {f.zoom for f in findings_for(repo, "shdr", rule="zoom-range") if f.severity == "info"}
    assert {9, 10, 11, 12, 13} <= hits


# ---------------------------------------------------------------------------
# DLP / DLR / HKDL
# ---------------------------------------------------------------------------


def test_dlp_z20_above_maxzoom_is_reported(repo):
    hits = findings_for(repo, "dlp", rule="zoom-range", zoom=20)
    assert len(hits) == 1 and hits[0].severity == "info"


def test_dlp_has_no_span_errors(repo):
    """DLP's grid doubles cleanly -- only the stray z20 entry is unusual."""
    assert findings_for(repo, "dlp", rule="span") == []
    assert findings_for(repo, "dlp", rule="aspect") == []


def test_hkdl_z15_is_inset_on_all_four_edges(repo):
    """z14 is 13380-13383 x 7148-7150, so z15 should be 26760-26767 x 14296-14301.

    It is 26762-26765 x 14297-14300: narrower on every side, losing a ring of
    tiles the parent level claims exist.
    """
    hits = findings_for(repo, "hkdl", rule="coverage", zoom=15)
    assert len(hits) == 1
    assert set(hits[0].detail["shortfalls"]) == {"minX", "maxX", "minY", "maxY"}
    assert hits[0].detail["expected"] == {
        "minX": 26760, "maxX": 26767, "minY": 14296, "maxY": 14301
    }


def test_dlr_z20_overshoot_is_not_reported_as_a_shortfall(repo):
    """z20 maxY runs *past* the parent grid; that loses no coverage, so no finding."""
    assert findings_for(repo, "dlr", rule="coverage", zoom=20) == []


# ---------------------------------------------------------------------------
# general behaviour
# ---------------------------------------------------------------------------


def test_doctor_reports_but_never_mutates(repo):
    before = dict(repo.park("wdw").bounds_by_zoom)
    check_park(repo.park("wdw"))
    assert dict(repo.park("wdw").bounds_by_zoom) == before


def test_findings_are_json_serialisable(repo):
    import json

    for park_id in ("wdw", "dlr", "hkdl", "shdr", "dlp", "tdr"):
        json.dumps([f.as_dict() for f in check_park(repo.park(park_id))])


def test_worst_severity_ordering(repo):
    assert worst_severity(check_park(repo.park("wdw"))) == "error"
    assert worst_severity([]) is None


def test_every_park_produces_at_least_one_finding(repo):
    """Every config in the fixture set has something worth a human look."""
    for park_id in ("wdw", "dlr", "hkdl", "shdr", "dlp", "tdr"):
        assert check_park(repo.park(park_id)), park_id
