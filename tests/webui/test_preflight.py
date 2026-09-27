"""Contract test for GET /api/v1/preflight (PREFLIGHT-01, issue #771).

Three fixtures, matching the acceptance criteria literally:
  * healthy -- a fresh, fully-migrated state.db with one real, on-disk track
    -> every check ``pass``, overall ``pass``.
  * unmigrated-db -- the pinned v5 dump (same fixture #762's own regression
    test uses) -> state-db ``fail`` naming found-vs-expected versions.
  * missing-audio -- a migrated db whose only track's file does not exist
    -> audio-access ``fail``, and the assertion is on the TIMEOUT BOUND
    (elapsed wall-clock), not merely on the status code, per this issue's
    own caution that a hang-then-fail is a different defect from a fail.

Complements #769's ``test_audio_deck_load_contract.py`` (same fixture
technique: ``rb_config.STATE_DB`` monkeypatch, no rekordbox master db, no
packaged frontend build) and #762's own migration regression test in
``test_sqlite_backend.py`` (whose ``_verified_v5_sql`` dump is reused here
rather than a second copy).
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.adapters.rekordbox import config as rb_config
from apps.shared.state import db as state_db
from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend
from apps.webui.server.preflight_checks import AUDIO_ACCESS_TIMEOUT_S, run_preflight
from tests.test_schema_time_travel import _verified_v5_sql

pytestmark = [pytest.mark.requirement("PREFLIGHT-01"), pytest.mark.requirement("PREFLIGHT-03")]

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_AUDIO_FIXTURE = (
    REPO_ROOT / "tests" / "fixtures" / "conformance" / "03-8-hot-cues" / "audio" / "cues.mp3"
)
# Same pinned fixture and checksum as test_audio_deck_load_contract.py.
REAL_AUDIO_FIXTURE_SHA256 = "922d6cfa0886ef5a6ae195af01d992782d2680bc40244940254934c9aee7c4d0"

PLAYABLE_SID = "e" * 40
GONE_SID = "f" * 40


def _verify_fixture_checksum() -> None:
    assert REAL_AUDIO_FIXTURE.is_file(), f"checked-in fixture audio missing: {REAL_AUDIO_FIXTURE}"
    actual = hashlib.sha256(REAL_AUDIO_FIXTURE.read_bytes()).hexdigest()
    assert actual == REAL_AUDIO_FIXTURE_SHA256, (
        f"canonical fixture {REAL_AUDIO_FIXTURE} does not match its pinned checksum"
    )


def _insert_track(state_path: Path, stable_id: str, file_path: str) -> None:
    conn = state_db.open_rw(state_path)
    try:
        conn.execute(
            "INSERT INTO tracks (stable_id, stable_id_tier, duration_ms, "
            "file_path, created_at, updated_at) "
            "VALUES (?, 'inferred', 210000, ?, '2026-01-01', '2026-01-01')",
            (stable_id, file_path),
        )
        conn.commit()
    finally:
        conn.close()


def _build_client(monkeypatch: pytest.MonkeyPatch, state_path: Path) -> TestClient:
    monkeypatch.setattr(rb_config, "STATE_DB", state_path)
    monkeypatch.setattr(rb_config, "MASTER_PLAIN_DB", state_path.parent / "absent-master.db")
    app = create_app(
        backend=InMemoryBackend(),
        bind_host="127.0.0.1",
        hostname="test-host",
        lock_status_fn=lambda: None,
        syncthing_status_fn=lambda: None,
        # PREFLIGHT-02 (#2589): preflight now reads app.state.state_db_path
        # (via resolve_state_db_path), the SAME value health reads, instead
        # of the rb_config.STATE_DB module constant patched above. Without
        # this, every fixture here would silently point preflight at
        # create_app's own relative default ("data/state/state.db") rather
        # than the tmp_path fixture -- the exact divergence issue #2589 is
        # about, reproduced by omission if this line is removed.
        state_db_path=str(state_path),
    )
    # TestClient's default Host is ``testserver``; SEC-01's allowlist rejects it
    # (issue #2689), so drive requests through loopback.
    return TestClient(app, raise_server_exceptions=False, base_url="http://127.0.0.1")


@pytest.fixture
def healthy_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    _verify_fixture_checksum()
    state_path = tmp_path / "state.db"
    _insert_track(state_path, PLAYABLE_SID, str(REAL_AUDIO_FIXTURE))
    with _build_client(monkeypatch, state_path) as client:
        yield client


@pytest.fixture
def unmigrated_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    state_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(state_path))
    try:
        conn.executescript(_verified_v5_sql())
        conn.execute(
            "INSERT INTO tracks(stable_id, stable_id_tier, title, "
            "  file_path, created_at, updated_at) "
            "VALUES ('sid-v5', 'inferred', 'Old Build Track', "
            "  '/music/old.mp3', ?, ?)",
            (
                datetime.now(UTC).isoformat(),
                datetime.now(UTC).isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    with _build_client(monkeypatch, state_path) as client:
        yield client


@pytest.fixture
def missing_audio_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    state_path = tmp_path / "state.db"
    _insert_track(state_path, GONE_SID, str(tmp_path / "moved-away.mp3"))
    with _build_client(monkeypatch, state_path) as client:
        yield client


def _check(body: dict, check_id: str) -> dict:
    matches = [c for c in body["checks"] if c["id"] == check_id]
    assert len(matches) == 1, f"expected exactly one {check_id!r} row, got {matches}"
    return matches[0]


# REQ: PREFLIGHT-01
@pytest.mark.requirement("PREFLIGHT-01")
def test_healthy_fixture_is_all_pass(healthy_client: TestClient) -> None:
    """[if] the fixture is healthy [then] every preflight check passes, [else stop]."""
    r = healthy_client.get("/api/v1/preflight")
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    assert body["status"] == "pass"
    for check in body["checks"]:
        assert check["status"] == "pass", check


def test_unmigrated_db_fails_state_db_naming_versions(unmigrated_client: TestClient) -> None:
    r = unmigrated_client.get("/api/v1/preflight")
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    assert body["status"] == "fail"
    row = _check(body, "state-db")
    assert row["status"] == "fail"
    assert "5" in row["detail"], row["detail"]
    assert row["remediation"] is not None


def test_missing_audio_is_pending_not_fail_and_stays_within_timeout_bound(
    missing_audio_client: TestClient,
) -> None:
    """A missing file must come back fast (open() raises ENOENT immediately,
    the bound below proves it is not a hang) and must NOT hold the boot gate.
    A sampled file under an old home directory may no longer resolve.
    Moved files are a library problem, so the row is ``pending``
    with a relink remediation and the overall verdict stays ``pass``."""
    start = time.monotonic()
    r = missing_audio_client.get("/api/v1/preflight")
    elapsed = time.monotonic() - start
    assert r.status_code == 200, r.text[:300]
    assert elapsed < AUDIO_ACCESS_TIMEOUT_S + 2.0, (
        f"preflight took {elapsed:.1f}s against a timeout bound of "
        f"{AUDIO_ACCESS_TIMEOUT_S}s -- looks like a hang, not a fail"
    )
    body = r.json()
    assert body["status"] == "pass", "if a moved library holds the boot gate then broken"
    row = _check(body, "audio-access")
    assert row["status"] == "pending"
    assert "moved-away.mp3" in row["detail"]
    assert "0 of 1 sampled" in row["detail"]
    assert row["remediation"] is not None
    assert "Media" not in row["remediation"], "a missing file is not a permission problem"


def test_missing_then_readable_sample_passes_and_reports_the_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The probe walks the sample: a missing first row is skipped and the next
    readable location proves access. [if] the first missing row decides the
    verdict [then] broken."""
    state_path = tmp_path / "state.db"
    real = tmp_path / "present.mp3"
    real.write_bytes(b"ID3")
    _insert_track(state_path, "a" * 40, str(tmp_path / "moved-away.mp3"))
    _insert_track(state_path, "b" * 40, str(real))
    with _build_client(monkeypatch, state_path) as client:
        body = client.get("/api/v1/preflight").json()
    assert body["status"] == "pass"
    row = _check(body, "audio-access")
    assert row["status"] == "pass"
    assert "1 sampled location(s) missing on disk" in row["detail"]


def test_permission_denied_still_fails_with_media_remediation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """EACCES is the shape a macOS Media Library denial takes: that must stay a
    red light with the privacy-pane remediation, unlike a missing file."""
    if os.geteuid() == 0:
        pytest.skip("root ignores file modes")
    state_path = tmp_path / "state.db"
    locked = tmp_path / "locked.mp3"
    locked.write_bytes(b"ID3")
    locked.chmod(0)
    _insert_track(state_path, "c" * 40, str(locked))
    try:
        with _build_client(monkeypatch, state_path) as client:
            body = client.get("/api/v1/preflight").json()
    finally:
        locked.chmod(0o600)
    assert body["status"] == "fail"
    row = _check(body, "audio-access")
    assert row["status"] == "fail"
    assert "locked.mp3" in row["detail"]
    assert "Media" in row["remediation"]


# REQ: PREFLIGHT-01
@pytest.mark.requirement("PREFLIGHT-01")
def test_engine_alive_row_always_present_and_passing(healthy_client: TestClient) -> None:
    """[if] preflight runs on a healthy engine [then] engine-alive passes, [else stop]."""
    r = healthy_client.get("/api/v1/preflight")
    body = r.json()
    row = _check(body, "engine-alive")
    assert row["status"] == "pass"


# REQ: PREFLIGHT-01
@pytest.mark.requirement("PREFLIGHT-01")
def test_openapi_schema_carries_preflight(healthy_client: TestClient) -> None:
    """[if] openapi.json is served [then] it lists /api/v1/preflight, [else stop]."""
    r = healthy_client.get("/openapi.json")
    assert r.status_code == 200
    assert "/api/v1/preflight" in r.json()["paths"]


def test_fifo_open_times_out_without_hanging_the_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A FIFO with no writer blocks ``open()`` in-kernel like #766's shape.
    [if] the request takes longer than the bound plus slack [then] the boot
    gate hangs - broken."""
    from apps.webui.server import preflight_checks

    fifo = tmp_path / "blocked.fifo"
    os.mkfifo(fifo)
    monkeypatch.setattr(preflight_checks, "AUDIO_ACCESS_TIMEOUT_S", 0.5)
    state_path = tmp_path / "state.db"
    _insert_track(state_path, "d" * 40, str(fifo))
    start = time.monotonic()
    with _build_client(monkeypatch, state_path) as client:
        body = client.get("/api/v1/preflight").json()
    elapsed = time.monotonic() - start
    assert elapsed < 3.0, f"preflight waited {elapsed:.1f}s for a blocked fifo open()"
    row = _check(body, "audio-access")
    assert row["status"] == "fail"
    assert "did not return within" in row["detail"]


def test_boot_user_copy_is_plain_language(tmp_path: Path) -> None:
    state_path = tmp_path / "state.db"
    _insert_track(state_path, PLAYABLE_SID, str(REAL_AUDIO_FIXTURE))
    body = run_preflight(state_path).model_dump()
    engine = _check(body, "engine-alive")
    assert engine["user_label"] == "App connected"
    assert "schema_meta" not in engine["user_detail"]
    state = _check(body, "state-db")
    assert state["user_label"] == "Library database"
    assert "schema_meta" not in (state["user_detail"] or "")


def test_empty_library_user_copy_offers_import_path(tmp_path: Path) -> None:
    data_dir = tmp_path / "empty"
    data_dir.mkdir()
    state_path = data_dir / "state" / "state.db"
    state_path.parent.mkdir(parents=True)
    state_db.open_rw(state_path).close()
    body = run_preflight(state_path).model_dump()
    row = _check(body, "library-attached")
    assert row["status"] == "fail"
    assert row["user_label"] == "Your music library"
    assert row["user_remediation"] is not None
    assert "Import your music" in row["user_remediation"]


def test_blocked_open_does_not_starve_following_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] a blocked first sample leaves a wedged worker [then] the next
    preflight cannot finish within the timeout budget - broken."""
    from apps.webui.server import preflight_checks

    _verify_fixture_checksum()
    fifo = tmp_path / "blocked.fifo"
    os.mkfifo(fifo)
    blocked_sid = "d" * 40
    monkeypatch.setattr(preflight_checks, "AUDIO_ACCESS_TIMEOUT_S", 0.5)
    state_path = tmp_path / "state.db"
    _insert_track(state_path, blocked_sid, str(fifo))
    with _build_client(monkeypatch, state_path) as client:
        first = client.get("/api/v1/preflight").json()
        conn = state_db.open_rw(state_path)
        try:
            conn.execute(
                "UPDATE tracks SET file_path = ? WHERE stable_id = ?",
                (str(REAL_AUDIO_FIXTURE), blocked_sid),
            )
            conn.commit()
        finally:
            conn.close()
        start = time.monotonic()
        second = client.get("/api/v1/preflight").json()
        elapsed = time.monotonic() - start
    first_row = _check(first, "audio-access")
    assert first_row["status"] == "fail"
    assert elapsed < AUDIO_ACCESS_TIMEOUT_S + 2.0
    assert second["status"] == "pass"
    second_row = _check(second, "audio-access")
    assert second_row["status"] == "pass"
