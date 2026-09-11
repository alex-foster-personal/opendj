"""SYNC-05 regression: ``apply_analysis`` refuses to partial-apply.

Codex P04-02: ``_write_rb_field`` only implements ``bpm`` and
``energy``; for ``manual_bpm`` / ``key_camelot`` / ``tags`` it used to
return ``False`` and the CLI still exited 0, reporting success for a
no-op. This test locks in the non-zero exit behaviour.
"""
from __future__ import annotations

import csv
import hashlib
import sqlite3
from pathlib import Path

import pytest

from apps.analysis import selection
from apps.sync import apply_analysis
from apps.sync.apply_analysis import (
    _SUPPORTED_RB_WRITE_FIELDS,
    UnsupportedRbFieldError,
    _write_rb_field,
    live_run,
    main,
)


def _write_state_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE tracks (stable_id TEXT PRIMARY KEY, deleted_at TEXT);
        CREATE TABLE track_vendor_ids (
            stable_id TEXT, vendor TEXT, vendor_id TEXT, deleted_at TEXT
        );
        CREATE TABLE analysis_source_default (
            lane TEXT PRIMARY KEY, source TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE analysis_projection (
            stable_id TEXT, field TEXT, value, status TEXT, reason TEXT,
            confidence REAL, backend TEXT, backend_version TEXT, updated_at TEXT,
            PRIMARY KEY (stable_id, field)
        );
        """
    )
    return conn


def _write_rb_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE djmdContent (
            ID TEXT PRIMARY KEY, BPM INTEGER, KeyID TEXT,
            rb_local_deleted INTEGER DEFAULT 0
        );
        CREATE TABLE djmdKey (ID TEXT PRIMARY KEY, ScaleName TEXT);
        INSERT INTO djmdKey(ID, ScaleName) VALUES ('k1', 'Am');
        """
    )
    return conn


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.requirement("SYNC-05")
@pytest.mark.parametrize("field", ["manual_bpm", "key_camelot", "tags"])
def test_write_rb_field_refuses_unsupported(field: str) -> None:
    """Unsupported RB-side fields must raise, not silently return False."""
    with pytest.raises(UnsupportedRbFieldError):
        _write_rb_field(db=None, content_id="1", field=field, value="x")


@pytest.mark.requirement("SYNC-05")
def test_supported_fields_are_bpm_and_energy_only() -> None:
    assert _SUPPORTED_RB_WRITE_FIELDS == frozenset({"bpm", "energy"})


@pytest.mark.requirement("SYNC-05")
def test_live_run_raises_on_unsupported_rb_field(tmp_path: Path) -> None:
    rows = [
        {
            "rb_content_id": "1",
            "djay_uuid": "uuid-1",
            "field": "manual_bpm",
            "rb_value": "",
            "djay_value": "128.0",
            "resolution": "accept_djay",
            "action_hint": "write djay -> RB",
        }
    ]
    with pytest.raises(UnsupportedRbFieldError):
        live_run(
            rows,
            fields={"manual_bpm"},
            flag_ok=True,
            rb_db_path=tmp_path / "rb.db",
            djay_db_path=tmp_path / "djay.db",
        )


@pytest.mark.requirement("SYNC-05")
def test_cli_exits_nonzero_on_unsupported_rb_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """``main`` must NOT exit 0 when the plan contains unsupported RB fields.

    This is the exact regression Codex P04-02 described.
    """
    diff_csv = tmp_path / "analysis-diff.csv"
    with diff_csv.open("w", newline="", encoding="utf-8") as fp:
        w = csv.writer(fp)
        w.writerow([
            "rb_content_id", "djay_uuid", "field",
            "rb_value", "djay_value", "resolution", "action_hint",
        ])
        w.writerow([
            "1", "uuid-1", "key_camelot",
            "", "8A", "accept_djay", "write djay -> RB",
        ])

    # Route live-run away from real libraries.
    monkeypatch.setattr(
        apply_analysis, "_live_rb_db_path", lambda live: tmp_path / "rb.db"
    )
    monkeypatch.setattr(
        apply_analysis, "_live_djay_db_path", lambda live: tmp_path / "djay.db"
    )

    rc = main([
        "--diff-csv", str(diff_csv),
        "--live",
        "--i-understand-the-risks",
        "--fields", "key_camelot",
    ])
    assert rc != 0, (
        "apply_analysis must not exit 0 when the RB plan contains an "
        "unsupported field; regression of Codex P04-02."
    )
    err = capsys.readouterr().err
    assert "UnsupportedRbField" in err


@pytest.mark.requirement("SYNC-05")
def test_writeback_dry_run_refuses_unpromoted_lane(
    tmp_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    state_path = tmp_path / "state.db"
    rb_path = tmp_path / "rb.db"
    state_conn = _write_state_db(state_path)
    state_conn.commit()
    state_conn.close()
    rb_conn = _write_rb_db(rb_path)
    rb_conn.commit()
    rb_conn.close()

    state_hash = _sha256(state_path)
    rb_hash = _sha256(rb_path)

    rc = main([
        "--state-db", str(state_path),
        "--rb-db", str(rb_path),
        "--lanes", "key",
    ])
    assert rc == 2
    err = capsys.readouterr().err
    assert "key" in err
    assert "rbx" in err
    assert _sha256(state_path) == state_hash
    assert _sha256(rb_path) == rb_hash


@pytest.mark.requirement("SYNC-05")
def test_writeback_dry_run_refuses_partial_promoted_lanes(
    tmp_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    state_path = tmp_path / "state.db"
    rb_path = tmp_path / "rb.db"
    state_conn = _write_state_db(state_path)
    selection.set_default(state_conn, "beatgrid", "own")
    state_conn.commit()
    state_conn.close()
    rb_conn = _write_rb_db(rb_path)
    rb_conn.commit()
    rb_conn.close()

    rc = main([
        "--state-db", str(state_path),
        "--rb-db", str(rb_path),
        "--lanes", "beatgrid,key",
    ])
    assert rc == 2
    err = capsys.readouterr().err
    assert "key" in err


@pytest.mark.requirement("SYNC-05")
def test_writeback_dry_run_ignores_parity02_toggle_own_on_unpromoted(
    tmp_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    selection.reset_toggles()
    state_path = tmp_path / "state.db"
    rb_path = tmp_path / "rb.db"
    state_conn = _write_state_db(state_path)
    assert selection.get_default(state_conn, "key") == "rbx"
    selection.set_toggle("key", "own")
    assert selection.effective_source(state_conn, "key") == "own"
    state_conn.commit()
    state_conn.close()
    rb_conn = _write_rb_db(rb_path)
    rb_conn.commit()
    rb_conn.close()

    rc = main([
        "--state-db", str(state_path),
        "--rb-db", str(rb_path),
        "--lanes", "key",
    ])
    assert rc == 2
    selection.reset_toggles()

    state_path2 = tmp_path / "state2.db"
    rb_path2 = tmp_path / "rb2.db"
    state_conn = _write_state_db(state_path2)
    selection.set_default(state_conn, "beatgrid", "own")
    selection.set_toggle("key", "own")
    state_conn.execute(
        "INSERT INTO track_vendor_ids VALUES ('t1', 'rekordbox', 'rb-1', NULL)"
    )
    state_conn.execute(
        "INSERT INTO analysis_projection "
        "(stable_id, field, value, status) VALUES ('t1', 'key', '8A', 'ok')"
    )
    state_conn.commit()
    state_conn.close()
    rb_conn = _write_rb_db(rb_path2)
    rb_conn.execute(
        "INSERT INTO djmdContent(ID, BPM, KeyID) VALUES ('rb-1', 12000, 'k1')"
    )
    rb_conn.commit()
    rb_conn.close()

    rc2 = main([
        "--state-db", str(state_path2),
        "--rb-db", str(rb_path2),
    ])
    assert rc2 == 2
    out = capsys.readouterr().out
    for line in out.splitlines():
        if "key" in line and "writable" in line:
            pytest.fail(f"unpromoted key lane listed as writable: {line}")
    selection.reset_toggles()


@pytest.mark.requirement("SYNC-05")
def test_writeback_dry_run_gate_off_does_not_call_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = tmp_path / "state.db"
    rb_path = tmp_path / "rb.db"
    state_conn = _write_state_db(state_path)
    selection.set_default(state_conn, "beatgrid", "own")
    state_conn.execute(
        "INSERT INTO track_vendor_ids VALUES ('t1', 'rekordbox', 'rb-1', NULL)"
    )
    state_conn.execute(
        "INSERT INTO analysis_projection "
        "(stable_id, field, value, status) VALUES ('t1', 'bpm', 128.0, 'ok')"
    )
    state_conn.commit()
    state_conn.close()
    rb_conn = _write_rb_db(rb_path)
    rb_conn.execute(
        "INSERT INTO djmdContent(ID, BPM, KeyID) VALUES ('rb-1', 12800, 'k1')"
    )
    rb_conn.commit()
    rb_conn.close()

    monkeypatch.setattr(
        "apps.shared.rekordbox_writeback.require_writeback_enabled",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("require_writeback_enabled called")
        ),
    )
    rc = main([
        "--state-db", str(state_path),
        "--rb-db", str(rb_path),
        "--lanes", "beatgrid",
        "--fields", "bpm",
    ])
    assert rc == 0


@pytest.mark.requirement("SYNC-05")
def test_writeback_dry_run_denominator_excludes_unmatched(
    tmp_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    state_path = tmp_path / "state.db"
    rb_path = tmp_path / "rb.db"
    state_conn = _write_state_db(state_path)
    selection.set_default(state_conn, "beatgrid", "own")
    state_conn.execute(
        "INSERT INTO track_vendor_ids VALUES ('t-w', 'rekordbox', 'rb-w', NULL)"
    )
    state_conn.execute(
        "INSERT INTO analysis_projection "
        "(stable_id, field, value, status) VALUES ('t-w', 'bpm', 128.0, 'ok')"
    )
    state_conn.execute(
        "INSERT INTO analysis_projection "
        "(stable_id, field, value, status) VALUES ('t-u', 'bpm', 130.0, 'ok')"
    )
    state_conn.commit()
    state_conn.close()
    rb_conn = _write_rb_db(rb_path)
    rb_conn.execute(
        "INSERT INTO djmdContent(ID, BPM, KeyID) VALUES ('rb-w', 12800, 'k1')"
    )
    rb_conn.commit()
    rb_conn.close()

    rc = main([
        "--state-db", str(state_path),
        "--rb-db", str(rb_path),
        "--lanes", "beatgrid",
        "--fields", "bpm",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "denominator: 1" in out
    assert "unmatched: 1" in out
    assert "t-u" in out and "unmatched" in out
