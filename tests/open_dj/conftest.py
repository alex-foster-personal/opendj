"""Shared fixtures for tests/open_dj/."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURES_DIR: Path = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    """Absolute path to the JSON fixtures directory."""
    return FIXTURES_DIR


@pytest.fixture
def minimal_doc() -> dict:
    """Parsed minimal.open-dj.json (single track, ISRC path)."""
    with (FIXTURES_DIR / "minimal.open-dj.json").open("rb") as fh:
        return json.load(fh)


@pytest.fixture
def minimal_bytes() -> bytes:
    """Raw canonical bytes of minimal.open-dj.json."""
    return (FIXTURES_DIR / "minimal.open-dj.json").read_bytes()


@pytest.fixture
def full_doc() -> dict:
    """Parsed full.open-dj.json covering every entity type."""
    with (FIXTURES_DIR / "full.open-dj.json").open("rb") as fh:
        return json.load(fh)


@pytest.fixture
def full_bytes() -> bytes:
    """Raw canonical bytes of full.open-dj.json."""
    return (FIXTURES_DIR / "full.open-dj.json").read_bytes()


@pytest.fixture
def ext_doc() -> dict:
    """Parsed x_extension_roundtrip.open-dj.json."""
    with (FIXTURES_DIR / "x_extension_roundtrip.open-dj.json").open("rb") as fh:
        return json.load(fh)


@pytest.fixture
def invalid_wrapped_doc() -> dict:
    """Parsed invalid_wrapped_title.open-dj.json (should fail schema)."""
    with (FIXTURES_DIR / "invalid_wrapped_title.open-dj.json").open("rb") as fh:
        return json.load(fh)
