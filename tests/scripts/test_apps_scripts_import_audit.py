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


def test_the_paid_off_allowlist_entries_are_flagged_again(tmp_path: Path) -> None:
    """The package-wide allowlist entries, and then controls.py, must not creep back."""
    for rel, text in (
        (
            "apps/analysis_bench/cli.py",
            "def run():\n    from scripts.build_key_bundle import main\n",
        ),
        ("apps/stems/relay/app.py", "def run():\n    import scripts.modal_vocal_farm as farm\n"),
    ):
        sample = tmp_path / rel
        sample.parent.mkdir(parents=True, exist_ok=True)
        sample.write_text(text, encoding="utf-8")
    assert ALLOWLIST_PREFIXES == ()
    assert sorted(f.path for f in audit_apps_tree(tmp_path)) == [
        "apps/analysis_bench/cli.py",
        "apps/stems/relay/app.py",
    ]


def test_current_tree_only_allowlisted_paths_have_function_body_scripts_imports() -> None:
    findings = audit_apps_tree(REPO_ROOT)
    assert findings == [], (
        "unexpected function-body scripts imports outside allowlist:\n"
        + "\n".join(f.render() for f in findings)
        + f"\nallowlist: {ALLOWLIST_PREFIXES}"
    )
