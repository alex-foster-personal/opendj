"""Production frontend builds are shared once per commit (issue #1513).

Regression lines:
  - if a consumer job runs `pnpm build` after this lands then the saving is gone
  - if the artifact stamp is missing or mismatched then the consumer must fail
  - if e2e cannot read CI's artifact then it must fail, never rebuild locally
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
STAMP = REPO_ROOT / "scripts" / "ci_frontend_build_stamp.sh"
ASSERT = REPO_ROOT / "scripts" / "ci_frontend_build_assert.sh"
ACQUIRE = REPO_ROOT / "scripts" / "ci_frontend_build_acquire_from_ci.sh"
SHA = "a" * 40


def _steps(workflow: str, job_id: str) -> list[dict]:
    doc = yaml.safe_load((WORKFLOWS / workflow).read_text(encoding="utf-8"))
    return doc["jobs"][job_id]["steps"]


def _run_builds(workflow: str) -> list[tuple[str, str]]:
    doc = yaml.safe_load((WORKFLOWS / workflow).read_text(encoding="utf-8"))
    hits: list[tuple[str, str]] = []
    for job_id, job in doc["jobs"].items():
        for step in job.get("steps") or []:
            run = step.get("run") or ""
            if "pnpm build" in run and job_id != "frontend-build":
                hits.append((job_id, step.get("name") or run.splitlines()[0]))
    return hits


def test_stamp_and_assert_round_trip(tmp_path: Path) -> None:
    """if the stamp and assert disagree then stale artifacts would pass"""
    build_dir = tmp_path / "build"
    subprocess.run([str(STAMP), SHA, str(build_dir)], check=True)
    (build_dir / "index.html").write_text("<html></html>", encoding="utf-8")
    subprocess.run([str(ASSERT), SHA, str(build_dir)], check=True)
    bad = subprocess.run([str(ASSERT), "b" * 40, str(build_dir)], check=False)
    assert bad.returncode == 1


def test_assert_fails_when_stamp_is_missing(tmp_path: Path) -> None:
    """if the stamp is absent then the consumer must fail loudly"""
    build_dir = tmp_path / "build"
    build_dir.mkdir()
    (build_dir / "index.html").write_text("<html></html>", encoding="utf-8")
    result = subprocess.run([str(ASSERT), SHA, str(build_dir)], check=False)
    assert result.returncode == 1


def test_acquire_refuses_without_ci_artifact(tmp_path: Path, monkeypatch) -> None:
    """if CI never published the artifact then e2e must not build locally"""
    monkeypatch.setenv("GITHUB_REPOSITORY", "maintainer/music-dj-tools")
    monkeypatch.setenv("MDT_CI_FRONTEND_ARTIFACT_WAIT_S", "1")
    monkeypatch.setenv("MDT_CI_FRONTEND_ARTIFACT_POLL_S", "1")
    dest = tmp_path / "build"
    result = subprocess.run(
        [str(ACQUIRE), SHA, str(dest)],
        env={**os.environ, "PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "refusing to build locally" in result.stdout + result.stderr


def test_ci_builds_once_and_consumers_download() -> None:
    """if ci.yml still builds outside frontend-build then sharing failed"""
    assert _run_builds("ci.yml") == []
    build_steps = [
        s.get("run") or ""
        for s in _steps("ci.yml", "frontend-build")
        if "pnpm build" in (s.get("run") or "")
    ]
    assert len(build_steps) == 1
    consumer = _steps("ci.yml", "frontend")
    assert any("download-artifact" in (s.get("uses") or "") for s in consumer)
    assert any("ci_frontend_build_assert.sh" in (s.get("run") or "") for s in consumer)


def test_e2e_builds_never_and_acquires_from_ci() -> None:
    """if e2e gate or extended rebuilds then the workflow still pays twice"""
    assert _run_builds("e2e.yml") == []
    for job_id in ("gate", "extended"):
        steps = _steps("e2e.yml", job_id)
        assert any(
            "ci_frontend_build_acquire_from_ci.sh" in (s.get("run") or "") for s in steps
        ), job_id


def test_artifact_names_are_keyed_on_sha() -> None:
    """if the artifact name omits the sha then a stale download can pass"""
    uploads = [
        s
        for s in _steps("ci.yml", "frontend-build")
        if "upload-artifact" in (s.get("uses") or "")
    ]
    assert uploads
    assert uploads[0]["with"]["name"] == "production-frontend-${{ github.sha }}"
