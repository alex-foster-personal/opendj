"""Find DJ software trademarks used as a skin id or picker name.

PLAN.md 4.1: style skins use neutral names; a mark belongs only in descriptions.
Matching is case-insensitive on word boundaries (hyphens and spaces split words).
"""

from __future__ import annotations

import re

MARKS: tuple[str, ...] = ("rekordbox", "serato", "traktor", "virtualdj", "djay", "engine dj")

_PATTERNS = {
    mark: re.compile(r"(?<![a-z0-9])" + r"[\s_-]+".join(map(re.escape, mark.split())) + r"(?![a-z0-9])", re.I)
    for mark in MARKS
}


def find_mark_uses(text: str) -> list[str]:
    """Return the listed marks that appear as a whole word in ``text``."""
    return [mark for mark, pat in _PATTERNS.items() if pat.search(text or "")]


_CHOICES_RE = re.compile(r"export const UI_SKIN_CHOICES[^=]*=\s*\[(.*?)\];", re.S)
_LABELS_RE = re.compile(r"export const UI_SKIN_LABELS[^=]*=\s*\{(.*?)\};", re.S)
_STR_RE = re.compile(r"'([^']*)'|\"([^\"]*)\"")
_ENTRY_RE = re.compile(r"^\s*(?:'([^']+)'|\"([^\"]+)\"|([A-Za-z_$][\w$]*))\s*:\s*(?:'([^']*)'|\"([^\"]*)\")", re.M)


def _strip_comments(src: str) -> str:
    return re.sub(r"//[^\n]*", "", re.sub(r"/\*.*?\*/", "", src, flags=re.S))


def parse_ui_skin_ts(src: str) -> tuple[list[str], dict[str, str]]:
    """Return (UI_SKIN_CHOICES ids, UI_SKIN_LABELS map). Raises ValueError when unparseable."""
    src = _strip_comments(src)
    choices_m = _CHOICES_RE.search(src)
    labels_m = _LABELS_RE.search(src)
    if not choices_m or not labels_m:
        raise ValueError("UI_SKIN_CHOICES or UI_SKIN_LABELS block not found")
    choices = [a or b for a, b in _STR_RE.findall(choices_m.group(1))]
    labels = {(m[0] or m[1] or m[2]): (m[3] or m[4]) for m in _ENTRY_RE.findall(labels_m.group(1))}
    if not choices or not labels:
        raise ValueError("UI_SKIN_CHOICES or UI_SKIN_LABELS block is empty")
    return choices, labels
