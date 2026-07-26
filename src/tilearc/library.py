"""A multi-version tile library: one tree, one catalogue, no duplicate bytes.

Archiving every version of a park separately stores the same tile once per
version. Most tiles do not change between versions -- a new map usually redraws
a corner of one park -- so the great majority of that is copies. WDW alone is
575,490 tiles a version across some ninety versions; at 25 kB a tile that is
1.3 TB of mostly the same JPEG.

So: download oldest first, and store a tile only when its bytes differ from
what an earlier version already holds. A newer version's directory then
contains exactly what changed, which is both the storage saving and, read
directly, the answer to "what did this update actually alter?".

::

    library/
      catalogue.sqlite
      wdw/
        47/        <- oldest version archived: the full map
          11/555/851.jpg
        105/       <- only the tiles that differ from 47
          17/35712/54688.jpg

The catalogue is what makes that tree readable. On disk, a version's folder is
missing most of its tiles; the catalogue says, for every tile of every version,
which folder actually holds the bytes. Without it the tree is not an archive of
ninety versions, it is ninety partial folders and a puzzle.

Deduplication is by content hash, computed on the bytes as they arrive. Not by
timestamp or ETag, which describe the transfer rather than the image: a
re-uploaded but unchanged tile gets a new ETag, and would be stored again.

What this does not do is avoid *fetching* the tile. Knowing the bytes are
unchanged means having the bytes. Saving the request as well needs the origin
to answer a conditional one, which is a separate thing worth trying against a
CDN that supports it -- ``sha256`` is recorded per tile so that day is
straightforward.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from .manifest import MANIFEST_NAME, utcnow
from .plan import JobPlan
from .writers.base import TileWriter

CATALOGUE_NAME = "catalogue.sqlite"


def archive_version(plan: JobPlan, snapshot_date: str | None = None) -> str:
    """What this job's tiles are filed under.

    Normally the version code, because that is what identifies the map. For a
    park that keeps no history it cannot be: DLP serves one live map with no
    selectable servers, so every download of it is the same "current" and would
    overwrite the last. Such a park is filed by the date it was taken, which
    makes repeated downloads a series of dated snapshots -- and, since the
    library only stores what changed, each later one holds exactly what DLP
    altered since the previous.
    """
    if plan.park.supports_history:
        return plan.version.code
    return snapshot_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")


SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- One row per tile per version, including tiles whose bytes live elsewhere.
-- `stored_in` is the version whose directory actually holds the file, which is
-- this version for a tile that changed and an earlier one for a tile that did
-- not. A reader never has to guess: every (park, version, mode, z, x, y) it
-- might ask about has a row, and the row says where to look.
CREATE TABLE IF NOT EXISTS tiles (
    park       TEXT    NOT NULL,
    version    TEXT    NOT NULL,
    mode       TEXT    NOT NULL,   -- '' where the park has no modes
    z          INTEGER NOT NULL,
    x          INTEGER NOT NULL,
    y          INTEGER NOT NULL,
    sha256     TEXT    NOT NULL,
    bytes      INTEGER NOT NULL,
    stored_in  TEXT    NOT NULL,
    relpath    TEXT    NOT NULL,   -- from the library root
    fetched_at TEXT    NOT NULL,
    PRIMARY KEY (park, version, mode, z, x, y)
) WITHOUT ROWID;

-- The dedup lookup: has any earlier version of this park held these exact
-- bytes for this exact tile position?
CREATE INDEX IF NOT EXISTS tiles_by_content
    ON tiles (park, mode, z, x, y, sha256);

-- Which versions of a park have been archived, and how completely.
CREATE TABLE IF NOT EXISTS versions (
    park        TEXT NOT NULL,
    version     TEXT NOT NULL,
    label       TEXT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    complete    INTEGER NOT NULL DEFAULT 0,
    manifest    TEXT,
    PRIMARY KEY (park, version)
);
"""


@dataclass(frozen=True)
class TileRecord:
    park: str
    version: str
    mode: str
    z: int
    x: int
    y: int
    sha256: str
    bytes: int
    stored_in: str
    relpath: str

    @property
    def shared(self) -> bool:
        """True when the bytes came from an earlier version."""
        return self.stored_in != self.version


class Catalogue:
    """The index over a library tree."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.path = self.root / CATALOGUE_NAME
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        # A tile archive is written once and read many times, in long runs that
        # can be interrupted. WAL survives that better than the default journal
        # and lets a reader look at the catalogue while a download continues.
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(_SCHEMA)
        self.db.execute(
            "INSERT OR IGNORE INTO meta (key, value) VALUES ('schemaVersion', ?)",
            (str(SCHEMA_VERSION),),
        )
        self.db.commit()

    # -- writing -----------------------------------------------------------

    def begin_version(self, park: str, version: str, label: str | None) -> None:
        self.db.execute(
            "INSERT INTO versions (park, version, label, started_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (park, version) DO UPDATE SET started_at = excluded.started_at",
            (park, version, label, utcnow()),
        )
        self.db.commit()

    def finish_version(self, park: str, version: str, manifest: dict, complete: bool) -> None:
        self.db.execute(
            "UPDATE versions SET finished_at = ?, complete = ?, manifest = ? "
            "WHERE park = ? AND version = ?",
            (utcnow(), 1 if complete else 0, json.dumps(manifest, default=str), park, version),
        )
        self.db.commit()

    def find_existing(
        self, park: str, mode: str, z: int, x: int, y: int, sha256: str
    ) -> sqlite3.Row | None:
        """An earlier row holding these exact bytes for this exact tile."""
        return self.db.execute(
            "SELECT version, stored_in, relpath FROM tiles "
            "WHERE park = ? AND mode = ? AND z = ? AND x = ? AND y = ? AND sha256 = ? "
            "ORDER BY stored_in LIMIT 1",
            (park, mode, z, x, y, sha256),
        ).fetchone()

    def record(self, record: TileRecord) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO tiles "
            "(park, version, mode, z, x, y, sha256, bytes, stored_in, relpath, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (record.park, record.version, record.mode, record.z, record.x, record.y,
             record.sha256, record.bytes, record.stored_in, record.relpath, utcnow()),
        )

    def commit(self) -> None:
        self.db.commit()

    # -- reading -----------------------------------------------------------

    def has(self, park: str, version: str, mode: str, z: int, x: int, y: int) -> bool:
        return self.db.execute(
            "SELECT 1 FROM tiles WHERE park=? AND version=? AND mode=? AND z=? AND x=? AND y=?",
            (park, version, mode, z, x, y),
        ).fetchone() is not None

    def resolve(
        self, park: str, version: str, z: int, x: int, y: int, mode: str = ""
    ) -> Path | None:
        """Where a given tile's bytes actually live, or None if not archived."""
        row = self.db.execute(
            "SELECT relpath FROM tiles "
            "WHERE park=? AND version=? AND mode=? AND z=? AND x=? AND y=?",
            (park, version, mode, z, x, y),
        ).fetchone()
        return self.root / row["relpath"] if row else None

    def versions(self, park: str | None = None) -> list[sqlite3.Row]:
        if park:
            return list(self.db.execute(
                "SELECT * FROM versions WHERE park = ? ORDER BY started_at", (park,)))
        return list(self.db.execute("SELECT * FROM versions ORDER BY park, started_at"))

    def stats(self) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT park, version,"
            "       COUNT(*) AS tiles,"
            "       SUM(CASE WHEN stored_in = version THEN 1 ELSE 0 END) AS stored,"
            "       SUM(CASE WHEN stored_in = version THEN bytes ELSE 0 END) AS stored_bytes,"
            "       SUM(bytes) AS logical_bytes "
            "FROM tiles GROUP BY park, version ORDER BY park, version"
        )
        return [dict(row) for row in rows]

    def close(self) -> None:
        self.db.commit()
        self.db.close()

    # -- handing the tree to something else --------------------------------

    def export_index(self, park: str | None = None) -> dict[str, Any]:
        """A JSON view of where everything is, for a reader that is not SQLite.

        Grouped by park, version and zoom, with tiles as ``[x, y, relpath]``.
        Fine for one park's worth; the catalogue itself is the right thing to
        query for a whole library.
        """
        where, params = ("WHERE park = ?", (park,)) if park else ("", ())
        out: dict[str, Any] = {"root": str(self.root), "generated": utcnow(), "parks": {}}
        for row in self.db.execute(
            f"SELECT park, version, mode, z, x, y, relpath FROM tiles {where} "
            "ORDER BY park, version, mode, z, x, y", params
        ):
            park_entry = out["parks"].setdefault(row["park"], {})
            version_entry = park_entry.setdefault(row["version"], {})
            mode_entry = version_entry.setdefault(row["mode"] or "default", {})
            mode_entry.setdefault(str(row["z"]), []).append(
                [row["x"], row["y"], row["relpath"]]
            )
        return out


class LibraryWriter(TileWriter):
    """Writes into a shared library tree, storing only what is new.

    Layout is ``{root}/{park}/{version}/{z}/{x}/{y}.jpg`` -- the park, then the
    server id, then the tile path as the server lays it out.
    """

    #: The catalogue knows what is held, including tiles stored under another
    #: version, which the filesystem alone cannot tell you.
    verifies_existing = True

    def __init__(
        self,
        output: Path,
        plan: JobPlan,
        catalogue: Catalogue | None = None,
        snapshot_date: str | None = None,
    ) -> None:
        super().__init__(output, plan)
        # `output` is the library root, shared by every park and version.
        self.catalogue = catalogue or Catalogue(self.output)
        self.park = plan.park.park_id
        self.version = archive_version(plan, snapshot_date)
        self.root = self.catalogue.root / self.park / self.version
        self._made: set[Path] = set()
        self._pending = 0
        self.stored = 0
        self.shared = 0
        self.shared_bytes = 0

    def open(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.catalogue.begin_version(self.park, self.version, self.plan.version.label)

    def _relpath(self, z: int, x: int, y: int, mode: str, version: str) -> str:
        return f"{self.park}/{version}/{self.tile_relpath(z, x, y, mode)}"

    def write_tile(self, z: int, x: int, y: int, mode: str, data: bytes) -> None:
        digest = hashlib.sha256(data).hexdigest()
        existing = self.catalogue.find_existing(self.park, mode, z, x, y, digest)

        if existing is not None:
            # Identical bytes are already on disk under an earlier version. Point
            # at them rather than writing a second copy -- that is the whole
            # point of the tree, and it is also why this version's folder is
            # allowed to look sparse.
            self.catalogue.record(TileRecord(
                self.park, self.version, mode, z, x, y, digest, len(data),
                existing["stored_in"], existing["relpath"],
            ))
            self.shared += 1
            self.shared_bytes += len(data)
        else:
            relpath = self._relpath(z, x, y, mode, self.version)
            path = self.catalogue.root / relpath
            parent = path.parent
            if parent not in self._made:
                parent.mkdir(parents=True, exist_ok=True)
                self._made.add(parent)
            # Written via a temporary and renamed, so an interrupt can never
            # leave a half-written JPEG that a later run treats as valid.
            tmp = path.with_name(path.name + ".part")
            tmp.write_bytes(data)
            tmp.replace(path)
            self.catalogue.record(TileRecord(
                self.park, self.version, mode, z, x, y, digest, len(data),
                self.version, relpath,
            ))
            self.stored += 1

        # Batched, because committing per tile turns a download into a
        # fsync benchmark.
        self._pending += 1
        if self._pending >= 500:
            self.catalogue.commit()
            self._pending = 0

    def has_tile(self, z: int, x: int, y: int, mode: str) -> bool:
        return self.catalogue.has(self.park, self.version, mode, z, x, y)

    def finalize(self, manifest: dict[str, Any], *, complete: bool = True) -> Path:
        from .writers import dump_manifest

        self.catalogue.commit()
        self.root.mkdir(parents=True, exist_ok=True)
        manifest = dict(manifest)
        manifest["library"] = {
            "root": str(self.catalogue.root),
            "catalogue": CATALOGUE_NAME,
            "tilesStored": self.stored,
            "tilesSharedWithEarlierVersions": self.shared,
            "bytesSavedBySharing": self.shared_bytes,
        }
        (self.root / MANIFEST_NAME).write_bytes(dump_manifest(manifest))
        self.catalogue.finish_version(self.park, self.version, manifest, complete)
        return self.root

    def abort(self) -> None:
        self.catalogue.commit()


def iter_tiles(catalogue: Catalogue, park: str, version: str) -> Iterator[TileRecord]:
    for row in catalogue.db.execute(
        "SELECT * FROM tiles WHERE park = ? AND version = ? ORDER BY z, x, y",
        (park, version),
    ):
        yield TileRecord(
            row["park"], row["version"], row["mode"], row["z"], row["x"], row["y"],
            row["sha256"], row["bytes"], row["stored_in"], row["relpath"],
        )


def human_saving(stats: Iterable[dict[str, Any]]) -> dict[str, int]:
    total_logical = sum(row["logical_bytes"] or 0 for row in stats)
    total_stored = sum(row["stored_bytes"] or 0 for row in stats)
    return {
        "logicalBytes": total_logical,
        "storedBytes": total_stored,
        "savedBytes": total_logical - total_stored,
    }
