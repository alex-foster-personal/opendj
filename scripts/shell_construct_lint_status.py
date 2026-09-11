"""
shell_construct_lint_status.py -- the scanner behind shell_construct_lint S-05.

A command substitution RUNS a command, and bash and zsh update $? the moment it
finishes, while the rest of the same command is still being expanded. So

    f; echo "[$(date)] rc=$?"

logs date's 0, never f's status. Measured Fri 11 Sep 2026 with
`f(){ return 5; }; f; ...` in bash 3.2.57 and zsh 5.9: a double-quoted string,
separate words, backticks, `${?}` and an `a=$(x) b=$?` assignment pair all read
0. Only dash read 5. Arithmetic `$((...))` runs no command and reads 5.

Pure and dependency-free: it takes one statement's text and returns the index
of every offending read, so shell_construct_lint owns paths and line numbers.
"""

from __future__ import annotations

from dataclasses import dataclass

STATUS_READS: tuple[str, ...] = ("${?}", "$?")


@dataclass
class _Frame:
    """One expansion context.

    ``ran`` means a substitution already COMPLETED earlier in this frame's
    current simple command. A new frame inherits it, because `$(echo $?)`
    opened after `$(date)` reads date's 0 too.
    """

    close: str
    subst: bool
    ran: bool
    dquote: bool = False
    squote: bool = False


def status_reads_after_substitution(code: str, *, heredoc_body: bool) -> list[int]:
    """Indexes of every $? read that follows a completed substitution.

    Heredoc bodies expand like one double-quoted word: quotes are literal and
    there are no separators, so they start inside a double quote that never ends.
    """
    stack = [_Frame("", subst=False, ran=False, dquote=heredoc_body)]
    hits: list[int] = []
    i = 0
    while i < len(code):
        frame = stack[-1]
        read = next((r for r in STATUS_READS if code.startswith(r, i)), "")
        if read and not frame.squote:
            if frame.ran:
                hits.append(i)
            i += len(read)
            continue
        i = (
            _quote_step(code, i, frame, heredoc_body=heredoc_body)
            or _expansion_step(code, i, stack)
            or _operator_step(code, i, stack)
        )
    return hits


# ----- steps: each returns the next index, or None if the char is not its --


def _quote_step(code: str, i: int, frame: _Frame, *, heredoc_body: bool) -> int | None:
    ch = code[i]
    if frame.squote:
        frame.squote = ch != "'"
        return i + 1
    if ch == "\\":
        return i + 2
    if ch == "'" and not frame.dquote:
        frame.squote = True
        return i + 1
    if ch == '"' and not heredoc_body:
        frame.dquote = not frame.dquote
        return i + 1
    return None


def _expansion_step(code: str, i: int, stack: list[_Frame]) -> int | None:
    frame = stack[-1]
    if code.startswith("$((", i):
        # Arithmetic runs no command: two plain parens, closed by `))`.
        stack += [_Frame(")", False, frame.ran), _Frame(")", False, frame.ran)]
        return i + 3
    if code.startswith("$(", i):
        stack.append(_Frame(")", True, frame.ran))
        return i + 2
    if code[i] == "`" and frame.close == "`":
        stack.pop()
        stack[-1].ran = True
        return i + 1
    if code[i] == "`":
        stack.append(_Frame("`", True, frame.ran))
        return i + 1
    return None


def _operator_step(code: str, i: int, stack: list[_Frame]) -> int:
    frame, ch = stack[-1], code[i]
    if frame.dquote:
        return i + 1
    if ch == "(":
        stack.append(_Frame(")", False, frame.ran))
    elif ch == ")" and len(stack) > 1 and frame.close == ")":
        stack.pop()
        if frame.subst:
            stack[-1].ran = True
    elif ch in ";|\n" or (ch == "&" and (i == 0 or code[i - 1] not in "<>")):
        # A separator starts a new simple command: $? now belongs to whatever
        # ran, which is what a read here wants (`out=$(x) || status=$?`).
        frame.ran = False
    return i + 1
