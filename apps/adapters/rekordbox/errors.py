"""The adapter's failure vocabulary, and the read-only connection opener.

Moved verbatim from ``apps/webui/server/rb_vendor.py`` C1 (source lines
201-223 on ``af--t4-design``) per ``.planning/t3b-decomposition-map.md``
section 2 target #3 -- the module the map nominates, holding the code that
was there, but NOT yet holding the change the map wants made to it.

The map's target #3 is a *domain* error type: an adapter that imports a web
framework cannot be driven by the CLI, the jobs runner, or a conformance
harness, and the HTTP layer should own one code-to-status mapping table
(map section 1, "Two layering violations to fix while splitting", item 1).
That swap is assigned to slice S0, which built none of its four modules;
wave 4 extracted them so the rest of the split could proceed.

Doing the swap here would change the exception type of every 404/409/422/428
this adapter raises, which ``tests/webui/test_rb_hot_cue_write.py`` (513
lines) and the route tests assert on directly. That is a behavioural change
with its own test surface, not a relocation, so it stays a named debt: until
it lands, the decomposition's "no fastapi import anywhere under adapters/"
clause is unmet and this module is why.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi import HTTPException


def not_found(code: str, message: str) -> HTTPException:
    """404 with the explicit {code, message} detail shape (COMPONENT-MAP 2)."""
    return HTTPException(status_code=404, detail={"code": code, "message": message})


def unavailable(code: str, message: str) -> HTTPException:
    """503 for a capability that is missing, not a fact that is absent.

    Distinct from :func:`not_found`: a 404 asserts "this does not exist", which
    is a verdict the caller cannot honestly reach when the tool needed to check
    is itself missing (verification.md: report UNKNOWN, never a guessed
    verdict). Use this when an optional runtime dependency blocks the check.
    """
    return HTTPException(status_code=503, detail={"code": code, "message": message})


def _open_ro(path: Path, label: str) -> sqlite3.Connection:
    """Open ``path`` read-only, or fail loudly if it is not on disk.

    One home, as of wave 4. C1 and the C4 cue reader each carried a copy --
    the map lists ``_open_ro`` under both target #4 (paths.py) and target #9
    (db.py) and ``rb_vendor_pkg/db.py``'s docstring recorded the duplication
    as intentional-until-S8. This is S8.
    """
    if not path.exists():
        raise HTTPException(
            status_code=500,
            detail={
                "code": f"{label}_UNAVAILABLE",
                "message": f"required database missing on disk: {path}",
            },
        )
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only = ON")
    return conn


__all__ = ["_open_ro", "not_found"]
