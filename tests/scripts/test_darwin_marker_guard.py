"""Guard Darwin branches so a platform lane must execute their assertions."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_WORKFLOW = Path(".github/workflows/darwin-pytest.yml")


def _darwin_lane_globs() -> list[str]:
    """The pytest selection of the Darwin lane, read from the workflow's test_paths=( ... )."""
    text = _WORKFLOW.read_text(encoding="utf-8")
    block = text.split("test_paths=(", 1)[1].split(")", 1)[0]
    return [line.strip() for line in block.splitlines() if line.strip()]


_DARWIN_TEST_FILES = sorted({path for pattern in _darwin_lane_globs() for path in Path().glob(pattern)})


def test_darwin_lane_selects_files() -> None:
    """[if] the lane's globs match nothing [then] the guard below guards nothing, [else stop]."""
    assert _DARWIN_TEST_FILES, f"no test file matches the Darwin lane selection in {_WORKFLOW}"


def _contains_darwin_branch(node: ast.AST) -> bool:
    body = node.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else [node]
    return any(
        isinstance(child, ast.Compare)
        and isinstance(child.left, ast.Attribute)
        and isinstance(child.left.value, ast.Name)
        and child.left.value.id == "sys"
        and child.left.attr == "platform"
        and any(
            isinstance(comparator, ast.Constant) and comparator.value == "darwin" for comparator in child.comparators
        )
        for statement in body
        for child in ast.walk(statement)
    )


def _has_darwin_marker(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any(
        isinstance(decorator, ast.Attribute)
        and isinstance(decorator.value, ast.Attribute)
        and isinstance(decorator.value.value, ast.Name)
        and decorator.value.value.id == "pytest"
        and decorator.value.attr == "mark"
        and decorator.attr == "darwin"
        for decorator in node.decorator_list
    )


@pytest.mark.parametrize("path", _DARWIN_TEST_FILES)
def test_darwin_branch_tests_are_marked(path: Path) -> None:
    """[if] a scoped test branches on Darwin [then] it carries the marker, [else stop]."""
    assert path.is_file(), f"Darwin lane target vanished: {path}"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
        and _contains_darwin_branch(node)
        and not _has_darwin_marker(node)
    ]
    assert offenders == [], f"Darwin branch lacks @pytest.mark.darwin: {offenders}"


def test_marker_predicate_reads_the_real_decorator_shape() -> None:
    """[if] a correctly marked Darwin test is read as unmarked [then] the guard rejects compliant tests, [else stop]."""
    # Joined lines, so no backslash-n-at literal reads as a mail address to the OSS tip audit.
    source = "\n".join(
        [
            "import pytest, sys",
            "@pytest.mark.darwin",
            "def test_x():",
            "    if sys.platform == 'darwin':",
            "        pass",
            "",
        ]
    )
    marked = ast.parse(source).body[1]
    assert isinstance(marked, ast.FunctionDef)
    assert _contains_darwin_branch(marked) and _has_darwin_marker(marked)
    marked.decorator_list = []
    assert not _has_darwin_marker(marked)
