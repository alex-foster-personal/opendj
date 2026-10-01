"""Positive controls for :mod:`apps.sync_hub.sync_timing` wired through ``run_sync``."""

from __future__ import annotations

import time
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.sync_hub import client, service
from apps.sync_hub.sync_timing import PhaseTimer
from tests.cloudsync.test_hub_sync import _seed_common_track, _sync, _TestClientTransport


@pytest.fixture(autouse=True)
def _no_hub_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``MDT_IS_HUB`` from the developer's shell must not steer these tests."""
    monkeypatch.delenv("MDT_IS_HUB", raising=False)


@pytest.fixture
def hub_dir(tmp_path: Path) -> Path:
    """The hub's empty data dir. Guarantees as in ``test_hub_sync.py``."""
    return tmp_path / "hub"


@pytest.fixture
def spoke_a(tmp_path: Path) -> Path:
    """Spoke A's empty data dir. Guarantees as in ``test_hub_sync.py``."""
    return tmp_path / "spoke-a"


@pytest.fixture
def hub(hub_dir: Path) -> Iterator[_TestClientTransport]:
    """The real sync router on an empty hub DB. See ``test_hub_sync.hub``."""
    app = FastAPI()
    app.state.state_db_path = str(client.state_db_path(hub_dir))
    app.state.sync_hub_data_dir = str(hub_dir)
    app.state.sync_hub_machine_name = "hub"
    app.include_router(service.router, prefix="/api/v1")
    with TestClient(app) as http:
        yield _TestClientTransport(http)


class _DelayTransport:
    """Wrap a hub transport and sleep on selected calls."""

    __test__ = False

    def __init__(
        self,
        inner: _TestClientTransport,
        *,
        hello_delay_s: float = 0.0,
        digest_delay_s: float = 0.0,
    ) -> None:
        self._inner = inner
        self._hello_delay_s = hello_delay_s
        self._digest_delay_s = digest_delay_s

    def post(self, path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if self._hello_delay_s and path.endswith("/hello"):
            time.sleep(self._hello_delay_s)
        return self._inner.post(path, payload)

    def get(self, path: str, params: Mapping[str, str]) -> dict[str, Any]:
        if self._digest_delay_s and path.endswith("/digest"):
            time.sleep(self._digest_delay_s)
        return self._inner.get(path, params)


def test_hello_delay_increases_hello_and_total_timings(
    spoke_a: Path, hub: _TestClientTransport
) -> None:
    transport = _DelayTransport(hub, hello_delay_s=0.2)
    result = client.run_sync(
        spoke_a, "http://hub.invalid", transport=transport, name="spoke-a"
    )
    assert result.timings is not None
    assert result.timings.hello_s >= 0.2
    assert result.timings.total_s >= result.timings.hello_s


_TRANSACTION_CONTROL = frozenset({"BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE"})


@pytest.mark.requirement("PERF-CAPTURE-01")
def test_local_digest_delay_increases_local_digest_and_total_timings(
    spoke_a: Path, hub: _TestClientTransport
) -> None:
    """[if] local sync_digest is slowed [then] local_digest_s rises, [else stop]."""
    prior = _sync(spoke_a, hub, "spoke-a")
    delay_s = 0.2
    conn = state_db.open_rw(client.state_db_path(spoke_a))
    slept = False

    def on_statement(sql: str) -> None:
        nonlocal slept
        verb = sql.lstrip().split(None, 1)[0].upper()
        if verb in _TRANSACTION_CONTROL:
            return
        if not conn.in_transaction or slept:
            return
        slept = True
        time.sleep(delay_s)

    try:
        conn.set_trace_callback(on_statement)
        timer = PhaseTimer()
        client._digests(hub, conn, prior.machine_id, timer=timer)
        timings = timer.finish(prior, needs_full_offer_at_start=False)
    finally:
        conn.set_trace_callback(None)
        conn.close()

    assert timings.local_digest_s >= delay_s
    assert timings.total_s >= timings.local_digest_s


def test_digest_delay_increases_digest_timing(
    spoke_a: Path, hub: _TestClientTransport
) -> None:
    _sync(spoke_a, hub, "spoke-a")
    transport = _DelayTransport(hub, digest_delay_s=0.2)
    result = client.run_sync(
        spoke_a, "http://hub.invalid", transport=transport, name="spoke-a"
    )
    assert result.timings is not None
    assert result.timings.digest_s >= 0.2


def test_first_then_noop_classification(
    spoke_a: Path, hub: _TestClientTransport
) -> None:
    _seed_common_track((spoke_a,), "trk-timing-1")
    first = client.run_sync(spoke_a, "http://hub.invalid", transport=hub, name="spoke-a")
    assert first.timings is not None
    assert first.timings.kind == "first"

    second = client.run_sync(spoke_a, "http://hub.invalid", transport=hub, name="spoke-a")
    assert second.timings is not None
    assert second.timings.kind == "noop"
    assert second.pushed == 0
    assert second.pulled == 0
    assert second.rounds == 1
