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
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path

from scripts.lock_marker_parser import Unknown
from scripts.lock_metadata_toml import norm_name, toml_list, toml_strings
from scripts.lock_requirement_keys import Key, fmt_key, pair_by_meaning, same_requirement

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
# The index uv resolves registry requirements against when no `[[tool.uv.index]]`,
# legacy `index-url` / `extra-index-url`, `find-links` or `no-index` names another;
# index configuration changes what a fresh `uv lock` writes (round 47), and this
# check does not compare it, so any of it on either side is UNKNOWN.
DEFAULT_INDEX = "https://pypi.org/simple"
INDEX_KEYS = ("index", "index-url", "extra-index-url", "find-links", "no-index")


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


def _conflicts(value: object, where: str, project: str, *, recorded: bool) -> list[ConflictSet]:
    """Each conflict set as a sorted tuple of items, each item its sorted (key, value)
    pairs with `package` filled in the way uv records it. An item names at most one
    of `extra` / `group` (both is "Expected one of `extra` or `group` ... but found
    both"), at least one of the three ("Expected `package`, `extra` or `group` field"),
    no other key than those, string values only, and a set on either side holds at
    least two items; a recorded item carries `package` (measured uv 0.8.17, Codex P2
    on #3763, rounds 43, 45 and 46). A package-only item is read (uv 0.8.17 calls
    package conflicts experimental and warns; uv 0.7.22 refused them). uv
    writes each set sorted, and the spelled order does not matter, so both sides are
    compared sorted."""
    sets: list[ConflictSet] = []
    for group in toml_list(value, where):
        items: list[tuple[tuple[str, str], ...]] = []
        for item in toml_list(group, f"{where} set"):
            if (
                not isinstance(item, dict)
                or not all(isinstance(v, str) for v in item.values())
                or not set(item) <= {"package", "extra", "group"}
            ):
                raise Unknown(f"{where} item {item!r} is not a conflict selector; uv rejects it")
            if not item:
                raise Unknown(f"{where} item {{}} names no package, extra or group; uv rejects it")
            if "extra" in item and "group" in item:
                raise Unknown(f"{where} item {item!r} names both extra and group; uv rejects it")
            if recorded and "package" not in item:
                raise Unknown(f"{where} item {item!r} names no package; uv rejects the file")
            filled = {"package": project, **item}
            items.append(tuple(sorted((k, norm_name(v)) for k, v in filled.items())))
        if len(items) < 2:
            raise Unknown(f"{where} set {group!r} holds fewer than two items; uv rejects it")
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
    the checker's comparable key. Workspace `members`, `dependency-metadata` and index
    configuration (a `[tool.uv]` index key, or a package recorded from a registry other
    than the default index) on either side are not compared: UNKNOWN."""
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
    _refuse_index_configuration(tool, lock)
    lines = [line for spec in OPTIONS if (line := _option(tool, options, spec)) is not None]
    for spelled_key, recorded_key in MANIFEST:
        # uv records a repeated or equivalent requirement ONCE (`six<2` beside `six<2.0`,
        # or beside itself under a `python_version` marker uv canonicalizes), and reads a
        # lock that repeats one (round 45): both sides are compared as sets by meaning.
        want = _once(
            spelled(text)
            for text in toml_strings(tool.get(spelled_key, []), f"[tool.uv] {spelled_key}")
        )
        have = _once(
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
    want_sets = _conflicts(
        tool.get("conflicts", []), "[tool.uv] conflicts", project, recorded=False
    )
    have_sets = _conflicts(lock.get("conflicts", []), "uv.lock conflicts", project, recorded=True)
    if want_sets != have_sets:
        lines.append(f"  [tool.uv] conflicts: pyproject.toml {want_sets!r}, uv.lock {have_sets!r}")
    return lines


def refuse_dynamic_dependencies(project: dict[str, object]) -> None:
    """`[project] dynamic` listing `dependencies` or `optional-dependencies` hands them
    to the build backend at lock time (uv 0.8.17 resolves what the backend emits and
    `uv lock --check` exits 1 against the lock the static lists left, measured round
    48); this check never runs a backend, so it cannot see them: UNKNOWN."""
    dynamic = toml_strings(project.get("dynamic", []), "[project] dynamic")
    supplied = sorted({"dependencies", "optional-dependencies"} & set(dynamic))
    if supplied:
        raise Unknown(
            f"[project] dynamic lists {supplied}: the build backend supplies them at lock"
            " time and this check does not run it"
        )


def refuse_uv_toml(project_dir: Path) -> None:
    """A `uv.toml` beside the pyproject outranks `[tool.uv]` for every setting this
    check reads there (round 47): UNKNOWN."""
    if (project_dir / "uv.toml").exists():
        raise Unknown(f"{project_dir / 'uv.toml'} exists and is not read by this check")


def _refuse_index_configuration(tool: dict[str, object], lock: dict[str, object]) -> None:
    """A `[tool.uv]` index key, or a package recorded from a registry other than the
    default index (resolved under index configuration from a uv.toml, an environment
    variable or a since-removed key): UNKNOWN, round 47."""
    for key in INDEX_KEYS:
        if key in tool:
            raise Unknown(f"[tool.uv] {key}: index configuration is not compared by this check")
    for entry in toml_list(lock.get("package", []), "uv.lock package"):
        registry = entry["source"].get("registry") if isinstance(entry, dict) else None
        if isinstance(registry, str) and registry != DEFAULT_INDEX:
            raise Unknown(
                f"uv.lock [[package]] {entry['name']!r} registry {registry!r} is not the"
                f" default index {DEFAULT_INDEX!r}; index configuration is not compared"
                " by this check"
            )


def _once(keys: Iterable[Key]) -> Counter[Key]:
    """Each requirement once, the way uv records a repeated or equivalent spelling."""
    seen: Counter[Key] = Counter()
    for key in keys:
        if not any(same_requirement(key, other) for other in seen):
            seen[key] = 1
    return seen


def _refuse(message: str) -> bool:
    raise Unknown(f"{message}; uv rejects the file")
