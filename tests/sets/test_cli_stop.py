"""Regression test for ``python -m apps.sets stop`` finalisation.

Codex Phase 12 review finding: the prior ``stop`` dispatcher only
flipped ``ended_at`` in SQL and unlinked the pid, leaving no
``manifest.json`` behind. Downstream consumers (``sessions``,
``replay``, ``api``) require the manifest, so the recording was
effectively broken.

This test starts a recording (``no-capture`` test mode) and then
invokes the CLI ``stop`` subcommand, asserting the manifest exists
and marks the recording as finalised.
"""
from __future__ import annotations

import json

import pytest

from apps.sets import __main__ as cli
from apps.sets import paths as sets_paths_mod
from apps.sets import record as record_mod
from apps.sets import state as state_mod
from apps.sets.state import SetsState


@pytest.mark.requirement("SET-02")
def test_cli_stop_finalises_manifest_and_marks_ended(tmp_path, monkeypatch, capsys):
    sets_root = tmp_path / "sets"
    sets_root.mkdir()
    db_path = tmp_path / "sets.db"

    # Redirect module-level paths so the CLI writes into tmp_path.
    monkeypatch.setattr(sets_paths_mod, "SETS_DIR", sets_root)
    monkeypatch.setattr(state_mod.sets_paths, "SETS_DB", db_path)

    # Seed a live recording: test-mode (capture_disabled, no sources).
    cfg = record_mod.RecorderConfig(sources=(), capture_disabled=True)
    seed_state = SetsState(db_path=db_path)
    recorder = record_mod.start(
        session_id="2026-04-17T21-30-00",
        config=cfg,
        sets_root=sets_root,
        state=seed_state,
        source_factories={},
    )
    session_id = recorder.session_id
    session_dir = sets_root / session_id
    assert (session_dir / "recorder.pid").exists()
    assert not (session_dir / "manifest.json").exists()
    row = seed_state.get_session(session_id)
    assert row is not None and row.ended_at is None

    # Act: invoke the CLI ``stop`` entrypoint exactly as a user would.
    rc = cli.main(["stop", "--session-id", session_id])
    assert rc == 0, capsys.readouterr()

    # Assert: manifest exists on disk, is valid JSON, names the session,
    # and carries a non-null ``ended_at`` (i.e. finalised).
    manifest_path = session_dir / "manifest.json"
    assert manifest_path.exists(), "stop must write manifest.json"
    manifest = json.loads(manifest_path.read_text())
    assert manifest["session_id"] == session_id
    assert manifest["ended_at"], "manifest.ended_at must be set on stop"
    assert manifest["schema_version"] == 1
    assert "watermark" in manifest

    # DB row marked ended + pid file removed.
    verify_state = SetsState(db_path=db_path)
    row2 = verify_state.get_session(session_id)
    assert row2 is not None
    assert row2.ended_at is not None
    assert not (session_dir / "recorder.pid").exists()

    # ``session_end`` event was appended to timeline.jsonl.
    timeline = session_dir / "timeline.jsonl"
    assert timeline.exists()
    events = [json.loads(ln) for ln in timeline.read_text().splitlines() if ln.strip()]
    assert any(e.get("action") == "session_end" for e in events), events
