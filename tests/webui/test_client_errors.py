"""Real HTTP and filesystem coverage for browser error capture."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend

#: The request guard (#2689) refuses TestClient's default ``Host: testserver``.
_LOOPBACK = "http://127.0.0.1"


def _payload() -> dict[str, object]:
    return {
        "client_event_id": "browser-123",
        "kind": "ui-error",
        "message": "AudioWorklet is unavailable so Signalsmith cannot start",
        "name": "Error",
        "stack": "Error: AudioWorklet is unavailable\n    at createProcessor (audio.ts:42)",
        "url": "https://agentbox.example.ts.net/performance",
        "client_timestamp": "2026-08-17T09:00:00.000Z",
        "user_agent": "real-browser-user-agent",
        "secure_context": False,
        "audio_worklet_available": False,
        "context": {"source": "toast", "deck": 4},
    }


def test_client_error_writes_full_details_to_separate_log(tmp_path: Path) -> None:
    app = create_app(
        backend=InMemoryBackend(),
        mount_frontend=False,
        enable_cors=False,
        client_error_log_dir=tmp_path,
    )
    with TestClient(app, base_url=_LOOPBACK) as client:
        response = client.post("/api/v1/client-errors", json=_payload())

    assert response.status_code == 202
    body = response.json()
    assert body["stored"] is True
    assert body["error_id"].startswith("eid-")
    assert "sentry_event_id" in body
    paths = list(tmp_path.glob("webui-client-errors-*.log"))
    assert len(paths) == 1
    records = paths[0].read_text(encoding="utf-8").splitlines()
    assert len(records) == 1
    record = json.loads(records[0])
    assert record["event_id"] == response.json()["event_id"]
    assert record["error_id"].startswith("eid-")
    assert "host" in record
    assert "build_sha" in record
    assert record["secure_context"] is False
    assert record["audio_worklet_available"] is False
    assert "createProcessor" in record["stack"]
    assert record["received_at"].endswith("Z")


def test_list_client_errors_includes_stack_and_url(tmp_path: Path) -> None:
    app = create_app(
        backend=InMemoryBackend(),
        mount_frontend=False,
        enable_cors=False,
        client_error_log_dir=tmp_path,
    )
    with TestClient(app, base_url=_LOOPBACK) as client:
        created = client.post("/api/v1/client-errors", json=_payload())
        event_id = created.json()["event_id"]
        rows = client.get("/api/v1/client-errors").json()

    row = next(record for record in rows if record["event_id"] == event_id)
    assert "createProcessor" in row["stack"]
    assert row["url"] == _payload()["url"]


def test_client_error_accepts_browser_capture_kinds(tmp_path: Path) -> None:
    app = create_app(
        backend=InMemoryBackend(),
        mount_frontend=False,
        enable_cors=False,
        client_error_log_dir=tmp_path,
    )
    for kind in (
        "console-error",
        "console-warn",
        "resource-error",
        "csp-violation",
        "webview-console",
        "webview-navigation",
    ):
        payload = _payload()
        payload["kind"] = kind
        payload["client_event_id"] = f"{kind}-probe"
        with TestClient(app, base_url=_LOOPBACK) as client:
            response = client.post("/api/v1/client-errors", json=payload)
        assert response.status_code == 202, kind


def test_client_error_rejects_unbounded_stack(tmp_path: Path) -> None:
    app = create_app(
        backend=InMemoryBackend(),
        mount_frontend=False,
        enable_cors=False,
        client_error_log_dir=tmp_path,
    )
    payload = _payload()
    payload["stack"] = "x" * 32769
    with TestClient(app, base_url=_LOOPBACK) as client:
        response = client.post("/api/v1/client-errors", json=payload)

    assert response.status_code == 422
    assert list(tmp_path.iterdir()) == []


def test_client_error_triage_hides_decided_event_without_rewriting_daily_log(
    tmp_path: Path,
) -> None:
    app = create_app(
        backend=InMemoryBackend(),
        mount_frontend=False,
        enable_cors=False,
        client_error_log_dir=tmp_path,
    )
    with TestClient(app, base_url=_LOOPBACK) as client:
        created = client.post("/api/v1/client-errors", json=_payload())
        event_id = created.json()["event_id"]
        daily_log = next(tmp_path.glob("webui-client-errors-*.log"))
        daily_before = daily_log.read_bytes()

        untriaged = client.get("/api/v1/client-errors?untriaged=1")
        triaged = client.patch(
            f"/api/v1/client-errors/{event_id}",
            json={"disposition": "no-fix", "ref": "known browser limitation"},
        )
        after = client.get("/api/v1/client-errors?untriaged=1")

    assert untriaged.status_code == 200
    assert [record["event_id"] for record in untriaged.json()] == [event_id]
    assert triaged.status_code == 200
    assert triaged.json()["event_id"] == event_id
    assert after.json() == []
    assert daily_log.read_bytes() == daily_before
    sidecars = list(tmp_path.glob("webui-client-errors-*.triage.jsonl"))
    assert len(sidecars) == 1
    assert json.loads(sidecars[0].read_text(encoding="utf-8")) == {
        "disposition": "no-fix",
        "event_id": event_id,
        "ref": "known browser limitation",
    }

pytestmark = pytest.mark.rb_parity


def test_client_error_log_line_is_warning_not_a_second_sentry_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """if the route logs a reported browser error at ERROR then broken: warning_log
    forwards ERROR records to Sentry, so the one error would be sent twice."""
    app = create_app(
        backend=InMemoryBackend(),
        mount_frontend=False,
        enable_cors=False,
        client_error_log_dir=tmp_path,
    )
    route_logger = "apps.webui.server.routes.client_errors"
    with (
        caplog.at_level("WARNING", logger=route_logger),
        TestClient(app, base_url=_LOOPBACK) as client,
    ):
        response = client.post("/api/v1/client-errors", json=_payload())
    assert response.status_code == 202
    lines = [r for r in caplog.records if r.getMessage().startswith("browser error")]
    assert [r.levelname for r in lines] == ["WARNING"]


def test_any_deck_live_is_accepted_stored_and_forwarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """if the page says a deck was live then the flag reaches the daily log AND
    the Sentry forward's context, which is where the live-set gate reads it.

    Contract: the field is optional (None for a client that predates it) so a
    stale page never fails validation, and it is forwarded verbatim rather
    than defaulted, so "unknown" stays distinguishable from "idle".
    """
    forwarded: list[dict[str, object]] = []

    def record_forward(**kwargs: object) -> None:
        context = kwargs.get("context")
        assert isinstance(context, dict)
        forwarded.append(dict(context))

    monkeypatch.setattr(
        "apps.webui.server.routes.client_errors.capture_browser_error", record_forward
    )
    app = create_app(
        backend=InMemoryBackend(),
        mount_frontend=False,
        enable_cors=False,
        client_error_log_dir=tmp_path,
    )
    with TestClient(app, base_url=_LOOPBACK) as client:
        live = client.post("/api/v1/client-errors", json={**_payload(), "any_deck_live": True})
        legacy = client.post("/api/v1/client-errors", json=_payload())
    assert live.status_code == 202, live.text
    assert legacy.status_code == 202, legacy.text
    assert [ctx["any_deck_live"] for ctx in forwarded] == [True, None]
    daily = next(tmp_path.glob("webui-client-errors-*.log"))
    rows = [json.loads(line) for line in daily.read_text().splitlines()]
    assert [row["any_deck_live"] for row in rows] == [True, None]


def test_free_form_context_cannot_override_the_typed_live_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] the page's free-form context carries any_deck_live [then] the typed
    top-level flag is what reaches the forward, [else stop].

    The context dict is untrusted page data merged into the same mapping the
    live-set gate reads. Codex (#3737): with the context spread LAST, a body
    of `any_deck_live: true` plus `context: {"any_deck_live": false}` was
    forwarded as idle and sent mid-set. The typed fields now win.
    """
    forwarded: list[dict[str, object]] = []

    def record_forward(**kwargs: object) -> None:
        context = kwargs.get("context")
        assert isinstance(context, dict)
        forwarded.append(dict(context))

    monkeypatch.setattr(
        "apps.webui.server.routes.client_errors.capture_browser_error", record_forward
    )
    app = create_app(
        backend=InMemoryBackend(),
        mount_frontend=False,
        enable_cors=False,
        client_error_log_dir=tmp_path,
    )
    body = {
        **_payload(),
        "any_deck_live": True,
        "context": {"any_deck_live": False, "kind": "spoofed", "deck": "1"},
    }
    with TestClient(app, base_url=_LOOPBACK) as client:
        response = client.post("/api/v1/client-errors", json=body)
    assert response.status_code == 202, response.text
    assert forwarded[0]["any_deck_live"] is True
    assert forwarded[0]["kind"] == _payload()["kind"]
    # Non-reserved context keys still ride along.
    assert forwarded[0]["deck"] == "1"
