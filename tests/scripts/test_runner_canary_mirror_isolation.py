"""`scripts/runner_canary_mirror.py`: isolation. What the mirror credential may reach, which
URL git actually contacts, and what may be printed or raised.

Driven for real in the world tests/scripts/runner_canary_mirror_world.py builds; the happy
path, refusals and provenance live in tests/scripts/test_runner_canary_mirror.py.

Regression lines:
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
  - if URL userinfo, an Authorization value, or the token or its base64 reaches stdout,
    stderr or a refusal's text (the origin URL, a rewrite rule, git's own echoed output,
    a timed-out argv) then broken; and if redaction hides the host and path too then
    broken (opposite direction: the operator must still see WHERE)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from scripts import runner_canary_mirror as mirror_script
from tests.scripts.runner_canary_mirror_world import (
    CONFIG,
    ENCODED,
    TOKEN,
    World,
    bare_repo,
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


# ----- url rewrite rules: the checked URL must be the URL git contacts ---------------


def test_source_target_refuses_a_push_rewrite_of_the_checked_url(world) -> None:
    """Codex P1 on d18006414: `remote.origin.url` IS the source, but a local-scope
    `pushInsteadOf` makes git push that very URL to another repository."""
    impostor_url, impostor = impostor_repo(world)
    source_url = repo_url(world, CONFIG["source_repository"])
    git(
        world["source"],
        "config",
        f"url.{impostor_url}.pushInsteadOf",
        source_url,
        env=world["env"],
    )
    # Control: git itself resolves the push elsewhere, so a missing guard WOULD land there.
    resolved = git(world["source"], "remote", "get-url", "--push", "origin", env=world["env"])
    assert resolved == impostor_url
    result = run_mirror_script(world, world["on_main"], target="source")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "would rewrite" in result.stdout and "pushinsteadof" in result.stdout
    assert git(impostor, "for-each-ref", env=world["env"]) == ""
    assert set(refs_in(world, "real")) == {"refs/heads/main"}


def test_mirror_target_refuses_a_rewrite_of_the_mirror_url(world) -> None:
    """Same class on the other target: a global-scope `insteadOf` would send the mirror's
    ls-remote and push, auth header included, to another repository."""
    bootstrap_default_branch(world)
    impostor_url, impostor = impostor_repo(world)
    # A default branch, so without the guard the default-branch check passes and the push
    # WOULD land in the impostor rather than being refused for another reason.
    bootstrap_default_branch(world, bare=impostor)
    gitconfig = Path(world["env"]["GIT_CONFIG_GLOBAL"])
    mirror_url = repo_url(world, CONFIG["mirror_repository"])
    gitconfig.write_text(
        gitconfig.read_text() + f'[url "{impostor_url}"]\n\tinsteadOf = {mirror_url}\n'
    )
    result = run_mirror_script(world, world["on_main"])
    assert result.returncode == 1, result.stdout + result.stderr
    assert "would rewrite" in result.stdout and ".insteadof" in result.stdout
    assert canary_refs(impostor, world) == []
    assert canary_refs(world["mirror"], world) == []


def test_a_rewrite_rule_from_the_environment_scope_is_seen_too(world) -> None:
    """Every scope git reads, not only config files: GIT_CONFIG_* in the environment."""
    impostor_url, impostor = impostor_repo(world)
    result = run_mirror_script(
        world,
        world["on_main"],
        target="source",
        GIT_CONFIG_COUNT="1",
        GIT_CONFIG_KEY_0=f"url.{impostor_url}.pushInsteadOf",
        GIT_CONFIG_VALUE_0=repo_url(world, CONFIG["source_repository"]),
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "would rewrite" in result.stdout
    assert git(impostor, "for-each-ref", env=world["env"]) == ""


def test_unreadable_rewrite_rules_are_refused_not_read_as_none(world) -> None:
    """A config git cannot parse must not read as "no rewrite rules"."""
    bootstrap_default_branch(world)
    gitconfig = Path(world["env"]["GIT_CONFIG_GLOBAL"])
    readable = gitconfig.read_text()
    gitconfig.write_text(readable + "[url\n")
    result = run_mirror_script(world, world["on_main"])
    gitconfig.write_text(readable)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "cannot read git's url rewrite rules" in result.stdout
    assert canary_refs(world["mirror"], world) == []


@pytest.mark.parametrize("target", ["mirror", "source"])
def test_rewrite_rules_that_do_not_match_the_url_do_not_refuse(world, target: str) -> None:
    """Opposite direction: only a rule git would APPLY refuses. A rule for another host, and
    a near miss whose value merely EXTENDS the URL (a prefix check run backwards would
    match it), leave the push alone."""
    bootstrap_default_branch(world)
    impostor_url, impostor = impostor_repo(world)
    repo = CONFIG["mirror_repository"] if target == "mirror" else CONFIG["source_repository"]
    gitconfig = Path(world["env"]["GIT_CONFIG_GLOBAL"])
    gitconfig.write_text(
        gitconfig.read_text()
        + f'[url "{impostor_url}"]\n'
        + "\tinsteadOf = https://git.example.invalid/\n"
        + f"\tpushInsteadOf = {repo_url(world, repo)}-other\n"
    )
    result = run_mirror_script(world, world["on_main"], target=target)
    assert result.returncode == 0, result.stdout + result.stderr
    landed = Path(str(world["mirror"] if target == "mirror" else world["real"]))
    assert canary_refs(landed, world) == [f"refs/heads/canary/{world['on_main']}"]
    assert git(impostor, "for-each-ref", env=world["env"]) == ""


def _log_every_git_child(world: World) -> Path:
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


def test_source_target_children_never_see_the_mirror_credential(world) -> None:
    """Sol P1 on d18006414: the mirror token was exported into every git child (and so its
    hooks and credential helpers) of a push to the SOURCE repository."""
    log = _log_every_git_child(world)
    result = run_mirror_script(world, world["on_main"], target="source")  # the token IS exported
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
    bootstrap_default_branch(world)
    log = _log_every_git_child(world)
    result = run_mirror_script(world, world["on_main"])
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
    bootstrap_default_branch(world)
    log = Path(str(world["source"]) + "-hooks.log")
    _logging_program(Path(str(world["source"])) / ".git" / "hooks" / "pre-push", log)
    # Control: the hook is live, and an ordinary push from this checkout runs it.
    scratch = bare_repo(world, "scratch/hook-control")
    git(world["source"], "push", "-q", f"file://{scratch}", "HEAD:refs/heads/x", env=world["env"])
    assert "=== pre-push" in _read(log)
    log.unlink()
    result = run_mirror_script(world, world["on_main"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert canary_refs(world["mirror"], world) == [f"refs/heads/canary/{world['on_main']}"]
    assert TOKEN not in _read(log) and ENCODED not in _read(log)
    assert _read(log) == "", "no hook runs during a credentialed call"


def test_a_mirror_push_starts_no_signing_program_the_checkout_configures(world) -> None:
    """Same class, another program: `push.gpgSign` in the checkout's config makes git start
    `gpg.program` for a receiver that takes push certificates, inside the credentialed push."""
    bootstrap_default_branch(world)
    log = Path(str(world["source"]) + "-gpg.log")
    fake_gpg = _logging_program(Path(str(world["source"]) + "-bin") / "gpg", log, exit_code=1)
    git(world["mirror"], "config", "receive.certNonceSeed", "seed", env=world["env"])
    for key, value in (
        ("push.gpgSign", "true"),
        ("gpg.program", str(fake_gpg)),
        ("user.signingKey", "k"),
    ):
        git(world["source"], "config", key, value, env=world["env"])
    # Control: git does start the program for a push from this checkout.
    scratch = bare_repo(world, "scratch/gpg-control")
    scratch_config = ["config", "receive.certNonceSeed", "seed"]
    git(scratch, *scratch_config, env=world["env"])
    subprocess.run(
        ["git", "push", "-q", f"file://{scratch}", "HEAD:refs/heads/x"],
        cwd=world["source"],
        env=world["env"],
        capture_output=True,
        check=False,
    )
    assert "=== gpg" in _read(log)
    log.unlink()
    result = run_mirror_script(world, world["on_main"])
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
    # The challenge URL builds the mirror URL; the file spellings keep origin a valid source.
    templates = [f"{challenging_server}{{repo}}.git", *world["config"]["remote_url_templates"]]
    config = {**world["config"], "remote_url_templates": templates}
    world["config_path"].write_text(json.dumps(config))
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
    result = run_mirror_script(world, world["on_main"], GIT_ASKPASS=str(env_askpass))
    assert result.returncode == 1, result.stdout + result.stderr
    assert "cannot read" in result.stdout  # the challenge refused it, and nothing answered
    assert _read(log) == "", "no helper or askpass ran during a credentialed call"


def test_the_source_push_keeps_the_checkouts_own_hooks(world) -> None:
    """Opposite direction: the switches ride only on credentialed calls. The source target
    pushes with git's own credentials, and the checkout's hooks still run for it."""
    log = Path(str(world["source"]) + "-hooks.log")
    _logging_program(Path(str(world["source"])) / ".git" / "hooks" / "pre-push", log)
    result = run_mirror_script(world, world["on_main"], target="source")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "=== pre-push" in _read(log)
    assert TOKEN not in _read(log) and ENCODED not in _read(log)


def test_the_committed_mirror_url_is_https_so_no_ssh_program_runs() -> None:
    """The credentialed calls switch off hooks, helpers, askpass, fsmonitor, gpg and ext::,
    but not ssh: that holds only while the URL the mirror push builds is https."""
    assert CONFIG["remote_url_templates"][0].startswith("https://")


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
        'case "$1" in ls-remote|push)\n'
        '  for a in "$@"; do case "$a" in *://*) url="$a";; esac; done\n'
        "  echo \"fatal: unable to access '$url': sent $GIT_CONFIG_VALUE_0\" >&2\n"
        f'  [ "$1" = "{fail}" ] && exit 128;;\n'
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


def test_a_timed_out_git_call_raises_a_redacted_refusal_not_its_argv(monkeypatch) -> None:
    """subprocess.TimeoutExpired's text is the whole argv, URL userinfo included."""
    monkeypatch.setattr(mirror_script, "GIT_TIMEOUT_S", 0.5)
    with pytest.raises(mirror_script.MirrorRefused) as refused:
        mirror_script._git(["ls-remote", f"https://x-access-token:{TOKEN}@10.255.255.1/x.git"])
    _assert_no_secret(str(refused.value))
    assert "timed out" in str(refused.value) and "<redacted>@10.255.255.1/x.git" in str(
        refused.value
    )
    assert refused.value.__cause__ is None and refused.value.__suppress_context__
