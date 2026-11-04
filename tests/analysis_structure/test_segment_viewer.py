"""The segment viewer's JSON is what the page draws, and it never invents a lane (STRUCTVIEW-01).

Acceptance lines:
- [if] the viewer and the bench score the same inputs [then] the route serves the bench agreement, [else stop].
  [if] a track has All-In-One output [then] GET /api/tracks/{id}/segments serves own-fast and
       All-In-One sections with bars, and agreement equal to the bench's score_track
       ⛔️ the viewer and the bench disagreeing on the same inputs
  [if] All-In-One output is missing [then] its lane and own-fast are pending with a reason and
       agreement is unavailable ⛔️ a missing lane filled or read as "no sections"
  [if] the CLI `segments <id>` runs [then] it prints the same document the route serves
       ⛔️ agents reading something the page does not show
  [if] an id is not in the corpus [then] 404 ⛔️ arbitrary paths served
"""

from __future__ import annotations

import json
import math
import wave
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from apps.analysis_structure.bench import score_track
from scripts.structure.segment_viewer.__main__ import main as cli_main
from scripts.structure.segment_viewer.app import create_app
from scripts.structure.segment_viewer.corpus import Corpus, summarize

pytestmark = [pytest.mark.requirement("STRUCTVIEW-01"), pytest.mark.requires_ffmpeg]

RATE = 44_100
BAR_S = 2.0
PLAN = [(16, 0.05, 0.10, 0.02), (16, 0.6, 0.3, 0.3), (16, 0.01, 0.3, 0.01), (16, 0.6, 0.3, 0.3)]
READY, PENDING = "arc_ready", "arc_pending"


def _write_arc(path: Path) -> float:
    """A real WAV whose bands change at bars 16, 32 and 48, with a beat pulse."""
    chunks = []
    for bars, bass, mids, hats in PLAN:
        t = np.arange(int(bars * BAR_S * RATE)) / RATE
        pulse = 0.5 + 0.5 * (np.mod(t, BAR_S / 4) < 0.1)
        chunks.append(
            bass * pulse * np.sin(2 * math.pi * 55 * t)
            + mids * np.sin(2 * math.pi * 1000 * t)
            + hats * pulse * np.sin(2 * math.pi * 10_000 * t)
        )
    audio = (np.concatenate(chunks) * 32_000).astype("<i2")
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes(audio.tobytes())
    return len(audio) / RATE


def _runner_output(duration: float) -> dict:
    """An allin1_runner.py document in its real shape: boundaries at bars 16, 32, 48."""
    bounds = [0.0, 32.0, 64.0, 96.0, duration]
    labels = ["intro", "chorus", "bridge", "chorus"]
    return {
        "status": "ok",
        "segments": [
            {"start": a, "end": b, "label": lab} for a, b, lab in zip(bounds[:-1], bounds[1:], labels, strict=True)
        ],
        "model_bpm": 120,
        "model_beats": [k * BAR_S / 4 for k in range(int(duration / BAR_S) * 4)],
        "model_downbeats": [k * BAR_S for k in range(int(duration / BAR_S))],
        "envelopes": None,
        "envelopes_error": "not measured in this test",
        "seconds": 1.0,
    }


@pytest.fixture
def corpus(tmp_path: Path) -> Corpus:
    audio_dir, root = tmp_path / "audio", tmp_path / "corpus"
    audio_dir.mkdir()
    (root / "allin1").mkdir(parents=True)
    duration = 0.0
    for sid in (READY, PENDING):
        duration = _write_arc(audio_dir / f"{sid}.wav")
    (root / "allin1" / f"{READY}.json").write_text(json.dumps(_runner_output(duration)))
    (root / "corpus.jsonl").write_text(
        "".join(json.dumps({"stable_id": s, "file": f"{s}.wav"}) + "\n" for s in (READY, PENDING))
    )
    return Corpus.open(root, audio_dir)


def test_segments_route_serves_lanes_bars_and_bench_agreement(corpus: Corpus) -> None:
    client = TestClient(create_app(corpus))
    doc = client.get(f"/api/tracks/{READY}/segments").json()
    own, ref = doc["lanes"]["own_fast"], doc["lanes"]["allin1"]
    assert own["status"] == "ok" and ref["status"] == "ok", (own["reason"], ref["reason"])
    assert [s["start_s"] for s in ref["sections"]] == [0.0, 32.0, 64.0, 96.0]
    assert [s["start_bar"] for s in ref["sections"]] == [1, 17, 33, 49]
    assert all(s["start_bar"] is not None and s["end_bar"] >= s["start_bar"] for s in own["sections"])
    assert len(own["sections"]) >= 2, "control: the arc has band changes own-fast must find"
    assert doc["lanes"]["pssi"]["status"] == "unavailable" and doc["lanes"]["pssi"]["reason"]
    # The same inputs through the bench must give the same numbers.
    bench = score_track(corpus.allin1_raw(READY), corpus.peaks(READY))
    assert bench is not None
    for key in ("f@0.5", "f@2.0", "f@3.0", "base_f@2.0"):
        assert doc["agreement"][key] == bench[key], key
    assert doc["agreement"]["est_sections"] == bench["est_sections"]
    peaks = client.get(f"/api/tracks/{READY}/peaks")
    assert len(peaks.content) == doc["peaks"]["columns"] * 3
    ranged = client.get(f"/api/tracks/{READY}/audio", headers={"Range": "bytes=0-99"})
    assert ranged.status_code == 206 and len(ranged.content) == 100


def test_missing_allin1_is_pending_with_reason_never_empty_ok(corpus: Corpus) -> None:
    doc = TestClient(create_app(corpus)).get(f"/api/tracks/{PENDING}/segments").json()
    for key in ("allin1", "own_fast"):
        lane = doc["lanes"][key]
        assert lane["status"] == "pending" and "All-In-One" in lane["reason"], lane
        assert lane["sections"] == []
    assert doc["agreement"]["status"] == "unavailable"
    listing = {t["stable_id"]: t["allin1"] for t in TestClient(create_app(corpus)).get("/api/tracks").json()["tracks"]}
    assert listing == {READY: "ok", PENDING: "pending"}


def test_cli_prints_the_document_the_route_serves(corpus: Corpus, capsys: pytest.CaptureFixture[str]) -> None:
    served = TestClient(create_app(corpus)).get(f"/api/tracks/{READY}/segments").json()
    assert cli_main(["--corpus", str(corpus.root), "--audio", str(corpus.audio_dir), "segments", READY]) == 0
    printed = json.loads(capsys.readouterr().out)
    # segment_ms is a wall-clock measurement of each run, the one field allowed to differ.
    for doc in (printed, served):
        assert doc["lanes"]["own_fast"].pop("segment_ms") >= 0
    assert printed["lanes"] == served["lanes"] and printed["agreement"] == served["agreement"]


def test_unknown_track_is_404(corpus: Corpus) -> None:
    client = TestClient(create_app(corpus))
    assert client.get("/api/tracks/nope/segments").status_code == 404
    assert client.get("/api/tracks/..%2F..%2Fetc/audio").status_code == 404


def test_summary_pools_label_frames_like_the_bench_not_track_means() -> None:
    """A long agreeing track outweighs a short disagreeing one, and an undefined drop F1 still counts."""
    long_ok = {"status": "ok", "counts": {"frames": 90, "agree": 90, "drop_tp": 9, "drop_fp": 0, "drop_fn": 0}}
    short_bad = {"status": "ok", "counts": {"frames": 10, "agree": 0, "drop_tp": 0, "drop_fp": 0, "drop_fn": 0}}
    out = summarize(2, [long_ok, short_bad])
    assert (out["label_acc"], out["label_frames"]) == (0.9, 100), "if summary means per-track label_acc then broken"
    assert out["drop_f1"] == 1.0, "if a track with undefined drop F1 drops out of the pool then broken"
