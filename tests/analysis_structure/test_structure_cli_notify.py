"""A sidecar the All-In-One CLI writes out of process refreshes a loaded deck (issue #6496).

The CLI POSTs the written ids to ``POST /api/v1/structure/sidecars-changed``,
which publishes ``library.changed`` kind ``sections`` on the engine hub, the
same event own-fast emits in process. Only the HTTP boundary
(``notify._post_json``) is swapped, for an in-process ``TestClient`` call to the
real route.

[if] the CLI writes sidecars while an engine runs [then] the engine publishes library.changed kind sections for those ids, [else stop].

-Claude
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.error import HTTPError

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.analysis_structure import __main__ as cli
from apps.analysis_structure import notify
from apps.shared import events
from apps.webui.server.routes import structure_notify

pytestmark = pytest.mark.requirement("STRUCT-6496")

ROUTE = "/api/v1/structure/sidecars-changed"


class _Hub:
    """Records every event published on the engine hub."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def publish(self, topic: str, payload: dict[str, Any]) -> None:
        """Keep the event for the test to read."""
        self.events.append((topic, payload))


@pytest.fixture
def hub() -> Iterator[_Hub]:
    """A recording hub registered as the engine's, unregistered afterwards."""
    recorder = _Hub()
    events.set_hub(recorder)
    yield recorder
    events.set_hub(None)


def _client(peer: str = "127.0.0.1") -> TestClient:
    """The real route mounted under /api/v1, called from ``peer`` with a loopback Host."""
    app = FastAPI()
    app.include_router(structure_notify.router, prefix="/api/v1")
    return TestClient(app, base_url="http://127.0.0.1:7700", client=(peer, 50123))


def test_route_publishes_sections_changed_once_per_call(hub: _Hub) -> None:
    """A loopback POST publishes one library.changed kind sections with de-duplicated ids."""
    resp = _client().post(ROUTE, json={"stable_ids": ["a", "b", "a"]})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"topic": "library.changed", "kind": "sections", "ids": ["a", "b"]}
    assert hub.events == [("library.changed", {"kind": "sections", "ids": ["a", "b"]})]


def test_route_refuses_a_non_loopback_peer(hub: _Hub) -> None:
    """A tailnet or LAN peer cannot make every deck refetch."""
    resp = _client(peer="192.168.1.7").post(ROUTE, json={"stable_ids": ["a"]})
    assert resp.status_code == 403
    assert hub.events == []


def test_route_rejects_an_empty_id_list(hub: _Hub) -> None:
    """No ids is a malformed call, not an empty announcement."""
    assert _client().post(ROUTE, json={"stable_ids": []}).status_code == 422
    assert hub.events == []


def test_cli_notify_reaches_the_engine_from_its_lock_file(
    hub: _Hub, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI finds the engine through the lock file and its POST publishes the event."""
    lock = tmp_path / ".engine.lock"
    lock.write_text(json.dumps({"port": 7700, "host": "127.0.0.1"}), encoding="utf-8")
    client = _client()
    calls: list[str] = []

    def post_via_test_client(url: str, body: dict[str, object]) -> None:
        """Stand-in for the HTTP boundary: same request, answered by the real route."""
        calls.append(url)
        resp = client.post(url.removeprefix("http://127.0.0.1:7700"), json=body)
        if resp.status_code >= 400:
            raise HTTPError(url, resp.status_code, resp.text, None, None)  # type: ignore[arg-type]

    monkeypatch.setattr(notify, "_post_json", post_via_test_client)
    env = {notify.ENGINE_URL_ENV: "", "OPENDJ_LIVE_LOCK_PATH": str(lock)}
    assert notify.notify_sidecars_changed(["t1", "t2"], environ=env) is True
    assert calls == [f"http://127.0.0.1:7700{ROUTE}"]
    assert hub.events == [("library.changed", {"kind": "sections", "ids": ["t1", "t2"]})]


def test_cli_notify_is_best_effort_without_an_engine(tmp_path: Path) -> None:
    """No lock file, or an engine URL nobody listens on, returns False without raising."""
    missing = {notify.ENGINE_URL_ENV: "", "OPENDJ_LIVE_LOCK_PATH": str(tmp_path / "absent.lock")}
    assert notify.notify_sidecars_changed(["t1"], environ=missing) is False
    refused = {notify.ENGINE_URL_ENV: "http://127.0.0.1:9"}
    assert notify.notify_sidecars_changed(["t1"], environ=refused) is False


def test_cli_run_notifies_every_written_sidecar(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``run`` hands every doc it wrote, ok or failed, to the engine notify."""
    docs = [
        {"stable_id": "ok1", "status": "ok", "sections": [], "quantize": {"implausible": []}},
        {"stable_id": "bad1", "status": "failed", "reason": "no_own_beatgrid"},
    ]
    conn = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(cli, "analysis_store", SimpleNamespace(open_conn=lambda _p: conn))
    monkeypatch.setattr(cli, "state_db", SimpleNamespace(open_ro=lambda _p: conn))
    monkeypatch.setattr(cli, "bulk_local_audio_paths", lambda _s, ids: dict.fromkeys(ids))
    monkeypatch.setattr(cli, "analyze", lambda *_a, **_k: docs)
    notified: list[list[str]] = []
    monkeypatch.setattr(cli, "notify_sidecars_changed", lambda ids: notified.append(list(ids)) or False)
    rc = cli.main(["--data-dir", str(tmp_path), "run", "--stable-id", "ok1", "--stable-id", "bad1"])
    assert rc == 0, "an unreachable engine never fails the CLI"
    assert notified == [["ok1", "bad1"]]
