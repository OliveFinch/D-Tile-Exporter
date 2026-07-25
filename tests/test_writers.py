"""Output backends, including the MBTiles row conversion."""

from __future__ import annotations

import json
import sqlite3
import zipfile

import pytest

from tilearc.bounds import flip_y
from tilearc.manifest import MANIFEST_NAME, build_manifest, utcnow
from tilearc.plan import build_plan
from tilearc.writers import build_writer, default_output
from tilearc.writers.mbtiles import MBTilesWriter
from tilearc.writers.zipw import ZipWriter

JPEG = b"\xff\xd8" + b"\x00" * 64 + b"\xff\xd9"


@pytest.fixture
def plan(repo):
    return build_plan(repo.park("hkdl"), repo.version("hkdl", "19"), min_zoom=14, max_zoom=14)


def manifest_for(plan, **kwargs):
    kwargs.setdefault("fetched", plan.total_tiles)
    kwargs.setdefault("total_bytes", plan.total_tiles * len(JPEG))
    kwargs.setdefault("complete", True)
    return build_manifest(plan, started_at=utcnow(), **kwargs)


def fill(writer, plan):
    for z, x, y, mode in plan.iter_tiles():
        writer.write_tile(z, x, y, mode, JPEG)


# ---------------------------------------------------------------------------
# naming
# ---------------------------------------------------------------------------


def test_default_output_names(plan):
    assert str(default_output(plan, "zip")) == "hkdl_19.zip"
    assert str(default_output(plan, "mbtiles")) == "hkdl_19.mbtiles"
    assert str(default_output(plan, "dir")) == "hkdl_19"


def test_version_codes_are_sanitised_for_paths(repo):
    from tilearc.config import VersionEntry
    from tilearc.util import sanitize_component

    plan = build_plan(
        repo.park("dlp"), VersionEntry(code="../../etc/passwd"), min_zoom=13, max_zoom=13
    )
    assert "/" not in plan.slug
    assert ".." not in plan.slug
    assert plan.slug.startswith("dlp_")

    # Ordinary codes pass through untouched...
    assert sanitize_component("801755166") == "801755166"
    assert sanitize_component("jan2026") == "jan2026"
    assert sanitize_component("20260122183830") == "20260122183830"
    # ...and lossy rewrites stay distinct.
    assert sanitize_component("a/b") != sanitize_component("a_b")


# ---------------------------------------------------------------------------
# dir
# ---------------------------------------------------------------------------


def test_dir_writer_layout(plan, tmp_path):
    writer = build_writer("dir", tmp_path / "out", plan)
    writer.open()
    fill(writer, plan)
    root = writer.finalize(manifest_for(plan))

    assert root == tmp_path / "out" / "hkdl_19"
    assert (root / "14" / "13380" / "7148.jpg").read_bytes() == JPEG
    assert len(list(root.rglob("*.jpg"))) == 12
    assert json.loads((root / MANIFEST_NAME).read_text())["park"]["id"] == "hkdl"


def test_dir_writer_reports_existing_tiles(plan, tmp_path):
    writer = build_writer("dir", tmp_path / "out", plan)
    writer.open()
    assert writer.has_tile(14, 13380, 7148, "") is False
    writer.write_tile(14, 13380, 7148, "", JPEG)
    assert writer.has_tile(14, 13380, 7148, "") is True


def test_dir_writer_leaves_no_partial_files(plan, tmp_path):
    writer = build_writer("dir", tmp_path / "out", plan)
    writer.open()
    fill(writer, plan)
    assert list(writer.root.rglob("*.part")) == []


# ---------------------------------------------------------------------------
# zip
# ---------------------------------------------------------------------------


def test_zip_layout_is_standard_slippy_map(plan, tmp_path):
    output = tmp_path / "hkdl_19.zip"
    writer = ZipWriter(output, plan)
    writer.open()
    fill(writer, plan)
    writer.finalize(manifest_for(plan))

    with zipfile.ZipFile(output) as archive:
        names = set(archive.namelist())
        assert "hkdl_19/14/13380/7148.jpg" in names
        assert "hkdl_19/manifest.json" in names
        assert len([n for n in names if n.endswith(".jpg")]) == 12
        assert archive.read("hkdl_19/14/13380/7148.jpg") == JPEG


def test_zip_manifest_contents(plan, tmp_path):
    output = tmp_path / "a.zip"
    writer = ZipWriter(output, plan)
    writer.open()
    fill(writer, plan)
    writer.finalize(manifest_for(plan))

    with zipfile.ZipFile(output) as archive:
        manifest = json.loads(archive.read("hkdl_19/manifest.json"))

    assert manifest["park"]["id"] == "hkdl"
    assert manifest["version"]["code"] == "19"
    assert manifest["version"]["label"] == "Unknown 1"
    assert manifest["zoom"] == {"min": 14, "max": 14, "levels": [14]}
    assert manifest["tiles"] == {"requested": 12, "fetched": 12, "missing": 0, "failed": 0}
    assert manifest["totalBytes"] == 12 * len(JPEG)
    assert manifest["tool"]["name"] == "tilearc"
    assert manifest["bounds"]["byZoom"]["14"]["tiles"] == 12
    assert manifest["timestamps"]["started"] and manifest["timestamps"]["finished"]
    assert manifest["complete"] is True


def test_zip_staging_survives_an_abort(plan, tmp_path):
    """An interrupted zip job must leave resumable files, not a broken archive."""
    output = tmp_path / "a.zip"
    writer = ZipWriter(output, plan)
    writer.open()
    writer.write_tile(14, 13380, 7148, "", JPEG)
    writer.abort()

    assert not output.exists()
    assert (writer.staging_root / "14" / "13380" / "7148.jpg").is_file()
    assert writer.has_tile(14, 13380, 7148, "") is True


def test_incomplete_zip_job_is_not_packed(plan, tmp_path):
    """Packing an unfinished job would produce a zip that can never be completed.

    The next run consults the state DB, sees those tiles as done, and skips
    them -- so anything absent from the premature zip would stay absent.
    """
    output = tmp_path / "a.zip"
    writer = ZipWriter(output, plan)
    writer.open()
    writer.write_tile(14, 13380, 7148, "", JPEG)
    artefact = writer.finalize(manifest_for(plan, fetched=1, complete=False), complete=False)

    assert not output.exists()
    assert artefact == writer.staging_root
    assert (writer.staging_root / "14" / "13380" / "7148.jpg").is_file()
    assert (writer.staging_root / MANIFEST_NAME).is_file()


def test_incomplete_dir_job_still_gets_a_manifest(plan, tmp_path):
    from tilearc.writers.dirw import DirWriter

    writer = DirWriter(tmp_path / "out", plan)
    writer.open()
    writer.write_tile(14, 13380, 7148, "", JPEG)
    root = writer.finalize(manifest_for(plan, fetched=1, complete=False), complete=False)

    manifest = json.loads((root / MANIFEST_NAME).read_text())
    assert manifest["complete"] is False


def test_zip_cleans_up_staging_on_success(plan, tmp_path):
    output = tmp_path / "a.zip"
    writer = ZipWriter(output, plan)
    writer.open()
    fill(writer, plan)
    writer.finalize(manifest_for(plan))
    assert not writer.staging.exists()


def test_zip_is_readable_by_other_tools(plan, tmp_path):
    output = tmp_path / "a.zip"
    writer = ZipWriter(output, plan)
    writer.open()
    fill(writer, plan)
    writer.finalize(manifest_for(plan))
    with zipfile.ZipFile(output) as archive:
        assert archive.testzip() is None


# ---------------------------------------------------------------------------
# mbtiles
# ---------------------------------------------------------------------------


def test_mbtiles_flips_y_for_an_xyz_park(plan, tmp_path):
    """MBTiles mandates TMS rows, and HKDL serves XYZ -- so it must flip."""
    output = tmp_path / "a.mbtiles"
    writer = MBTilesWriter(output, plan)
    writer.open()
    writer.write_tile(14, 13380, 7148, "", JPEG)
    writer.finalize(manifest_for(plan))

    with sqlite3.connect(output) as conn:
        rows = conn.execute("SELECT zoom_level, tile_column, tile_row FROM tiles").fetchall()
    assert rows == [(14, 13380, flip_y(14, 7148))]
    assert rows[0][2] == 16383 - 7148


def test_mbtiles_does_not_flip_a_tms_park(repo, tmp_path):
    """SHDR's rows are already TMS; flipping again would mirror the map."""
    plan = build_plan(repo.park("shdr"), repo.version("shdr", "18"), min_zoom=17, max_zoom=17)
    output = tmp_path / "s.mbtiles"
    writer = MBTilesWriter(output, plan)
    writer.open()
    writer.write_tile(17, 26447, 7084, "", JPEG)
    writer.finalize(manifest_for(plan))

    with sqlite3.connect(output) as conn:
        rows = conn.execute("SELECT tile_row FROM tiles").fetchall()
    assert rows == [(7084,)]


def test_mbtiles_metadata(plan, tmp_path):
    output = tmp_path / "a.mbtiles"
    writer = MBTilesWriter(output, plan)
    writer.open()
    fill(writer, plan)
    writer.finalize(manifest_for(plan))

    with sqlite3.connect(output) as conn:
        meta = dict(conn.execute("SELECT name, value FROM metadata"))
        count = conn.execute("SELECT COUNT(*) FROM tiles").fetchone()[0]

    assert count == 12
    assert meta["format"] == "jpg"
    assert meta["type"] == "baselayer"
    assert meta["minzoom"] == "14" and meta["maxzoom"] == "14"
    assert "Hong Kong Disneyland" in meta["name"]
    assert meta["tilearc:yScheme"] == "xyz"
    assert json.loads(meta["tilearc:manifest"])["version"]["code"] == "19"
    # Geographic bounds land near Hong Kong Disneyland.
    west, south, east, north = (float(v) for v in meta["bounds"].split(","))
    assert 113.9 < west < 114.1 and 22.2 < south < 22.4
    assert east > west and north > south


def test_mbtiles_omits_bounds_for_a_non_mercator_grid(repo, tmp_path):
    plan = build_plan(repo.park("shdr"), repo.version("shdr", "18"), min_zoom=17, max_zoom=17)
    output = tmp_path / "s.mbtiles"
    writer = MBTilesWriter(output, plan)
    writer.open()
    writer.write_tile(17, 26447, 7084, "", JPEG)
    writer.finalize(manifest_for(plan))

    with sqlite3.connect(output) as conn:
        meta = dict(conn.execute("SELECT name, value FROM metadata"))
    assert "bounds" not in meta        # would be a fiction for a Baidu-derived grid
    assert meta["tilearc:yScheme"] == "tms"


def test_mbtiles_writes_one_file_per_mode(repo, tmp_path):
    plan = build_plan(
        repo.park("tdr"),
        repo.version("tdr", "20260122183830"),
        min_zoom=16,
        max_zoom=16,
        modes=["daytime", "nighttime"],
    )
    output = tmp_path / "tdr.mbtiles"
    writer = MBTilesWriter(output, plan)
    writer.open()
    writer.write_tile(16, 58230, 25810, "daytime", JPEG)
    writer.write_tile(16, 58230, 25810, "nighttime", JPEG + b"x")
    writer.finalize(manifest_for(plan))

    day = tmp_path / "tdr_daytime.mbtiles"
    night = tmp_path / "tdr_nighttime.mbtiles"
    assert day.is_file() and night.is_file()
    with sqlite3.connect(day) as conn:
        assert conn.execute("SELECT tile_data FROM tiles").fetchone()[0] == JPEG
    with sqlite3.connect(night) as conn:
        assert conn.execute("SELECT tile_data FROM tiles").fetchone()[0] == JPEG + b"x"


def test_mbtiles_reports_existing_tiles(plan, tmp_path):
    writer = MBTilesWriter(tmp_path / "a.mbtiles", plan)
    writer.open()
    assert writer.has_tile(14, 13380, 7148, "") is False
    writer.write_tile(14, 13380, 7148, "", JPEG)
    assert writer.has_tile(14, 13380, 7148, "") is True
    writer.close()


# ---------------------------------------------------------------------------
# modes in path-based formats
# ---------------------------------------------------------------------------


def test_mode_appears_in_the_path_only_when_present(repo, tmp_path):
    plan = build_plan(
        repo.park("tdr"),
        repo.version("tdr", "20260122183830"),
        min_zoom=16,
        max_zoom=16,
        modes=["daytime", "nighttime"],
    )
    writer = build_writer("dir", tmp_path / "out", plan)
    writer.open()
    writer.write_tile(16, 58230, 25810, "daytime", JPEG)
    writer.write_tile(16, 58230, 25810, "nighttime", JPEG)
    root = writer.finalize(manifest_for(plan, fetched=2, total_bytes=2 * len(JPEG)))

    assert (root / "daytime" / "16" / "58230" / "25810.jpg").is_file()
    assert (root / "nighttime" / "16" / "58230" / "25810.jpg").is_file()


def test_manifest_records_both_modes(repo):
    plan = build_plan(
        repo.park("tdr"),
        repo.version("tdr", "20260122183830"),
        min_zoom=16,
        max_zoom=16,
        modes=["daytime", "nighttime"],
    )
    manifest = build_manifest(plan, started_at=utcnow())
    assert manifest["modes"] == ["daytime", "nighttime"]
    assert manifest["tiles"]["requested"] == 882   # 441 per mode
