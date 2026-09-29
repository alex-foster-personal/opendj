"""`scripts/runner_canary_mirror.py`: push one `main` SHA to `canary/<sha>` in a target.

Two targets (ci/runner-canary.json `target_repo` per vendor): `--target mirror`, the canary
org's mirror, for Blacksmith and Ubicloud; `--target source`, the source repository itself
through the checkout's own `origin`, for Avrea and Tenki.

Driven for real: a source checkout and two bare repositories (the mirror, and the source
repository's "remote") are built in `tmp_path`, and their https URLs are rewritten to the
bare repositories through an isolated git config (`url.<file>.insteadOf`), so the script's
own URL construction, auth header, preflight and push all run unmodified. Nothing touches
GitHub.

Regression lines:
  - if a missing push token does anything but exit non-zero naming CANARY_MIRROR_PUSH_TOKEN
    then broken
  - if CANARY_MIRROR_REPO, the retired override, is silently ignored or honored then broken
    (it let a push land in a repository whose budget gate starts at zero minutes)
  - if `--target source` pushes anywhere but the checkout's `origin` when that origin IS
    the configured source repository then broken, in both directions
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
    # The source repository's own remote: a bare repository whose default branch is main,
    # reached through the checkout's `origin` at its real GitHub URL.
    real = tmp_path / "real.git"
    _git(tmp_path, "init", "-q", "--bare", str(real), env=base_env)
    _git(source, "push", "-q", f"file://{real}", f"{on_main}:refs/heads/main", env=base_env)
    with gitconfig.open("a") as fh:
        fh.write(
            f'[url "file://{real}"]\n'
            f"\tinsteadOf = https://github.com/{CONFIG['source_repository']}.git\n"
        )
    _git(
        source,
        "remote",
        "add",
        "origin",
        f"https://github.com/{CONFIG['source_repository']}.git",
        env=base_env,
    )
    return {
        "env": base_env,
        "source": source,
        "mirror": mirror,
        "real": real,
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


def _run(
    world: dict[str, object], sha: str, target: str | None = "mirror", **extra_env: str
) -> subprocess.CompletedProcess:
    env = {**world["env"], "CANARY_MIRROR_PUSH_TOKEN": TOKEN, **extra_env}
    env = {k: v for k, v in env.items() if v is not None and v != "<unset>"}
    target_args = [] if target is None else ["--target", target]
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            sha,
            *target_args,
            "--config",
            str(REPO / "ci" / "runner-canary.json"),
        ],
        cwd=world["source"],
        capture_output=True,
        check=False,
        text=True,
        env=env,
        timeout=60,
    )


def _mirror_refs(world: dict[str, object], which: str = "mirror") -> dict[str, str]:
    out = _git(
        Path(str(world[which])),
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


def test_the_retired_mirror_override_is_refused_by_name_not_ignored(world) -> None:
    """Codex P1 on #4472: an override let a push land where the budget gate read zero."""
    _bootstrap_default_branch(world)
    result = _run(world, world["on_main"], CANARY_MIRROR_REPO=CONFIG["mirror_repository"])
    assert result.returncode == 1, result.stdout + result.stderr
    assert "CANARY_MIRROR_REPO is no longer honored" in result.stdout
    assert set(_mirror_refs(world)) == {"refs/heads/main"}


def test_the_target_must_be_named(world) -> None:
    _bootstrap_default_branch(world)
    result = _run(world, world["on_main"], target=None)
    assert result.returncode == 2
    assert "--target" in result.stderr
    assert set(_mirror_refs(world)) == {"refs/heads/main"}


# ----- --target source: the source repository, through the checkout's origin --------


def test_source_target_lands_canary_sha_through_origin_without_the_mirror_token(world) -> None:
    sha = world["on_main"]
    result = _run(world, sha, target="source", CANARY_MIRROR_PUSH_TOKEN="<unset>")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _mirror_refs(world, "real")[f"refs/heads/canary/{sha}"] == sha
    assert set(_mirror_refs(world, "real")) == {"refs/heads/main", f"refs/heads/canary/{sha}"}
    # Control: the mirror, the other target, was not touched.
    assert _mirror_refs(world) == {}


def test_source_target_refuses_an_origin_that_is_not_the_source_repository(world) -> None:
    other = "someone-else/music-dj-tools"
    gitconfig = Path(world["env"]["GIT_CONFIG_GLOBAL"])
    # Route the impostor URL to the same bare repository, so a missing guard WOULD land.
    gitconfig.write_text(
        gitconfig.read_text()
        + f'[url "file://{world["real"]}"]\n\tinsteadOf = https://github.com/{other}.git\n'
    )
    _git(
        world["source"],
        "remote",
        "set-url",
        "origin",
        f"https://github.com/{other}.git",
        env=world["env"],
    )
    result = _run(world, world["on_main"], target="source")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "origin is" in result.stdout and "not the source repository" in result.stdout
    assert set(_mirror_refs(world, "real")) == {"refs/heads/main"}


@pytest.mark.parametrize(
    "url",
    [
        "git@github.com:{repo}.git",
        "ssh://git@github.com/{repo}.git",
        "https://github.com/{repo}",
    ],
    ids=["scp", "ssh", "https-no-suffix"],
)
def test_source_target_accepts_every_spelling_of_the_source_origin(world, url: str) -> None:
    """Opposite direction: the guard must not refuse the real repository spelled otherwise."""
    spelled = url.format(repo=CONFIG["source_repository"])
    gitconfig = Path(world["env"]["GIT_CONFIG_GLOBAL"])
    gitconfig.write_text(
        gitconfig.read_text() + f'[url "file://{world["real"]}"]\n\tinsteadOf = {spelled}\n'
    )
    _git(world["source"], "remote", "set-url", "origin", spelled, env=world["env"])
    result = _run(world, world["on_main"], target="source")
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"refs/heads/canary/{world['on_main']}" in _mirror_refs(world, "real")


def test_source_target_refuses_a_sha_not_on_main(world) -> None:
    result = _run(world, world["off_main"], target="source")
    assert result.returncode == 1
    assert "not on refs/remotes/origin/main" in result.stdout
    assert set(_mirror_refs(world, "real")) == {"refs/heads/main"}


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
