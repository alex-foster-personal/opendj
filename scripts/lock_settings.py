"""`[tool.uv]` settings that shape a resolution, against what uv.lock records of them
(stdlib only, like the checker).

uv records `resolution`, `prerelease`, `fork-strategy` and `exclude-newer` under
`[options]` (only when set away from the default), `constraint-dependencies`,
`override-dependencies` and `build-constraint-dependencies` under `[manifest]` as
requirement records, and `conflicts` at the top level with the project's name filled
in. `uv lock --check` is exit 1 when either side of any pair differs (measured uv
0.8.17, Codex P2 on #3763, round 43); the checker had compared none of them, so
adding `resolution = "lowest-direct"` without relocking read as clean.

Landmine, measured the same round: a scalar setting uv cannot read (`resolution = 1`
or `"bogus"`, `exclude-newer = 1` or a date with no time) is not a parse failure. uv
prints "Failed to parse `pyproject.toml` during settings discovery" as a WARNING,
drops the WHOLE settings table, and resolves with the defaults: exit 0 when the lock
happens to match those. That verdict rests on settings uv did not read, so it is
UNKNOWN here, not a compare against the defaults.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from datetime import datetime

from scripts.lock_marker_parser import Unknown
from scripts.lock_metadata_toml import norm_name, toml_list, toml_strings
from scripts.lock_requirement_keys import Key, fmt_key, pair_by_meaning

# (pyproject key, lock key under [options], uv's default, the values uv reads)
OPTIONS: tuple[tuple[str, str, str | None, tuple[str, ...] | None], ...] = (
    ("resolution", "resolution-mode", "highest", ("highest", "lowest", "lowest-direct")),
    (
        "prerelease",
        "prerelease-mode",
        "if-necessary-or-explicit",
        ("disallow", "allow", "if-necessary", "explicit", "if-necessary-or-explicit"),
    ),
    ("fork-strategy", "fork-strategy", "requires-python", ("fewest", "requires-python")),
    ("exclude-newer", "exclude-newer", None, None),
)
MANIFEST = (
    ("constraint-dependencies", "constraints"),
    ("override-dependencies", "overrides"),
    ("build-constraint-dependencies", "build-constraints"),
)
ConflictSet = tuple[tuple[tuple[str, str], ...], ...]
IGNORED = "uv drops every [tool.uv] setting with a warning and resolves with the defaults"


def _stamp(value: object, where: str) -> datetime:
    if isinstance(value, str):
        try:
            stamp = datetime.fromisoformat(value)
        except ValueError:
            stamp = None
        if stamp is not None and stamp.tzinfo is not None:
            return stamp
    raise Unknown(f"{where} = {value!r} is not a zoned timestamp; {IGNORED}")


def _option(uv_tool: dict, options: dict, spec: tuple) -> str | None:
    spelled_key, recorded_key, default, allowed = spec
    spelled: object = uv_tool.get(spelled_key, default)
    recorded: object = options.get(recorded_key, default)
    if allowed is None:  # exclude-newer: a zoned timestamp, compared by instant
        if spelled is None and recorded is None:
            return None
        same = (
            spelled is not None
            and recorded is not None
            and _stamp(spelled, f"[tool.uv] {spelled_key}")
            == _stamp(recorded, f"uv.lock options {recorded_key}")
        )
        if spelled is not None:
            _stamp(spelled, f"[tool.uv] {spelled_key}")
        if recorded is not None:
            _stamp(recorded, f"uv.lock options {recorded_key}")
    else:
        if spelled not in allowed:
            raise Unknown(
                f"[tool.uv] {spelled_key} = {spelled!r} is not one of {allowed}; {IGNORED}"
            )
        if recorded not in allowed:
            raise Unknown(
                f"uv.lock options {recorded_key} = {recorded!r} is not one of {allowed}; "
                "uv rejects the file"
            )
        same = spelled == recorded
    if same:
        return None
    return f"  [tool.uv] {spelled_key}: pyproject.toml {spelled!r}, uv.lock options {recorded!r}"


def _conflicts(value: object, where: str, project: str) -> list[ConflictSet]:
    """Each conflict set as a sorted tuple of (key, value) items with `package` filled in
    the way uv records it; a shape uv refuses (`conflicts = "bad"`) is UNKNOWN."""
    sets: list[ConflictSet] = []
    for group in toml_list(value, where):
        items: list[tuple[tuple[str, str], ...]] = []
        for item in toml_list(group, f"{where} set"):
            if not isinstance(item, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in item.items()
            ):
                raise Unknown(f"{where} item {item!r} is not a table of strings; uv rejects it")
            filled = {"package": project, **item}
            items.append(tuple(sorted((k, norm_name(v)) for k, v in filled.items())))
        sets.append(tuple(sorted(items)))
    return sorted(sets)


def settings_delta(
    uv_tool: dict | None,
    lock: dict,
    project: str,
    spelled: Callable[[str], Key],
    recorded: Callable[[dict], Key],
) -> list[str]:
    """Stale lines for every setting pair; UNKNOWN on a shape uv refuses or drops.
    `spelled`/`recorded` turn one pyproject requirement string / one lock record into
    the checker's comparable key. Workspace `members` and `dependency-metadata` on
    either side are not compared: UNKNOWN."""
    tool = uv_tool or {}
    options = lock.get("options", {})
    if not isinstance(options, dict):
        raise Unknown(f"uv.lock options = {options!r} is not a table; uv rejects the file")
    manifest = lock.get("manifest", {})
    if not isinstance(manifest, dict):
        raise Unknown(f"uv.lock manifest = {manifest!r} is not a table; uv rejects the file")
    for key, where in (
        ("workspace", "[tool.uv] workspace"),
        ("dependency-metadata", "[tool.uv] dependency-metadata"),
    ):
        if key in tool:
            raise Unknown(f"{where} is not compared by this check")
    for key in ("members", "dependency-metadata"):
        if key in manifest:
            raise Unknown(f"uv.lock manifest {key} is not compared by this check")
    lines = [line for spec in OPTIONS if (line := _option(tool, options, spec)) is not None]
    for spelled_key, recorded_key in MANIFEST:
        want: Counter[Key] = Counter(
            spelled(text)
            for text in toml_strings(tool.get(spelled_key, []), f"[tool.uv] {spelled_key}")
        )
        have: Counter[Key] = Counter(
            recorded(entry)
            for entry in toml_list(
                manifest.get(recorded_key, []), f"uv.lock manifest {recorded_key}"
            )
            if isinstance(entry, dict)
            or _refuse(f"uv.lock manifest {recorded_key} record {entry!r} is not a table")
        )
        missing, extra = pair_by_meaning(want - have, have - want)
        lines.extend(
            f"  [tool.uv] {spelled_key}: not in uv.lock manifest: {fmt_key(k)}"
            for k in sorted(missing)
        )
        lines.extend(
            f"  [tool.uv] {spelled_key}: in uv.lock manifest only: {fmt_key(k)}"
            for k in sorted(extra)
        )
    want_sets = _conflicts(tool.get("conflicts", []), "[tool.uv] conflicts", project)
    have_sets = _conflicts(lock.get("conflicts", []), "uv.lock conflicts", project)
    if want_sets != have_sets:
        lines.append(f"  [tool.uv] conflicts: pyproject.toml {want_sets!r}, uv.lock {have_sets!r}")
    return lines


def _refuse(message: str) -> bool:
    raise Unknown(f"{message}; uv rejects the file")
