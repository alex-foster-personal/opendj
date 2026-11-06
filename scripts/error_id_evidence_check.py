"""PR body gate: a cited stable error id must carry FEATURE or FIX EVIDENCE.

Issue #2325 (OBS-01 Part 3 of #2320): when a PR body cites ``eid-`` plus 12 hex (stable
error id), it must also name FEATURE EVIDENCE or FIX EVIDENCE (markdown heading or
inline). N/A with a non-empty reason is accepted for nonvisual work; a UI-test proof
names ``trace.zip`` or a screenshot path (or the word screenshot). This restores a
narrower rule than the full issue-feature-evidence contract (AGENTS.md).

Requirements (mini-PRD):
  / Citations are read from the raw body after stripping HTML comments only; fenced
    json sink rows stay visible (do not strip fences).
    [if] a citation lives only inside a fenced json block [then] it still counts [else stop]
  / The all-zeros id ``eid-`` + twelve ``0`` chars, a bare ``eid-`` with no tail,
    and too-short tails are not citations.
    [if] only eid-000000000000 appears [then] pass with no evidence [else stop]
  / Evidence is FEATURE EVIDENCE or FIX EVIDENCE (case-insensitive), as ``##`` heading
    or inline ``FEATURE EVIDENCE:`` / ``FIX EVIDENCE:``.
    [if] inline N/A with reason passes [then] heading-only is not required [else stop]

Usage: ``python -m scripts.error_id_evidence_check --body-file F``. Exit 0 pass,
1 fail, 2 could not measure (missing ``--body-file`` or unreadable file). Stdlib
only. ``review_coverage`` stays advisory (REVIEW-8220); this gate reports via
``pr-body-gates`` and is not a required merge status (ADR-0124).
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from apps.shared.telemetry.error_id import ERROR_ID_HEX_LEN, ERROR_ID_PREFIX

#: Full stable id: prefix + exactly ERROR_ID_HEX_LEN hex digits (case-insensitive).
_CITATION_RE = re.compile(
    rf"\b{re.escape(ERROR_ID_PREFIX)}[0-9a-f]{{{ERROR_ID_HEX_LEN}}}\b",
    re.IGNORECASE,
)
_PLACEHOLDER_ALL_ZERO = ERROR_ID_PREFIX + "0" * ERROR_ID_HEX_LEN
#: Inline: FEATURE EVIDENCE: or FIX EVIDENCE: at line start (optional markdown lead).
_INLINE_LABEL = re.compile(
    r"(?im)^[ \t]*#*[ \t]*((?:FEATURE|FIX)[ \t]+EVIDENCE)\s*:?",
)
#: Heading: ## FEATURE EVIDENCE / ## FIX EVIDENCE
_HEADING_LABEL = re.compile(
    r"(?im)^[ \t]*#{1,6}[ \t]*((?:FEATURE|FIX)[ \t]+EVIDENCE)\b",
)
_HTML_COMMENT = re.compile(r"<!--.*?(?:-->|\Z)", re.DOTALL)
def _na_has_reason_after(remainder: str) -> bool:
    """True when text after ``N/A`` carries a non-empty reason (3+ non-space chars)."""
    line = remainder.split("\n", 1)[0].strip()
    if not line:
        return False
    if line.startswith("("):
        inner = re.match(r"\(([^)]*)\)", line)
        return bool(inner and len(inner.group(1).strip()) >= 3)
    if line.startswith(":"):
        return len(line[1:].strip()) >= 3
    return len(line.strip()) >= 3
_TRACE = re.compile(r"trace\.zip", re.IGNORECASE)
_SCREENSHOT_WORD = re.compile(r"\bscreenshots?\b", re.IGNORECASE)
_IMAGE_PATH = re.compile(
    r"\b[^\s`>]+\.(?:png|jpe?g|webp)\b",
    re.IGNORECASE,
)


@dataclass
class Verdict:
    ok: bool
    problems: list[str]


def _normalized(body: str) -> str:
    return body.replace("\r\n", "\n").replace("\r", "\n")


def strip_html_comments(body: str) -> str:
    return _HTML_COMMENT.sub("", body)


def citations(body: str) -> list[str]:
    """Unique stable error ids in scan order; placeholders dropped."""
    seen: set[str] = set()
    out: list[str] = []
    for m in _CITATION_RE.finditer(body):
        raw = m.group(0)
        if raw.casefold() == _PLACEHOLDER_ALL_ZERO.casefold():
            continue
        key = raw.casefold()
        if key not in seen:
            seen.add(key)
            out.append(raw)
    return out


def _evidence_regions(body: str) -> list[str]:
    """Slices of the body starting at each FEATURE/FIX EVIDENCE label (inline or heading)."""
    text = _normalized(body)
    regions: list[str] = []
    for rx in (_INLINE_LABEL, _HEADING_LABEL):
        regions.extend(text[m.start() :] for m in rx.finditer(text))
    return regions


def _region_has_capture(region: str) -> bool:
    if _TRACE.search(region):
        return True
    if _SCREENSHOT_WORD.search(region):
        return True
    return bool(_IMAGE_PATH.search(region))


def _region_has_na_reason(region: str) -> bool:
    m = re.search(r"\bN/A\b", region, re.IGNORECASE)
    if not m:
        return False
    return _na_has_reason_after(region[m.end() :])


def has_evidence(body: str) -> bool:
    """True when some label exists and (N/A with reason) OR (trace/screenshot capture)."""
    if not _INLINE_LABEL.search(body) and not _HEADING_LABEL.search(body):
        return False
    return any(
        _region_has_na_reason(region) or _region_has_capture(region)
        for region in _evidence_regions(body)
    )


def problems(body: str) -> list[str]:
    """Empty means pass."""
    stripped = strip_html_comments(body)
    ids = citations(stripped)
    if not ids:
        return []
    if not has_evidence(stripped):
        cited = ", ".join(ids)
        if _INLINE_LABEL.search(stripped) or _HEADING_LABEL.search(stripped):
            return [
                f"PR body cites error id(s) {cited} but FEATURE/FIX EVIDENCE has no "
                "N/A with a reason (3+ chars) and no trace.zip or screenshot path"
            ]
        return [f"PR body cites error id(s) {cited} but has no FEATURE EVIDENCE or FIX EVIDENCE"]
    return []


def check(body: str) -> Verdict:
    msgs = problems(body)
    return Verdict(ok=not msgs, problems=msgs)


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    else:
        argv = list(argv)
        # Tests may pass a leading argv0 (``error_id_evidence_check``); strip it.
        if argv and not argv[0].startswith("-"):
            argv = argv[1:]
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--body-file")
    args = parser.parse_args(argv)
    if not args.body_file:
        print("[UNKNOWN] --body-file is required", file=sys.stderr)
        return 2
    path = Path(args.body_file)
    if not path.is_file():
        print(f"[UNKNOWN] body file missing or not a file: {path}", file=sys.stderr)
        return 2
    try:
        body = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"[UNKNOWN] cannot read body file: {exc}", file=sys.stderr)
        return 2
    verdict = check(body)
    if verdict.ok:
        ids = citations(strip_html_comments(body))
        if ids:
            print(f"[PASS] error-id citation(s) {', '.join(ids)} have acceptable evidence")
        else:
            print("[PASS] no stable error-id citation in PR body")
        return 0
    for line in verdict.problems:
        print(f"::error title=error-id evidence refused::{line}")
        print(f"[FAIL] {line}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
