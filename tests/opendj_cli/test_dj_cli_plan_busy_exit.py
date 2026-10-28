"""``opendj dj schedule`` exit codes for a refused plan (AGENT-29)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.opendj_cli import EXIT_UNCONFIRMED
from apps.opendj_cli.dj_cli import run
from apps.opendj_cli.dj_service import DjError


class _BusyService:
    def schedule(self, plan: dict[str, object]) -> dict[str, object]:
        raise DjError("plan_busy", "plan plan-one is running; abort or finish it first")


@pytest.mark.requirement("AGENT-29")
def test_schedule_refused_as_plan_busy_exits_4_like_the_other_refusals(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] schedule meets a running plan [then] it exits 4 with the plan_busy document, [else stop]."""
    plan = tmp_path / "plan.json"
    plan.write_text("{}", encoding="utf-8")
    argv = ["schedule", str(plan)]
    code = run(argv, lock=None, service_factory=lambda _lock: _BusyService())  # type: ignore[arg-type, return-value]
    assert code == EXIT_UNCONFIRMED
    document = json.loads(capsys.readouterr().out)
    assert document["error"] == "plan_busy"
