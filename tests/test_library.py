"""The multi-version library: store what changed, index all of it.

The archive is built oldest version first, and each later version keeps only
the tiles whose bytes differ from what an earlier one already holds. That makes
a version's directory an incomplete thing on its own, so these tests care about
two properties equally: the bytes on disk are not duplicated, and every tile of
every version can still be found.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from tilearc.config import ParkConfig, TileBounds, VersionEntry
from tilearc.library import Catalogue, LibraryWriter, human_saving, iter_tiles
from tilearc.plan import JobPlan, ZoomPlan


def park(park_id: str = "wdw", modes: bool = False) -> ParkConfig:
    return ParkConfig(
        park_id=park_id, label="Walt Disney World",
        tile_template="https://cdn/{code}/{z}/{x}/{y}.jpg",
        min_zoom=11, max_zoom=12, y_scheme="xyz",
        bounds_by_zoom={11: TileBounds(0, 1, 0, 1)},
    )


def plan_for(version: str, *, park_id: str = "wdw", modes: list[str] | None = None) -> JobPlan:
    return JobPlan(
        park=park(park_id), version=VersionEntry(code=version, label=f"v{version}"),
        zooms=[ZoomPlan(11, TileBounds(0, 1, 0, 1))], modes=modes or [],
    )


def write(root, version, tiles, *, park_id="wdw", mode=""):
    """Archive one version, `tiles` mapping (z, x, y) -> bytes."""
    writer = LibraryWriter(root, plan_for(version, park_id=park_id))
    writer.open()
    for (z, x, y), data in tiles.items():
        writer.write_tile(z, x, y, mode, data)
    writer.finalize({"park": {"id": park_id}}, complete=True)
    writer.catalogue.close()
    return writer


# ---------------------------------------------------------------------------
# storing only what changed
# ---------------------------------------------------------------------------


def test_an_unchanged_tile_is_not_written_twice(tmp_path):
    old = {(11, 0, 0): b"aaa", (11, 0, 1): b"bbb"}
    new = {(11, 0, 0): b"aaa", (11, 0, 1): b"CHANGED"}

    write(tmp_path, "47", old)
    second = write(tmp_path, "105", new)

    assert second.stored == 1          # only the changed tile
    assert second.shared == 1
    # The unchanged tile has no file under the newer version.
    assert not (tmp_path / "wdw" / "105" / "11" / "0" / "0.jpg").exists()
    assert (tmp_path / "wdw" / "105" / "11" / "0" / "1.jpg").read_bytes() == b"CHANGED"
    assert (tmp_path / "wdw" / "47" / "11" / "0" / "0.jpg").read_bytes() == b"aaa"


def test_every_tile_of_every_version_is_still_findable(tmp_path):
    """A sparse folder is only acceptable if the catalogue closes the gap."""
    write(tmp_path, "47", {(11, 0, 0): b"aaa", (11, 0, 1): b"bbb"})
    write(tmp_path, "105", {(11, 0, 0): b"aaa", (11, 0, 1): b"CHANGED"})

    catalogue = Catalogue(tmp_path)
    try:
        # v105's unchanged tile resolves to the file under v47.
        path = catalogue.resolve("wdw", "105", 11, 0, 0)
        assert path is not None
        assert path.read_bytes() == b"aaa"
        assert "47" in path.parts

        # and its changed tile to its own.
        path = catalogue.resolve("wdw", "105", 11, 0, 1)
        assert path.read_bytes() == b"CHANGED"
        assert "105" in path.parts

        # v47 is unaffected by anything that came later.
        assert catalogue.resolve("wdw", "47", 11, 0, 1).read_bytes() == b"bbb"
        assert catalogue.resolve("wdw", "47", 11, 9, 9) is None
    finally:
        catalogue.close()


def test_a_tile_reverting_to_older_bytes_reuses_the_original(tmp_path):
    """Content, not chronology: v3 matching v1 should not store a third copy."""
    write(tmp_path, "1", {(11, 0, 0): b"original"})
    write(tmp_path, "2", {(11, 0, 0): b"changed"})
    third = write(tmp_path, "3", {(11, 0, 0): b"original"})

    assert third.stored == 0
    assert third.shared == 1
    catalogue = Catalogue(tmp_path)
    try:
        path = catalogue.resolve("wdw", "3", 11, 0, 0)
        assert path.read_bytes() == b"original"
        assert "1" in path.parts       # the first copy, not a new one
    finally:
        catalogue.close()


def test_identical_bytes_at_a_different_position_are_stored_separately(tmp_path):
    """Dedup is per tile position. Two blank sea tiles are still two tiles.

    Sharing them would be a bigger saving and a worse archive: the tree would
    stop mirroring the server's layout, and a reader walking the directory
    would find tiles missing with no local explanation.
    """
    first = write(tmp_path, "47", {(11, 0, 0): b"same", (11, 1, 1): b"same"})

    assert first.stored == 2
    assert first.shared == 0
    assert (tmp_path / "wdw" / "47" / "11" / "0" / "0.jpg").exists()
    assert (tmp_path / "wdw" / "47" / "11" / "1" / "1.jpg").exists()


def test_parks_do_not_share_tiles_with_each_other(tmp_path):
    write(tmp_path, "47", {(11, 0, 0): b"same"}, park_id="wdw")
    other = write(tmp_path, "47", {(11, 0, 0): b"same"}, park_id="dlr")

    assert other.stored == 1
    assert (tmp_path / "dlr" / "47" / "11" / "0" / "0.jpg").exists()


def test_modes_do_not_share_tiles_with_each_other(tmp_path):
    """TDR's daytime and nighttime are different pictures of the same place."""
    writer = LibraryWriter(tmp_path, plan_for("2026", park_id="tdr"))
    writer.open()
    writer.write_tile(11, 0, 0, "daytime", b"same")
    writer.write_tile(11, 0, 0, "nighttime", b"same")
    writer.finalize({}, complete=True)
    writer.catalogue.close()

    assert writer.stored == 2
    assert (tmp_path / "tdr" / "2026" / "daytime" / "11" / "0" / "0.jpg").exists()
    assert (tmp_path / "tdr" / "2026" / "nighttime" / "11" / "0" / "0.jpg").exists()


# ---------------------------------------------------------------------------
# the layout the archive is meant to have
# ---------------------------------------------------------------------------


def test_the_tree_is_park_then_server_id_then_the_tile_path(tmp_path):
    write(tmp_path, "900014458", {(18, 71424, 109376): b"tile"})

    expected = tmp_path / "wdw" / "900014458" / "18" / "71424" / "109376.jpg"
    assert expected.is_file()


def test_resume_knows_a_tile_is_held_even_when_another_version_stores_it(tmp_path):
    write(tmp_path, "47", {(11, 0, 0): b"aaa"})

    writer = LibraryWriter(tmp_path, plan_for("105"))
    writer.open()
    writer.write_tile(11, 0, 0, "", b"aaa")
    assert writer.has_tile(11, 0, 0, "")     # recorded, though stored under 47
    assert not writer.has_tile(11, 0, 1, "")
    writer.catalogue.close()


# ---------------------------------------------------------------------------
# the log a future reader needs
# ---------------------------------------------------------------------------


def test_the_catalogue_records_the_hash_of_every_tile(tmp_path):
    write(tmp_path, "47", {(11, 0, 0): b"aaa"})

    catalogue = Catalogue(tmp_path)
    try:
        record = next(iter_tiles(catalogue, "wdw", "47"))
        assert record.sha256 == hashlib.sha256(b"aaa").hexdigest()
        assert record.bytes == 3
        assert not record.shared
    finally:
        catalogue.close()


def test_the_saving_is_reported_honestly(tmp_path):
    write(tmp_path, "47", {(11, 0, 0): b"x" * 100, (11, 0, 1): b"y" * 100})
    write(tmp_path, "105", {(11, 0, 0): b"x" * 100, (11, 0, 1): b"y" * 100})

    catalogue = Catalogue(tmp_path)
    try:
        totals = human_saving(catalogue.stats())
        # Two versions of 200 bytes each, one copy on disk.
        assert totals["logicalBytes"] == 400
        assert totals["storedBytes"] == 200
        assert totals["savedBytes"] == 200
    finally:
        catalogue.close()


def test_the_exported_index_lists_where_each_tile_lives(tmp_path):
    write(tmp_path, "47", {(11, 0, 0): b"aaa", (11, 0, 1): b"bbb"})
    write(tmp_path, "105", {(11, 0, 0): b"aaa"})

    catalogue = Catalogue(tmp_path)
    try:
        index = catalogue.export_index("wdw")
    finally:
        catalogue.close()

    tiles = index["parks"]["wdw"]["105"]["default"]["11"]
    assert tiles == [[0, 0, "wdw/47/11/0/0.jpg"]]
    assert json.dumps(index)            # must survive a round trip to a file


def test_an_interrupted_version_is_marked_incomplete(tmp_path):
    writer = LibraryWriter(tmp_path, plan_for("47"))
    writer.open()
    writer.write_tile(11, 0, 0, "", b"aaa")
    writer.finalize({}, complete=False)
    catalogue = writer.catalogue
    try:
        row = catalogue.versions("wdw")[0]
        assert row["complete"] == 0
        assert row["finished_at"]
    finally:
        catalogue.close()


def test_a_reopened_catalogue_sees_what_an_earlier_run_wrote(tmp_path):
    """The library is built over many runs, so nothing may live only in memory."""
    write(tmp_path, "47", {(11, 0, 0): b"aaa"})

    reopened = Catalogue(tmp_path)
    try:
        assert reopened.resolve("wdw", "47", 11, 0, 0).read_bytes() == b"aaa"
        assert reopened.find_existing(
            "wdw", "", 11, 0, 0, hashlib.sha256(b"aaa").hexdigest()
        ) is not None
    finally:
        reopened.close()
