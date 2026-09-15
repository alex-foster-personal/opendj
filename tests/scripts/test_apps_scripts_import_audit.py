"""Gate: function-body ``scripts`` imports under ``apps/`` are inventoried.

Module-level ``scripts.*`` imports are ``test_route_table_without_scripts``'s
job. This audit catches lazy imports inside functions that survive route-table
boot and break only when exercised in the packaged engine.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from scripts.apps_scripts_import_audit import (
    ALLOWLIST_PREFIXES,
    audit_apps_tree,
    findings_in_file,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_function_body_scripts_import_is_flagged(tmp_path: Path) -> None:
    sample = tmp_path / "apps" / "sample" / "lazy.py"
    sample.parent.mkdir(parents=True)
    sample.write_text(
        textwrap.dedent(
            """
            def load():
                from scripts.foo import bar
            """
        ),
        encoding="utf-8",
    )
    findings = findings_in_file(sample, rel_path="apps/sample/lazy.py")
    assert len(findings) == 1
    assert "scripts.foo" in findings[0].import_text


def test_module_level_scripts_import_is_not_flagged(tmp_path: Path) -> None:
    sample = tmp_path / "apps" / "sample" / "top.py"
    sample.parent.mkdir(parents=True)
    sample.write_text(
        "from scripts.foo import bar\n\ndef ok():\n    return bar\n",
        encoding="utf-8",
    )
    assert findings_in_file(sample, rel_path="apps/sample/top.py") == []


def test_allowlisted_analysis_bench_is_skipped(tmp_path: Path) -> None:
    sample = tmp_path / "apps" / "analysis_bench" / "cli.py"
    sample.parent.mkdir(parents=True)
    sample.write_text(
        "def run():\n    from scripts.build_key_bundle import main\n",
        encoding="utf-8",
    )
    assert audit_apps_tree(tmp_path) == []


def test_current_tree_only_allowlisted_paths_have_function_body_scripts_imports() -> None:
    findings = audit_apps_tree(REPO_ROOT)
    assert findings == [], (
        "unexpected function-body scripts imports outside allowlist:\n"
        + "\n".join(f.render() for f in findings)
        + f"\nallowlist: {ALLOWLIST_PREFIXES}"
    )
