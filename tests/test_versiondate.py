"""Dating a map version from what the viewer records about it.

The codes are not ordered and the dates are not stored, so every case here is
taken from a real entry in one of the six parks' server lists.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tilearc.versiondate import UNDATED_SORT_KEY, version_date, version_sort_key


# ---------------------------------------------------------------------------
# the label, which is where the date usually is
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "label, expected",
    [
        ("May '17", "2017-05"),          # wdw 47
        ("Jun '18", "2018-06"),          # wdw 105
        ("Aug '18 (Late)", "2018-08"),   # the qualifier orders, it does not date
        ("April 2019", "2019-04"),       # hkdl spells them out
        ("Nov 2020", "2020-11"),
        ("Jan 2026", "2026-01"),
        ("Sept '21", "2021-09"),
        ("DECEMBER 2022", "2022-12"),
    ],
)
def test_a_month_and_year_in_the_label(label, expected):
    assert version_date("12345", label) == expected


@pytest.mark.parametrize("label", ["Unknown 1", "Unknown 9", "Current", "", None])
def test_a_label_that_names_no_month(label):
    """Hong Kong lists nine of these. An empty cell beats an invented date."""
    assert version_date("29", label) is None


def test_a_word_that_is_not_a_month_is_not_a_month():
    assert version_date("29", "Season 19") is None
    assert version_date("29", "Park 21") is None


# ---------------------------------------------------------------------------
# the code, when it is itself a date
# ---------------------------------------------------------------------------


def test_tokyos_code_is_a_timestamp():
    assert version_date("20260122183830", "Jan 2026") == "2026-01-22"


def test_a_dated_snapshot_folder_is_already_the_answer():
    """What a park with no version history is filed under."""
    assert version_date("2026-07-27", None) == "2026-07-27"


def test_the_code_is_preferred_over_the_label():
    """A timestamp is exact; a label is a month at best."""
    assert version_date("20260122183830", "Jan 2026") == "2026-01-22"


@pytest.mark.parametrize("code", ["900014458", "671203034", "840388841", "981376074"])
def test_a_long_serial_is_not_read_as_a_date(code):
    """900014458 would otherwise parse as the year 9000."""
    assert version_date(code, None) is None


@pytest.mark.parametrize("code", ["47", "105", "52", "current"])
def test_a_short_code_is_not_a_date(code):
    assert version_date(code, None) is None


def test_an_impossible_month_is_refused():
    assert version_date("20261332000000", None) is None


# ---------------------------------------------------------------------------
# ordering
# ---------------------------------------------------------------------------


def test_versions_sort_oldest_first_not_by_code():
    """47 -> 105 -> 900014458 is chronological and lexically backwards."""
    versions = [
        ("900014458", "Jul '26 (Late)"),
        ("47", "May '17"),
        ("105", "Jun '18"),
    ]
    ordered = sorted(versions, key=lambda v: version_sort_key(*v))
    assert [code for code, _ in ordered] == ["47", "105", "900014458"]


def test_two_maps_from_one_month_keep_a_stable_order():
    versions = [("110", "Oct '18 (Late)"), ("109", "Oct '18")]
    ordered = sorted(versions, key=lambda v: version_sort_key(*v))
    assert [code for code, _ in ordered] == ["109", "110"]


def test_undated_versions_collect_at_the_end():
    versions = [("19", "Unknown 1"), ("24", "April 2019"), ("21", "Unknown 2")]
    ordered = sorted(versions, key=lambda v: version_sort_key(*v))
    assert [code for code, _ in ordered] == ["24", "19", "21"]
    assert version_sort_key("19", "Unknown 1")[0] == UNDATED_SORT_KEY


# ---------------------------------------------------------------------------
# against the real server lists
# ---------------------------------------------------------------------------


def _server_lists():
    root = Path(__file__).resolve().parent / "fixtures" / "parks"
    for path in sorted(root.glob("*/*_dis_servers.json")):
        yield path.parent.name, json.loads(path.read_text())


def test_most_real_versions_can_be_dated():
    """A parser that dates a third of them would pass every case above."""
    dated = undated = 0
    for _park, entries in _server_lists():
        for entry in entries:
            if version_date(str(entry.get("code")), entry.get("label")):
                dated += 1
            else:
                undated += 1

    assert dated + undated > 100, "fixtures got smaller; this test is now weak"
    # The undated remainder is Hong Kong's "Unknown N" and DLP's "Current".
    assert dated / (dated + undated) > 0.85, f"only dated {dated} of {dated + undated}"


def test_every_wdw_version_is_dated():
    """WDW is the archive being built, and it labels every server."""
    versions = dict(_server_lists())["wdw"]
    undated = [
        entry for entry in versions
        if not version_date(str(entry.get("code")), entry.get("label"))
    ]
    assert undated == []
