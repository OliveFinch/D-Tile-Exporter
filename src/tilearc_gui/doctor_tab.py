"""The Bounds check tab: report suspicious boundsByZoom data.

Reports only. Nothing here edits a config — a rectangle silently widened would
add thousands of 404s to every future job, and one silently narrowed would drop
part of the map from the archive without saying so.
"""

from __future__ import annotations

import asyncio

from PySide6.QtCore import Slot
from PySide6.QtGui import QBrush, QColor, QFontDatabase
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from tilearc.bounds import select_zooms
from tilearc.discover import ProbeOptions, bounds_block, discover, estimate_requests
from tilearc.doctor import check_park
from tilearc.urls import build_source

from .context import AppContext
from .formatting import severity_colour
from .workers import run_async

COLUMNS = ["Park", "Zoom", "Check", "Severity", "What looks wrong"]


class DoctorTab(QWidget):
    def __init__(self, context: AppContext) -> None:
        super().__init__()
        self.context = context
        self._build()
        self.context.changed.connect(self.reload_parks)

    def _build(self) -> None:
        outer = QVBoxLayout(self)

        outer.addWidget(
            QLabel(
                "The zoom bounds in the park configs are maintained by hand and have "
                "drifted. This lists what looks wrong so you can decide — it never "
                "changes a config."
            )
        )

        row = QHBoxLayout()
        row.addWidget(QLabel("Park"))
        self.park_combo = QComboBox()
        self.park_combo.setMinimumWidth(240)
        row.addWidget(self.park_combo)
        row.addStretch(1)
        self.run_button = QPushButton("Check bounds")
        self.run_button.setToolTip(
            "Looks for suspicious numbers in the config. Fast, offline, and "
            "guesses — a park whose map covers less ground at deeper zoom will "
            "be flagged even though the data is right."
        )
        self.run_button.clicked.connect(self._run)
        row.addWidget(self.run_button)

        self.measure_button = QPushButton("Measure from server…")
        self.measure_button.setToolTip(
            "Asks the tile server where the map actually stops, instead of "
            "guessing from the numbers. Costs a few hundred requests and "
            "settles the question."
        )
        self.measure_button.clicked.connect(self._measure)
        row.addWidget(self.measure_button)
        outer.addLayout(row)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(
            len(COLUMNS) - 1, QHeaderView.Stretch
        )
        outer.addWidget(self.table, 1)

        self.summary_label = QLabel("")
        self.summary_label.setWordWrap(True)
        outer.addWidget(self.summary_label)

    # ---------------------------------------------------------------- parks

    @Slot()
    def reload_parks(self) -> None:
        self.park_combo.clear()
        self.park_combo.addItem("Loading…", None)
        run_async(self.context.all_parks, self._parks_loaded, self._error)

    def _parks_loaded(self, parks: list[tuple[str, str]]) -> None:
        self.park_combo.clear()
        self.park_combo.addItem("All parks", None)
        for park_id, label in parks:
            self.park_combo.addItem(f"{label}  ({park_id})", park_id)

    # ---------------------------------------------------------------- check

    @Slot()
    def _run(self) -> None:
        selected = self.park_combo.currentData()
        self.run_button.setEnabled(False)
        self.summary_label.setText("Checking…")

        def work():
            repository = self.context.repository()
            ids = [selected] if selected else [p for p, _ in self.context.all_parks()]
            findings = []
            for park_id in ids:
                try:
                    findings.extend(check_park(repository.park(park_id)))
                except Exception:
                    continue
            return findings

        run_async(work, self._done, self._error)

    def _done(self, findings: list) -> None:
        self.run_button.setEnabled(True)
        self.table.setRowCount(len(findings))

        for row, finding in enumerate(findings):
            values = [
                finding.park,
                "" if finding.zoom is None else f"z{finding.zoom}",
                finding.rule,
                finding.severity,
                finding.message,
            ]
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                if column == 3:
                    item.setForeground(QBrush(QColor(severity_colour(finding.severity))))
                self.table.setItem(row, column, item)

        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setSectionResizeMode(
            len(COLUMNS) - 1, QHeaderView.Stretch
        )

        errors = sum(1 for f in findings if f.severity == "error")
        if not findings:
            self.summary_label.setText("No problems found.")
        else:
            self.summary_label.setText(
                f"{len(findings)} finding(s), {errors} of them error(s). "
                f"Reported, not corrected — edit the source configs if you agree."
            )

    def _error(self, message: str) -> None:
        self.run_button.setEnabled(True)
        self.measure_button.setEnabled(True)
        self.summary_label.setText(f"Could not check bounds: {message}")

    # -------------------------------------------------------------- measure

    @Slot()
    def _measure(self) -> None:
        """Probe the tile server for the real bounds of the selected park."""
        park_id = self.park_combo.currentData()
        if not park_id:
            QMessageBox.information(
                self, "Choose a park",
                "Measuring works one park at a time — pick a specific park first.",
            )
            return

        try:
            repository = self.context.repository()
            park = repository.park(park_id)
            versions = [v for v in repository.versions(park_id) if v.active]
        except Exception as exc:
            self._error(str(exc))
            return

        if not park.tile_template and not any(v.url for v in versions):
            QMessageBox.information(
                self, "Cannot measure this park",
                f"{park.label} has no public tile template, so there is nothing "
                f"to probe. That means Tokyo Disney Resort, which needs "
                f"credentials and a proxy.",
            )
            return
        if not versions:
            self._error(f"{park.label} has no active versions to measure against")
            return

        version = versions[-1]        # the newest is the most likely to be complete
        selection = select_zooms(park, None, None)
        zooms = [(z, park.bounds_at(z)) for z in selection.zooms if park.bounds_at(z)]
        if not zooms:
            self._error("no zoom levels with bounds to measure")
            return

        upper_bound = estimate_requests(len(zooms))
        answer = QMessageBox.question(
            self,
            "Measure the real bounds?",
            f"This asks {park.label}'s tile server where the map actually stops, "
            f"for zooms {zooms[0][0]}–{zooms[-1][0]} of version {version.code}.\n\n"
            f"Up to about {upper_bound:,} requests, and far fewer if the current "
            f"bounds are already right. It changes nothing — you get the numbers "
            f"and decide.",
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Yes,
        )
        if answer != QMessageBox.Yes:
            return

        self.run_button.setEnabled(False)
        self.measure_button.setEnabled(False)
        self.summary_label.setText(f"Measuring {park.label}… 0 requests")
        self._probe_count = 0

        source = build_source(park, version)

        def work():
            return asyncio.run(discover(source, zooms, ProbeOptions()))

        run_async(work, lambda rows: self._measured(rows, park, version), self._error)

    def _measured(self, rows: list, park, version) -> None:
        self.run_button.setEnabled(True)
        self.measure_button.setEnabled(True)

        changed = [m for m in rows if m.changed]
        total = sum(m.requests for m in rows)

        lines = [
            f"{park.label} — measured against version {version.code}",
            f"{total:,} requests",
            "",
            f"{'zoom':>5}  {'declared':<28} {'measured':<28} change",
        ]
        for m in rows:
            declared = (
                f"{m.declared.min_x}-{m.declared.max_x},"
                f"{m.declared.min_y}-{m.declared.max_y}" if m.declared else "-"
            )
            measured = (
                f"{m.measured.min_x}-{m.measured.max_x},"
                f"{m.measured.min_y}-{m.measured.max_y}" if m.measured else "-"
            )
            lines.append(f"{m.zoom:>5}  {declared:<28} {measured:<28} {m.describe_change()}")
            for note in m.notes:
                lines.append(f"{'':>5}  note: {note}")

        if changed:
            delta = sum(m.tile_delta for m in rows)
            lines += [
                "",
                f"{len(changed)} zoom(s) differ from the config "
                f"({delta:+,} tiles at full depth).",
                "",
                "Paste this into "
                f"parks/{park.park_id}/{park.park_id}_config.json:",
                "",
                bounds_block(rows),
            ]
            self.summary_label.setText(
                f"{len(changed)} zoom(s) differ from the config — see the details."
            )
        else:
            lines += ["", "The config matches the server exactly. Nothing to change."]
            self.summary_label.setText("Measured: the config matches the server exactly.")

        self._show_result("\n".join(lines))

    def _show_result(self, text: str) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("Measured tile bounds")
        dialog.resize(760, 520)
        layout = QVBoxLayout(dialog)

        view = QPlainTextEdit()
        view.setReadOnly(True)
        view.setPlainText(text)
        view.setLineWrapMode(QPlainTextEdit.NoWrap)
        view.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        layout.addWidget(view)

        buttons = QHBoxLayout()
        copy = QPushButton("Copy")
        copy.clicked.connect(lambda: QApplication.clipboard().setText(text))
        buttons.addWidget(copy)
        buttons.addStretch(1)
        close = QPushButton("Close")
        close.setDefault(True)
        close.clicked.connect(dialog.accept)
        buttons.addWidget(close)
        layout.addLayout(buttons)

        dialog.exec()
