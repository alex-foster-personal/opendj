"""GB-1 (#5985): the analyzed key reaches the All Tracks listing wire.

[if] an own key lane has a record for a track [then] GET /api/v1/tracks serves
a NON-EMPTY `key` for that row, [else stop].

The reported defect: on an empty data dir with no rekordbox, the librosa drain
writes a key, but the listing row carries a blank `key` cell. The producer half
(record -> ``analysis_projection``) is already covered by
``tests/analysis/test_key_projection_no_track_fields_write.py`` and
``(private path)``. This module pins the WIRE half, which
is where the row is assembled: ``rb_vendor.build_track_rows`` ->
``_lane_or_track_value`` -> ``TrackListItemOut``.

No rekordbox and no ``track_fields`` key on purpose: the only possible source
of the value is the own lane's projection, so a blank cell here is the bug and
nothing else can supply it.

The real-drain leg needs the ``odj-audio`` decode binary for the canonical
decode fingerprint (the same capability limit that skips two tests in
``tests/analysis/test_key_projection_no_track_fields_write.py``), so it carries
``requires_canonical_decode``: on a host without that build it reports
UNAVAILABLE rather than passing, and on one with it, a drain that produces or
persists no key turns the leg red. It also carries ``requires_audio_stack``,
because ``own_key.backfill`` needs librosa and ``drain.run`` warms that
backend's JIT cache before it decodes: a host with the engine build but without
the ``analysis`` extra must report the leg UNAVAILABLE, not fail it. The
record-level leg below starts at the record the drain writes, so the wire
behaviour stays pinned even where the drain itself cannot run.
"""
from __future__ import annotations

import math
import wave
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from apps.adapters.rekordbox import config as rb_config
from apps.analysis import run as drain
from apps.analysis import store as analysis_store
from apps.analysis.lane_payloads import _CAMELOT_PITCH_CLASS
from apps.analysis.lanes import LaneResult
from apps.analysis.record import AnalysisRecord
from apps.shared.state import db as state_db
from apps.shared.state.events import FakeEventBus
from apps.shared.state.writer import StateWriter
from apps.webui.server.app import create_app
from apps.webui.server.sqlite_backend import SqliteBackend

#: No ``pytest.mark.requirement``: GB-1 is an issue, not a reqs.json requirement,
#: and ``(private path)::test_no_orphan_requirement_markers``
#: refuses an ID that is not registered there (measured on PR #7309, shard 4).

STABLE_ID = "d" * 40
DECODE_FINGERPRINT = "sha256:" + "3c" * 32
DEPENDS_ON_BEATGRID = {
    "backend": "own_beatgrid.backfill", "producer_version": "1.0.0",
    "model_sha256": None, "decode_fingerprint": "sha256:" + "1f" * 32,
    "record_digest": "sha256:" + "2f" * 32,
}


def _write_a_minor_wav(path: Path, *, seconds: float = 12.0, sr: int = 44100) -> None:
    """A minor's tonic triad (root 9, minor third 0, fifth 4) as real audio.

    The same synthetic signal ``tests/analysis/test_key_projection_no_track_fields_write.py``
    feeds the real producer: audible to a decoder, unambiguous to the
    Krumhansl estimate, and generated here so the drain leg needs no fixture.
    """
    timeline = np.arange(int(seconds * sr), dtype=np.float64) / sr
    samples = np.zeros_like(timeline)
    for pitch_class in (9, 0, 4):
        frequency = 440.0 * 2.0 ** ((pitch_class - 9) / 12.0)
        samples += np.sin(2.0 * math.pi * frequency * timeline)
    samples = samples / (np.max(np.abs(samples)) + 1e-9) * 0.5
    pcm = (samples * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sr)
        handle.writeframes(pcm.tobytes())


def _persisted_key(path: Path) -> str | None:
    """What the STORE holds for the `key` scalar, read from the projection."""
    conn = analysis_store.open_conn(path)
    try:
        row = conn.execute(
            "SELECT value FROM analysis_projection WHERE stable_id = ? AND field = 'key'",
            (STABLE_ID,),
        ).fetchone()
    finally:
        conn.close()
    return None if row is None else row[0]


def _key_payload(camelot: str) -> dict:
    """Derived from the Camelot table, so the contract cross-checks all spellings."""
    pitch_class, is_minor = _CAMELOT_PITCH_CLASS[camelot]
    return {
        "camelot": camelot, "openkey": "1m", "pitch_class": pitch_class,
        "is_minor": is_minor, "confidence": 0.8,
        "segments": {
            "status": "ok", "reason": None,
            "segments": [
                {"start_bar": 0, "end_bar": 8, "start_s": 0.0, "end_s": 16.0,
                 "key_camelot": camelot, "key_openkey": "1m", "confidence": 0.8},
            ],
        },
        "depends_on": {"beatgrid": dict(DEPENDS_ON_BEATGRID)},
    }


def _own_key_record(stable_id: str) -> AnalysisRecord:
    return AnalysisRecord(
        stable_id=stable_id, backend="own_key.backfill", backend_version="1.0.0",
        analyzed_at=datetime(2026, 10, 8, tzinfo=UTC),
        duration_s=200.0, sample_rate=44100,
        bpm=120.0, bpm_confidence=0.9,
        key_camelot="8A", key_openkey="1m", key_confidence=0.8,
        energy=5, energy_source="inferred",
        producer="backfill", producer_version="1.0.0", uses_model=False,
        model_sha256=None, decode_fingerprint=DECODE_FINGERPRINT,
        lanes={"key": LaneResult(status="ok", payload=_key_payload("8A"))},
    )


@pytest.fixture
def empty_library_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty data dir: one track, NO track_fields key, no rekordbox master."""
    path = tmp_path / "state.db"
    conn = state_db.open_rw(path)
    writer = StateWriter(conn, bus=FakeEventBus(), actor="test")
    try:
        writer.upsert_track(
            stable_id=STABLE_ID,
            stable_id_tier="inferred",
            title="Own Key Only",
            artists=[],
            album=None,
            isrc=None,
            duration_ms=200_000,
            file_path=str(tmp_path / "owned.wav"),
        )
    finally:
        writer.close()
        conn.close()
    monkeypatch.setattr(rb_config, "STATE_DB", path)
    monkeypatch.setattr(rb_config, "MASTER_PLAIN_DB", tmp_path / "absent-master.db")
    monkeypatch.setattr(rb_config, "LOCAL_WAVEFORM_CACHE_DIR", tmp_path / "wf-cache")
    return path


def _write_key_record(path: Path) -> None:
    """What the drain leaves behind: the analysis record + its projection row."""
    conn = analysis_store.open_conn(path)
    try:
        analysis_store.upsert_record(_own_key_record(STABLE_ID), conn=conn)
        projected = conn.execute(
            "SELECT value FROM analysis_projection WHERE stable_id = ? AND field = 'key'",
            (STABLE_ID,),
        ).fetchone()
    finally:
        conn.close()
    # The projection row is the drain's side effect; without it the reader has
    # nothing to serve and the listing is blank for a reason this test names.
    assert projected is not None, "the record write did not project `key`"


def _listing_item(path: Path) -> dict:
    app = create_app()
    app.state.backend = SqliteBackend(path)
    app.state.state_db_path = str(path)
    app.state.analysis_db_path = str(path)
    with TestClient(app) as client:
        response = client.get("/api/v1/tracks")
    assert response.status_code == 200, response.text
    rows = [row for row in response.json()["items"] if row["stable_id"] == STABLE_ID]
    assert len(rows) == 1, response.json()
    return rows[0]


def test_the_analyzed_key_reaches_the_all_tracks_listing(empty_library_state: Path) -> None:
    """[if] the drain wrote a key [then] the listing row carries it, [else stop]."""
    _write_key_record(empty_library_state)
    item = _listing_item(empty_library_state)
    assert item["key"] not in (None, ""), item
    assert item["key"] == "8A", item
    # An ``ok`` status is compacted OFF the wire when the row carries the value
    # that proves it (models.compact_listing_analysis_wire): the value is the
    # evidence, so the absence of ``key_status`` here is the compacted ok. Both
    # halves of the pair are asserted: a status with no reason is still bytes,
    # and a regression that stops compacting would leave ``key_status: "ok"``
    # on this row, which only the explicit absence check can catch.
    assert "key_reason" not in item, item
    assert "key_status" not in item, item


#: The drain leg's gates, both of them capability reports rather than passes.
#: ``requires_canonical_decode`` covers the fingerprint: UNAVAILABLE on hosts
#: without an odj-audio decode build, a FAILURE on the job that just built one
#: (``MDT_REQUIRE_AUDIO_ENGINE_BUILD=1``). ``requires_audio_stack`` is the other
#: half: ``own_key.backfill`` declares librosa a required module
#: (``apps/analysis/backends/own_key.py:96``) and ``drain.run`` warms that
#: backend's JIT cache before it decodes anything
#: (``apps/analysis/run.py:314`` -> ``warm_jit_cache`` -> ``_require_deps``), so
#: on a host that has the engine build but not the ``analysis`` extra this leg
#: would raise BackendNotAvailable and read as red instead of as unmeasured.
#: The engine probe (``apps/analysis/pcm_fingerprint.py``) imports no librosa,
#: so that pairing is reachable and neither marker subsumes the other. CI's
#: contracts job installs requirements.txt, so both are present and the leg
#: still gates there.
@pytest.mark.requires_audio_stack
@pytest.mark.requires_canonical_decode
def test_the_real_drain_writes_the_key_the_listing_serves(
    empty_library_state: Path,
) -> None:
    """[if] the real drain runs over the track [then] its key is on the wire.

    Not a fixture of the drain's expected output: the drain itself (queue ->
    backend -> analyzer -> store) runs over real audio, and only the row it
    persisted is allowed to supply the listing cell. A regression anywhere on
    that path -- no key measured, a key measured but not projected, a
    projection the reader does not reach -- leaves the wire blank or stale and
    fails here.

    UNAVAILABLE, not a pass, without an ``odj-audio`` decode build: the
    canonical decode fingerprint is that binary's (NAE-21).
    """
    audio = empty_library_state.parent / "owned.wav"
    _write_a_minor_wav(audio)

    summary = drain.run(
        [drain.TrackRef(stable_id=STABLE_ID, path=audio)],
        backend_name="own_key.backfill",
        db_path=empty_library_state,
    )
    assert summary.failed == 0, summary.errors
    # A run that skipped the row (e.g. an only-missing filter that matched a
    # record nobody wrote) measures nothing, so the row must have been
    # ANALYSED, not just queued.
    assert summary.analysed == 1, summary

    persisted = _persisted_key(empty_library_state)
    assert persisted not in (None, ""), "the drain measured or persisted no key"

    item = _listing_item(empty_library_state)
    assert item["key"] == persisted, item


def test_an_empty_data_dir_listing_is_not_silently_blank(empty_library_state: Path) -> None:
    """Control, same fixture, no record: the cell is empty AND says why."""
    item = _listing_item(empty_library_state)
    assert item["key"] in (None, ""), item
    assert item["key_status"] == "missing", item
