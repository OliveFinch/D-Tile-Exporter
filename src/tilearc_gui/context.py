"""Shared park-data source, plus park discovery."""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from tilearc.config import ParkRepository, build_repository

from . import DEFAULT_CONFIG_URL

#: Listing the `parks/` directory is a GitHub-specific trick; a plain static
#: host cannot be enumerated. Used only to *discover* park IDs -- every actual
#: park detail still comes from the config files themselves.
GITHUB_PARKS_API = "https://api.github.com/repos/OliveFinch/WDWMap/contents/parks"

#: Last resort if the listing is unavailable (the API allows 60 unauthenticated
#: requests an hour).
FALLBACK_PARK_IDS = ("wdw", "dlr", "hkdl", "shdr", "dlp", "tdr")


class AppContext(QObject):
    """Where park configs are read from, shared by every tab."""

    changed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._config_url: str | None = DEFAULT_CONFIG_URL
        self._config_dir: Path | None = None
        self._repository: ParkRepository | None = None

    # -- source ------------------------------------------------------------

    @property
    def is_local(self) -> bool:
        return self._config_dir is not None

    @property
    def description(self) -> str:
        if self._config_dir is not None:
            return str(self._config_dir)
        return self._config_url or ""

    def use_live(self, url: str = DEFAULT_CONFIG_URL) -> None:
        self._config_url = url
        self._config_dir = None
        self._repository = None
        self.changed.emit()

    def use_directory(self, path: Path) -> None:
        self._config_dir = Path(path)
        self._config_url = None
        self._repository = None
        self.changed.emit()

    # -- access ------------------------------------------------------------

    def repository(self) -> ParkRepository:
        if self._repository is None:
            self._repository = build_repository(
                config_dir=self._config_dir, config_url=self._config_url
            )
        return self._repository

    def refresh(self) -> None:
        self._repository = None
        self.changed.emit()

    # -- discovery ---------------------------------------------------------

    def park_ids(self) -> list[str]:
        """Every park ID the source offers. Blocking -- call off the UI thread."""
        repository = self.repository()
        try:
            found = repository.list_parks()
            if found:
                return found
        except Exception:
            pass  # an HTTP source cannot be listed; fall through

        try:
            request = urllib.request.Request(
                GITHUB_PARKS_API,
                headers={"Accept": "application/vnd.github+json", "User-Agent": "tilearc-gui"},
            )
            with urllib.request.urlopen(request, timeout=20) as response:
                entries = json.load(response)
            ids = sorted(e["name"] for e in entries if e.get("type") == "dir")
            if ids:
                return ids
        except Exception:
            pass

        return list(FALLBACK_PARK_IDS)

    def downloadable_park_ids(self) -> list[tuple[str, str]]:
        """``(id, label)`` for parks this app can actually fetch.

        Parks with no tile template need credentials and a proxy -- only Tokyo
        Disney Resort, which this front-end does not handle. Filtering on the
        missing template rather than on the name keeps the rule honest.
        """
        repository = self.repository()
        usable: list[tuple[str, str]] = []
        for park_id in self.park_ids():
            try:
                config = repository.park(park_id)
            except Exception:
                continue
            if config.tile_template or any(v.url for v in repository.versions(park_id)):
                usable.append((park_id, config.label))
        return usable

    def all_parks(self) -> list[tuple[str, str]]:
        """``(id, label)`` for every readable park, including credentialed ones."""
        repository = self.repository()
        parks: list[tuple[str, str]] = []
        for park_id in self.park_ids():
            try:
                parks.append((park_id, repository.park(park_id).label))
            except Exception:
                continue
        return parks
