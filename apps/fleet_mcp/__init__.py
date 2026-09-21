"""Fleet dispatch MCP server (AGENT-15).

One stdio MCP server, named ``dispatch``, that gives every agent working in
this checkout -- Claude Code, Codex, or anything else that speaks MCP -- the
same first-class view of the build fleet: the GitHub-issues queue, the nucbox
dispatcher's live health, and the fan-out progress ledger.

The runtime it reports on is NOT in this repository. The dispatcher and its
residents live in ``maintainer/nucbox-jobs``, deployed to ``~/jobs``
on ``nucbox-wsl`` (``docs/ops/nucbox-fleet.md``). This package is a READ and
ENQUEUE surface over that system; it never edits it.
"""

from __future__ import annotations

SERVER_NAME = "dispatch"
SERVER_VERSION = "0.1.0"

__all__ = ["SERVER_NAME", "SERVER_VERSION"]
