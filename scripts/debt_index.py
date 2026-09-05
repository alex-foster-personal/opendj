"""Render `.planning/TECH-DEBT.md` from the per-PR debt files.

The rule the files themselves follow lives in `.planning/DEBT-POLICY.md`.

Requirements (mini-PRD)
- [if] a debt file is added [then] one index row names its PR, branch, entry
  count, and newest logged date, [else stop]
- [if] a debt file is missing its branch, an entry's logged date, or an
  entry's source [then] regeneration raises instead of substituting a
  placeholder, [else stop]
- [if] a debt file's heading number differs from its filename [then]
  regeneration raises, because triage locates debt by filename, [else stop]
- [if] the index is hand-edited [then] `--check` fails, [else stop]
- [if] inputs do not change [then] regeneration is byte-for-byte deterministic,
  [else stop]
"""

from __future__ import annotations

import argparse
import re
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


def _logged_date(raw: str, path: Path) -> date:
    """Parse one `Logged:` value, refusing anything the format does not cover."""
    text = raw.strip()
    try:
        parsed = datetime.strptime(text, _DATE_FORMAT).date()  # noqa: DTZ007 -- a calendar day, not an instant
    except ValueError as exc:
        raise ValueError(
            f"{path} has an unparseable Logged date {text!r}; "
            f"the format is 'Ddd D Mmm YYYY', for example 'Sat 5 Sep 2026'"
        ) from exc
    # The weekday is a checksum on the number, not decoration. Recomputing it
    # silently would let a typo published as fact survive a regeneration.
    if not text.startswith(parsed.strftime("%a")):
        raise ValueError(
            f"{path} logs {text!r}, but that date is a "
            f"{parsed.strftime('%A')}; correct the weekday or the day"
        )
    return parsed


def _entries(text: str, path: Path) -> list[date]:
    """Return one logged date per entry, refusing incomplete entries.

    An entry starts at its `Logged:` line and runs to the next one. Counting
    entries rather than permalinks is what keeps the index honest: one entry
    may cite several review rounds, and debt found outside a review thread
    cites none at all.
    """
    starts = [(m.start(), m.group(1)) for m in _LOGGED.finditer(text)]
    if not starts:
        raise ValueError(f"{path} is missing Logged metadata; every entry needs 'Logged: <date>'")
    bounds = [start for start, _ in starts] + [len(text)]
    dates: list[date] = []
    for index, (_, raw) in enumerate(starts):
        block = text[bounds[index] : bounds[index + 1]]
        if _SOURCE.search(block) is None:
            raise ValueError(
                f"{path} entry logged {raw.strip()!r} is missing its "
                f"'Source: <permalink or provenance>' line"
            )
        dates.append(_logged_date(raw, path))
    return dates


def _entry(path: Path) -> DebtFile:
    """Read one debt file, raising on any metadata it does not actually record."""
    if not path.stem.isdigit():
        raise ValueError(
            f"{path} is not named for a pull request; triage reads .planning/debt/<pr>.md, "
            f"so the filename must be the PR number"
        )
    text = path.read_text(encoding="utf-8")
    match = _PR.search(text)
    if match is None:
        raise ValueError(f"{path} must start with a PR or Issue number heading")
    number = int(match.group(1))
    if number != int(path.stem):
        raise ValueError(
            f"{path} declares PR #{number} but triage would read it as PR #{int(path.stem)}; "
            f"rename the file or correct the heading"
        )
    branch = _BRANCH.search(text)
    if branch is None:
        raise ValueError(f"{path} is missing Branch metadata: add a 'Branch: `<branch>`' line")
    dates = _entries(text, path)
    return DebtFile(number=number, branch=branch.group(1), count=len(dates), newest=max(dates))


# ----- rendering ----------------------------------------------------------


def _render_date(value: date) -> str:
    """Render a date the way the repository writes them: `Sat 5 Sep 2026`."""
    return f"{value.strftime('%a')} {value.day} {value.strftime('%b %Y')}"


def render_index(debt_dir: Path = DEBT_DIR) -> str:
    """Return the deterministic, complete index for `debt_dir`."""
    entries = sorted((_entry(path) for path in debt_dir.glob("*.md")), key=lambda item: item.number)
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
        for entry in entries
    )
    lines.extend(
        [
            "",
            f"{sum(entry.count for entry in entries)} entries across {len(entries)} pull requests.",
        ]
    )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--check", action="store_true", help="fail when the index is stale")
    args = parser.parse_args(argv)
    rendered = render_index()
    current = INDEX.read_text(encoding="utf-8") if INDEX.exists() else ""
    if args.check:
        if current != rendered:
            print("[ERROR] .planning/TECH-DEBT.md is stale; run python -m scripts.debt_index")
            return 1
        print("[OK] .planning/TECH-DEBT.md is generated and current")
        return 0
    INDEX.write_text(rendered, encoding="utf-8")
    print(f"[OK] regenerated {INDEX}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
