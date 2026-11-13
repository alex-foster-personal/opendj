"""Edge sampler clip renderer: a slot's region plus a 20 ms guard, cut once into a FLAC s16 clip (EDGESAMP-03).

Spec: ``(private path)`` sections 5.1 (clip render) and 10 (clip route).
The route is ``GET /api/v1/sampler/clips/{slot_id}`` in ``routes/sampler.py``; this module renders,
keys and serves the file. A clip is decoded by the same ffmpeg the server decodes everything with
(``apps.shared.ffmpeg.resolve_ffmpeg``) and lands under ``<state dir>/sampler-clip-cache/<stable_id>/``.
``?part=`` cuts the same region from the track's existing stem-set file instead (EDGESAMP-11872h);
nothing is separated here. The cache is a bounded LRU (spec 5.3): a serve touches the clip's mtime and a
render evicts the least recently served clips once the cache passes ``CLIP_CACHE_BUDGET_BYTES``. Per-clip
render locks make two first requests for one clip render it once. While a deck plays (the ui_mirror the
deck-play gate reads), ``prerender_clips`` renders nothing and an on-demand render runs alone at low priority.

Mini-PRD:
  ✔︎ ✅ 🎯 a clip is the region plus GUARD_MS either side, FLAC s16 stereo at CLIP_SAMPLE_RATE
    [if] a clip is served [then] it is flac, s16, 48 kHz and region + 40 ms long [else ⛔️]
    [if] a region starts inside the first 20 ms [then] the lead guard is padded with silence [else ⛔️]
  ✔︎ ✅ 🎯 rendered once, then served from disk by a key of region, rate and source size+mtime
    [if] a clip is fetched twice [then] ffmpeg ran once and both answers are the same bytes [else ⛔️]
    [if] the cue moves or the source file is rewritten [then] the old clip is not served [else ⛔️]
  ✔︎ ✅ 🎯 stem part clips are cut from the existing stem-set, never separated here
    [if] ?part=drums is fetched twice [then] it is flac with the guard, rendered once [else ⛔️]
    [if] the track has no stem-set [then] ?part answers 404 STEMS_NOT_READY [else ⛔️]
  ✔︎ ✅ 🎯 the cache is a 2 GB LRU: a serve is a touch, a render evicts the least recently served clips
    [if] the cache passes its budget [then] the least recently served clips go, the newest stays [else ⛔️]
  ✔︎ ✅ 🎯 per-clip render locks: concurrent first requests for one clip run ffmpeg once
    [if] four requests race for one new clip [then] one render, four identical flac answers [else ⛔️]
  ✔︎ ✅ 🎯 while a deck plays: no background pre-render, on-demand renders one at a time at low priority
    [if] a deck is playing [then] prerender_clips renders nothing [else ⛔️]
    [if] three new clips are asked for while a deck plays [then] never two renders at once [else ⛔️]
  ? the key uses the source file's size+mtime, not the engine's decode fingerprint (fingerprint.rs)
"""

from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import threading
import time
from collections.abc import Callable, Iterable
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Literal

from fastapi import HTTPException, Request, Response

from apps.shared.ffmpeg import resolve_ffmpeg
from apps.shared.process_priority import background_argv, lowered_priority
from apps.shared.sync_runtime_gates import any_deck_playing

from .routes.grid_edit import state_db_path
from .sampler_sources import sources_for

_log = logging.getLogger(__name__)

GUARD_MS = 20
#: The audio engine's output rate; a clip at it plays without a resample on the voice.
CLIP_SAMPLE_RATE = 48000
CLIP_CACHE_DIRNAME = "sampler-clip-cache"
ClipPart = Literal["vocals", "drums", "bass", "inst"]

# Input seek (-ss before -i) matches a full decode plus atrim for these formats: same length, no
# lag, bit-identical for a VBR mp3 and FLAC at 5 to 200 s (Tue 10 Nov), within 1 LSB for 44.1 kHz
# WAV (the resampler's rounding, about -90 dB). AAC in mp4 is NOT: its edit list and priming give
# 28 to 44 dB SNR against the full decode, so mp4/m4a, ogg and anything unmeasured keep decoding
# from the top of the file.
SEEK_SAFE_SUFFIXES = frozenset({".mp3", ".flac", ".wav", ".aif", ".aiff"})
SEEK_PREROLL_MS = 1000  # decoded and discarded, so an mp3 bit reservoir is primed before the region

#: Warm disk cache budget (spec 5.3: 2 GB, about 2,000 average clips); a sweep evicts to EVICT_TO_FRACTION of it.
CLIP_CACHE_BUDGET_BYTES = 2 * 1024**3
EVICT_TO_FRACTION = 0.9
#: The niceness of a pre-render, or of an on-demand render while a deck plays (the audio scan's level).
LOW_PRIORITY_NICENESS = 10
_RENDER_LOCKS = tuple(threading.Lock() for _ in range(64))  # striped by clip: one render per clip at a time
_PLAYING_RENDER_SLOT = threading.Lock()  # while a deck plays, one render at a time across all clips


# ---------- render ---------------------------------------------------------------------


def render_argv(source: Path, in_ms: int, out_ms: int, out: str) -> list[str]:
    """Region plus GUARD_MS either side, sample-accurate (atrim after decode), lead padded at track start."""
    start_ms = max(0, in_ms - GUARD_MS)
    lead_pad_ms = GUARD_MS - (in_ms - start_ms)
    seek_ms = max(0, start_ms - SEEK_PREROLL_MS) if source.suffix.lower() in SEEK_SAFE_SUFFIXES else 0
    filters = [
        f"atrim=start={(start_ms - seek_ms) / 1000:.6f}:end={(out_ms + GUARD_MS - seek_ms) / 1000:.6f}",
        "asetpts=PTS-STARTPTS",
    ]
    if lead_pad_ms > 0:
        filters.append(f"adelay={lead_pad_ms}:all=1")
    seek = ["-ss", f"{seek_ms / 1000:.6f}"] if seek_ms > 0 else []
    return [
        resolve_ffmpeg(),
        "-nostdin",
        "-v",
        "error",
        "-y",
        *seek,
        "-i",
        str(source),
        "-af",
        ",".join(filters),
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


def render_clip(source: Path, in_ms: int, out_ms: int, dest: Path, *, low_priority: bool) -> None:
    """Render a region of ``source`` onto ``dest`` (see ``run_render``)."""
    run_render(lambda out: render_argv(source, in_ms, out_ms, out), dest, low_priority=low_priority)


def run_render(argv_for: Callable[[str], list[str]], dest: Path, *, low_priority: bool) -> None:
    """Run ffmpeg into a private temp file and move it onto ``dest``; a failed render leaves nothing."""
    # A per-process, per-thread temp name: two renders of one clip never write the same file.
    tmp = dest.with_name(f"{dest.stem}.{os.getpid()}.{threading.get_ident()}.tmp.flac")
    argv = argv_for(str(tmp))
    priority = lowered_priority(LOW_PRIORITY_NICENESS) if low_priority else {}
    proc = subprocess.run(
        background_argv(argv) if low_priority else argv, capture_output=True, text=True, check=False, **priority
    )
    if proc.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg clip render failed ({proc.returncode}): {proc.stderr.strip()[:400]}")
    tmp.replace(dest)


# ---------- bounded LRU and render locks (spec 5.3) -------------------------------------


def evict_clip_cache(root: Path, budget_bytes: int, keep: Path) -> int:
    """Drop the least recently served clips (mtime, touched on every serve) until under 90% of budget."""
    files = [
        (e.stat().st_mtime_ns, e.stat().st_size, Path(e.path))
        for d in os.scandir(root)
        if d.is_dir()
        for e in os.scandir(d.path)
        if e.is_file() and e.name.endswith(".flac") and ".tmp." not in e.name
    ]
    total = sum(size for _, size, _ in files)
    if total <= budget_bytes:
        return 0
    removed = 0
    for _, size, path in sorted(files):
        if total <= budget_bytes * EVICT_TO_FRACTION:
            break
        if path == keep:  # the clip about to be served
            continue
        try:
            path.unlink()
        except OSError as exc:  # e.g. Windows refuses while another request streams it; the next sweep retries
            _log.warning("sampler: clip cache could not evict %s: %s", path.name, exc)
            continue
        total -= size
        removed += 1
    _log.info("sampler: clip cache evicted %d clips, %.0f MB left", removed, total / 1e6)
    return removed


def deck_playing(request: Request) -> bool:
    """True while the page's ui_mirror shows a deck playing: the signal the deck-play gate reads."""
    return any_deck_playing(getattr(request.app.state, "ui_mirror", None))


def _render_lock(dest: Path) -> threading.Lock:
    return _RENDER_LOCKS[hash(dest) % len(_RENDER_LOCKS)]


# ---------- keys and sources -----------------------------------------------------------


def clip_cache_dir(request: Request) -> Path:
    """The clip directory beside state.db."""
    return state_db_path(request).parent / CLIP_CACHE_DIRNAME


def parse_slot_id(slot_id: str) -> tuple[str, str, int, int] | None:
    """``<stable_id>:<source>:<in_ms>-<out_ms>`` split into its parts; stable ids may themselves hold ':'."""
    parts = slot_id.rsplit(":", 2)
    if len(parts) != 3:
        return None
    a, _, b = parts[2].partition("-")
    return (parts[0], parts[1], int(a), int(b)) if a.isdigit() and b.isdigit() else None


def clip_source(request: Request, stable_id: str, part: ClipPart | None) -> Path:
    """The track's audio file, or its stem-set file for ``part``; 404 when the file or the stem-set is missing."""
    sources = sources_for(request)
    if part is None:
        try:
            return sources.audio_path(request, stable_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail={"code": "AUDIO_NOT_FOUND", "message": str(exc)}) from exc
    path = sources.stem_part_path(request, stable_id, part)
    if path is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "STEMS_NOT_READY",
                "message": f"track {stable_id} has no stem-set with a {part} part; play the mix clip",
            },
        )
    return path


def clip_dest(request: Request, stable_id: str, in_ms: int, out_ms: int, source: Path, part: ClipPart | None) -> Path:
    """The cache file for a region of ``source``: a moved cue or a rewritten source is a new key."""
    st = source.stat()
    # The part file's size+mtime is the stem-set fingerprint: a re-separated stem-set renders anew.
    key = f"{stable_id}|{in_ms}|{out_ms}|{CLIP_SAMPLE_RATE}|{st.st_size}|{st.st_mtime_ns}" + (
        "" if part is None else f"|part={part}|{source}"
    )
    return clip_cache_dir(request) / stable_id / f"{hashlib.sha1(key.encode(), usedforsecurity=False).hexdigest()}.flac"


# ---------- serve ----------------------------------------------------------------------


def serve_clip(dest: Path) -> Response:
    """The clip bytes as audio/flac with the guard length the client trims; a serve is an LRU touch."""
    os.utime(dest)  # the LRU clock: a served clip is recent
    return Response(
        content=dest.read_bytes(),
        media_type="audio/flac",
        headers={"X-Guard-Ms": str(GUARD_MS), "Cache-Control": "private, max-age=86400"},
    )


def cached_clip(request: Request, slot_id: str, part: ClipPart | None) -> Path | None:
    """The already-rendered clip for a well-formed slot id, without building the catalogue; None on a miss."""
    parsed = parse_slot_id(slot_id)
    if parsed is None:
        return None
    stable_id, _, in_ms, out_ms = parsed
    try:
        dest = clip_dest(request, stable_id, in_ms, out_ms, clip_source(request, stable_id, part), part)
    except (HTTPException, OSError, ValueError):
        return None  # the miss path answers with the catalogue's own error
    return dest if dest.is_file() else None


def rendered_clip(request: Request, slot: dict[str, Any], part: ClipPart | None, *, background: bool) -> Path:
    """The clip of a catalogue slot, rendered now (once, under its lock) when it is not on disk yet."""
    source = clip_source(request, slot["stable_id"], part)
    dest = clip_dest(request, slot["stable_id"], slot["in_ms"], slot["out_ms"], source, part)

    def render(playing: bool) -> None:
        render_clip(source, slot["in_ms"], slot["out_ms"], dest, low_priority=background or playing)

    return render_once(request, dest, render, slot["slot_id"])


def render_once(request: Request, dest: Path, render: Callable[[bool], None], label: str) -> Path:
    """``dest`` once on disk: ``render(deck_playing)`` runs under its lock (alone while a deck plays), then evicts."""
    with _render_lock(dest):
        if dest.is_file():
            return dest
        playing = deck_playing(request)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with _PLAYING_RENDER_SLOT if playing else nullcontext():
            t0 = time.perf_counter()
            render(playing)
            _log.info("sampler: rendered clip %s in %.0f ms", label, (time.perf_counter() - t0) * 1000)
        evict_clip_cache(clip_cache_dir(request), CLIP_CACHE_BUDGET_BYTES, keep=dest)
    return dest


def prerender_clips(request: Request, slots: Iterable[dict[str, Any]]) -> int:
    """Background pre-render of the slots' missing mix clips; stops (or never starts) while a deck plays."""
    rendered = 0
    for slot in slots:
        if deck_playing(request):  # checked per clip: a deck that starts mid-batch stops the batch
            _log.info("sampler: clip pre-render held, a deck is playing (%d rendered)", rendered)
            break
        source = clip_source(request, slot["stable_id"], None)
        if not clip_dest(request, slot["stable_id"], slot["in_ms"], slot["out_ms"], source, None).is_file():
            rendered_clip(request, slot, None, background=True)
            rendered += 1
    return rendered
