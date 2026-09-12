"""NATIVE-14 pending guard: D1 persisted-default promotion stays open until gates pass.

[if] production seeds beatgrid own before NATIVE-14 gates [then] fail, [else stop].

-Cursor
"""
from __future__ import annotations

import ast
import dataclasses
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from apps.analysis import selection
from apps.analysis import store as store_mod
from apps.analysis.lanes import LaneResult
from apps.webui.server.rb_vendor_pkg import own_beatgrid_overlay as overlay_mod
from tests.analysis_contract.conftest import beatgrid_payload, own_record

pytestmark = pytest.mark.requirement("NATIVE-14")

STABLE_ID = "sid-native14-guard"
REPO_ROOT = Path(__file__).resolve().parents[2]


def _state_db_path(db: sqlite3.Connection, tmp_path: Path) -> Path:
    return tmp_path / "state.db"


def _rekordbox_payload(stable_id: str = STABLE_ID) -> dict[str, Any]:
    bpm = 100.0
    interval = 60.0 / bpm
    beats = [
        {"n": i, "bpm": bpm, "t": round(1.0 + (i - 1) * interval, 3)}
        for i in range(1, 5)
    ]
    return {
        "stable_id": stable_id,
        "beatgrid": {
            "source": "rekordbox",
            "beat_count": len(beats),
            "beats": beats,
        },
    }


def _write_own_ok_record(db: sqlite3.Connection, stable_id: str = STABLE_ID) -> dict[str, Any]:
    payload = beatgrid_payload(bpm=128.0, tempo_changes=0)
    record = dataclasses.replace(
        own_record(
            stable_id=stable_id,
            producer="backfill",
            version="1.0.0",
            result=LaneResult(status="ok", payload=payload),
        ),
        bpm=128.0,
    )
    store_mod.upsert_record(record, conn=db)
    db.commit()
    return payload


def test_beatgrid_persisted_default_stays_rbx_until_native14_ships(
    db: sqlite3.Connection,
) -> None:
    """[if] NATIVE-14 has not shipped [then] launch default stays rbx, [else stop]."""
    assert selection.get_default(db, "beatgrid") == "rbx"
    assert selection.lane_is_promoted(db, "beatgrid") is False
    assert selection.get_toggle("beatgrid") == "unset"
    assert selection.effective_source(db, "beatgrid") == "rbx"
    assert selection.DEFAULT_SOURCE == "rbx"


def test_an_own_record_is_not_the_launch_grid_while_default_is_rbx(
    db: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    """[if] NATIVE-14 has not shipped [then] an own record does not become the launch grid, [else stop]."""
    _write_own_ok_record(db)
    payload = _rekordbox_payload()
    before = json.dumps(payload, sort_keys=True)
    after = overlay_mod.apply_own_beatgrid(
        payload,
        STABLE_ID,
        state_db_path=_state_db_path(db, tmp_path),
    )
    assert json.dumps(after, sort_keys=True) == before
    assert after["beatgrid"]["source"] == "rekordbox"
    rekordbox_times = [b["t"] for b in after["beatgrid"]["beats"]]
    own_times = [b["t"] for b in beatgrid_payload()["beats"]]
    assert rekordbox_times != own_times[: len(rekordbox_times)]


def test_no_apps_call_set_default_beatgrid_own() -> None:
    """[if] production seeds beatgrid own before NATIVE-14 gates [then] fail, [else stop]."""
    apps_root = REPO_ROOT / "apps"
    offenders: list[str] = []
    for path in apps_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name: str | None = None
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name != "set_default":
                continue
            literals: list[str] = []
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    literals.append(arg.value)
            for kw in node.keywords:
                if isinstance(kw.value, ast.Constant) and isinstance(kw.value, str):
                    literals.append(kw.value)
            if "beatgrid" in literals and "own" in literals:
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    assert offenders == []
