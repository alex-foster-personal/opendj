"""Shared KPI ledger row context and builder for S13 login and BOOT-LIB captures.

Provenance is passed in via ``LedgerProvenance``, never read from this module, because
S13 stamps ``issue-1885`` and BOOT-LIB stamps ``issue-2697``.

Supersedes: ``scripts.perf.capture_ledger._LedgerRowCtx``,
``scripts.perf.capture_ledger._base_row``,
``scripts.perf.capture_boot_library_ledger._LedgerRowCtx``,
``scripts.perf.capture_boot_library_ledger._base_row``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any


@dataclass(frozen=True)
class LedgerRowCtx:
    sha: str
    machine: str
    capture_date: date
    note: str
    status: str | None = None
    measured: bool | None = None


@dataclass(frozen=True)
class LedgerProvenance:
    round: str
    source: str
    method: str
    capture_id: str


def base_row(
    kpi: str,
    value: float | None,
    unit: str,
    ctx: LedgerRowCtx,
    provenance: LedgerProvenance,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "date": ctx.capture_date.isoformat(),
        "round": provenance.round,
        "kpi": kpi,
        "value": value,
        "unit": unit,
        "machine": ctx.machine,
        "source": provenance.source,
        "method": provenance.method,
        "sha": ctx.sha,
        "capture_id": provenance.capture_id,
        "note": ctx.note,
    }
    if ctx.status is not None:
        row["status"] = ctx.status
    if ctx.measured is not None:
        row["measured"] = ctx.measured
    return row
