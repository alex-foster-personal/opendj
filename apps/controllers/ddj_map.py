"""Diff generated controller bindings against a hand-written DDJ device map.

The hand-written map is a device-map document in the installed ``MidiMapDoc`` shape
(``apps/webui/server/routes/midi_maps.py``), for example the body of
``GET /api/v1/midi/maps/{map_id}``. The caller validates it with ``MidiMapDoc`` and
passes the model in: this domain package never imports the web UI, so it reads the
model through the ``DeviceMapLike`` protocol. Bindings are matched by MIDI source and
shift; the diff lists added, removed and changed bindings and never edits either side.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

SourceKey = tuple[int, str, int, bool]


@dataclass(frozen=True)
class ChangedBinding:
    """One MIDI source both maps bind, to different actions."""

    source: SourceKey
    hand_action: dict[str, Any]
    generated_action: dict[str, Any]


@dataclass(frozen=True)
class MapDiff:
    """Bindings only in the generated map, only in the hand map, or bound differently."""

    added: tuple[dict[str, Any], ...]
    removed: tuple[dict[str, Any], ...]
    changed: tuple[ChangedBinding, ...]

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.removed or self.changed)


class _Dumpable(Protocol):
    def model_dump(self, *, by_alias: bool = ..., exclude_none: bool = ...) -> dict[str, Any]: ...


class BindingLike(Protocol):
    """One validated device-map binding (``MidiMapDoc.bindings[n]``)."""

    @property
    def source(self) -> _Dumpable: ...
    @property
    def action(self) -> _Dumpable: ...
    @property
    def shift(self) -> bool: ...


class DeviceMapLike(Protocol):
    """A validated device-map document (``MidiMapDoc``)."""

    @property
    def bindings(self) -> Sequence[BindingLike]: ...


def hand_written_bindings(doc: DeviceMapLike) -> list[dict[str, Any]]:
    """Return the bindings of a validated device map as plain dicts."""
    return [
        {
            "source": binding.source.model_dump(),
            "action": binding.action.model_dump(by_alias=True, exclude_none=True),
            "shift": binding.shift,
        }
        for binding in doc.bindings
    ]


def _source_key(binding: Mapping[str, Any]) -> SourceKey:
    source = binding["source"]
    return (int(source["ch"]), str(source["kind"]), int(source["id"]), bool(binding.get("shift", False)))


def _index(bindings: Iterable[Mapping[str, Any]], side: str) -> dict[SourceKey, dict[str, Any]]:
    index: dict[SourceKey, dict[str, Any]] = {}
    for binding in bindings:
        key = _source_key(binding)
        if key in index:
            raise ValueError(f"{side} map binds source {key} twice")
        index[key] = {"source": dict(binding["source"]), "action": dict(binding["action"]), "shift": key[3]}
    return index


def diff_maps(generated: Iterable[Mapping[str, Any]], hand_written: Iterable[Mapping[str, Any]]) -> MapDiff:
    """Return the bindings that differ between ``generated`` and ``hand_written``."""
    new = _index(generated, "generated")
    old = _index(hand_written, "hand-written")
    return MapDiff(
        added=tuple(new[key] for key in sorted(new.keys() - old.keys())),
        removed=tuple(old[key] for key in sorted(old.keys() - new.keys())),
        changed=tuple(
            ChangedBinding(key, old[key]["action"], new[key]["action"])
            for key in sorted(new.keys() & old.keys())
            if new[key]["action"] != old[key]["action"]
        ),
    )
