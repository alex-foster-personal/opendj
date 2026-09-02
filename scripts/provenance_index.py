"""The readable VIEW of the prompt ledger: PROMPT-INDEX.md, rendered and parsed.

Split out of :mod:`scripts.provenance_sweep` when that module crossed the
600-line python ratchet. The seam is one the test suite had already drawn -
`tests/scripts/test_provenance_index.py` existed before this module did - and
it is a real one: everything here is PRESENTATION over rows somebody else
decided to keep, while the sweep module is about DURABILITY, deciding what may
be written at all. They fail differently. A defect here renders a misleading
table; a defect there loses a prompt permanently.

The index is a lossy projection on purpose. It truncates, it re-applies the
current noise filter to an archive that cannot be edited, and it shows a window
rather than the whole. `LEDGER` is the complete record; this is the thing a
human reads.

Dependency direction is one-way, sweep -> index, so there is no cycle: the
shared identity granularity `KEY_PREFIX` lives in `provenance_sources`, which
is what defines what a harvested prompt is, and both importers read it from
there rather than from each other.

Raising the ratchet allowance was the alternative and was refused: those
numbers are the maintainer's, and editing a gate config because the gate went red is the
change the merge brief prohibits.
"""

from __future__ import annotations

import re
from datetime import datetime

from scripts.provenance_sources import KEY_PREFIX, _clean

OUT_DIRNAME = "docs/threads"
LEDGER = "prompts.jsonl"
INDEX = "PROMPT-INDEX.md"
INDEX_PATH = f"{OUT_DIRNAME}/{INDEX}"

# How many rows the markdown table renders. The ledger is the complete store; the
# index is a newest-first WINDOW over it, and says so in its own header so the
# window is never mistaken for the whole.
INDEX_WINDOW = 400

# Markdown cell boundary: a pipe that is not backslash-escaped.
CELL_SPLIT = re.compile(r"(?<!\\)\|")


def index_rows(text: str) -> set[tuple[str, str, str]]:
    """(when, tool, truncated-text) for each data row of a rendered index table.

    The index stores truncated fields, so a row cannot be turned back into a
    ledger key. Comparison therefore happens in this lossy view, which both
    sides can be projected into.
    """
    out: set[tuple[str, str, str]] = set()
    for line in text.splitlines():
        if not line.startswith("| ") or line.startswith("| when "):
            continue
        # Split on UNESCAPED pipes only. A prompt containing "|" is rendered as
        # "\|", and a naive split truncates that cell, so the guard would report
        # the row missing from its own output and abort every sweep forever.
        cells = [c.strip() for c in CELL_SPLIT.split(line.strip().strip("|"))]
        if len(cells) < 3:
            continue
        out.add((cells[0], cells[1], cells[2]))
    return out


def _comparable(row: tuple[str, str, str]) -> tuple[str, str, str]:
    """Narrow a rendered index row to the granularity the ledger can answer at.

    `row_key` identifies a prompt by its first 80 characters; the table renders
    96. Comparing at 96 asks a finer question than the store keeps an answer to,
    so two rows the ledger considers the same can render differently and read as
    a loss. Truncating both sides to KEY_PREFIX asks the answerable question.
    """
    when, tool, text = row
    return (when, tool, text[:KEY_PREFIX].strip())


def _index_view(rec: dict) -> tuple[str, str, str]:
    when = (rec.get("at") or "")[:16].replace("T", " ") or "-"
    # .strip() AFTER truncation, not before: a 96-char cut lands on a space often
    # enough, and a markdown cell reader strips it back off. Without this the
    # rendered row no longer matches its own source row, and the superset guard
    # reports 46 present rows as lost (measured against the real index).
    text = rec.get("text", "").replace("|", "\\|").replace("\n", " ")[:96].strip()
    return (when, rec.get("tool", "?"), text)


def _without_swept_stamp(text: str) -> list[str]:
    """Index content minus the volatile 'Swept ...' line, for change detection."""
    return [ln for ln in text.splitlines() if not ln.startswith("Swept ")]


def render_index(allrecs: list[dict]) -> str:
    # Re-apply the CURRENT noise policy at render time. The ledger is an archive
    # and the union guard forbids deleting from it, so rows harvested under an
    # older, looser filter stay there forever - 98 wrapped agent-to-agent
    # messages among them. Filtering the VIEW instead keeps the history intact
    # and still stops that traffic crowding real prompts out of the table.
    originals = [r for r in allrecs
                 if not r.get("fork") and _clean(r.get("text")) is not None]
    # Counted from the flag, not as the leftover of the subtraction: `originals`
    # drops noise AND forks, so a difference labels every filtered agent-to-agent
    # message a fork replay. With 98 such rows in the archive that is not a
    # rounding error, it is a number that says something untrue.
    forks = sum(1 for r in allrecs if r.get("fork"))
    by_tool: dict[str, int] = {}
    for r in originals:
        by_tool[r.get("tool", "?")] = by_tool.get(r.get("tool", "?"), 0) + 1

    shown = originals[:INDEX_WINDOW]
    window_note = (
        f"Showing the newest {len(shown)} of {len(originals)}. "
        f"The complete record is `{LEDGER}`; this table is a window over it."
    )
    lines = [
        "# Prompt index (all agents)",
        "",
        "Every human prompt across Claude Code, Codex and Cursor for this repo,",
        "newest first, linked to the branch and PR live at the time.",
        "",
        f"Swept {datetime.now().astimezone().strftime('%a %d %b %Y %H:%M')}. ",
        f"{len(originals)} prompts typed: "
        + ", ".join(f"{k} {v}" for k, v in sorted(by_tool.items()))
        + (f"  (+{forks} fork replays, excluded)" if forks else ""),
        "",
        window_note,
        "",
        "| when | tool | prompt | branch / PR |",
        "|---|---|---|---|",
    ]
    for r in shown:
        when, tool, text = _index_view(r)
        link = ""
        if r.get("prs"):
            link = " ".join(f"[#{p['number']}]({p['url']})" for p in r["prs"])
        elif r.get("branches"):
            link = f"`{r['branches'][0]}`"
        lines.append(f"| {when} | {tool} | {text} | {link} |")
    return "\n".join(lines) + "\n"


