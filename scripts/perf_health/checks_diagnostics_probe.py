"""Check 4: diagnostics-probe presence (the launchd-sampled ANLZ/deck-load probe)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .config import PROBE_INSTALL_DOC, PROBE_MAX_SAMPLE_AGE_HOURS, SEVERITY_OK, SEVERITY_WARN
from .models import CheckResult
from .util import _age_hours, _describe_age, _mtime


def check_diagnostics_probe(probe_dir: Path, now: datetime) -> CheckResult:
    window = f"newest sample within {PROBE_MAX_SAMPLE_AGE_HOURS:.0f}h"
    if not probe_dir.is_dir():
        return CheckResult(
            check="diagnostics-probe",
            severity=SEVERITY_WARN,
            classification="not-installed",
            detail=f"probe not installed - no directory at {probe_dir}, so 0 samples exist",
            window=window,
            remediation=f"probe not installed - see {PROBE_INSTALL_DOC} once PR #548 merges",
        )
    samples = sorted(probe_dir.glob("*.jsonl"))
    if not samples:
        return CheckResult(
            check="diagnostics-probe",
            severity=SEVERITY_WARN,
            classification="no-samples",
            detail=f"probe directory exists but holds 0 *.jsonl samples ({probe_dir})",
            window=window,
            sources=[str(probe_dir)],
            remediation=f"probe installed but never sampled - see {PROBE_INSTALL_DOC}",
        )
    newest = max(samples, key=lambda path: path.stat().st_mtime)
    age = _age_hours(_mtime(newest), now)
    detail = (
        f"newest of {len(samples)} sample files is {_describe_age(age)} ({newest.name})"
    )
    if age > PROBE_MAX_SAMPLE_AGE_HOURS:
        return CheckResult(
            check="diagnostics-probe",
            severity=SEVERITY_WARN,
            classification="stale-samples",
            detail=detail,
            window=window,
            sources=[str(newest)],
            remediation=(
                "the probe samples every 15s under launchd, so a stale newest sample "
                f"means it stopped - see {PROBE_INSTALL_DOC}"
            ),
        )
    return CheckResult(
        check="diagnostics-probe",
        severity=SEVERITY_OK,
        classification="healthy",
        detail=detail,
        window=window,
        sources=[str(newest)],
    )
