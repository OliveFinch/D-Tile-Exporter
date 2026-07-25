"""Running a download job, independent of any user interface.

The CLI and the GUI both go through :func:`run_job`, so the fiddly parts stay
in one place -- in particular the rule that an unfinished job must never be
packed into a zip, because the state DB would then mark those tiles done and
the missing ones could never be added on a later run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping

from .downloader import Downloader, DownloadOptions, DownloadResult
from .errors import TilearcError
from .manifest import build_manifest, utcnow
from .plan import JobPlan
from .progress import Progress
from .state import JobState, default_state_path
from .urls import TileSource
from .writers import build_writer, default_output


@dataclass
class JobRequest:
    plan: JobPlan
    sources: Mapping[str, TileSource]
    fmt: str = "zip"
    output: Path | None = None
    state_path: Path | None = None
    options: DownloadOptions = field(default_factory=DownloadOptions)
    restart: bool = False

    def resolved_output(self) -> Path:
        return Path(self.output) if self.output else default_output(self.plan, self.fmt)

    def resolved_state_path(self) -> Path:
        if self.state_path:
            return Path(self.state_path)
        return default_state_path(self.resolved_output())


@dataclass
class JobOutcome:
    result: DownloadResult
    counts: dict[str, int]
    manifest: dict
    artefact: Path | None
    state_path: Path
    complete: bool
    stopped_early: bool
    resumed: bool
    error: BaseException | None = None

    @property
    def failed(self) -> int:
        return self.counts.get("failed", 0)

    @property
    def downloaded(self) -> int:
        return self.counts.get("done", 0)

    @property
    def missing(self) -> int:
        return self.counts.get("missing", 0)


async def run_job(
    request: JobRequest,
    progress: Progress,
    *,
    log: Callable[[str], None] = lambda _message: None,
    on_resume: Callable[[dict[str, int]], None] | None = None,
    on_downloader: Callable[[Downloader], None] | None = None,
) -> JobOutcome:
    """Download a plan and finalise the output.

    ``on_downloader`` receives the live :class:`Downloader` so a caller can
    keep a handle on it -- the GUI uses this to wire up its Stop button.
    """
    plan = request.plan
    output = request.resolved_output()
    state_path = request.resolved_state_path()
    started_at = utcnow()

    writer = build_writer(request.fmt, output, plan)
    state = JobState(state_path)

    descriptor = {
        "park": plan.park.park_id,
        "version": plan.version.code,
        "zooms": [zp.zoom for zp in plan.zooms],
        "modes": plan.modes,
    }
    resumed = state.bind_job(
        plan.fingerprint(), descriptor, allow_restart=request.restart
    )
    if resumed and on_resume is not None:
        on_resume(state.counts())

    writer.open()
    downloader = Downloader(
        plan, dict(request.sources), writer, state, request.options, progress, log=log
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

    # Ctrl-C (and the GUI's Stop) set `interrupted` without raising, so both
    # paths have to be checked.
    stopped_early = result.interrupted or error is not None
    complete = not stopped_early and result.failed == 0

    counts = state.counts()
    templates = {mode: source.template for mode, source in request.sources.items()}

    # A manifest is written even for a partial run: an archive that documents
    # what it contains is far more useful than a silent one.
    manifest = build_manifest(
        plan,
        started_at=started_at,
        fetched=counts.get("done", 0),
        missing=counts.get("missing", 0),
        failed=counts.get("failed", 0),
        total_bytes=state.total_bytes(),
        tile_urls=templates,
        complete=complete,
        extra={"stateDatabase": str(state_path)},
    )

    nothing_done = not counts.get("done") and not counts.get("missing")
    if error is not None and nothing_done:
        writer.abort()
        state.close()
        raise error

    artefact = writer.finalize(manifest, complete=complete)
    state.close()

    return JobOutcome(
        result=result,
        counts=counts,
        manifest=manifest,
        artefact=artefact,
        state_path=state_path,
        complete=complete,
        stopped_early=stopped_early,
        resumed=resumed,
        error=error,
    )
