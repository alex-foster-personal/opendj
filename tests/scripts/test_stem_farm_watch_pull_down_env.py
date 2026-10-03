"""Guard: scripts/stem_farm_watch_pull_down.sh fails loudly without connection
settings, rather than falling back to a hardcoded host/key/project (OSSPUB-01;
PR #4861 removed the real stem-farm IP and GCE project id that used to live
here as silent defaults).

This script is bash-only by nature (an operator task, per AGENTS.md's shell
policy), so these tests skip, naming the reason, on a host with no `bash` on
PATH (e.g. a bare Windows runner) rather than failing with FileNotFoundError.

  - [if] MDT_STEM_HOST/MDT_STEM_SSH_KEY/MDT_VOCAL_GCE_PROJECT are all unset
    [then] the script exits non-zero with a message naming the missing var,
    before attempting any ssh/gcloud call, [else broken]
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts" / "stem_farm_watch_pull_down.sh"
)


def _bash() -> str:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("UNAVAILABLE: bash is required to run stem_farm_watch_pull_down.sh")
        raise AssertionError("unreachable: pytest.skip raises")  # narrows bash for the isolated mypy gate
    return bash


def _run(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_bash(), str(_SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def test_missing_host_fails_fast_with_a_named_error() -> None:
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("MDT_STEM_HOST", "MDT_STEM_SSH_KEY", "MDT_VOCAL_GCE_PROJECT")
    }
    result = _run(env)
    assert result.returncode != 0
    assert "MDT_STEM_HOST" in result.stderr


def test_missing_ssh_key_fails_fast_with_a_named_error() -> None:
    env = dict(os.environ)
    env.pop("MDT_STEM_SSH_KEY", None)
    env["MDT_STEM_HOST"] = "stem-farm.example.invalid"
    env["MDT_VOCAL_GCE_PROJECT"] = "example-project"
    result = _run(env)
    assert result.returncode != 0
    assert "MDT_STEM_SSH_KEY" in result.stderr


def test_missing_gce_project_fails_fast_with_a_named_error() -> None:
    env = dict(os.environ)
    env.pop("MDT_VOCAL_GCE_PROJECT", None)
    env["MDT_STEM_HOST"] = "stem-farm.example.invalid"
    env["MDT_STEM_SSH_KEY"] = "/dev/null"
    result = _run(env)
    assert result.returncode != 0
    assert "MDT_VOCAL_GCE_PROJECT" in result.stderr
