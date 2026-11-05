"""Where macOS keeps ``sysctl``, pinned: never looked up on PATH.

``sysctl`` ships at ``/usr/sbin/sysctl`` on every macOS. A PATH lookup adds
nothing on a Mac and takes the tool away from any process whose PATH has no
``/usr/sbin``, which is what launchd hands a user agent that sets its own PATH:
the preview engine could not find ``sysctl``, so its machine-pressure sampler
reported unavailable (Wed 16 Sep 2026).

Readers dereference ``macos_sysctl.SYSCTL`` at call time. ``native_tools``
re-exports this module for call sites that already import from there.
"""

from __future__ import annotations

import subprocess

SYSCTL = "/usr/sbin/sysctl"


def sysctl_n(oid: str, *, timeout: float = 2.0) -> subprocess.CompletedProcess[str]:
    """Run ``sysctl -n`` for one OID. Never raises on a non-zero exit status."""
    return subprocess.run(
        [SYSCTL, "-n", oid],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def try_sysctl_n(oid: str, *, timeout: float = 2.0) -> str | None:
    """Return one ``sysctl -n`` value, or ``None`` when the call fails or times out."""
    try:
        completed = subprocess.run(
            [SYSCTL, "-n", oid],
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    return completed.stdout.strip()


__all__ = ["SYSCTL", "sysctl_n", "try_sysctl_n"]
