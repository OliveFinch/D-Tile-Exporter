"""Entry point for the packaged app.

PyInstaller runs its entry script as a top-level module, not as part of a
package, so `src/tilearc_gui/__main__.py` cannot be used directly: its
`from .app import main` is a relative import with no parent package, and it
fails before anything is drawn. Worse, the same failure happens during
analysis, so PyInstaller never sees the PySide6 import and silently builds a
bundle with no Qt in it.

Absolute imports here keep both the build and the launch honest.
"""

import sys

from tilearc_gui.app import main

if __name__ == "__main__":
    sys.exit(main())
