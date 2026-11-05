"""SYNC-03 regression: apply_plan must refuse live writes when the caller
has not wrapped the call in a LiveWriteSession AND a DJ app is running.

Tracked as forensics finding "P1-A" in CHANGELOG.md -- that is an audit
label, not a requirement ID; the requirement it defends is SYNC-03
(bi-directional playlist sync, RB-canonical, with conflict policy).

Forensics audit (PR #105) flagged that apps/sync/playlist_apply.py:apply_plan
opened ``MediaLibrary.db`` with ``mode=rwc`` directly, relying entirely on
its CLI / smartlists-writer callers to have run the six-rail harness first.
A future direct caller could silently bypass the process-gate (rail 2).

This test asserts the defense-in-depth self-guard: with ``live=True`` and
no ``safety_session=`` passed, if ``pgrep`` reports djay or Rekordbox
running, ``apply_plan`` raises ``PlaylistApplyError`` instead of writing.

Notably we do NOT launch djay or Rekordbox; we monkeypatch
``apps.sync.safety._is_running`` to simulate a live process.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.sync import playlist_apply as pa
from apps.sync import safety as _safety


@pytest.mark.requirement("SYNC-03")
def test_playlist_apply_refuses_without_safety_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """live=True + no safety_session + djay running => refuse."""
    # Fixture DB — empty file is enough because the guard short-circuits
    # BEFORE any sqlite connection is opened.
    db_path = tmp_path / "MediaLibrary.db"
    db_path.touch()

    def _fake_is_running(name: str) -> bool:
        # Simulate djay Pro alive. Rekordbox dead.
        return name == "djay Pro"

    monkeypatch.setattr(_safety, "_is_running", _fake_is_running)

    with pytest.raises(pa.PlaylistApplyError, match="self-guard"):
        pa.apply_plan({"playlists": []}, db_path=db_path, live=True)


@pytest.mark.requirement("SYNC-03")
def test_playlist_apply_refuses_when_rekordbox_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard also catches Rekordbox, not only djay."""
    db_path = tmp_path / "MediaLibrary.db"
    db_path.touch()

    def _fake_is_running(name: str) -> bool:
        return name == "Rekordbox"

    monkeypatch.setattr(_safety, "_is_running", _fake_is_running)

    with pytest.raises(pa.PlaylistApplyError, match="self-guard"):
        pa.apply_plan({"playlists": []}, db_path=db_path, live=True)


@pytest.mark.requirement("SYNC-03")
def test_playlist_apply_allows_when_safety_session_provided(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With an explicit safety_session= the inline guard is skipped
    (the caller asserts it has already run the full harness).

    Empty plan => no sqlite writes => success without needing a real DB.
    """
    db_path = tmp_path / "MediaLibrary.db"
    # apply_plan opens a sqlite connection even for empty plans, so create
    # an actual (empty) SQLite DB rather than a zero-byte file.
    import sqlite3

    con = sqlite3.connect(str(db_path))
    con.close()

    def _fake_is_running(name: str) -> bool:
        # Would normally abort, but safety_session sentinel says the
        # caller has already run the harness.
        return True

    monkeypatch.setattr(_safety, "_is_running", _fake_is_running)

    sentinel = object()  # stands in for an active LiveWriteSession
    result = pa.apply_plan(
        {"playlists": []},
        db_path=db_path,
        live=True,
        safety_session=sentinel,
    )
    assert result.per_playlist == []


@pytest.mark.requirement("SYNC-03")
def test_playlist_apply_dry_run_bypasses_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """live=False (default) never triggers the guard — dry-run / staging
    workflows remain unaffected even if a DJ app is running.
    """
    db_path = tmp_path / "staging.db"
    import sqlite3

    con = sqlite3.connect(str(db_path))
    con.close()

    monkeypatch.setattr(_safety, "_is_running", lambda _n: True)

    # live defaults to False → guard MUST NOT fire.
    result = pa.apply_plan({"playlists": []}, db_path=db_path)
    assert result.per_playlist == []


@pytest.mark.requirement("SYNC-03")
@pytest.mark.requirement("SYNC-8454")
def test_playlist_apply_self_guard_refuses_when_is_running_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[if] _is_running raises SafetyAbort [then] apply_plan self-guard refuses, [else stop]."""
    db_path = tmp_path / "MediaLibrary.db"
    db_path.touch()

    def _unmeasurable(_name: str) -> bool:
        raise _safety.SafetyAbort("pgrep is unavailable; cannot prove djay Pro is closed")

    monkeypatch.setattr(_safety, "_is_running", _unmeasurable)

    with pytest.raises(pa.PlaylistApplyError, match="self-guard") as exc_info:
        pa.apply_plan({"playlists": []}, db_path=db_path, live=True)
    assert "pgrep" in str(exc_info.value)


@pytest.mark.requirement("SYNC-03")
@pytest.mark.requirement("SYNC-8454")
def test_playlist_apply_force_no_pgrep_passes_safety_session_to_apply_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[if] --force-no-pgrep with unmeasurable pgrep [then] apply_plan gets sentinel, [else stop]."""
    captured: dict[str, object] = {}

    def _pgrep_unavailable(_pattern: str) -> bool:
        raise pa.PlaylistApplyError("pgrep not available; cannot verify 'djay' is not running.")

    def _capture_apply(*args: object, **kwargs: object) -> pa.ApplyResult:
        captured.update(kwargs)
        return pa.ApplyResult()

    monkeypatch.setattr(pa, "_process_running", _pgrep_unavailable)
    monkeypatch.setattr(pa, "_integrity_gate", lambda *a, **k: None)
    monkeypatch.setattr(pa, "_live_integrity_report", object)
    db = tmp_path / "djay.db"
    db.write_bytes(b"djay")
    monkeypatch.setattr(pa, "_live_db_path", lambda live: db)
    monkeypatch.setattr(pa, "apply_plan", _capture_apply)
    monkeypatch.setattr("builtins.input", lambda: pa.CONFIRMATION_PHRASE)

    plan = tmp_path / "plan.json"
    plan.write_text(
        '{"generated_at":"","match_set_sha256":"","playlists":[],"djay_only":[]}',
        encoding="utf-8",
    )

    rc = pa.main(
        [
            "--plan",
            str(plan),
            "--backup-dir",
            str(tmp_path / "backups"),
            "--live",
            "--i-understand-the-risks",
            "--force-no-pgrep",
            "--skip-tsaf-validation",
        ]
    )
    assert rc == 0
    assert captured.get("safety_session") is not None
