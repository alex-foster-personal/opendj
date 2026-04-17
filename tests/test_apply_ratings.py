"""Phase 4 SYNC-06: apply_ratings CLI unit tests (dry-run + planning + rails).

Covers:
  * dry-run / CLI-surface behaviour (existing tests)
  * six-rail safety harness on the live path:
      - rail 1 (pgrep gate)   -- live_run aborts when Rekordbox is running
      - rail 2 (backup)       -- LiveWriteSession backs up the target DB
                                  before any rating write occurs
      - rail 4 (post-verify)  -- verifier callback is wired into the
                                  session and trips abort on mismatch
  * ``_verify_rb_rating`` / ``_verify_djay_rating`` helpers
"""
from __future__ import annotations

import csv
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from apps.shared import paths
from apps.sync.apply_ratings import (
    _live_djay_db_path,
    _live_rb_db_path,
    _summarise_plan,
    _verify_djay_rating,
    _verify_rb_rating,
    dry_run,
    live_run,
    main,
)
from apps.sync.safety import SafetyAbort

pytestmark = pytest.mark.requirement("SYNC-06")


def _make_diff(tmp_path: Path, rows: list[dict]) -> Path:
    path = tmp_path / "ratings-diff.csv"
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


def test_dry_run_empty_rows_returns_zero(capsys):
    assert dry_run([]) == 0
    assert "no rows" in capsys.readouterr().out


def test_dry_run_summary_counts_resolutions(capsys):
    rows = [
        {"resolution": "accept_rb"},
        {"resolution": "accept_rb"},
        {"resolution": "accept_djay"},
        {"resolution": "conflict"},
        {"resolution": "no_change"},
    ]
    dry_run(rows)
    out = capsys.readouterr().out
    assert "accept_rb: 2" in out
    assert "accept_djay: 1" in out
    assert "conflict: 1" in out


def test_summarise_plan_keys():
    counts = _summarise_plan(
        [{"resolution": "accept_rb"}, {"resolution": "accept_rb"}]
    )
    assert counts == {"accept_rb": 2}


def test_main_dry_run_reads_csv(tmp_path: Path, capsys):
    path = _make_diff(
        tmp_path,
        [
            {
                "rb_content_id": "1",
                "djay_uuid": "a",
                "field": "rating",
                "rb_value": "0",
                "djay_value": "5",
                "resolution": "accept_djay",
                "action_hint": "write djay -> RB",
            }
        ],
    )
    assert main(["--diff-csv", str(path)]) == 0
    assert "accept_djay: 1" in capsys.readouterr().out


def test_main_bulk_without_flag_aborts(tmp_path: Path):
    path = _make_diff(tmp_path, [])
    assert main(["--diff-csv", str(path), "--live", "--bulk"]) == 2


def test_main_missing_csv_returns_empty_summary(tmp_path: Path, capsys):
    missing = tmp_path / "nope.csv"
    assert main(["--diff-csv", str(missing)]) == 0
    assert "no rows" in capsys.readouterr().out


def test_main_tracks_filter_dry_run(tmp_path: Path):
    path = _make_diff(
        tmp_path,
        [
            {
                "rb_content_id": "1",
                "djay_uuid": "a",
                "field": "rating",
                "rb_value": "5",
                "djay_value": "0",
                "resolution": "accept_rb",
                "action_hint": "",
            }
        ],
    )
    assert main(["--diff-csv", str(path), "--tracks", "a"]) == 0


# ---------------------------------------------------- six-rail live tests


class _FakeContent:
    """Duck-typed stand-in for pyrekordbox ``DjmdContent`` rows."""

    def __init__(self, id_: str, rating: int = 0) -> None:
        self.ID = id_
        self.Rating = rating


class _OneShotQuery:
    """Duck-typed ``.one()`` scalar wrapper."""

    def __init__(self, value: _FakeContent | None) -> None:
        self._value = value

    def one(self) -> _FakeContent:
        if self._value is None:
            raise LookupError("not found")
        return self._value


class _FakeRBDB:
    """Minimal ``open_db``-compatible stub for rating writes."""

    def __init__(self, contents: dict[str, _FakeContent]) -> None:
        self._contents = contents
        self.commits = 0

    def get_content(self, *, ID: str) -> _OneShotQuery:
        return _OneShotQuery(self._contents.get(str(ID)))

    def commit(self) -> None:
        self.commits += 1

    def close(self) -> None:  # pragma: no cover - trivial
        pass


class TestVerifyRBRating:
    def test_matches_written_rating(self) -> None:
        db = _FakeRBDB({"10": _FakeContent("10", rating=5)})
        assert _verify_rb_rating(db, "10", 5) is True

    def test_mismatch_fails(self) -> None:
        db = _FakeRBDB({"10": _FakeContent("10", rating=3)})
        assert _verify_rb_rating(db, "10", 5) is False

    def test_missing_row_fails(self) -> None:
        db = _FakeRBDB({})
        assert _verify_rb_rating(db, "99", 5) is False


class TestLiveRunRBRails:
    """Rail 1 (pgrep) + Rail 2 (backup) for the RB side of live_run."""

    def _rows_accept_djay(self) -> list[dict]:
        # resolution=accept_djay -> writes djay_value into RB.
        return [
            {
                "rb_content_id": "10",
                "djay_uuid": "u-10",
                "field": "rating",
                "rb_value": "0",
                "djay_value": "5",
                "resolution": "accept_djay",
                "action_hint": "",
            }
        ]

    def test_pgrep_gate_blocks_writes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        rb_db = tmp_path / "master.db"
        rb_db.write_bytes(b"SQLite fake rb")
        djay_db = tmp_path / "MediaLibrary.db"
        djay_db.write_bytes(b"SQLite fake djay")

        # Rekordbox reports running -> SafetyAbort.
        monkeypatch.setattr(
            "apps.sync.safety._is_running",
            lambda name: name == "Rekordbox",
        )
        # open_db should never be reached, but guard anyway.
        db = _FakeRBDB({"10": _FakeContent("10", rating=0)})
        monkeypatch.setattr(
            "apps.shared.rekordbox_db.open_db", lambda _p: db,
        )

        with pytest.raises(SafetyAbort, match="Rekordbox is running"):
            live_run(
                self._rows_accept_djay(),
                flag_ok=True,
                rb_db_path=rb_db,
                djay_db_path=djay_db,
            )
        # Fake DB was opened but no rating was applied.
        assert db.commits == 0

    def test_backup_file_created_before_write(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        rb_db = tmp_path / "master.db"
        rb_db.write_bytes(b"original rb bytes 123")
        djay_db = tmp_path / "MediaLibrary.db"
        djay_db.write_bytes(b"djay bytes")

        monkeypatch.setattr(
            "apps.sync.safety._is_running", lambda _name: False,
        )
        db = _FakeRBDB({"10": _FakeContent("10", rating=0)})
        monkeypatch.setattr(
            "apps.shared.rekordbox_db.open_db", lambda _p: db,
        )

        rc = live_run(
            self._rows_accept_djay(),
            flag_ok=True,
            rb_db_path=rb_db,
            djay_db_path=djay_db,
        )
        assert rc == 0
        backups = list(rb_db.parent.glob("master.db.bak.*"))
        assert len(backups) == 1
        assert backups[0].read_bytes() == b"original rb bytes 123"

    def test_verify_fail_aborts_batch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        rb_db = tmp_path / "master.db"
        rb_db.write_bytes(b"rb")
        djay_db = tmp_path / "MediaLibrary.db"
        djay_db.write_bytes(b"djay")

        monkeypatch.setattr(
            "apps.sync.safety._is_running", lambda _name: False,
        )
        # Fake DB whose Rating field does NOT update despite the write.
        row = _FakeContent("10", rating=0)

        class _FrozenDB(_FakeRBDB):
            def get_content(self, *, ID: str) -> _OneShotQuery:
                return _OneShotQuery(row)

            def commit(self) -> None:  # pragma: no cover - trivial
                self.commits += 1

        frozen = _FrozenDB({"10": row})

        # Monkey-patch _write_rb_rating so the write "succeeds" but the
        # backing row never actually changes -> verifier returns False.
        def _fake_write_rb(
            db_: Any, content_id: str, rating: int,
        ) -> bool:
            return True  # pretend write ok, but row.Rating stays 0

        monkeypatch.setattr(
            "apps.sync.apply_ratings._write_rb_rating", _fake_write_rb,
        )
        monkeypatch.setattr(
            "apps.shared.rekordbox_db.open_db", lambda _p: frozen,
        )

        with pytest.raises(SafetyAbort, match="verify_readback failed"):
            live_run(
                self._rows_accept_djay(),
                flag_ok=True,
                rb_db_path=rb_db,
                djay_db_path=djay_db,
            )


class TestMainLiveFlagGating:
    """Typed-confirm rail via the ``--i-understand-the-risks`` CLI flag."""

    def test_main_live_without_risks_flag_refused_in_safety_layer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        path = _make_diff(
            tmp_path,
            [
                {
                    "rb_content_id": "10",
                    "djay_uuid": "u-10",
                    "field": "rating",
                    "rb_value": "0",
                    "djay_value": "5",
                    "resolution": "accept_djay",
                    "action_hint": "",
                }
            ],
        )
        monkeypatch.setattr(
            "apps.sync.safety._is_running", lambda _name: False,
        )
        monkeypatch.setattr(
            "apps.shared.rekordbox_db.open_db",
            lambda _p: _FakeRBDB({"10": _FakeContent("10")}),
        )
        # Missing --i-understand-the-risks -> flag_ok=False -> SafetyAbort
        # from require_typed_confirm; our main() catches it and returns 3.
        rc = main(["--diff-csv", str(path), "--live"])
        assert rc == 3


# ---------------------------------------------------- live-DB path routing (#1)


class TestLiveDbPathHelpers:
    """``_live_rb_db_path`` / ``_live_djay_db_path`` mirror the pattern in
    ``playlist_apply._live_db_path``: ``--live`` routes to LIVE_DB constants,
    otherwise to WORKING_DB copies under ``data/``.
    """

    def test_rb_live_flag_returns_live_path(self) -> None:
        assert _live_rb_db_path(True) == paths.REKORDBOX_LIVE_DB

    def test_rb_default_returns_working_copy(self) -> None:
        assert _live_rb_db_path(False) == paths.REKORDBOX_WORKING_DB

    def test_djay_live_flag_returns_live_path(self) -> None:
        assert _live_djay_db_path(True) == paths.DJAY_LIVE_DB

    def test_djay_default_returns_working_copy(self) -> None:
        assert _live_djay_db_path(False) == paths.DJAY_WORKING_DB


class TestMainLiveRoutesToLiveDbPaths:
    """P1 regression (#1): ``main(['--live', ...])`` MUST pass the LIVE DB
    paths to ``live_run``, not the WORKING_DB defaults.
    """

    def test_main_live_forwards_live_db_paths_to_live_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Temp paths that stand in for the user's real LIVE DBs; we patch
        # the module-level constants so _live_*_db_path resolves to these.
        fake_rb_live = tmp_path / "fake_rb_live.db"
        fake_rb_live.write_bytes(b"rb-live")
        fake_djay_live = tmp_path / "fake_djay_live.db"
        fake_djay_live.write_bytes(b"djay-live")

        monkeypatch.setattr(paths, "REKORDBOX_LIVE_DB", fake_rb_live)
        monkeypatch.setattr(paths, "DJAY_LIVE_DB", fake_djay_live)

        # Rail 1: pgrep -> nothing is running.
        monkeypatch.setattr(
            "apps.sync.safety._is_running", lambda _name: False,
        )
        # Typed-confirm rail: --i-understand-the-risks sets flag_ok=True,
        # so require_typed_confirm is bypassed in LiveWriteSession. Still,
        # we stub live_run so no real DB work is attempted.
        captured: dict[str, Any] = {}

        def _fake_live_run(
            rows: list[dict], **kwargs: Any,
        ) -> int:
            captured["rows"] = rows
            captured["kwargs"] = kwargs
            return 0

        monkeypatch.setattr(
            "apps.sync.apply_ratings.live_run", _fake_live_run,
        )

        path = _make_diff(
            tmp_path,
            [
                {
                    "rb_content_id": "10",
                    "djay_uuid": "u-10",
                    "field": "rating",
                    "rb_value": "0",
                    "djay_value": "5",
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

    def test_main_non_live_dry_run_does_not_reach_live_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Complementary check: without --live, live_run must not be called
        # (dry-run only); this guards against regressions in the branch.
        called = {"hit": False}

        def _boom(*_a: Any, **_kw: Any) -> int:
            called["hit"] = True
            return 0

        monkeypatch.setattr(
            "apps.sync.apply_ratings.live_run", _boom,
        )
        path = _make_diff(tmp_path, [])
        assert main(["--diff-csv", str(path)]) == 0
        assert called["hit"] is False
