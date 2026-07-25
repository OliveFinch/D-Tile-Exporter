"""The Download tab: pick a park, a version, a zoom range and a folder."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QThread, Slot
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from tilearc.config import ParkConfig, VersionEntry
from tilearc.downloader import CONCURRENCY_WARN_THRESHOLD, DownloadOptions
from tilearc.job import JobRequest
from tilearc.plan import DEFAULT_BYTES_PER_TILE, build_plan
from tilearc.urls import build_source

from .context import AppContext
from .formatting import human_bytes, human_duration
from .workers import DownloadWorker, run_async

FORMAT_CHOICES = [
    ("dir", "Folder of tiles  —  {z}/{x}/{y}.jpg"),
    ("zip", "Zip archive"),
    ("mbtiles", "MBTiles database"),
]

#: Above this the UI says something; the library caps hard elsewhere.
LARGE_JOB_TILES = 100_000


class DownloadTab(QWidget):
    def __init__(self, context: AppContext) -> None:
        super().__init__()
        self.context = context
        self.park: ParkConfig | None = None
        self.versions: list[VersionEntry] = []
        self.plan = None
        self.destination: Path | None = None

        self._thread: QThread | None = None
        self._worker: DownloadWorker | None = None
        # Bumped on every load so a slow reply from a source the user has
        # already switched away from is discarded instead of overwriting the
        # newer one.
        self._generation = 0

        self._build()
        self.context.changed.connect(self.reload_parks)

    # ------------------------------------------------------------------ UI

    def _build(self) -> None:
        outer = QVBoxLayout(self)

        # -- source -------------------------------------------------------
        source_box = QGroupBox("Map version")
        form = QFormLayout(source_box)

        self.park_combo = QComboBox()
        self.park_combo.currentIndexChanged.connect(self._park_changed)
        form.addRow("Park", self.park_combo)

        version_row = QHBoxLayout()
        self.version_combo = QComboBox()
        self.version_combo.setMinimumWidth(260)
        self.version_combo.currentIndexChanged.connect(self._rebuild_plan)
        version_row.addWidget(self.version_combo, 1)
        self.inactive_check = QCheckBox("Show retired")
        self.inactive_check.setToolTip(
            "Versions the viewer no longer lists. They usually still work — "
            "preserving them is the point of archiving."
        )
        self.inactive_check.toggled.connect(self._reload_versions)
        version_row.addWidget(self.inactive_check)
        form.addRow("Version", version_row)

        zoom_row = QHBoxLayout()
        self.min_zoom = QSpinBox()
        self.max_zoom = QSpinBox()
        for box in (self.min_zoom, self.max_zoom):
            box.setRange(0, 24)
            box.valueChanged.connect(self._rebuild_plan)
        zoom_row.addWidget(QLabel("from"))
        zoom_row.addWidget(self.min_zoom)
        zoom_row.addWidget(QLabel("to"))
        zoom_row.addWidget(self.max_zoom)
        self.zoom_hint = QLabel("")
        self.zoom_hint.setStyleSheet("color: gray;")
        zoom_row.addWidget(self.zoom_hint)
        zoom_row.addStretch(1)
        form.addRow("Zoom levels", zoom_row)

        outer.addWidget(source_box)

        # -- estimate -----------------------------------------------------
        self.estimate_label = QLabel("—")
        self.estimate_label.setTextFormat(Qt.RichText)
        self.estimate_label.setWordWrap(True)
        self.estimate_label.setStyleSheet(
            "QLabel { background: palette(alternate-base); padding: 8px; "
            "border-radius: 4px; }"
        )
        outer.addWidget(self.estimate_label)

        # -- destination --------------------------------------------------
        dest_box = QGroupBox("Save to")
        dest_layout = QVBoxLayout(dest_box)

        picker_row = QHBoxLayout()
        self.format_combo = QComboBox()
        for value, label in FORMAT_CHOICES:
            self.format_combo.addItem(label, value)
        self.format_combo.currentIndexChanged.connect(self._update_destination_label)
        picker_row.addWidget(self.format_combo, 1)
        self.browse_button = QPushButton("Choose folder…")
        self.browse_button.clicked.connect(self._choose_folder)
        picker_row.addWidget(self.browse_button)
        dest_layout.addLayout(picker_row)

        self.destination_label = QLabel("No folder chosen")
        self.destination_label.setStyleSheet("color: gray;")
        self.destination_label.setWordWrap(True)
        dest_layout.addWidget(self.destination_label)

        polite_row = QHBoxLayout()
        polite_row.addWidget(QLabel("Parallel requests"))
        self.concurrency = QSpinBox()
        self.concurrency.setRange(1, 32)
        self.concurrency.setValue(5)
        self.concurrency.valueChanged.connect(self._check_politeness)
        polite_row.addWidget(self.concurrency)
        polite_row.addSpacing(12)
        polite_row.addWidget(QLabel("Max requests/second"))
        self.rps = QSpinBox()
        self.rps.setRange(0, 100)
        self.rps.setValue(10)
        self.rps.setSpecialValueText("no limit")
        polite_row.addWidget(self.rps)
        polite_row.addStretch(1)
        dest_layout.addLayout(polite_row)

        self.politeness_warning = QLabel("")
        self.politeness_warning.setStyleSheet("color: #b36b00;")
        self.politeness_warning.setWordWrap(True)
        dest_layout.addWidget(self.politeness_warning)

        outer.addWidget(dest_box)

        # -- progress -----------------------------------------------------
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        outer.addWidget(self.progress_bar)

        self.progress_label = QLabel("Ready.")
        outer.addWidget(self.progress_label)

        self.counts_label = QLabel("")
        self.counts_label.setStyleSheet("color: gray;")
        outer.addWidget(self.counts_label)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(500)
        self.log.setMinimumHeight(90)
        self.log.setPlaceholderText(
            "Missing tiles are normal — park bounds are rectangles, the drawn map is not."
        )
        outer.addWidget(self.log, 1)

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        self.stop_button = QPushButton("Stop")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self._stop)
        button_row.addWidget(self.stop_button)
        self.download_button = QPushButton("Download")
        self.download_button.setDefault(True)
        self.download_button.setEnabled(False)
        self.download_button.clicked.connect(self._start)
        button_row.addWidget(self.download_button)
        outer.addLayout(button_row)

    # -------------------------------------------------------------- loading

    @Slot()
    def reload_parks(self) -> None:
        self._generation += 1
        generation = self._generation

        self.park_combo.blockSignals(True)
        self.park_combo.clear()
        self.park_combo.addItem("Loading…", None)
        self.park_combo.blockSignals(False)
        self.park_combo.setEnabled(False)

        run_async(
            self.context.downloadable_park_ids,
            lambda parks, g=generation: self._parks_loaded(parks, g),
            lambda message, g=generation: self._load_failed(message, g),
        )

    def _parks_loaded(self, parks: list[tuple[str, str]], generation: int) -> None:
        if generation != self._generation:
            return
        self.park_combo.setEnabled(True)
        self.park_combo.blockSignals(True)
        self.park_combo.clear()
        for park_id, label in parks:
            self.park_combo.addItem(f"{label}  ({park_id})", park_id)
        self.park_combo.blockSignals(False)

        if parks:
            self._park_changed()
        else:
            self._note("No parks could be loaded. Check the park data source.")

    def _load_failed(self, message: str, generation: int | None = None) -> None:
        if generation is not None and generation != self._generation:
            return
        self.park_combo.setEnabled(True)
        self._note(f"Could not load park data: {message}")

    @Slot()
    def _park_changed(self) -> None:
        park_id = self.park_combo.currentData()
        if not park_id:
            return
        generation = self._generation

        def load():
            repository = self.context.repository()
            return repository.park(park_id), repository.versions(park_id)

        run_async(
            load,
            lambda payload, g=generation: self._park_loaded(payload, g),
            lambda message, g=generation: self._load_failed(message, g),
        )

    def _park_loaded(self, payload, generation: int) -> None:
        if generation != self._generation:
            return
        park, versions = payload
        self.park = park
        self._all_versions = versions

        for box in (self.min_zoom, self.max_zoom):
            box.blockSignals(True)
            box.setRange(park.min_zoom, park.max_zoom)
        # Deep enough to be useful, shallow enough that nobody starts a 14 GB
        # job by accident.
        self.min_zoom.setValue(park.min_zoom)
        self.max_zoom.setValue(min(17, park.max_zoom))
        for box in (self.min_zoom, self.max_zoom):
            box.blockSignals(False)

        self.zoom_hint.setText(f"(this park has {park.min_zoom}–{park.max_zoom})")
        self._reload_versions()

    @Slot()
    def _reload_versions(self) -> None:
        if self.park is None:
            return
        show_all = self.inactive_check.isChecked()
        self.versions = [v for v in self._all_versions if show_all or v.active]

        self.version_combo.blockSignals(True)
        self.version_combo.clear()
        for version in self.versions:
            label = version.label or version.code
            suffix = "" if version.active else "  · retired"
            self.version_combo.addItem(f"{label}  —  {version.code}{suffix}", version.code)
        self.version_combo.blockSignals(False)
        self._rebuild_plan()

    # ----------------------------------------------------------------- plan

    @Slot()
    def _rebuild_plan(self) -> None:
        code = self.version_combo.currentData()
        version = next((v for v in self.versions if v.code == code), None)

        if self.park is None or version is None or self.min_zoom.value() > self.max_zoom.value():
            self.plan = None
            self.estimate_label.setText("—")
            self._update_destination_label()
            return

        self.plan = build_plan(
            self.park,
            version,
            min_zoom=self.min_zoom.value(),
            max_zoom=self.max_zoom.value(),
        )
        self._show_estimate()
        self._update_destination_label()

    def _show_estimate(self) -> None:
        plan = self.plan
        if plan is None or not plan.zooms:
            self.estimate_label.setText(
                "<b>Nothing to download</b> at these zoom levels."
            )
            return

        size = human_bytes(plan.estimated_bytes(DEFAULT_BYTES_PER_TILE))
        lines = [f"<b>{plan.total_tiles:,} tiles</b> &nbsp; ≈ {size}"]

        if plan.total_tiles > LARGE_JOB_TILES:
            lines.append(
                "<span style='color:#b36b00;'>That is a very large job — "
                "consider lowering the maximum zoom.</span>"
            )
        for note in plan.notes:
            lines.append(f"<span style='color:gray;'>{note}</span>")

        try:
            source = build_source(plan.park, plan.version)
            example = source.url(plan.zooms[0].zoom, plan.zooms[0].bounds.min_x,
                                 plan.zooms[0].bounds.min_y)
            lines.append(f"<span style='color:gray; font-family:monospace;'>{example}</span>")
        except Exception as exc:
            lines.append(f"<span style='color:#c0392b;'>{exc}</span>")

        self.estimate_label.setText("<br>".join(lines))

    # ---------------------------------------------------------- destination

    @Slot()
    def _choose_folder(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Choose where to save the tiles")
        if chosen:
            self.destination = Path(chosen)
            self._update_destination_label()

    def _output_path(self) -> Path | None:
        if self.destination is None or self.plan is None:
            return None
        fmt = self.format_combo.currentData()
        stem = self.plan.slug
        if fmt == "zip":
            return self.destination / f"{stem}.zip"
        if fmt == "mbtiles":
            return self.destination / f"{stem}.mbtiles"
        return self.destination / stem

    @Slot()
    def _update_destination_label(self) -> None:
        path = self._output_path()
        if path is None:
            self.destination_label.setText(
                "No folder chosen" if self.destination is None else str(self.destination)
            )
        else:
            self.destination_label.setText(str(path))
        self._refresh_buttons()

    @Slot()
    def _check_politeness(self) -> None:
        if self.concurrency.value() > CONCURRENCY_WARN_THRESHOLD:
            self.politeness_warning.setText(
                f"{self.concurrency.value()} parallel requests is a lot for someone "
                f"else's production CDN. Please keep this near 5 unless you have a reason."
            )
        else:
            self.politeness_warning.setText("")

    def _refresh_buttons(self) -> None:
        running = self._thread is not None
        ready = (
            not running
            and self.plan is not None
            and self.plan.total_tiles > 0
            and self.destination is not None
        )
        self.download_button.setEnabled(ready)
        self.stop_button.setEnabled(running)
        for widget in (
            self.park_combo, self.version_combo, self.inactive_check,
            self.min_zoom, self.max_zoom, self.format_combo,
            self.browse_button, self.concurrency, self.rps,
        ):
            widget.setEnabled(not running)

    # ------------------------------------------------------------- download

    @Slot()
    def _start(self) -> None:
        plan, output = self.plan, self._output_path()
        if plan is None or output is None:
            return

        try:
            sources = {"": build_source(plan.park, plan.version)}
        except Exception as exc:
            QMessageBox.critical(self, "Cannot build tile URLs", str(exc))
            return

        request = JobRequest(
            plan=plan,
            sources=sources,
            fmt=self.format_combo.currentData(),
            output=output,
            options=DownloadOptions(
                concurrency=self.concurrency.value(),
                rps=float(self.rps.value()),
            ),
        )

        self.log.clear()
        self._note(f"Downloading {plan.total_tiles:,} tiles to {output}")
        self.progress_bar.setValue(0)

        self._worker = DownloadWorker(request)
        self._thread = QThread(self)
        self._worker.moveToThread(self._thread)

        self._thread.started.connect(self._worker.run)
        self._worker.progressed.connect(self._on_progress)
        self._worker.logged.connect(self._note)
        self._worker.resumed.connect(self._on_resumed)
        self._worker.finished.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)

        self._thread.start()
        self._refresh_buttons()

    @Slot()
    def _stop(self) -> None:
        if self._worker is not None:
            self.stop_button.setEnabled(False)
            self._note("Stopping — finishing the tiles already in flight…")
            self._worker.stop()

    @Slot(dict)
    def _on_progress(self, snapshot: dict) -> None:
        total = snapshot["total"] or 1
        done = snapshot["done"]
        self.progress_bar.setValue(int(done / total * 100))

        # Rate and time remaining count only tiles actually fetched; tiles
        # skipped on resume are free and would otherwise distort both.
        fetched = snapshot["ok"] + snapshot["missing"] + snapshot["failed"]
        elapsed = max(snapshot["elapsed"], 1e-6)
        rate = fetched / elapsed
        remaining = max(0, total - done)
        eta = human_duration(remaining / rate) if rate > 0.01 else "—"

        self.progress_label.setText(
            f"{done:,} of {snapshot['total']:,} tiles  ·  "
            f"{human_bytes(snapshot['bytes'])}  ·  "
            f"{rate:.0f} tiles/s  ·  {eta} left"
        )
        self.counts_label.setText(
            f"downloaded {snapshot['ok']:,}   ·   "
            f"already there {snapshot['skipped']:,}   ·   "
            f"no imagery {snapshot['missing']:,}   ·   "
            f"failed {snapshot['failed']:,}"
        )

    @Slot(dict)
    def _on_resumed(self, counts: dict) -> None:
        self._note(
            f"Resuming: {counts.get('done', 0):,} already downloaded, "
            f"{counts.get('missing', 0):,} known to have no imagery, "
            f"{counts.get('failed', 0):,} to retry."
        )

    @Slot(object)
    def _on_finished(self, outcome) -> None:
        if outcome.stopped_early:
            if outcome.error is not None:
                self._note(f"Stopped: {outcome.error}")
            else:
                self._note("Stopped.")
            if self.format_combo.currentData() == "zip":
                self._note(
                    f"Staged in {outcome.artefact} — not packed, because the job "
                    f"is unfinished. Press Download again to carry on."
                )
            else:
                self._note(f"Partial archive at {outcome.artefact}.")
        elif outcome.failed:
            self._note(
                f"Finished with {outcome.failed:,} failed tile(s). "
                f"Press Download again to retry just those."
            )
        else:
            self._note(
                f"Done. {outcome.downloaded:,} tiles, "
                f"{outcome.missing:,} with no imagery. Written to {outcome.artefact}"
            )
        self._teardown()

    @Slot(str)
    def _on_failed(self, message: str) -> None:
        self._note(f"Failed: {message}")
        QMessageBox.warning(self, "Download failed", message)
        self._teardown()

    def _teardown(self) -> None:
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(5000)
            self._thread.deleteLater()
        self._thread = None
        self._worker = None
        self._refresh_buttons()

    def _note(self, message: str) -> None:
        self.log.appendPlainText(message.strip())
