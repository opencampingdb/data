"""Shared pytest fixtures for the pipeline tests."""

from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def v1_osm() -> Path:
    """First version of the hand-written OSM file."""
    return FIXTURES / "v1.osm"


@pytest.fixture
def v2_osm() -> Path:
    """Second version: one object changed, one gone, one new."""
    return FIXTURES / "v2.osm"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """An empty data repository with the two data folders in place."""
    (tmp_path / "places").mkdir()
    (tmp_path / "reviews").mkdir()
    return tmp_path
