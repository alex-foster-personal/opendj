"""ops/testmac/verify.sh's `app` line - CLI/MCP surface + build-staleness check.

issue #2887: demon-llama's installed Open DJ passed the old `app` check (present,
spctl-accepted) for a full day with payload/bin/opendj ENTIRELY ABSENT - a build that
predates the CLI/MCP server. spctl proves Gatekeeper accepted the signature; it says
nothing about which git sha got bundled or whether the CLI binary shipped at all. This
runs the REAL verify.sh through real bash (no reimplementation) against three fixture
hosts, with only the `ssh` transport stubbed - the same technique this repo already uses
for ship_dmg.sh in tests/scripts/ship_dmg_build_on_helpers.py, since no live Mac is
reachable from this box (nucbox is Linux; the two test Macs are not addressable from CI).

[if] payload/bin/opendj responds to `--json state` and its own build-info reports an age
  under TESTMAC_APP_MAX_AGE_H [then] the `app` line is PASS, [else ⛔️]
[if] payload/bin/opendj responds but build-info reports an age OVER the bound
  [then] the `app` line is FAIL naming the measured age and the bound, [else ⛔️]
[if] payload/bin/opendj is absent or exits nonzero (demon-llama's actual issue #2887
  state) [then] the `app` line is FAIL, never PASS - this is the negative control the
  issue's acceptance check requires, [else ⛔️]
[if] the age clause is bypassed (mutation: comment out the AGE_H comparison) [then] the
  stale fixture's own test goes red, proving the clause is load-bearing rather than a
  no-op, [else ⛔️]
"""

from __future__ import annotations

import re
import stat
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
VERIFY_SH = REPO_ROOT / "ops/testmac/verify.sh"

APP_LINE_RE = re.compile(r"^TESTMAC-app (PASS|FAIL|UNKNOWN) (.+)$", re.MULTILINE)
MAX_AGE_H = 24


def _iso(hours_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fake_ssh(tmp_path: Path, *, state_rc: int, built_at_utc: str | None) -> Path:
    """A fake `ssh` standing in for the test Mac's transport.

    Dispatches on argv substrings, exactly like tests/scripts/ship_dmg_build_on_helpers.py's
    `_fake_ssh` for the same script family. `state_rc` controls whether
    `payload/bin/opendj --json state` succeeds (CLI present and answering) or fails
    (absent/broken, demon-llama's actual state). `built_at_utc=None` means the build-info
    call is never reached in a real run, but we still answer it (with an old timestamp)
    so a test bug that skips the state-rc check would be caught by the age check instead,
    not silently pass.
    """
    built = built_at_utc or _iso(999)
    state_body = 'echo \'{"mirror": {}}\'\nexit 0' if state_rc == 0 else "exit 127"
    fake = tmp_path / "ssh"
    fake.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        '  *"Applications/Open DJ.app\' ] &&"*) echo yes ;;\n'
        '  *"spctl -a -vv"*) echo "Open DJ.app: accepted" ;;\n'
        f'  *"--json state"*) {state_body} ;;\n'
        '  *"build-info"*)\n'
        "cat <<BUILD_EOF\n"
        '{"git_sha_full": "deadbeefcafe0000000000000000000000beef", '
        f'"built_at_utc": "{built}", "app_version": "0.1.1"}}\n'
        "BUILD_EOF\n"
        "    ;;\n"
        '  *) echo "" ;;\n'
        "esac\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    return fake


def _run_verify(tmp_path: Path, ssh_dir: Path) -> str:
    import os

    env = {**os.environ, "PATH": f"{ssh_dir}:{os.environ['PATH']}"}
    result = subprocess.run(
        ["bash", str(VERIFY_SH), "test-fixture-host"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        check=False,
    )
    return result.stdout


def _app_line(stdout: str) -> tuple[str, str]:
    match = APP_LINE_RE.search(stdout)
    assert match, f"no TESTMAC-app line in verify.sh output:\n{stdout}"
    return match.group(1), match.group(2)


# REQ: TESTMAC-11
def test_app_line_passes_for_a_current_build(tmp_path: Path) -> None:
    _fake_ssh(tmp_path, state_rc=0, built_at_utc=_iso(1))
    status, detail = _app_line(_run_verify(tmp_path, tmp_path))
    assert status == "PASS", detail
    assert "1h old" in detail or "0h old" in detail
    assert f"bound {MAX_AGE_H}h" in detail


# REQ: TESTMAC-11
def test_app_line_fails_for_a_stale_build_with_working_cli(tmp_path: Path) -> None:
    """The middle case: CLI present and answering, but the build it answers for is old.
    This is the clause a check built on CLI-presence alone would miss entirely."""
    _fake_ssh(tmp_path, state_rc=0, built_at_utc=_iso(30))
    status, detail = _app_line(_run_verify(tmp_path, tmp_path))
    assert status == "FAIL", detail
    assert "30h old" in detail
    assert f"{MAX_AGE_H}h bound" in detail


# REQ: TESTMAC-11
def test_app_line_fails_when_cli_is_absent_negative_control(tmp_path: Path) -> None:
    """demon-llama's actual issue #2887 state: app installed and signed, no working CLI.
    This is the negative control the issue's acceptance check names explicitly."""
    _fake_ssh(tmp_path, state_rc=127, built_at_utc=None)
    status, detail = _app_line(_run_verify(tmp_path, tmp_path))
    assert status == "FAIL", detail
    assert "no working CLI" in detail or "rc=127" in detail


@pytest.mark.parametrize("hours_ago", [23, 25])
def test_age_bound_is_the_documented_default(tmp_path: Path, hours_ago: int) -> None:
    """Mutation-adjacent: pins the exact bound (24h) rather than just its existence, so a
    silent CFG edit (e.g. widening it to hide demon-llama-shaped drift) fails this test."""
    _fake_ssh(tmp_path, state_rc=0, built_at_utc=_iso(hours_ago))
    status, _detail = _app_line(_run_verify(tmp_path, tmp_path))
    expected = "PASS" if hours_ago <= MAX_AGE_H else "FAIL"
    assert status == expected
