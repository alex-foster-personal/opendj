"""Phase 4 SYNC-05: apply_analysis CLI unit tests (dry-run + planning + rails).

Covers:
  * dry-run and key-index helpers (existing tests)
  * six-rail safety harness on the live path:
      - rail 1 (pgrep gate)
      - rail 2 (timestamped backup)
      - rail 4 (post-write verify via ``_verify_rb_field``)
"""
from __future__ import annotations

import csv
import hashlib
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from apps.analysis import selection
from apps.shared import paths
from apps.sync.apply_analysis import (
    _camelot_to_djay_key_idx,
    _csv_resolution_summary,
    _live_djay_db_path,
    _live_rb_db_path,
    _verify_rb_field,
    live_run,
    main,
)
from apps.sync.safety import SafetyAbort

# Live-write MECHANICS against tmp fixtures: runs with the one-way rekordbox
# import gate ON (root conftest reads the marker). Never a real rb target.
pytestmark = [pytest.mark.requirement("SYNC-05"), pytest.mark.rekordbox_writeback]


def _make_diff(tmp_path: Path, rows: list[dict]) -> Path:
    path = tmp_path / "analysis-diff.csv"
    with path.open("w", newline="", encoding="utf-8") as fp:
        writer = csv.DictWriter(
            fp,
            fieldnames=[
                "rb_content_id",
                "djay_uuid",
                "field",
                "rb_value",
                "djay_value",
                "resolution",
                "action_hint",
            ],
        )
        writer.writeheader()
        for r in rows:
            writer.writerow(r)
    return path


def test_camelot_to_djay_key_8b_is_c_major():
    assert _camelot_to_djay_key_idx("8B") == 0


def test_camelot_to_djay_key_8a_is_a_minor():
    assert _camelot_to_djay_key_idx("8A") == 21


def test_camelot_to_djay_key_invalid_returns_none():
    assert _camelot_to_djay_key_idx("XX") is None


def test_dry_run_empty_rows(capsys):
    assert _csv_resolution_summary([]) == 0
    assert "no rows" in capsys.readouterr().out


def test_dry_run_summary_per_field(capsys):
    rows = [
        {"field": "bpm", "resolution": "accept_rb"},
        {"field": "bpm", "resolution": "accept_djay"},
        {"field": "tags", "resolution": "no_change"},
    ]
    _csv_resolution_summary(rows)
    out = capsys.readouterr().out
    assert "bpm:" in out
    assert "tags:" in out


def test_main_default_is_dry_run(tmp_path: Path, capsys):
    path = _make_diff(
        tmp_path,
        [
            {
                "rb_content_id": "1",
                "djay_uuid": "a",
                "field": "bpm",
                "rb_value": "128.0",
                "djay_value": "130.0",
                "resolution": "accept_rb",
                "action_hint": "write RB -> djay",
            }
        ],
    )
    assert main(["--diff-csv", str(path)]) == 0
    assert "bpm:" in capsys.readouterr().out


def test_main_bulk_without_flag_aborts(tmp_path: Path):
    path = _make_diff(tmp_path, [])
    assert main(["--diff-csv", str(path), "--live", "--bulk"]) == 2


def test_main_fields_filter_respected(tmp_path: Path):
    path = _make_diff(tmp_path, [])
    assert main(["--diff-csv", str(path), "--fields", "bpm,key_camelot"]) == 0


def test_camelot_round_trip_for_all_keys():
    from apps.shared.djay_db import _DJAY_KEY_IDX_TO_STD
    from apps.shared.harmonic import key_to_camelot

    for idx, std in _DJAY_KEY_IDX_TO_STD.items():
        camelot = str(key_to_camelot(std))
        back = _camelot_to_djay_key_idx(camelot)
        assert back is not None
        assert str(key_to_camelot(_DJAY_KEY_IDX_TO_STD[back])) == camelot


def test_main_missing_csv_is_dry_run(tmp_path: Path, capsys):
    assert main(["--diff-csv", str(tmp_path / "nope.csv")]) == 0


# ---------------------------------------------------- six-rail live tests


class _FakeContent:
    def __init__(self, id_: str, bpm: int = 0, color_id: int = 0) -> None:
        self.ID = id_
        self.BPM = bpm
        self.ColorID = color_id


class _OneShot:
    def __init__(self, value: _FakeContent | None) -> None:
        self._v = value

    def one(self) -> _FakeContent:
        if self._v is None:
            raise LookupError("no row")
        return self._v


class _FakeRBDB:
    def __init__(self, rows: dict[str, _FakeContent]) -> None:
        self._rows = rows
        self.commits = 0

    def get_content(self, *, ID: str) -> _OneShot:
        return _OneShot(self._rows.get(str(ID)))

    def commit(self) -> None:
        self.commits += 1

    def close(self) -> None:  # pragma: no cover - trivial
        pass


class TestVerifyRBField:
    def test_bpm_match(self) -> None:
        row = _FakeContent("5", bpm=12800)  # 128.00 BPM * 100
        assert _verify_rb_field(_FakeRBDB({"5": row}), "5", "bpm", 128.0)

    def test_bpm_mismatch(self) -> None:
        row = _FakeContent("5", bpm=12000)
        assert not _verify_rb_field(
            _FakeRBDB({"5": row}), "5", "bpm", 128.0,
        )

    def test_energy_match(self) -> None:
        row = _FakeContent("5", color_id=3)
        assert _verify_rb_field(
            _FakeRBDB({"5": row}), "5", "energy", 3,
        )

    def test_unknown_field_returns_false(self) -> None:
        row = _FakeContent("5")
        assert not _verify_rb_field(
            _FakeRBDB({"5": row}), "5", "tags", "x",
        )

    def test_missing_row_returns_false(self) -> None:
        assert not _verify_rb_field(_FakeRBDB({}), "missing", "bpm", 128.0)


class TestAnalysisLiveRBRails:
    """Rails 1 (pgrep) + 2 (backup) + 4 (verify) for live_run RB path."""

    def _rows(self) -> list[dict]:
        # accept_djay means write the djay value (130.0) into RB.
        return [
            {
                "rb_content_id": "5",
                "djay_uuid": "d-5",
                "field": "bpm",
                "rb_value": "128.0",
                "djay_value": "130.0",
                "resolution": "accept_djay",
                "action_hint": "",
            }
        ]

    def test_pgrep_aborts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        rb_db = tmp_path / "master.db"
        rb_db.write_bytes(b"rb")
        djay_db = tmp_path / "ml.db"
        djay_db.write_bytes(b"dj")
        monkeypatch.setattr(
            "apps.sync.safety._is_running",
            lambda name: name == "Rekordbox",
        )
        monkeypatch.setattr(
            "apps.shared.rekordbox_db.open_db",
            lambda _p: _FakeRBDB({"5": _FakeContent("5")}),
        )
        with pytest.raises(SafetyAbort, match="Rekordbox is running"):
            live_run(
                self._rows(),
                flag_ok=True,
                rb_db_path=rb_db,
                djay_db_path=djay_db,
            )

    def test_backup_created(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        rb_db = tmp_path / "master.db"
        rb_db.write_bytes(b"rb-original")
        djay_db = tmp_path / "ml.db"
        djay_db.write_bytes(b"dj")
        monkeypatch.setattr(
            "apps.sync.safety._is_running", lambda _n: False,
        )
        monkeypatch.setattr(
            "apps.shared.rekordbox_db.open_db",
            lambda _p: _FakeRBDB({"5": _FakeContent("5")}),
        )
        rc = live_run(
            self._rows(),
            flag_ok=True,
            rb_db_path=rb_db,
            djay_db_path=djay_db,
        )
        assert rc == 0
        assert list(rb_db.parent.glob("master.db.bak.*"))

    def test_verify_fail_aborts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        rb_db = tmp_path / "master.db"
        rb_db.write_bytes(b"rb")
        djay_db = tmp_path / "ml.db"
        djay_db.write_bytes(b"dj")
        monkeypatch.setattr(
            "apps.sync.safety._is_running", lambda _n: False,
        )
        row = _FakeContent("5", bpm=0)
        db = _FakeRBDB({"5": row})
        monkeypatch.setattr(
            "apps.shared.rekordbox_db.open_db", lambda _p: db,
        )
        # Force the writer to report success while the row stays unchanged.
        monkeypatch.setattr(
            "apps.sync.apply_analysis._write_rb_field",
            lambda _db, _cid, _fld, _val: True,
        )
        with pytest.raises(SafetyAbort, match="verify_readback failed"):
            live_run(
                self._rows(),
                flag_ok=True,
                rb_db_path=rb_db,
                djay_db_path=djay_db,
            )


# ---------------------------------------------------- live-DB path routing (#1)


class TestLiveDbPathHelpers:
    """``_live_rb_db_path`` / ``_live_djay_db_path`` mirror the
    ``apply_ratings`` pattern: ``--live`` routes to LIVE_DB constants,
    otherwise to WORKING_DB copies under ``data/``.
    """

    # This module runs with the rekordbox writeback gate ON (see pytestmark),
    # so the constants are REDIRECTED onto tmp_path rather than read: comparing
    # a global to itself proved nothing anyway, while naming the real live DB
    # inside a gate-on module is one careless edit away from opening it.
    @staticmethod
    def _redirect(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Path]:
        fixtures = {
            name: tmp_path / f"{name.lower()}.db"
            for name in (
                "REKORDBOX_LIVE_DB", "REKORDBOX_WORKING_DB",
                "DJAY_LIVE_DB", "DJAY_WORKING_DB",
            )
        }
        for name, target in fixtures.items():
            monkeypatch.setattr(paths, name, target)
        return fixtures

    def test_rb_live_flag_returns_live_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fixtures = self._redirect(monkeypatch, tmp_path)
        assert _live_rb_db_path(True) == fixtures["REKORDBOX_LIVE_DB"]

    def test_rb_default_returns_working_copy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fixtures = self._redirect(monkeypatch, tmp_path)
        assert _live_rb_db_path(False) == fixtures["REKORDBOX_WORKING_DB"]

    def test_djay_live_flag_returns_live_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fixtures = self._redirect(monkeypatch, tmp_path)
        assert _live_djay_db_path(True) == fixtures["DJAY_LIVE_DB"]

    def test_djay_default_returns_working_copy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fixtures = self._redirect(monkeypatch, tmp_path)
        assert _live_djay_db_path(False) == fixtures["DJAY_WORKING_DB"]


class TestMainLiveRoutesToLiveDbPaths:
    """P0 regression (adversarial #1, CRITICAL): ``main(['--live', ...])``
    MUST pass LIVE DB paths into ``live_run``. Pre-fix the kwargs were
    omitted and every live write silently wrote to the WORKING DB copy.
    """

    def test_main_live_routes_to_live_db_paths(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fake_rb_live = tmp_path / "fake_rb_live.db"
        fake_rb_live.write_bytes(b"rb-live")
        fake_djay_live = tmp_path / "fake_djay_live.db"
        fake_djay_live.write_bytes(b"djay-live")

        monkeypatch.setattr(paths, "REKORDBOX_LIVE_DB", fake_rb_live)
        monkeypatch.setattr(paths, "DJAY_LIVE_DB", fake_djay_live)

        captured: dict[str, Any] = {}

        def _fake_live_run(rows: list[dict], **kwargs: Any) -> int:
            captured["rows"] = rows
            captured["kwargs"] = kwargs
            return 0

        monkeypatch.setattr(
            "apps.sync.apply_analysis.live_run", _fake_live_run,
        )

        path = _make_diff(
            tmp_path,
            [
                {
                    "rb_content_id": "10",
                    "djay_uuid": "u-10",
                    "field": "bpm",
                    "rb_value": "128.0",
                    "djay_value": "130.0",
                    "resolution": "accept_djay",
                    "action_hint": "",
                }
            ],
        )
        rc = main(
            [
                "--diff-csv", str(path),
                "--live",
                "--i-understand-the-risks",
            ]
        )
        assert rc == 0
        assert captured["kwargs"]["rb_db_path"] == fake_rb_live
        assert captured["kwargs"]["djay_db_path"] == fake_djay_live


# ---------------------------------------------------- write-back dry-run (#2048)


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


class TestWritebackDryRun:
    def test_disk_unchanged_and_ro_connect(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
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
            "(stable_id, field, value, status) VALUES ('t1', 'key', '8A', 'ok')"
        )
        state_conn.commit()
        state_conn.close()

        rb_conn = _write_rb_db(rb_path)
        rb_conn.execute(
            "INSERT INTO djmdContent(ID, BPM, KeyID) VALUES ('rb-1', 12000, 'k1')"
        )
        rb_conn.commit()
        rb_conn.close()

        state_path.write_bytes(state_path.read_bytes() + b"\x00")
        rb_path.write_bytes(rb_path.read_bytes() + b"\x01")
        state_hash = _sha256(state_path)
        rb_hash = _sha256(rb_path)

        connect_args: list[str] = []
        real_connect = sqlite3.connect

        def _recording_connect(arg: str, *args: Any, **kwargs: Any) -> sqlite3.Connection:
            connect_args.append(str(arg))
            return real_connect(arg, *args, **kwargs)

        monkeypatch.setattr(sqlite3, "connect", _recording_connect)
        monkeypatch.setattr(
            "apps.sync.safety.backup_db",
            lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("backup_db called")),
        )
        monkeypatch.setattr(
            "apps.sync.safety.LiveWriteSession",
            type(
                "X",
                (),
                {"__init__": lambda *_a, **_k: (_ for _ in ()).throw(
                    AssertionError("LiveWriteSession called")
                )},
            ),
        )
        monkeypatch.setattr(
            "apps.shared.paths.copy_live_dbs",
            lambda *_a, **_k: (_ for _ in ()).throw(
                AssertionError("copy_live_dbs called")
            ),
        )
        monkeypatch.setattr(
            "apps.shared.rekordbox_writeback.require_writeback_enabled",
            lambda *_a, **_k: (_ for _ in ()).throw(
                AssertionError("require_writeback_enabled called")
            ),
        )

        rc = main([
            "--state-db", str(state_path),
            "--rb-db", str(rb_path),
            "--lanes", "key",
            "--fields", "key",
        ])
        assert rc == 0
        assert _sha256(state_path) == state_hash
        assert _sha256(rb_path) == rb_hash
        assert not list(tmp_path.glob("*.bak*"))
        rb_connects = [a for a in connect_args if "rb.db" in a]
        assert rb_connects
        assert all("mode=ro" in a for a in rb_connects)

    def test_buckets_and_denominator(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        state_path = tmp_path / "state.db"
        rb_path = tmp_path / "rb.db"
        state_conn = _write_state_db(state_path)
        selection.set_default(state_conn, "key", "own")
        state_conn.execute("INSERT INTO tracks VALUES ('t4', NULL)")
        state_conn.executemany(
            "INSERT INTO track_vendor_ids VALUES (?, 'rekordbox', ?, NULL)",
            [("t1", "rb-1"), ("t2", "rb-2")],
        )
        state_conn.execute(
            "INSERT INTO analysis_projection "
            "(stable_id, field, value, status) VALUES ('t1', 'key', '8A', 'ok')"
        )
        state_conn.execute(
            "INSERT INTO analysis_projection "
            "(stable_id, field, value, status) VALUES ('t3', 'key', '9A', 'ok')"
        )
        state_conn.commit()
        state_conn.close()

        rb_conn = _write_rb_db(rb_path)
        rb_conn.executemany(
            "INSERT INTO djmdContent(ID, BPM, KeyID) VALUES (?, 12000, 'k1')",
            [("rb-1",), ("rb-2",)],
        )
        rb_conn.commit()
        rb_conn.close()

        rc = main([
            "--state-db", str(state_path),
            "--rb-db", str(rb_path),
            "--lanes", "key",
            "--fields", "key",
        ])
        assert rc == 0
        out = capsys.readouterr().out
        label = (
            "tracks resolvable in rekordbox that carry an own value for a promoted lane"
        )
        assert label in out
        assert "denominator: 1" in out
        assert "writable" in out
        assert "no-own-value" in out
        assert "unmatched" in out
        assert "rbx" in out.lower() or "rbx     own" in out
        assert "delta" in out
        assert "lane" in out
        assert "promoted" in out
        assert "last_round" in out
        assert "t1" in out and "writable" in out
        assert "t2" in out and "no-own-value" in out
        assert "t3" in out and "unmatched" in out

    def test_bpm_x100_boundary(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
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
            "(stable_id, field, value, status) VALUES ('t1', 'bpm', 129.0, 'ok')"
        )
        state_conn.commit()
        state_conn.close()

        rb_conn = _write_rb_db(rb_path)
        rb_conn.execute(
            "INSERT INTO djmdContent(ID, BPM, KeyID) VALUES ('rb-1', 12800, 'k1')"
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
        assert "128.00" in out
        assert "12800" not in out
        assert "+1.00" in out

    def test_tracks_filter_accepts_vendor_id_writable(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
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
            "(stable_id, field, value, status) VALUES ('t1', 'key', '8A', 'ok')"
        )
        state_conn.commit()
        state_conn.close()

        rb_conn = _write_rb_db(rb_path)
        rb_conn.execute(
            "INSERT INTO djmdContent(ID, BPM, KeyID) VALUES ('rb-1', 12000, 'k1')"
        )
        rb_conn.commit()
        rb_conn.close()

        rc = main([
            "--state-db", str(state_path),
            "--rb-db", str(rb_path),
            "--lanes", "key",
            "--fields", "key",
            "--tracks", "rb-1",
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "denominator: 1" in out
        assert "writable: 1" in out
        assert "t1" in out and "writable" in out

    def test_tracks_filter_vendor_id_missing_from_rbx_is_unmatched(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        state_path = tmp_path / "state.db"
        rb_path = tmp_path / "rb.db"
        state_conn = _write_state_db(state_path)
        selection.set_default(state_conn, "beatgrid", "own")
        state_conn.execute(
            "INSERT INTO track_vendor_ids VALUES ('t1', 'rekordbox', 'rb-gone', NULL)"
        )
        state_conn.execute(
            "INSERT INTO analysis_projection "
            "(stable_id, field, value, status) VALUES ('t1', 'bpm', 128.0, 'ok')"
        )
        state_conn.commit()
        state_conn.close()

        rb_conn = _write_rb_db(rb_path)
        rb_conn.commit()
        rb_conn.close()

        rc = main([
            "--state-db", str(state_path),
            "--rb-db", str(rb_path),
            "--lanes", "beatgrid",
            "--fields", "bpm",
            "--tracks", "rb-gone",
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "denominator: 0" in out
        assert "unmatched: 1" in out
        assert "t1" in out and "unmatched" in out

    def test_main_with_no_flags_is_writeback_dry_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
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

        monkeypatch.setattr(paths, "STATE_DB", state_path)
        monkeypatch.setattr(paths, "REKORDBOX_PLAIN_DB", rb_path)

        captured: dict[str, Any] = {}

        def _fake_writeback_dry_run(**kwargs: Any) -> int:
            captured.update(kwargs)
            return 0

        monkeypatch.setattr(
            "apps.sync.apply_analysis.writeback_dry_run",
            _fake_writeback_dry_run,
        )
        assert main([]) == 0
        assert captured["state_db"] == state_path
        assert captured["rb_db"] == rb_path
