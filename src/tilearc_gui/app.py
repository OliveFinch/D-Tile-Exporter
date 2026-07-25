import sys

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from . import APP_NAME
from .main_window import MainWindow

#: Build a window, then quit. `build-app.sh` runs the packaged binary with this
#: so a bundle that cannot start is caught at build time rather than at the
#: user's first double-click.
SELF_TEST_FLAG = "--self-test"


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    self_test = SELF_TEST_FLAG in argv
    if self_test:
        argv.remove(SELF_TEST_FLAG)

    app = QApplication(argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setOrganizationName("Magic Parks Explorer")

    window = MainWindow()
    window.show()

    if self_test:
        QTimer.singleShot(0, app.quit)
        app.exec()
        print(f"{APP_NAME}: self-test ok")
        return 0

    return app.exec()
