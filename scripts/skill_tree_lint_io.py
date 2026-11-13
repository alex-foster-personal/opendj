"""UTF-8 file reads and reference-line filtering for skill_tree_lint."""

from __future__ import annotations

from pathlib import Path

from scripts.export_global_skills import doc_line_mask, is_markdown


def _read(path: Path) -> str:
    """A file's text, decoded strictly: a replaced byte could alter a
    reference or a line and let the ratchet undercount while reporting
    success, so an undecodable file aborts the scan and names itself."""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"skill_tree_lint: {path} is not valid UTF-8 ({exc})") from exc


def _ref_lines(path: Path) -> list[str]:
    """Lines that can carry a reference, with documentation filtered out."""
    lines = _read(path).splitlines()
    return [
        line for line, is_doc in zip(lines, doc_line_mask(lines, markdown=is_markdown(path)), strict=True) if not is_doc
    ]


def _line_count(path: Path) -> int:
    return len(_read(path).splitlines())
