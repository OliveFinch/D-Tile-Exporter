"""Headless tests for the desktop front-end.

These construct the real widgets under Qt's ``offscreen`` platform, so the
window is built, populated and interrogated exactly as it would be on screen —
no display and no network required.
"""

from __future__ import annotations

import os

import pytest

# Must be set before Qt is imported.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6", reason="the GUI extra is not installed")

from PySide6.QtCore import QThreadPool  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from tilearc_gui.download_tab import DownloadTab  # noqa: E402
from tilearc_gui.doctor_tab import DoctorTab  # noqa: E402
from tilearc_gui.context import AppContext  # noqa: E402


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def context(fixtures_dir):
    ctx = AppContext()
    ctx.use_directory(fixtures_dir)
    return ctx


def drain(qapp, timeout_s: float = 20.0) -> None:
    """Let background tasks finish and their queued signals be delivered."""
    import time

    pool = QThreadPool.globalInstance()
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        qapp.processEvents()
        if pool.activeThreadCount() == 0:
            break
        time.sleep(0.02)
    # One more pass so queued signals from the last task are delivered.
    for _ in range(5):
        qapp.processEvents()
        time.sleep(0.02)
    qapp.processEvents()


def select(combo, value) -> None:
    index = combo.findData(value)
    assert index >= 0, f"{value!r} not in {[combo.itemData(i) for i in range(combo.count())]}"
    combo.setCurrentIndex(index)


# ---------------------------------------------------------------------------
# park + version loading
# ---------------------------------------------------------------------------


def test_download_tab_lists_parks(qapp, context):
    tab = DownloadTab(context)
    tab.reload_parks()
    drain(qapp)

    ids = {tab.park_combo.itemData(i) for i in range(tab.park_combo.count())}
    assert {"wdw", "dlr", "hkdl", "shdr", "dlp"} <= ids


def test_tdr_is_offered_for_download(qapp, context):
    """TDR is credential-gated, but the Download tab now knows how to ask."""
    tab = DownloadTab(context)
    tab.reload_parks()
    drain(qapp)

    ids = {tab.park_combo.itemData(i) for i in range(tab.park_combo.count())}
    assert "tdr" in ids


# ---------------------------------------------------------------------------
# TDR
# ---------------------------------------------------------------------------


def _tdr_tab(qapp, context) -> DownloadTab:
    tab = DownloadTab(context)
    tab.reload_parks()
    drain(qapp)
    select(tab.park_combo, "tdr")
    drain(qapp)
    return tab


def test_tdr_panel_is_shown_only_for_tdr(qapp, context):
    tab = DownloadTab(context)
    tab.reload_parks()
    drain(qapp)

    select(tab.park_combo, "wdw")
    drain(qapp)
    assert not tab.tdr_box.isVisibleTo(tab)

    select(tab.park_combo, "tdr")
    drain(qapp)
    assert tab.tdr_box.isVisibleTo(tab)


def test_tdr_both_modes_doubles_the_job(qapp, context):
    tab = _tdr_tab(qapp, context)

    select(tab.tdr_mode, "daytime")
    drain(qapp)
    assert tab.plan.modes == ["daytime"]
    single = tab.plan.total_tiles

    select(tab.tdr_mode, "both")
    drain(qapp)
    assert tab.plan.modes == ["daytime", "nighttime"]
    assert tab.plan.total_tiles == single * 2


def test_tdr_sources_are_built_per_mode(qapp, context):
    tab = _tdr_tab(qapp, context)
    select(tab.tdr_mode, "both")
    drain(qapp)

    sources = tab._build_sources()
    assert set(sources) == {"daytime", "nighttime"}
    # Worker route by default, and the worker's "missing" marker is 204.
    assert all(s.uses_shared_proxy for s in sources.values())
    assert all(204 in s.missing_statuses for s in sources.values())
    assert "daytime" in sources["daytime"].template


def test_tdr_direct_route_uses_origin_and_treats_403_as_auth(qapp, context):
    """403 direct from the origin is a bad signature, not an absent tile."""
    tab = _tdr_tab(qapp, context)
    select(tab.tdr_route, True)
    drain(qapp)

    source = tab._build_sources()["daytime"]
    assert not source.uses_shared_proxy
    assert "tokyodisneyresort.jp" in source.template
    assert 403 in source.auth_statuses
    assert 403 not in source.missing_statuses
    assert source.cookies["CloudFront-Policy"] == "FIXTURE-POLICY"


def test_tdr_status_reports_where_credentials_came_from(qapp, context):
    tab = _tdr_tab(qapp, context)
    assert "tdr_config.json" in tab.tdr_status.text()


def test_tdr_missing_credentials_file_is_reported_not_raised(qapp, context):
    tab = _tdr_tab(qapp, context)
    tab.tdr_credentials.setText("/nonexistent/tdr_credentials.json")
    tab._tdr_settings_changed()
    drain(qapp)

    assert "not found" in tab.tdr_status.text().lower()


def test_worker_quota_note_flags_oversized_jobs(qapp, context):
    from tilearc.tdr import WORKER_TILE_CAP

    assert "over the" in DownloadTab._worker_quota_note(WORKER_TILE_CAP + 1)
    assert "over the" not in DownloadTab._worker_quota_note(WORKER_TILE_CAP - 1)


def test_retired_versions_are_hidden_until_asked_for(qapp, context):
    tab = DownloadTab(context)
    tab.reload_parks()
    drain(qapp)
    select(tab.park_combo, "shdr")
    drain(qapp)

    active_only = tab.version_combo.count()
    assert all(v.active for v in tab.versions)

    tab.inactive_check.setChecked(True)
    drain(qapp)
    assert tab.version_combo.count() > active_only


# ---------------------------------------------------------------------------
# the estimate must agree with the library
# ---------------------------------------------------------------------------


def test_estimate_matches_the_published_scale_reference(qapp, context):
    tab = DownloadTab(context)
    tab.reload_parks()
    drain(qapp)
    select(tab.park_combo, "wdw")
    drain(qapp)
    select(tab.version_combo, "801755166")

    # The tab defaults to the park minimum through z17.
    assert (tab.min_zoom.value(), tab.max_zoom.value()) == (11, 17)

    # The published reference counts the declared rectangles.
    tab.use_coverage.setChecked(False)
    assert tab.plan.total_tiles == 37_850
    assert "37,850 tiles" in tab.estimate_label.text()

    tab.max_zoom.setValue(20)
    assert tab.plan.total_tiles == 575_450
    assert "very large job" in tab.estimate_label.text()


def test_measured_coverage_is_on_without_being_asked_for(qapp, context):
    """It has to be on by default or it is not on at all.

    The find ran only when loading park data *failed*, so on every working
    launch the box was unticked and every job planned from the declared
    rectangles -- which is the bug this whole feature exists to fix.
    """
    tab = DownloadTab(context)
    tab.reload_parks()
    drain(qapp)

    assert tab.coverage_path is not None, "no coverage file found at all"
    assert tab.coverage_path.name == "measured-coverage.json"
    assert tab.use_coverage.isChecked()

    select(tab.park_combo, "wdw")
    drain(qapp)
    select(tab.version_combo, "801755166")

    # 40 more than the declared bounds: z12's imagery starts four columns west
    # of where the config says it does, and those were never being asked for.
    assert tab.plan.total_tiles == 37_890
    tab.use_coverage.setChecked(False)
    assert tab.plan.total_tiles == 37_850


def test_zoom_spinboxes_are_clamped_to_the_park_range(qapp, context):
    tab = DownloadTab(context)
    tab.reload_parks()
    drain(qapp)
    select(tab.park_combo, "hkdl")
    drain(qapp)

    assert (tab.min_zoom.minimum(), tab.min_zoom.maximum()) == (14, 20)
    tab.max_zoom.setValue(99)          # a spin box clamps rather than accepting
    assert tab.max_zoom.value() == 20

    # Hong Kong is L-shaped; the declared rectangle is 21,852 and more than half
    # of it was never drawn.
    assert tab.plan.total_tiles == 9_332
    tab.use_coverage.setChecked(False)
    assert tab.plan.total_tiles == 21_852


# ---------------------------------------------------------------------------
# the two traps, surfaced in the UI
# ---------------------------------------------------------------------------


def test_dlp_jan2026_shows_the_r2_url_not_the_disney_cdn(qapp, context):
    tab = DownloadTab(context)
    tab.reload_parks()
    drain(qapp)
    select(tab.park_combo, "dlp")
    drain(qapp)

    select(tab.version_combo, "jan2026")
    text = tab.estimate_label.text()
    assert "pub-" in text
    assert "own tile server" in text or "overrides" in text
    # The example URL is the one that says where tiles would come from. The
    # re-host warning names the Disney host too, so look at that line alone.
    example = [line for line in text.split("<br>") if "monospace" in line]
    assert example and "media.disneylandparis.com" not in example[0]

    select(tab.version_combo, "current")
    assert "media.disneylandparis.com" in tab.estimate_label.text()


def test_shdr_example_url_uses_the_bounds_y_verbatim(qapp, context):
    """A TMS flip here would request y=124051 instead of y=7084."""
    tab = DownloadTab(context)
    tab.reload_parks()
    drain(qapp)
    select(tab.park_combo, "shdr")
    drain(qapp)
    tab.min_zoom.setValue(17)
    tab.max_zoom.setValue(17)

    text = tab.estimate_label.text()
    assert "/17/26447/7084.jpg" in text
    assert "124051" not in text


def test_zooms_without_bounds_are_reported(qapp, context):
    tab = DownloadTab(context)
    tab.reload_parks()
    drain(qapp)
    select(tab.park_combo, "shdr")
    drain(qapp)
    tab.max_zoom.setValue(21)          # SHDR declares z21 but has no z21 bounds

    assert "no boundsByZoom entry" in tab.estimate_label.text()


# ---------------------------------------------------------------------------
# output paths
# ---------------------------------------------------------------------------


def test_output_path_per_format(qapp, context, tmp_path):
    tab = DownloadTab(context)
    tab.reload_parks()
    drain(qapp)
    select(tab.park_combo, "hkdl")
    drain(qapp)
    select(tab.version_combo, "19")
    tab.destination = tmp_path

    for fmt, expected in [
        ("dir", tmp_path / "hkdl_19"),
        ("zip", tmp_path / "hkdl_19.zip"),
        ("mbtiles", tmp_path / "hkdl_19.mbtiles"),
    ]:
        select(tab.format_combo, fmt)
        assert tab._output_path() == expected

    # The directory must not gain a second copy of the job name.
    select(tab.format_combo, "dir")
    assert tab._output_path().name == "hkdl_19"
    assert tab._output_path().parent == tmp_path


def test_download_button_needs_a_destination(qapp, context, tmp_path):
    tab = DownloadTab(context)
    tab.reload_parks()
    drain(qapp)
    select(tab.park_combo, "hkdl")
    drain(qapp)

    assert tab.download_button.isEnabled() is False
    tab.destination = tmp_path
    tab._update_destination_label()
    assert tab.download_button.isEnabled() is True


def test_high_concurrency_warns(qapp, context):
    tab = DownloadTab(context)
    tab.concurrency.setValue(24)
    assert "is a lot" in tab.politeness_warning.text()
    tab.concurrency.setValue(5)
    assert tab.politeness_warning.text() == ""


# ---------------------------------------------------------------------------
# doctor tab
# ---------------------------------------------------------------------------


def test_doctor_tab_reports_the_wdw_anomalies(qapp, context):
    tab = DoctorTab(context)
    tab.reload_parks()
    drain(qapp)
    select(tab.park_combo, "wdw")

    tab._run()
    drain(qapp)

    rows = tab.table.rowCount()
    assert rows > 0
    cells = [
        [tab.table.item(r, c).text() for c in range(tab.table.columnCount())]
        for r in range(rows)
    ]
    assert any(row[1] == "z19" and row[2] == "aspect" for row in cells)
    assert any(row[1] == "z12" and row[2] == "span" for row in cells)
    assert "not corrected" in tab.summary_label.text()


def test_doctor_tab_covers_every_park(qapp, context):
    tab = DoctorTab(context)
    tab.reload_parks()
    drain(qapp)
    select(tab.park_combo, None)       # "All parks"

    tab._run()
    drain(qapp)

    parks = {tab.table.item(r, 0).text() for r in range(tab.table.rowCount())}
    assert {"wdw", "dlr", "hkdl", "shdr", "dlp", "tdr"} <= parks


# ---------------------------------------------------------------------------
# source switching
# ---------------------------------------------------------------------------


def test_switching_source_mid_load_is_not_ignored(qapp, context, fixtures_dir):
    """A stale reply must not overwrite the newer source's results."""
    tab = DownloadTab(context)
    tab.reload_parks()          # first load, deliberately not awaited
    tab.reload_parks()          # user switches source immediately
    drain(qapp)

    ids = {tab.park_combo.itemData(i) for i in range(tab.park_combo.count())}
    assert "wdw" in ids
    assert None not in ids, "the 'Loading…' placeholder was left behind"


# ---------------------------------------------------------------------------
# packaging invariants
# ---------------------------------------------------------------------------


def test_self_test_flag_builds_a_window_and_exits():
    """`build-app.sh` relies on this to prove a bundle can actually start.

    Run in a subprocess: `main` creates its own QApplication, and a second one
    in this process is an error.
    """
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "tilearc_gui", "--self-test"],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "self-test ok" in result.stdout


def test_packaged_entry_point_avoids_relative_imports():
    """PyInstaller runs its entry script with no parent package.

    Pointing it at `tilearc_gui/__main__.py` makes `from .app import main` an
    illegal relative import -- and the same failure during analysis means Qt is
    silently left out of the bundle, so the app dies instantly on launch.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    launcher = (root / "packaging" / "launcher.py").read_text()
    assert "from tilearc_gui.app import main" in launcher
    # Check the code, not the docstring that explains why.
    code = [
        line for line in launcher.splitlines()
        if line.startswith(("import ", "from "))
    ]
    assert code and not any(line.startswith("from .") for line in code)

    spec = (root / "packaging" / "ParkTileArchiver.spec").read_text()
    assert 'ENTRY = os.path.join(HERE, "launcher.py")' in spec
    assert "__main__.py" not in spec
    # Paths must not be relative to the invoking directory.
    assert '"../src' not in spec


def test_spec_keeps_the_qt_modules_plugins_may_need():
    from pathlib import Path

    spec = (Path(__file__).resolve().parent.parent / "packaging" / "ParkTileArchiver.spec")
    text = spec.read_text()
    for module in ("QtSvg", "QtOpenGL", "QtNetwork", "QtDBus"):
        assert f'"PySide6.{module}"' not in text, f"{module} must not be excluded"


# ---------------------------------------------------------------------------
# measuring bounds from the server
# ---------------------------------------------------------------------------


def test_measure_button_reports_differences_and_offers_json(qapp, context, monkeypatch):
    """The GUI wiring around discovery; the search itself is tested elsewhere."""
    from PySide6.QtWidgets import QMessageBox

    from tilearc.config import TileBounds
    from tilearc.discover import ZoomMeasurement
    import tilearc_gui.doctor_tab as module

    tab = DoctorTab(context)
    tab.reload_parks()
    drain(qapp)
    select(tab.park_combo, "wdw")

    fake = [
        ZoomMeasurement(11, TileBounds(555, 564, 851, 859), TileBounds(555, 564, 851, 859)),
        ZoomMeasurement(12, TileBounds(1118, 1125, 1706, 1715),
                        TileBounds(1114, 1125, 1706, 1715), requests=40),
    ]
    async def fake_discover(*args, **kwargs):
        return fake

    monkeypatch.setattr(module, "discover", fake_discover)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)

    shown: list[str] = []
    monkeypatch.setattr(module.DoctorTab, "_show_result", lambda self, text: shown.append(text))

    tab._measure()
    drain(qapp)

    assert shown, "no result was shown"
    text = shown[0]
    assert "1114-1125" in text
    assert "minX" in text
    assert '"12": { "minX": 1114' in text          # pasteable block
    assert "1 zoom(s) differ" in tab.summary_label.text()
    assert tab.measure_button.isEnabled()


def test_measure_button_says_nothing_to_do_when_the_config_is_right(
    qapp, context, monkeypatch
):
    from PySide6.QtWidgets import QMessageBox

    from tilearc.config import TileBounds
    from tilearc.discover import ZoomMeasurement
    import tilearc_gui.doctor_tab as module

    tab = DoctorTab(context)
    tab.reload_parks()
    drain(qapp)
    select(tab.park_combo, "hkdl")

    same = TileBounds(13380, 13383, 7148, 7150)
    async def fake_discover(*args, **kwargs):
        return [ZoomMeasurement(14, same, same)]

    monkeypatch.setattr(module, "discover", fake_discover)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    monkeypatch.setattr(module.DoctorTab, "_show_result", lambda self, text: None)

    tab._measure()
    drain(qapp)
    assert "matches the server exactly" in tab.summary_label.text()


def test_measure_refuses_a_park_with_no_public_template(qapp, context, monkeypatch):
    """TDR cannot be probed -- it needs credentials and a proxy."""
    from PySide6.QtWidgets import QMessageBox

    import tilearc_gui.doctor_tab as module

    tab = DoctorTab(context)
    tab.reload_parks()
    drain(qapp)
    select(tab.park_combo, "tdr")

    told: list[str] = []
    monkeypatch.setattr(
        QMessageBox, "information", lambda parent, title, text, *a, **k: told.append(text)
    )
    called = []

    async def fake_discover(*args, **kwargs):
        called.append(1)
        return []

    monkeypatch.setattr(module, "discover", fake_discover)

    tab._measure()
    drain(qapp)

    assert called == [], "must not probe a credential-gated park"
    assert told and "no public tile template" in told[0]


def test_measure_asks_before_spending_requests(qapp, context, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    import tilearc_gui.doctor_tab as module

    tab = DoctorTab(context)
    tab.reload_parks()
    drain(qapp)
    select(tab.park_combo, "hkdl")

    asked: list[str] = []

    def question(parent, title, text, *args, **kwargs):
        asked.append(text)
        return QMessageBox.Cancel

    monkeypatch.setattr(QMessageBox, "question", question)
    called = []

    async def fake_discover(*args, **kwargs):
        called.append(1)
        return []

    monkeypatch.setattr(module, "discover", fake_discover)

    tab._measure()
    drain(qapp)

    assert called == [], "cancelling must not send any requests"
    assert asked and "requests" in asked[0]


# ---------------------------------------------------------------------------
# the library format's two different paths
# ---------------------------------------------------------------------------


def _plan(*, park_id: str, version: str, supports_history: bool = True):
    from tilearc.config import ParkConfig, TileBounds, VersionEntry
    from tilearc.plan import JobPlan, ZoomPlan

    park = ParkConfig(
        park_id=park_id, label=park_id.upper(), tile_template="t",
        min_zoom=11, max_zoom=11, y_scheme="xyz",
        bounds_by_zoom={11: TileBounds(0, 1, 0, 1)},
        supports_history=supports_history,
    )
    return JobPlan(park=park, version=VersionEntry(code=version),
                   zooms=[ZoomPlan(11, TileBounds(0, 1, 0, 1))], modes=[])


def _select_format(tab, value):
    index = tab.format_combo.findData(value)
    assert index >= 0, f"no {value!r} in the format picker"
    tab.format_combo.setCurrentIndex(index)


def test_library_format_hands_the_writer_the_root_not_the_version_folder(qapp, context, tmp_path):
    """The writer appends {park}/{version} itself.

    Handing it the displayed path would bury the tiles under wdw/47/wdw/47 --
    the shape of mistake that only shows up once a download has been running
    for an hour.
    """
    tab = DownloadTab(context)
    tab.plan = _plan(park_id="wdw", version="47")
    tab.destination = tmp_path
    _select_format(tab, "library")

    assert tab._job_output() == tmp_path
    assert tab._output_path() == tmp_path / "wdw" / "47"


def test_the_other_formats_are_unchanged_by_that(qapp, context, tmp_path):
    tab = DownloadTab(context)
    tab.plan = _plan(park_id="wdw", version="47")
    tab.destination = tmp_path

    _select_format(tab, "dir")
    assert tab._job_output() == tab._output_path() == tmp_path / "wdw_47"

    _select_format(tab, "zip")
    assert tab._job_output() == tab._output_path() == tmp_path / "wdw_47.zip"

    _select_format(tab, "mbtiles")
    assert tab._job_output() == tab._output_path() == tmp_path / "wdw_47.mbtiles"


# ---------------------------------------------------------------------------
# measured coverage
# ---------------------------------------------------------------------------


def test_the_app_finds_and_uses_a_coverage_file(qapp, context, tmp_path, monkeypatch):
    """Everything measured was reachable only from the CLI, which is not the
    thing being used. A DLR v52 download planned from declared bounds asked for
    380,800 tiles at z20 where 349,312 exist."""
    import json
    from pathlib import Path
    from tilearc_gui import download_tab as module

    coverage = tmp_path / "measured-coverage.json"
    coverage.write_text(json.dumps({"maps": {"dlr": {
        "measuredAgainst": {"version": "840388841", "label": "Jul '26 (Late)"},
        "zooms": {"20": {
            "box": {"minX": 180352, "maxX": 181247, "minY": 419328, "maxY": 419752},
            "tiles": 349312, "shape": "irregular",
            "runs": [[419328, 419711, 180352, 181247],
                     [419712, 419752, 180736, 180863]]}}}}}))

    tab = DownloadTab(context)
    tab.reload_parks()
    drain(qapp)
    select(tab.park_combo, "dlr")
    drain(qapp)
    tab._set_coverage(coverage, enable=True)
    idx = tab.version_combo.findData("52")
    assert idx >= 0
    tab.version_combo.setCurrentIndex(idx)
    tab.min_zoom.setValue(20)
    tab.max_zoom.setValue(20)
    drain(qapp)

    assert tab.plan is not None
    assert tab.plan.total_tiles == 349_312, (
        f"planned {tab.plan.total_tiles:,}; the declared box is 380,800"
    )
    assert tab.plan.zooms[0].runs is not None

    # and turning it off goes back to the declared rectangle
    tab.use_coverage.setChecked(False)
    assert tab.plan.total_tiles != 349_312


def test_a_park_missing_from_the_coverage_file_says_so(qapp, context, tmp_path):
    import json
    coverage = tmp_path / "measured-coverage.json"
    coverage.write_text(json.dumps({"maps": {"dlr": {"zooms": {}}}}))

    tab = DownloadTab(context)
    tab.reload_parks()
    drain(qapp)
    select(tab.park_combo, "wdw")
    drain(qapp)
    tab._set_coverage(coverage, enable=True)
    drain(qapp)

    assert tab.plan is not None
    assert any(
        "has no measurements for wdw" in note for note in tab.plan.notes
    ), tab.plan.notes


# ---------------------------------------------------------------------------
# a park with no version history is filed by date
# ---------------------------------------------------------------------------


def test_a_park_without_versions_is_filed_under_the_date(qapp, context, tmp_path):
    """DLP has no selectable servers -- it is always 'current'.

    Filing every download of it under 'current' would have each one overwrite
    the last, which is the opposite of an archive. The path shown has to be the
    one the writer will actually use, or the two disagree silently.
    """
    from datetime import datetime, timezone

    tab = DownloadTab(context)
    tab.plan = _plan(park_id="dlp", version="current", supports_history=False)
    tab.destination = tmp_path
    _select_format(tab, "library")

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert tab._job_output() == tmp_path
    assert tab._output_path() == tmp_path / "dlp" / today


# ---------------------------------------------------------------------------
# re-hosted versions
# ---------------------------------------------------------------------------


def test_a_rehosted_version_is_flagged_in_the_estimate(qapp, context):
    """jan2026 is a copy of DLP already downloaded and re-hosted.

    The CLI refuses it outright. The app said nothing at all, so the one park
    that must not be archived from its listed URL was the easiest to archive.
    """
    tab = DownloadTab(context)
    tab.reload_parks()
    drain(qapp)
    select(tab.park_combo, "dlp")
    drain(qapp)

    select(tab.version_combo, "jan2026")
    assert "not" in tab.estimate_label.text()
    assert "copies a copy" in tab.estimate_label.text()

    select(tab.version_combo, "current")
    assert "copies a copy" not in tab.estimate_label.text()


def test_starting_a_rehosted_version_asks_first(qapp, context, monkeypatch, tmp_path):
    from PySide6.QtWidgets import QMessageBox

    tab = DownloadTab(context)
    tab.reload_parks()
    drain(qapp)
    select(tab.park_combo, "dlp")
    drain(qapp)
    select(tab.version_combo, "jan2026")
    tab.destination = tmp_path

    asked = []
    monkeypatch.setattr(
        QMessageBox, "warning",
        lambda *args, **kwargs: (asked.append(args[2]), QMessageBox.Cancel)[1],
    )
    tab._start()
    drain(qapp)

    assert asked, "a re-hosted version must not start silently"
    assert "pub-" in asked[0] or "not the park's own" in asked[0]
    assert tab._thread is None, "cancelling must not start the job"


def test_a_normal_version_is_not_asked_about(qapp, context):
    tab = DownloadTab(context)
    tab.plan = _plan(park_id="wdw", version="47")
    assert tab._confirm_rehosted() is True


# ---------------------------------------------------------------------------
# resume state left behind by a different job
# ---------------------------------------------------------------------------


def _request_for(tab, tmp_path):
    from tilearc.downloader import DownloadOptions
    from tilearc.job import JobRequest
    from tilearc.urls import TileSource

    return JobRequest(
        plan=tab.plan,
        sources={"": TileSource("test", "https://example.test/{z}/{x}/{y}.jpg")},
        fmt="library",
        output=tmp_path,
        options=DownloadOptions(),
    )


def test_state_from_another_job_offers_to_start_over(qapp, context, monkeypatch, tmp_path):
    """The error told the user to pass --restart, which the app cannot do.

    So the job simply failed, every time, with an instruction for a program
    they do not run.
    """
    from PySide6.QtWidgets import QMessageBox
    from tilearc.state import JobState

    tab = DownloadTab(context)
    tab.plan = _plan(park_id="wdw", version="47")
    request = _request_for(tab, tmp_path)

    state = JobState(request.resolved_state_path())
    state.bind_job("a-different-job", {"park": "dlr"})
    state.close()

    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    assert tab._settle_state(request) is True
    assert request.restart is True


def test_declining_to_start_over_does_not_start(qapp, context, monkeypatch, tmp_path):
    from PySide6.QtWidgets import QMessageBox
    from tilearc.state import JobState

    tab = DownloadTab(context)
    tab.plan = _plan(park_id="wdw", version="47")
    request = _request_for(tab, tmp_path)

    state = JobState(request.resolved_state_path())
    state.bind_job("a-different-job", {"park": "dlr"})
    state.close()

    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Cancel)
    assert tab._settle_state(request) is False
    assert request.restart is False


def test_resuming_the_same_job_asks_nothing(qapp, context, monkeypatch, tmp_path):
    from PySide6.QtWidgets import QMessageBox
    from tilearc.state import JobState

    tab = DownloadTab(context)
    tab.plan = _plan(park_id="wdw", version="47")
    request = _request_for(tab, tmp_path)

    state = JobState(request.resolved_state_path())
    state.bind_job(tab.plan.fingerprint(), {"park": "wdw"})
    state.close()

    def refuse(*args, **kwargs):
        raise AssertionError("resuming its own work must not ask anything")

    monkeypatch.setattr(QMessageBox, "question", refuse)
    assert tab._settle_state(request) is True
    assert request.restart is False


def test_a_fresh_job_asks_nothing(qapp, context, tmp_path):
    tab = DownloadTab(context)
    tab.plan = _plan(park_id="wdw", version="47")
    request = _request_for(tab, tmp_path)
    assert not request.resolved_state_path().is_file()
    assert tab._settle_state(request) is True


# ---------------------------------------------------------------------------
# the Library tab
# ---------------------------------------------------------------------------


def _library(root):
    """Two versions of one park, the second changing one tile of two."""
    from tilearc.config import ParkConfig, TileBounds, VersionEntry
    from tilearc.library import LibraryWriter
    from tilearc.plan import JobPlan, ZoomPlan

    park = ParkConfig(
        park_id="wdw", label="WDW", tile_template="https://cdn/{z}/{x}/{y}.jpg",
        min_zoom=11, max_zoom=11, y_scheme="xyz",
        bounds_by_zoom={11: TileBounds(0, 1, 0, 1)},
    )

    def archive(version, tiles, complete=True):
        plan = JobPlan(park=park, version=VersionEntry(code=version),
                       zooms=[ZoomPlan(11, TileBounds(0, 1, 0, 1))], modes=[])
        writer = LibraryWriter(root, plan)
        writer.open()
        for (z, x, y), data in tiles.items():
            writer.write_tile(z, x, y, "", data)
        writer.finalize({"park": {"id": "wdw"}}, complete=complete)
        writer.catalogue.close()

    archive("47", {(11, 0, 0): b"aaaa", (11, 0, 1): b"bbbb"})
    archive("105", {(11, 0, 0): b"aaaa", (11, 0, 1): b"CHANGED"}, complete=False)


def test_library_tab_lists_what_the_archive_holds(qapp, tmp_path):
    from tilearc_gui.library_tab import LibraryTab

    _library(tmp_path)
    tab = LibraryTab()
    tab.set_root(tmp_path)

    assert tab.table.rowCount() == 2
    rows = {
        tab.table.item(r, 1).text(): [
            tab.table.item(r, c).text() for c in range(tab.table.columnCount())
        ]
        for r in range(tab.table.rowCount())
    }
    assert rows["47"][0] == "wdw"
    assert rows["47"][2] == "2" and rows["47"][3] == "2"   # both stored here
    assert rows["105"][2] == "2" and rows["105"][3] == "1"  # one reused
    assert rows["105"][4] == "1"
    assert rows["47"][6] == "yes"
    assert rows["105"][6] == "unfinished"
    assert "saves" in tab.summary.text()


def test_library_tab_finds_the_version_that_actually_stores_a_tile(qapp, tmp_path):
    """The question no amount of looking at the folders can answer.

    wdw/105/11/0/0.jpg does not exist -- it did not change -- but version 105
    does have that tile, and it is in 47.
    """
    from tilearc_gui.library_tab import LibraryTab

    _library(tmp_path)
    assert not (tmp_path / "wdw" / "105" / "11" / "0" / "0.jpg").exists()

    tab = LibraryTab()
    tab.set_root(tmp_path)
    tab.q_park.setText("wdw")
    tab.q_version.setText("105")
    tab.q_z.setText("11")
    tab.q_x.setText("0")
    tab.q_y.setText("0")
    tab._resolve()

    assert "wdw/47/11/0/0.jpg" in tab.found.text().replace("\\", "/")
    assert "stored under version 47" in tab.found.text()


def test_library_tab_says_when_a_tile_is_simply_not_there(qapp, tmp_path):
    from tilearc_gui.library_tab import LibraryTab

    _library(tmp_path)
    tab = LibraryTab()
    tab.set_root(tmp_path)
    for box, value in ((tab.q_park, "wdw"), (tab.q_version, "105"),
                       (tab.q_z, "11"), (tab.q_x, "9"), (tab.q_y, "9")):
        box.setText(value)
    tab._resolve()
    assert "not in this library" in tab.found.text()


def test_library_tab_does_not_create_a_catalogue_in_a_stray_folder(qapp, tmp_path):
    """Opening a Catalogue makes one, so a wrong folder would gain a database."""
    from tilearc.library import CATALOGUE_NAME
    from tilearc_gui.library_tab import LibraryTab

    tab = LibraryTab()
    tab.set_root(tmp_path)
    assert "No catalogue here" in tab.summary.text()
    assert tab.table.rowCount() == 0

    for box, value in ((tab.q_park, "wdw"), (tab.q_version, "47"),
                       (tab.q_z, "11"), (tab.q_x, "0"), (tab.q_y, "0")):
        box.setText(value)
    tab._resolve()

    assert "No catalogue" in tab.found.text()
    assert not (tmp_path / CATALOGUE_NAME).exists()


def test_library_tab_rejects_coordinates_that_are_not_numbers(qapp, tmp_path):
    from tilearc_gui.library_tab import LibraryTab

    _library(tmp_path)
    tab = LibraryTab()
    tab.set_root(tmp_path)
    for box, value in ((tab.q_park, "wdw"), (tab.q_version, "47"),
                       (tab.q_z, "eleven"), (tab.q_x, "0"), (tab.q_y, "0")):
        box.setText(value)
    tab._resolve()
    assert "whole numbers" in tab.found.text()


def test_the_window_has_a_library_tab_wired_to_the_download(qapp, context, monkeypatch):
    from tilearc_gui.main_window import MainWindow

    window = MainWindow()
    drain(qapp)
    titles = [window.tabs.tabText(i) for i in range(window.tabs.count())]
    assert "Library" in titles

    seen = []
    monkeypatch.setattr(window.library_tab, "set_root", lambda root: seen.append(root))
    window.download_tab.library_written.emit("/somewhere")
    assert seen == ["/somewhere"]


# ---------------------------------------------------------------------------
# choosing a folder without the system's folder panel
# ---------------------------------------------------------------------------


def _drop(widget, path):
    """Deliver a real drag of `path` onto `widget`."""
    from PySide6.QtCore import QMimeData, QPointF, QUrl, Qt
    from PySide6.QtGui import QDragEnterEvent, QDropEvent

    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(path))])
    enter = QDragEnterEvent(
        QPointF(1, 1).toPoint(), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier
    )
    widget.dragEnterEvent(enter)
    drop = QDropEvent(
        QPointF(1, 1), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier
    )
    widget.dropEvent(drop)
    return enter, drop


def test_qt_dialogs_can_be_asked_for(monkeypatch):
    """macOS draws its panel in another process, which can wedge.

    Qt's own dialog is drawn here and cannot hang on a service it never
    contacts, so there has to be a way to ask for it.
    """
    from PySide6.QtWidgets import QFileDialog
    from tilearc_gui import pickers

    monkeypatch.delenv(pickers.ENV_QT_DIALOGS, raising=False)
    assert pickers.prefers_qt_dialogs() is False
    assert pickers._options() == QFileDialog.Option(0)

    for off in ("0", "no", "false", ""):
        monkeypatch.setenv(pickers.ENV_QT_DIALOGS, off)
        assert pickers.prefers_qt_dialogs() is False

    monkeypatch.setenv(pickers.ENV_QT_DIALOGS, "1")
    assert pickers.prefers_qt_dialogs() is True
    assert pickers._options() == QFileDialog.Option.DontUseNativeDialog


def test_a_dropped_file_means_the_folder_holding_it(qapp, tmp_path):
    from PySide6.QtCore import QMimeData, QUrl
    from tilearc_gui.pickers import dropped_directory

    tile = tmp_path / "851.jpg"
    tile.write_bytes(b"x")

    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(tile))])
    assert dropped_directory(mime) == tmp_path

    mime.setUrls([QUrl.fromLocalFile(str(tmp_path))])
    assert dropped_directory(mime) == tmp_path

    assert dropped_directory(QMimeData()) is None


def test_a_destination_can_be_typed_instead_of_browsed(qapp, context, tmp_path):
    tab = DownloadTab(context)
    tab.plan = _plan(park_id="wdw", version="47")
    _select_format(tab, "library")

    tab.destination_edit.setText(str(tmp_path))
    tab.destination_edit.editingFinished.emit()

    assert tab.destination == tmp_path
    assert tab._job_output() == tmp_path
    assert str(tmp_path / "wdw" / "47") in tab.destination_label.text()


def test_a_typed_folder_that_is_not_there_says_so(qapp, context, tmp_path):
    """Silently creating a tree at a mistyped path is the worse failure."""
    tab = DownloadTab(context)
    tab.plan = _plan(park_id="wdw", version="47")
    _select_format(tab, "library")

    tab.destination_edit.setText(str(tmp_path / "tpyo"))
    tab.destination_edit.editingFinished.emit()

    assert "does not exist yet" in tab.destination_label.text()


def test_clearing_the_typed_folder_disables_download(qapp, context, tmp_path):
    tab = DownloadTab(context)
    tab.plan = _plan(park_id="wdw", version="47")
    tab.destination_edit.setText(str(tmp_path))
    tab.destination_edit.editingFinished.emit()
    assert tab.download_button.isEnabled()

    tab.destination_edit.setText("")
    tab.destination_edit.editingFinished.emit()
    assert tab.destination is None
    assert not tab.download_button.isEnabled()


def test_a_folder_dropped_on_the_download_tab_becomes_the_destination(
    qapp, context, tmp_path
):
    tab = DownloadTab(context)
    tab.plan = _plan(park_id="wdw", version="47")
    enter, drop = _drop(tab, tmp_path)

    assert enter.isAccepted() and drop.isAccepted()
    assert tab.destination == tmp_path
    assert tab.destination_edit.text() == str(tmp_path)


def test_a_folder_dropped_on_the_library_tab_opens_it(qapp, tmp_path):
    from tilearc_gui.library_tab import LibraryTab

    _library(tmp_path)
    tab = LibraryTab()
    _drop(tab, tmp_path)

    assert tab.root == tmp_path
    assert tab.table.rowCount() == 2


def test_a_library_folder_can_be_typed(qapp, tmp_path):
    from tilearc_gui.library_tab import LibraryTab

    _library(tmp_path)
    tab = LibraryTab()
    tab.path_edit.setText(str(tmp_path))
    tab.path_edit.editingFinished.emit()

    assert tab.root == tmp_path
    assert tab.table.rowCount() == 2


def test_the_pickers_are_the_only_route_to_a_file_dialog(qapp):
    """A call site that reaches past pickers.py keeps the old hang."""
    import pathlib

    import tilearc_gui

    gui = pathlib.Path(tilearc_gui.__file__).parent
    offenders = [
        path.name
        for path in gui.glob("*.py")
        if path.name != "pickers.py" and "QFileDialog" in path.read_text()
    ]
    assert offenders == []


# ---------------------------------------------------------------------------
# the coverage file has to survive being packaged
# ---------------------------------------------------------------------------


def test_the_bundle_carries_the_coverage_file():
    """A packaged app has no repo above it to find the file in.

    Without it in `datas` the built app plans every download from the declared
    bounds -- the exact failure the measurements were taken to prevent, and
    invisible unless you count the tiles.
    """
    from pathlib import Path

    spec = Path(__file__).resolve().parent.parent / "packaging" / "ParkTileArchiver.spec"
    text = spec.read_text()
    assert "measured-coverage.json" in text
    assert '(COVERAGE, "tools")' in text, "must land at tools/ inside the bundle"


def test_the_app_looks_inside_the_bundle_for_it(qapp, context, monkeypatch, tmp_path):
    """PyInstaller unpacks to sys._MEIPASS, which is where the spec puts it."""
    import json

    bundled = tmp_path / "tools"
    bundled.mkdir()
    (bundled / "measured-coverage.json").write_text(json.dumps({"maps": {}}))
    monkeypatch.setattr("sys._MEIPASS", str(tmp_path), raising=False)

    tab = DownloadTab(context)
    assert tab.coverage_path == bundled / "measured-coverage.json"


def test_no_coverage_file_is_said_out_loud(qapp, context, monkeypatch):
    """Falling back to declared bounds silently is how the DLR run lost 31,684."""
    from tilearc_gui import download_tab as module

    monkeypatch.setattr(module.Path, "is_file", lambda self: False)
    tab = DownloadTab(context)
    monkeypatch.undo()

    assert tab.coverage_path is None
    assert not tab.use_coverage.isEnabled()
    assert "none found" in tab.coverage_label.text()

    tab.reload_parks()
    drain(qapp)
    select(tab.park_combo, "wdw")
    drain(qapp)
    assert any(
        "no measured-coverage file was found" in note for note in tab.plan.notes
    ), tab.plan.notes


def test_switching_coverage_off_says_so_too(qapp, context):
    tab = DownloadTab(context)
    tab.reload_parks()
    drain(qapp)
    select(tab.park_combo, "wdw")
    drain(qapp)
    # "extends past its declared bounds" is a coverage note, not a fallback.
    assert not any("planning from the declared" in n for n in tab.plan.notes)

    tab.use_coverage.setChecked(False)
    assert any("switched off" in note for note in tab.plan.notes), tab.plan.notes


def test_pointing_at_another_file_replans_without_the_tick_changing(qapp, context, tmp_path):
    """Same tick, different file: nothing is emitted, so nothing rebuilt."""
    import json

    other = tmp_path / "measured-coverage.json"
    other.write_text(json.dumps({"maps": {"wdw": {"zooms": {
        "11": {"box": {"minX": 0, "maxX": 1, "minY": 0, "maxY": 1}, "tiles": 4}}}}}))

    tab = DownloadTab(context)
    tab.reload_parks()
    drain(qapp)
    select(tab.park_combo, "wdw")
    drain(qapp)
    tab.min_zoom.setValue(11)
    tab.max_zoom.setValue(11)
    assert tab.use_coverage.isChecked()
    assert tab.plan.total_tiles == 90

    tab._set_coverage(other, enable=True)
    assert tab.use_coverage.isChecked()
    assert tab.plan.total_tiles == 4
