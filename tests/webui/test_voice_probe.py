"""Voice probe endpoint tests (text-command-entry).

Covers the text in -> deterministic grammar -> intent out path exposed at
POST /api/v1/voice/probe, the destructive-intent block, the grammar-miss
response, and the 503 voice_probe_unavailable fallback when apps/voice
cannot be imported.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.webui.server.routes import voice_probe


def test_search_intent_dispatches_and_reports_browser_action(client, monkeypatch):
    _, _, bus, _, _ = voice_probe._import_voice_stack()

    def _forbid_persistent_bus(*args, **kwargs):
        raise AssertionError("voice probe must not construct a persistent bus")

    monkeypatch.setattr(bus, "make_bus", _forbid_persistent_bus)
    r = client.post("/api/v1/voice/probe", json={"text": "find daft punk"})
    assert r.status_code == 200
    body = r.json()
    assert body["intent"] == "SEARCH"
    assert body["slots"] == {"query": "daft punk"}
    assert body["blocked"] is False
    assert body["reason"] is None
    assert body["reply"] == "search:daft punk"
    assert body["client_action"] == "browser_search"
    assert body["probe_only"] is False


def test_read_bpm_intent_with_no_deck_state(client):
    r = client.post("/api/v1/voice/probe", json={"text": "whats the bpm"})
    assert r.status_code == 200
    body = r.json()
    assert body["intent"] == "READ_BPM"
    assert body["blocked"] is False
    assert body["reply"] == "no_deck_state"


def test_advance_queue_intent(client):
    r = client.post("/api/v1/voice/probe", json={"text": "next track"})
    assert r.status_code == 200
    body = r.json()
    assert body["intent"] == "ADVANCE_QUEUE"
    assert body["blocked"] is False
    assert body["client_action"] is None
    assert body["probe_only"] is True


@pytest.mark.parametrize(
    "text,intent_kind",
    [
        ("rate this 5 stars", "RATE_TRACK"),
        ("save that transition", "SAVE_CUE"),
    ],
)
def test_destructive_intents_are_blocked_not_executed(client, text, intent_kind):
    r = client.post("/api/v1/voice/probe", json={"text": text})
    assert r.status_code == 200
    body = r.json()
    assert body["intent"] == intent_kind
    assert body["blocked"] is True
    assert body["reason"]
    assert body["reply"] is None


def test_grammar_miss_returns_null_intent(client):
    r = client.post("/api/v1/voice/probe", json={"text": "gibberish text here"})
    assert r.status_code == 200
    body = r.json()
    assert body["intent"] is None
    assert body["blocked"] is False
    assert body["reason"] == "grammar_miss"


def test_voice_probe_unavailable_returns_503_with_missing_dep(client, monkeypatch):
    def _raise():
        raise ImportError("No module named 'whisper'", name="whisper")

    monkeypatch.setattr(voice_probe, "_import_voice_stack", _raise)
    r = client.post("/api/v1/voice/probe", json={"text": "find daft punk"})
    assert r.status_code == 503
    detail = r.json()["detail"]
    assert detail["code"] == "voice_probe_unavailable"
    assert "whisper" in detail["message"]


def test_openapi_lists_voice_probe(client):
    r = client.get("/openapi.json")
    assert r.status_code == 200
    assert "/api/v1/voice/probe" in r.json()["paths"]


def test_committed_openapi_matches_live_app(client):
    committed_path = (
        Path(__file__).resolve().parents[2] / "apps" / "webui" / "openapi.json"
    )
    committed = json.loads(committed_path.read_text(encoding="utf-8"))

    assert committed == client.get("/openapi.json").json()
