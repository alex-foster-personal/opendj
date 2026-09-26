"""Environment for driving the real perf KPI nightly job in tests.

Shared by `test_perf_kpi_job.py` and `test_perf_kpi_job_publish.py`: a free
local port, an empty schema-valid ledger, and the MDT_PERF_KPI_* environment
`load_config` reads. Nothing is faked; these only point real config at temp paths.

Supersedes: nothing on main; these helpers were private to
`test_perf_kpi_job.py` within PR #3827 and are deleted there.

-Claude
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def empty_ledger(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"schema_version": 2, "entries": []}, indent=2) + "\n",
        encoding="utf-8",
    )


def nightly_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **overrides: str) -> Path:
    state_dir = tmp_path / "state"
    ledger = tmp_path / "kpi-ledger.json"
    empty_ledger(ledger)
    values = {
        "MDT_PERF_KPI_STATE_DIR": str(state_dir),
        "MDT_PERF_KPI_LEDGER": str(ledger),
        "MDT_PERF_KPI_SCRATCH_PORT": str(free_port()),
        "MDT_PERF_KPI_SAMPLES": "2",
        "MDT_PERF_KPI_MACHINE": "test",
        **overrides,
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return state_dir
