"""The own key lane fails a truncated file instead of keying its surviving frames.

[if] the own key lane decodes under half a file's claimed length [then] the track fails with a reason and gets no key, [else stop].

Issue #6150 B3. The installed-app acceptance probe (fresh HOME, no rekordbox,
folder import) replaced a 2 s A440 mp3 with its first 4096 bytes. BPM and
beatgrid failed with a reason, but the key lane stored ``11B`` with
``key_status: ok``: ``librosa.load`` decoded the 0.26 s before the cut without
an error, and that slice really is an A, so Krumhansl read A major. Nothing
was swallowed and no default was applied; the decode simply never said the
file was short.

The guard first landed with #6204 on the night branch `af--night-1010--library`
and never reached main; the Electron acceptance on the Air (Tue 3 Nov 2026,
sha 11404b55) stored the same ``11B`` again. This restores it on main.

[if] the decode is far shorter than the file claims [then ⛔️] a STORED failed
     key lane whose reason starts ``truncated_decode`` and names both durations,
     so the track row reads "failed" with the reason, never "not run yet"
[if] a valid short file (the same 2 s carrier, uncut) [then] it still gets a key

The decode, chroma and estimate are real. Only the canonical-decode
fingerprint, which needs an ``odj-audio`` build and has nothing to do with the
question, is pinned so this runs on a host without one; the whole-backend
variants below run with the real fingerprint where odj-audio exists.

-Claude
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

pytest.importorskip("librosa", reason="needs the optional librosa package")

from apps.analysis.backends import own_key as own_key_module
from apps.analysis.backends.own_key import OwnKeyBackfillBackend
from apps.analysis.store import open_conn, upsert_record

pytestmark = pytest.mark.requirement("DECODE-01")

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "app-acceptance"
TRUNCATED_MP3 = FIXTURES / "corrupt" / "99-acceptance-truncated.mp3"
CARRIER_MP3 = FIXTURES / "library" / "07-acceptance-corrupt-carrier.mp3"
#: A 440 Hz tone is A major to a key profile: Camelot 11B, the value the
#: probe saw stored for the truncated file.
A_MAJOR_CAMELOT = "11B"


@pytest.fixture
def state_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "state" / "state.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    open_conn(db_path).close()
    return db_path


@pytest.fixture
def pinned_fingerprint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(own_key_module, "require_resampler", lambda: None)
    monkeypatch.setattr(own_key_module, "canonical_decode_fingerprint", lambda _path: "2e" * 32)


def _copy(src: Path, tmp_path: Path) -> Path:
    dest = tmp_path / src.name
    shutil.copyfile(src, dest)
    return dest


def test_the_probe_truncated_mp3_fails_the_key_lane_with_a_reason(
    tmp_path: Path, state_db: Path, pinned_fingerprint: None
) -> None:
    audio = _copy(TRUNCATED_MP3, tmp_path)

    record = own_key_module.analyze_audio(audio, "sid-truncated", db_path=state_db)

    lane = record.lanes["key"]
    assert lane.status == "failed", lane
    assert lane.reason is not None and lane.reason.startswith("truncated_decode: "), lane.reason
    assert "0.26 s" in lane.reason and "2.04 s" in lane.reason, lane.reason
    assert lane.payload == {}, "a failed lane carries no key"
    assert record.key_camelot == "" and record.key_confidence == 0.0
    upsert_record(record, db_path=state_db)  # the record contract takes it as stored


def test_control_the_uncut_short_carrier_still_gets_its_key(
    tmp_path: Path, state_db: Path, pinned_fingerprint: None
) -> None:
    audio = _copy(CARRIER_MP3, tmp_path)

    record = own_key_module.analyze_audio(audio, "sid-carrier", db_path=state_db)

    lane = record.lanes["key"]
    assert lane.status == "ok", lane.reason
    assert lane.payload["camelot"] == A_MAJOR_CAMELOT
    assert record.duration_s == pytest.approx(2.0, abs=0.1)


@pytest.mark.requires_canonical_decode
def test_the_backend_fails_the_truncated_mp3_with_the_real_fingerprint(tmp_path: Path) -> None:
    audio = _copy(TRUNCATED_MP3, tmp_path)
    record = OwnKeyBackfillBackend.analyze(audio, "sid-truncated-real")
    assert record.lanes["key"].status == "failed"
    assert record.lanes["key"].reason.startswith("truncated_decode: ")


@pytest.mark.requires_canonical_decode
def test_the_backend_still_keys_the_uncut_carrier_with_the_real_fingerprint(
    tmp_path: Path,
) -> None:
    audio = _copy(CARRIER_MP3, tmp_path)
    record = OwnKeyBackfillBackend.analyze(audio, "sid-carrier-real")
    assert record.lanes["key"].status == "ok", record.lanes["key"].reason
    assert record.lanes["key"].payload["camelot"] == A_MAJOR_CAMELOT
