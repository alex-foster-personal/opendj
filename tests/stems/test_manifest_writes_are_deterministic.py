"""Every stem bundle manifest writer emits the same bytes on every platform.

[if] a production writer of a bundle's manifest.json omits UTF-8 or LF [then] this fails naming the call, [else stop].

``Path.write_text`` translates "\\n" to CRLF on Windows and picks the locale
codec, so an identical bundle would hash differently there (Codex on #6820).
Read from the real modules' source: the translation itself only happens on
Windows, which no pull-request lane runs.

-Claude
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.requirement("STEM-58")

ROOT = Path(__file__).resolve().parents[2]
WRITERS = (
    "apps/stems/bundle_publish.py",
    "apps/stems/recompress.py",
    "apps/lyrics/register_stems.py",
    "scripts/stem_bundle_worker.py",
    "scripts/modal_vocal_farm.py",
)


def _manifest_writes(path: Path) -> list[ast.Call]:
    source = path.read_text(encoding="utf-8")
    calls = []
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "write_text":
            continue
        target = ast.get_source_segment(source, node.func.value) or ""
        if "manifest" in target:
            calls.append(node)
    return calls


def _keyword(call: ast.Call, name: str) -> object:
    for keyword in call.keywords:
        if keyword.arg == name and isinstance(keyword.value, ast.Constant):
            return keyword.value.value
    return None


@pytest.mark.parametrize("rel", WRITERS)
def test_each_manifest_write_pins_utf8_and_lf(rel: str) -> None:
    calls = _manifest_writes(ROOT / rel)
    assert calls, f"{rel}: found no manifest write, so this check would pass for the wrong reason"
    loose = [
        f"{rel}:{call.lineno}"
        for call in calls
        if _keyword(call, "encoding") != "utf-8" or _keyword(call, "newline") != "\n"
    ]
    assert not loose, f"manifest writes without encoding='utf-8', newline='\\n': {loose}"


def test_every_recompress_file_write_pins_utf8_and_lf() -> None:
    """[if] the re-compress pass writes any text file, its --json report included [then] it pins UTF-8 and LF, [else stop].

    Identical results must give identical report bytes on every platform (Codex on #6820).
    """
    calls = []
    # The pass writes the manifest; its command line writes the --json report.
    for rel in ("apps/stems/recompress.py", "apps/stems/recompress_cli.py"):
        source = (ROOT / rel).read_text(encoding="utf-8")
        calls += [
            (rel, node)
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "write_text"
        ]
    assert len(calls) >= 2, "found fewer than the manifest and report writes: the probe is not reading the modules"
    loose = [
        f"{rel}:{call.lineno}"
        for rel, call in calls
        if _keyword(call, "encoding") != "utf-8" or _keyword(call, "newline") != "\n"
    ]
    assert not loose, f"writes without encoding='utf-8', newline='\\n': {loose}"
