"""DECKUX-8350: HTML5 drag and drop reaches the web UI inside the Tauri shell.

the maintainer, Tue 3 Nov 2026: dragging a library track onto a deck stopped working in
the installed app. With Tauri's native drag-drop handler on, wry's
`draggingEntered:` / `performDragOperation:` overrides answer every drag and
never call WebKit's, so no `dragover` or `drop` reaches the page.

- if the main window keeps Tauri's drag-drop handler then deck, playlist and
  Finder-file drops never fire in the desktop app -> broken
- if the frontend starts relying on Tauri's native drop events while the handler
  is off then those listeners never fire -> broken

-Claude
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MAIN_RS = ROOT / "apps" / "desktop" / "src-tauri" / "src" / "main.rs"
FRONTEND_SRC = ROOT / "apps" / "webui" / "frontend" / "src"


def test_main_window_disables_tauri_drag_drop_handler() -> None:
    text = MAIN_RS.read_text(encoding="utf-8")
    start = text.index("WebviewWindowBuilder::new(app, MAIN_WINDOW_LABEL")
    chain = text[start : text.index(".build()?", start)]
    assert ".disable_drag_drop_handler()" in chain


def test_frontend_never_listens_for_tauri_native_drop_events() -> None:
    pattern = re.compile(r"onDragDropEvent|tauri://drag-")
    offenders = [
        str(path.relative_to(ROOT))
        for path in FRONTEND_SRC.rglob("*")
        if path.suffix in {".ts", ".svelte", ".js"} and pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []
