"""Recount the Electron parity matrix rows against the Summary and validate tracking cells."""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MATRIX = REPO_ROOT / "specs" / "electron-parity-matrix.md"
STATUSES = ("PARITY", "PARTIAL", "MISSING", "NOT-NEEDED")
TRACKING_HEADER = "Tracking issues"
TRACKING_ISSUE_RE = re.compile(r"#\d+")
TRACKING_PERSON_RE = re.compile(r"^none, needs a person: \S.*")


@dataclass(frozen=True)
class MatrixRow:
    number: int
    status: str
    tracking: str | None
    line: int


def split_cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def section(
    lines: list[str],
    start_prefix: str,
    stop_prefix: str,
) -> tuple[list[tuple[int, str]], list[str]]:
    problems: list[str] = []
    start_idx: int | None = None
    for idx, line in enumerate(lines):
        if line.startswith(start_prefix):
            start_idx = idx + 1
            break
    if start_idx is None:
        problems.append(f"missing heading {start_prefix!r}")
        return [], problems
    stop_idx = len(lines)
    for idx in range(start_idx, len(lines)):
        if lines[idx].startswith(stop_prefix):
            stop_idx = idx
            break
    span = [(lineno, text) for lineno, text in enumerate(lines[start_idx:stop_idx], start=start_idx + 1)]
    return span, problems


def _is_separator(cells: list[str]) -> bool:
    return bool(cells) and all(set(c) <= {"-", ":"} and c for c in cells)


def _header_column_indices(cells: list[str]) -> tuple[int | None, int | None]:
    status_idx: int | None = None
    tracking_idx: int | None = None
    for i, name in enumerate(cells):
        if name == "Status":
            status_idx = i
        elif name == TRACKING_HEADER:
            tracking_idx = i
    return status_idx, tracking_idx


def _matrix_row_from_cells(
    line_no: int,
    cells: list[str],
    header_cols: int,
    status_idx: int | None,
    tracking_idx: int | None,
    problems: list[str],
) -> MatrixRow | None:
    row_num = int(cells[0])
    if len(cells) != header_cols and header_cols:
        problems.append(f"line {line_no}: row {row_num} has {len(cells)} cells, header has {header_cols}")
    if status_idx is None:
        problems.append(f"line {line_no}: row {row_num} has no Status column in this table")
        return None
    status = cells[status_idx] if status_idx < len(cells) else ""
    tracking: str | None = None
    if tracking_idx is not None and tracking_idx < len(cells):
        tracking = cells[tracking_idx]
    return MatrixRow(number=row_num, status=status, tracking=tracking, line=line_no)


def parse_matrix_rows(span: list[tuple[int, str]]) -> tuple[list[MatrixRow], list[str]]:
    problems: list[str] = []
    rows: list[MatrixRow] = []
    status_idx: int | None = None
    tracking_idx: int | None = None
    header_cols = 0

    for line_no, line in span:
        if not line.lstrip().startswith("|"):
            continue
        cells = split_cells(line)
        if not cells:
            continue
        if cells[0] == "#":
            header_cols = len(cells)
            status_idx, tracking_idx = _header_column_indices(cells)
            if tracking_idx is None:
                problems.append(f"table at line {line_no} has no 'Tracking issues' column")
            continue
        if _is_separator(cells) or not cells[0].isdigit():
            continue
        row = _matrix_row_from_cells(line_no, cells, header_cols, status_idx, tracking_idx, problems)
        if row is not None:
            rows.append(row)

    return rows, problems


def _parse_summary_value(label: str, value_raw: str, problems: list[str]) -> int | None:
    try:
        return int(value_raw)
    except ValueError:
        problems.append(f"Summary {label} row has non-integer value {value_raw!r}")
        return None


def _ingest_summary_row(
    label: str,
    value_raw: str,
    counts: dict[str, int],
    problems: list[str],
) -> int | None:
    if label == "Total":
        return _parse_summary_value("Total", value_raw, problems)
    if label in STATUSES:
        parsed = _parse_summary_value(label, value_raw, problems)
        if parsed is not None:
            counts[label] = parsed
        return None
    if label:
        problems.append(f"Summary counts table has unknown status {label!r}")
    return None


def parse_summary_counts(
    span: list[tuple[int, str]],
) -> tuple[dict[str, int], int | None, list[str]]:
    problems: list[str] = []
    counts: dict[str, int] = {}
    total: int | None = None
    in_table = False

    for _line_no, line in span:
        if not line.lstrip().startswith("|"):
            if in_table:
                break
            continue
        cells = split_cells(line)
        if len(cells) >= 2 and cells[0].strip("*") == "Status" and cells[1].strip("*") == "Rows":
            in_table = True
            continue
        if not in_table or _is_separator(cells):
            continue
        label = cells[0].strip("*").strip()
        value_raw = cells[1].strip("*").strip()
        maybe_total = _ingest_summary_row(label, value_raw, counts, problems)
        if maybe_total is not None:
            total = maybe_total

    if not counts and total is None:
        problems.append("Summary has no '| Status | Rows |' counts table")
    return counts, total, problems


def parse_summary_not_parity(span: list[tuple[int, str]]) -> tuple[set[int], list[str]]:
    problems: list[str] = []
    found = False
    rows: set[int] = set()

    for _line_no, line in span:
        if not line.lstrip().startswith("|"):
            continue
        cells = split_cells(line)
        if len(cells) >= 2 and cells[0] == "Row" and cells[1] == "Capability":
            found = True
            continue
        if not found:
            continue
        if _is_separator(cells):
            continue
        if cells[0].isdigit():
            rows.add(int(cells[0]))
        elif cells[0] not in ("Row", ""):
            break

    if not found:
        problems.append("Summary has no 'Not PARITY yet' table (| Row | Capability |)")
    return rows, problems


def recount(rows: list[MatrixRow]) -> dict[str, int]:
    out = {s: 0 for s in STATUSES}
    for row in rows:
        if row.status in out:
            out[row.status] += 1
    return out


def _tracking_valid(tracking: str | None) -> bool:
    if not tracking:
        return False
    if TRACKING_ISSUE_RE.search(tracking):
        return True
    return TRACKING_PERSON_RE.match(tracking) is not None


def _index_rows(rows: list[MatrixRow], problems: list[str]) -> dict[int, MatrixRow]:
    by_number: dict[int, MatrixRow] = {}
    for row in rows:
        if row.number in by_number:
            problems.append(f"row {row.number} appears twice")
        else:
            by_number[row.number] = row
        if row.status not in STATUSES:
            problems.append(f"row {row.number}: unknown status {row.status!r}")
    if by_number:
        max_num = max(by_number)
        problems.extend(f"row {n} is missing" for n in range(1, max_num + 1) if n not in by_number)
    return by_number


def _check_status_counts(
    rows: list[MatrixRow],
    summary_counts: dict[str, int],
    summary_total: int | None,
    problems: list[str],
) -> None:
    actual = recount(rows)
    for status in STATUSES:
        expected = summary_counts.get(status, 0)
        got = actual.get(status, 0)
        if got != expected:
            problems.append(f"{status}: rows say {got}, Summary says {expected}")
    if summary_total is not None and len(rows) != summary_total:
        problems.append(f"Total: {len(rows)} rows in the matrix, Summary says {summary_total}")


def _check_not_parity_list(
    by_number: dict[int, MatrixRow],
    rows: list[MatrixRow],
    not_parity_rows: set[int],
    problems: list[str],
) -> None:
    status_by_row = {r.number: r.status for r in rows}
    for row_num, row in by_number.items():
        if row.status in ("PARTIAL", "MISSING") and row_num not in not_parity_rows:
            problems.append(
                f"row {row_num}: matrix says {row.status}, "
                "the Summary's 'Not PARITY yet' table does not list it"
            )
    for row_num in not_parity_rows:
        matrix_status = status_by_row.get(row_num)
        if matrix_status is None:
            problems.append(
                f"row {row_num}: the Summary's 'Not PARITY yet' table lists it, matrix has no such row"
            )
        elif matrix_status in ("PARITY", "NOT-NEEDED"):
            problems.append(
                f"row {row_num}: the Summary's 'Not PARITY yet' table lists it, matrix says {matrix_status}"
            )


def _check_tracking_cells(rows: list[MatrixRow], problems: list[str]) -> None:
    for matrix_row in rows:
        if matrix_row.status not in ("PARTIAL", "MISSING") or _tracking_valid(matrix_row.tracking):
            continue
        tracking_got = matrix_row.tracking if matrix_row.tracking is not None else ""
        problems.append(
            f"row {matrix_row.number}: {matrix_row.status} row needs an open issue number or "
            f"'none, needs a person: <reason>' in Tracking issues, got {tracking_got!r}"
        )


def check(text: str) -> list[str]:
    lines = text.splitlines()
    problems: list[str] = []

    matrix_span, matrix_sec_probs = section(lines, "## Matrix", "## Gaps")
    problems.extend(matrix_sec_probs)
    summary_span, summary_sec_probs = section(lines, "## Summary", "## Matrix")
    problems.extend(summary_sec_probs)

    rows, row_probs = parse_matrix_rows(matrix_span)
    problems.extend(row_probs)
    summary_counts, summary_total, count_probs = parse_summary_counts(summary_span)
    problems.extend(count_probs)
    not_parity_rows, not_parity_probs = parse_summary_not_parity(summary_span)
    problems.extend(not_parity_probs)

    by_number = _index_rows(rows, problems)
    _check_status_counts(rows, summary_counts, summary_total, problems)
    _check_not_parity_list(by_number, rows, not_parity_rows, problems)
    _check_tracking_cells(rows, problems)

    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Recount the Electron parity matrix against its Summary.")
    parser.add_argument("--matrix", type=Path, default=DEFAULT_MATRIX, help="Path to electron-parity-matrix.md")
    args = parser.parse_args(argv)

    path = args.matrix
    if not path.is_file():
        print(f"electron parity matrix: file not found: {path}", file=sys.stderr)
        return 2

    text = path.read_text(encoding="utf-8")
    problems = check(text)
    if problems:
        print(f"electron parity matrix: {len(problems)} problem(s) in {path}", file=sys.stderr)
        for problem in problems:
            print(problem, file=sys.stderr)
        return 1

    lines = text.splitlines()
    matrix_span, _ = section(lines, "## Matrix", "## Gaps")
    rows, _ = parse_matrix_rows(matrix_span)
    actual = recount(rows)
    print(
        "electron parity matrix OK: "
        f"{len(rows)} rows, "
        f"PARITY {actual['PARITY']}, "
        f"PARTIAL {actual['PARTIAL']}, "
        f"MISSING {actual['MISSING']}, "
        f"NOT-NEEDED {actual['NOT-NEEDED']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
