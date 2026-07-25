from __future__ import annotations

import pytest

from tilearc.errors import JobMismatchError
from tilearc.state import STATUS_DONE, STATUS_FAILED, STATUS_MISSING, JobState, _pack


def test_new_job_is_not_a_resume(tmp_path):
    with JobState(tmp_path / "s.sqlite") as state:
        assert state.bind_job("abc", {"park": "wdw"}) is False


def test_reopening_the_same_job_resumes(tmp_path):
    path = tmp_path / "s.sqlite"
    with JobState(path) as state:
        state.bind_job("abc", {"park": "wdw"})
        state.record(11, 555, 851, "", STATUS_DONE, size=900)
    with JobState(path) as state:
        assert state.bind_job("abc", {"park": "wdw"}) is True
        assert state.counts() == {STATUS_DONE: 1}


def test_a_different_job_is_refused(tmp_path):
    path = tmp_path / "s.sqlite"
    with JobState(path) as state:
        state.bind_job("abc", {"park": "wdw"})
    with JobState(path) as state:
        with pytest.raises(JobMismatchError, match="different job"):
            state.bind_job("xyz", {"park": "dlr"})


def test_restart_discards_prior_state(tmp_path):
    path = tmp_path / "s.sqlite"
    with JobState(path) as state:
        state.bind_job("abc", {})
        state.record(11, 1, 1, "", STATUS_DONE, size=10)
    with JobState(path) as state:
        assert state.bind_job("xyz", {}, allow_restart=True) is False
        assert state.counts() == {}


def test_completed_covers_done_and_missing_but_not_failed(tmp_path):
    with JobState(tmp_path / "s.sqlite") as state:
        state.bind_job("abc", {})
        state.record(11, 1, 1, "", STATUS_DONE, size=10)
        state.record(11, 1, 2, "", STATUS_MISSING)
        state.record(11, 1, 3, "", STATUS_FAILED)
        done = state.completed("")
    assert _pack(11, 1, 1) in done
    assert _pack(11, 1, 2) in done          # never re-probe a known-absent tile
    assert _pack(11, 1, 3) not in done      # failures are retried


def test_modes_are_tracked_separately(tmp_path):
    with JobState(tmp_path / "s.sqlite") as state:
        state.bind_job("abc", {})
        state.record(16, 1, 1, "daytime", STATUS_DONE, size=5)
        assert _pack(16, 1, 1) in state.completed("daytime")
        assert _pack(16, 1, 1) not in state.completed("nighttime")


def test_rerecording_a_tile_accumulates_attempts(tmp_path):
    with JobState(tmp_path / "s.sqlite") as state:
        state.bind_job("abc", {})
        state.record(11, 1, 1, "", STATUS_FAILED, attempts=3)
        state.record(11, 1, 1, "", STATUS_DONE, size=42, attempts=2)
        state.flush()
        row = state.conn.execute(
            "SELECT status, size, attempts FROM tiles WHERE z=11 AND x=1 AND y=1"
        ).fetchone()
    assert row == (STATUS_DONE, 42, 5)


def test_total_bytes_counts_only_downloaded_tiles(tmp_path):
    with JobState(tmp_path / "s.sqlite") as state:
        state.bind_job("abc", {})
        state.record(11, 1, 1, "", STATUS_DONE, size=100)
        state.record(11, 1, 2, "", STATUS_DONE, size=250)
        state.record(11, 1, 3, "", STATUS_MISSING, size=0)
        assert state.total_bytes() == 350


def test_pack_is_collision_free_at_realistic_coordinates():
    coords = [(20, 286528, 437504), (20, 286529, 437504), (20, 286528, 437505), (19, 1, 1)]
    assert len({_pack(*c) for c in coords}) == len(coords)


def test_failures_are_listable(tmp_path):
    with JobState(tmp_path / "s.sqlite") as state:
        state.bind_job("abc", {})
        state.record(11, 5, 5, "", STATUS_FAILED, attempts=6)
        assert state.failures() == [(11, 5, 5, "", 6)]
