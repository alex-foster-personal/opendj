"""10xHunter PR evidence gate.

A PR titled ``10xHunter-- ...`` claims an order-of-magnitude improvement found by
the 10x Constraint Hunt. The claim has to arrive with its evidence and its
arithmetic in the PR body, so that review can check the numbers, not only the code.

Requirements (mini-PRD):
  / A PR whose title does not start with ``10xHunter--`` passes untouched.
    [if] an ordinary PR fails this gate [then broken]
  / A 10xHunter PR needs a ``## 10x Evidence`` section with ``Before:``,
    ``After:``, ``Ratio:`` and ``Scorer:`` lines, each with a value.
    [if] a 10xHunter PR with no Evidence section passes [then broken]
    [if] an Evidence section missing ``After:`` passes [then broken]
  / A 10xHunter PR needs a ``## 10x Workings`` section that names its hunt card
    (``Card:``) and shows its working in at least one more non-empty line.
    [if] a Workings section holding only the card link passes [then broken]
  / The claimed ratio is at least 9x (the maintainer, Tue 29 Sep 2026: the gate is >9x).
    [if] ``Ratio: 3x`` passes [then broken]
    [if] a complete, well-formed 10xHunter body fails [then broken]

Usage: ``python -m scripts.tenx_evidence_check --title T --body-file F``. The
workflow passes both from the event payload. Exit 0 = pass, 1 = fail.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

TITLE_PREFIX = "10xHunter--"
MIN_RATIO = 9.0
EVIDENCE_KEYS = ("Before", "After", "Ratio", "Scorer")


@dataclass
class Verdict:
    ok: bool
    problems: list[str]


# ----- helpers
def _section(body: str, heading: str) -> str | None:
    match = re.search(rf"^##\s+{re.escape(heading)}\s*$(.*?)(?=^##\s|\Z)", body, re.M | re.S)
    return match.group(1) if match else None


def _field(section: str, key: str) -> str | None:
    match = re.search(rf"^\s*[-*]?\s*\**{key}\**\s*:\s*(\S.*)$", section, re.M)
    return match.group(1).strip() if match else None


def _ratio(value: str) -> float | None:
    match = re.match(r"(\d+(?:\.\d+)?)\s*x\b", value, re.I)
    return float(match.group(1)) if match else None


def _strip_fences(body: str) -> str:
    """Drop fenced code blocks (CommonMark: indent <= 3, unclosed runs to end)."""
    kept: list[str] = []
    fence: str | None = None
    for line in body.splitlines():
        opener = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if fence is None and opener:
            fence = opener.group(1)
        elif fence is not None and re.match(rf"^ {{0,3}}{fence[0]}{{{len(fence)},}}\s*$", line):
            fence = None
        elif fence is None:
            kept.append(line)
    return "\n".join(kept)


# ----- check
def check(title: str, body: str) -> Verdict:
    if not title.startswith(TITLE_PREFIX):
        return Verdict(True, [])
    # The template's commented placeholders and fenced text are not rendered evidence.
    body = re.sub(r"<!--.*?(?:-->|\Z)", "", _strip_fences(body), flags=re.S)
    problems: list[str] = []
    evidence = _section(body, "10x Evidence")
    if evidence is None:
        problems.append("missing '## 10x Evidence' section")
    elif evidence is not None:
        problems.extend(
            f"10x Evidence has no '{key}:' line"
            for key in EVIDENCE_KEYS
            if _field(evidence, key) is None
        )
        ratio_text = _field(evidence, "Ratio")
        ratio = _ratio(ratio_text) if ratio_text else None
        if ratio_text and ratio is None:
            problems.append(f"Ratio '{ratio_text}' has no '<number>x' value")
        elif ratio is not None and ratio <= MIN_RATIO:
            problems.append(f"Ratio {ratio}x is not above the {MIN_RATIO:g}x gate")
    workings = _section(body, "10x Workings")
    if workings is None:
        problems.append("missing '## 10x Workings' section")
    elif workings is not None:
        if _field(workings, "Card") is None:
            problems.append("10x Workings has no 'Card:' line linking the hunt card")
        lines = [
            ln
            for ln in workings.splitlines()
            if ln.strip() and not re.match(r"^\s*[-*]?\s*\**Card\**\s*:", ln)
        ]
        if not lines:
            problems.append("10x Workings shows no working beyond the card link")
    return Verdict(not problems, problems)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--title", required=True)
    ap.add_argument("--body-file", required=True, type=Path)
    args = ap.parse_args()
    verdict = check(args.title, args.body_file.read_text())
    if not args.title.startswith(TITLE_PREFIX):
        print(f"[OK] not a {TITLE_PREFIX} PR, nothing to check")
    elif verdict.ok:
        print("[OK] 10x Evidence and 10x Workings present, ratio at or above the gate")
    elif not verdict.ok:
        for problem in verdict.problems:
            print(f"::error::{problem}")
    return 0 if verdict.ok else 1


if __name__ == "__main__":
    sys.exit(main())
