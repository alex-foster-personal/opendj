"""ANLZ .EXT writer round-trips through the stick reader (USBEXP-10746, issue #10035).

[if] build_ext writes PCO2, PWV3, PWV5 and PSSI [then] read_stick_track_analysis round-trips them, [else stop].
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from apps.analysis_waveform.bands import _downsample_max
from apps.sync.usb.pioneer import __main__ as pioneer_cli
from apps.sync.usb.pioneer.anlz_track import read_stick_track_analysis
from apps.sync.usb.pioneer.anlz_writer import (
    BeatInput,
    CueInput,
    PhraseInput,
    TrackAnalysisInput,
    build_dat,
    build_ext,
    build_anlz_pair,
)

pytestmark = pytest.mark.requirement("USBEXP-10746")

POINTS = 200
REL_DAT = "PIONEER/USBANLZ/P001/0000BEEF/ANLZ0000.DAT"
DURATION_S = 6.0


def _beats() -> tuple[BeatInput, ...]:
    step_ms = int(60000 / 128)
    return tuple(BeatInput(beat_in_bar=(i % 4) + 1, bpm=128.0, time_ms=i * step_ms) for i in range(32))


def _band_columns(duration_s: float, seed: int) -> tuple[tuple[int, ...], tuple[int, ...], tuple[int, ...]]:
    n_src = int(duration_s * 150) * 4
    low = np.array([(i * 3 + seed) % 32 for i in range(n_src)], dtype=np.uint8)
    mid = np.array([(i * 5 + seed + 1) % 32 for i in range(n_src)], dtype=np.uint8)
    high = np.array([(i * 7 + seed + 2) % 32 for i in range(n_src)], dtype=np.uint8)
    n = round(duration_s * 150)

    def _bucket(arr: np.ndarray) -> tuple[int, ...]:
        down = _downsample_max(arr.astype(np.float64), n)
        return tuple(int(min(31, max(0, round(v)))) for v in down)

    return _bucket(low), _bucket(mid), _bucket(high)


def _track(*, phrases: tuple[PhraseInput, ...] | None = None, phrase_end: int = 32) -> TrackAnalysisInput:
    low, mid, high = _band_columns(DURATION_S, 11)
    return TrackAnalysisInput(
        duration_s=DURATION_S,
        beats=_beats(),
        hot_cues=(
            CueInput("hot", 0, 500, comment="Drop", color=43),
            CueInput("hot", 2, 2500, comment="Build", color=19, loop_end_ms=4000),
        ),
        memory_cues=(CueInput("memory", 0, 1200, comment="M1", color=5),),
        detail_low=low,
        detail_mid=mid,
        detail_high=high,
        phrases=phrases,
        phrase_end_beat=phrase_end,
    )


def _write_pair(root: Path, track: TrackAnalysisInput) -> str:
    dat_path = root / REL_DAT
    dat_path.parent.mkdir(parents=True, exist_ok=True)
    dat_path.write_bytes(build_dat(track))
    dat_path.with_suffix(".EXT").write_bytes(build_ext(track))
    return "/" + REL_DAT


def _read(root: Path, analyze_path: str):
    return read_stick_track_analysis(volume_root=root, analyze_path=analyze_path, points=POINTS)


# ----- acceptance: PCO2 cues ------------------------------------------------


def test_pco2_hot_cues_round_trip_and_pcob_is_ignored_when_pco2_is_present(tmp_path: Path) -> None:
    """[if] PCO2 hot cues carry color and comment [then] stick reader matches them and not .DAT PCOB, [else stop]."""
    dat_cues = (
        CueInput("hot", 0, 99999, comment="wrong"),
        CueInput("hot", 1, 88888, comment="wrong"),
    )
    ext_track = _track()
    dat_track = TrackAnalysisInput(
        duration_s=ext_track.duration_s,
        beats=ext_track.beats,
        hot_cues=dat_cues,
        memory_cues=(),
        detail_low=ext_track.detail_low,
        detail_mid=ext_track.detail_mid,
        detail_high=ext_track.detail_high,
        phrases=None,
    )
    dat_path = tmp_path / REL_DAT
    dat_path.parent.mkdir(parents=True, exist_ok=True)
    dat_path.write_bytes(build_dat(dat_track))
    dat_path.with_suffix(".EXT").write_bytes(build_ext(ext_track))
    decoded = _read(tmp_path, "/" + REL_DAT)
    hot = sorted(decoded.hot_cues, key=lambda c: c["slot"])
    assert hot == [
        {"slot": 0, "position_ms": 500, "color": 43, "label": "Drop"},
        {"slot": 2, "position_ms": 2500, "color": 19, "label": "Build"},
    ], "if PCOB positions leak while PCO2 decodes then broken"
    assert decoded.memory_cues[0]["position_ms"] == 1200 and decoded.memory_cues[0]["label"] == "M1"
    loop = next(c for c in decoded.payload["cues"] if c["kind"] == "hot_cue" and c["slot"] == "C")
    assert loop["out_ms"] == 4000 and loop["is_loop"], "if the PCO2 loop end is lost then broken"


# ----- acceptance: PWV3 / PWV5 ----------------------------------------------


def test_pwv3_and_pwv5_lengths_follow_duration_and_bands_per_bucket_max(tmp_path: Path) -> None:
    """[if] detail bands are bucketed to 150/s [then] PWV3 is n bytes and PWV5 is 2n from those maxima, [else stop]."""
    track = _track()
    ext = build_ext(track)
    n = round(track.duration_s * 150)
    assert ext.find(b"PWV3") != -1 and ext.find(b"PWV5") != -1
    idx3 = ext.index(b"PWV3")
    count3 = int.from_bytes(ext[idx3 + 16 : idx3 + 20], "big")
    assert count3 == n, "if PWV3 column count is not round(duration_s*150) then broken"
    head_len = int.from_bytes(ext[idx3 + 4 : idx3 + 8], "big")
    total_len = int.from_bytes(ext[idx3 + 8 : idx3 + 12], "big")
    assert total_len - head_len == n, "if PWV3 body is not one byte per column then broken"
    idx5 = ext.index(b"PWV5")
    count5 = int.from_bytes(ext[idx5 + 16 : idx5 + 20], "big")
    assert count5 == n, "if PWV5 entry count is not n then broken"
    decoded = _read(tmp_path, _write_pair(tmp_path, track))
    detail_len = decoded.payload["waveform"]["detail"]["length"]
    assert detail_len == min(n, POINTS), "if the reader does not ingest the encoded detail waveform then broken"
    heights = tuple(int(h * 31) for h in decoded.payload["waveform"]["detail"]["low"])
    assert max(heights) > 0, "if the detail waveform was invented instead of bucketed then broken"


# ----- acceptance: PSSI ------------------------------------------------------


def test_pssi_phrases_mask_round_trip_and_absent_structure_writes_no_pssi(tmp_path: Path) -> None:
    """[if] structure phrases are provided [then] masked PSSI round-trips kinds, else no phrases tag, [else stop]."""
    with_phrases = _track(phrases=(PhraseInput(1, 2), PhraseInput(9, 5), PhraseInput(17, 3)))
    decoded = _read(tmp_path, _write_pair(tmp_path, with_phrases))
    kinds = [p["kind"] for p in decoded.payload["phrases"]]
    assert kinds == [2, 5, 3], "if masked PSSI does not decode to input kinds then broken"
    bare = _track(phrases=None)
    ext = build_ext(bare)
    assert b"PSSI" not in ext, "if a track with no structure still writes PSSI then broken"
    decoded_bare = _read(tmp_path, _write_pair(tmp_path, bare))
    assert decoded_bare.payload["phrases"] == [], "if the reader invents phrases without PSSI then broken"


# ----- acceptance: DAT + EXT pair --------------------------------------------


def test_dat_and_ext_pair_is_accepted_with_empty_unreadable_anlz(tmp_path: Path) -> None:
    """[if] sibling build_dat and build_ext are written together [then] the reader reports no unreadable tags, [else stop]."""
    decoded = _read(tmp_path, _write_pair(tmp_path, _track()))
    assert decoded.payload["unreadable_anlz"] == [], (
        f"if the DAT+EXT pair is not fully readable then broken: {decoded.payload['unreadable_anlz']}"
    )


# ----- acceptance: agent parity (CLI) ----------------------------------------


def test_cli_anlz_ext_dry_run_prints_ext_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """[if] pioneer CLI anlz-ext is called [then] it returns encoded EXT metadata without UI, [else stop]."""
    spec_path = tmp_path / "track.json"
    low, mid, high = _band_columns(2.0, 3)
    spec = {
        "duration_s": 2.0,
        "beats": [{"beat_in_bar": 1, "bpm": 128.0, "time_ms": 0}],
        "hot_cues": [],
        "memory_cues": [],
        "detail_low": list(low),
        "detail_mid": list(mid),
        "detail_high": list(high),
        "phrases": None,
    }
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    code = pioneer_cli.main(["anlz-ext", "--input", str(spec_path)])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["columns"] == 300 and out["bytes"] > 28
    assert out["sha256"], "if anlz-ext does not expose agent parity without the UI then broken"
