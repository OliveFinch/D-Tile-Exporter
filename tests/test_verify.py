from __future__ import annotations

import json
import sqlite3
import zipfile

import pytest

from tilearc.errors import VerifyError
from tilearc.manifest import MANIFEST_NAME, build_manifest, utcnow
from tilearc.plan import build_plan
from tilearc.verify import verify
from tilearc.writers.dirw import DirWriter
from tilearc.writers.mbtiles import MBTilesWriter
from tilearc.writers.zipw import ZipWriter

JPEG = b"\xff\xd8" + b"\x00" * 64 + b"\xff\xd9"
TRUNCATED = b"\xff\xd8" + b"\x00" * 64


@pytest.fixture
def plan(repo):
    return build_plan(repo.park("hkdl"), repo.version("hkdl", "19"), min_zoom=14, max_zoom=14)


def make_manifest(plan, **kwargs):
    kwargs.setdefault("fetched", plan.total_tiles)
    kwargs.setdefault("total_bytes", plan.total_tiles * len(JPEG))
    kwargs.setdefault("complete", True)
    return build_manifest(plan, started_at=utcnow(), **kwargs)


def build_zip(plan, path, payload=JPEG):
    writer = ZipWriter(path, plan)
    writer.open()
    for z, x, y, mode in plan.iter_tiles():
        writer.write_tile(z, x, y, mode, payload)
    writer.finalize(make_manifest(plan))
    return path


# ---------------------------------------------------------------------------
# happy paths
# ---------------------------------------------------------------------------


def test_good_zip_passes(plan, tmp_path):
    report = verify(build_zip(plan, tmp_path / "a.zip"))
    assert report.ok
    assert report.kind == "zip"
    assert report.tiles_found == 12
    assert report.bytes_found == 12 * len(JPEG)
    assert report.problems == []


def test_good_dir_passes(plan, tmp_path):
    writer = DirWriter(tmp_path / "out", plan)
    writer.open()
    for z, x, y, mode in plan.iter_tiles():
        writer.write_tile(z, x, y, mode, JPEG)
    root = writer.finalize(make_manifest(plan))

    report = verify(root)
    assert report.ok and report.tiles_found == 12 and report.kind == "dir"


def test_dir_verify_descends_into_the_archive_folder(plan, tmp_path):
    """`verify out/` should work as well as `verify out/hkdl_19/`."""
    writer = DirWriter(tmp_path / "out", plan)
    writer.open()
    for z, x, y, mode in plan.iter_tiles():
        writer.write_tile(z, x, y, mode, JPEG)
    writer.finalize(make_manifest(plan))

    assert verify(tmp_path / "out").ok


def test_good_mbtiles_passes(plan, tmp_path):
    output = tmp_path / "a.mbtiles"
    writer = MBTilesWriter(output, plan)
    writer.open()
    for z, x, y, mode in plan.iter_tiles():
        writer.write_tile(z, x, y, mode, JPEG)
    writer.finalize(make_manifest(plan))

    report = verify(output)
    assert report.ok and report.kind == "mbtiles" and report.tiles_found == 12


# ---------------------------------------------------------------------------
# detection
# ---------------------------------------------------------------------------


def test_truncated_jpeg_is_caught(plan, tmp_path):
    report = verify(build_zip(plan, tmp_path / "a.zip", payload=TRUNCATED))
    assert not report.ok
    assert any("truncated" in p for p in report.problems)


def test_non_jpeg_payload_is_caught(plan, tmp_path):
    report = verify(build_zip(plan, tmp_path / "a.zip", payload=b"<html>404</html>"))
    assert not report.ok
    assert any("not a JPEG" in p for p in report.problems)


def test_quick_mode_skips_image_checks(plan, tmp_path):
    path = build_zip(plan, tmp_path / "a.zip", payload=TRUNCATED)
    assert verify(path, deep=False).ok
    assert not verify(path, deep=True).ok


def test_zero_length_tile_is_caught_even_in_quick_mode(plan, tmp_path):
    path = build_zip(plan, tmp_path / "a.zip", payload=b"")
    # A zero-byte tile is never legitimate output.
    assert not verify(path, deep=False).ok


def test_count_mismatch_against_the_manifest_is_caught(plan, tmp_path):
    path = tmp_path / "a.zip"
    build_zip(plan, path)

    # Rewrite the archive with one tile removed.
    with zipfile.ZipFile(path) as source:
        entries = [(i.filename, source.read(i.filename)) for i in source.infolist()]
    dropped = next(n for n, _ in entries if n.endswith(".jpg"))
    with zipfile.ZipFile(path, "w") as out:
        for name, data in entries:
            if name != dropped:
                out.writestr(name, data)

    report = verify(path)
    assert not report.ok
    assert any("manifest records" in p for p in report.problems)


def test_missing_manifest_is_a_problem(tmp_path):
    path = tmp_path / "a.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("x/14/1/1.jpg", JPEG)
    report = verify(path)
    assert not report.ok
    assert any(MANIFEST_NAME in p for p in report.problems)


def test_incomplete_manifest_warns_but_does_not_fail(plan, tmp_path):
    path = tmp_path / "a.zip"
    writer = ZipWriter(path, plan)
    writer.open()
    for z, x, y, mode in plan.iter_tiles():
        writer.write_tile(z, x, y, mode, JPEG)
    writer.finalize(make_manifest(plan, complete=False, failed=3))

    report = verify(path)
    assert report.ok
    assert any("incomplete" in w for w in report.warnings)
    assert any("failed" in w for w in report.warnings)


def test_leftover_partial_files_are_reported(plan, tmp_path):
    writer = DirWriter(tmp_path / "out", plan)
    writer.open()
    writer.write_tile(14, 13380, 7148, "", JPEG)
    root = writer.finalize(make_manifest(plan, fetched=1, total_bytes=len(JPEG)))
    (root / "14" / "13380" / "7149.jpg.part").write_bytes(b"half")

    assert any("partial" in w for w in verify(root).warnings)


# ---------------------------------------------------------------------------
# error handling
# ---------------------------------------------------------------------------


def test_missing_path(tmp_path):
    with pytest.raises(VerifyError, match="no such archive"):
        verify(tmp_path / "nope.zip")


def test_unrecognised_format(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("hello")
    with pytest.raises(VerifyError, match="unrecognised archive format"):
        verify(path)


def test_corrupt_zip(tmp_path):
    path = tmp_path / "a.zip"
    path.write_bytes(b"PK\x03\x04 definitely not a zip")
    with pytest.raises(VerifyError, match="not a readable zip"):
        verify(path)


def test_sqlite_without_mbtiles_tables(tmp_path):
    path = tmp_path / "a.mbtiles"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE unrelated (x INTEGER)")
    with pytest.raises(VerifyError, match="MBTiles metadata"):
        verify(path)


def test_report_is_json_serialisable(plan, tmp_path):
    report = verify(build_zip(plan, tmp_path / "a.zip"))
    assert json.loads(json.dumps(report.as_dict()))["ok"] is True
