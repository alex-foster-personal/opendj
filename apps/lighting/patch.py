"""Lighting patch: which fixture sits on which DMX universe and address, its groups and stage position.

A ``Patch`` is one rig layout. rekordbox calls a saved patch a Venue; a ``VenueStore`` holds
several named patches with exactly one active. ``validate_patch`` needs the fixture profiles
(``apps.lighting.fixtures``) to know each fixture's footprint (its mode's channel count), and
refuses overlapping footprints on one universe and fixtures running past slot 512, naming both
fixtures. The store persists as one JSON file at ``<data>/lighting/patch.json``, written
atomically and only after validation passes:

    {"version": 1, "active": "club", "patches": [
      {"name": "club", "fixtures": [
        {"id": "par-left-1", "profile": "test-rgb-par-3", "mode": "3ch", "universe": 1,
         "address": 1, "groups": ["left", "pars"], "position": {"x": 0.1, "y": 0.2}}]}]}

Groups are free-form names (``left``, ``right``, ``pars``, ``movers``, ``strobes`` ...).
Position x runs left to right from the audience, y front to back, both 0..1.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from apps.lighting.fixtures import FixtureProfile
from apps.lighting.fixtures.profile import DMX_UNIVERSE_CHANNELS
from apps.lighting.sacn import MAX_UNIVERSE, MIN_UNIVERSE
from apps.shared import paths

PATCH_FILE_VERSION = 1

_STORE_KEYS = frozenset({"version", "active", "patches"})
_PATCH_KEYS = frozenset({"name", "fixtures"})
_FIXTURE_KEYS = frozenset({"id", "profile", "mode", "universe", "address", "groups", "position"})
_POSITION_KEYS = frozenset({"x", "y"})


# ----- model --------------------------------------------------------------


@dataclass(frozen=True)
class StagePosition:
    x: float
    y: float


@dataclass(frozen=True)
class PatchedFixture:
    id: str
    profile_id: str
    mode: str
    universe: int
    address: int
    groups: tuple[str, ...]
    position: StagePosition


@dataclass(frozen=True)
class Patch:
    name: str
    fixtures: tuple[PatchedFixture, ...]

    def fixture(self, fixture_id: str) -> PatchedFixture:
        for fixture in self.fixtures:
            if fixture.id == fixture_id:
                return fixture
        raise KeyError(f"patch {self.name!r} has no fixture {fixture_id!r}; fixtures: {[f.id for f in self.fixtures]}")

    def group(self, name: str) -> tuple[PatchedFixture, ...]:
        return tuple(f for f in self.fixtures if name in f.groups)


@dataclass(frozen=True)
class VenueStore:
    """Several named patches (Venues) with exactly one active."""

    active: str
    patches: tuple[Patch, ...]

    def patch(self, name: str) -> Patch:
        for patch in self.patches:
            if patch.name == name:
                return patch
        raise KeyError(f"no patch {name!r}; patches: {[p.name for p in self.patches]}")

    @property
    def active_patch(self) -> Patch:
        return self.patch(self.active)

    def with_patch(self, patch: Patch) -> VenueStore:
        """Add ``patch``, or replace the patch of the same name in place."""
        if patch.name not in {p.name for p in self.patches}:
            return replace(self, patches=(*self.patches, patch))
        return replace(self, patches=tuple(patch if p.name == patch.name else p for p in self.patches))

    def with_active(self, name: str) -> VenueStore:
        self.patch(name)
        return replace(self, active=name)


class PatchError(ValueError):
    """A patch failed validation; ``errors`` holds one ``<where>: <reason>`` per problem."""

    def __init__(self, source: str, errors: Sequence[str]) -> None:
        self.errors = tuple(errors)
        super().__init__(f"invalid lighting patch {source}:\n" + "\n".join(f"  {e}" for e in self.errors))


# ----- validation ---------------------------------------------------------


class _Errors:
    def __init__(self) -> None:
        self.items: list[str] = []

    def add(self, path: str, reason: str) -> None:
        self.items.append(f"{path}: {reason}")


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_unit(value: object) -> bool:
    return (
        isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0.0 <= value <= 1.0
    )


def _footprint(
    fixture: PatchedFixture, profiles: Mapping[str, FixtureProfile], path: str, errors: _Errors
) -> int | None:
    profile = profiles.get(fixture.profile_id)
    if profile is None:
        errors.add(f"{path}.profile", f"unknown profile {fixture.profile_id!r}; profiles: {sorted(profiles)}")
        return None
    try:
        return len(profile.mode(fixture.mode).channels)
    except KeyError as err:
        errors.add(f"{path}.mode", str(err.args[0]))
        return None


def _check_fields(fixture: PatchedFixture, path: str, errors: _Errors) -> bool:
    """Field checks; returns True when universe and address are usable for footprint checks."""
    usable = True
    if not fixture.id.strip():
        errors.add(f"{path}.id", "must be a non-empty string")
    if not _is_int(fixture.universe) or not MIN_UNIVERSE <= fixture.universe <= MAX_UNIVERSE:
        errors.add(f"{path}.universe", f"must be {MIN_UNIVERSE}..{MAX_UNIVERSE}, got {fixture.universe!r}")
        usable = False
    if not _is_int(fixture.address) or not 1 <= fixture.address <= DMX_UNIVERSE_CHANNELS:
        errors.add(f"{path}.address", f"must be 1..{DMX_UNIVERSE_CHANNELS}, got {fixture.address!r}")
        usable = False
    for index, group in enumerate(fixture.groups):
        if not isinstance(group, str) or not group.strip():
            errors.add(f"{path}.groups[{index}]", "must be a non-empty string")
        elif group in fixture.groups[:index]:
            errors.add(f"{path}.groups[{index}]", f"duplicate group {group!r}")
    for axis in ("x", "y"):
        value = getattr(fixture.position, axis)
        if not _is_unit(value):
            errors.add(f"{path}.position.{axis}", f"must be a number in 0..1, got {value!r}")
    return usable


def _patch_errors(patch: Patch, profiles: Mapping[str, FixtureProfile]) -> list[str]:
    errors = _Errors()
    if not patch.name.strip():
        errors.add("name", "must be a non-empty string")
    first_index: dict[str, int] = {}
    spans: dict[int, list[tuple[int, int, str]]] = {}
    for index, fixture in enumerate(patch.fixtures):
        path = f"fixtures[{index}]"
        if fixture.id in first_index:
            errors.add(f"{path}.id", f"duplicate id {fixture.id!r} (also fixtures[{first_index[fixture.id]}])")
        first_index.setdefault(fixture.id, index)
        usable = _check_fields(fixture, path, errors)
        width = _footprint(fixture, profiles, path, errors)
        if width is None or not usable:
            continue
        last = fixture.address + width - 1
        if last > DMX_UNIVERSE_CHANNELS:
            errors.add(
                path,
                f"fixture {fixture.id!r} ({fixture.profile_id} {width}ch) at universe {fixture.universe} "
                f"address {fixture.address} runs to slot {last}, past {DMX_UNIVERSE_CHANNELS}",
            )
        spans.setdefault(fixture.universe, []).append((fixture.address, last, fixture.id))
    for universe, ranges in sorted(spans.items()):
        ranges.sort()
        for i, (start_a, end_a, id_a) in enumerate(ranges):
            for start_b, end_b, id_b in ranges[i + 1 :]:
                if start_b > end_a:
                    break
                errors.add(
                    f"universe {universe}",
                    f"fixtures {id_a!r} ({start_a}..{end_a}) and {id_b!r} ({start_b}..{end_b}) "
                    f"overlap at slots {start_b}..{min(end_a, end_b)}",
                )
    return errors.items


def validate_patch(patch: Patch, profiles: Mapping[str, FixtureProfile]) -> None:
    """Raise ``PatchError`` listing every problem in ``patch``."""
    items = _patch_errors(patch, profiles)
    if items:
        raise PatchError(repr(patch.name), items)


def validate_store(store: VenueStore, profiles: Mapping[str, FixtureProfile]) -> None:
    """Validate every patch, unique patch names and the active name; raise ``PatchError`` listing all."""
    items: list[str] = []
    seen: set[str] = set()
    for patch in store.patches:
        if patch.name in seen:
            items.append(f"patch {patch.name!r}: duplicate patch name")
        seen.add(patch.name)
        items.extend(f"patch {patch.name!r} {item}" for item in _patch_errors(patch, profiles))
    if store.active not in seen:
        items.append(f"active: no patch {store.active!r}; patches: {[p.name for p in store.patches]}")
    if items:
        raise PatchError("store", items)


# ----- JSON ---------------------------------------------------------------


def _fixture_to_json(fixture: PatchedFixture) -> dict[str, Any]:
    return {
        "id": fixture.id,
        "profile": fixture.profile_id,
        "mode": fixture.mode,
        "universe": fixture.universe,
        "address": fixture.address,
        "groups": list(fixture.groups),
        "position": {"x": fixture.position.x, "y": fixture.position.y},
    }


def store_to_json(store: VenueStore) -> dict[str, Any]:
    return {
        "version": PATCH_FILE_VERSION,
        "active": store.active,
        "patches": [{"name": p.name, "fixtures": [_fixture_to_json(f) for f in p.fixtures]} for p in store.patches],
    }


def _object(value: object, path: str, allowed: frozenset[str], errors: _Errors) -> Mapping[str, Any] | None:
    if not isinstance(value, dict):
        errors.add(path, f"must be an object, got {type(value).__name__}")
        return None
    for key in sorted(set(value) - allowed):
        errors.add(f"{path}.{key}", f"unknown key; allowed: {sorted(allowed)}")
    for key in sorted(allowed - set(value)):
        errors.add(f"{path}.{key}", "missing")
    return value


def _typed(obj: Mapping[str, Any], key: str, path: str, kind: str, errors: _Errors) -> Any:
    value = obj.get(key)
    ok = {
        "string": isinstance(value, str),
        "integer": _is_int(value),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "list": isinstance(value, list),
    }[kind]
    if key in obj and not ok:
        errors.add(f"{path}.{key}", f"must be a {kind}, got {value!r}")
    return value if ok else None


def _fixture_from_json(raw: object, path: str, errors: _Errors) -> PatchedFixture | None:
    obj = _object(raw, path, _FIXTURE_KEYS, errors)
    if obj is None:
        return None
    fields = {
        key: _typed(obj, key, path, kind, errors)
        for key, kind in (
            ("id", "string"),
            ("profile", "string"),
            ("mode", "string"),
            ("universe", "integer"),
            ("address", "integer"),
            ("groups", "list"),
        )
    }
    pos = _object(obj.get("position"), f"{path}.position", _POSITION_KEYS, errors) if "position" in obj else None
    x = _typed(pos, "x", f"{path}.position", "number", errors) if pos is not None else None
    y = _typed(pos, "y", f"{path}.position", "number", errors) if pos is not None else None
    groups = fields["groups"]
    if groups is not None:
        for index, group in enumerate(groups):
            if not isinstance(group, str):
                errors.add(f"{path}.groups[{index}]", f"must be a string, got {group!r}")
                groups = None
    if None in fields.values() or groups is None or x is None or y is None:
        return None
    return PatchedFixture(
        fields["id"],
        fields["profile"],
        fields["mode"],
        fields["universe"],
        fields["address"],
        tuple(groups),
        StagePosition(x, y),
    )


def store_from_json(data: object, source: str = "<data>") -> VenueStore:
    """Structural parse of the patch file; profile-dependent checks are ``validate_store``."""
    errors = _Errors()
    obj = _object(data, "$", _STORE_KEYS, errors)
    if obj is None:
        raise PatchError(source, errors.items)
    if "version" in obj and obj["version"] != PATCH_FILE_VERSION:
        errors.add("$.version", f"unsupported version {obj['version']!r}; expected {PATCH_FILE_VERSION}")
    active = _typed(obj, "active", "$", "string", errors)
    raw_patches = _typed(obj, "patches", "$", "list", errors) or []
    patches: list[Patch] = []
    for index, raw in enumerate(raw_patches):
        path = f"$.patches[{index}]"
        patch_obj = _object(raw, path, _PATCH_KEYS, errors)
        if patch_obj is None:
            continue
        name = _typed(patch_obj, "name", path, "string", errors)
        raw_fixtures = _typed(patch_obj, "fixtures", path, "list", errors) or []
        fixtures = [_fixture_from_json(f, f"{path}.fixtures[{i}]", errors) for i, f in enumerate(raw_fixtures)]
        if name is not None:
            patches.append(Patch(name, tuple(f for f in fixtures if f is not None)))
    if errors.items:
        raise PatchError(source, errors.items)
    return VenueStore(active, tuple(patches))


# ----- persistence --------------------------------------------------------


def default_patch_path() -> Path:
    return paths.DATA_DIR / "lighting" / "patch.json"


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, suffix=".tmp", encoding="utf-8") as tmp:
        tmp_path = Path(tmp.name)
        try:
            tmp.write(text)
            tmp.flush()
            os.fsync(tmp.fileno())
        except BaseException:
            tmp.close()
            tmp_path.unlink(missing_ok=True)
            raise
    try:
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def save_store(path: Path, store: VenueStore, profiles: Mapping[str, FixtureProfile]) -> None:
    """Validate, then write atomically; an invalid store never reaches disk."""
    validate_store(store, profiles)
    _atomic_write_text(path, json.dumps(store_to_json(store), indent=2, sort_keys=True) + "\n")


def load_store(path: Path) -> VenueStore:
    """Read and parse the patch file; a missing file raises ``FileNotFoundError``."""
    return store_from_json(json.loads(path.read_text(encoding="utf-8")), str(path))


def switch_active(path: Path, name: str, profiles: Mapping[str, FixtureProfile]) -> VenueStore:
    """Make ``name`` the active patch on disk; every patch's content is rewritten unchanged."""
    store = load_store(path).with_active(name)
    save_store(path, store, profiles)
    return store
