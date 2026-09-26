"""Runtime policy module tests (issue #284, POLICY-01)."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from apps.adapters.rekordbox import config as rb_config

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _flatten(body: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for group in body["groups"]:
        for item in group["items"]:
            out[item["key"]] = item
    return out


@pytest.mark.requirement("POLICY-01")
def test_runtime_policy_shipped_defaults():
    """[if] runtime_policy imports [then] shipped defaults match 4/38400/30s, [else stop]."""
    from apps.shared import runtime_policy

    assert runtime_policy.HIDE_BROKEN_PLAYLIST_MIN_AVAILABLE_TRACKS == 4
    assert runtime_policy.ANLZ_POINTS_DEFAULT == 38400
    assert runtime_policy.ANLZ_POINTS_MIN == 100
    assert runtime_policy.ANLZ_POINTS_MAX == 38400
    assert runtime_policy.FILE_EXISTS_TTL_S == 30.0
    assert rb_config.FILE_EXISTS_TTL_S == 30.0


@pytest.mark.requirement("POLICY-01")
def test_mostly_broken_playlist_rules():
    """[if] mostly_broken_playlist runs [then] fast-list and min-track rules hold, [else stop]."""
    from apps.shared.runtime_policy import mostly_broken_playlist

    assert mostly_broken_playlist(-1, 10) is False
    assert mostly_broken_playlist(0, 0) is True
    assert mostly_broken_playlist(3, 100) is True
    assert mostly_broken_playlist(4, 100) is False
    assert mostly_broken_playlist(50, 100) is False


@pytest.mark.requirement("POLICY-01")
def test_settings_exposes_runtime_policy_keys(client):
    """[if] GET /api/v1/settings [then] all five POLICY-01 keys publish defaults, [else stop]."""
    items = _flatten(client.get("/api/v1/settings").json())
    assert items["hide_broken_playlist_min_available_tracks"]["value"] == 4
    assert items["anlz_points_default"]["value"] == 38400
    assert items["anlz_points_min"]["value"] == 100
    assert items["anlz_points_max"]["value"] == 38400
    assert items["file_exists_ttl_s"]["value"] == 30.0


def _import_policy_in_subprocess(env: dict[str, str], code: str) -> subprocess.CompletedProcess:
    """Import the policy modules in a fresh interpreter with ``env`` applied.

    The knobs resolve at import time, so an override can only be observed by a
    process that imports them after the variable is set. A subprocess keeps
    this test from rewriting ``sys.modules`` or the environment for every test
    that runs after it (Codex P1 on #3739).
    """
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=PROJECT_ROOT,
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.mark.requirement("POLICY-01")
def test_file_exists_ttl_env_override():
    """[if] MDT_FILE_EXISTS_TTL_S is set [then] config and settings read 45s, [else stop]."""
    result = _import_policy_in_subprocess(
        {"MDT_FILE_EXISTS_TTL_S": "45"},
        "from apps.shared import runtime_policy\n"
        "from apps.adapters.rekordbox import config\n"
        "print(runtime_policy.FILE_EXISTS_TTL_S, config.FILE_EXISTS_TTL_S)",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["45.0", "45.0"]


@pytest.mark.requirement("POLICY-01")
@pytest.mark.parametrize(
    ("name", "raw"),
    [
        ("MDT_HIDE_BROKEN_PLAYLIST_MIN_AVAILABLE_TRACKS", "0"),
        ("MDT_FILE_EXISTS_TTL_S", "0"),
        ("MDT_FILE_EXISTS_TTL_S", "nan"),
        ("MDT_FILE_EXISTS_TTL_S", "inf"),
    ],
)
def test_invalid_policy_raises_at_import(name: str, raw: str):
    """[if] a policy env is out of range or non-finite [then] import raises, [else stop]."""
    result = _import_policy_in_subprocess({name: raw}, "import apps.shared.runtime_policy")
    assert result.returncode != 0
    assert "ValueError" in result.stderr
    assert name in result.stderr


@pytest.mark.requirement("POLICY-01")
def test_policy_import_succeeds_in_subprocess_without_overrides():
    """[if] no policy env is set [then] the subprocess import succeeds, [else stop]."""
    env_without_policy = {k: v for k, v in os.environ.items() if not k.startswith("MDT_")}
    result = subprocess.run(
        [sys.executable, "-c", "import apps.shared.runtime_policy"],
        cwd=PROJECT_ROOT,
        env=env_without_policy,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
