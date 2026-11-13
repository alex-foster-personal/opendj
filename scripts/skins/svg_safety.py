"""Refuse unsafe SVG assets in a skin package (PLAN.md 3.5 safe loading).

Pure standard library, no network. A DOCTYPE or entity declaration is refused
before parsing, so no entity is ever expanded.
"""

from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

FORBIDDEN_ELEMENTS = {"script", "foreignObject"}
_DOCTYPE_RE = re.compile(rb"<!(DOCTYPE|ENTITY)", re.IGNORECASE)
_URL_RE = re.compile(r"url\(\s*['\"]?([^'\")]*)", re.IGNORECASE)


@dataclass(frozen=True)
class Violation:
    rule: str
    detail: str
    line: int | None = None


def _local(name: str) -> str:
    return name.rsplit("}", 1)[-1]


def _check_style(value: str, line: int | None, out: list[Violation]) -> None:
    if "@import" in value.lower():
        out.append(Violation("style-import", value, line))
    for target in _URL_RE.findall(value):
        if not target.strip().startswith("#"):
            out.append(Violation("style-url", target, line))


def check_svg_asset(path: str | Path) -> list[Violation]:
    data = Path(path).read_bytes()
    m = _DOCTYPE_RE.search(data)
    if m:
        line = data[: m.start()].count(b"\n") + 1
        return [Violation("doctype", m.group(1).decode().upper(), line)]
    out: list[Violation] = []
    parser = ET.XMLPullParser(events=("start",))
    lines = data.decode("utf-8", errors="replace").splitlines(keepends=True)
    # Feed line by line so each start event can be tagged with a line number.
    for lineno, text in enumerate(lines, 1):
        parser.feed(text)
        for _event, el in parser.read_events():
            tag = _local(el.tag)
            if tag in FORBIDDEN_ELEMENTS:
                out.append(Violation("element", tag, lineno))
            if tag == "style" and el.text:
                _check_style(el.text, lineno, out)
            for attr, value in el.attrib.items():
                name = _local(attr)
                if name == "href" and not value.startswith("#"):
                    out.append(Violation("href", f"{attr}={value}", lineno))
                elif name.lower().startswith("on"):
                    out.append(Violation("event-handler", f"{name}={value}", lineno))
                elif name == "style":
                    _check_style(value, lineno, out)
    parser.close()
    return out


def main(argv: list[str] | None = None) -> int:
    bad = 0
    for p in argv if argv is not None else sys.argv[1:]:
        for v in check_svg_asset(p):
            bad += 1
            print(f"{p}:{v.line}: {v.rule}: {v.detail}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
