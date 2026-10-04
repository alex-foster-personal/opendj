"""mypy incremental cache must stay sound under reuse (#1496)."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import quality_gate as qg
from scripts.mypy_cache import CACHE_KEY_FILE, resolve_cache_dir

REPO_ROOT = Path(__file__).resolve().parents[2]


def _mypy_error_total(metrics: list[qg.Metric]) -> int:
    return int(
        sum(
            m.value
            for m in metrics
            if m.key in ("mypy.errors_apps", "mypy.errors_tests", "mypy.errors_scripts")
        )
    )


def _run_eval_mypy() -> list[qg.Metric]:
    try:
        return qg._eval_mypy()
    except RuntimeError as exc:
        pytest.skip(f"UNAVAILABLE: real mypy could not run: {exc}")
    raise AssertionError("unreachable")


def _copy_source(destination: Path) -> None:
    """Copy genuine tracked working-tree bytes; never mutate the shared checkout."""
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("UNAVAILABLE: git is required to copy the real mypy source")
    tracked = subprocess.check_output(
        [git, "ls-files", "-z"], cwd=REPO_ROOT, timeout=30,
    ).decode("utf-8").split("\0")
    for relative in filter(None, tracked):
        source = REPO_ROOT / relative
        if source.is_file():
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)


def _eval_owned_source(repo: Path) -> list[qg.Metric]:
    """A fresh interpreter imports the copied production evaluator and cache code."""
    command = (
        "import dataclasses, json; from scripts import quality_gate as qg; "
        "print(json.dumps([dataclasses.asdict(m) for m in qg._eval_mypy()]))"
    )
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, "-c", command], cwd=repo, env=env,
        capture_output=True, text=True, check=True, timeout=300,
    )
    return [qg.Metric(**row) for row in json.loads(result.stdout)]


@pytest.mark.slow
def test_warm_mypy_cache_reports_the_same_error_count(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    os.environ["MDT_MYPY_CACHE_DIR"] = str(cache_dir)
    try:
        first = _run_eval_mypy()
        second = _run_eval_mypy()
    finally:
        os.environ.pop("MDT_MYPY_CACHE_DIR", None)

    assert _mypy_error_total(first) == _mypy_error_total(second)
    assert (cache_dir / CACHE_KEY_FILE).exists()


@pytest.mark.slow
def test_stale_cache_key_is_purged_fail_closed(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    os.environ["MDT_MYPY_CACHE_DIR"] = str(cache_dir)
    try:
        resolve_cache_dir()
        (cache_dir / CACHE_KEY_FILE).write_text("stale-key\n", encoding="utf-8")
        (cache_dir / "marker").write_text("poison", encoding="utf-8")
        resolved = resolve_cache_dir()
        assert resolved == cache_dir
        assert not (cache_dir / "marker").exists()
        assert (cache_dir / CACHE_KEY_FILE).read_text(encoding="utf-8").strip() != "stale-key"
    finally:
        os.environ.pop("MDT_MYPY_CACHE_DIR", None)


@pytest.mark.slow
def test_warm_cache_still_finds_a_new_error(tmp_path: Path) -> None:
    repo = tmp_path / "source"
    _copy_source(repo)
    cache_dir = tmp_path / "cache"
    previous_cache = os.environ.get("MDT_MYPY_CACHE_DIR")
    os.environ["MDT_MYPY_CACHE_DIR"] = str(cache_dir)
    target = repo / "scripts" / "mypy_cache.py"
    shared_target = REPO_ROOT / "scripts" / "mypy_cache.py"
    shared_hash = hashlib.sha256(shared_target.read_bytes()).digest()
    original = target.read_text(encoding="utf-8")
    injected = "\n_MYPY_CACHE_PROBE: int = 'warm-must-see-this'\n"
    try:
        cold = _eval_owned_source(repo)
        target.write_text(original + injected, encoding="utf-8")
        warm = _eval_owned_source(repo)
        target.write_text(original, encoding="utf-8")
        restored = _eval_owned_source(repo)
    finally:
        target.write_text(original, encoding="utf-8")
        if previous_cache is None:
            os.environ.pop("MDT_MYPY_CACHE_DIR", None)
        else:
            os.environ["MDT_MYPY_CACHE_DIR"] = previous_cache

    assert _mypy_error_total(warm) > _mypy_error_total(cold)
    assert restored == cold
    assert hashlib.sha256(shared_target.read_bytes()).digest() == shared_hash
    assert target.read_text(encoding="utf-8") == original
