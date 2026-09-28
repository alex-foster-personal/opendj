"""Shell lexing for scripts/runner_toolset_scan.py: text in, tokens out.

Pure functions with no knowledge of the manifest. They turn a bash `run:`
block or script into (token, line) pairs a command-position walker can read,
without letting quoted prose, comments or heredoc bodies masquerade as
commands. Deliberately not a full bash parser: it errs toward reporting a
word as a command, since a false "used" is a visible test failure while a
false "unused" is a silent miss.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from itertools import takewhile

PUNCT = set(";&|()<>")
HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][\w-]*)\1")
PLACEHOLDER = "GHEXPR"


@dataclass
class Heredoc:
    feeder: str  # the line that opened it, e.g. `python3 - <<'PY'`
    body: str
    first_line: int


# ----- comments, continuations, heredocs --------------------------------------


@dataclass
class _Cleaner:
    """Walks characters with a quoting stack; `"$(cmd 'x')"` reopens quoting."""

    stack: list[str] = field(default_factory=list)
    heredoc_delims: list[str] = field(default_factory=list)

    def in_quote(self) -> bool:
        return bool(self.stack) and self.stack[-1] in {"'", '"'}

    def clean_line(self, line: str) -> str:
        buf: list[str] = []
        j = 0
        while j < len(line):
            step = self._step(line, j)
            if step is None:
                break
            buf.append(line[j : j + step])
            j += step
        return "".join(buf)

    def _step(self, line: str, j: int) -> int | None:
        """Characters consumed at `j`, or None at a comment."""
        ch = line[j]
        top = self.stack[-1] if self.stack else ""
        if top == "'":
            return self._close_if(ch == "'")
        if ch == "\\":
            return 2
        if top == '"':
            return self._in_double_quotes(line, j)
        return self._in_code(line, j, top)

    def _close_if(self, closes: bool) -> int:
        if closes:
            self.stack.pop()
        return 1

    def _in_double_quotes(self, line: str, j: int) -> int:
        if line.startswith("$(", j):
            self.stack.append("(")
            return 2
        return self._close_if(line[j] == '"')

    def _in_code(self, line: str, j: int, top: str) -> int | None:
        ch = line[j]
        if ch in "'\"(":
            self.stack.append(ch)
        elif ch == ")" and top == "(":
            self.stack.pop()
        elif ch == "#" and (j == 0 or line[j - 1] in " \t;&|("):
            return None
        elif line.startswith("<<", j) and not line.startswith("<<<", j):
            match = HEREDOC_RE.match(line, j)
            self.heredoc_delims += [match.group(2)] if match else []
        return 1


def _join_continuations(lines: list[str]) -> list[str]:
    """Join `\\`-continued lines onto their first line, keeping the line count."""
    out: list[str] = []
    start: int | None = None
    for line in lines:
        continued = line.endswith("\\") and not line.endswith("\\\\")
        piece = line[:-1] + " " if continued else line
        if start is None:
            out.append(piece)
            start = len(out) - 1 if continued else None
        else:
            out[start] += piece
            out.append("")
            start = start if continued else None
    return out


def strip_comments_and_heredocs(text: str) -> tuple[str, list[Heredoc]]:
    """Drop comments, cut heredoc bodies out, turn unquoted newlines into `;`.

    The line count is preserved so token line numbers stay true.
    """
    raw = text.split("\n")
    out: list[str] = []
    heredocs: list[Heredoc] = []
    cleaner = _Cleaner()
    i = 0
    while i < len(raw):
        code = cleaner.clean_line(raw[i])
        continues = code.endswith("\\") and not code.endswith("\\\\")
        out.append(code if cleaner.in_quote() or continues else code + " ;")
        i += 1
        for delim in cleaner.heredoc_delims:
            end = next((n for n in range(i, len(raw)) if raw[n].strip() == delim), len(raw))
            heredocs.append(Heredoc(code, "\n".join(raw[i:end]), i))
            out += [""] * (min(end + 1, len(raw)) - i)
            i = end + 1
        cleaner.heredoc_delims = []
    return "\n".join(_join_continuations(out)), heredocs


# ----- command substitutions ---------------------------------------------------


def command_substitutions(text: str) -> list[tuple[int, int, str]]:
    """(start, end, body) of each `$(...)` / backtick / `<(...)` / `>(...)` substitution.

    Nothing inside single quotes or after a backslash counts, and a
    double-quoted apostrophe (`"can't"`) does not open a quote. Process
    substitution, like bash, only counts outside any quotes.
    """
    bodies: list[tuple[int, int, str]] = []
    quote = ""
    i = 0
    while i < len(text):
        ch = text[i]
        end = None
        if quote == "'":
            quote = "" if ch == "'" else quote
        elif ch == "\\":
            i += 1
        elif ch in "'\"":
            quote = _toggle_quote(quote, ch)
        elif (text.startswith("$(", i) and not text.startswith("$((", i)) or (
            not quote and text.startswith(("<(", ">("), i)
        ):
            end = _matching_paren(text, i + 2)
            bodies.append((i, end, text[i + 2 : end - 1]))
        elif ch == "`" and text.find("`", i + 1) > 0:
            end = text.find("`", i + 1) + 1
            bodies.append((i, end, text[i + 1 : end - 1]))
        i = end if end is not None else i + 1
    return bodies


def _toggle_quote(quote: str, ch: str) -> str:
    if ch == '"':
        return "" if quote == '"' else '"'
    return quote or "'"


def _matching_paren(text: str, j: int) -> int:
    """Index just past the `)` closing a `$(` whose body starts at `j`."""
    depth, quote = 1, ""
    while j < len(text) and depth:
        ch = text[j]
        if ch == "\\" and quote != "'":
            j += 1
        elif quote:
            quote = "" if ch == quote else quote
        elif ch in "'\"":
            quote = ch
        else:
            depth += {"(": 1, ")": -1}.get(ch, 0)
        j += 1
    return j


def mask_substitutions(text: str, subs: list[tuple[int, int, str]]) -> str:
    """Replace each substitution by a placeholder, keeping its newlines."""
    for start, end, body in reversed(subs):
        text = text[:start] + PLACEHOLDER + "\n" * body.count("\n") + text[end:]
    return text


# ----- tokens -------------------------------------------------------------------


def tokens(text: str) -> list[tuple[str, int]]:
    """(token, 0-based line) pairs; operators such as `&&` and `);` are tokens."""
    lex = shlex.shlex(text, posix=True, punctuation_chars=";&|()<>")
    lex.whitespace_split = True
    lex.commenters = ""
    found: list[tuple[str, int]] = []
    try:
        found.extend((tok, lex.lineno - 1) for tok in lex)
    except ValueError:
        # Unbalanced quoting: rescan line by line so one bad line hides nothing else.
        if "\n" not in text:
            return found
        return [
            (tok, n + ln) for n, line in enumerate(text.split("\n")) for tok, ln in tokens(line)
        ]
    return found


def is_separator(tok: str) -> bool:
    """A control operator (`;`, `&&`, `|`, `(`, `);` ...), not a redirect."""
    return bool(tok) and set(tok) <= PUNCT and "<" not in tok and ">" not in tok


def is_redirect(tok: str) -> bool:
    return bool(tok) and set(tok) <= PUNCT and ("<" in tok or ">" in tok)


# ----- commands launched from inside another command's arguments -------------------

FIND_ACTIONS = frozenset({"-exec", "-execdir", "-ok", "-okdir"})
# Every option the tool's own help shows with a <VALUE>. A missing one makes its
# value the launched command. uv: `uv run --help`, identical in 0.12.4, 0.12.8 and
# 0.12.19 (the fleet's versions, Tue 29 Sep 2026); the test compares only a uv
# inside UV_RUN_VALUE_OPTIONS_VERIFIED. pnpm: `pnpm help exec`, the union of 11.9.0
# (the packageManager pin), 11.22.0 and 12.6.0 (bifrost1's corepack default); the
# test compares the pin, run through corepack. See test_runner_toolset_launchers.py.
UV_RUN_VALUE_OPTIONS_VERIFIED = ("0.12.4", "0.12.19")
UV_RUN_VALUE_OPTIONS = frozenset(
    {"--allow-insecure-host", "--cache-dir", "--color", "--config-file", "--config-setting"}
    | {"--config-settings-package", "--default-index", "--directory", "--env-file"}
    | {"--exclude-newer", "--exclude-newer-package", "--extra", "--extra-index-url"}
    | {"--find-links", "--fork-strategy", "--group", "--index", "--index-strategy"}
    | {"--index-url", "--keyring-provider", "--link-mode", "--no-binary-package"}
    | {"--no-build-isolation-package", "--no-build-package", "--no-editable-package"}
    | {"--no-extra", "--no-group", "--no-sources-package", "--only-group", "--package"}
    | {"--prerelease", "--prerelease-package", "--project", "--python", "--python-platform"}
    | {"--refresh-package", "--reinstall-package", "--resolution", "--upgrade-group"}
    | {"--upgrade-package", "--with", "--with-editable", "--with-requirements"}
    | {"-C", "-P", "-f", "-i", "-p", "-w"}
)
PNPM_EXEC_VALUE_OPTIONS = frozenset(
    {"--changed-files-ignore-pattern", "--dir", "--filter", "--filter-prod", "--loglevel"}
    | {"--test-pattern", "-C", "-F"}
    | {"--http-proxy", "--https-proxy", "--no-proxy", "--npmrc-auth-file", "--registry"}
    | {"--reporter", "--state-dir", "--store-dir", "--workspace-concurrency"}
    | {"--workspace-packages"}
)
# name: (tokens that start the launched command, options that take a value before it).
# `uv run` and `pnpm exec` look in the project environment first and then fall back
# to the runner PATH, so their command is recorded like any other; the completeness
# test allowlists the ones the project itself provides (PROJECT_PROVIDED).
LAUNCHERS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "find": (FIND_ACTIONS, frozenset()),
    "doppler": (frozenset({"--"}), frozenset()),
    "uv": (frozenset({"run"}), UV_RUN_VALUE_OPTIONS),
    "pnpm": (frozenset({"exec"}), PNPM_EXEC_VALUE_OPTIONS),
}


def launched_commands(name: str, toks: list[str]) -> list[list[str]]:
    """The argv of each command `name` launches from its arguments `toks`.

    `find ... -exec CMD {} +` (a `;` terminator is already a separator token),
    `doppler run -- CMD`, `uv run [options] CMD`, `pnpm exec [options] CMD`.
    """
    starts, value_options = LAUNCHERS[name]
    args = list(takewhile(lambda tok: not is_separator(tok), toks))
    found: list[list[str]] = []
    idx = 0
    while idx < len(args):
        idx += 1
        if args[idx - 1] not in starts:
            continue
        while name != "find" and idx < len(args) and args[idx].startswith("-"):
            idx += 2 if args[idx] in value_options else 1
        end = len(args)
        if name == "find":
            end = next((j for j in range(idx + 1, end) if args[j - 1 : j + 1] == ["{}", "+"]), end)
        if idx < end:
            found.append(args[idx:end])
        idx = end if name == "find" else len(args)
    return found
