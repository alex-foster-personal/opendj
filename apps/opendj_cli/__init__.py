"""AGENT-05: the ``opendj`` console script over the AGENT-03 command bus.

A thin client, and nothing else: orders go to ``POST /api/v1/commands`` on the
running engine, whose origin is read from the engine lock file
(``apps/engine_core/lock.py`` writes ``{pid, role, host, port, ...}``), never
guessed. There is no headless audio path and this package must not imply one:
the Web Audio engine is browser-owned, so an order is held until an open
performance page claims it.

Exit codes are the interface (``--json`` carries the same distinction in the
body as ``error.code``):

===  ==========================================================================
0    the order resolved and nothing in the mirror contradicts it: either
     ``confirmed`` (a control the verb names agrees) or ``accepted`` (the
     mirror names no control this command moves). The printed verdict says
     which, and ``--json`` carries it as ``verdict``; only ``confirmed``
     claims the effect was observed. See :mod:`confirm`.
1    the order ran and failed, or the invocation was rejected before dispatch
2    the engine is not running or its identity does not match the lock file
     (``engine_not_running`` or ``engine_identity_mismatch``); the message
     names the lock file that was checked
3    the engine is up and no performance page is open, so nothing can be claimed
4    the page accepted the order but the mirror never confirmed it
5    the engine did not answer before the deadline
===  ==========================================================================
"""

from __future__ import annotations

EXIT_CONFIRMED = 0
EXIT_FAILED = 1
EXIT_NO_ENGINE = 2
EXIT_NO_PAGE = 3
EXIT_UNCONFIRMED = 4
EXIT_TIMEOUT = 5
