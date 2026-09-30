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
  pays a library scan (the round 2 finding, back through the page size).
  The election is observed from outside, as the one statement it runs (the
  bare live-tracks identity select) on the hub connection's SQLite trace;
* the probe for that must see the election when a page does cross it;
* ``PULL_LIMIT`` stays within what the hub serves (``MAX_PULL_LIMIT``);
* a push of WIDE track rows (long ``title``, ``artists_json``, ``file_path``)
  closes each request at ``PUSH_BODY_MAX_BYTES``, under a 1 MiB proxy
  default, rather than at the row cap alone;
* a proxy that still refuses a body as too large (413) gets it again in
  halves, and one row it refuses alone fails loud, naming the row.

The proxy is :class:`_ProxiedHub`: the real hub router behind a body limit,
weighing each POST exactly as :class:`apps.sync_hub.transport.HttpTransport`
encodes it. It fakes no hub answer; a body over its limit never reaches the
hub, which is what a reverse proxy's 413 means.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.sync_hub import client, engine, identity_verdicts, protocol, service, transport
from apps.sync_hub.sync_set import identity_row_select
from tests.cloudsync.enrollment_transport import TestClientTransport
from tests.cloudsync.test_hub_sync import _DEV_B, _T0, _T1
from tests.cloudsync.test_track_identity_collapse import _open_hub
from tests.cloudsync.test_track_identity_lookup_scale import _digest, _isrc, _seed_library, _values

pytestmark = pytest.mark.requirement("LIBM-120")

SMALL_LIBRARY = 300
LARGE_LIBRARY = 1_200
ROWS_PER_REQUEST = 1_000
PROXY_BODY_LIMIT_BYTES = 1_048_576
#: A proxy tighter than the client's own bound, so the 413 split must run.
TIGHT_PROXY_LIMIT_BYTES = 256 * 1024
#: Characters added to each of ``title``, ``artists_json`` and ``file_path``:
#: about 1.5 KB a row, so ``PUSH_BATCH_ROWS`` of them is well over 1 MiB.
WIDE_TEXT_CHARS = 500


# ----- fixtures -----------------------------------------------------------------


def _hub_app(hub_dir: Path) -> FastAPI:
    app = FastAPI()
    app.state.state_db_path = str(client.state_db_path(hub_dir))
    app.state.sync_hub_data_dir = str(hub_dir)
    app.state.sync_hub_machine_name = "hub"
    app.include_router(service.router, prefix="/api/v1")
    return app


@pytest.fixture
def hub(tmp_path: Path) -> Iterator[TestClientTransport]:
    with TestClient(_hub_app(tmp_path / "hub")) as http:
        yield TestClientTransport(http)


class _ProxiedHub(TestClientTransport):
    """The real hub router behind a reverse proxy with a request body limit.

    Every POST is encoded as :class:`apps.sync_hub.transport.HttpTransport`
    encodes it and weighed; one over ``body_limit`` is answered 413 without
    reaching the hub, as nginx's ``client_max_body_size`` does. The rest go
    to the real router unchanged.
    """

    __test__ = False

    def __init__(self, http: TestClient, *, body_limit: int | None) -> None:
        super().__init__(http)
        self._body_limit = body_limit
        #: Bytes of every push body the hub received, in order.
        self.delivered_push_bytes: list[int] = []
        #: Bytes of every push body the proxy refused, in order.
        self.refused_push_bytes: list[int] = []

    def post(self, path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        body = json.dumps(dict(payload)).encode("utf-8")
        is_push = path.endswith("/push")
        if self._body_limit is not None and len(body) > self._body_limit:
            if is_push:
                self.refused_push_bytes.append(len(body))
            raise transport.refused(
                f"POST {path}", 413, '{"detail":"request entity too large"}'
            )
        if is_push:
            self.delivered_push_bytes.append(len(body))
        response = self._http.post(
            path, content=body, headers={"content-type": "application/json"}
        )
        return self._decoded(response, f"POST {path}")


def _widen(data_dir: Path, tracks: int, *, extra_chars: int) -> None:
    """A spoke library of ``tracks`` whose text columns are ``extra_chars`` longer."""
    conn = state_db.open_rw(client.state_db_path(data_dir))
    try:
        _seed_library(conn, tracks)
        conn.execute(
            "UPDATE tracks SET title = title || ?, artists_json = ?, file_path = ?",
            (
                "t" * extra_chars,
                json.dumps(["a" * extra_chars]),
                "/Music/" + "p" * extra_chars + ".mp3",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _sync_through(proxy: _ProxiedHub, data_dir: Path) -> client.SyncResult:
    return client.run_sync(data_dir, "http://hub.invalid", transport=proxy, name=data_dir.name)


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


def _library_elections(conn: sqlite3.Connection) -> list[str]:
    """Record, from now on, every whole-library identity election on ``conn``.

    An election reads every live track with the bare identity select
    (:func:`apps.sync_hub.sync_set.identity_duplicate_remap`); a per-row
    verdict runs the same select with a ``rowid`` column and a filter, so
    only the election matches it exactly.
    """
    election = identity_row_select(conn)
    seen: list[str] = []

    def trace(statement: str) -> None:
        if statement.strip() == election:
            seen.append(statement)

    conn.set_trace_callback(trace)
    return seen


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


def _elections_serving_one_page(tmp_path: Path, limit: int) -> tuple[int, int]:
    """(rows on the page, library elections) for one hub pull page of ``limit`` new tracks."""
    conn = _open_hub(tmp_path / f"page-{limit}")
    try:
        _seed_library(conn, limit)
        since = engine.current_seq(conn)
        engine.hub_apply(conn, _new_tracks(conn, limit))
        conn.commit()
        elections = _library_elections(conn)
        page = engine.hub_changes_since(conn, since, limit=limit)
        conn.set_trace_callback(None)
        return len(page.rows), len(elections)
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


@pytest.mark.requirement("CLOUDSYNC-27")
def test_wide_rows_close_each_push_at_the_body_bound(tmp_path: Path) -> None:
    print("if one push request of wide track rows exceeds PUSH_BODY_MAX_BYTES, then broken")
    assert client.PUSH_BODY_MAX_BYTES < PROXY_BODY_LIMIT_BYTES
    spoke = tmp_path / "wide"
    _widen(spoke, client.PUSH_BATCH_ROWS, extra_chars=WIDE_TEXT_CHARS)
    with TestClient(_hub_app(tmp_path / "hub")) as http:
        proxy = _ProxiedHub(http, body_limit=None)
        result = _sync_through(proxy, spoke)
    assert result.accepted == result.pushed >= client.PUSH_BATCH_ROWS, (
        "nothing to weigh: the push did not deliver the library"
    )
    by_rows = math.ceil(result.pushed / client.PUSH_BATCH_ROWS)
    assert result.push_requests > by_rows, (
        f"{result.pushed} wide rows went in {result.push_requests} request(s), "
        f"no more than the row cap alone makes ({by_rows}): no batch closed on bytes"
    )
    widest = max(proxy.delivered_push_bytes)
    assert widest <= client.PUSH_BODY_MAX_BYTES, (
        f"a push body of {widest:,} bytes is over PUSH_BODY_MAX_BYTES "
        f"{client.PUSH_BODY_MAX_BYTES:,}: a proxy with a 1 MiB default refuses it"
    )


@pytest.mark.requirement("CLOUDSYNC-27")
def test_a_push_the_proxy_refuses_as_too_large_is_resent_in_halves(tmp_path: Path) -> None:
    print("if a push refused with HTTP 413 is not split and re-sent, then broken")
    assert TIGHT_PROXY_LIMIT_BYTES < client.PUSH_BODY_MAX_BYTES
    spoke = tmp_path / "wide"
    _widen(spoke, client.PUSH_BATCH_ROWS, extra_chars=WIDE_TEXT_CHARS)
    with TestClient(_hub_app(tmp_path / "hub")) as http:
        proxy = _ProxiedHub(http, body_limit=TIGHT_PROXY_LIMIT_BYTES)
        result = _sync_through(proxy, spoke)
    assert proxy.refused_push_bytes, "the proxy refused nothing, so no split was tested"
    assert result.accepted == result.pushed >= client.PUSH_BATCH_ROWS, (
        f"{result.pushed} rows offered, {result.accepted} accepted behind a "
        f"{TIGHT_PROXY_LIMIT_BYTES:,}-byte proxy"
    )
    assert max(proxy.delivered_push_bytes) <= TIGHT_PROXY_LIMIT_BYTES


@pytest.mark.requirement("CLOUDSYNC-27")
def test_one_row_the_proxy_refuses_alone_fails_loud(tmp_path: Path) -> None:
    print("if a single row over the proxy limit fails without naming the row, then broken")
    spoke = tmp_path / "one-huge-row"
    _widen(spoke, 1, extra_chars=TIGHT_PROXY_LIMIT_BYTES)
    with TestClient(_hub_app(tmp_path / "hub")) as http:
        proxy = _ProxiedHub(http, body_limit=TIGHT_PROXY_LIMIT_BYTES)
        with pytest.raises(client.SyncTransportError) as excinfo:
            _sync_through(proxy, spoke)
    assert excinfo.value.status_code == 413
    assert "ONE tracks row ['stored-0']" in str(excinfo.value), str(excinfo.value)
    assert proxy.refused_push_bytes and not proxy.delivered_push_bytes


# ----- overshoot controls -------------------------------------------------------


def test_a_full_pull_page_never_elects_the_library(tmp_path: Path) -> None:
    print("if a full pull page crosses the identity cutover and scans the library, then broken")
    rows, elections = _elections_serving_one_page(tmp_path, client.PULL_LIMIT)
    assert rows == client.PULL_LIMIT, "the page must be full, or it tests a smaller one"
    assert elections == 0, (
        f"one pull page of {rows} tracks elected the whole library {elections} time(s): "
        f"PULL_LIMIT {client.PULL_LIMIT} crosses ELECT_LIBRARY_AFTER_VERDICTS "
        f"{identity_verdicts.CFG.ELECT_LIBRARY_AFTER_VERDICTS} (LIBM-120 L6 round 2)"
    )


def test_probe_sees_the_election_when_a_page_crosses_the_cutover(tmp_path: Path) -> None:
    """Negative control: a page as big as the cutover must elect, or the probe is blind."""
    limit = identity_verdicts.CFG.ELECT_LIBRARY_AFTER_VERDICTS + 1
    rows, elections = _elections_serving_one_page(tmp_path, limit)
    assert rows == limit
    assert elections >= 1
