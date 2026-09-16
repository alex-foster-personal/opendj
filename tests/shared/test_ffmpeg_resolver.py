"""The one ffmpeg lookup: MDT_FFMPEG first, then PATH, never a silent fallback.

apps.shared.ffmpeg is where "where is ffmpeg" is answered for the whole repo.
Its two consumers translate its failure into their own type, so these checks
cover both the resolution itself and that each consumer's translation still
carries the reason rather than swallowing it.

Regression one-liners:
  - if MDT_FFMPEG names an executable and PATH is consulted anyway then broken
  - if a set-but-non-executable MDT_FFMPEG falls back to PATH then broken
  - if MDT_FFMPEG is unset and the PATH hit is not returned then broken
  - if neither is available and the error does not name MDT_FFMPEG then broken
  - if a resolution failure reaches a caller as a bare RuntimeError, rather
    than that caller's own unavailable-type, then broken
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from apps.analysis.pcm_fingerprint import FingerprintUnavailable, _resolve_or_raise
from apps.analysis_waveform.decode import LocalDecodeUnavailable
from apps.analysis_waveform.decode import resolve_ffmpeg as decode_resolve
from apps.shared import ffmpeg as shared_ffmpeg


def _make_executable(path: Path) -> Path:
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def test_override_wins_and_path_is_never_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    override = _make_executable(tmp_path / "custom-ffmpeg")
    monkeypatch.setenv("MDT_FFMPEG", str(override))
    monkeypatch.setattr(
        shared_ffmpeg.shutil,
        "which",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("PATH lookup must not run")),
    )
    assert shared_ffmpeg.resolve_ffmpeg() == str(override)


def test_broken_override_raises_rather_than_falling_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = tmp_path / "not-executable"
    broken.write_text("not a binary")
    monkeypatch.setenv("MDT_FFMPEG", str(broken))
    on_path = _make_executable(tmp_path / "ffmpeg")
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(shared_ffmpeg.FfmpegUnavailable, match="MDT_FFMPEG"):
        shared_ffmpeg.resolve_ffmpeg()
    # The fallback the override must NOT reach was genuinely available, so
    # this is a refusal rather than an absence.
    assert on_path.exists()


def test_unset_override_falls_back_to_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MDT_FFMPEG", raising=False)
    on_path = _make_executable(tmp_path / "ffmpeg")
    monkeypatch.setenv("PATH", str(tmp_path))
    assert shared_ffmpeg.resolve_ffmpeg() == str(on_path)


def test_neither_available_names_the_override_in_the_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MDT_FFMPEG", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "no-binaries-here"))
    with pytest.raises(shared_ffmpeg.FfmpegUnavailable, match="MDT_FFMPEG"):
        shared_ffmpeg.resolve_ffmpeg()


def test_waveform_decode_translates_the_failure_to_its_own_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MDT_FFMPEG", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "no-binaries-here"))
    with pytest.raises(LocalDecodeUnavailable, match="MDT_FFMPEG"):
        decode_resolve()


def test_pcm_fingerprint_translates_the_failure_to_its_own_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MDT_FFMPEG", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "no-binaries-here"))
    with pytest.raises(FingerprintUnavailable, match="MDT_FFMPEG"):
        _resolve_or_raise()


def test_the_shared_module_is_the_only_copy_of_the_lookup() -> None:
    """A second private MDT_FFMPEG resolver in an apps module is the drift.

    scripts/ is excluded on purpose: stem_bundle_worker.py and
    vocal_region_worker.py read MDT_FFMPEG with deliberately different
    semantics (a bare-name fallback, and a PATH mutation), documented in
    apps/shared/ffmpeg.py.
    """
    repo_root = Path(__file__).resolve().parents[2]
    offenders = [
        path
        for path in (repo_root / "apps").rglob("*.py")
        if path != repo_root / "apps" / "shared" / "ffmpeg.py"
        and 'environ.get("MDT_FFMPEG")' in path.read_text(encoding="utf-8", errors="replace")
    ]
    assert offenders == [], (
        "these modules read MDT_FFMPEG themselves instead of calling "
        f"apps.shared.ffmpeg.resolve_ffmpeg: {[str(p) for p in offenders]}"
    )
