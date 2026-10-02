"""Tri-band waveform peaks decoded by US, for tracks rekordbox never analyzed.

A track imported straight into OpenDJ has a ``tracks`` row and playable audio
but no ``track_vendor_ids`` mapping, so there is no ANLZ file to read: the
deck lane and the browser preview strip both went blank (PARITY-TODO, "Local
import - what breaks"). This module fills that gap from the only source that
actually exists for such a track - its own audio bytes.

The decode itself lives in :mod:`apps.analysis_waveform.decode` (ffmpeg, the
three band chains, the streaming peak reduction, and why 200 Hz / 4 kHz /
44.1 kHz). This module is the on-disk cache, the payload shapes and the
admission gate that keeps a decode off the API's worker threads.

Three hard rules, all tested:

* Nothing here EVER synthesises a shape. When the decode has not run and
  cannot run, callers get empty bands plus an explicit ``not_decoded`` status
  naming the reason - never a plausible-looking envelope. The three bands are
  three MEASURED envelopes (NATIVE-06), never one envelope copied three times.
* The peaks describe the bytes the deck actually streams. The audio path comes
  from :func:`resolve_playable_audio` under the SAME ``share`` audience
  ``GET /audio`` resolved for this request, so the lane and the sound cannot
  disagree - a Share listener hearing the Warehouse rung sees the Warehouse
  rung's peaks. That resolver carries the repo's library-mode contract with
  it, so on a non-darwin host with no ``MDT_LIBRARY_MODE`` set this endpoint
  raises the same loud misconfiguration error ``GET /audio`` already raises
  for every track. That is deliberate: a host that cannot resolve audio cannot
  honestly draw its waveform either, and a quietly empty lane would hide the
  real fault.
* PCM is never buffered whole. See :mod:`apps.analysis_waveform.decode`.

Where the decode runs: ``GET /anlz`` (one track, on demand, cached after).
Library row hydration is cache-READ-only - a listing of 200 rows must never
fan out 200 ffmpeg processes, so a row shows the strip once that track's
/anlz has been fetched, and an honest dash before then. The decoded strip
travels back on that same /anlz response (``local_waveform.preview_b64``) so
an already-rendered row can pick it up without a reload.

Thread budget: ``/anlz`` is a sync route, so it runs on Starlette's anyio
worker pool (40 threads by default). An unbounded queue in front of a
2-wide decoder could park every one of those threads behind a 180 s ffmpeg
run and stall the whole API, so admission is capped at MAX_DECODE_WAITERS and
the wait for a decode slot is capped at DECODE_QUEUE_WAIT_S. Past either cap
the answer is an immediate, honest ``not_decoded`` - never a stalled request.

One cache slot per track, keyed by the resolved file's path/mtime/size AND by
:func:`peaks_version`. An audience switch resolves a different file, so the key
stops matching and the peaks are re-decoded rather than a stale shape being
served; a band, rate or crossover change moves the version, so the mono entries
this cache held before NATIVE-06 are rebuilt rather than reshaped into garbage.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import HTTPException

from apps.adapters.rekordbox import config
from apps.analysis_waveform import decode
from apps.analysis_waveform.bands import (
    PWV6_SCALE_VERSION,
    STRIP_COLUMNS,
    _bands_payload,
    _downsample_max,
    pwv6_scale,
    pwv6_scaled_bands,
)
from apps.analysis_waveform.decode import (
    BAND_COUNT,
    BAND_NAMES,
    OVERVIEW_COLUMNS,
    LocalDecodeUnavailable,
    decode_peaks_from,
    decoder_mode,
    select_decoder,
)

# STRIP_COLUMNS is imported, not restated: the browser strip is ONE contract
# (120 columns x 3 bands, SPIKE-A1) whichever source filled it, and two copies
# of that width could drift apart without anything noticing.

log = logging.getLogger(__name__)

_PEAK_SCALE: int = 255  # int16 |amplitude| >> 7, so a full-scale sine hits 255
# ffmpeg is CPU-bound. Two decoders keep a deck load quick without turning a
# burst of selects into a fork bomb.
MAX_CONCURRENT_DECODES: int = 2
# Hard cap on Starlette worker threads this module may ever hold. The anyio
# pool is 40 wide; 8 leaves 32 for every other route no matter how many decode
# requests arrive at once.
MAX_DECODE_WAITERS: int = 8
# How long an admitted request may wait for one of the 2 decode slots before
# giving up. Well under any sane client timeout, so a queued request answers
# honestly instead of hanging.
DECODE_QUEUE_WAIT_S: float = 30.0

# Generation prefix for changes with no decode constant behind them. The
# mono -> tri column shape was one: nothing in the numbers below moved, but a
# cached entry written before it means something different.
PEAKS_GENERATION: str = "tri-1"


def peaks_version(profile: decode.DecodeProfile = decode.PROFILE) -> str:
    """What peaks decoded under ``profile`` MEAN, derived from the profile.

    Derived, not hand-maintained: a version anyone has to remember to bump is a
    value that rots silently between maintenances, and this one cannot, because
    changing any field of the profile IS the bump. Taking the profile as an
    ARGUMENT is what lets a caller ask about a different real producer
    configuration without patching a module attribute (AGENTS.md forbids
    monkeypatching; Codex review, PR #1536).
    """
    return f"{PEAKS_GENERATION}:{profile.identity()}"

_DECODE_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT_DECODES)
_DECODE_ADMISSION = threading.BoundedSemaphore(MAX_DECODE_WAITERS)
_TRACK_LOCKS_GUARD = threading.Lock()
_TRACK_LOCKS: dict[str, threading.Lock] = {}


# ----- on-disk cache ----------------------------------------------------------


def _entry_path(stable_id: str) -> Path:
    return config.LOCAL_WAVEFORM_CACHE_DIR / f"{stable_id}.json"


def _strip_path(stable_id: str) -> Path:
    return config.LOCAL_WAVEFORM_CACHE_DIR / f"{stable_id}.strip.json"


def _source_key(path: Path) -> dict[str, Any]:
    """Identity of the decoded bytes: path, inode, size, nanosecond mtime,
    and (POSIX only) nanosecond ctime.

    mtime alone cannot detect a replacement that preserves its timestamp - a
    sync/restore/repair tool commonly replaces a file via a temp-file plus
    atomic rename (the same pattern routes/relocate.py itself uses) and can
    explicitly carry the original mtime across that rename. inode is the
    signal that still moves for a rename-based replacement even when mtime
    and size coincide (Codex finding, issue #735 follow-up).

    inode alone still misses an IN-PLACE overwrite (same file, same inode)
    that also restores the original mtime - size can coincide there too.
    ctime closes that gap: unlike mtime it cannot be set from user space on
    POSIX (no ctime argument to utime/os.utime), so the kernel bumps it to
    "now" on every content write regardless of what the writer does to
    mtime afterward (verified live: a same-mtime in-place rewrite still
    moved ctime, Codex finding, issue #735 follow-up). Gated to POSIX
    because st_ctime means something different on Windows (creation time,
    which a content rewrite does NOT move) - see IS_WINDOWS in
    apps/shared/platform_paths.py. This narrower in-place-overwrite gap is
    unaddressed on Windows and is shared, un-fixed-there-either, by
    apps/vocals/cache.py's identical mtime+inode signature (Sat 8 Aug 2026).

    device is deliberately NOT included - it is mount-scoped, not file
    identity, and comparing it invalidated 300 of 640 live vocal-cache
    entries on a remount.
    """
    stat = path.stat()
    key: dict[str, Any] = {
        "source": str(path),
        "inode": stat.st_ino,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }
    if os.name == "posix":
        key["ctime_ns"] = stat.st_ctime_ns
    return key


NO_DECODER: str = "none"
# A decoder the operator forced (or an invalid setting) that is unavailable.
# Unlike NO_DECODER it matches no cache entry: a forced decoder fails closed
# rather than serving another decoder's columns.
FORCED_DECODER_UNAVAILABLE: str = "forced-unavailable"


def _decode_key(path: Path) -> dict[str, Any]:
    """``_source_key`` plus the decoder that would produce these peaks now.

    The engine and ffmpeg agree to within 1 of 255 on 44.1 kHz files but not on
    a 48 kHz high band or an AAC file's start (``decode`` module docstring), so
    a switch of decoder is a different decode convention and rebuilds the entry
    rather than serving the other decoder's columns. With no decoder available
    the key says so (``NO_DECODER``): nothing is ever decoded under it, and the
    decode attempt raises the reason. Only ``auto`` mode may fall back to an
    existing cache that way; a forced decoder that is missing, or an invalid
    setting, keys as ``FORCED_DECODER_UNAVAILABLE`` and never hits the cache.
    """
    try:
        decoder: str = select_decoder(path)
    except LocalDecodeUnavailable:
        decoder = NO_DECODER if decoder_mode() == "auto" else FORCED_DECODER_UNAVAILABLE
    return {**_source_key(path), "decoder": decoder}


def _entry_is_current(entry: dict[str, Any], key: dict[str, Any]) -> bool:
    """Whether ``entry`` was written by THIS decoder from THESE bytes.

    ``peaks_version`` is absent from every entry written before NATIVE-06, so a
    mono cache misses here and is re-decoded. It must never be reshaped as if it
    were tri-band: a 1-D mono array reinterpreted as ``(n, 3)`` would render as
    a plausible waveform made of the wrong numbers, which is the exact failure
    an explicit version key exists to prevent.
    """
    return (
        entry.get("schema") == config.LOCAL_WAVEFORM_CACHE_SCHEMA
        and entry.get("peaks_version") == peaks_version()
        and all(
            entry.get(name) == value
            for name, value in key.items()
            # With no decoder on the host nothing can rebuild the entry, so the
            # last real decode of these same bytes stands, whichever decoder
            # wrote it; a pre-decoder-key entry (no "decoder") still misses.
            if not (name == "decoder" and value == NO_DECODER and entry.get(name))
        )
    )


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("local waveform cache unreadable, recomputing: %s (%s)", path, exc)
        return None


def _write_json(path: Path, entry: dict[str, Any]) -> None:
    """Publish atomically: a crash mid-write must not leave a truncated entry."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    os.close(handle)
    tmp = Path(name)
    try:
        tmp.write_text(json.dumps(entry), encoding="utf-8")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _track_lock(stable_id: str) -> threading.Lock:
    with _TRACK_LOCKS_GUARD:
        lock = _TRACK_LOCKS.get(stable_id)
        if lock is None:
            lock = threading.Lock()
            _TRACK_LOCKS[stable_id] = lock
        return lock


def _cached_peaks(stable_id: str, key: dict[str, Any]) -> np.ndarray | None:
    entry = _read_json(_entry_path(stable_id))
    if entry is None or not _entry_is_current(entry, key):
        return None
    raw = np.frombuffer(base64.b64decode(entry["peaks_b64"]), dtype=np.uint8)
    if raw.size % BAND_COUNT:
        # A current-version entry whose payload is not a whole number of
        # 3-band columns is corrupt, not a shape to guess at.
        log.warning("local waveform cache entry has a ragged band payload: %s", stable_id)
        return None
    return raw.reshape(-1, BAND_COUNT)


def _store_peaks(stable_id: str, key: dict[str, Any], peaks: np.ndarray) -> None:
    """Publish the strip sidecar BEFORE the full entry.

    ``_cached_peaks`` gates solely on the entry file's presence, so it is the
    commit marker for this whole publication. If the process exits or a write
    fails between the two files, the entry must be the one still missing -
    that makes the next ``/anlz`` request recompute and republish both files
    instead of treating a half-published pair as done and leaving the strip
    sidecar (and therefore the browser listing preview) permanently absent.
    """
    preview_b64, preview_max = _strip_from_peaks(peaks)
    _write_json(
        _strip_path(stable_id),
        {
            "schema": config.LOCAL_WAVEFORM_CACHE_SCHEMA,
            "peaks_version": peaks_version(),
            "strip_scale": PWV6_SCALE_VERSION,
            "preview_b64": preview_b64,
            "preview_max": preview_max,
            **key,
        },
    )
    _write_json(
        _entry_path(stable_id),
        {
            "schema": config.LOCAL_WAVEFORM_CACHE_SCHEMA,
            "peaks_version": peaks_version(),
            "peaks_b64": base64.b64encode(np.ascontiguousarray(peaks).tobytes()).decode("ascii"),
            **key,
        },
    )


# ----- payload shapes ---------------------------------------------------------


def _strip_from_peaks(peaks: np.ndarray) -> tuple[str | None, int | None]:
    """The 120x3 browser strip, or ``(None, None)`` for too few columns.

    Under 120 columns (below 0.8 s of audio) there is nothing to downsample to
    the contract width, and padding it would be invented data. The bytes are on
    rekordbox PWV6's per-band scale (``bands.pwv6_scale``), so an own strip and
    a rekordbox one in the same list are drawn with the same band balance.
    """
    if peaks.shape[0] < STRIP_COLUMNS:
        return None, None
    strip = _downsample_max(pwv6_scale(peaks), STRIP_COLUMNS)
    return base64.b64encode(strip.tobytes()).decode("ascii"), int(strip.max())


def _bands_from_peaks(peaks: np.ndarray) -> dict[str, np.ndarray]:
    """The three MEASURED envelopes, 0..1, under their band keys.

    ``kind="tri"`` tells the client these are real per-band heights, the same
    claim a rekordbox PWV6/PWV7 lane makes - so unlike the mono fallback this
    one has to be earned by the decoder, not by duplicating one array.
    """
    scaled = np.clip(peaks.astype(np.float64) / _PEAK_SCALE, 0.0, 1.0)
    return {name: scaled[:, index] for index, name in enumerate(BAND_NAMES)}


def _waveform_payload(peaks: np.ndarray, points: int) -> dict[str, Any]:
    overview = _downsample_max(peaks, OVERVIEW_COLUMNS)
    return {
        "kind": "tri",
        "preview": _bands_payload(pwv6_scaled_bands(overview), points),
        "detail": _bands_payload(_bands_from_peaks(peaks), points),
    }


# ----- public surface ---------------------------------------------------------


def ensure_local_peaks(stable_id: str, *, share: bool = False) -> np.ndarray:
    """Decoded ``(n, 3)`` peaks for ``stable_id``, from cache or a fresh decode.

    ``share`` is the request's audio audience, forwarded to the same resolver
    ``GET /audio`` uses so the peaks describe the file this listener actually
    hears.

    Raises :class:`LocalDecodeUnavailable` with a stated reason when the audio
    cannot be resolved or decoded, when the decoder is saturated, and lets a
    genuine ``TRACK_NOT_FOUND`` 404 propagate untouched - an unknown id must
    still fail loudly.
    """
    from apps.adapters.rekordbox.paths import resolve_playable_audio

    try:
        path = resolve_playable_audio(stable_id, share=share).path
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        if detail.get("code") == "TRACK_NOT_FOUND":
            raise
        raise LocalDecodeUnavailable(str(detail.get("message") or exc.detail)) from None
    try:
        key = _decode_key(path)
    except OSError as exc:
        raise LocalDecodeUnavailable(f"audio file cannot be stat'd: {exc}") from None

    cached = _cached_peaks(stable_id, key)
    if cached is not None:
        return cached
    # Admission BEFORE the per-track lock: everything that can park a worker
    # thread lives inside this gate, so the parked count can never exceed
    # MAX_DECODE_WAITERS.
    if not _DECODE_ADMISSION.acquire(blocking=False):
        raise LocalDecodeUnavailable(
            f"{MAX_DECODE_WAITERS} local decodes are already in flight; retry shortly",
            retryable=True,
        )
    try:
        with _track_lock(stable_id):
            cached = _cached_peaks(stable_id, key)
            if cached is not None:
                return cached
            if not _DECODE_SLOTS.acquire(timeout=DECODE_QUEUE_WAIT_S):
                raise LocalDecodeUnavailable(
                    f"no decode slot came free within {DECODE_QUEUE_WAIT_S:.0f}s",
                    retryable=True,
                )
            try:
                decoder = key["decoder"]
                if decoder in (NO_DECODER, FORCED_DECODER_UNAVAILABLE):
                    decoder = select_decoder(path)  # raises the reason
                peaks = decode_peaks_from(decoder, path)
            finally:
                _DECODE_SLOTS.release()
            _store_peaks(stable_id, key, peaks)
    finally:
        _DECODE_ADMISSION.release()
    return peaks


def local_anlz_payload(
    stable_id: str, points: int, *, share: bool = False
) -> dict[str, Any]:
    """``/anlz`` payload for an unmapped track, with OUR peaks when we have them.

    Everything rekordbox owns (beatgrid, cues, phrases, PVDI vocals) stays
    empty - see ``empty_anlz_payload``. Only the waveform is ours to fill, and
    the extra ``local_waveform`` field says whether it was filled, why not, and
    carries the browser strip so an already-rendered library row can adopt it
    without waiting for the next listing fetch.
    """
    from apps.adapters.rekordbox.paths import empty_anlz_payload

    payload = empty_anlz_payload(stable_id, points)
    try:
        peaks = ensure_local_peaks(stable_id, share=share)
    except LocalDecodeUnavailable as exc:
        log.info("no local waveform for %s: %s", stable_id, exc.reason)
        payload["local_waveform"] = {
            "status": "not_decoded",
            "reason": exc.reason,
            "preview_b64": None,
            "preview_max": None,
            # A transient decoder-saturation reject, not a fact about the
            # track: the route must not cache this response, and the client
            # must not treat it as a terminal state (issue #735 follow-up).
            "retryable": exc.retryable,
        }
        return payload
    preview_b64, preview_max = _strip_from_peaks(peaks)
    payload["waveform"] = _waveform_payload(peaks, points)
    payload["local_waveform"] = {
        "status": "decoded",
        "reason": None,
        "preview_b64": preview_b64,
        "preview_max": preview_max,
    }
    return payload


def local_preview_strip(stable_id: str) -> tuple[str | None, int | None]:
    """The cached browser strip for an unmapped track. NEVER decodes.

    ``(None, None)`` is the real "this track's decode has not run yet" state -
    the row renders the same explicit dash an unanalyzed rekordbox track gets.
    The tiny sidecar is revalidated against its own recorded audio file, so a
    replaced file drops the strip instead of showing the old shape. The listing
    has no audience of its own, so the strip is whichever rung was decoded
    last; both rungs are the same track, and the /anlz response carries the
    audience-correct strip for the row that is actually selected.
    """
    entry = _read_json(_strip_path(stable_id))
    if entry is None:
        return None, None
    source = entry.get("source")
    try:
        key = _decode_key(Path(str(source)))
    except OSError:
        return None, None
    if not _entry_is_current(entry, key):
        return None, None
    if entry.get("strip_scale") != PWV6_SCALE_VERSION:
        # Written on another byte scale (or before strips had one): drawing it
        # would put this row's band balance at odds with its neighbours'. The
        # cached peaks it came from are still current, so re-derive it from
        # them (no decode) and rewrite the sidecar once.
        peaks = _cached_peaks(stable_id, key)
        if peaks is None:
            return None, None
        preview_b64, preview_max = _strip_from_peaks(peaks)
        _write_json(
            _strip_path(stable_id),
            {**entry, "strip_scale": PWV6_SCALE_VERSION, "preview_b64": preview_b64, "preview_max": preview_max},
        )
        return preview_b64, preview_max
    return entry["preview_b64"], entry["preview_max"]


__all__ = [
    "DECODE_QUEUE_WAIT_S",
    "MAX_CONCURRENT_DECODES",
    "MAX_DECODE_WAITERS",
    "PEAKS_GENERATION",
    "STRIP_COLUMNS",
    "LocalDecodeUnavailable",
    "ensure_local_peaks",
    "local_anlz_payload",
    "local_preview_strip",
    "peaks_version",
]
