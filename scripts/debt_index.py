"""Render `.planning/TECH-DEBT.md` from the per-PR debt files.

The rule the files themselves follow lives in `.planning/DEBT-POLICY.md`.

Requirements (mini-PRD)
- [if] a debt file is added on main [then] one index row names its PR, branch,
  entry count, and newest logged date, [else stop]
- [if] a branch adds a debt file without regenerating the shared index [then]
  `--check` still passes, because a PR never regenerates it (issue #1415: two
  branches rewriting the index is the conflict; regeneration is main-side),
  [else stop]
- [if] a debt file is missing its branch, an entry's logged date, or an
  entry's source [then] regeneration raises instead of substituting a
  placeholder, [else stop]
- [if] a debt file's heading number differs from its filename [then]
  regeneration raises, because triage locates debt by filename, [else stop]
- [if] the committed index is stale for the debt files it references, or is
  hand-edited, dangling, or duplicated [then] `--check` fails, [else stop]
- [if] main's index must cover every debt file [then] `--check --require-current`
  fails while any file is unindexed, [else stop]
- [if] inputs do not change [then] regeneration is byte-for-byte deterministic,
  [else stop]
- [if] `review_thread_triage` validates a PR's own debt file [then] it reuses
  `_parse_debt_text` (via `check_pr_debt_file`), never a second copy of the
  format rules (issue #2980), [else stop]
- [if] a PR has no debt file [then] `check_pr_debt_file` passes it through
  untouched -- an absent file is not a format defect, [else stop]
- [if] a sibling debt file is malformed [then] `check_pr_debt_file` for a
  DIFFERENT PR number does not see it at all, and only `other_debt_file_problems`
  reports it, as non-blocking, [else stop]
"""

from __future__ import annotations

import argparse
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEBT_DIR = ROOT / ".planning" / "debt"
INDEX = ROOT / ".planning" / "TECH-DEBT.md"
_PR = re.compile(r"^# (?:PR|Issue) #(\d+)", re.MULTILINE)
_BRANCH = re.compile(r"^Branch: `([^`]+)`$", re.MULTILINE)
_LOGGED = re.compile(r"^Logged: (.+)$", re.MULTILINE)
_SOURCE = re.compile(r"^Source: (\S.*)$", re.MULTILINE)
_DATE_FORMAT = "%a %d %b %Y"


@dataclass(frozen=True)
class DebtFile:
    number: int
    branch: str
    count: int
    newest: date


# ----- parsing ------------------------------------------------------------


def _logged_date(raw: str, label: str) -> date:
    """Parse one `Logged:` value, refusing anything the format does not cover."""
    text = raw.strip()
    try:
        parsed = datetime.strptime(text, _DATE_FORMAT).date()  # noqa: DTZ007 -- a calendar day, not an instant
    except ValueError as exc:
        raise ValueError(
            f"{label} has an unparseable Logged date {text!r}; "
            f"the format is 'Ddd D Mmm YYYY', for example 'Sat 5 Sep 2026'"
        ) from exc
    # The weekday is a checksum on the number, not decoration. Recomputing it
    # silently would let a typo published as fact survive a regeneration.
    if not text.startswith(parsed.strftime("%a")):
        raise ValueError(
            f"{label} logs {text!r}, but that date is a "
            f"{parsed.strftime('%A')}; correct the weekday or the day"
        )
    return parsed


def _entries(text: str, label: str) -> list[date]:
    """Return one logged date per entry, refusing incomplete entries.

    An entry starts at its `Logged:` line and runs to the next one. Counting
    entries rather than permalinks is what keeps the index honest: one entry
    may cite several review rounds, and debt found outside a review thread
    cites none at all. `label` identifies the file in a raised message; it is
    a string rather than a `Path` because `_parse_debt_text` (below) validates
    text fetched over the network, at a pinned commit, with nothing on local
    disk to point a `Path` at.
    """
    starts = [(m.start(), m.group(1)) for m in _LOGGED.finditer(text)]
    if not starts:
        raise ValueError(f"{label} is missing Logged metadata; every entry needs 'Logged: <date>'")
    bounds = [start for start, _ in starts] + [len(text)]
    dates: list[date] = []
    for index, (_, raw) in enumerate(starts):
        block = text[bounds[index] : bounds[index + 1]]
        if _SOURCE.search(block) is None:
            raise ValueError(
                f"{label} entry logged {raw.strip()!r} is missing its "
                f"'Source: <permalink or provenance>' line"
            )
        dates.append(_logged_date(raw, label))
    return dates


def _parse_debt_text(number: int, text: str, label: str) -> DebtFile:
    """The format rules, applied to text already in hand -- the core `_entry`
    shares with `scripts.review_thread_triage`'s PR-head debt check.

    `number` is the PR the caller expects this text to belong to (a filename's
    stem for `_entry`, a PR argument for triage); `label` identifies the
    source in a raised message. Split out so triage's own-file check reuses
    these exact format rules instead of a second copy of them (issue #2980).
    """
    match = _PR.search(text)
    if match is None:
        raise ValueError(f"{label} must start with a PR or Issue number heading")
    heading = int(match.group(1))
    if heading != number:
        raise ValueError(
            f"{label} declares PR #{heading} but triage would read it as PR #{number}; "
            f"rename the file or correct the heading"
        )
    branch = _BRANCH.search(text)
    if branch is None:
        raise ValueError(f"{label} is missing Branch metadata: add a 'Branch: `<branch>`' line")
    dates = _entries(text, label)
    return DebtFile(number=heading, branch=branch.group(1), count=len(dates), newest=max(dates))


def _entry(path: Path) -> DebtFile:
    """Read one debt file on disk, raising on any metadata it does not
    actually record. Thin wrapper over `_parse_debt_text`: the filename
    supplies the expected PR number and doubles as the error label."""
    if not path.stem.isdigit():
        raise ValueError(
            f"{path} is not named for a pull request; triage reads .planning/debt/<pr>.md, "
            f"so the filename must be the PR number"
        )
    text = path.read_text(encoding="utf-8")
    return _parse_debt_text(int(path.stem), text, str(path))


def check_pr_debt_file(number: int, text: str) -> None:
    """Raise when PR `number`'s OWN debt file text fails `_parse_debt_text`.

    Scoped to exactly one file on purpose (issue #2980): a caller passes only
    the text of `.planning/debt/<number>.md`, fetched at that PR's own head,
    never a whole directory. That is what keeps a pre-existing malformed file
    elsewhere from failing a blameless PR -- this function structurally
    cannot see any file but the one it was handed. An empty `text` (no debt
    file at all) passes silently; a missing file is not a format defect.
    """
    if not text:
        return
    _parse_debt_text(number, text, f".planning/debt/{number}.md")


def other_debt_file_problems(number: int, debt_dir: Path = DEBT_DIR) -> list[str]:
    """Best-effort, non-blocking report of a SIBLING debt file that fails the
    parser -- diagnostic only, for a `review-triage` run to label as a
    main-side problem rather than stay silent about it. Never raises: this
    scan is only as current as the checkout it runs from, so it must never
    gate a PR the way `check_pr_debt_file` does for the PR's own, pinned-SHA
    file.
    """
    problems: list[str] = []
    if not debt_dir.exists():
        return problems
    for path in sorted(debt_dir.glob("*.md")):
        if path.stem.isdigit() and int(path.stem) == number:
            continue
        try:
            _entry(path)
        except ValueError as exc:
            problems.append(str(exc))
    return problems


# ----- rendering ----------------------------------------------------------


def _render_date(value: date) -> str:
    """Render a date the way the repository writes them: `Sat 5 Sep 2026`."""
    return f"{value.strftime('%a')} {value.day} {value.strftime('%b %Y')}"


def _render(entries: Iterable[DebtFile]) -> str:
    """Render the deterministic index for an iterable of parsed debt files."""
    ordered = sorted(entries, key=lambda item: item.number)
    lines = [
        "# Tech debt index",
        "",
        "Generated by `python -m scripts.debt_index`. Do not edit by hand.",
        "Each row summarizes one `.planning/debt/<pr>.md` file: how many deferred",
        "entries it records, and the newest date any of them was logged.",
        "The rule those files follow is `.planning/DEBT-POLICY.md`.",
        "",
        "| PR | branch | entries | newest entry date |",
        "|---|---|---:|---|",
    ]
    lines.extend(
        f"| #{entry.number} | `{entry.branch}` | {entry.count} | {_render_date(entry.newest)} |"
        for entry in ordered
    )
    lines.extend(
        [
            "",
            f"{sum(entry.count for entry in ordered)} entries across {len(ordered)} pull requests.",
        ]
    )
    return "\n".join(lines) + "\n"


def render_index(debt_dir: Path = DEBT_DIR) -> str:
    """Return the deterministic, complete index for `debt_dir`."""
    return _render(_entry(path) for path in debt_dir.glob("*.md"))


_ROW = re.compile(r"^\| #(\d+) \|", re.MULTILINE)


def _referenced_numbers(committed: str) -> list[int]:
    """The PR numbers the committed index rows name, in row order."""
    return [int(match.group(1)) for match in _ROW.finditer(committed)]


def current_check(debt_dir: Path, index: Path) -> bool:
    """True when the committed index is current over EVERY debt file.

    This is the main-side contract: the post-merge workflow regenerates after
    a debt file lands, and `--require-current` asserts that regeneration is
    complete. A PR head is deliberately NOT held to it (see `pr_head_check`),
    because a branch that adds an entry has not merged yet.
    """
    rendered = render_index(debt_dir)
    current = index.read_text(encoding="utf-8") if index.exists() else ""
    return current == rendered


def _validated_files(debt_dir: Path) -> dict[int, Path]:
    """Parse every debt file, raising on a non-numeric filename or malformed
    metadata, and return `{pr number: path}`. Every file is validated, indexed
    or not, so malformed debt cannot ride a docs-only PR past the pytest lane.
    """
    paths = sorted(debt_dir.glob("*.md"), key=lambda path: path.stem)
    for path in paths:
        _entry(path)
    return {int(path.stem): path for path in paths}


def _ensure_rows_consistent(index: Path, committed: str, files: dict[int, Path]) -> None:
    """Raise when the committed rows cannot be what regeneration would emit.

    A row is corrupt if it is duplicated, if it names a PR with no debt file
    on the branch (a dangling row), or if the committed text is not the exact
    rendering of the referenced files. A NEW debt file with no row yet is not
    corrupt: it is a branch's own addition awaiting the main-side regeneration.
    """
    referenced = _referenced_numbers(committed)
    duplicated = sorted({number for number in referenced if referenced.count(number) > 1})
    if duplicated:
        names = ", ".join(f"#{n}" for n in duplicated)
        raise ValueError(
            f"{index} lists PR {names} more than once; regeneration emits each row exactly once"
        )
    missing = [number for number in sorted(set(referenced)) if number not in files]
    if missing:
        names = ", ".join("#" + str(number) for number in missing)
        raise ValueError(
            f"{index} carries rows for PRs with no debt file on this branch: "
            f"{names}; restore the file or delete the row"
        )
    expected = _render(_entry(files[number]) for number in sorted(set(referenced)))
    if committed != expected:
        raise ValueError(
            f"{index} is stale or hand-edited: it is not byte-for-byte what regeneration "
            "produces from the debt files it references. Fix a hand edit where it stands; "
            "to index a debt file merged to main, regenerate on main "
            "(python -m scripts.debt_index) - never inside a pull request"
        )


def pr_head_check(debt_dir: Path, index: Path) -> None:
    """Raise when the committed index is not acceptable at a branch head.

    A branch adds only `.planning/debt/<pr>.md` and never regenerates the
    shared index; that is the whole fix for the concurrent-ledger conflicts
    (issue #1415), and the pytest lane and `just pre-push` run this check at
    every head. It refuses only what a faithful index cannot tolerate: a
    malformed debt file, or a committed index that is stale, hand-edited,
    dangling, or duplicated for the debt files it references. A NEW debt file
    that simply has no row yet is the one legal gap; the main-side workflow
    closes it after the merge.
    """
    files = _validated_files(debt_dir)
    if not index.exists():
        raise ValueError(f"{index} is missing; a debt file must branch from a main that ships one")
    _ensure_rows_consistent(index, index.read_text(encoding="utf-8"), files)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="PR-head guard: fail on a malformed debt file, or on an index that is stale, "
        "hand-edited, dangling, or duplicated for the debt files it references",
    )
    parser.add_argument(
        "--require-current",
        action="store_true",
        help="main-side guard: fail unless the committed index covers every debt file "
        "(--check tolerates a branch's own not-yet-indexed debt files)",
    )
    args = parser.parse_args(argv)
    if args.require_current:
        if not current_check(DEBT_DIR, INDEX):
            print(
                "[ERROR] .planning/TECH-DEBT.md is not current over every debt file; "
                "run python -m scripts.debt_index on main"
            )
            return 1
        print("[OK] .planning/TECH-DEBT.md is generated and current over every debt file")
        return 0
    if args.check:
        try:
            pr_head_check(DEBT_DIR, INDEX)
        except ValueError as exc:
            print(f"[ERROR] {exc}")
            return 1
        print(
            "[OK] every debt file is well formed and .planning/TECH-DEBT.md is a faithful "
            "index of the debt files it references"
        )
        return 0
    INDEX.write_text(render_index(), encoding="utf-8")
    print(f"[OK] regenerated {INDEX}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
