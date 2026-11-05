"""AGENT-8304: GET /api/v1/client-errors filters, over the real daily log files.

  - if since/build_sha/kind/contains do not narrow the records then broken
  - if limit does not return the newest N, oldest first, then broken
  - if a bad kind, since or limit answers 200 instead of 422 then broken
  - if a read with no filters changes shape or order then broken

[if] a filter does not narrow the records or a bad filter answers 200 instead of 422 [then] fail, [else stop].

-Claude
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend

pytestmark = pytest.mark.requirement("AGENT-8304")


_LOOPBACK = "http://127.0.0.1"

_RECORDS = [
    ("e1", "2026-11-02T23:59:00.000Z", "aaaa111", "ui-error", "Stem decode failed"),
    ("e2", "2026-11-03T08:00:00.000Z", "aaaa111", "console-error", "[events-bus] unknown kind 'plays'"),
    ("e3", "2026-11-03T09:00:00.000Z", "bbbb222", "console-error", "Fetch is aborted"),
    ("e4", "2026-11-03T10:00:00.000Z", "bbbb222", "window-error", "TypeError: x is undefined"),
]


def _seed(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    for event_id, received_at, sha, kind, message in _RECORDS:
        day = received_at[:10]
        with (log_dir / f"webui-client-errors-{day}.log").open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "event_id": event_id,
                        "received_at": received_at,
                        "kind": kind,
                        "message": message,
                        "build_sha": sha,
                        "url": "http://127.0.0.1/",
                    }
                )
                + "\n"
            )


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    _seed(tmp_path)
    app = create_app(backend=InMemoryBackend(), mount_frontend=False, enable_cors=False, client_error_log_dir=tmp_path)
    with TestClient(app, base_url=_LOOPBACK) as test_client:
        yield test_client


def _ids(client: TestClient, **params: object) -> list[str]:
    response = client.get("/api/v1/client-errors", params=params)
    assert response.status_code == 200, response.text
    return [record["event_id"] for record in response.json()]


def test_no_filters_returns_every_record_oldest_first_with_build_sha(client: TestClient) -> None:
    response = client.get("/api/v1/client-errors")
    assert [r["event_id"] for r in response.json()] == ["e1", "e2", "e3", "e4"]
    assert response.json()[0]["build_sha"] == "aaaa111"


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"since": "2026-11-03T08:30:00Z"}, ["e3", "e4"]),
        ({"since": "2026-11-03T08:00:00+00:00"}, ["e2", "e3", "e4"]),
        ({"build_sha": "bbbb"}, ["e3", "e4"]),
        ({"kind": "console-error"}, ["e2", "e3"]),
        ({"contains": "FETCH IS"}, ["e3"]),
        ({"limit": 2}, ["e3", "e4"]),
        ({"kind": "console-error", "build_sha": "aaaa", "limit": 5}, ["e2"]),
    ],
)
def test_each_filter_narrows(client: TestClient, params: dict[str, object], expected: list[str]) -> None:
    assert _ids(client, **params) == expected


@pytest.mark.parametrize(
    "params",
    [
        {"kind": "not-a-kind"},
        {"since": "yesterday"},
        {"since": "2026-11-03T08:00:00"},
        {"limit": 0},
        {"limit": 1001},
        {"contains": ""},
    ],
)
def test_bad_filters_are_422(client: TestClient, params: dict[str, object]) -> None:
    assert client.get("/api/v1/client-errors", params=params).status_code == 422


def test_untriaged_combines_with_filters(client: TestClient) -> None:
    triaged = client.patch("/api/v1/client-errors/e3", json={"disposition": "no-fix", "ref": "boot timeout"})
    assert triaged.status_code == 200
    assert _ids(client, kind="console-error", untriaged="true") == ["e2"]
