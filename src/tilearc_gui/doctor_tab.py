"""The Bounds check tab: report suspicious boundsByZoom data.

Reports only. Nothing here edits a config — a rectangle silently widened would
add thousands of 404s to every future job, and one silently narrowed would drop
part of the map from the archive without saying so.
"""

from __future__ import annotations

from PySide6.QtGui import QBrush, QColor
from PySide6.QtCore import Slot
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from tilearc.doctor import check_park

from .context import AppContext
from .formatting import severity_colour
from .workers import run_async

COLUMNS = ["Park", "Zoom", "Check", "Severity", "What looks wrong"]


class DoctorTab(QWidget):
    def __init__(self, context: AppContext) -> None:
        super().__init__()
        self.context = context
        self._tasks: list = []
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
        self.run_button.clicked.connect(self._run)
        row.addWidget(self.run_button)
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
        self._tasks.append(run_async(self.context.all_parks, self._parks_loaded, self._error))

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

        self._tasks.append(run_async(work, self._done, self._error))

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
        self.summary_label.setText(f"Could not check bounds: {message}")
