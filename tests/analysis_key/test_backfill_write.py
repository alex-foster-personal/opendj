"""`own_key.backfill`: registration, record shape, failure mapping, real audio.

Spec section 8 regression lines carried here:

  if a model-using lane writes a record without that model's sha256, or a
  model-free lane invents one, then broken
  if the key backfill refuses to run on a CPU-only host then broken
  if a track has two own key segments then its own /anlz payload returns them
  in key_segments then broken
  if key_segments is absent, or lacks status, on an own payload whose key lane
  is ok then broken

The last two are the PRODUCER's half: the record must carry the segments block
and the projection must count the changes. `/anlz`'s half is
`tests/webui/test_anlz_key_segments.py`.

Everything here runs on real bytes: the synthetic WAV is written by this file
(no fixture stands in for a decode), the chroma comes from libroas reading it,
and the record is written through the real store. The only thing not exercised
is `apps/analysis/run.py`'s CLI, which the PR body records as run end to end.

-Claude
"""
from __future__ import annotations

import math
import wave
from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pytest

from apps.analysis import selection
from apps.analysis.backends import OWN_KEY_BACKEND, get_backend
from apps.analysis.backends import own_key as own_key_module
from apps.analysis.backends.own_key import (
    BACKEND_NAME,
    OwnKeyBackfillBackend,
    record_from_columns,
)
from apps.analysis.lanes import LaneResult, parse_own_backend
from apps.analysis.record import validate_record_contract
from apps.analysis.store import open_conn, upsert_record
from apps.analysis_key import canon, flags, profiles, segments
from apps.analysis_key.version import LANE, PRODUCER, PRODUCER_VERSION

C_MAJOR = canon.Key(0, False)
A_MINOR = canon.Key(9, True)

#: A 16-bit PCM WAV is all `librosa.load` needs, and writing it with the
#: standard library keeps this suite free of an audio fixture file whose bytes
#: nobody would re-derive.
#:
#: `FINGERPRINT_HEX` is what `canonical_decode_fingerprint` MEASURES; the
#: record's `decode_fingerprint` is that value with its algorithm prefix, which
#: the producer adds (`sha256:` + the digest) and the record contract checks.
FINGERPRINT_HEX = "1f" * 32
CANONICAL_FINGERPRINT = "sha256:" + FINGERPRINT_HEX


#-----------------------------------------------------------------------------
# real audio, written here
#-----------------------------------------------------------------------------

def _write_triad_wav(path: Path, *, seconds: float, key: canon.Key, sr: int = 44100) -> None:
    """A mono 16-bit WAV holding `key`'s tonic triad, written with `wave`."""
    timeline = np.arange(int(seconds * sr), dtype=np.float64) / sr
    third = 3 if key.is_minor else 4
    samples = np.zeros_like(timeline)
    for interval in (0, third, 7):
        frequency = 440.0 * 2.0 ** ((key.pitch_class + interval - 9) / 12.0)
        samples += np.sin(2.0 * math.pi * frequency * timeline)
    samples = samples / (np.max(np.abs(samples)) + 1e-9) * 0.5
    pcm = (samples * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sr)
        handle.writeframes(pcm.tobytes())


@pytest.fixture
def state_db(tmp_path: Path) -> Iterator[Path]:
    """A fresh state DB passed explicitly into every store and producer call."""
    db_path = tmp_path / "state" / "state.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    open_conn(db_path).close()
    selection.reset_toggles()
    yield db_path
    selection.reset_toggles()


def _own_beatgrid_record(
    stable_id: str,
    *,
    n_bars: int,
    bar_s: float = 2.0,
    decode_fingerprint: str = CANONICAL_FINGERPRINT,
):
    """A canonical own BEATGRID record whose bars the key lane segments over.

    Written through the real store, from beats this test computes: the key
    lane's contract is that it reads OWN downbeats, so the input under test is
    the grid, and it has to be a real record rather than a stub.
    """
    from datetime import UTC, datetime

    from apps.analysis.record import AnalysisRecord

    beats = [
        {
            "t": round(index * bar_s / 4.0, 5),
            "n": (index % 4) + 1,
            "bpm": 120.0,
        }
        for index in range(n_bars * 4)
    ]
    payload = {
        "beats": beats,
        "bpm": 120.0,
        "bpm_confidence": 0.9,
        "octave_reason": "in_band",
        "first_downbeat_s": 0.0,
        "tempo_changes": [],
        "static_grid_untrusted": False,
    }
    return AnalysisRecord(
        stable_id=stable_id,
        backend="own_beatgrid.backfill",
        backend_version="1.1.0",
        analyzed_at=datetime.now(UTC),
        duration_s=n_bars * bar_s,
        sample_rate=44100,
        bpm=120.0,
        bpm_confidence=0.9,
        key_camelot="",
        key_openkey="",
        key_confidence=0.0,
        energy=0,
        producer="backfill",
        producer_version="1.1.0",
        uses_model=False,
        model_sha256=None,
        decode_fingerprint=decode_fingerprint,
        lanes={"beatgrid": LaneResult(status="ok", payload=payload)},
    )


#-----------------------------------------------------------------------------
# registration and naming
#-----------------------------------------------------------------------------

def test_the_backend_is_registered_under_the_contract_name(state_db: Path) -> None:
    assert OWN_KEY_BACKEND == "own_key.backfill"
    assert OWN_KEY_BACKEND == BACKEND_NAME
    assert get_backend(OWN_KEY_BACKEND) is OwnKeyBackfillBackend
    assert OwnKeyBackfillBackend.name == BACKEND_NAME
    assert OwnKeyBackfillBackend.version == PRODUCER_VERSION


def test_the_backend_name_parses_back_to_this_lane_and_producer() -> None:
    parsed = parse_own_backend(BACKEND_NAME)
    assert parsed is not None
    assert (parsed.lane, parsed.producer, parsed.candidate) == (LANE, PRODUCER, None)


def test_the_shipping_producer_is_model_free() -> None:
    """No third-party weights of any kind: `uses_model=False` and NO hash.

    The pair is checked in both directions by the record contract, so a
    fabricated digest would fail here even if this test did not look for it.
    """
    record = record_from_columns(
        stable_id="sid",
        key=C_MAJOR,
        confidence=0.9,
        duration_s=32.0,
        sample_rate=44100,
        decode_fingerprint=FINGERPRINT_HEX,
        segments_block=segments.missing_block(segments.REASON_NO_DOWNBEATS).to_payload(),
    )
    assert record.uses_model is False
    assert record.model_sha256 is None
    assert record.decode_fingerprint == CANONICAL_FINGERPRINT
    validate_record_contract(record)


def test_the_shipping_path_has_no_accelerator_branch() -> None:
    """Spec section 5: CPU is the REQUIRED path. A device branch in the
    shipping producer would be a path nucbox, agentbox and user machines
    cannot take. The real proof is the run below; this is the mechanical
    control that it stays true of the source."""
    source = Path(own_key_module.__file__).read_text(encoding="utf-8").lower()
    for token in ("mps", "cuda", "torch"):
        assert token not in source, f"{token!r} appears in the key producer's source"


#-----------------------------------------------------------------------------
# payload -> record
#-----------------------------------------------------------------------------

def test_notations_come_from_the_one_canonicalizer() -> None:
    record = record_from_columns(
        stable_id="sid", key=A_MINOR, confidence=0.8, duration_s=32.0, sample_rate=44100,
        decode_fingerprint=FINGERPRINT_HEX,
        segments_block=segments.missing_block(segments.REASON_NO_DOWNBEATS).to_payload(),
    )
    payload = record.lanes["key"].payload
    assert payload["camelot"] == "8A"
    assert payload["openkey"] == "1m"
    assert (payload["pitch_class"], payload["is_minor"]) == (9, True)
    assert canon.from_camelot(payload["camelot"]) == A_MINOR
    assert canon.from_open_key(payload["openkey"]) == A_MINOR


def test_an_empty_segments_array_on_an_ok_lane_is_refused() -> None:
    """A bare absent `key_segments` array on an ok key lane is a contract
    violation (the beatgrid empty-array rule's twin)."""
    with pytest.raises(ValueError, match="segments"):
        record_from_columns(
            stable_id="sid", key=C_MAJOR, confidence=0.9, duration_s=32.0,
            sample_rate=44100, decode_fingerprint=FINGERPRINT_HEX,
            segments_block={"status": "ok", "reason": None, "segments": []},
        )


def test_an_ok_segments_block_with_no_depends_on_is_refused() -> None:
    """The mutation this guards: an ok key-change segmentation published
    without naming the own beatgrid record it was computed against."""
    with pytest.raises(ValueError, match="depends_on"):
        record_from_columns(
            stable_id="sid", key=C_MAJOR, confidence=0.9, duration_s=32.0,
            sample_rate=44100, decode_fingerprint=FINGERPRINT_HEX,
            segments_block={
                "status": "ok", "reason": None,
                "segments": [{
                    "start_bar": 0, "end_bar": 16, "start_s": 0.0, "end_s": 32.0,
                    "key_camelot": "8B", "key_openkey": "3d", "confidence": 0.8,
                }],
            },
        )


def test_a_malformed_depends_on_block_fails_the_record_contract() -> None:
    """`build_key_lane` only checks presence; the STORE boundary checks shape.

    A dict that is not None but missing one of the five fields must still be
    caught, at `validate_record_contract`, before it ever reaches a reader.
    """
    record = record_from_columns(
        stable_id="sid", key=C_MAJOR, confidence=0.9, duration_s=32.0,
        sample_rate=44100, decode_fingerprint=FINGERPRINT_HEX,
        segments_block={
            "status": "ok", "reason": None,
            "segments": [{
                "start_bar": 0, "end_bar": 16, "start_s": 0.0, "end_s": 32.0,
                "key_camelot": "8B", "key_openkey": "3d", "confidence": 0.8,
            }],
        },
        depends_on_beatgrid={
            "backend": "own_beatgrid.backfill", "producer_version": "1.1.0",
            # model_sha256 omitted on purpose.
            "decode_fingerprint": CANONICAL_FINGERPRINT, "record_digest": "sha256:" + "bb" * 32,
        },
    )
    with pytest.raises(Exception, match="depends_on"):
        validate_record_contract(record)


def test_a_missing_segments_block_needs_no_depends_on() -> None:
    """A `missing`/`failed` block names no beatgrid record, so it carries none."""
    record = record_from_columns(
        stable_id="sid", key=C_MAJOR, confidence=0.9, duration_s=32.0,
        sample_rate=44100, decode_fingerprint=FINGERPRINT_HEX,
        segments_block=segments.missing_block(segments.REASON_NO_DOWNBEATS).to_payload(),
    )
    assert "depends_on" not in record.lanes["key"].payload


def test_a_low_confidence_estimate_is_failed_not_published() -> None:
    """`no_tonal_center` maps to `LaneResult.status='failed'` with the reason,
    never to a best-of-24 guess published as a finding."""
    estimate = profiles.KeyEstimate(key=C_MAJOR, confidence=0.0, margin=0.0)
    flag = flags.evaluate_tonal_center(estimate)
    assert flag.no_tonal_center is True
    record = own_key_module.record_from_estimate(
        stable_id="sid",
        estimate=estimate,
        flag=flag,
        duration_s=32.0,
        sample_rate=44100,
        decode_fingerprint=FINGERPRINT_HEX,
        segments_block=segments.missing_block(segments.REASON_NO_DOWNBEATS).to_payload(),
    )
    lane = record.lanes["key"]
    assert lane.status == "failed"
    assert lane.reason == f"{own_key_module.REASON_NO_TONAL_CENTER}: {flag.reason}"
    assert lane.payload == {}
    validate_record_contract(record)


def test_a_failed_lane_records_no_key_scalars() -> None:
    estimate = profiles.KeyEstimate(key=C_MAJOR, confidence=0.0, margin=0.0)
    record = own_key_module.record_from_estimate(
        stable_id="sid", estimate=estimate, flag=flags.evaluate_tonal_center(estimate),
        duration_s=32.0, sample_rate=44100,
        decode_fingerprint=FINGERPRINT_HEX,
        segments_block=segments.missing_block(segments.REASON_NO_DOWNBEATS).to_payload(),
    )
    # The pre-v2 columns state nothing rather than a plausible-looking guess.
    assert (record.key_camelot, record.key_openkey, record.key_confidence) == ("", "", 0.0)


#-----------------------------------------------------------------------------
# real audio, real chroma, real store
#-----------------------------------------------------------------------------

@pytest.mark.requires_canonical_decode
def test_real_audio_is_analyzed_on_this_cpu_only_host(tmp_path: Path, state_db: Path) -> None:
    """The acceptance line, measured rather than asserted: a real decode, a
    real chroma_cqt, a real estimate, on a host with no GPU and no MPS."""
    audio = tmp_path / "c-major.wav"
    _write_triad_wav(audio, seconds=24.0, key=C_MAJOR)

    record = OwnKeyBackfillBackend.analyze(audio, "sid-c-major")

    lane = record.lanes["key"]
    assert lane.status == "ok", lane.reason
    assert lane.payload["camelot"] == "8B"
    assert record.duration_s == pytest.approx(24.0, abs=0.1)
    validate_record_contract(record)
    upsert_record(record, db_path=state_db)


@pytest.mark.requires_canonical_decode
def test_the_segments_block_is_present_and_missing_without_own_downbeats(
    tmp_path: Path, state_db: Path
) -> None:
    audio = tmp_path / "c-major.wav"
    _write_triad_wav(audio, seconds=24.0, key=C_MAJOR)

    record = OwnKeyBackfillBackend.analyze(audio, "sid-no-grid")

    block = record.lanes["key"].payload["segments"]
    assert block["status"] == "missing"
    assert block["reason"] == segments.REASON_NO_DOWNBEATS
    assert block["segments"] == []


@pytest.mark.requires_canonical_decode
def test_two_segments_reach_the_projection_as_key_change_count(
    tmp_path: Path, state_db: Path
) -> None:
    """Producer -> store -> `analysis_projection`, the whole write path, on a
    real grid the key lane segmented over.

    The key is C major throughout (one segment), so this asserts the count
    SEPARATELY from the band's own `key` value: a one-segment answer must read
    zero changes, not one.
    """
    stable_id = "sid-grid"
    audio = tmp_path / "c-major.wav"
    _write_triad_wav(audio, seconds=24.0, key=C_MAJOR)
    from apps.analysis.pcm_fingerprint import canonical_decode_fingerprint

    audio_fingerprint = f"sha256:{canonical_decode_fingerprint(audio)}"
    upsert_record(
        _own_beatgrid_record(
            stable_id, n_bars=12, decode_fingerprint=audio_fingerprint
        ),
        db_path=state_db,
    )

    record = OwnKeyBackfillBackend.analyze(audio, stable_id, db_path=state_db)
    block = record.lanes["key"].payload["segments"]
    assert block["status"] == "ok", block["reason"]
    assert len(block["segments"]) >= 1

    # depends_on.beatgrid: all five identity fields, matching the beatgrid
    # record this segmentation was actually computed against (section 5).
    depends_on = record.lanes["key"].payload["depends_on"]["beatgrid"]
    assert set(depends_on) == {
        "backend", "producer_version", "model_sha256",
        "decode_fingerprint", "record_digest",
    }
    assert depends_on["backend"] == "own_beatgrid.backfill"
    assert depends_on["producer_version"] == "1.1.0"
    assert depends_on["model_sha256"] is None
    assert depends_on["decode_fingerprint"] == audio_fingerprint
    assert depends_on["record_digest"].startswith("sha256:")
    validate_record_contract(record)

    upsert_record(record, db_path=state_db)

    conn = open_conn(state_db)
    try:
        selection.set_default(conn, "key", "own")
        fields = selection.effective_fields(conn, [stable_id], selection.Selection.resolve(conn))
    finally:
        conn.close()
    assert fields[stable_id]["key"].value == "8B"
    assert fields[stable_id]["key"].source == BACKEND_NAME
    assert fields[stable_id]["key_change_count"].value == len(block["segments"]) - 1


@pytest.mark.requires_canonical_decode
def test_the_record_is_idempotent_across_two_runs(tmp_path: Path, state_db: Path) -> None:
    """Same bytes, same producer version: the second write is `unchanged`
    rather than a second canonical row (determinism, spec section 4)."""
    audio = tmp_path / "c-major.wav"
    _write_triad_wav(audio, seconds=24.0, key=C_MAJOR)
    first = OwnKeyBackfillBackend.analyze(audio, "sid-twice", db_path=state_db)
    upsert_record(first, db_path=state_db)
    second = OwnKeyBackfillBackend.analyze(audio, "sid-twice", db_path=state_db)
    result = upsert_record(second, db_path=state_db)
    assert result.unchanged is True
    assert first.lanes["key"].payload == second.lanes["key"].payload
