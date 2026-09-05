"""OS-visible process identities for Open DJ services.

The browser URL is an address, not an application name.  Every process we own
sets a bounded role name; system-owned WebKit XPC executables keep Apple's
names, while the page title and icon provide their Open DJ presentation.
"""

from __future__ import annotations

import os
import re
import shlex
import sys
from collections.abc import Sequence

import setproctitle

PROCESS_ROLE_ENV = "OPEN_DJ_PROCESS_ROLE"
_ROLE_SEPARATOR = re.compile(r"[^a-z0-9.]+")


def process_title(role: str, instance: str | int | None = None) -> str:
    """Return a ``pgrep -f opendj-`` discoverable OS process title."""
    clean_role = _ROLE_SEPARATOR.sub("-", role.lower()).strip("-")
    if not clean_role:
        raise ValueError("process role must be non-empty")
    name = f"opendj-{clean_role}"
    port = "" if instance is None else f" --port {instance}"
    return f"{name} --name {name}{port}"


def _os_title(
    title: str,
    invocation_marker: str | None,
    invocation_argv: Sequence[str] | None,
) -> str:
    """The string actually handed to setproctitle: the title, argv appended.

    setproctitle REPLACES the OS-visible command line wholesale, confirmed
    empirically: ps/pgrep read back exactly what it wrote and nothing of the
    original argv survives alongside it. Several probes outside this process
    identify it by substrings of that original argv rather than by pid --
    ship_dmg.sh and virgin_boot.sh `pgrep -f` the bundled interpreter's path
    (``Resources/payload/runtime/bin/python3``), and
    scripts/diagnostics/probe_process_family.py greps a live command column
    for that same path plus the literal ``apps.engine_core serve`` invocation
    to tell the real engine apart from an orphan. A bare rename makes every
    one of those probes blind to a process that reached this call.

    ``sys.argv`` cannot supply that second marker: for a real
    ``python -m apps.engine_core serve`` launch, CPython's ``-m`` machinery
    rewrites ``sys.argv[0]`` to the resolved ``__main__.py`` path and drops
    the ``-m apps.engine_core`` tokens entirely, so joining
    ``sys.argv[1:]`` after ``sys.executable`` only ever yields
    ``"python3 serve --port 8585"`` -- never the module name a probe greps
    for. A caller that knows its own true invocation shape (an entry point
    module knows what it is) passes it explicitly via ``invocation_marker``;
    only callers with no such probe contract fall back to ``sys.argv``.

    The human-readable title leads, because the Activity Monitor / short `ps`
    column is the actual product requirement here (see the module docstring);
    the original argv follows in brackets so every substring those probes
    grep for is still present somewhere on the line.
    """
    if invocation_argv is not None:
        original = shlex.join(invocation_argv)
    else:
        marker = invocation_marker if invocation_marker is not None else " ".join(sys.argv[1:])
        original = " ".join(part for part in (sys.executable, marker) if part)
    return f"{title} [{original}]"


def process_command(
    role: str,
    instance: str | int | None = None,
    *,
    invocation_marker: str | None = None,
    invocation_argv: Sequence[str] | None = None,
) -> str:
    """Return the exact OS-visible command line for an Open DJ process."""
    return _os_title(process_title(role, instance), invocation_marker, invocation_argv)


def set_process_identity(
    role: str,
    instance: str | int | None = None,
    *,
    invocation_marker: str | None = None,
    invocation_argv: Sequence[str] | None = None,
) -> str:
    """Set this process's OS-visible Open DJ title; return the clean title.

    ``invocation_marker`` lets a caller that knows how it was actually
    launched (e.g. ``apps.engine_core serve``) guarantee that substring
    survives in the OS-visible title for external probes, since
    ``sys.argv`` cannot reconstruct a ``-m`` invocation (see ``_os_title``).

    The RETURNED string (and ``PROCESS_ROLE_ENV``) is the clean title without
    the argv marker: callers and tests reading either want the display name,
    not the probe-compatibility payload riding along on the OS-level string.
    ``invocation_argv`` is for a supervised child whose parent persists its
    exact spawn argv for later identity checks; it preserves that full argv,
    including the executable, without reconstructing it from Python globals.
    """
    title = process_title(role, instance)
    setproctitle.setproctitle(
        process_command(
            role,
            instance,
            invocation_marker=invocation_marker,
            invocation_argv=invocation_argv,
        )
    )
    os.environ[PROCESS_ROLE_ENV] = title
    return title


__all__ = [
    "PROCESS_ROLE_ENV",
    "process_command",
    "process_title",
    "set_process_identity",
]
