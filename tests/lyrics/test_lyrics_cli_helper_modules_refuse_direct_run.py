"""The `apps.lyrics.cli_*` helper modules refuse to run as `python -m`.

Sat 26 Sep 2026, hands-on retest of the installed Windows build on bifrost1:
`python -m apps.lyrics.cli_pipeline jobs work --once` printed nothing and
exited 0, and so did a bogus-flag control through the same module. The
module only REGISTERS subcommands on `python -m apps.lyrics`'s parser; it has
no entrypoint of its own, so running it imported the file and exited clean.
An agent reading exit 0 concluded the jobs queue was idle. The class is every
helper module in that family, not just the one that was hit.

- if `python -m apps.lyrics.cli_<x> ...` exits 0 then a caller reads "did the
  work" from a run that did nothing -- broken
- if the refusal does not name `python -m apps.lyrics` then the operator is
  told what is wrong but not what to run instead -- broken
- CONTROL: if `python -m apps.lyrics --help` does not exit 0 then the harness,
  not the module, is what failed, and the two tests above prove nothing

-Claude
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HELPER_MODULES = ("apps.lyrics.cli_pipeline", "apps.lyrics.cli_storage", "apps.lyrics.cli_verdicts")


def _run_module(module: str, *argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", module, *argv],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


@pytest.mark.parametrize("module", HELPER_MODULES)
def test_helper_module_run_directly_fails_loud_and_names_the_entrypoint(module: str) -> None:
    result = _run_module(module, "jobs", "work", "--once")
    assert result.returncode != 0, (
        f"if `python -m {module} jobs work --once` exits 0 then broken: it did no work "
        f"(stdout={result.stdout!r}, stderr={result.stderr!r})"
    )
    assert "python -m apps.lyrics " in result.stderr, (
        f"if the refusal from {module} does not name `python -m apps.lyrics` then broken (stderr={result.stderr!r})"
    )


def test_control_the_real_entrypoint_runs_under_the_same_harness() -> None:
    result = _run_module("apps.lyrics", "--help")
    assert result.returncode == 0, (
        "if `python -m apps.lyrics --help` fails under this harness then the refusal "
        f"tests prove nothing (stderr={result.stderr!r})"
    )
    assert "jobs" in result.stdout, "the pipeline subcommands must be reachable on the real entrypoint"
