"""Classify shell commands for destructive `git reset --hard` (house git-safety rule).

Policy: NEVER `git reset --hard` in any worktree (`(private path)`, Thu
15 Oct 2026). This module parses argv and common shell wrappers; it does not scan
tracked files for substrings.

Acceptance:
  - [if] argv is a parsed `git reset --hard` [then] classify block, [else stop].
  - [if] a read-only git command mentions reset text in an argument [then] allow,
    [else stop].
  - [if] a reset help or non-hard reset command is invoked [then] allow, [else stop].

Usage::

    python -m scripts.git_safety check --command 'git reset --hard'
"""

from __future__ import annotations

import argparse
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Literal

from scripts.sparse_worktree import is_linked_worktree

Classification = Literal["allow", "block", "unknown"]

_RULE_DOC = "(private path)"

_GIT_GLOBAL_OPTS_ONE_ARG = frozenset(
    {
        "-C",
        "-c",
        "--exec-path",
        "--git-dir",
        "--work-tree",
        "--namespace",
        "--super-prefix",
    }
)

_GIT_GLOBAL_OPTS_ZERO_ARG = frozenset({"--no-pager", "--no-replace-objects"})

_SHELL_WRAPPERS = frozenset({"bash", "sh", "zsh", "dash"})

_HARD_ABBREV = re.compile(r"^--h(a(r(d)?)?)?$")


class GitSafetyError(RuntimeError):
    """A command violates git-safety policy or could not be classified."""


def _quotes_balanced(command: str) -> bool:
    in_single = False
    in_double = False
    escape = False
    for ch in command:
        if escape:
            escape = False
            continue
        if ch == "\\" and in_double:
            escape = True
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
    return not in_single and not in_double


def _shlex_split(command: str) -> list[str] | None:
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return None


def _split_shell_operators(command: str) -> list[str] | None:
    if not _quotes_balanced(command):
        return None
    parts: list[str] = []
    buf: list[str] = []
    in_single = False
    in_double = False
    escape = False
    i = 0
    while i < len(command):
        ch = command[i]
        if escape:
            buf.append(ch)
            escape = False
            i += 1
            continue
        if ch == "\\" and in_double:
            buf.append(ch)
            escape = True
            i += 1
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
            buf.append(ch)
            i += 1
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            buf.append(ch)
            i += 1
            continue
        if not in_single and not in_double:
            if command.startswith("&&", i):
                parts.append("".join(buf))
                buf = []
                i += 2
                continue
            if ch == "&":
                parts.append("".join(buf))
                buf = []
                i += 1
                continue
            if ch in ";|\n":
                parts.append("".join(buf))
                buf = []
                i += 1
                continue
        buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def _strip_git_globals(argv: list[str]) -> list[str]:
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in _GIT_GLOBAL_OPTS_ONE_ARG:
            if i + 1 >= len(argv):
                break
            i += 2
            continue
        attached_global = False
        for opt_name in _GIT_GLOBAL_OPTS_ONE_ARG:
            prefix = opt_name + "="
            if tok.startswith(prefix) and len(tok) > len(prefix):
                attached_global = True
                break
        if attached_global:
            i += 1
            continue
        if tok in _GIT_GLOBAL_OPTS_ZERO_ARG:
            i += 1
            continue
        break
    return argv[i:]


def _hard_option_scan(opt_tokens: list[str]) -> Classification:
    saw_unknown = False
    for tok in opt_tokens:
        if tok == "--hard":
            return "block"
        if tok in ("-h", "--help"):
            continue
        if tok.startswith("--") and _HARD_ABBREV.fullmatch(tok):
            saw_unknown = True
    if saw_unknown:
        return "unknown"
    return "allow"


def classify_git_reset(argv: list[str]) -> Classification:
    """Classify argv after the ``git`` token (global git options allowed)."""
    rest = _strip_git_globals(argv)
    if not rest:
        return "allow"
    if rest[0] == "help" and len(rest) >= 2 and rest[1] == "reset":
        return "allow"
    if rest[0] != "reset":
        return "allow"

    tail = rest[1:]
    opt_part = tail[: tail.index("--")] if "--" in tail else tail

    if opt_part and all(t in ("-h", "--help") for t in opt_part):
        return "allow"
    if not opt_part and not tail:
        return "allow"

    verdict = _hard_option_scan(opt_part)
    if verdict != "allow":
        return verdict
    return "allow"


def _git_invocations_in_tokens(tokens: list[str]) -> list[list[str]]:
    if not tokens:
        return []
    invocations: list[list[str]] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "cd":
            i += 2 if i + 1 < len(tokens) else 1
            continue
        if tok == "env":
            i += 1
            while i < len(tokens) and "=" in tokens[i] and not tokens[i].startswith("-"):
                i += 1
            if i < len(tokens) and tokens[i] == "git":
                invocations.append(tokens[i:])
            break
        if "=" in tok and not tok.startswith("-") and tok != "git":
            i += 1
            continue
        if tok == "git":
            invocations.append(tokens[i:])
            break
        i += 1
    return invocations


def _git_invocations_in_segment(segment: str) -> list[list[str]] | None:
    tokens = _shlex_split(segment)
    if tokens is None:
        return None
    return _git_invocations_in_tokens(tokens)


def _shell_script_after_c_flag(tokens: list[str]) -> str | None:
    """Script string after bash/sh -c, including clustered short options (-lc, -ec)."""
    if not tokens or tokens[0] not in _SHELL_WRAPPERS:
        return None
    i = 1
    while i < len(tokens):
        tok = tokens[i]
        if tok == "-c":
            if i + 1 < len(tokens):
                return tokens[i + 1]
            return None
        if tok.startswith("--"):
            return None
        if tok.startswith("-") and len(tok) >= 2:
            if "c" in tok[1:]:
                if i + 1 < len(tokens):
                    return tokens[i + 1]
                return None
            i += 1
            continue
        break
    return None


def _git_invocations_from_segment(segment: str) -> list[list[str]] | None:
    invocations: list[list[str]] = []
    tokens = _shlex_split(segment)
    if tokens is None:
        return None
    inner = _shell_script_after_c_flag(tokens)
    if inner is not None:
        inner_git = extract_git_invocations(inner)
        if inner_git is None:
            return None
        invocations.extend(inner_git)
    bare = _git_invocations_in_tokens(tokens)
    invocations.extend(bare)
    return invocations


def extract_git_invocations(shell_command: str) -> list[list[str]] | None:
    """Return argv lists beginning with ``git``, or ``None`` when shell is unparseable."""
    command = shell_command.strip()
    if not command:
        return []
    if not _quotes_balanced(command):
        return None

    segments = _split_shell_operators(command)
    if segments is None:
        return None

    invocations: list[list[str]] = []
    for segment in segments:
        part = _git_invocations_from_segment(segment)
        if part is None:
            return None
        invocations.extend(part)
    return invocations


def classify_shell_command(command: str) -> Classification:
    invocations = extract_git_invocations(command)
    if invocations is None:
        return "unknown"
    saw_unknown = False
    for argv in invocations:
        if not argv or argv[0] != "git":
            continue
        verdict = classify_git_reset(argv[1:])
        if verdict == "block":
            return "block"
        if verdict == "unknown":
            saw_unknown = True
    if saw_unknown:
        return "unknown"
    return "allow"


def _checkout_label(repo: Path) -> str:
    try:
        if is_linked_worktree(repo):
            return "linked worktree"
    except (OSError, subprocess.CalledProcessError):
        pass
    return "primary checkout"


def _policy_message(repo: Path | None, *, unknown: bool) -> str:
    where = ""
    if repo is not None:
        try:
            where = f" ({_checkout_label(repo.resolve())})"
        except OSError:
            where = ""
    if unknown:
        return (
            "UNKNOWN: could not classify the command's git reset form safely; "
            f"refuse rather than allow a destructive reset. See {_RULE_DOC}."
        )
    return f"Refusing `git reset --hard`{where}: it discards uncommitted work across shared worktrees. See {_RULE_DOC}."


def assert_safe_reset_command(command: str, *, repo: Path | None = None) -> None:
    verdict = classify_shell_command(command)
    if verdict == "allow":
        return
    raise GitSafetyError(_policy_message(repo, unknown=verdict == "unknown"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Classify shell commands for git reset --hard.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    check = sub.add_parser("check", help="Classify one shell command string.")
    check.add_argument("--command", required=True, help="Shell command text to classify.")
    check.add_argument(
        "--repo",
        type=Path,
        default=None,
        help="Optional repo path to name primary vs linked checkout in block messages.",
    )
    args = parser.parse_args(argv)

    verdict = classify_shell_command(args.command)
    if verdict == "allow":
        return 0
    message = _policy_message(args.repo, unknown=verdict == "unknown")
    print(message, file=sys.stderr)
    return 1 if verdict == "block" else 2


if __name__ == "__main__":
    raise SystemExit(main())
