"""Weekday checksum guard for ``.planning/REQUIREMENTS.md``.

House rule: every ``Ddd D Mon YYYY`` date carries its weekday as a checksum on
the calendar day (see ``scripts/debt_index.py``). This module scans
REQUIREMENTS.md and reports lines where the weekday disagrees with the day
number.

Allowlist keys:
- When the line contains ``**REQ-ID**``, key is ``(req_id, written_date_text)``.
- Otherwise key is ``(line_fingerprint, written_date_text)`` where the
  fingerprint is the first 60 stripped characters of the source line.

Requirements (mini-PRD)
- [if] a date's weekday disagrees with its day number [then] ``scan_requirements``
  reports a ``DateMismatch``, [else stop]
- [if] ``--check`` runs with uncovered mismatches [then] exit 1, [else stop]
- [if] an allowlist entry names a row that is no longer mismatched [then]
  ``validate_allowlist`` reports it as stale, [else stop]
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = ROOT / ".planning" / "REQUIREMENTS.md"

# Changelog lines prefix dates with ``_``; ``\b`` alone misses ``_Fri`` because ``_`` is a word char.
_DATE_RE = re.compile(
    r"(?<![A-Za-z])(Mon|Tue|Wed|Thu|Fri|Sat|Sun) (\d{1,2}) (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) (\d{4})\b"
)
_DATE_FORMAT = "%a %d %b %Y"
_REQ_RE = re.compile(r"\*\*([A-Z][A-Z0-9]+-\d+)\*\*")
_FINGERPRINT_LEN = 60

# Shrinks to frozenset() once every mismatch in REQUIREMENTS.md is corrected.
KNOWN_WEEKDAY_MISMATCHES: frozenset[tuple[str, str]] = frozenset()


@dataclass(frozen=True)
class DateMismatch:
    line_no: int
    req_id: str | None
    written: str
    expected_weekday: str
    line_text: str
    line_fingerprint: str

    @property
    def allowlist_key(self) -> tuple[str, str]:
        if self.req_id:
            return (self.req_id, self.written)
        return (self.line_fingerprint, self.written)


def _line_fingerprint(line: str) -> str:
    return line.strip()[:_FINGERPRINT_LEN]


def _weekday_ok(written: str) -> tuple[bool, str]:
    parsed = datetime.strptime(written, _DATE_FORMAT)  # noqa: DTZ007 -- calendar day
    expected = parsed.strftime("%a")
    return written.startswith(expected), expected


def scan_requirements(path: Path = REQUIREMENTS) -> list[DateMismatch]:
    """Return every weekday checksum mismatch in REQUIREMENTS.md."""
    text = path.read_text(encoding="utf-8")
    mismatches: list[DateMismatch] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        req_match = _REQ_RE.search(line)
        req_id = req_match.group(1) if req_match else None
        fingerprint = _line_fingerprint(line)
        for match in _DATE_RE.finditer(line):
            written = match.group(0)
            ok, expected = _weekday_ok(written)
            if not ok:
                mismatches.append(
                    DateMismatch(
                        line_no=line_no,
                        req_id=req_id,
                        written=written,
                        expected_weekday=expected,
                        line_text=line.strip(),
                        line_fingerprint=fingerprint,
                    )
                )
    return mismatches


def validate_allowlist(
    mismatches: list[DateMismatch],
    allowlist: frozenset[tuple[str, str]],
) -> list[str]:
    """Return human-readable errors for allowlist drift (uncovered or stale)."""
    live_keys = {m.allowlist_key for m in mismatches}
    errors: list[str] = []
    uncovered = live_keys - allowlist
    for key in sorted(uncovered):
        errors.append(f"uncovered mismatch: {key[0]!r} {key[1]!r}")
    stale = allowlist - live_keys
    for key in sorted(stale):
        errors.append(f"stale allowlist entry: {key[0]!r} {key[1]!r}")
    return errors


def format_mismatch(m: DateMismatch) -> str:
    who = m.req_id or m.line_fingerprint
    return (
        f"line {m.line_no}: {who} wrote {m.written!r} "
        f"but that day is a {m.expected_weekday}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 when mismatches are not fully covered by the allowlist",
    )
    parser.add_argument(
        "--list-mismatches",
        action="store_true",
        help="print every live mismatch",
    )
    args = parser.parse_args(argv)
    mismatches = scan_requirements()
    if args.list_mismatches or not (args.check):
        if not mismatches:
            print("[OK] no weekday checksum mismatches")
        else:
            for m in mismatches:
                print(format_mismatch(m))
    if args.check:
        errors = validate_allowlist(mismatches, KNOWN_WEEKDAY_MISMATCHES)
        if errors:
            for err in errors:
                print(f"[ERROR] {err}")
            return 1
        if mismatches and not KNOWN_WEEKDAY_MISMATCHES:
            print("[ERROR] mismatches remain but allowlist is empty")
            return 1
        print(
            f"[OK] {len(mismatches)} mismatch(es) covered by allowlist "
            f"({len(KNOWN_WEEKDAY_MISMATCHES)} entries)"
            if mismatches
            else "[OK] REQUIREMENTS.md weekday checksums are clean"
        )
        return 0
    return 0 if not mismatches else 1


if __name__ == "__main__":
    raise SystemExit(main())
