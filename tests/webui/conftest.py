"""Test fixtures for the webui daemon (CAT-05)."""
from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.webui.server.app import create_app
from apps.webui.server.backend import (
    InMemoryBackend,
    Pairing,
    Playlist,
    Provenance,
    QueueItem,
    Track,
)
from apps.webui.server.etag import compute_etag

# SEC-01 (issue #2689): the TestClient host-allowlist default lives once in
# the root tests/conftest.py (it applies fleet-wide, not just to this
# suite) -- see tests/testclient_host_allowlist.py for why.

WEBUI_TEST_ROOT: Path = Path(__file__).resolve().parent

# request_guard's host allowlist (issue #2689) checks the literal HTTP Host
# header; httpx's TestClient default base_url sends "testserver", which
# matches nothing in the allowlist. Every fixture below passes hostname=
# "test-host" to create_app(), so pinning base_url to that same name is the
# one consistent value the app will actually accept.
TEST_HOST_BASE_URL = "http://test-host"


@pytest.fixture(autouse=True)
def _restore_serving_lanes_after_test() -> Iterator[None]:
    """``test_analysis_source_serving`` clears :data:`SERVING_LANES` mid-module;
    re-run the production bootstrap after every webui test so later files
    (``test_anlz_pssi_on_own_switch``) still see ``waveform`` as served."""
    from apps.analysis.serving_lanes import SERVING_LANES
    from apps.webui.server.analysis_serving_bootstrap import ensure_analysis_serving_lanes

    yield
    SERVING_LANES.clear()
    ensure_analysis_serving_lanes()


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Auto-mark webui tests with pytest.mark.rb_parity at collection time.

    The focused Rekordbox parity gates select with ``-m rb_parity``, and
    tests/scripts/test_run_rb_parity_check.py enrolls every
    tests/webui/test_*.py by rglob. A new webui test file must therefore be
    selected with no hand-written marker (issue #1140): this hook supplies the
    marker. It is handed items from the whole collection session, not just
    this directory, so it filters on the item path - only modules under
    tests/webui named test_*.py, exactly the set the rglob enrolls.
    """
    marker = pytest.mark.rb_parity
    for item in items:
        module_path = Path(getattr(item, "path", ".")).resolve()
        if (
            module_path.name.startswith("test_")
            and module_path.suffix == ".py"
            and module_path.is_relative_to(WEBUI_TEST_ROOT)
        ):
            item.add_marker(marker)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _stub_rb_vendor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the app fixtures hermetic: no live master.plain.db dependency.

    ``list_playlists`` consults ``rb_vendor.playlist_order_index`` whenever
    rekordbox playlists exist; it reads the live ``master.plain.db``. On a
    dev Mac that happens to exist, so the unit test silently depended on
    machine state; on any other machine it fails loudly (by design) and the
    endpoint 500s. Stub ONLY the db-backed order lookup -- everything else
    (``bulk_availability`` is a pure stat pass) stays real. rb_vendor's own
    behaviour is covered by tests/webui/test_rb_vendor_portability.py and
    the live-db suite.
    """
    from apps.webui.server import rb_vendor

    monkeypatch.setattr(rb_vendor, "playlist_order_index", lambda: {})


@pytest.fixture
def seed_backend() -> InMemoryBackend:
    """5 tracks, 2 playlists, 1 pairing, 1 dedup candidate."""
    backend = InMemoryBackend()
    base = datetime(2026, 4, 17, 10, 0, 0, tzinfo=UTC)
    for i, (title, artist, bpm, key, rating, tags) in enumerate(
        [
            ("Midnight Drive", "the maintainer", 124.0, "8A", 4, ["deep-house"]),
            ("Oxide", "Beta", 128.0, "7A", 3, ["techno"]),
            ("Gulf", "Gamma", 118.0, "5A", 5, ["ambient", "downtempo"]),
            ("Phoenix", "Delta", 140.0, "11A", 2, ["trance"]),
            ("Nest", "Epsilon", 92.0, "3B", 4, ["breaks"]),
        ], start=1,
    ):
        sid = f"track-{i:03d}"
        created = _iso(base)
        backend.seed_track(Track(
            stable_id=sid, title=title, artist=artist,
            bpm=bpm, key=key, rating=rating, tags=tags,
            created_at=created, updated_at=created,
            provenance={
                "rating": Provenance(value=rating, source="rekordbox",
                                     confidence=1.0, modified_at=created,
                                     status="ok"),
                "bpm": Provenance(value=bpm, source="rekordbox",
                                  confidence=0.95, modified_at=created,
                                  status="ok"),
            },
        ))
    backend.seed_playlist(Playlist(
        playlist_id="pl-001", name="Opener Set", vendor="rekordbox",
        items=["track-003", "track-005"],
        created_at=_iso(base), updated_at=_iso(base),
    ))
    backend.seed_playlist(Playlist(
        playlist_id="pl-002", name="Peak Hour", vendor="djay",
        items=["track-001", "track-002", "track-004"],
        created_at=_iso(base), updated_at=_iso(base),
    ))
    backend.seed_pairing(Pairing(
        pairing_id="p-001", from_stable_id="track-001",
        to_stable_id="track-002", direction="->", source="manual",
        notes="great opener transition",
        created_at=_iso(base), updated_at=_iso(base),
    ))
    backend.seed_queue("dedup", [
        QueueItem(stable_id="track-003", kind="dedup", payload={
            "cluster": ["track-003", "track-003-dup"],
            "canonical": "track-003", "similarity": 0.98,
        })
    ])
    return backend


@pytest.fixture
def client(
    seed_backend: InMemoryBackend, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    _stub_rb_vendor(monkeypatch)
    app = create_app(
        backend=seed_backend, bind_host="127.0.0.1", hostname="test-host",
        port=18697, frontend_port=19411,
        lock_status_fn=lambda: None, syncthing_status_fn=lambda: None,
    )
    with TestClient(app, base_url=TEST_HOST_BASE_URL) as c:
        yield c


@pytest.fixture
def insecure_client(
    seed_backend: InMemoryBackend, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    _stub_rb_vendor(monkeypatch)
    app = create_app(backend=seed_backend, bind_host="0.0.0.0",
                     hostname="test-host")
    with TestClient(app, base_url=TEST_HOST_BASE_URL) as c:
        yield c


@pytest.fixture
def locked_client(
    seed_backend: InMemoryBackend, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    _stub_rb_vendor(monkeypatch)
    app = create_app(
        backend=seed_backend, bind_host="127.0.0.1", hostname="test-host",
        lock_status_fn=lambda: {"holder": "other-host",
                                 "expires_at": "2099-01-01T00:00:00Z"},
    )
    with TestClient(app, base_url=TEST_HOST_BASE_URL) as c:
        yield c


def current_etag(backend: InMemoryBackend, stable_id: str) -> str:
    track = backend.get_track(stable_id)
    return compute_etag(track.stable_id, track.updated_at)
