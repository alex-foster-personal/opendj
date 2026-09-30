"""`scripts/runner_canary_mirror.py`: redaction. Nothing printed or raised carries the
mirror credential, and the operator still sees WHERE.

Driven for real in the world tests/scripts/runner_canary_mirror_world.py builds.

Regression lines:
  - if URL userinfo, an Authorization value, or the token or its base64 reaches stdout,
    stderr or a refusal's text (the origin URL, a rewrite rule, git's own echoed output,
    a timed-out argv) then broken; and if redaction hides the host and path too then
    broken (opposite direction: the operator must still see WHERE)
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from scripts import runner_canary_mirror as mirror_script
from tests.scripts.runner_canary_mirror_world import (
    CONFIG,
    ENCODED,
    TOKEN,
    World,
    bootstrap_default_branch,
    canary_refs,
    git,
    make_world,
    repo_url,
    run_mirror_script,
)

world = pytest.fixture(name="world")(make_world)


# ----- redaction: nothing printed or raised carries a credential ----------------------


def _assert_no_secret(text: str) -> None:
    assert TOKEN not in text and ENCODED not in text, text


def test_a_refused_origin_url_is_printed_without_its_credential(world) -> None:
    """Sol P1 on c445557c4: refusals printed URLs verbatim, userinfo included."""
    leaky = f"https://x-access-token:{TOKEN}@github.com/someone-else/music-dj-tools.git"
    git(world["source"], "remote", "set-url", "origin", leaky, env=world["env"])
    result = run_mirror_script(world, world["on_main"], target="source")
    assert result.returncode == 1, result.stdout + result.stderr
    _assert_no_secret(result.stdout + result.stderr)
    # Control: WHERE is still shown, with only the userinfo replaced.
    assert "https://<redacted>@github.com/someone-else/music-dj-tools.git" in result.stdout


def test_a_refused_rewrite_rule_is_printed_without_its_credential(world) -> None:
    """The exact site: the matching rule's key is a URL, and may carry userinfo."""
    bootstrap_default_branch(world)
    gitconfig = Path(world["env"]["GIT_CONFIG_GLOBAL"])
    mirror_url = repo_url(world, CONFIG["mirror_repository"])
    rule = f"https://x-access-token:{TOKEN}@github.com/evil/x.git"
    gitconfig.write_text(gitconfig.read_text() + f'[url "{rule}"]\n\tinsteadOf = {mirror_url}\n')
    result = run_mirror_script(world, world["on_main"])
    assert result.returncode == 1, result.stdout + result.stderr
    assert "would rewrite" in result.stdout
    _assert_no_secret(result.stdout + result.stderr)
    assert "<redacted>@github.com/evil/x.git" in result.stdout


def _echoing_git(world: World, fail: str) -> None:
    """A `git` on PATH that, for ls-remote and push, first echoes the URL it was given and
    its auth header to stderr, as a verbose or traced git does, then exits 128 for `fail`
    and runs the real git otherwise. Real git anonymizes userinfo in its own messages, so
    this is how a test gets git's stderr to carry the secret."""
    shim_dir = Path(str(world["mirror"]) + "-echo-bin")
    shim_dir.mkdir()
    real_git = shutil.which("git")
    assert real_git
    shim = shim_dir / "git"
    shim.write_text(
        "#!/bin/sh\n"
        # The subcommand is the first word after the leading `-c key=value` pairs.
        'sub=""; skip=0\n'
        'for a in "$@"; do\n'
        '  if [ "$skip" = 1 ]; then skip=0; continue; fi\n'
        '  case "$a" in -c) skip=1;; *) sub="$a"; break;; esac\n'
        "done\n"
        'case "$sub" in ls-remote|push)\n'
        '  for a in "$@"; do case "$a" in *://*) url="$a";; esac; done\n'
        "  echo \"fatal: unable to access '$url': sent $GIT_CONFIG_VALUE_0\" >&2\n"
        f'  [ "$sub" = "{fail}" ] && exit 128;;\n'
        "esac\n"
        f'exec "{real_git}" "$@"\n'
    )
    shim.chmod(0o755)
    world["env"]["PATH"] = f"{shim_dir}{os.pathsep}{world['env']['PATH']}"


def _token_in_mirror_url(world: World) -> str:
    """Point the mirror template at the same bare repositories, with the token as userinfo."""
    remotes = world["remotes"]
    template = f"file://x-access-token:{TOKEN}@localhost{remotes}/{{repo}}.git"
    config = {
        **world["config"],
        "remote_url_templates": [template, *world["config"]["remote_url_templates"]],
    }
    world["config_path"].write_text(json.dumps(config))
    return f"localhost{remotes}/{CONFIG['mirror_repository']}.git"


def test_a_failing_git_whose_stderr_echoes_the_url_and_header_leaks_neither(world) -> None:
    bootstrap_default_branch(world)
    where = _token_in_mirror_url(world)
    _echoing_git(world, fail="ls-remote")
    result = run_mirror_script(world, world["on_main"])
    assert result.returncode == 1, result.stdout + result.stderr
    assert "cannot read" in result.stdout and "unable to access" in result.stdout
    _assert_no_secret(result.stdout + result.stderr)
    assert f"<redacted>@{where}" in result.stdout
    assert "AUTHORIZATION: basic <redacted>" in result.stdout


def test_git_push_output_is_printed_through_the_redactor_and_the_push_lands(world) -> None:
    bootstrap_default_branch(world)
    where = _token_in_mirror_url(world)
    _echoing_git(world, fail="none")
    result = run_mirror_script(world, world["on_main"])
    assert result.returncode == 0, result.stdout + result.stderr
    _assert_no_secret(result.stdout + result.stderr)
    assert f"<redacted>@{where}" in result.stdout  # git's own output is still shown
    assert canary_refs(world["mirror"], world) == [f"refs/heads/canary/{world['on_main']}"]


def test_a_refusal_is_redacted_when_raised_whatever_its_text() -> None:
    refusal = mirror_script.MirrorRefused(
        f"url https://x-access-token:{TOKEN}@github.com/a/b.git header AUTHORIZATION: basic "
        f"{ENCODED} path https://github.com/a/b@v1"
    )
    _assert_no_secret(str(refusal))
    assert "https://<redacted>@github.com/a/b.git" in str(refusal)
    assert "https://github.com/a/b@v1" in str(refusal)  # an @ in a path is not userinfo


def test_a_timed_out_git_call_raises_a_redacted_refusal_not_its_argv(
    monkeypatch, tmp_path: Path
) -> None:
    """subprocess.TimeoutExpired's text is the whole argv, URL userinfo included.

    The hang is a `git` on PATH that sleeps, so the timeout is real and certain; an
    unroutable address hung on one network and failed fast on another."""
    hanging = tmp_path / "bin"
    hanging.mkdir()
    shim = hanging / "git"
    shim.write_text("#!/bin/sh\nexec sleep 30\n")
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{hanging}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(mirror_script, "GIT_TIMEOUT_S", 0.5)
    with pytest.raises(mirror_script.MirrorRefused) as refused:
        mirror_script._git(["ls-remote", f"https://x-access-token:{TOKEN}@10.255.255.1/x.git"])
    _assert_no_secret(str(refused.value))
    assert "timed out" in str(refused.value) and "<redacted>@10.255.255.1/x.git" in str(
        refused.value
    )
    assert refused.value.__cause__ is None and refused.value.__suppress_context__
