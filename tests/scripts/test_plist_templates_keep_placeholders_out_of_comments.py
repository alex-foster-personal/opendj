"""Placeholders belong in element text, never inside an XML comment.

[if] any ops/**/*.plist.template spells a placeholder token inside <!-- --> [then]
a substituted value holding `--` (every af--<slug> worktree path) renders a plist
that does not parse, [else stop].

The per-renderer `--` tests prove each installer end to end; this one is the
class guard, so a template added later cannot reopen the defect quietly. XML
forbids `--` inside a comment, and no escaping helper can help there: the
`xml_escape` functions in the installers escape `&`, `<` and `>` for element
text only.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = sorted((REPO_ROOT / "ops").rglob("*.plist.template"))
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
# Two spellings are in use: __NAME__ (agt, autoreposync, dmg-smoke, cloudsync)
# and {{NAME}} (perf-kpi).
_PLACEHOLDER = re.compile(r"__[A-Z][A-Z0-9_]*__|\{\{[A-Z][A-Z0-9_]*\}\}")


def test_the_scan_sees_templates() -> None:
    """Positive control: an empty rglob would pass the scan for the wrong reason."""
    assert len(TEMPLATES) >= 3, TEMPLATES  # the agentic-testing and autoreposync templates moved to fleet-af


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda t: t.relative_to(REPO_ROOT).as_posix())
def test_no_placeholder_inside_a_comment(template: Path) -> None:
    raw = template.read_text(encoding="utf-8")
    assert _PLACEHOLDER.search(raw), f"{template} substitutes nothing; is it a template?"
    leaked = sorted({tok for comment in _COMMENT.findall(raw) for tok in _PLACEHOLDER.findall(comment)})
    assert not leaked, (
        f"{template.relative_to(REPO_ROOT)} spells {leaked} inside an XML comment; "
        "a substituted value holding `--` makes the rendered plist unparseable"
    )
