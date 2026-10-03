"""The own key BACKFILL producer, as an `apps.analysis` backend.

`python -m apps.analysis.run --backend own_key.backfill --files track.wav`
writes an `AnalysisRecord` v2 row under backend ``own_key.backfill``, carrying
the `key` lane block that `/anlz` then serves (its `segments`) and that
`analysis_projection` then publishes (its scalars). The registry key and the
stored backend string are the same string for the reason
`apps/analysis/backends/own_beatgrid.py` spells out: `run.py` filters
already-analyzed tracks by the CLI's own `--backend` value, so an alias would
silently make `--only-missing` match nothing.

## What this producer is, exactly

The classical profile DSP of `specs/native-analysis-v1.md` section 5:
`librosa.feature.chroma_cqt` over a full-track decode (the same call
`scripts/keybench/run_krumhansl.py` scores the `krumhansl` bench arm with, 36
bins per octave and automatic tuning), Krumhansl-Kessler 1982 profiles from
`apps.analysis_key.profiles`, our own `apps.analysis_key.canon` canonicalizer
and `no_tonal_center`. It is model-FREE -- no weights of any kind, ours or
anyone's -- so every record it writes carries `uses_model=False` and
`model_sha256=None`.

The AGREE-fitted 24 templates and the HPCP-style front end that section 5
describes as the upgraded shipping producer are rung (a)/(b) of the classical
upgrade, are not on main, and are explicitly `nav1-key-r0`'s deliverable
(issue #1601). This producer does not invent them; when they land they are a
MINOR version bump here and a re-queue, not a new backend.

## Two things it reads besides the audio

**Own downbeats, from the canonical own beatgrid record.** Key-change
segmentation is bar-synchronous, so a track with no own grid yet gets
`segments: {status: missing, reason: no_own_downbeats}` -- the scalar key is
still published, and the block says the change analysis has not run rather
than guessing bars from the beat tracker this lane does not run (spec section
3). A missing state DB, a missing `analysis` schema, a missing canonical
pointer and a `failed` beatgrid lane are all the same answer for the same
reason: there are no own downbeats to segment over.

**The canonical decode fingerprint**, from `apps.analysis.pcm_fingerprint`.
It is taken from the AUDIO, not from the runner's own model-input hash, for
the reason own_beatgrid documents at length: it is the digest a second host,
or the player about to use this record, can recompute.

## Why the record carries key scalars in its pre-v2 columns too

`key_camelot`/`key_openkey`/`key_confidence` are filled with what this producer
measured, and left EMPTY (with a zero confidence, the same "not measured"
sentinel own_beatgrid uses for the fields it does not measure) when the lane
failed. Nothing reads them through a lane selector -- spec section 3 routes
every scalar reader through `effective_fields` and `analysis_projection` -- but
a row that hid its own measurement would make a stored record harder to audit
than the store requires.

-Claude
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from apps.analysis.pcm_fingerprint import (
    FingerprintUnavailable,
    canonical_decode_fingerprint,
    require_resampler,
)
from apps.analysis_key import canon, flags, profiles, segments
from apps.analysis_key.lane_payload import (
    REASON_NO_TONAL_CENTER,
    REASON_STALE_DEPENDENCY,
    build_key_lane,
    depends_on_identity,
)
from apps.analysis_key.version import LANE, PRODUCER, PRODUCER_VERSION
from apps.shared import engine_decode

from ..lanes import LaneResult, own_backend
from ..record import AnalysisRecord
from . import register
from .base import (
    BackendNotAvailable,
    TrackUnreadable,
    TrackVanished,
    librosa_numba_cache_roots,
)

log = logging.getLogger("apps.analysis.backends.own_key")

BACKEND_NAME = own_backend(LANE, PRODUCER)

#: What `librosa.load` needs, named so the failure that matters (a build
#: shipped without the `analysis` extra) reports which host-wide dependency is
#: missing rather than raising an ImportError from inside a decode.
REQUIRED_MODULES: tuple[str, ...] = ("librosa",)

#: The lane's own backend name, spelled here for the same reason
#: own_beatgrid spells it: `apps/analysis/backends/__init__.py` decides whether
#: to import this module at all, and importing it to learn its name would
#: defeat the laziness that branch exists for.
OWN_KEY_BACKEND = BACKEND_NAME


#-----------------------------------------------------------------------------
# payload -> record
#-----------------------------------------------------------------------------

def _record(
    *,
    stable_id: str,
    lane: Any,
    duration_s: float,
    sample_rate: int,
    decode_fingerprint: str,
) -> AnalysisRecord:
    """One lane block into one v2 record, with the pre-v2 columns derived."""
    key_camelot = ""
    key_openkey = ""
    key_confidence = 0.0
    if lane.ok:
        key_camelot = str(lane.payload["camelot"])
        key_openkey = str(lane.payload["openkey"])
        key_confidence = float(lane.payload["confidence"])
    return AnalysisRecord(
        stable_id=stable_id,
        backend=BACKEND_NAME,
        backend_version=PRODUCER_VERSION,
        analyzed_at=datetime.now(UTC),
        duration_s=duration_s,
        sample_rate=sample_rate,
        # This producer measures no tempo and no energy, so it states nothing
        # rather than a plausible-looking default; every own reader consults
        # the lane block (canonical.py projects from `lanes`).
        bpm=0.0,
        bpm_confidence=0.0,
        key_camelot=key_camelot,
        key_openkey=key_openkey,
        key_confidence=key_confidence,
        energy=0,
        energy_source="inferred",
        producer=PRODUCER,
        producer_version=PRODUCER_VERSION,
        # Model-free: no third-party weights and none of ours either.
        uses_model=False,
        model_sha256=None,
        decode_fingerprint=f"sha256:{decode_fingerprint}",
        lanes={
            LANE: LaneResult(
                status=lane.status,
                reason=lane.reason,
                confidence=lane.confidence,
                payload=lane.payload,
            )
        },
    )


def record_from_columns(
    *,
    stable_id: str,
    key: canon.Key,
    confidence: float,
    duration_s: float,
    sample_rate: int,
    decode_fingerprint: str,
    segments_block: dict[str, Any],
    depends_on_beatgrid: dict[str, Any] | None = None,
) -> AnalysisRecord:
    """An `ok` record from an already-known key. Not the producer's own path.

    Exists for callers that HAVE a key and need the record around it -- the
    notation round-trips and the store-level tests -- so those do not have to
    construct a chroma matrix to exercise a payload shape. `record_from_estimate`
    is what the producer calls, and it is the one that consults the flag.
    """
    estimate = profiles.KeyEstimate(key=key, confidence=confidence, margin=confidence)
    lane = build_key_lane(
        estimate,
        flags.TonalCenterFlag(
            no_tonal_center=False, reason=None,
            confidence=confidence, margin=confidence,
        ),
        segments_block,
        depends_on_beatgrid,
    )
    return _record(
        stable_id=stable_id, lane=lane, duration_s=duration_s,
        sample_rate=sample_rate, decode_fingerprint=decode_fingerprint,
    )


def record_from_estimate(
    *,
    stable_id: str,
    estimate: profiles.KeyEstimate,
    flag: flags.TonalCenterFlag,
    duration_s: float,
    sample_rate: int,
    decode_fingerprint: str,
    segments_block: dict[str, Any],
    depends_on_beatgrid: dict[str, Any] | None = None,
) -> AnalysisRecord:
    """The producer's own path: one estimate, its flag, one lane block.

    A flagged estimate becomes a `failed` lane carrying the reason, and the
    segment block is DISCARDED with it: a failed lane may carry no payload at
    all (the contract refuses one), and publishing segments beside a key the
    analyzer declined to name would attach a timeline to a guess.
    """
    lane = build_key_lane(estimate, flag, segments_block, depends_on_beatgrid)
    return _record(
        stable_id=stable_id, lane=lane, duration_s=duration_s,
        sample_rate=sample_rate, decode_fingerprint=decode_fingerprint,
    )


#-----------------------------------------------------------------------------
# own downbeats, from the store
#-----------------------------------------------------------------------------

def _read_only_state_conn(db_path: Path | None = None) -> Any:
    """A read-only state connection, or None when there is no state DB.

    None is a state with a correct answer here (`segments: missing`), not a
    failure to paper over: no state DB means no own beatgrid record can exist.
    """
    from apps.shared import paths as state_paths
    from apps.shared.state import db as state_db

    target = Path(db_path) if db_path is not None else state_paths.STATE_DB
    if not target.exists():
        return None
    return state_db.open_ro(target)


def _table_present(conn: Any, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def canonical_beatgrid_record(
    stable_id: str, *, db_path: Path | None = None,
) -> AnalysisRecord | None:
    """The canonical own beatgrid record, or None when there is none YET.

    None covers every way this track has no own grid YET (no state DB, no
    analysis schema, no canonical pointer, a lane that failed): all four mean
    the same thing to a bar-synchronous segmenter, and the block reports it as
    `missing` with one reason. A pointer that names a row that is not there, or
    a row carrying no `beatgrid` lane, is corruption rather than an ordinary
    state and raises -- the same distinction `own_beatgrid_overlay` draws.

    Returns the WHOLE record, not just its beats, so a caller can also build
    the `depends_on.beatgrid` identity block (backend, producer_version,
    model_sha256, decode_fingerprint) from the SAME read the beats came from,
    rather than a second query that could race a concurrent re-analysis.
    """
    conn = _read_only_state_conn(db_path)
    if conn is None:
        return None
    try:
        if not _table_present(conn, "analysis_canonical"):
            return None
        from apps.analysis.canonical import canonical_pointer

        pointer = canonical_pointer(conn, stable_id, "beatgrid")
        if pointer is None:
            return None
        row = conn.execute(
            "SELECT record_json FROM analysis "
            "WHERE stable_id = ? AND backend = ? AND backend_version = ?",
            (stable_id, pointer[0], pointer[1]),
        ).fetchone()
        if row is None:
            raise RuntimeError(
                f"canonical beatgrid pointer for {stable_id} names {pointer[0]}@"
                f"{pointer[1]} but no such analysis row exists"
            )
        record = AnalysisRecord.from_json(row[0])
        result = record.lanes.get("beatgrid")
        if result is None:
            raise RuntimeError(
                f"canonical beatgrid record {pointer[0]}@{pointer[1]} for "
                f"{stable_id} carries no 'beatgrid' lane"
            )
        if result.status != "ok":
            # The lane ran and could not grid the track. There are no downbeats
            # to segment over, and the key lane says `missing` for that rather
            # than inheriting another lane's failure reason.
            return None
        return record
    finally:
        conn.close()


#-----------------------------------------------------------------------------
# backend
#-----------------------------------------------------------------------------

def _require_deps() -> None:
    import importlib.util

    missing = [
        name for name in REQUIRED_MODULES
        if importlib.util.find_spec(name) is None
    ]
    if missing:
        raise BackendNotAvailable(
            f"{BACKEND_NAME} needs {missing} importable; install the `analysis` "
            "extra (librosa, scipy, soundfile) on this host"
        )


def _decode(audio_path: Path) -> tuple[Any, int]:
    """`(samples, sample_rate)` for a whole file, failing by name per file."""
    if engine_decode.needs_engine_decode(audio_path):
        return _engine_decode(audio_path)
    import librosa

    try:
        samples, sample_rate = librosa.load(str(audio_path), sr=None, mono=True)
    except FileNotFoundError as exc:
        raise TrackVanished(f"{audio_path.name}: {exc}") from exc
    except Exception as exc:
        raise TrackUnreadable(f"{audio_path.name}: {exc}") from exc
    if samples.size == 0:
        raise TrackUnreadable(f"empty audio: {audio_path.name}")
    return samples, int(sample_rate)


def _engine_decode(audio_path: Path) -> tuple[Any, int]:
    """Mono float samples from the engine, for a container librosa reads only via ffmpeg.

    librosa opens m4a through audioread, which needs ffmpeg, and the shipped
    app has none (NAE-23). The engine's mono mix is (L+R)/2, the same as
    librosa's.
    """
    import numpy as np

    try:
        pcm, sample_rate, _ = engine_decode.decode_f32(audio_path, mono=True)
    except engine_decode.EngineDecoderUnavailable as exc:
        raise BackendNotAvailable(f"the engine cannot decode {audio_path}: {exc}") from exc
    except engine_decode.EngineDecodeFailed as exc:
        if not audio_path.exists():
            raise TrackVanished(f"{audio_path.name} vanished before it was decoded") from None
        raise TrackUnreadable(f"{audio_path.name}: {exc}") from None
    # A read-only view of the engine's bytes, not a second whole-track copy:
    # librosa's own decode holds one copy, so this lane must not hold two.
    return np.frombuffer(pcm, dtype="<f4"), sample_rate


def analyze_audio(
    audio_path: Path, stable_id: str, *, db_path: Path | None = None
) -> AnalysisRecord:
    """The whole per-track pipeline: decode, chroma, estimate, segment, record.

    Split out of the backend class so tests can drive it with the file they
    wrote, and so the store path and the analysis path are not entangled: this
    function reads the analysis DB only for the beatgrid record it segments
    over.
    """
    import librosa

    require_resampler()
    decode_fingerprint = canonical_decode_fingerprint(audio_path)
    samples, sample_rate = _decode(audio_path)
    duration_s = float(len(samples)) / float(sample_rate)
    chroma = librosa.feature.chroma_cqt(y=samples, sr=sample_rate)
    times = librosa.times_like(chroma, sr=sample_rate)
    if canonical_decode_fingerprint(audio_path) != decode_fingerprint:
        raise TrackVanished(
            f"{audio_path} changed while key analysis was running, so its "
            "estimate came from bytes this record cannot vouch for"
        )

    estimate = profiles.estimate_key_krumhansl(chroma)
    flag = flags.evaluate_tonal_center(estimate)

    beatgrid_record = canonical_beatgrid_record(stable_id, db_path=db_path)
    depends_on_beatgrid: dict[str, Any] | None = None
    audio_fingerprint = f"sha256:{decode_fingerprint}"
    if beatgrid_record is None:
        block = segments.missing_block(segments.REASON_NO_DOWNBEATS)
    elif beatgrid_record.decode_fingerprint != audio_fingerprint:
        block = segments.missing_block(REASON_STALE_DEPENDENCY)
    else:
        beatgrid_lane = beatgrid_record.lanes["beatgrid"]
        block = segments.segment_audio(
            chroma, times, beatgrid_lane.payload["beats"], duration_s=duration_s
        )
        if block.status == "ok":
            depends_on_beatgrid = depends_on_identity(
                backend=beatgrid_record.backend,
                producer_version=beatgrid_record.producer_version,
                model_sha256=beatgrid_record.model_sha256,
                decode_fingerprint=beatgrid_record.decode_fingerprint,
                beatgrid_payload=beatgrid_lane.payload,
            )
    return record_from_estimate(
        stable_id=stable_id,
        estimate=estimate,
        flag=flag,
        duration_s=duration_s,
        sample_rate=sample_rate,
        decode_fingerprint=decode_fingerprint,
        segments_block=block.to_payload(),
        depends_on_beatgrid=depends_on_beatgrid,
    )


class OwnKeyBackfillBackend:
    """`apps.analysis.backends.base.AnalyzerBackend` for the own key lane."""

    name: str = BACKEND_NAME
    version: str = PRODUCER_VERSION

    @classmethod
    def analyze(
        cls, path: Path, stable_id: str, *, db_path: Path | None = None
    ) -> AnalysisRecord:
        _require_deps()
        audio_path = Path(path)
        if not audio_path.exists():
            raise TrackVanished(f"{audio_path} was gone before the decode opened it")
        try:
            return analyze_audio(audio_path, stable_id, db_path=db_path)
        except TrackUnreadable:
            if not audio_path.exists():
                raise TrackVanished(
                    f"{audio_path} vanished while it was being analyzed"
                ) from None
            raise
        except FingerprintUnavailable as exc:
            if not audio_path.exists():
                raise TrackVanished(
                    f"{audio_path} vanished while its fingerprint was being taken"
                ) from None
            raise TrackUnreadable(
                f"the canonical decode fingerprint could not be taken for "
                f"{audio_path}: {exc}"
            ) from None

    #: Length and rate of the synthetic warm-up signal. Long enough for the
    #: CQT's lowest-octave filters; numba caches per dtype signature, not per
    #: rate, so this compiles what a native-rate decode later loads.
    _WARMUP_SECONDS: float = 3.0
    _WARMUP_RATE_HZ: int = 22_050

    @classmethod
    def jit_cache_roots(cls) -> tuple[Path, ...]:
        return librosa_numba_cache_roots()

    @classmethod
    def warm_jit_cache(cls) -> str:
        """Drive ``chroma_cqt`` once, in the parent, before any worker exists.

        ``chroma_cqt`` reaches librosa's ``cache=True`` gufuncs (``piptrack``'s
        parabolic interpolation via ``estimate_tuning``). Left to the workers,
        a cold cache is compiled by all of them at once and poisoned for every
        later loader: issue #1316's SIGSEGV, seen on this lane by the NATIVE-10
        offline acceptance run. ``float32`` because ``librosa.load`` returns
        it and numba caches per type signature; fixed seed so a failure here
        reproduces.
        """
        _require_deps()
        import librosa
        import numpy as np

        rate = cls._WARMUP_RATE_HZ
        rng = np.random.default_rng(1316)
        samples = rng.standard_normal(int(rate * cls._WARMUP_SECONDS)).astype(np.float32)
        librosa.feature.chroma_cqt(y=samples * np.float32(0.05), sr=rate)
        return (
            f"{BACKEND_NAME}: warmed chroma_cqt on {cls._WARMUP_SECONDS:g}s "
            f"float32 @ {rate}Hz"
        )


register(OwnKeyBackfillBackend.name, OwnKeyBackfillBackend)

__all__ = [
    "BACKEND_NAME",
    "OWN_KEY_BACKEND",
    "REASON_NO_TONAL_CENTER",
    "REQUIRED_MODULES",
    "OwnKeyBackfillBackend",
    "analyze_audio",
    "canonical_beatgrid_record",
    "record_from_columns",
    "record_from_estimate",
]
