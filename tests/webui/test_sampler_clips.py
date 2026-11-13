"""Edge sampler clip route: GET /clips/{slot_id} renders a real FLAC clip once with ffmpeg and serves it.

[if] a sampler clip is the wrong format, length, region or part, or renders twice [then] these fail, [else stop].

Regression one-liners:
  - if a clip is not flac s16 stereo at 48 kHz with X-Guard-Ms 20, or not region + 40 ms long, then broken
  - if a clip fetched twice runs ffmpeg twice or answers different bytes then broken
  - if a region at the track start does not lead with 20 ms of silence then broken
  - if a moved cue or a rewritten source file serves the old clip then broken
  - if a part clip is not cut from the stem-set file, or renders twice, then broken
  - if a part clip of a track with no stem-set is not a 404 STEMS_NOT_READY then broken
  - if an unknown slot's clip is not a 404 SLOT_NOT_FOUND then broken
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.shared.ffmpeg import resolve_ffmpeg
from apps.webui.server import sampler_clips
from apps.webui.server.routes import sampler
from tests.webui.sampler_support import GRID, make_app, make_env, wav

pytestmark = [pytest.mark.requires_ffmpeg]

VECTOR = {
    "trim_db": 0.0,
    "eq_db": {"low": 0.0, "mid": 0.0, "high": 0.0},
    "filter": 0.0,
    "stems": None,
    "key_shift_st": 0,
    "master_tempo": True,
}


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    renders: list[Path] = []
    real = sampler_clips.render_clip

    def counted(source: Path, in_ms: int, out_ms: int, dest: Path, *, low_priority: bool) -> None:
        renders.append(source)
        real(source, in_ms, out_ms, dest, low_priority=low_priority)

    monkeypatch.setattr(sampler_clips, "render_clip", counted)  # a spy: the real ffmpeg render still runs
    for base in make_env(tmp_path, monkeypatch):
        app = make_app(base.state, base.sources)
        app.include_router(sampler.router, prefix="/api/v1")
        with TestClient(app) as client:
            yield SimpleNamespace(**vars(base), client=client, renders=renders)


def _capture(client: TestClient, in_ms: int, out_ms: int) -> str:
    body = {
        "stable_id": GRID,
        "in_ms": in_ms,
        "out_ms": out_ms,
        "beats": 4,
        "vector": VECTOR,
        "set_id": "L1",
        "index": 0,
    }
    r = client.post("/api/v1/sampler/slots", json=body)
    assert r.status_code == 201, r.text
    return str(r.json()["slot_id"])


def _clip(client: TestClient, slot_id: str, **params: Any) -> Any:
    return client.get(f"/api/v1/sampler/clips/{slot_id}", params=params)


def _decoded(tmp: Path, content: bytes) -> tuple[str, bytes]:
    """ffmpeg's stream line for the clip and its s16le stereo samples."""
    path = tmp / "probe.flac"
    path.write_bytes(content)
    proc = subprocess.run(
        [resolve_ffmpeg(), "-nostdin", "-hide_banner", "-i", str(path), "-f", "s16le", "-"],
        capture_output=True,
        check=True,
    )
    stream = re.search(r"Audio: .*", proc.stderr.decode())
    assert stream is not None, proc.stderr
    return stream.group(0), proc.stdout


def _cache_files(env: SimpleNamespace) -> list[Path]:
    return sorted((env.state.parent / sampler_clips.CLIP_CACHE_DIRNAME).rglob("*.flac"))


def test_if_a_clip_is_fetched_twice_then_it_is_flac_with_guard_and_rendered_once(env: SimpleNamespace) -> None:
    """[if] a clip is fetched twice (with and without .flac) [then] ffmpeg ran once, same flac bytes, [else stop]."""
    slot_id = _capture(env.client, 1000, 3000)
    first, second = _clip(env.client, slot_id), _clip(env.client, f"{slot_id}.flac")
    assert (
        first.status_code,
        first.headers["content-type"],
        first.headers["x-guard-ms"],
        first.content[:4],
        len(_cache_files(env)),
        len(env.renders),
        second.content,
    ) == (200, "audio/flac", "20", b"fLaC", 1, 1, first.content)


def test_if_a_clip_is_served_then_it_is_s16_stereo_at_48k_and_region_plus_40_ms(env: SimpleNamespace) -> None:
    """[if] a 2000 ms region's clip is served [then] it is s16 stereo 48 kHz, 2040 ms long, [else stop]."""
    stream, pcm = _decoded(env.tmp, _clip(env.client, _capture(env.client, 1000, 3000)).content)
    frames = len(pcm) // 4
    assert ("48000 Hz" in stream, "stereo" in stream, "s16" in stream) == (True, True, True), stream
    assert abs(frames - 2040 * 48) <= 48, frames


def test_if_a_region_starts_at_the_track_start_then_the_lead_guard_is_silence(env: SimpleNamespace) -> None:
    """[if] a region starts at 0 ms [then] the clip is still region + 40 ms and its first 20 ms are silent, [else stop]."""
    _, pcm = _decoded(env.tmp, _clip(env.client, _capture(env.client, 0, 2000)).content)
    lead, body = pcm[: 19 * 48 * 4], pcm[25 * 48 * 4 :]
    assert (abs(len(pcm) // 4 - 2040 * 48) <= 48, set(lead), body.count(0) < len(body) // 2) == (True, {0}, True)


def test_if_the_cue_moves_then_the_old_clip_is_not_served(env: SimpleNamespace) -> None:
    """[if] a capture moves one bar later [then] its clip is a new render of the new region, not the old one, [else stop]."""
    old_id = _capture(env.client, 1000, 3000)
    old = _clip(env.client, old_id).content
    assert env.client.delete(f"/api/v1/sampler/slots/{old_id}").status_code == 204
    new = _clip(env.client, _capture(env.client, 3000, 5000)).content
    assert (len(env.renders), len(_cache_files(env)), new != old) == (2, 2, True)


def test_if_the_source_file_is_rewritten_then_the_clip_renders_again(env: SimpleNamespace) -> None:
    """[if] the track's audio file is rewritten (re-import) [then] the next fetch renders a new clip, [else stop]."""
    slot_id = _capture(env.client, 1000, 3000)
    before = _clip(env.client, slot_id).content
    quieter = env.tmp / "quiet.wav"
    subprocess.run(
        [resolve_ffmpeg(), "-nostdin", "-v", "error", "-i", str(env.sources.audio), "-af", "volume=0.25", str(quieter)],
        check=True,
    )
    quieter.replace(env.sources.audio)
    os.utime(env.sources.audio, ns=(1, 1))  # a different mtime even on a coarse filesystem clock
    after = _clip(env.client, slot_id).content
    assert (len(env.renders), after[:4], after != before) == (2, b"fLaC", True)


def test_if_a_track_has_a_stem_set_then_a_part_clip_is_cut_from_it_and_rendered_once(env: SimpleNamespace) -> None:
    """[if] ?part=drums is fetched twice for a track with a stem-set [then] flac from the drums file, one render, [else stop]."""
    drums = wav(env.tmp / "drums.wav", seconds=6.0, rate=48_000)
    env.sources.stems[GRID] = {
        p: (drums if p == "drums" else env.sources.audio) for p in ("vocals", "drums", "bass", "inst")
    }
    slot_id = _capture(env.client, 1000, 3000)
    mix = _clip(env.client, slot_id)
    first, second = _clip(env.client, slot_id, part="drums"), _clip(env.client, slot_id, part="drums")
    assert (first.status_code, first.headers["x-guard-ms"], first.content[:4], second.content) == (
        200,
        "20",
        b"fLaC",
        first.content,
    )
    assert (env.renders, len(_cache_files(env)), mix.content != first.content) == (
        [env.sources.audio, drums],
        2,
        True,
    )


def test_if_a_track_has_no_stem_set_then_a_part_clip_is_404_stems_not_ready(env: SimpleNamespace) -> None:
    """[if] ?part=vocals is fetched for a track with no stem-set [then] 404 STEMS_NOT_READY, unknown part 422, [else stop]."""
    slot_id = _capture(env.client, 1000, 3000)
    r, bad = _clip(env.client, slot_id, part="vocals"), _clip(env.client, slot_id, part="other")
    assert (r.status_code, r.json()["detail"]["code"], bad.status_code, env.renders) == (
        404,
        "STEMS_NOT_READY",
        422,
        [],
    )


def test_if_an_unknown_slot_clip_is_fetched_then_404_slot_not_found(env: SimpleNamespace) -> None:
    """[if] the clip of a slot id that is not in the catalogue is fetched [then] 404 SLOT_NOT_FOUND, no render, [else stop]."""
    r = _clip(env.client, f"{GRID}:capture:1-2")
    assert (r.status_code, r.json()["detail"]["code"], env.renders) == (404, "SLOT_NOT_FOUND", [])
