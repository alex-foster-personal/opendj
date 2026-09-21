"""Constants and environment reads for the ``dispatch`` MCP server.

Every value a caller could otherwise smuggle a shell fragment through is
pinned here as a literal or validated against an allowlist. The server takes
NO free-form command from a caller, on purpose: a connector that can run an
arbitrary command on nucbox is a remote shell for anyone who can reach the
connector, which is a different -- and much larger -- grant than "read the
queue and add work to it".
"""

from __future__ import annotations

import os
from collections.abc import Mapping

# The queue IS GitHub Issues on this repository (.agents/skills/nucbox-job-queue).
QUEUE_REPO = "maintainer/music-dj-tools"

# ssh alias only. Host, port and user live in ~/.ssh/config and never in this
# repo (nucbox-job-queue SKILL.md, "Ops quick reference").
NUCBOX_SSH_HOST = "nucbox-wsl"

GH_TIMEOUT_S = 25.0
SSH_TIMEOUT_S = 20.0
SSH_CONNECT_TIMEOUT_S = 8
LEDGER_TIMEOUT_S = 10.0

# Queue label vocabulary. A caller may only name labels from these sets; the
# server refuses anything else rather than passing it to `gh`.
QUEUE_STATES: tuple[str, ...] = ("ready", "running", "review", "blocked", "done")
QUEUE_PRIORITIES: tuple[str, ...] = ("p0", "p1", "p2")
QUEUE_SHAPES: tuple[str, ...] = ("hard", "big")

# Bounds. MCP tool results are budgeted client-side (Claude Code's default
# MAX_MCP_OUTPUT_TOKENS is 25k), so every list surface is capped here rather
# than trusting a caller to ask for a sane page.
MAX_QUEUE_ITEMS = 50
DEFAULT_QUEUE_ITEMS = 20
MAX_LOG_LINES = 200
DEFAULT_LOG_LINES = 40
MAX_BODY_CHARS = 8_000

# Writes that take something over from another agent (a running claim, another
# fleet's ledger node) stay behind this flag. Reading the queue and ADDING to
# it are the point of the connector and need no flag.
ENABLE_TAKEOVER_ENV = "DISPATCH_MCP_ENABLE_TAKEOVER"

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def takeover_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """True when this process may overwrite another agent's claim."""
    env = os.environ if environ is None else environ
    return env.get(ENABLE_TAKEOVER_ENV, "").strip().lower() in _TRUTHY


def queue_label(kind: str, value: str) -> str:
    """Return a ``queue:<value>`` label, or raise for a value off the allowlist.

    ``kind`` names which allowlist applies and appears in the refusal, so a
    caller sees which vocabulary it missed rather than a bare rejection.
    """
    allowed = {
        "state": QUEUE_STATES,
        "priority": QUEUE_PRIORITIES,
        "shape": QUEUE_SHAPES,
    }[kind]
    if value not in allowed:
        raise ValueError(f"unknown queue {kind} {value!r}; allowed: {', '.join(allowed)}")
    return f"queue:{value}"


def clamp(value: int | None, default: int, maximum: int) -> int:
    """Clamp a caller-supplied count into ``1..maximum``, defaulting when unset."""
    if value is None:
        return default
    return max(1, min(int(value), maximum))
