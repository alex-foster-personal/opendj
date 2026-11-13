"""Live DB path routing: ``--live`` selects LIVE_DB constants.

[if] main passes --live [then] live_run receives LIVE DB paths not working copies, [else stop].
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from apps.shared import paths
from apps.sync.apply_analysis import _live_djay_db_path, _live_rb_db_path, main
from tests.apply_analysis_support import (
    _make_diff,
    _owned_rollout_directory,  # noqa: F401 (autouse fixture, scoped to this module by the import)
)


pytestmark = [pytest.mark.requirement("SYNC-05"), pytest.mark.rekordbox_writeback]

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
                "REKORDBOX_LIVE_DB",
                "REKORDBOX_WORKING_DB",
                "DJAY_LIVE_DB",
                "DJAY_WORKING_DB",
            )
        }
        for name, target in fixtures.items():
            monkeypatch.setattr(paths, name, target)
        return fixtures

    def test_rb_live_flag_returns_live_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        fixtures = self._redirect(monkeypatch, tmp_path)
        assert _live_rb_db_path(True) == fixtures["REKORDBOX_LIVE_DB"]

    def test_rb_default_returns_working_copy(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        fixtures = self._redirect(monkeypatch, tmp_path)
        assert _live_rb_db_path(False) == fixtures["REKORDBOX_WORKING_DB"]

    def test_djay_live_flag_returns_live_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        fixtures = self._redirect(monkeypatch, tmp_path)
        assert _live_djay_db_path(True) == fixtures["DJAY_LIVE_DB"]

    def test_djay_default_returns_working_copy(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        fixtures = self._redirect(monkeypatch, tmp_path)
        assert _live_djay_db_path(False) == fixtures["DJAY_WORKING_DB"]


class TestMainLiveRoutesToLiveDbPaths:
    """P0 regression (adversarial #1, CRITICAL): ``main(['--live', ...])``
    MUST pass LIVE DB paths into ``live_run``. Pre-fix the kwargs were
    omitted and every live write silently wrote to the WORKING DB copy.
    """

    def test_main_live_routes_to_live_db_paths(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
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
            "apps.sync.apply_analysis.live_run",
            _fake_live_run,
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
                "--diff-csv",
                str(path),
                "--live",
                "--i-understand-the-risks",
            ]
        )
        assert rc == 0
        assert captured["kwargs"]["rb_db_path"] == fake_rb_live
        assert captured["kwargs"]["djay_db_path"] == fake_djay_live
