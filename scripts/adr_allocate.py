"""Allocate numeric ADR ids at merge time (issue #3076).

Feature branches author ``docs/decisions/ADR-NEW-<slug>.md`` placeholders.
The merge lane runs ``--finalize`` to assign monotonic ``ADR-NNNN-<slug>.md``
names and regenerate ``docs/decisions/README.md``.

    python -m scripts.adr_allocate --check
    python -m scripts.adr_allocate --finalize
    python -m scripts.adr_allocate --build-index
    python -m scripts.adr_allocate --dry-run
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ADR_DIR = REPO_ROOT / "docs" / "decisions"
DEFAULT_README = DEFAULT_ADR_DIR / "README.md"

_NUMERIC_RE = re.compile(r"^ADR-(\d{4})-")
_NEW_RE = re.compile(r"^ADR-NEW-(.+)\.md$")
_H1_RE = re.compile(r"^#\s+ADR-(?:NEW|\d{4}):\s*(.+)$", re.MULTILINE)
_STATUS_INLINE_RE = re.compile(r"^Status:\s*(\S+)", re.MULTILINE | re.IGNORECASE)
_STATUS_HEADING_RE = re.compile(
    r"^##\s+Status\s*\n+\s*(\S+)", re.MULTILINE | re.IGNORECASE
)
_DATE_INLINE_RE = re.compile(
    r"^Date:\s*(\w{3}\s+\d{1,2}\s+\w{3}\s+\d{4})", re.MULTILINE
)
_DATE_HEADING_RE = re.compile(
    r"^##\s+Date\s*\n+\s*(\w{3}\s+\d{1,2}\s+\w{3}\s+\d{4})", re.MULTILINE
)
_SUPERSEDED_RE = re.compile(
    r"superseded-number:.*?renumbered to ADR-(\d{4})",
    re.IGNORECASE,
)


def next_numeric_id(adr_dir: Path) -> str:
    """Return the next free four-digit id (e.g. ``0073``)."""
    highest = 0
    if adr_dir.is_dir():
        for path in adr_dir.glob("ADR-*.md"):
            match = _NUMERIC_RE.match(path.name)
            if match:
                highest = max(highest, int(match.group(1)))
    return f"{highest + 1:04d}"


def list_pending_new(adr_dir: Path) -> list[Path]:
    if not adr_dir.is_dir():
        return []
    return sorted(adr_dir.glob("ADR-NEW-*.md"))


def _parse_title(text: str) -> str:
    match = _H1_RE.search(text)
    return match.group(1).strip() if match else "untitled"


def _parse_status(text: str) -> str:
    match = _STATUS_INLINE_RE.search(text) or _STATUS_HEADING_RE.search(text)
    return match.group(1).strip() if match else "proposed"


def _parse_date(text: str) -> str:
    match = _DATE_INLINE_RE.search(text) or _DATE_HEADING_RE.search(text)
    return match.group(1).strip() if match else "unknown"


def _rewrite_h1(text: str, adr_id: str, title: str) -> str:
    replacement = f"# ADR-{adr_id}: {title}"
    if _H1_RE.search(text):
        return _H1_RE.sub(replacement, text, count=1)
    return f"{replacement}\n\n{text.lstrip()}"


def finalize_pending(
    adr_dir: Path,
    *,
    dry_run: bool = False,
    repo_root: Path = REPO_ROOT,
) -> list[tuple[Path, Path]]:
    """Rename each ``ADR-NEW-<slug>.md`` to the next free numeric id."""
    planned: list[tuple[Path, Path]] = []
    next_id = int(next_numeric_id(adr_dir))

    for src in list_pending_new(adr_dir):
        slug_match = _NEW_RE.match(src.name)
        if not slug_match:
            continue
        slug = slug_match.group(1)
        adr_id = f"{next_id:04d}"
        dst = adr_dir / f"ADR-{adr_id}-{slug}.md"
        planned.append((src, dst))
        if dry_run:
            next_id += 1
            continue

        text = src.read_text(encoding="utf-8")
        title = _parse_title(text)
        text = _rewrite_h1(text, adr_id, title)
        dst.write_text(text, encoding="utf-8")
        src.unlink()
        next_id += 1

    return planned


def _adr_rows(adr_dir: Path) -> list[tuple[str, str, str, str, str]]:
    rows: list[tuple[str, str, str, str, str]] = []
    for path in sorted(adr_dir.glob("ADR-*.md")):
        match = _NUMERIC_RE.match(path.name)
        if not match:
            continue
        adr_id = match.group(1)
        text = path.read_text(encoding="utf-8")
        title = _parse_title(text)
        superseded = _SUPERSEDED_RE.search(text)
        if superseded:
            old = superseded.group(1)
            title = f"{title} (superseded-number, was ADR-{old})"
        rows.append((adr_id, path.name, _parse_status(text), _parse_date(text), title))
    return rows


def build_adr_index(adr_dir: Path, readme_path: Path) -> str:
    """Regenerate the ``## Index`` block in ``docs/decisions/README.md``."""
    readme = readme_path.read_text(encoding="utf-8")
    marker = "## Index"
    if marker not in readme:
        raise ValueError(f"{readme_path} is missing a {marker!r} section")

    before, _rest = readme.split(marker, 1)
    tail_marker = "When `docs/moc/README.md` lands"
    tail = ""
    if tail_marker in _rest:
        _, tail = _rest.split(tail_marker, 1)
        tail = tail_marker + tail

    rows = _adr_rows(adr_dir)
    next_id = next_numeric_id(adr_dir)
    lines = [
        marker,
        "",
        "| ID | Status | Date | Title |",
        "|---|---|---|---|",
    ]
    for adr_id, filename, status, date, title in rows:
        lines.append(
            f"| [ADR-{adr_id}]({filename}) | {status} | {date} | {title} |"
        )
    lines.extend(
        [
            "",
            f"Next number to allocate: **{next_id}**. (ADR-0041 is a pre-existing allocation",
            "gap with no file, unrelated to this renumbering.)",
            "",
        ]
    )
    if tail:
        lines.append(tail.lstrip("\n"))
    return before.rstrip() + "\n\n" + "\n".join(lines).rstrip() + "\n"


def write_adr_index(adr_dir: Path = DEFAULT_ADR_DIR, readme_path: Path = DEFAULT_README) -> None:
    readme_path.write_text(build_adr_index(adr_dir, readme_path), encoding="utf-8")


def check_no_pending(adr_dir: Path) -> list[str]:
    pending = list_pending_new(adr_dir)
    return [path.name for path in pending]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="adr_allocate")
    parser.add_argument("--adr-dir", default=str(DEFAULT_ADR_DIR))
    parser.add_argument("--readme", default=str(DEFAULT_README))
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--finalize", action="store_true")
    parser.add_argument("--build-index", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    adr_dir = Path(args.adr_dir)
    readme_path = Path(args.readme)

    if args.check:
        pending = check_no_pending(adr_dir)
        if pending:
            print(
                "[adr-allocate] ADR-NEW placeholder(s) remain on main: "
                + ", ".join(pending),
                file=sys.stderr,
            )
            return 1
        print("[adr-allocate] OK -- no ADR-NEW placeholders")
        return 0

    if args.dry_run or args.finalize:
        planned = finalize_pending(adr_dir, dry_run=args.dry_run)
        if not planned:
            print("[adr-allocate] no ADR-NEW placeholders to finalize")
            return 0
        for src, dst in planned:
            print(f"{src.name} -> {dst.name}")
        if args.finalize and not args.dry_run:
            print(f"[adr-allocate] finalized {len(planned)} placeholder(s)")
        return 0

    if args.build_index:
        write_adr_index(adr_dir, readme_path)
        print(f"[adr-allocate] regenerated {readme_path}")
        return 0

    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
