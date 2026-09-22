"""Row-level digest mismatch diagnosis (CSSTATUS-09)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.sync_hub import client, digest_diff, service
from apps.sync_hub.client_transport_ops import state_db_path
from tests.cloudsync.enrollment_transport import TestClientTransport
from tests.cloudsync.test_hub_sync import (
    _open,
    _seed_common_track,
    _sync,
)

_TestClientTransport = TestClientTransport


@pytest.fixture
def hub_dir(tmp_path: Path) -> Path:
    return tmp_path / "hub"


@pytest.fixture
def spoke_a(tmp_path: Path) -> Path:
    return tmp_path / "spoke-a"


@pytest.fixture
def hub(hub_dir: Path) -> Iterator[_TestClientTransport]:
    app = FastAPI()
    app.state.state_db_path = str(client.state_db_path(hub_dir))
    app.state.sync_hub_data_dir = str(hub_dir)
    app.state.sync_hub_machine_name = "hub"
    app.include_router(service.router, prefix="/api/v1")
    with TestClient(app) as http:
        yield _TestClientTransport(http)


def test_format_mismatch_message_includes_row_sample() -> None:
    row = digest_diff.DigestDiffRow(
        table="tracks",
        pk=("trk-1",),
        local_stamp="2026-01-01T00:00:00+00:00@dev-a",
        hub_stamp="2026-01-02T00:00:00+00:00@hub",
        newer_side="hub",
        reason="canonical bytes differ",
    )
    message = digest_diff.format_mismatch_message(
        hub_machine_id="hub-1",
        rounds=1,
        divergent=["tracks"],
        local_overall="aaa",
        remote_overall="bbb",
        diffs=[row],
    )
    assert "tracks/trk-1" in message
    assert "hub newer" in message
    assert digest_diff.parse_digest_samples(message) is not None


@pytest.mark.requirement("CSSTATUS-09")
def test_digest_mismatch_names_row_and_newer_side(
    hub: _TestClientTransport, spoke_a: Path
) -> None:
    """[if] digests diverge after sync [then] the error names a row and newer side, [else stop]."""
    _seed_common_track((spoke_a,), "trk-1")
    _sync(spoke_a, hub, "spoke-a")

    conn_a = _open(spoke_a)
    try:
        conn_a.execute(
            "UPDATE tracks SET title = ? WHERE stable_id = ?",
            ("silently rewritten", "trk-1"),
        )
    finally:
        conn_a.close()

    with pytest.raises(client.SyncDigestMismatch) as excinfo:
        _sync(spoke_a, hub, "spoke-a")
    message = str(excinfo.value)
    assert "tracks" in message
    assert excinfo.value.diff
    assert excinfo.value.diff[0].stable_id.startswith("tracks/")


def test_rows_endpoint_lists_sync_eligible_rows(
    hub: _TestClientTransport, spoke_a: Path, hub_dir: Path
) -> None:
    _seed_common_track((spoke_a,), "trk-1")
    _sync(spoke_a, hub, "spoke-a")
    conn = state_db.open_ro(state_db_path(spoke_a))
    try:
        machine_id = conn.execute("SELECT machine_id FROM machines LIMIT 1").fetchone()[0]
    finally:
        conn.close()
    payload = hub.get(
        "/api/v1/sync/rows",
        {
            "machine_id": machine_id,
            "table": "tracks",
            "limit": "10",
            "capabilities": "quarantine/v1",
        },
    )
    assert payload["rows"]
    assert payload["rows"][0]["pk"] == ["trk-1"]
