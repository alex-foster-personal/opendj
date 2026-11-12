"""CLI wiring for tests.audio.render_loudness."""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.loudness.scan import LoudnessError, require_ffmpeg
from tests.audio.ffmpeg_fixtures import sine_wav
from tests.audio.render_loudness import build_parser, run_compare_command


def _ffmpeg_available() -> bool:
    try:
        require_ffmpeg()
        return True
    except LoudnessError:
        return False


pytestmark = pytest.mark.skipif(
    not _ffmpeg_available(),
    reason="integrated loudness uses resolved ffmpeg (MDT_FFMPEG or PATH)",
)


def test_cli_parser_defaults_tolerance() -> None:
    args = build_parser().parse_args(["a.wav", "b.wav"])
    assert args.tolerance_lu == 0.5


def test_cli_matching_files_exit_zero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """[if] CLI sees a match within 0.5 LU [then] exit 0, [else stop]."""
    source = sine_wav(tmp_path / "source.wav", 0.5)
    render = sine_wav(tmp_path / "render.wav", 0.5)
    args = build_parser().parse_args([str(source), str(render)])
    code = run_compare_command(args)
    out = capsys.readouterr()
    assert code == 0
    assert "ok source=" in out.out
    assert "LUFS" in out.out


def test_cli_mismatch_exit_one_reports_both_lufs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """[if] CLI sees >0.5 LU delta [then] exit 1 with both LUFS in stderr, [else stop]."""
    source = sine_wav(tmp_path / "source.wav", 0.5)
    render = sine_wav(tmp_path / "render.wav", 0.05)
    args = build_parser().parse_args([str(source), str(render)])
    code = run_compare_command(args)
    err = capsys.readouterr().err
    assert code == 1
    assert "source integrated" in err
    assert "render integrated" in err
    assert "LUFS" in err


def test_cli_unreadable_exit_two(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """[if] CLI input is unreadable [then] exit 2 and no ok line, [else stop]."""
    source = sine_wav(tmp_path / "source.wav", 0.5)
    missing = tmp_path / "nope.wav"
    args = build_parser().parse_args([str(source), str(missing)])
    code = run_compare_command(args)
    captured = capsys.readouterr()
    assert code == 2
    assert "[ERROR]" in captured.err
    assert "ok " not in captured.out
