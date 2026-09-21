"""Shared fixtures for the CLOUDSYNC suite: synthetic tier and real tier.

The ``live_r2`` marker is registered in ``pyproject.toml`` alongside the
other repo markers (``--strict-markers`` is on, so it has to be somewhere).
This file holds only fixtures, so a subset run of ``tests/cloudsync`` gets
the same migrated DB and the same dict-backed S3 as the full suite.

Two tiers live here and they answer different questions:

* the SYNTHETIC tier (``conn``, ``fake_s3``, ``cfg``) seeds a handful of rows
  it fully controls, which is what makes an assertion about one merge rule
  readable;
* the REAL tier (``real_library_db``, ``real_library_hub_and_spokes``) builds
  a fleet from a read-only snapshot of a supplied real library. This tier
  exercises the supplied writer history, schema migration and playlists;
  historical private inputs are not included with this source.

The real tier SKIPS where the library is absent (CI, cloud sessions) rather
than fabricating one, mirroring ``tests/webui/test_settings.py``. Its
machinery is :mod:`tests.cloudsync.real_library`; only the guarantees are
stated here.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import socket
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.cloud.asset_store import AssetBody, AssetHead
from apps.cloud.config import CloudConfig
from apps.shared.state import db as state_db
from apps.shared.state import schema as state_schema
from apps.shared.state import sync_stamp
from apps.sync_hub import client as sync_client
from apps.sync_hub import maintenance_enroll, quarantine_log
from apps.sync_hub import service as sync_service

# The hub/spoke fixtures live in ``test_hub_sync`` next door, and a conftest is
# where a fixture shared across a directory belongs -- so re-export them here
# rather than have each module import them. The import shape matters: a module
# that imports a fixture AND takes it as a test parameter shadows the imported
# name, which reads as dead code plus a redefinition to a linter. That is what
# happened in 17c7e99da, which deleted exactly these four names from
# ``test_legacy_track_fields`` as unused imports and left its three tests
# erroring at collection with "fixture 'hub' not found". Nothing in this file
# takes them as a parameter, so only the one unused-import marker is needed and
# the deletion cannot recur. A module defining its own ``hub``/``hub_dir``
# still wins: a module-level fixture takes precedence over a conftest one.
from tests.cloudsync.test_hub_sync import hub, hub_dir, spoke_a, spoke_b  # noqa: F401
from tests.waits import start_uvicorn_in_thread

from .enrollment_transport import TestClientTransport
from .real_library import (
    DEFAULT_SUBSET_TRACKS,
    PreparedLibrary,
    RealLibraryFleet,
    fleet_from,
    skip_unless_real_library,
    source_state_db,
)
from .real_library_premise import prepared_or_skip

# SEC-01 (issue #2689): the TestClient host-allowlist default lives once in
# the root tests/conftest.py (it applies fleet-wide, not just to this
# suite) -- see tests/testclient_host_allowlist.py for why.

#: Env vars that must ALL be set for the live R2 round-trip to run.
LIVE_R2_ENV_VARS: tuple[str, ...] = (
    "R2_ACCOUNT_ID",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
)

#: Captured private timestamp-input bindings LEGACY_STAMPS and
#: CANONICAL_STAMPS are withheld with their exact consumer family in the
#: public derivative. No replacement input or baseline is supplied.


@pytest.fixture(autouse=True)
def _nothing_reported_yet() -> None:
    """Every test starts with no quarantine or inconclusive-digest report made.

    Both are reported at ERROR once per process (``apps.sync_hub.
    quarantine_log``, ``client._report_inconclusive``), so without this an
    earlier test's byte-identical fixture rows would silence the ERROR a later
    test asserts on, and which test failed would depend on collection order.
    """
    quarantine_log.reset_for_tests()
    sync_client._reported_inconclusive.clear()


def live_r2_config() -> CloudConfig | None:
    """CloudConfig from the ambient env, or ``None`` when creds are absent.

    Returning ``None`` rather than raising is what lets the live test SKIP on
    a laptop with no Doppler session while still failing loudly the moment
    credentials exist and the round-trip breaks.
    """
    if any(not os.environ.get(name) for name in LIVE_R2_ENV_VARS):
        return None
    return CloudConfig.from_env()


class InMemoryAssetS3:
    """Dict-backed implementation of the narrow asset S3 protocol.

    Same sanctioned pattern as :class:`apps.cloud.lock.FakeS3Client`: a real
    object store's semantics, no network. ``etag`` is ``sha1(body)`` so a
    conditional create is predictable.

    ``fail_puts`` exists for exactly one assertion -- that push-then-delete
    never deletes a local file whose upload did not land.
    """

    def __init__(self, *, fail_puts: bool = False) -> None:
        self.store: dict[tuple[str, str], tuple[bytes, str]] = {}
        self.fail_puts = fail_puts
        self.head_calls: list[tuple[str, str]] = []
        self.put_calls: list[tuple[str, str]] = []

    @staticmethod
    def _etag(body: bytes) -> str:
        return '"' + hashlib.sha1(body).hexdigest() + '"'

    def head_object(self, bucket: str, key: str) -> AssetHead | None:
        self.head_calls.append((bucket, key))
        got = self.store.get((bucket, key))
        if got is None:
            return None
        return AssetHead(size=len(got[0]), etag=got[1])

    def get_object(self, bucket: str, key: str) -> tuple[bytes, str] | None:
        got = self.store.get((bucket, key))
        return (got[0], got[1]) if got else None

    def put_object_if_none_match(
        self, bucket: str, key: str, body: AssetBody
    ) -> tuple[bool, str | None]:
        self.put_calls.append((bucket, key))
        if self.fail_puts:
            return False, None
        if (bucket, key) in self.store:
            return False, None
        if not isinstance(body, bytes):
            chunks: list[bytes] = []
            while chunk := body.read(1 << 16):
                chunks.append(chunk)
            body = b"".join(chunks)
        etag = self._etag(body)
        self.store[(bucket, key)] = (body, etag)
        return True, etag

    def delete_object(self, bucket: str, key: str) -> bool:
        return self.store.pop((bucket, key), None) is not None


@pytest.fixture
def fake_s3() -> InMemoryAssetS3:
    """An empty store. Guarantees: no key exists until a test puts it, every
    put is recorded in ``put_calls``, and puts succeed (``fail_puts`` is off)."""
    return InMemoryAssetS3()


@pytest.fixture
def cfg() -> CloudConfig:
    """A fully populated config. Credential-absence is tested explicitly.

    Guarantees every field the asset tier reads is non-empty, so a test that
    fails does so for the reason it names and not because a credential was
    missing. ``bind_host`` is loopback: nothing in this suite listens.
    """
    return CloudConfig(
        r2_account_id="acct1234",
        r2_access_key_id="AKIDEXAMPLE",
        r2_secret_access_key="secret-key",
        state_bucket="test-state",
        audio_bucket="test-audio",
        hostname="test-host",
        bind_host="127.0.0.1",
    )


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """An EMPTY state DB at the current ``SCHEMA_VERSION``, FKs enforced.

    Guarantees: every sync-set table exists and holds zero rows, and the
    REFERENCES clauses are live, so a fixture that inserts a child row
    without its parent fails here rather than in the assertion. It carries no
    machine identity and no ``local_changelog`` entries -- a test that needs
    either must create them the way a writer does
    (``apps.shared.state.sync_stamp.stamp_and_log``).
    """
    connection = sqlite3.connect(tmp_path / "state.db")
    connection.execute("PRAGMA foreign_keys = ON")
    state_schema.apply_migrations(connection)
    try:
        yield connection
    finally:
        connection.close()


# ----- real-library tier ------------------------------------------------------


@pytest.fixture(scope="session")
def real_library_db(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[PreparedLibrary]:
    """A private, migrated, stamp-repaired copy of a supplied real library.

    Guarantees (all asserted in ``real_library.prepare_library``, not assumed):
    the source file is opened read-only and copied before anything touches it;
    the copy is at ``state_schema.SCHEMA_VERSION`` having climbed the real
    migration ladder from the library's own v5; and every stored timestamp in
    it is orderable, with ``.repairs`` recording what
    ``normalize_stamps.scan`` had to fix to get there.

    SKIPS, loudly and with the path, where the library is absent or this
    process cannot stat it -- CI and cloud sessions have no real library
    and this tier must never fabricate one. Also SKIPS when the DEFAULT
    source carries no legacy stamp (the packaged library usually does
    not); a source named in MDT_REAL_LIBRARY_STATE_DB fails loud instead,
    via ``real_library_premise.prepared_or_skip``. Session-scoped because the
    copy plus migration costs a few seconds and nothing writes to it
    afterwards, and the copy is DELETED at the end of the session rather
    than left to pytest's keep-the-last-three temp-root policy, which
    would retain copied state databases on disk.
    """
    source = source_state_db()
    skip_unless_real_library(source)
    root = tmp_path_factory.mktemp("real-library")
    try:
        yield prepared_or_skip(source, root, repaired=True)
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def real_library_subset_tracks(request: pytest.FixtureRequest) -> int:
    """How many real tracks each spoke is seeded with.

    Overridable per test with
    ``@pytest.mark.parametrize("real_library_subset_tracks", [n], indirect=True)``
    so a test that needs a smaller or larger library does not need its own
    fixture.
    """
    return int(getattr(request, "param", DEFAULT_SUBSET_TRACKS))


@pytest.fixture
def real_library_hub_and_spokes(
    real_library_db: PreparedLibrary,
    real_library_subset_tracks: int,
    tmp_path: Path,
) -> Iterator[RealLibraryFleet]:
    """An EMPTY hub plus two spokes holding the same real library subset.

    Guarantees:

    * the hub's DB exists, is at the current schema version and holds ZERO
      synced rows: bootstrapping the fleet is what the first sync does.
    * both spokes hold the same deterministic slice (``ORDER BY stable_id
      LIMIT n``), so a failure reproduces from the subset size alone, and
      their domain rows are byte-identical because both came from one
      library.
    * their ``track_locations`` deliberately differ: each row carries its own
      machine's ``machine_id`` and a locally minted ``location_id``, which is
      what two real machines holding one library look like (ADR 08 point 1).
    * neither spoke has synced, so ``fleet.transport`` counters start at zero
      and the first ``run_sync`` a test makes is a true FIRST sync.
    """
    with fleet_from(
        real_library_db.path, tmp_path, real_library_subset_tracks
    ) as fleet:
        yield fleet


@pytest.fixture(scope="session")
def real_library_db_unrepaired(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[PreparedLibrary]:
    """The same library, migrated to v7 and DELIBERATELY NOT stamp-repaired.

    Guarantees: ``.repairs`` is NON-EMPTY (asserted, not assumed -- see
    ``real_library._require_legacy_stamps``) and every one of those values is
    still stored. This is the only fixture in the suite that can put a real
    legacy stamp in a LOCAL table, which is what the quarantine tier needs
    and what no synthetic fixture could provide: they all mint canonical
    stamps, and ``real_library_db`` repairs before handing anything out.

    SKIPS where the library is absent or unreadable, like its repaired twin.
    """
    source = source_state_db()
    skip_unless_real_library(source)
    root = tmp_path_factory.mktemp("real-library-unrepaired")
    try:
        yield prepared_or_skip(source, root, repaired=False)
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def real_library_hub_and_spokes_unrepaired(
    real_library_db_unrepaired: PreparedLibrary,
    real_library_subset_tracks: int,
    tmp_path: Path,
) -> Iterator[RealLibraryFleet]:
    """:func:`real_library_hub_and_spokes` over the UNREPAIRED library.

    Same guarantees, one difference that is the whole point: the spokes carry
    the library's own unorderable stamps, so a sync here exercises the
    quarantine path against production-shaped data rather than a stamp a
    fixture typed in.
    """
    with fleet_from(
        real_library_db_unrepaired.path, tmp_path, real_library_subset_tracks
    ) as fleet:
        yield fleet


#: Requesting any of these puts a test in the ``real_library`` tier.
REAL_LIBRARY_FIXTURES: frozenset[str] = frozenset(
    {
        "real_library_db",
        "real_library_db_unrepaired",
        "real_library_hub_and_spokes",
        "real_library_hub_and_spokes_unrepaired",
    }
)


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark every test that needs the real library ``real_library``.

    Derived from the fixtures a test requests rather than hand-applied, so a
    new real-library test cannot land outside the tier: ``just cloudsync-fast``
    deselects the marker and ``just cloudsync-slow`` selects it. ``tryfirst``
    because ``-m`` deselection runs in this same hook and must see the marker.
    """
    for item in items:
        if REAL_LIBRARY_FIXTURES.intersection(getattr(item, "fixturenames", ())):
            item.add_marker(pytest.mark.real_library)


# ----- enrollment tier (ADR 12) --------------------------------------------
#
# A third tier alongside the synthetic and real ones above, answering a
# different question again: who is allowed to JOIN this fleet. It lives here
# rather than in one test module because two modules need it -- the mechanism
# tests and the CLI/parity tests -- and two copies of a hub fixture is how the
# two halves of one contract start disagreeing about what a hub is.
#
# Every name is prefixed ``enroll_``. ``hub``, ``hub_dir`` and ``spoke_dir``
# are all already defined by individual modules in this directory, and a
# conftest fixture of the same name would be SHADOWED there but VISIBLE
# everywhere else -- so a module that forgot to define its own would silently
# get this one instead of failing. A distinct prefix cannot do that.

ENROLL_OWNER_SUB: str = "google-sub-maintainer"
ENROLL_OWNER_EMAIL: str = "maintainer@example.com"
ENROLL_OTHER_SUB: str = "google-sub-someone-else"
ENROLL_OTHER_EMAIL: str = "someone@example.com"

ENROLL_PATH: str = "/api/v1/sync/enroll"
HELLO_PATH: str = "/api/v1/sync/hello"


def seed_user(db_path: Path, *, sub: str, email: str) -> None:
    """Put a real ``users`` row in place. Grants reference it by FK."""
    conn = state_db.open_rw(db_path)
    try:
        stamp = sync_stamp.canonical_now()
        conn.execute(
            "INSERT INTO users(google_sub, email, name, avatar_url, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (sub, email, None, None, stamp, stamp),
        )
    finally:
        conn.close()


@pytest.fixture
def clean_machine_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``MDT_IS_HUB`` unset, so ``is_hub`` is a fact and not an inherited leak.

    Not autouse: requested by the enrollment fixtures below rather than
    imposed on every test in this directory.
    """
    monkeypatch.delenv("MDT_IS_HUB", raising=False)


@pytest.fixture
def enroll_hub_dir(tmp_path: Path, clean_machine_env: None) -> Path:
    """A hub data dir: migrated DB, one signed-in user, no machines yet."""
    path = tmp_path / "hub"
    path.mkdir(parents=True)
    db_path = sync_client.state_db_path(path)
    state_db.open_rw(db_path).close()
    seed_user(db_path, sub=ENROLL_OWNER_SUB, email=ENROLL_OWNER_EMAIL)
    return path


def _enroll_hub_app(enroll_hub_dir: Path) -> FastAPI:
    """The hub app both enrollment hub fixtures serve.

    One builder, deliberately: the socket-less fixture and the live one below
    have to be the SAME hub for the live test to say anything about the other
    tests. Two inline copies would let them drift into two different hubs
    while both still passed.
    """
    app = FastAPI()
    app.state.state_db_path = str(sync_client.state_db_path(enroll_hub_dir))
    app.state.sync_hub_data_dir = str(enroll_hub_dir)
    app.state.sync_hub_machine_name = "enroll-hub"
    app.include_router(sync_service.router, prefix="/api/v1")
    return app


@pytest.fixture
def enroll_hub(enroll_hub_dir: Path) -> Iterator[TestClientTransport]:
    """The real sync router over the real ASGI stack. Only the socket is absent."""
    with TestClient(_enroll_hub_app(enroll_hub_dir)) as http:
        yield TestClientTransport(http)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture
def enroll_live_hub(enroll_hub_dir: Path) -> Iterator[str]:
    """The same hub behind uvicorn on the loopback, and its real URL.

    Codex review, PR #1648, P1 BLOCKING: every other enrollment CLI test
    monkeypatches ``maintenance_enroll._transport_for``, so none of them ever
    executes ``HttpTransport``, opens a socket, or exercises real HTTP
    serialization -- and the PR claimed a DEV path proved end to end. A test
    using this fixture must NOT patch the transport factory: the point is
    that the CLI builds its own production transport and talks real bytes.

    Same shape as ``live_hub`` in ``test_hub_sync_round4_restore``, which
    made exactly this move for the ``sync`` CLI in round 7.
    """
    port = free_port()
    config = uvicorn.Config(
        _enroll_hub_app(enroll_hub_dir),
        host="127.0.0.1",
        port=port,
        log_level="warning",
    )
    server, thread = start_uvicorn_in_thread(config, what="the live enrollment hub")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10.0)


@pytest.fixture
def enroll_spoke_dir(tmp_path: Path, clean_machine_env: None) -> Path:
    path = tmp_path / "spoke"
    path.mkdir(parents=True)
    return path


@pytest.fixture
def enroll_other_spoke_dir(tmp_path: Path, clean_machine_env: None) -> Path:
    path = tmp_path / "other-spoke"
    path.mkdir(parents=True)
    return path


@pytest.fixture
def enroll_cli_transport(
    enroll_hub: TestClientTransport, monkeypatch: pytest.MonkeyPatch
) -> TestClientTransport:
    """Point the CLI's transport factory at the in-process hub.

    The CLI itself is untouched: it still builds its own payload, posts it and
    prints its own result. Only the socket is replaced, exactly as the
    existing ``sync`` CLI tests do -- which is what keeps the parity test an
    honest comparison rather than a comparison of two things the test wired
    together itself.
    """
    monkeypatch.setattr(
        maintenance_enroll, "_transport_for", lambda hub_url: enroll_hub
    )
    return enroll_hub
