"""Archiving every version of a park in one pass, coordinate by coordinate.

The chain walks one tile position through all of a park's versions before
moving to the next position. These run it against a fake CDN that serves a
small park with a real shape of history: tiles that never change, a tile that
changes once, a tile that changes and reverts, and an area the early versions
did not cover at all.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager

import httpx
import pytest

from tilearc.chain import (
    ChainDownloader,
    ChainPlan,
    ChainRequest,
    ChainWriter,
    build_chain_sources,
    run_chain,
)
from tilearc.config import ParkConfig, TileBounds, VersionEntry
from tilearc.downloader import DownloadOptions
from tilearc.library import Catalogue
from tilearc.plan import JobPlan, ZoomPlan
from tilearc.progress import Progress
from tilearc.urls import TileSource

TEMPLATE = "https://cdn.test/{code}/{z}/{x}/{y}.jpg"

#: Four coordinates over three versions. Between them they cover every case
#: the chain has to get right.
WORLD = {
    # unchanged throughout: stored once, shared twice
    (11, 0, 0): {"1": b"steady", "2": b"steady", "3": b"steady"},
    # changes once: two stored, one shared
    (11, 0, 1): {"1": b"early", "2": b"early", "3": b"LATER"},
    # changes and reverts: the third must match the first, not be stored again
    (11, 1, 0): {"1": b"there", "2": b"GONE!", "3": b"there"},
    # the park grew: absent from the oldest version
    (11, 1, 1): {"2": b"new-ar", "3": b"new-ar"},
}
VERSIONS = ["1", "2", "3"]


def _park() -> ParkConfig:
    return ParkConfig(
        park_id="test", label="Test Park", tile_template=TEMPLATE,
        min_zoom=11, max_zoom=11, y_scheme="xyz",
        bounds_by_zoom={11: TileBounds(0, 1, 0, 1)},
    )


def _plan(versions=VERSIONS) -> ChainPlan:
    park = _park()
    coordinates = JobPlan(
        park=park, version=VersionEntry(code=versions[-1]),
        zooms=[ZoomPlan(11, TileBounds(0, 1, 0, 1))], modes=[],
    )
    return ChainPlan(
        park=park,
        versions=[VersionEntry(code=v, label=f"v{v}") for v in versions],
        coordinates=coordinates,
    )


def _sources(plan):
    """A source per version, each pointing at that version's path."""
    def make(entry, mode):
        return TileSource(
            name=f"test {entry.code}",
            template=TEMPLATE.replace("{code}", entry.code),
        )

    return build_chain_sources(plan, make)


@contextmanager
def _faked_cdn(handler):
    """Serve every request from `handler`, as the downloader tests do."""
    import tilearc.downloader as module

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    class Patched(real_client):
        def __init__(self, **kw):
            kw.pop("limits", None)
            kw["transport"] = transport
            super().__init__(**kw)

    module.httpx.AsyncClient = Patched
    try:
        yield
    finally:
        module.httpx.AsyncClient = real_client


def _handler(served: list | None = None, fail: set | None = None):
    def handle(request: httpx.Request) -> httpx.Response:
        parts = request.url.path.strip("/").split("/")
        code, z, x, y = parts[0], int(parts[1]), int(parts[2]), int(parts[3].split(".")[0])
        if served is not None:
            served.append((code, z, x, y))
        if fail and (code, z, x, y) in fail:
            return httpx.Response(500)
        body = WORLD.get((z, x, y), {}).get(code)
        if body is None:
            return httpx.Response(404)
        return httpx.Response(200, content=body)

    return handle


def _run(tmp_path, plan=None, handler=None, served=None, **kwargs):
    plan = plan or _plan()
    request = ChainRequest(
        plan=plan,
        sources=_sources(plan),
        root=tmp_path,
        options=DownloadOptions(concurrency=2, rps=0, retries=1, backoff_base=0.001),
        **kwargs,
    )
    progress = Progress(plan.total_requests, enabled=False)
    with _faked_cdn(handler or _handler(served)):
        return asyncio.run(run_chain(request, progress))


# ---------------------------------------------------------------------------
# what it fetches
# ---------------------------------------------------------------------------


def test_every_version_of_every_coordinate_is_asked_for(tmp_path):
    served = []
    outcome = _run(tmp_path, served=served)

    assert outcome.complete
    assert len(served) == 4 * 3, "four coordinates, three versions"
    assert outcome.result.fetched == 11   # one is absent from v1
    assert outcome.result.missing == 1


def test_a_coordinate_is_walked_through_its_versions_in_order(tmp_path):
    """The whole point: 1/... then 2/... then 3/... before the next tile."""
    served = []
    _run(tmp_path, served=served, plan=_plan())

    # With concurrency the coordinates interleave, so check the order within
    # each one rather than across the run.
    per_coordinate: dict[tuple, list[str]] = {}
    for code, z, x, y in served:
        per_coordinate.setdefault((z, x, y), []).append(code)
    for coordinate, codes in per_coordinate.items():
        assert codes == VERSIONS, f"{coordinate} was not walked oldest first: {codes}"


def test_requests_are_the_same_count_either_way_round(tmp_path):
    """A chain is not fewer requests, and the plan says so up front."""
    plan = _plan()
    assert plan.total_requests == plan.tiles_per_version * len(plan.versions)
    served = []
    _run(tmp_path, served=served, plan=plan)
    assert len(served) == plan.total_requests


# ---------------------------------------------------------------------------
# what it stores
# ---------------------------------------------------------------------------


def test_an_unchanged_tile_is_stored_once_and_shared(tmp_path):
    _run(tmp_path)
    catalogue = Catalogue(tmp_path)
    try:
        rows = {
            v: catalogue.resolve("test", v, 11, 0, 0)
            for v in VERSIONS
        }
    finally:
        catalogue.close()

    assert all(path is not None for path in rows.values()), "every version has it"
    assert len({str(p) for p in rows.values()}) == 1, "and they all point at one file"
    assert "test/1/" in str(rows["3"]).replace("\\", "/"), "stored under the oldest"


def test_a_changed_tile_is_stored_again(tmp_path):
    _run(tmp_path)
    catalogue = Catalogue(tmp_path)
    try:
        early = catalogue.resolve("test", "2", 11, 0, 1)
        later = catalogue.resolve("test", "3", 11, 0, 1)
    finally:
        catalogue.close()

    assert early != later
    assert later.read_bytes() == b"LATER"
    assert early.read_bytes() == b"early"


def test_a_tile_that_reverts_points_back_at_the_first_copy(tmp_path):
    """v3 matches v1 again; storing a third copy would be the bug."""
    _run(tmp_path)
    catalogue = Catalogue(tmp_path)
    try:
        first = catalogue.resolve("test", "1", 11, 1, 0)
        third = catalogue.resolve("test", "3", 11, 1, 0)
        stored = list(catalogue.db.execute(
            "SELECT DISTINCT stored_in FROM tiles "
            "WHERE park='test' AND z=11 AND x=1 AND y=0 ORDER BY stored_in"))
    finally:
        catalogue.close()

    assert first == third
    assert [row["stored_in"] for row in stored] == ["1", "2"]


def test_a_version_that_never_had_the_tile_is_recorded_as_absent(tmp_path):
    _run(tmp_path)
    catalogue = Catalogue(tmp_path)
    try:
        assert catalogue.is_absent("test", "1", "", 11, 1, 1)
        assert not catalogue.is_absent("test", "2", "", 11, 1, 1)
        assert catalogue.resolve("test", "1", 11, 1, 1) is None
        assert catalogue.resolve("test", "2", 11, 1, 1) is not None
    finally:
        catalogue.close()


def test_when_a_corner_of_the_park_first_appeared(tmp_path):
    """What the absences are for, beyond resuming."""
    _run(tmp_path)
    catalogue = Catalogue(tmp_path)
    try:
        assert catalogue.first_seen("test", "", 11, 1, 1) == "2"
        assert catalogue.first_seen("test", "", 11, 0, 0) == "1"
    finally:
        catalogue.close()


def test_each_touched_version_gets_its_own_manifest(tmp_path):
    _run(tmp_path)
    for version in VERSIONS:
        assert (tmp_path / "test" / version / "manifest.json").is_file()


# ---------------------------------------------------------------------------
# resuming
# ---------------------------------------------------------------------------


def test_a_second_run_asks_for_nothing(tmp_path):
    _run(tmp_path)
    served = []
    outcome = _run(tmp_path, served=served)

    assert served == [], "everything was already held or known absent"
    assert outcome.resumed
    assert outcome.result.skipped == 4 * 3


def test_an_absence_is_not_asked_for_again(tmp_path):
    """Without the absent table this is the request that repeats forever."""
    _run(tmp_path)
    served = []
    _run(tmp_path, served=served)
    assert ("1", 11, 1, 1) not in served


def test_a_coordinate_interrupted_mid_chain_resumes_within_itself(tmp_path):
    """Stopping inside a coordinate must not restart that coordinate.

    The state DB only knows about whole coordinates; it is the catalogue that
    remembers the versions already done inside one.
    """
    plan = _plan()
    writer = ChainWriter(tmp_path, plan)
    writer.open()
    writer.write("1", 11, 0, 0, "", b"steady")   # as if the run stopped here
    writer.finalize({"tool": "test"}, complete=False)
    writer.close()

    served = []
    _run(tmp_path, served=served, plan=plan)
    assert ("1", 11, 0, 0) not in served, "v1 of this tile was already held"
    assert ("2", 11, 0, 0) in served


def test_adding_a_version_is_a_different_chain(tmp_path):
    """Resuming across a changed version list would leave the new one empty."""
    assert _plan(["1", "2", "3"]).fingerprint() != _plan(["1", "2", "3", "4"]).fingerprint()


def test_a_failed_version_leaves_the_coordinate_pending(tmp_path):
    """A coordinate is only finished when every version of it is settled."""
    served = []
    outcome = _run(
        tmp_path,
        handler=_handler(served, fail={("2", 11, 0, 0)}),
    )
    assert outcome.result.failed >= 1
    assert not outcome.complete

    # The coordinate must come round again next run, and the versions that did
    # succeed must not.
    again = []
    _run(tmp_path, served=again)
    assert ("2", 11, 0, 0) in again, "the failure is retried"
    assert ("1", 11, 0, 0) not in again, "the success is not"


# ---------------------------------------------------------------------------
# the plan
# ---------------------------------------------------------------------------


def test_the_plan_counts_the_whole_job(tmp_path):
    plan = _plan()
    assert plan.tiles_per_version == 4
    assert plan.total_requests == 12


@pytest.mark.parametrize("versions", [["1"], ["1", "2"], VERSIONS])
def test_a_chain_of_any_length_archives_every_version_it_names(tmp_path, versions):
    outcome = _run(tmp_path, plan=_plan(versions))
    catalogue = Catalogue(tmp_path)
    try:
        archived = {row["version"] for row in catalogue.versions("test")}
    finally:
        catalogue.close()
    assert archived == set(versions)
    assert outcome.complete
