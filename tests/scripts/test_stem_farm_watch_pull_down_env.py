"""Guard: scripts/stem_farm_watch_pull_down.sh fails loudly without connection
settings, rather than falling back to a hardcoded host/key/project (OSSPUB-01;
PR #4861 removed the real stem-farm IP and GCE project id that used to live
here as silent defaults).

  - [if] MDT_STEM_HOST/MDT_STEM_SSH_KEY/MDT_VOCAL_GCE_PROJECT are all unset
    [then] the script exits non-zero with a message naming the missing var,
    before attempting any ssh/gcloud call, [else broken]
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

_SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts" / "stem_farm_watch_pull_down.sh"
)


def _run_without(*unset: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k not in unset}
    return subprocess.run(
        ["bash", str(_SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_missing_host_fails_fast_with_a_named_error() -> None:
    result = _run_without(
        "MDT_STEM_HOST", "MDT_STEM_SSH_KEY", "MDT_VOCAL_GCE_PROJECT"
    )
    assert result.returncode != 0
    assert "MDT_STEM_HOST" in result.stderr


def test_missing_ssh_key_fails_fast_with_a_named_error() -> None:
    env = dict(os.environ)
    env.pop("MDT_STEM_SSH_KEY", None)
    env["MDT_STEM_HOST"] = "stem-farm.example.invalid"
    env["MDT_VOCAL_GCE_PROJECT"] = "example-project"
    result = subprocess.run(
        ["bash", str(_SCRIPT)], env=env, capture_output=True, text=True, timeout=10
    )
    assert result.returncode != 0
    assert "MDT_STEM_SSH_KEY" in result.stderr


def test_missing_gce_project_fails_fast_with_a_named_error() -> None:
    env = dict(os.environ)
    env.pop("MDT_VOCAL_GCE_PROJECT", None)
    env["MDT_STEM_HOST"] = "stem-farm.example.invalid"
    env["MDT_STEM_SSH_KEY"] = "/dev/null"
    result = subprocess.run(
        ["bash", str(_SCRIPT)], env=env, capture_output=True, text=True, timeout=10
    )
    assert result.returncode != 0
    assert "MDT_VOCAL_GCE_PROJECT" in result.stderr
