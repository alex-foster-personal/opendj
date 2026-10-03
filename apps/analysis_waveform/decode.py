"""Audio decode -> tri-band peak columns, for audio WE analyze ourselves.

**Two decoders, one contract.** The engine's own ``odj-audio waveform``
(symphonia decode plus the same biquads, ``apps/audio-engine/src/waveform.rs``)
needs nothing installed; ffmpeg is the original path and the fallback.
``MDT_WAVEFORM_DECODER`` picks: ``auto`` (default) uses the engine, except for
MP4-family files when ffmpeg is present (symphonia 0.6.1 ignores the MP4 edit
list, so AAC keeps its 1024-sample encoder delay: 23 ms late at 44.1 kHz,
measured Wed 1 Oct 2026); ``engine`` or ``ffmpeg`` force one and fail closed
when it is missing. On 44.1 kHz FLAC/MP3/WAV the two agree to within 1 of 255
on every column (r = 1.0000 per band over 54,000 columns of a real track). The
engine filters at the file's own rate instead of resampling to 44.1 kHz first,
so a 48 kHz file's high band keeps its 22-24 kHz content. The decoder is part
of the cache key (``local_waveform``), so switching it rebuilds, never mixes.

Split out of ``local_waveform.py`` when the tri-band split landed (NATIVE-06):
that module is the cache, the payload shapes and the admission gate, this one
is "bytes on disk -> ``(columns, 3)`` uint8 peaks" and nothing else. Neither
half reaches the filesystem cache from here.

**Why three bands and not one.** rekordbox's own color waveform is a three-band
envelope (PWV6 preview / PWV7 detail, 3 bytes per column: low, mid, high). Until
this module existed a locally imported track got ``kind: "mono"`` - one real
envelope duplicated under three band keys, honest but visibly different from
every rekordbox-analyzed track in the same deck. The bands here are measured
from the same audio, never synthesized from a mono envelope.

**Crossovers: 200 Hz and 4 kHz.** Stated, not discovered: rekordbox publishes no
band definition and its PWV6 bytes cannot be inverted to one. 200/4000 is the
brief's contract and matches the conventional 3-band DJ split (kick and bass
body under 200 Hz; the whole vocal/synth midrange to 4 kHz; hats, cymbals and
air above). ``score.py`` measures what this choice is worth against real PWV6
truth per band, so the number moves the constant rather than an opinion doing it.

**Filter shape.** Two cascaded 2-pole Butterworth sections per edge, so 24
dB/octave. One section (12 dB/oct) leaks a 12 kHz tone into the "mid" band at
0.11 of full scale and a 60 Hz tone at 0.090, either of which draws a visible
mid bar that is not there; cascading squares those to 0.004 and 0.008.
``FILTER_SECTIONS`` is the knob, and the experiment log in
``specs/native-analysis-v1.md`` records what each value scored, including the
measurement showing the cheaper filter is not measurably cheaper.

**Sample rate: 44.1 kHz, not the 8 kHz the mono decoder used.** The high band is
everything above 4 kHz, and 8 kHz sampling has a 4 kHz Nyquist - the band that
defines the split would be entirely unrepresentable. 44.1 kHz also makes the
column density exact: 44100 / 294 = 150.0 columns/s on the nose, where 8000/53
was 150.94.

**One ffmpeg process, three bands.** The three band chains are merged into one
3-channel interleaved s16le stream (``amerge``), so a track is decoded once, not
three times. Channel order is input order: low, mid, high. That ordering is not
taken on trust - ``tests/analysis_waveform/test_band_split.py`` puts a distinct
tone in each band and fails if any of them lands in the wrong column.

PCM is never buffered whole: 44.1 kHz x 3 channels x 2 bytes is 265 KB per
second of audio (an hour is 950 MB), so the reduction consumes ffmpeg's stdout
in fixed chunks and keeps only the uint8 peak columns - 450 bytes per second of
audio, 1.6 MB for that same hour.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import threading
from dataclasses import dataclass, fields
from pathlib import Path
from typing import IO, Literal

import numpy as np

from apps.shared.ffmpeg import FfmpegUnavailable
from apps.shared.ffmpeg import resolve_ffmpeg as _shared_resolve_ffmpeg

BAND_NAMES: tuple[str, str, str] = ("low", "mid", "high")
# stdout is consumed in chunks; only the uint8 peak columns are retained.
READ_CHUNK_BYTES: int = 1 << 20

_PEAK_SCALE: int = 255  # int16 |amplitude| >> 7, so a full-scale sine hits 255
_INT16_MAX: int = 32_767


@dataclass(frozen=True, slots=True)
class DecodeProfile:
    """Everything that decides what a decoded peak column MEANS.

    A VALUE, not a set of module constants, and every function below takes one.
    That is what lets a test exercise a different producer configuration by
    CONSTRUCTING a real ``DecodeProfile`` - the same production type the shipped
    path uses - instead of monkeypatching a module attribute, which AGENTS.md
    "No mocks and locked real fixtures" forbids outright (Codex review,
    PR #1536). It is also what makes an experiment round explicit: the crossover
    sweep in the spec's experiment log passes profiles, so the graph that
    produced a number travels with it instead of living in a patch nobody can
    see from the result.
    """

    # Full CD rate, so the > 4 kHz band exists at all (see the module docstring).
    sample_rate_hz: int = 44_100
    # Target detail density for the locally decoded lane and ANLZ display.
    # Private-library sampling evidence is unavailable in this public copy.
    detail_columns_per_s: int = 150
    # Target preview length; this is configuration, not a published census.
    overview_columns: int = 1200
    band_count: int = 3
    crossover_low_hz: int = 200
    crossover_high_hz: int = 4_000
    # Cascaded 2-pole Butterworth sections per edge: 2 sections = 24 dB/octave.
    filter_sections: int = 2
    timeout_s: float = 180.0

    @property
    def samples_per_column(self) -> int:
        """44100 / 150 divides exactly, so the real density IS 150.0 columns/s."""
        return self.sample_rate_hz // self.detail_columns_per_s

    @property
    def frame_bytes(self) -> int:
        return self.band_count * 2

    @property
    def column_bytes(self) -> int:
        return self.samples_per_column * self.frame_bytes

    def identity(self) -> str:
        """Every field, in declaration order, as one ":"-joined string.

        Built by walking the dataclass fields rather than by naming them, so a
        field ADDED here cannot be forgotten in the cache key that consumes this
        (``local_waveform.peaks_version``). ``timeout_s`` is included: it does
        not change what a COMPLETED decode means, but a shorter deadline changes
        which decodes complete, and a cache key that is a superset of what
        matters only costs a rebuild.
        """
        return ":".join(str(getattr(self, f.name)) for f in fields(self))


PROFILE: DecodeProfile = DecodeProfile()

# Read-only conveniences for callers that only report the shipped values. Every
# FUNCTION reads its profile argument, so rebinding one of these changes nothing
# - deliberately, because a rebindable constant is the monkeypatch surface this
# module just removed.
DECODE_SAMPLE_RATE_HZ: int = PROFILE.sample_rate_hz
DETAIL_COLUMNS_PER_S: int = PROFILE.detail_columns_per_s
SAMPLES_PER_COLUMN: int = PROFILE.samples_per_column
OVERVIEW_COLUMNS: int = PROFILE.overview_columns
BAND_COUNT: int = PROFILE.band_count
CROSSOVER_LOW_HZ: int = PROFILE.crossover_low_hz
CROSSOVER_HIGH_HZ: int = PROFILE.crossover_high_hz
FILTER_SECTIONS: int = PROFILE.filter_sections
DECODE_TIMEOUT_S: float = PROFILE.timeout_s


class LocalDecodeUnavailable(Exception):
    """The decode did not run, and why. Surfaced in the payload, never hidden.

    ``retryable`` marks a TRANSIENT condition (decoder momentarily saturated)
    as opposed to a fact about the track itself (no ffmpeg, corrupt audio,
    missing file). The distinction matters downstream: a retryable failure
    must never be cached as if it were permanent - see its use in
    ``local_waveform.local_anlz_payload`` and the route's Cache-Control choice.
    """

    def __init__(self, reason: str, *, retryable: bool = False) -> None:
        super().__init__(reason)
        self.reason: str = reason
        self.retryable: bool = retryable


# ----- streaming peak reduction -----------------------------------------------


def _column_peaks(block: bytes, profile: DecodeProfile) -> np.ndarray:
    """Peak byte per band per whole column in ``block`` (a multiple of column_bytes).

    The stream is frame-interleaved (low, mid, high, low, mid, high, ...), so a
    column is ``samples_per_column`` frames and the max runs down the frame axis.
    """
    samples = np.frombuffer(block, dtype="<i2").reshape(
        -1, profile.samples_per_column, profile.band_count
    )
    # -32768 has no positive int16 twin; clamp before the shift so one sample
    # cannot wrap a column to 0.
    amplitude = np.minimum(np.abs(samples.astype(np.int32)), _INT16_MAX)
    return (amplitude.max(axis=1) >> 7).astype(np.uint8)


def _tail_peak(block: bytes, profile: DecodeProfile) -> np.ndarray:
    """One final column for a short tail. Real samples, never padded to width."""
    usable = len(block) - len(block) % profile.frame_bytes
    samples = np.frombuffer(block[:usable], dtype="<i2").reshape(-1, profile.band_count)
    amplitude = np.minimum(np.abs(samples.astype(np.int32)), _INT16_MAX)
    return (amplitude.max(axis=0, keepdims=True) >> 7).astype(np.uint8)


def _reduce_stream(
    stream: IO[bytes],
    profile: DecodeProfile = PROFILE,
    *,
    chunk_bytes: int = READ_CHUNK_BYTES,
) -> np.ndarray:
    """Peak columns ``(n, 3)`` for a PCM stream, holding at most one chunk.

    A read boundary lands anywhere, so bytes past the last whole column carry
    over into the next chunk. Reducing as we go is what keeps an hour-long
    track at ~1.6 MB of peaks instead of ~950 MB of PCM.
    """
    columns: list[np.ndarray] = []
    remainder = b""
    while True:
        chunk = stream.read(chunk_bytes)
        if not chunk:
            break
        buffer = remainder + chunk if remainder else chunk
        whole = len(buffer) - len(buffer) % profile.column_bytes
        if whole:
            columns.append(_column_peaks(buffer[:whole], profile))
        remainder = buffer[whole:]
    if len(remainder) >= profile.frame_bytes:
        columns.append(_tail_peak(remainder, profile))
    if not columns:
        return np.empty((0, profile.band_count), dtype=np.uint8)
    return np.concatenate(columns)


def _peak_columns(
    pcm: bytes, profile: DecodeProfile = PROFILE, *, chunk_bytes: int = READ_CHUNK_BYTES
) -> np.ndarray:
    """Whole-buffer form of :func:`_reduce_stream`, for callers holding bytes."""
    return _reduce_stream(io.BytesIO(pcm), profile, chunk_bytes=chunk_bytes)


# ----- decoder choice ---------------------------------------------------------

Decoder = Literal["engine", "ffmpeg"]
DECODER_ENV: str = "MDT_WAVEFORM_DECODER"
DECODER_CHOICES: tuple[str, ...] = ("auto", "engine", "ffmpeg")
#: Containers whose encoder delay lives in an MP4 edit list, which the engine's
#: symphonia 0.6.1 does not apply (see the module docstring).
EDIT_LIST_SUFFIXES: frozenset[str] = frozenset({".m4a", ".mp4", ".m4b", ".aac", ".m4v"})


_ENGINE_CAPABLE: dict[tuple[str, int, int], bool] = {}
_ENGINE_CAPABLE_LOCK = threading.Lock()


def _engine_has_waveform(exe: str) -> bool:
    """Whether this ``odj-audio`` build lists ``waveform`` in ``version``.

    A build older than the subcommand prints no ``commands`` and is refused
    here rather than failing every decode: a reused CI workspace kept exactly
    such a stale cargo build, and ``auto`` picked it over a working ffmpeg.
    Cached per binary path, size and mtime, so a rebuild is re-asked.
    """
    try:
        st = os.stat(exe)
    except OSError:
        return False
    key = (exe, st.st_size, st.st_mtime_ns)
    with _ENGINE_CAPABLE_LOCK:
        known = _ENGINE_CAPABLE.get(key)
    if known is not None:
        return known
    try:
        done = subprocess.run(  # fixed argv, never a shell
            [exe, "version"], stdin=subprocess.DEVNULL, capture_output=True, timeout=10, check=False
        )
        info = json.loads(done.stdout.decode("utf-8").strip().splitlines()[0])
        capable = done.returncode == 0 and "waveform" in (info.get("commands") or [])
    except (OSError, subprocess.TimeoutExpired, ValueError, IndexError, AttributeError):
        capable = False
    with _ENGINE_CAPABLE_LOCK:
        _ENGINE_CAPABLE[key] = capable
    return capable


def resolve_engine() -> str:
    """The ``odj-audio`` binary, by the supervisor's own rule, or raise.

    Same lookup as the playback engine (``ODJ_AUDIO_BIN`` wins and is never
    second-guessed, else the newest local cargo build), so a packaged app's
    waveform and its playback come from one binary. A build without the
    ``waveform`` subcommand counts as unavailable, with that reason.
    """
    from apps.shared.odj_audio_binary import OdjAudioUnavailable, find_binary
    from apps.shared.platform_paths import PROJECT_ROOT

    try:
        exe = str(find_binary(os.environ, PROJECT_ROOT).path)
    except OdjAudioUnavailable as exc:
        raise LocalDecodeUnavailable(f"odj-audio unavailable: {exc}") from None
    if not _engine_has_waveform(exe):
        raise LocalDecodeUnavailable(
            f"odj-audio unavailable: {exe} has no waveform command (an older build; "
            "rebuild apps/audio-engine)"
        )
    return exe


def decoder_mode() -> str:
    """The ``DECODER_ENV`` setting as written, lowercased; unset is ``auto``."""
    return os.environ.get(DECODER_ENV, "auto").strip().lower() or "auto"


def select_decoder(path: Path) -> Decoder:
    """Which decoder will produce ``path``'s peaks, or raise with why none can.

    A pure function of the environment, what is installed and the file's
    suffix, so the cache key can name the decoder before decoding anything.
    """
    raw = decoder_mode()
    if raw not in DECODER_CHOICES:
        raise LocalDecodeUnavailable(
            f"{DECODER_ENV}={raw!r} is not one of {', '.join(DECODER_CHOICES)}"
        )
    if raw == "engine":
        resolve_engine()
        return "engine"
    if raw == "ffmpeg":
        resolve_ffmpeg()
        return "ffmpeg"
    reasons: list[str] = []
    order: tuple[Decoder, Decoder] = (
        ("ffmpeg", "engine") if path.suffix.lower() in EDIT_LIST_SUFFIXES else ("engine", "ffmpeg")
    )
    for name in order:
        try:
            resolve_engine() if name == "engine" else resolve_ffmpeg()
        except LocalDecodeUnavailable as exc:
            reasons.append(exc.reason)
            continue
        return name
    raise LocalDecodeUnavailable("no waveform decoder: " + "; ".join(reasons))


def decode_peaks(path: Path, profile: DecodeProfile = PROFILE) -> np.ndarray:
    """Tri-band peak columns ``(n, 3)`` for ``path``, or raise with a reason."""
    return decode_peaks_from(select_decoder(path), path, profile)


def decode_peaks_from(
    decoder: Decoder, path: Path, profile: DecodeProfile = PROFILE
) -> np.ndarray:
    """``decode_peaks`` with the decoder named, for callers that keyed on it."""
    return decode_peaks_measured(decoder, path, profile)[0]


def decode_peaks_measured(
    decoder: Decoder, path: Path, profile: DecodeProfile = PROFILE
) -> tuple[np.ndarray, int]:
    """Peaks and the sample rate the bands were filtered at.

    ffmpeg resamples to ``profile.sample_rate_hz`` first; the engine filters
    at the file's own rate, which it reports.
    """
    peaks, rate, _used = decode_peaks_used(decoder, path, profile)
    return peaks, rate


def decode_peaks_used(
    decoder: Decoder, path: Path, profile: DecodeProfile = PROFILE
) -> tuple[np.ndarray, int, Decoder]:
    """``decode_peaks_measured`` plus the decoder that actually produced them.

    Under ``auto`` a file the engine refuses (e.g. an 8 kHz WAV whose Nyquist
    sits under the high crossover) still decodes through ffmpeg, which
    resamples first, as it did before the engine path; a cache must record
    that ffmpeg made these peaks. A forced engine stays fail-closed, and so
    does a retryable error.
    """
    if decoder == "engine":
        try:
            peaks, rate = _decode_peaks_engine(path, profile)
            return peaks, rate, "engine"
        except LocalDecodeUnavailable as engine_exc:
            if engine_exc.retryable or decoder_mode() != "auto":
                raise
            try:
                resolve_ffmpeg()
            except LocalDecodeUnavailable:
                raise engine_exc from None
    return _decode_peaks_ffmpeg(path, profile), profile.sample_rate_hz, "ffmpeg"


# ----- engine decode ----------------------------------------------------------


def _decode_peaks_engine(path: Path, profile: DecodeProfile) -> tuple[np.ndarray, int]:
    """``odj-audio waveform``: peaks to a temp file, one JSON summary line out."""
    exe = resolve_engine()
    with tempfile.TemporaryDirectory(prefix="odj-waveform-") as scratch:
        out = Path(scratch) / "peaks.u8"
        command = [
            exe, "waveform",
            "--in", str(path),
            "--out", str(out),
            "--low-hz", str(profile.crossover_low_hz),
            "--high-hz", str(profile.crossover_high_hz),
            "--sections", str(profile.filter_sections),
            "--columns-per-s", str(profile.detail_columns_per_s),
        ]
        try:
            done = subprocess.run(  # fixed argv, never a shell
                command,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=profile.timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired:
            # RETRYABLE, for the reason the ffmpeg path's deadline gives.
            raise LocalDecodeUnavailable(
                f"odj-audio did not finish decoding within {profile.timeout_s:.3g}s",
                retryable=True,
            ) from None
        except OSError as exc:
            raise LocalDecodeUnavailable(f"odj-audio could not be launched: {exc}") from None
        if done.returncode != 0:
            tail = done.stderr.decode("utf-8", "replace").strip().splitlines()
            raise LocalDecodeUnavailable(
                f"odj-audio exited {done.returncode}: {tail[-1] if tail else 'no stderr'}"
            )
        try:
            summary = json.loads(done.stdout.decode("utf-8").strip().splitlines()[-1])
            raw = out.read_bytes()
        except (ValueError, IndexError, OSError) as exc:
            raise LocalDecodeUnavailable(f"odj-audio wrote no readable peaks: {exc}") from None
    if len(raw) % profile.band_count or len(raw) // profile.band_count != summary.get("columns"):
        raise LocalDecodeUnavailable(
            f"odj-audio wrote {len(raw)} bytes for {summary.get('columns')} columns"
        )
    if not raw:
        raise LocalDecodeUnavailable("odj-audio decoded zero audio samples")
    peaks = np.frombuffer(raw, dtype=np.uint8).reshape(-1, profile.band_count).copy()
    return peaks, int(summary.get("sample_rate") or 0)


# ----- ffmpeg decode ----------------------------------------------------------


def resolve_ffmpeg() -> str:
    """ffmpeg executable path, as this module's own failure type.

    The lookup itself (MDT_FFMPEG override, else PATH, never a silent
    fallback behind a broken override) lives in :mod:`apps.shared.ffmpeg`,
    because more than one caller needs the same answer to "where is ffmpeg"
    and two copies of that rule would drift. All this adds is the translation
    to LocalDecodeUnavailable, so a missing decoder still surfaces to the
    waveform routes as an honest ``not_decoded`` reason rather than a 500.

    Unlike scripts/vocal_region_worker.py nothing here mutates
    os.environ["PATH"]: this module runs inside a long-lived, multi-threaded
    server process, where a process-wide PATH mutation on a request path
    would race every concurrently-decoding request.
    """
    try:
        return _shared_resolve_ffmpeg()
    except FfmpegUnavailable as exc:
        raise LocalDecodeUnavailable(str(exc)) from None


def _sections(kind: str, hz: int, sections: int) -> str:
    """``sections`` cascaded 2-pole ``lowpass``/``highpass`` stages."""
    return ",".join([f"{kind}=f={hz}:poles=2"] * sections)


def band_filter_graph(profile: DecodeProfile = PROFILE) -> str:
    """The ``-filter_complex`` graph: one decode, three bands, one merged stream.

    Public (no underscore) because the scorer and the fixture builder print it
    into their reports: the crossover choice has to travel with every number
    measured under it, or a later round cannot attribute a delta to it.
    """
    low, high, n = profile.crossover_low_hz, profile.crossover_high_hz, profile.filter_sections
    return (
        "[0:a:0]aformat=sample_fmts=fltp:sample_rates="
        f"{profile.sample_rate_hz}:channel_layouts=mono,asplit={profile.band_count}[a][b][c];"
        f"[a]{_sections('lowpass', low, n)}[low];"
        f"[b]{_sections('highpass', low, n)},{_sections('lowpass', high, n)}[mid];"
        f"[c]{_sections('highpass', high, n)}[high];"
        f"[low][mid][high]amerge=inputs={profile.band_count}[out]"
    )


def _decode_peaks_ffmpeg(path: Path, profile: DecodeProfile) -> np.ndarray:
    """The ffmpeg filter graph decode."""
    exe = resolve_ffmpeg()
    command = [
        exe, "-nostdin", "-v", "error",
        "-i", str(path),
        "-filter_complex", band_filter_graph(profile),
        "-map", "[out]",
        "-f", "s16le", "-acodec", "pcm_s16le",
        "-ar", str(profile.sample_rate_hz),
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
            # vanishing between resolve_ffmpeg's check and this spawn. Left
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

        watchdog = threading.Timer(profile.timeout_s, _kill_on_deadline)
        watchdog.start()
        try:
            peaks = _reduce_stream(stdout, profile)
        finally:
            watchdog.cancel()
            stdout.close()
            returncode = process.wait()
        if timed_out.is_set():
            # RETRYABLE. A deadline miss is a statement about the host at that
            # moment, not about the file: a 44.1 kHz three-band decode on a
            # contended machine can miss a wall clock that the same file clears
            # easily when the machine is quiet. Marked permanent, the route
            # sends the ordinary cacheable response rather than `no-store`, and
            # one overloaded moment leaves perfectly good audio reading
            # `not_decoded` to every client that keeps it (Sol review, PR
            # #1536).
            raise LocalDecodeUnavailable(
                f"ffmpeg did not finish decoding within {profile.timeout_s:.3g}s",
                retryable=True,
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


__all__ = [
    "BAND_COUNT",
    "BAND_NAMES",
    "CROSSOVER_HIGH_HZ",
    "CROSSOVER_LOW_HZ",
    "DECODER_ENV",
    "DECODE_SAMPLE_RATE_HZ",
    "DECODE_TIMEOUT_S",
    "DETAIL_COLUMNS_PER_S",
    "FILTER_SECTIONS",
    "OVERVIEW_COLUMNS",
    "PROFILE",
    "SAMPLES_PER_COLUMN",
    "DecodeProfile",
    "Decoder",
    "LocalDecodeUnavailable",
    "band_filter_graph",
    "decode_peaks",
    "decode_peaks_from",
    "decode_peaks_measured",
    "resolve_engine",
    "select_decoder",
]
