"""Phase 4 SYNC-04: safety-rail harness tests."""
from __future__ import annotations

from pathlib import Path

import pytest

from apps.sync.safety import (
    LiveWriteSession,
    SafetyAbort,
    assert_icloud_quiesced,
    backup_db,
    require_typed_confirm,
    target_process_name,
)

# Live-write MECHANICS against tmp fixtures: runs with the one-way rekordbox
# import gate ON (root conftest reads the marker). Never a real rb target.
pytestmark = [pytest.mark.requirement("SYNC-04"), pytest.mark.rekordbox_writeback]


def _make_db(tmp_path: Path, name: str = "live.db") -> Path:
    p = tmp_path / name
    p.write_bytes(b"SQLite fixture bytes")
    return p


def test_require_typed_confirm_raises_without_flag():
    with pytest.raises(SafetyAbort):
        require_typed_confirm(False)


def test_require_typed_confirm_passes_with_flag():
    require_typed_confirm(True)


def test_target_process_name_known():
    assert target_process_name("rekordbox") == "Rekordbox"
    assert target_process_name("djay") == "djay Pro"


def test_backup_db_writes_timestamped_copy(tmp_path: Path):
    src = _make_db(tmp_path)
    bak = backup_db(src)
    assert bak.exists()
    assert bak.read_bytes() == src.read_bytes()
    assert ".bak." in bak.name


def test_backup_db_missing_raises(tmp_path: Path):
    missing = tmp_path / "nonexistent.db"
    with pytest.raises(SafetyAbort):
        backup_db(missing)


def test_session_requires_flag(tmp_path: Path):
    src = _make_db(tmp_path)
    sess = LiveWriteSession(
        target="rekordbox",
        reason="test",
        flag_ok=False,
        db_path=src,
        process_gate_override=lambda _t: None,
    )
    with pytest.raises(SafetyAbort):
        sess.__enter__()


def test_session_pgrep_gate_aborts(tmp_path: Path):
    src = _make_db(tmp_path)

    def _raise(target):
        raise SafetyAbort("process running")

    sess = LiveWriteSession(
        target="rekordbox",
        reason="test",
        flag_ok=True,
        db_path=src,
        process_gate_override=_raise,
    )
    with pytest.raises(SafetyAbort):
        sess.__enter__()


def test_session_writes_backup_and_reverse_script(tmp_path: Path):
    src = _make_db(tmp_path)
    with LiveWriteSession(
        target="rekordbox",
        reason="test",
        flag_ok=True,
        db_path=src,
        process_gate_override=lambda t: None,
        reversal_root=tmp_path / "reversal",
    ) as sess:
        with sess.per_track("track-1") as w:
            w.write("payload")
            w.append_reverse("# restore row 1")
    assert sess.backup_path is not None and sess.backup_path.exists()
    script_text = sess.reverse_script_path.read_text(encoding="utf-8")
    assert "restore row 1" in script_text
    assert "cp -n" in script_text
    assert "track-1" in sess.written_tracks


def test_session_per_track_records_skip_when_not_written(tmp_path: Path):
    src = _make_db(tmp_path)
    with LiveWriteSession(
        target="rekordbox",
        reason="test",
        flag_ok=True,
        db_path=src,
        process_gate_override=lambda t: None,
        reversal_root=tmp_path / "reversal",
    ) as sess:
        with sess.per_track("skipme") as w:
            pass
    assert "skipme" in sess.skipped_tracks


def test_session_icloud_gate_called_for_djay(tmp_path: Path):
    called = {"ran": False}
    src = _make_db(tmp_path)

    def icloud_gate(p, s):
        called["ran"] = True

    with LiveWriteSession(
        target="djay",
        reason="test",
        flag_ok=True,
        db_path=src,
        process_gate_override=lambda t: None,
        icloud_gate_override=icloud_gate,
        reversal_root=tmp_path / "reversal",
    ) as sess:
        pass
    assert called["ran"]


def test_session_icloud_gate_not_called_for_rekordbox(tmp_path: Path):
    called = {"ran": False}
    src = _make_db(tmp_path)

    def icloud_gate(p, s):
        called["ran"] = True

    with LiveWriteSession(
        target="rekordbox",
        reason="test",
        flag_ok=True,
        db_path=src,
        process_gate_override=lambda t: None,
        icloud_gate_override=icloud_gate,
        reversal_root=tmp_path / "reversal",
    ) as sess:
        pass
    assert not called["ran"]


def test_assert_icloud_quiesced_wal_too_new(tmp_path: Path):
    src = _make_db(tmp_path)
    wal = Path(str(src) + "-wal")
    wal.write_bytes(b"")
    with pytest.raises(SafetyAbort):
        assert_icloud_quiesced(src, wal_quiesce_seconds=9999)


def test_verifier_failure_aborts(tmp_path: Path):
    src = _make_db(tmp_path)
    with pytest.raises(SafetyAbort):
        with LiveWriteSession(
            target="rekordbox",
            reason="test",
            flag_ok=True,
            db_path=src,
            process_gate_override=lambda t: None,
            verifier=lambda *a: False,
            reversal_root=tmp_path / "reversal",
        ) as sess:
            with sess.per_track("x") as w:
                w.write("payload")
                w.verify_readback()


def test_reverse_script_appends_incrementally(tmp_path: Path):
    src = _make_db(tmp_path)
    with LiveWriteSession(
        target="rekordbox",
        reason="test",
        flag_ok=True,
        db_path=src,
        process_gate_override=lambda t: None,
        reversal_root=tmp_path / "reversal",
    ) as sess:
        with sess.per_track("a") as w:
            w.write("1")
            w.append_reverse("# undo a")
        with sess.per_track("b") as w:
            w.write("2")
            w.append_reverse("# undo b")
    text = sess.reverse_script_path.read_text(encoding="utf-8")
    assert "undo a" in text and "undo b" in text


def test_reverse_script_footer_written_on_exception(tmp_path: Path):
    """Regression for [I2]: reverse.sh must still receive the last-resort
    restore footer when the ``with`` block exits via an exception, so the
    user can roll back manually even after a crash mid-session.
    """
    src = _make_db(tmp_path)
    sess = LiveWriteSession(
        target="rekordbox",
        reason="test",
        flag_ok=True,
        db_path=src,
        process_gate_override=lambda t: None,
        reversal_root=tmp_path / "reversal",
    )
    with pytest.raises(RuntimeError):
        with sess:
            with sess.per_track("boom") as w:
                w.write("x")
                w.append_reverse("# pre-crash undo")
                raise RuntimeError("simulated crash")
    text = sess.reverse_script_path.read_text(encoding="utf-8")
    assert "pre-crash undo" in text
    assert "cp -n" in text
    assert "Last-resort full restore" in text
    assert "session aborted with exception" in text
