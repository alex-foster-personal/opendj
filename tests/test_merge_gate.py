"""Regression tests for scripts/merge_gate.py.

Acceptance criteria, each phrased as a failure statement:
  - if the gate does NOT pass settings-panel/01..04 then broken (good era must pass)
  - if the gate does NOT fail perf-sync-ux/00..03 then broken (rushed era must fail)
  - if the gate does NOT flag a stash-blob subject then broken
  - if _is_test misclassifies a tests/ path then broken
  - if the conventional-commit regex accepts a bare subject then broken
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
GATE = REPO / "scripts" / "merge_gate.py"


def _load_gate():
    spec = importlib.util.spec_from_file_location("merge_gate", GATE)
    assert spec and spec.loader, "merge_gate.py could not be loaded"
    module = importlib.util.module_from_spec(spec)
    # @dataclass resolves its module via sys.modules, so register before exec.
    sys.modules["merge_gate"] = module
    spec.loader.exec_module(module)
    return module


gate = _load_gate()


def _tag_exists(tag: str) -> bool:
    return subprocess.run(["git", "-C", str(REPO), "rev-parse", "--verify", tag],
                          capture_output=True).returncode == 0


# -----------------------------------------------------------------------------
# pure logic
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,expected",
    [
        ("tests/test_foo.py", True),
        ("apps/webui/frontend/tests/unit/midi-core.test.mjs", True),
        ("apps/webui/frontend/src/lib/rb/audio.spec.ts", True),
        ("apps/webui/server/app.py", False),
        ("apps/webui/frontend/src/lib/rb/audio-engine.svelte.ts", False),
    ],
)
def test_is_test_classification(path: str, expected: bool) -> None:
    assert gate._is_test(path) is expected, f"if _is_test misclassifies {path} then broken"


@pytest.mark.parametrize(
    "path,expected",
    [
        ("apps/webui/server/app.py", True),
        ("scripts/merge_gate.py", True),
        ("tests/test_foo.py", False),          # tests are not "source" for this gate
        ("docs/architecture.md", False),
        ("README.md", False),
    ],
)
def test_is_source_classification(path: str, expected: bool) -> None:
    assert gate._is_source(path) is expected, f"if _is_source misclassifies {path} then broken"


@pytest.mark.parametrize(
    "subject,ok",
    [
        ("feat(midi): add device map", True),
        ("fix: correct off-by-one", True),
        ("test(settings): catalog filter helpers", True),
        ("added some stuff", False),
        ("WIP on af--stem-tiers-sml: 620d7c6d feat(roformer)", False),
        ("On af--stem-tiers-sml: fireworks scripts", False),
    ],
)
def test_conventional_subject(subject: str, ok: bool) -> None:
    matched = bool(gate.CONVENTIONAL.match(subject))
    assert matched is ok, f"if CONVENTIONAL {'rejects' if ok else 'accepts'} {subject!r} then broken"


@pytest.mark.parametrize(
    "subject",
    ["On af--stem-tiers-sml: fireworks scripts", "WIP on af--x: 1234 feat(y): z"],
)
def test_stash_blob_detected(subject: str) -> None:
    assert gate.STASH_BLOB.match(subject), f"if STASH_BLOB misses {subject!r} then broken"


# -----------------------------------------------------------------------------
# integration against real history -- these encode the audit findings
# -----------------------------------------------------------------------------


@pytest.mark.skipif(not _tag_exists("settings-panel/04"), reason="settings-panel tags absent")
def test_disciplined_era_passes_test_gate() -> None:
    """Fri 24 Jul used feat,feat,feat + closing test(...) -- must pass."""
    result = gate.check_test_accompanies_source("settings-panel/01..settings-panel/04", 70)
    assert result.passed, f"if the gate fails the disciplined era then broken: {result.detail}"


@pytest.mark.skipif(not _tag_exists("perf-sync-ux/03"), reason="perf-sync-ux tags absent")
def test_rushed_era_fails_test_gate() -> None:
    """Thu 30 Jul shipped an untested feature with no closing test -- must fail."""
    result = gate.check_test_accompanies_source("perf-sync-ux/00..perf-sync-ux/03", 70)
    assert not result.passed, "if the gate passes the rushed era then broken"


@pytest.mark.skipif(not _tag_exists("rescue/gemini-stash-0832"), reason="rescue tag absent")
def test_stash_blob_range_fails_hygiene() -> None:
    result = gate.check_commit_hygiene("620d7c6d..rescue/gemini-stash-0832")
    assert not result.passed, "if the gate passes a stash-blob commit then broken"
