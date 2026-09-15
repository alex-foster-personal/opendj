"""Headless contract test: a track can load into a deck (#769).

The failure chain (#762 unmigrated db, #766 blocked audio opens, #767 masked
error bodies) is reachable headlessly -- no packaged build is needed.

Cloud-buildable: boots the real ``create_app()`` engine against a synthetic
fixture library (a fresh, fully-migrated state.db in tmp_path plus one real,
checked-in mp3) rather than the real local ``data/`` tree, so this always
runs in the fast pytest lane, on any machine, with no rekordbox share assets.

Scope, precisely (see #769's acceptance criteria -- deliberately narrow):
this covers the ``/audio`` GET's HTTP contract only -- status, bytes,
content-type, headers, and the error shape on a missing file. It does NOT
re-verify #762 (migration-on-boot already has its own dedicated, real
subprocess-boot regression test: ``tests/engine_core/test_migrate_on_boot.py``,
which exercises ``create_app`` + ``make_backend()`` against a genuine stale
schema -- duplicating that here would be scope creep, not defense in depth).
It also does NOT decode the audio or exercise the frontend's DSP graph --
the issue explicitly scopes this to "no Playwright" and a Range-request
contract, not a browser-level decode acceptance test.

The ``rb_config.STATE_DB`` / ``MASTER_PLAIN_DB`` monkeypatch below is the
same technique already used by the merged, CI-green
``tests/webui/test_rb_meta_local_track.py`` for this identical router: a
config-path substitution that redirects the real, unmodified resolver
functions at real fixture data, not a faked behavior or a bypassed code
path. ``get_track_audio`` reads these two module-level path constants
directly rather than through ``app.state.backend`` regardless of which
backend ``create_app()`` is given, so that is the injection seam this
endpoint's own architecture exposes.

``raise_server_exceptions=False`` matters: it is what makes the TestClient
behave like the real ASGI stack a browser deck talks to. An unhandled
exception on this path would otherwise be re-raised INTO the test process
instead of rendered as the response a real client receives -- which is
exactly the gap that let #767 (a raw text/plain 500 crashing the frontend's
JSON parser) ship unnoticed.

``_get_with_deadline`` matters too, and is not decorative: ``TestClient``
talks to the app over httpx's in-process ASGI transport, where httpx's own
``timeout=`` kwarg is a documented no-op (starlette even warns on it) -- a
handler that never returns just hangs the call forever, which is precisely
the #766 failure shape this test exists to catch. Verified empirically
before writing this: an endpoint that ``await``s a 30s sleep, called with
``client.get(url, timeout=2.0)``, blocks for the full 30 seconds and returns
200 -- the timeout kwarg is silently ignored. Running the request on a
daemon thread and joining it via a queue with a real ``queue.Empty`` deadline
enforces a genuine wall-clock cutoff, confirmed against the same probe: it
raises at 2s, not 30s.

Regression one-liners:
  - if a deck's Range probe on a real on-disk track doesn't 206 with >=1
    audio byte, an audio/* content-type and X-Audio-Kind then broken
  - if a track whose file is absent ever hangs, or answers with anything but
    a real JSON error body, then broken (#767's exact failure shape)
"""

from __future__ import annotations

import hashlib
import os
import queue
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from apps.adapters.rekordbox import config as rb_config
from apps.shared.state import db as state_db
from apps.shared.state import sync_stamp
from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend

pytestmark = [
    pytest.mark.requirement("CAT-05"),
    pytest.mark.requirement("AUDIO-ACCESS-01"),
    pytest.mark.rb_parity,
]

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_AUDIO_FIXTURE = (
    REPO_ROOT / "tests" / "fixtures" / "conformance" / "03-8-hot-cues" / "audio" / "cues.mp3"
)
# Pinned per AGENTS.md "No mocks and locked real fixtures": verify the
# canonical fixture by checksum before use, so a truncated or swapped-out
# file fails loudly here instead of silently passing a one-byte Range probe.
# Recomputed with: python3 -c "import hashlib; print(hashlib.sha256(
#   open('tests/fixtures/conformance/03-8-hot-cues/audio/cues.mp3',
#        'rb').read()).hexdigest())"
REAL_AUDIO_FIXTURE_SHA256 = "922d6cfa0886ef5a6ae195af01d992782d2680bc40244940254934c9aee7c4d0"

PLAYABLE_SID = "e" * 40
GONE_SID = "f" * 40
FIFO_SID = "a" * 40
DURATION_MS = 210_000
CLIENT_TIMEOUT_S = 5.0


def _get_with_deadline(client: TestClient, url: str, *, timeout: float, **kwargs: Any) -> Response:
    """``client.get`` with a real enforced wall-clock deadline.

    httpx's own ``timeout=`` kwarg does nothing against TestClient's
    in-process ASGI transport (see module docstring) -- a handler that hangs
    just hangs the test. Running the call on a daemon thread and bounding the
    wait with ``queue.get(timeout=...)`` is what actually stops it.
    """
    result: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)

    def _run() -> None:
        try:
            result.put(("ok", client.get(url, **kwargs)))
        except Exception as exc:  # noqa: BLE001 - re-raised on the main thread below
            result.put(("error", exc))

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    try:
        outcome, payload = result.get(timeout=timeout)
    except queue.Empty:
        pytest.fail(
            f"GET {url} did not respond within {timeout}s -- reproduces the "
            "#766 hang class (a blocked audio open that never returns)"
        )
    if outcome == "error":
        raise payload
    return payload


def _insert_track(
    state_path: Path,
    stable_id: str,
    file_path: str,
    *,
    available: int | None = None,
) -> None:
    path = Path(file_path)
    digest = (
        hashlib.sha256(path.read_bytes()).hexdigest()
        if path.is_file()
        else hashlib.sha256(file_path.encode()).hexdigest()
    )
    if available is None:
        available = 1 if path.is_file() else 0
    conn = state_db.open_rw(state_path)
    try:
        conn.execute(
            "INSERT INTO tracks (stable_id, stable_id_tier, duration_ms, "
            "file_path, content_hash, created_at, updated_at) "
            "VALUES (?, 'inferred', ?, ?, ?, '2026-01-01', '2026-01-01')",
            (stable_id, DURATION_MS, file_path, digest),
        )
        machine_id = sync_stamp.ensure_local_machine(conn)
        conn.execute(
            "INSERT OR IGNORE INTO sync_policies(machine_id, asset_kind, mode, "
            "cache_budget_mb, updated_at) VALUES (?, 'audio', 'pinned', 100, "
            "'2026-01-01')",
            (machine_id,),
        )
        conn.execute(
            "INSERT INTO track_locations(stable_id, machine_id, kind, role, "
            "file_path, available, content_hash, created_at, updated_at) "
            "VALUES (?, ?, 'local', 'primary', ?, ?, ?, '2026-01-01', '2026-01-01')",
            (stable_id, machine_id, file_path, available, digest),
        )
        conn.commit()
    finally:
        conn.close()


def _verify_fixture_checksum() -> None:
    assert REAL_AUDIO_FIXTURE.is_file(), f"checked-in fixture audio missing: {REAL_AUDIO_FIXTURE}"
    actual = hashlib.sha256(REAL_AUDIO_FIXTURE.read_bytes()).hexdigest()
    assert actual == REAL_AUDIO_FIXTURE_SHA256, (
        f"canonical fixture {REAL_AUDIO_FIXTURE} does not match its pinned "
        f"checksum (expected {REAL_AUDIO_FIXTURE_SHA256}, got {actual}) -- "
        "it was truncated, corrupted, or replaced since this test was "
        "written; the assertions below assume real, unmodified audio bytes"
    )


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    _verify_fixture_checksum()

    state_path = tmp_path / "state.db"
    _insert_track(state_path, PLAYABLE_SID, str(REAL_AUDIO_FIXTURE))
    _insert_track(state_path, GONE_SID, str(tmp_path / "moved-away.mp3"))

    # A purely local fixture library: no rekordbox master db, no packaged
    # frontend build. resolve_playable_audio must serve straight off
    # tracks.file_path with neither.
    monkeypatch.setattr(rb_config, "STATE_DB", state_path)
    monkeypatch.setattr(rb_config, "MASTER_PLAIN_DB", tmp_path / "absent-master.db")

    app = create_app(
        backend=InMemoryBackend(),
        bind_host="127.0.0.1",
        hostname="test-host",
        lock_status_fn=lambda: None,
        syncthing_status_fn=lambda: None,
    )
    with TestClient(
        app, raise_server_exceptions=False, base_url="http://127.0.0.1"
    ) as test_client:
        yield test_client


def test_track_loads_into_deck(client: TestClient) -> None:
    """The exact shape of a deck's load probe: a tiny Range request."""
    response = _get_with_deadline(
        client,
        f"/api/v1/tracks/{PLAYABLE_SID}/audio",
        headers={"Range": "bytes=0-0"},
        timeout=CLIENT_TIMEOUT_S,
    )
    assert response.status_code == 206, response.text[:300]
    assert len(response.content) >= 1
    assert response.headers["content-type"].startswith("audio/")
    assert "X-Audio-Kind" in response.headers


def test_head_probe_answers_like_get_without_a_body(client: TestClient) -> None:
    """[if] a client probes playability with HEAD [then] it gets GET's status and
    headers with no body, never 405 (the LyricsPanel probe of Sat 12 Sep 2026)."""
    head = client.head(f"/api/v1/tracks/{PLAYABLE_SID}/audio")
    assert head.status_code == 200, head.text[:300]
    assert head.content == b""
    assert head.headers["content-type"].startswith("audio/")
    assert "X-Audio-Kind" in head.headers
    gone = client.head(f"/api/v1/tracks/{GONE_SID}/audio")
    assert gone.status_code == client.get(f"/api/v1/tracks/{GONE_SID}/audio").status_code
    assert gone.status_code != 405


def test_missing_audio_file_reports_structured_error_not_bare_500(
    client: TestClient,
) -> None:
    """Negative control for #767: absent audio must never hang, and must
    never answer with anything but a genuine JSON error body."""
    response = _get_with_deadline(
        client,
        f"/api/v1/tracks/{GONE_SID}/audio",
        timeout=CLIENT_TIMEOUT_S,
    )
    assert response.status_code != 200
    content_type = response.headers.get("content-type", "")
    assert content_type.startswith("application/json"), (
        "a non-JSON error body crashes the frontend's _throwRbApiError "
        f"(#767): content-type={content_type!r} body={response.text[:300]!r}"
    )
    assert "detail" in response.json()


def test_blocked_audio_open_returns_503_audio_access_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] the resolved audio path blocks in ``open()`` past the timeout [then]
    503 with code ``AUDIO_ACCESS_BLOCKED``, never a hang and never a zero-byte
    200, [else stop]"""
    from apps.webui.server import preflight_checks

    _verify_fixture_checksum()
    fifo = tmp_path / "blocked.mp3"
    os.mkfifo(fifo)
    monkeypatch.setattr(preflight_checks, "AUDIO_ACCESS_TIMEOUT_S", 0.5)

    state_path = tmp_path / "state.db"
    _insert_track(state_path, FIFO_SID, str(fifo), available=1)
    _insert_track(state_path, PLAYABLE_SID, str(REAL_AUDIO_FIXTURE))

    monkeypatch.setattr(rb_config, "STATE_DB", state_path)
    monkeypatch.setattr(rb_config, "MASTER_PLAIN_DB", tmp_path / "absent-master.db")
    app = create_app(
        backend=InMemoryBackend(),
        bind_host="127.0.0.1",
        hostname="test-host",
        lock_status_fn=lambda: None,
        syncthing_status_fn=lambda: None,
    )
    with TestClient(app, raise_server_exceptions=False, base_url="http://127.0.0.1") as client:
        response = _get_with_deadline(
            client,
            f"/api/v1/tracks/{FIFO_SID}/audio",
            timeout=CLIENT_TIMEOUT_S,
        )
        assert response.status_code == 503, response.text[:300]
        assert response.headers.get("content-type", "").startswith("application/json")
        assert response.json()["detail"]["code"] == "AUDIO_ACCESS_BLOCKED"

        head = client.head(f"/api/v1/tracks/{FIFO_SID}/audio")
        assert head.status_code == 503
        assert head.content == b""

        follow_up = _get_with_deadline(
            client,
            f"/api/v1/tracks/{PLAYABLE_SID}/audio",
            headers={"Range": "bytes=0-0"},
            timeout=CLIENT_TIMEOUT_S,
        )
        assert follow_up.status_code == 206, follow_up.text[:300]
