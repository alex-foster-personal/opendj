"""Regression lines for scripts/tenx_evidence_check.py (10xHunter PR evidence gate)."""

from scripts.tenx_evidence_check import check

GOOD = """## What changed
x

## 10x Evidence
- Before: gh pr list with mergeable, p50 13.5s (n=2, nucbox)
- After: gh pr list without mergeable, p50 1.25s (n=2, nucbox)
- Ratio: 10.8x
- Scorer: wall time of the same command against the same ~300 open PRs

## 10x Workings
- Card: https://github.com/maintainer/10x-hunter/blob/master/cards/HUNT-2026-09-29-H-Loop-01.md
- 13.5 / 1.25 = 10.8. The floor is the listing without per-PR mergeability.
"""


def test_ordinary_pr_passes_untouched() -> None:
    assert check("fix: something", "").ok, "if an ordinary PR fails this gate then broken"


def test_complete_body_passes() -> None:
    assert check("10xHunter-- drop mergeable", GOOD).ok, "if a complete 10xHunter body fails then broken"


def test_missing_evidence_fails() -> None:
    body = GOOD.split("## 10x Evidence")[0] + "## 10x Workings" + GOOD.split("## 10x Workings")[1]
    assert not check("10xHunter-- x", body).ok, "if a 10xHunter PR with no Evidence section passes then broken"


def test_missing_after_fails() -> None:
    body = "\n".join(ln for ln in GOOD.splitlines() if "After:" not in ln)
    assert not check("10xHunter-- x", body).ok, "if an Evidence section missing After: passes then broken"


def test_ratio_below_gate_fails() -> None:
    assert not check("10xHunter-- x", GOOD.replace("10.8x", "3x")).ok, "if Ratio: 3x passes then broken"


def test_workings_card_only_fails() -> None:
    body = GOOD.replace("- 13.5 / 1.25 = 10.8. The floor is the listing without per-PR mergeability.\n", "")
    assert not check("10xHunter-- x", body).ok, "if a Workings section holding only the card link passes then broken"


def test_untouched_template_fails() -> None:
    from pathlib import Path

    template = Path(".github/PULL_REQUEST_TEMPLATE.md").read_text()
    assert not check("10xHunter-- x", template).ok, "if the untouched PR template passes then broken"


def test_ratio_must_lead_with_number() -> None:
    body = GOOD.replace("Ratio: 10.8x", "Ratio: <number>x (gate: >= 9x)")
    assert not check("10xHunter-- x", body).ok, "if a placeholder Ratio passes then broken"
