"""Where sections sit among a playlist's tracks. Pure: no database, no writes.

A section's ``before_position`` is a 0-based index into the playlist's live
tracks in display order: the section renders above the track at that index, and
an index past the last track puts it at the end (PRD s6.3, E40). Sections and
tracks together form one display list of tokens, which is what a drag moves.
"""
from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Any, Literal

TokenKind = Literal["track", "section"]


@dataclass(frozen=True)
class Token:
    """One line of the display list."""

    kind: TokenKind
    id: str


@dataclass(frozen=True)
class Member:
    """One live membership: its own id, and the track it holds."""

    item_id: str
    stable_id: str


def ordered_sections(rows: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Top to bottom. Two dividers on one index keep their creation order."""
    return sorted(rows, key=lambda r: (r["before_position"], r["created_at"], r["id"]))


def display_tokens(
    item_ids: Sequence[str], sections: Iterable[Mapping[str, Any]]
) -> list[Token]:
    """Tracks with each section placed above its track."""
    above: dict[int, list[str]] = {}
    for section in ordered_sections(sections):
        index = min(section["before_position"], len(item_ids))
        above.setdefault(index, []).append(section["id"])
    tokens: list[Token] = []
    for index, item_id in enumerate(item_ids):
        tokens.extend(Token("section", s) for s in above.get(index, []))
        tokens.append(Token("track", item_id))
    tokens.extend(Token("section", s) for s in above.get(len(item_ids), []))
    return tokens


def _require_lines(tokens: Sequence[Token], wanted: Iterable[Token]) -> None:
    present = set(tokens)
    for token in wanted:
        if token not in present:
            raise KeyError(token)


def _first_unmoved(tokens: Sequence[Token], moved: Collection[Token]) -> Token | None:
    return next((token for token in tokens if token not in moved), None)


def move_tokens(
    tokens: Sequence[Token], moved: Collection[Token], before: Token | None
) -> list[Token]:
    """Lift ``moved`` out as one block, in display order, and land it above ``before``.

    ``before`` None means the end. A target inside the block means the next line
    below the block, so a drop on the dragged rows themselves stays put.
    """
    _require_lines(tokens, [*moved, *([before] if before is not None else [])])
    block = [token for token in tokens if token in moved]
    if before is not None and before in moved:
        before = _first_unmoved(tokens[tokens.index(before):], moved)
    rest = [token for token in tokens if token not in moved]
    index = len(rest) if before is None else rest.index(before)
    return [*rest[:index], *block, *rest[index:]]


def section_positions(tokens: Sequence[Token]) -> dict[str, int]:
    """Each section's ``before_position`` in this display list."""
    positions: dict[str, int] = {}
    tracks_above = 0
    for token in tokens:
        if token.kind == "track":
            tracks_above += 1
        elif token.kind == "section":
            positions[token.id] = tracks_above
    return positions


def track_order(tokens: Sequence[Token]) -> list[str]:
    """The track ids of a display list, in order."""
    return [token.id for token in tokens if token.kind == "track"]


def regroup_positions(
    before: Sequence[Member],
    after: Sequence[Member],
    sections: Sequence[Mapping[str, Any]],
) -> dict[str, int] | None:
    """Section starts for a reorder that kept every section's tracks together.

    "Save this order" over a sort within sections (LIBUX-8678) changes which
    track opens each section, so following tracks would scatter the dividers.
    When each section's tracks still sit together, in section order, each
    divider goes to its group's new start. Otherwise None: follow the tracks.
    ``sections`` must be top to bottom.
    """
    starts = [section["before_position"] for section in sections]
    group_of = {member.item_id: sum(1 for start in starts if start <= index)
                for index, member in enumerate(before)}
    groups = [group_of.get(member.item_id) for member in after]
    if None in groups or any(a > b for a, b in pairwise(groups)):  # type: ignore[operator]
        return None
    return {
        section["id"]: next((i for i, g in enumerate(groups) if g >= number), len(after))  # type: ignore[operator]
        for number, section in enumerate(sections, start=1)
    }


def follow_members(
    before: Sequence[Member],
    after: Sequence[Member],
    sections: Iterable[Mapping[str, Any]],
) -> dict[str, int]:
    """Where each section belongs after a write that knows nothing of sections.

    A section stays above the first track at or below it that survived the
    write; with none left, it goes to the end. Item ids decide, because a track
    can sit in a list twice. Only a full replace, which mints new item ids for
    every row, falls back to stable ids.
    """
    kept = {member.item_id: index for index, member in reversed(list(enumerate(after)))}
    minted = not any(member.item_id in kept for member in before)
    by_track = {member.stable_id: index for index, member in reversed(list(enumerate(after)))}
    index_of = (lambda m: by_track.get(m.stable_id)) if minted else (lambda m: kept.get(m.item_id))
    positions: dict[str, int] = {}
    for section in sections:
        survivors = (index_of(member) for member in before[section["before_position"]:])
        positions[section["id"]] = next(
            (index for index in survivors if index is not None), len(after)
        )
    return positions
