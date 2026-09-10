"""Shared fixtures for the ship-dmg --build-on contract tests.

Extracted from tests/scripts/test_ship_dmg_build_on.py when that module
crossed the 600-line review limit. The two test modules that import this one
(test_ship_dmg_build_on.py, test_ship_dmg_build_host_probe.py) drive the same
shipped script through the same fake ssh, so the fakes belong in one place
rather than in whichever module happened to be written first.

REAL PATH, NOTHING STUBBED: `_run` executes the shipped script and returns its
real stdout/stderr. Only `ssh` is faked, and only so the dispatch cases can
stop at the guard without touching a machine.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / ".agents/skills/ship-dmg/scripts/ship_dmg.sh"


def _run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    import os

    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=300,
        env={**os.environ, **(env or {})},
        check=False,
    )


def _head_identity() -> tuple[str, str]:
    """This checkout's HEAD sha and tree, as the build host would report them."""
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout.strip()
    tree = subprocess.run(
        ["git", "rev-parse", "HEAD^{tree}"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout.strip()
    return sha, tree


def _fake_ssh(tmp: str, reply: str) -> Path:
    """An `ssh` that answers the checkout probe with `reply`, and echoes argv otherwise.

    The dispatch makes exactly one ssh carrying the word CANDIDATE, which is
    the checkout probe, and one carrying the composed rollout command.
    Separating them on that substring is what lets a test drive the probe to a
    chosen answer and still observe what was composed afterwards.

    The reply is emitted through a quoted heredoc rather than `echo`, so a
    multi-line probe answer survives verbatim and no shell interprets it.
    """
    import stat

    fake = Path(tmp) / "ssh"
    fake.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        "  *CANDIDATE*)\n"
        "cat <<'PROBE_EOF'\n"
        f"{reply}\n"
        "PROBE_EOF\n"
        "    ;;\n"
        '  *) echo "SSH_ARGV: $*" ;;\n'
        "esac\n"
        "exit 0\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    return fake


def _probe(
    path: str, head: str, tree: str, have: str = "yes", flag: str = "yes", dirty: str = "0"
) -> str:
    """One CANDIDATE line plus the DONE sentinel that proves the probe finished."""
    return f"CANDIDATE {path} {head} {tree} {have} {flag} {dirty}\nDONE"
