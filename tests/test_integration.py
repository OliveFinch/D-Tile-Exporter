"""Full `tilearc download` runs against a mock transport.

These drive the real CLI end to end -- planning, gating, downloading, writing,
manifest, resume -- with only the HTTP layer replaced.
"""

from __future__ import annotations

import json
import sqlite3
import zipfile
from pathlib import Path

import httpx
import pytest

from tilearc.cli import main, normalise_argv

JPEG = b"\xff\xd8" + b"\x00" * 300 + b"\xff\xd9"


@pytest.fixture(autouse=True)
def _config_dir(monkeypatch, fixtures_dir):
    monkeypatch.setenv("TILEARC_CONFIG_DIR", str(fixtures_dir))


@pytest.fixture
def mock_http(monkeypatch):
    """Replace httpx.AsyncClient with one bound to a MockTransport."""
    state = {"handler": lambda request: httpx.Response(200, content=JPEG), "requests": []}

    real = httpx.AsyncClient

    class Patched(real):
        def __init__(self, **kwargs):
            kwargs.pop("limits", None)

            def handler(request):
                state["requests"].append(str(request.url))
                return state["handler"](request)

            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(**kwargs)

    monkeypatch.setattr("tilearc.downloader.httpx.AsyncClient", Patched)
    return state


def download(*argv, expect=0):
    code = main(["download", *argv, "--yes", "--rps", "0", "--no-progress"])
    assert code == expect, f"expected exit {expect}, got {code}"
    return code


# ---------------------------------------------------------------------------
# zip
# ---------------------------------------------------------------------------


def test_end_to_end_zip(mock_http, tmp_path):
    output = tmp_path / "hkdl_19.zip"
    download(
        "--park", "hkdl", "--version", "19", "--min-zoom", "14", "--max-zoom", "15",
        "-o", str(output),
    )

    assert output.is_file()
    with zipfile.ZipFile(output) as archive:
        names = archive.namelist()
        manifest = json.loads(archive.read("hkdl_19/manifest.json"))

    assert "hkdl_19/14/13380/7148.jpg" in names
    assert "hkdl_19/15/26762/14297.jpg" in names
    assert len([n for n in names if n.endswith(".jpg")]) == 12 + 16

    assert manifest["tiles"] == {"requested": 28, "fetched": 28, "missing": 0, "failed": 0}
    assert manifest["complete"] is True
    assert manifest["totalBytes"] == 28 * len(JPEG)
    assert manifest["park"]["id"] == "hkdl"
    assert manifest["version"]["code"] == "19"
    assert manifest["zoom"] == {"min": 14, "max": 15, "levels": [14, 15]}
    assert "cdn6.parksmedia" in manifest["tileTemplate"][""]

    # Staging is cleaned up on success.
    assert not (tmp_path / "hkdl_19.zip.parts").exists()


def test_end_to_end_zip_verifies(mock_http, tmp_path):
    output = tmp_path / "a.zip"
    download("--park", "hkdl", "--version", "19", "--max-zoom", "14", "-o", str(output))
    assert main(["verify", str(output)]) == 0


def test_missing_tiles_are_recorded_not_fatal(mock_http, tmp_path):
    mock_http["handler"] = lambda r: (
        httpx.Response(404) if "7148" in str(r.url) else httpx.Response(200, content=JPEG)
    )
    output = tmp_path / "a.zip"
    download("--park", "hkdl", "--version", "19", "--max-zoom", "14", "-o", str(output))

    with zipfile.ZipFile(output) as archive:
        manifest = json.loads(archive.read("hkdl_19/manifest.json"))
    assert manifest["tiles"] == {"requested": 12, "fetched": 8, "missing": 4, "failed": 0}
    assert manifest["complete"] is True          # gaps in coverage are normal
    assert main(["verify", str(output)]) == 0


# ---------------------------------------------------------------------------
# dir and mbtiles
# ---------------------------------------------------------------------------


def test_end_to_end_dir(mock_http, tmp_path):
    output = tmp_path / "out"
    download(
        "--park", "hkdl", "--version", "19", "--max-zoom", "14",
        "--format", "dir", "-o", str(output),
    )
    root = output
    assert (root / "14" / "13380" / "7148.jpg").read_bytes() == JPEG
    assert (root / "manifest.json").is_file()
    assert main(["verify", str(root)]) == 0


def test_end_to_end_mbtiles(mock_http, tmp_path):
    output = tmp_path / "a.mbtiles"
    download(
        "--park", "hkdl", "--version", "19", "--max-zoom", "14",
        "--format", "mbtiles", "-o", str(output),
    )
    with sqlite3.connect(output) as conn:
        count = conn.execute("SELECT COUNT(*) FROM tiles").fetchone()[0]
        meta = dict(conn.execute("SELECT name, value FROM metadata"))
    assert count == 12
    assert meta["format"] == "jpg"
    assert main(["verify", str(output)]) == 0


# ---------------------------------------------------------------------------
# the DLP per-version override, end to end
# ---------------------------------------------------------------------------


def test_dlp_jan2026_fetches_from_r2_not_the_disney_cdn(mock_http, tmp_path):
    download(
        "--park", "dlp", "--version", "jan2026", "--max-zoom", "13",
        "-o", str(tmp_path / "a.zip"),
    )
    assert mock_http["requests"]
    assert all(url.startswith("https://pub-") for url in mock_http["requests"])
    assert not any("disneylandparis.com" in url for url in mock_http["requests"])


def test_dlp_current_fetches_from_the_park_template(mock_http, tmp_path):
    download(
        "--park", "dlp", "--version", "current", "--max-zoom", "13",
        "-o", str(tmp_path / "a.zip"),
    )
    assert all("media.disneylandparis.com" in url for url in mock_http["requests"])


# ---------------------------------------------------------------------------
# SHDR: the TMS bounds must be used verbatim
# ---------------------------------------------------------------------------


def test_shdr_requests_the_bounds_y_not_the_flipped_one(mock_http, tmp_path):
    download(
        "--park", "shdr", "--version", "18", "--min-zoom", "17", "--max-zoom", "17",
        "-o", str(tmp_path / "a.zip"),
    )
    ys = {int(url.rsplit("/", 1)[1].removesuffix(".jpg")) for url in mock_http["requests"]}
    assert ys == set(range(7084, 7096))          # exactly the declared bounds
    assert max(ys) < 10_000                      # a flip would give ~124,000


def test_shdr_mbtiles_keeps_tms_rows(mock_http, tmp_path):
    output = tmp_path / "s.mbtiles"
    download(
        "--park", "shdr", "--version", "18", "--min-zoom", "17", "--max-zoom", "17",
        "--format", "mbtiles", "-o", str(output),
    )
    with sqlite3.connect(output) as conn:
        rows = {r[0] for r in conn.execute("SELECT tile_row FROM tiles")}
    assert rows == set(range(7084, 7096))


# ---------------------------------------------------------------------------
# resume
# ---------------------------------------------------------------------------


def test_rerunning_refetches_nothing(mock_http, tmp_path):
    output = tmp_path / "a.zip"
    args = ("--park", "hkdl", "--version", "19", "--max-zoom", "14", "-o", str(output))

    download(*args)
    assert len(mock_http["requests"]) == 12

    mock_http["requests"].clear()
    download(*args)
    assert mock_http["requests"] == []
    assert output.is_file()


def test_resume_after_failures_only_retries_the_failures(mock_http, tmp_path):
    output = tmp_path / "a.zip"
    args = (
        "--park", "hkdl", "--version", "19", "--max-zoom", "14",
        "-o", str(output), "--retries", "0",
    )

    mock_http["handler"] = lambda r: (
        httpx.Response(503) if "7148" in str(r.url) else httpx.Response(200, content=JPEG)
    )
    download(*args, expect=1)                    # failures -> nonzero exit
    assert len(mock_http["requests"]) == 12

    mock_http["requests"].clear()
    mock_http["handler"] = lambda r: httpx.Response(200, content=JPEG)
    download(*args)
    assert len(mock_http["requests"]) == 4       # only the four that failed

    with zipfile.ZipFile(output) as archive:
        manifest = json.loads(archive.read("hkdl_19/manifest.json"))
    assert manifest["tiles"]["fetched"] == 12
    assert manifest["complete"] is True


def test_state_db_is_written_beside_the_output(mock_http, tmp_path):
    output = tmp_path / "a.zip"
    download("--park", "hkdl", "--version", "19", "--max-zoom", "14", "-o", str(output))
    assert (tmp_path / "a.zip.tilearc-state.sqlite").is_file()


def test_a_different_job_will_not_reuse_the_state_db(mock_http, tmp_path):
    output = tmp_path / "a.zip"
    download("--park", "hkdl", "--version", "19", "--max-zoom", "14", "-o", str(output))
    # Same output path, different version -> must refuse rather than mix.
    code = main([
        "download", "--park", "hkdl", "--version", "21", "--max-zoom", "14",
        "-o", str(output), "--yes", "--rps", "0", "--no-progress",
    ])
    assert code == 5


def test_restart_discards_prior_state(mock_http, tmp_path):
    output = tmp_path / "a.zip"
    args = ("--park", "hkdl", "--version", "19", "--max-zoom", "14", "-o", str(output))
    download(*args)

    mock_http["requests"].clear()
    download(*args, "--restart")
    assert len(mock_http["requests"]) == 12      # everything fetched again


def test_aborted_job_keeps_staging_and_resumes_to_a_complete_archive(mock_http, tmp_path):
    """The Ctrl-C contract, exercised via the circuit breaker (same code path).

    An unfinished job must not leave a packed zip: the state DB would mark
    those tiles done, so the missing ones would never be added on resume.
    """
    output = tmp_path / "a.zip"
    args = (
        "--park", "hkdl", "--version", "19", "--min-zoom", "14", "--max-zoom", "15",
        "-o", str(output), "--retries", "0", "--concurrency", "1",
    )

    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        # Succeed for a while, then fail hard enough to trip the breaker.
        return httpx.Response(200, content=JPEG) if calls["n"] <= 10 else httpx.Response(500)

    mock_http["handler"] = handler
    code = main(["download", *args, "--yes", "--rps", "0", "--no-progress"])
    assert code != 0

    assert not output.exists(), "an unfinished job must not leave a packed zip"
    staging = tmp_path / "a.zip.parts"
    assert staging.is_dir()
    assert len(list(staging.rglob("*.jpg"))) == 10

    # Resume cleanly.
    mock_http["handler"] = lambda r: httpx.Response(200, content=JPEG)
    download(*args)

    assert output.is_file()
    with zipfile.ZipFile(output) as archive:
        tiles = [n for n in archive.namelist() if n.endswith(".jpg")]
        manifest = json.loads(archive.read("hkdl_19/manifest.json"))

    assert len(tiles) == 28, "every tile must reach the final archive"
    assert manifest["tiles"]["fetched"] == 28
    assert manifest["complete"] is True
    assert not (tmp_path / "a.zip.parts").exists()


def test_aborted_job_manifest_is_marked_incomplete(mock_http, tmp_path):
    output = tmp_path / "out"
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, content=JPEG) if calls["n"] <= 5 else httpx.Response(500)

    mock_http["handler"] = handler
    main([
        "download", "--park", "hkdl", "--version", "19", "--max-zoom", "14",
        "--format", "dir", "-o", str(output), "--retries", "0", "--concurrency", "1",
        "--yes", "--rps", "0", "--no-progress",
    ])

    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["complete"] is False
    assert manifest["tiles"]["fetched"] == 5


def test_partial_run_leaves_resumable_staging(mock_http, tmp_path):
    """A job that aborts mid-way keeps its staged tiles and its state DB."""
    output = tmp_path / "a.zip"
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] > 4:
            return httpx.Response(403)           # 403 = missing for a CDN park
        return httpx.Response(200, content=JPEG)

    mock_http["handler"] = handler
    download("--park", "hkdl", "--version", "19", "--max-zoom", "14", "-o", str(output))

    state_db = tmp_path / "a.zip.tilearc-state.sqlite"
    assert state_db.is_file()
    with sqlite3.connect(state_db) as conn:
        counts = dict(conn.execute("SELECT status, COUNT(*) FROM tiles GROUP BY status"))
    assert counts == {"done": 4, "missing": 8}


# ---------------------------------------------------------------------------
# bbox on the command line
# ---------------------------------------------------------------------------


def test_negative_bbox_is_accepted_without_an_equals_sign():
    """Every Florida/California bbox starts with a negative longitude."""
    normalised = normalise_argv(
        ["estimate", "--park", "wdw", "--bbox", "-81.60,28.34,-81.51,28.42"]
    )
    assert normalised == ["estimate", "--park", "wdw", "--bbox=-81.60,28.34,-81.51,28.42"]


def test_bbox_download_narrows_the_job(mock_http, tmp_path):
    output = tmp_path / "a.zip"
    download(
        "--park", "wdw", "--version", "801755166",
        "--bbox", "-81.60,28.34,-81.51,28.42",
        "--min-zoom", "14", "--max-zoom", "14", "-o", str(output),
    )
    assert 0 < len(mock_http["requests"]) < 1920  # z14 full is 48x40


# ---------------------------------------------------------------------------
# TDR
# ---------------------------------------------------------------------------


def test_tdr_worker_job_end_to_end(mock_http, tmp_path):
    output = tmp_path / "tdr.zip"
    download(
        "--park", "tdr", "--version", "20260122183830",
        "--min-zoom", "16", "--max-zoom", "16", "-o", str(output),
    )
    assert len(mock_http["requests"]) == 441
    sample = mock_http["requests"][0]
    assert "/tdr-tiles/z16/" in sample
    assert "_" in sample.rsplit("/", 1)[1]
    assert "mode=daytime" in sample and "sid=20260122183830" in sample


def test_tdr_both_modes_write_separate_trees(mock_http, tmp_path):
    output = tmp_path / "out"
    download(
        "--park", "tdr", "--version", "20260122183830",
        "--min-zoom", "16", "--max-zoom", "16", "--mode", "both",
        "--format", "dir", "-o", str(output),
    )
    root = output
    assert (root / "daytime" / "16" / "58230" / "25810.jpg").is_file()
    assert (root / "nighttime" / "16" / "58230" / "25810.jpg").is_file()
    assert len(mock_http["requests"]) == 882

    manifest = json.loads((root / "manifest.json").read_text())
    assert manifest["modes"] == ["daytime", "nighttime"]
    assert manifest["tiles"]["requested"] == 882


def test_tdr_204_counts_as_missing(mock_http, tmp_path):
    mock_http["handler"] = lambda r: (
        httpx.Response(204) if "58230_" in str(r.url) else httpx.Response(200, content=JPEG)
    )
    output = tmp_path / "a.zip"
    download(
        "--park", "tdr", "--version", "20260122183830",
        "--min-zoom", "16", "--max-zoom", "16", "-o", str(output),
    )
    with zipfile.ZipFile(output) as archive:
        manifest = json.loads(archive.read("tdr_20260122183830/manifest.json"))
    assert manifest["tiles"]["missing"] == 21
    assert manifest["tiles"]["fetched"] == 420
    assert manifest["complete"] is True


def test_tdr_everything_204_reads_as_expired_credentials(mock_http, tmp_path, capsys):
    mock_http["handler"] = lambda r: httpx.Response(204)
    code = main([
        "download", "--park", "tdr", "--version", "20260122183830",
        "--min-zoom", "16", "--max-zoom", "16", "-o", str(tmp_path / "a.zip"),
        "--yes", "--rps", "0", "--no-progress",
    ])
    err = capsys.readouterr().err
    assert code == 3
    assert "expired" in err
    # It must give up early, not grind through all 441 tiles.
    assert len(mock_http["requests"]) < 441
