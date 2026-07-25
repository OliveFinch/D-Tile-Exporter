"""Resumable job state, kept in a small sqlite database.

The DB is the answer to "what has this job already done?", and it is the reason
a re-run costs nothing for work already completed. Two statuses are terminal:

``done``     the tile was fetched and written.
``missing``  the server said there is no tile here (403/404, or 204 via the TDR
             worker). Missing tiles are recorded so a resume does **not** ask
             again -- re-probing tens of thousands of known-absent tiles on
             every run would be exactly the impolite behaviour we are avoiding.

``failed`` is not terminal: those are retried on the next run.

Writes are batched and committed on a timer so an interrupt loses at most a
second of progress, never the database.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Iterable, Mapping

from .errors import JobMismatchError

SCHEMA_VERSION = 1

STATUS_DONE = "done"
STATUS_MISSING = "missing"
STATUS_FAILED = "failed"
TERMINAL = (STATUS_DONE, STATUS_MISSING)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tiles (
    z       INTEGER NOT NULL,
    x       INTEGER NOT NULL,
    y       INTEGER NOT NULL,
    mode    TEXT    NOT NULL DEFAULT '',
    status  TEXT    NOT NULL,
    size    INTEGER NOT NULL DEFAULT 0,
    etag    TEXT,
    sha256  TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    updated REAL    NOT NULL,
    PRIMARY KEY (z, x, y, mode)
);
CREATE INDEX IF NOT EXISTS tiles_status ON tiles (status);
"""


def _pack(z: int, x: int, y: int) -> int:
    """Pack a tile coordinate into one int, so resume sets stay compact.

    29 bits per axis covers z28, far beyond any zoom in these configs.
    """
    return (z << 58) | (x << 29) | y


class JobState:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, isolation_level=None)
        self.conn.execute("PRAGMA journal_mode=WAL")
        # Durability is traded for speed deliberately: losing the last few
        # records to a power cut just means re-fetching a handful of tiles.
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(_SCHEMA)
        self._pending: list[tuple] = []
        self._last_commit = time.monotonic()

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        self.flush()
        self.conn.close()

    def __enter__(self) -> "JobState":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # -- meta --------------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def bind_job(
        self,
        fingerprint: str,
        descriptor: Mapping[str, object],
        *,
        allow_restart: bool = False,
    ) -> bool:
        """Attach this DB to a job. Returns True when resuming existing work.

        Refuses to reuse a DB that belongs to a different job, because mixing
        two jobs' tiles into one state file would make both unreliable.
        """
        existing = self.get_meta("fingerprint")
        if existing and existing != fingerprint:
            if not allow_restart:
                previous = self.get_meta("descriptor") or "{}"
                raise JobMismatchError(
                    f"{self.path} holds state for a different job "
                    f"({previous}). Use --restart to discard it, or point "
                    f"--state-db somewhere else."
                )
            self.conn.execute("DELETE FROM tiles")
            existing = None
        elif existing and allow_restart:
            self.conn.execute("DELETE FROM tiles")
            existing = None

        self.set_meta("fingerprint", fingerprint)
        self.set_meta("schema_version", str(SCHEMA_VERSION))
        self.set_meta("descriptor", json.dumps(descriptor, sort_keys=True))
        self.set_meta("updated", str(time.time()))
        if not self.get_meta("created"):
            self.set_meta("created", str(time.time()))
        return bool(existing)

    # -- tiles -------------------------------------------------------------

    def completed(self, mode: str = "") -> set[int]:
        """Packed coordinates that need no further work for ``mode``."""
        self.flush()  # buffered records must be visible to the query
        rows = self.conn.execute(
            "SELECT z, x, y FROM tiles WHERE mode = ? AND status IN (?, ?)",
            (mode, STATUS_DONE, STATUS_MISSING),
        )
        return {_pack(z, x, y) for z, x, y in rows}

    def record(
        self,
        z: int,
        x: int,
        y: int,
        mode: str,
        status: str,
        *,
        size: int = 0,
        etag: str | None = None,
        sha256: str | None = None,
        attempts: int = 1,
    ) -> None:
        self._pending.append((z, x, y, mode, status, size, etag, sha256, attempts, time.time()))
        if len(self._pending) >= 250 or (time.monotonic() - self._last_commit) > 1.0:
            self.flush()

    def flush(self) -> None:
        if not self._pending:
            return
        self.conn.executemany(
            "INSERT INTO tiles (z, x, y, mode, status, size, etag, sha256, attempts, updated) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(z, x, y, mode) DO UPDATE SET "
            "  status = excluded.status, size = excluded.size, etag = excluded.etag, "
            "  sha256 = excluded.sha256, attempts = tiles.attempts + excluded.attempts, "
            "  updated = excluded.updated",
            self._pending,
        )
        self._pending.clear()
        self._last_commit = time.monotonic()

    # -- reporting ---------------------------------------------------------

    def counts(self) -> dict[str, int]:
        self.flush()
        rows = self.conn.execute("SELECT status, COUNT(*) FROM tiles GROUP BY status")
        return {status: count for status, count in rows}

    def total_bytes(self) -> int:
        self.flush()
        row = self.conn.execute(
            "SELECT COALESCE(SUM(size), 0) FROM tiles WHERE status = ?", (STATUS_DONE,)
        ).fetchone()
        return int(row[0])

    def failures(self, limit: int = 20) -> list[tuple[int, int, int, str, int]]:
        self.flush()
        rows = self.conn.execute(
            "SELECT z, x, y, mode, attempts FROM tiles WHERE status = ? "
            "ORDER BY z, x, y LIMIT ?",
            (STATUS_FAILED, limit),
        )
        return list(rows)

    def iter_done(self, mode: str = "") -> Iterable[tuple[int, int, int, int, str | None]]:
        self.flush()
        yield from self.conn.execute(
            "SELECT z, x, y, size, sha256 FROM tiles WHERE mode = ? AND status = ? "
            "ORDER BY z, x, y",
            (mode, STATUS_DONE),
        )


def default_state_path(output: str | Path) -> Path:
    """State lives beside the output so a job and its resume data travel together."""
    output = Path(output)
    return output.with_name(output.name + ".tilearc-state.sqlite")
