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
  - if a `remote.origin.pushurl` pointing elsewhere is not the URL checked then broken
    (git pushes to pushurl, not url, so the guard would check the wrong address)
  - if the mirror credential, raw or as its git auth header, reaches ANY git child of a
    `--target source` push (and so its hooks and credential helpers) then broken; and if
    the mirror push itself stops carrying the auth header then broken (opposite direction)
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
import shutil
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


def _impostor(world: dict[str, object]) -> tuple[str, Path]:
    """A writable bare repository at another GitHub URL: a push there WOULD land."""
    other = "someone-else/music-dj-tools"
    bare = Path(str(world["real"]) + "-impostor")
    _git(bare.parent, "init", "-q", "--bare", str(bare), env=world["env"])
    gitconfig = Path(world["env"]["GIT_CONFIG_GLOBAL"])
    gitconfig.write_text(
        gitconfig.read_text()
        + f'[url "file://{bare}"]\n\tinsteadOf = https://github.com/{other}.git\n'
    )
    return f"https://github.com/{other}.git", bare


def test_source_target_checks_the_push_url_git_will_actually_use(world) -> None:
    """Codex P1 on f0f50fe68: `url` is the source, but `pushurl` sends the push elsewhere."""
    impostor_url, impostor = _impostor(world)
    _git(world["source"], "remote", "set-url", "--push", "origin", impostor_url, env=world["env"])
    result = _run(world, world["on_main"], target="source")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "not the source repository" in result.stdout and "someone-else" in result.stdout
    assert _git(impostor, "for-each-ref", env=world["env"]) == ""
    assert set(_mirror_refs(world, "real")) == {"refs/heads/main"}


def test_source_target_refuses_more_than_one_push_url(world) -> None:
    impostor_url, impostor = _impostor(world)
    source_url = f"https://github.com/{CONFIG['source_repository']}.git"
    _git(
        world["source"],
        "remote",
        "set-url",
        "--add",
        "--push",
        "origin",
        source_url,
        env=world["env"],
    )
    _git(
        world["source"],
        "remote",
        "set-url",
        "--add",
        "--push",
        "origin",
        impostor_url,
        env=world["env"],
    )
    result = _run(world, world["on_main"], target="source")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "push URLs" in result.stdout
    assert _git(impostor, "for-each-ref", env=world["env"]) == ""


def test_source_target_honors_a_push_url_that_is_the_source_repository(world) -> None:
    """Opposite direction: a pushurl spelled over ssh for the SAME repository is fine."""
    spelled = f"git@github.com:{CONFIG['source_repository']}.git"
    gitconfig = Path(world["env"]["GIT_CONFIG_GLOBAL"])
    gitconfig.write_text(
        gitconfig.read_text() + f'[url "file://{world["real"]}"]\n\tinsteadOf = {spelled}\n'
    )
    _git(world["source"], "remote", "set-url", "--push", "origin", spelled, env=world["env"])
    result = _run(world, world["on_main"], target="source")
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"refs/heads/canary/{world['on_main']}" in _mirror_refs(world, "real")


def _log_every_git_child(world: dict[str, object]) -> Path:
    """Put a `git` on PATH that records each invocation's argv and environment, then runs
    the real git. Hooks and credential helpers inherit exactly this environment."""
    log = Path(str(world["mirror"]) + "-git-env.log")
    shim_dir = Path(str(world["mirror"]) + "-bin")
    shim_dir.mkdir()
    real_git = shutil.which("git")
    assert real_git, "control: a real git is on PATH"
    shim = shim_dir / "git"
    shim.write_text(
        f'#!/bin/sh\n{{ printf "=== %s\\n" "$*"; env; }} >> "{log}"\nexec "{real_git}" "$@"\n'
    )
    shim.chmod(0o755)
    world["env"]["PATH"] = f"{shim_dir}{os.pathsep}{world['env']['PATH']}"
    return log


def _invocations(log: Path) -> list[tuple[str, str]]:
    """(argv, environment) for every git child the script started."""
    chunks = log.read_text().split("=== ")[1:]
    return [(chunk.split("\n", 1)[0], chunk.split("\n", 1)[1]) for chunk in chunks]


ENCODED = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()


def test_source_target_children_never_see_the_mirror_credential(world) -> None:
    """Sol P1 on d18006414: the mirror token was exported into every git child (and so its
    hooks and credential helpers) of a push to the SOURCE repository."""
    log = _log_every_git_child(world)
    result = _run(world, world["on_main"], target="source")  # the token IS exported
    assert result.returncode == 0, result.stdout + result.stderr
    calls = _invocations(log)
    # Controls: every spawn site ran through the shim, the push included.
    argvs = [argv for argv, _ in calls]
    assert any(a.startswith("config ") for a in argvs), argvs
    assert any(a.startswith("merge-base ") for a in argvs), argvs
    assert any(a.startswith("push ") for a in argvs), argvs
    for argv, env in calls:
        assert "CANARY_MIRROR_PUSH_TOKEN" not in env, argv
        assert TOKEN not in env and ENCODED not in env, argv


def test_mirror_push_still_carries_the_auth_header_and_nothing_else_does(world) -> None:
    """Opposite direction: stripping the credential must not reach the mirror's own
    network calls, and the raw variable reaches no child on either target."""
    _bootstrap_default_branch(world)
    log = _log_every_git_child(world)
    result = _run(world, world["on_main"])
    assert result.returncode == 0, result.stdout + result.stderr
    calls = _invocations(log)
    network = [(a, e) for a, e in calls if a.startswith(("push ", "ls-remote "))]
    local = [(a, e) for a, e in calls if not a.startswith(("push ", "ls-remote "))]
    assert len(network) == 2 and local, [a for a, _ in calls]
    for argv, env in network:
        assert ENCODED in env, argv
    for argv, env in local:
        assert ENCODED not in env, argv
    for argv, env in calls:
        assert "CANARY_MIRROR_PUSH_TOKEN" not in env, argv


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
