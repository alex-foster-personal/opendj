"""CloudSync policy contract for issue #1450."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from apps.shared.platform_paths import PROJECT_ROOT

ARTIFACT_KINDS = ("audio", "stem_bundle", "anlz_cache", "vocal_cache", "lyrics_cache")


def _write_policy_config(data_dir: Path, mode: object) -> None:
    state_dir = data_dir / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "cloudsync-policy.json").write_text(json.dumps({"mode": mode}), encoding="utf-8")


def _run_policy(
    data_dir: Path, mode_override: str | None = None
) -> subprocess.CompletedProcess[str]:
    environment = {
        **os.environ,
        "MDT_DATA_DIR": str(data_dir),
    }
    environment.pop("MUSIC_DJ_CLOUDSYNC_MODE", None)
    if mode_override is not None:
        environment["MUSIC_DJ_CLOUDSYNC_MODE"] = mode_override
    return subprocess.run(
        [sys.executable, "-m", "apps.cloud.policy"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


@pytest.mark.requirement("CLOUDSYNC-01")
def test_default_policy_path_is_anchored_to_the_project_root(tmp_path: Path) -> None:
    """[if] a process starts outside the repo [then] it still finds project data, [else stop]."""
    environment = {key: value for key, value in os.environ.items() if key != "MDT_DATA_DIR"}
    inherited_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        str(PROJECT_ROOT)
        if inherited_pythonpath is None
        else f"{PROJECT_ROOT}{os.pathsep}{inherited_pythonpath}"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from apps.cloud.policy import policy_config_path; print(policy_config_path())",
        ],
        check=True,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=environment,
    )
    assert result.stdout.strip() == str(PROJECT_ROOT / "data" / "state" / "cloudsync-policy.json")


@pytest.mark.requirement("CLOUDSYNC-01")
def test_structured_mode_is_a_policy_error(tmp_path: Path) -> None:
    """[if] policy mode is structured JSON [then] loading raises the policy error, [else stop]."""
    data_dir = tmp_path / "data"
    _write_policy_config(data_dir, ["cloud"])
    result = _run_policy(data_dir)
    assert result.returncode != 0
    assert "CloudSyncPolicyError" in result.stderr


@pytest.mark.requirement("CLOUDSYNC-01")
def test_non_utf8_policy_is_a_policy_error(tmp_path: Path) -> None:
    """[if] policy bytes are not UTF-8 [then] loading raises the policy error, [else stop]."""
    data_dir = tmp_path / "data"
    state_dir = data_dir / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "cloudsync-policy.json").write_bytes(b"\xff")
    result = _run_policy(data_dir)
    assert result.returncode != 0
    assert "CloudSyncPolicyError" in result.stderr


@pytest.mark.requirement("CLOUDSYNC-01")
def test_dangling_policy_symlink_is_a_policy_error(tmp_path: Path) -> None:
    """[if] the policy link is dangling [then] loading fails closed, [else stop]."""
    data_dir = tmp_path / "data"
    state_dir = data_dir / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "cloudsync-policy.json").symlink_to(state_dir / "missing-policy.json")
    result = _run_policy(data_dir)
    assert result.returncode != 0
    assert "CloudSyncPolicyError" in result.stderr


@pytest.mark.requirement("CLOUDSYNC-01")
@pytest.mark.parametrize(
    ("mode", "source_of_truth", "remote_processing_allowed"),
    (("cloud", "r2", True), ("local", "local_machine", False)),
)
def test_policy_mode_controls_canonical_artifacts_and_remote_processing(
    tmp_path: Path, mode: str, source_of_truth: str, remote_processing_allowed: bool
) -> None:
    """[if] state config selects a mode [then] every artifact resolves its home, [else stop]."""
    data_dir = tmp_path / "data"
    _write_policy_config(data_dir, mode)
    result = _run_policy(data_dir)
    assert result.returncode == 0, result.stderr
    policy = json.loads(result.stdout)
    assert policy["mode"] == mode
    assert policy["remote_processing_allowed"] is remote_processing_allowed
    assert set(policy["artifacts"]) == set(ARTIFACT_KINDS)
    for artifact in policy["artifacts"].values():
        assert artifact["source_of_truth"] == source_of_truth
        assert artifact["local_cache_path"].startswith("state/")
        if mode == "cloud":
            assert artifact["r2_key_scheme"] == "assets/{sha256[:2]}/{sha256}"
        else:
            assert artifact["r2_key_scheme"] is None
            assert set(artifact["default_mode_by_machine_class"].values()) == {"pinned"}


@pytest.mark.requirement("CLOUDSYNC-01")
def test_environment_override_and_cfg_mappings_are_immutable(tmp_path: Path) -> None:
    """[if] deployment overrides config [then] CFG stays immutable, [else stop]."""
    data_dir = tmp_path / "data"
    _write_policy_config(data_dir, "local")
    result = _run_policy(data_dir, mode_override="cloud")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["mode"] == "cloud"
    immutable = subprocess.run(
        [
            sys.executable,
            "-c",
            "from apps.cloud.policy import CFG; CFG.artifacts['audio'] = CFG.artifacts['audio']",
        ],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "MDT_DATA_DIR": str(data_dir)},
    )
    assert immutable.returncode != 0
    assert "TypeError" in immutable.stderr


@pytest.mark.requirement("CLOUDSYNC-01")
def test_cfg_nested_mappings_do_not_alias_policy_defaults() -> None:
    """[if] backing defaults mutate [then] CFG keeps its resolved value, [else stop]."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from apps.cloud import policy; "
            "policy._CLOUD_DEFAULTS['audio']['portable'] = 'pinned'; "
            "assert policy.CFG.artifacts['audio'].default_mode_by_machine_class['portable'] "
            "== 'cached'",
        ],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "MUSIC_DJ_CLOUDSYNC_MODE": "cloud"},
    )
    assert result.returncode == 0, result.stderr
