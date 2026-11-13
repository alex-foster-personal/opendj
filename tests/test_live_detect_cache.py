"""Regression tests for scripts/live_detect.py cache compaction and verdict thresholds.

- if cache file missing then _compact_cache returns empty set without creating file
- if ok rows present then retained; missing/decode_error omitted for retry
- if incomplete unterminated final fragment then stderr notice and prior ok row kept
- if complete final row lacks trailing newline then row retained and file normalized
- if malformed JSON in middle then raises with path:line and bytes unchanged
- if malformed JSON on newline-terminated last line then raises and bytes unchanged
- if invalid row shape then raises and bytes unchanged
- if status is not a string then raises with path:line before cache rewrite
- if compaction rewrites cache then file uses LF-only JSONL bytes
- if edge share equals LIVE_EDGE then verdict is live
- if edge share equals LIKELY_EDGE below LIVE_EDGE then verdict is likely
- if only middle share equals LIKELY_MID then verdict is likely
- if both shares are below likely thresholds then verdict is studio
- if duration is shorter than EDGE_S then every edge window has start >= 0 and start + length <= duration
- if duration is shorter than MID_S then every middle window has start >= 0 and start + length <= duration
- if duration is long enough for baseline windows then edge and mid tuples match the pre-fix unclamped geometry
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO = Path(__file__).resolve().parents[1]
LIVE_DETECT = REPO / "scripts" / "live_detect.py"

pytestmark = pytest.mark.requires_ffmpeg


@pytest.fixture(scope="module")
def live_detect_mod():
    # PEP 723 script deps are not in the repo venv; stub numpy so cache helpers load.
    sys.modules.setdefault("numpy", types.ModuleType("numpy"))
    spec = importlib.util.spec_from_file_location("live_detect_cache_test", LIVE_DETECT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["live_detect_cache_test"] = module
    spec.loader.exec_module(module)
    return module


def _assert_windows_within_duration(
    edge: list[tuple[float, float]],
    mid: list[tuple[float, float]],
    duration: float,
) -> None:
    for start, length in edge + mid:
        assert 0.0 <= start <= duration
        assert length >= 0.0
        assert start + length <= duration + 1e-9


def _ok_line(path: str) -> str:
    row = {
        "path": path,
        "status": "ok",
        "verdict": "studio",
        "edge_share": 0.0,
        "mid_share": 0.0,
        "peak": 0.0,
        "duration_s": 1.0,
    }
    return json.dumps(row, separators=(",", ":"))


def test_missing_cache_returns_empty(tmp_path, live_detect_mod):
    cache = tmp_path / "cache.jsonl"
    assert live_detect_mod._compact_cache(cache) == set()
    assert not cache.exists()


def test_ok_retained_missing_and_decode_error_dropped(tmp_path, live_detect_mod):
    cache = tmp_path / "cache.jsonl"
    lines = [
        _ok_line("/a.mp3"),
        json.dumps({"path": "/b.mp3", "status": "missing"}, separators=(",", ":")),
        json.dumps({"path": "/c.mp3", "status": "decode_error"}, separators=(",", ":")),
        _ok_line("/d.mp3"),
    ]
    cache.write_text("\n".join(lines) + "\n", encoding="utf-8")
    done = live_detect_mod._compact_cache(cache)
    assert done == {"/a.mp3", "/d.mp3"}
    text = cache.read_text(encoding="utf-8")
    assert "/b.mp3" not in text
    assert "/c.mp3" not in text
    assert text.endswith("\n")


def test_incomplete_final_fragment_repaired(capsys, tmp_path, live_detect_mod):
    cache = tmp_path / "cache.jsonl"
    good = _ok_line("/good.mp3")
    cache.write_text(good + "\n" + '{"path":"/bad","status":"ok","ver', encoding="utf-8")
    done = live_detect_mod._compact_cache(cache)
    assert done == {"/good.mp3"}
    err = capsys.readouterr().err
    assert "discarding incomplete cache fragment" in err
    assert "will rescore" in err
    assert cache.read_text(encoding="utf-8") == good + "\n"


def test_complete_final_row_without_trailing_newline_retained(tmp_path, live_detect_mod):
    cache = tmp_path / "cache.jsonl"
    line = _ok_line("/solo.mp3")
    cache.write_text(line, encoding="utf-8")
    done = live_detect_mod._compact_cache(cache)
    assert done == {"/solo.mp3"}
    assert cache.read_text(encoding="utf-8") == line + "\n"


def test_malformed_json_middle_raises_and_preserves_bytes(tmp_path, live_detect_mod):
    cache = tmp_path / "cache.jsonl"
    before = _ok_line("/a.mp3") + "\n{not json}\n" + _ok_line("/b.mp3") + "\n"
    cache.write_bytes(before.encode("utf-8"))
    with pytest.raises(ValueError, match=r":2: malformed cache JSON"):
        live_detect_mod._compact_cache(cache)
    assert cache.read_bytes() == before.encode("utf-8")


def test_malformed_json_final_newline_terminated_raises(tmp_path, live_detect_mod):
    cache = tmp_path / "cache.jsonl"
    before = _ok_line("/a.mp3") + "\n{broken}\n"
    cache.write_bytes(before.encode("utf-8"))
    with pytest.raises(ValueError, match=r":2: malformed cache JSON"):
        live_detect_mod._compact_cache(cache)
    assert cache.read_bytes() == before.encode("utf-8")


@pytest.mark.parametrize(
    "bad_line, pattern",
    [
        ("[]", "must be a JSON object"),
        ('{"status":"ok"}', "missing string path"),
        ('{"path":1,"status":"ok"}', "missing string path"),
        ('{"path":"/x","status":"bogus"}', "unknown status"),
        ('{"path":"/x","status":[]}', "missing string status"),
    ],
)
def test_invalid_row_shape_raises_and_preserves(tmp_path, live_detect_mod, bad_line: str, pattern: str):
    cache = tmp_path / "cache.jsonl"
    before = _ok_line("/a.mp3") + "\n" + bad_line + "\n"
    cache.write_bytes(before.encode("utf-8"))
    with pytest.raises((TypeError, ValueError), match=pattern):
        live_detect_mod._compact_cache(cache)
    assert cache.read_bytes() == before.encode("utf-8")


def test_unhashable_status_reports_path_line(tmp_path, live_detect_mod):
    cache = tmp_path / "cache.jsonl"
    before = _ok_line("/a.mp3") + "\n" + '{"path":"/x","status":[]}' + "\n"
    cache.write_bytes(before.encode("utf-8"))
    with pytest.raises(TypeError, match=rf"{cache}:2: cache row missing string status"):
        live_detect_mod._compact_cache(cache)
    assert cache.read_bytes() == before.encode("utf-8")


@pytest.mark.requirement("TESTINFRA-13648")
@pytest.mark.parametrize("duration", [5.0, 12.0, 19.9])
def test_windows_short_edge_bounded(live_detect_mod, duration: float):
    """[if] duration is shorter than EDGE_S [then] every edge window lies inside [0, duration], [else stop]."""
    edge_s = live_detect_mod.CFG.EDGE_S
    assert duration < edge_s
    edge, mid = live_detect_mod._windows(duration)
    _assert_windows_within_duration(edge, mid, duration)
    for start, length in edge:
        assert start + length <= duration + 1e-9


@pytest.mark.requirement("TESTINFRA-13648")
@pytest.mark.parametrize("duration", [3.0, 8.0, 9.5])
def test_windows_short_mid_bounded(live_detect_mod, duration: float):
    """[if] duration is shorter than MID_S [then] every middle window lies inside [0, duration], [else stop]."""
    mid_s = live_detect_mod.CFG.MID_S
    assert duration < mid_s
    edge, mid = live_detect_mod._windows(duration)
    _assert_windows_within_duration(edge, mid, duration)


@pytest.mark.requirement("TESTINFRA-13648")
def test_windows_long_track_geometry_unchanged(live_detect_mod):
    """[if] the track is long enough for the baseline windows [then] edge and mid tuples keep the pre-fix geometry, [else stop]."""
    duration = 120.0
    edge_s = live_detect_mod.CFG.EDGE_S
    mid_s = live_detect_mod.CFG.MID_S
    edge, mid = live_detect_mod._windows(duration)
    assert edge == [(0.0, edge_s), (duration - edge_s, edge_s)]
    half = mid_s / 2
    assert mid == [
        (duration * 0.3 - half, mid_s),
        (duration * 0.5 - half, mid_s),
        (duration * 0.7 - half, mid_s),
    ]


def test_compacted_rewrite_is_lf_jsonl(tmp_path, live_detect_mod):
    cache = tmp_path / "cache.jsonl"
    lines = [
        _ok_line("/a.mp3"),
        json.dumps({"path": "/b.mp3", "status": "missing"}, separators=(",", ":")),
    ]
    cache.write_text("\n".join(lines) + "\n", encoding="utf-8")
    live_detect_mod._compact_cache(cache)
    raw = cache.read_bytes()
    assert b"\r" not in raw
    assert raw.endswith(b"\n")
    assert raw.count(b"\n") == 1


@pytest.mark.requirement("TESTINFRA-14279")
def test_verdict_live_at_live_edge_threshold(live_detect_mod):
    """[if] edge share equals LIVE_EDGE [then] verdict is live, [else stop]."""
    cfg = live_detect_mod.CFG
    assert live_detect_mod._verdict(cfg.LIVE_EDGE, 0.0) == "live"


@pytest.mark.requirement("TESTINFRA-14279")
def test_verdict_likely_at_likely_edge_below_live_edge(live_detect_mod):
    """[if] edge share equals LIKELY_EDGE below LIVE_EDGE [then] verdict is likely, [else stop]."""
    cfg = live_detect_mod.CFG
    assert cfg.LIKELY_EDGE < cfg.LIVE_EDGE
    assert live_detect_mod._verdict(cfg.LIKELY_EDGE, 0.0) == "likely"


@pytest.mark.requirement("TESTINFRA-14279")
def test_verdict_likely_when_only_mid_share_hits_likely_mid(live_detect_mod):
    """[if] only middle share equals LIKELY_MID [then] verdict is likely, [else stop]."""
    cfg = live_detect_mod.CFG
    below_likely_edge = max(0.0, cfg.LIKELY_EDGE - 0.01)
    assert live_detect_mod._verdict(below_likely_edge, cfg.LIKELY_MID) == "likely"


@pytest.mark.requirement("TESTINFRA-14279")
def test_verdict_studio_when_both_shares_below_likely_thresholds(live_detect_mod):
    """[if] both shares are below likely thresholds [then] verdict is studio, [else stop]."""
    cfg = live_detect_mod.CFG
    edge = max(0.0, cfg.LIKELY_EDGE - 0.01)
    mid = max(0.0, cfg.LIKELY_MID - 0.01)
    assert edge < cfg.LIKELY_EDGE
    assert mid < cfg.LIKELY_MID
    assert live_detect_mod._verdict(edge, mid) == "studio"


def test_score_file_ffprobe_failure_is_decode_error_without_verdict(tmp_path, live_detect_mod, monkeypatch):
    """[if] ffprobe fails for an existing path [then] decode_error row has no verdict, [else stop]."""
    audio = tmp_path / "exists.mp3"
    audio.write_bytes(b"\x00")
    monkeypatch.setattr(
        live_detect_mod,
        "_probe_duration",
        lambda _path: (_ for _ in ()).throw(subprocess.CalledProcessError(1, "ffprobe")),
    )
    result = live_detect_mod.score_file(MagicMock(), str(audio))
    assert result.status == "decode_error"
    assert result.verdict is None


def test_score_file_decode_window_failure_is_decode_error_without_verdict(tmp_path, live_detect_mod, monkeypatch):
    """[if] one decode window fails [then] decode_error row has no verdict, [else stop]."""
    audio = tmp_path / "exists.mp3"
    audio.write_bytes(b"\x00")
    monkeypatch.setattr(live_detect_mod, "_probe_duration", lambda _path: 120.0)
    monkeypatch.setattr(
        live_detect_mod,
        "_decode_window",
        lambda _path, _start, _length: (_ for _ in ()).throw(subprocess.CalledProcessError(1, "ffmpeg")),
    )
    result = live_detect_mod.score_file(MagicMock(), str(audio))
    assert result.status == "decode_error"
    assert result.verdict is None


def test_score_file_decode_error_text_is_bounded(tmp_path, live_detect_mod, monkeypatch):
    """[if] decode failure includes tool output [then] stored error text is bounded, [else stop]."""
    audio = tmp_path / "exists.mp3"
    audio.write_bytes(b"\x00")
    long_tail = "x" * 500

    def fail_probe(_path: str) -> float:
        raise subprocess.CalledProcessError(1, "ffprobe", stderr=long_tail)

    monkeypatch.setattr(live_detect_mod, "_probe_duration", fail_probe)
    result = live_detect_mod.score_file(MagicMock(), str(audio))
    assert result.status == "decode_error"
    assert result.error is not None
    assert len(result.error) <= 200
