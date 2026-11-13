"""PQTZ write-back live: promoted anchor, multi-anchor refuse, undo.

[if] PQTZ writeback lives [then] DAT hash, undo and refuse paths hold, [else stop].
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
    _owned_rollout_directory,  # noqa: F401 (autouse fixture, scoped to this module by the import)
    _pin_reversal_root,
    _seed_own_grid,
    _sha256,
    _stamp_writeback,
    _write_rb_db,
    _write_state_db,
)


pytestmark = [pytest.mark.requirement("SYNC-05"), pytest.mark.rekordbox_writeback]


class TestPqtzWritebackLive:
    def _beats(self, count: int = 8, bpm: float = 128.0) -> list[dict[str, float | int]]:
        return [
            {
                "n": (i % 4) + 1,
                "bpm": bpm,
                "t": round(i * 0.46875, 3),
            }
            for i in range(count)
        ]

    def test_promoted_one_anchor_write_readback(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from pyrekordbox.anlz import AnlzFile

        from apps.sync.analysis_writeback_pqtz import (
            build_minimal_dat,
            served_beats_from_own,
            served_beats_from_pqtz_tag,
        )

        state_path = tmp_path / "state.db"
        rb_path = tmp_path / "rb.db"
        dat_path = tmp_path / "track.DAT"
        seed = self._beats(4, 120.0)
        build_minimal_dat(dat_path, seed)
        state_conn = _write_state_db(state_path)
        selection.set_default(state_conn, "beatgrid", "own")
        state_conn.execute("INSERT INTO track_vendor_ids VALUES ('t1', 'rekordbox', 'rb-1', NULL)")
        state_conn.commit()
        state_conn.close()
        _seed_own_grid(state_path, "t1", self._beats())
        rb_conn = _write_rb_db(rb_path)
        rb_conn.execute(
            "INSERT INTO djmdContent(ID, BPM, KeyID, AnalysisDataPath) VALUES ('rb-1', 12800, 'k1', ?)",
            (str(dat_path),),
        )
        rb_conn.commit()
        rb_conn.close()
        _stamp_writeback(state_path, rb_path, ("beatgrid",), ("pqtz",))
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
                    "beatgrid",
                    "--fields",
                    "pqtz",
                ]
            )
            == 0
        )
        own_beats = self._beats()
        pqtz = AnlzFile.parse_file(dat_path).get_tag("PQTZ")
        assert served_beats_from_pqtz_tag(pqtz) == served_beats_from_own(own_beats)

    def test_multi_anchor_refuses_write(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys,
    ) -> None:
        from apps.sync.analysis_writeback_pqtz import build_minimal_dat

        state_path = tmp_path / "state.db"
        rb_path = tmp_path / "rb.db"
        dat_path = tmp_path / "track.DAT"
        build_minimal_dat(dat_path, self._beats(4))
        dat_hash = _sha256(dat_path)
        state_conn = _write_state_db(state_path)
        selection.set_default(state_conn, "beatgrid", "own")
        state_conn.execute("INSERT INTO track_vendor_ids VALUES ('t1', 'rekordbox', 'rb-1', NULL)")
        state_conn.commit()
        state_conn.close()
        changes: tuple[dict[str, object], ...] = (
            {"at_s": 10.0, "bpm_before": 128.0, "bpm_after": 130.0, "confidence": 1.0},
            {"at_s": 20.0, "bpm_before": 130.0, "bpm_after": 132.0, "confidence": 1.0},
        )
        _seed_own_grid(
            state_path,
            "t1",
            self._beats(bpm=128.0),
            tempo_changes=changes,
        )
        rb_conn = _write_rb_db(rb_path)
        rb_conn.execute(
            "INSERT INTO djmdContent(ID, BPM, KeyID, AnalysisDataPath) VALUES ('rb-1', 12800, 'k1', ?)",
            (str(dat_path),),
        )
        rb_conn.commit()
        rb_conn.close()
        assert (
            main(
                [
                    "--state-db",
                    str(state_path),
                    "--rb-db",
                    str(rb_path),
                    "--lanes",
                    "beatgrid",
                    "--fields",
                    "pqtz",
                ]
            )
            == 0
        )
        out = capsys.readouterr().out
        assert "dynamic" in out
        _stamp_writeback(state_path, rb_path, ("beatgrid",), ("pqtz",))
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
                    "beatgrid",
                    "--fields",
                    "pqtz",
                ]
            )
            == 0
        )
        assert _sha256(dat_path) == dat_hash

    def test_bpm_x100_boundary_separate_from_pqtz(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from apps.sync.analysis_writeback_pqtz import build_minimal_dat

        state_path = tmp_path / "state.db"
        rb_path = tmp_path / "rb.db"
        dat_path = tmp_path / "track.DAT"
        build_minimal_dat(dat_path, self._beats(4, 120.0))
        dat_hash = _sha256(dat_path)
        state_conn = _write_state_db(state_path)
        selection.set_default(state_conn, "beatgrid", "own")
        state_conn.execute("INSERT INTO track_vendor_ids VALUES ('t1', 'rekordbox', 'rb-1', NULL)")
        state_conn.execute(
            "INSERT INTO analysis_projection (stable_id, field, value, status) VALUES ('t1', 'bpm', 129.0, 'ok')"
        )
        state_conn.commit()
        state_conn.close()
        _seed_own_grid(state_path, "t1", self._beats(bpm=129.0), bpm=129.0)
        rb_conn = _write_rb_db(rb_path)
        rb_conn.execute(
            "INSERT INTO djmdContent(ID, BPM, KeyID, AnalysisDataPath) VALUES ('rb-1', 12800, 'k1', ?)",
            (str(dat_path),),
        )
        rb_conn.commit()
        rb_conn.close()
        _stamp_writeback(state_path, rb_path, ("beatgrid",), ("bpm", "pqtz"))
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
                    "beatgrid",
                    "--fields",
                    "bpm,pqtz",
                ]
            )
            == 0
        )
        conn = sqlite3.connect(str(rb_path))
        assert conn.execute("SELECT BPM FROM djmdContent WHERE ID = 'rb-1'").fetchone()[0] == 12900
        conn.close()
        _stamp_writeback(state_path, rb_path, ("beatgrid",), ("pqtz",))
        assert (
            main(
                [
                    "--live",
                    "--i-understand-the-risks",
                    "--state-db",
                    str(state_path),
                    "--lanes",
                    "beatgrid",
                    "--fields",
                    "pqtz",
                ]
            )
            == 0
        )
        conn = sqlite3.connect(str(rb_path))
        assert conn.execute("SELECT BPM FROM djmdContent WHERE ID = 'rb-1'").fetchone()[0] == 12900
        conn.close()
        dat_hash_after_pqtz = _sha256(dat_path)
        _stamp_writeback(state_path, rb_path, ("beatgrid",), ("bpm",))
        assert (
            main(
                [
                    "--live",
                    "--i-understand-the-risks",
                    "--state-db",
                    str(state_path),
                    "--lanes",
                    "beatgrid",
                    "--fields",
                    "bpm",
                ]
            )
            == 0
        )
        assert _sha256(dat_path) == dat_hash_after_pqtz

    def test_running_refuses_dat_unchanged(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys,
    ) -> None:
        from apps.sync.analysis_writeback_pqtz import build_minimal_dat

        state_path = tmp_path / "state.db"
        rb_path = tmp_path / "rb.db"
        dat_path = tmp_path / "track.DAT"
        build_minimal_dat(dat_path, self._beats(4))
        dat_hash = _sha256(dat_path)
        state_conn = _write_state_db(state_path)
        selection.set_default(state_conn, "beatgrid", "own")
        state_conn.execute("INSERT INTO track_vendor_ids VALUES ('t1', 'rekordbox', 'rb-1', NULL)")
        state_conn.commit()
        state_conn.close()
        _seed_own_grid(state_path, "t1", self._beats())
        rb_conn = _write_rb_db(rb_path)
        rb_conn.execute(
            "INSERT INTO djmdContent(ID, BPM, KeyID, AnalysisDataPath) VALUES ('rb-1', 12800, 'k1', ?)",
            (str(dat_path),),
        )
        rb_conn.commit()
        rb_conn.close()
        _stamp_writeback(state_path, rb_path, ("beatgrid",), ("pqtz",))
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
        assert (
            main(
                [
                    "--live",
                    "--i-understand-the-risks",
                    "--state-db",
                    str(state_path),
                    "--lanes",
                    "beatgrid",
                    "--fields",
                    "pqtz",
                ]
            )
            == 3
        )
        assert "Rekordbox is running" in capsys.readouterr().err
        assert _sha256(dat_path) == dat_hash

    def test_snapshot_write_restore_roundtrip(self, tmp_path: Path) -> None:
        from apps.sync.analysis_writeback_pqtz import (
            build_minimal_dat,
            restore_pqtz_dat,
            snapshot_pqtz_dat,
            write_pqtz,
        )

        dat_path = tmp_path / "track.DAT"
        build_minimal_dat(dat_path, self._beats(4, 120.0))
        seed_hash = _sha256(dat_path)
        snap = snapshot_pqtz_dat(dat_path)
        assert write_pqtz(dat_path, self._beats())
        assert _sha256(dat_path) != seed_hash
        restore_pqtz_dat(snap)
        assert _sha256(dat_path) == seed_hash

    def test_verify_mismatch_restores_dat(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from apps.sync.analysis_writeback_pqtz import build_minimal_dat

        state_path = tmp_path / "state.db"
        rb_path = tmp_path / "rb.db"
        dat_path = tmp_path / "track.DAT"
        build_minimal_dat(dat_path, self._beats(4, 120.0))
        seed_hash = _sha256(dat_path)
        state_conn = _write_state_db(state_path)
        selection.set_default(state_conn, "beatgrid", "own")
        state_conn.execute("INSERT INTO track_vendor_ids VALUES ('t1', 'rekordbox', 'rb-1', NULL)")
        state_conn.commit()
        state_conn.close()
        _seed_own_grid(state_path, "t1", self._beats())
        rb_conn = _write_rb_db(rb_path)
        rb_conn.execute(
            "INSERT INTO djmdContent(ID, BPM, KeyID, AnalysisDataPath) VALUES ('rb-1', 12800, 'k1', ?)",
            (str(dat_path),),
        )
        rb_conn.commit()
        rb_conn.close()
        _stamp_writeback(state_path, rb_path, ("beatgrid",), ("pqtz",))
        monkeypatch.setattr(paths, "REKORDBOX_PLAIN_DB", rb_path)
        monkeypatch.setattr(paths, "REKORDBOX_LIVE_DB", rb_path)
        monkeypatch.setattr(
            "apps.sync.apply_analysis._live_rb_db_path",
            lambda live: rb_path,
        )
        monkeypatch.setattr("apps.sync.safety._is_running", lambda _n: False)
        monkeypatch.setattr(
            "apps.sync.analysis_writeback_pqtz.verify_pqtz",
            lambda *_a: False,
        )
        assert (
            main(
                [
                    "--live",
                    "--i-understand-the-risks",
                    "--state-db",
                    str(state_path),
                    "--lanes",
                    "beatgrid",
                    "--fields",
                    "pqtz",
                ]
            )
            == 3
        )
        assert _sha256(dat_path) == seed_hash

    def test_undo_restores_original_pqtz(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from apps.sync.analysis_writeback_pqtz import build_minimal_dat

        state_path = tmp_path / "state.db"
        rb_path = tmp_path / "rb.db"
        dat_path = tmp_path / "track.DAT"
        build_minimal_dat(dat_path, self._beats(4, 120.0))
        seed_hash = _sha256(dat_path)
        state_conn = _write_state_db(state_path)
        selection.set_default(state_conn, "beatgrid", "own")
        state_conn.execute("INSERT INTO track_vendor_ids VALUES ('t1', 'rekordbox', 'rb-1', NULL)")
        state_conn.commit()
        state_conn.close()
        _seed_own_grid(state_path, "t1", self._beats())
        rb_conn = _write_rb_db(rb_path)
        rb_conn.execute(
            "INSERT INTO djmdContent(ID, BPM, KeyID, AnalysisDataPath) VALUES ('rb-1', 12800, 'k1', ?)",
            (str(dat_path),),
        )
        rb_conn.commit()
        rb_conn.close()
        _stamp_writeback(state_path, rb_path, ("beatgrid",), ("pqtz",))
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
                    "beatgrid",
                    "--fields",
                    "pqtz",
                ]
            )
            == 0
        )
        preimage_path = _analysis_preimage_path(reversal, dat_path=dat_path)
        assert main(["--undo", str(preimage_path)]) == 0
        assert _sha256(dat_path) == seed_hash
        conn = sqlite3.connect(str(rb_path))
        assert conn.execute("SELECT BPM FROM djmdContent WHERE ID = 'rb-1'").fetchone()[0] == 12800
        conn.close()
