"""ENT-01: nothing outside the entitlement seam may branch on a plan NAME.

``if plan == "pro"`` is cheap to write and expensive forever after. It
hardcodes pricing into business logic, so a price change needs a redeploy; it
turns every comped, grandfathered and trial account into a special case; and
it scatters the pricing model across however many call sites happen to need
it. The seam exists so the only question anyone asks is ``has(feature_id)``.

This is a grep, deliberately, because the rule is about a SHAPE of code rather
than a symbol an import graph could catch. It is scoped to comparisons against
a string literal so the word "plan" stays usable -- ``apps/stems/api.py``
serves a stems PLAN, the setup wizard has an import plan, and neither is a
commercial plan.

Regression lines:
  - if any module outside apps/entitlements compares a plan-ish name to a
    string literal then pricing has leaked into business logic
  - if the frontend does the same then the leak is on the other side of the
    wire
  - if this test's own scan finds no files at all then it is passing
    vacuously and is measuring nothing
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
APPS = REPO_ROOT / "apps"
FRONTEND_SRC = APPS / "webui" / "frontend" / "src"

#: The seam itself is allowed to know plan names; that is what it is for.
EXEMPT_DIRS: tuple[Path, ...] = (APPS / "entitlements",)

SKIP_DIR_NAMES: frozenset[str] = frozenset(
    {"node_modules", "__pycache__", ".svelte-kit", "build", "dist"}
)

#: A comparison, either way round, between a plan-shaped identifier and a
#: string literal. Also catches ``plan in ("pro", "team")``.
_PLAN_WORD = r"(?:plan|plan_id|planId|plan_name|planName|tier_name|subscription)"
PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(rf"\b{_PLAN_WORD}\s*(?:===|==|!==|!=)\s*['\"]"),
    re.compile(rf"['\"]\s*(?:===|==|!==|!=)\s*\b{_PLAN_WORD}\b"),
    re.compile(rf"\b{_PLAN_WORD}\s+in\s*[\(\[\{{]\s*['\"]"),
    re.compile(rf"\bcase\s+['\"][^'\"]*['\"]\s*:.*\b{_PLAN_WORD}\b"),
)


def _scanned_files() -> list[Path]:
    out: list[Path] = []
    for root, suffixes in (
        (APPS, (".py",)),
        (FRONTEND_SRC, (".ts", ".svelte", ".js")),
    ):
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix not in suffixes:
                continue
            if SKIP_DIR_NAMES & set(path.parts):
                continue
            if any(path.is_relative_to(exempt) for exempt in EXEMPT_DIRS):
                continue
            out.append(path)
    return out


def test_no_call_site_branches_on_a_plan_name() -> None:
    files = _scanned_files()
    assert len(files) > 500, (
        f"only {len(files)} files scanned; the walk is broken and this test "
        "is passing vacuously"
    )
    offenders: list[str] = []
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        if "plan" not in text and "subscription" not in text:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if any(pattern.search(line) for pattern in PATTERNS):
                rel = path.relative_to(REPO_ROOT)
                offenders.append(f"{rel}:{number}: {line.strip()[:120]}")
    assert offenders == [], (
        "these call sites branch on a plan name instead of asking the "
        "entitlement seam what the account may DO:\n" + "\n".join(offenders)
    )


def test_the_seam_itself_is_actually_exempted() -> None:
    """The exemption must point somewhere, or the scope is a typo."""
    for exempt in EXEMPT_DIRS:
        assert exempt.is_dir(), exempt
        assert any(exempt.glob("*.py")), exempt
