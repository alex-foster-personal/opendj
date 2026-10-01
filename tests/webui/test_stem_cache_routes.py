"""Stem cache budget over HTTP, on the engine timer, and through the CLI (STEM-43).

The routes, the timer tick and the CLI verbs all resolve the same
``cache_inputs(app)``, so these tests drive each surface against one real app
and one real stems directory. Disk is injected by monkeypatching
``measure_disk``: the surfaces must not depend on the test host's free space.

* [if] the disk is under the floor [then] ``GET /stems/cache/status`` says
  ``low_disk`` and names why nothing was done when it is blocked.
* [if] a bundle is loaded on a deck in THIS engine [then] neither the route
  nor the timer evicts it.
* [if] the CLI is pointed at a running engine [then] it reports what the
  route reports, byte for byte.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import time
import wave
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.cloud import stem_cache_budget
from apps.cloud.stem_cache_budget import GIB, DiskUsage, StemCacheSettings
from apps.cloud.stem_hydration import OPEN_DECKS
from apps.cloud.stem_index import save_cached_index
from apps.stems.cache_cli import EXIT_LOW_DISK
from apps.stems.cli import main as stems_cli_main
from apps.webui.server.routes.stem_cache import router
from apps.webui.server.stem_cache_enforcer import StemCacheEnforcer
from tests.waits import start_uvicorn_in_thread

VOLUME: int = 460 * GIB
FLOOR: int = 23 * GIB


def _wav_bytes() -> bytes:
    import io

    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(2)
        out.setsampwidth(2)
        out.setframerate(44_100)
        out.writeframes(b"\x00\x00" * 16)
    return buf.getvalue()


def _make_bundle(stems_dir: Path, stable_id: str, *, atime: float) -> dict[str, str]:
    bundle_dir = stems_dir / stable_id
    bundle_dir.mkdir(parents=True)
    manifest = {
        "schema_version": 1,
        "stable_id": stable_id,
        "model": {"name": "htdemucs", "version": "4.0.1"},
        "source": {"path": "/music/x.wav", "sha256": "a" * 64},
        "files": {p: f"{p}.wav" for p in ("vocals", "drums", "bass", "other")},
    }
    bodies = {"manifest.json": (json.dumps(manifest) + "\n").encode("utf-8")}
    for part in ("vocals", "drums", "bass", "other"):
        bodies[f"{part}.wav"] = _wav_bytes()
    entry: dict[str, str] = {}
    for filename, body in bodies.items():
        path = bundle_dir / filename
        path.write_bytes(body)
        os.utime(path, (atime, atime))
        entry[filename] = hashlib.sha256(body).hexdigest()
    return entry


def _app(tmp_path: Path, *, armed: bool) -> FastAPI:
    app = FastAPI()
    app.state.stems_dir = tmp_path / "stems"
    app.state.stem_cache_data_dir = tmp_path / "data"
    # ``can_rehydrate`` is only whether a source object is bound; the routes
    # under test never call it.
    app.state.stem_hydration_source = object() if armed else None
    app.include_router(router, prefix="/api/v1")
    return app


def _set_disk(monkeypatch: pytest.MonkeyPatch, *, free: int) -> None:
    monkeypatch.setattr(
        stem_cache_budget,
        "measure_disk",
        lambda _path: DiskUsage(total_bytes=VOLUME, free_bytes=free),
    )


@pytest.fixture
def two_bundles(tmp_path: Path) -> dict[str, dict[str, str]]:
    """``indexed`` is in the cached R2 index, ``local-only`` is not."""
    stems_dir = tmp_path / "stems"
    index = {"indexed": _make_bundle(stems_dir, "indexed", atime=1_000_000)}
    _make_bundle(stems_dir, "local-only", atime=2_000_000)
    save_cached_index(tmp_path / "data", index)
    return index


# --- status ------------------------------------------------------------------------


@pytest.mark.requirement("STEM-43")
def test_status_route_reports_low_disk_and_the_local_only_bundle(
    tmp_path: Path, two_bundles, monkeypatch: pytest.MonkeyPatch
):
    """[if] the disk is under the floor [then] the status route reads low_disk and names the local-only bundle, [else stop]."""
    _set_disk(monkeypatch, free=4 * GIB)
    client = TestClient(_app(tmp_path, armed=True))

    response = client.get("/api/v1/stems/cache/status")

    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "low_disk"
    assert body["disk_free_bytes"] == 4 * GIB
    assert body["floor_bytes"] == FLOOR
    assert body["shortfall_bytes"] == FLOOR - 4 * GIB
    assert body["local_only_stable_ids"] == ["local-only"]
    assert body["evictable_bundle_count"] == 1
    assert body["can_rehydrate"] is True
    assert body["enforcer_running"] is False
    assert body["last_enforcement"] is None
    # Reading status removed nothing.
    assert (tmp_path / "stems" / "indexed").exists()


@pytest.mark.requirement("STEM-43")
def test_status_route_is_healthy_on_a_roomy_disk(
    tmp_path: Path, two_bundles, monkeypatch: pytest.MonkeyPatch
):
    """[if] the volume is roomy [then] the status route reads healthy, [else stop].

    The opposite-direction control: the same app on a roomy volume must
    NOT report low disk, so a status route stuck on ``low_disk`` is caught.
    """
    _set_disk(monkeypatch, free=300 * GIB)
    body = TestClient(_app(tmp_path, armed=True)).get("/api/v1/stems/cache/status").json()
    assert body["state"] == "healthy"
    assert body["shortfall_bytes"] == 0
    assert body["blocked_reason"] is None


@pytest.mark.requirement("STEM-43")
def test_status_route_names_unarmed_hydration_as_the_blocker(
    tmp_path: Path, two_bundles, monkeypatch: pytest.MonkeyPatch
):
    """[if] no hydration source is armed on a low disk [then] status names hydration_not_armed, [else stop]."""
    _set_disk(monkeypatch, free=4 * GIB)
    body = TestClient(_app(tmp_path, armed=False)).get("/api/v1/stems/cache/status").json()
    assert body["can_rehydrate"] is False
    assert body["blocked_reason"] == stem_cache_budget.BLOCKED_HYDRATION_NOT_ARMED


@pytest.mark.requirement("STEM-43")
def test_status_route_fails_loud_on_malformed_settings(tmp_path: Path):
    """[if] the settings file is malformed [then] the status route answers 422 with the code, [else stop]."""
    path = stem_cache_budget.settings_path(tmp_path / "data")
    path.parent.mkdir(parents=True)
    path.write_text('{"floor_gib": "lots"}', encoding="utf-8")
    response = TestClient(_app(tmp_path, armed=True)).get("/api/v1/stems/cache/status")
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "STEM_CACHE_SETTINGS_INVALID"


# --- enforce -----------------------------------------------------------------------


@pytest.mark.requirement("STEM-40")
def test_enforce_route_evicts_the_indexed_bundle_and_keeps_the_local_only_one(
    tmp_path: Path, two_bundles, monkeypatch: pytest.MonkeyPatch
):
    """[if] enforce runs on a low disk [then] the indexed bundle goes and the local-only one stays, [else stop]."""
    _set_disk(monkeypatch, free=0)
    client = TestClient(_app(tmp_path, armed=True))

    dry = client.post("/api/v1/stems/cache/enforce", json={"dry_run": True}).json()
    assert dry["evicted_stable_ids"] == ["indexed"]
    assert (tmp_path / "stems" / "indexed").exists()

    real = client.post("/api/v1/stems/cache/enforce", json={}).json()
    assert real["evicted_stable_ids"] == ["indexed"]
    assert real["queued_for_upload"] == ["local-only"]
    assert not (tmp_path / "stems" / "indexed").exists()
    assert (tmp_path / "stems" / "local-only").exists()


@pytest.mark.requirement("STEM-41")
def test_enforce_route_evicts_nothing_when_hydration_is_not_armed(
    tmp_path: Path, two_bundles, monkeypatch: pytest.MonkeyPatch
):
    """[if] hydration is not armed [then] the enforce route removes nothing, [else stop]."""
    _set_disk(monkeypatch, free=0)
    body = (
        TestClient(_app(tmp_path, armed=False))
        .post("/api/v1/stems/cache/enforce", json={})
        .json()
    )
    assert body["evicted_stable_ids"] == []
    assert body["blocked_reason"] == stem_cache_budget.BLOCKED_HYDRATION_NOT_ARMED
    assert (tmp_path / "stems" / "indexed").exists()


@pytest.mark.requirement("STEM-42")
def test_enforce_route_never_evicts_a_bundle_loaded_on_a_deck(
    tmp_path: Path, two_bundles, monkeypatch: pytest.MonkeyPatch
):
    """[if] a bundle is open on a deck [then] the enforce route leaves it on disk, [else stop].

    The route reads the ENGINE's own open-deck registry, so a deck opened
    through the stems routes protects its bundle here with no extra wiring.
    """
    _set_disk(monkeypatch, free=0)
    OPEN_DECKS.mark_open("indexed")
    try:
        body = (
            TestClient(_app(tmp_path, armed=True))
            .post("/api/v1/stems/cache/enforce", json={})
            .json()
        )
    finally:
        OPEN_DECKS.mark_closed("indexed")
    assert body["evicted_stable_ids"] == []
    assert body["protected_count"] == 1
    assert (tmp_path / "stems" / "indexed").exists()


# --- settings ----------------------------------------------------------------------


@pytest.mark.requirement("STEM-43")
def test_settings_route_round_trips_a_partial_override(tmp_path: Path):
    """[if] a partial settings update is PUT [then] only that field changes and it reads back, [else stop]."""
    client = TestClient(_app(tmp_path, armed=True))
    assert client.get("/api/v1/stems/cache/settings").json() == {
        "floor_gib": 20.0,
        "floor_fraction": 0.05,
        "max_cache_gib": None,
        "enforce_interval_s": 300.0,
        "auto_evict": True,
    }

    updated = client.put(
        "/api/v1/stems/cache/settings", json={"floor_gib": 60, "max_cache_gib": 20}
    )
    assert updated.status_code == 200
    assert updated.json()["floor_gib"] == 60.0
    assert updated.json()["max_cache_gib"] == 20.0
    # Untouched fields keep their value, and the change is on disk.
    assert updated.json()["floor_fraction"] == 0.05
    assert stem_cache_budget.load_settings(tmp_path / "data") == StemCacheSettings(
        floor_gib=60.0, max_cache_gib=20.0
    )

    cleared = client.put("/api/v1/stems/cache/settings", json={"clear_max_cache_gib": True})
    assert cleared.json()["max_cache_gib"] is None
    assert cleared.json()["floor_gib"] == 60.0


@pytest.mark.requirement("STEM-43")
@pytest.mark.parametrize(
    "payload", [{"floor_fraction": 1.5}, {"floor_gib": -1}, {"enforce_interval_s": 0}]
)
def test_settings_route_rejects_a_bad_value_and_saves_nothing(
    tmp_path: Path, payload: dict[str, float]
):
    """[if] a settings value is invalid [then] the route answers 422 and stores nothing, [else stop]."""
    client = TestClient(_app(tmp_path, armed=True))
    response = client.put("/api/v1/stems/cache/settings", json=payload)
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "STEM_CACHE_SETTINGS_INVALID"
    assert not stem_cache_budget.settings_path(tmp_path / "data").exists()


@pytest.mark.requirement("STEM-43")
def test_settings_route_rejects_an_unknown_key(tmp_path: Path):
    """[if] a settings update names an unknown key [then] the route rejects it, [else stop]."""
    response = TestClient(_app(tmp_path, armed=True)).put(
        "/api/v1/stems/cache/settings", json={"floor_gb": 30}
    )
    assert response.status_code == 422


# --- the engine timer --------------------------------------------------------------


@pytest.mark.requirement("STEM-39")
def test_enforcer_tick_evicts_on_low_disk_and_status_shows_the_last_run(
    tmp_path: Path, two_bundles, monkeypatch: pytest.MonkeyPatch
):
    """[if] a tick runs on a low disk [then] it evicts and status carries that run, [else stop]."""
    _set_disk(monkeypatch, free=0)
    app = _app(tmp_path, armed=True)
    enforcer = StemCacheEnforcer(app)
    app.state.stem_cache_enforcer = enforcer

    report = enforcer.tick()

    assert report is not None
    assert report.evicted_stable_ids == ("indexed",)
    assert (tmp_path / "stems" / "local-only").exists()
    body = TestClient(app).get("/api/v1/stems/cache/status").json()
    assert body["last_enforcement"]["evicted_stable_ids"] == ["indexed"]
    assert body["upload_queue_count"] == 1
    assert body["last_error"] is None


@pytest.mark.requirement("STEM-41")
def test_enforcer_tick_leaves_a_healthy_disk_alone(
    tmp_path: Path, two_bundles, monkeypatch: pytest.MonkeyPatch
):
    """[if] a tick runs on a healthy disk [then] nothing is removed, [else stop]."""
    _set_disk(monkeypatch, free=300 * GIB)
    report = StemCacheEnforcer(_app(tmp_path, armed=True)).tick()
    assert report is not None
    assert report.state == "healthy"
    assert report.evicted_stable_ids == ()
    assert (tmp_path / "stems" / "indexed").exists()


@pytest.mark.requirement("STEM-43")
def test_enforcer_tick_records_a_failure_instead_of_dying(tmp_path: Path, two_bundles):
    """[if] the settings file is corrupt [then] the tick reports it through ``last_error`` and evicts nothing, [else stop]."""
    path = stem_cache_budget.settings_path(tmp_path / "data")
    path.write_text("{not json", encoding="utf-8")
    enforcer = StemCacheEnforcer(_app(tmp_path, armed=True))

    assert enforcer.tick() is None
    assert enforcer.last_error is not None
    assert "StemCacheSettingsError" in enforcer.last_error
    assert (tmp_path / "stems" / "indexed").exists()


@pytest.mark.requirement("STEM-39")
def test_enforcer_thread_ticks_on_its_own_and_stops_cleanly(
    tmp_path: Path, two_bundles, monkeypatch: pytest.MonkeyPatch
):
    """[if] the loop is started [then] it enforces without being asked, and ``stop`` ends the thread, [else stop]."""
    _set_disk(monkeypatch, free=0)
    stem_cache_budget.save_settings(
        tmp_path / "data", StemCacheSettings(enforce_interval_s=0.05)
    )
    enforcer = StemCacheEnforcer(_app(tmp_path, armed=True), first_tick_delay_s=0.0)
    enforcer.start()
    try:
        deadline = time.monotonic() + 10.0
        while (tmp_path / "stems" / "indexed").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not (tmp_path / "stems" / "indexed").exists()
        assert enforcer.running
    finally:
        enforcer.stop()
    assert not enforcer.running
    assert (tmp_path / "stems" / "local-only").exists()


# --- CLI parity ----------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def live_engine(tmp_path: Path, two_bundles) -> Iterator[str]:
    """A real uvicorn server on the routes under test: the CLI talks real
    HTTP to it, exactly as it does to the engine."""
    port = _free_port()
    config = uvicorn.Config(
        _app(tmp_path, armed=True), host="127.0.0.1", port=port, log_level="warning"
    )
    server, thread = start_uvicorn_in_thread(config, what="the stem cache test engine")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10.0)


@pytest.mark.requirement("STEM-43")
def test_cli_cache_status_matches_the_route_and_gates_on_low_disk(
    live_engine: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    """[if] cache-status runs on a low disk [then] it prints the route payload and exits 3 when gated, [else stop]."""
    _set_disk(monkeypatch, free=4 * GIB)

    assert stems_cli_main(["cache-status", "--engine-url", live_engine]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["state"] == "low_disk"
    assert printed["local_only_stable_ids"] == ["local-only"]

    assert (
        stems_cli_main(["cache-status", "--engine-url", live_engine, "--fail-on-low-disk"])
        == EXIT_LOW_DISK
    )
    capsys.readouterr()

    _set_disk(monkeypatch, free=300 * GIB)
    assert (
        stems_cli_main(["cache-status", "--engine-url", live_engine, "--fail-on-low-disk"])
        == 0
    )


@pytest.mark.requirement("STEM-43")
def test_cli_cache_enforce_and_settings_drive_the_engine(
    tmp_path: Path,
    live_engine: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    """[if] cache-enforce or cache-settings runs [then] the running engine performs it, [else stop]."""
    _set_disk(monkeypatch, free=0)

    assert stems_cli_main(["cache-enforce", "--engine-url", live_engine, "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["dry_run"] is True
    assert (tmp_path / "stems" / "indexed").exists()

    assert stems_cli_main(["cache-enforce", "--engine-url", live_engine]) == 0
    assert json.loads(capsys.readouterr().out)["evicted_stable_ids"] == ["indexed"]
    assert not (tmp_path / "stems" / "indexed").exists()
    assert (tmp_path / "stems" / "local-only").exists()

    assert (
        stems_cli_main(
            ["cache-settings", "--engine-url", live_engine, "--floor-gib", "55",
             "--auto-evict", "off"]
        )
        == 0
    )
    written = json.loads(capsys.readouterr().out)
    assert written["floor_gib"] == 55.0 and written["auto_evict"] is False

    assert stems_cli_main(["cache-settings", "--engine-url", live_engine]) == 0
    assert json.loads(capsys.readouterr().out) == written


@pytest.mark.requirement("STEM-43")
def test_cli_refuses_to_act_without_an_engine(capsys: pytest.CaptureFixture[str]):
    """[if] no engine answers [then] the verb exits non-zero naming the URL, never enforcing in-process where it cannot see the decks, [else stop]."""
    dead = f"http://127.0.0.1:{_free_port()}"
    with pytest.raises(SystemExit) as raised:
        stems_cli_main(["cache-enforce", "--engine-url", dead])
    assert "engine not reachable" in str(raised.value)
    assert dead in str(raised.value)


# --- the real app: armed with hydration, started and stopped by the lifespan -----


@pytest.mark.requirement("STEM-39")
def test_real_app_lifespan_runs_the_enforcer_only_when_hydration_is_armed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """[if] the daemon arms stem hydration [then] the lifespan starts and stops the enforcer thread, [else stop].

    An app that did not arm it (every other test app) gets no thread.
    """
    from apps.webui.server.app import create_app

    monkeypatch.setenv("MUSIC_DJ_CLOUDSYNC_MODE", "local")
    state_db = tmp_path / "data" / "state" / "state.db"
    state_db.parent.mkdir(parents=True)

    armed = create_app(state_db_path=str(state_db), mount_frontend=False, stem_hydration=True)
    with TestClient(armed) as client:
        enforcer = armed.state.stem_cache_enforcer
        assert enforcer.running
        body = client.get("/api/v1/stems/cache/status").json()
        assert body["enforcer_running"] is True
        assert body["can_rehydrate"] is False
        assert body["stems_dir"]
    assert not enforcer.running

    plain = create_app(state_db_path=str(state_db), mount_frontend=False)
    with TestClient(plain) as client:
        assert getattr(plain.state, "stem_cache_enforcer", None) is None
        assert client.get("/api/v1/stems/cache/status").json()["enforcer_running"] is False
