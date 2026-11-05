"""Release gate contract: portable core vs optional-native vs full tree.

Issue #159 split `make test` / `make release-check` onto the same --ignore
set as ci.yml's fast lane so a plain dev venv does not die on madmom, Quartz,
or cliclick. These tests pin that partition so a future edit cannot silently
reunify the gates.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = REPO_ROOT / "Makefile"
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"

RELEASE_GATE_IGNORES = (
    "tests/analysis",
    "tests/vocals",
    "tests/sync/usb",
    "tests/sets",
)


def _makefile() -> str:
    return MAKEFILE.read_text(encoding="utf-8")


def _ci_pytest_run() -> str:
    jobs = yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"]
    shard = jobs["test"]
    steps = [s for s in shard["steps"] if ".venv/bin/pytest" in (s.get("run") or "")]
    assert len(steps) == 1, f"expected exactly one pytest step, found {len(steps)}"
    return steps[0]["run"]


def _extract_ignores(text: str) -> list[str]:
    return re.findall(r"--ignore=(tests/[^\s\\]+)", text)


def test_release_check_still_depends_on_portable_test() -> None:
    """[if] release-check drops test [then] the portable gate is bypassed [else stop]."""
    text = _makefile()
    block = re.search(r"^release-check:.*", text, re.MULTILINE)
    assert block, "release-check target missing from Makefile"
    assert "test" in block.group(0).split(), block.group(0)


def test_test_target_uses_release_gate_ignores_and_floor() -> None:
    """[if] make test omits an ignore path [then] optional deps re-enter the gate [else stop]."""
    text = _makefile()
    test_recipe = re.search(r"^test:\n((?:\t.*\n)+)", text, re.MULTILINE)
    assert test_recipe, "test target missing from Makefile"
    recipe = test_recipe.group(1)
    assert "RELEASE_GATE_PYTEST_IGNORES" in recipe, recipe
    for path in RELEASE_GATE_IGNORES:
        assert f"--ignore={path}" in text, f"missing --ignore={path} in Makefile"
    assert "RELEASE_GATE_COLLECT_FLOOR" in recipe, recipe
    assert "PYTEST_COLLECT_FLOOR" not in recipe, "test must use RELEASE_GATE_COLLECT_FLOOR, not the full-tree floor"


def test_test_optional_native_runs_the_ignored_paths() -> None:
    """[if] test-optional-native drops a path [then] that suite has no local runner [else stop]."""
    text = _makefile()
    recipe = re.search(r"^test-optional-native:\n((?:\t.*\n)+)", text, re.MULTILINE)
    assert recipe, "test-optional-native target missing from Makefile"
    body = recipe.group(1)
    for path in RELEASE_GATE_IGNORES:
        assert path in body, f"missing {path} in test-optional-native recipe"
    assert "--collect-floor" not in body, "optional-native must not carry a collect floor"


def test_test_full_keeps_the_historical_floor() -> None:
    """[if] test-full drops floor 3700 [then] the full tree loses its guard [else stop]."""
    text = _makefile()
    recipe = re.search(r"^test-full:\n((?:\t.*\n)+)", text, re.MULTILINE)
    assert recipe, "test-full target missing from Makefile"
    body = recipe.group(1)
    assert "PYTEST_COLLECT_FLOOR" in body, body
    assert "RELEASE_GATE_PYTEST_IGNORES" not in body, "test-full must run the entire tree"


def test_rb_parity_check_unchanged_scope() -> None:
    """[if] rb-parity-check gains ignore paths [then] parity scope shrinks [else stop]."""
    text = _makefile()
    recipe = re.search(r"^rb-parity-check:\n((?:\t.*\n)+)", text, re.MULTILINE)
    assert recipe, "rb-parity-check target missing from Makefile"
    body = recipe.group(1)
    assert "$(PYTEST) -q -m rb_parity" in body.replace("\n\t", " "), body
    for path in RELEASE_GATE_IGNORES:
        assert f"--ignore={path}" not in body, f"rb-parity-check must not ignore {path}"


def test_release_gate_ignores_match_ci_fast_lane() -> None:
    """[if] Makefile ignores diverge from ci.yml [then] release and PR gates disagree [else stop]."""
    makefile_ignores = _extract_ignores(_makefile())
    ci_ignores = _extract_ignores(_ci_pytest_run())
    for path in RELEASE_GATE_IGNORES:
        assert path in makefile_ignores, f"Makefile missing --ignore={path}"
        assert path in ci_ignores, f"ci.yml missing --ignore={path}"
