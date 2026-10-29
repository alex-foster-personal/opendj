"""Inject the explainer glossary sidebar into an HTML page.

Usage: python3 inject_sidebar.py <index.json> <in.html> <out.html>

The page must contain exactly one </main> and an inline <style> block. The sidebar's CSS
(sidebar/sidebar.css) is appended to the first style block; the markup (sidebar/sidebar.html)
and script (sidebar/sidebar.js) go right after </main>, with the index embedded as JSON. The
page's own CSS must define the tokens --panel, --bg, --ink, --ink-dim, --rule, --accent,
--code-bg, --body and --mono in its light and dark theme blocks. Viewers get a Terms button,
search, grouped entries with the picture and the frame animation, related-term jumps, dotted
term links in the main text (first occurrence of each term or alias, plus any hand-placed
<button class="term-link" data-slug="..."> the page carries) and a draggable sidebar width.
Built for the transitions research brief on Tue 27 Oct 2026; any page with one <main> and those
tokens can use it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ASSETS = Path(__file__).resolve().parent / "sidebar"
BANNED = (chr(0x2013), chr(0x2014))


def _payload(index_path: Path) -> tuple[str, int]:
    index = json.loads(index_path.read_text(encoding="utf-8"))
    assert "groups" in index, "index.json carries no groups list; rebuild it with build_index.py"
    entries = index["entries"]
    text = json.dumps({"groups": index["groups"], "entries": entries}, ensure_ascii=False)
    return text.replace("</", "<\\/"), len(entries)


def _sidebar_block(payload: str, count: int) -> str:
    shell = (ASSETS / "sidebar.html").read_text(encoding="utf-8")
    script = (ASSETS / "sidebar.js").read_text(encoding="utf-8")
    shell = shell.replace("__COUNT__", str(count)).replace("__PAYLOAD__", payload)
    return "\n" + shell + "<script>\n" + script + "</script>\n"


def inject(index_path: Path, in_path: Path, out_path: Path) -> None:
    html = in_path.read_text(encoding="utf-8")
    assert html.count("</main>") == 1, "the page needs exactly one </main>"
    assert "</style>" in html, "the page needs an inline <style> block for the sidebar CSS"
    assert 'id="gloss-side"' not in html, "the page already carries a sidebar; strip it first"
    css = (ASSETS / "sidebar.css").read_text(encoding="utf-8")
    payload, count = _payload(index_path)
    block = _sidebar_block(payload, count)
    assert not any(bad in css + block for bad in BANNED)
    html = html.replace("</style>", "\n" + css + "</style>", 1)
    html = html.replace("</main>", "</main>" + block, 1)
    out_path.write_text(html, encoding="utf-8")
    print(f"[OK] sidebar injected: {count} entries, {len(html.encode())} bytes -> {out_path}")


if __name__ == "__main__":
    inject(*map(Path, sys.argv[1:4]))
