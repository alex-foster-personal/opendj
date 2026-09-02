"""Waveform peaks decoded by US, for tracks rekordbox never analyzed.

A track imported straight into OpenDJ has a ``tracks`` row and playable audio
but no ``track_vendor_ids`` mapping, so there is no ANLZ file to read: the
deck lane and the browser preview strip both went blank (PARITY-TODO, "Local
import - what breaks"). This module fills that gap from the only source that
actually exists for such a track - its own audio bytes.

Decoder: ffmpeg, the same external tool the USB transcode preflight already
requires (``apps/sync/usb/preflight.py``). It is decoding to a mono 8 kHz
s16le stream purely to build a peak envelope, so no resampling quality
argument applies, and it reaches every container in AUDIO_MEDIA_TYPES without
adding librosa/soundfile (the opt-in ``analysis`` extra) to the web server's
runtime deps.

Three hard rules, all tested:

* Nothing here EVER synthesises a shape. When the decode has not run and
  cannot run, callers get empty bands plus an explicit ``not_decoded`` status
  naming the reason - never a plausible-looking envelope.
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
* PCM is never buffered whole. An hour of 8 kHz mono s16le is ~58 MB, and two
  of those in flight could take the web server down, so the peak reduction
  consumes ffmpeg's stdout in fixed chunks and keeps only the uint8 peak
  columns (~151 bytes per second of audio).

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

One cache slot per track, keyed by the resolved file's path/mtime/size. An
audience switch resolves a different file, so the key stops matching and the
peaks are re-decoded rather than a stale shape being served.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import IO, Any

import numpy as np
from fastapi import HTTPException

from apps.adapters.rekordbox import config
from apps.webui.server.rb_vendor_pkg.anlz import PREVIEW_COLUMNS as STRIP_COLUMNS
from apps.webui.server.rb_vendor_pkg.waveform_bands import (
    _bands_payload,
    _downsample_max,
)

# STRIP_COLUMNS is imported, not restated: the browser strip is ONE contract
# (120 columns x 3 bands, SPIKE-A1) whichever source filled it, and two copies
# of that width could drift apart without anything noticing.

log = logging.getLogger(__name__)

FFMPEG_BINARY: str = "ffmpeg"
# A peak envelope, not a listenable signal: 8 kHz is 53x the ~150 columns/s the
# detail lane draws, so every column still sees 53 samples to take a max over.
DECODE_SAMPLE_RATE_HZ: int = 8_000
# Matches the rekordbox PWV3/PWV7 detail density, so a locally decoded lane and
# an ANLZ lane draw at the same scale.
DETAIL_COLUMNS_PER_S: int = 150
# The reduction works in WHOLE columns so it can run on a stream: 8000/150 is
# not an integer, so a column is 53 samples and the real density is 150.94
# columns/s. That 0.6% is invisible - the client maps a column to a position by
# fraction-of-length, never by assuming a columns-per-second constant.
SAMPLES_PER_COLUMN: int = DECODE_SAMPLE_RATE_HZ // DETAIL_COLUMNS_PER_S
# Matches the rekordbox PWAV/PWV6 preview length, which is what the deck's
# strip overview draws. Deliberately NOT called PREVIEW_COLUMNS: that name is
# already the 120-wide BROWSER strip in anlz.py, and the two are different
# widths for different surfaces.
OVERVIEW_COLUMNS: int = 400
DECODE_TIMEOUT_S: float = 180.0
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
# stdout is consumed in chunks; only the uint8 peak columns are retained.
READ_CHUNK_BYTES: int = 1 << 20

_PEAK_SCALE: int = 255  # int16 |amplitude| >> 7, so a full-scale sine hits 255
_INT16_MAX: int = 32_767
_COLUMN_BYTES: int = SAMPLES_PER_COLUMN * 2

_DECODE_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT_DECODES)
_DECODE_ADMISSION = threading.BoundedSemaphore(MAX_DECODE_WAITERS)
_TRACK_LOCKS_GUARD = threading.Lock()
_TRACK_LOCKS: dict[str, threading.Lock] = {}


class LocalDecodeUnavailable(Exception):
    """The decode did not run, and why. Surfaced in the payload, never hidden.

    ``retryable`` marks a TRANSIENT condition (decoder momentarily saturated)
    as opposed to a fact about the track itself (no ffmpeg, corrupt audio,
    missing file). The distinction matters downstream: a retryable failure
    must never be cached as if it were permanent - see its use in
    :func:`local_anlz_payload` and the route's Cache-Control choice.
    """

    def __init__(self, reason: str, *, retryable: bool = False) -> None:
        super().__init__(reason)
        self.reason: str = reason
        self.retryable: bool = retryable


# ----- streaming peak reduction -----------------------------------------------


def _column_peaks(block: bytes) -> np.ndarray:
    """Peak byte per whole column in ``block`` (a multiple of _COLUMN_BYTES)."""
    samples = np.frombuffer(block, dtype="<i2").reshape(-1, SAMPLES_PER_COLUMN)
    # -32768 has no positive int16 twin; clamp before the shift so one sample
    # cannot wrap a column to 0.
    amplitude = np.minimum(np.abs(samples.astype(np.int32)), _INT16_MAX)
    return (amplitude.max(axis=1) >> 7).astype(np.uint8)


def _tail_peak(block: bytes) -> np.ndarray:
    """One final column for a short tail. Real samples, never padded to width."""
    usable = len(block) - len(block) % 2
    samples = np.frombuffer(block[:usable], dtype="<i2")
    amplitude = np.minimum(np.abs(samples.astype(np.int32)), _INT16_MAX)
    return (amplitude.max(keepdims=True) >> 7).astype(np.uint8)


def _reduce_stream(stream: IO[bytes], *, chunk_bytes: int = READ_CHUNK_BYTES) -> np.ndarray:
    """Peak columns for a PCM stream, holding at most one chunk in memory.

    A read boundary lands anywhere, so bytes past the last whole column carry
    over into the next chunk. Reducing as we go is what keeps an hour-long
    track at ~540 KB of peaks instead of ~58 MB of PCM.
    """
    columns: list[np.ndarray] = []
    remainder = b""
    while True:
        chunk = stream.read(chunk_bytes)
        if not chunk:
            break
        buffer = remainder + chunk if remainder else chunk
        whole = len(buffer) - len(buffer) % _COLUMN_BYTES
        if whole:
            columns.append(_column_peaks(buffer[:whole]))
        remainder = buffer[whole:]
    if len(remainder) >= 2:
        columns.append(_tail_peak(remainder))
    if not columns:
        return np.empty(0, dtype=np.uint8)
    return np.concatenate(columns)


def _peak_columns(pcm: bytes, *, chunk_bytes: int = READ_CHUNK_BYTES) -> np.ndarray:
    """Whole-buffer form of :func:`_reduce_stream`, for callers holding bytes."""
    return _reduce_stream(io.BytesIO(pcm), chunk_bytes=chunk_bytes)


# ----- ffmpeg decode ----------------------------------------------------------


def _resolve_ffmpeg() -> str:
    """ffmpeg executable path: MDT_FFMPEG override first, else PATH lookup.

    A packaged/GUI-launched app does not inherit Homebrew's PATH the way a
    shell does, so a bare PATH lookup can find nothing even with ffmpeg
    installed (discussion_r3908337225, issue #735 follow-up). MDT_FFMPEG is
    the same escape hatch scripts/vocal_region_worker.py and
    scripts/stem_bundle_worker.py already use for this. Unlike
    vocal_region_worker.py this does not mutate os.environ["PATH"]: this
    module runs inside a long-lived, multi-threaded server process, and a
    process-wide PATH mutation on a request path would race every
    concurrently-decoding request.

    A set-but-broken MDT_FFMPEG raises rather than falling back to PATH -
    silently ignoring an explicit override would mask the misconfiguration
    (fail-fast, no hidden defaults).
    """
    override = os.environ.get("MDT_FFMPEG")
    if override:
        if Path(override).is_file() and os.access(override, os.X_OK):
            return override
        raise LocalDecodeUnavailable(f"MDT_FFMPEG={override!r} is not an executable file")
    exe = shutil.which(FFMPEG_BINARY)
    if exe is None:
        raise LocalDecodeUnavailable(
            "ffmpeg is not on PATH, so no local waveform can be decoded "
            "(set MDT_FFMPEG to an ffmpeg executable path to override - a "
            "packaged app launch does not inherit Homebrew's PATH)"
        )
    return exe


def _decode_peaks(path: Path) -> np.ndarray:
    """Peak columns for ``path`` via ffmpeg, or raise with a stated reason."""
    exe = _resolve_ffmpeg()
    command = [
        exe, "-nostdin", "-v", "error",
        "-i", str(path),
        "-map", "0:a:0",
        "-f", "s16le", "-acodec", "pcm_s16le",
        "-ac", "1", "-ar", str(DECODE_SAMPLE_RATE_HZ),
        "-",
    ]
    timed_out = threading.Event()
    # stderr goes to a temp FILE, not a pipe: nothing drains a second pipe
    # while stdout is being consumed, and a chatty decoder filling the stderr
    # buffer would deadlock the process forever.
    with tempfile.TemporaryFile() as errors:
        try:
            process = subprocess.Popen(  # fixed argv, never a shell
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=errors,
            )
        except OSError as exc:
            # The kernel, not ffmpeg, rejected the launch - a wrong-architecture
            # binary, a script with a missing interpreter, or the resolved path
            # vanishing between _resolve_ffmpeg's check and this spawn. Left
            # uncaught this propagates past LocalDecodeUnavailable's one catch
            # in local_anlz_payload, turning a should-be not_decoded response
            # into a 500 (discussion_r3909904294, issue #735 follow-up).
            raise LocalDecodeUnavailable(f"ffmpeg could not be launched: {exc}") from None
        stdout = process.stdout
        if stdout is None:  # pragma: no cover - Popen(stdout=PIPE) always sets it
            raise LocalDecodeUnavailable("ffmpeg stdout could not be opened")

        def _kill_on_deadline() -> None:
            timed_out.set()
            process.kill()

        watchdog = threading.Timer(DECODE_TIMEOUT_S, _kill_on_deadline)
        watchdog.start()
        try:
            peaks = _reduce_stream(stdout)
        finally:
            watchdog.cancel()
            stdout.close()
            returncode = process.wait()
        if timed_out.is_set():
            raise LocalDecodeUnavailable(
                f"ffmpeg did not finish decoding within {DECODE_TIMEOUT_S:.0f}s"
            )
        if returncode != 0:
            errors.seek(0)
            tail = errors.read().decode("utf-8", "replace").strip().splitlines()
            raise LocalDecodeUnavailable(
                f"ffmpeg exited {returncode}: {tail[-1] if tail else 'no stderr'}"
            )
    if peaks.size == 0:
        raise LocalDecodeUnavailable("ffmpeg decoded zero audio samples")
    return peaks


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


def _entry_is_current(entry: dict[str, Any], key: dict[str, Any]) -> bool:
    return (
        entry.get("schema") == config.LOCAL_WAVEFORM_CACHE_SCHEMA
        and entry.get("samples_per_column") == SAMPLES_PER_COLUMN
        and all(entry.get(name) == value for name, value in key.items())
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
    return np.frombuffer(base64.b64decode(entry["peaks_b64"]), dtype=np.uint8)


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
            "samples_per_column": SAMPLES_PER_COLUMN,
            "preview_b64": preview_b64,
            "preview_max": preview_max,
            **key,
        },
    )
    _write_json(
        _entry_path(stable_id),
        {
            "schema": config.LOCAL_WAVEFORM_CACHE_SCHEMA,
            "samples_per_column": SAMPLES_PER_COLUMN,
            "peaks_b64": base64.b64encode(peaks.tobytes()).decode("ascii"),
            **key,
        },
    )


# ----- payload shapes ---------------------------------------------------------


def _strip_from_peaks(peaks: np.ndarray) -> tuple[str | None, int | None]:
    """The 120x3 browser strip, or ``(None, None)`` for too few columns.

    Under 120 columns (below ~0.8 s of audio) there is nothing to downsample to
    the contract width, and padding it would be invented data.
    """
    if peaks.size < STRIP_COLUMNS:
        return None, None
    columns = np.repeat(peaks[:, np.newaxis], 3, axis=1)
    strip = _downsample_max(columns, STRIP_COLUMNS)
    return base64.b64encode(strip.tobytes()).decode("ascii"), int(strip.max())


def _mono_bands_from_peaks(peaks: np.ndarray) -> dict[str, np.ndarray]:
    """One decoded envelope 0..1 under all three band keys.

    Identical convention to ``waveform_bands._mono_bands`` for a rekordbox PWAV
    lane: ``kind="mono"`` tells the client these are single-band heights, so
    duplicating them is a declared shape, not three invented frequency bands.
    """
    scaled = np.clip(peaks.astype(np.float64) / _PEAK_SCALE, 0.0, 1.0)
    return {"low": scaled, "mid": scaled, "high": scaled}


def _waveform_payload(peaks: np.ndarray, points: int) -> dict[str, Any]:
    overview = _downsample_max(peaks, OVERVIEW_COLUMNS)
    return {
        "kind": "mono",
        "preview": _bands_payload(_mono_bands_from_peaks(overview), points),
        "detail": _bands_payload(_mono_bands_from_peaks(peaks), points),
    }


# ----- public surface ---------------------------------------------------------


def ensure_local_peaks(stable_id: str, *, share: bool = False) -> np.ndarray:
    """Decoded peaks for ``stable_id``, from cache or a fresh ffmpeg decode.

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
        key = _source_key(path)
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
                peaks = _decode_peaks(path)
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
        key = _source_key(Path(str(source)))
    except OSError:
        return None, None
    if not _entry_is_current(entry, key):
        return None, None
    return entry["preview_b64"], entry["preview_max"]


__all__ = [
    "DECODE_QUEUE_WAIT_S",
    "DECODE_SAMPLE_RATE_HZ",
    "DETAIL_COLUMNS_PER_S",
    "MAX_DECODE_WAITERS",
    "OVERVIEW_COLUMNS",
    "SAMPLES_PER_COLUMN",
    "STRIP_COLUMNS",
    "LocalDecodeUnavailable",
    "ensure_local_peaks",
    "local_anlz_payload",
    "local_preview_strip",
]
