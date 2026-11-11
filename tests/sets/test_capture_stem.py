"""SETREC-12009: stem control capture as v2 events."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.sets.control_events import parse_control_event_v2
from apps.sets.control_ids import (
    SCRIPTED_STEM_CONTROL_IDS,
    stem_gain_id,
    stem_mute_id,
)
from apps.sets.recorder_service import RecorderService
from apps.sets.state import SetsState


@pytest.fixture()
def recorder_client(tmp_path: Path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from apps.sets import api as sets_api
    from tests.sets.conftest import mount_sets_router

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


@pytest.mark.requirement("SETREC-12009")
def test_stem_mute_and_gain_rows_land_in_controls_jsonl(recorder_client) -> None:
    """[if] stem mute and gain post [then] controls.jsonl has both ids, [else stop]."""
    client, service, root = recorder_client
    session_id = _start_none(client, service)
    mute = stem_mute_id(1, "vocal")
    gain = stem_gain_id(1, "vocal")
    events = [
        _event(session_id, 0, mute, True, deck=1),
        _event(session_id, 1, gain, 0.42, deck=1),
    ]
    resp = client.post("/api/sets/control-events", json={"session_id": session_id, "events": events})
    assert resp.status_code == 202, resp.text
    stored = SetsState(db_path=root / "sets.db").list_control_events(session_id)
    logged = {e.control for e in stored}
    assert mute in logged
    assert gain in logged
    lines = (root / session_id / "controls.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2


@pytest.mark.requirement("SETREC-12009")
def test_stem_events_without_sample_field_are_refused(recorder_client) -> None:
    """[if] a stem row lacks sample [then] ingest is 422, [else stop]."""
    client, service, _root = recorder_client
    session_id = _start_none(client, service)
    bad = _event(session_id, 0, stem_mute_id(1, "drums"), True, deck=1)
    del bad["sample"]
    resp = client.post("/api/sets/control-events", json={"session_id": session_id, "events": [bad]})
    assert resp.status_code == 422


@pytest.mark.requirement("SETREC-12009")
def test_stem_rows_carry_sample_clock_not_wall_clock_only(recorder_client) -> None:
    """[if] stem mute lands [then] stored row has sample and rate, [else stop]."""
    client, service, root = recorder_client
    session_id = _start_none(client, service)
    events = [_event(session_id, 0, stem_gain_id(2, "instrumental"), 0.5, sample=96_000, deck=2)]
    resp = client.post("/api/sets/control-events", json={"session_id": session_id, "events": events})
    assert resp.status_code == 202
    stored = SetsState(db_path=root / "sets.db").list_control_events(session_id)[0]
    assert stored.sample == 96_000
    assert stored.sample_rate == 48_000
    parse_control_event_v2(stored.to_json())


@pytest.mark.requirement("SETREC-12009")
def test_scripted_stem_session_meets_coverage_threshold(recorder_client) -> None:
    """[if] scripted stem ids fire [then] every SCRIPTED id is logged, [else stop]."""
    client, service, root = recorder_client
    session_id = _start_none(client, service)
    values: dict[str, object] = {}
    for cid in SCRIPTED_STEM_CONTROL_IDS:
        if cid.endswith(".gain"):
            values[cid] = 0.55
        elif cid.endswith(".mute"):
            values[cid] = True
        else:
            values[cid] = True
    events = [
        _event(
            session_id,
            seq,
            control,
            values[control],
            deck=int(control.split(".")[1]) if control.startswith("deck.") else None,
        )
        for seq, control in enumerate(SCRIPTED_STEM_CONTROL_IDS)
    ]
    resp = client.post("/api/sets/control-events", json={"session_id": session_id, "events": events})
    assert resp.status_code == 202, resp.text
    logged = {e.control for e in SetsState(db_path=root / "sets.db").list_control_events(session_id)}
    assert set(SCRIPTED_STEM_CONTROL_IDS) <= logged
    triggers = {c for c in SCRIPTED_STEM_CONTROL_IDS if not c.endswith(".gain")}
    continuous = {c for c in SCRIPTED_STEM_CONTROL_IDS if c.endswith(".gain")}
    assert sum(1 for c in triggers if c in logged) == len(triggers)
    assert sum(1 for c in continuous if c in logged) / len(continuous) >= 0.99
    jsonl = (root / session_id / "controls.jsonl").read_text(encoding="utf-8").strip().splitlines()
    seqs = [json.loads(line)["seq"] for line in jsonl]
    assert seqs == list(range(len(SCRIPTED_STEM_CONTROL_IDS)))
