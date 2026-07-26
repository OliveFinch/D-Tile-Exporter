"""Walking a coverage border rather than measuring four edges.

:mod:`tilearc.discover` finds where each of the four edges of a zoom sits. That
answers "how wide is the imagery" but not "what shape is it": an L, a bite out
of one corner, or two separate blobs all report the same rectangle, and a
downloader driven by that rectangle asks for tiles nobody ever drew.

This walks the outline instead -- Moore-neighbour tracing, the way a hand
follows a wall -- and then fills what the outline encloses. The cost scales with
the perimeter rather than the area, and the result says what the footprint
actually is. Measured against the public maps, Hong Kong Disneyland turns out to
be an L whose bounding box is 15% empty.

Why this exists here as well as in ``tools/tile-border-trace.html``: that page
probes with ``<img>``, which cannot carry credentials and cannot see a status
code. TDR needs both. Signed CloudFront cookies have to go on the request, and
the difference between "no tile" and "your signature was rejected" is the
difference between a measurement and a fiction.

Which brings us to the reason for :class:`Verdict`. A tile probe has three
outcomes, not two, and conflating the last two is the bug this whole module is
careful about:

* **present** -- imagery is there.
* **absent** -- the server says there is nothing, and means it.
* **refused** -- the server declined to answer. Says nothing about the tile.

Going direct, TDR distinguishes them itself: 404 is absent, 403 is a rejected
signature. Through the viewer's Worker it does not -- upstream 403 and 404 both
come back as 204 -- so there the two are indistinguishable at the protocol
level, and the audit at the end of every trace is the only thing standing
between a throttled run and a confidently wrong answer.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Iterable, Sequence

import httpx

from .config import TileBounds
from .errors import CredentialsError, TilearcError
from .ratelimit import NullRateLimiter, RateLimiter
from .urls import Outcome, TileSource

#: Clockwise from north-west. The tracer rotates through this ring, so the
#: order decides which way round the border is walked.
RING = ((-1, -1), (0, -1), (1, -1), (1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0))

#: Lattice sizes the seed search steps through, coarse to fine. A region larger
#: than the finest lattice cannot be missed; one smaller than it can.
SEED_LATTICES = (9, 49, 225, 900)


class Verdict:
    PRESENT = "present"
    ABSENT = "absent"
    REFUSED = "refused"


@dataclass
class TraceOptions:
    #: How far outside the declared box the walk may stray. A border that
    #: reaches this has not been measured, it has been cut off.
    margin: int = 8
    #: Hard ceiling per zoom, so a runaway walk cannot spend all night.
    max_requests: int = 60_000
    #: Separate ceilings: `rps` is politeness, `concurrency` is how much of the
    #: round-trip latency can be hidden. Against a slow CDN the second one is
    #: usually what binds.
    concurrency: int = 6
    rps: float = 12.0
    timeout: float = 20.0
    retries: int = 3
    backoff_base: float = 1.0
    #: Distinct regions to report before giving up looking for more.
    max_regions: int = 6
    #: Sample sizes for the post-trace audit.
    audit_absences: int = 24
    audit_controls: int = 12
    #: Consecutive refusals tolerated before the trace is abandoned. A run this
    #: long means the server has stopped talking to us, and every further
    #: verdict would be noise.
    refusal_limit: int = 12


class TraceRefused(TilearcError):
    """The server stopped answering partway through a walk."""


@dataclass
class BatchProbe:
    """A bulk existence endpoint on a Worker you control.

    The reason to use it is correctness. It can answer ``refused`` as a verdict
    of its own, which the tile proxy cannot: serving imagery, an upstream 403
    and 404 both correctly become "204, nothing to draw", and a walk cannot tell
    a stale signature from the edge of the map.

    It is also cheaper, though by less than the batch size suggests -- about 3x
    on TDR-shaped work. The seed lattice fills a batch, but the walk dominates
    and one ring step is one call whether a batch holds 8 tiles or 48.

    See ``tools/worker-exists-endpoint.js`` for the other half.
    """

    url: str
    server_id: str
    mode: str
    #: Cloudflare's free tier allows 50 subrequests per invocation.
    limit: int = 48

    async def check(
        self, client: httpx.AsyncClient, zoom: int, points: Sequence[tuple[int, int]]
    ) -> list[str]:
        response = await client.post(
            self.url,
            json={
                "sid": self.server_id,
                "mode": self.mode,
                "z": zoom,
                "tiles": [[x, y] for x, y in points],
            },
        )
        if response.status_code != 200:
            raise TraceRefused(
                f"the batch endpoint at {self.url} answered HTTP "
                f"{response.status_code}: {response.text[:200]}"
            )
        verdicts = str(response.json().get("verdicts", ""))
        if len(verdicts) != len(points):
            # Lining a short string up against coordinates by index would
            # quietly attribute one tile's answer to another.
            raise TraceRefused(
                f"the batch endpoint returned {len(verdicts)} verdicts for "
                f"{len(points)} tiles; refusing to guess which is which"
            )
        return [
            {"P": Verdict.PRESENT, "A": Verdict.ABSENT}.get(char, Verdict.REFUSED)
            for char in verdicts
        ]


# ---------------------------------------------------------------------------
# asking the server
# ---------------------------------------------------------------------------


class TileOracle:
    """Answers "is this tile there?", once per tile, politely, in parallel.

    Two things beyond a plain request. The cache is not an optimisation but a
    requirement: each step of the walk inspects up to eight neighbours, most of
    which the previous step already inspected, and consecutive border tiles
    share most of their ring. And because those overlapping asks now go out
    concurrently, a second ask for a tile already in flight has to join the
    first rather than issue its own.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        source: TileSource,
        options: TraceOptions,
        limiter=None,
        on_request: Callable[[], None] | None = None,
        batch: "BatchProbe | None" = None,
    ) -> None:
        self.client = client
        self.source = source
        self.options = options
        self.batch = batch
        self.limiter = limiter or (
            RateLimiter(options.rps) if options.rps > 0 else NullRateLimiter()
        )
        self.on_request = on_request
        self.requests = 0
        self.refusals = 0
        self._run_of_refusals = 0
        self._cache: dict[tuple[int, int], bool] = {}
        self._inflight: dict[tuple[int, int], asyncio.Task[bool]] = {}
        self._semaphore = asyncio.Semaphore(max(1, options.concurrency))
        self._headers = dict(source.request_headers())
        # Only the first byte is needed to know a tile is there. A server that
        # ignores Range just sends the whole thing, which still works.
        self._headers["Range"] = "bytes=0-0"
        self.limits: TileBounds | None = None
        self.capped = False

    # -- the raw question --------------------------------------------------

    async def _ask(self, zoom: int, x: int, y: int) -> str:
        """One tile, one of three answers, with retries for transient trouble."""
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
                    return Verdict.REFUSED       # not "absent": we never heard back
                await asyncio.sleep(min(self.options.backoff_base * 2 ** (attempt - 1), 20))
                continue

            status = response.status_code
            body = len(response.content)
            # 206 is a Range hit; classify() only knows about 200.
            outcome = self.source.classify(200 if status == 206 else status, body)

            if outcome == Outcome.AUTH:
                raise CredentialsError(
                    f"{self.source.name}: the server rejected the request for tile "
                    f"{zoom}/{x}/{y} with HTTP {status}. For TDR this means the "
                    f"CloudFront signed cookies are wrong or expired -- refresh them "
                    f"and re-run. Nothing has been recorded as missing."
                )
            if outcome == Outcome.RETRY:
                if attempt > self.options.retries:
                    return Verdict.REFUSED
                retry_after = response.headers.get("Retry-After", "")
                delay = (
                    float(retry_after) if retry_after.isdigit()
                    else self.options.backoff_base * 2 ** (attempt - 1)
                )
                await self.limiter.penalise()
                await asyncio.sleep(min(delay, 30))
                continue
            return Verdict.PRESENT if outcome == Outcome.OK else Verdict.ABSENT

    async def _resolve(self, zoom: int, x: int, y: int) -> bool:
        verdict = await self._ask(zoom, x, y)
        if verdict == Verdict.REFUSED:
            self.refusals += 1
            self._run_of_refusals += 1
            if self._run_of_refusals >= self.options.refusal_limit:
                raise TraceRefused(
                    f"{self.source.name}: {self._run_of_refusals} requests in a row were "
                    f"refused or timed out. A refusal is not an absent tile, so carrying "
                    f"on would draw the border around the outage. Try again later, or "
                    f"with a lower --rps and --concurrency."
                )
        else:
            self._run_of_refusals = 0
        present = verdict == Verdict.PRESENT
        self._cache[(x, y)] = present
        return present

    # -- the cached, deduplicated question ---------------------------------

    async def present(self, zoom: int, x: int, y: int) -> bool:
        if self.limits is not None and not _inside(self.limits, x, y):
            return False                      # outside the window: never asked
        key = (x, y)
        if key in self._cache:
            return self._cache[key]
        if self.capped:
            return False
        task = self._inflight.get(key)
        if task is None:
            if self.requests >= self.options.max_requests:
                self.capped = True
                return False
            task = asyncio.ensure_future(self._resolve(zoom, x, y))
            self._inflight[key] = task
            try:
                return await task
            finally:
                self._inflight.pop(key, None)
        return await task

    async def many(self, zoom: int, points: Sequence[tuple[int, int]]) -> list[bool]:
        """Ask about several tiles at once, answering in the order given.

        Order matters to every caller: the walk takes the first filled neighbour
        going round the ring, and "first" has to keep meaning first however the
        responses happen to land.
        """
        if self.batch is not None:
            await self._prefetch(zoom, points)
        return list(await asyncio.gather(*(self.present(zoom, x, y) for x, y in points)))

    async def _prefetch(self, zoom: int, points: Sequence[tuple[int, int]]) -> None:
        """Resolve everything not already known in as few requests as possible.

        Only tiles that are neither cached nor already in flight are asked
        about, so the batch shrinks as the walk re-treads ground -- which it
        does constantly, since consecutive border tiles share most of their
        ring.
        """
        wanted: list[tuple[int, int]] = []
        seen: set[tuple[int, int]] = set()
        for x, y in points:
            key = (x, y)
            if key in seen or key in self._cache or key in self._inflight:
                continue
            if self.limits is not None and not _inside(self.limits, x, y):
                continue
            seen.add(key)
            wanted.append(key)
        if not wanted:
            return

        assert self.batch is not None
        for start in range(0, len(wanted), self.batch.limit):
            chunk = wanted[start:start + self.batch.limit]
            if self.capped:
                return
            if self.requests >= self.options.max_requests:
                self.capped = True
                return
            await self.limiter.acquire()
            self.requests += len(chunk)
            if self.on_request:
                for _ in chunk:
                    self.on_request()
            verdicts = await self.batch.check(self.client, zoom, chunk)
            for point, verdict in zip(chunk, verdicts):
                if verdict == Verdict.REFUSED:
                    self.refusals += 1
                    self._run_of_refusals += 1
                else:
                    self._run_of_refusals = 0
                self._cache[point] = verdict == Verdict.PRESENT
            if self._run_of_refusals >= self.options.refusal_limit:
                raise TraceRefused(
                    f"{self.source.name}: {self._run_of_refusals} tiles in a row came "
                    f"back refused from the batch endpoint. A refusal is not an absent "
                    f"tile -- for TDR this usually means the Worker's CloudFront "
                    f"cookies have expired."
                )

    async def recheck(self, zoom: int, x: int, y: int) -> bool:
        """Ask again, ignoring what the cache was told last time."""
        self._cache.pop((x, y), None)
        return await self.present(zoom, x, y)

    async def recheck_many(self, zoom: int, points: Sequence[tuple[int, int]]) -> list[bool]:
        for point in points:
            self._cache.pop(point, None)
        return await self.many(zoom, points)

    @property
    def ask_width(self) -> int:
        """How many tiles it is worth asking about in one go.

        Probing one tile per request, this is the concurrency -- more in flight
        than that just queues. Through a batch endpoint the unit is the batch,
        so anywhere without an inherent limit should fill one.
        """
        if self.batch is not None:
            return max(1, self.batch.limit)
        return max(1, self.options.concurrency)

    def recorded_absent(self) -> list[tuple[int, int]]:
        return [point for point, present in self._cache.items() if not present]

    def reset(self, limits: TileBounds) -> None:
        self._cache.clear()
        self._inflight.clear()
        self._run_of_refusals = 0
        self.capped = False
        self.limits = limits


def _inside(box: TileBounds, x: int, y: int) -> bool:
    return box.min_x <= x <= box.max_x and box.min_y <= y <= box.max_y


# ---------------------------------------------------------------------------
# results
# ---------------------------------------------------------------------------


@dataclass
class Region:
    """One closed border and everything it encloses."""

    box: TileBounds
    border: set[tuple[int, int]]
    #: ``(y, x_from, x_to)``, inclusive, one per contiguous run.
    spans: list[tuple[int, int, int]]
    covered: int
    #: Sides of the search window the border is pressed against, if any.
    clipped: tuple[str, ...] = ()

    @property
    def rectangle(self) -> bool:
        """Is the footprint the whole of its bounding box?"""
        return self.covered == self.box.count


@dataclass
class ZoomTrace:
    zoom: int
    declared: TileBounds
    regions: list[Region]
    requests: int
    margin: int
    capped: bool = False
    #: Tiles called absent that answered when asked again.
    flipped: list[tuple[int, int]] = field(default_factory=list)
    #: Tiles seen present that stopped answering.
    dead: list[tuple[int, int]] = field(default_factory=list)

    @property
    def clipped(self) -> tuple[str, ...]:
        seen: list[str] = []
        for region in self.regions:
            for edge in region.clipped:
                if edge not in seen:
                    seen.append(edge)
        return tuple(seen)

    @property
    def complete(self) -> bool:
        """Did the measurement finish, regardless of how tidy the answer is?

        Being a rectangle is a property of the map; being complete is a property
        of the walk. An irregular footprint traced all the way round inside its
        window and its budget is a finished measurement that happens to report
        an interesting shape.
        """
        return not (self.capped or self.clipped or self.flipped or self.dead)

    @property
    def spans(self) -> list[tuple[int, int, int]]:
        return merge_spans(region.spans for region in self.regions)

    @property
    def covered(self) -> int:
        """Tiles enclosed, counting ground shared by two borders only once.

        A shape with a hole gets walked from both sides -- the outer rim and the
        rim of the hole -- and adding up what each encloses counts the overlap
        twice.
        """
        return sum(x_to - x_from + 1 for _y, x_from, x_to in self.spans)

    @property
    def box(self) -> TileBounds | None:
        if not self.regions:
            return None
        return TileBounds(
            min(r.box.min_x for r in self.regions),
            max(r.box.max_x for r in self.regions),
            min(r.box.min_y for r in self.regions),
            max(r.box.max_y for r in self.regions),
        )

    @property
    def rectangle(self) -> bool:
        box = self.box
        return box is not None and len(self.regions) == 1 and self.covered == box.count

    def runs(self) -> list[tuple[int, int, int, int]]:
        """Spans with identical consecutive rows collapsed: ``(y0, y1, x0, x1)``.

        These footprints are a few stacked rectangles, so a 425-row shape
        collapses to two entries. A row covered by several separate runs simply
        contributes one entry per run over the same y range.
        """
        return group_spans(self.spans)

    def describe(self) -> str:
        if self.capped:
            return f"stopped at the {self.requests:,}-request cap; the answer is incomplete"
        if self.flipped:
            x, y = self.flipped[0]
            return (
                f"unreliable -- {len(self.flipped)} tile(s) recorded as absent answered "
                f"when asked again (e.g. {x},{y}); the server was refusing requests"
            )
        if self.dead:
            x, y = self.dead[0]
            return (
                f"unreliable -- the server stopped serving {len(self.dead)} tile(s) it had "
                f"already served (e.g. {x},{y}); it is refusing requests now"
            )
        if self.clipped:
            return (
                f"reached the {' and '.join(self.clipped)} edge of the search window; "
                f"the imagery runs past it, so this box is a floor. Re-run with a "
                f"margin above {self.margin}"
            )
        if not self.regions:
            return "no imagery at this zoom"
        if self.rectangle:
            return "a rectangle"
        box = self.box
        assert box is not None
        empty = box.count - self.covered
        parts = []
        if len(self.regions) > 1:
            parts.append(f"{len(self.regions)} separate regions")
        parts.append(
            f"irregular -- {empty:,} tile(s) inside the bounding box are not served"
        )
        return ", ".join(parts)


# ---------------------------------------------------------------------------
# geometry
# ---------------------------------------------------------------------------


def fill_region(border: Iterable[tuple[int, int]], box: TileBounds) -> tuple[list[tuple[int, int, int]], int]:
    """Everything the border encloses, as row runs.

    Flooded inward from outside the box rather than taken as the min and max x
    on each row. On an L, a C or a bitten corner those are not the same answer:
    min/max bridges the concavity and claims tiles that are not there. An
    8-connected border is watertight against a 4-connected flood, so whatever
    the flood cannot reach is genuinely enclosed.
    """
    width, height = box.width, box.height
    stride, rows = width + 2, height + 2       # one cell of padding to flood from
    wall = bytearray(stride * rows)
    for x, y in border:
        wall[(y - box.min_y + 1) * stride + (x - box.min_x + 1)] = 1

    outside = bytearray(stride * rows)
    outside[0] = 1
    stack = [0]
    while stack:
        index = stack.pop()
        cx, cy = index % stride, index // stride
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = cx + dx, cy + dy
            if not (0 <= nx < stride and 0 <= ny < rows):
                continue
            j = ny * stride + nx
            if outside[j] or wall[j]:
                continue
            outside[j] = 1
            stack.append(j)

    spans: list[tuple[int, int, int]] = []
    count = 0
    for row in range(height):
        run = -1
        for column in range(width + 1):
            held = column < width and not outside[(row + 1) * stride + (column + 1)]
            if held and run == -1:
                run = column
            elif not held and run != -1:
                spans.append((box.min_y + row, box.min_x + run, box.min_x + column - 1))
                count += column - run
                run = -1
    return spans, count


def merge_spans(lists: Iterable[Iterable[tuple[int, int, int]]]) -> list[tuple[int, int, int]]:
    """Union of several regions' runs, so overlapping ground is counted once."""
    by_row: dict[int, list[tuple[int, int]]] = {}
    for spans in lists:
        for y, x_from, x_to in spans:
            by_row.setdefault(y, []).append((x_from, x_to))

    merged: list[tuple[int, int, int]] = []
    for y in sorted(by_row):
        runs = sorted(by_row[y])
        start, end = runs[0]
        for a, b in runs[1:]:
            if a <= end + 1:               # touching or overlapping: one run
                end = max(end, b)
            else:
                merged.append((y, start, end))
                start, end = a, b
        merged.append((y, start, end))
    return merged


def group_spans(spans: Sequence[tuple[int, int, int]]) -> list[tuple[int, int, int, int]]:
    by_row: dict[int, list[tuple[int, int]]] = {}
    for y, x_from, x_to in spans:
        by_row.setdefault(y, []).append((x_from, x_to))

    groups: list[tuple[int, int, int, int]] = []
    run_start: int | None = None
    run_end = 0
    signature: tuple[tuple[int, int], ...] = ()

    def flush() -> None:
        if run_start is None:
            return
        for x_from, x_to in signature:
            groups.append((run_start, run_end, x_from, x_to))

    for y in sorted(by_row):
        current = tuple(sorted(by_row[y]))
        if signature == current and run_start is not None and y == run_end + 1:
            run_end = y
            continue
        flush()
        run_start, run_end, signature = y, y, current
    flush()
    return groups


# ---------------------------------------------------------------------------
# the walk
# ---------------------------------------------------------------------------


def _ring_index(px: int, py: int, qx: int, qy: int) -> int:
    for index, (dx, dy) in enumerate(RING):
        if px + dx == qx and py + dy == qy:
            return index
    return 0


async def _walk(oracle: TileOracle, zoom: int, seed: tuple[int, int]) -> list[tuple[int, int]]:
    """Moore-neighbour tracing from a tile known to be on or inside the border.

    Rotate around the current tile starting from the empty one we arrived from,
    step to the first filled neighbour, repeat. Jacob's criterion stops it: the
    same (tile, arrived-from) pair means the loop has closed.
    """
    width = oracle.ask_width
    sx, sy = seed
    # West until the tile west is empty -- that is a border tile, and starting
    # anywhere else risks tracing off in the wrong direction. Asked a run at a
    # time; the tiles past the edge cost one batch and land in the cache the
    # walk is about to use anyway.
    while True:
        span = min(width, 32)
        answers = await oracle.many(zoom, [(sx - i, sy) for i in range(1, span + 1)])
        gap = answers.index(False) if False in answers else -1
        sx -= span if gap == -1 else gap
        if gap != -1 or oracle.capped:
            break

    bx, by = sx - 1, sy
    px, py = sx, sy
    contour = [(sx, sy)]
    states: set[tuple[int, int, int, int]] = set()

    while not oracle.capped:
        state = (px, py, bx, by)
        if state in states:
            break
        states.add(state)

        start = _ring_index(px, py, bx, by)
        ring = [
            ((start + k) % 8, px + RING[(start + k) % 8][0], py + RING[(start + k) % 8][1])
            for k in range(1, 9)
        ]
        # Ask about several neighbours together rather than waiting out a round
        # trip before asking about the next. Still consumed in ring order, so
        # the tile chosen is the same one -- speculation changes what is asked,
        # never what is picked -- and consecutive border tiles share most of
        # their ring, so the extra asks are largely reclaimed from the cache.
        look = min(8, max(oracle.options.concurrency, 8 if oracle.batch else 1))
        moved = False
        for base in range(0, 8, look):
            slice_ = ring[base:base + look]
            answers = await oracle.many(zoom, [(cx, cy) for _j, cx, cy in slice_])
            if True not in answers:
                continue
            j, cx, cy = slice_[answers.index(True)]
            previous = (j + 7) % 8
            bx, by = px + RING[previous][0], py + RING[previous][1]
            px, py = cx, cy
            contour.append((px, py))
            moved = True
            break
        if not moved:
            break                              # a single isolated tile
    return contour


async def _find_seed(
    oracle: TileOracle,
    zoom: int,
    declared: TileBounds,
    done: Sequence[TileBounds],
) -> tuple[int, int] | None:
    """A filled tile no walk so far accounts for, on a lattice that gets finer.

    This has to probe rather than sift the cache: a region the walks never went
    near has no cached tiles at all, and consulting the cache alone would report
    one region for a map that has several. Ground already walked is skipped by
    bounding box, which is what keeps the sweep affordable -- and is also why a
    region sitting wholly inside another's box goes unreported.
    """
    width = oracle.ask_width
    for target in SEED_LATTICES:
        side = target ** 0.5
        step_x = max(1, -(-declared.width // int(side)))
        step_y = max(1, -(-declared.height // int(side)))
        batch: list[tuple[int, int]] = []

        async def drain() -> tuple[int, int] | None:
            if not batch:
                return None
            points, batch[:] = list(batch), []
            answers = await oracle.many(zoom, points)
            return points[answers.index(True)] if True in answers else None

        for y in range(declared.min_y, declared.max_y + 1, step_y):
            for x in range(declared.min_x, declared.max_x + 1, step_x):
                if oracle.capped:
                    return None
                if any(_inside(box, x, y) for box in done):
                    continue
                batch.append((x, y))
                if len(batch) >= width:
                    found = await drain()
                    if found:
                        return found
        found = await drain()
        if found:
            return found
    return None


async def _audit(
    oracle: TileOracle, zoom: int, regions: Sequence[Region]
) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """Tell refusals apart from absences, after the fact.

    Guessing from the shape cannot do it -- a genuinely small region and a
    truncated walk leave the same border -- so ask again. Two questions, because
    a throttle that has eased and one still running fail differently: do
    absences the border rests on answer on a second ask, and do tiles already
    served still serve? The second catches a refusal that is still going, where
    re-asking about an absence would simply be refused again.
    """
    suspects: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()
    known: list[tuple[int, int]] = []
    for region in regions:
        for x, y in region.border:
            known.append((x, y))
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                point = (x + dx, y + dy)
                if point in seen:
                    continue
                if oracle._cache.get(point) is False:
                    seen.add(point)
                    suspects.append(point)
    if not suspects:
        suspects = oracle.recorded_absent()

    probes = _spread(suspects, oracle.options.audit_absences)
    controls = _spread(known, oracle.options.audit_controls)
    answered = await oracle.recheck_many(zoom, probes)
    alive = await oracle.recheck_many(zoom, controls)
    return (
        [point for point, hit in zip(probes, answered) if hit],
        [point for point, hit in zip(controls, alive) if not hit],
    )


def _spread(points: Sequence[tuple[int, int]], want: int) -> list[tuple[int, int]]:
    """An evenly spaced sample rather than the first few.

    Refusals arrive in bursts, so corrupted verdicts sit together; a clustered
    sample could miss the burst entirely or land wholly inside it.
    """
    count = min(len(points), want)
    if count <= 0:
        return []
    step = len(points) / count
    return [points[int(index * step)] for index in range(count)]


async def trace_zoom(
    oracle: TileOracle, zoom: int, declared: TileBounds, options: TraceOptions
) -> ZoomTrace:
    before = oracle.requests
    margin = max(0, options.margin)
    limits = TileBounds(
        declared.min_x - margin, declared.max_x + margin,
        declared.min_y - margin, declared.max_y + margin,
    )
    oracle.reset(limits)

    regions: list[Region] = []
    walked: list[TileBounds] = []
    seen: set[tuple[int, int, int, int, int]] = set()

    for _attempt in range(options.max_regions * 8):
        if oracle.capped or len(regions) >= options.max_regions:
            break
        seed = await _find_seed(oracle, zoom, declared, walked)
        if seed is None:
            break
        contour = await _walk(oracle, zoom, seed)
        if not contour:
            continue
        border = set(contour)
        box = TileBounds(
            min(x for x, _y in contour), max(x for x, _y in contour),
            min(y for _x, y in contour), max(y for _x, y in contour),
        )
        walked.append(box)
        # A seed elsewhere in the same region produces the same walk. Recognise
        # it rather than reporting one region several times.
        key = (box.min_x, box.max_x, box.min_y, box.max_y, len(border))
        if key in seen:
            continue
        seen.add(key)

        spans, covered = fill_region(border, box)
        clipped = tuple(
            edge for edge, hit in (
                ("minX", box.min_x <= limits.min_x),
                ("maxX", box.max_x >= limits.max_x),
                ("minY", box.min_y <= limits.min_y),
                ("maxY", box.max_y >= limits.max_y),
            ) if hit
        )
        regions.append(Region(box, border, spans, covered, clipped))

    flipped: list[tuple[int, int]] = []
    dead: list[tuple[int, int]] = []
    if not oracle.capped:
        flipped, dead = await _audit(oracle, zoom, regions)

    return ZoomTrace(
        zoom=zoom,
        declared=declared,
        regions=regions,
        requests=oracle.requests - before,
        margin=margin,
        capped=oracle.capped,
        flipped=flipped,
        dead=dead,
    )


async def trace(
    source: TileSource,
    zooms: Sequence[tuple[int, TileBounds]],
    options: TraceOptions | None = None,
    *,
    client: httpx.AsyncClient | None = None,
    on_zoom_done: Callable[[ZoomTrace], None] | None = None,
    on_request: Callable[[], None] | None = None,
    batch: BatchProbe | None = None,
) -> list[ZoomTrace]:
    options = options or TraceOptions()
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(options.timeout),
            limits=httpx.Limits(max_connections=max(1, options.concurrency)),
            follow_redirects=True,
        )
    oracle = TileOracle(client, source, options, on_request=on_request, batch=batch)
    results: list[ZoomTrace] = []
    try:
        for zoom, declared in zooms:
            result = await trace_zoom(oracle, zoom, declared, options)
            results.append(result)
            if on_zoom_done:
                on_zoom_done(result)
    finally:
        if owns_client:
            await client.aclose()
    return results


def estimate_requests(zooms: Sequence[tuple[int, TileBounds]]) -> int:
    """Roughly what a trace will cost, for telling someone before they start.

    The walk pays about a couple of probes per border tile once the neighbour
    cache is warm, plus the seed lattice. Good enough to answer "will this
    finish before the credentials expire?".
    """
    total = 0
    for _zoom, bounds in zooms:
        perimeter = 2 * bounds.width + 2 * bounds.height
        total += int(perimeter * 2.2) + 80
    return total


def coverage_payload(traces: Iterable[ZoomTrace]) -> dict:
    """The measured footprints, in the shape ``tools/measured-coverage.json`` uses."""
    zooms: dict[str, dict] = {}
    for item in traces:
        box = item.box
        entry: dict = {
            "box": box.as_dict() if box else None,
            "tiles": item.covered,
            "shape": (
                "empty" if not item.regions
                else "rectangle" if item.rectangle
                else "irregular"
            ),
        }
        if not item.complete:
            entry["incomplete"] = item.describe()
        if item.regions and not item.rectangle:
            entry["runs"] = [list(run) for run in item.runs()]
        zooms[str(item.zoom)] = entry
    return zooms
