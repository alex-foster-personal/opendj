"""Pins for #712: nightly webkit-deckload runs every listed test and checks presence.

Regression lines:
  - if mode serial returns to webkit-deckload then quarantined tests skip again
  - if the nightly step regains --grep or --retries then the suite truncates
  - if --floor drifts from the config's listed test count then the pin fails
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
E2E = REPO_ROOT / ".github" / "workflows" / "e2e.yml"
E2E_DIR = REPO_ROOT / "apps" / "webui" / "frontend" / "tests" / "e2e"
WEBKIT_SPEC = E2E_DIR / "webkit-deckload.spec.ts"
WEBKIT_CONFIG = E2E_DIR / "playwright.webkit-deckload.config.ts"

FLOOR_RE = re.compile(r"--floor\s+(\d+)")
TEST_MATCH_RE = re.compile(r"testMatch:\s*\[(.*?)\]", re.DOTALL)
FILENAME_RE = re.compile(r"'([^']+\.spec\.ts)'")

# Listed test counts per spec file (update when tests are added/removed).
FILE_TEST_COUNTS: dict[str, int] = {
    "webkit-deckload.spec.ts": 14,
    "setup-entry-points.spec.ts": 10,
    "deckload-smoke.spec.ts": 5,
    "autoplay-explainer-placement.spec.ts": 1,
    "playlist-detail.spec.ts": 1,
    "zz-autoplay-playlist-switch.spec.ts": 2,
}


def _parse_test_match_arrays(config_text: str) -> tuple[list[str], list[str]]:
    matches = TEST_MATCH_RE.findall(config_text)
    assert len(matches) >= 2, "expected default and chromium testMatch arrays"
    default_files = FILENAME_RE.findall(matches[0])
    chromium_files = FILENAME_RE.findall(matches[1])
    return default_files, chromium_files


def _count_tests_in_file(name: str) -> int:
    assert name in FILE_TEST_COUNTS, f"add {name!r} to FILE_TEST_COUNTS"
    return FILE_TEST_COUNTS[name]


def _listed_test_total(config_text: str) -> int:
    default_files, chromium_files = _parse_test_match_arrays(config_text)
    chromium_set = set(chromium_files)
    total = 0
    for name in default_files:
        per_file = _count_tests_in_file(name)
        multiplier = 2 if name in chromium_set else 1
        total += per_file * multiplier
    return total


def _extended_steps() -> list[dict]:
    doc = yaml.safe_load(E2E.read_text(encoding="utf-8"))
    return doc["jobs"]["extended"]["steps"]


def _step_run(name_fragment: str) -> str:
    for step in _extended_steps():
        if name_fragment in step.get("name", ""):
            run = step.get("run")
            assert isinstance(run, str), f"step {name_fragment!r} has no run block"
            return run
    raise AssertionError(f"no extended step matching {name_fragment!r}")


def test_webkit_deckload_has_no_serial_mode() -> None:
    """if mode serial returns then the first failure skips the quarantined remainder"""
    text = WEBKIT_SPEC.read_text(encoding="utf-8")
    assert "mode: 'serial'" not in text
    assert 'mode: "serial"' not in text


def test_webkit_deckload_config_keeps_sequential_pins() -> None:
    """workers 1 + fullyParallel false + retries 0 keep the shared page safe"""
    text = WEBKIT_CONFIG.read_text(encoding="utf-8")
    assert "workers: 1" in text
    assert "fullyParallel: false" in text
    assert "retries: 0" in text


def test_nightly_playwright_step_has_no_grep_or_retries() -> None:
    """the nightly must run the whole config, not a grep subset"""
    run = _step_run("FULL webkit artifact suite")
    assert "playwright.webkit-deckload.config.ts" in run
    assert "--grep" not in run
    assert "--retries" not in run


def test_nightly_playwright_step_emits_json_and_long_timeout() -> None:
    """JSON report + 30m global timeout are required for presence"""
    run = _step_run("FULL webkit artifact suite")
    assert "--reporter=json" in run or "reporter=json" in run
    assert "--global-timeout=1800000" in run or "global-timeout=1800000" in run


def test_presence_step_runs_always_and_checks_floor() -> None:
    """presence must run when playwright is red and enforce --floor"""
    steps = _extended_steps()
    idx = next(
        i for i, step in enumerate(steps) if "FULL webkit artifact suite" in step.get("name", "")
    )
    presence = steps[idx + 1]
    assert "Assert nightly webkit suite executed every listed test" in presence.get("name", "")
    condition = str(presence.get("if", ""))
    assert "always()" in condition
    assert "cancelled()" in condition
    run = presence.get("run", "")
    assert "playwright_presence_check.py" in run
    assert "--floor" in run


def test_presence_floor_matches_config_listed_count() -> None:
    """--floor must equal webkit + chromium listed tests from testMatch"""
    config_text = WEBKIT_CONFIG.read_text(encoding="utf-8")
    expected = _listed_test_total(config_text)
    run = _step_run("Assert nightly webkit suite executed every listed test")
    match = FLOOR_RE.search(run)
    assert match, "presence step must pass --floor N"
    assert int(match.group(1)) == expected, (
        f"--floor {match.group(1)} != listed test count {expected}; "
        "update e2e.yml and per-file counts together"
    )
