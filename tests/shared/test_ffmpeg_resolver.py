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
  - if a relative MDT_FFMPEG is returned as given, so subprocess PATH-searches
    the bare name and launches nothing or the wrong binary, then broken
  - if the bundled ODJ_FFMPEG_BIN loses to a PATH ffmpeg then broken
  - if the bundled ODJ_FFMPEG_BIN beats an MDT_FFMPEG override then broken
  - if a set-but-broken ODJ_FFMPEG_BIN falls back to PATH then broken
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from apps.analysis.pcm_fingerprint import FingerprintUnavailable, _resolve_or_raise
from apps.analysis_waveform.decode import LocalDecodeUnavailable
from apps.analysis_waveform.decode import resolve_ffmpeg as decode_resolve
from apps.shared import ffmpeg as shared_ffmpeg


@pytest.fixture(autouse=True)
def _no_ambient_bundled_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> None:
    """A payload launch exports ODJ_FFMPEG_BIN; no test may inherit one."""
    monkeypatch.delenv(shared_ffmpeg.BUNDLED_ENV, raising=False)


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


_RELATIVE_OVERRIDE_DRIVER = """
import subprocess, sys
from apps.shared.ffmpeg import resolve_ffmpeg

resolved = resolve_ffmpeg()
assert resolved.startswith("/"), f"resolver returned a relative path: {resolved!r}"
# The point of the whole check: hand the answer to subprocess exactly as a
# caller does. A bare basename is PATH-searched here, and this PATH has no
# customff on it, so an unresolved override raises FileNotFoundError.
subprocess.run([resolved], check=True, stdin=subprocess.DEVNULL, timeout=10)
print(resolved)
"""


def test_a_relative_override_is_returned_absolute_and_is_launchable(tmp_path: Path) -> None:
    """MDT_FFMPEG=customff, present in the cwd and absent from PATH.

    Driven in a real child process with its own cwd, PATH and MDT_FFMPEG
    rather than by mutating this one: the defect is about what subprocess does
    with the returned string, so the evidence has to be an actual launch.
    """
    workdir = tmp_path / "server-cwd"
    workdir.mkdir()
    _make_executable(workdir / "customff")
    empty_path_dir = tmp_path / "empty-path"
    empty_path_dir.mkdir()

    completed = subprocess.run(
        [sys.executable, "-c", _RELATIVE_OVERRIDE_DRIVER],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,  # ruff PLW1510: the return code is inspected explicitly below
        cwd=workdir,
        env={
            **os.environ,
            "MDT_FFMPEG": "customff",
            "PATH": str(empty_path_dir),
            "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
        },
    )
    assert completed.returncode == 0, (
        f"relative override driver failed:\nstdout={completed.stdout}\nstderr={completed.stderr}"
    )
    assert completed.stdout.strip() == str(workdir / "customff")


# ----- bundled payload ffmpeg (ODJ_FFMPEG_BIN) ---------------------------------


def test_bundled_beats_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] the payload ships ffmpeg and PATH has one too [then] the bundled one wins."""
    monkeypatch.delenv("MDT_FFMPEG", raising=False)
    bundled = _make_executable(tmp_path / "payload-ffmpeg")
    path_dir = tmp_path / "path"
    path_dir.mkdir()
    _make_executable(path_dir / "ffmpeg")
    monkeypatch.setenv("PATH", str(path_dir))
    monkeypatch.setenv(shared_ffmpeg.BUNDLED_ENV, str(bundled))
    assert shared_ffmpeg.resolve_ffmpeg() == str(bundled)


def test_override_beats_bundled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] MDT_FFMPEG and the bundled path are both set [then] MDT_FFMPEG wins."""
    override = _make_executable(tmp_path / "operator-ffmpeg")
    bundled = _make_executable(tmp_path / "payload-ffmpeg")
    monkeypatch.setenv("MDT_FFMPEG", str(override))
    monkeypatch.setenv(shared_ffmpeg.BUNDLED_ENV, str(bundled))
    assert shared_ffmpeg.resolve_ffmpeg() == str(override)


def test_broken_bundled_raises_rather_than_using_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] the payload's ffmpeg is missing [then] raise naming ODJ_FFMPEG_BIN, PATH unread."""
    monkeypatch.delenv("MDT_FFMPEG", raising=False)
    monkeypatch.setenv(shared_ffmpeg.BUNDLED_ENV, str(tmp_path / "gone"))
    _make_executable(tmp_path / "ffmpeg")
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(shared_ffmpeg.FfmpegUnavailable, match=shared_ffmpeg.BUNDLED_ENV):
        shared_ffmpeg.resolve_ffmpeg()


def test_consumers_see_the_bundled_binary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] the bundled path is set [then] waveform and fingerprint resolve it too."""
    monkeypatch.delenv("MDT_FFMPEG", raising=False)
    bundled = _make_executable(tmp_path / "payload-ffmpeg")
    monkeypatch.setenv(shared_ffmpeg.BUNDLED_ENV, str(bundled))
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert decode_resolve() == str(bundled)
    assert _resolve_or_raise() == str(bundled)


def test_mutation_control_path_first_order_is_caught(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] the resolver consulted PATH before the bundled path [then] test_bundled_beats_path fails.

    Re-runs that test against a mutated resolver (PATH first) and requires it
    to fail, so the ordering test is known to bite rather than passing under
    any order.
    """
    def path_first() -> str:
        found = shared_ffmpeg.shutil.which(shared_ffmpeg.FFMPEG_BINARY)
        if found:
            return found
        return os.environ[shared_ffmpeg.BUNDLED_ENV]

    monkeypatch.setattr(shared_ffmpeg, "resolve_ffmpeg", path_first)
    with pytest.raises(AssertionError):
        test_bundled_beats_path(tmp_path, monkeypatch)
