"""AUTOPLAY decision record and verdict: the decide route stores why a track won, and nothing reads it back into a plan."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.autodj.pair_memory import record_played, save_pair_transition
from apps.autodj.propose import propose_transition
from apps.opendj_cli.dj_why_cli import explain
from apps.shared.state import db as state_db
from apps.webui.server.routes import dj_transitions_record
from tests.autodj.fixtures import profile
from tests.webui.test_dj_transition_decide import DECIDE, OUT, _body, app  # noqa: F401  # pytest fixture

LAST, VERDICT = "/dj/transitions/last", "/dj/transitions/verdict"


def _client(served: FastAPI) -> TestClient:
    served.include_router(dj_transitions_record.router)
    return TestClient(served)


def _ran(tmp_path: Path, out_id: str, in_id: str) -> None:
    plan = propose_transition(profile(out_id), profile(in_id, key="9A"), out_deck=1, in_deck=2)["proposals"][0]["plan"]
    conn = state_db.open_rw(tmp_path / "state.db")
    try:
        with state_db.write_unit(conn, "test"):
            record_played(conn, out_id, in_id, plan=plan, source="planner", style=str(plan["style"]))
    finally:
        conn.close()


@pytest.mark.requirement("AUTOPLAY-8881")
def test_decide_stores_the_shortlist_candidates_and_chosen_warnings(app: FastAPI, tmp_path: Path) -> None:  # noqa: F811
    """[if] AutoPlay decides [then] the shortlist, rejections and every candidate are stored and served, [else stop]."""
    client = _client(app)
    answer = client.post(
        DECIDE, json=_body(shortlist=["trk-missing", "trk-in"], intent="showy", recent_styles=["long_blend"])
    ).json()
    record = client.get(LAST).json()
    decision = record["decision"]
    assert record["out_track_id"] == OUT.stable_id and record["in_track_id"] == answer["track"] == "trk-in"
    assert decision["shortlist"] == ["trk-missing", "trk-in"], "if the asked shortlist is not stored then broken"
    assert {"track": "trk-missing", "reason": "trk-missing has no analysis"} in decision["rejected"], (
        "if a track dropped before planning is missing from the stored rejections then broken"
    )
    assert decision["candidates"], "if no candidate's style and score is stored then broken"
    assert decision["chosen"] in {c["cid"] for c in decision["candidates"]}
    assert decision["intent"] == "showy" and decision["recent_styles"] == ["long_blend"], (
        "if the intent and variety memory the chooser weighed are not stored then broken"
    )
    assert all(isinstance(c["score"], float) and isinstance(c["warnings"], list) for c in decision["candidates"])
    assert client.get(LAST, params={"pair": f"{OUT.stable_id}:trk-in"}).json() == record
    assert client.get(LAST, params={"pair": "x:y"}).status_code == 404, "control: an unknown pair is not found"
    text = explain(record)
    assert "Intent: showy; recent styles: long_blend" in text
    assert "CHOSEN trk-in" in text and "Verdict: none given" in text and "Played: not yet" in text


@pytest.mark.requirement("AUTOPLAY-8881b")
def test_a_verdict_lands_on_the_pair_and_never_changes_the_plan(app: FastAPI, tmp_path: Path) -> None:  # noqa: F811
    """[if] a verdict is given [then] it is stored on the pair and the next decision is unchanged, [else stop]."""
    client = _client(app)
    refused = client.post(VERDICT, json={"out": OUT.stable_id, "in": "trk-in", "verdict": "bad"})
    assert refused.status_code == 404, "if a pair never heard takes a verdict then broken"
    before = client.post(DECIDE, json=_body()).json()
    _ran(tmp_path, OUT.stable_id, "trk-in")
    assert client.post(VERDICT, json={"out": OUT.stable_id, "in": "trk-in", "verdict": "bad"}).status_code == 200
    record: dict[str, Any] = client.get(LAST).json()
    assert record["verdict"] == "bad" and record["verdict_at"], "if the verdict is not on the record then broken"
    assert "Verdict: bad" in explain(record)
    after = client.post(DECIDE, json=_body()).json()
    assert after == before, "if a verdict changes the planner's decision then broken"
    assert client.post(VERDICT, json={"out": OUT.stable_id, "in": "trk-in", "verdict": "great"}).status_code == 422


def _plan(out_id: str, in_id: str) -> dict[str, Any]:
    return propose_transition(profile(out_id), profile(in_id, key="9A"), out_deck=1, in_deck=2)["proposals"][1]["plan"]


@pytest.mark.requirement("AUTOPLAY-8881b")
def test_a_saved_but_unplayed_pair_takes_no_verdict_and_a_new_plan_clears_it(app: FastAPI, tmp_path: Path) -> None:  # noqa: F811
    """[if] a pair was saved but never played, or its plan is replaced [then] no verdict stands for it, [else stop]."""
    client = _client(app)
    client.post(DECIDE, json=_body())
    conn = state_db.open_rw(tmp_path / "state.db")
    try:
        with state_db.write_unit(conn, "test"):
            plan = _plan(OUT.stable_id, "trk-in")
            save_pair_transition(conn, plan, source="edited", style=str(plan["style"]))
    finally:
        conn.close()
    assert client.get(LAST).json()["played"] is None, "if an unplayed saved plan reads as played then broken"
    refused = client.post(VERDICT, json={"out": OUT.stable_id, "in": "trk-in", "verdict": "good"})
    assert refused.status_code == 404, "if a never-played pair takes a verdict then broken"
    _ran(tmp_path, OUT.stable_id, "trk-in")
    assert client.post(VERDICT, json={"out": OUT.stable_id, "in": "trk-in", "verdict": "good"}).status_code == 200
    assert client.get(LAST).json()["verdict"] == "good", "control: a played pair keeps its verdict"
    conn = state_db.open_rw(tmp_path / "state.db")
    try:
        with state_db.write_unit(conn, "test"):
            replacement = _plan(OUT.stable_id, "trk-in")
            record_played(
                conn, OUT.stable_id, "trk-in", plan=replacement, source="planner", style=str(replacement["style"])
            )
    finally:
        conn.close()
    assert client.get(LAST).json()["verdict"] is None, "if the old plan's verdict sticks to the replacement then broken"
