"""ops/testmac/setup.sh's own functions, run for real: engine choice, login checks and
their host-side bounds, extracted from the script and executed through real bash, with
the live halves over real ssh against demon-llama (UNAVAILABLE off the tailnet).

Split from test_testmac_scripts.py to keep each module under the repo's Python file-size
limit. No mocks, per AGENTS.md: the API-only engine is the real production daemon with
no frontend, and every login status comes from a real CLI.
"""

from __future__ import annotations

import re
import shlex
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SETUP_SH = REPO_ROOT / "ops/testmac/setup.sh"
CLAUDE_SEATS = ["True firstParty claude.ai", "True firstParty oauth_token"]


def _setup_function(name: str) -> str:
    """One function's real source from setup.sh: a one-liner, or a block up to its `}`."""
    text = SETUP_SH.read_text(encoding="utf-8")
    one_line = re.search(rf"^{name}\(\)\s+\{{[^\n]*\}}\n", text, re.M)
    block = re.search(rf"^{name}\(\)\s+\{{\n.*?^\}}\n", text, re.M | re.S)
    match = one_line or block
    assert match, f"setup.sh must define {name}()"
    return match.group(0)


LIVE_ENGINE_HOST = "demon-llama"


def _host_reachable(host: str) -> bool:
    probe = ["ssh", "-n", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", host, "true"]
    return subprocess.run(probe, capture_output=True, check=False, timeout=30).returncode == 0


def _free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@contextmanager
def _real_api_only_engine_on(host: str, tmp_path: Path) -> Iterator[int]:
    """The REAL production daemon app (`apps.webui.server.app:app`, the same app
    `python -m apps.webui.server` serves) as a subprocess over an empty data dir, its frontend
    build dir pointed at nothing: a genuine API-only engine (/api/v1/health ok,
    /performance a JSON 404) on any checkout. Exposed on `host` by a real `ssh -R`
    forward; yields the port on `host`."""
    port = _free_local_port()
    daemon_env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "MDT_DATA_DIR": str(tmp_path / "data"),
        "MDT_FRONTEND_BUILD_DIR": str(tmp_path / "no-frontend-build"),
        "AF_SERVICE_ID": "com.af.opendj.test-api-only-engine",
    }
    daemon_argv = [
        sys.executable, "-m", "uvicorn", "apps.webui.server.app:app",
        "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning",
    ]  # fmt: skip
    log = (tmp_path / "daemon.log").open("wb")
    daemon = subprocess.Popen(daemon_argv, cwd=REPO_ROOT, env=daemon_env, stdout=log, stderr=log)
    tunnel = None
    try:
        deadline = time.monotonic() + 60
        while True:
            assert daemon.poll() is None, (tmp_path / "daemon.log").read_text()
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1/health", timeout=2):
                    break
            except OSError:
                assert time.monotonic() < deadline, "the real daemon never answered health"
                time.sleep(0.5)
        forward = [
            "ssh", "-N", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes",
            "-R", f"0:127.0.0.1:{port}", host,
        ]  # fmt: skip
        tunnel = subprocess.Popen(forward, stderr=subprocess.PIPE, text=True)
        assert tunnel.stderr is not None
        remote_port = None
        for _ in range(20):
            match = re.search(r"Allocated port (\d+)", tunnel.stderr.readline())
            if match:
                remote_port = int(match.group(1))
                break
        assert remote_port, "ssh -R never reported its allocated port"
        yield remote_port
    finally:
        if tunnel is not None:
            tunnel.terminate()
            tunnel.wait(timeout=10)
        daemon.terminate()
        daemon.wait(timeout=30)
        log.close()


def test_setup_chooses_only_an_engine_that_serves_the_app_shell(tmp_path: Path) -> None:
    """[if] setup.sh counts an engine as live when it is down, or healthy but API-only
    (no app shell at /performance), or picks such a checkout while the -wt-agt-mini
    sibling serves the app [then] the loop parks forever despite a usable engine,
    [else stop].

    Live, no stand-ins: setup.sh's own run/probe_engine_repo/engine_serves_app/
    choose_engine_repo over real ssh against demon-llama's real engines (its main
    checkout's engine is down there, the agt-mini sibling serves the app), plus the
    real production daemon with no frontend forwarded onto that host as the API-only
    case. Its
    health is re-measured through the same forward, so "not live" cannot come from a
    broken tunnel. UNAVAILABLE off the tailnet."""
    if not _host_reachable(LIVE_ENGINE_HOST):
        pytest.skip(f"UNAVAILABLE: {LIVE_ENGINE_HOST} is not reachable over BatchMode ssh")
    with _real_api_only_engine_on(LIVE_ENGINE_HOST, tmp_path) as api_only_port:
        api_only = f"http://127.0.0.1:{api_only_port}"
        harness = "\n".join(
            [
                "set -euo pipefail",
                f"HOST={LIVE_ENGINE_HOST}",
                "log() { printf '[OK] %s\\n' \"$*\"; }",
                _setup_function("run"),
                "REMOTE_REPO=$(run 'cd ~/code/music-dj-tools 2>/dev/null && pwd')",
                _setup_function("probe_engine_repo"),
                _setup_function("engine_serves_app"),
                _setup_function("choose_engine_repo"),
                "choose_engine_repo",
                'echo "ENGINE_REPO=$ENGINE_REPO"',
                'echo "ENGINE_BASE=$(probe_engine_repo "$ENGINE_REPO")"',
                "engine_serves_app http://127.0.0.1:1 && echo CLOSED=live || echo CLOSED=no",
                f"engine_serves_app {api_only} && echo API_ONLY=live || echo API_ONLY=not-live",
                f'echo API_ONLY_HEALTH=$(run "curl -s -m 5 {api_only}/api/v1/health" | head -c 15)',
            ]
        )
        done = subprocess.run(
            ["bash", "-c", harness], capture_output=True, text=True, check=False, timeout=180
        )
    assert done.returncode == 0, done.stderr
    fields = dict(line.split("=", 1) for line in done.stdout.splitlines() if "=" in line)
    assert fields["API_ONLY_HEALTH"].startswith('{"status":"ok"'), "control: API-only engine is up"
    assert fields["API_ONLY"] == "not-live", "a healthy engine with no app shell is not live"
    assert fields["CLOSED"] == "no", "a closed port is not live"
    assert fields["ENGINE_REPO"].endswith("-wt-agt-mini"), done.stdout + done.stderr
    assert "[WARN]" not in done.stderr
    # Independent instrument: the chosen base really is healthy AND serves the app shell.
    base = fields["ENGINE_BASE"]
    assert base.startswith("http://127.0.0.1:"), base
    remeasure = subprocess.run(
        [
            "ssh", "-n", "-o", "BatchMode=yes", LIVE_ENGINE_HOST,
            f"curl -s -m 5 {base}/api/v1/health; echo; "
            f"curl -s -m 5 {base}/performance | head -c 200",
        ],
        capture_output=True, text=True, check=False, timeout=60,
    )  # fmt: skip
    assert '"status":"ok"' in remeasure.stdout
    assert "<!doctype html" in remeasure.stdout.lower()


def _setup_line(prefix: str) -> str:
    match = re.search(rf"^{re.escape(prefix)}.*$", SETUP_SH.read_text(encoding="utf-8"), re.M)
    assert match, f"setup.sh must have a line starting {prefix!r}"
    return match.group(0)


def test_setup_auth_checks_are_bounded_on_the_host() -> None:
    """[if] a hung `codex login status` or `claude auth status` (broken CLI, keychain,
    network) blocks setup.sh forever, since ssh's ConnectTimeout bounds only the
    connection [then] setup never fails fast, [else stop]. Runs setup's own BOUNDED
    prefix on real hanging processes: locally, and live on demon-llama through setup's
    real `run` in both remote forms (plain, and inside /bin/zsh -c) when reachable."""
    text = SETUP_SH.read_text(encoding="utf-8")
    assert 'run "$BOUNDED codex login status"' in text
    assert "run \"/bin/zsh -c '$BOUNDED claude auth status --json'\"" in text
    prefix = "\n".join(["set -euo pipefail", "AUTH_CHECK_TIMEOUT_S=1", _setup_line("BOUNDED=")])
    started = time.monotonic()
    hung = subprocess.run(
        ["bash", "-c", prefix + '\neval "$BOUNDED /bin/sleep 30"'],
        capture_output=True, text=True, check=False, timeout=60,
    )  # fmt: skip
    assert hung.returncode != 0 and time.monotonic() - started < 10, "the bound must fire"
    answered = subprocess.run(
        ["bash", "-c", prefix + '\neval "$BOUNDED /bin/echo answered"'],
        capture_output=True, text=True, check=False, timeout=60,
    )  # fmt: skip
    assert answered.stdout == "answered\n", "control: a prompt command still answers"
    if not _host_reachable(LIVE_ENGINE_HOST):
        pytest.skip(f"UNAVAILABLE (live half): {LIVE_ENGINE_HOST} is not reachable")
    remote = "\n".join(
        [
            prefix,
            f"HOST={LIVE_ENGINE_HOST}",
            _setup_function("run"),
            'run "$BOUNDED /bin/sleep 30" && echo PLAIN=answered || echo PLAIN=bounded',
            "run \"/bin/zsh -c '$BOUNDED /bin/sleep 30'\" && echo ZSH=answered || echo ZSH=bounded",
            "run \"/bin/zsh -c '$BOUNDED /bin/echo ok'\" && echo CONTROL=answered || echo CONTROL=",
        ]
    )
    started = time.monotonic()
    done = subprocess.run(
        ["bash", "-c", remote], capture_output=True, text=True, check=False, timeout=120
    )
    fields = dict(line.split("=", 1) for line in done.stdout.splitlines() if "=" in line)
    assert fields == {"PLAIN": "bounded", "ZSH": "bounded", "CONTROL": "answered"}, done
    assert time.monotonic() - started < 60


@pytest.mark.parametrize(
    ("rc", "status", "patterns", "confirmed"),
    [
        (0, "Logged in using ChatGPT", ["*Logged in*"], True),
        (255, "Logged in using ChatGPT", ["*Logged in*"], False),  # printed, then hung
        (1, "Logged in using ChatGPT", ["*Logged in*"], False),
        (0, "Not logged in", ["*Logged in*"], False),
        (0, "True firstParty oauth_token", CLAUDE_SEATS, True),
        (0, "True firstParty claude.ai", CLAUDE_SEATS, True),
        (255, "True firstParty oauth_token", CLAUDE_SEATS, False),
        (0, "True firstParty api_key", CLAUDE_SEATS, False),
        (0, "", CLAUDE_SEATS, False),
    ],
)
def test_setup_confirms_a_login_only_with_a_zero_exit(
    rc: int, status: str, patterns: list[str], confirmed: bool
) -> None:
    """[if] setup accepts a login status whose command then hung (killed by the bound)
    or failed [then] it installs a loop whose every preflight is refused, [else stop].
    Runs setup.sh's own status_confirmed()."""
    quoted = " ".join(shlex.quote(item) for item in [str(rc), status, *patterns])
    probe = f"{_setup_function('status_confirmed')}status_confirmed {quoted}"
    done = subprocess.run(["bash", "-c", probe], capture_output=True, check=False, timeout=30)
    assert (done.returncode == 0) is confirmed


def test_setup_auth_section_confirms_demon_llamas_real_logins() -> None:
    """[if] requiring a zero exit rejects a genuinely signed-in host (a CLI that exits
    nonzero on success) [then] setup can never install the loop, [else stop]. Runs
    setup.sh's whole TESTMAC-04 / AGT-28 section live on demon-llama (codex and claude
    both signed in there); UNAVAILABLE off the tailnet."""
    if not _host_reachable(LIVE_ENGINE_HOST):
        pytest.skip(f"UNAVAILABLE: {LIVE_ENGINE_HOST} is not reachable over BatchMode ssh")
    text = SETUP_SH.read_text(encoding="utf-8")
    start = text.index(
        "# ---------------------------------------------------------------- TESTMAC-04"
    )
    end = text.index(
        "# ---------------------------------------------------------------- TESTMAC-07"
    )
    harness = "\n".join(
        [
            "set -euo pipefail",
            f"HOST={LIVE_ENGINE_HOST}",
            "AGENT_CLI=claude",
            _setup_function("log"),
            _setup_function("fail"),
            _setup_function("run"),
            text[start:end],
        ]
    )
    done = subprocess.run(
        ["bash", "-c", harness], capture_output=True, text=True, check=False, timeout=240
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "codex login status reports logged in" in done.stdout
    assert "claude auth status reports a first-party subscription login" in done.stdout


def test_setup_engine_discovery_survives_a_failed_probe() -> None:
    """[if] a checkout whose port probe fails (no claim, no checkout) aborts setup.sh
    under `set -euo pipefail` [then] the -wt-agt-mini fallback is never examined and
    the warn-and-install path never runs, [else stop]. setup.sh's real functions over
    real ssh on demon-llama, pointed at checkouts that do not exist there, so both
    probes really fail. UNAVAILABLE off the tailnet."""
    if not _host_reachable(LIVE_ENGINE_HOST):
        pytest.skip(f"UNAVAILABLE: {LIVE_ENGINE_HOST} is not reachable over BatchMode ssh")
    harness = "\n".join(
        [
            "set -euo pipefail",
            f"HOST={LIVE_ENGINE_HOST}",
            "REMOTE_REPO=/nonexistent-opendj-checkout/music-dj-tools",
            _setup_function("log"),
            _setup_function("run"),
            _setup_function("probe_engine_repo"),
            _setup_function("engine_serves_app"),
            _setup_function("choose_engine_repo"),
            "choose_engine_repo",
            'echo "ENGINE_REPO=$ENGINE_REPO"',
        ]
    )
    done = subprocess.run(
        ["bash", "-c", harness], capture_output=True, text=True, check=False, timeout=180
    )
    assert done.returncode == 0, done.stderr
    assert "ENGINE_REPO=/nonexistent-opendj-checkout/music-dj-tools\n" in done.stdout
    assert "-wt-agt-mini" in done.stderr and "[WARN]" in done.stderr, "both were examined"
