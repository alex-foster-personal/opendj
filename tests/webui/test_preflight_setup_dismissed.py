"""PREFLIGHT-02 (issue #2589): the dismissed-empty library must not lock a
brand-new user out of the app forever behind a preflight gate that never
knew "Continue without importing" was a real, intentional answer.

Three states, matching the issue's own acceptance criteria literally:
  * fresh -- no state.db, no setup record at all (the true first boot,
    before the wizard has been touched). ``library-attached`` stays ``fail``
    exactly as before: the setup wizard, not this gate, owns that ask.
  * dismissed-empty -- setup.json says ``dismissed: true`` and the library
    has 0 tracks (no state.db, or a state.db with an empty tracks table).
    ``library-attached`` must be ``pending`` (non-blocking) with a
    remediation naming "Run setup", and the overall preflight verdict must
    be ``pass`` so the boot gate actually clears.
  * attached -- a real, non-empty library. Unaffected by the dismissed flag
    either way: ``pass``.

Also covers the resolver-unification half of the issue: ``GET /api/v1/health``
and ``GET /api/v1/preflight`` must read the identical ``state.db`` path
(:func:`apps.webui.server.state_paths.resolve_state_db_path`), so a file
health reports as existing (or attached, with real tracks) can never be
called "no state.db" by preflight in the same breath.

[if] a dismissed-empty library reports fail [then] the boot gate never clears, [else stop].
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.adapters.rekordbox import config as rb_config
from apps.engine_core.setup import record as setup_record
from apps.shared.state import db as state_db
from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend
from apps.webui.server.preflight_checks import LIBRARY_ATTACHED_DISMISSED_REMEDIATION

pytestmark = pytest.mark.requirement("PREFLIGHT-02")


def _insert_track(state_path: Path, stable_id: str) -> None:
    conn = state_db.open_rw(state_path)
    try:
        conn.execute(
            "INSERT INTO tracks (stable_id, stable_id_tier, duration_ms, "
            "file_path, created_at, updated_at) "
            "VALUES (?, 'inferred', 210000, '/music/present.mp3', '2026-01-01', '2026-01-01')",
            (stable_id,),
        )
        conn.commit()
    finally:
        conn.close()


def _build_client(
    monkeypatch: pytest.MonkeyPatch, data_dir: Path, *, create_state_db: bool
) -> TestClient:
    """A daemon whose data dir is ``data_dir`` (``setup.json`` lives there
    directly) and whose ``state.db`` is ``data_dir/state/state.db`` -- the
    same ``<data_dir>/state/state.db`` layout ``EngineConfig`` and
    ``platform_paths.DATA_DIR`` both assume, so ``_data_dir_for_state_db``'s
    derivation round-trips.
    """
    state_path = data_dir / "state" / "state.db"
    if create_state_db:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_db.open_rw(state_path).close()
    monkeypatch.setattr(rb_config, "STATE_DB", state_path)
    monkeypatch.setattr(rb_config, "MASTER_PLAIN_DB", data_dir / "absent-master.db")
    app = create_app(
        backend=InMemoryBackend(),
        bind_host="127.0.0.1",
        hostname="test-host",
        lock_status_fn=lambda: None,
        syncthing_status_fn=lambda: None,
        state_db_path=str(state_path),
    )
    return TestClient(app, raise_server_exceptions=False)


def _check(body: dict, check_id: str) -> dict:
    matches = [c for c in body["checks"] if c["id"] == check_id]
    assert len(matches) == 1, f"expected exactly one {check_id!r} row, got {matches}"
    return matches[0]


# ----- fresh: no state.db, no setup record --------------------------------


def test_fresh_install_not_dismissed_stays_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The true first boot: nobody has touched the wizard yet. This must NOT
    become non-blocking, or an operator who never saw the wizard at all
    would sail straight past this gate with the wizard's own job undone.
    [if] a fresh, untouched install's library-attached is anything but
    ``fail`` [then] broken."""
    with _build_client(monkeypatch, tmp_path, create_state_db=False) as client:
        body = client.get("/api/v1/preflight").json()
    assert body["status"] == "fail"
    row = _check(body, "library-attached")
    assert row["status"] == "fail"
    assert "no state.db" in row["detail"]


# ----- dismissed-empty ------------------------------------------------------


def test_dismissed_empty_no_state_db_is_pending_not_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact issue #2589 repro: "Continue without importing" recorded
    the dismissal, no state.db was ever written, and the boot gate must
    clear rather than lock the user out forever.
    [if] this stays ``fail`` [then] broken (issue #2589)."""
    setup_record.set_dismissed(tmp_path, True)
    with _build_client(monkeypatch, tmp_path, create_state_db=False) as client:
        body = client.get("/api/v1/preflight").json()
    assert body["status"] == "pass", "a dismissed-empty library must not hold the boot gate"
    row = _check(body, "library-attached")
    assert row["status"] == "pending"
    assert "dismissed" in row["detail"]
    assert row["remediation"] == LIBRARY_ATTACHED_DISMISSED_REMEDIATION
    assert "Run setup" in row["remediation"]


def test_dismissed_empty_with_a_real_but_empty_state_db_is_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same dismissed decision, but the daemon has since restarted and a real
    (empty) state.db now exists on disk -- the other half of the
    ``_library_attached`` branch (0 tracks, not a missing file)."""
    setup_record.set_dismissed(tmp_path, True)
    with _build_client(monkeypatch, tmp_path, create_state_db=True) as client:
        body = client.get("/api/v1/preflight").json()
    assert body["status"] == "pass"
    row = _check(body, "library-attached")
    assert row["status"] == "pending"
    assert "0 tracks" in row["detail"]
    assert "dismissed" in row["detail"]


def test_malformed_setup_record_fails_loudly_not_silently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A setup.json this engine cannot parse must not be read as "not
    dismissed" by default -- record.py's own doc says defaulting it away
    would silently re-show (or, here, re-hide) a decision the operator made.
    [if] a corrupt setup record renders as anything but a named ``fail``
    [then] broken."""
    (tmp_path / "setup.json").write_text("{not valid json", encoding="utf-8")
    with _build_client(monkeypatch, tmp_path, create_state_db=False) as client:
        body = client.get("/api/v1/preflight").json()
    assert body["status"] == "fail"
    row = _check(body, "library-attached")
    assert row["status"] == "fail"
    assert "setup record" in row["detail"]
    assert "could not be read" in row["detail"]


# ----- attached: real, non-empty library -----------------------------------


def test_attached_library_passes_regardless_of_dismissed_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real library always passes: the dismissed flag only matters while
    the library is empty, and must never downgrade an attached one."""
    for dismissed in (True, False):
        data_dir = tmp_path / f"dismissed-{dismissed}"
        setup_record.set_dismissed(data_dir, dismissed)
        state_path = data_dir / "state" / "state.db"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        _insert_track(state_path, "a" * 40)
        with _build_client(monkeypatch, data_dir, create_state_db=False) as client:
            body = client.get("/api/v1/preflight").json()
        assert body["status"] == "pass", dismissed
        row = _check(body, "library-attached")
        assert row["status"] == "pass", dismissed
        assert row["detail"] == "1 tracks"


# ----- resolver unification: health and preflight agree ---------------------


def test_health_and_preflight_resolve_the_identical_state_db_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] health reports a path as existing (real tracks) and preflight
    calls that SAME path "no state.db" [then] broken (issue #2589) -- the
    two endpoints must be reading one function, not two independent guesses."""
    state_path = tmp_path / "state" / "state.db"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    _insert_track(state_path, "b" * 40)
    with _build_client(monkeypatch, tmp_path, create_state_db=False) as client:
        health_body = client.get("/api/v1/health").json()
        preflight_body = client.get("/api/v1/preflight").json()

    # health's track count comes from the ACTIVE backend (InMemoryBackend in
    # this fixture, deliberately -- that mismatch is real and orthogonal to
    # this bug), so the thing under test here is the PATH the two endpoints
    # agree on, not the count each independently reports for it.
    assert Path(health_body["state_db"]["path"]) == state_path

    state_db_row = _check(preflight_body, "state-db")
    library_row = _check(preflight_body, "library-attached")
    assert "no state.db" not in state_db_row["detail"]
    assert "no state.db" not in library_row["detail"]
    assert library_row["status"] == "pass"
