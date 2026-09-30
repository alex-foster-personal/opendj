"""`scripts/runner_canary_mirror.py`: push one `main` SHA to `canary/<sha>` in a target.

Two targets (ci/runner-canary.json `target_repo` per vendor): `--target mirror`, the canary
org's mirror, for Blacksmith and Ubicloud; `--target source`, the source repository itself
through the checkout's own `origin`, for Avrea and Tenki.

Driven for real in the world tests/scripts/runner_canary_mirror_world.py builds. This file
holds the happy path, the refusals, provenance and the default-branch guards; what the
credential may reach and url rewrite rules live in test_runner_canary_mirror_isolation.py,
redaction in test_runner_canary_mirror_redaction.py.

Regression lines:
  - if a missing push token does anything but exit non-zero naming CANARY_MIRROR_PUSH_TOKEN
    then broken
  - if CANARY_MIRROR_REPO, the retired override, is silently ignored or honored then broken
    (it let a push land in a repository whose budget gate starts at zero minutes)
  - if `--target source` pushes anywhere but the checkout's `origin` when that origin IS
    the configured source repository then broken, in both directions
  - if a `remote.origin.pushurl` pointing elsewhere is not the URL checked then broken
    (git pushes to pushurl, not url, so the guard would check the wrong address)
  - if a SHA that is not on the SOURCE repository's main can be mirrored then broken: main
    is fetched fresh from origin's fetch URL, which must be the source, be one URL, and
    match no rewrite rule, so a forged or foreign local origin/main proves nothing (Codex
    P1 on c445557c4), on both targets; and if a checkout with no local origin/main cannot
    mirror a real main SHA, or the script overwrites FETCH_HEAD, then broken
  - if an EMPTY mirror is pushed to then broken: the first branch pushed becomes the
    default branch, which arms every `schedule` and `workflow_run` workflow in the mirror
  - if a mirror whose default branch is a canary branch is pushed to then broken (same)
  - if the token or its base64 form reaches the process output or git's argv then broken
  - if a valid push does not land exactly `refs/heads/canary/<sha>` at `<sha>` then broken

"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from tests.scripts.runner_canary_mirror_world import (
    CONFIG,
    TOKEN,
    World,
    bootstrap_default_branch,
    canary_refs,
    git,
    impostor_repo,
    make_world,
    refs_in,
    repo_url,
    run_mirror_script,
)

world = pytest.fixture(name="world")(make_world)


# ----- the happy path, with its own controls -----------------------------------


def test_a_main_sha_lands_exactly_at_canary_sha_and_is_idempotent(world) -> None:
    bootstrap_default_branch(world)
    sha = world["on_main"]
    first = run_mirror_script(world, sha)
    assert first.returncode == 0, first.stdout + first.stderr
    assert refs_in(world)[f"refs/heads/canary/{sha}"] == sha
    assert f"refs/heads/canary/{sha}" in first.stdout
    again = run_mirror_script(world, sha)
    assert again.returncode == 0, again.stdout + again.stderr
    # The bootstrap branch is untouched: the script pushes one ref and nothing else.
    assert set(refs_in(world)) == {"refs/heads/main", f"refs/heads/canary/{sha}"}


def test_the_token_reaches_neither_output_nor_git_argv(world) -> None:
    bootstrap_default_branch(world)
    result = run_mirror_script(world, world["on_main"], GIT_TRACE="1")
    assert result.returncode == 0, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    # Control: the trace really is on, so an absent token is a measured absence.
    assert "trace:" in combined and "push" in combined
    encoded = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
    assert TOKEN not in combined and encoded not in combined


# ----- refusals: each one leaves the mirror untouched --------------------------


def test_a_missing_token_is_refused_by_name(world) -> None:
    bootstrap_default_branch(world)
    result = run_mirror_script(world, world["on_main"], CANARY_MIRROR_PUSH_TOKEN="<unset>")
    assert result.returncode == 1
    assert "CANARY_MIRROR_PUSH_TOKEN" in result.stdout + result.stderr
    assert set(refs_in(world)) == {"refs/heads/main"}


def test_an_empty_token_is_refused_like_a_missing_one(world) -> None:
    bootstrap_default_branch(world)
    result = run_mirror_script(world, world["on_main"], CANARY_MIRROR_PUSH_TOKEN="")
    assert result.returncode == 1
    assert "CANARY_MIRROR_PUSH_TOKEN" in result.stdout + result.stderr


@pytest.mark.parametrize("sha", ["abc123", "Z" * 40, "HEAD", ""])
def test_a_malformed_sha_is_refused(world, sha: str) -> None:
    bootstrap_default_branch(world)
    result = run_mirror_script(world, sha)
    assert result.returncode != 0
    assert set(refs_in(world)) == {"refs/heads/main"}


def test_a_sha_not_on_origin_main_is_refused(world) -> None:
    bootstrap_default_branch(world)
    result = run_mirror_script(world, world["off_main"])
    assert result.returncode == 1
    assert "not on the source repository's main" in result.stdout + result.stderr
    assert set(refs_in(world)) == {"refs/heads/main"}


# ----- provenance: main is read from the source itself, never from a local ref ------


def _impostor_with_side_history(world: World) -> str:
    """An impostor repository whose main holds the off-main commit, as a fetch from it
    would leave refs/remotes/origin/main."""
    impostor_url, impostor = impostor_repo(world)
    git(
        world["source"],
        "push",
        "-q",
        f"file://{impostor}",
        f"{world['off_main']}:refs/heads/main",
        env=world["env"],
    )
    return impostor_url


def test_a_forged_local_origin_main_does_not_make_a_sha_mirrorable(world) -> None:
    """Codex P1 on c445557c4: ancestry was proved against whatever the LOCAL
    refs/remotes/origin/main referenced, which any update-ref can point anywhere."""
    bootstrap_default_branch(world)
    git(
        world["source"],
        "update-ref",
        "refs/remotes/origin/main",
        world["off_main"],
        env=world["env"],
    )
    result = run_mirror_script(world, world["off_main"])
    assert result.returncode == 1, result.stdout + result.stderr
    assert "not on the source repository's main" in result.stdout
    assert canary_refs(world["mirror"], world) == []


def test_the_mirror_target_refuses_a_checkout_whose_origin_is_not_the_source(world) -> None:
    """The mirror path never checked origin, so another repository's commits (workflow
    code included) could be pushed to the mirror and run there."""
    bootstrap_default_branch(world)
    impostor_url = _impostor_with_side_history(world)
    git(world["source"], "remote", "set-url", "origin", impostor_url, env=world["env"])
    git(
        world["source"],
        "update-ref",
        "refs/remotes/origin/main",
        world["off_main"],
        env=world["env"],
    )
    result = run_mirror_script(world, world["off_main"])
    assert result.returncode == 1, result.stdout + result.stderr
    assert "fetch URL" in result.stdout and "not the source repository" in result.stdout
    assert canary_refs(world["mirror"], world) == []


def test_the_source_target_refuses_a_fetch_url_that_is_not_the_source(world) -> None:
    """A valid source pushurl does not vouch for where origin/main was fetched from."""
    impostor_url = _impostor_with_side_history(world)
    git(world["source"], "remote", "set-url", "origin", impostor_url, env=world["env"])
    source_url = repo_url(world, CONFIG["source_repository"])
    git(world["source"], "remote", "set-url", "--push", "origin", source_url, env=world["env"])
    git(
        world["source"],
        "update-ref",
        "refs/remotes/origin/main",
        world["off_main"],
        env=world["env"],
    )
    result = run_mirror_script(world, world["off_main"], target="source")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "fetch URL" in result.stdout and "not the source repository" in result.stdout
    assert set(refs_in(world, "real")) == {"refs/heads/main"}


def test_a_rewrite_of_the_origin_fetch_url_is_refused(world) -> None:
    """The fetch URL is a contacted URL too: a rewrite would fetch main from elsewhere."""
    bootstrap_default_branch(world)
    impostor_url = _impostor_with_side_history(world)
    gitconfig = Path(world["env"]["GIT_CONFIG_GLOBAL"])
    source_url = repo_url(world, CONFIG["source_repository"])
    gitconfig.write_text(
        gitconfig.read_text() + f'[url "{impostor_url}"]\n\tinsteadOf = {source_url}\n'
    )
    result = run_mirror_script(world, world["off_main"])
    assert result.returncode == 1, result.stdout + result.stderr
    assert "would rewrite" in result.stdout
    assert canary_refs(world["mirror"], world) == []


def test_more_than_one_origin_fetch_url_is_refused(world) -> None:
    bootstrap_default_branch(world)
    source_url = repo_url(world, CONFIG["source_repository"])
    git(world["source"], "config", "--add", "remote.origin.url", source_url, env=world["env"])
    result = run_mirror_script(world, world["on_main"])
    assert result.returncode == 1, result.stdout + result.stderr
    assert "fetch URLs" in result.stdout


def test_no_local_origin_main_is_needed_because_main_is_fetched_fresh(world) -> None:
    """Opposite direction: provenance comes from the source, so a checkout with no (or a
    stale) local origin/main still mirrors a real main SHA, and FETCH_HEAD is left alone."""
    bootstrap_default_branch(world)
    git(world["source"], "update-ref", "-d", "refs/remotes/origin/main", env=world["env"])
    fetch_head = Path(str(world["source"])) / ".git" / "FETCH_HEAD"
    fetch_head.write_text("another agent's fetch\n")
    result = run_mirror_script(world, world["on_main"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert canary_refs(world["mirror"], world) == [f"refs/heads/canary/{world['on_main']}"]
    assert fetch_head.read_text() == "another agent's fetch\n"


def test_the_retired_mirror_override_is_refused_by_name_not_ignored(world) -> None:
    """Codex P1 on #4472: an override let a push land where the budget gate read zero."""
    bootstrap_default_branch(world)
    result = run_mirror_script(
        world, world["on_main"], CANARY_MIRROR_REPO=CONFIG["mirror_repository"]
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "CANARY_MIRROR_REPO is no longer honored" in result.stdout
    assert set(refs_in(world)) == {"refs/heads/main"}


def test_the_target_must_be_named(world) -> None:
    bootstrap_default_branch(world)
    result = run_mirror_script(world, world["on_main"], target=None)
    assert result.returncode == 2
    assert "--target" in result.stderr
    assert set(refs_in(world)) == {"refs/heads/main"}


# ----- --target source: the source repository, through the checkout's origin --------


def test_source_target_lands_canary_sha_through_origin_without_the_mirror_token(world) -> None:
    sha = world["on_main"]
    result = run_mirror_script(world, sha, target="source", CANARY_MIRROR_PUSH_TOKEN="<unset>")
    assert result.returncode == 0, result.stdout + result.stderr
    assert refs_in(world, "real")[f"refs/heads/canary/{sha}"] == sha
    assert set(refs_in(world, "real")) == {"refs/heads/main", f"refs/heads/canary/{sha}"}
    # Control: the mirror, the other target, was not touched.
    assert refs_in(world) == {}


def test_source_target_refuses_an_origin_that_is_not_the_source_repository(world) -> None:
    impostor_url, impostor = impostor_repo(world)
    git(world["source"], "remote", "set-url", "origin", impostor_url, env=world["env"])
    result = run_mirror_script(world, world["on_main"], target="source")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "origin's" in result.stdout and "not the source repository" in result.stdout
    assert git(impostor, "for-each-ref", env=world["env"]) == ""
    assert set(refs_in(world, "real")) == {"refs/heads/main"}


@pytest.mark.parametrize("spelling", [0, 1], ids=["with-dot-git", "without-dot-git"])
def test_source_target_accepts_every_spelling_of_the_source_origin(world, spelling: int) -> None:
    """Opposite direction: the guard must not refuse the real repository spelled otherwise."""
    spelled = repo_url(world, CONFIG["source_repository"], spelling)
    git(world["source"], "remote", "set-url", "origin", spelled, env=world["env"])
    result = run_mirror_script(world, world["on_main"], target="source")
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"refs/heads/canary/{world['on_main']}" in refs_in(world, "real")


def test_source_target_checks_the_push_url_git_will_actually_use(world) -> None:
    """Codex P1 on f0f50fe68: `url` is the source, but `pushurl` sends the push elsewhere."""
    impostor_url, impostor = impostor_repo(world)
    git(world["source"], "remote", "set-url", "--push", "origin", impostor_url, env=world["env"])
    result = run_mirror_script(world, world["on_main"], target="source")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "not the source repository" in result.stdout and "someone-else" in result.stdout
    assert git(impostor, "for-each-ref", env=world["env"]) == ""
    assert set(refs_in(world, "real")) == {"refs/heads/main"}


def test_source_target_refuses_more_than_one_push_url(world) -> None:
    impostor_url, impostor = impostor_repo(world)
    source_url = repo_url(world, CONFIG["source_repository"])
    git(
        world["source"],
        "remote",
        "set-url",
        "--add",
        "--push",
        "origin",
        source_url,
        env=world["env"],
    )
    git(
        world["source"],
        "remote",
        "set-url",
        "--add",
        "--push",
        "origin",
        impostor_url,
        env=world["env"],
    )
    result = run_mirror_script(world, world["on_main"], target="source")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "push URLs" in result.stdout
    assert git(impostor, "for-each-ref", env=world["env"]) == ""


def test_source_target_honors_a_push_url_that_is_the_source_repository(world) -> None:
    """Opposite direction: a pushurl spelled differently for the SAME repository is fine."""
    spelled = repo_url(world, CONFIG["source_repository"], 1)
    git(world["source"], "remote", "set-url", "--push", "origin", spelled, env=world["env"])
    result = run_mirror_script(world, world["on_main"], target="source")
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"refs/heads/canary/{world['on_main']}" in refs_in(world, "real")


def test_source_target_refuses_a_sha_not_on_main(world) -> None:
    result = run_mirror_script(world, world["off_main"], target="source")
    assert result.returncode == 1
    assert "not on the source repository's main" in result.stdout
    assert set(refs_in(world, "real")) == {"refs/heads/main"}


def test_an_empty_mirror_is_refused_because_the_first_push_becomes_the_default(world) -> None:
    result = run_mirror_script(world, world["on_main"])
    assert result.returncode == 1
    assert "default branch" in result.stdout + result.stderr
    assert refs_in(world) == {}


def test_a_mirror_whose_default_is_a_canary_branch_is_refused(world) -> None:
    bootstrap_default_branch(world, branch="canary/0000000000000000000000000000000000000000")
    result = run_mirror_script(world, world["on_main"])
    assert result.returncode == 1
    assert "default branch" in result.stdout + result.stderr
    assert len(refs_in(world)) == 1
