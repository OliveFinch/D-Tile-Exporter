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
from .bounds import BBox, select_zooms
from .config import ParkRepository, build_repository
from .discover import (
    ProbeOptions,
    bounds_block,
    discover,
    estimate_requests,
    measurements_to_json,
    patch_config_text,
)
from .doctor import check_park, worst_severity
from .downloader import CONCURRENCY_WARN_THRESHOLD, DownloadOptions
from .errors import ConfigError, QuotaError, TilearcError
from .job import JobRequest, run_job
from .plan import DEFAULT_BYTES_PER_TILE, JobPlan, build_plan, load_coverage
from .progress import Progress
from .state import default_state_path
from .tdr import (
    WORKER_DAILY_QUOTA,
    build_tdr_source,
    check_worker_quota,
    load_credentials,
    normalise_modes,
)
from .trace import BatchProbe, TraceOptions, coverage_payload
from .trace import estimate_requests as estimate_trace_requests
from .trace import trace as trace_coverage
from .urls import build_source
from .util import human_bytes, sanitize_component
from .verify import verify as verify_archive
from .writers import FORMATS, default_output

DEFAULT_MAX_TILES = 250_000

#: Tile probes per Worker request when tracing through a batch endpoint.
#: Measured on TDR-shaped work, where the walk dominates and each ring step
#: costs one call regardless of how many tiles a batch could carry.
BATCH_REDUCTION = 3


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
    group.add_argument(
        "--coverage",
        default=os.environ.get("TILEARC_COVERAGE"),
        help="a measured-coverage JSON file (see tools/measured-coverage.json). "
             "Plans from what the server actually serves rather than what the "
             "config declares -- which both stops the job asking for tiles that "
             "do not exist and stops it missing tiles that do. "
             "Env: TILEARC_COVERAGE",
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

    coverage = coverage_version = None
    path = getattr(args, "coverage", None)
    if path:
        coverage, coverage_version = load_coverage(path, park.park_id)

    return build_plan(
        park,
        version,
        coverage=coverage,
        coverage_version=coverage_version,
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


def cmd_discover(args: argparse.Namespace) -> int:
    repo = repository_from(args)
    park = repo.park(args.park)
    version = repo.version(args.park, args.version)

    if not park.tile_template and not version.url:
        raise TilearcError(
            f"'{park.park_id}' has no public tile template, so its bounds cannot "
            f"be measured by this command."
        )

    selection = select_zooms(park, args.min_zoom, args.max_zoom)
    zooms = [(z, park.bounds_at(z)) for z in selection.zooms if park.bounds_at(z)]
    if not zooms:
        raise TilearcError("no zoom levels with bounds to measure")

    options = ProbeOptions(
        samples=args.samples, concurrency=args.concurrency, rps=args.rps,
        max_expand=args.max_expand,
    )
    upper_bound = estimate_requests(len(zooms), options)

    info(f"park     {park.park_id}  {park.label}")
    info(f"version  {version.code}")
    info(f"zooms    {', '.join(str(z) for z, _b in zooms)}")
    info(
        f"cost     up to about {upper_bound:,} requests "
        f"(far fewer if the declared bounds are already right)"
    )
    info(f"polite   concurrency {options.concurrency}, {options.rps:g} req/s")
    info("")

    if args.dry_run:
        info("dry run -- stopping before any request")
        return 0
    if not _confirm("Measure the real tile bounds from the server?", args.yes):
        info("aborted")
        return 1

    source = build_source(park, version)
    progress = Progress(upper_bound, enabled=not args.no_progress)

    measurements = asyncio.run(
        discover(
            source,
            zooms,
            options,
            on_request=lambda: progress.update(ok=1),
            on_zoom_done=lambda m: None,
        )
    )
    progress.close()

    total_requests = sum(m.requests for m in measurements)
    changed = [m for m in measurements if m.changed]

    if args.json:
        emit_json(
            {
                "park": park.park_id,
                "version": version.code,
                "requests": total_requests,
                "boundsByZoom": measurements_to_json(measurements),
                "changes": {
                    str(m.zoom): {
                        "declared": m.declared.as_dict() if m.declared else None,
                        "measured": m.measured.as_dict() if m.measured else None,
                        "tileDelta": m.tile_delta,
                    }
                    for m in changed
                },
            }
        )
        return 0

    info("")
    info(f"{'zoom':>5}  {'declared':<28} {'measured':<28} change")
    for m in measurements:
        declared = (
            f"{m.declared.min_x}-{m.declared.max_x},{m.declared.min_y}-{m.declared.max_y}"
            if m.declared else "-"
        )
        measured = (
            f"{m.measured.min_x}-{m.measured.max_x},{m.measured.min_y}-{m.measured.max_y}"
            if m.measured else "-"
        )
        info(f"{m.zoom:>5}  {declared:<28} {measured:<28} {m.describe_change()}")
        for note in m.notes:
            info(f"{'':>5}  note: {note}")

    info("")
    info(f"{total_requests:,} requests, {len(changed)} zoom(s) differ from the config")

    if not changed:
        info("The declared bounds match the server exactly. Nothing to change.")
        return 0

    delta = sum(m.tile_delta for m in measurements)
    info(f"net change to a full-depth job: {delta:+,} tiles")

    if args.write:
        target = Path(repo.source.describe()) / park.park_id / f"{park.park_id}_config.json"
        if not target.is_file():
            raise TilearcError(
                f"--write needs a local checkout; {target} does not exist. "
                f"Re-run with --config-dir pointing at the viewer repo."
            )
        original = target.read_text(encoding="utf-8")
        target.write_text(
            patch_config_text(original, measurements, version.code), encoding="utf-8"
        )
        info(f"\nupdated {target}")
        info("Review the diff, then commit it so both apps pick it up.")
    else:
        info("\nPaste this into the park config (or re-run with --write):\n")
        print(bounds_block(measurements))

    return 0


def cmd_trace(args: argparse.Namespace) -> int:
    """Walk each zoom's coverage border and report the footprint it encloses.

    Unlike ``discover`` this works for credentialled parks, which is the whole
    reason it exists on this side rather than only in the browser tool: TDR's
    tiles need signed cookies on the request, and an ``<img>`` cannot carry
    them.
    """
    repo = repository_from(args)
    park = repo.park(args.park)
    version = repo.version(args.park, args.version)

    # Every zoom with bounds, not just those inside the declared minZoom/maxZoom.
    # `select_zooms` excludes the rest on the grounds that they are "not part of
    # the map" -- reasonable when planning a download, wrong when the job is to
    # find out what is actually served. Shanghai settled that: it declares
    # minZoom 14 and serves z12 and z13 perfectly well. A measurement should not
    # inherit the assumption it exists to test.
    candidates = sorted(park.bounds_by_zoom)
    if args.min_zoom is not None:
        candidates = [z for z in candidates if z >= args.min_zoom]
    if args.max_zoom is not None:
        candidates = [z for z in candidates if z <= args.max_zoom]
    zooms = [(z, park.bounds_at(z)) for z in candidates]
    if not zooms:
        raise TilearcError("no zoom levels with bounds to trace")
    undeclared = [z for z, _b in zooms if z < park.min_zoom or z > park.max_zoom]

    # -- sources ----------------------------------------------------------
    if park.requires_credentials:
        credentials = load_credentials(park, args.tdr_credentials, warn=warn)
        expiry = credentials.expiry_warning()
        if expiry:
            warn(expiry)
        modes = normalise_modes(args.mode)
        sources = {
            mode: build_tdr_source(park, version.code, mode, credentials, direct=args.direct)
            for mode in modes
        }
    else:
        sources = {"": build_source(park, version)}

    options = TraceOptions(
        margin=args.margin,
        concurrency=args.concurrency,
        rps=args.rps,
        max_requests=args.max_requests,
        max_regions=args.max_regions,
    )
    per_mode = estimate_trace_requests(zooms)
    upper_bound = per_mode * len(sources)

    info(f"park     {park.park_id}  {park.label}")
    info(f"version  {version.code}")
    if len(sources) > 1 or any(sources):
        info(f"modes    {', '.join(m or 'default' for m in sources)}")
    info(f"zooms    {', '.join(str(z) for z, _b in zooms)}")
    if undeclared:
        info(
            f"         including z{', z'.join(str(z) for z in undeclared)}, outside the "
            f"declared {park.min_zoom}-{park.max_zoom} range but with bounds to check"
        )
    info(f"cost     roughly {upper_bound:,} requests, scaling with perimeter not area")
    info(f"polite   concurrency {options.concurrency}, {options.rps:g} req/s")

    warnings: list[str] = []
    if any(source.uses_shared_proxy for source in sources.values()):
        if args.probe_url:
            # Measured at about 3x on TDR-shaped work, not the batch size: the
            # walk dominates, and one ring step is one batch call however many
            # tiles a batch could hold. The real gain is that a refusal comes
            # back as a refusal.
            batches = -(-upper_bound // BATCH_REDUCTION)
            info("")
            info(f"batched  {args.probe_url}")
            info(
                f"         reports refusals as refusals rather than as missing tiles, "
                f"which is\n         the ambiguity that makes Worker-routed traces "
                f"untrustworthy. Roughly\n         {batches:,} Worker requests instead of "
                f"{upper_bound:,} -- about 3x, since a ring step is\n         one call "
                f"whatever the batch size allows."
            )
        elif args.my_worker:
            # Owning it does not make the free tier's 100k/day imaginary, and
            # live viewer traffic still shares it -- but the cost is a fact to
            # report, not a warning to issue.
            share = upper_bound / WORKER_DAILY_QUOTA * 100
            info("")
            info(
                f"worker   your own, so no cap applied. This is {upper_bound:,} requests, "
                f"~{share:.0f}% of\n         the free tier's {WORKER_DAILY_QUOTA:,}/day, "
                f"shared with live viewer traffic."
            )
            info(
                "         --probe-url cuts that to about a third and, more to the "
                "point, tells\n         a refusal from a missing tile; see "
                "tools/worker-exists-endpoint.js"
            )
        else:
            warnings.extend(
                check_worker_quota(upper_bound, forced=args.force, cap=args.worker_max_tiles)
            )
        if not args.probe_url:
            info("")
            info(
                "note     the tile proxy answers 204 both for a missing tile and for an "
                "upstream\n         refusal, so the two are indistinguishable here. Every "
                "zoom is audited\n         afterwards; --probe-url or --direct removes the "
                "ambiguity outright."
            )
    for message in warnings:
        warn(message)
    info("")

    if args.dry_run:
        info("dry run -- stopping before any request")
        return 0
    if not _confirm("Walk the coverage border for these zooms?", args.yes):
        info("aborted")
        return 1

    progress = Progress(upper_bound, enabled=not args.no_progress)
    results: dict[str, list] = {}
    for mode, source in sources.items():
        batch = None
        if args.probe_url:
            batch = BatchProbe(
                url=args.probe_url,
                server_id=(
                    load_credentials(park, args.tdr_credentials).server_id or version.code
                ),
                mode=mode or "daytime",
                limit=args.probe_batch,
                token=args.probe_token,
            )
        results[mode] = asyncio.run(
            trace_coverage(
                source, zooms, options,
                on_request=progress.tick,
                on_zoom_done=lambda _t: None,
                batch=batch,
            )
        )
    progress.close()

    if args.json:
        emit_json({
            "park": park.park_id,
            "version": version.code,
            "requests": sum(t.requests for group in results.values() for t in group),
            "modes": {mode or "default": coverage_payload(group) for mode, group in results.items()},
        })
        return 0

    incomplete = 0
    for mode, group in results.items():
        info("")
        if mode:
            info(f"--- {mode}")
        info(f"{'zoom':>5}  {'requests':>9}  {'footprint':<30} {'tiles':>9}  shape")
        for item in group:
            box = item.box
            where = (
                f"{box.min_x}-{box.max_x}, {box.min_y}-{box.max_y}" if box else "-"
            )
            info(f"{item.zoom:>5}  {item.requests:>9,}  {where:<30} {item.covered:>9,}  "
                 f"{item.describe()}")
            if not item.complete:
                incomplete += 1
            for run in item.runs() if item.regions and not item.rectangle else []:
                info(f"{'':>5}  rows {run[0]}-{run[1]}: x {run[2]}-{run[3]}")

    total = sum(t.covered for group in results.values() for t in group)
    info("")
    info(f"{sum(t.requests for g in results.values() for t in g):,} requests, "
         f"{total:,} tiles across {len(zooms)} zoom(s)"
         + (f" x {len(results)} modes" if len(results) > 1 else ""))
    if incomplete:
        warn(f"{incomplete} zoom(s) did not finish cleanly -- see the shape column. "
             f"Those figures are floors, not measurements.")
        return 1
    return 0


def cmd_library(args: argparse.Namespace) -> int:
    """Report on, or query, a library tree's catalogue."""
    from .library import Catalogue, human_saving

    root = Path(args.root)
    if not (root / "catalogue.sqlite").is_file():
        raise TilearcError(
            f"no catalogue at {root / 'catalogue.sqlite'}. Download with "
            f"--format library --output {root} first."
        )
    catalogue = Catalogue(root)
    try:
        # -- resolve one tile ---------------------------------------------
        if args.resolve:
            try:
                park, version, z, x, y = args.resolve
                path = catalogue.resolve(park, version, int(z), int(x), int(y), args.mode or "")
            except ValueError as exc:
                raise TilearcError(f"--resolve wants PARK VERSION Z X Y: {exc}") from exc
            if path is None:
                info("not in the library")
                return 1
            print(path)
            return 0

        # -- dump the index -----------------------------------------------
        if args.export:
            payload = catalogue.export_index(args.park)
            Path(args.export).write_text(json.dumps(payload, indent=2), encoding="utf-8")
            info(f"wrote {args.export}")
            return 0

        stats = catalogue.stats()
        if args.park:
            stats = [row for row in stats if row["park"] == args.park]
        if args.json:
            emit_json({"root": str(root), "versions": stats, "totals": human_saving(stats)})
            return 0

        if not stats:
            info("the library is empty")
            return 0

        info(f"{root}")
        info("")
        info(f"{'park':<6} {'version':<18} {'tiles':>10} {'stored here':>12} "
             f"{'reused':>9} {'on disk':>10}")
        for row in stats:
            reused = row["tiles"] - row["stored"]
            info(f"{row['park']:<6} {row['version']:<18} {row['tiles']:>10,} "
                 f"{row['stored']:>12,} {reused:>9,} "
                 f"{human_bytes(row['stored_bytes'] or 0):>10}")

        totals = human_saving(stats)
        info("")
        info(f"on disk  {human_bytes(totals['storedBytes'])}")
        info(f"as if every version were archived separately  "
             f"{human_bytes(totals['logicalBytes'])}")
        if totals["logicalBytes"]:
            saved = totals["savedBytes"] / totals["logicalBytes"] * 100
            info(f"saved by not storing unchanged tiles twice  "
                 f"{human_bytes(totals['savedBytes'])} ({saved:.0f}%)")
        return 0
    finally:
        catalogue.close()


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
    if args.rps <= 0:
        rate_note = "no rate cap"
    elif args.adaptive:
        rate_note = f"up to {args.rps:g} req/s, backing off if pushed back"
    else:
        rate_note = f"{args.rps:g} req/s held"
    info(f"polite   concurrency {args.concurrency}, {rate_note}, {args.retries} retries")
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
    options = DownloadOptions(
        concurrency=args.concurrency,
        rps=args.rps,
        adaptive=args.adaptive,
        retries=args.retries,
        timeout=args.timeout,
    )
    progress = Progress(plan.total_tiles, enabled=not args.no_progress)

    def announce_resume(counts: dict[str, int]) -> None:
        info(
            f"resuming: {counts.get('done', 0):,} already downloaded, "
            f"{counts.get('missing', 0):,} known missing, "
            f"{counts.get('failed', 0):,} to retry"
        )

    request = JobRequest(
        plan=plan,
        sources=tile_sources,
        fmt=fmt,
        output=output,
        state_path=state_path,
        options=options,
        restart=args.restart,
        retry_missing=args.retry_missing,
    )

    try:
        outcome = asyncio.run(
            run_job(
                request,
                progress,
                log=(lambda m: None) if args.no_progress else _progress_safe_log(progress),
                on_resume=announce_resume,
            )
        )
    except KeyboardInterrupt:
        progress.close()
        raise
    finally:
        progress.close()

    info("")
    info(progress.summary())

    if outcome.stopped_early:
        if fmt == "zip":
            info(f"staged   {outcome.artefact} (not packed -- the job is unfinished)")
        else:
            info(f"wrote    {outcome.artefact}")
        info(f"state    {outcome.state_path}")
        info("")
        if outcome.error is not None:
            warn(f"job did not complete: {outcome.error}")
        else:
            warn("job was interrupted")
        info("re-run the same command to continue where it stopped")
        if outcome.error is not None:
            return getattr(outcome.error, "exit_code", 1)
        return 130

    info(f"wrote    {outcome.artefact}")

    if outcome.failed:
        warn(
            f"{outcome.failed:,} tile(s) failed after retries; re-run the same "
            f"command to retry just those"
        )
        return 1

    info(f"archive complete ({outcome.downloaded:,} tiles, "
         f"{outcome.missing:,} with no coverage)")
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
    download.add_argument(
        "--format", choices=FORMATS, default="zip",
        help="zip, dir, mbtiles, or library. 'library' writes into a shared tree "
             "at --output, storing a tile only when its bytes differ from what an "
             "earlier version already holds, and indexing every version's tiles in "
             "a catalogue so a reader can find them.",
    )
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
    politeness.add_argument(
        "--no-adaptive-rate", dest="adaptive", action="store_false",
        help="hold --rps exactly instead of backing off automatically on 429/503",
    )
    politeness.add_argument("--retries", type=int, default=5, help="retries per tile (default 5)")
    politeness.add_argument("--timeout", type=float, default=30.0, help="per-request timeout")

    resume = download.add_argument_group("resume")
    resume.add_argument("--state-db", help="path to the job state DB")
    resume.add_argument(
        "--restart", action="store_true", help="discard existing job state and start over"
    )
    resume.add_argument(
        "--retry-missing", action="store_true",
        help="re-ask for tiles previously recorded as having no imagery, keeping "
             "downloaded ones (use when a run was rate-limited)",
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
    # discover ------------------------------------------------------------
    discover_parser = subparsers.add_parser(
        "discover",
        help="measure the real tile bounds by probing the server",
        description=(
            "Finds where tiles actually stop existing, rather than inferring it. "
            "Anchored on the declared bounds, so it is cheap when they are right."
        ),
    )
    discover_parser.add_argument("--park", required=True)
    discover_parser.add_argument("--version", required=True)
    discover_parser.add_argument("--min-zoom", type=int)
    discover_parser.add_argument("--max-zoom", type=int)
    discover_parser.add_argument(
        "--write", action="store_true",
        help="update the park config in --config-dir with the measured bounds",
    )
    discover_parser.add_argument(
        "--samples", type=int, default=5,
        help="tiles sampled along a row/column before calling it empty (default 5)",
    )
    discover_parser.add_argument(
        "--max-expand", type=int, default=512,
        help="how far beyond a declared edge to keep looking (default 512 tiles)",
    )
    discover_parser.add_argument("--concurrency", type=int, default=4)
    discover_parser.add_argument("--rps", type=float, default=8.0)
    discover_parser.add_argument("-y", "--yes", action="store_true")
    discover_parser.add_argument("--dry-run", action="store_true")
    discover_parser.add_argument("--no-progress", action="store_true")
    discover_parser.add_argument("--json", action="store_true")
    add_source_args(discover_parser)
    discover_parser.set_defaults(func=cmd_discover)

    trace_parser = subparsers.add_parser(
        "trace",
        help="walk each zoom's coverage border and report the real footprint",
        description=(
            "Follows the outline of the imagery tile by tile and fills what it "
            "encloses, so an L-shaped or bitten footprint is reported as it is "
            "rather than as the rectangle that bounds it. Costs the perimeter, "
            "not the area. Unlike 'discover' this works for credentialled parks."
        ),
    )
    trace_parser.add_argument("--park", required=True)
    trace_parser.add_argument("--version", required=True)
    trace_parser.add_argument("--min-zoom", type=int)
    trace_parser.add_argument("--max-zoom", type=int)
    trace_parser.add_argument(
        "--margin", type=int, default=8,
        help="how far outside the declared box the walk may stray (default 8 tiles)",
    )
    trace_parser.add_argument(
        "--max-requests", type=int, default=60_000,
        help="per-zoom ceiling, so a runaway walk cannot spend all night",
    )
    trace_parser.add_argument(
        "--max-regions", type=int, default=6,
        help="distinct regions to report per zoom before giving up looking (default 6)",
    )
    trace_parser.add_argument("--concurrency", type=int, default=6)
    trace_parser.add_argument("--rps", type=float, default=12.0)
    trace_parser.add_argument(
        "--mode", default="both", help="TDR only: daytime, nighttime, or both",
    )
    trace_parser.add_argument(
        "--direct", action="store_true",
        help="TDR only: go to the origin with CloudFront cookies instead of the "
             "shared Worker. Slower to set up, but a 403 is then distinguishable "
             "from a missing tile, which the Worker's 204 is not.",
    )
    trace_parser.add_argument("--tdr-credentials")
    trace_parser.add_argument(
        "--probe-url",
        help="URL of a batch existence endpoint on your own Worker (see "
             "tools/worker-exists-endpoint.js). Asks about tiles in bulk, so a trace "
             "costs hundreds of Worker requests instead of tens of thousands, and a "
             "refusal comes back as a refusal instead of as a missing tile.",
    )
    trace_parser.add_argument(
        "--probe-token",
        default=os.environ.get("TILEARC_PROBE_TOKEN"),
        help="bearer token for --probe-url, if the endpoint is guarded. "
             "Env: TILEARC_PROBE_TOKEN",
    )
    trace_parser.add_argument(
        "--probe-batch", type=int, default=48,
        help="tiles per batch request (default 48; Cloudflare's free tier allows 50 "
             "subrequests per invocation)",
    )
    trace_parser.add_argument(
        "--my-worker", action="store_true",
        help="the Worker is yours: report the quota cost rather than capping the run",
    )
    trace_parser.add_argument(
        "--worker-max-tiles", type=int, default=10_000,
        help="safety cap on requests routed through the shared Worker",
    )
    trace_parser.add_argument("--force", action="store_true")
    trace_parser.add_argument("-y", "--yes", action="store_true")
    trace_parser.add_argument("--dry-run", action="store_true")
    trace_parser.add_argument("--no-progress", action="store_true")
    trace_parser.add_argument("--json", action="store_true")
    add_source_args(trace_parser)
    trace_parser.set_defaults(func=cmd_trace)

    library = subparsers.add_parser(
        "library",
        help="report on a multi-version library tree",
        description=(
            "A library stores each version's tiles only where they differ from an "
            "earlier version, and indexes every version's tiles in a catalogue so a "
            "reader can find bytes that live under another version's folder. This "
            "reports what is held, and can resolve individual tiles."
        ),
    )
    library.add_argument("--root", default="library", help="the library directory")
    library.add_argument("--park", help="limit the report to one park")
    library.add_argument(
        "--resolve", nargs=5, metavar=("PARK", "VERSION", "Z", "X", "Y"),
        help="print the file holding one tile, wherever it actually lives",
    )
    library.add_argument("--mode", default="", help="TDR only: daytime or nighttime")
    library.add_argument(
        "--export", metavar="FILE",
        help="write a JSON index of where every tile lives, for a reader that is "
             "not SQLite",
    )
    library.add_argument("--json", action="store_true")
    library.set_defaults(func=cmd_library)

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
