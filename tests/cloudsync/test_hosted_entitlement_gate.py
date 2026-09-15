"""The hub-side CloudSync entitlement check point, against the real router.

A self-hosted hub (the only kind today) is always entitled and never consults
a source. A hosted hub asks the configured source where the calling machine's
ENROLLED owner stands, and read_only means pull works and push is refused.
Every hub here is the real sync router on a real migrated sqlite DB; the
source is the shipped :class:`StaticEntitlementSource`, recorded.

- [if] a self-hosted hub consults a source at all [then] broken, [else stop].
- [if] a hosted hub accepts a push from a read_only owner [then] broken, [else stop].
- [if] a hosted hub refuses a pull from a read_only owner [then] broken, [else stop].
- [if] an unowned caller gets the source consulted with no subject [then] broken, [else stop].
- [if] any lifecycle transition deletes or changes a stored row [then] broken, [else stop].
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.entitlements import NOT_IN_PLAN_CODE, UI_REFUSAL_TITLE, Standing, StaticEntitlementSource
from apps.entitlements.lifecycle import TRANSITIONS, LifecycleState
from apps.shared.state import db as state_db
from apps.shared.state import machine_identity
from apps.shared.state import schema as state_schema
from apps.sync_hub import capabilities, client, client_refusal, entitlement_gate, maintenance
from apps.sync_hub import service as sync_service
from apps.sync_hub import status as sync_status
from apps.sync_hub.transport import refused

from .conftest import ENROLL_OWNER_SUB, seed_user
from .enrollment_helpers import http_enroll, machine_payload, mint_grant
from .enrollment_transport import TestClientTransport
from .test_hub_sync import _DEV_A, _T0, _T1, _insert_track, _open

pytestmark = pytest.mark.requirement("CAT-04")

SPOKE_NAME: str = "spoke"
OTHER_NAME: str = "other-spoke"
SECOND_SUB: str = "zz-test-second-owner-sub"
SECOND_EMAIL: str = "zz-test-second-owner@example.invalid"
PUSH_PATH: str = "/api/v1/sync/push"
PULL_PATH: str = "/api/v1/sync/pull"
DIGEST_PATH: str = "/api/v1/sync/digest"
STATUS_PATH: str = "/api/v1/sync/status"
HELLO_PATH: str = "/api/v1/sync/hello"
FEATURE: str = entitlement_gate.HOSTED_HUB_FEATURE


class RecordingSource:
    """The shipped static source, plus a log of every subject it was asked about."""

    def __init__(self, state: LifecycleState | None) -> None:
        table = {} if state is None else {(ENROLL_OWNER_SUB, FEATURE): Standing(state, None)}
        self._inner = StaticEntitlementSource(table, provider="recording-static")
        self.asked: list[object] = []

    @property
    def provider(self) -> str:
        return self._inner.provider

    def standing(self, subject: str, feature_id: str) -> Standing | None:
        self.asked.append(subject)
        return self._inner.standing(subject, feature_id)


@dataclass
class Hub:
    app: FastAPI
    http: TestClient
    transport: TestClientTransport
    hub_dir: Path

    def set_source(self, source: RecordingSource | None) -> None:
        self.app.state.entitlement_source = source


def _hub_app(hub_dir: Path, *, hosted: bool | None) -> FastAPI:
    app = FastAPI()
    app.state.state_db_path = str(client.state_db_path(hub_dir))
    app.state.sync_hub_data_dir = str(hub_dir)
    app.state.sync_hub_machine_name = "hosted-hub"
    if hosted is not None:
        app.state.sync_hub_hosted = hosted
    app.include_router(sync_service.router, prefix="/api/v1")
    return app


@pytest.fixture
def hosted_hub(enroll_hub_dir: Path) -> Iterator[Hub]:
    """A HOSTED hub with an active owner source. Tests swap the source."""
    app = _hub_app(enroll_hub_dir, hosted=True)
    app.state.entitlement_source = RecordingSource("active")
    with TestClient(app) as http:
        yield Hub(app, http, TestClientTransport(http), enroll_hub_dir)


@pytest.fixture
def self_hosted_hub(enroll_hub_dir: Path) -> Iterator[Hub]:
    """A hub that never set the hosted flag: the shipped kind. A source is attached anyway."""
    app = _hub_app(enroll_hub_dir, hosted=None)
    app.state.entitlement_source = RecordingSource("archived")
    with TestClient(app) as http:
        yield Hub(app, http, TestClientTransport(http), enroll_hub_dir)


# ----- helpers -------------------------------------------------------------


def _enroll(hub: Hub, spoke_dir: Path, *, name: str = SPOKE_NAME) -> str:
    """Enroll the spoke under the seeded owner through the real /enroll door."""
    http_enroll(hub.transport, spoke_dir, name=name, token=mint_grant(hub.hub_dir))
    return machine_identity.get_or_create_machine_id(spoke_dir)


def _local_track_ids(spoke_dir: Path) -> list[str]:
    conn = _open(spoke_dir)
    try:
        return [r[0] for r in conn.execute("SELECT stable_id FROM tracks ORDER BY stable_id")]
    finally:
        conn.close()


def _add_track(spoke_dir: Path, stable_id: str, updated_at: str) -> None:
    conn = _open(spoke_dir)
    try:
        _insert_track(conn, stable_id, title=stable_id, updated_at=updated_at, origin=_DEV_A)
    finally:
        conn.close()


def _sync(hub: Hub, spoke_dir: Path, *, name: str = SPOKE_NAME) -> client.SyncResult:
    return client.run_sync(spoke_dir, "http://hub.invalid", transport=hub.transport, name=name)


def _hub_track_ids(hub_dir: Path) -> list[str]:
    conn = state_db.open_rw(client.state_db_path(hub_dir))
    try:
        return [r[0] for r in conn.execute("SELECT stable_id FROM tracks ORDER BY stable_id")]
    finally:
        conn.close()


def _snapshot(hub_dir: Path) -> dict[str, list[tuple[Any, ...]]]:
    """Every row of every table on the hub, except the machines' last_seen churn."""
    conn = state_db.open_rw(client.state_db_path(hub_dir))
    try:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        out: dict[str, list[tuple[Any, ...]]] = {}
        for table in tables:
            query = (
                "SELECT machine_id, name, platform, is_hub, data_root, first_seen "
                "FROM machines ORDER BY machine_id"
                if table == "machines"
                else f"SELECT * FROM {table} ORDER BY 1"
            )
            out[table] = [tuple(row) for row in conn.execute(query)]
        return out
    finally:
        conn.close()


def _push_empty(hub: Hub, machine_id: str) -> Any:
    return hub.http.post(
        PUSH_PATH,
        json={
            "machine_id": machine_id,
            "schema_version": state_schema.SCHEMA_VERSION,
            "rows": [],
            "capabilities": list(capabilities.THIS_BUILD),
        },
    )


def _read(hub: Hub, path: str, machine_id: str) -> Any:
    return hub.http.get(
        path,
        params={
            "machine_id": machine_id,
            "since_seq": "0",
            "capabilities": list(capabilities.THIS_BUILD),
        }
        if path == PULL_PATH
        else {"machine_id": machine_id, "capabilities": list(capabilities.THIS_BUILD)},
    )


# ----- self-hosted: unchanged, never consults ------------------------------


def test_a_self_hosted_hub_syncs_and_never_consults_the_source(
    self_hosted_hub: Hub, enroll_spoke_dir: Path
) -> None:
    """If a self-hosted hub asks a source anything (even one saying archived) then broken."""
    _enroll(self_hosted_hub, enroll_spoke_dir)
    _add_track(enroll_spoke_dir, "t-self", _T0)
    _sync(self_hosted_hub, enroll_spoke_dir)
    assert _hub_track_ids(self_hosted_hub.hub_dir) == ["t-self"]
    assert self_hosted_hub.app.state.entitlement_source.asked == []


def test_an_explicit_false_flag_is_self_hosted(
    enroll_hub_dir: Path, enroll_spoke_dir: Path
) -> None:
    """If hosted=False is treated differently from an absent flag then broken."""
    app = _hub_app(enroll_hub_dir, hosted=False)
    app.state.entitlement_source = RecordingSource("archived")
    with TestClient(app) as http:
        hub = Hub(app, http, TestClientTransport(http), enroll_hub_dir)
        machine_id = _enroll(hub, enroll_spoke_dir)
        assert _push_empty(hub, machine_id).status_code == 200
    assert app.state.entitlement_source.asked == []


def test_a_non_bool_hosted_flag_fails_fast(enroll_hub_dir: Path, enroll_spoke_dir: Path) -> None:
    """If a truthy string like "0" silently switches billing on or off then broken."""
    app = _hub_app(enroll_hub_dir, hosted=None)
    app.state.sync_hub_hosted = "0"
    with TestClient(app) as http:
        hub = Hub(app, http, TestClientTransport(http), enroll_hub_dir)
        machine_id = _enroll(hub, enroll_spoke_dir)
        response = _push_empty(hub, machine_id)
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == entitlement_gate.FLAG_INVALID_CODE


def test_status_reports_a_hosted_hub_and_its_provider(
    hosted_hub: Hub, enroll_spoke_dir: Path
) -> None:
    """If /sync/status cannot tell an agent the hub is hosted, and by which source, then broken."""
    machine_id = _enroll(hosted_hub, enroll_spoke_dir)
    body = hosted_hub.http.get(STATUS_PATH, params={"machine_id": machine_id}).json()
    assert (body["hosted"], body["entitlement_provider"]) == (True, "recording-static")


def test_status_reports_a_self_hosted_hub_with_no_provider(
    self_hosted_hub: Hub, enroll_spoke_dir: Path
) -> None:
    """If a self-hosted hub's status names its attached source then it reads as gated."""
    machine_id = _enroll(self_hosted_hub, enroll_spoke_dir)
    body = self_hosted_hub.http.get(STATUS_PATH, params={"machine_id": machine_id}).json()
    assert (body["hosted"], body["entitlement_provider"]) == (False, None)


def test_a_hosted_hub_db_with_two_owners_is_refused(
    hosted_hub: Hub, enroll_spoke_dir: Path, enroll_other_spoke_dir: Path
) -> None:
    """If a hosted hub serves a DB two owners share then pull hands each the other's library."""
    machine_id = _enroll(hosted_hub, enroll_spoke_dir)
    assert _push_empty(hosted_hub, machine_id).status_code == 200  # one owner: served
    seed_user(client.state_db_path(hosted_hub.hub_dir), sub=SECOND_SUB, email=SECOND_EMAIL)
    http_enroll(
        hosted_hub.transport,
        enroll_other_spoke_dir,
        name="second-owner-spoke",
        token=mint_grant(hosted_hub.hub_dir, email=SECOND_EMAIL),
    )
    for response in (_push_empty(hosted_hub, machine_id), _read(hosted_hub, PULL_PATH, machine_id)):
        assert response.status_code == 503, response.text
        assert response.json()["detail"]["code"] == entitlement_gate.MULTI_OWNER_CODE


# ----- hosted: the owner's lifecycle decides -------------------------------


def test_a_hosted_hub_asks_about_the_enrolled_owner_and_serves_an_active_one(
    hosted_hub: Hub, enroll_spoke_dir: Path
) -> None:
    """If a hosted hub skips the source, or asks about anyone but the owner, then broken."""
    _enroll(hosted_hub, enroll_spoke_dir)
    _add_track(enroll_spoke_dir, "t-active", _T0)
    _sync(hosted_hub, enroll_spoke_dir)
    assert _hub_track_ids(hosted_hub.hub_dir) == ["t-active"]
    asked = hosted_hub.app.state.entitlement_source.asked
    assert asked, "the hosted gate never consulted the source"
    assert set(asked) == {ENROLL_OWNER_SUB}


def test_a_read_only_owner_cannot_push_but_can_still_pull(
    hosted_hub: Hub, enroll_spoke_dir: Path
) -> None:
    """If read_only accepts a push, or refuses a pull, then a lapse strands a library."""
    machine_id = _enroll(hosted_hub, enroll_spoke_dir)
    _add_track(enroll_spoke_dir, "t-before", _T0)
    _sync(hosted_hub, enroll_spoke_dir)
    hosted_hub.set_source(RecordingSource("read_only"))

    # Push through the REAL client: refused with the plan code, nothing lands.
    _add_track(enroll_spoke_dir, "t-after", _T1)
    assert _sync(hosted_hub, enroll_spoke_dir).push_refused is True
    assert _hub_track_ids(hosted_hub.hub_dir) == ["t-before"]

    refused = _push_empty(hosted_hub, machine_id)
    assert refused.status_code == 403
    detail = refused.json()["detail"]
    assert detail["code"] == NOT_IN_PLAN_CODE
    assert detail["ui_title"] == UI_REFUSAL_TITLE
    assert "read_only" in detail["message"]

    pulled = _read(hosted_hub, PULL_PATH, machine_id)
    assert pulled.status_code == 200, pulled.text
    assert [row["pk"] for row in pulled.json()["rows"] if row["table"] == "tracks"] == [
        ["t-before"]
    ]
    assert _read(hosted_hub, DIGEST_PATH, machine_id).status_code == 200


def test_the_real_client_still_pulls_under_read_only_with_a_pending_edit(
    hosted_hub: Hub, enroll_spoke_dir: Path, enroll_other_spoke_dir: Path
) -> None:
    """If run_sync with a pending edit never pulls under read_only then a lapse strands data."""
    _enroll(hosted_hub, enroll_spoke_dir)
    _enroll(hosted_hub, enroll_other_spoke_dir, name=OTHER_NAME)
    _add_track(enroll_spoke_dir, "t-before", _T0)
    _sync(hosted_hub, enroll_spoke_dir)
    _add_track(enroll_other_spoke_dir, "t-other", _T0)
    _sync(hosted_hub, enroll_other_spoke_dir, name=OTHER_NAME)
    assert "t-other" not in _local_track_ids(enroll_spoke_dir)  # presence below means it arrived

    hosted_hub.set_source(RecordingSource("read_only"))
    _add_track(enroll_spoke_dir, "t-pending", _T1)
    result = maintenance.sync(
        enroll_spoke_dir, "http://hub.invalid", name=SPOKE_NAME, transport=hosted_hub.transport
    )
    assert result.push_refused is True and result.pulled >= 1
    assert result.digest_inconclusive is False  # the refusal owns the verdict
    assert "t-other" in _local_track_ids(enroll_spoke_dir)  # pulled through the real client
    assert "t-pending" in _local_track_ids(enroll_spoke_dir)  # the edit is kept locally
    assert _hub_track_ids(hosted_hub.hub_dir) == ["t-before", "t-other"]
    assert maintenance._report_sync(result, enroll_spoke_dir) == maintenance.EXIT_PUSH_REFUSED
    journal = sync_status.read_results(enroll_spoke_dir)[0]
    assert journal.status == "error" and NOT_IN_PLAN_CODE in journal.message

    # The fence was held: resubscribing delivers the edit that was refused.
    hosted_hub.set_source(RecordingSource("active"))
    assert _sync(hosted_hub, enroll_spoke_dir).push_refused is False
    assert _hub_track_ids(hosted_hub.hub_dir) == ["t-before", "t-other", "t-pending"]


def test_any_other_refusal_still_raises_through_the_real_client(
    hosted_hub: Hub, enroll_spoke_dir: Path
) -> None:
    """If the client swallows any refusal but the read_only push then archived reads as success."""
    _enroll(hosted_hub, enroll_spoke_dir)
    hosted_hub.set_source(RecordingSource("archived"))
    _add_track(enroll_spoke_dir, "t-pending", _T0)
    with pytest.raises(client.SyncTransportError) as excinfo:
        _sync(hosted_hub, enroll_spoke_dir)
    assert excinfo.value.status_code == 403
    assert PULL_PATH in str(excinfo.value)  # push was survived, the archived PULL raised


def test_only_the_declared_plan_refusal_is_survivable() -> None:
    """If the client survives any refusal but a 403 entitlement_not_in_plan then broken."""
    plan = json.dumps({"detail": {"code": NOT_IN_PLAN_CODE, "message": "m"}})
    unowned = json.dumps({"detail": {"code": entitlement_gate.UNOWNED_CODE, "message": "m"}})
    assert client_refusal.is_plan_refusal(refused("POST /push", 403, plan))
    assert not client_refusal.is_plan_refusal(refused("POST /push", 403, unowned))
    assert not client_refusal.is_plan_refusal(refused("POST /push", 500, plan))
    assert not client_refusal.is_plan_refusal(refused("POST /push", 403, "not json"))


def test_an_archived_owner_is_refused_everything_but_status(
    hosted_hub: Hub, enroll_spoke_dir: Path
) -> None:
    """If archived still moves rows, or stops reporting status and digest, then broken."""
    machine_id = _enroll(hosted_hub, enroll_spoke_dir)
    hosted_hub.set_source(RecordingSource("archived"))
    assert _push_empty(hosted_hub, machine_id).status_code == 403
    pulled = _read(hosted_hub, PULL_PATH, machine_id)
    assert pulled.status_code == 403
    assert pulled.json()["detail"]["code"] == NOT_IN_PLAN_CODE
    # The reporting endpoints never transfer a row, so they answer in every
    # state: a lapsed client can always see why its sync stopped.
    assert _read(hosted_hub, DIGEST_PATH, machine_id).status_code == 200
    assert hosted_hub.http.get(STATUS_PATH, params={"machine_id": machine_id}).status_code == 200


def test_an_owner_with_no_record_is_refused(hosted_hub: Hub, enroll_spoke_dir: Path) -> None:
    """If an owner the source never heard of can push to a hosted hub then it is free."""
    machine_id = _enroll(hosted_hub, enroll_spoke_dir)
    hosted_hub.set_source(RecordingSource(None))
    response = _push_empty(hosted_hub, machine_id)
    assert response.status_code == 403
    assert "no plan on record" in response.json()["detail"]["message"]


def test_an_unowned_machine_is_refused_without_consulting_the_source(
    hosted_hub: Hub, enroll_spoke_dir: Path
) -> None:
    """If an unenrolled caller makes the hub consult the source with no subject then broken."""
    hosted_hub.transport.post(
        HELLO_PATH,
        {
            "machine": machine_payload(enroll_spoke_dir, name=SPOKE_NAME),
            "schema_version": state_schema.SCHEMA_VERSION,
        },
    )
    machine_id = machine_identity.get_or_create_machine_id(enroll_spoke_dir)
    response = _push_empty(hosted_hub, machine_id)
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == entitlement_gate.UNOWNED_CODE
    assert hosted_hub.app.state.entitlement_source.asked == []


def test_a_hosted_hub_with_no_source_fails_fast(hosted_hub: Hub, enroll_spoke_dir: Path) -> None:
    """If a hosted hub with no source guesses an answer instead of 503 then broken."""
    machine_id = _enroll(hosted_hub, enroll_spoke_dir)
    hosted_hub.set_source(None)
    response = _push_empty(hosted_hub, machine_id)
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == entitlement_gate.NO_SOURCE_CODE


# ----- never destroy data ---------------------------------------------------


def test_no_lifecycle_transition_deletes_or_changes_a_row(
    hosted_hub: Hub, enroll_spoke_dir: Path
) -> None:
    """If any lifecycle transition removes or rewrites a stored row then a lapse destroys data."""
    machine_id = _enroll(hosted_hub, enroll_spoke_dir)
    _add_track(enroll_spoke_dir, "t-keep", _T0)
    _sync(hosted_hub, enroll_spoke_dir)
    baseline = _snapshot(hosted_hub.hub_dir)
    # Presence first: the snapshot must actually hold the rows it protects,
    # or "nothing changed" would be the answer for an empty hub too.
    assert baseline["tracks"] and baseline["machine_owners"] and baseline["users"]

    walked = 0
    for (source_state, _event), target_state in TRANSITIONS.items():
        for state in (source_state, target_state):
            hosted_hub.set_source(RecordingSource(state))
            _push_empty(hosted_hub, machine_id)
            _read(hosted_hub, PULL_PATH, machine_id)
            _read(hosted_hub, DIGEST_PATH, machine_id)
            hosted_hub.http.get(STATUS_PATH, params={"machine_id": machine_id})
            assert _snapshot(hosted_hub.hub_dir) == baseline, (source_state, _event, state)
        walked += 1
    assert walked == len(TRANSITIONS) > 0

    # And resubscribing restores exactly what was there.
    hosted_hub.set_source(RecordingSource("active"))
    pulled = _read(hosted_hub, PULL_PATH, machine_id)
    assert pulled.status_code == 200
    assert ["t-keep"] in [row["pk"] for row in pulled.json()["rows"]]


def test_snapshot_covers_every_table(hosted_hub: Hub) -> None:
    """If the snapshot helper silently drops tables then the no-delete test proves nothing."""
    snap = _snapshot(hosted_hub.hub_dir)
    conn = sqlite3.connect(client.state_db_path(hosted_hub.hub_dir))
    try:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
    finally:
        conn.close()
    assert set(snap) == tables
    assert {"tracks", "users", "machine_owners", "machines", "hub_changelog"} <= tables
