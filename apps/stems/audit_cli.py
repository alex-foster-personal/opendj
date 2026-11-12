"""STEM-10946: read-only audit-quality CLI handler (stdout/stderr contract).

``python -m apps.stems.cli audit-quality --data-dir DIR [--json]``

-Cursor
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from apps.stems.stem_set_audit import AuditError, run_audit


def run_audit_quality(*, data_dir: Path, json_output: bool) -> int:
    """Run the stems store audit and print results. Exit 0 iff all sets pass."""
    from apps.stems.cli import stems_dir

    try:
        report = run_audit(stems_dir(data_dir))
    except AuditError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if json_output:
        print(json.dumps(report.to_json(), indent=2))
    else:
        for row in report.rows:
            axis = f" [{row.axis}]" if row.axis else ""
            print(f"{row.status:<10} {row.stable_id}{axis} {row.reason}")
        print(report.summary_line())
    return 0 if report.passed else 1
