"""Round 4 (R8 lane): the round 3 "R8 small findings" list, as permanent tests.

Companion to :mod:`tests.cloudsync.test_hub_sync_round3` (round 2 findings)
and :mod:`tests.cloudsync.test_hub_sync_round4` (this round's R1/R2/R6/R7
findings, the engine-security-restore lane). Filename carries the ``_r8``
suffix rather than plain ``round4`` because both round 4 lanes independently
picked "round4" for their module name; this lane's file lost that race on
disk and was renamed rather than overwriting the other lane's tests.

Every test here is one reproduction sketch from
``.planning/cloudsync-round3-adversarial.md`` Part 2 ("R8. Smaller things")
turned into a regression. The mapping, so a failure names the defect it just
let back in:

- R8 (``deleted_at``) -> ``_checked_value`` special-cased ``updated_at`` only,
  so a push carrying an unparseable ``deleted_at`` was accepted, stored
  verbatim, and hashed into the digest -- the one column a tombstone's
  correctness depends on was the one column this boundary never checked.
- R8 (``/status`` write ordering) -> ``service.status`` ran ``_hub_identity``
  (which upserts the HUB's own ``machines`` row) in a transaction BEFORE
  checking whether the CALLER was registered, so a refused status call still
  had a write side effect.
- 7b (``/digest`` registration) -> every other endpoint refuses a machine
  that never said hello (round 1 finding 7b); ``/digest`` alone took no
  ``machine_id`` and answered anyone, including the hub's live changelog
  position.

Acceptance criteria:
- if an unparseable ``deleted_at`` on the wire is accepted instead of 422ing,
  the protocol boundary is validating ``updated_at`` only -- broken.
- if a real ``deleted_at`` timestamp stops propagating (soft delete breaks),
  the fix over-tightened the boundary -- also broken.
- if a ``/status`` call from an unregistered machine still writes the hub's
  own machines row before refusing, the guard is decorative -- broken.
- if ``/digest`` answers a machine that never said hello, 7b is closed
  everywhere except here -- broken.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.shared.state import schema as state_schema
from apps.sync_hub import client, service
from tests.cloudsync.test_hub_sync import (
    _DEV_A,
    _T0,
    _T1,
    _open,
    _sync,
    _TestClientTransport,
)

pytestmark = pytest.mark.requirement("CAT-04")


# ----- fixtures ------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_hub_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``MDT_IS_HUB`` from the developer's shell must not steer these tests."""
    monkeypatch.delenv("MDT_IS_HUB", raising=False)


@pytest.fixture
def hub_dir(tmp_path: Path) -> Path:
    return tmp_path / "hub"


@pytest.fixture
def spoke_a(tmp_path: Path) -> Path:
    return tmp_path / "spoke-a"


@pytest.fixture
def hub(hub_dir: Path) -> object:
    app = FastAPI()
    app.state.state_db_path = str(client.state_db_path(hub_dir))
    app.state.sync_hub_data_dir = str(hub_dir)
    app.state.sync_hub_machine_name = "hub"
    app.include_router(service.router, prefix="/api/v1")
    with TestClient(app) as http:
        yield _TestClientTransport(http)


def _track_row(stable_id: str, *, updated_at: str, deleted_at: str | None) -> dict:
    return {
        "table": "tracks",
        "pk": [stable_id],
        "values": {
            "stable_id": stable_id,
            "stable_id_tier": "inferred",
            "title": "r8 deleted_at",
            "artists_json": None,
            "album": None,
            "isrc": None,
            "duration_ms": None,
            "file_path": None,
            "content_hash": None,
            "audio_hash": None,
            "restored_at": None,
            "deleted_reason": None,
            "created_at": _T0,
            "updated_at": updated_at,
            "origin_device_id": _DEV_A,
            "deleted_at": deleted_at,
        },
    }


# ----- R8: deleted_at validated at the protocol boundary -------------------


@pytest.mark.parametrize(
    "bad",
    ["not-a-timestamp-at-all", "2026-08-30T10:00:00", "", "2026-13-01T00:00:00+00:00"],
)
def test_an_unparseable_deleted_at_is_refused_with_422(
    hub: _TestClientTransport, spoke_a: Path, bad: str
) -> None:
    """Round 3 R8: garbage ``deleted_at`` was accepted and stored verbatim.

    ``[observed]`` in the round 3 review: a push carrying
    ``deleted_at: "not-a-timestamp-at-all"`` came back
    ``{'accepted': 1, 'rejected': 0, 'seq': 1}`` and the hub stored the
    garbage string, then handed it to the next spoke that pulled. This must
    422 exactly like an unparseable ``updated_at`` does.
    """
    result = _sync(spoke_a, hub, "spoke-a")
    with pytest.raises(client.SyncTransportError) as excinfo:
        hub.post(
            f"{client.API_PREFIX}/push",
            {
                "machine_id": result.machine_id,
                "schema_version": state_schema.SCHEMA_VERSION,
                "rows": [_track_row("trk-garbage", updated_at=_T1, deleted_at=bad)],
            },
        )
    assert "422" in str(excinfo.value)
    assert "SYNC_PROTOCOL" in str(excinfo.value)


def test_a_null_deleted_at_still_means_not_deleted(
    hub: _TestClientTransport, spoke_a: Path
) -> None:
    """The fix must not touch the NULL case: NULL still means "not deleted".

    ``_checked_value`` returns ``None`` before the column-specific branch
    runs, for every column -- confirmed here so the boundary fix did not
    accidentally start requiring every row to carry a tombstone stamp.
    """
    result = _sync(spoke_a, hub, "spoke-a")
    response = hub.post(
        f"{client.API_PREFIX}/push",
        {
            "machine_id": result.machine_id,
            "schema_version": state_schema.SCHEMA_VERSION,
            "rows": [_track_row("trk-alive", updated_at=_T1, deleted_at=None)],
        },
    )
    assert response["accepted"] == 1


def test_a_real_deleted_at_timestamp_still_propagates(
    hub: _TestClientTransport, spoke_a: Path
) -> None:
    """A genuine soft delete must still sync -- the fix rejects garbage only."""
    result = _sync(spoke_a, hub, "spoke-a")
    response = hub.post(
        f"{client.API_PREFIX}/push",
        {
            "machine_id": result.machine_id,
            "schema_version": state_schema.SCHEMA_VERSION,
            "rows": [_track_row("trk-tombstoned", updated_at=_T1, deleted_at=_T1)],
        },
    )
    assert response["accepted"] == 1


# ----- R8: /status refuses BEFORE it writes ---------------------------------


def test_a_refused_status_call_does_not_register_the_hub(
    hub: _TestClientTransport, hub_dir: Path
) -> None:
    """Round 3 R8: a refused ``/status`` call still had a write side effect.

    ``[observed]`` in the round 3 review: ``status refused: HTTP 409
    SYNC_UNKNOWN_MACHINE`` then ``machines rows after a REFUSED status:
    [('427380e6...', 'hub')]`` -- the hub's own row landed anyway, because
    ``_hub_identity`` ran (and committed) before ``_require_registered`` ever
    looked at the caller. Nothing must be written on the refused path.
    """
    with pytest.raises(client.SyncTransportError) as excinfo:
        hub.get(f"{client.API_PREFIX}/status", {"machine_id": "deadbeef" * 4})
    assert "SYNC_UNKNOWN_MACHINE" in str(excinfo.value)

    conn = _open(hub_dir)
    try:
        rows = conn.execute("SELECT machine_id FROM machines").fetchall()
    finally:
        conn.close()
    assert rows == [], "a refused /status call registered the hub's own row"


# ----- 7b: /digest requires registration like every other endpoint ---------


def test_digest_refuses_a_machine_that_never_said_hello(
    hub: _TestClientTransport,
) -> None:
    """Round 1 finding 7b, still open for ``/digest`` as of round 3.

    ``/pull`` and ``/status`` both refuse an unregistered caller (round 2);
    ``/digest`` took no ``machine_id`` at all and answered anyone, including
    the hub's live changelog position.
    """
    with pytest.raises(client.SyncTransportError) as excinfo:
        hub.get(f"{client.API_PREFIX}/digest", {"machine_id": "deadbeef" * 4})
    assert "SYNC_UNKNOWN_MACHINE" in str(excinfo.value)


def test_digest_without_a_machine_id_is_refused(hub: _TestClientTransport) -> None:
    """The guard cannot be skipped by omitting the parameter, like ``/pull``."""
    with pytest.raises(client.SyncTransportError) as excinfo:
        hub.get(f"{client.API_PREFIX}/digest", {})
    assert "422" in str(excinfo.value)


def test_digest_answers_a_registered_machine(
    hub: _TestClientTransport, spoke_a: Path
) -> None:
    """The tightened endpoint still works for the case it exists to serve."""
    result = _sync(spoke_a, hub, "spoke-a")
    response = hub.get(
        f"{client.API_PREFIX}/digest", {"machine_id": result.machine_id}
    )
    assert "overall" in response and "seq" in response
