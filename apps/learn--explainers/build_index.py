"""Compile apps/learn--explainers/terms/*.md into index.json (stdlib only).

Run from the repository root: python apps/learn--explainers/build_index.py
Fails loudly on a malformed entry: a missing field, a picture wider than 72 columns or taller
than 24 lines, a banned character, or a slug that does not match its file name.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TERMS = ROOT / "terms"
ANIM = ROOT / "animations"
OUT = ROOT / "index.json"
REQUIRED = (
    "term",
    "slug",
    "aliases",
    "group",
    "level",
    "one_liner",
    "why_it_matters_here",
    "related",
    "sources",
    "status",
)
GROUPS = (
    "Filters and EQ",
    "Loudness and gain",
    "Time, sync and loops",
    "Harmony and analysis",
    "Evaluation and method",
    "Engineering and product",
    "People and papers",
)
BANNED = (chr(0x2013), chr(0x2014))


def _split_front_matter(text: str, path: Path) -> tuple[dict, str]:
    if not text.startswith("---\n"):
        sys.exit(f"{path}: no front matter")
    end = text.find("\n---\n", 4)
    if end < 0:
        sys.exit(f"{path}: unterminated front matter")
    return _parse_yaml_lite(text[4:end], path), text[end + 5 :]


def _parse_yaml_lite(block: str, path: Path) -> dict:
    """Enough YAML for the template: scalars, inline lists, and block lists of scalars."""
    data: dict = {}
    key = None
    for raw in block.splitlines():
        if not raw.strip():
            continue
        if raw.startswith("  - ") and key is not None:
            data.setdefault(key, []).append(raw[4:].strip().strip('"'))
            continue
        m = re.match(r"^([a-z_]+):\s*(.*)$", raw)
        if not m:
            sys.exit(f"{path}: cannot parse front matter line: {raw!r}")
        key, value = m.group(1), m.group(2).strip()
        if value == "":
            data[key] = []
        elif value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            data[key] = [v.strip().strip('"').strip("'") for v in inner.split(",")] if inner else []
        else:
            data[key] = value.strip('"').strip("'")
    return data


def _section(body: str, heading: str, path: Path) -> str:
    m = re.search(rf"^## {re.escape(heading)}\s*\n(.*?)(?=^## |\Z)", body, flags=re.S | re.M)
    if not m:
        sys.exit(f"{path}: missing section '## {heading}'")
    return m.group(1).strip()


def _picture(section: str, path: Path) -> str:
    m = re.search(r"```text\n(.*?)```", section, flags=re.S)
    if not m:
        sys.exit(f"{path}: Picture section has no ```text block")
    pic = m.group(1).rstrip("\n")
    lines = pic.splitlines()
    if len(lines) > 24 or any(len(line) > 72 for line in lines):
        sys.exit(f"{path}: picture exceeds 24 lines by 72 columns")
    if not _ascii(lines):
        sys.exit(f"{path}: picture is not plain ASCII")
    return pic


def _ascii(lines: list[str]) -> bool:
    return all(ord(ch) <= 126 for line in lines for ch in line)


def _fits(lines: list[str]) -> bool:
    return len(lines) <= 24 and all(len(line) <= 72 for line in lines) and _ascii(lines)


def _animation(slug: str) -> list[str] | None:
    p = ANIM / f"{slug}.txt"
    if not p.exists():
        return None
    frames = [f.strip("\n") for f in p.read_text(encoding="utf-8").split("\n@@\n")]
    frames = [f for f in frames if f.strip()]
    for f in frames:
        lines = f.splitlines()
        if not _fits(lines):
            sys.exit(f"{p}: a frame exceeds 24 by 72 or is not plain ASCII")
    return frames or None


def _validate(fm: dict, path: Path) -> None:
    missing = [k for k in REQUIRED if k not in fm]
    if missing:
        sys.exit(f"{path}: missing front matter fields {missing}")
    if fm["slug"] != path.stem:
        sys.exit(f"{path}: slug {fm['slug']!r} does not match file name")
    if fm["group"] not in GROUPS:
        sys.exit(f"{path}: unknown group {fm['group']!r}")
    if str(fm["level"]) not in {"1", "2", "3"}:
        sys.exit(f"{path}: level must be 1, 2 or 3")
    if len(fm["one_liner"]) > 160:
        sys.exit(f"{path}: one_liner over 160 characters")


def _entry(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if any(b in text for b in BANNED):
        sys.exit(f"{path}: banned dash character")
    fm, body = _split_front_matter(text, path)
    _validate(fm, path)
    return {
        "term": fm["term"],
        "slug": fm["slug"],
        "aliases": fm["aliases"],
        "group": fm["group"],
        "level": int(fm["level"]),
        "one_liner": fm["one_liner"],
        "why_it_matters_here": fm["why_it_matters_here"],
        "related": fm["related"],
        "sources": fm["sources"],
        "status": fm["status"],
        "plain": _section(body, "In plain words", path),
        "picture": _picture(_section(body, "Picture", path), path),
        "in_open_dj": _section(body, "In Open DJ", path),
        "animation": _animation(fm["slug"]),
    }


def main() -> None:
    entries = [_entry(path) for path in sorted(TERMS.glob("*.md"))]
    slugs = {e["slug"] for e in entries}
    dangling = sorted({r for e in entries for r in e["related"] if r and r not in slugs})
    index = {
        "generated_from": "apps/learn--explainers/terms",
        "groups": list(GROUPS),
        "count": len(entries),
        "dangling_related": dangling,
        "entries": entries,
    }
    OUT.write_text(json.dumps(index, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"[OK] {len(entries)} entries -> {OUT.relative_to(ROOT.parent.parent)}; dangling related: {len(dangling)}")
    if dangling:
        print("     " + ", ".join(dangling))


if __name__ == "__main__":
    main()
