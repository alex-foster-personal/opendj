"""Audit lazy ``scripts`` imports inside ``apps/`` function bodies.

The packaged engine ships ``apps/`` only. Module-level ``scripts.*`` imports are
caught by ``tests/webui/test_route_table_without_scripts.py``; this gate catches
the lazy class: ``from scripts...`` inside a function, which passes route-table
boot and fails only when the endpoint is exercised.

Dev-only trees not shipped in the engine payload are allowlisted:

- ``apps/analysis_bench/``
- ``apps/stems/relay/``

    python -m scripts.apps_scripts_import_audit
    python -m scripts.apps_scripts_import_audit --root /path/to/checkout
"""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
from pathlib import Path

ALLOWLIST_PREFIXES: tuple[str, ...] = (
    "apps/analysis_bench/",
    "apps/stems/relay/",
)

__all__ = [
    "ALLOWLIST_PREFIXES",
    "Finding",
    "audit_apps_tree",
    "findings_in_file",
    "main",
]


@dataclass(frozen=True)
class Finding:
    path: str
    lineno: int
    import_text: str

    def render(self) -> str:
        return f"{self.path}:{self.lineno}: {self.import_text}"


def _is_scripts_import(node: ast.Import | ast.ImportFrom) -> bool:
    if isinstance(node, ast.Import):
        return any(
            alias.name == "scripts" or alias.name.startswith("scripts.")
            for alias in node.names
        )
    if isinstance(node, ast.ImportFrom) and node.module is not None:
        return node.module == "scripts" or node.module.startswith("scripts.")
    return False


def _import_text(node: ast.Import | ast.ImportFrom) -> str:
    if isinstance(node, ast.Import):
        names = ", ".join(
            f"{alias.name} as {alias.asname}" if alias.asname else alias.name
            for alias in node.names
        )
        return f"import {names}"
    names = ", ".join(
        f"{alias.name} as {alias.asname}" if alias.asname else alias.name
        for alias in node.names
    )
    module = node.module or ""
    level = "." * node.level
    return f"from {level}{module} import {names}"


class _FunctionBodyImportVisitor(ast.NodeVisitor):
    def __init__(self, rel_path: str) -> None:
        self._rel_path = rel_path
        self._in_function = 0
        self.findings: list[Finding] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._in_function += 1
        self.generic_visit(node)
        self._in_function -= 1

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._in_function += 1
        self.generic_visit(node)
        self._in_function -= 1

    def visit_Import(self, node: ast.Import) -> None:
        if self._in_function and _is_scripts_import(node):
            self.findings.append(
                Finding(self._rel_path, node.lineno, _import_text(node))
            )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if self._in_function and _is_scripts_import(node):
            self.findings.append(
                Finding(self._rel_path, node.lineno, _import_text(node))
            )


def _is_allowlisted(rel_path: str) -> bool:
    normalized = rel_path.replace("\\", "/")
    return any(normalized.startswith(prefix) for prefix in ALLOWLIST_PREFIXES)


def findings_in_file(path: Path, *, rel_path: str) -> list[Finding]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=rel_path)
    visitor = _FunctionBodyImportVisitor(rel_path)
    visitor.visit(tree)
    return visitor.findings


def audit_apps_tree(root: Path) -> list[Finding]:
    apps_root = root / "apps"
    findings: list[Finding] = []
    for path in sorted(apps_root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel_path = path.relative_to(root).as_posix()
        if _is_allowlisted(rel_path):
            continue
        findings.extend(findings_in_file(path, rel_path=rel_path))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="List function-body scripts imports under apps/"
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="repository root (default: parent of scripts/)",
    )
    args = parser.parse_args(argv)
    findings = audit_apps_tree(args.root)
    for finding in findings:
        print(finding.render())
    if findings:
        return 1
    print("ok: no function-body scripts imports under apps/ (outside allowlist)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
