from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Slot
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import APP_NAME, DEFAULT_CONFIG_URL
from .context import AppContext
from .doctor_tab import DoctorTab
from .download_tab import DownloadTab
from .library_tab import LibraryTab
from .verify_tab import VerifyTab


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(780, 660)

        self.context = AppContext()

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addWidget(self._build_source_bar())

        self.tabs = QTabWidget()
        self.download_tab = DownloadTab(self.context)
        self.library_tab = LibraryTab()
        self.verify_tab = VerifyTab()
        self.doctor_tab = DoctorTab(self.context)
        self.tabs.addTab(self.download_tab, "Download")
        self.tabs.addTab(self.library_tab, "Library")
        self.tabs.addTab(self.verify_tab, "Verify")
        self.tabs.addTab(self.doctor_tab, "Bounds check")
        layout.addWidget(self.tabs, 1)

        # A finished library download is exactly when someone wants to look at
        # the catalogue, and the tab already knows where it is.
        self.download_tab.library_written.connect(self.library_tab.set_root)

        self.setCentralWidget(central)

        self.download_tab.reload_parks()
        self.doctor_tab.reload_parks()

    def _build_source_bar(self) -> QWidget:
        bar = QWidget()
        row = QHBoxLayout(bar)
        row.setContentsMargins(0, 0, 0, 0)

        row.addWidget(QLabel("Park data"))
        self.source_label = QLabel(DEFAULT_CONFIG_URL)
        self.source_label.setStyleSheet("color: gray;")
        self.source_label.setToolTip(
            "Park configs and version lists are read from here. Nothing about "
            "the parks is built into this app."
        )
        row.addWidget(self.source_label, 1)

        use_live = QPushButton("Use GitHub")
        use_live.clicked.connect(self._use_live)
        row.addWidget(use_live)

        use_local = QPushButton("Use local folder…")
        use_local.clicked.connect(self._use_local)
        row.addWidget(use_local)

        return bar

    @Slot()
    def _use_live(self) -> None:
        self.context.use_live()
        self.source_label.setText(self.context.description)

    @Slot()
    def _use_local(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Choose a checkout of the viewer repo (or its parks folder)"
        )
        if chosen:
            self.context.use_directory(Path(chosen))
            self.source_label.setText(self.context.description)
