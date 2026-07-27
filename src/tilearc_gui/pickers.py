"""Choosing a file or a folder, and the ways round having to.

macOS draws its open panel in a *different process* -- the app asks a system
service for one and waits. When that service is wedged, which a stale network
mount or an unreachable iCloud item is enough to do, the panel never appears
and the app beach-balls until the request gives up. Nothing in this process is
stuck and nothing in this process can unstick it; the call simply does not
return.

Two answers, because neither alone is enough:

* ``TILEARC_QT_DIALOGS=1`` picks Qt's own dialog instead. It is plainer, but it
  is drawn in this process and cannot hang waiting on a service it never talks
  to.
* Nothing should *need* a dialog. Every place that takes a folder also takes
  one typed, pasted or dropped onto the window, so a broken panel costs you the
  convenience of browsing rather than the use of the app.
"""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QMimeData
from PySide6.QtWidgets import QFileDialog, QWidget

#: Set to anything but 0/no/false to draw the pickers with Qt instead of the
#: platform. Read per call, so it can be changed without a rebuild.
ENV_QT_DIALOGS = "TILEARC_QT_DIALOGS"

_OFF = ("", "0", "no", "false", "off")


def prefers_qt_dialogs() -> bool:
    return os.environ.get(ENV_QT_DIALOGS, "").strip().lower() not in _OFF


def _options() -> QFileDialog.Option:
    if prefers_qt_dialogs():
        return QFileDialog.Option.DontUseNativeDialog
    return QFileDialog.Option(0)


def choose_directory(
    parent: QWidget | None, caption: str, start: Path | str | None = None
) -> Path | None:
    chosen = QFileDialog.getExistingDirectory(
        parent,
        caption,
        str(start or ""),
        QFileDialog.Option.ShowDirsOnly | _options(),
    )
    return Path(chosen) if chosen else None


def choose_file(
    parent: QWidget | None,
    caption: str,
    name_filter: str = "All files (*)",
    start: Path | str | None = None,
) -> Path | None:
    chosen, _selected = QFileDialog.getOpenFileName(
        parent, caption, str(start or ""), name_filter, "", _options()
    )
    return Path(chosen) if chosen else None


def dropped_directory(mime: QMimeData) -> Path | None:
    """The first folder in a drag, or None if it carries none.

    A file is read as the folder containing it: dragging a tile out of an
    archive to say "this one" is a reasonable thing to try, and refusing it
    teaches nothing.
    """
    if not mime.hasUrls():
        return None
    for url in mime.urls():
        local = url.toLocalFile()
        if not local:
            continue
        path = Path(local)
        if path.is_dir():
            return path
        if path.is_file():
            return path.parent
    return None
