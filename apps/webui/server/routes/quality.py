"""Quality ratchet routes -- read-only view of ops/quality/baseline.json.

``ops/quality/baseline.json`` is the merge gate's allowance table (see
``ops/quality/README.md``): ``just quality`` fails any commit that makes a
tracked metric worse than the number recorded here, and only a ratchet-down
(``just quality-baseline``) is allowed to move it, and only downward. That
means the file is never staler than the last commit that touched the branch
it lives on -- it is the live gate state, not a vendored snapshot -- so this
route serves it verbatim, the same read-only pattern GET /bench/kpi already
uses for the farm ledger.

Requirements (mini-PRD):
  ✔︎ ✅ GET /admin/quality-ratchet: the current allowance table (generated
  timestamp + flat metrics dict), verbatim from ops/quality/baseline.json.
Acceptance:
  [if] the baseline file is absent [then ⛔️] 503 naming the missing path
  [if] the baseline is not valid JSON [then ⛔️] 500 naming the parse error
  [if] the baseline lacks 'generated' or 'metrics' [then ⛔️] 500 naming the key
  [if] the baseline is well-formed [then] 200 with generated + metrics intact,
       and nothing else from the file (the 'burn_down' narrative is for
       humans reading the repo, not for the panel)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/admin", tags=["admin"])

REPO_ROOT: Path = Path(__file__).resolve().parents[4]
# Module-level so tests can monkeypatch to a tmp baseline.
QUALITY_BASELINE_FILE: Path = REPO_ROOT / "ops" / "quality" / "baseline.json"


@router.get("/quality-ratchet")
def get_quality_ratchet() -> dict[str, Any]:
    if not QUALITY_BASELINE_FILE.exists():
        raise HTTPException(
            status_code=503,
            detail={
                "code": "quality_baseline_missing",
                "message": f"quality baseline not found: {QUALITY_BASELINE_FILE}",
            },
        )
    try:
        baseline = json.loads(QUALITY_BASELINE_FILE.read_text())
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "code": "quality_baseline_unparseable",
                "message": f"{QUALITY_BASELINE_FILE} is not valid JSON: {exc}",
            },
        ) from exc
    for key in ("generated", "metrics"):
        if key not in baseline:
            raise HTTPException(
                status_code=500,
                detail={
                    "code": "quality_baseline_malformed",
                    "message": f"{QUALITY_BASELINE_FILE} missing '{key}'",
                },
            )
    return {"generated": baseline["generated"], "metrics": baseline["metrics"]}
