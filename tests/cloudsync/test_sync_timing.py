"""Positive controls for :mod:`apps.sync_hub.sync_timing` wired through ``run_sync``."""

from __future__ import annotations

import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from apps.sync_hub import client
from tests.cloudsync.test_hub_sync import _TestClientTransport, _seed_common_track, _sync

pytest_plugins = ["tests.cloudsync.test_hub_sync"]


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
