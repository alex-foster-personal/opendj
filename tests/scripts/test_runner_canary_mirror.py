"""`scripts/runner_canary_mirror.py`: push one `main` SHA to `canary/<sha>` on the mirror.

Driven for real: a source repository and a bare "mirror" repository are built in
`tmp_path`, and the mirror's https URL is rewritten to the bare repository through an
isolated git config (`url.<file>.insteadOf`), so the script's own URL construction, auth
header, preflight and push all run unmodified. Nothing touches GitHub.

Regression lines:
  - if a missing push token does anything but exit non-zero naming CANARY_MIRROR_PUSH_TOKEN
    then broken
  - if the mirror target can be overridden to the source repository or another owner then
    broken (the canary would push branches, and vendor jobs, where they do not belong)
  - if a SHA that is not on origin/main can be mirrored then broken
  - if an EMPTY mirror is pushed to then broken: the first branch pushed becomes the
    default branch, which arms every `schedule` and `workflow_run` workflow in the mirror
  - if a mirror whose default branch is a canary branch is pushed to then broken (same)
  - if the token or its base64 form reaches the process output or git's argv then broken
  - if a valid push does not land exactly `refs/heads/canary/<sha>` at `<sha>` then broken
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "runner_canary_mirror.py"
CONFIG = json.loads((REPO / "ci" / "runner-canary.json").read_text())
TOKEN = "canary-test-token-4f1d"
GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "canary-test",
    "GIT_AUTHOR_EMAIL": "canary-test@example.invalid",
    "GIT_COMMITTER_NAME": "canary-test",
    "GIT_COMMITTER_EMAIL": "canary-test@example.invalid",
}


# ----- fixtures: a real source repo and a real bare mirror ---------------------


def _git(cwd: Path, *args: str, env: dict[str, str] | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


@pytest.fixture()
def world(tmp_path: Path) -> dict[str, object]:
    gitconfig = tmp_path / "gitconfig"
    mirror = tmp_path / "mirror.git"
    base_env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "GIT_CONFIG_GLOBAL": str(gitconfig),
        "GIT_CONFIG_NOSYSTEM": "1",
        **GIT_IDENTITY,
    }
    gitconfig.write_text(
        f'[url "file://{mirror}"]\n'
        f"\tinsteadOf = https://github.com/{CONFIG['mirror_repository']}.git\n"
        "[init]\n\tdefaultBranch = main\n"
    )
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-q", env=base_env)
    (source / "a.txt").write_text("one\n")
    _git(source, "add", "a.txt", env=base_env)
    _git(source, "commit", "-q", "-m", "one", env=base_env)
    on_main = _git(source, "rev-parse", "HEAD", env=base_env)
    _git(source, "update-ref", "refs/remotes/origin/main", on_main, env=base_env)
    _git(source, "checkout", "-q", "-b", "side", env=base_env)
    (source / "b.txt").write_text("two\n")
    _git(source, "add", "b.txt", env=base_env)
    _git(source, "commit", "-q", "-m", "two", env=base_env)
    off_main = _git(source, "rev-parse", "HEAD", env=base_env)
    _git(tmp_path, "init", "-q", "--bare", str(mirror), env=base_env)
    return {
        "env": base_env,
        "source": source,
        "mirror": mirror,
        "on_main": on_main,
        "off_main": off_main,
    }


def _bootstrap_default_branch(world: dict[str, object], branch: str = "main") -> None:
    """What the ADR's one-time bootstrap does: a workflow-free default branch."""
    env = world["env"]
    scratch = Path(str(world["mirror"]) + "-boot")
    _git(scratch.parent, "init", "-q", str(scratch), env=env)
    (scratch / "README.md").write_text("canary mirror\n")
    _git(scratch, "add", "README.md", env=env)
    _git(scratch, "commit", "-q", "-m", "boot", env=env)
    _git(scratch, "push", "-q", f"file://{world['mirror']}", f"HEAD:refs/heads/{branch}", env=env)
    _git(Path(str(world["mirror"])), "symbolic-ref", "HEAD", f"refs/heads/{branch}", env=env)


def _run(world: dict[str, object], sha: str, **extra_env: str) -> subprocess.CompletedProcess:
    env = {**world["env"], "CANARY_MIRROR_PUSH_TOKEN": TOKEN, **extra_env}
    env = {k: v for k, v in env.items() if v is not None and v != "<unset>"}
    return subprocess.run(
        [sys.executable, str(SCRIPT), sha, "--config", str(REPO / "ci" / "runner-canary.json")],
        cwd=world["source"],
        capture_output=True,
        check=False,
        text=True,
        env=env,
        timeout=60,
    )


def _mirror_refs(world: dict[str, object]) -> dict[str, str]:
    out = _git(
        Path(str(world["mirror"])),
        "for-each-ref",
        "--format=%(refname) %(objectname)",
        env=world["env"],
    )
    return dict(line.split(" ", 1) for line in out.splitlines() if line)


# ----- the happy path, with its own controls -----------------------------------


def test_a_main_sha_lands_exactly_at_canary_sha_and_is_idempotent(world) -> None:
    _bootstrap_default_branch(world)
    sha = world["on_main"]
    first = _run(world, sha)
    assert first.returncode == 0, first.stdout + first.stderr
    assert _mirror_refs(world)[f"refs/heads/canary/{sha}"] == sha
    assert f"refs/heads/canary/{sha}" in first.stdout
    again = _run(world, sha)
    assert again.returncode == 0, again.stdout + again.stderr
    # The bootstrap branch is untouched: the script pushes one ref and nothing else.
    assert set(_mirror_refs(world)) == {"refs/heads/main", f"refs/heads/canary/{sha}"}


def test_the_token_reaches_neither_output_nor_git_argv(world) -> None:
    _bootstrap_default_branch(world)
    result = _run(world, world["on_main"], GIT_TRACE="1")
    assert result.returncode == 0, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    # Control: the trace really is on, so an absent token is a measured absence.
    assert "trace:" in combined and "push" in combined
    encoded = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
    assert TOKEN not in combined and encoded not in combined


# ----- refusals: each one leaves the mirror untouched --------------------------


def test_a_missing_token_is_refused_by_name(world) -> None:
    _bootstrap_default_branch(world)
    result = _run(world, world["on_main"], CANARY_MIRROR_PUSH_TOKEN="<unset>")
    assert result.returncode == 1
    assert "CANARY_MIRROR_PUSH_TOKEN" in result.stdout + result.stderr
    assert set(_mirror_refs(world)) == {"refs/heads/main"}


def test_an_empty_token_is_refused_like_a_missing_one(world) -> None:
    _bootstrap_default_branch(world)
    result = _run(world, world["on_main"], CANARY_MIRROR_PUSH_TOKEN="")
    assert result.returncode == 1
    assert "CANARY_MIRROR_PUSH_TOKEN" in result.stdout + result.stderr


@pytest.mark.parametrize("sha", ["abc123", "Z" * 40, "HEAD", ""])
def test_a_malformed_sha_is_refused(world, sha: str) -> None:
    _bootstrap_default_branch(world)
    result = _run(world, sha)
    assert result.returncode != 0
    assert set(_mirror_refs(world)) == {"refs/heads/main"}


def test_a_sha_not_on_origin_main_is_refused(world) -> None:
    _bootstrap_default_branch(world)
    result = _run(world, world["off_main"])
    assert result.returncode == 1
    assert "not on refs/remotes/origin/main" in result.stdout + result.stderr
    assert set(_mirror_refs(world)) == {"refs/heads/main"}


def test_a_missing_origin_main_ref_is_refused_with_the_fix(world) -> None:
    _bootstrap_default_branch(world)
    _git(world["source"], "update-ref", "-d", "refs/remotes/origin/main", env=world["env"])
    result = _run(world, world["on_main"])
    assert result.returncode == 1
    assert "git fetch origin main" in result.stdout + result.stderr


@pytest.mark.parametrize(
    "override",
    [
        CONFIG["source_repository"],
        "someone-else/music-dj-tools-canary",
        "not-a-repo-slug",
    ],
    ids=["source-repo", "other-owner", "malformed"],
)
def test_a_mirror_override_outside_the_canary_owner_is_refused(world, override: str) -> None:
    _bootstrap_default_branch(world)
    result = _run(world, world["on_main"], CANARY_MIRROR_REPO=override)
    assert result.returncode == 1, result.stdout + result.stderr
    assert set(_mirror_refs(world)) == {"refs/heads/main"}


def test_an_override_inside_the_canary_owner_is_used_and_named(world) -> None:
    """Opposite direction: a legitimate override is honored, not refused with the rest."""
    _bootstrap_default_branch(world)
    alt = f"{CONFIG['mirror_owner']}/another-canary"
    gitconfig = Path(world["env"]["GIT_CONFIG_GLOBAL"])
    gitconfig.write_text(
        gitconfig.read_text()
        + f'[url "file://{world["mirror"]}"]\n\tinsteadOf = https://github.com/{alt}.git\n'
    )
    result = _run(world, world["on_main"], CANARY_MIRROR_REPO=alt)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"{alt} (from CANARY_MIRROR_REPO)" in result.stdout


def test_an_empty_mirror_is_refused_because_the_first_push_becomes_the_default(world) -> None:
    result = _run(world, world["on_main"])
    assert result.returncode == 1
    assert "default branch" in result.stdout + result.stderr
    assert _mirror_refs(world) == {}


def test_a_mirror_whose_default_is_a_canary_branch_is_refused(world) -> None:
    _bootstrap_default_branch(world, branch="canary/0000000000000000000000000000000000000000")
    result = _run(world, world["on_main"])
    assert result.returncode == 1
    assert "default branch" in result.stdout + result.stderr
    assert len(_mirror_refs(world)) == 1
