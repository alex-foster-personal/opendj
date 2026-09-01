"""Live coverage of the one part of the bifrost2 shell layer that needs the host.

``test_backup_music_shell.py`` exercises the PURE command builders and parsers
through the production path with real captured payloads, and needs no host. What
it cannot cover is whether those commands still do what we believe on the actual
machine: that the shell is bash, that the existence guard answers in-band, and
that `find -printf` output still parses. That belief is exactly what was wrong
for months, so it gets a real check rather than an assumption.

AGENTS.md: "Never silently skip acceptance because data, credentials, hardware,
a service, or a platform is missing... fail explicitly or report the capability
as UNAVAILABLE; do not manufacture a passing result." So:

  - Default (no env var): reported UNAVAILABLE with the command to enable it.
    It does NOT pass. A skip here is a capability report, not evidence.
  - MDT_LIVE_BIFROST2=1: the test RUNS, and an unreachable host is a FAILURE,
    not a skip. Opting in is a claim that the host is there.

Run:  MDT_LIVE_BIFROST2=1 uv run pytest tests/scripts/test_b2shell_live.py -q

Needs the tailnet: `tailscale switch owner@example.com`.

-Claude
"""

from __future__ import annotations

import os
import subprocess

import pytest

from scripts.b2shell import (
    ABSENT_SENTINEL,
    find_command,
    listing_or_absent,
    mkdir_command,
    parse_names,
    parse_sizes,
    ssh,
)

HOST = "bifrost2"
SSH_OPTS = ["-o", "ConnectTimeout=20", "-o", "ServerAliveInterval=15"]
# Never the real store: this suite creates and removes directories.
SCRATCH_ROOT = "D:/asset-store/_b2shell-live-test"

LIVE = os.environ.get("MDT_LIVE_BIFROST2") == "1"
UNAVAILABLE = (
    "UNAVAILABLE: bifrost2 live checks not run. This is a capability report, "
    "not a pass. Enable with MDT_LIVE_BIFROST2=1 and the tailnet up "
    "(tailscale switch owner@example.com)."
)

pytestmark = pytest.mark.skipif(not LIVE, reason=UNAVAILABLE)


@pytest.fixture(scope="module")
def reachable() -> None:
    """Opting in is a claim the host is there, so unreachable is a FAILURE.

    Deliberately not a skip: a skip here would let a broken tailnet look like a
    clean run, which is the shape of every bug this module exists to prevent.
    """
    try:
        ssh(HOST, SSH_OPTS, "echo ok", timeout=30)
    except (RuntimeError, subprocess.TimeoutExpired, OSError) as exc:
        pytest.fail(
            f"MDT_LIVE_BIFROST2=1 was set but {HOST} is unreachable, so the live "
            f"contract is UNVERIFIED rather than passing: {exc}"
        )


@pytest.fixture(scope="module")
def scratch(reachable: None) -> str:
    """A disposable directory on the store; removed even if a test fails."""
    ssh(HOST, SSH_OPTS, f"rm -rf {SCRATCH_ROOT!r}".replace("'", "'"), timeout=60)
    try:
        yield SCRATCH_ROOT
    finally:
        ssh(HOST, SSH_OPTS, mkdir_command([SCRATCH_ROOT]), timeout=60)
        ssh(HOST, SSH_OPTS, f"rm -rf '{SCRATCH_ROOT}'", timeout=60)


def test_the_shell_really_is_bash_not_cmd(reachable: None) -> None:
    """The belief this whole layer rests on. It was wrong for months."""
    shell = ssh(HOST, SSH_OPTS, "echo $0", timeout=30).strip()
    kernel = ssh(HOST, SSH_OPTS, "uname -a", timeout=30).strip()
    assert shell.endswith("bash"), f"shell is {shell!r}, the POSIX layer assumes bash"
    assert "MINGW" in kernel or "MSYS" in kernel, kernel


def test_backslashes_are_still_eaten_by_the_remote_shell(reachable: None) -> None:
    """The mechanism of the original bug, asserted rather than remembered. If
    this ever stops being true, the quoting rules can be revisited."""
    got = ssh(HOST, SSH_OPTS, r"echo D:\asset-store", timeout=30).strip()
    assert got == "D:asset-store", f"expected the backslash eaten, got {got!r}"


def test_absent_path_reports_the_sentinel_in_band(reachable: None) -> None:
    """NEGATIVE CONTROL: a known-absent path must report absent, loudly and on
    exit 0, rather than returning empty for an unknown reason."""
    command = find_command(f"{SCRATCH_ROOT}-definitely-not-there",
                           "-type f -printf '%s %P\\n'")
    out = ssh(HOST, SSH_OPTS, command, timeout=60)
    assert out.strip() == ABSENT_SENTINEL, out
    assert listing_or_absent(out) is None


def test_guard_does_not_create_the_path_it_tests(scratch: str) -> None:
    """R5: --dry-run reads the remote and must not mutate it."""
    ssh(HOST, SSH_OPTS, find_command(scratch, "-type f -printf '%s %P\\n'"), timeout=60)
    still_absent = ssh(
        HOST, SSH_OPTS,
        f"if [ -d '{scratch}' ]; then echo CREATED; else echo STILL_ABSENT; fi",
        timeout=30,
    ).strip()
    assert still_absent == "STILL_ABSENT", "the read path created the directory"


def test_mkdir_then_find_round_trips_through_the_real_host(scratch: str) -> None:
    """mkdir -p, then PROVE it by re-listing, then parse the real output."""
    shards = ["00", "ab", "ff"]
    ssh(HOST, SSH_OPTS, mkdir_command([scratch, *(f"{scratch}/{s}" for s in shards)]),
        timeout=60)

    listing = listing_or_absent(ssh(
        HOST, SSH_OPTS,
        find_command(scratch, "-maxdepth 1 -mindepth 1 -type d -printf '%P\\n'"),
        timeout=60,
    ))
    assert listing is not None, "freshly created root still reported absent"
    assert parse_names(listing) == set(shards)

    # mkdir -p is idempotent: a second call must not fail.
    ssh(HOST, SSH_OPTS, mkdir_command([f"{scratch}/{s}" for s in shards]), timeout=60)


def test_find_printf_sizes_parse_against_a_real_remote_file(scratch: str) -> None:
    """The resume oracle end to end: write ten known bytes, read them back
    through the production builder and parser."""
    ssh(HOST, SSH_OPTS, mkdir_command([f"{scratch}/ab"]), timeout=60)
    ssh(HOST, SSH_OPTS, f"printf '0123456789' > '{scratch}/ab/probe.bin'", timeout=60)

    listing = listing_or_absent(ssh(
        HOST, SSH_OPTS, find_command(scratch, "-type f -printf '%s %P\\n'"), timeout=60,
    ))
    assert listing is not None
    assert parse_sizes(listing, source=HOST) == {"ab/probe.bin": 10}


def test_fsutil_still_execs_under_bash(reachable: None) -> None:
    """Windows .exe tools remain callable; only cmd BUILTINS are unavailable."""
    out = ssh(HOST, SSH_OPTS, "fsutil volume diskfree D:", timeout=60)
    assert "bytes" in out.lower(), out
