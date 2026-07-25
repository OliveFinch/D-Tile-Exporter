"""The Verify tab: check an archive is complete and its tiles are intact."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Slot
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from tilearc.verify import verify as verify_archive

from .formatting import human_bytes
from .workers import run_async


class VerifyTab(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.path: Path | None = None
        self._tasks: list = []
        self._build()

    def _build(self) -> None:
        outer = QVBoxLayout(self)

        outer.addWidget(
            QLabel(
                "Checks a zip, folder or MBTiles archive: that every tile is a "
                "readable image, and that the counts match its manifest."
            )
        )

        row = QHBoxLayout()
        self.path_label = QLabel("No archive chosen")
        self.path_label.setStyleSheet("color: gray;")
        self.path_label.setWordWrap(True)
        row.addWidget(self.path_label, 1)
        pick_file = QPushButton("Choose file…")
        pick_file.clicked.connect(self._choose_file)
        row.addWidget(pick_file)
        pick_folder = QPushButton("Choose folder…")
        pick_folder.clicked.connect(self._choose_folder)
        row.addWidget(pick_folder)
        outer.addLayout(row)

        options = QHBoxLayout()
        self.deep_check = QCheckBox("Check every image, not just file sizes")
        self.deep_check.setChecked(True)
        self.deep_check.setToolTip(
            "Reads each tile and confirms it is a complete JPEG. Slower, but it "
            "is what catches a truncated download."
        )
        options.addWidget(self.deep_check)
        options.addStretch(1)
        self.verify_button = QPushButton("Verify")
        self.verify_button.setEnabled(False)
        self.verify_button.clicked.connect(self._verify)
        options.addWidget(self.verify_button)
        outer.addLayout(options)

        self.summary_label = QLabel("")
        outer.addWidget(self.summary_label)

        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        outer.addWidget(self.output, 1)

    @Slot()
    def _choose_file(self) -> None:
        chosen, _filter = QFileDialog.getOpenFileName(
            self, "Choose an archive", "", "Archives (*.zip *.mbtiles);;All files (*)"
        )
        if chosen:
            self._set_path(Path(chosen))

    @Slot()
    def _choose_folder(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Choose an archive folder")
        if chosen:
            self._set_path(Path(chosen))

    def _set_path(self, path: Path) -> None:
        self.path = path
        self.path_label.setText(str(path))
        self.verify_button.setEnabled(True)

    @Slot()
    def _verify(self) -> None:
        if self.path is None:
            return
        path, deep = self.path, self.deep_check.isChecked()

        self.verify_button.setEnabled(False)
        self.summary_label.setText("Checking…")
        self.output.clear()

        self._tasks.append(
            run_async(lambda: verify_archive(path, deep=deep), self._done, self._error)
        )

    def _done(self, report) -> None:
        self.verify_button.setEnabled(True)

        colour = "#2d7a2d" if report.ok else "#c0392b"
        verdict = "OK" if report.ok else f"FAILED — {len(report.problems)} problem(s)"
        self.summary_label.setText(
            f"<span style='color:{colour}; font-weight:bold;'>{verdict}</span> &nbsp; "
            f"{report.tiles_found:,} tiles, {human_bytes(report.bytes_found)}"
        )

        lines: list[str] = []
        if report.manifest:
            tiles = report.manifest.get("tiles", {})
            lines.append(
                "manifest: requested {r}, fetched {f}, missing {m}, failed {x}".format(
                    r=tiles.get("requested", "?"), f=tiles.get("fetched", "?"),
                    m=tiles.get("missing", "?"), x=tiles.get("failed", "?"),
                )
            )
        for warning in report.warnings:
            lines.append(f"warning: {warning}")
        for problem in report.problems[:200]:
            lines.append(f"PROBLEM: {problem}")
        if len(report.problems) > 200:
            lines.append(f"… and {len(report.problems) - 200} more")
        if not lines:
            lines.append("Nothing to report — every tile is present and readable.")

        self.output.setPlainText("\n".join(lines))

    def _error(self, message: str) -> None:
        self.verify_button.setEnabled(True)
        self.summary_label.setText(
            f"<span style='color:#c0392b; font-weight:bold;'>Could not read it</span>"
        )
        self.output.setPlainText(message)
