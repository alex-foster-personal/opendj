"""Far-future stamps: what the fleet ACTUALLY does today (plan W8, map L3 gap).

[if] far-future stamp handling drifts from the pinned behavior [then] fail, [else stop].

Nothing bounds a future-dated ``updated_at``. ``canonical_timestamp`` only
parses, so any stamp ``datetime`` can represent is accepted. The tolerance,
and whether an over-tolerance stamp is quarantined or refused with 422, is an
open OWNER decision (decision batch item (c) in
``.planning/cloudsync-levels-map-2026-09-11.md``), so this module pins
behavior rather than choosing it.

Measured on this branch, and asserted below, the behavior is loud, not
silent, but it surfaces on the wrong machine:

1. The hub accepts a 2099 or 9999 stamp as-is. The far-future value wins LWW
   on the hub and on its author.
2. A peer that pulls that row and then edits it on a real clock has its push
   REJECTED, keeps its own edit locally, and halts with
   :class:`~apps.sync_hub.client.SyncDigestMismatch` on every retry. The
   edit is stranded rather than lost, and the spoke is wedged until someone
   repairs the row by hand.
3. A stamp past the last representable instant (year 10000) is refused off
   the wire with 422 ``SYNC_PROTOCOL``. That is the only bound today.

The strict xfail at the bottom records the guard the owner has to specify.
It flips to a failure the day either a quarantine or a refusal lands, so
whoever builds it has to replace it with the real contract.

Acceptance, one test each:
- if a far-future stamp's win, and the wedge it causes, change unnoticed then broken
- if a stamp past year 9999 lands on the hub instead of a 422 then broken
- if a push stamped decades ahead is accepted as-is then broken (owner decision pending)
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.shared.state import schema as state_schema
from apps.shared.state import sync_stamp
from apps.sync_hub import client, service

from .enrollment_transport import TestClientTransport
from .test_hub_sync import _DEV_A, _DEV_B, _open, _seed_common_track, _set_track_title, _track_title
from .test_hub_sync_schema_version_matrix import _hub_row_as_wire

pytestmark = pytest.mark.requirement("CLOUDSYNC-03")

FAR_FUTURE_STAMPS: tuple[str, ...] = (
    "2099-01-01T00:00:00.000000+00:00",
    "9999-12-31T23:59:59.999999+00:00",
)
BEYOND_THE_CALENDAR: str = "10000-01-01T00:00:00+00:00"
FUTURE_TITLE: str = "from a clock that is far ahead"
REAL_TITLE: str = "a real edit made now"
PUSH_PATH: str = f"{client.API_PREFIX}/push"


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
def spoke_b(tmp_path: Path) -> Path:
    return tmp_path / "spoke-b"


@pytest.fixture
def hub(hub_dir: Path) -> Iterator[TestClientTransport]:
    """The real sync router on an empty hub DB; only the socket is absent."""
    app = FastAPI()
    app.state.state_db_path = str(client.state_db_path(hub_dir))
    app.state.sync_hub_data_dir = str(hub_dir)
    app.state.sync_hub_machine_name = "hub"
    app.include_router(service.router, prefix="/api/v1")
    with TestClient(app) as http:
        yield TestClientTransport(http)


def _sync(data_dir: Path, hub: TestClientTransport, name: str) -> client.SyncResult:
    return client.run_sync(data_dir, "http://hub.invalid", transport=hub, name=name)


def _title(data_dir: Path) -> str | None:
    conn = _open(data_dir)
    try:
        return _track_title(conn, "trk-1")
    finally:
        conn.close()


def _stored_stamp(data_dir: Path) -> str:
    conn = _open(data_dir)
    try:
        return str(
            conn.execute("SELECT updated_at FROM tracks WHERE stable_id = 'trk-1'").fetchone()[0]
        )
    finally:
        conn.close()


def _edit(data_dir: Path, *, title: str, updated_at: str, origin: str) -> None:
    conn = _open(data_dir)
    try:
        _set_track_title(conn, "trk-1", title=title, updated_at=updated_at, origin=origin)
    finally:
        conn.close()


@pytest.mark.parametrize("future", FAR_FUTURE_STAMPS, ids=["year-2099", "year-9999"])
def test_a_far_future_stamp_wins_and_wedges_the_next_real_clock_editor(
    hub: TestClientTransport,
    hub_dir: Path,
    spoke_a: Path,
    spoke_b: Path,
    future: str,
) -> None:
    """if a far-future stamp's win, and the wedge it causes, change unnoticed then broken"""
    print("if a far-future stamp's win, and the wedge it causes, change unnoticed then broken")
    _seed_common_track((spoke_a, spoke_b), "trk-1")
    _sync(spoke_a, hub, "spoke-a")
    _sync(spoke_b, hub, "spoke-b")

    _edit(spoke_b, title=FUTURE_TITLE, updated_at=future, origin=_DEV_B)
    assert _sync(spoke_b, hub, "spoke-b").accepted == 1, "the hub refused the future stamp"
    assert _stored_stamp(hub_dir) == future, "the hub clamped or rewrote the future stamp"
    assert _sync(spoke_a, hub, "spoke-a").applied == 1, "A never received the future row"

    _edit(spoke_a, title=REAL_TITLE, updated_at=sync_stamp.canonical_now(), origin=_DEV_A)
    for attempt in range(2):
        with pytest.raises(client.SyncDigestMismatch, match="tables \\['tracks'\\] differ"):
            _sync(spoke_a, hub, "spoke-a")
        assert _title(spoke_a) == REAL_TITLE, f"attempt {attempt}: A's own edit was overwritten"

    assert _title(hub_dir) == FUTURE_TITLE, "the far-future row stopped winning on the hub"
    assert _title(spoke_b) == FUTURE_TITLE


def test_a_stamp_past_the_calendar_is_refused_with_422(
    hub: TestClientTransport, hub_dir: Path, spoke_a: Path
) -> None:
    """if a stamp past year 9999 lands on the hub instead of a 422 then broken"""
    print("if a stamp past year 9999 lands on the hub instead of a 422 then broken")
    _seed_common_track((spoke_a,), "trk-1")
    registered = _sync(spoke_a, hub, "spoke-a")

    def push(stamp: str) -> dict[str, object]:
        row = _hub_row_as_wire(hub_dir, "trk-1", title=FUTURE_TITLE, updated_at=stamp)
        return hub.post(
            PUSH_PATH,
            {
                "machine_id": registered.machine_id,
                "schema_version": state_schema.SCHEMA_VERSION,
                "rows": [row],
            },
        )

    with pytest.raises(client.SyncTransportError) as excinfo:
        push(BEYOND_THE_CALENDAR)
    assert "HTTP 422" in str(excinfo.value) and "SYNC_PROTOCOL" in str(excinfo.value)
    assert _title(hub_dir) == "original", "the refused push landed a row"

    # Positive control: the last representable instant is still accepted,
    # which is exactly why the far-future pin above is needed.
    assert push(FAR_FUTURE_STAMPS[-1])["accepted"] == 1
    assert _title(hub_dir) == FUTURE_TITLE


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "OWNER DECISION PENDING: no future-skew tolerance exists. Decide the "
        "tolerance and quarantine vs 422 (map decision batch item (c)), then "
        "replace this with the real contract."
    ),
)
def test_a_push_stamped_decades_ahead_is_held_or_refused(
    hub: TestClientTransport, spoke_a: Path
) -> None:
    """if a push stamped decades ahead is accepted as-is then broken (owner decision pending)"""
    print("if a push stamped decades ahead is accepted as-is then broken (owner decision pending)")
    _seed_common_track((spoke_a,), "trk-1")
    _edit(spoke_a, title=FUTURE_TITLE, updated_at=FAR_FUTURE_STAMPS[0], origin=_DEV_A)
    result = _sync(spoke_a, hub, "spoke-a")
    assert result.accepted == 0, (
        f"a row stamped {FAR_FUTURE_STAMPS[0]} was accepted as-is "
        f"(accepted={result.accepted}); no skew guard exists yet"
    )
