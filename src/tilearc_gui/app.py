import sys

from PySide6.QtWidgets import QApplication

from . import APP_NAME
from .main_window import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setOrganizationName("Magic Parks Explorer")

    window = MainWindow()
    window.show()
    return app.exec()
