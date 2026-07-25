"""Measuring the real tile bounds by asking the server.

``doctor`` reasons about bounds from geometry, which only works if a tile
pyramid covers the same ground at every zoom. Real park maps do not: WDW spans
about 172 km across at z11 and 13 km at z19, because deep detail is only drawn
near the resort. That makes most of what ``doctor`` reports for WDW correct
data, and leaves the genuine errors buried.

Whether a tile exists is not a matter of opinion, so this module settles it by
asking. For each zoom it verifies the four declared edges and walks outward or
inward until it finds where content actually stops.

The search is anchored on the declared bounds rather than starting from
nothing. When they are already right that costs roughly ``4 x samples``
requests per zoom -- a couple of hundred for a whole park -- and it only gets
expensive where the data is actually wrong.

What this cannot tell you: coverage *inside* the rectangle. The bounding box is
measurable; the drawn map inside it still has holes, and those remain normal.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterable, Sequence

import httpx

from . import __version__
from .config import ParkConfig, TileBounds, VersionEntry
from .errors import TilearcError
from .ratelimit import NullRateLimiter, RateLimiter
from .urls import TileSource

EDGES = ("minX", "maxX", "minY", "maxY")


@dataclass
class ProbeOptions:
    #: Tiles sampled along a candidate row/column before calling it empty.
    samples: int = 5
    #: A denser second pass, so a sparse-but-occupied line is not misjudged.
    dense_samples: int = 17
    #: How far beyond a declared edge to keep looking, in tiles.
    max_expand: int = 512
    concurrency: int = 4
    rps: float = 8.0
    timeout: float = 20.0
    retries: int = 3
    backoff_base: float = 1.0


@dataclass
class ZoomMeasurement:
    zoom: int
    declared: TileBounds | None
    measured: TileBounds | None
    requests: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.measured is not None and self.measured != self.declared

    @property
    def tile_delta(self) -> int:
        before = self.declared.count if self.declared else 0
        after = self.measured.count if self.measured else 0
        return after - before

    def describe_change(self) -> str:
        if self.measured is None:
            return "no tiles found"
        if self.declared is None:
            return "new"
        if not self.changed:
            return "unchanged"
        parts = []
        for edge, before, after in (
            ("minX", self.declared.min_x, self.measured.min_x),
            ("maxX", self.declared.max_x, self.measured.max_x),
            ("minY", self.declared.min_y, self.measured.min_y),
            ("maxY", self.declared.max_y, self.measured.max_y),
        ):
            if before != after:
                parts.append(f"{edge} {before:+d}->{after}".replace("+", ""))
        return ", ".join(parts)


def sample_points(lo: int, hi: int, count: int) -> list[int]:
    """Evenly spaced indices across ``[lo, hi]``, always including both ends."""
    if hi <= lo:
        return [lo]
    span = hi - lo + 1
    if count >= span:
        return list(range(lo, hi + 1))
    step = (hi - lo) / (count - 1)
    return sorted({int(round(lo + index * step)) for index in range(count)})


class Prober:
    """Answers 'does this tile exist?' politely."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        source: TileSource,
        options: ProbeOptions,
        limiter=None,
        on_request: Callable[[], None] | None = None,
    ) -> None:
        self.client = client
        self.source = source
        self.options = options
        self.limiter = limiter or (
            RateLimiter(options.rps) if options.rps > 0 else NullRateLimiter()
        )
        self.on_request = on_request
        self.requests = 0
        self._headers = dict(source.request_headers())
        # Only the first byte is needed to know a tile is there. Servers that
        # ignore Range just send the whole thing, which still works.
        self._headers["Range"] = "bytes=0-0"
        self._semaphore = asyncio.Semaphore(max(1, options.concurrency))

    async def exists(self, zoom: int, x: int, y: int) -> bool:
        url = self.source.url(zoom, x, y)
        attempt = 0
        while True:
            attempt += 1
            await self.limiter.acquire()
            self.requests += 1
            if self.on_request:
                self.on_request()
            try:
                async with self._semaphore:
                    response = await self.client.get(url, headers=self._headers)
            except (httpx.TimeoutException, httpx.TransportError):
                if attempt > self.options.retries:
                    return False
                await asyncio.sleep(min(self.options.backoff_base * 2 ** (attempt - 1), 20))
                continue

            status = response.status_code
            if status in (200, 206):
                return len(response.content) > 0
            if status in self.source.missing_statuses or status == 204:
                return False
            if status == 429 or status >= 500:
                if attempt > self.options.retries:
                    return False
                retry_after = response.headers.get("Retry-After")
                delay = (
                    float(retry_after) if (retry_after or "").isdigit()
                    else self.options.backoff_base * 2 ** (attempt - 1)
                )
                await asyncio.sleep(min(delay, 30))
                continue
            return False

    async def line_has_content(
        self, zoom: int, axis: str, index: int, lo: int, hi: int
    ) -> bool:
        """Is there any tile on the row/column at ``index``?

        ``axis='x'`` tests the column x=index over y in [lo, hi].
        """
        for count in (self.options.samples, self.options.dense_samples):
            points = sample_points(lo, hi, count)
            coords = [
                (index, point) if axis == "x" else (point, index) for point in points
            ]
            results = await asyncio.gather(
                *(self.exists(zoom, x, y) for x, y in coords)
            )
            if any(results):
                return True
            if count >= (hi - lo + 1):
                break            # the line was exhaustively checked
        return False


async def _find_edge(
    line_test: Callable[[int], "asyncio.Future[bool]"],
    *,
    anchor: int,
    start: int,
    direction: int,
    max_expand: int,
) -> tuple[int, bool]:
    """The outermost index in ``direction`` that still has content.

    ``anchor`` must be an index known to have content; ``start`` is the
    declared edge. Returns ``(index, hit_limit)``.
    """
    hit_limit = False

    if await line_test(start):
        inside, outside = start, None
        step = 1
        while step <= max_expand:
            candidate = start + direction * step
            if candidate < 0:
                break
            if await line_test(candidate):
                inside = candidate
                step *= 2
            else:
                outside = candidate
                break
        if outside is None:
            return inside, True
    else:
        inside, outside = anchor, start

    # Binary search for the last index with content.
    while abs(outside - inside) > 1:
        middle = (inside + outside) // 2
        if await line_test(middle):
            inside = middle
        else:
            outside = middle
    return inside, hit_limit


async def _find_seed(
    prober: Prober, zoom: int, declared: TileBounds
) -> tuple[int, int] | None:
    """Any tile inside the declared box that exists, to anchor the search."""
    centre = ((declared.min_x + declared.max_x) // 2, (declared.min_y + declared.max_y) // 2)
    candidates: list[tuple[int, int]] = [centre]
    for x in sample_points(declared.min_x, declared.max_x, 5):
        for y in sample_points(declared.min_y, declared.max_y, 5):
            candidates.append((x, y))

    seen: set[tuple[int, int]] = set()
    for x, y in candidates:
        if (x, y) in seen:
            continue
        seen.add((x, y))
        if await prober.exists(zoom, x, y):
            return x, y
    return None


async def measure_zoom(
    prober: Prober, zoom: int, declared: TileBounds
) -> ZoomMeasurement:
    before = prober.requests
    notes: list[str] = []

    seed = await _find_seed(prober, zoom, declared)
    if seed is None:
        return ZoomMeasurement(
            zoom, declared, None,
            requests=prober.requests - before,
            notes=["no tiles found anywhere inside the declared bounds"],
        )
    seed_x, seed_y = seed

    def column(index: int, lo: int, hi: int):
        return prober.line_has_content(zoom, "x", index, lo, hi)

    def row(index: int, lo: int, hi: int):
        return prober.line_has_content(zoom, "y", index, lo, hi)

    limit = prober.options.max_expand

    min_x, limit_min_x = await _find_edge(
        lambda i: column(i, declared.min_y, declared.max_y),
        anchor=seed_x, start=declared.min_x, direction=-1, max_expand=limit,
    )
    max_x, limit_max_x = await _find_edge(
        lambda i: column(i, declared.min_y, declared.max_y),
        anchor=seed_x, start=declared.max_x, direction=+1, max_expand=limit,
    )
    # Y edges use the freshly measured X range, so a widened map is sampled
    # across its true width.
    min_y, limit_min_y = await _find_edge(
        lambda i: row(i, min_x, max_x),
        anchor=seed_y, start=declared.min_y, direction=-1, max_expand=limit,
    )
    max_y, limit_max_y = await _find_edge(
        lambda i: row(i, min_x, max_x),
        anchor=seed_y, start=declared.max_y, direction=+1, max_expand=limit,
    )

    if any((limit_min_x, limit_max_x, limit_min_y, limit_max_y)):
        notes.append(
            f"stopped at the {limit}-tile search limit; the real extent may be wider"
        )

    return ZoomMeasurement(
        zoom,
        declared,
        TileBounds(min_x, max_x, min_y, max_y),
        requests=prober.requests - before,
        notes=notes,
    )


async def discover(
    source: TileSource,
    zooms: Sequence[tuple[int, TileBounds]],
    options: ProbeOptions | None = None,
    *,
    client: httpx.AsyncClient | None = None,
    on_zoom_done: Callable[[ZoomMeasurement], None] | None = None,
    on_request: Callable[[], None] | None = None,
) -> list[ZoomMeasurement]:
    options = options or ProbeOptions()
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(options.timeout),
            limits=httpx.Limits(max_connections=options.concurrency),
            follow_redirects=True,
        )

    prober = Prober(client, source, options, on_request=on_request)
    results: list[ZoomMeasurement] = []
    try:
        for zoom, declared in zooms:
            measurement = await measure_zoom(prober, zoom, declared)
            results.append(measurement)
            if on_zoom_done:
                on_zoom_done(measurement)
    finally:
        if owns_client:
            await client.aclose()
    return results


def estimate_requests(zoom_count: int, options: ProbeOptions | None = None) -> int:
    """A rough upper bound, for telling the user what a run will cost."""
    options = options or ProbeOptions()
    # seed probing + 4 edges x (expansion + binary search) x samples
    per_zoom = 25 + 4 * 12 * options.samples
    return zoom_count * per_zoom


# ---------------------------------------------------------------------------
# writing the result back
# ---------------------------------------------------------------------------


def bounds_block(measurements: Iterable[ZoomMeasurement], indent: str = "  ") -> str:
    """Render ``boundsByZoom`` in the compact one-line-per-zoom style the
    viewer's configs already use, so a rewrite produces a readable diff."""
    lines = []
    for measurement in measurements:
        if measurement.measured is None:
            continue
        bounds = measurement.measured
        lines.append(
            f'{indent}{indent}"{measurement.zoom}": '
            f'{{ "minX": {bounds.min_x}, "maxX": {bounds.max_x}, '
            f'"minY": {bounds.min_y}, "maxY": {bounds.max_y} }}'
        )
    body = ",\n".join(lines)
    return f'{indent}"boundsByZoom": {{\n{body}\n{indent}}}'


def provenance_block(version_code: str, indent: str = "  ") -> str:
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return (
        f'{indent}"boundsMeasured": {{ "tool": "tilearc {__version__}", '
        f'"at": "{stamp}", "version": "{version_code}" }}'
    )


def _find_key_span(text: str, key: str) -> tuple[int, int] | None:
    """Character span of ``"key": {...}`` including the closing brace."""
    match = re.search(rf'^([ \t]*)"{re.escape(key)}"\s*:\s*\{{', text, re.MULTILINE)
    if not match:
        return None
    depth = 0
    index = match.end() - 1
    while index < len(text):
        char = text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return match.start(), index + 1
        elif char == '"':
            index += 1
            while index < len(text) and text[index] != '"':
                index += 2 if text[index] == "\\" else 1
        index += 1
    return None


def patch_config_text(
    text: str, measurements: Iterable[ZoomMeasurement], version_code: str
) -> str:
    """Replace ``boundsByZoom`` in a config file, touching nothing else."""
    measurements = list(measurements)
    span = _find_key_span(text, "boundsByZoom")
    if span is None:
        raise TilearcError("config has no boundsByZoom object to replace")

    start, end = span
    updated = text[:start] + bounds_block(measurements) + text[end:]

    # Record when and against which version the measurement was taken.
    provenance = provenance_block(version_code)
    existing = _find_key_span(updated, "boundsMeasured")
    if existing:
        updated = updated[: existing[0]] + provenance + updated[existing[1] :]
    else:
        anchor = _find_key_span(updated, "boundsByZoom")
        assert anchor is not None
        updated = updated[: anchor[0]] + provenance + ",\n" + updated[anchor[0] :]

    json.loads(updated)          # never hand back something unparseable
    return updated


def measurements_to_json(measurements: Iterable[ZoomMeasurement]) -> dict:
    return {
        str(m.zoom): m.measured.as_dict()
        for m in measurements
        if m.measured is not None
    }


def build_source_for(park: ParkConfig, version: VersionEntry) -> TileSource:
    from .urls import build_source

    return build_source(park, version)
