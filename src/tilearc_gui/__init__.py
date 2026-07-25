"""Desktop front-end for tilearc.

A thin Qt layer over the ``tilearc`` package: every decision about which tiles
exist, how to address them and how politely to fetch them lives in the library,
which is covered by its own tests. This package only draws widgets and moves
work off the UI thread.
"""

__all__ = ["APP_NAME", "DEFAULT_CONFIG_URL"]

APP_NAME = "Park Tile Archiver"

#: Where the park configs live when no local checkout is chosen.
DEFAULT_CONFIG_URL = "https://raw.githubusercontent.com/OliveFinch/WDWMap/main"
