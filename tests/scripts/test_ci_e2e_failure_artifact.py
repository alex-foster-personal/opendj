"""The e2e failure artifact carries the engine's own logs, not just Playwright's.

Twice on Sat 5 Sep 2026 (agentbox-2 18:16 UTC, agentbox-7 18:27 UTC) the Root
Playwright suite failed with "analysis stopped after a failure that is not
about these files (apps.analysis.run exited 5)". The traceback lives in the
engine's data dir under the suite's fixture root, which the failure artifact
did not include, and the next job's checkout cleaned it before anyone could
read it. A failure the artifact cannot explain is a failure that gets rerun.

Regression line:
  - if the failure artifact drops the fixture data logs then the next
    analysis crash in e2e is undiagnosable again
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
E2E = REPO_ROOT / ".github" / "workflows" / "e2e.yml"
ENGINE_LOGS = "apps/webui/frontend/tests/e2e/fixtures/root-playwright-data/logs/**"


def test_e2e_gate_failure_artifact_includes_the_engine_logs() -> None:
    """if the artifact omits the engine logs then an exit-5 analysis crash has no trace"""
    doc = yaml.safe_load(E2E.read_text(encoding="utf-8"))
    uploads = [
        step
        for step in doc["jobs"]["gate"]["steps"]
        if "upload-artifact" in (step.get("uses") or "")
        and (step.get("with") or {}).get("name") == "e2e-gate-failures"
    ]
    assert len(uploads) == 1, "exactly one e2e-gate-failures upload"
    paths = uploads[0]["with"]["path"].split()
    assert ENGINE_LOGS in paths, f"{ENGINE_LOGS} missing from {paths}"
