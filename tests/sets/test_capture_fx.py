"""SETREC-11827: FX control-event capture ingest and coverage."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.requirement

from apps.sets.control_events import parse_control_event_v2
from apps.sets.control_ids import (
    BEAT_FX_ON,
    BEAT_FX_RELEASE,
    BEAT_FX_SELECT,
    BEAT_FX_WET,
    FX_COLOR_ECHO_BEATS,
    FX_COLOR_SELECT,
    FX_COLOR_WET,
    FX_SCRIPTED_TOUCH_IDS,
)
from apps.sets.recorder_service import RecorderService
from apps.sets.state import SetsState


@pytest.fixture()
def recorder_client(tmp_path: Path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from apps.sets import api as sets_api

    app = FastAPI()
    app.include_router(sets_api.router, prefix=sets_api.SETS_API_PREFIX)
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
    kind: str = "set",
    source: str = "user",
    deck: int | None = None,
) -> dict:
    row = {
        "v": 2,
        "session_id": session_id,
        "seq": seq,
        "sample": 48_000 + seq,
        "sample_rate": 48_000,
        "wall_clock": "2026-11-10T12:00:00Z",
        "control": control,
        "value": value,
        "kind": kind,
        "source": source,
    }
    if deck is not None:
        row["deck"] = deck
    return row


def test_fx_on_off_select_and_wet_produce_v2_rows_with_fx_ids(recorder_client) -> None:
    """[if] FX on/select/wet post [then] rows carry fx.N ids, [else stop]."""
    client, service, root = recorder_client
    session_id = _start_none(client, service)
    events = [
        _event(session_id, 0, FX_COLOR_SELECT[1], "space", deck=1),
        _event(session_id, 1, FX_COLOR_WET[1], 0.42, deck=1),
        _event(session_id, 2, BEAT_FX_ON, True),
        _event(session_id, 3, BEAT_FX_SELECT, "echo"),
        _event(session_id, 4, BEAT_FX_WET, 0.8),
        _event(session_id, 5, BEAT_FX_RELEASE, True, kind="trigger"),
    ]
    resp = client.post("/api/sets/control-events", json={"session_id": session_id, "events": events})
    assert resp.status_code == 202, resp.text
    state = SetsState(db_path=root / "sets.db")
    stored = state.list_control_events(session_id)
    assert [e.control for e in stored] == [e["control"] for e in events]
    for row in stored:
        assert row.control.startswith("fx.")
        parse_control_event_v2(row.to_json())


def test_agent_fx_changes_are_tagged_source_agent(recorder_client) -> None:
    """[if] agent-source FX row [then] source is agent, [else stop]."""
    client, service, root = recorder_client
    session_id = _start_none(client, service)
    events = [
        _event(session_id, 0, FX_COLOR_WET[2], 0.55, source="agent", deck=2),
    ]
    resp = client.post("/api/sets/control-events", json={"session_id": session_id, "events": events})
    assert resp.status_code == 202
    stored = SetsState(db_path=root / "sets.db").list_control_events(session_id)
    assert stored[0].source == "agent"


def test_scripted_fx_session_meets_trigger_and_continuous_coverage(recorder_client) -> None:
    """[if] scripted FX ids fire once each [then] trigger 100% and continuous >=99%, [else stop]."""
    client, service, root = recorder_client
    session_id = _start_none(client, service)
    scripted_values = {
        FX_COLOR_SELECT[1]: "echo",
        FX_COLOR_WET[1]: 0.7,
        FX_COLOR_ECHO_BEATS[1]: 0.75,
        FX_COLOR_SELECT[2]: "space",
        FX_COLOR_WET[2]: 0.35,
        BEAT_FX_ON: True,
        BEAT_FX_SELECT: "echo",
        BEAT_FX_WET: 0.5,
        BEAT_FX_RELEASE: True,
    }
    events = [
        _event(
            session_id,
            seq,
            control,
            scripted_values[control],
            kind="trigger" if control == BEAT_FX_RELEASE else "set",
            deck=1
            if control in (FX_COLOR_SELECT[1], FX_COLOR_WET[1], FX_COLOR_ECHO_BEATS[1])
            else (2 if control in (FX_COLOR_SELECT[2], FX_COLOR_WET[2]) else None),
        )
        for seq, control in enumerate(FX_SCRIPTED_TOUCH_IDS)
    ]
    resp = client.post("/api/sets/control-events", json={"session_id": session_id, "events": events})
    assert resp.status_code == 202, resp.text

    state = SetsState(db_path=root / "sets.db")
    logged = {e.control for e in state.list_control_events(session_id)}
    assert set(FX_SCRIPTED_TOUCH_IDS) <= logged

    triggers = {BEAT_FX_RELEASE}
    continuous = set(FX_SCRIPTED_TOUCH_IDS) - triggers
    trigger_hits = sum(1 for cid in triggers if cid in logged)
    cont_hits = sum(1 for cid in continuous if cid in logged)
    assert trigger_hits == len(triggers)
    assert cont_hits / len(continuous) >= 0.99

    jsonl = (root / session_id / "controls.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(jsonl) == len(FX_SCRIPTED_TOUCH_IDS)
    seqs = [json.loads(line)["seq"] for line in jsonl]
    assert seqs == list(range(len(FX_SCRIPTED_TOUCH_IDS)))
