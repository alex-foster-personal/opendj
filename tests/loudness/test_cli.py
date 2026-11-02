"""CLI wiring for apps.loudness (parser defaults and human rendering)."""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.loudness.cli import _render_human, build_parser, run_scan_command
from apps.loudness.scan import DEFAULT_CEILING_DBTP, DEFAULT_TARGET_LUFS


def test_build_parser_scan_defaults_match_module_constants() -> None:
    args = build_parser().parse_args(["scan", "track.wav"])
    assert args.command == "scan"
    assert args.tracks == [Path("track.wav")]
    assert args.target_lufs == DEFAULT_TARGET_LUFS
    assert args.ceiling_dbtp == DEFAULT_CEILING_DBTP
    assert args.json is False


def test_render_human_notes_peak_clamp() -> None:
    payload = {
        "integrated_lufs": -20.0,
        "loudness_range_lu": 5.0,
        "true_peak_dbtp": -2.0,
        "gain_db": 1.0,
        "gain_clamped": True,
    }
    text = _render_human(Path("/music/hot.wav"), payload)
    assert "hot.wav" in text
    assert "(clamped by peak ceiling)" in text
    assert "     1.0 dB" in text


def test_run_scan_command_missing_file_exits_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "nope.wav"
    parser = build_parser()
    args = parser.parse_args(["scan", str(missing)])
    code = run_scan_command(args)
    captured = capsys.readouterr()
    assert code == 1
    assert "[ERROR]" in captured.err
    assert "not a file" in captured.err
