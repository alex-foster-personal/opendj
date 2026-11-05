"""Tests that change the real machine are opt-in (TESTHOST-01).

A plain pytest run on a DJ's own Mac changed its real output volume. Every test
that sets the system volume, attaches a disk image under the real /Volumes or
ejects a real stick carries ``@pytest.mark.host_mutation``, and the root
conftest skips those unless ``ODJ_ALLOW_HOST_MUTATION=1``.

[if] a run has no ODJ_ALLOW_HOST_MUTATION=1 [then] host_mutation tests skip, [else stop].
[if] set_volume outside pytest.raises, round-trip volume, or disk_image/eject_stick lacks marker [then] this fails, [else stop].
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.conftest import HOST_MUTATION_ENV, host_mutation_skip_reason

pytestmark = [pytest.mark.requirement("TESTHOST-01")]

TESTS = Path(__file__).resolve().parent
HOST_FIXTURES = {"disk_image", "eject_stick"}


@pytest.mark.parametrize("value", [None, "", "0", "true", "yes", " 1"])
def test_host_mutation_is_skipped_unless_opted_in(value: str | None) -> None:
    environ = {} if value is None else {HOST_MUTATION_ENV: value}
    reason = host_mutation_skip_reason(environ)
    assert reason is not None and HOST_MUTATION_ENV in reason


def test_host_mutation_runs_when_opted_in() -> None:
    assert host_mutation_skip_reason({HOST_MUTATION_ENV: "1"}) is None


def _is_host_mutation_marker(node: ast.expr) -> bool:
    return ast.unparse(node) == "pytest.mark.host_mutation"


def _call_ids_inside_pytest_raises(function: ast.FunctionDef) -> set[int]:
    guarded: set[int] = set()
    for node in ast.walk(function):
        if not isinstance(node, ast.With):
            continue
        for item in node.items:
            ctx = item.context_expr
            if isinstance(ctx, ast.Call) and ast.unparse(ctx.func) == "pytest.raises":
                for inner in ast.walk(node):
                    if isinstance(inner, ast.Call):
                        guarded.add(id(inner))
    return guarded


def _sets_the_real_volume(call: ast.Call, source: str, guarded_call_ids: set[int]) -> bool:
    """A volume round trip, a real run of the system_volume_set tool, or a PUT to the volume route."""
    func = ast.unparse(call.func)
    if func == "_round_trip" and "set_volume" in source:
        return True
    if func == "_run_tool" and any(
        isinstance(arg, ast.Constant) and arg.value == "system_volume_set" for arg in call.args
    ):
        return True
    if func.endswith(".put") and bool(call.args) and ast.unparse(call.args[0]) == "VOLUME_URL":
        return True
    return func == "set_volume" and id(call) not in guarded_call_ids


def _unmarked_host_mutating_tests(scan_root: Path | None = None) -> list[str]:
    root = scan_root or TESTS
    found: list[str] = []
    for path in sorted(root.rglob("test_*.py")):
        source = path.read_text(encoding="utf-8")
        if not any(token in source for token in (*HOST_FIXTURES, "set_volume", "system_volume_set", "VOLUME_URL")):
            continue
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.FunctionDef) or not node.name.startswith("test_"):
                continue
            guarded = _call_ids_inside_pytest_raises(node)
            params = {arg.arg for arg in node.args.args}
            mutates = bool(params & HOST_FIXTURES) or any(
                _sets_the_real_volume(call, source, guarded) for call in ast.walk(node) if isinstance(call, ast.Call)
            )
            if mutates and not any(_is_host_mutation_marker(d) for d in node.decorator_list):
                label = (
                    f"{path.relative_to(TESTS.parent)}::{node.name}"
                    if scan_root is None
                    else f"{path.relative_to(scan_root)}::{node.name}"
                )
                found.append(label)
    return found


def test_every_host_mutating_test_carries_the_marker() -> None:
    assert _unmarked_host_mutating_tests() == []


def test_detector_flags_unmarked_set_volume_outside_pytest_raises(tmp_path: Path) -> None:
    module = tmp_path / "test_synthetic_unmarked_set_volume.py"
    module.write_text(
        "def test_mutates_volume_without_marker() -> None:\n    set_volume(50)\n",
        encoding="utf-8",
    )
    hits = _unmarked_host_mutating_tests(tmp_path)
    assert hits == ["test_synthetic_unmarked_set_volume.py::test_mutates_volume_without_marker"]


def test_detector_ignores_set_volume_inside_pytest_raises(tmp_path: Path) -> None:
    module = tmp_path / "test_synthetic_raises_guarded.py"
    module.write_text(
        "import pytest\n\n"
        "def test_refusal_before_mixer() -> None:\n"
        "    with pytest.raises(ValueError):\n"
        "        set_volume(150)\n",
        encoding="utf-8",
    )
    assert _unmarked_host_mutating_tests(tmp_path) == []
