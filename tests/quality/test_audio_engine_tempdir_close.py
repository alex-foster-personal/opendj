"""Guard: audio-engine src unit tests must close tempfile TempDir values.

tempfile::TempDir's Drop ignores delete errors. A #[cfg(test)] block that
builds a scratch dir and lets it drop can pass green and leave the tree
behind. Call .close() (or a Drop that does) so a failed delete fails the test.

[if] a #[cfg(test)] item in apps/audio-engine/src constructs a tempfile
TempDir and never calls .close( [then] this scan names the construction,
[else stop].

Regression lines:
  - if git ls-files returns nothing then the scan is unmeasured, not clean
  - if a construction sits outside #[cfg(test)] then ignored
  - if a #[cfg(test)] body has only a websocket .close() then clean
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT: Path = Path(__file__).resolve().parents[2]

_CFG_TEST = "#[cfg(test)]"
_ITEM_START = frozenset(
    {
        "mod",
        "fn",
        "impl",
        "struct",
        "enum",
        "union",
        "trait",
        "type",
        "const",
        "static",
        "use",
        "macro_rules",
    }
)
_QUALIFIERS = frozenset({"unsafe", "async", "default"})
_CONST_FN = frozenset({"fn", "unsafe", "async"})
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_TEMPDIR_FN = re.compile(r"tempfile::tempdir\s*\(")
_TEMPDIR_NEW = re.compile(r"TempDir::new\s*\(")
_TEMPDIR_TY = re.compile(r"tempfile::TempDir\b")
_BUILDER = re.compile(r"tempfile::Builder\b")
_TEMPDIR_METHOD = re.compile(r"\.tempdir\s*\(")
_CLOSE = re.compile(r"\.close\s*\(")
_CONSTRUCT_RX = (_TEMPDIR_FN, _TEMPDIR_NEW, _TEMPDIR_TY)


def _is_ident_char(c: str) -> bool:
    return c.isalnum() or c == "_"


def _blank(chunk: str) -> str:
    return "".join("\n" if c == "\n" else " " for c in chunk)


def _line_comment_end(source: str, i: int) -> int:
    k = source.find("\n", i)
    return len(source) if k < 0 else k


def _block_comment_token(source: str, j: int) -> tuple[int, int] | None:
    if source.startswith("/*", j):
        return 1, j + 2
    if source.startswith("*/", j):
        return -1, j + 2
    return None


def _block_comment_end(source: str, i: int) -> int:
    depth = 1
    j = i + 2
    n = len(source)
    while j < n and depth:
        token = _block_comment_token(source, j)
        if token is None:
            j += 1
            continue
        depth += token[0]
        j = token[1]
    return j


def _skip_bc_prefix(source: str, j: int) -> int:
    if j < len(source) and source[j] in "bc":
        return j + 1
    return j


def _count_hashes(source: str, j: int) -> tuple[int, int]:
    hashes = 0
    n = len(source)
    while j < n and source[j] == "#":
        hashes += 1
        j += 1
    return hashes, j


def _raw_hashes(source: str, j: int) -> tuple[bool, int, int]:
    if j >= len(source) or source[j] != "r":
        return False, 0, j
    hashes, j = _count_hashes(source, j + 1)
    return True, hashes, j


def _string_open(source: str, i: int) -> tuple[int, bool, int] | None:
    if i > 0 and _is_ident_char(source[i - 1]):
        return None
    j = _skip_bc_prefix(source, i)
    raw, hashes, j = _raw_hashes(source, j)
    if j >= len(source) or source[j] != '"':
        return None
    return j, raw, hashes


def _cooked_string_end(source: str, j: int) -> int:
    n = len(source)
    j += 1
    while j < n:
        if source[j] == "\\":
            j += 2
            continue
        if source[j] == '"':
            return j + 1
        j += 1
    return n


def _raw_string_end(source: str, j: int, hashes: int) -> int:
    closer = '"' + "#" * hashes
    k = source.find(closer, j + 1)
    return len(source) if k < 0 else k + len(closer)


def _string_end(source: str, i: int) -> int | None:
    opened = _string_open(source, i)
    if opened is None:
        return None
    j, raw, hashes = opened
    if raw:
        return _raw_string_end(source, j, hashes)
    return _cooked_string_end(source, j)


def _escaped_char_end(source: str, j: int) -> int | None:
    n = len(source)
    if j >= n:
        return None
    if source[j] == "u" and j + 1 < n and source[j + 1] == "{":
        k = source.find("}", j)
        j = n if k < 0 else k + 1
    else:
        j += 1
    if j < n and source[j] == "'":
        return j + 1
    return None


def _char_body_end(source: str, j: int) -> int | None:
    n = len(source)
    if j >= n:
        return None
    if source[j] == "\\":
        return _escaped_char_end(source, j + 1)
    if j + 1 < n and source[j + 1] == "'":
        return j + 2
    return None


def _char_end(source: str, i: int) -> int | None:
    if i > 0 and _is_ident_char(source[i - 1]):
        return None
    j = i + 1 if i < len(source) and source[i] == "b" else i
    if j >= len(source) or source[j] != "'":
        return None
    return _char_body_end(source, j + 1)


def _literal_or_comment_end(source: str, i: int) -> int | None:
    if source.startswith("//", i):
        return _line_comment_end(source, i)
    if source.startswith("/*", i):
        return _block_comment_end(source, i)
    end = _string_end(source, i)
    if end is not None:
        return end
    return _char_end(source, i)


def _mask_rust(source: str) -> str:
    """Replace comments and string/char literals with spaces; keep newlines."""
    out: list[str] = []
    i = 0
    n = len(source)
    while i < n:
        end = _literal_or_comment_end(source, i)
        if end is None:
            out.append(source[i])
            i += 1
            continue
        out.append(_blank(source[i:end]))
        i = end
    return "".join(out)


def _skip_ws(s: str, i: int) -> int:
    n = len(s)
    while i < n and s[i] in " \t\r\n":
        i += 1
    return i


def _skip_balanced(s: str, i: int, open_ch: str, close_ch: str) -> int:
    depth = 0
    n = len(s)
    while i < n:
        if s[i] == open_ch:
            depth += 1
        elif s[i] == close_ch:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def _ident_at(s: str, i: int) -> tuple[str, int]:
    i = _skip_ws(s, i)
    m = _IDENT.match(s, i)
    if not m:
        return "", i
    return m.group(0), m.end()


def _skip_attrs(masked: str, i: int) -> int:
    n = len(masked)
    i = _skip_ws(masked, i)
    while i < n and masked.startswith("#[", i):
        i += 1
        if i < n and masked[i] == "[":
            i = _skip_balanced(masked, i, "[", "]")
        i = _skip_ws(masked, i)
    return i


def _after_pub(masked: str, j: int) -> int:
    i = _skip_ws(masked, j)
    if i < len(masked) and masked[i] == "(":
        return _skip_balanced(masked, i, "(", ")")
    return i


def _after_const_qualifier(masked: str, j: int) -> int | None:
    nxt, _k = _ident_at(masked, j)
    if nxt in _CONST_FN:
        return j
    return None


def _qualifier_advance(masked: str, tok: str, j: int) -> int | None:
    if tok == "pub":
        return _after_pub(masked, j)
    if tok in _QUALIFIERS:
        return j
    if tok == "extern":
        return _skip_ws(masked, j)
    if tok == "const":
        return _after_const_qualifier(masked, j)
    return None


def _skip_qualifiers(masked: str, i: int) -> int:
    while True:
        tok, j = _ident_at(masked, i)
        nxt = _qualifier_advance(masked, tok, j)
        if nxt is None:
            return i
        i = nxt


def _step_depth(c: str, depth_paren: int, depth_bracket: int) -> tuple[int, int]:
    if c == "(":
        return depth_paren + 1, depth_bracket
    if c == ")":
        return max(0, depth_paren - 1), depth_bracket
    if c == "[":
        return depth_paren, depth_bracket + 1
    if c == "]":
        return depth_paren, max(0, depth_bracket - 1)
    return depth_paren, depth_bracket


def _brace_kind(c: str, nested: bool) -> str:
    if c == "{" and not nested:
        return "body"
    if c in ";," and not nested:
        return "stop"
    return "other"


def _brace_body_span(masked: str, i: int) -> tuple[int, int] | None:
    depth_paren = 0
    depth_bracket = 0
    n = len(masked)
    while i < n:
        kind = _brace_kind(masked[i], depth_paren != 0 or depth_bracket != 0)
        if kind == "body":
            end = _skip_balanced(masked, i, "{", "}")
            return i + 1, end - 1
        if kind == "stop":
            return None
        depth_paren, depth_bracket = _step_depth(masked[i], depth_paren, depth_bracket)
        i += 1
    return None


def _item_body_span(masked: str, i: int) -> tuple[int, int] | None:
    """Span of the `{...}` body of the item after a #[cfg(test)] attribute."""
    i = _skip_qualifiers(masked, _skip_attrs(masked, i))
    tok, j = _ident_at(masked, i)
    if not tok:
        return None
    i = _skip_ws(masked, j)
    if i < len(masked) and masked[i] == "!":
        i += 1
    elif tok not in _ITEM_START:
        return None
    return _brace_body_span(masked, i)


def _line_at(source: str, index: int) -> int:
    return source.count("\n", 0, index) + 1


def _match_lines(source: str, body: str, lo: int, rx: re.Pattern[str]) -> list[int]:
    return [_line_at(source, lo + m.start()) for m in rx.finditer(body)]


def _construction_lines(source: str, masked: str, lo: int, hi: int) -> list[int]:
    body = masked[lo:hi]
    lines: list[int] = []
    for rx in _CONSTRUCT_RX:
        lines.extend(_match_lines(source, body, lo, rx))
    if _BUILDER.search(body) and _TEMPDIR_METHOD.search(body):
        lines.extend(_match_lines(source, body, lo, _BUILDER))
    return sorted(set(lines))


def unchecked_tempdir_sites(source: str) -> list[int]:
    """Line numbers of tempfile TempDir constructions in #[cfg(test)] bodies with no .close(."""
    masked = _mask_rust(source)
    found: list[int] = []
    start = 0
    while True:
        k = masked.find(_CFG_TEST, start)
        if k < 0:
            break
        start = k + len(_CFG_TEST)
        span = _item_body_span(masked, start)
        if span is None:
            continue
        lo, hi = span
        if _CLOSE.search(masked[lo:hi]):
            continue
        found.extend(_construction_lines(source, masked, lo, hi))
    return sorted(set(found))


def _tracked_rs_files(*roots: str) -> list[str]:
    listing = subprocess.run(
        ["git", "ls-files", "-z", "--", *roots],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    names = [name for name in listing.split("\0") if name.endswith(".rs")]
    assert names, f"git ls-files returned no .rs files for {roots}; the scan root is wrong"
    return names


def test_cfg_test_tempdir_without_close_is_flagged() -> None:
    source = "#[cfg(test)] mod tests { let t = tempfile::tempdir().unwrap(); }\n"
    assert unchecked_tempdir_sites(source) == [1]


def test_cfg_test_tempdir_with_close_is_clean() -> None:
    source = (
        "#[cfg(test)] mod tests { let t = tempfile::tempdir().unwrap(); t.close().unwrap(); }\n"
    )
    assert unchecked_tempdir_sites(source) == []


def test_cfg_test_drop_impl_close_after_builder_is_clean() -> None:
    source = (
        "#[cfg(test)]\n"
        "mod tests {\n"
        "    struct Dir { dir: Option<tempfile::TempDir> }\n"
        "    impl Drop for Dir {\n"
        "        fn drop(&mut self) {\n"
        "            if let Some(dir) = self.dir.take() {\n"
        "                let _ = dir.close();\n"
        "            }\n"
        "        }\n"
        "    }\n"
        "    fn dir() {\n"
        "        let _g = tempfile::Builder::new().prefix(\"x\").tempdir().unwrap();\n"
        "    }\n"
        "}\n"
    )
    assert unchecked_tempdir_sites(source) == []


def test_tempdir_outside_cfg_test_is_ignored() -> None:
    source = (
        "fn setup() { let t = tempfile::tempdir().unwrap(); }\n"
        "#[cfg(test)]\n"
        "mod tests { fn no_scratch() {} }\n"
    )
    assert unchecked_tempdir_sites(source) == []


def test_cfg_test_websocket_close_without_tempdir_is_clean() -> None:
    source = (
        "#[cfg(test)]\n"
        "mod tests {\n"
        "    fn a_failed_stdin_read_is_an_error_not_a_clean_close() {\n"
        "        ws.close();\n"
        "    }\n"
        "}\n"
    )
    assert unchecked_tempdir_sites(source) == []


def test_audio_engine_src_cfg_test_tempdirs_are_closed() -> None:
    names = _tracked_rs_files("apps/audio-engine/src")
    offenders: list[str] = []
    for name in names:
        text = (REPO_ROOT / name).read_text(encoding="utf-8")
        offenders.extend(f"{name}:{line}" for line in unchecked_tempdir_sites(text))
    assert not offenders, "#[cfg(test)] TempDir dropped without close(): " + ", ".join(offenders)
