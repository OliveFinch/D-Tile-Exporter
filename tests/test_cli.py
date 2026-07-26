"""End-to-end CLI behaviour. No test here reaches the network."""

from __future__ import annotations

import json

import pytest

from tilearc.cli import main

FIXTURE_ARGS = None  # set by the fixture below


@pytest.fixture(autouse=True)
def _config_dir(monkeypatch, fixtures_dir):
    """Point every invocation at the fixtures, and keep real env vars out."""
    monkeypatch.setenv("TILEARC_CONFIG_DIR", str(fixtures_dir))
    monkeypatch.delenv("TILEARC_CONFIG_URL", raising=False)
    for name in list(__import__("os").environ):
        if name.startswith("TILEARC_TDR_"):
            monkeypatch.delenv(name, raising=False)


def run(capsys, *argv):
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# ---------------------------------------------------------------------------
# versions
# ---------------------------------------------------------------------------


def test_versions_lists_active_entries(capsys):
    code, out, _err = run(capsys, "versions", "--park", "wdw")
    assert code == 0
    assert "801755166" in out and "47" in out


def test_versions_json(capsys):
    code, out, _err = run(capsys, "versions", "--park", "dlp", "--json")
    assert code == 0
    entries = {e["code"]: e for e in json.loads(out)}
    assert entries["jan2026"]["url"].startswith("https://pub-")
    assert entries["current"]["url"] is None


def test_versions_hides_inactive_by_default(capsys):
    _c, active, _e = run(capsys, "versions", "--park", "shdr", "--json")
    _c, everything, _e = run(capsys, "versions", "--park", "shdr", "--all", "--json")
    assert len(json.loads(everything)) > len(json.loads(active))
    assert all(e["active"] for e in json.loads(active))


# ---------------------------------------------------------------------------
# estimate
# ---------------------------------------------------------------------------


def test_estimate_matches_the_scale_reference(capsys):
    code, out, _err = run(
        capsys, "estimate", "--park", "wdw", "--version", "801755166",
        "--max-zoom", "17", "--json",
    )
    assert code == 0
    payload = json.loads(out)
    assert payload["totalTiles"] == 37_850
    assert payload["bytesPerTile"] == 25_000
    assert payload["estimatedBytes"] == 37_850 * 25_000


def test_estimate_human_output_reports_size(capsys):
    code, _out, err = run(
        capsys, "estimate", "--park", "wdw", "--version", "801755166", "--max-zoom", "17"
    )
    assert code == 0
    assert "37,850" in err
    assert "946.2 MB" in err          # 37,850 x 25,000 bytes


def test_estimate_fetches_nothing(capsys, monkeypatch):
    """A network call during `estimate` is a bug."""
    import httpx

    def boom(*a, **k):
        raise AssertionError("estimate must not touch the network")

    monkeypatch.setattr(httpx, "get", boom)
    monkeypatch.setattr(httpx, "AsyncClient", boom)
    assert run(capsys, "estimate", "--park", "hkdl", "--version", "19")[0] == 0


def test_estimate_all_zooms(capsys):
    code, out, _err = run(
        capsys, "estimate", "--park", "hkdl", "--version", "19", "--all-zooms", "--json"
    )
    assert json.loads(out)["totalTiles"] == 21_852


def test_estimate_tdr_both_modes(capsys):
    code, out, _err = run(
        capsys, "estimate", "--park", "tdr", "--version", "20260122183830",
        "--max-zoom", "17", "--mode", "both", "--json",
    )
    payload = json.loads(out)
    assert payload["tilesPerMode"] == 2_122
    assert payload["totalTiles"] == 4_244
    assert payload["modes"] == ["daytime", "nighttime"]


def test_estimate_reports_skipped_zooms(capsys):
    code, out, _err = run(
        capsys, "estimate", "--park", "shdr", "--version", "18", "--all-zooms", "--json"
    )
    notes = " ".join(json.loads(out)["notes"])
    assert "no boundsByZoom entry" in notes


def test_estimate_with_bbox(capsys):
    code, out, _err = run(
        capsys, "estimate", "--park", "wdw", "--version", "801755166",
        "--bbox", "-81.60,28.34,-81.51,28.42", "--max-zoom", "19", "--json",
    )
    assert code == 0
    assert 0 < json.loads(out)["totalTiles"] < 575_450


def test_estimate_rejects_bbox_on_a_tms_park(capsys):
    code, _out, err = run(
        capsys, "estimate", "--park", "shdr", "--version", "18",
        "--bbox", "121.6,31.1,121.7,31.2",
    )
    assert code != 0
    assert "not aligned to web mercator" in err


def test_estimate_rejects_a_malformed_bbox(capsys):
    code, _out, err = run(
        capsys, "estimate", "--park", "wdw", "--version", "47", "--bbox", "1,2,3"
    )
    assert code != 0 and "bbox" in err


def test_unknown_park_is_a_clean_error(capsys):
    code, _out, err = run(capsys, "estimate", "--park", "nope", "--version", "1")
    assert code == 2
    assert "not found" in err
    assert "Traceback" not in err


def test_missing_config_source_explains_itself(capsys, monkeypatch):
    monkeypatch.delenv("TILEARC_CONFIG_DIR")
    code, _out, err = run(capsys, "versions", "--park", "wdw")
    assert code == 2
    assert "--config-dir" in err and "TILEARC_CONFIG_DIR" in err


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def test_doctor_reports_the_wdw_anomalies(capsys):
    code, out, _err = run(capsys, "doctor", "--park", "wdw", "--json")
    assert code == 0
    findings = json.loads(out)
    assert any(f["rule"] == "aspect" and f["zoom"] == 19 for f in findings)
    assert any(f["rule"] == "span" and f["zoom"] == 12 for f in findings)


def test_doctor_covers_every_park_by_default(capsys):
    code, out, _err = run(capsys, "doctor", "--json")
    parks = {f["park"] for f in json.loads(out)}
    assert parks == {"wdw", "dlr", "hkdl", "shdr", "dlp", "tdr"}


def test_doctor_strict_exits_nonzero(capsys):
    assert run(capsys, "doctor", "--park", "wdw")[0] == 0
    assert run(capsys, "doctor", "--park", "wdw", "--strict")[0] == 1


def test_doctor_human_output_is_readable(capsys):
    code, out, err = run(capsys, "doctor", "--park", "tdr")
    assert code == 0
    assert "coverage" in out and "z16" in out
    assert "reported, not corrected" in err


# ---------------------------------------------------------------------------
# download gating (nothing is fetched in these tests)
# ---------------------------------------------------------------------------


def test_download_dry_run_fetches_nothing(capsys, tmp_path, monkeypatch):
    import httpx

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **k: (_ for _ in ()).throw(AssertionError("no network"))
    )
    code, _out, err = run(
        capsys, "download", "--park", "hkdl", "--version", "19",
        "--max-zoom", "14", "--dry-run", "-o", str(tmp_path / "a.zip"),
    )
    assert code == 0
    assert "dry run" in err
    assert not (tmp_path / "a.zip").exists()


def test_download_refuses_an_oversized_job(capsys, tmp_path):
    code, _out, err = run(
        capsys, "download", "--park", "wdw", "--version", "801755166",
        "--all-zooms", "--max-tiles", "1000", "--yes", "-o", str(tmp_path / "a.zip"),
    )
    assert code == 4
    assert "575,450 tiles exceeds --max-tiles" in err
    assert "--force" in err


def test_download_shows_the_plan_before_the_cap_check(capsys, tmp_path):
    _code, _out, err = run(
        capsys, "download", "--park", "wdw", "--version", "801755166",
        "--all-zooms", "--max-tiles", "1000", "--yes", "-o", str(tmp_path / "a.zip"),
    )
    assert "575,450" in err and "14.4 GB" in err


def test_download_warns_about_high_concurrency(capsys, tmp_path):
    _code, _out, err = run(
        capsys, "download", "--park", "hkdl", "--version", "19", "--max-zoom", "14",
        "--concurrency", "32", "--dry-run", "-o", str(tmp_path / "a.zip"),
    )
    assert "--concurrency 32 is high" in err


def test_download_prints_the_user_agent(capsys, tmp_path):
    _code, _out, err = run(
        capsys, "download", "--park", "hkdl", "--version", "19", "--max-zoom", "14",
        "--dry-run", "-o", str(tmp_path / "a.zip"),
    )
    assert "tilearc/" in err and "archiver" in err


def test_download_shows_the_resolved_url_template(capsys, tmp_path):
    _code, _out, err = run(
        capsys, "download", "--park", "dlp", "--version", "jan2026", "--max-zoom", "13",
        "--allow-rehosted", "--dry-run", "-o", str(tmp_path / "a.zip"),
    )
    assert "pub-" in err                          # the R2 override, not the CDN
    assert "media.disneylandparis.com" not in err
    assert "overrides the park tile template" in err


def test_a_version_hosted_off_the_park_s_own_domain_is_refused(capsys, tmp_path):
    """DLP's jan2026 is a copy already downloaded and re-hosted on R2.

    Archiving it snapshots the archive rather than the map, so it takes saying
    so out loud. The override itself stays legal -- the test above passes
    --allow-rehosted and still gets the R2 URL.
    """
    code, _out, err = run(
        capsys, "download", "--park", "dlp", "--version", "jan2026", "--max-zoom", "13",
        "--dry-run", "-o", str(tmp_path / "a.zip"),
    )
    assert code != 0
    assert "r2.dev" in err and "media.disneylandparis.com" in err
    assert "--allow-rehosted" in err


# ---------------------------------------------------------------------------
# TDR quota gating
# ---------------------------------------------------------------------------


def test_full_tdr_job_via_the_worker_is_refused(capsys, tmp_path):
    code, _out, err = run(
        capsys, "download", "--park", "tdr", "--version", "20260122183830",
        "--all-zooms", "--max-tiles", "999999", "--yes", "-o", str(tmp_path / "a.zip"),
    )
    assert code == 4
    assert "safety cap for proxied jobs" in err
    assert "100,000 requests/day" in err
    assert "break TDR for everyone" in err


def test_small_tdr_job_via_the_worker_is_allowed(capsys, tmp_path):
    code, _out, err = run(
        capsys, "download", "--park", "tdr", "--version", "20260122183830",
        "--max-zoom", "17", "--dry-run", "-o", str(tmp_path / "a.zip"),
    )
    assert code == 0
    assert "2,122" in err
    assert "safety cap" not in err


def test_tdr_force_warns_but_proceeds(capsys, tmp_path):
    code, _out, err = run(
        capsys, "download", "--park", "tdr", "--version", "20260122183830",
        "--all-zooms", "--force", "--dry-run", "-o", str(tmp_path / "a.zip"),
    )
    assert code == 0
    assert "Proceeding because --force was given" in err


def test_expired_tdr_credentials_fail_before_any_request(capsys, tmp_path, monkeypatch):
    creds = tmp_path / "creds.json"
    creds.write_text(
        json.dumps(
            {
                "proxy_url": "https://w.example/tdr-tiles/",
                "expires": "2020-01-01T00:00:00Z",
                "cookies": {"CloudFront-Policy": "p"},
            }
        )
    )
    import httpx

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **k: (_ for _ in ()).throw(AssertionError("no network"))
    )
    code, _out, err = run(
        capsys, "download", "--park", "tdr", "--version", "20260122183830",
        "--max-zoom", "16", "--tdr-credentials", str(creds), "--yes",
        "-o", str(tmp_path / "a.zip"),
    )
    assert code == 3
    assert "expired at" in err
    assert "job state is preserved" in err


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------


def test_verify_command_on_a_real_archive(capsys, tmp_path, repo):
    from tilearc.manifest import build_manifest, utcnow
    from tilearc.plan import build_plan
    from tilearc.writers.zipw import ZipWriter

    jpeg = b"\xff\xd8" + b"\x00" * 32 + b"\xff\xd9"
    plan = build_plan(repo.park("hkdl"), repo.version("hkdl", "19"), min_zoom=14, max_zoom=14)
    output = tmp_path / "hkdl_19.zip"
    writer = ZipWriter(output, plan)
    writer.open()
    for z, x, y, mode in plan.iter_tiles():
        writer.write_tile(z, x, y, mode, jpeg)
    writer.finalize(
        build_manifest(
            plan, started_at=utcnow(), fetched=12, total_bytes=12 * len(jpeg), complete=True
        )
    )

    code, _out, err = run(capsys, "verify", str(output))
    assert code == 0 and "OK" in err

    code, out, _err = run(capsys, "verify", str(output), "--json")
    assert code == 0 and json.loads(out)["ok"] is True


def test_verify_reports_failure_with_a_nonzero_exit(capsys, tmp_path):
    import zipfile

    from tilearc.manifest import MANIFEST_NAME

    path = tmp_path / "bad.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("x/14/1/1.jpg", b"not a jpeg at all")
        archive.writestr(
            f"x/{MANIFEST_NAME}",
            json.dumps({"tiles": {"fetched": 1}, "totalBytes": 17, "tileExtension": "jpg"}),
        )
    code, _out, err = run(capsys, "verify", str(path))
    assert code == 1
    assert "FAILED" in err


# ---------------------------------------------------------------------------
# misc
# ---------------------------------------------------------------------------


def test_help_lists_every_command(capsys):
    with pytest.raises(SystemExit):
        main(["--help"])
    out = capsys.readouterr().out
    for command in ("download", "estimate", "versions", "verify", "doctor"):
        assert command in out


# ---------------------------------------------------------------------------
# discover
# ---------------------------------------------------------------------------


@pytest.fixture
def probe_server(monkeypatch):
    """Serve tiles for an explicit truth rectangle per zoom."""
    import httpx

    truth: dict = {}
    calls: list = []

    def handler(request):
        parts = request.url.path.strip("/").removesuffix(".jpg").split("/")
        z, x, y = int(parts[-3]), int(parts[-2]), int(parts[-1])
        calls.append((z, x, y))
        box = truth.get(z)
        inside = box and box[0] <= x <= box[1] and box[2] <= y <= box[3]
        return httpx.Response(200, content=b"\xff\xd8ok\xff\xd9") if inside else httpx.Response(404)

    real = httpx.AsyncClient

    class Patched(real):
        def __init__(self, **kwargs):
            kwargs.pop("limits", None)
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(**kwargs)

    monkeypatch.setattr("tilearc.discover.httpx.AsyncClient", Patched)
    return {"truth": truth, "calls": calls}


def test_discover_confirms_correct_bounds(capsys, probe_server, fixtures_dir):
    import json as _json
    from tilearc.config import DirConfigSource, ParkRepository

    park = ParkRepository(DirConfigSource(fixtures_dir)).park("hkdl")
    for zoom in (14, 15):
        bounds = park.bounds_at(zoom)
        probe_server["truth"][zoom] = (bounds.min_x, bounds.max_x, bounds.min_y, bounds.max_y)

    code, out, _err = run(
        capsys, "discover", "--park", "hkdl", "--version", "19",
        "--min-zoom", "14", "--max-zoom", "15", "--yes", "--rps", "0",
        "--no-progress", "--json",
    )
    assert code == 0
    payload = _json.loads(out)
    assert payload["changes"] == {}
    assert payload["boundsByZoom"]["14"] == park.bounds_at(14).as_dict()


def test_discover_finds_a_too_narrow_edge(capsys, probe_server, fixtures_dir):
    import json as _json
    from tilearc.config import DirConfigSource, ParkRepository

    park = ParkRepository(DirConfigSource(fixtures_dir)).park("wdw")
    declared = park.bounds_at(12)
    # The real map extends four tiles further west than the config says.
    probe_server["truth"][12] = (
        declared.min_x - 4, declared.max_x, declared.min_y, declared.max_y
    )

    code, out, _err = run(
        capsys, "discover", "--park", "wdw", "--version", "801755166",
        "--min-zoom", "12", "--max-zoom", "12", "--yes", "--rps", "0",
        "--no-progress", "--json",
    )
    assert code == 0
    payload = _json.loads(out)
    assert payload["boundsByZoom"]["12"]["minX"] == declared.min_x - 4
    assert payload["changes"]["12"]["tileDelta"] == 4 * declared.height


def test_discover_dry_run_sends_nothing(capsys, probe_server):
    code, _out, err = run(
        capsys, "discover", "--park", "hkdl", "--version", "19",
        "--max-zoom", "15", "--dry-run",
    )
    assert code == 0
    assert "dry run" in err
    assert probe_server["calls"] == []


def test_discover_reports_the_cost_before_asking(capsys, probe_server):
    _code, _out, err = run(
        capsys, "discover", "--park", "hkdl", "--version", "19",
        "--max-zoom", "15", "--dry-run",
    )
    assert "requests" in err
    assert "req/s" in err


def test_discover_refuses_a_park_with_no_template(capsys, probe_server):
    code, _out, err = run(
        capsys, "discover", "--park", "tdr", "--version", "20260122183830", "--yes",
    )
    assert code != 0
    assert "no public tile template" in err
    assert probe_server["calls"] == []


def test_discover_write_updates_the_config(capsys, probe_server, fixtures_dir, tmp_path):
    import json as _json
    import shutil

    from tilearc.config import DirConfigSource, ParkRepository

    shutil.copytree(fixtures_dir, tmp_path / "parks")
    repo_dir = tmp_path / "parks"

    park = ParkRepository(DirConfigSource(repo_dir)).park("wdw")
    declared = park.bounds_at(12)
    probe_server["truth"][12] = (
        declared.min_x - 4, declared.max_x, declared.min_y, declared.max_y
    )

    code, _out, err = run(
        capsys, "discover", "--park", "wdw", "--version", "801755166",
        "--min-zoom", "12", "--max-zoom", "12", "--yes", "--rps", "0",
        "--no-progress", "--write", "--config-dir", str(repo_dir),
    )
    assert code == 0
    assert "updated" in err

    written = _json.loads((repo_dir / "wdw" / "wdw_config.json").read_text())
    assert written["boundsByZoom"]["12"]["minX"] == declared.min_x - 4
    assert written["boundsMeasured"]["version"] == "801755166"
    # Untouched keys survive.
    assert written["tileTemplate"] == park.tile_template
    assert written["defaultCenter"] == [-81.567406, 28.386276]

    # And it reloads through the normal parser.
    reloaded = ParkRepository(DirConfigSource(repo_dir)).park("wdw")
    assert reloaded.bounds_at(12).min_x == declared.min_x - 4
