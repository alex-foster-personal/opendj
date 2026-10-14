"""Genre suite fixtures: the local provider server lives in pipeline_support."""

from __future__ import annotations

import gc
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.genre_infer.pipeline_support import providers_live


@pytest.fixture(autouse=True)
def _close_leaked_connections(tmp_path: Path) -> Iterator[None]:
    """Close every sqlite3 connection a test left open, before its tmp dir is removed.

    A ``sqlite3.Connection`` holds a reference cycle (its statement cache is an
    ``lru_cache`` over its own bound method), so one a test never closed is freed
    only when the cyclic collector next runs, at some later test. Parametrized
    cases of a long-named test share ONE tmp path (pytest truncates the name and
    reuses the number once a passed case's dir is removed), and closing the last
    connection of a WAL database unlinks ``state.db-wal`` and ``state.db-shm`` by
    PATH: a late collection deleted the next case's files and its COMMIT failed
    with ``disk I/O error`` (measured Tue 13 Oct 2026: up to six live connections
    to one path, one red run in eight). Depending on ``tmp_path`` orders this
    teardown before the directory's removal.
    """
    yield
    gc.collect()


__all__ = ["providers_live"]
