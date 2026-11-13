"""Classic sampler clips: ``GET /api/v1/sampler/clips/classic:<pad>:<source key>`` serves a bank slot's audio (#9987).

The classic store (``classic-sampler-logic.ts``) fires through the shared engine, which fetches every voice
from the clips route. Its slot id is ``classic:<bank letter><slot 1-16>:<encodeURIComponent(source key)>``,
the source key ``track:<stable_id>`` or ``file:<path>``. The pad is resolved through the banks document
(``apps.shared.state.sampler_banks``): the id names a pad AND the source the client believes is in it, and
both must agree, so a stale or forged id never serves another file. A track is its library audio file, a
file is the path the slot stores (a capture WAV from ``POST /sampler/captures`` or a loaded file).

The clip is the whole source with ``GUARD_MS`` of silence either side (the client's region is
0..duration_ms), FLAC s16 stereo at ``CLIP_SAMPLE_RATE``, cached and locked like an edge clip under
``sampler-clip-cache/~classic/``, keyed by the source file's path, size and mtime.

Mini-PRD:
  ✔︎ ✅ 🎯 a track slot serves the track's whole audio as a guarded flac, rendered once
    [if] a track slot's clip is fetched twice [then] flac, X-Guard-Ms 20, source + 40 ms, ffmpeg ran once [else ⛔️]
  ✔︎ ✅ 🎯 a file slot (a capture WAV) serves that file the same way
    [if] a stored capture is loaded into a pad [then] its clip is the capture + 40 ms [else ⛔️]
  ✔︎ ✅ 🎯 an empty pad, a changed pad, a missing file or a malformed id is a 404 with its own code, never a fallback
    [if] the pad is empty [then] 404 CLASSIC_SLOT_EMPTY [else ⛔️]
    [if] the pad now holds another source [then] 404 CLASSIC_SLOT_CHANGED [else ⛔️]
    [if] the file or the track's audio is gone [then] 404 CLASSIC_FILE_NOT_FOUND / AUDIO_NOT_FOUND [else ⛔️]
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from urllib.parse import unquote

from fastapi import HTTPException, Request

from apps.shared.ffmpeg import resolve_ffmpeg
from apps.shared.state import sampler_banks

from .routes.grid_edit import state_db_path
from .sampler_clips import CLIP_SAMPLE_RATE, GUARD_MS, ClipPart, clip_cache_dir, clip_source, render_once, run_render

PREFIX = "classic:"
BANK_LETTERS = "ABCD"  # must match BANK_LETTERS in classic-sampler-logic.ts
CACHE_SUBDIR = "~classic"  # never a stable id: those are hex
_PAD = re.compile(r"([A-D])([1-9]|1[0-6])")


def is_classic(slot_id: str) -> bool:
    """True for a classic sampler pad's slot id."""
    return slot_id.startswith(PREFIX)


def _not_found(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=404, detail={"code": code, "message": message})


def whole_source_argv(source: Path, out: str) -> list[str]:
    """The whole source with GUARD_MS of silence either side, FLAC s16 stereo at CLIP_SAMPLE_RATE."""
    guard_s = f"{GUARD_MS / 1000:.3f}"
    return [
        resolve_ffmpeg(),
        "-nostdin",
        "-v",
        "error",
        "-y",
        "-i",
        str(source),
        "-af",
        f"adelay={GUARD_MS}:all=1,apad=pad_dur={guard_s}",
        "-ar",
        str(CLIP_SAMPLE_RATE),
        "-ac",
        "2",
        "-sample_fmt",
        "s16",
        "-f",
        "flac",
        out,
    ]


def classic_source(request: Request, slot_id: str) -> Path:
    """The audio file a classic pad's slot id names, checked against the banks document; 404 otherwise."""
    pad, _, encoded_key = slot_id.removeprefix(PREFIX).partition(":")
    match = _PAD.fullmatch(pad)
    if match is None or not encoded_key:
        raise _not_found("SLOT_NOT_FOUND", f"{slot_id} is not classic:<A-D><1-16>:<source key>")
    bank, slot = BANK_LETTERS.index(match[1]), int(match[2]) - 1
    stored = sampler_banks.read_banks(state_db_path(request))["banks"][bank][slot]
    if stored is None:
        raise _not_found("CLASSIC_SLOT_EMPTY", f"bank {match[1]} slot {slot + 1} is empty")
    stored_key = f"track:{stored['stable_id']}" if "stable_id" in stored else f"file:{stored['path']}"
    if unquote(encoded_key) != stored_key:
        raise _not_found("CLASSIC_SLOT_CHANGED", f"bank {match[1]} slot {slot + 1} no longer holds that source")
    if "stable_id" in stored:
        path = clip_source(request, stored["stable_id"], None)
        if not path.is_file():
            raise _not_found("AUDIO_NOT_FOUND", f"track {stored['stable_id']} audio file {path} is missing")
        return path
    path = Path(stored["path"])
    if not path.is_file():
        raise _not_found("CLASSIC_FILE_NOT_FOUND", f"bank {match[1]} slot {slot + 1} file {path} is missing")
    return path


def classic_clip(request: Request, slot_id: str, part: ClipPart | None) -> Path:
    """The rendered (or cached) clip of a classic pad; a classic pad has no stem parts (422)."""
    if part is not None:
        raise HTTPException(
            status_code=422,
            detail={"code": "CLASSIC_PART_UNSUPPORTED", "message": "a classic sampler pad plays its whole source"},
        )
    source = classic_source(request, slot_id)
    st = source.stat()
    key = f"classic|{source}|{CLIP_SAMPLE_RATE}|{st.st_size}|{st.st_mtime_ns}"
    name = f"{hashlib.sha1(key.encode(), usedforsecurity=False).hexdigest()}.flac"
    dest = clip_cache_dir(request) / CACHE_SUBDIR / name

    def render(playing: bool) -> None:
        run_render(lambda out: whole_source_argv(source, out), dest, low_priority=playing)

    return render_once(request, dest, render, slot_id)
