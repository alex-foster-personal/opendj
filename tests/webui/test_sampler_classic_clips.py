"""Classic sampler clips: GET /clips/classic:<pad>:<source key> serves a bank pad's whole source (#9987).

[if] a classic pad's clip is not its whole source with the guard, or a bad pad does not 404 with its code [then] these fail, [else stop].

Regression one-liners:
  - if a track pad's clip is not flac + X-Guard-Ms 20, source + 40 ms long, rendered once, then broken
  - if a capture WAV in a pad is not served as capture + 40 ms then broken
  - if an empty pad is not a 404 CLASSIC_SLOT_EMPTY then broken
  - if a pad that now holds another source does not 404 CLASSIC_SLOT_CHANGED (no fallback) then broken
  - if a gone file or gone track audio is not a 404 CLASSIC_FILE_NOT_FOUND / AUDIO_NOT_FOUND then broken
  - if a malformed classic id is not a 404 SLOT_NOT_FOUND, or ?part is not a 422, then broken
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from apps.shared.ffmpeg import resolve_ffmpeg
from apps.shared.state import sampler_banks
from apps.webui.server import sampler_classic_clips
from apps.webui.server.routes import sampler
from tests.webui.sampler_support import GRID, make_app, make_env, wav

pytestmark = [pytest.mark.requires_ffmpeg]


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    renders: list[Path] = []
    real = sampler_classic_clips.run_render

    def counted(argv_for: Callable[[str], list[str]], dest: Path, *, low_priority: bool) -> None:
        renders.append(dest)
        real(argv_for, dest, low_priority=low_priority)

    monkeypatch.setattr(sampler_classic_clips, "run_render", counted)  # a spy: the real ffmpeg render still runs
    for base in make_env(tmp_path, monkeypatch):
        app = make_app(base.state, base.sources)
        app.include_router(sampler.router, prefix="/api/v1")
        with TestClient(app) as client:
            yield SimpleNamespace(**vars(base), client=client, renders=renders)


def _load(env: SimpleNamespace, slot: dict[str, Any] | None, bank: int = 0, index: int = 0) -> None:
    doc = sampler_banks.default_document()
    doc["banks"][bank][index] = slot
    sampler_banks.write_banks(env.state, doc)


def _pad(source: dict[str, str]) -> dict[str, Any]:
    return {**source, "mode": "one_shot", "gain_db": 0.0, "title": "pad"}


def _slot_id(pad: str, source_key: str) -> str:
    """The id ``engineSlotId`` builds: ``classic:<pad>:<encodeURIComponent(source key)>``."""
    return f"classic:{pad}:{quote(source_key, safe='')}"


def _clip(env: SimpleNamespace, slot_id: str, **params: Any) -> Any:
    """Fetch as ``clipUrl`` does: the whole slot id encoded once more into the path."""
    return env.client.get(f"/api/v1/sampler/clips/{quote(slot_id, safe='')}", params=params)


def _frames(tmp: Path, content: bytes) -> int:
    path = tmp / "probe.flac"
    path.write_bytes(content)
    proc = subprocess.run(
        [resolve_ffmpeg(), "-nostdin", "-v", "error", "-i", str(path), "-f", "s16le", "-ac", "2", "-ar", "48000", "-"],
        capture_output=True,
        check=True,
    )
    return len(proc.stdout) // 4


def _code(response: Any) -> tuple[int, str]:
    return response.status_code, response.json()["detail"]["code"]


def test_if_a_track_pad_is_fetched_twice_then_it_is_the_whole_track_with_guard_rendered_once(
    env: SimpleNamespace,
) -> None:
    """[if] a track pad's clip is fetched twice [then] flac, guard 20, 6 s + 40 ms, one render, same bytes, [else stop]."""
    _load(env, _pad({"stable_id": GRID}))
    first, second = _clip(env, _slot_id("A1", f"track:{GRID}")), _clip(env, _slot_id("A1", f"track:{GRID}"))
    assert (first.status_code, first.headers["x-guard-ms"], first.content[:4], len(env.renders)) == (
        200,
        "20",
        b"fLaC",
        1,
    ), first.text
    assert second.content == first.content
    assert abs(_frames(env.tmp, first.content) - 6040 * 48) <= 48


def test_if_a_capture_wav_is_in_a_pad_then_its_clip_is_the_capture_plus_guard(env: SimpleNamespace) -> None:
    """[if] a 2 s capture WAV sits in bank D slot 16 [then] its clip is 2040 ms of flac, [else stop]."""
    capture = wav(env.tmp / "sampler-capture.wav", seconds=2.0, rate=48_000)
    _load(env, _pad({"path": str(capture)}), bank=3, index=15)
    r = _clip(env, _slot_id("D16", f"file:{capture}"))
    assert (r.status_code, r.content[:4]) == (200, b"fLaC"), r.text
    assert abs(_frames(env.tmp, r.content) - 2040 * 48) <= 48


def test_if_a_flac_file_is_in_a_pad_then_its_name_is_not_cut_as_a_suffix(env: SimpleNamespace) -> None:
    """[if] a pad holds a file named *.flac [then] its clip is served, not 404 for a cut-off name, [else stop]."""
    flac = env.tmp / "loop.flac"
    subprocess.run([resolve_ffmpeg(), "-nostdin", "-v", "error", "-i", str(env.sources.audio), str(flac)], check=True)
    _load(env, _pad({"path": str(flac)}))
    r = _clip(env, _slot_id("A1", f"file:{flac}"))
    assert (r.status_code, r.content[:4]) == (200, b"fLaC"), r.text


def test_if_the_pad_is_empty_then_404_classic_slot_empty(env: SimpleNamespace) -> None:
    """[if] bank A slot 2 holds nothing [then] its clip is a 404 CLASSIC_SLOT_EMPTY and nothing renders, [else stop]."""
    _load(env, _pad({"stable_id": GRID}))
    assert (_code(_clip(env, _slot_id("A2", f"track:{GRID}"))), env.renders) == ((404, "CLASSIC_SLOT_EMPTY"), [])


def test_if_the_pad_now_holds_another_source_then_404_classic_slot_changed(env: SimpleNamespace) -> None:
    """[if] the id names a track but the pad now holds a file [then] 404 CLASSIC_SLOT_CHANGED, no fallback, [else stop]."""
    _load(env, _pad({"path": str(env.sources.audio)}))
    assert (_code(_clip(env, _slot_id("A1", f"track:{GRID}"))), env.renders) == ((404, "CLASSIC_SLOT_CHANGED"), [])


def test_if_the_file_is_gone_then_404_classic_file_not_found(env: SimpleNamespace) -> None:
    """[if] a file pad's file was deleted [then] its clip is a 404 CLASSIC_FILE_NOT_FOUND, [else stop]."""
    gone = env.tmp / "gone.wav"
    _load(env, _pad({"path": str(gone)}))
    assert _code(_clip(env, _slot_id("A1", f"file:{gone}"))) == (404, "CLASSIC_FILE_NOT_FOUND")


def test_if_the_track_audio_is_gone_then_404_audio_not_found(env: SimpleNamespace) -> None:
    """[if] a track pad's audio file was deleted [then] its clip is a 404 AUDIO_NOT_FOUND, [else stop]."""
    _load(env, _pad({"stable_id": GRID}))
    env.sources.audio.unlink()
    assert _code(_clip(env, _slot_id("A1", f"track:{GRID}"))) == (404, "AUDIO_NOT_FOUND")


@pytest.mark.parametrize("slot_id", ["classic:E1:track%3Ax", "classic:A17:track%3Ax", "classic:A1:", "classic:A1"])
def test_if_the_classic_id_is_malformed_then_404_slot_not_found(env: SimpleNamespace, slot_id: str) -> None:
    """[if] the pad is out of range or the source key is missing [then] 404 SLOT_NOT_FOUND, [else stop]."""
    assert _code(_clip(env, slot_id)) == (404, "SLOT_NOT_FOUND")


def test_if_a_part_is_asked_of_a_classic_pad_then_422(env: SimpleNamespace) -> None:
    """[if] ?part=drums is asked of a classic pad [then] 422 CLASSIC_PART_UNSUPPORTED, [else stop]."""
    _load(env, _pad({"stable_id": GRID}))
    assert _code(_clip(env, _slot_id("A1", f"track:{GRID}"), part="drums")) == (422, "CLASSIC_PART_UNSUPPORTED")
