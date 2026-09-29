"""`scripts/runner_canary_mirror.py`: push one `main` SHA to `canary/<sha>` in a target.

Two targets (ci/runner-canary.json `target_repo` per vendor): `--target mirror`, the canary
org's mirror, for Blacksmith and Ubicloud; `--target source`, the source repository itself
through the checkout's own `origin`, for Avrea and Tenki.

Driven for real: a source checkout and two bare repositories (the mirror, and the source
repository's "remote") are built in `tmp_path`, laid out as `<remotes>/<owner>/<name>.git`,
and the script runs against a copy of ci/runner-canary.json whose `remote_url_templates` are
`file://` spellings of that layout. No url rewrite rule is involved (the script refuses
those), so the script's own URL construction, auth header, preflight and push all run
unmodified. Nothing touches GitHub.

Regression lines:
  - if a missing push token does anything but exit non-zero naming CANARY_MIRROR_PUSH_TOKEN
    then broken
  - if CANARY_MIRROR_REPO, the retired override, is silently ignored or honored then broken
    (it let a push land in a repository whose budget gate starts at zero minutes)
  - if `--target source` pushes anywhere but the checkout's `origin` when that origin IS
    the configured source repository then broken, in both directions
  - if a `remote.origin.pushurl` pointing elsewhere is not the URL checked then broken
    (git pushes to pushurl, not url, so the guard would check the wrong address)
  - if a git url rewrite rule (`insteadOf` or `pushInsteadOf`, any config scope) that
    matches the URL about to be contacted does not refuse the run, on either target, then
    broken (git would contact the rewritten address, not the checked one); and if a rule
    that does NOT match refuses it then broken (opposite direction)
  - if the mirror credential, raw or as its git auth header, reaches ANY git child of a
    `--target source` push (and so its hooks and credential helpers) then broken; and if
    the mirror push itself stops carrying the auth header then broken (opposite direction)
  - if a credentialed git call (the mirror's ls-remote or push) starts a checkout hook or
    another configured program (a signing program, say) then broken, since it would inherit
    the auth header; and if the source target's own push stops running the checkout's
    hooks then broken (opposite direction)
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
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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


def _url(world: dict[str, object], repo: str, spelling: int = 0) -> str:
    return world["config"]["remote_url_templates"][spelling].format(repo=repo)


def _bare(world: dict[str, object], repo: str) -> Path:
    """A writable bare repository at `repo`'s place in the remotes layout."""
    bare = Path(str(world["remotes"])) / f"{repo}.git"
    bare.parent.mkdir(parents=True, exist_ok=True)
    _git(bare.parent, "init", "-q", "--bare", str(bare), env=world["env"])
    return bare


@pytest.fixture()
def world(tmp_path: Path) -> dict[str, object]:
    gitconfig = tmp_path / "gitconfig"
    remotes = tmp_path / "remotes"
    config = {
        **CONFIG,
        "remote_url_templates": [f"file://{remotes}/{{repo}}.git", f"file://{remotes}/{{repo}}"],
    }
    config_path = tmp_path / "runner-canary.json"
    config_path.write_text(json.dumps(config))
    base_env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "GIT_CONFIG_GLOBAL": str(gitconfig),
        "GIT_CONFIG_NOSYSTEM": "1",
        **GIT_IDENTITY,
    }
    gitconfig.write_text("[init]\n\tdefaultBranch = main\n")
    world = {"env": base_env, "remotes": remotes, "config": config, "config_path": config_path}
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
    mirror = _bare(world, CONFIG["mirror_repository"])
    # The source repository's own remote: a bare repository whose default branch is main,
    # reached through the checkout's `origin` at its configured URL.
    real = _bare(world, CONFIG["source_repository"])
    _git(source, "push", "-q", f"file://{real}", f"{on_main}:refs/heads/main", env=base_env)
    source_url = _url(world, CONFIG["source_repository"])
    _git(source, "remote", "add", "origin", source_url, env=base_env)
    return {
        **world,
        "source": source,
        "mirror": mirror,
        "real": real,
        "on_main": on_main,
        "off_main": off_main,
    }


def _bootstrap_default_branch(
    world: dict[str, object], branch: str = "main", bare: Path | None = None
) -> None:
    """What the ADR's one-time bootstrap does: a workflow-free default branch."""
    env = world["env"]
    bare = bare or Path(str(world["mirror"]))
    scratch = Path(str(bare) + "-boot")
    _git(scratch.parent, "init", "-q", str(scratch), env=env)
    (scratch / "README.md").write_text("canary mirror\n")
    _git(scratch, "add", "README.md", env=env)
    _git(scratch, "commit", "-q", "-m", "boot", env=env)
    _git(scratch, "push", "-q", f"file://{bare}", f"HEAD:refs/heads/{branch}", env=env)
    _git(bare, "symbolic-ref", "HEAD", f"refs/heads/{branch}", env=env)


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
            str(world["config_path"]),
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
    impostor_url, impostor = _impostor(world)
    _git(world["source"], "remote", "set-url", "origin", impostor_url, env=world["env"])
    result = _run(world, world["on_main"], target="source")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "origin is" in result.stdout and "not the source repository" in result.stdout
    assert _git(impostor, "for-each-ref", env=world["env"]) == ""
    assert set(_mirror_refs(world, "real")) == {"refs/heads/main"}


@pytest.mark.parametrize("spelling", [0, 1], ids=["with-dot-git", "without-dot-git"])
def test_source_target_accepts_every_spelling_of_the_source_origin(world, spelling: int) -> None:
    """Opposite direction: the guard must not refuse the real repository spelled otherwise."""
    spelled = _url(world, CONFIG["source_repository"], spelling)
    _git(world["source"], "remote", "set-url", "origin", spelled, env=world["env"])
    result = _run(world, world["on_main"], target="source")
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"refs/heads/canary/{world['on_main']}" in _mirror_refs(world, "real")


def _impostor(world: dict[str, object]) -> tuple[str, Path]:
    """A writable bare repository at another repository's URL: a push there WOULD land."""
    other = "someone-else/music-dj-tools"
    return _url(world, other), _bare(world, other)


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
    source_url = _url(world, CONFIG["source_repository"])
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
    """Opposite direction: a pushurl spelled differently for the SAME repository is fine."""
    spelled = _url(world, CONFIG["source_repository"], 1)
    _git(world["source"], "remote", "set-url", "--push", "origin", spelled, env=world["env"])
    result = _run(world, world["on_main"], target="source")
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"refs/heads/canary/{world['on_main']}" in _mirror_refs(world, "real")


# ----- url rewrite rules: the checked URL must be the URL git contacts ---------------


def _canary_refs(bare: Path, world: dict[str, object]) -> list[str]:
    out = _git(bare, "for-each-ref", "--format=%(refname)", "refs/heads/canary/", env=world["env"])
    return out.splitlines()


def test_source_target_refuses_a_push_rewrite_of_the_checked_url(world) -> None:
    """Codex P1 on d18006414: `remote.origin.url` IS the source, but a local-scope
    `pushInsteadOf` makes git push that very URL to another repository."""
    impostor_url, impostor = _impostor(world)
    source_url = _url(world, CONFIG["source_repository"])
    _git(
        world["source"],
        "config",
        f"url.{impostor_url}.pushInsteadOf",
        source_url,
        env=world["env"],
    )
    # Control: git itself resolves the push elsewhere, so a missing guard WOULD land there.
    resolved = _git(world["source"], "remote", "get-url", "--push", "origin", env=world["env"])
    assert resolved == impostor_url
    result = _run(world, world["on_main"], target="source")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "would rewrite" in result.stdout and "pushinsteadof" in result.stdout
    assert _git(impostor, "for-each-ref", env=world["env"]) == ""
    assert set(_mirror_refs(world, "real")) == {"refs/heads/main"}


def test_mirror_target_refuses_a_rewrite_of_the_mirror_url(world) -> None:
    """Same class on the other target: a global-scope `insteadOf` would send the mirror's
    ls-remote and push, auth header included, to another repository."""
    _bootstrap_default_branch(world)
    impostor_url, impostor = _impostor(world)
    # A default branch, so without the guard the default-branch check passes and the push
    # WOULD land in the impostor rather than being refused for another reason.
    _bootstrap_default_branch(world, bare=impostor)
    gitconfig = Path(world["env"]["GIT_CONFIG_GLOBAL"])
    mirror_url = _url(world, CONFIG["mirror_repository"])
    gitconfig.write_text(
        gitconfig.read_text() + f'[url "{impostor_url}"]\n\tinsteadOf = {mirror_url}\n'
    )
    result = _run(world, world["on_main"])
    assert result.returncode == 1, result.stdout + result.stderr
    assert "would rewrite" in result.stdout and ".insteadof" in result.stdout
    assert _canary_refs(impostor, world) == []
    assert _canary_refs(Path(str(world["mirror"])), world) == []


def test_a_rewrite_rule_from_the_environment_scope_is_seen_too(world) -> None:
    """Every scope git reads, not only config files: GIT_CONFIG_* in the environment."""
    impostor_url, impostor = _impostor(world)
    result = _run(
        world,
        world["on_main"],
        target="source",
        GIT_CONFIG_COUNT="1",
        GIT_CONFIG_KEY_0=f"url.{impostor_url}.pushInsteadOf",
        GIT_CONFIG_VALUE_0=_url(world, CONFIG["source_repository"]),
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "would rewrite" in result.stdout
    assert _git(impostor, "for-each-ref", env=world["env"]) == ""


def test_unreadable_rewrite_rules_are_refused_not_read_as_none(world) -> None:
    """A config git cannot parse must not read as "no rewrite rules"."""
    _bootstrap_default_branch(world)
    gitconfig = Path(world["env"]["GIT_CONFIG_GLOBAL"])
    readable = gitconfig.read_text()
    gitconfig.write_text(readable + "[url\n")
    result = _run(world, world["on_main"])
    gitconfig.write_text(readable)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "cannot read git's url rewrite rules" in result.stdout
    assert _canary_refs(Path(str(world["mirror"])), world) == []


@pytest.mark.parametrize("target", ["mirror", "source"])
def test_rewrite_rules_that_do_not_match_the_url_do_not_refuse(world, target: str) -> None:
    """Opposite direction: only a rule git would APPLY refuses. A rule for another host, and
    a near miss whose value merely EXTENDS the URL (a prefix check run backwards would
    match it), leave the push alone."""
    _bootstrap_default_branch(world)
    impostor_url, impostor = _impostor(world)
    repo = CONFIG["mirror_repository"] if target == "mirror" else CONFIG["source_repository"]
    gitconfig = Path(world["env"]["GIT_CONFIG_GLOBAL"])
    gitconfig.write_text(
        gitconfig.read_text()
        + f'[url "{impostor_url}"]\n'
        + "\tinsteadOf = https://git.example.invalid/\n"
        + f"\tpushInsteadOf = {_url(world, repo)}-other\n"
    )
    result = _run(world, world["on_main"], target=target)
    assert result.returncode == 0, result.stdout + result.stderr
    landed = Path(str(world["mirror"] if target == "mirror" else world["real"]))
    assert _canary_refs(landed, world) == [f"refs/heads/canary/{world['on_main']}"]
    assert _git(impostor, "for-each-ref", env=world["env"]) == ""


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


# ----- credentialed calls start no program the checkout or git config names -----------


def _logging_program(path: Path, log: Path, exit_code: int = 0) -> Path:
    """An executable that records its name and full environment, then exits `exit_code`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'#!/bin/sh\n{{ echo "=== {path.name}"; env; }} >> "{log}"\nexit {exit_code}\n')
    path.chmod(0o755)
    return path


def _read(log: Path) -> str:
    return log.read_text() if log.exists() else ""


def test_a_mirror_push_runs_no_checkout_hook_so_none_sees_the_credential(world) -> None:
    """Codex P1 on b9db90801: the mirror push ran the checkout's pre-push hook, whose
    environment held the auth header (the token in base64)."""
    _bootstrap_default_branch(world)
    log = Path(str(world["source"]) + "-hooks.log")
    _logging_program(Path(str(world["source"])) / ".git" / "hooks" / "pre-push", log)
    # Control: the hook is live, and an ordinary push from this checkout runs it.
    scratch = _bare(world, "scratch/hook-control")
    _git(world["source"], "push", "-q", f"file://{scratch}", "HEAD:refs/heads/x", env=world["env"])
    assert "=== pre-push" in _read(log)
    log.unlink()
    result = _run(world, world["on_main"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert _canary_refs(Path(str(world["mirror"])), world) == [
        f"refs/heads/canary/{world['on_main']}"
    ]
    assert TOKEN not in _read(log) and ENCODED not in _read(log)
    assert _read(log) == "", "no hook runs during a credentialed call"


def test_a_mirror_push_starts_no_signing_program_the_checkout_configures(world) -> None:
    """Same class, another program: `push.gpgSign` in the checkout's config makes git start
    `gpg.program` for a receiver that takes push certificates, inside the credentialed push."""
    _bootstrap_default_branch(world)
    log = Path(str(world["source"]) + "-gpg.log")
    fake_gpg = _logging_program(Path(str(world["source"]) + "-bin") / "gpg", log, exit_code=1)
    _git(Path(str(world["mirror"])), "config", "receive.certNonceSeed", "seed", env=world["env"])
    for key, value in (
        ("push.gpgSign", "true"),
        ("gpg.program", str(fake_gpg)),
        ("user.signingKey", "k"),
    ):
        _git(world["source"], "config", key, value, env=world["env"])
    # Control: git does start the program for a push from this checkout.
    scratch = _bare(world, "scratch/gpg-control")
    scratch_config = ["config", "receive.certNonceSeed", "seed"]
    _git(scratch, *scratch_config, env=world["env"])
    subprocess.run(
        ["git", "push", "-q", f"file://{scratch}", "HEAD:refs/heads/x"],
        cwd=world["source"],
        env=world["env"],
        capture_output=True,
        check=False,
    )
    assert "=== gpg" in _read(log)
    log.unlink()
    result = _run(world, world["on_main"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert _read(log) == ""


class _AskForCredentials(BaseHTTPRequestHandler):
    """Answers every request 401 with a Basic challenge, the reply that makes git ask its
    credential helpers and askpass programs for a username and password."""

    def do_GET(self) -> None:
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="canary"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args: object) -> None:
        return


@pytest.fixture()
def challenging_server() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _AskForCredentials)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/"
    finally:
        server.shutdown()
        server.server_close()


def test_a_credentialed_call_starts_no_credential_helper_or_askpass(
    world, challenging_server: str
) -> None:
    """Same class: a credential helper or askpass program named in git config, or in
    GIT_ASKPASS, would be started inside the mirror's ls-remote and inherit the header."""
    log = Path(str(world["source"]) + "-cred.log")
    bin_dir = Path(str(world["source"]) + "-bin")
    helper = _logging_program(bin_dir / "git-credential-canary-probe", log, exit_code=1)
    askpass = _logging_program(bin_dir / "askpass-config", log, exit_code=1)
    env_askpass = _logging_program(bin_dir / "askpass-env", log, exit_code=1)
    gitconfig = Path(world["env"]["GIT_CONFIG_GLOBAL"])
    gitconfig.write_text(
        gitconfig.read_text()
        + f"[credential]\n\thelper = {helper}\n[core]\n\taskPass = {askpass}\n"
    )
    config = {**world["config"], "remote_url_templates": [f"{challenging_server}{{repo}}.git"]}
    Path(str(world["config_path"])).write_text(json.dumps(config))
    probe_env = {**world["env"], "GIT_ASKPASS": str(env_askpass)}
    # Control: plain git, challenged at that URL, does start the helper and askpass.
    subprocess.run(
        ["git", "ls-remote", f"{challenging_server}control.git"],
        cwd=world["source"],
        env=probe_env,
        capture_output=True,
        check=False,
        timeout=60,
    )
    assert "=== git-credential-canary-probe" in _read(log) and "=== askpass-env" in _read(log)
    log.unlink()
    result = _run(world, world["on_main"], GIT_ASKPASS=str(env_askpass))
    assert result.returncode == 1, result.stdout + result.stderr
    assert "cannot read" in result.stdout  # the challenge refused it, and nothing answered
    assert _read(log) == "", "no helper or askpass ran during a credentialed call"


def test_the_source_push_keeps_the_checkouts_own_hooks(world) -> None:
    """Opposite direction: the switches ride only on credentialed calls. The source target
    pushes with git's own credentials, and the checkout's hooks still run for it."""
    log = Path(str(world["source"]) + "-hooks.log")
    _logging_program(Path(str(world["source"])) / ".git" / "hooks" / "pre-push", log)
    result = _run(world, world["on_main"], target="source")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "=== pre-push" in _read(log)
    assert TOKEN not in _read(log) and ENCODED not in _read(log)


def test_the_committed_mirror_url_is_https_so_no_ssh_program_runs() -> None:
    """The credentialed calls switch off hooks, helpers, askpass, fsmonitor, gpg and ext::,
    but not ssh: that holds only while the URL the mirror push builds is https."""
    assert CONFIG["remote_url_templates"][0].startswith("https://")


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
