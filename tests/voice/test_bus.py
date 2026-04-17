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

    def test_returns_state_backed_when_phase5_available(self, monkeypatch, tmp_path):
        """Phase 5 is shipped, so the happy path is StateBackedBus.

        We point the state DB at ``tmp_path`` to avoid touching the
        developer's real ``data/state/state.db``.
        """
        monkeypatch.setattr(
            "apps.shared.state.paths.STATE_DB", tmp_path / "state.db"
        )
        # The module-level constant used by db.open_rw's default path
        # lives in apps.shared.state.paths; the db module re-imports it,
        # so patch both spellings to be safe.
        from apps.shared.state import db as state_db
        from apps.shared.state import paths as state_paths
        monkeypatch.setattr(state_paths, "STATE_DB", tmp_path / "state.db")
        warnings: list[str] = []
        b = bus.make_bus(warn=warnings.append)
        try:
            assert isinstance(b, bus.StateBackedBus)
            assert warnings == []
        finally:
            b.close()

    def test_falls_back_when_state_db_cannot_open(self, monkeypatch):
        """A broken state layer must fall back to JSONL with one warning."""
        def _boom(*a, **kw):  # noqa: ANN001,ANN002,ANN003
            raise RuntimeError("state DB not available")

        monkeypatch.setattr(
            "apps.shared.state.db.open_rw", _boom
        )
        warnings: list[str] = []
        b = bus.make_bus(warn=warnings.append)
        assert isinstance(b, bus.JsonlStubBus)
        assert any("JSONL stub" in w for w in warnings)

    def test_warning_is_once_per_run(self, monkeypatch):
        def _boom(*a, **kw):  # noqa: ANN001,ANN002,ANN003
            raise RuntimeError("state DB not available")

        monkeypatch.setattr(
            "apps.shared.state.db.open_rw", _boom
        )
        warnings: list[str] = []
        bus.make_bus(warn=warnings.append)
        bus.make_bus(warn=warnings.append)
        # One warning only for the fallback path.
        assert sum("JSONL stub" in w for w in warnings) == 1


class TestStateBackedBus:
    def test_publish_writes_to_events_table(self, tmp_path, monkeypatch):
        from apps.shared.state import paths as state_paths
        monkeypatch.setattr(state_paths, "STATE_DB", tmp_path / "state.db")
        b = bus.StateBackedBus()
        try:
            eid = b.publish({"kind": "SEARCH", "slots": {"query": "x"}})
            assert eid >= 1
            import sqlite3 as _sqlite3
            with _sqlite3.connect(str(tmp_path / "state.db")) as conn:
                rows = conn.execute(
                    "SELECT kind, actor FROM events WHERE actor = 'voice'"
                ).fetchall()
            assert rows == [("SEARCH", "voice")]
        finally:
            b.close()

    def test_publish_commits_so_row_survives_process_restart(
        self, tmp_path, monkeypatch,
    ):
        """Adversarial R4 F2: publish() must explicitly commit.

        Simulates a daemon crash by publishing, closing the bus (without
        the original connection doing anything else that could force a
        commit), then reopening the raw SQLite file and asserting the
        event row is durable.
        """
        from apps.shared.state import paths as state_paths
        monkeypatch.setattr(state_paths, "STATE_DB", tmp_path / "state.db")
        b = bus.StateBackedBus()
        try:
            eid = b.publish({"kind": "SEARCH", "slots": {"query": "x"}})
            assert eid >= 1
            # Read from a fresh connection BEFORE close(): if publish()
            # did not commit, this independent reader would see zero rows
            # because SQLite's deferred transaction is still open on the
            # writer connection.
            import sqlite3 as _sqlite3
            with _sqlite3.connect(str(tmp_path / "state.db")) as probe:
                rows = probe.execute(
                    "SELECT kind FROM events WHERE actor = 'voice'"
                ).fetchall()
            assert rows == [("SEARCH",)], (
                "publish() did not commit; fresh reader saw no rows"
            )
        finally:
            b.close()

    def test_publish_is_durable_when_fanout_raises(
        self, tmp_path, monkeypatch, caplog,
    ):
        """Adversarial R4 F1: a raising in-process subscriber (or closed
        fanout bus) must not silently leave DB and bus out of sync.

        The invariant we enforce: fanout failures are logged at ERROR
        level, the DB row is committed, the publisher does not die, and
        ``recent()`` (which reads from the same committed table) returns
        the event.
        """
        import logging
        from apps.shared.state import paths as state_paths
        monkeypatch.setattr(state_paths, "STATE_DB", tmp_path / "state.db")
        b = bus.StateBackedBus()
        try:
            # Force the in-process fanout to raise. We monkey-patch the
            # bound EventBus instance's publish so the real Phase 5 code
            # path fires but the fanout call explodes.
            def _boom(_event):  # noqa: ANN001
                raise RuntimeError("fanout exploded")
            monkeypatch.setattr(b._bus, "publish", _boom)

            caplog.set_level(logging.ERROR, logger="apps.voice.bus")
            eid = b.publish({"kind": "SEARCH", "slots": {}})
            assert eid >= 1, "publisher must not die when fanout raises"

            # DB row is committed + readable via recent() (which is the
            # observable "DB view" of the bus).
            rows = b.recent("SEARCH")
            assert len(rows) == 1
            assert rows[0]["kind"] == "SEARCH"

            # Loud failure: structured error log, not silent desync.
            assert any(
                "fanout failed" in rec.getMessage()
                for rec in caplog.records
            ), "expected structured error log for fanout failure"
        finally:
            b.close()

    def test_recent_filters_by_kind(self, tmp_path, monkeypatch):
        from apps.shared.state import paths as state_paths
        monkeypatch.setattr(state_paths, "STATE_DB", tmp_path / "state.db")
        b = bus.StateBackedBus()
        try:
            b.publish({"kind": "SEARCH"})
            b.publish({"kind": "READ_BPM", "slots": {}})
            b.publish({"kind": "SEARCH"})
            searches = b.recent("SEARCH")
            reads = b.recent("READ_BPM")
            assert len(searches) == 2
            assert len(reads) == 1
            assert reads[0]["kind"] == "READ_BPM"
        finally:
            b.close()
