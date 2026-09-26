"""Read the TypeScript command contract the browser actually enforces.

The AGENT-05 verb table is a second copy of a contract that already exists in
TypeScript, and a second copy is only safe if something compares them. These
readers are that comparison: ``test_bus_parity.py`` re-reads
``performance-ipc.svelte.ts`` and ``quick-draw-catalog.ts`` from the working
tree rather than from a checked-in copy, so a command type added to the bus
without a verb is a red test the next time anyone runs the lane.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
IPC_SOURCE = REPO_ROOT / "apps/webui/frontend/src/lib/rb/performance-ipc.svelte.ts"
QUICK_DRAW_SOURCE = REPO_ROOT / "apps/webui/frontend/src/lib/rb/quick-draw-catalog.ts"
MIRROR_SOURCE = REPO_ROOT / "apps/webui/frontend/src/lib/rb/ui-mirror.ts"

_MIRROR_DECK_OPEN = "Object.entries(state.decks).map(([id, deck]) => [id, {"
_MIRROR_OPEN = "export function buildUiMirror()"

_COMMAND_UNION = "export type PerformanceCommand ="
_HEADPHONE_COMMAND_UNION = "export type HeadphoneCommand ="
_QUICK_DRAW_UNION = "export type QuickDrawActionId ="
_FIELD = re.compile(r"^\| \{ (?P<body>.*?) \}$")
_TYPE_FIELD = re.compile(r"^type: '(?P<type>[a-z_]+)'$")


def _union_lines(source: Path, marker: str) -> list[str]:
    """The union member lines that follow ``marker``.

    Comments, including the block comments that sit between members, are
    skipped; the union ends at the first line that is neither a member nor a
    comment, which is the ``export interface`` that follows it. A member the
    formatter wrapped over several lines is joined back into the one-line
    ``| { a; b }`` shape the readers below parse.
    """
    lines = source.read_text(encoding="utf-8").splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith(marker))
    members: list[str] = []
    rest = iter(lines[start + 1:])
    for line in rest:
        stripped = line.strip()
        if stripped.startswith("|"):
            members.append(_join_wrapped_member(stripped, rest, source))
        elif stripped == "" or stripped.startswith(("//", "/*", "*")):
            continue
        else:
            break
    if not members:
        raise AssertionError(f"no union members found after {marker!r} in {source}")
    return members


def _join_wrapped_member(first: str, rest: Iterator[str], source: Path) -> str:
    """Consume the continuation lines of a wrapped member from ``rest``.

    ``| {`` / ``type: 'play';`` / ... / ``}`` -> ``| { type: 'play'; ... }``.
    A one-line member has balanced braces and comes back unchanged.
    """
    pieces = [first]
    depth = first.count("{") - first.count("}")
    while depth > 0:
        line = next(rest, None)
        if line is None:
            raise AssertionError(f"union member never closes in {source}: {first}")
        stripped = line.strip()
        pieces.append(stripped)
        depth += stripped.count("{") - stripped.count("}")
    return re.sub(r";\s*(\};?)$", r" \1", " ".join(pieces))


def _command_member(line: str) -> str:
    """``| { type: 'load'; deck: DeckId };`` -> ``load``."""
    match = _FIELD.match(line.rstrip(";"))
    if match is None:
        raise AssertionError(f"union member does not match the declared shape: {line}")
    head = match.group("body").split(";")[0].strip()
    typed = _TYPE_FIELD.match(head)
    if typed is None:
        raise AssertionError(f"union member does not open with a type field: {line}")
    return typed.group("type")


def _top_level_parts(body: str) -> list[str]:
    """Split a member body on ``;`` at brace depth 0.

    The loop command nests ``{ in_ms: number; out_ms: number }``, and a naive
    split invents two top-level fields that do not exist (``loop`` would look
    like it were missing ``out_ms``).
    """
    parts: list[str] = []
    current = ""
    depth = 0
    for character in body:
        if character in "{<(":
            depth += 1
        elif character in "}>)" or character == ">":
            depth -= 1
        if character == ";" and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += character
    parts.append(current)
    return parts


def headphone_command_fields() -> frozenset[str]:
    """Every ``HeadphoneCommand`` type name (CUEOUT-04 HTTP-mirrored controls)."""
    return frozenset(
        _command_member(line) for line in _union_lines(IPC_SOURCE, _HEADPHONE_COMMAND_UNION)
    )


def _field_part_name(part: str) -> str | None:
    """One ``;``-delimited fragment of a union member body, or None if it is noise.

    Wrapped ``load`` members carry line comments between fields; treating those
    fragments as field names invents required keys the CLI never emits and reds
    ``test_every_verb_emits_only_fields_the_wire_validator_accepts``.
    """
    text = part.strip()
    if not text or text.startswith("//"):
        return None
    if "//" in text:
        text = text.split("//", 1)[0].strip()
    if not text or ":" not in text:
        return None
    name = text.split(":", 1)[0].strip()
    if not re.match(r"^[a-z_][a-z0-9_]*\??$", name):
        return None
    return name


def command_fields() -> dict[str, dict[str, bool]]:
    """Every ``PerformanceCommand`` type -> its fields -> is the field optional.

    The wire validator calls ``_exactKeys`` per branch, so this is the set of
    keys a command of that type may carry. ``type`` is not listed: every
    command has it.
    """
    fields: dict[str, dict[str, bool]] = {}
    for line in _union_lines(IPC_SOURCE, _COMMAND_UNION):
        match = _FIELD.match(line.rstrip(";"))
        assert match is not None, line
        parts = [part.strip() for part in _top_level_parts(match.group("body"))]
        command_type = _command_member(line)
        declared: dict[str, bool] = {}
        for part in parts[1:]:
            name = _field_part_name(part)
            if name is None:
                continue
            declared[name.rstrip("?")] = name.endswith("?")
        fields[command_type] = declared
    return fields


def quick_draw_ids() -> tuple[str, ...]:
    """Every ``QuickDrawActionId`` member, in source order."""
    found: list[str] = []
    for line in _union_lines(QUICK_DRAW_SOURCE, _QUICK_DRAW_UNION):
        member = re.search(r"'([^']+)'", line)
        if member is None:
            raise AssertionError(f"quick-draw member carries no id: {line}")
        found.append(member.group(1))
    return tuple(found)


def mirror_deck_keys() -> frozenset[str]:
    """Every key ``buildUiMirror`` publishes per deck.

    A verb's observation is worthless if the mirror does not carry the path it
    reads, and the CLI cannot tell the difference between "the control did not
    move" and "this field was never published" without saying so. `load`
    shipped observing only `title` because the mirror omitted `stable_id`
    entirely, so a load onto an already-loaded deck confirmed the PREVIOUS
    track (#1739). Read from the live source rather than a checked-in copy,
    for the same reason the command union is.

    Scope is the DECK projection only: mixer and master are assembled from
    `state.mixer` / `state.master` elsewhere and are not a literal here.
    """
    lines = MIRROR_SOURCE.read_text(encoding="utf-8").splitlines()
    start = next(
        (index for index, line in enumerate(lines) if _MIRROR_DECK_OPEN in line),
        None,
    )
    if start is None:
        raise AssertionError(
            f"the deck projection opener is not in {MIRROR_SOURCE}; this reader "
            "is pinned to a shape that has changed and must be updated, not skipped"
        )
    keys: set[str] = set()
    depth = 0
    for line in lines[start:]:
        stripped = line.strip()
        if stripped.startswith("//"):
            continue
        if depth == 1:
            for match in re.finditer(r"(?:^|[{,]|\s)([a-z_][a-z0-9_]*)\s*:", stripped):
                keys.add(match.group(1))
        depth += line.count("{") - line.count("}")
        if depth <= 0 and keys:
            break
    if not keys:
        raise AssertionError(f"no deck keys parsed out of {MIRROR_SOURCE}")
    return frozenset(keys)


def mirror_top_level_keys() -> frozenset[str]:
    """Every key ``buildUiMirror`` publishes at the top of the document.

    Same reason as the deck projection: a CLI that reads a top-level path the
    mirror never publishes cannot tell "absent" from "no". `master_deck` is the
    case that forced this reader - without it the CLI cannot resolve
    `clock: master` to the deck the page will actually time against, and it
    guessed a tempo instead (#1739).
    """
    lines = MIRROR_SOURCE.read_text(encoding="utf-8").splitlines()
    start = next(
        (index for index, line in enumerate(lines) if line.startswith(_MIRROR_OPEN)),
        None,
    )
    if start is None:
        raise AssertionError(
            f"{_MIRROR_OPEN!r} is not in {MIRROR_SOURCE}; this reader is pinned to "
            "a shape that has changed and must be updated, not skipped"
        )
    opener = next(
        (index for index, line in enumerate(lines[start:], start) if line.strip() == "return {"),
        None,
    )
    if opener is None:
        raise AssertionError(f"buildUiMirror has no object literal to read in {MIRROR_SOURCE}")
    keys: set[str] = set()
    depth = 0
    for line in lines[opener:]:
        stripped = line.strip()
        if stripped.startswith(("//", "*", "/*")):
            continue
        if depth == 1:
            match = re.match(r"^([a-z_][a-z0-9_]*)\s*:", stripped)
            if match is not None:
                keys.add(match.group(1))
        depth += line.count("{") - line.count("}")
        if depth <= 0 and keys:
            break
    if not keys:
        raise AssertionError(f"read no top-level mirror keys from {MIRROR_SOURCE}")
    return frozenset(keys)
