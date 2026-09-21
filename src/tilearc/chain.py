"""Archiving every version of a park in one pass, tile by tile.

The ordinary way round is a job per version: fetch all 575,490 WDW tiles for
version 47, then all of them again for 105, and so on for ninety-three
versions. A chain turns that inside out. It takes one coordinate and walks it
through every version oldest first --

    47/19/90412/209771.jpg -> 105/19/90412/209771.jpg -> ... -> 671203034/...

-- storing a tile only where its bytes differ from what an earlier version
already holds, and then moves to the next coordinate.

**It is not fewer requests.** Ninety-three versions of 575,490 tiles is
53,520,570 either way round; transposing a nested loop does not shrink it.
What it buys is one run instead of ninety-three: one button, one resume point,
one progress bar over the whole archive.

The cost of that is worth stating plainly, because it is the one thing the
other order does better: stopping a chain half way leaves *every* version half
done, where stopping a queue of per-version jobs leaves the early versions
finished and the rest untouched.

Two pieces of bookkeeping make it resumable without a database the size of the
archive:

* the **coordinate** is the unit of work. Resume state holds one row per
  coordinate, not per coordinate per version -- 575,490 rows rather than
  53,520,570, which is the difference between a resume set that fits in memory
  and one that does not.
* within a coordinate the **catalogue** is the record. It already knows every
  (version, tile) it holds, and now also every one it has been told is absent,
  so a chain interrupted mid-coordinate picks up where it stopped instead of
  starting that coordinate again.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Mapping

import httpx

from .config import ParkConfig, VersionEntry
from .downloader import Downloader
from .errors import TilearcError
from .library import CATALOGUE_NAME, Catalogue, LibraryWriter
from .manifest import MANIFEST_NAME
from .plan import JobPlan, ZoomPlan
from .state import STATUS_DONE, JobState, _pack, default_state_path
from .urls import TileSource


@dataclass
class ChainPlan:
    """One park, every version, over a set of coordinates.

    The coordinates come from a single :class:`JobPlan` -- the footprint, zoom
    range and modes are decided once and then applied to every version, because
    only one version's footprint has ever been measured. Older maps are smaller
    than newer ones, so a chain reports absences at their edges; that is the
    map's history, not a fault, and the ``absent`` table is where it is kept.
    """

    park: ParkConfig
    versions: list[VersionEntry]
    coordinates: JobPlan
    modes: list[str] = field(default_factory=list)

    @property
    def zooms(self) -> list[ZoomPlan]:
        return self.coordinates.zooms

    @property
    def tiles_per_version(self) -> int:
        return self.coordinates.total_tiles

    @property
    def total_requests(self) -> int:
        return self.coordinates.total_tiles * len(self.versions)

    def iter_coordinates(self) -> Iterator[tuple[int, int, int, str]]:
        return self.coordinates.iter_tiles()

    def fingerprint(self) -> str:
        """Resume state belongs to one chain over one set of versions.

        The version list is part of the identity: adding a newly published
        server changes what "this coordinate is finished" means, and silently
        resuming would leave the new version empty for every coordinate the
        earlier run had already passed.
        """
        import hashlib
        import json

        payload = {
            "chain": self.coordinates.fingerprint(),
            "versions": [v.code for v in self.versions],
        }
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


class ChainWriter:
    """One library tree written across every version of a park at once.

    A :class:`LibraryWriter` is bound to a single version, which is right for a
    job that archives one. A chain touches all of them for every coordinate, so
    this keeps one writer per version behind a shared catalogue and routes each
    tile to the right one. The dedup is the catalogue's as before: a tile whose
    bytes an earlier version already holds is recorded, not written again.
    """

    def __init__(self, root: Path, plan: ChainPlan) -> None:
        self.root = Path(root)
        self.plan = plan
        self.park = plan.park.park_id
        self.catalogue = Catalogue(self.root)
        self._writers: dict[str, LibraryWriter] = {}
        self._labels = {v.code: v.label for v in plan.versions}

    def open(self) -> None:
        # Nothing up front: a version gets its directory and its `versions` row
        # the first time a tile is actually filed under it.
        pass

    def _writer(self, version: str) -> LibraryWriter:
        writer = self._writers.get(version)
        if writer is None:
            entry = VersionEntry(code=version, label=self._labels.get(version))
            plan = JobPlan(
                park=self.plan.park,
                version=entry,
                zooms=self.plan.zooms,
                modes=self.plan.modes,
            )
            writer = LibraryWriter(self.root, plan, catalogue=self.catalogue)
            writer.open()
            self._writers[version] = writer
        return writer

    def settled(self, version: str, z: int, x: int, y: int, mode: str) -> bool:
        return self.catalogue.settled(self.park, version, mode, z, x, y)

    def write(self, version: str, z: int, x: int, y: int, mode: str, data: bytes) -> None:
        self._writer(version).write_tile(z, x, y, mode, data)

    def record_absent(self, version: str, z: int, x: int, y: int, mode: str) -> None:
        self.catalogue.record_absent(self.park, version, mode, z, x, y)

    def finalize(self, manifest: Mapping[str, Any], *, complete: bool) -> Path:
        """Close every version that was touched, each with its own manifest."""
        for version, writer in sorted(self._writers.items()):
            per_version = dict(manifest)
            per_version["park"] = {"id": self.park}
            per_version["version"] = version
            per_version["chain"] = {
                "versions": [v.code for v in self.plan.versions],
                "complete": complete,
            }
            writer.finalize(per_version, complete=complete)
        self.catalogue.commit()
        return self.root

    def abort(self) -> None:
        self.catalogue.commit()

    def close(self) -> None:
        self.catalogue.close()

    @property
    def stored(self) -> int:
        return sum(w.stored for w in self._writers.values())

    @property
    def shared(self) -> int:
        return sum(w.shared for w in self._writers.values())

    @property
    def shared_bytes(self) -> int:
        return sum(w.shared_bytes for w in self._writers.values())


class ChainDownloader(Downloader):
    """A :class:`Downloader` whose unit of work is a coordinate, not a tile.

    Everything about fetching one tile -- retries, backoff, the rate limiter,
    the checks for a server refusing rather than reporting empty space -- is
    inherited unchanged. What differs is only which source a request uses,
    where the bytes go, and when a coordinate counts as finished.
    """

    def __init__(
        self,
        plan: ChainPlan,
        sources: Mapping[tuple[str, str], TileSource],
        writer: ChainWriter,
        state,
        options,
        progress,
        **kwargs,
    ) -> None:
        # The base class wants a plan with `.modes` and a plain source dict for
        # its "everything is missing" hint; give it the coordinate plan and a
        # representative source.
        super().__init__(
            plan.coordinates, {"": next(iter(sources.values()))},
            writer, state, options, progress, **kwargs,
        )
        self.chain = plan
        self.chain_writer = writer
        self._chain_sources = dict(sources)
        #: Tile positions that came back absent from every version of the park.
        self._absent_positions = 0

    # -- what the chain varies ---------------------------------------------

    def _check_all_missing(self) -> None:
        """Not per response. A chain counts whole positions.

        The inherited check aborts once 200 responses in a row are "no tile
        here" with nothing yet downloaded, which is right for a job that asks
        each position once. A chain asks each position of every version, so for
        a park with sixty-seven of them 200 responses is three positions -- and
        three bad coordinates at the start of a footprint is nowhere near
        enough to condemn a month-long job. Counting positions absent from
        *all* versions asks the question that actually matters.
        """
        return

    def _check_absent_positions(self) -> None:
        probe = self.options.chain_absent_probe
        if self._had_success or probe <= 0 or self._absent_positions < probe:
            return

        # Name a URL. Whether these coordinates exist is settled in a browser
        # in ten seconds, and no amount of reasoning here substitutes for it.
        example = ""
        if self._recent_missing:
            z, x, y, mode, version = self._recent_missing[-1]
            example = self._source_for(mode, version).url(z, x, y)

        self._abort(TilearcError(
            f"{self._absent_positions} tile positions in a row were absent from "
            f"all {len(self.chain.versions)} versions, and nothing has "
            f"downloaded. Stopping rather than asking the other "
            f"{self.chain.total_requests:,} times.\n"
            f"  The coordinates are the thing to doubt, not the server: the "
            f"footprint was measured against one version, and a chain applies "
            f"it to every one of them.\n"
            f"  Open this in a browser — if it loads, the tiles are there and "
            f"the fault is here; if it 404s, these coordinates are simply not "
            f"on the server:\n"
            f"    {example}"
        ))

    def _source_for(self, mode: str, version: str) -> TileSource:
        source = self._chain_sources.get((mode, version))
        if source is None:  # pragma: no cover - guarded by the builder
            raise KeyError(f"no tile source for version {version!r} mode {mode!r}")
        return source

    def _store_tile(
        self, z, x, y, mode, version, body, etag, digest, attempts
    ) -> None:
        # No state row here: the coordinate is what resume tracks, and it is
        # not finished until every version of it has been asked for. The
        # counting belongs to the base class -- doing it here as well counts
        # every request twice.
        self.chain_writer.write(version, z, x, y, mode, body)

    def _note_missing(self, z, x, y, mode, version, attempts) -> None:
        self.chain_writer.record_absent(version, z, x, y, mode)

    def _note_failed(self, z, x, y, mode, version, attempts) -> None:
        pass  # the coordinate simply stays pending; see _handle

    def _pending_tiles(self):
        """Coordinates still to do.

        The base class counts one skip per queue item; here a finished
        coordinate stands for a whole chain of requests, and the progress bar
        measures requests.
        """
        done = self.state.completed("")
        span = len(self.chain.versions)
        for z, x, y, mode in self.chain.iter_coordinates():
            if _pack(z, x, y) in done:
                self.result.skipped += span
                self.progress.update(skipped=span)
                continue
            yield z, x, y, mode

    async def _handle(self, client: httpx.AsyncClient, limiter, item) -> None:
        """Walk one coordinate through every version, oldest first."""
        z, x, y, mode = item
        before = self.result.failed
        fetched_before = self.result.fetched
        asked = held = 0
        for entry in self.chain.versions:
            if self._stop.is_set():
                return  # not finished; the coordinate stays pending
            if self.chain_writer.settled(entry.code, z, x, y, mode):
                held += 1
                self.result.skipped += 1
                self.progress.update(skipped=1)
                continue
            asked += 1
            await self._fetch_tile(client, limiter, z, x, y, mode, entry.code)

        # A position nobody has ever had is the chain's version of "everything
        # is missing"; one proves nothing, a run of them does.
        #
        # `held` is why this counts positions rather than responses. Start a
        # chain over a library that already has some of these versions and the
        # versions that *do* have the tile are skipped rather than fetched, so
        # nothing is downloaded and the older ones legitimately 404 -- which
        # looks identical to a job asking for coordinates that do not exist.
        # It is not: the archive already holds this position.
        if asked and not held and self.result.fetched == fetched_before:
            self._absent_positions += 1
            self._check_absent_positions()
        else:
            self._absent_positions = 0

        if self.result.failed == before:
            # Every version of this coordinate is now either held or known
            # absent, so a resume need never look at it again.
            self.state.record(z, x, y, mode, STATUS_DONE, attempts=1)



def build_chain_sources(
    plan: ChainPlan, make_source
) -> dict[tuple[str, str], TileSource]:
    """One source per (mode, version); ``make_source(version, mode)``."""
    modes = plan.modes or [""]
    return {
        (mode, entry.code): make_source(entry, mode)
        for entry in plan.versions
        for mode in modes
    }


def chain_manifest(plan: ChainPlan, writer: ChainWriter, result) -> dict[str, Any]:
    return {
        "tool": "tilearc chain",
        "park": {"id": plan.park.park_id, "label": plan.park.label},
        "versions": [
            {"code": v.code, "label": v.label} for v in plan.versions
        ],
        "coordinatesPerVersion": plan.tiles_per_version,
        "requestsPlanned": plan.total_requests,
        "tilesStored": writer.stored,
        "tilesSharedWithEarlierVersions": writer.shared,
        "bytesSavedBySharing": writer.shared_bytes,
        "fetched": result.fetched,
        "absent": result.missing,
        "failed": result.failed,
        "library": {"root": str(writer.root), "catalogue": CATALOGUE_NAME,
                    "manifest": MANIFEST_NAME},
    }


# ---------------------------------------------------------------------------
# running one
# ---------------------------------------------------------------------------


@dataclass
class ChainRequest:
    plan: ChainPlan
    sources: Mapping[tuple[str, str], TileSource]
    root: Path
    options: Any = None
    state_path: Path | None = None
    restart: bool = False

    def resolved_state_path(self) -> Path:
        if self.state_path:
            return Path(self.state_path)
        # One chain per park, so it can sit at the library root next to the
        # catalogue rather than inside any one version's folder.
        return default_state_path(Path(self.root) / f"{self.plan.park.park_id}.chain")


@dataclass
class ChainOutcome:
    result: Any
    manifest: dict
    root: Path
    state_path: Path
    complete: bool
    stopped_early: bool
    resumed: bool
    coordinates_done: int
    error: BaseException | None = None


async def run_chain(
    request: ChainRequest,
    progress,
    *,
    log=lambda _message: None,
    on_resume=None,
    on_downloader=None,
) -> ChainOutcome:
    """Archive every version of a park, coordinate by coordinate."""
    from .downloader import DownloadOptions
    from .errors import TilearcError

    plan = request.plan
    state_path = request.resolved_state_path()
    options = request.options or DownloadOptions()

    writer = ChainWriter(Path(request.root), plan)
    state = JobState(state_path)

    descriptor = {
        "park": plan.park.park_id,
        "versions": [v.code for v in plan.versions],
        "zooms": [zp.zoom for zp in plan.zooms],
        "modes": plan.modes,
        "kind": "chain",
    }
    resumed = state.bind_job(
        plan.fingerprint(), descriptor, allow_restart=request.restart
    )
    if resumed and on_resume is not None:
        on_resume(state.counts())

    writer.open()
    downloader = ChainDownloader(
        plan, dict(request.sources), writer, state, options, progress, log=log
    )
    if on_downloader is not None:
        on_downloader(downloader)

    error: BaseException | None = None
    try:
        result = await downloader.run()
    except (KeyboardInterrupt, TilearcError) as exc:
        result = downloader.result
        result.interrupted = True
        error = exc

    stopped_early = result.interrupted or error is not None
    complete = not stopped_early and result.failed == 0
    counts = state.counts()
    manifest = chain_manifest(plan, writer, result)
    manifest["coordinatesFinished"] = counts.get("done", 0)
    manifest["complete"] = complete
    manifest["stateDatabase"] = str(state_path)

    nothing_done = not result.fetched and not result.missing
    if error is not None and nothing_done:
        writer.abort()
        writer.close()
        state.close()
        raise error

    writer.finalize(manifest, complete=complete)
    writer.close()
    state.close()

    return ChainOutcome(
        result=result,
        manifest=manifest,
        root=Path(request.root),
        state_path=state_path,
        complete=complete,
        stopped_early=stopped_early,
        resumed=resumed,
        coordinates_done=counts.get("done", 0),
        error=error,
    )
