"""The auto-drain driver: run missing vocals/lyrics work until the lights are green.

Regression lines:
  - if the drain runs a job while a deck is playing then broken
  - if the drain runs anything when coverage is already green then broken
  - if the drain runs more than one job per tick then broken
  - if lyrics run while vocals work is still outstanding then broken
  - if the drain ever starts a stems job locally then broken
  - if an unchanged failure is retried in a loop then broken
  - if a job that reports success without producing its artifact is retried forever then broken
"""
from __future__ import annotations

from pathlib import Path

import pytest

from apps.lyrics.service import LyricsFetchService, Track
from apps.webui.server import coverage_drain as cd
from apps.webui.server import coverage_outcomes as co
from apps.webui.server.routes import ingest as ingest_mod
from tests.health_lights import fixtures as fx
from tests.health_lights.conftest import Library

pytestmark = pytest.mark.requirement("HEALTH-05")

LRC = "[00:01.00]first line\n[00:05.00]second line\n"


class Lrclib:
    """The lyrics service's own provider seam (the network, not app behavior)."""

    def __init__(self, synced: dict[str, str | None]) -> None:
        self.synced = synced
        self.calls: list[str] = []

    def fetch_synced(self, track: Track) -> str | None:
        self.calls.append(track.stable_id)
        return self.synced.get(track.stable_id)


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


class Rig:
    """A real drain over the real snapshot, with recording job functions."""

    def __init__(self, library: Library, lrclib: Lrclib | None = None) -> None:
        self.library = library
        self.playing = False
        self.clock = Clock()
        self.vocals_runs: list[str] = []
        self.lyrics_runs: list[str] = []
        self.vocals_fails = False
        self.vocals_writes = True
        service = LyricsFetchService(library.data_dir, provider=lrclib or Lrclib({}))
        self.outcomes = co.OutcomeStore(co.store_path(library.data_dir))
        self.drain = cd.CoverageDrain(
            snapshot_fn=lambda: ingest_mod.build_snapshot(library.app),
            jobs={"vocals": self._vocals, "lyrics": cd.lyrics_job(service, self.lyrics_runs)},
            playing_fn=lambda: self.playing,
            outcomes=self.outcomes,
            config=cd.DrainConfig(cd.config_path(library.data_dir)),
            clock=self.clock,
        )

    def _vocals(self, stable_id: str, audio_path: str) -> None:
        self.vocals_runs.append(stable_id)
        if self.vocals_fails:
            raise RuntimeError("stems unreadable")
        if self.vocals_writes:
            fx.write_vocals(self.library.vocal_cache, stable_id, Path(audio_path))


def _present(library: Library, stable_id: str, *, stems: bool = False) -> Path:
    audio = fx.audio_file(library.music, f"{stable_id}.mp3")
    fx.seed_track(library.state_db, stable_id, str(audio))
    if stems:
        fx.write_stem_bundle(library.stems, stable_id)
    return audio


def _fully_covered(library: Library, stable_id: str) -> None:
    audio = _present(library, stable_id, stems=True)
    fx.write_vocals(library.vocal_cache, stable_id, audio)
    fx.write_lyrics(library.lyrics_cache, stable_id)


def test_drain_does_nothing_when_already_green(library: Library) -> None:
    _fully_covered(library, "a")
    rig = Rig(library)

    assert rig.drain.tick() == "green"
    assert rig.drain.tick() == "green"

    assert rig.vocals_runs == [] and rig.lyrics_runs == []
    assert rig.drain.status().state == "green"


def test_drain_does_not_start_during_playback(library: Library) -> None:
    _present(library, "a", stems=True)
    rig = Rig(library, Lrclib({"a": LRC}))
    rig.playing = True

    assert rig.drain.tick() == "paused_playing"
    assert rig.drain.tick() == "paused_playing"
    assert rig.vocals_runs == [] and rig.lyrics_runs == []
    assert rig.drain.status().state == "paused_playing"

    # Control: the same library DOES drain once the deck stops, so the two
    # empty lists above are the pause, not a drain that could never run.
    rig.playing = False
    assert rig.drain.tick() == "ran:vocals"
    assert rig.vocals_runs == ["a"]


def test_drain_pauses_between_jobs_when_playback_starts(library: Library) -> None:
    for stable_id in ("a", "b"):
        _present(library, stable_id, stems=True)
    rig = Rig(library)

    assert rig.drain.tick() == "ran:vocals"
    rig.playing = True
    assert rig.drain.tick() == "paused_playing"
    assert len(rig.vocals_runs) == 1


def test_one_job_per_tick_vocals_before_lyrics_until_green(library: Library) -> None:
    for stable_id in ("a", "b"):
        _present(library, stable_id, stems=True)
    rig = Rig(library, Lrclib({"a": LRC, "b": None}))

    outcomes = [rig.drain.tick() for _ in range(5)]

    assert outcomes == ["ran:vocals", "ran:vocals", "ran:lyrics", "ran:lyrics", "green"]
    assert rig.vocals_runs == ["a", "b"]
    assert rig.lyrics_runs == ["a", "b"]
    coverage = library.client.get("/api/v1/ingest/coverage").json()
    assert coverage["pending"] == {"analysis": 2, "stems": 0, "vocals": 0, "lyrics": 0}
    # "b" had no lyrics anywhere: recorded as terminal, not left pending.
    assert coverage["done"]["lyrics"] == 1
    assert coverage["terminal"]["lyrics"] == 1


def test_stems_are_reported_for_the_farm_never_run_locally(library: Library) -> None:
    _present(library, "needs-farm")
    fx.write_lyrics(library.lyrics_cache, "needs-farm")
    rig = Rig(library)

    assert rig.drain.tick() == "blocked"

    status = rig.drain.status()
    assert status.state == "blocked"
    assert status.stems_needing_farm == ["needs-farm"]
    assert rig.vocals_runs == [] and rig.lyrics_runs == []
    assert "stems" not in rig.drain.job_steps


def test_an_unchanged_failure_backs_off_then_goes_terminal(library: Library) -> None:
    audio = _present(library, "a", stems=True)
    fx.write_lyrics(library.lyrics_cache, "a")
    rig = Rig(library)
    rig.vocals_fails = True

    assert rig.drain.tick() == "failed:vocals"
    assert rig.drain.tick() == "blocked"            # inside the backoff: no retry
    assert rig.vocals_runs == ["a"]

    rig.clock.now += co.BACKOFF_BASE_S
    assert rig.drain.tick() == "failed:vocals"
    rig.clock.now += co.BACKOFF_BASE_S * 2
    assert rig.drain.tick() == "failed:vocals"
    assert rig.vocals_runs == ["a", "a", "a"]

    rig.clock.now += 10**9                          # terminal: time does not re-arm it
    assert rig.drain.tick() == "blocked"
    assert rig.vocals_runs == ["a", "a", "a"]
    coverage = library.client.get("/api/v1/ingest/coverage").json()
    assert coverage["failed"]["vocals"] == 1
    assert coverage["pending"]["vocals"] == 0

    # A changed audio file is a changed failure: the track is tried again.
    audio.write_bytes(b"z" * 8192)
    rig.vocals_fails = False
    assert rig.drain.tick() == "ran:vocals"


def test_a_job_that_succeeds_without_an_artifact_is_a_failure(library: Library) -> None:
    _present(library, "a", stems=True)
    fx.write_lyrics(library.lyrics_cache, "a")
    rig = Rig(library)
    rig.vocals_writes = False

    assert rig.drain.tick() == "failed:vocals"
    assert rig.outcomes.load()[("vocals", "a")].kind == "failed"


def test_stop_and_start_are_honored(library: Library) -> None:
    _present(library, "a", stems=True)
    rig = Rig(library)

    rig.drain.request_stop()
    assert rig.drain.tick() == "stopped"
    assert rig.vocals_runs == []
    rig.drain.request_start()
    assert rig.drain.tick() == "ran:vocals"


def test_the_setting_defaults_on_and_turns_the_drain_off(library: Library) -> None:
    _present(library, "a", stems=True)
    rig = Rig(library)
    assert rig.drain.status().enabled is True

    rig.drain.set_enabled(False)
    assert rig.drain.tick() == "disabled"
    assert rig.vocals_runs == []
    assert cd.DrainConfig(cd.config_path(library.data_dir)).enabled() is False


def test_http_parity_start_stop_status_config(library: Library) -> None:
    _present(library, "a", stems=True)
    rig = Rig(library)
    library.app.state.coverage_drain = rig.drain
    client = library.client

    status = client.get("/api/v1/coverage-drain/status").json()
    assert status["enabled"] is True and status["state"] == "idle"

    assert client.post("/api/v1/coverage-drain/stop").json()["state"] == "stopped"
    assert rig.drain.tick() == "stopped"
    assert client.post("/api/v1/coverage-drain/start").json()["state"] == "idle"
    assert rig.drain.tick() == "ran:vocals"
    assert client.get("/api/v1/coverage-drain/status").json()["last_job"]["step"] == "vocals"

    off = client.put("/api/v1/coverage-drain/config", json={"enabled": False}).json()
    assert off["enabled"] is False
    assert rig.drain.tick() == "disabled"


def test_http_reports_an_unarmed_drain_as_unarmed_not_idle(library: Library) -> None:
    response = library.client.get("/api/v1/coverage-drain/status")
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "COVERAGE_DRAIN_NOT_ARMED"


def test_an_armed_engine_app_runs_the_real_drain_and_stops_it_on_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Smoke: the wiring, not the driver. Real create_app, real lifespan, real loop."""
    import sqlite3
    import threading
    import time

    from fastapi.testclient import TestClient

    from apps.webui.server.app import create_app
    from apps.webui.server.sqlite_backend import SqliteBackend

    data_dir = tmp_path / "data"
    state_db = fx.make_state_db(data_dir)
    audio = fx.audio_file(tmp_path / "music", "a.mp3")
    fx.seed_track(state_db, "a", str(audio))
    fx.write_stem_bundle(data_dir / "state" / "stems", "a")
    fx.write_vocals(data_dir / "state" / "vocal-cache", "a", audio)
    fx.write_lyrics(data_dir / "state" / "lyrics-cache", "a")
    fx.write_analysis_row(state_db, "a")     # the armed drain owns analysis too
    monkeypatch.setenv("MDT_DATA_DIR", str(data_dir))
    monkeypatch.setattr(ingest_mod, "open_ro", lambda: sqlite3.connect(state_db))
    monkeypatch.setattr(ingest_mod, "COVERAGE_DATA_DIR", data_dir)
    monkeypatch.setattr(ingest_mod, "VOCAL_CACHE_DIR", data_dir / "state" / "vocal-cache")
    monkeypatch.setattr(ingest_mod, "LYRICS_CACHE_DIR", data_dir / "state" / "lyrics-cache")
    monkeypatch.setattr(cd, "ACTIVE_INTERVAL_S", 0.05)
    app = create_app(
        backend=SqliteBackend(state_db),
        bind_host="127.0.0.1",
        hostname="test-host",
        lock_status_fn=lambda: None,
        mount_frontend=False,
        state_db_path=str(state_db),
        stem_roots=(data_dir / "state" / "stems",),
        auto_coverage_drain=True,
    )
    app.state.stems_source_refusal_fn = lambda: None
    with TestClient(app) as client:
        deadline = time.monotonic() + 20.0
        status = client.get("/api/v1/coverage-drain/status").json()
        while status["ticks"] == 0 and time.monotonic() < deadline:
            time.sleep(0.05)
            status = client.get("/api/v1/coverage-drain/status").json()
        assert status["ticks"] >= 1, status
        assert status["state"] == "green", status
        assert status["jobs_run"] == 0
        assert any(thread.name == cd.THREAD_NAME for thread in threading.enumerate())
    assert not any(thread.name == cd.THREAD_NAME for thread in threading.enumerate())


def test_an_unarmed_app_builds_no_drain(tmp_path: Path) -> None:
    """Control for the smoke above: create_app defaults to no drain thread."""
    import threading

    from fastapi.testclient import TestClient

    from apps.webui.server.app import create_app
    from apps.webui.server.sqlite_backend import SqliteBackend

    state_db = fx.make_state_db(tmp_path / "data")
    app = create_app(
        backend=SqliteBackend(state_db),
        bind_host="127.0.0.1",
        hostname="test-host",
        lock_status_fn=lambda: None,
        mount_frontend=False,
        state_db_path=str(state_db),
    )
    with TestClient(app) as client:
        assert client.get("/api/v1/coverage-drain/status").status_code == 503
        assert not any(thread.name == cd.THREAD_NAME for thread in threading.enumerate())


def test_arm_from_environ_is_a_fail_fast_enum() -> None:
    assert cd.arm_from_environ({}) is True
    assert cd.arm_from_environ({cd.COVERAGE_DRAIN_ENV: "off"}) is False
    with pytest.raises(ValueError, match="not a member"):
        cd.arm_from_environ({cd.COVERAGE_DRAIN_ENV: "maybe"})


def test_cli_verbs_map_to_the_http_surface() -> None:
    from apps.webui import coverage_drain_cli as cli

    assert cli.request_for("status") == ("GET", "/api/v1/coverage-drain/status", None)
    assert cli.request_for("start") == ("POST", "/api/v1/coverage-drain/start", None)
    assert cli.request_for("stop") == ("POST", "/api/v1/coverage-drain/stop", None)
    assert cli.request_for("enable") == ("PUT", "/api/v1/coverage-drain/config", {"enabled": True})
    assert cli.request_for("disable") == ("PUT", "/api/v1/coverage-drain/config", {"enabled": False})
    assert cli.request_for("retry") == ("POST", "/api/v1/coverage-drain/retry", None)
    with pytest.raises(SystemExit):
        cli.main(["frobnicate"])
