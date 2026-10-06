"""HTTP acceptance tests for the Open DJ deck-observation ingest.

[if] snapshots are posted with no live recorder [then ⛔️] they are accepted.
[if] a track is audible past the threshold [then ⛔️] the set has no row for it.
[if] a snapshot is malformed [then ⛔️] it is quietly dropped instead of 422.
[if] the recorder never enabled the source [then ⛔️] the post looks like it worked.
[if] a batch names a session that is no longer recording [then ⛔️] its playback
     is credited to whatever set happens to be live now.
[if] a batch carries no session id at all [then ⛔️] it is accepted and filed
     under whichever set happens to be recording.
[if] the body the BROWSER actually posts is replayed here [then ⛔️] the server
     refuses it, or accepts it and records nothing.
"""
from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.sets.api import router
from apps.sets.recorder_service import RecorderService
from apps.sets.sources.opendj_source import ADVISORY_DWELL_S
from apps.sets.state import SetsState

SESSION_ID = "2026-08-31T20-00-00"
T0 = datetime(2026, 8, 31, 20, 0, 0, tzinfo=UTC)
CADENCE_S = 2.0
#: The browser's OWN request body, captured from the production emitter. The
#: node lane owns keeping it current; this lane owns judging it. See
#: `test_the_body_the_browser_actually_posts_is_accepted_and_recorded`.
EMITTER_POST = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "sets"
    / "deck-observations-emitter-post.json"
)


@pytest.fixture
def observer_client(tmp_path: Path):
    service = RecorderService(
        sets_root=tmp_path / "sets",
        db_path=tmp_path / "sets" / "sets.db",
        capture_enabled=False,
    )
    app = FastAPI()
    app.state.sets_recorder_service = service
    app.include_router(router)
    with TestClient(app) as client:
        yield client, service


def _deck(stable_id, *, audible: bool, playing: bool = True) -> dict:
    return {
        "stable_id": stable_id,
        "playing": playing,
        "audible": audible,
        "position_ms": 0.0,
        "duration_ms": 240_000.0,
        "title": "Test Track",
        "artist": "Test Artist",
    }


def _run(span_s: float, decks: dict) -> list[dict]:
    out: list[dict] = []
    offset = 0.0
    while offset <= span_s:
        out.append(
            {
                "observed_at": (T0 + timedelta(seconds=offset)).isoformat(
                    timespec="milliseconds"
                ),
                "decks": decks,
            }
        )
        offset += CADENCE_S
    return out


def _start(client: TestClient, sources: list[str]) -> None:
    resp = client.post(
        "/api/sets/recorder/start",
        json={
            "session_id": SESSION_ID,
            "source": "external",
            "ffmpeg_device_idx": 0,
            "sources": sources,
        },
    )
    assert resp.status_code == 201, resp.text


@pytest.mark.requirement("SET-01")
def test_posting_observations_without_a_live_recorder_is_409(observer_client):
    client, _ = observer_client
    resp = client.post(
        "/api/sets/deck-observations",
        json={
            "session_id": SESSION_ID,
            "snapshots": _run(0, {"1": _deck("trk-a", audible=True)}),
        },
    )
    assert resp.status_code == 409
    assert "no HTTP-owned recorder is active" in resp.json()["detail"]


@pytest.mark.requirement("SET-01")
def test_recorder_without_the_source_rejects_observations(observer_client):
    client, _ = observer_client
    _start(client, [])
    resp = client.post(
        "/api/sets/deck-observations",
        json={
            "session_id": SESSION_ID,
            "snapshots": _run(0, {"1": _deck("trk-a", audible=True)}),
        },
    )
    assert resp.status_code == 409
    assert "without the 'opendj_decks' source" in resp.json()["detail"]
    client.post(f"/api/sets/recorder/{SESSION_ID}/stop")


@pytest.mark.requirement("SET-01")
def test_audible_run_posted_over_http_lands_in_the_set(observer_client):
    """The whole point: a track played on OUR deck ends up in the set."""
    client, service = observer_client
    _start(client, ["opendj_decks"])

    posted = client.post(
        "/api/sets/deck-observations",
        json={
            "session_id": SESSION_ID,
            "snapshots": _run(
                ADVISORY_DWELL_S + 4, {"1": _deck("trk-a", audible=True)}
            ),
        },
    )
    assert posted.status_code == 202, posted.text

    # The recorder's own poll thread drains the queue; force one drain so
    # the test does not race the 500 ms cadence.
    service.active_source("opendj_decks").poll_once()

    status = client.get("/api/sets/deck-observations")
    assert status.status_code == 200
    deck_one = status.json()["decks"]["1"]
    assert deck_one["stable_id"] == "trk-a"
    assert deck_one["recorded"] is True

    client.post(f"/api/sets/recorder/{SESSION_ID}/stop")

    state = SetsState(db_path=service.db_path)
    rows = state.fetch_events(SESSION_ID, action="track_loaded")
    assert [r.track_stable_id for r in rows] == ["trk-a"]
    assert rows[0].deck == "1"
    assert rows[0].source == "opendj_decks"
    stamp = datetime.fromisoformat(rows[0].wall_clock)
    assert stamp.utcoffset() == timedelta(0)


@pytest.mark.requirement("SET-01")
def test_loaded_but_silent_run_posted_over_http_records_nothing(observer_client):
    client, service = observer_client
    _start(client, ["opendj_decks"])

    client.post(
        "/api/sets/deck-observations",
        json={
            "session_id": SESSION_ID,
            "snapshots": _run(
                ADVISORY_DWELL_S * 3, {"1": _deck("trk-a", audible=False)}
            ),
        },
    )
    service.active_source("opendj_decks").poll_once()
    client.post(f"/api/sets/recorder/{SESSION_ID}/stop")

    state = SetsState(db_path=service.db_path)
    assert state.fetch_events(SESSION_ID, action="track_loaded") == []


@pytest.mark.requirement("SET-01")
@pytest.mark.parametrize(
    "bad_deck, expect",
    [
        ({"9": {"stable_id": "a", "playing": True, "audible": True,
                "position_ms": 0}}, "unknown deck"),
        ({"1": {"stable_id": "a", "playing": True, "audible": "yes",
                "position_ms": 0}}, "bool"),
        ({"1": {"stable_id": None, "playing": True, "audible": True,
                "position_ms": 0}}, "audible"),
        ({"1": {"stable_id": "a", "playing": True, "position_ms": 0}},
         "missing audible"),
    ],
)
def test_malformed_snapshots_are_422_not_silently_dropped(
    observer_client, bad_deck, expect
):
    client, _ = observer_client
    _start(client, ["opendj_decks"])
    resp = client.post(
        "/api/sets/deck-observations",
        json={
            "session_id": SESSION_ID,
            "snapshots": [
                {"observed_at": T0.isoformat(timespec="milliseconds"),
                 "decks": bad_deck}
            ],
        },
    )
    assert resp.status_code == 422, resp.text
    assert expect in resp.json()["detail"]
    client.post(f"/api/sets/recorder/{SESSION_ID}/stop")


@pytest.mark.requirement("SET-01")
def test_empty_batch_is_rejected(observer_client):
    client, _ = observer_client
    _start(client, ["opendj_decks"])
    resp = client.post(
        "/api/sets/deck-observations",
        json={"session_id": SESSION_ID, "snapshots": []},
    )
    assert resp.status_code == 422
    client.post(f"/api/sets/recorder/{SESSION_ID}/stop")


@pytest.mark.requirement("SET-01")
def test_batch_named_for_a_finished_session_is_refused(observer_client):
    """A backlog that outlived its recording must not land in the next set.

    The client cannot prevent this on its own. It can re-read the recorder
    before posting and the recording can still stop and be replaced before
    the request arrives, and a `pagehide` flush re-reads nothing at all. So
    the batch carries the session it was sampled under and the check happens
    here, where the answer cannot change underneath the caller.
    """
    client, _ = observer_client
    _start(client, ["opendj_decks"])
    snapshots = _run(0, {"1": _deck("trk-a", audible=True)})

    # Control: the session it really was sampled under is accepted.
    accepted = client.post(
        "/api/sets/deck-observations",
        json={"snapshots": snapshots, "session_id": SESSION_ID},
    )
    assert accepted.status_code == 202, accepted.text

    stale = client.post(
        "/api/sets/deck-observations",
        json={"snapshots": snapshots, "session_id": "2020-01-01T00-00-00"},
    )
    assert stale.status_code == 409
    detail = stale.json()["detail"]
    assert "2020-01-01T00-00-00" in detail
    assert SESSION_ID in detail
    client.post(f"/api/sets/recorder/{SESSION_ID}/stop")


@pytest.mark.requirement("SET-09")
def test_a_batch_with_no_session_id_is_refused(observer_client):
    """An unbound batch is not "compatible", it is one nobody can place.

    The field was optional for one round so an already-open browser running
    the shipped emitter would keep posting. That is the fallback that masks a
    failure: nothing in an unbound batch says which set it belongs to, so
    accepting it files a dead session's playback under whichever set happens
    to be recording now - silently, permanently, and exactly in the window
    where a stale tab is most likely. Codex found it on #709.

    422 is the right shape. The client is not merely unlucky, its payload is
    incomplete, and a user who reloads gets a build that sends the field. A
    loud recoverable failure in place of a quiet unrecoverable one.
    """
    client, _ = observer_client
    _start(client, ["opendj_decks"])
    resp = client.post(
        "/api/sets/deck-observations",
        json={"snapshots": _run(0, {"1": _deck("trk-a", audible=True)})},
    )
    assert resp.status_code == 422, resp.text
    assert "session_id" in resp.text, resp.text
    # CONTROL: the SAME batch with the live session id is accepted, so the
    # refusal above is about the missing field and not about the payload.
    ok = client.post(
        "/api/sets/deck-observations",
        json={
            "session_id": SESSION_ID,
            "snapshots": _run(0, {"1": _deck("trk-a", audible=True)}),
        },
    )
    assert ok.status_code == 202, ok.text
    client.post(f"/api/sets/recorder/{SESSION_ID}/stop")


@pytest.mark.requirement("SET-09")
def test_the_body_the_browser_actually_posts_is_accepted_and_recorded(
    observer_client,
):
    """The real client's payload, through the real endpoint. (Codex #709 P1)

    Every other test in this file hands the endpoint a dict THIS FILE wrote,
    and the browser tests are answered by a `fetch` THAT file wrote, so both
    halves could stay green while the two disagree about the wire. This is
    the joint: the fixture is the exact body the production emitter produces,
    captured and asserted CURRENT by
    ``apps/webui/frontend/tests/unit/deck-observer-emitter.test.mjs``, and
    replayed here through the production app. Change the client and the node
    lane goes red; change the server and this does. Neither can pass on a
    payload nobody sends.

    What this is NOT: a live socket. The transport itself is still covered by
    the end-to-end run, not by this pair.
    """
    payload = json.loads(EMITTER_POST.read_text(encoding="utf-8"))
    assert payload["session_id"] == SESSION_ID, (
        "the captured body names a session this test does not start; "
        "regenerate it with MDT_UPDATE_FIXTURES=1"
    )
    stamps = [
        datetime.fromisoformat(snap["observed_at"]) for snap in payload["snapshots"]
    ]
    span_s = (stamps[-1] - stamps[0]).total_seconds()
    assert span_s > ADVISORY_DWELL_S, (
        f"the captured run spans {span_s}s, under the {ADVISORY_DWELL_S}s "
        "advisory dwell, so it could not produce a row whatever the server "
        "did. Capture a longer run rather than lowering this assertion."
    )

    client, service = observer_client
    _start(client, ["opendj_decks"])

    posted = client.post("/api/sets/deck-observations", json=payload)
    assert posted.status_code == 202, posted.text

    # CONTROL: the same captured body with ONE field reinterpreted the way a
    # coercing client would send it. A 422 here is what earns the 202 above:
    # without it, an endpoint that accepted anything would satisfy this test.
    coerced = copy.deepcopy(payload)
    coerced["snapshots"][0]["decks"]["1"]["audible"] = "true"
    refused = client.post("/api/sets/deck-observations", json=coerced)
    assert refused.status_code == 422, refused.text

    service.active_source("opendj_decks").poll_once()
    client.post(f"/api/sets/recorder/{SESSION_ID}/stop")

    state = SetsState(db_path=service.db_path)
    rows = state.fetch_events(SESSION_ID, action="track_loaded")
    assert [r.track_stable_id for r in rows] == ["sid-1"], (
        "the browser's own payload did not produce a recorded row"
    )
    assert rows[0].deck == "1"
    assert rows[0].source == "opendj_decks"
