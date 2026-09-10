"""Bidirectional hub sync: read-surface parity, schema tripwire, wiring.

Split out of ``test_hub_sync.py`` (round 4 quality-gate ratchet: that file
crossed 600 lines). Same contract; the ``hub``/``spoke_a`` fixtures below are
duplicated rather than imported, matching the convention
``test_soft_delete.py`` already established for this package -- a pytest
fixture re-exported by import collides (pyflakes F811) with the same-named
parameter every test using it declares, so a short, stable fixture like this
one is cheaper to keep in sync by eye than to fight the linter over. The
substantial helpers (``_open``, ``_seed_common_track``, ``_sync``,
``_TestClientTransport``) are imported, not duplicated, so THEY cannot drift.

Contract under test: ``specs/design_decision_04.md`` over the v6 schema of
``specs/design_decision_05.md``. Acceptance criteria, one test each:

- if ``GET /status`` or ``GET /digest`` cannot answer without a spoke
  driving a full sync first, the agent-native-parity read surface is
  missing -- broken.
- if a table joins the LWW sync set without ``updated_at`` /
  ``origin_device_id`` / ``deleted_at``, every one of its rows would look
  epoch-old and silently lose every conflict -- broken.
- if a push from an unregistered machine, or a hello from a peer on another
  schema version, is accepted rather than refused at the boundary, a
  malformed or incompatible spoke can corrupt the hub -- broken.
- if the webui app does not mount the sync router, the CLI/HTTP twin the
  agent-native parity rule requires does not exist -- broken.
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.shared.state import schema as state_schema
from apps.sync_hub import client, protocol, service
from apps.webui.server.app import create_app

from .test_hub_sync import _T0, _open, _seed_common_track, _sync, _TestClientTransport

pytestmark = pytest.mark.requirement("CAT-04")

#: Hex characters in a sha256 digest. ``protocol.sync_digest`` rolls the
#: per-table hashes up with ``hashlib.sha256``, so a rollup of any other
#: length is a different algorithm, not a different value.
SHA256_HEX_LENGTH: int = 64


@pytest.fixture(autouse=True)
def _no_hub_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``MDT_IS_HUB`` from the developer's shell must not steer these tests."""
    monkeypatch.delenv("MDT_IS_HUB", raising=False)


@pytest.fixture
def hub_dir(tmp_path: Path) -> Path:
    """The hub's empty data dir. Guarantees as in ``test_hub_sync.py``."""
    return tmp_path / "hub"


@pytest.fixture
def spoke_a(tmp_path: Path) -> Path:
    """Spoke A's empty data dir. Guarantees as in ``test_hub_sync.py``."""
    return tmp_path / "spoke-a"


@pytest.fixture
def hub(hub_dir: Path) -> Iterator[_TestClientTransport]:
    """The real sync router on an empty hub DB. See ``test_hub_sync.hub``."""
    app = FastAPI()
    app.state.state_db_path = str(client.state_db_path(hub_dir))
    app.state.sync_hub_data_dir = str(hub_dir)
    app.state.sync_hub_machine_name = "hub"
    app.include_router(service.router, prefix="/api/v1")
    with TestClient(app) as http:
        yield _TestClientTransport(http)


def test_status_and_digest_endpoints_answer(
    hub: _TestClientTransport, spoke_a: Path
) -> None:
    """Agent parity: the hub's read surface works without a spoke driving it."""
    _seed_common_track((spoke_a,), "trk-1")
    result = _sync(spoke_a, hub, "spoke-a")

    status = hub.get(
        f"{client.API_PREFIX}/status", {"machine_id": result.machine_id}
    )
    assert status["seq"] >= 1
    assert status["row_counts"]["tracks"] == 1
    assert {machine["name"] for machine in status["machines"]} >= {"hub", "spoke-a"}

    digest = protocol.SyncDigest.from_wire(
        hub.get(f"{client.API_PREFIX}/digest", {"machine_id": result.machine_id})
    )
    assert set(digest.tables) == set(protocol.DIGEST_TABLES)
    assert len(digest.overall) == SHA256_HEX_LENGTH


def test_every_lww_table_carries_the_sync_trio(spoke_a: Path) -> None:
    """Tripwire: a table joins the sync set only with the v6 sync columns.

    Without this, adding a table to ``SYNC_TABLES`` that lacks
    ``updated_at`` / ``origin_device_id`` / ``deleted_at`` would make every
    one of its rows look epoch-old and silently lose every conflict.
    """
    conn = _open(spoke_a)
    try:
        for spec in protocol.SYNC_TABLES:
            columns = set(protocol.table_columns(conn, spec.name))
            missing = [c for c in protocol.SYNC_COLUMNS if c not in columns]
            assert not missing, f"{spec.name} lacks {missing}"
            assert set(spec.pk) <= columns, spec.name
        membership = set(protocol.table_columns(conn, protocol.MEMBERSHIP_TABLE))
        assert set(protocol.SYNC_COLUMNS) <= membership
    finally:
        conn.close()


def test_push_from_an_unregistered_machine_is_refused(
    hub: _TestClientTransport,
) -> None:
    """A pusher that skipped ``hello`` gets a 409, not an anonymous write."""
    with pytest.raises(client.SyncTransportError) as excinfo:
        hub.post(
            f"{client.API_PREFIX}/push",
            {
                "machine_id": "deadbeef" * 4,
                "schema_version": state_schema.SCHEMA_VERSION,
                "rows": [],
            },
        )
    assert "SYNC_UNKNOWN_MACHINE" in str(excinfo.value)


def test_a_peer_on_another_schema_version_is_refused(
    hub: _TestClientTransport,
) -> None:
    """Version skew fails at the handshake, not halfway through a row."""
    with pytest.raises(client.SyncTransportError) as excinfo:
        hub.post(
            f"{client.API_PREFIX}/hello",
            {
                "machine": {
                    "machine_id": "cafe" * 8,
                    "name": "future-spoke",
                    "platform": "linux",
                    "is_hub": False,
                    "data_root": "/tmp/future",
                    "first_seen": _T0,
                    "last_seen": _T0,
                },
                "schema_version": state_schema.SCHEMA_VERSION + 1,
            },
        )
    assert "SYNC_SCHEMA_VERSION" in str(excinfo.value)


def test_webui_app_exposes_the_sync_router() -> None:
    """The one-line wire-up in app.py actually mounts the endpoints."""
    app = create_app(mount_frontend=False, enable_cors=False)
    paths = {getattr(route, "path", "") for route in app.routes}
    assert f"{client.API_PREFIX}/hello" in paths
    assert f"{client.API_PREFIX}/push" in paths
    assert f"{client.API_PREFIX}/pull" in paths
    assert f"{client.API_PREFIX}/status" in paths
    assert f"{client.API_PREFIX}/digest" in paths
