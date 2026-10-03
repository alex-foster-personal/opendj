"""The three-endpoint chain a deck walks for an UNMAPPED track's beatgrid.

PARITY-10. The frontend gate (``apps/webui/frontend/src/lib/rb/beatgrid-
fallback.ts``) keys on three facts served by this backend, and it was built
against HAND-WRITTEN TypeScript mirrors of these payloads rather than against
the pydantic models. This pins the real ones, so a rename or a shape change
reds here instead of silently making the deck stop asking for its grid.

The three facts, in the order the deck learns them:
  1. ``GET /anlz`` answers 200 with an EMPTY beatgrid for a locally imported
     track (``rb_vendor.empty_anlz_payload``), not 404. That 200 is exactly
     why the old ``ANALYSIS_NOT_FOUND``-only gate never fired.
  2. ``GET /rb-meta`` answers 200 with ``vendor == "local"``. It is the only
     honest source of "this track has no rekordbox mapping" - the empty
     ``/anlz`` payload does not say so itself.
  3. ``GET /beatgrid-fallback`` answers 200 with a real, ANLZ-shaped grid the
     deck's own validator accepts, or 404 when apps/analysis has not run.

Cloud-buildable: synthetic state.db in tmp_path, no data/master.plain.db and
no real library. Both routers are mounted on one app because the point is the
CHAIN, not any single endpoint.

[if] the pydantic models for /anlz, /rb-meta and /beatgrid-fallback drift from the frontend gate's assumptions [then] this module reds, [else stop].

Regression one-liners:
  - if /anlz stops answering 200-with-an-empty-grid for an unmapped track then broken
  - if /rb-meta drops the vendor key or stops saying local for an unmapped track then broken
  - if /beatgrid-fallback beats stop being ANLZ-shaped {n,bpm,t} then broken
  - if the fallback grid would fail the deck's own validateBeatGrid rules then broken
  - if an unmapped track with no analysis row gets an invented grid instead of a 404 then broken
  - if a decodable unmapped track's /anlz stops decoding real waveform peaks then broken
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import struct
import wave
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.adapters.rekordbox import config as rb_config
from apps.analysis.record import AnalysisRecord
from apps.analysis.store import upsert_record
from apps.analysis_waveform.bands import PWV6_MUSIC_GAIN
from apps.shared.state import db as state_db
from apps.shared.state.events import FakeEventBus
from apps.shared.state.writer import StateWriter
from apps.webui.server.routes.analysis import router as analysis_router
from apps.webui.server.routes.rb_assets import RbMetaOut
from apps.webui.server.routes.rb_assets import router as assets_router
from apps.webui.server.sqlite_backend import make_backend

pytestmark = pytest.mark.requirement("PARITY-10")

ANALYZED_SID = "a" * 40      # unmapped, WITH an apps.analysis row
UNANALYZED_SID = "b" * 40    # unmapped, no analysis row at all
DURATION_S = 60.0
BPM = 120.0
BAR_S = 240.0 / BPM
DURATION_MS = int(DURATION_S * 1000)


def _insert_local_track(path: Path, stable_id: str, file_path: str) -> None:
    """A track row with a file_path and deliberately NO vendor mapping.

    Through the real production writer (StateWriter.upsert_track), not a
    direct INSERT: a hand-rolled INSERT only ever exercises the columns it
    already knows about, so a schema change to the real ingest/write path
    (a new NOT NULL column, a changed default, a trigger) could drift silently
    out of sync with what this fixture serves (discussion_r3919293440 P1
    BLOCKING).
    """
    conn = state_db.open_rw(path)
    writer = StateWriter(conn, bus=FakeEventBus(), actor="test-seed")
    try:
        writer.upsert_track(
            stable_id=stable_id, stable_id_tier="inferred",
            title=None, artists=[], album=None, isrc=None,
            duration_ms=DURATION_MS, file_path=file_path,
        )
    finally:
        writer.close()
        conn.close()


def _analysis_record(stable_id: str) -> AnalysisRecord:
    downbeats = [i * BAR_S for i in range(int(DURATION_S / BAR_S))]
    return AnalysisRecord(
        stable_id=stable_id,
        backend="librosa+madmom",
        backend_version="test-1.0",
        analyzed_at=datetime(2026, 8, 31, tzinfo=UTC),
        duration_s=DURATION_S,
        sample_rate=44100,
        bpm=BPM, bpm_confidence=0.9,
        key_camelot="8A", key_openkey="8m", key_confidence=0.9,
        energy=6,
        onsets_s=[round(0.25 + i * 1.9, 3) for i in range(30)],
        downbeats_s=downbeats,
        features_blob={"rms": [0.1] * 400, "rms_hop": 512},
    )


_SAMPLE_RATE_HZ = 44_100
_TONE_S = 0.5


def _write_wav(path: Path, *, seconds: float) -> None:
    """A real, ffmpeg-decodable WAV: a full-scale 220 Hz sine.

    r3914268001 P1 BLOCKING: all-zero bytes with an ``.mp3`` suffix cannot be
    treated as valid media by the production decoder, so ``/anlz`` exercised
    the decoder-FAILURE payload (``local_waveform.status == "not_decoded"``)
    rather than the valid-unmapped-media path this chain is meant to pin - a
    real regression in decoding or path handling for local tracks could not
    have failed this suite. A genuine sine, decoded through the same ffmpeg
    path production uses, is what makes ``test_anlz_decodes_real_peaks_for_an_
    unmapped_track`` below able to fail for that reason.
    """
    frames = bytearray()
    for i in range(int(_SAMPLE_RATE_HZ * seconds)):
        value = int(32000 * math.sin(2 * math.pi * 220.0 * i / _SAMPLE_RATE_HZ))
        frames += struct.pack("<h", value)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(_SAMPLE_RATE_HZ)
        handle.writeframes(bytes(frames))


@pytest.fixture
def audio_file(tmp_path: Path) -> Path:
    """A real, decodable on-disk file so quality is measured, not guessed."""
    path = tmp_path / "imported track.wav"
    _write_wav(path, seconds=_TONE_S)
    return path


@pytest.fixture
def client(
    tmp_path: Path, audio_file: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    state_path = tmp_path / "state.db"
    _insert_local_track(state_path, ANALYZED_SID, str(audio_file))
    _insert_local_track(state_path, UNANALYZED_SID, str(audio_file))
    upsert_record(_analysis_record(ANALYZED_SID), db_path=state_path)
    monkeypatch.setattr(rb_config, "STATE_DB", state_path)
    # A local library has no rekordbox db, and none of this may need one.
    monkeypatch.setattr(rb_config, "MASTER_PLAIN_DB", tmp_path / "absent.db")

    app = FastAPI()
    app.state.backend = make_backend()
    app.state.analysis_db_path = state_path
    # No anlz_available_fn override: neither track has a track_vendor_ids row,
    # so the production _default_anlz_available lookup itself resolves
    # VENDOR_MAPPING_NOT_FOUND and reports False - exercising the real code
    # path this chain depends on, not a stand-in that would stay green
    # through a regression in it (r3913350826).
    app.include_router(assets_router, prefix="/api/v1")
    app.include_router(analysis_router, prefix="/api/v1")
    with TestClient(app) as test_client:
        yield test_client


# ----- fact 1: /anlz is a 200 with an empty grid, not a 404 -------------------

def test_anlz_serves_an_empty_grid_rather_than_analysis_not_found(
    client: TestClient,
) -> None:
    response = client.get(f"/api/v1/tracks/{ANALYZED_SID}/anlz")
    assert response.status_code == 200, response.text
    body = response.json()
    # The 200 is the parity trap: a gate that waits for ANALYSIS_NOT_FOUND
    # never fires here, so the deck kept an empty grid forever. Rekordbox-only
    # facts (the beatgrid) stay empty regardless of whether OUR waveform decode
    # below can run - see test_anlz_decodes_real_peaks_for_an_unmapped_track
    # for the decoder-dependent half of this same response.
    # `source` is REQUIRED on every /anlz beatgrid block, own or rekordbox
    # (NATIVE-01, PR #1587): a consumer must never infer the producer. An
    # unmapped file has no rekordbox grid, but this IS the rekordbox branch
    # and "rekordbox with no beats" is what the empty arrays already say.
    # STANDALONE-06: unmapped libraries default beatgrid source to own. This
    # fixture's legacy librosa row is not a canonical own beatgrid lane, so the
    # honest own answer is `missing`, not a rekordbox-labelled empty grid.
    grid = body["beatgrid"]
    assert body["beatgrid_source"] == "own"
    assert grid["source"] == "own"
    assert grid["status"] == "missing"


@pytest.mark.requires_ffmpeg
def test_anlz_decodes_real_peaks_for_an_unmapped_track(client: TestClient) -> None:
    """The waveform half of fact 1, decoder-dependent so it is gated separately.

    r3914268001 P1 BLOCKING: the shared ``audio_file`` fixture used to be
    all-zero bytes with an ``.mp3`` suffix, which the production ffmpeg
    decoder cannot treat as valid media - so this same request always fell
    into ``local_waveform.status == "not_decoded"`` and an empty waveform
    passed for that wrong reason, never actually exercising a successful
    decode. ``audio_file`` is now a real 220 Hz sine (see ``_write_wav``), so
    a genuine regression in local decode or path handling for real local
    tracks fails THIS test instead of hiding behind an always-empty payload.
    """
    response = client.get(f"/api/v1/tracks/{ANALYZED_SID}/anlz")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["local_waveform"]["status"] == "decoded", body["local_waveform"]
    low = body["waveform"]["preview"]["low"]
    assert low, "a real, loud sine must decode to a non-empty peak envelope"
    # The preview is on rekordbox's PWV6 scale (NATIVE-22), where even a
    # full-scale sine tops out at 83/127 in the low band, and 220 Hz sits just
    # above the 200 Hz crossover, so it measures about half that (0.33). A
    # failed or silent decode reads 0, so 40% of the band's ceiling still
    # tells a real decode from a broken one.
    low_ceiling = PWV6_MUSIC_GAIN[0] / 127
    assert max(low) > 0.4 * low_ceiling, "a full-scale 220 Hz sine must decode loud"


# ----- fact 2: /rb-meta names the missing vendor mapping ----------------------

def test_rb_meta_declares_vendor_local_for_an_unmapped_track(
    client: TestClient,
) -> None:
    response = client.get(f"/api/v1/tracks/{ANALYZED_SID}/rb-meta")
    assert response.status_code == 200, response.text
    assert response.json()["vendor"] == "local"


def test_vendor_is_a_declared_field_of_the_response_model(
    client: TestClient,
) -> None:
    # Read off the pydantic model, not the hand-written frontend mirror: the
    # frontend gate branches on this exact key and this exact value set.
    assert "vendor" in RbMetaOut.model_fields
    served = client.get(f"/api/v1/tracks/{ANALYZED_SID}/rb-meta").json()
    assert set(served) >= {"stable_id", "vendor"}
    assert served["vendor"] in {"rekordbox", "local"}


# ----- fact 3: /beatgrid-fallback serves a grid the deck can actually use -----

def test_fallback_serves_an_anlz_shaped_grid_for_the_analyzed_track(
    client: TestClient,
) -> None:
    response = client.get(f"/api/v1/tracks/{ANALYZED_SID}/beatgrid-fallback")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["anlz_available"] is False
    beats = body["beatgrid"]["beats"]
    assert beats, "an analyzed track must yield beats, not an empty grid"
    assert body["beatgrid"]["beat_count"] == len(beats)
    assert body["beatgrid"]["status"] == "ok"
    for beat in beats:
        assert set(beat) == {"n", "bpm", "t", "extrapolated"}


def test_fallback_grid_passes_the_decks_own_beatgrid_rules(
    client: TestClient,
) -> None:
    """Mirrors beat-sync-math.validateBeatGrid, which every deck consumer of
    the merged payload runs before quantize or Beat Sync will engage. A grid
    that fails these is served but inert, which looks like the bug this
    requirement closed."""
    beats = client.get(
        f"/api/v1/tracks/{ANALYZED_SID}/beatgrid-fallback"
    ).json()["beatgrid"]["beats"]
    assert len(beats) >= 2, "under 2 beats reads as gridless to the deck"
    previous = -1.0
    for index, beat in enumerate(beats):
        assert isinstance(beat["n"], int) and 1 <= beat["n"] <= 4
        assert beat["bpm"] > 0
        assert beat["t"] >= 0
        assert beat["t"] > previous, f"beat[{index}] time is not increasing"
        previous = beat["t"]


# ----- fixture provenance: the frontend's stub transport serves REAL bytes ---

_FRONTEND_FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "apps/webui/frontend/tests/unit/fixtures/beatgrid-fallback-unmapped-captured.json"
)
_FRONTEND_FIXTURE_MANIFEST = _FRONTEND_FIXTURE.with_suffix(".manifest.json")
FIXTURE_REWRITE_ENV = "MDT_BEATGRID_CHAIN_FIXTURE_REWRITE"


def test_frontend_fixture_capture_matches_the_live_route(client: TestClient) -> None:
    """deck-beatgrid-fallback-upgrade.test.mjs stubs globalThis.fetch (there is
    no browser or network stack in a node:test run), but it serves the EXACT
    bytes this test just re-captured live, not a hand-typed guess at the
    pydantic models' shape. If a route ever changes shape, THIS assertion goes
    red before the frontend's stub silently drifts out of sync with reality -
    the fixture is captured input, never a replacement implementation (AGENTS.md
    "No mocks and locked real fixtures").

    `artwork_available` must be a real False for a resident file with no
    embedded art. It used to degrade to None ("could not check") in a venv
    without the old optional mutagen reader; the reader (tinytag) is now a
    core dependency, and this guard stays so a None capture can never land.

    Re-capture command (always fails after writing; read the diff, then re-run
    without the switch):

    MDT_BEATGRID_CHAIN_FIXTURE_REWRITE=1 pytest tests/webui/test_unmapped_track_beatgrid_chain.py::test_frontend_fixture_capture_matches_the_live_route"""
    captured = json.loads(_FRONTEND_FIXTURE.read_text())

    rb_meta = client.get(f"/api/v1/tracks/{ANALYZED_SID}/rb-meta")
    assert rb_meta.status_code == captured["rb_meta_ok"]["status"]
    rb_meta_body = rb_meta.json()
    # folder_path carries this run's own tmp_path and is never the same string
    # twice; every other field is asserted against the literal capture.
    assert rb_meta_body["folder_path"], "expected a real resolved path, not empty"
    rb_meta_body["folder_path"] = "<audio-file-path>"
    if rb_meta_body.get("artwork_available") is not False:
        pytest.fail(
            "rb_meta artwork_available must be False before capture; the tag "
            "reader did not check the file (is tinytag installed? uv sync --locked)"
        )

    fallback_ok = client.get(f"/api/v1/tracks/{ANALYZED_SID}/beatgrid-fallback")
    assert fallback_ok.status_code == captured["beatgrid_fallback_ok"]["status"]
    fallback_ok_body = fallback_ok.json()

    fallback_404 = client.get(f"/api/v1/tracks/{UNANALYZED_SID}/beatgrid-fallback")
    assert fallback_404.status_code == captured["beatgrid_fallback_not_found"]["status"]
    fallback_404_body = fallback_404.json()

    if os.environ.get(FIXTURE_REWRITE_ENV) == "1":
        provenance = (
            captured["_provenance"]
            + " Re-captured Sat 12 Sep 2026 (issue #2246, after PR #2237 / #1777); "
            "each fallback beat now carries required `extrapolated` (false through "
            "last detected downbeat t=58.0, true for the three tail beats past it)."
        )
        fresh = {
            "_provenance": provenance,
            "beatgrid_fallback_not_found": {
                "body": fallback_404_body,
                "status": fallback_404.status_code,
            },
            "beatgrid_fallback_ok": {
                "body": fallback_ok_body,
                "status": fallback_ok.status_code,
            },
            "rb_meta_ok": {
                "body": rb_meta_body,
                "status": rb_meta.status_code,
            },
        }
        json_bytes = (json.dumps(fresh, indent=2, sort_keys=True) + "\n").encode("utf-8")
        _FRONTEND_FIXTURE.write_bytes(json_bytes)
        manifest = {
            "schema_version": 1,
            "capture_version": "2026-09-12",
            "files": {
                "beatgrid-fallback-unmapped-captured.json": {
                    "bytes": len(json_bytes),
                    "sha256": hashlib.sha256(json_bytes).hexdigest(),
                }
            },
        }
        _FRONTEND_FIXTURE_MANIFEST.write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        pytest.fail(
            f"re-captured {_FRONTEND_FIXTURE} and {_FRONTEND_FIXTURE_MANIFEST} "
            f"because {FIXTURE_REWRITE_ENV}=1 was set. This always fails: read the "
            "diff, then re-run without the switch."
        )

    assert rb_meta_body == captured["rb_meta_ok"]["body"]
    assert fallback_ok_body == captured["beatgrid_fallback_ok"]["body"]
    assert fallback_404_body == captured["beatgrid_fallback_not_found"]["body"]


def test_no_analysis_row_is_a_settled_404_not_an_invented_grid(
    client: TestClient,
) -> None:
    response = client.get(f"/api/v1/tracks/{UNANALYZED_SID}/beatgrid-fallback")
    assert response.status_code == 404, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "BEATGRID_FALLBACK_NOT_FOUND"
    assert detail["anlz_available"] is False
    # The deck reads this as "not analyzed yet" and keeps playing gridless.
    assert client.get(f"/api/v1/tracks/{UNANALYZED_SID}/anlz").status_code == 200
