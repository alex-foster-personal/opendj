"""Edge sampler warm clip cache: a bounded LRU, per-clip render locks and the deck-playing render policy.

[if] the clip cache grows unbounded, renders a clip twice, or bulk renders while a deck plays [then] these fail, [else stop].

Every render is a real ffmpeg render; the spy only counts calls and how many run at once.

Regression one-liners:
  - if the clip cache passes its budget and keeps the least recently served clip, or drops the newest, then broken
  - if a deck is playing and prerender_clips renders anything then broken
  - if three new clips asked for while a deck plays ever render two at once, or at normal priority, then broken
  - if four concurrent first requests for one clip run ffmpeg more than once, or answer different bytes, then broken
  - if a seeking render of a wav or flac region is not sample-aligned (within 1 LSB) with a full decode then broken
  - if an m4a source is rendered with an input seek then broken (AAC is not seek-exact)
  - if serving a cached clip builds the catalogue then broken
"""

from __future__ import annotations

import subprocess
import threading
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from apps.shared.ffmpeg import resolve_ffmpeg
from apps.webui.server import sampler_clips
from apps.webui.server.routes import sampler
from tests.webui.sampler_support import GRID, make_app, make_env, wav
from tests.webui.test_sampler_clips import VECTOR

pytestmark = [pytest.mark.requires_ffmpeg]

PLAYING = {"decks": {"1": {"playing": True}, "2": {"playing": False}}}
STOPPED = {"decks": {"1": {"playing": False}, "2": {"playing": False}}}


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    spy = SimpleNamespace(renders=0, active=0, peak=0, low_priority=[], lock=threading.Lock())
    real = sampler_clips.render_clip

    def counted(source: Path, in_ms: int, out_ms: int, dest: Path, *, low_priority: bool) -> None:
        with spy.lock:
            spy.renders, spy.active = spy.renders + 1, spy.active + 1
            spy.peak = max(spy.peak, spy.active)
            spy.low_priority.append(low_priority)
        try:
            real(source, in_ms, out_ms, dest, low_priority=low_priority)
        finally:
            with spy.lock:
                spy.active -= 1

    monkeypatch.setattr(sampler_clips, "render_clip", counted)  # a spy: the real ffmpeg render still runs
    for base in make_env(tmp_path, monkeypatch):
        app = make_app(base.state, base.sources)
        app.include_router(sampler.router, prefix="/api/v1")
        with TestClient(app) as client:
            yield SimpleNamespace(
                **{**vars(base), "request": Request({"type": "http", "app": app, "headers": []})},
                client=client,
                spy=spy,
            )


def _capture(env: SimpleNamespace, in_ms: int) -> dict[str, Any]:
    body = {"stable_id": GRID, "in_ms": in_ms, "out_ms": in_ms + 2000, "beats": 4, "vector": VECTOR}
    r = env.client.post("/api/v1/sampler/slots", json={**body, "set_id": "L1", "index": 0})
    assert r.status_code == 201, r.text
    return dict(r.json())


def _clip(env: SimpleNamespace, slot: dict[str, Any]) -> bytes:
    r = env.client.get(f"/api/v1/sampler/clips/{slot['slot_id']}")
    assert (r.status_code, r.content[:4]) == (200, b"fLaC"), r.text
    return r.content


def _cache_files(env: SimpleNamespace) -> set[Path]:
    return set((env.state.parent / sampler_clips.CLIP_CACHE_DIRNAME).rglob("*.flac"))


def _get_all(env: SimpleNamespace, slots: list[dict[str, Any]]) -> list[bytes]:
    bodies: list[bytes] = [b""] * len(slots)

    def get(i: int) -> None:
        bodies[i] = _clip(env, slots[i])

    threads = [threading.Thread(target=get, args=(i,)) for i in range(len(slots))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return bodies


def test_if_the_cache_passes_its_budget_then_the_least_recently_served_clip_goes(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] a render takes the cache past its budget [then] the least recently served clip goes, the others stay, [else stop]."""
    a, b, c = (_capture(env, ms) for ms in (1000, 2000, 3000))
    _clip(env, a)
    (file_a,) = _cache_files(env)
    _clip(env, b)
    (file_b,) = _cache_files(env) - {file_a}
    _clip(env, a)  # a hit: A is now more recently served than B
    size = max(file_a.stat().st_size, file_b.stat().st_size)
    # A small cap instead of 2 GB: three clips pass it, two fit under the 90% sweep target.
    monkeypatch.setattr(sampler_clips, "CLIP_CACHE_BUDGET_BYTES", int(size * 2.6))
    _clip(env, c)
    left = _cache_files(env)
    assert (file_a in left, file_b in left, len(left), env.spy.renders) == (True, False, 2, 3)
    _clip(env, b)  # an evicted clip is rendered again on its next ask
    assert env.spy.renders == 4


def test_if_a_deck_is_playing_then_background_pre_render_renders_nothing(env: SimpleNamespace) -> None:
    """[if] the ui_mirror shows a deck playing [then] prerender_clips renders 0 clips; once stopped it renders both, [else stop]."""
    slots = [_capture(env, 1000), _capture(env, 3000)]
    env.client.app.state.ui_mirror = PLAYING
    held = sampler_clips.prerender_clips(env.request, slots)
    files_while_playing = _cache_files(env)
    env.client.app.state.ui_mirror = STOPPED
    rendered = sampler_clips.prerender_clips(env.request, slots)
    again = sampler_clips.prerender_clips(env.request, slots)
    assert (held, files_while_playing, rendered, len(_cache_files(env)), again) == (0, set(), 2, 2, 0)
    assert env.spy.low_priority == [True, True]


def test_if_a_deck_is_playing_then_on_demand_renders_run_one_at_a_time_at_low_priority(env: SimpleNamespace) -> None:
    """[if] three new clips are fetched at once while a deck plays [then] all render, never two at once, low priority, [else stop]."""
    slots = [_capture(env, ms) for ms in (1000, 2000, 3000)]
    env.client.app.state.ui_mirror = PLAYING
    _get_all(env, slots)  # a 220 Hz sine repeats every beat, so the three clips may hold the same samples
    assert (len(_cache_files(env)), env.spy.renders, env.spy.peak, env.spy.low_priority) == (3, 3, 1, [True] * 3)


def test_if_four_requests_race_for_one_new_clip_then_ffmpeg_runs_once(env: SimpleNamespace) -> None:
    """[if] four concurrent first requests ask for one clip [then] ffmpeg ran once and all four got the same flac, [else stop]."""
    slot = _capture(env, 1000)
    bodies = _get_all(env, [slot] * 4)
    assert (env.spy.renders, len(set(bodies)), bodies[0][:4], env.spy.low_priority) == (1, 1, b"fLaC", [False])


def _pcm(path: Path) -> np.ndarray:
    out = subprocess.run(
        [resolve_ffmpeg(), "-nostdin", "-v", "error", "-i", str(path), "-f", "s16le", "-"],
        capture_output=True,
        check=True,
    ).stdout
    return np.frombuffer(out, dtype=np.int16)


def _full_decode_reference(src: Path, in_ms: int, out_ms: int, dest: Path) -> None:
    a, b = (in_ms - sampler_clips.GUARD_MS) / 1000, (out_ms + sampler_clips.GUARD_MS) / 1000
    rate = str(sampler_clips.CLIP_SAMPLE_RATE)
    trim = f"atrim=start={a:.6f}:end={b:.6f},asetpts=PTS-STARTPTS"
    argv = [resolve_ffmpeg(), "-nostdin", "-v", "error", "-y", "-i", str(src), "-af", trim, "-ar", rate]
    subprocess.run([*argv, "-ac", "2", "-sample_fmt", "s16", "-f", "flac", str(dest)], check=True, capture_output=True)


@pytest.mark.parametrize("suffix", [".wav", ".flac"])
def test_if_a_late_region_is_rendered_with_seek_then_it_matches_a_full_decode(tmp_path: Path, suffix: str) -> None:
    """[if] a late wav or flac region renders with an input seek [then] it is sample-aligned with a full decode, [else stop]."""
    src = wav(tmp_path / "src.wav", seconds=40.0)
    if suffix == ".flac":
        flac = tmp_path / "src.flac"
        subprocess.run([resolve_ffmpeg(), "-nostdin", "-v", "error", "-i", str(src), str(flac)], check=True)
        src = flac
    argv = sampler_clips.render_argv(src, 30_000, 34_000, str(tmp_path / "seek.flac"))
    subprocess.run(argv, check=True, capture_output=True)
    _full_decode_reference(src, 30_000, 34_000, tmp_path / "ref.flac")
    got, ref = _pcm(tmp_path / "seek.flac"), _pcm(tmp_path / "ref.flac")
    # Same length and no lag; 44.1 -> 48 kHz resampling may round 1 LSB differently (about -90 dB).
    diff = int(np.abs(got.astype(np.int32) - ref.astype(np.int32)).max())
    assert ("-ss" in argv, len(got) == len(ref), diff <= 1) == (True, True, True), diff


def test_if_the_source_is_m4a_then_the_render_never_seeks(tmp_path: Path) -> None:
    """[if] the source is m4a (AAC, not seek-exact) [then] the render argv has no input seek, [else stop]."""
    assert "-ss" not in sampler_clips.render_argv(tmp_path / "x.m4a", 120_000, 124_000, "out.flac")


def test_if_a_cached_clip_is_served_then_no_catalogue_is_built(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] an already rendered clip is fetched again [then] it is served without building the catalogue, [else stop]."""
    slot = _capture(env, 1000)
    first = _clip(env, slot)
    builds: list[int] = []
    real = sampler.build_catalogue

    def counted(*args: Any) -> Any:
        builds.append(1)
        return real(*args)

    monkeypatch.setattr(sampler, "build_catalogue", counted)
    assert (_clip(env, slot), len(builds), env.spy.renders) == (first, 0, 1)
