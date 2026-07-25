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


def test_tdr_is_not_offered_for_download(qapp, context):
    """TDR needs credentials and a proxy, which this front-end does not do."""
    tab = DownloadTab(context)
    tab.reload_parks()
    drain(qapp)

    ids = {tab.park_combo.itemData(i) for i in range(tab.park_combo.count())}
    assert "tdr" not in ids


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
    assert tab.plan.total_tiles == 37_850
    assert "37,850 tiles" in tab.estimate_label.text()

    tab.max_zoom.setValue(20)
    assert tab.plan.total_tiles == 575_450
    assert "very large job" in tab.estimate_label.text()


def test_zoom_spinboxes_are_clamped_to_the_park_range(qapp, context):
    tab = DownloadTab(context)
    tab.reload_parks()
    drain(qapp)
    select(tab.park_combo, "hkdl")
    drain(qapp)

    assert (tab.min_zoom.minimum(), tab.min_zoom.maximum()) == (14, 20)
    tab.max_zoom.setValue(99)          # a spin box clamps rather than accepting
    assert tab.max_zoom.value() == 20
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
    assert "media.disneylandparis.com" not in text
    assert "own tile server" in text or "overrides" in text

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
