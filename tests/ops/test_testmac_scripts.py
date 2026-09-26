"""ops/testmac/* - what can be verified without demon-llama/megamac actually up.

TESTMAC-01..10 (`.planning/REQUIREMENTS.md`, "App-test Macs") need two live tailnet
Macs to fully verify. These tests cover what does not: the scripts parse as valid
bash, `agt-persona-loop.sh` fails fast (no hidden default) when its required env is
missing, and `verify.sh`'s per-id line format is `TESTMAC-<id> <STATUS> <value>` for
every status verify.sh can emit, checked against the REAL `line()` function extracted
from the script - not a reimplementation of it. No mocks: everything here runs the
real script text through real `bash`, per AGENTS.md.

[if] ops/testmac/verify.sh or setup.sh has a bash syntax error [then] `bash -n` on it
  fails, and CI catches it before it ever reaches a host ⛔️
[if] agt-persona-loop.sh is started with no TESTMAC_REPO set [then] it exits nonzero
  with a named error, rather than defaulting to some path or hanging ⛔️
[if] verify.sh emits a status this test does not expect (a typo, e.g. "PASSS") [then]
  the line-format regex fails it, since a caller parsing verify.sh's output needs a
  closed set of statuses ⛔️
[if] verify.sh is missing a line for any TESTMAC id 01-10 [then] the source-scan test
  fails, naming the missing id ⛔️
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TESTMAC_DIR = REPO_ROOT / "ops/testmac"
VERIFY_SH = TESTMAC_DIR / "verify.sh"
SETUP_SH = TESTMAC_DIR / "setup.sh"
LOOP_SH = TESTMAC_DIR / "agt-persona-loop.sh"

LINE_RE = re.compile(r"^TESTMAC-\S+ (PASS|FAIL|SKIP|UNKNOWN|PARTIAL) .+$")


@pytest.mark.parametrize("script", [VERIFY_SH, SETUP_SH, LOOP_SH])
def test_script_is_valid_bash(script: Path) -> None:
    result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_scripts_are_executable() -> None:
    for script in (VERIFY_SH, SETUP_SH, LOOP_SH):
        assert script.stat().st_mode & 0o111, f"{script} is not executable"


def test_verify_sh_usage_error_is_fast_and_named() -> None:
    """No host arg -> a named usage error, not a hang or a silent default host."""
    result = subprocess.run(["bash", str(VERIFY_SH)], capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert "usage" in result.stderr.lower()


def test_setup_sh_usage_error_is_fast_and_named() -> None:
    result = subprocess.run(["bash", str(SETUP_SH)], capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert "usage" in result.stderr.lower()


def test_agt_persona_loop_fails_fast_without_testmac_repo() -> None:
    """set -u + ${TESTMAC_REPO:?...} - a real fail-fast check, not a hidden default
    that would otherwise start rummaging around $HOME/code/music-dj-tools uninvited."""
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}
    result = subprocess.run(
        ["bash", str(LOOP_SH)], capture_output=True, text=True, timeout=10, env=env
    )
    assert result.returncode != 0
    assert "TESTMAC_REPO" in result.stderr


def test_setup_plist_relaunches_on_crash_but_honors_the_stop_flag() -> None:
    """[if] the plist uses a bare KeepAlive=true (relaunch on the clean stop-flag exit,
    which re-creates RUN) [then] fail, [else stop]."""
    text = SETUP_SH.read_text(encoding="utf-8")
    assert "<key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>" in text
    assert "<key>KeepAlive</key><true/>" not in text
    assert "<key>RunAtLoad</key><true/>" in text
    loop = LOOP_SH.read_text(encoding="utf-8").rstrip().splitlines()
    assert loop[-1] == "exit 0", "a stop-flag exit must be a clean 0 so launchd leaves it stopped"


def test_setup_retires_x86_hosts_before_any_toolchain_step() -> None:
    """[if] the x86_64 retirement runs after uv sync / pnpm / playwright [then] a toolchain
    failure under set -e leaves the old loop installed on a retired host, [else stop]."""
    text = SETUP_SH.read_text(encoding="utf-8")
    gate = text.index('= x86_64 ]; then')
    for step in ("uv sync", "pnpm install", "playwright install webkit", "codex login status"):
        assert gate < text.index(step), f"the x86_64 retirement must run before `{step}`"


def test_agt_loop_refuses_to_start_without_a_declared_agent_cli(tmp_path: Path) -> None:
    """[if] the loop starts with no AGT_AGENT_CLI and picks a driver itself [then] fail,
    [else stop] (AGT-28: the host declares its driver, the loop never guesses one)."""
    state_dir = tmp_path / "state"
    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "TESTMAC_REPO": str(REPO_ROOT),
        "TESTMAC_STATE_DIR": str(state_dir),
    }
    result = subprocess.run(
        ["bash", str(LOOP_SH)], capture_output=True, text=True, timeout=10, env=env, check=False
    )
    assert result.returncode != 0
    assert "AGT_AGENT_CLI" in result.stderr
    assert not state_dir.exists(), "the loop must refuse before creating its state dir"


@pytest.mark.parametrize(
    ("value", "needle"), [(None, "AGT_AGENT_CLI"), ("gemini", "not one of codex, claude")]
)
def test_setup_sh_requires_a_known_agent_cli_before_touching_the_host(
    value: str | None, needle: str
) -> None:
    """[if] setup.sh installs a loop with no driver, or an unknown one [then] fail,
    [else stop]; the refusal comes before any ssh, so the host name here is never dialed."""
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}
    if value is not None:
        env["AGT_AGENT_CLI"] = value
    result = subprocess.run(
        ["bash", str(SETUP_SH), "no-such-host.invalid"],
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
        check=False,
    )
    assert result.returncode != 0
    assert needle in result.stderr
    assert "unreachable" not in result.stderr


def _extract_line_function(script_text: str) -> str:
    """Pull the real `line() { ... }` definition out of verify.sh verbatim (it is a
    one-liner today; tolerate a multi-line form too so a future reformat does not
    silently stop this test from testing anything)."""
    match = re.search(r"^line\(\)\s*\{.*\}\s*$", script_text, re.MULTILINE) or re.search(
        r"^line\(\)\s*\{.*?\n\}\n", script_text, re.MULTILINE | re.DOTALL
    )
    assert match, "verify.sh no longer defines a `line()` function - update this test"
    return match.group(0)


@pytest.mark.parametrize("status", ["PASS", "FAIL", "SKIP", "UNKNOWN", "PARTIAL"])
def test_line_format_matches_real_function(status: str) -> None:
    """Every status verify.sh can print comes out of the SAME real `line()` function,
    run for real (not reimplemented here), and matches the closed format a caller
    parsing verify.sh's stdout can rely on."""
    script_text = VERIFY_SH.read_text()
    line_fn = _extract_line_function(script_text)
    probe = f'{line_fn}\nline 07 {status} "some measured value"\n'
    result = subprocess.run(
        ["bash", "-c", probe], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
    output = result.stdout.strip()
    assert LINE_RE.match(output), f"{output!r} does not match the expected line format"


def test_verify_sh_covers_every_testmac_id() -> None:
    script_text = VERIFY_SH.read_text()
    for req_id in [f"{n:02d}" for n in range(1, 11)]:
        assert f"TESTMAC-{req_id}" in script_text or f"'{req_id}'" in script_text, (
            f"verify.sh has no TESTMAC-{req_id} line"
        )
    # TESTMAC-08 is v2 (the maintainer, Mon 14 Sep 2026): must be SKIP, never a live check.
    skip_line_match = re.search(r'line 08 SKIP "([^"]*)"', script_text)
    assert skip_line_match, "TESTMAC-08 must be an unconditional SKIP line (moved to v2)"
    assert "v2" in skip_line_match.group(1)


def test_setup_sh_does_not_implement_testmac_08() -> None:
    """Scope guard: TESTMAC-08 (volume) was pulled to v2 mid-session. Fail loudly if a
    future edit reintroduces volume-control logic in setup.sh without updating this
    test and REQUIREMENTS.md together."""
    script_text = SETUP_SH.read_text()
    assert "output volume" not in script_text
    assert "osascript" not in script_text


def test_verify_sh_resolves_reverse_targets_live_not_via_literal_ip() -> None:
    """OSSPUB-01/02 (issue #1808): a literal tailnet CGNAT address baked into
    the tracked tree is a real-identity leak, and it also goes stale the
    moment a device re-enrolls. verify.sh must resolve silver/air's addresses
    LIVE via `tailscale ip -4 <node>` on $HOST, never a hardcoded 100.x.x.x
    literal."""
    script_text = VERIFY_SH.read_text()
    assert not re.search(r"\b100\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", script_text), (
        "verify.sh contains a literal tailnet CGNAT address"
    )
    assert "tailscale ip -4 silver" in script_text
    assert "tailscale ip -4 air" in script_text


def test_readme_documents_all_testmac_ids() -> None:
    readme = (TESTMAC_DIR / "README.md").read_text()
    for req_id in [f"{n:02d}" for n in range(1, 11)]:
        assert f"TESTMAC-{req_id}" in readme, f"README.md does not mention TESTMAC-{req_id}"
