"""A first sync must not pay a hub request per 200 rows (LIBM-120 L6).

At 10,000 tracks a first sync pushed about 20,000 rows as 100 requests of
200, and pulled them back as 40 pages of 500. Every push request opens a hub
connection and commits its own write transaction, and each commit rewrites
the same index and changelog pages: 2.5 s of hub ``COMMIT`` alone, measured
by py-spy. Every pull page builds its own walk state on the hub and its own
transaction on the spoke.

The instrument is the request count a real first sync reports
(``push_requests``, ``pull_requests``), at 300 and at 1,200 tracks: it may
grow by one request per 1,000 rows, not per 200 or 500.

[if] first-sync requests grow faster than one per 1,000 rows [then] per-request cost, [else stop].

Controls:
* a pull page of ``PULL_LIMIT`` tracks must never elect the whole library
  for identity verdicts: the page stays under the cutover, or every page
  pays a library scan (the round 2 finding, back through the page size);
* the probe for that must see the election when a page does cross it;
* ``PULL_LIMIT`` stays within what the hub serves (``MAX_PULL_LIMIT``);
* a push body of ``PUSH_BATCH_ROWS`` track rows 200 bytes wider than the
  10k fixture's (580 B each) stays under a 1 MiB proxy default.
"""

from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.sync_hub import client, engine, identity_verdicts, protocol, service
from apps.sync_hub.engine_identity_map import effective_identity_remap
from tests.cloudsync.enrollment_transport import TestClientTransport
from tests.cloudsync.test_hub_sync import _DEV_B, _T0, _T1
from tests.cloudsync.test_track_identity_collapse import _open_hub
from tests.cloudsync.test_track_identity_lookup_scale import _digest, _isrc, _seed_library, _values

pytestmark = pytest.mark.requirement("LIBM-120")

SMALL_LIBRARY = 300
LARGE_LIBRARY = 1_200
ROWS_PER_REQUEST = 1_000
PROXY_BODY_LIMIT_BYTES = 1_048_576
WIDER_TITLE = "w" * 200


# ----- fixtures -----------------------------------------------------------------


@pytest.fixture
def hub(tmp_path: Path) -> TestClientTransport:
    app = FastAPI()
    app.state.state_db_path = str(client.state_db_path(tmp_path / "hub"))
    app.state.sync_hub_data_dir = str(tmp_path / "hub")
    app.state.sync_hub_machine_name = "hub"
    app.include_router(service.router, prefix="/api/v1")
    with TestClient(app) as http:
        yield TestClientTransport(http)


def _first_syncs(hub: TestClientTransport, tmp_path: Path, tracks: int) -> tuple[int, int, int]:
    """(rows pushed, push requests, pull requests of a second spoke's first sync)."""
    writer = tmp_path / f"writer-{tracks}"
    conn = state_db.open_rw(client.state_db_path(writer))
    try:
        _seed_library(conn, tracks)
    finally:
        conn.close()
    pushed = client.run_sync(writer, "http://hub.invalid", transport=hub, name=f"writer-{tracks}")
    reader = client.run_sync(
        tmp_path / f"reader-{tracks}", "http://hub.invalid", transport=hub, name=f"reader-{tracks}"
    )
    assert pushed.accepted == tracks and reader.applied >= tracks
    return pushed.pushed, pushed.push_requests, reader.pull_requests


class _Elections:
    """Counts library-wide identity elections in one walk."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.count = 0

        def counted(conn: sqlite3.Connection) -> dict[str, str]:
            self.count += 1
            return effective_identity_remap(conn)

        monkeypatch.setattr(identity_verdicts, "effective_identity_remap", counted)


def _new_tracks(conn: sqlite3.Connection, count: int) -> list[protocol.RowChange]:
    """``count`` tracks sharing no identity with the library, each its own component."""
    return [
        protocol.RowChange(
            table="tracks",
            pk=(f"page-{i}",),
            values=_values(
                conn,
                "tracks",
                stable_id=f"page-{i}",
                stable_id_tier="fingerprint",
                title=f"page {i}",
                isrc=_isrc("PGE", i),
                content_hash=_digest("page-c", i),
                audio_hash=_digest("page-a", i),
                created_at=_T0,
                updated_at=_T1,
                origin_device_id=_DEV_B,
                deleted_at=None,
            ),
        )
        for i in range(count)
    ]


def _elections_serving_one_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, limit: int
) -> tuple[int, int]:
    """(rows on the page, library elections) for one hub pull page of ``limit`` new tracks."""
    conn = _open_hub(tmp_path / f"page-{limit}")
    try:
        _seed_library(conn, limit)
        since = engine.current_seq(conn)
        engine.hub_apply(conn, _new_tracks(conn, limit))
        conn.commit()
        elections = _Elections(monkeypatch)
        page = engine.hub_changes_since(conn, since, limit=limit)
        return len(page.rows), elections.count
    finally:
        conn.close()


# ----- the finding --------------------------------------------------------------


@pytest.mark.parametrize("tracks", [SMALL_LIBRARY, LARGE_LIBRARY])
def test_first_sync_requests_grow_by_one_per_thousand_rows(
    hub: TestClientTransport, tmp_path: Path, tracks: int
) -> None:
    print("if a first sync makes a hub request per 200 pushed rows or per 500 pulled, then broken")
    rows, push_requests, pull_requests = _first_syncs(hub, tmp_path, tracks)
    allowed = math.ceil(rows / ROWS_PER_REQUEST)
    assert push_requests <= allowed, (
        f"{rows} rows took {push_requests} push requests; at most {allowed} "
        f"({ROWS_PER_REQUEST} rows per request, LIBM-120 L6)"
    )
    assert pull_requests <= allowed, (
        f"{rows} rows took {pull_requests} pull pages; at most {allowed} (LIBM-120 L6)"
    )


def test_the_pull_limit_is_one_the_hub_serves() -> None:
    assert 0 < client.PULL_LIMIT <= service.MAX_PULL_LIMIT


def test_a_full_push_body_stays_under_a_proxy_default(tmp_path: Path) -> None:
    print("if one push request of wide track rows exceeds a 1 MiB proxy body limit, then broken")
    conn = _open_hub(tmp_path / "body")
    try:
        rows = [
            protocol.RowChange(
                table=change.table,
                pk=change.pk,
                values={**change.values, "title": f"{WIDER_TITLE}{index}"},
            )
            for index, change in enumerate(_new_tracks(conn, client.PUSH_BATCH_ROWS))
        ]
    finally:
        conn.close()
    body = json.dumps(
        {"rows": [row.to_wire() for row in rows], "machine_id": "m" * 32},
        separators=(",", ":"),
    ).encode()
    assert len(body) < PROXY_BODY_LIMIT_BYTES, (
        f"{client.PUSH_BATCH_ROWS} rows make a {len(body):,}-byte push body, over "
        f"{PROXY_BODY_LIMIT_BYTES:,}: a proxy with a default body limit refuses every push"
    )


# ----- overshoot controls -------------------------------------------------------


def test_a_full_pull_page_never_elects_the_library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    print("if a full pull page crosses the identity cutover and scans the library, then broken")
    rows, elections = _elections_serving_one_page(tmp_path, monkeypatch, client.PULL_LIMIT)
    assert rows == client.PULL_LIMIT, "the page must be full, or it tests a smaller one"
    assert elections == 0, (
        f"one pull page of {rows} tracks elected the whole library {elections} time(s): "
        f"PULL_LIMIT {client.PULL_LIMIT} crosses ELECT_LIBRARY_AFTER_VERDICTS "
        f"{identity_verdicts.CFG.ELECT_LIBRARY_AFTER_VERDICTS} (LIBM-120 L6 round 2)"
    )


def test_probe_sees_the_election_when_a_page_crosses_the_cutover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative control: a page as big as the cutover must elect, or the probe is blind."""
    limit = identity_verdicts.CFG.ELECT_LIBRARY_AFTER_VERDICTS + 1
    rows, elections = _elections_serving_one_page(tmp_path, monkeypatch, limit)
    assert rows == limit
    assert elections >= 1
