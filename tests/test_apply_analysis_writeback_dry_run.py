"""Write-back dry-run (#2048): disk hash, buckets, and denominator.

[if] writeback dry-run runs [then] disk hashes stay unchanged and buckets count right, [else stop].
"""

from __future__ import annotations

from typing import Any

import pytest
import sqlite3
from pathlib import Path

from apps.analysis import selection
from apps.shared import paths
from apps.sync.apply_analysis import main
from tests.apply_analysis_support import (
    _owned_rollout_directory,  # noqa: F401 (autouse fixture, scoped to this module by the import)
    _sha256,
    _write_rb_db,
    _write_state_db,
)


pytestmark = [pytest.mark.requirement("SYNC-05"), pytest.mark.rekordbox_writeback]


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
        state_conn.execute("INSERT INTO track_vendor_ids VALUES ('t1', 'rekordbox', 'rb-1', NULL)")
        state_conn.execute(
            "INSERT INTO analysis_projection (stable_id, field, value, status) VALUES ('t1', 'key', '8A', 'ok')"
        )
        state_conn.commit()
        state_conn.close()

        rb_conn = _write_rb_db(rb_path)
        rb_conn.execute("INSERT INTO djmdContent(ID, BPM, KeyID) VALUES ('rb-1', 12000, 'k1')")
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
            "apps.sync.apply_analysis._run_writeback_live",
            lambda **_k: (_ for _ in ()).throw(AssertionError("_run_writeback_live called")),
        )
        monkeypatch.setattr(
            "apps.shared.paths.copy_live_dbs",
            lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("copy_live_dbs called")),
        )
        monkeypatch.setattr(
            "apps.shared.rekordbox_writeback.require_writeback_enabled",
            lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("require_writeback_enabled called")),
        )

        rc = main(
            [
                "--state-db",
                str(state_path),
                "--rb-db",
                str(rb_path),
                "--lanes",
                "key",
                "--fields",
                "key",
            ]
        )
        assert rc == 0
        assert _sha256(state_path) == state_hash
        assert _sha256(rb_path) == rb_hash
        assert not list(tmp_path.glob("*.bak*"))
        rb_connects = [a for a in connect_args if "rb.db" in a]
        assert rb_connects
        assert all("mode=ro" in a for a in rb_connects)

    def test_buckets_and_denominator(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
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
            "INSERT INTO analysis_projection (stable_id, field, value, status) VALUES ('t1', 'key', '8A', 'ok')"
        )
        state_conn.execute(
            "INSERT INTO analysis_projection (stable_id, field, value, status) VALUES ('t3', 'key', '9A', 'ok')"
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

        rc = main(
            [
                "--state-db",
                str(state_path),
                "--rb-db",
                str(rb_path),
                "--lanes",
                "key",
                "--fields",
                "key",
            ]
        )
        assert rc == 0
        out = capsys.readouterr().out
        label = "tracks resolvable in rekordbox that carry an own value for a promoted lane"
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

    def test_bpm_x100_boundary(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        state_path = tmp_path / "state.db"
        rb_path = tmp_path / "rb.db"
        state_conn = _write_state_db(state_path)
        selection.set_default(state_conn, "beatgrid", "own")
        state_conn.execute("INSERT INTO track_vendor_ids VALUES ('t1', 'rekordbox', 'rb-1', NULL)")
        state_conn.execute(
            "INSERT INTO analysis_projection (stable_id, field, value, status) VALUES ('t1', 'bpm', 129.0, 'ok')"
        )
        state_conn.commit()
        state_conn.close()

        rb_conn = _write_rb_db(rb_path)
        rb_conn.execute("INSERT INTO djmdContent(ID, BPM, KeyID) VALUES ('rb-1', 12800, 'k1')")
        rb_conn.commit()
        rb_conn.close()

        rc = main(
            [
                "--state-db",
                str(state_path),
                "--rb-db",
                str(rb_path),
                "--lanes",
                "beatgrid",
                "--fields",
                "bpm",
            ]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "128.00" in out
        assert "12800" not in out
        assert "+1.00" in out

    def test_tracks_filter_accepts_vendor_id_writable(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        state_path = tmp_path / "state.db"
        rb_path = tmp_path / "rb.db"
        state_conn = _write_state_db(state_path)
        selection.set_default(state_conn, "key", "own")
        state_conn.execute("INSERT INTO track_vendor_ids VALUES ('t1', 'rekordbox', 'rb-1', NULL)")
        state_conn.execute(
            "INSERT INTO analysis_projection (stable_id, field, value, status) VALUES ('t1', 'key', '8A', 'ok')"
        )
        state_conn.commit()
        state_conn.close()

        rb_conn = _write_rb_db(rb_path)
        rb_conn.execute("INSERT INTO djmdContent(ID, BPM, KeyID) VALUES ('rb-1', 12000, 'k1')")
        rb_conn.commit()
        rb_conn.close()

        rc = main(
            [
                "--state-db",
                str(state_path),
                "--rb-db",
                str(rb_path),
                "--lanes",
                "key",
                "--fields",
                "key",
                "--tracks",
                "rb-1",
            ]
        )
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
        state_conn.execute("INSERT INTO track_vendor_ids VALUES ('t1', 'rekordbox', 'rb-gone', NULL)")
        state_conn.execute(
            "INSERT INTO analysis_projection (stable_id, field, value, status) VALUES ('t1', 'bpm', 128.0, 'ok')"
        )
        state_conn.commit()
        state_conn.close()

        rb_conn = _write_rb_db(rb_path)
        rb_conn.commit()
        rb_conn.close()

        rc = main(
            [
                "--state-db",
                str(state_path),
                "--rb-db",
                str(rb_path),
                "--lanes",
                "beatgrid",
                "--fields",
                "bpm",
                "--tracks",
                "rb-gone",
            ]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "denominator: 0" in out
        assert "unmatched: 1" in out
        assert "t1" in out and "unmatched" in out

    def test_main_with_no_flags_is_writeback_dry_run(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        state_path = tmp_path / "state.db"
        rb_path = tmp_path / "rb.db"
        state_conn = _write_state_db(state_path)
        selection.set_default(state_conn, "beatgrid", "own")
        state_conn.execute("INSERT INTO track_vendor_ids VALUES ('t1', 'rekordbox', 'rb-1', NULL)")
        state_conn.execute(
            "INSERT INTO analysis_projection (stable_id, field, value, status) VALUES ('t1', 'bpm', 128.0, 'ok')"
        )
        state_conn.commit()
        state_conn.close()
        rb_conn = _write_rb_db(rb_path)
        rb_conn.execute("INSERT INTO djmdContent(ID, BPM, KeyID) VALUES ('rb-1', 12800, 'k1')")
        rb_conn.commit()
        rb_conn.close()

        monkeypatch.setattr(paths, "STATE_DB", state_path)
        monkeypatch.setattr(paths, "REKORDBOX_PLAIN_DB", rb_path)

        captured: dict[str, Any] = {}

        def _fake_dry_run(**kwargs: Any) -> int:
            captured.update(kwargs)
            return 0

        monkeypatch.setattr(
            "apps.sync.apply_analysis.dry_run",
            _fake_dry_run,
        )
        assert main([]) == 0
        assert captured["state_db"] == state_path
        assert captured["rb_db"] == rb_path
