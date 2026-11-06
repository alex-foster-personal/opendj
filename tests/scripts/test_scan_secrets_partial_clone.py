"""Python materialize helper for trufflehog file:// on partial clones (issue #4224, #8613).

Real `--filter=blob:none` fixtures; no mocked git output. Slow cases use the same
HTTP git backend as tests/quality_gate_base_compare_partial_clone.py.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
import yaml

from scripts import quality_gate as qg
from tests.quality_gate_base_compare_partial_clone import (
    _TOKEN,
    _auth_gated_origin,
    _build_partial_clone,
    _git,
)

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / "scripts" / "security" / "lib.sh"
MATERIALIZE = ROOT / "scripts" / "security" / "materialize.py"
SCAN_SECRETS = ROOT / "scripts" / "security" / "scan_secrets.py"
SECURITY_YML = ROOT / ".github" / "workflows" / "security.yml"


def _run_materialize(repo: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    run_env = os.environ.copy()
    if env:
        run_env.update(env)
    return subprocess.run(
        [sys.executable, "-m", "scripts.security.materialize", str(repo)],
        cwd=str(ROOT),
        env=run_env,
        capture_output=True,
        text=True,
        check=False,
    )


def _missing_print_oids(repo: Path) -> list[str]:
    git = shutil.which("git")
    assert git is not None
    result = subprocess.run(
        [
            git,
            "-C",
            str(repo),
            "rev-list",
            "--objects",
            "--all",
            "--missing=print",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return [
        line[1:].split()[0]
        for line in result.stdout.splitlines()
        if line.startswith("?")
    ]


@contextmanager
def _http_partial_clone(tmp_path: Path) -> Iterator[Path]:
    """Partial clone with http(s) promisor, matching the pool checkout shape."""
    srv_root, _, _ = _build_partial_clone(tmp_path)
    with _auth_gated_origin(srv_root, _TOKEN) as (origin_url, prefix):
        partial = tmp_path / "partial"
        subprocess.run(
            [
                "git",
                "-c",
                f"http.{prefix}.extraheader=AUTHORIZATION: basic {qg._git_basic_credential(_TOKEN)}",
                "-c",
                "protocol.version=2",
                "clone",
                "-q",
                "--filter=blob:none",
                "--",
                origin_url,
                str(partial),
            ],
            check=True,
            capture_output=True,
        )
        yield partial


def test_file_clone_of_partial_reproduces_upload_pack_failure(tmp_path: Path) -> None:
    with _http_partial_clone(tmp_path) as partial:
        copy = tmp_path / "copy"
        result = subprocess.run(
            ["git", "clone", f"file://{partial.resolve()}", str(copy)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 128, result.stderr
        assert "lazy fetching disabled" in result.stderr


def test_materialize_then_file_clone_succeeds(tmp_path: Path) -> None:
    with _http_partial_clone(tmp_path) as partial:
        mat = _run_materialize(partial, env={"GH_TOKEN": _TOKEN})
        assert mat.returncode == 0, mat.stderr
        copy = tmp_path / "copy"
        result = subprocess.run(
            ["git", "clone", f"file://{partial.resolve()}", str(copy)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert (copy / "track.txt").read_text(encoding="utf-8") == "v2\n"


def test_materialize_leaves_cat_file_batch_check_without_missing(tmp_path: Path) -> None:
    with _http_partial_clone(tmp_path) as partial:
        before = _missing_print_oids(partial)
        assert before, "fixture must actually be blobless"
        mat = _run_materialize(partial, env={"GH_TOKEN": _TOKEN})
        assert mat.returncode == 0, mat.stderr
        assert _missing_print_oids(partial) == []


def test_materialize_fails_when_git_not_on_path(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    env = os.environ.copy()
    env["PATH"] = str(empty)
    result = subprocess.run(
        [sys.executable, "-m", "scripts.security.materialize", str(tmp_path / "repo")],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "git" in result.stderr.lower()


def test_lib_sh_thin_caller_no_bash_c() -> None:
    text = LIB.read_text(encoding="utf-8")
    assert "bash -c" not in text
    assert "materialize.py" in text
    assert "sec_materialize_missing_objects" in text


def test_full_clone_is_noop(tmp_path: Path) -> None:
    srv = tmp_path / "srv"
    seed = srv / "seed"
    seed.mkdir(parents=True)
    _git(seed, "init", "-q", "-b", "main")
    _git(seed, "config", "user.email", "test@example.com")
    _git(seed, "config", "user.name", "Test")
    (seed / "f.txt").write_text("x\n", encoding="utf-8")
    _git(seed, "add", "f.txt")
    _git(seed, "commit", "-q", "-m", "c1")
    bare = srv / "origin.git"
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(seed), str(bare)],
        check=True,
        capture_output=True,
    )
    full = tmp_path / "full"
    subprocess.run(
        ["git", "clone", "-q", bare.resolve().as_uri(), str(full)],
        check=True,
        capture_output=True,
    )
    mat = _run_materialize(
        full,
        env={"GH_TOKEN": "", "GITHUB_TOKEN": ""},
    )
    assert mat.returncode == 0, mat.stderr
    copy = tmp_path / "copy"
    result = subprocess.run(
        ["git", "clone", f"file://{full.resolve()}", str(copy)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.slow
def test_http_origin_without_token_is_unknown(tmp_path: Path) -> None:
    srv_root, _, _ = _build_partial_clone(tmp_path)
    with _auth_gated_origin(srv_root, _TOKEN) as (origin_url, prefix):
        partial = tmp_path / "partial"
        subprocess.run(
            [
                "git",
                "-c",
                f"http.{prefix}.extraheader=AUTHORIZATION: basic {qg._git_basic_credential(_TOKEN)}",
                "-c",
                "protocol.version=2",
                "clone",
                "-q",
                "--filter=blob:none",
                "--",
                origin_url,
                str(partial),
            ],
            check=True,
            capture_output=True,
        )
        env = {k: v for k, v in os.environ.items() if k not in ("GH_TOKEN", "GITHUB_TOKEN")}
        mat = subprocess.run(
            [sys.executable, "-m", "scripts.security.materialize", str(partial)],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert mat.returncode == 2, mat.stderr
        assert "GH_TOKEN" in mat.stderr or "GITHUB_TOKEN" in mat.stderr


@pytest.mark.slow
def test_http_origin_with_gh_token_materializes(tmp_path: Path) -> None:
    srv_root, _, _ = _build_partial_clone(tmp_path)
    with _auth_gated_origin(srv_root, _TOKEN) as (origin_url, prefix):
        partial = tmp_path / "partial"
        subprocess.run(
            [
                "git",
                "-c",
                f"http.{prefix}.extraheader=AUTHORIZATION: basic {qg._git_basic_credential(_TOKEN)}",
                "-c",
                "protocol.version=2",
                "clone",
                "-q",
                "--filter=blob:none",
                "--",
                origin_url,
                str(partial),
            ],
            check=True,
            capture_output=True,
        )
        mat = _run_materialize(partial, env={"GH_TOKEN": _TOKEN})
        assert mat.returncode == 0, mat.stderr
        copy = tmp_path / "copy"
        result = subprocess.run(
            ["git", "clone", f"file://{partial.resolve()}", str(copy)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr


def test_detection_does_not_use_is_partial_clone() -> None:
    text = MATERIALIZE.read_text(encoding="utf-8")
    assert "is-partial-clone" not in text
    assert "remote.origin.promisor" in text


def test_batch_check_uses_rest_format_and_omits_per_object_probe() -> None:
    text = MATERIALIZE.read_text(encoding="utf-8")
    assert "%(objectname) %(objecttype) %(objectsize) %(rest)" in text
    assert "--batch-check=" in text
    assert 'cat-file", "-e"' not in text


def test_scan_full_materializes_before_history_and_keeps_verified_unknown() -> None:
    text = SCAN_SECRETS.read_text(encoding="utf-8")
    mat = text.index("materialize.materialize_repo")
    hist = text.index('"--results=verified,unknown"')
    assert mat < hist
    assert "--only-verified" not in text
    assert "--results=verified,unknown" in text


def test_scheduled_scan_step_has_gh_token_and_no_persisted_creds() -> None:
    doc = yaml.safe_load(SECURITY_YML.read_text(encoding="utf-8"))
    job = doc["jobs"]["scheduled"]
    assert job["timeout-minutes"] == 20
    job_if = job.get("if") or ""
    assert "endsWith(github.repository, '/music-dj-tools')" not in job_if
    checkouts = [s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/checkout@")]
    assert len(checkouts) == 1
    with_ = checkouts[0].get("with") or {}
    assert with_.get("persist-credentials") is False
    assert with_.get("fetch-depth") == 0
    assert with_.get("clean") is False
    scan_steps = [s for s in job["steps"] if s.get("id") == "scan"]
    assert len(scan_steps) == 1
    env = scan_steps[0].get("env") or {}
    assert "GH_TOKEN" in env


def test_pr_scan_fetches_unfiltered_with_token_and_goes_neutral_on_fetch_failure() -> None:
    doc = yaml.safe_load(SECURITY_YML.read_text(encoding="utf-8"))
    steps = doc["jobs"]["pr"]["steps"]
    names = [s.get("name", "") for s in steps]
    fetch = next(s for s in steps if s.get("id") == "fetch")
    scan = next(s for s in steps if s.get("name", "").startswith("Scan (new findings"))
    assert names.index(fetch["name"]) < names.index(scan["name"])
    assert "GH_TOKEN" in (fetch.get("env") or {})
    assert "--no-filter origin" in fetch["run"]
    assert "GIT_CONFIG_VALUE_0" in fetch["run"]
    assert "[UNKNOWN] secrets scan: fetch failed" in fetch["run"]
    assert "steps.fetch.outputs.fetched" in str(scan["continue-on-error"])
    assert "steps.fetch.outputs.merge_base" in scan["env"]["SECURITY_BASE_SHA"]
