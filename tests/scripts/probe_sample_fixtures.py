"""Real-schema probe records shared by the OpenDJ performance probe tests.

The record below is a sanitized subset of an actual 2026-08-21 Air probe
sample, kept in one place so the aggregation lane and the CLI lane cannot
drift onto two different ideas of what the probe writes.
"""

from __future__ import annotations

import json
from pathlib import Path


def captured_record(timestamp: str, total: float, webcontent: float) -> dict[str, object]:
    """Sanitized subset of the real 2026-08-21 Air probe schema."""

    return {
        "schema_version": 1,
        "kind": "sample",
        "timestamp": timestamp,
        "shell_pid": 712,
        "totals": {
            "physical_footprint_mb": total,
            "cpu_percent": 1.25,
            "process_count": 5,
        },
        "processes": [
            {"pid": 712, "role": "desktop-shell", "physical_footprint_mb": 32.0},
            {"pid": 905, "role": "python-engine", "physical_footprint_mb": 151.0},
            {"pid": 1183, "role": "webkit-webcontent", "physical_footprint_mb": webcontent},
        ],
        "machine": {"swap_used_mb": 2600.0},
        "engine": {"jobs": {"active": []}},
        "build": {
            "available": True,
            "git_sha": "146dfba0",
            "git_sha_full": "146dfba04408752e2f45a1e7ec2d206ca6384bde",
            "git_branch": "af--rebuild-agentB-from-Fable",
            "built_at_utc": "2026-08-19T16:43:03Z",
        },
    }


def write_trend_rows(directory: Path, rows: list[dict[str, object]]) -> Path:
    """Write one JSONL day file exactly as the sampling loop appends it."""

    path = directory / "opendj-performance-2026-08-21.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path
