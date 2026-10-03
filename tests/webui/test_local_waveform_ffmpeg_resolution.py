"""ffmpeg executable resolution for local waveform decode.

A packaged/GUI-launched app does not inherit Homebrew's PATH the way a shell
does, so a bare ``shutil.which("ffmpeg")`` can find nothing even with ffmpeg
installed. ``MDT_FFMPEG`` is the escape hatch already established by
scripts/vocal_region_worker.py and scripts/stem_bundle_worker.py; this module
adopts the same convention (discussion_r3908337225, issue #735 follow-up).

Split into its own file rather than folded into test_local_waveform_decode.py
(already at the file_size.over_limit_python ceiling) - see that file for the
end-to-end /anlz behavior this unit-level resolver feeds into. The resolver
moved to apps.analysis_waveform.decode with the tri-band split (NATIVE-06).

Regression one-liners:
  - if MDT_FFMPEG is set to a real executable and PATH lookup is used instead then broken
  - if MDT_FFMPEG is set to a non-executable path and it is silently ignored then broken
  - if MDT_FFMPEG is unset and PATH lookup is skipped then broken
  - if a resolved ffmpeg the kernel cannot launch raises OSError instead of
    LocalDecodeUnavailable then broken
"""
from __future__ import annotations

import stat
from pathlib import Path

import pytest

from apps.analysis_waveform import decode
from apps.shared import ffmpeg as shared_ffmpeg


def _make_executable(path: Path) -> Path:
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def test_mdt_ffmpeg_override_wins_over_path_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    override = _make_executable(tmp_path / "custom-ffmpeg")
    monkeypatch.setenv("MDT_FFMPEG", str(override))
    # Patched where the lookup actually lives. decode.resolve_ffmpeg now
    # delegates to apps.shared.ffmpeg, so a patch aimed at decode's own
    # namespace would no longer intercept anything and this assertion would
    # pass without ever biting.
    monkeypatch.setattr(
        shared_ffmpeg.shutil,
        "which",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("PATH lookup must not run")),
    )
    assert decode.resolve_ffmpeg() == str(override)


def test_mdt_ffmpeg_set_but_not_executable_raises_rather_than_falling_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = tmp_path / "not-executable"
    broken.write_text("not a binary")
    monkeypatch.setenv("MDT_FFMPEG", str(broken))
    with pytest.raises(decode.LocalDecodeUnavailable, match="MDT_FFMPEG"):
        decode.resolve_ffmpeg()


def test_mdt_ffmpeg_unset_falls_back_to_path_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MDT_FFMPEG", raising=False)
    on_path = _make_executable(tmp_path / "ffmpeg")
    monkeypatch.setenv("PATH", str(tmp_path))
    assert decode.resolve_ffmpeg() == str(on_path)


def test_neither_mdt_ffmpeg_nor_path_yields_explicit_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MDT_FFMPEG", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "no-binaries-here"))
    with pytest.raises(decode.LocalDecodeUnavailable, match="MDT_FFMPEG"):
        decode.resolve_ffmpeg()


def test_a_kernel_launch_failure_becomes_local_decode_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wrong-architecture binary or a script whose interpreter is missing
    passes resolve_ffmpeg's is_file + X_OK check (both are real) but still
    fails at the kernel's exec step: subprocess.Popen raises OSError, not a
    nonzero ffmpeg exit. Real repro, no mocked exception: an executable
    script naming an interpreter that does not exist, which the kernel
    rejects at exec time (discussion_r3909904294, issue #735 follow-up)."""
    unlaunchable = tmp_path / "ffmpeg"
    unlaunchable.write_text("#!/nonexistent-interpreter-xyz-12345\nexit 0\n")
    unlaunchable.chmod(
        unlaunchable.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH
    )
    monkeypatch.setenv("MDT_FFMPEG", str(unlaunchable))
    monkeypatch.setenv("MDT_WAVEFORM_DECODER", "ffmpeg")

    with pytest.raises(decode.LocalDecodeUnavailable, match="could not be launched"):
        decode.decode_peaks(tmp_path / "irrelevant.wav")

pytestmark = pytest.mark.rb_parity
