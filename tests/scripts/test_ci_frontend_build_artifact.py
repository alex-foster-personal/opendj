"""Production frontend builds are shared once per commit (issue #1513).

Regression lines:
  - if a consumer job runs `pnpm build` after this lands then the saving is gone
  - if the artifact stamp is missing or mismatched then the consumer must fail
  - if CI's own build job is queued/not-started/over-budget then e2e must build
    locally rather than burn its own gate minutes waiting (issue: none, #2665
    trunk was green; measured Mon 14 Sep 2026, see
    docs/decisions/ADR-0028-e2e-frontend-artifact-local-build.md)
  - if CI's own build job failed/cancelled/timed-out then e2e must refuse,
    never rebuild over a real CI failure
  - if CI's state cannot be read at all then e2e must refuse, never guess
"""

from __future__ import annotations

import os
import subprocess
import zipfile
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
STAMP = REPO_ROOT / "scripts" / "ci_frontend_build_stamp.sh"
ASSERT = REPO_ROOT / "scripts" / "ci_frontend_build_assert.sh"
ACQUIRE = REPO_ROOT / "scripts" / "ci_frontend_build_acquire_from_ci.sh"
SHA = "a" * 40
REPO_SLUG = "private_owner/music-dj-tools"

# Fake `gh` used for the CI-job-state tests below. Behavior is entirely
# env-var driven (read at run time) so one script content covers every
# scenario; `argv` pattern-matching mirrors how the real acquire script
# only ever calls three `gh api` shapes.
_FAKE_GH = """#!/usr/bin/env bash
set -euo pipefail
argv="$*"
case "$argv" in
  *"actions/artifacts?name="*)
    count_file="${FAKE_GH_STATE_DIR:?FAKE_GH_STATE_DIR not set}/artifact_poll_count"
    count=0
    [ -f "$count_file" ] && count="$(cat "$count_file")"
    count=$((count + 1))
    printf '%s' "$count" > "$count_file"
    ready_after="${FAKE_GH_ARTIFACT_READY_AFTER:-999999}"
    if [ "$count" -ge "$ready_after" ]; then
      printf '%s\\n' "${FAKE_GH_ARTIFACT_ID:-fake-artifact-id}"
    fi
    exit 0
    ;;
  *"actions/artifacts/"*"/zip"*)
    cat "${FAKE_GH_ARTIFACT_ZIP:?FAKE_GH_ARTIFACT_ZIP not set}"
    exit 0
    ;;
  *"actions/runs?head_sha="*)
    case "${FAKE_GH_RUNS_MODE:-none}" in
      none) exit 0 ;;
      fail) exit 4 ;;
      found) printf '%s\\n' "${FAKE_GH_RUN_ID:-999}" ;;
    esac
    exit 0
    ;;
  *"actions/runs/"*"/jobs"*)
    case "${FAKE_GH_JOBS_MODE:-empty}" in
      empty) exit 0 ;;
      fail) exit 4 ;;
      found) printf '%s\\t%s\\n' "${FAKE_GH_JOB_STATUS:-queued}" "${FAKE_GH_JOB_CONCLUSION:-}" ;;
    esac
    exit 0
    ;;
  *)
    echo "fake gh: unhandled invocation: $argv" >&2
    exit 4
    ;;
esac
"""

# Fake local-build script: writes a recognizable build plus a marker file so
# tests can assert PRESENCE of an invocation, not merely absence of an error.
_FAKE_LOCAL_BUILD = """#!/usr/bin/env bash
set -euo pipefail
sha="$1"
dest="${2:-apps/webui/frontend/build}"
mkdir -p "$dest"
printf '<html>fake local build</html>' > "$dest/index.html"
printf '%s\\n' "$sha" > "$dest/.ci-production-frontend-sha"
: >> "${FAKE_LOCAL_BUILD_MARKER:?FAKE_LOCAL_BUILD_MARKER not set}"
"""


def _write_executable(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)
    return path


def _fake_artifact_zip(tmp_path: Path, sha: str) -> Path:
    zip_path = tmp_path / "fake-artifact.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("index.html", "<html>real ci build</html>")
        zf.writestr(".ci-production-frontend-sha", f"{sha}\n")
    return zip_path


def _acquire_env(tmp_path: Path, **overrides: str) -> dict[str, str]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    _write_executable(fake_bin / "gh", _FAKE_GH)
    state_dir = tmp_path / "gh-state"
    state_dir.mkdir(exist_ok=True)
    local_build_marker = tmp_path / "local-build-invoked"
    local_build_script = _write_executable(
        tmp_path / "fake_local_build.sh", _FAKE_LOCAL_BUILD
    )
    env = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "GITHUB_REPOSITORY": REPO_SLUG,
        "MDT_CI_FRONTEND_ARTIFACT_WAIT_S": "5",
        "MDT_CI_FRONTEND_ARTIFACT_POLL_S": "1",
        "MDT_CI_FRONTEND_BUILD_LOCAL_SCRIPT": str(local_build_script),
        "FAKE_GH_STATE_DIR": str(state_dir),
        "FAKE_LOCAL_BUILD_MARKER": str(local_build_marker),
    }
    env.pop("GH_TOKEN", None)
    env["GH_TOKEN"] = "fake-token-for-test"
    env.update(overrides)
    return env


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


def test_acquire_refuses_when_ci_state_is_unreadable(tmp_path: Path, monkeypatch) -> None:
    """[if] CI's state cannot be read at all then e2e must refuse, never guess [else stop].

    Updated for the new rule (was: "CI never published the artifact then e2e
    must not build locally"). gh exit 4 is "authentication required": the
    fast tier job has no token, so a live `gh api` dies before any
    refuse message unless acquire fail-closes on EVERY call, not just the
    artifact-list one. Pin that path with a real subprocess, not the
    developer's logged-in gh (head ed73bf4f7 asserted 4 == 1).
    """
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_gh = fake_bin / "gh"
    fake_gh.write_text("#!/usr/bin/env bash\nexit 4\n", encoding="utf-8")
    fake_gh.chmod(0o755)
    local_build_marker = tmp_path / "local-build-invoked"
    local_build_script = _write_executable(
        tmp_path / "fake_local_build.sh", _FAKE_LOCAL_BUILD
    )
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO_SLUG)
    monkeypatch.setenv("MDT_CI_FRONTEND_ARTIFACT_WAIT_S", "1")
    monkeypatch.setenv("MDT_CI_FRONTEND_ARTIFACT_POLL_S", "1")
    monkeypatch.setenv("MDT_CI_FRONTEND_BUILD_LOCAL_SCRIPT", str(local_build_script))
    monkeypatch.setenv("FAKE_LOCAL_BUILD_MARKER", str(local_build_marker))
    dest = tmp_path / "build"
    result = subprocess.run(
        [str(ACQUIRE), SHA, str(dest)],
        env={**os.environ},
        capture_output=True,
        text=True,
        check=False,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "unreadable" in output.lower(), output
    assert "refusing to build locally" in output, output
    assert not local_build_marker.exists(), "unreadable CI state must never trigger a local build"


def test_acquire_builds_locally_when_ci_job_is_queued(tmp_path: Path) -> None:
    """[if] CI's own build job is queued (or not started) then e2e builds locally [else stop].

    This is the vacuous-red case measured Mon 14 Sep 2026: 15 of 17 failed
    `gate` jobs burned their whole 900s budget polling for an artifact whose
    CI build job had not even started. The artifact never appears (ready_after
    left at the default "never"), so this also proves the fix does not wait
    out the budget when the job's own state already says "not going to
    finish in time".
    """
    env = _acquire_env(
        tmp_path,
        FAKE_GH_RUNS_MODE="found",
        FAKE_GH_JOBS_MODE="found",
        FAKE_GH_JOB_STATUS="queued",
    )
    dest = tmp_path / "build"
    result = subprocess.run(
        [str(ACQUIRE), SHA, str(dest)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert Path(env["FAKE_LOCAL_BUILD_MARKER"]).exists(), "local build was never invoked"
    assert (dest / "index.html").read_text(encoding="utf-8") == "<html>fake local build</html>"
    assert (dest / ".ci-production-frontend-sha").read_text(encoding="utf-8").strip() == SHA
    assert "::warning::" in output
    assert "queued" in output.lower()


def test_acquire_refuses_when_ci_job_failed(tmp_path: Path) -> None:
    """[if] CI's own build job failed/cancelled/timed-out then e2e refuses [else stop].

    Never rebuild over a real CI failure: masking a genuine build break as a
    passing local rebuild would hide the actual defect from the gate.
    """
    env = _acquire_env(
        tmp_path,
        FAKE_GH_RUNS_MODE="found",
        FAKE_GH_JOBS_MODE="found",
        FAKE_GH_JOB_STATUS="completed",
        FAKE_GH_JOB_CONCLUSION="failure",
    )
    dest = tmp_path / "build"
    result = subprocess.run(
        [str(ACQUIRE), SHA, str(dest)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "::error::" in output
    assert "failure" in output.lower()
    assert not Path(env["FAKE_LOCAL_BUILD_MARKER"]).exists(), (
        "a real CI build failure must never trigger a local rebuild"
    )


def test_acquire_downloads_when_ci_job_in_progress_then_artifact_appears(
    tmp_path: Path,
) -> None:
    """[if] CI's build job is in progress and then publishes then e2e downloads it [else stop].

    Local build must NOT fire here: the job is on track, so the whole point
    of sharing one build per commit (issue #1513) still holds. Ready-after=2
    means the artifact is absent on the first poll and present on the
    second, forcing the acquire script through one real poll/sleep cycle.
    """
    zip_path = _fake_artifact_zip(tmp_path, SHA)
    env = _acquire_env(
        tmp_path,
        FAKE_GH_RUNS_MODE="found",
        FAKE_GH_JOBS_MODE="found",
        FAKE_GH_JOB_STATUS="in_progress",
        FAKE_GH_ARTIFACT_READY_AFTER="2",
        FAKE_GH_ARTIFACT_ZIP=str(zip_path),
    )
    dest = tmp_path / "build"
    result = subprocess.run(
        [str(ACQUIRE), SHA, str(dest)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert (dest / "index.html").read_text(encoding="utf-8") == "<html>real ci build</html>"
    assert not Path(env["FAKE_LOCAL_BUILD_MARKER"]).exists(), (
        "an in-progress CI job that then publishes must download, never build locally"
    )


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
    # upload-artifact v4 skips dotfiles unless told otherwise. The stamp is
    # `.ci-production-frontend-sha`; without this the zip has index.html and
    # e2e assert fails "stamp is missing" (head ed73bf4f7, job 103219857971).
    assert uploads[0]["with"].get("include-hidden-files") is True, (
        ".ci-production-frontend-sha is a dotfile; upload-artifact v4 skips it otherwise"
    )
