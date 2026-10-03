"""Round 2 hardening: findings A3, 7b, A2, 6a and A1, as permanent tests.

Split out of :mod:`tests.cloudsync.test_hub_sync_hardening` (round 4
quality-gate ratchet: that file crossed 600 lines). Findings 1/7a, 3 and 2
stayed there; ``_insert_location`` is imported back from it rather than
duplicated, since it is the one helper this module still needs.

Every test here is one reproduction sketch from
``.planning/cloudsync-round1-adversarial.md`` turned into a regression, and
the fix it guards is specified in ``specs/design_decision_08.md``. The
mapping, so a failure names the defect it just let back in:

- A3              -> a hub restored from a point in time never got its rows
  back, because the spoke's floors survived the restore.
- finding 7b      -> ``/pull`` and ``/status`` served anyone.
- A2              -> ``/pull`` and ``/push`` were unpaginated.
- finding 6a      -> NFD and NFC spellings of one path diverged the digest.
- A1              -> no writer stamped ``updated_at``, so the second
  ``track_fields`` edit ever made bricked the machine.

Acceptance criteria, one test each:
- if a restored hub does not get its rows back, ADR 04 c3 recovery does not
  exist -- broken.
- if ``/pull`` or ``/status`` answers a machine that never said hello, the
  consistency guard is push-only -- broken.
- if a few hundred rows cross in one request, the wire is unpaginated and a
  first sync is tens of MB in one body -- broken.
- if two spellings of one path produce two digests, converged peers halt
  each other -- broken.
- if a second ``provenance.write_field`` edit does not converge, the writers
  are not stamping and ``track_fields`` sync is dead -- broken.
"""
from __future__ import annotations

import math
import unicodedata
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.shared.state import provenance
from apps.sync_hub import client, protocol, service
from tests.cloudsync.test_hub_sync import (
    _DEV_A,
    _T0,
    _T1,
    _T2,
    _insert_track,
    _open,
    _seed_common_track,
    _sync,
    _TestClientTransport,
    _track_title,
)
from tests.cloudsync.test_hub_sync_hardening import _insert_location

pytestmark = pytest.mark.requirement("CAT-04")

# One file, two correct spellings (finding 6a). macOS hands back the first
# (o + combining diaeresis), Windows and Linux the second (o-umlaut). Written
# as an escape so the difference survives any editor that helpfully
# normalizes this source file.
_PATH_RAW = "/Music/Sigr\u00fan Hlin - Morgunn.mp3"
_PATH_NFD = unicodedata.normalize("NFD", _PATH_RAW)
_PATH_NFC = unicodedata.normalize("NFC", _PATH_RAW)


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
def spoke_b(tmp_path: Path) -> Path:
    return tmp_path / "spoke-b"


@pytest.fixture
def hub(hub_dir: Path) -> Iterator[_TestClientTransport]:
    app = FastAPI()
    app.state.state_db_path = str(client.state_db_path(hub_dir))
    app.state.sync_hub_data_dir = str(hub_dir)
    app.state.sync_hub_machine_name = "hub"
    app.include_router(service.router, prefix="/api/v1")
    with TestClient(app) as http:
        yield _TestClientTransport(http)


# ----- A3: hub restore ------------------------------------------------------


def test_a_restored_hub_resets_the_floors_and_recovers(
    hub: _TestClientTransport, hub_dir: Path, spoke_a: Path
) -> None:
    """A3's sketch: the hub loses rows, the spoke must re-offer everything.

    Round 1 observed ``hub after re-sync: 1 track`` (only the row sitting on
    the inclusive floor) and a bricked spoke. ``hello`` reports the hub's max
    seq now, and a spoke whose pull floor is above it drops both floors.
    """
    conn_a = _open(spoke_a)
    try:
        _insert_track(conn_a, "trk-1", title="one", updated_at=_T0, origin=_DEV_A)
        _insert_track(conn_a, "trk-2", title="two", updated_at=_T1, origin=_DEV_A)
    finally:
        conn_a.close()
    first = _sync(spoke_a, hub, "spoke-a")
    assert first.accepted == 2
    assert first.hub_restore_detected is False

    hub_conn = _open(hub_dir)
    try:
        hub_conn.execute("DELETE FROM tracks")
        hub_conn.execute("DELETE FROM hub_changelog")
    finally:
        hub_conn.close()

    recovered = _sync(spoke_a, hub, "spoke-a")
    assert recovered.hub_restore_detected is True
    assert recovered.accepted == 2, "the spoke did not re-offer the whole library"

    hub_conn = _open(hub_dir)
    try:
        assert _track_title(hub_conn, "trk-1") == "one"
        assert _track_title(hub_conn, "trk-2") == "two"
    finally:
        hub_conn.close()


# ----- finding 7b: registration on every endpoint ---------------------------


def test_pull_refuses_a_machine_that_never_said_hello(
    hub: _TestClientTransport,
) -> None:
    """An unregistered ``/pull`` returned the whole changelog in round 1."""
    with pytest.raises(client.SyncTransportError) as excinfo:
        hub.get(
            f"{client.API_PREFIX}/pull",
            {"machine_id": "deadbeef" * 4, "since_seq": "0"},
        )
    assert "SYNC_UNKNOWN_MACHINE" in str(excinfo.value)


def test_status_refuses_a_machine_that_never_said_hello(
    hub: _TestClientTransport,
) -> None:
    """``/status`` leaks every machine's absolute ``data_root``."""
    with pytest.raises(client.SyncTransportError) as excinfo:
        hub.get(f"{client.API_PREFIX}/status", {"machine_id": "deadbeef" * 4})
    assert "SYNC_UNKNOWN_MACHINE" in str(excinfo.value)


def test_pull_without_a_machine_id_is_refused(hub: _TestClientTransport) -> None:
    """The guard cannot be skipped by omitting the parameter."""
    with pytest.raises(client.SyncTransportError) as excinfo:
        hub.get(f"{client.API_PREFIX}/pull", {"since_seq": "0"})
    assert "422" in str(excinfo.value)


# ----- A2: pagination -------------------------------------------------------


def test_a_few_hundred_rows_cross_in_several_chunks(
    hub: _TestClientTransport,
    spoke_a: Path,
    spoke_b: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both directions chunk, and the whole library still converges.

    ``PULL_LIMIT`` is lowered so the loop runs several times over a test-sized
    library; ``PUSH_BATCH_ROWS`` is left alone (1,000 since LIBM-120 L6 round
    6) and the library is sized one hundred rows past it, so the push splits
    on the real constant. Convergence is the real assertion -- a chunked
    transfer that dropped a chunk would still make several requests.
    """
    monkeypatch.setattr(client, "PULL_LIMIT", 100)
    total = client.PUSH_BATCH_ROWS + 100
    push_batches = math.ceil(total / client.PUSH_BATCH_ROWS)
    conn_a = _open(spoke_a)
    try:
        for index in range(total):
            _insert_track(
                conn_a,
                f"trk-{index:04d}",
                title=f"track {index}",
                updated_at=_T1,
                origin=_DEV_A,
            )
    finally:
        conn_a.close()

    pushed = _sync(spoke_a, hub, "spoke-a")
    assert pushed.pushed == total
    assert pushed.accepted == total
    assert pushed.push_requests == push_batches, (
        f"{total} rows at {client.PUSH_BATCH_ROWS}/request should be "
        f"{push_batches} requests, got {pushed.push_requests}"
    )
    assert pushed.pull_requests >= 3, "the pull did not loop"

    received = _sync(spoke_b, hub, "spoke-b")
    assert received.pull_requests >= 3
    assert received.applied == total

    conn_b = _open(spoke_b)
    try:
        count = conn_b.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
    finally:
        conn_b.close()
    assert count == total, "a chunk went missing"


# ----- finding 6a: Unicode ---------------------------------------------------


def test_two_spellings_of_one_path_hash_the_same(
    spoke_a: Path, spoke_b: Path
) -> None:
    """NFD on the Mac, NFC on Windows: one file, one digest.

    Round 1 observed ``hub track_locations: 2 rows for ONE file`` and two
    different digests, so a Mac and a Windows spoke could never agree.
    """
    assert _PATH_NFD != _PATH_NFC, "pick a path that actually differs by form"
    digests: list[str] = []
    for data_dir, path in ((spoke_a, _PATH_NFD), (spoke_b, _PATH_NFC)):
        conn = _open(data_dir)
        try:
            # Every column except the spelling is pinned identical across the
            # two DBs, so a difference in the digest can only be the path.
            conn.execute(
                "INSERT INTO machines(machine_id, name, platform, is_hub, "
                "data_root, first_seen, last_seen) "
                "VALUES ('fixed', 'fixed', 'macos', 0, NULL, ?, ?)",
                (_T0, _T0),
            )
            _insert_track(conn, "trk-1", title="t", updated_at=_T0, origin=_DEV_A)
            _insert_location(
                conn,
                location_id="e" * 32,
                stable_id="trk-1",
                machine_id="fixed",
                file_path=path,
                updated_at=_T1,
                origin=_DEV_A,
            )
            digests.append(protocol.table_digest(conn, "track_locations").hash)
        finally:
            conn.close()
    assert digests[0] == digests[1], "NFD and NFC spellings still diverge"


# ----- A1: the writers stamp -------------------------------------------------


def test_provenance_edits_converge_edit_sync_edit_sync(
    hub: _TestClientTransport, hub_dir: Path, spoke_a: Path, spoke_b: Path
) -> None:
    """A1's sketch, end to end: set a field, sync, set it again, sync.

    Round 1 observed ``second sync: BRICKED, SyncDigestMismatch, tables
    ['track_fields']`` and ``hub value: [('120', None, None)]`` -- the write
    left ``updated_at`` NULL, so the second edit presented the same LWW key
    and was rejected forever. The value must now move, and reach B.
    """
    _seed_common_track((spoke_a, spoke_b), "trk-1")

    conn_a = _open(spoke_a)
    try:
        provenance.write_field(
            conn_a,
            stable_id="trk-1",
            field_name="bpm",
            value=120.0,
            source="webui",
            modified_at=_T1,
        )
    finally:
        conn_a.close()
    _sync(spoke_a, hub, "spoke-a")

    conn_a = _open(spoke_a)
    try:
        provenance.write_field(
            conn_a,
            stable_id="trk-1",
            field_name="bpm",
            value=174.0,
            source="webui",
            modified_at=_T2,
        )
    finally:
        conn_a.close()
    second = _sync(spoke_a, hub, "spoke-a")
    assert second.accepted >= 1, "the second edit was rejected; A1 is back"

    _sync(spoke_b, hub, "spoke-b")
    for data_dir in (hub_dir, spoke_b):
        conn = _open(data_dir)
        try:
            row = conn.execute(
                "SELECT value_json, updated_at, origin_device_id FROM track_fields "
                "WHERE stable_id = 'trk-1' AND field_name = 'bpm'"
            ).fetchone()
            assert row is not None, data_dir.name
            assert row[0] == "174.0", data_dir.name
            assert row[1] is not None, f"{data_dir.name} stored a NULL updated_at"
            assert row[2] is not None, f"{data_dir.name} stored a NULL origin"
        finally:
            conn.close()
