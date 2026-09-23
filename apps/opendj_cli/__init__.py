"""AGENT-05: the ``opendj`` console script over the AGENT-03 command bus.

A thin client, and nothing else: orders go to ``POST /api/v1/commands`` on the
running engine, whose origin is read from the engine lock file
(``apps/engine_core/lock.py`` writes ``{pid, role, host, port, ...}``), never
guessed. There is no headless audio path and this package must not imply one:
the Web Audio engine is browser-owned, so an order is held until an open
performance page claims it.

Exit codes are the interface (``--json`` carries the same distinction in the
body as ``error.code``). One table for the whole ``opendj`` binary; codes 3 and
4 mean different things on bus verbs vs ``opendj api`` (disambiguate by
subcommand and message, not code alone).

Bus verbs (``play``, ``open``, ``status``, ``state``, …):

===  ==========================================================================
0    the order resolved and nothing in the mirror contradicts it: either
     ``confirmed`` (a control the verb names agrees) or ``accepted`` (the
     mirror names no control this command moves). The printed verdict says
     which, and ``--json`` carries it as ``verdict``; only ``confirmed``
     claims the effect was observed. See :mod:`confirm`.
1    the order ran and failed, or the invocation was rejected before dispatch
     (usage/argv errors also exit 1)
2    the engine is not running or its identity does not match the lock file
     (``engine_not_running`` or ``engine_identity_mismatch``); the message
     names the lock file that was checked
3    the engine is up and no performance page is open, so nothing can be claimed
4    the page accepted the order but the mirror never confirmed it
5    the engine did not answer before the deadline
===  ==========================================================================

``opendj api`` (HTTP escape hatch, issue #3776):

===  ==========================================================================
0    HTTP 2xx with a JSON body
1    HTTP error, safety refusal, usage/argv mistake, worktree dead port
     without lock, or non-JSON 2xx body
2    engine lock missing, identity mismatch, or lock-resolved origin unreachable
3    HTTP 412 precondition failed
4    HTTP 409 conflict
===  ==========================================================================
"""

from __future__ import annotations

EXIT_CONFIRMED = 0
EXIT_FAILED = 1
EXIT_NO_ENGINE = 2
EXIT_NO_PAGE = 3
EXIT_UNCONFIRMED = 4
EXIT_TIMEOUT = 5

# ``opendj api`` aliases and HTTP-specific codes (numeric overlap with bus
# codes 3/4 is intentional; subcommand disambiguates).
EXIT_OK = EXIT_CONFIRMED
EXIT_PRECONDITION = EXIT_NO_PAGE
EXIT_CONFLICT = EXIT_UNCONFIRMED
