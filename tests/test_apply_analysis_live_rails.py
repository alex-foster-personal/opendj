"""Six-rail live harness: pgrep gate, backup, and verify read-back.

[if] live_run safety rails run [then] pgrep, backup and verify gate failures abort, [else stop].
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from apps.sync.analysis_csv_live import live_run
from apps.sync.apply_analysis import _verify_rb_field
from apps.sync.safety import SafetyAbort
from tests.apply_analysis_support import (
    _FakeContent,
    _FakeRBDB,
    _OneShot,
    _owned_rollout_directory,  # noqa: F401 (autouse fixture, scoped to this module by the import)
)


pytestmark = [pytest.mark.requirement("SYNC-05"), pytest.mark.rekordbox_writeback]


class TestVerifyRBField:
    def test_bpm_match(self) -> None:
        row = _FakeContent("5", bpm=12800)  # 128.00 BPM * 100
        assert _verify_rb_field(_FakeRBDB({"5": row}), "5", "bpm", 128.0)

    def test_bpm_mismatch(self) -> None:
        row = _FakeContent("5", bpm=12000)
        assert not _verify_rb_field(
            _FakeRBDB({"5": row}),
            "5",
            "bpm",
            128.0,
        )

    def test_color_label_is_not_a_synced_field(self) -> None:
        row = _FakeContent("5", color_id=3)
        assert not _verify_rb_field(
            _FakeRBDB({"5": row}),
            "5",
            "color_index",
            3,
        )

    def test_unknown_field_returns_false(self) -> None:
        row = _FakeContent("5")
        assert not _verify_rb_field(
            _FakeRBDB({"5": row}),
            "5",
            "tags",
            "x",
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
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
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
        import apps.shared.rekordbox_db as rb_db_mod

        with pytest.raises(SafetyAbort, match="Rekordbox is running"):
            live_run(
                self._rows(),
                flag_ok=True,
                rb_db_path=rb_db,
                djay_db_path=djay_db,
                open_rb_db=rb_db_mod.open_db,
            )

    def test_backup_created(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        rb_db = tmp_path / "master.db"
        rb_db.write_bytes(b"rb-original")
        djay_db = tmp_path / "ml.db"
        djay_db.write_bytes(b"dj")
        monkeypatch.setattr(
            "apps.sync.safety._is_running",
            lambda _n: False,
        )
        monkeypatch.setattr(
            "apps.shared.rekordbox_db.open_db",
            lambda _p: _FakeRBDB({"5": _FakeContent("5")}),
        )
        import apps.shared.rekordbox_db as rb_db_mod

        rc = live_run(
            self._rows(),
            flag_ok=True,
            rb_db_path=rb_db,
            djay_db_path=djay_db,
            open_rb_db=rb_db_mod.open_db,
        )
        assert rc == 0
        assert list(rb_db.parent.glob("master.db.bak.*"))

    def test_verify_fail_aborts(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        rb_db = tmp_path / "master.db"
        rb_db.write_bytes(b"rb")
        djay_db = tmp_path / "ml.db"
        djay_db.write_bytes(b"dj")
        monkeypatch.setattr(
            "apps.sync.safety._is_running",
            lambda _n: False,
        )
        row = _FakeContent("5", bpm=0)
        db = _FakeRBDB({"5": row})
        monkeypatch.setattr(
            "apps.shared.rekordbox_db.open_db",
            lambda _p: db,
        )
        # Force the writer to report success while the row stays unchanged.
        monkeypatch.setattr(
            "apps.sync.apply_analysis._write_rb_field",
            lambda _db, _cid, _fld, _val: True,
        )
        import apps.shared.rekordbox_db as rb_db_mod

        with pytest.raises(SafetyAbort, match="verify_readback failed"):
            live_run(
                self._rows(),
                flag_ok=True,
                rb_db_path=rb_db,
                djay_db_path=djay_db,
                open_rb_db=rb_db_mod.open_db,
            )
