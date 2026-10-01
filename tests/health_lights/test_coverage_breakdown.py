"""GET /ingest/coverage: honest denominator, terminal states, fast path.

Regression lines:
  - if coverage quotes a denominator other than `present` then broken
  - if a track with no lyrics available still counts as pending then broken
  - if a never-tried track counts as terminal then broken
  - if a stem bundle is re-validated on every request then broken
  - if a bundle that changed on disk keeps its cached verdict then broken
  - if reconcile summary and coverage disagree on the playable count then broken
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.webui.server import coverage_outcomes, rb_vendor
from apps.webui.server.app import create_app
from apps.webui.server.routes import ingest as ingest_mod
from apps.webui.server.routes import ingest_job
from apps.webui.server.sqlite_backend import SqliteBackend
from tests.health_lights import fixtures as fx
from tests.health_lights.conftest import Library

STEPS = ("vocals", "stems", "lyrics")


def _coverage(library: Library) -> dict:
    response = library.client.get("/api/v1/ingest/coverage")
    assert response.status_code == 200, response.text
    return response.json()


def _seed_present(library: Library, stable_id: str) -> Path:
    audio = fx.audio_file(library.music, f"{stable_id}.mp3")
    fx.seed_track(library.state_db, stable_id, str(audio))
    return audio


@pytest.mark.requirement("HEALTH-03")
def test_every_step_partitions_the_present_denominator(library: Library) -> None:
    """[if] a coverage step does not partition the present denominator [then] fail, [else stop]."""
    for stable_id in ("a", "b", "c"):
        _seed_present(library, stable_id)
    fx.seed_track(library.state_db, "elsewhere", "/Users/someone-else/Music/x.mp3")
    fx.write_lyrics(library.lyrics_cache, "a")

    out = _coverage(library)

    assert out["on_disk"] == 3
    assert out["availability"]["present"] == 3
    assert out["availability"]["off_machine"] == 1
    assert out["availability"]["broken_here"] == 0
    for step in STEPS:
        parts = out["done"][step] + out["terminal"][step] + out["failed"][step] + out["pending"][step]
        assert parts == out["on_disk"], step
    assert out["done"]["lyrics"] == 1
    assert out["pending"]["lyrics"] == 2


@pytest.mark.requirement("HEALTH-04")
def test_no_lyrics_available_is_terminal_not_pending(library: Library) -> None:
    """[if] a track with no lyrics available still counts as pending [then] fail, [else stop]."""
    for stable_id in ("sung", "instrumental", "untried"):
        _seed_present(library, stable_id)
    fx.write_lyrics(library.lyrics_cache, "sung")
    fx.write_fetch_verdict(library.data_dir, "instrumental", "instrumental")

    out = _coverage(library)

    assert out["done"]["lyrics"] == 1
    assert out["terminal"]["lyrics"] == 1
    # Overshoot control: a track nobody has tried yet is NOT terminal.
    assert out["pending"]["lyrics"] == 1
    # `missing` keeps its artifact meaning for the refresh job's targeting.
    assert out["missing"]["lyrics"] == 2


@pytest.mark.requirement("HEALTH-04")
def test_no_stems_source_is_terminal_and_carries_vocals_with_it(library: Library) -> None:
    """[if] a no-source stems verdict is not terminal for stems and vocals [then] fail, [else stop]."""
    audio = _seed_present(library, "unstemmable")
    _seed_present(library, "untried")
    store = coverage_outcomes.OutcomeStore(coverage_outcomes.store_path(library.data_dir))
    store.record_no_source(
        "stems", "unstemmable", coverage_outcomes.audio_token(audio), "farm rejected the file", now=1.0
    )

    out = _coverage(library)

    assert out["terminal"]["stems"] == 1
    assert out["pending"]["stems"] == 1
    assert out["terminal"]["vocals"] == 1   # no stems to derive vocals from
    assert out["pending"]["vocals"] == 1
    assert out["waiting_on_stems"] == 1


@pytest.mark.requirement("HEALTH-04")
def test_a_recorded_no_source_expires_when_the_audio_changes(library: Library) -> None:
    """[if] a recorded no-source verdict survives an audio change [then] fail, [else stop]."""
    audio = _seed_present(library, "replaced")
    store = coverage_outcomes.OutcomeStore(coverage_outcomes.store_path(library.data_dir))
    store.record_no_source("stems", "replaced", coverage_outcomes.audio_token(audio), "bad file", now=1.0)
    assert _coverage(library)["terminal"]["stems"] == 1
    audio.write_bytes(b"y" * 9000)

    assert _coverage(library)["terminal"]["stems"] == 0


@pytest.mark.requirement("HEALTH-04")
def test_no_executor_anywhere_makes_missing_stems_terminal(library: Library) -> None:
    """[if] missing stems stay pending with no executor anywhere [then] fail, [else stop]."""
    _seed_present(library, "a")
    library.app.state.stems_source_refusal_fn = lambda: "local stems are off for this build"

    out = _coverage(library)

    assert out["terminal"]["stems"] == 1
    assert out["pending"]["stems"] == 0
    assert out["stems_source_refusal"] == "local stems are off for this build"


@pytest.mark.requirement("HEALTH-03")
def test_stem_bundles_are_validated_once_until_they_change(library: Library) -> None:
    """[if] a stem bundle is re-validated on every request [then] fail, [else stop]."""
    for stable_id in ("a", "b"):
        _seed_present(library, stable_id)
        fx.write_stem_bundle(library.stems, stable_id)

    first = _coverage(library)
    misses_after_first = ingest_job.BUNDLE_CACHE.misses
    second = _coverage(library)

    assert first["done"]["stems"] == second["done"]["stems"] == 2
    assert misses_after_first == 2
    assert ingest_job.BUNDLE_CACHE.misses == 2       # nothing re-validated
    assert ingest_job.BUNDLE_CACHE.hits >= 2


@pytest.mark.requirement("HEALTH-03")
def test_a_changed_bundle_is_revalidated(library: Library) -> None:
    """Overshoot control: the cache must not freeze a verdict past a change.

    [if] a bundle that changed on disk keeps its cached verdict [then] fail, [else stop].
    """
    _seed_present(library, "a")
    bundle = fx.write_stem_bundle(library.stems, "a")
    assert _coverage(library)["done"]["stems"] == 1

    (bundle / "vocals.wav").unlink()          # a part vanishes, manifest untouched
    lost_part = _coverage(library)
    assert lost_part["done"]["stems"] == 0
    assert lost_part["corrupt"]["stems"] == 1

    fx.write_stem_bundle(library.stems, "b")
    _seed_present(library, "b")
    assert _coverage(library)["done"]["stems"] == 1   # a new bundle is seen


@pytest.mark.requirement("HEALTH-02")
def test_reconcile_summary_and_coverage_agree_on_playable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] reconcile summary and coverage disagree on the playable count [then] fail, [else stop]."""
    data_dir = tmp_path / "data"
    state_db = fx.make_state_db(data_dir)
    music = tmp_path / "music"
    for stable_id in ("a", "b", "c"):
        fx.seed_track(state_db, stable_id, str(fx.audio_file(music, f"{stable_id}.mp3")))
    lost = fx.audio_file(music, "lost.mp3")
    fx.seed_track(state_db, "lost", str(lost))
    fx.claim_here(state_db, "lost", lost)
    lost.unlink()
    fx.seed_track(state_db, "elsewhere", "/Users/someone-else/Music/x.mp3")
    fx.seed_track(state_db, "stream", "tidal:1")
    monkeypatch.setenv("MDT_DATA_DIR", str(data_dir))
    monkeypatch.setattr(rb_vendor, "bulk_rb_meta", lambda stable_ids: {})
    monkeypatch.setattr(ingest_mod, "open_ro", lambda: sqlite3.connect(state_db))
    monkeypatch.setattr(ingest_mod, "COVERAGE_DATA_DIR", data_dir)
    monkeypatch.setattr(ingest_mod, "VOCAL_CACHE_DIR", data_dir / "state" / "vocal-cache")
    monkeypatch.setattr(ingest_mod, "LYRICS_CACHE_DIR", data_dir / "state" / "lyrics-cache")
    app = create_app(
        backend=SqliteBackend(state_db),
        bind_host="127.0.0.1",
        hostname="test-host",
        lock_status_fn=lambda: None,
        mount_frontend=False,
        state_db_path=str(state_db),
        stem_roots=(data_dir / "state" / "stems",),
    )
    app.state.stems_source_refusal_fn = lambda: None
    with TestClient(app) as client:
        summary = client.get("/api/v1/reconcile/summary").json()
        coverage = client.get("/api/v1/ingest/coverage").json()

    assert summary["availability"] == coverage["availability"]
    assert summary["availability"]["present"] == coverage["on_disk"] == 3
    assert summary["availability"]["broken_here"] == 1
    assert summary["availability"]["off_machine"] == 1
    assert summary["availability"]["streaming"] == 1
    assert summary["availability"]["total"] == summary["total_tracks"] == 6
