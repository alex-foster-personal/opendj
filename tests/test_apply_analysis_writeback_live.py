"""Write-back live (#2049): key, loudness, pgrep refusal, and verify restore.

[if] promoted writeback lives [then] key and loudness round-trip and undo restore RB rows, [else stop].
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from apps.analysis import selection
from apps.shared import paths
from apps.sync.apply_analysis import main
from tests.apply_analysis_support import (
    _analysis_preimage_path,
    _key_camelot,
    _owned_rollout_directory,  # noqa: F401 (autouse fixture, scoped to this module by the import)
    _pin_reversal_root,
    _sha256,
    _stamp_writeback,
    _write_rb_db,
    _write_state_db,
)


pytestmark = [pytest.mark.requirement("SYNC-05"), pytest.mark.rekordbox_writeback]


class TestWritebackLive:
    def test_key_write_readback_and_undo(
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
            "INSERT INTO analysis_projection (stable_id, field, value, status) VALUES ('t1', 'key', '9A', 'ok')"
        )
        state_conn.commit()
        state_conn.close()

        rb_conn = _write_rb_db(rb_path)
        rb_conn.execute("INSERT INTO djmdContent(ID, BPM, KeyID) VALUES ('rb-1', 12800, 'k1')")
        rb_conn.execute("INSERT INTO djmdContent(ID, BPM, KeyID) VALUES ('rb-2', 12000, 'k1')")
        rb_conn.commit()
        rb_conn.close()
        _stamp_writeback(state_path, rb_path, ("key",), ("key",))

        monkeypatch.setattr(paths, "REKORDBOX_PLAIN_DB", rb_path)
        monkeypatch.setattr(paths, "REKORDBOX_LIVE_DB", rb_path)
        monkeypatch.setattr(
            "apps.sync.apply_analysis._live_rb_db_path",
            lambda live: rb_path,
        )
        monkeypatch.setattr("apps.sync.safety._is_running", lambda _n: False)
        reversal = _pin_reversal_root(tmp_path, monkeypatch)

        assert (
            main(
                [
                    "--live",
                    "--i-understand-the-risks",
                    "--state-db",
                    str(state_path),
                    "--lanes",
                    "key",
                    "--fields",
                    "key",
                ]
            )
            == 0
        )

        conn = sqlite3.connect(str(rb_path))
        assert _key_camelot(conn, "rb-1") == "9A"
        k1_scale = conn.execute("SELECT ScaleName FROM djmdKey WHERE ID = 'k1'").fetchone()[0]
        assert k1_scale == "Am"
        assert _key_camelot(conn, "rb-2") == "8A"
        preimage_path = _analysis_preimage_path(reversal, content_id="rb-1")
        conn.close()

        assert main(["--undo", str(preimage_path)]) == 0
        conn = sqlite3.connect(str(rb_path))
        row = conn.execute("SELECT KeyID FROM djmdContent WHERE ID = 'rb-1'").fetchone()
        assert row[0] == "k1"
        assert _key_camelot(conn, "rb-1") == "8A"
        conn.close()

    def test_loudness_write_readback_and_undo(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        state_path = tmp_path / "state.db"
        rb_path = tmp_path / "rb.db"
        state_conn = _write_state_db(state_path)
        selection.set_default(state_conn, "loudness", "own")
        state_conn.execute("INSERT INTO track_vendor_ids VALUES ('t1', 'rekordbox', 'rb-1', NULL)")
        state_conn.execute(
            "INSERT INTO analysis_projection "
            "(stable_id, field, value, status) "
            "VALUES ('t1', 'loudness_lufs', -8.2, 'ok')"
        )
        state_conn.execute(
            "INSERT INTO analysis_projection "
            "(stable_id, field, value, status) "
            "VALUES ('t1', 'loudness_dbtp', -0.5, 'ok')"
        )
        state_conn.commit()
        state_conn.close()

        rb_conn = _write_rb_db(rb_path)
        rb_conn.executescript(
            """
            ALTER TABLE djmdContent ADD COLUMN Commnt TEXT;
            INSERT INTO djmdContent(ID, BPM, KeyID, Commnt)
            VALUES ('rb-1', 12800, 'k1', 'user comment');
            """
        )
        rb_conn.commit()
        pre_hash = _sha256(rb_path)
        rb_conn.close()
        _stamp_writeback(
            state_path,
            rb_path,
            ("loudness",),
            ("loudness_lufs", "loudness_dbtp"),
        )

        monkeypatch.setattr(paths, "REKORDBOX_PLAIN_DB", rb_path)
        monkeypatch.setattr(paths, "REKORDBOX_LIVE_DB", rb_path)
        monkeypatch.setattr(
            "apps.sync.apply_analysis._live_rb_db_path",
            lambda live: rb_path,
        )
        monkeypatch.setattr("apps.sync.safety._is_running", lambda _n: False)
        reversal = _pin_reversal_root(tmp_path, monkeypatch)

        assert (
            main(
                [
                    "--live",
                    "--i-understand-the-risks",
                    "--state-db",
                    str(state_path),
                    "--lanes",
                    "loudness",
                    "--fields",
                    "loudness_lufs,loudness_dbtp",
                ]
            )
            == 0
        )

        conn = sqlite3.connect(str(rb_path))
        assert conn.execute("SELECT Commnt FROM djmdContent WHERE ID = 'rb-1'").fetchone()[0] == "user comment"
        lufs = conn.execute(
            "SELECT value FROM odjAnalysisScalar WHERE ContentID = 'rb-1' AND field = 'loudness_lufs'"
        ).fetchone()[0]
        dbtp = conn.execute(
            "SELECT value FROM odjAnalysisScalar WHERE ContentID = 'rb-1' AND field = 'loudness_dbtp'"
        ).fetchone()[0]
        assert abs(float(lufs) - (-8.2)) < 1e-6
        assert abs(float(dbtp) - (-0.5)) < 1e-6
        preimage_path = _analysis_preimage_path(reversal, content_id="rb-1")
        conn.close()

        assert main(["--undo", str(preimage_path)]) == 0
        conn = sqlite3.connect(str(rb_path))
        assert conn.execute("SELECT COUNT(*) FROM odjAnalysisScalar WHERE ContentID = 'rb-1'").fetchone()[0] == 0
        conn.close()

    def test_writeback_live_refuses_when_rekordbox_running(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        state_path = tmp_path / "state.db"
        rb_path = tmp_path / "rb.db"
        state_conn = _write_state_db(state_path)
        selection.set_default(state_conn, "key", "own")
        state_conn.execute("INSERT INTO track_vendor_ids VALUES ('t1', 'rekordbox', 'rb-1', NULL)")
        state_conn.execute(
            "INSERT INTO analysis_projection (stable_id, field, value, status) VALUES ('t1', 'key', '9A', 'ok')"
        )
        state_conn.commit()
        state_conn.close()
        rb_conn = _write_rb_db(rb_path)
        rb_conn.execute("INSERT INTO djmdContent(ID, BPM, KeyID) VALUES ('rb-1', 12800, 'k1')")
        rb_conn.commit()
        rb_conn.close()
        rb_hash = _sha256(rb_path)
        _stamp_writeback(state_path, rb_path, ("key",), ("key",))

        monkeypatch.setattr(paths, "REKORDBOX_PLAIN_DB", rb_path)
        monkeypatch.setattr(paths, "REKORDBOX_LIVE_DB", rb_path)
        monkeypatch.setattr(
            "apps.sync.apply_analysis._live_rb_db_path",
            lambda live: rb_path,
        )
        monkeypatch.setattr(
            "apps.sync.safety._is_running",
            lambda name: name == "Rekordbox",
        )

        rc = main(
            [
                "--live",
                "--i-understand-the-risks",
                "--state-db",
                str(state_path),
                "--lanes",
                "key",
                "--fields",
                "key",
            ]
        )
        assert rc == 3
        assert "Rekordbox is running" in capsys.readouterr().err
        assert _sha256(rb_path) == rb_hash

    def test_verify_mismatch_restores_preimage(
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
            "INSERT INTO analysis_projection (stable_id, field, value, status) VALUES ('t1', 'key', '9A', 'ok')"
        )
        state_conn.commit()
        state_conn.close()
        rb_conn = _write_rb_db(rb_path)
        rb_conn.execute("INSERT INTO djmdContent(ID, BPM, KeyID) VALUES ('rb-1', 12800, 'k1')")
        rb_conn.commit()
        rb_conn.close()
        _stamp_writeback(state_path, rb_path, ("key",), ("key",))

        monkeypatch.setattr(paths, "REKORDBOX_PLAIN_DB", rb_path)
        monkeypatch.setattr(paths, "REKORDBOX_LIVE_DB", rb_path)
        monkeypatch.setattr(
            "apps.sync.apply_analysis._live_rb_db_path",
            lambda live: rb_path,
        )
        monkeypatch.setattr("apps.sync.safety._is_running", lambda _n: False)

        from apps.sync.analysis_writeback import verify_scalar

        real_verify = verify_scalar

        def _fail_verify(conn, content_id, field, value):
            if field == "key":
                return False
            return real_verify(conn, content_id, field, value)

        monkeypatch.setattr(
            "apps.sync.analysis_writeback.verify_scalar",
            _fail_verify,
        )

        rc = main(
            [
                "--live",
                "--i-understand-the-risks",
                "--state-db",
                str(state_path),
                "--lanes",
                "key",
                "--fields",
                "key",
            ]
        )
        assert rc == 3
        conn = sqlite3.connect(str(rb_path))
        assert conn.execute("SELECT KeyID FROM djmdContent WHERE ID = 'rb-1'").fetchone()[0] == "k1"
        conn.close()

    def test_key_write_leaves_bpm_x100(
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
            "INSERT INTO analysis_projection (stable_id, field, value, status) VALUES ('t1', 'key', '9A', 'ok')"
        )
        state_conn.commit()
        state_conn.close()
        rb_conn = _write_rb_db(rb_path)
        rb_conn.execute("INSERT INTO djmdContent(ID, BPM, KeyID) VALUES ('rb-1', 12800, 'k1')")
        rb_conn.commit()
        rb_conn.close()
        _stamp_writeback(state_path, rb_path, ("key",), ("key",))

        monkeypatch.setattr(paths, "REKORDBOX_PLAIN_DB", rb_path)
        monkeypatch.setattr(paths, "REKORDBOX_LIVE_DB", rb_path)
        monkeypatch.setattr(
            "apps.sync.apply_analysis._live_rb_db_path",
            lambda live: rb_path,
        )
        monkeypatch.setattr("apps.sync.safety._is_running", lambda _n: False)

        assert (
            main(
                [
                    "--live",
                    "--i-understand-the-risks",
                    "--state-db",
                    str(state_path),
                    "--lanes",
                    "key",
                    "--fields",
                    "key",
                ]
            )
            == 0
        )

        conn = sqlite3.connect(str(rb_path))
        assert conn.execute("SELECT BPM FROM djmdContent WHERE ID = 'rb-1'").fetchone()[0] == 12800
        conn.close()
