"""mypy incremental cache must stay sound under reuse (#1496)."""

from __future__ import annotations

import os
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
    cache_dir = tmp_path / "cache"
    os.environ["MDT_MYPY_CACHE_DIR"] = str(cache_dir)
    target = REPO_ROOT / "scripts" / "mypy_cache.py"
    original = target.read_text(encoding="utf-8")
    injected = "\n_MYPY_CACHE_PROBE: int = 'warm-must-see-this'\n"
    try:
        cold = _run_eval_mypy()
        target.write_text(original + injected, encoding="utf-8")
        warm = _run_eval_mypy()
    finally:
        target.write_text(original, encoding="utf-8")
        os.environ.pop("MDT_MYPY_CACHE_DIR", None)

    assert _mypy_error_total(warm) > _mypy_error_total(cold)
