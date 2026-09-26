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

import os
import re
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ops.agentic_testing.driver_claude import BILLING_REROUTE_ENV_VARS

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
    gate = text.index("= x86_64 ]; then")
    for step in ("uv sync", "pnpm install", "playwright install webkit", "codex login status"):
        assert gate < text.index(step), f"the x86_64 retirement must run before `{step}`"


PERSONA_LOOP_LABELS = ("opendj-agt-persona-loop", "com.af.agt-loop")


def test_x86_retirement_covers_every_persona_loop_label() -> None:
    """[if] the x86_64 gate in setup.sh or verify.sh names only the current label [then]
    a legacy com.af.agt-loop keeps running on a retired host while verify reports SKIP,
    [else stop]. Also guards the pipefail trap: `launchctl print` exits nonzero for a
    missing service, so piping it straight into `grep -q` reads every host as loaded
    (hit live on megamac Sat 26 Sep 2026)."""
    setup = SETUP_SH.read_text(encoding="utf-8")
    gate = setup[
        setup.index("= x86_64 ]; then") : setup.index("exit 0", setup.index("= x86_64 ]; then"))
    ]
    verify = VERIFY_SH.read_text(encoding="utf-8")
    x86_branch = verify[verify.index("x86_64") :]
    for label in PERSONA_LOOP_LABELS:
        assert label in setup[: setup.index("= x86_64 ]; then")] + gate, (
            f"setup.sh x86 gate skips {label}"
        )
        assert label in x86_branch, f"verify.sh x86 branch skips {label}"
    for text in (gate, x86_branch):
        assert not re.search(r"launchctl print[^\n]*\| *grep -q", text), (
            "pipe launchctl print into grep under pipefail and a missing service reads as loaded"
        )


def test_agt_loop_holds_after_a_walled_attempt() -> None:
    """[if] the loop re-probes a walled seat every minute [then] a weekly limit churns an
    empty run dir per minute for days (live on demon-llama Sat 26 Sep 2026), [else stop].
    The hold keys on the harness's own rc=3 AND its `CLI is walled` line, so an ordinary
    failed run keeps the normal cadence."""
    text = LOOP_SH.read_text(encoding="utf-8")
    assert 'AGT_WALLED_BACKOFF_S="${AGT_WALLED_BACKOFF_S:-' in text
    hold = re.search(
        r'if \[ "\$rc" = 3 \] && grep -qE "\$DRIVER_BLOCK_PATTERN" "\$out\.log".*?\n'
        r'\s*hold_while_running "\$AGT_WALLED_BACKOFF_S"',
        text,
        re.S,
    )
    assert hold, "the walled hold must be gated on rc=3 plus a driver-step verdict"


def _driver_verdicts() -> set[str]:
    """Every agent-CLI-step verdict phrase the drivers emit, read from their source."""
    sources = (REPO_ROOT / "ops/agentic_testing").glob("driver_*.py")
    found = {
        match.group(1)
        for source in sources
        for match in re.finditer(r'"(?:Codex|Claude) (CLI (?:is \w+|refused))', source.read_text())
    }
    assert {"CLI is walled", "CLI is unavailable", "CLI refused"} <= found, found
    return found


def test_every_driver_verdict_blocks_verify_and_holds_the_loop() -> None:
    """[if] a driver verdict (walled, refused, unavailable) is missing from verify.sh's
    blocked matcher or the loop's hold [then] historical completions PASS a host whose
    driver cannot run now, or the loop re-probes it every minute, [else stop]."""
    verify = VERIFY_SH.read_text(encoding="utf-8")
    matcher = re.search(r"DRIVER_BLOCK_PATTERN='([^']+)'", verify).group(1)
    loop = LOOP_SH.read_text(encoding="utf-8")
    hold = re.search(r"DRIVER_BLOCK_PATTERN='([^']+)'", loop).group(1)
    for verdict in _driver_verdicts():
        line = f"Claude {verdict}: detail"
        assert re.search(matcher, line), f"verify misses {verdict}"
        assert re.search(hold, line), f"the loop hold misses {verdict}"


def test_verify_finds_a_verdict_that_opens_a_long_message(tmp_path: Path) -> None:
    """[if] verify reads only the tail of the latest log [then] a verdict followed by more
    diagnostic lines is missed and earlier completions PASS a blocked host, [else stop].
    Runs verify.sh's real remote grep over a real file; a persona review line that only
    MENTIONS a phrase is the negative control."""
    verify = VERIFY_SH.read_text(encoding="utf-8")
    pattern = re.search(r"DRIVER_BLOCK_PATTERN='([^']+)'", verify).group(1)
    assert "tail -" not in verify[verify.index("DRIVER_BLOCK_PATTERN=") :].split("\n")[2]
    log = tmp_path / "attempt.log"

    def block_line(text: str) -> str:
        log.write_text(text, encoding="utf-8")
        done = subprocess.run(
            ["grep", "-m1", "-E", pattern, str(log)], capture_output=True, text=True, check=False
        )
        return done.stdout.strip()

    long_message = "agent CLI: claude\nClaude CLI is unavailable: crashed\n" + "trace\n" * 20
    assert block_line(long_message) == "Claude CLI is unavailable: crashed"
    review = "agent CLI: claude\n## Findings\n- the sync pane says Claude CLI is walled?\n"
    assert block_line(review) == "", "a review that mentions a phrase is not a verdict"


def _hold_helper() -> str:
    text = LOOP_SH.read_text(encoding="utf-8")
    start = text.index("hold_while_running() {")
    return text[start : text.index("\n}\n", start) + 3]


def test_walled_hold_returns_as_soon_as_the_stop_flag_is_gone(tmp_path: Path) -> None:
    """[if] the walled hold sleeps its full backoff after RUN is removed [then] the loop's
    promised clean stop waits up to 30 min, [else stop]. Runs the loop's real helper."""
    script = _hold_helper() + "hold_while_running 600\n"
    env = {"PATH": "/usr/bin:/bin", "STATE_DIR": str(tmp_path), "AGT_HOLD_CHUNK_S": "1"}
    started = time.monotonic()
    subprocess.run(["bash", "-c", script], env=env, check=True, timeout=10)
    assert time.monotonic() - started < 3, "a hold with no RUN flag must return at once"
    # Control: with RUN present the helper really holds, one chunk here.
    (tmp_path / "RUN").touch()
    started = time.monotonic()
    subprocess.run(
        ["bash", "-c", _hold_helper() + "hold_while_running 1\n"], env=env, check=True, timeout=10
    )
    assert time.monotonic() - started >= 1


def test_setup_refuses_the_same_billing_reroutes_as_the_driver() -> None:
    """[if] setup.sh's reroute list drifts from the driver's [then] setup installs a loop
    whose every run the driver refuses, [else stop]."""
    text = SETUP_SH.read_text(encoding="utf-8")
    listed = re.search(r'BILLING_REROUTE_ENV_VARS="([^"]+)"', text)
    assert listed, "setup.sh must name the reroute vars it refuses"
    assert tuple(listed.group(1).split()) == BILLING_REROUTE_ENV_VARS


def test_setup_never_overwrites_the_live_loop_script_in_place() -> None:
    """[if] setup.sh copies straight onto the installed loop script [then] a live loop's
    bash, which reads its script incrementally, can run a torn file, [else stop]."""
    text = SETUP_SH.read_text(encoding="utf-8")
    live = "~/.local/state/af-agt/opendj-agt-persona-loop.sh"
    copies = [line for line in text.splitlines() if line.startswith("scp ") and live in line]
    assert copies, "control: setup.sh must still install the loop script"
    assert all(f"{live}.new" in line for line in copies), copies
    assert f"mv -f {live}.new {live}" in text


def _run_stats_snippet(agent_cli: str) -> str:
    """verify.sh's REAL remote RUN_STATS command, unescaped as the remote shell sees it."""
    text = VERIFY_SH.read_text(encoding="utf-8")
    start = text.index('RUN_STATS=$(run "') + len('RUN_STATS=$(run "')
    end = text.index('echo \\"\\$total \\$completed \\$latest\\"")', start)
    body = text[start:end] + 'echo \\"\\$total \\$completed \\$latest\\"'
    body = body.replace("$AGENT_CLI", agent_cli)
    return body.replace('\\"', '"').replace("\\$", "$")


def test_verify_counts_only_attempts_after_the_plist_was_written(tmp_path: Path) -> None:
    """[if] after a driver switch or reinstall verify.sh counts, or reads as latest, an
    attempt that STARTED before the plist was last written [then] a new driver passes on
    old completions or is blamed for the old driver's wall, [else stop]. Attempts are
    keyed on the UTC start stamp in their log name. Runs verify.sh's real remote snippet."""
    runs = tmp_path / ".local/state/af-agt/runs"
    runs.mkdir(parents=True)
    plist = tmp_path / "Library/LaunchAgents/opendj-agt-persona-loop.plist"
    plist.parent.mkdir(parents=True)
    plist.write_text("<plist/>", encoding="utf-8")
    written = datetime(2026, 9, 26, 2, 5, 0, tzinfo=UTC).timestamp()
    os.utime(plist, (written, written))
    env = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin", "TZ": "Europe/London"}

    def attempt(name: str, log: str, cli: str | None) -> None:
        (runs / f"{name}.log").write_text(log, encoding="utf-8")
        if cli is not None:  # None: refused before its bundle dir existed
            (runs / name).mkdir()
            (runs / name / "run-state.json").write_text('{"state": "completed"}', "utf-8")
            (runs / name / "agent.cli").write_text(f"{cli}\n", encoding="utf-8")

    def stats(agent_cli: str) -> list[str]:
        done = subprocess.run(
            ["bash", "-c", _run_stats_snippet(agent_cli)],
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )
        return done.stdout.split()

    attempt("20260926T020000Z-hostile-s1", "Claude CLI is walled: weekly\n", "claude")
    # In flight across the reinstall: started 02:04:00Z, before the 02:05:00Z plist, and
    # its log and completion are written only now, long after it.
    attempt("20260926T020400Z-hostile-s2", "Claude CLI is walled: weekly\n", "claude")
    assert stats("claude") == ["0", "0"], "an attempt started before the plist leaked in"
    attempt("20260926T030000Z-hostile-s3", "ok\n", "codex")
    assert stats("codex") == ["1", "1", "20260926T030000Z-hostile-s3.log"]
    # A later attempt refused before its bundle existed is still the latest attempt.
    attempt("20260926T031000Z-hostile-s4", "Claude CLI refused: ANTHROPIC_BASE_URL\n", None)
    assert stats("codex") == ["2", "1", "20260926T031000Z-hostile-s4.log"]
    # Control: the post-switch completion does not count for a driver it was not run by.
    assert stats("claude")[:2] == ["2", "0"]


def test_verify_reports_a_dead_engine_before_a_wall() -> None:
    """[if] a walled latest attempt is reported (PARTIAL) while the engine is also down
    [then] a current outage hides behind a subscription limit instead of FAILing,
    [else stop]. PASS must also require a live engine."""
    text = VERIFY_SH.read_text(encoding="utf-8")
    dead = text.index('elif [ -z "$ENGINE_LIVE" ]; then')
    assert dead < text.index('elif [ -n "$DRIVER_BLOCK_LINE" ]; then')
    passing = text.rindex("elif", 0, dead)
    assert '[ -n "$ENGINE_LIVE" ]' in text[passing:dead], "PASS must require a live engine"


def test_setup_stops_the_old_loop_before_writing_the_cutoff_plist() -> None:
    """[if] setup.sh writes the new plist while the old loop still runs [then] an old-loop
    attempt can start after verify's cutoff and count as new evidence, [else stop]."""
    text = SETUP_SH.read_text(encoding="utf-8")
    write = text.index('scp -q "$PLIST_PATH"')
    assert "launchctl bootout gui/\\$(id -u)/${PLIST_LABEL}" in text[:write]


@pytest.mark.skipif(not Path("/bin/zsh").exists(), reason="UNAVAILABLE: no /bin/zsh here")
def test_plist_driver_survives_a_zshenv_that_exports_another(tmp_path: Path) -> None:
    """[if] ~/.zshenv exports AGT_AGENT_CLI and the plist's zsh -c command does not
    re-export the declared one [then] the loop drives the profile's CLI while setup and
    verify report the plist's, [else stop]. Runs the plist's real command string."""
    text = SETUP_SH.read_text(encoding="utf-8")
    command = re.search(r"<string>-c</string>\s*<string>([^<]+)</string>", text).group(1)
    probe = tmp_path / "loop.sh"
    probe.write_text('printf %s "$AGT_AGENT_CLI"\n', encoding="utf-8")
    command = command.replace("${AGENT_CLI}", "claude")
    command = re.sub(r"/Users/\$\{REMOTE_USER\}/\S+", str(probe), command)
    (tmp_path / ".zshenv").write_text("export AGT_AGENT_CLI=codex\n", encoding="utf-8")
    env = {"PATH": "/usr/bin:/bin", "ZDOTDIR": str(tmp_path), "AGT_AGENT_CLI": "claude"}
    done = subprocess.run(
        ["/bin/zsh", "-c", command], capture_output=True, text=True, env=env, check=True
    )
    assert done.stdout == "claude"
    # Control: the hostile profile really does override a bare inherited value.
    bare = subprocess.run(
        ["/bin/zsh", "-c", f"exec /bin/bash {probe}"],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    assert bare.stdout == "codex"


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
    result = subprocess.run(["bash", "-c", probe], capture_output=True, text=True, timeout=10)
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
