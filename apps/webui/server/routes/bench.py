"""Bench routes -- serve the demucs-farm KPI ledger to the admin panel.

``scripts/bench/kpi_ledger.json`` is the canonical, machine-appended record of
farm runs (written by ``scripts/bench/kpi_append.py``). This endpoint is the
only supported way for the app to read it: the browser never touches the file,
and an agent can curl the same JSON the UI renders.

Read-only on purpose. Appending a snapshot stays a CLI action
(``uv run scripts/bench/kpi_append.py --label <run> --set k=v --note "..."``)
so the ledger keeps one writer.

Requirements (mini-PRD):
  ✔︎ ✅ GET /bench/kpi: the ledger verbatim (kpis dict + snapshots array)
Acceptance:
  [if] the ledger file is absent [then ⛔️] 503 naming the missing path
  [if] the ledger is not valid JSON [then ⛔️] 500 naming the parse error
  [if] the ledger lacks 'kpis' or 'snapshots' [then ⛔️] 500 naming the key
  [if] the ledger is well-formed [then] 200 with kpis + snapshots intact
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/bench", tags=["bench"])

REPO_ROOT: Path = Path(__file__).resolve().parents[4]
# Module-level so tests can monkeypatch to a tmp ledger.
KPI_LEDGER_FILE: Path = REPO_ROOT / "scripts" / "bench" / "kpi_ledger.json"


@router.get("/kpi")
def get_kpi_ledger() -> dict[str, Any]:
    if not KPI_LEDGER_FILE.exists():
        raise HTTPException(
            status_code=503,
            detail={
                "code": "kpi_ledger_missing",
                "message": f"KPI ledger not found: {KPI_LEDGER_FILE}",
            },
        )
    try:
        ledger = json.loads(KPI_LEDGER_FILE.read_text())
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "code": "kpi_ledger_unparseable",
                "message": f"{KPI_LEDGER_FILE} is not valid JSON: {exc}",
            },
        ) from exc
    for key in ("kpis", "snapshots"):
        if key not in ledger:
            raise HTTPException(
                status_code=500,
                detail={
                    "code": "kpi_ledger_malformed",
                    "message": f"{KPI_LEDGER_FILE} missing '{key}'",
                },
            )
    return ledger
