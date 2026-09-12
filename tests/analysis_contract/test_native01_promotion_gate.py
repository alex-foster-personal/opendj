"""NATIVE-01 beatgrid promotion gate: re-verify landed guarantees, block premature promotion.

[if] beatgrid is promoted to own before BEATMAP-01's threshold is agreed [then] fail, [else stop].

Requirement: NATIVE-01 (issue #2314). BEATMAP-01's agreed threshold is still not set,
so D1 does not flip the persisted beatgrid default to own. This file re-verifies the
four landed consumer/producer/contract guarantees and asserts the launch state stays rbx.

Acceptance lines exercised here:
- [if] BEATMAP-01's agreed threshold is reached [then] the own static grid and downbeat,
  served in the `/anlz` shape, are promoted per D1 (blocked here; threshold not met).
- [if] promotion has not yet happened [then] the four already-landed guarantees continue
  to hold and are re-verified, not assumed from the PARTIAL note.
- [if] a `minimal` post-processor threshold change moves fixed-tempo F by more than 0.01
  during the work on this issue [then] it is still recorded as a regression via the
  existing guard, and closing this issue must not weaken that guard.

-Cursor
"""
from __future__ import annotations

import ast
import dataclasses
import inspect
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from apps.analysis import selection
from apps.analysis import store as store_mod
from apps.analysis.lanes import LaneContractError, LaneResult, validate_lane_result
from apps.analysis_bench import rounds
from apps.analysis_bench.scorers.beatgrid import (
    FIXED_TEMPO_F_REGRESSION_TOL,
    evaluate_fixed_tempo_f_shift,
)
from apps.webui.server.rb_vendor_pkg import own_beatgrid_overlay as overlay_mod
from tests.analysis_contract.conftest import beatgrid_payload, own_record

pytestmark = pytest.mark.requirement("NATIVE-01")

STABLE_ID = "sid-native01-gate"
REPO_ROOT = Path(__file__).resolve().parents[2]
ANALYSIS_SOURCE_UI = (
    REPO_ROOT / "apps/webui/frontend/src/lib/rb/analysis-source.svelte.ts"
)


def _state_db_path(db: sqlite3.Connection, tmp_path: Path) -> Path:
    return tmp_path / "state.db"


def _rekordbox_payload(stable_id: str = STABLE_ID) -> dict[str, Any]:
    """A 4-beat rekordbox grid at 100 BPM from t=1.0 (distinct from the 128 BPM fixture)."""
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


#-----------------------------------------------------------------------------
# A. Promotion has not happened
#-----------------------------------------------------------------------------


def test_beatgrid_persisted_default_is_still_rbx_on_a_fresh_store(
    db: sqlite3.Connection,
) -> None:
    """[if] promotion has not happened [then] launch default stays rbx, [else stop]."""
    assert selection.get_default(db, "beatgrid") == "rbx"
    assert selection.lane_is_promoted(db, "beatgrid") is False
    assert selection.get_toggle("beatgrid") == "unset"
    assert selection.effective_source(db, "beatgrid") == "rbx"
    assert selection.DEFAULT_SOURCE == "rbx"


def test_an_own_record_is_not_served_while_the_default_stays_rbx(
    db: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    """[if] promotion has not happened [then] rbx launch grid is unchanged, [else stop]."""
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


#-----------------------------------------------------------------------------
# B. Re-verify the four landed guarantees
#-----------------------------------------------------------------------------


def test_own_toggle_serves_own_beats_not_the_rekordbox_grid(
    db: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    """[if] own is selected and the track has an own grid [then] own beats are served, [else stop]."""
    own_payload = _write_own_ok_record(db)
    own_times = [b["t"] for b in own_payload["beats"]]
    payload = _rekordbox_payload()
    rekordbox_times = [b["t"] for b in payload["beatgrid"]["beats"]]
    selection.set_toggle("beatgrid", "own")

    served = overlay_mod.apply_own_beatgrid(
        payload,
        STABLE_ID,
        state_db_path=_state_db_path(db, tmp_path),
    )["beatgrid"]

    assert served["source"] == "own"
    assert served["status"] == "ok"
    assert [b["t"] for b in served["beats"]] == own_times
    assert [b["t"] for b in served["beats"]] != rekordbox_times


def test_own_failed_lane_serves_empty_beats_and_the_named_reason(
    db: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    """[if] own beatgrid status is failed [then] no rekordbox beats appear, [else stop]."""
    record = own_record(
        stable_id=STABLE_ID,
        producer="backfill",
        result=LaneResult(status="failed", reason="no_trackable_pulse", payload={}),
    )
    store_mod.upsert_record(record, conn=db)
    db.commit()
    payload = _rekordbox_payload()
    rekordbox_times = [b["t"] for b in payload["beatgrid"]["beats"]]
    selection.set_toggle("beatgrid", "own")

    served = overlay_mod.apply_own_beatgrid(
        payload,
        STABLE_ID,
        state_db_path=_state_db_path(db, tmp_path),
    )["beatgrid"]

    assert served["source"] == "own"
    assert served["status"] == "failed"
    assert served["reason"] == "no_trackable_pulse"
    assert served["beats"] == []
    assert rekordbox_times != []


def test_own_missing_record_is_named_missing_not_rekordbox(
    db: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    """[if] own is selected [then] missing is named, never silent rekordbox, [else stop]."""
    payload = _rekordbox_payload()
    rekordbox_times = [b["t"] for b in payload["beatgrid"]["beats"]]
    selection.set_toggle("beatgrid", "own")

    served = overlay_mod.apply_own_beatgrid(
        payload,
        STABLE_ID,
        state_db_path=_state_db_path(db, tmp_path),
    )["beatgrid"]

    assert served["status"] == "missing"
    assert served["source"] == "own"
    assert served["beats"] == []
    assert served["reason"] == overlay_mod.OWN_BEATGRID_MISSING_REASON
    assert rekordbox_times != []


def test_ok_with_empty_beats_is_a_contract_violation() -> None:
    """[if] beatgrid.status ok carries empty beats [then] contract refuses, [else stop]."""
    result = LaneResult(
        status="ok",
        payload={
            "beats": [],
            "bpm": 128.0,
            "bpm_confidence": 0.9,
            "octave_reason": "fixture",
            "first_downbeat_s": 0.0,
            "tempo_changes": [],
            "static_grid_untrusted": False,
        },
    )
    with pytest.raises(LaneContractError, match="beats is empty with status ok"):
        validate_lane_result("beatgrid", result)


#-----------------------------------------------------------------------------
# C. F-guard still wired
#-----------------------------------------------------------------------------


def test_fixed_tempo_f_guard_is_still_wired() -> None:
    """[if] minimal threshold moves fixed-tempo F by more than 0.01 [then] regression, [else stop]."""
    assert FIXED_TEMPO_F_REGRESSION_TOL == 0.01
    eval_src = inspect.getsource(evaluate_fixed_tempo_f_shift)
    assert "FIXED_TEMPO_F_REGRESSION_TOL" in eval_src
    assert "is_regression" in eval_src
    append_src = inspect.getsource(rounds.append_round)
    assert "apply_fixed_tempo_f_guard" in append_src
    shift = evaluate_fixed_tempo_f_shift(1.0, 0.989)
    assert shift.is_regression is True


#-----------------------------------------------------------------------------
# D. No production path seeds beatgrid=own
#-----------------------------------------------------------------------------


def test_no_apps_call_set_default_beatgrid_own() -> None:
    """[if] production seeds beatgrid=own before threshold [then] fail, [else stop]."""
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


def test_analysis_source_ui_put_writes_toggle_not_default() -> None:
    """[if] the UI writes analysis_source_default [then] fail, [else stop]."""
    text = ANALYSIS_SOURCE_UI.read_text(encoding="utf-8")
    assert "toggle: attemptedToggle" in text
    put_bodies = re.findall(
        r"api\.PUT\('/api/v1/analysis/source',\s*\{\s*body:\s*\{([^}]+)\}",
        text,
    )
    assert put_bodies, "expected at least one PUT /api/v1/analysis/source body"
    for body in put_bodies:
        assert re.search(r"\btoggle\s*:", body)
        assert not re.search(r"\bdefault\s*:", body)
