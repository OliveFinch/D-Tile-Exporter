"""Command-line interface."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence

from . import USER_AGENT, __version__
from .bounds import BBox
from .config import ParkRepository, build_repository
from .doctor import check_park, worst_severity
from .downloader import CONCURRENCY_WARN_THRESHOLD, Downloader, DownloadOptions
from .errors import ConfigError, QuotaError, TilearcError
from .manifest import build_manifest, utcnow
from .plan import DEFAULT_BYTES_PER_TILE, JobPlan, build_plan
from .progress import Progress
from .state import JobState, default_state_path
from .tdr import build_tdr_source, check_worker_quota, load_credentials, normalise_modes
from .urls import build_source
from .util import human_bytes, sanitize_component
from .verify import verify as verify_archive
from .writers import FORMATS, build_writer, default_output

DEFAULT_MAX_TILES = 250_000


# ---------------------------------------------------------------------------
# output helpers
# ---------------------------------------------------------------------------


def info(message: str) -> None:
    print(message, file=sys.stderr)


def warn(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr)


def emit_json(payload: Any) -> None:
    json.dump(payload, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")


# ---------------------------------------------------------------------------
# shared argument groups
# ---------------------------------------------------------------------------


def add_source_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("park data source")
    group.add_argument(
        "--config-dir",
        default=os.environ.get("TILEARC_CONFIG_DIR"),
        help="path to a checkout of the viewer repo (or its parks/ directory). "
        "Env: TILEARC_CONFIG_DIR",
    )
    group.add_argument(
        "--config-url",
        default=os.environ.get("TILEARC_CONFIG_URL"),
        help="base URL of the live site to read park configs from. "
        "Env: TILEARC_CONFIG_URL",
    )


def add_selection_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("tile selection")
    group.add_argument("--min-zoom", type=int, help="lowest zoom to include")
    group.add_argument("--max-zoom", type=int, help="highest zoom to include")
    group.add_argument(
        "--all-zooms",
        action="store_true",
        help="use the park's full zoom range (the default when no zooms are given)",
    )
    group.add_argument(
        "--bbox",
        help="clip to a geographic box: minLon,minLat,maxLon,maxLat",
    )
    group.add_argument(
        "--allow-tms-bbox",
        action="store_true",
        help="permit --bbox on a yScheme:tms park whose grid is not web mercator",
    )
    group.add_argument(
        "--mode",
        default="daytime",
        help="TDR only: daytime, nighttime, or both",
    )


def repository_from(args: argparse.Namespace) -> ParkRepository:
    return build_repository(config_dir=args.config_dir, config_url=args.config_url)


def plan_from(args: argparse.Namespace, repo: ParkRepository) -> JobPlan:
    park = repo.park(args.park)
    version = repo.version(args.park, args.version)

    min_zoom = None if args.all_zooms else args.min_zoom
    max_zoom = None if args.all_zooms else args.max_zoom

    bbox = None
    if args.bbox:
        try:
            bbox = BBox.parse(args.bbox)
        except ValueError as exc:
            raise TilearcError(str(exc)) from exc

    modes = normalise_modes(args.mode) if park.requires_credentials else []

    return build_plan(
        park,
        version,
        min_zoom=min_zoom,
        max_zoom=max_zoom,
        bbox=bbox,
        modes=modes,
        allow_tms_bbox=getattr(args, "allow_tms_bbox", False),
    )


def print_plan(plan: JobPlan, bytes_per_tile: int) -> None:
    park, version = plan.park, plan.version
    info(f"park     {park.park_id}  {park.label}")
    label = f"  ({version.label})" if version.label else ""
    status = "" if version.active else "  [inactive in version list]"
    info(f"version  {version.code}{label}{status}")
    info(f"yScheme  {park.y_scheme}")
    if plan.modes:
        info(f"modes    {', '.join(plan.modes)}")
    if plan.bbox:
        info(f"bbox     {', '.join(f'{v:g}' for v in plan.bbox.as_list())}")

    if not plan.zooms:
        info("\nNo zoom levels to download.")
        for note in plan.notes:
            info(f"  note: {note}")
        return

    info("")
    info(f"{'zoom':>5}  {'x range':>19}  {'y range':>19}  {'grid':>13}  {'tiles':>12}")
    for zoom, bounds, count in plan.summary_rows():
        info(
            f"{zoom:>5}  {bounds.min_x:>8}-{bounds.max_x:<10}  "
            f"{bounds.min_y:>8}-{bounds.max_y:<10}  "
            f"{bounds.width:>5} x {bounds.height:<5}  {count:>12,}"
        )

    per_mode = plan.tiles_per_mode
    info("")
    if len(plan.modes) > 1:
        info(f"tiles per mode  {per_mode:,}")
    info(f"total tiles     {plan.total_tiles:,}")
    info(
        f"projected size  {human_bytes(plan.estimated_bytes(bytes_per_tile))} "
        f"(at {bytes_per_tile:,} bytes/tile)"
    )
    for note in plan.notes:
        info(f"note: {note}")


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def cmd_versions(args: argparse.Namespace) -> int:
    repo = repository_from(args)
    entries = repo.versions(args.park)
    if not args.all:
        entries = [e for e in entries if e.active]

    if args.json:
        emit_json(
            [
                {
                    "code": e.code,
                    "label": e.label,
                    "active": e.active,
                    "url": e.url,
                    "safeName": sanitize_component(e.code),
                }
                for e in entries
            ]
        )
        return 0

    park = repo.park(args.park)
    info(f"{park.label} ({park.park_id}) -- {len(entries)} version(s) from {repo.describe()}")
    info("")
    print(f"{'code':<18} {'label':<20} {'active':<7} template")
    for entry in entries:
        template = entry.url if entry.url else "(park default)"
        print(
            f"{entry.code:<18} {(entry.label or ''):<20} "
            f"{('yes' if entry.active else 'no'):<7} {template}"
        )
    return 0


def cmd_estimate(args: argparse.Namespace) -> int:
    repo = repository_from(args)
    plan = plan_from(args, repo)
    bytes_per_tile = args.bytes_per_tile

    if args.json:
        emit_json(
            {
                "park": plan.park.park_id,
                "version": plan.version.code,
                "modes": plan.modes or None,
                "zooms": {
                    str(z): {"bounds": b.as_dict(), "tiles": c}
                    for z, b, c in plan.summary_rows()
                },
                "tilesPerMode": plan.tiles_per_mode,
                "totalTiles": plan.total_tiles,
                "bytesPerTile": bytes_per_tile,
                "estimatedBytes": plan.estimated_bytes(bytes_per_tile),
                "notes": plan.notes,
                "fingerprint": plan.fingerprint(),
            }
        )
        return 0

    print_plan(plan, bytes_per_tile)

    findings = [f for f in check_park(plan.park) if f.severity == "error"]
    relevant = [f for f in findings if f.zoom in {zp.zoom for zp in plan.zooms}]
    if relevant:
        info("")
        warn(
            f"{len(relevant)} bounds problem(s) affect the zooms in this job; "
            f"run `tilearc doctor --park {plan.park.park_id}` for details"
        )
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    repo = repository_from(args)
    park_ids = [args.park] if args.park else repo.list_parks()

    findings = []
    for park_id in park_ids:
        findings.extend(check_park(repo.park(park_id)))

    if args.json:
        emit_json([f.as_dict() for f in findings])
    else:
        if not findings:
            info(f"no bounds problems found in {len(park_ids)} park(s)")
        else:
            current = None
            for finding in findings:
                if finding.park != current:
                    current = finding.park
                    print(f"\n{current}")
                zoom = f"z{finding.zoom}" if finding.zoom is not None else "-"
                print(f"  [{finding.severity:<7}] {zoom:<5} {finding.rule:<10} {finding.message}")
            print("")
            info(
                f"{len(findings)} finding(s). These are reported, not corrected -- "
                f"edit the source configs if you agree with them."
            )

    return 1 if worst_severity(findings) == "error" and args.strict else 0


def cmd_verify(args: argparse.Namespace) -> int:
    report = verify_archive(args.path, deep=not args.quick)
    if args.json:
        emit_json(report.as_dict())
        return 0 if report.ok else 1

    info(f"{report.path}  ({report.kind})")
    info(f"  tiles   {report.tiles_found:,}")
    info(f"  bytes   {human_bytes(report.bytes_found)}")
    if report.manifest:
        tiles = report.manifest.get("tiles", {})

        def count(key: str) -> str:
            value = tiles.get(key)
            return f"{value:,}" if isinstance(value, int) else "?"

        info(
            f"  manifest: requested {count('requested')} / fetched {count('fetched')} / "
            f"missing {count('missing')} / failed {count('failed')}"
        )
    for message in report.warnings:
        warn(message)
    for message in report.problems[:50]:
        print(f"  PROBLEM: {message}", file=sys.stderr)
    if len(report.problems) > 50:
        info(f"  ... and {len(report.problems) - 50} more problem(s)")

    info("  OK" if report.ok else f"  FAILED ({len(report.problems)} problem(s))")
    return 0 if report.ok else 1


def _confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        raise TilearcError(
            "refusing to start a download without confirmation on a "
            "non-interactive terminal; pass --yes if that is what you want"
        )
    try:
        answer = input(f"{prompt} [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in ("y", "yes")


def cmd_download(args: argparse.Namespace) -> int:
    repo = repository_from(args)
    plan = plan_from(args, repo)
    park = plan.park

    if not plan.zooms:
        raise TilearcError(
            "nothing to download: no zoom level has usable bounds. "
            + " ".join(plan.notes)
        )

    # -- build the tile source(s) ----------------------------------------
    sources: dict[str, str] = {}
    if park.requires_credentials:
        credentials = load_credentials(park, args.tdr_credentials, warn=warn)
        expiry = credentials.expiry_warning()
        if expiry:
            warn(expiry)
        tile_sources = {
            mode: build_tdr_source(
                park, plan.version.code, mode, credentials, direct=args.direct
            )
            for mode in plan.modes
        }
    else:
        tile_sources = {"": build_source(park, plan.version)}

    sources = {mode: source.template for mode, source in tile_sources.items()}

    # -- report and gate --------------------------------------------------
    print_plan(plan, args.bytes_per_tile)
    info("")
    for mode, template in sources.items():
        prefix = f"{mode}: " if mode else ""
        info(f"url      {prefix}{template.replace('{z}', 'Z').replace('{x}', 'X').replace('{y}', 'Y')}")

    warnings: list[str] = []
    if any(s.uses_shared_proxy for s in tile_sources.values()):
        warnings.extend(
            check_worker_quota(plan.total_tiles, forced=args.force, cap=args.worker_max_tiles)
        )

    if plan.total_tiles > args.max_tiles and not args.force:
        raise QuotaError(
            f"{plan.total_tiles:,} tiles exceeds --max-tiles ({args.max_tiles:,}). "
            f"Narrow the job with --max-zoom/--bbox, raise the cap, or pass --force."
        )

    if args.concurrency > CONCURRENCY_WARN_THRESHOLD:
        warnings.append(
            f"--concurrency {args.concurrency} is high for someone else's production "
            f"CDN. Please keep it near the default of 5 unless you have a reason."
        )

    for message in warnings:
        warn(message)

    fmt = args.format
    output = Path(args.output) if args.output else default_output(plan, fmt)
    state_path = Path(args.state_db) if args.state_db else default_state_path(output)

    info("")
    info(f"format   {fmt} -> {output}")
    info(f"state    {state_path}")
    info(
        f"polite   concurrency {args.concurrency}, "
        f"{'no rate cap' if args.rps <= 0 else f'{args.rps:g} req/s'}, "
        f"{args.retries} retries"
    )
    info(f"agent    {USER_AGENT}")
    info("")

    if args.dry_run:
        info("dry run -- stopping before any tile is fetched")
        return 0

    if not _confirm(
        f"Download {plan.total_tiles:,} tiles "
        f"(~{human_bytes(plan.estimated_bytes(args.bytes_per_tile))})?",
        args.yes,
    ):
        info("aborted")
        return 1

    # -- run ---------------------------------------------------------------
    started_at = utcnow()
    options = DownloadOptions(
        concurrency=args.concurrency,
        rps=args.rps,
        retries=args.retries,
        timeout=args.timeout,
    )

    writer = build_writer(fmt, output, plan)
    state = JobState(state_path)
    descriptor = {
        "park": park.park_id,
        "version": plan.version.code,
        "zooms": [zp.zoom for zp in plan.zooms],
        "modes": plan.modes,
    }
    resumed = state.bind_job(plan.fingerprint(), descriptor, allow_restart=args.restart)
    if resumed:
        counts = state.counts()
        info(
            f"resuming: {counts.get('done', 0):,} already downloaded, "
            f"{counts.get('missing', 0):,} known missing, "
            f"{counts.get('failed', 0):,} to retry"
        )

    progress = Progress(plan.total_tiles, enabled=not args.no_progress)
    writer.open()
    downloader = Downloader(
        plan, tile_sources, writer, state, options, progress,
        log=(lambda m: None) if args.no_progress else _progress_safe_log(progress),
    )

    error: BaseException | None = None
    try:
        result = asyncio.run(downloader.run())
    except (KeyboardInterrupt, TilearcError) as exc:
        result = downloader.result
        result.interrupted = True
        error = exc
    finally:
        progress.close()

    # Ctrl-C sets `interrupted` without raising, so both paths must be checked.
    stopped_early = result.interrupted or error is not None
    complete = not stopped_early and result.failed == 0

    counts = state.counts()

    # A manifest is written even for a partial run: an archive that documents
    # what it contains is far more useful than a silent one.
    manifest = build_manifest(
        plan,
        started_at=started_at,
        fetched=counts.get("done", 0),
        missing=counts.get("missing", 0),
        failed=counts.get("failed", 0),
        total_bytes=state.total_bytes(),
        tile_urls=sources,
        complete=complete,
        extra={"stateDatabase": str(state_path)},
    )

    nothing_done = not counts.get("done") and not counts.get("missing")
    if error is not None and nothing_done:
        writer.abort()
        state.close()
        raise error if isinstance(error, TilearcError) else TilearcError(str(error))

    artefact = writer.finalize(manifest, complete=complete)
    state.close()

    info("")
    info(progress.summary())

    if stopped_early:
        if fmt == "zip":
            info(f"staged   {artefact} (not packed -- the job is unfinished)")
        else:
            info(f"wrote    {artefact}")
        info(f"state    {state_path}")
        info("")
        if error is not None:
            warn(f"job did not complete: {error}")
        else:
            warn("job was interrupted")
        info("re-run the same command to continue where it stopped")
        if error is not None:
            return getattr(error, "exit_code", 1)
        return 130

    info(f"wrote    {artefact}")

    if counts.get("failed"):
        warn(
            f"{counts['failed']:,} tile(s) failed after retries; re-run the same "
            f"command to retry just those"
        )
        return 1

    info(f"archive complete ({counts.get('done', 0):,} tiles, "
         f"{counts.get('missing', 0):,} with no coverage)")
    return 0


def _progress_safe_log(progress: Progress):
    def log(message: str) -> None:
        if progress.enabled:
            sys.stderr.write("\r" + " " * 100 + "\r")
        print(message, file=sys.stderr)

    return log


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tilearc",
        description="Archive historical Disney park map tiles, politely.",
    )
    parser.add_argument("--version", action="version", version=f"tilearc {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # versions ------------------------------------------------------------
    versions = subparsers.add_parser("versions", help="list known versions for a park")
    versions.add_argument("--park", required=True)
    versions.add_argument("--all", action="store_true", help="include inactive versions")
    versions.add_argument("--json", action="store_true")
    add_source_args(versions)
    versions.set_defaults(func=cmd_versions)

    # estimate ------------------------------------------------------------
    estimate = subparsers.add_parser(
        "estimate", help="count tiles and project size without fetching anything"
    )
    estimate.add_argument("--park", required=True)
    estimate.add_argument("--version", required=True)
    estimate.add_argument(
        "--bytes-per-tile", type=int, default=DEFAULT_BYTES_PER_TILE,
        help=f"mean tile size for the projection (default {DEFAULT_BYTES_PER_TILE:,})",
    )
    estimate.add_argument("--json", action="store_true")
    add_selection_args(estimate)
    add_source_args(estimate)
    estimate.set_defaults(func=cmd_estimate)

    # doctor --------------------------------------------------------------
    doctor = subparsers.add_parser("doctor", help="report suspicious boundsByZoom data")
    doctor.add_argument("--park", help="check one park (default: all parks found)")
    doctor.add_argument("--json", action="store_true")
    doctor.add_argument(
        "--strict", action="store_true", help="exit non-zero when errors are found"
    )
    add_source_args(doctor)
    doctor.set_defaults(func=cmd_doctor)

    # download ------------------------------------------------------------
    download = subparsers.add_parser("download", help="archive a version's tiles")
    download.add_argument("--park", required=True)
    download.add_argument("--version", required=True)
    download.add_argument("--format", choices=FORMATS, default="zip")
    download.add_argument("-o", "--output", help="output path (default: {park}_{version}.zip)")
    add_selection_args(download)

    limits = download.add_argument_group("safety limits")
    limits.add_argument(
        "--max-tiles", type=int, default=DEFAULT_MAX_TILES,
        help=f"refuse jobs larger than this without --force (default {DEFAULT_MAX_TILES:,})",
    )
    limits.add_argument(
        "--worker-max-tiles", type=int, default=None,
        help="cap for jobs routed through the shared Cloudflare Worker "
             "(default 10,000)",
    )
    limits.add_argument("--force", action="store_true", help="proceed past tile caps")
    limits.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt")
    limits.add_argument(
        "--dry-run", action="store_true", help="print the plan and exit before fetching"
    )
    limits.add_argument(
        "--bytes-per-tile", type=int, default=DEFAULT_BYTES_PER_TILE,
        help=argparse.SUPPRESS,
    )

    politeness = download.add_argument_group("politeness")
    politeness.add_argument(
        "--concurrency", type=int, default=5,
        help="parallel requests (default 5; warns above 10)",
    )
    politeness.add_argument(
        "--rps", type=float, default=10.0,
        help="global requests/second ceiling, 0 to disable (default 10)",
    )
    politeness.add_argument("--retries", type=int, default=5, help="retries per tile (default 5)")
    politeness.add_argument("--timeout", type=float, default=30.0, help="per-request timeout")

    resume = download.add_argument_group("resume")
    resume.add_argument("--state-db", help="path to the job state DB")
    resume.add_argument(
        "--restart", action="store_true", help="discard existing job state and start over"
    )
    resume.add_argument("--no-progress", action="store_true")

    tdr = download.add_argument_group("Tokyo Disney Resort")
    tdr.add_argument("--tdr-credentials", help="path to the TDR credentials JSON file")
    tdr.add_argument(
        "--direct", action="store_true",
        help="bypass the Cloudflare Worker and hit the origin with signed cookies",
    )

    add_source_args(download)
    download.set_defaults(func=cmd_download)

    # verify --------------------------------------------------------------
    verify = subparsers.add_parser("verify", help="check an archive's integrity")
    verify.add_argument("path")
    verify.add_argument(
        "--quick", action="store_true", help="skip per-tile image checks (sizes only)"
    )
    verify.add_argument("--json", action="store_true")
    verify.set_defaults(func=cmd_verify)

    return parser


#: Options whose value legitimately starts with '-' (a negative longitude).
_NEGATIVE_VALUE_OPTIONS = ("--bbox",)


def normalise_argv(argv: Sequence[str]) -> list[str]:
    """Let ``--bbox -81.6,28.3,-81.5,28.4`` work without an ``=``.

    argparse treats any token starting with ``-`` as an option unless it parses
    as a number, so every Florida and California bbox would otherwise be
    rejected with an unhelpful "expected one argument".
    """
    out: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if (
            token in _NEGATIVE_VALUE_OPTIONS
            and index + 1 < len(argv)
            and argv[index + 1].startswith("-")
        ):
            out.append(f"{token}={argv[index + 1]}")
            index += 2
            continue
        out.append(token)
        index += 1
    return out


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(normalise_argv(list(sys.argv[1:] if argv is None else argv)))

    if getattr(args, "worker_max_tiles", None) is None and hasattr(args, "worker_max_tiles"):
        from .tdr import WORKER_TILE_CAP

        args.worker_max_tiles = WORKER_TILE_CAP

    try:
        return args.func(args)
    except (TilearcError, ConfigError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return getattr(exc, "exit_code", 1)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
