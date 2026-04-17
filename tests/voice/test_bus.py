"""Unit tests for apps.voice.bus (VOICE-01)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.voice import bus


pytestmark = pytest.mark.requirement("VOICE-01")


class TestJsonlStubBus:
    def test_publish_appends_line(self, tmp_path: Path):
        path = tmp_path / "events.jsonl"
        b = bus.JsonlStubBus(path=path)
        eid1 = b.publish({"kind": "SEARCH", "slots": {"query": "x"}})
        eid2 = b.publish({"kind": "READ_BPM", "slots": {}})
        assert eid1 == 1 and eid2 == 2
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        payload = json.loads(lines[0])
        assert payload["kind"] == "SEARCH"
        assert payload["source"] == "voice"
        assert "ts" in payload

    def test_recent_returns_matching_kind(self, tmp_path: Path):
        b = bus.JsonlStubBus(path=tmp_path / "events.jsonl")
        b.publish({"kind": "SEARCH"})
        b.publish({"kind": "deck_state", "bpm": 128})
        b.publish({"kind": "SEARCH"})
        assert len(b.recent("SEARCH")) == 2
        assert b.recent("deck_state")[0]["bpm"] == 128


class TestInMemoryBus:
    def test_publish_and_recent(self):
        b = bus.InMemoryBus()
        b.publish({"kind": "SEARCH"})
        b.publish({"kind": "SEARCH"})
        b.publish({"kind": "READ_BPM"})
        assert len(b.recent("SEARCH")) == 2
        assert b.recent("READ_BPM", limit=5)[0]["kind"] == "READ_BPM"

    def test_empty_recent(self):
        b = bus.InMemoryBus()
        assert b.recent("SEARCH") == []


class TestMakeBus:
    def test_force_stub(self):
        b = bus.make_bus(force_stub=True)
        assert isinstance(b, bus.JsonlStubBus)

    def test_falls_back_when_state_events_missing(self, monkeypatch):
        """Simulate the Phase-5-not-yet-shipped case.

        Phase 5 currently ships only schema/db/ids/paths; the `events`
        submodule is absent so real construction raises ImportError and
        make_bus must fall back to the JSONL stub.
        """
        warnings: list[str] = []

        # StateBackedBus __init__ will try `from apps.shared.state import events`.
        # That import is expected to fail at time of this phase since
        # Phase 5's events layer is not yet shipped. Verify the fallback.
        b = bus.make_bus(warn=warnings.append)
        assert isinstance(b, bus.JsonlStubBus)
        assert any("JSONL stub" in w for w in warnings)

    def test_warning_is_once_per_run(self):
        warnings: list[str] = []
        bus.make_bus(warn=warnings.append)
        bus.make_bus(warn=warnings.append)
        # One warning only for the fallback path.
        assert sum("JSONL stub" in w for w in warnings) == 1
