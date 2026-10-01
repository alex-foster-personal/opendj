"""Tests-tree conftest -- cross-platform collection gate.

Distinct from the repo-root ``conftest.py`` (which registers the reqs
plugin -- coverage-matrix.md writer + ``--live-db`` gate). This module
owns collection-time test gates, including the marker-owned Rekordbox parity
suite and skipping ``requires_darwin``-marked items off macOS.

Both this hook and the reqs plugin's own ``pytest_collection_modifyitems``
run -- pytest calls every registered implementation of a hook, it is not
a single-winner override.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# SEC-01 (issue #2689): the daemon host allowlist middleware
# (apps/webui/server/request_guard.py) rejects any request whose Host header
# is not allowlisted. starlette.testclient.TestClient defaults base_url to
# "http://testserver", which is not allowlisted, so every TestClient built
# anywhere in this tree without an explicit base_url got a 403
# HOST_NOT_ALLOWED instead of reaching the route under test -- seen across
# tests/webui, tests/cloudsync, tests/engine_core, tests/scripts and
# tests/analysis_contract, not just the two suites that own an app fixture.
# This is the ONE place that default lives for the whole tests/ tree; see
# tests/testclient_host_allowlist.py for why a shared default beats patching
# every call site. A caller that passes its own base_url (e.g. to exercise a
# foreign-host rejection, see tests/webui/test_request_guard.py) is
# untouched -- this only changes the default.
#
# Gated on starlette being installed: CI runs some quality suites (the
# frontend typing gate among them) in an isolated pytest-only environment,
# and this conftest loads for them too. Without starlette no TestClient can
# exist, so there is no default to patch; importing the helper there would
# only fail every such suite at conftest load.
if importlib.util.find_spec("starlette") is not None:
    from tests.testclient_host_allowlist import install_loopback_testclient_default

    install_loopback_testclient_default()


def _can_import(module_name: str) -> bool:
    """Return whether an optional dependency is actually importable."""
    try:
        __import__(module_name)
    except Exception:  # noqa: BLE001 - optional package imports may be broken.
        return False
    return True


# Optional-dependency gates, resolved once at collection time. Absence is a
# SKIP (the extra is deliberately opt-in), never a silent pass or a failure.
_HAS_JOBLIB: bool = importlib.util.find_spec("joblib") is not None
_HAS_AUDIO_STACK: bool = (
    _can_import("soundfile") and _can_import("librosa")
)
_HAS_FPCALC: bool = shutil.which("fpcalc") is not None
# ffmpeg is the local waveform decoder (apps.webui.server.rb_vendor_pkg.
# local_waveform). Absence is a real, tested runtime state -- the tests that
# assert what a MISSING ffmpeg produces run everywhere; only the ones that
# need a real decode are gated here.
_HAS_FFMPEG: bool = shutil.which("ffmpeg") is not None


def _ffmpeg_can_resample() -> bool:
    """True when this host's ffmpeg can actually run the pinned soxr chain."""
    try:
        from apps.analysis.pcm_fingerprint import (
            FingerprintUnavailable,
            require_resampler,
        )
    except ImportError:
        # apps.analysis.pcm_fingerprint pulls in numpy through
        # apps.analysis_waveform.decode. Some CI lanes (e.g. the frontend
        # typing gate) run pytest against a deliberately minimal, isolated
        # env with none of that installed, and this conftest is the root one
        # every pytest invocation in the repo collects. A host missing the
        # stack this probe needs genuinely cannot run the canonical soxr
        # decode either, so that is the correct, honest answer here -- not a
        # collection-time crash for suites nowhere near this lane.
        return False

    try:
        require_resampler()
    except FingerprintUnavailable:
        return False
    return True


# soxr is a BUILD option of ffmpeg, not a runtime flag: every build accepts
# `resampler=soxr` as an option value and a build without libsoxr then fails
# at filter-configure time. decode_fingerprint is defined over that
# resampler, so the probe RUNS the filter chain rather than reading a
# version string (Homebrew ffmpeg 9.0.1 on macOS passes the first check and
# fails the second, measured Wed 9 Sep 2026).
#
# Called UNCONDITIONALLY, not gated behind `_HAS_FFMPEG`: that gate is a bare
# PATH lookup, but `_ffmpeg_can_resample` resolves through the production
# `resolve_ffmpeg` (MDT_FFMPEG override first, else PATH) via
# `require_resampler`. Gating this call behind `_HAS_FFMPEG` meant a host with
# ffmpeg available ONLY through `MDT_FFMPEG` - as a packaged or GUI-launched
# app without Homebrew on PATH is expected to be - never ran the probe at
# all, so `_HAS_SOXR` stayed False and every `requires_soxr` test was skipped
# despite the capability being present (Codex P2 BLOCKING, PR #1587).
# `_ffmpeg_can_resample` already catches an absent/broken ffmpeg (ImportError,
# `FingerprintUnavailable`) and returns False, so calling it with no ffmpeg at
# all anywhere is safe.
_HAS_SOXR: bool = _ffmpeg_can_resample()
# madmom is not installable from PyPI on Python 3.10+ (0.16.1 imports the
# long-removed collections.MutableSequence), so requirements.txt pulls the
# git HEAD with --no-build-isolation and no pyproject extra can supply it.
# CI installs it and runs these tests; a plain `uv sync` venv cannot.
_HAS_MADMOM: bool = importlib.util.find_spec("madmom") is not None


def _expected_ci_venv_root(repo_root: Path) -> Path | None:
    """Return this repo's own venv root, or None if it was never provisioned.

    Scoped deliberately narrow (issue #3486), because a blunt assert here
    would fail every session whose interpreter is legitimately not this
    venv, not just the wrong-interpreter class it targets:

    - CI only (``CI`` env var). A local dev run has no such convention to
      enforce and must never start failing because of one.
    - Only when ``.venv`` was actually provisioned at the path this repo's
      own tooling uses (``scripts/ci_venv.sh``, ``uv sync``). A job that
      never provisions one has nothing to compare against.
    - Per-invocation opt-out via ``CI_VENV_PROBE_ALLOW_MISMATCH``, for the
      one CI step that runs pytest in a deliberately isolated,
      dependency-free uv environment on purpose
      (.github/workflows/ci.yml "Frontend typing-gate unit tests").
    """
    if not os.environ.get("CI") or os.environ.get("CI_VENV_PROBE_ALLOW_MISMATCH"):
        return None
    venv_root = repo_root / ".venv"
    return venv_root if (venv_root / "pyvenv.cfg").is_file() else None


def _log_ci_venv_probe(phase: str) -> None:
    """Record the test interpreter and a non-preloading audio import probe.

    Also fail-fasts, within the narrow scope above, when this session is not
    running inside this repo's own venv -- the trap CLAUDE.md documents:
    ``uv run`` falls back to a PATH command's own interpreter when the
    command is absent from the project environment.

    Compares ``sys.prefix`` (the active venv root, set from ``pyvenv.cfg``
    regardless of where the underlying interpreter binary lives), never
    ``sys.executable``: a POSIX ``uv``-managed venv's ``bin/python`` is a
    symlink into a base interpreter shared across every venv on the machine
    (``~/.local/share/uv/python/...``), so a PATH-resolved ``pytest`` running
    under an unrelated venv on the identical Python build would resolve to
    that same shared binary and pass a binary-path comparison while still
    lacking every project dependency (Codex P1 on PR #3487, issue #3486).
    """
    probe = (
        "try:\n"
        "    import soundfile\n"
        "except ModuleNotFoundError as error:\n"
        "    if error.name == 'soundfile':\n"
        "        print('soundfile=absent')\n"
        "    else:\n"
        "        print(f'soundfile=ERROR: {type(error).__name__}: {error}')\n"
        "except Exception as error:\n"
        "    print(f'soundfile=ERROR: {type(error).__name__}: {error}')\n"
        "else:\n"
        "    print('soundfile=present')\n"
    )
    try:
        completed = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            check=False,
            text=True,
        )
        result = (completed.stdout or completed.stderr or "").strip().replace(
            "\n", " | "
        )
        if completed.returncode != 0:
            if "soundfile=ERROR" not in result:
                exit_detail = (
                    f"soundfile=ERROR: subprocess exit {completed.returncode}"
                )
                result = f"{result} | {exit_detail}" if result else exit_detail
        elif not result:
            result = "soundfile=ERROR: subprocess produced no output"
    except OSError as error:
        result = f"soundfile=ERROR: {type(error).__name__}: {error}"
    print(
        "CI_VENV_PROBE "
        f"phase={phase} pid={os.getpid()} executable={sys.executable!r} "
        f"prefix={sys.prefix!r} venv_exists={(Path.cwd() / '.venv').is_dir()} {result}",
        flush=True,
    )
    repo_root = Path(__file__).resolve().parents[1]
    expected_root = _expected_ci_venv_root(repo_root)
    if expected_root is None:
        return
    actual_root = Path(sys.prefix).resolve()
    if actual_root != expected_root.resolve():
        raise RuntimeError(
            f"CI_VENV_PROBE phase={phase}: wrong interpreter -- expected this "
            f"repo's own venv at {str(expected_root.resolve())!r} (sys.prefix) "
            f"but this pytest session is running under {str(actual_root)!r} "
            f"(executable={sys.executable!r}). `uv run` falls back to a PATH "
            "command's own interpreter when the command is absent from the "
            "project environment (see CLAUDE.md); run `uv sync --extra dev` "
            "first. If this session is a deliberately isolated, "
            "dependency-free pytest run, set CI_VENV_PROBE_ALLOW_MISMATCH=1 "
            "for it."
        )


def _has_rb_parity_marker(path: Path) -> bool:
    """Return whether a test module belongs to the focused parity gate.

    Every tests/webui test_*.py is gate-owned: the focused-gate module
    enrolls them all by rglob, so a newly added one must be collected (and
    then marker-selected by tests/webui/conftest.py) with no hand-written
    marker (issue #1140). Any other module must declare the marker in source
    to be gate-owned.
    """
    if not (path.name.startswith("test_") and path.suffix == ".py"):
        return False
    if path.is_relative_to(Path(__file__).resolve().parent / "webui"):
        return True
    return "pytest.mark.rb_parity" in path.read_text(encoding="utf-8")


def _contains_rb_parity_marker(path: Path) -> bool:
    """Return whether a collection path contains a marker-owned test module."""
    if path.is_file():
        return _has_rb_parity_marker(path)
    if path.is_dir():
        return any(_has_rb_parity_marker(module) for module in path.rglob("test_*.py"))
    return False


def pytest_ignore_collect(
    collection_path: Path,
    config: pytest.Config,
) -> bool | None:
    """Avoid importing unowned modules when the focused marker gate runs."""
    mark_expression = str(getattr(config.option, "markexpr", "") or "").strip()
    if mark_expression != "rb_parity":
        return None
    return not _contains_rb_parity_marker(Path(str(collection_path)))


def pytest_collection_finish(session: pytest.Session) -> None:
    """Snapshot the environment after collection chose optional-dependency skips."""
    _log_ci_venv_probe("collection")


def pytest_runtestloop(session: pytest.Session) -> None:
    """Snapshot the same environment immediately before test-body execution."""
    _log_ci_venv_probe("execution")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip ``requires_*``-marked items whose platform/extra is absent."""
    skip_darwin = pytest.mark.skip(reason="macOS-only")
    skip_joblib = pytest.mark.skip(reason="needs joblib")
    skip_madmom = pytest.mark.skip(
        reason="needs madmom (git HEAD; `pip install -r requirements.txt`)"
    )
    # UNAVAILABLE, not "fine": the analysis extra gates PARITY-03's only
    # end-to-end acceptance test, the one proving a real drain decodes audio
    # and writes a canonical analysis row. CI's pytest lane installs
    # requirements.txt, so librosa and soundfile are always present there and
    # that test really does gate every PR. On a bare `uv sync` dev venv it is
    # unmeasured, and the reason has to say so rather than read as a pass.
    skip_audio = pytest.mark.skip(
        reason="UNAVAILABLE: needs the analysis extra (soundfile/librosa), so "
               "the end-to-end drain is unmeasured here. This is a capability "
               "report, not a pass. CI installs it via requirements.txt."
    )
    skip_fpcalc = pytest.mark.skip(reason="needs chromaprint's fpcalc on PATH")
    skip_ffmpeg = pytest.mark.skip(reason="needs ffmpeg on PATH")
    skip_soxr = pytest.mark.skip(
        reason="UNAVAILABLE: this host's ffmpeg has no libsoxr, so the "
               "canonical decode fingerprint is unmeasured here. This is a "
               "capability report, not a pass."
    )
    for item in items:
        if sys.platform != "darwin" and "requires_darwin" in item.keywords:
            item.add_marker(skip_darwin)
        if not _HAS_JOBLIB and "requires_joblib" in item.keywords:
            item.add_marker(skip_joblib)
        if not _HAS_MADMOM and "requires_madmom" in item.keywords:
            item.add_marker(skip_madmom)
        if not _HAS_AUDIO_STACK and "requires_audio_stack" in item.keywords:
            item.add_marker(skip_audio)
        if not _HAS_FPCALC and "requires_fpcalc" in item.keywords:
            item.add_marker(skip_fpcalc)
        if not _HAS_FFMPEG and "requires_ffmpeg" in item.keywords:
            item.add_marker(skip_ffmpeg)
        if not _HAS_SOXR and "requires_soxr" in item.keywords:
            item.add_marker(skip_soxr)


@pytest.fixture(autouse=True)
def _stem_cache_budget_sees_a_roomy_disk(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the suite's result independent of this machine's free space.

    ``hydrate_one`` enforces the disk-aware stem cache budget against the
    REAL volume (STEM-39). On a host under the floor, that would evict a
    test's own R2-confirmed bundles and turn a pass into a fail by the state
    of someone's laptop. Every test therefore sees a volume with ample room
    unless it injects a measurement itself (``disk=`` or its own monkeypatch,
    which runs after this fixture and wins).
    """
    from apps.cloud import stem_cache_budget

    roomy = stem_cache_budget.DiskUsage(
        total_bytes=1000 * stem_cache_budget.GIB, free_bytes=900 * stem_cache_budget.GIB
    )
    monkeypatch.setattr(stem_cache_budget, "measure_disk", lambda _path: roomy)
