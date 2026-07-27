"""The Library tab: what the archive holds, and where each tile actually is.

A library stores a tile only where it differs from an earlier version, so most
version folders on disk are mostly empty. That is the point of it, and it is
also why looking at the folders tells you very little: the answer to "do I have
this version" is in the catalogue, not the filesystem.

So this reads the catalogue. It is the only window onto an archive that is
deliberately not self-describing on disk.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Slot
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from tilearc.library import CATALOGUE_NAME, Catalogue, human_saving

from .formatting import human_bytes
from .pickers import choose_directory, dropped_directory

COLUMNS = ("Park", "Version", "Tiles", "Stored here", "Reused", "On disk", "Done")


class LibraryTab(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.root: Path | None = None
        self._build()
        self.setAcceptDrops(True)

    def _build(self) -> None:
        outer = QVBoxLayout(self)

        outer.addWidget(
            QLabel(
                "A library keeps one copy of each tile and records which version "
                "folder holds it, so a newer version stores only what changed. "
                "Most of its folders are meant to look sparse — this is where the "
                "archive actually describes itself."
            )
        )

        picker = QHBoxLayout()
        # Typed as well as browsed, and droppable: the system's folder panel is
        # a separate process, and a wedged one must not take the tab with it.
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText(
            "type or paste a library folder, or drop one onto this window"
        )
        self.path_edit.returnPressed.connect(self._typed)
        self.path_edit.editingFinished.connect(self._typed)
        picker.addWidget(self.path_edit, 1)
        self.browse = QPushButton("Choose library folder…")
        self.browse.clicked.connect(self._choose)
        picker.addWidget(self.browse)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.refresh)
        self.refresh_button.setEnabled(False)
        picker.addWidget(self.refresh_button)
        outer.addLayout(picker)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        outer.addWidget(self.table, 1)

        self.summary = QLabel("—")
        self.summary.setTextFormat(Qt.RichText)
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet(
            "QLabel { background: palette(alternate-base); padding: 8px; "
            "border-radius: 4px; }"
        )
        outer.addWidget(self.summary)

        # -- find one tile -------------------------------------------------
        # The question the tree cannot answer on its own: a version's folder
        # will not contain a tile it did not change, and nothing on disk says
        # which folder does.
        finder = QHBoxLayout()
        finder.addWidget(QLabel("Where is"))
        self.q_park = QLineEdit(); self.q_park.setPlaceholderText("park")
        self.q_version = QLineEdit(); self.q_version.setPlaceholderText("version")
        self.q_z = QLineEdit(); self.q_z.setPlaceholderText("z")
        self.q_x = QLineEdit(); self.q_x.setPlaceholderText("x")
        self.q_y = QLineEdit(); self.q_y.setPlaceholderText("y")
        # Only TDR has these; everything else is stored under the empty mode.
        self.q_mode = QLineEdit(); self.q_mode.setPlaceholderText("mode")
        self.q_mode.setToolTip("Only Tokyo needs this — daytime or nighttime.")
        for box, width in (
            (self.q_park, 70), (self.q_version, 110),
            (self.q_z, 45), (self.q_x, 80), (self.q_y, 80),
            (self.q_mode, 90),
        ):
            box.setMaximumWidth(width)
            box.returnPressed.connect(self._resolve)
            finder.addWidget(box)
        self.find_button = QPushButton("Find")
        self.find_button.clicked.connect(self._resolve)
        finder.addWidget(self.find_button)
        finder.addStretch(1)
        outer.addLayout(finder)

        self.found = QLabel("")
        self.found.setWordWrap(True)
        self.found.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.found.setStyleSheet("font-family: monospace;")
        outer.addWidget(self.found)

    # ------------------------------------------------------------------ use

    @Slot()
    def _choose(self) -> None:
        chosen = choose_directory(self, "Library folder", self.root)
        if chosen:
            self.set_root(chosen)

    @Slot()
    def _typed(self) -> None:
        text = self.path_edit.text().strip()
        if text and Path(text).expanduser() != self.root:
            self.set_root(Path(text).expanduser())

    def set_root(self, root: Path) -> None:
        self.root = Path(root)
        self.path_edit.setText(str(self.root))
        self.refresh_button.setEnabled(True)
        self.refresh()

    def dragEnterEvent(self, event) -> None:  # noqa: N802 - Qt's name
        if dropped_directory(event.mimeData()) is not None:
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802 - Qt's name
        folder = dropped_directory(event.mimeData())
        if folder is None:
            return
        self.set_root(folder)
        event.acceptProposedAction()

    @Slot()
    def refresh(self) -> None:
        self.table.setRowCount(0)
        if self.root is None:
            return
        if not (self.root / CATALOGUE_NAME).is_file():
            self.summary.setText(
                f"<b>No catalogue here.</b> {CATALOGUE_NAME} is written by a "
                f"download whose format is Library — this folder has not had one."
            )
            return

        catalogue = Catalogue(self.root)
        try:
            stats = catalogue.stats()
            done = {
                (row["park"], row["version"]): row["complete"]
                for row in catalogue.versions()
            }
        finally:
            catalogue.close()

        if not stats:
            self.summary.setText("<b>The library is empty.</b>")
            return

        self.table.setRowCount(len(stats))
        for index, row in enumerate(stats):
            reused = row["tiles"] - row["stored"]
            finished = done.get((row["park"], row["version"]), 0)
            cells = (
                row["park"],
                row["version"],
                f"{row['tiles']:,}",
                f"{row['stored']:,}",
                f"{reused:,}",
                human_bytes(row["stored_bytes"] or 0),
                "yes" if finished else "unfinished",
            )
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if column >= 2 and column <= 5:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if column == 6 and not finished:
                    item.setForeground(QColor("#b36b00"))
                self.table.setItem(index, column, item)

        totals = human_saving(stats)
        saved = (
            totals["savedBytes"] / totals["logicalBytes"] * 100
            if totals["logicalBytes"] else 0
        )
        self.summary.setText(
            f"<b>{len(stats)} version(s)</b> &nbsp; "
            f"{human_bytes(totals['storedBytes'])} on disk<br>"
            f"<span style='color:gray;'>Archiving each version separately would "
            f"take {human_bytes(totals['logicalBytes'])}; not storing unchanged "
            f"tiles twice saves {human_bytes(totals['savedBytes'])} "
            f"({saved:.0f}%).</span>"
        )

    @Slot()
    def _resolve(self) -> None:
        if self.root is None:
            self.found.setText("Choose a library folder first.")
            return
        # Opening a catalogue creates one, so a mistyped folder would otherwise
        # leave an empty database behind and answer "not in this library".
        if not (self.root / CATALOGUE_NAME).is_file():
            self.found.setText(f"No {CATALOGUE_NAME} in {self.root}.")
            return
        try:
            park = self.q_park.text().strip()
            version = self.q_version.text().strip()
            z, x, y = (int(b.text()) for b in (self.q_z, self.q_x, self.q_y))
        except ValueError:
            self.found.setText("z, x and y must be whole numbers.")
            return
        if not park or not version:
            self.found.setText("Park and version are both needed.")
            return
        mode = self.q_mode.text().strip()

        catalogue = Catalogue(self.root)
        try:
            path = catalogue.resolve(park, version, z, x, y, mode)
        finally:
            catalogue.close()

        if path is None:
            self.found.setText(
                f"{park} {version} {z}/{x}/{y} is not in this library."
            )
            return
        # Saying which version stores it is the useful part: for most tiles it
        # is not the one asked about.
        parts = path.relative_to(self.root).parts
        holder = parts[1] if len(parts) > 1 else version
        note = "" if holder == version else f"   (stored under version {holder})"
        self.found.setText(f"{path}{note}")
