"""SETREC-07: loop and hot-cue control capture as v2 events."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from apps.sets.control_events import parse_control_event_v2
from apps.sets.control_ids import (
    ENGINE_LOOP_CUE_CONTROL_IDS,
    HOT_CUE_SLOTS,
    LOOP_CUE_TRIGGER_IDS,
    SCRIPTED_LOOP_CUE_CONTROL_IDS,
    assert_engine_page_loop_cue_disjoint,
    hot_cue_id,
    loop_id,
)
from apps.sets.recorder_service import RecorderService
from apps.sets.state import SetsState
from tests.rust_build_env import run_cargo_lib_tests
from tests.sets.conftest import mount_sets_router

CRATE = Path(__file__).resolve().parents[2] / "apps" / "audio-engine"
RUST_LOOP_CUE_TESTS = (
    "loop_cue_capture_loop_is_one_compound_event_with_both_points",
    "loop_cue_capture_keeps_every_trigger_in_one_quantum",
    "loop_cue_capture_from_controller_is_tagged_controller",
    "loop_cue_capture_rejected_command_logs_nothing",
    "loop_cue_capture_scripted_session_stamps_every_trigger",
)


def _cargo_loop_cue_tests() -> subprocess.CompletedProcess[str]:
    return run_cargo_lib_tests(CRATE, RUST_LOOP_CUE_TESTS)


@pytest.fixture()
def recorder_client(tmp_path: Path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from apps.sets import api as sets_api

    app = FastAPI()
    mount_sets_router(app)
    app.state.sets_root = tmp_path
    service = RecorderService(sets_root=tmp_path, db_path=tmp_path / "sets.db", capture_enabled=False)
    app.state.sets_recorder_service = service
    with TestClient(app) as client:
        sets_api._CONTROL_EVENT_SOURCES.clear()
        yield client, service, tmp_path
        sets_api._CONTROL_EVENT_SOURCES.clear()


def _start_none(client, service) -> str:
    resp = client.post("/api/sets/recorder/start", json={"source": "none", "sources": ["opendj_decks"]})
    assert resp.status_code == 201, resp.text
    session_id = resp.json()["session_id"]
    assert service._recorder is not None
    return session_id


def _event(
    session_id: str,
    seq: int,
    control: str,
    value: object,
    *,
    sample: int = 48_000,
    sample_rate: int = 48_000,
    kind: str = "set",
    source: str = "user",
    deck: int | None = None,
) -> dict:
    row = {
        "v": 2,
        "session_id": session_id,
        "seq": seq,
        "sample": sample,
        "sample_rate": sample_rate,
        "wall_clock": "2026-11-10T12:00:00Z",
        "control": control,
        "value": value,
        "kind": kind,
        "source": source,
    }
    if deck is not None:
        row["deck"] = deck
    return row


@pytest.fixture(scope="module", name="rust_loop_cue_capture")
def _rust_loop_cue_capture() -> subprocess.CompletedProcess[str]:
    done = _cargo_loop_cue_tests()
    if done.returncode != 0 and "fatal error" in done.stderr and "complex" in done.stderr:
        pytest.skip("UNAVAILABLE: odj-audio build needs a C++ stdlib with <complex>")
    return done


@pytest.mark.requirement("SETREC-07")
def test_engine_loop_cue_registry_covers_scripted_session() -> None:
    """[if] SCRIPTED ids cover engine table [then] denominator is complete, [else stop]."""
    assert set(ENGINE_LOOP_CUE_CONTROL_IDS) <= set(SCRIPTED_LOOP_CUE_CONTROL_IDS)
    for slot in HOT_CUE_SLOTS:
        assert hot_cue_id(1, slot) in SCRIPTED_LOOP_CUE_CONTROL_IDS


@pytest.mark.requirement("SETREC-07")
def test_no_loop_cue_control_is_logged_by_both_engine_and_page() -> None:
    """[if] engine and page registries overlap [then] fail, [else stop]."""
    assert_engine_page_loop_cue_disjoint()


@pytest.mark.requirement("SETREC-07")
def test_loop_cue_id_helpers_reject_invalid_deck_or_slot() -> None:
    """[if] deck 5 or slot I [then] ValueError, [else stop]."""
    with pytest.raises(ValueError):
        loop_id(5)
    with pytest.raises(ValueError):
        hot_cue_id(1, "I")


@pytest.mark.requirement("SETREC-07")
def test_loop_compound_event_rust(rust_loop_cue_capture: subprocess.CompletedProcess[str]) -> None:
    """[if] rust loop engage runs [then] one compound row with both points, [else stop]."""
    assert rust_loop_cue_capture.returncode == 0, rust_loop_cue_capture.stderr[-4000:]
    assert "loop_cue_capture_loop_is_one_compound_event_with_both_points ... ok" in rust_loop_cue_capture.stdout


@pytest.mark.requirement("SETREC-07")
def test_triggers_never_coalesced_in_one_quantum_rust(
    rust_loop_cue_capture: subprocess.CompletedProcess[str],
) -> None:
    """[if] 14 triggers in one quantum [then] all 14 drain, [else stop]."""
    assert rust_loop_cue_capture.returncode == 0, rust_loop_cue_capture.stderr[-4000:]
    assert "loop_cue_capture_keeps_every_trigger_in_one_quantum ... ok" in rust_loop_cue_capture.stdout


@pytest.mark.requirement("SETREC-07")
def test_loop_from_controller_is_source_controller_rust(
    rust_loop_cue_capture: subprocess.CompletedProcess[str],
) -> None:
    """[if] loop from controller [then] source is controller, [else stop]."""
    assert rust_loop_cue_capture.returncode == 0, rust_loop_cue_capture.stderr[-4000:]
    assert "loop_cue_capture_from_controller_is_tagged_controller ... ok" in rust_loop_cue_capture.stdout


@pytest.mark.requirement("SETREC-07")
def test_rejected_beat_loop_logs_nothing_rust(rust_loop_cue_capture: subprocess.CompletedProcess[str]) -> None:
    """[if] beat loop on gridless track [then] drain is empty, [else stop]."""
    assert rust_loop_cue_capture.returncode == 0, rust_loop_cue_capture.stderr[-4000:]
    assert "loop_cue_capture_rejected_command_logs_nothing ... ok" in rust_loop_cue_capture.stdout


@pytest.mark.requirement("SETREC-07")
def test_scripted_engine_session_stamps_every_trigger_rust(
    rust_loop_cue_capture: subprocess.CompletedProcess[str],
) -> None:
    """[if] scripted engine session runs [then] every ENGINE_LOOP id is captured, [else stop]."""
    assert rust_loop_cue_capture.returncode == 0, rust_loop_cue_capture.stderr[-4000:]
    assert "loop_cue_capture_scripted_session_stamps_every_trigger ... ok" in rust_loop_cue_capture.stdout


@pytest.mark.requirement("SETREC-07")
def test_loop_engage_is_one_compound_row_with_both_points(recorder_client) -> None:
    """[if] deck.1.loop posts compound value [then] jsonl has both in_ms and out_ms, [else stop]."""
    client, _service, root = recorder_client
    session_id = _start_none(client, _service)
    cid = loop_id(1)
    events = [_event(session_id, 0, cid, {"in_ms": 1000.0, "out_ms": 3000.0}, deck=1)]
    resp = client.post("/api/sets/control-events", json={"session_id": session_id, "events": events})
    assert resp.status_code == 202, resp.text
    lines = (root / session_id / "controls.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert set(row["value"].keys()) == {"in_ms", "out_ms"}
    stored = SetsState(db_path=root / "sets.db").list_control_events(session_id)[0]
    parse_control_event_v2(stored.to_json())


@pytest.mark.requirement("SETREC-07")
def test_every_hot_cue_press_is_logged_even_on_one_sample(recorder_client) -> None:
    """[if] 8 hot-cue triggers share one sample [then] all 8 land with seq 0..7, [else stop]."""
    client, _service, root = recorder_client
    session_id = _start_none(client, _service)
    hid = hot_cue_id(1, "A")
    events = [_event(session_id, seq, hid, 1500.0 + seq, sample=48_000, kind="trigger", deck=1) for seq in range(8)]
    resp = client.post("/api/sets/control-events", json={"session_id": session_id, "events": events})
    assert resp.status_code == 202, resp.text
    stored = SetsState(db_path=root / "sets.db").list_control_events(session_id)
    assert len(stored) == 8
    assert [e.seq for e in sorted(stored, key=lambda r: r.seq)] == list(range(8))


@pytest.mark.requirement("SETREC-07")
def test_scripted_loop_cue_session_logs_every_trigger_with_its_sample(recorder_client) -> None:
    """[if] scripted loop/cue ids fire [then] each lands at its scripted sample, [else stop]."""
    client, _service, root = recorder_client
    session_id = _start_none(client, _service)
    events = []
    for i, control in enumerate(SCRIPTED_LOOP_CUE_CONTROL_IDS):
        sample = 48_000 + 480 * i
        kind = "trigger" if control in LOOP_CUE_TRIGGER_IDS else "set"
        if control.endswith(".loop"):
            value: object = {"in_ms": 1000.0, "out_ms": 3000.0}
        elif control.endswith(".beat_loop"):
            value = {"beats": 4.0, "in_ms": 1000.0, "out_ms": 3000.0}
        elif control.endswith(".beat_jump"):
            value = 1.0
        elif control.endswith(".quantize"):
            value = True
        elif ".hot_cue." in control:
            value = 1200.0
        else:
            value = True
        deck = int(control.split(".")[1]) if control.startswith("deck.") else None
        events.append(_event(session_id, i, control, value, sample=sample, kind=kind, deck=deck))
    resp = client.post("/api/sets/control-events", json={"session_id": session_id, "events": events})
    assert resp.status_code == 202, resp.text
    logged = {e.control: e.sample for e in SetsState(db_path=root / "sets.db").list_control_events(session_id)}
    assert set(SCRIPTED_LOOP_CUE_CONTROL_IDS) <= set(logged)
    triggers = [c for c in SCRIPTED_LOOP_CUE_CONTROL_IDS if c in LOOP_CUE_TRIGGER_IDS]
    assert all(c in logged for c in triggers)
    for i, control in enumerate(SCRIPTED_LOOP_CUE_CONTROL_IDS):
        assert logged[control] == 48_000 + 480 * i
    jsonl = (root / session_id / "controls.jsonl").read_text(encoding="utf-8").strip().splitlines()
    seqs = [json.loads(line)["seq"] for line in jsonl]
    assert seqs == list(range(len(SCRIPTED_LOOP_CUE_CONTROL_IDS)))
