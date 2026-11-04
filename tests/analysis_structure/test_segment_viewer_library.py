"""Library mode scores own-fast and All-In-One against the track's rekordbox phrases (STRUCTVIEW-02).

[if] library mode scores a track [then] it uses rekordbox phrases on their grid, [else stop].

Acceptance lines:
  [if] a library track has ANLZ with PQTZ and PSSI [then] GET /api/library/tracks/{id}/segments serves
       the rekordbox phrases on the rekordbox grid, with their rekordbox names and DJ mapping, and scores
       both lanes against them ⛔️ a reference lane drawn from anything but the vendor's phrases
  [if] All-In-One's boundaries sit exactly on the phrases [then] F@1bar is 1.0, and shifted by 1.5 bars it
       is 0 at 1 bar but 1.0 at 2 bars ⛔️ a tolerance that is too strict or too loose passing silently
  [if] the track has no PSSI tag, or no audio here [then] the lane is unavailable with a reason, scores
       are unavailable (never 0.0), and audio is 404 ⛔️ an unmeasured track counted as a miss
  [if] the CLI `library-segments <id>` runs [then] it prints the document the route serves
       ⛔️ agents reading something the page does not show
  [if] the bench is selected [then] it holds only tracks with phrases AND audio, and reports each
       denominator on the way ⛔️ a bench drawn from rows that cannot be scored

The fixture is real bytes end to end: sqlite databases in the app's schema, ANLZ files built tag by
tag (tests/sync/usb/anlz_bytes.py), a real WAV, an allin1_runner.py document in its real shape.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

import scripts.structure.segment_viewer.library as libmod
from scripts.structure.segment_viewer.__main__ import main as cli_main
from scripts.structure.segment_viewer.app import create_app
from scripts.structure.segment_viewer.library import Library, against_rekordbox, pssi_sections, select_bench
from tests.analysis_structure.test_segment_viewer import BAR_S, _runner_output, _write_arc
from tests.sync.usb.anlz_bytes import grid, pmai, pqtz, pssi

pytestmark = [pytest.mark.requirement("STRUCTVIEW-02"), pytest.mark.requires_ffmpeg]

READY, NO_PHRASES, NO_AUDIO = "trk_ready", "trk_no_phrases", "trk_no_audio"
BEATS = 256  # 64 bars of 4 at 120 BPM: the arc's length
# (start beat, PSSI kind) in mood high: Intro, Up, Chorus, Down at bars 1, 17, 33, 49 (the arc's band changes)
PHRASES = [(1, 1), (65, 2), (129, 5), (193, 3)]


def _identity(path: str) -> Path:
    return Path(path)


def _write_anlz(directory: Path, with_phrases: bool) -> Path:
    directory.mkdir(parents=True)
    dat = directory / "ANLZ0000.DAT"
    dat.write_bytes(pmai(pqtz(grid(0, 120.0, BEATS))))
    if with_phrases:
        (directory / "ANLZ0000.EXT").write_bytes(pmai(pssi(PHRASES, end_beat=BEATS + 1, masked=False)))
    return dat


def _write_dbs(root: Path, tracks: list[tuple[str, Path | None, Path]]) -> None:
    (root / "state").mkdir(parents=True)
    master = sqlite3.connect(root / "master.plain.db")
    master.executescript(
        "CREATE TABLE djmdContent (ID TEXT, Title TEXT, ArtistID TEXT, GenreID TEXT, BPM INTEGER, Length INTEGER,"
        " FolderPath TEXT, AnalysisDataPath TEXT, rb_local_deleted INTEGER);"
        "CREATE TABLE djmdArtist (ID TEXT, Name TEXT); CREATE TABLE djmdGenre (ID TEXT, Name TEXT);"
        "INSERT INTO djmdArtist VALUES ('a1', 'Fixture Artist'); INSERT INTO djmdGenre VALUES ('g1', 'Tech House');"
    )
    state = sqlite3.connect(root / "state" / "state.db")
    state.executescript(
        "CREATE TABLE tracks (stable_id TEXT, file_path TEXT, deleted_at TEXT);"
        "CREATE TABLE track_vendor_ids (stable_id TEXT, vendor TEXT, vendor_id TEXT, deleted_at TEXT);"
        "CREATE TABLE track_locations (stable_id TEXT, kind TEXT, role TEXT, file_path TEXT, deleted_at TEXT);"
    )
    for i, (sid, audio, dat) in enumerate(tracks):
        master.execute(
            "INSERT INTO djmdContent VALUES (?, ?, 'a1', 'g1', 12000, 128, ?, ?, 0)",
            (str(100 + i), f"Fixture {sid}", str(audio) if audio else "", str(dat)),
        )
        state.execute("INSERT INTO tracks VALUES (?, ?, NULL)", (sid, str(audio) if audio else None))
        state.execute("INSERT INTO track_vendor_ids VALUES (?, 'rekordbox', ?, NULL)", (sid, str(100 + i)))
    master.commit()
    state.commit()
    master.close()
    state.close()


@pytest.fixture
def library(tmp_path: Path) -> Library:
    audio = tmp_path / "audio"
    audio.mkdir()
    duration = _write_arc(audio / f"{READY}.wav")
    _write_arc(audio / f"{NO_PHRASES}.wav")
    anlz = tmp_path / "share" / "PIONEER" / "USBANLZ"
    root, work = tmp_path / "appdata", tmp_path / "work"
    _write_dbs(
        root,
        [
            (READY, audio / f"{READY}.wav", _write_anlz(anlz / "r", True)),
            (NO_PHRASES, audio / f"{NO_PHRASES}.wav", _write_anlz(anlz / "n", False)),
            (NO_AUDIO, None, _write_anlz(anlz / "a", True)),
        ],
    )
    (work / "allin1").mkdir(parents=True)
    (work / "allin1" / f"{READY}.json").write_text(json.dumps(_runner_output(duration)))
    (work / "bench.json").write_text(json.dumps({"stable_ids": [READY, NO_PHRASES]}))
    return Library.open(root, work, resolve=_identity)


def test_segments_route_serves_rekordbox_phrases_and_scores_both_lanes(library: Library) -> None:
    client = TestClient(create_app(None, library))
    doc = client.get(f"/api/library/tracks/{READY}/segments").json()
    assert doc["grid"]["source"] == "rekordbox.pqtz" and len(doc["grid"]["downbeats"]) == BEATS // 4
    pssi_lane = doc["lanes"]["pssi"]
    assert pssi_lane["status"] == "ok", pssi_lane["reason"]
    assert [s["label"] for s in pssi_lane["sections"]] == ["Intro", "Up", "Chorus", "Down"]
    assert [s["dj_label"] for s in pssi_lane["sections"]] == ["intro", "buildup", "drop", "breakdown"]
    assert [s["start_bar"] for s in pssi_lane["sections"]] == [1, 17, 33, 49]
    assert pssi_lane["mood"] == "high"
    allin1 = doc["scores"]["allin1"]
    # All-In-One's fixture boundaries are bars 17, 33, 49: exactly the phrases (positive control).
    assert allin1["status"] == "ok" and allin1["f@1bar"] == 1.0 and allin1["f@0.5s"] == 1.0, allin1
    assert allin1["bar_s"] == pytest.approx(BAR_S)
    own = doc["scores"]["own_fast"]
    assert own["status"] == "ok" and own["est_sections"] >= 2, own
    assert 0.0 <= own["f@2bar"] <= 1.0
    peaks = client.get(f"/api/library/tracks/{READY}/peaks")
    assert len(peaks.content) == doc["peaks"]["columns"] * 3
    ranged = client.get(f"/api/library/tracks/{READY}/audio", headers={"Range": "bytes=0-99"})
    assert ranged.status_code == 206 and len(ranged.content) == 100


def test_bar_tolerance_bites_both_ways() -> None:
    downbeats = [k * BAR_S for k in range(64)]
    ref = [
        {"start_s": a, "end_s": b, "dj_label": lab}
        for a, b, lab in [(0.0, 32.0, "intro"), (32.0, 64.0, "drop"), (64.0, 96.0, "breakdown"), (96.0, 128.0, "drop")]
    ]
    exact = against_rekordbox(ref, ref, downbeats, 128.0)
    assert exact["f@1bar"] == 1.0 and exact["label_acc"] == 1.0 and exact["drop_f1"] == 1.0
    shifted = [{**s, "start_s": s["start_s"] + 1.5 * BAR_S if s["start_s"] else 0.0} for s in ref]
    moved = against_rekordbox(ref, shifted, downbeats, 128.0)
    assert moved["f@1bar"] == 0.0, "1.5 bars off must miss at a 1-bar tolerance (too loose)"
    assert moved["f@2bar"] == 1.0, "1.5 bars off must hit at a 2-bar tolerance (too strict)"


def test_label_mapping_drops_unmapped_frames_and_merges_cooldown() -> None:
    downbeats = [k * BAR_S for k in range(32)]
    ref = [{"start_s": 0.0, "end_s": 32.0, "dj_label": None}, {"start_s": 32.0, "end_s": 64.0, "dj_label": "breakdown"}]
    est = [
        {"start_s": 0.0, "end_s": 32.0, "dj_label": "drop"},
        {"start_s": 32.0, "end_s": 64.0, "dj_label": "cooldown"},
    ]
    row = against_rekordbox(ref, est, downbeats, 64.0)
    assert row["ref_mapped_frames"] == 64 and row["ref_frames"] == 128, "a Verse half is unmapped, not a miss"
    assert row["frames"]["frames"] == 64 and row["label_acc"] == 1.0, "cooldown agrees with rekordbox Down"
    est_wrong = [{**est[0]}, {**est[1], "dj_label": "drop"}]
    assert against_rekordbox(ref, est_wrong, downbeats, 64.0)["label_acc"] == 0.0


def test_unmeasurable_tracks_say_why_and_are_never_scored_zero(library: Library) -> None:
    client = TestClient(create_app(None, library))
    doc = client.get(f"/api/library/tracks/{NO_PHRASES}/segments").json()
    assert doc["lanes"]["pssi"]["status"] == "unavailable" and "PSSI" in doc["lanes"]["pssi"]["reason"]
    assert doc["lanes"]["own_fast"]["status"] == "ok", "own-fast still runs on the rekordbox grid"
    for lane in ("own_fast", "allin1"):
        assert doc["scores"][lane]["status"] == "unavailable" and "f@1bar" not in doc["scores"][lane]
    assert client.get(f"/api/library/tracks/{NO_AUDIO}/segments").status_code == 409
    assert client.get(f"/api/library/tracks/{NO_AUDIO}/audio").status_code == 404
    assert client.get("/api/library/tracks/nope/segments").status_code == 404
    summary = client.get("/api/library/summary").json()
    assert summary["bench"] == 2 and summary["with_phrases"] == 1
    assert summary["lanes"]["own_fast"]["scored"] == 1 and summary["lanes"]["allin1"]["scored"] == 1
    assert summary["both_scored"] == 1 and summary["paired"]["allin1"]["f@1bar"] == 1.0


def test_cli_prints_the_document_the_route_serves(
    library: Library, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    served = TestClient(create_app(None, library)).get(f"/api/library/tracks/{READY}/segments").json()
    monkeypatch.setattr(libmod, "default_resolver", _identity)  # the CLI opens the library with the default
    args = ["--library", str(library.root), "--work", str(library.work)]
    assert cli_main([*args, "library-segments", READY]) == 0
    printed = json.loads(capsys.readouterr().out)
    for doc in (printed, served):
        assert doc["lanes"]["own_fast"].pop("segment_ms") >= 0
    assert printed["lanes"] == served["lanes"] and printed["scores"] == served["scores"]
    assert cli_main([*args, "library-search", "tech house"]) == 0
    found = {t["stable_id"] for t in json.loads(capsys.readouterr().out)["tracks"]}
    assert found == {READY, NO_PHRASES, NO_AUDIO}
    assert cli_main([*args, "library-search", "no such words"]) == 0
    assert json.loads(capsys.readouterr().out)["tracks"] == [], "negative control: search can say nothing"


def test_bench_selection_only_takes_scorable_tracks_and_counts_denominators(library: Library) -> None:
    sel = select_bench(library, size=5, seed=1)
    assert sel["stable_ids"] == [READY], sel
    den = sel["denominators"]
    assert den == {
        "library_mapped": 3,
        "with_anlz_path": 3,
        "anlz_on_disk": 3,
        "audio_on_disk": 2,
        "anlz_unreadable": 0,
        "with_phrases": 1,
    }


def test_pssi_names_follow_the_mood_and_only_dj_phrases_map() -> None:
    phrases = [
        {"start_s": float(i), "end_s": float(i + 1), "kind": k, "mood": 2} for i, k in enumerate((1, 2, 8, 9, 10, 42))
    ]
    got = [(s["label"], s["dj_label"]) for s in pssi_sections(phrases, 2)]
    assert got == [
        ("Intro", "intro"),
        ("Verse 1", None),
        ("Bridge", None),
        ("Chorus", "drop"),
        ("Outro", "outro"),
        ("kind 42", None),
    ]
    high = [(s["label"], s["dj_label"]) for s in pssi_sections([{**phrases[1], "kind": 2}], 1)]
    assert high == [("Up", "buildup")], "the same kind id means Up in mood high, Verse 1 in mood mid"


def test_peaks_cached_without_decoder_identity_are_decoded_again(library: Library) -> None:
    """A size/mtime-only cache entry (no decoder, no profile) is never served as current peaks."""
    real = library.peaks(READY)
    path = library.peaks_path(READY)
    meta = path.with_suffix(".json")
    st = library.audio_path(READY).stat()
    stale = real * 0
    np.save(path, stale)
    meta.write_text(json.dumps({"size": st.st_size, "mtime_ns": st.st_mtime_ns}), encoding="utf-8", newline="\n")
    again = library.peaks(READY)
    assert again.any(), "if peaks cached under an unknown decoder are reused then broken"
    assert {"decoder", "peaks_version"} <= json.loads(meta.read_text(encoding="utf-8")).keys()
