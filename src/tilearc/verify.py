"""Archive integrity checking for all three output formats."""

from __future__ import annotations

import json
import sqlite3
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import VerifyError
from .manifest import MANIFEST_NAME

#: Every JPEG starts with SOI (FFD8) and ends with EOI (FFD9). Cheap, and it
#: catches the failure that matters here: a truncated download.
_JPEG_SOI = b"\xff\xd8"
_JPEG_EOI = b"\xff\xd9"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


@dataclass
class VerifyReport:
    path: Path
    kind: str
    manifest: dict[str, Any] | None = None
    tiles_found: int = 0
    bytes_found: int = 0
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "kind": self.kind,
            "ok": self.ok,
            "tilesFound": self.tiles_found,
            "bytesFound": self.bytes_found,
            "problems": self.problems,
            "warnings": self.warnings,
            "manifest": self.manifest,
        }


def _check_image(name: str, data: bytes, extension: str, problems: list[str]) -> None:
    if not data:
        problems.append(f"{name}: zero-length tile")
        return
    if extension == "jpg":
        if not data.startswith(_JPEG_SOI):
            problems.append(f"{name}: not a JPEG (bad magic)")
        elif not data.rstrip(b"\x00").endswith(_JPEG_EOI):
            problems.append(f"{name}: JPEG is truncated (no end-of-image marker)")
    elif extension == "png" and not data.startswith(_PNG_MAGIC):
        problems.append(f"{name}: not a PNG (bad magic)")


def _compare_with_manifest(report: VerifyReport) -> None:
    manifest = report.manifest
    if not manifest:
        report.problems.append(f"no {MANIFEST_NAME} in archive")
        return
    tiles = manifest.get("tiles") or {}
    expected = tiles.get("fetched")
    if isinstance(expected, int) and expected != report.tiles_found:
        report.problems.append(
            f"manifest records {expected:,} fetched tiles but archive holds "
            f"{report.tiles_found:,}"
        )
    total_bytes = manifest.get("totalBytes")
    if isinstance(total_bytes, int) and total_bytes != report.bytes_found:
        report.warnings.append(
            f"manifest totalBytes is {total_bytes:,} but archive holds "
            f"{report.bytes_found:,}"
        )
    if manifest.get("complete") is False:
        report.warnings.append(
            "manifest is marked incomplete -- the job was interrupted or had failures"
        )
    failed = tiles.get("failed")
    if isinstance(failed, int) and failed:
        report.warnings.append(f"manifest records {failed:,} failed tiles")


# ---------------------------------------------------------------------------
# per-format
# ---------------------------------------------------------------------------


def verify_zip(path: Path, *, deep: bool = True) -> VerifyReport:
    report = VerifyReport(path=path, kind="zip")
    try:
        archive = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise VerifyError(f"{path} is not a readable zip: {exc}") from exc

    with archive:
        broken = archive.testzip()
        if broken is not None:
            report.problems.append(f"CRC mismatch in {broken}")

        manifest_names = [n for n in archive.namelist() if n.endswith(MANIFEST_NAME)]
        if manifest_names:
            try:
                report.manifest = json.loads(archive.read(manifest_names[0]))
            except (json.JSONDecodeError, KeyError) as exc:
                report.problems.append(f"{manifest_names[0]} is not valid JSON: {exc}")

        extension = (report.manifest or {}).get("tileExtension", "jpg")
        for info in archive.infolist():
            if info.is_dir() or info.filename.endswith(MANIFEST_NAME):
                continue
            report.tiles_found += 1
            report.bytes_found += info.file_size
            if info.file_size == 0:
                report.problems.append(f"{info.filename}: zero-length tile")
            elif deep:
                _check_image(info.filename, archive.read(info), extension, report.problems)

    _compare_with_manifest(report)
    return report


def verify_dir(path: Path, *, deep: bool = True) -> VerifyReport:
    report = VerifyReport(path=path, kind="dir")
    manifest_path = path / MANIFEST_NAME
    if manifest_path.is_file():
        try:
            report.manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            report.problems.append(f"{MANIFEST_NAME} is not valid JSON: {exc}")

    extension = (report.manifest or {}).get("tileExtension", "jpg")
    for tile in sorted(path.rglob(f"*.{extension}")):
        report.tiles_found += 1
        size = tile.stat().st_size
        report.bytes_found += size
        name = str(tile.relative_to(path))
        if size == 0:
            report.problems.append(f"{name}: zero-length tile")
        elif deep:
            _check_image(name, tile.read_bytes(), extension, report.problems)

    for leftover in path.rglob("*.part"):
        report.warnings.append(f"leftover partial file: {leftover.relative_to(path)}")

    _compare_with_manifest(report)
    return report


def verify_mbtiles(path: Path, *, deep: bool = True) -> VerifyReport:
    report = VerifyReport(path=path, kind="mbtiles")
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise VerifyError(f"{path} is not a readable sqlite database: {exc}") from exc

    with conn:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()
        if integrity and integrity[0] != "ok":
            report.problems.append(f"sqlite integrity check failed: {integrity[0]}")

        try:
            metadata = dict(conn.execute("SELECT name, value FROM metadata"))
        except sqlite3.Error as exc:
            raise VerifyError(f"{path} has no MBTiles metadata table: {exc}") from exc

        for required in ("name", "format", "minzoom", "maxzoom"):
            if required not in metadata:
                report.problems.append(f"metadata is missing required key '{required}'")

        blob = metadata.get("tilearc:manifest")
        if blob:
            try:
                report.manifest = json.loads(blob)
            except json.JSONDecodeError as exc:
                report.warnings.append(f"embedded manifest is not valid JSON: {exc}")
        else:
            report.warnings.append("no embedded tilearc manifest (not written by this tool?)")

        extension = metadata.get("format", "jpg")
        rows = conn.execute(
            "SELECT zoom_level, tile_column, tile_row, LENGTH(tile_data), tile_data FROM tiles"
            if deep
            else "SELECT zoom_level, tile_column, tile_row, LENGTH(tile_data), NULL FROM tiles"
        )
        for z, x, y, size, data in rows:
            report.tiles_found += 1
            report.bytes_found += size or 0
            name = f"{z}/{x}/{y}"
            if not size:
                report.problems.append(f"{name}: zero-length tile")
            elif deep and data is not None:
                _check_image(name, data, extension, report.problems)

    _compare_with_manifest(report)
    return report


def verify(path: str | Path, *, deep: bool = True) -> VerifyReport:
    path = Path(path)
    if not path.exists():
        raise VerifyError(f"no such archive: {path}")
    if path.is_dir():
        # A `dir` job writes into {output}/{park}_{version}/, so accept either.
        if not (path / MANIFEST_NAME).is_file():
            nested = [c for c in sorted(path.iterdir()) if (c / MANIFEST_NAME).is_file()]
            if len(nested) == 1:
                path = nested[0]
        return verify_dir(path, deep=deep)
    if zipfile.is_zipfile(path):
        return verify_zip(path, deep=deep)
    if path.suffix.lower() in (".mbtiles", ".sqlite", ".db"):
        return verify_mbtiles(path, deep=deep)

    with path.open("rb") as handle:
        magic = handle.read(16)
    if magic.startswith(b"SQLite format 3"):
        return verify_mbtiles(path, deep=deep)
    if magic.startswith(b"PK"):
        # Has the zip signature but no readable central directory -- almost
        # always a download or copy that was cut short.
        raise VerifyError(
            f"{path} is not a readable zip: the file starts with a zip signature "
            f"but has no valid central directory, so it is truncated or corrupt."
        )
    raise VerifyError(f"{path}: unrecognised archive format (expected zip, mbtiles, or directory)")
