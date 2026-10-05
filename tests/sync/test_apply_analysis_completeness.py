"""SYNC-05 regression: ``apply_analysis`` refuses to partial-apply.

Codex P04-02: ``_write_rb_field`` only implements ``bpm`` and
``energy``; for ``manual_bpm`` / ``key_camelot`` / ``tags`` it used to
return ``False`` and the CLI still exited 0, reporting success for a
no-op. This test locks in the non-zero exit behaviour.
"""
from __future__ import annotations

import csv
import hashlib
import os
import sqlite3
from pathlib import Path

import pytest

from apps.analysis import selection
from apps.shared import paths
from apps.sync import apply_analysis
from apps.sync.apply_analysis import (
    _SUPPORTED_RB_WRITE_FIELDS,
    UnsupportedRbFieldError,
    _write_rb_field,
    live_run,
    main,
)


@pytest.fixture(autouse=True)
def _owned_rollout_directory(tmp_path: Path):
    """Relative production rollout stamps belong to one test, not all workers."""
    previous = Path.cwd()
    os.chdir(tmp_path)
    try:
        yield
    finally:
        os.chdir(previous)


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
            AnalysisDataPath TEXT,
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
def test_supported_fields_include_key_and_loudness() -> None:
    assert _SUPPORTED_RB_WRITE_FIELDS == frozenset({
        "bpm", "energy", "key", "loudness_lufs", "loudness_dbtp", "pqtz",
    })


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
            open_rb_db=lambda _p: None,
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


def _stamp_writeback(
    state_path: Path,
    rb_path: Path,
    lanes: tuple[str, ...],
    fields: tuple[str, ...],
) -> None:
    from apps.sync.analysis_writeback import build_writeback_plan, writable_plan_hash
    from apps.sync.safety import mark_writeback_plan

    plan = build_writeback_plan(
        state_db=state_path,
        rb_db_path=rb_path,
        lanes=lanes,
        fields=fields,
        only_tracks=None,
    )
    mark_writeback_plan("apply_analysis", writable_plan_hash(plan))


@pytest.mark.requirement("SYNC-05")
def test_every_supported_field_has_verifier(tmp_path: Path) -> None:
    from apps.sync.apply_analysis import _verify_rb_field

    rb_path = tmp_path / "rb.db"
    rb_conn = _write_rb_db(rb_path)
    rb_conn.execute(
        "INSERT INTO djmdContent(ID, BPM, KeyID) VALUES ('rb-1', 12800, 'k1')"
    )
    rb_conn.commit()
    rb_conn.close()

    assert _verify_rb_field(
        sqlite3.connect(str(rb_path)), "rb-1", "key", "8A",
    )
    assert _verify_rb_field(
        sqlite3.connect(str(rb_path)), "rb-1", "bpm", 128.0,
    )

    class _Fake:
        def get_content(self, *, ID: str):
            class _One:
                def one(self_inner):
                    return type("C", (), {"BPM": 12800, "ColorID": 3, "ID": ID})()

            return _One()

    assert _verify_rb_field(_Fake(), "rb-1", "bpm", 128.0)
    assert _verify_rb_field(_Fake(), "rb-1", "energy", 3)

    dat_path = tmp_path / "pqtz.DAT"
    beats = [{"n": 1, "bpm": 128.0, "t": 0.0}, {"n": 2, "bpm": 128.0, "t": 0.469}]
    from apps.sync.analysis_writeback_pqtz import build_minimal_dat

    build_minimal_dat(dat_path, beats)
    rb_conn = sqlite3.connect(str(rb_path))
    rb_conn.execute(
        "UPDATE djmdContent SET AnalysisDataPath = ? WHERE ID = 'rb-1'",
        (str(dat_path),),
    )
    rb_conn.commit()
    assert _verify_rb_field(rb_conn, "rb-1", "pqtz", beats)
    rb_conn.close()


@pytest.mark.requirement("SYNC-05")
@pytest.mark.rekordbox_writeback
def test_writeback_live_exits_when_verifier_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    state_path = tmp_path / "state.db"
    rb_path = tmp_path / "rb.db"
    state_conn = _write_state_db(state_path)
    selection.set_default(state_conn, "key", "own")
    state_conn.execute(
        "INSERT INTO track_vendor_ids VALUES ('t1', 'rekordbox', 'rb-1', NULL)"
    )
    state_conn.execute(
        "INSERT INTO analysis_projection "
        "(stable_id, field, value, status) VALUES ('t1', 'key', '9A', 'ok')"
    )
    state_conn.commit()
    state_conn.close()
    rb_conn = _write_rb_db(rb_path)
    rb_conn.execute(
        "INSERT INTO djmdContent(ID, BPM, KeyID) VALUES ('rb-1', 12800, 'k1')"
    )
    rb_conn.commit()
    rb_conn.close()
    _stamp_writeback(state_path, rb_path, ("key",), ("key",))

    monkeypatch.setattr(paths, "REKORDBOX_PLAIN_DB", rb_path)
    monkeypatch.setattr(paths, "REKORDBOX_LIVE_DB", rb_path)
    monkeypatch.setattr(apply_analysis, "_live_rb_db_path", lambda live: rb_path)
    monkeypatch.setattr("apps.sync.safety._is_running", lambda _n: False)

    from apps.sync.analysis_writeback import verify_scalar as real_verify_scalar

    def _broken_verify(conn, content_id, field, value):
        if field == "key":
            return False
        return real_verify_scalar(conn, content_id, field, value)

    monkeypatch.setattr(
        "apps.sync.analysis_writeback.verify_scalar", _broken_verify,
    )

    rc = main([
        "--live",
        "--i-understand-the-risks",
        "--state-db", str(state_path),
        "--lanes", "key",
        "--fields", "key",
    ])
    assert rc != 0


@pytest.mark.requirement("SYNC-05")
def test_writeback_live_refuses_unpromoted_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    state_path = tmp_path / "state.db"
    rb_path = tmp_path / "rb.db"
    state_conn = _write_state_db(state_path)
    state_conn.execute(
        "INSERT INTO track_vendor_ids VALUES ('t1', 'rekordbox', 'rb-1', NULL)"
    )
    state_conn.execute(
        "INSERT INTO analysis_projection "
        "(stable_id, field, value, status) VALUES ('t1', 'key', '9A', 'ok')"
    )
    state_conn.commit()
    state_conn.close()
    rb_conn = _write_rb_db(rb_path)
    rb_conn.execute(
        "INSERT INTO djmdContent(ID, BPM, KeyID) VALUES ('rb-1', 12800, 'k1')"
    )
    rb_conn.commit()
    rb_conn.close()
    rb_hash = _sha256(rb_path)

    monkeypatch.setattr(paths, "REKORDBOX_PLAIN_DB", rb_path)
    monkeypatch.setattr(paths, "REKORDBOX_LIVE_DB", rb_path)
    monkeypatch.setattr(apply_analysis, "_live_rb_db_path", lambda live: rb_path)

    rc = main([
        "--live",
        "--i-understand-the-risks",
        "--state-db", str(state_path),
        "--lanes", "key",
        "--fields", "key",
    ])
    assert rc == 2
    err = capsys.readouterr().err
    assert "key" in err
    assert "rbx" in err or "not promoted" in err
    assert _sha256(rb_path) == rb_hash


@pytest.mark.requirement("SYNC-05")
def test_writeback_live_ignores_parity02_toggle_on_unpromoted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    selection.reset_toggles()
    state_path = tmp_path / "state.db"
    rb_path = tmp_path / "rb.db"
    state_conn = _write_state_db(state_path)
    selection.set_toggle("key", "own")
    state_conn.execute(
        "INSERT INTO track_vendor_ids VALUES ('t1', 'rekordbox', 'rb-1', NULL)"
    )
    state_conn.execute(
        "INSERT INTO analysis_projection "
        "(stable_id, field, value, status) VALUES ('t1', 'key', '9A', 'ok')"
    )
    state_conn.commit()
    state_conn.close()
    rb_conn = _write_rb_db(rb_path)
    rb_conn.execute(
        "INSERT INTO djmdContent(ID, BPM, KeyID) VALUES ('rb-1', 12800, 'k1')"
    )
    rb_conn.commit()
    rb_conn.close()
    rb_hash = _sha256(rb_path)

    monkeypatch.setattr(paths, "REKORDBOX_PLAIN_DB", rb_path)
    monkeypatch.setattr(paths, "REKORDBOX_LIVE_DB", rb_path)
    monkeypatch.setattr(apply_analysis, "_live_rb_db_path", lambda live: rb_path)

    rc = main([
        "--live",
        "--i-understand-the-risks",
        "--state-db", str(state_path),
        "--lanes", "key",
        "--fields", "key",
    ])
    selection.reset_toggles()
    assert rc == 2
    assert _sha256(rb_path) == rb_hash


@pytest.mark.requirement("SYNC-05")
def test_writeback_live_refuses_without_dry_run_stamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    from apps.sync.safety import writeback_plan_stamp_path

    stamp = writeback_plan_stamp_path("apply_analysis")
    if stamp.exists():
        stamp.unlink()

    state_path = tmp_path / "state.db"
    rb_path = tmp_path / "rb.db"
    state_conn = _write_state_db(state_path)
    selection.set_default(state_conn, "key", "own")
    state_conn.execute(
        "INSERT INTO track_vendor_ids VALUES ('t1', 'rekordbox', 'rb-1', NULL)"
    )
    state_conn.execute(
        "INSERT INTO analysis_projection "
        "(stable_id, field, value, status) VALUES ('t1', 'key', '9A', 'ok')"
    )
    state_conn.commit()
    state_conn.close()
    rb_conn = _write_rb_db(rb_path)
    rb_conn.execute(
        "INSERT INTO djmdContent(ID, BPM, KeyID) VALUES ('rb-1', 12800, 'k1')"
    )
    rb_conn.commit()
    rb_conn.close()
    rb_hash = _sha256(rb_path)

    monkeypatch.setattr(paths, "REKORDBOX_PLAIN_DB", rb_path)
    monkeypatch.setattr(paths, "REKORDBOX_LIVE_DB", rb_path)
    monkeypatch.setattr(apply_analysis, "_live_rb_db_path", lambda live: rb_path)

    rc = main([
        "--live",
        "--i-understand-the-risks",
        "--state-db", str(state_path),
        "--lanes", "key",
        "--fields", "key",
    ])
    assert rc == 3
    err = capsys.readouterr().err
    assert "dry-run" in err.lower()
    assert _sha256(rb_path) == rb_hash


def _seed_own_grid(
    state_path: Path,
    stable_id: str,
    beats: list[dict[str, float | int]],
) -> None:
    from datetime import UTC, datetime

    from apps.analysis.lanes import LaneResult
    from apps.analysis.record import AnalysisRecord
    payload = {
        "beats": beats,
        "bpm": 128.0,
        "bpm_confidence": 1.0,
        "octave_reason": "test",
        "first_downbeat_s": float(beats[0]["t"]) if beats else 0.0,
        "static_grid_untrusted": False,
        "tempo_changes": [],
    }
    record = AnalysisRecord(
        stable_id=stable_id,
        backend="own_beatgrid.backfill",
        backend_version="1.0.0",
        analyzed_at=datetime.now(UTC),
        duration_s=60.0,
        sample_rate=44100,
        bpm=128.0,
        bpm_confidence=1.0,
        key_camelot="8A",
        key_openkey="",
        key_confidence=1.0,
        energy=5,
        producer="backfill",
        producer_version="1.0.0",
        uses_model=True,
        model_sha256="sha256:" + "ab" * 32,
        decode_fingerprint="sha256:" + "cd" * 32,
        lanes={"beatgrid": LaneResult(status="ok", payload=payload)},
    )
    conn = sqlite3.connect(str(state_path))
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS analysis (
            stable_id TEXT, backend TEXT, backend_version TEXT,
            analyzed_at TEXT, duration_s REAL, sample_rate INTEGER,
            bpm REAL, bpm_confidence REAL, key_camelot TEXT, key_openkey TEXT,
            key_confidence REAL, energy INTEGER, energy_source TEXT,
            record_json TEXT,
            PRIMARY KEY (stable_id, backend, backend_version)
        );
        CREATE TABLE IF NOT EXISTS analysis_canonical (
            stable_id TEXT, lane TEXT, backend TEXT, backend_version TEXT,
            updated_at TEXT, PRIMARY KEY (stable_id, lane)
        );
        """
    )
    now = datetime.now(UTC).isoformat()
    record_json = record.to_json()
    conn.execute(
        """
        INSERT OR REPLACE INTO analysis (
            stable_id, backend, backend_version, analyzed_at, duration_s,
            sample_rate, bpm, bpm_confidence, key_camelot, key_openkey,
            key_confidence, energy, energy_source, record_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            stable_id, record.backend, record.backend_version, now,
            record.duration_s, record.sample_rate, record.bpm,
            record.bpm_confidence, record.key_camelot, record.key_openkey,
            record.key_confidence, record.energy, record.energy_source,
            record_json,
        ),
    )
    conn.execute(
        """
        INSERT OR REPLACE INTO analysis_canonical
        (stable_id, lane, backend, backend_version, updated_at)
        VALUES (?, 'beatgrid', ?, ?, ?)
        """,
        (stable_id, record.backend, record.backend_version, now),
    )
    conn.commit()
    conn.close()


@pytest.mark.requirement("SYNC-05")
def test_writeback_live_refuses_unpromoted_pqtz_toggle_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    from apps.sync.analysis_writeback_pqtz import build_minimal_dat

    selection.reset_toggles()
    state_path = tmp_path / "state.db"
    rb_path = tmp_path / "rb.db"
    dat_path = tmp_path / "track.DAT"
    beats = [{"n": (i % 4) + 1, "bpm": 128.0, "t": round(i * 0.46875, 3)} for i in range(8)]
    build_minimal_dat(dat_path, beats[:4])
    dat_hash = _sha256(dat_path)
    state_conn = _write_state_db(state_path)
    selection.set_toggle("beatgrid", "own")
    state_conn.execute(
        "INSERT INTO track_vendor_ids VALUES ('t1', 'rekordbox', 'rb-1', NULL)"
    )
    state_conn.commit()
    state_conn.close()
    _seed_own_grid(state_path, "t1", beats)
    rb_conn = _write_rb_db(rb_path)
    rb_conn.execute(
        "INSERT INTO djmdContent(ID, BPM, KeyID, AnalysisDataPath) "
        "VALUES ('rb-1', 12800, 'k1', ?)",
        (str(dat_path),),
    )
    rb_conn.commit()
    rb_conn.close()

    monkeypatch.setattr(paths, "REKORDBOX_PLAIN_DB", rb_path)
    monkeypatch.setattr(paths, "REKORDBOX_LIVE_DB", rb_path)
    monkeypatch.setattr(apply_analysis, "_live_rb_db_path", lambda live: rb_path)

    rc = main([
        "--live",
        "--i-understand-the-risks",
        "--state-db", str(state_path),
        "--lanes", "beatgrid",
        "--fields", "pqtz",
    ])
    selection.reset_toggles()
    assert rc == 2
    err = capsys.readouterr().err
    assert "beatgrid" in err
    assert "not promoted" in err
    assert _sha256(dat_path) == dat_hash


@pytest.mark.requirement("SYNC-05")
def test_writeback_live_refuses_pqtz_without_stamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    from apps.sync.analysis_writeback_pqtz import build_minimal_dat
    from apps.sync.safety import writeback_plan_stamp_path

    stamp = writeback_plan_stamp_path("apply_analysis")
    if stamp.exists():
        stamp.unlink()

    state_path = tmp_path / "state.db"
    rb_path = tmp_path / "rb.db"
    dat_path = tmp_path / "track.DAT"
    beats = [{"n": (i % 4) + 1, "bpm": 128.0, "t": round(i * 0.46875, 3)} for i in range(8)]
    build_minimal_dat(dat_path, beats[:4])
    dat_hash = _sha256(dat_path)
    state_conn = _write_state_db(state_path)
    selection.set_default(state_conn, "beatgrid", "own")
    state_conn.execute(
        "INSERT INTO track_vendor_ids VALUES ('t1', 'rekordbox', 'rb-1', NULL)"
    )
    state_conn.commit()
    state_conn.close()
    _seed_own_grid(state_path, "t1", beats)
    rb_conn = _write_rb_db(rb_path)
    rb_conn.execute(
        "INSERT INTO djmdContent(ID, BPM, KeyID, AnalysisDataPath) "
        "VALUES ('rb-1', 12800, 'k1', ?)",
        (str(dat_path),),
    )
    rb_conn.commit()
    rb_conn.close()

    monkeypatch.setattr(paths, "REKORDBOX_PLAIN_DB", rb_path)
    monkeypatch.setattr(paths, "REKORDBOX_LIVE_DB", rb_path)
    monkeypatch.setattr(apply_analysis, "_live_rb_db_path", lambda live: rb_path)

    rc = main([
        "--live",
        "--i-understand-the-risks",
        "--state-db", str(state_path),
        "--lanes", "beatgrid",
        "--fields", "pqtz",
    ])
    assert rc == 3
    assert "dry-run" in capsys.readouterr().err.lower()
    assert _sha256(dat_path) == dat_hash


@pytest.mark.requirement("SYNC-05")
@pytest.mark.rekordbox_writeback
def test_writeback_live_does_not_touch_track_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apps.shared import paths

    state_path = tmp_path / "state.db"
    rb_path = tmp_path / "rb.db"
    state_conn = _write_state_db(state_path)
    state_conn.executescript(
        """
        CREATE TABLE track_fields (
            stable_id TEXT, field TEXT, value, source TEXT,
            PRIMARY KEY (stable_id, field)
        );
        CREATE TABLE track_field_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stable_id TEXT, field TEXT, value, source TEXT, changed_at TEXT
        );
        """
    )
    selection.set_default(state_conn, "key", "own")
    selection.set_default(state_conn, "loudness", "own")
    state_conn.execute(
        "INSERT INTO track_vendor_ids VALUES ('t1', 'rekordbox', 'rb-1', NULL)"
    )
    state_conn.execute(
        "INSERT INTO analysis_projection "
        "(stable_id, field, value, status) VALUES ('t1', 'key', '9A', 'ok')"
    )
    state_conn.execute(
        "INSERT INTO analysis_projection "
        "(stable_id, field, value, status) "
        "VALUES ('t1', 'loudness_lufs', -8.2, 'ok')"
    )
    state_conn.commit()
    state_conn.close()
    rb_conn = _write_rb_db(rb_path)
    rb_conn.execute(
        "INSERT INTO djmdContent(ID, BPM, KeyID) VALUES ('rb-1', 12800, 'k1')"
    )
    rb_conn.commit()
    rb_conn.close()
    _stamp_writeback(
        state_path, rb_path, ("key", "loudness"), ("key", "loudness_lufs"),
    )

    monkeypatch.setattr(paths, "REKORDBOX_PLAIN_DB", rb_path)
    monkeypatch.setattr(paths, "REKORDBOX_LIVE_DB", rb_path)
    monkeypatch.setattr(apply_analysis, "_live_rb_db_path", lambda live: rb_path)
    monkeypatch.setattr("apps.sync.safety._is_running", lambda _n: False)
    from apps.shared.state.writer import StateWriter

    monkeypatch.setattr(
        StateWriter,
        "set_field",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("StateWriter.set_field must not be called")
        ),
    )

    rc = main([
        "--live",
        "--i-understand-the-risks",
        "--state-db", str(state_path),
        "--lanes", "key,loudness",
        "--fields", "key,loudness_lufs",
    ])
    assert rc == 0
    check = sqlite3.connect(str(state_path))
    assert check.execute("SELECT COUNT(*) FROM track_fields").fetchone()[0] == 0
    assert check.execute(
        "SELECT COUNT(*) FROM track_field_history"
    ).fetchone()[0] == 0
    check.close()
