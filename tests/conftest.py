from __future__ import annotations

from pathlib import Path

import pytest

from tilearc.config import ParkRepository, DirConfigSource

FIXTURES = Path(__file__).parent / "fixtures" / "parks"


@pytest.fixture(scope="session")
def repo() -> ParkRepository:
    return ParkRepository(DirConfigSource(FIXTURES))


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return FIXTURES
