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

import re
from collections import Counter
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path

from scripts.lock_marker_parser import Unknown
from scripts.lock_metadata_toml import norm_name, toml_list, toml_strings
from scripts.lock_requirement_keys import Key, fmt_key, pair_by_meaning, same_requirement
from scripts.lock_specifier_semantics import norm_spec

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
    _refuse_required_version(tool)  # round 57
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


_REQUIREMENT_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def refuse_unscoped_sources(pyproject: dict[str, object], sources: dict[str, object]) -> None:
    """A `[tool.uv.sources]` entry carrying `extra` or `group` names a VALID extra or
    dependency group that LISTS the package, or uv refuses the project, used or not
    (measured uv 0.8.17, Codex P2 on #3763, round 53): `extra = "bad space"`, `-bad`,
    `bad!` and a non-ASCII name are "Not a valid package or extra name", "Failed to
    parse: `pyproject.toml`", exit 2; a valid name whose extra or group does not exist
    ("the `dev` extra does not exist"), or exists without the package ("`unused` was
    not found under the `project.optional-dependencies` section for that extra", the
    same for `dependency-groups`), is "Failed to generate package metadata", exit 2.
    Names match normalized on both sides (`Dev_X` lists `LocalDep` for `extra =
    "dev-x"`), a requirement's extras and marker do not matter, and an `include-group`
    chain counts. Typing the value as a string had let every one of these through
    against an unchanged lock."""
    project = pyproject.get("project", {})
    extras = project.get("optional-dependencies", {}) if isinstance(project, dict) else {}
    groups = pyproject.get("dependency-groups", {})
    if not isinstance(extras, dict):
        raise Unknown(
            f"[project] optional-dependencies = {extras!r} is not a table; uv rejects the file"
        )
    if not isinstance(groups, dict):
        raise Unknown(f"[dependency-groups] = {groups!r} is not a table; uv rejects the file")
    for name, source in sources.items():
        for entry in source if isinstance(source, list) else [source]:
            if not isinstance(entry, dict):
                continue  # pyproject_source_shape reports the shape
            for scope, table in (("extra", extras), ("group", groups)):
                if scope in entry and isinstance(entry[scope], str):
                    _scope_lists(name, scope, entry[scope], table)


def _scope_lists(name: str, scope: str, spelled: str, table: dict) -> None:
    where = f"[tool.uv.sources] {name} {scope} = {spelled!r}"
    wanted = norm_name(spelled)  # UNKNOWN for a name uv refuses to parse
    listed = {norm_name(str(k)): v for k, v in table.items()}
    if wanted not in listed:
        raise Unknown(f"{where}: the {wanted!r} {scope} does not exist; uv refuses to lock")
    members = _group_members(listed, wanted, ()) if scope == "group" else _members(listed[wanted])
    if norm_name(name) not in members:
        raise Unknown(f"{where}: {name!r} is not listed under that {scope}; uv refuses to lock")


def _members(entries: object) -> set[str]:
    """The normalized names a requirement list names (extras and markers stripped; a
    malformed requirement names nothing here and is reported where it is parsed)."""
    if not isinstance(entries, list):
        return set()
    found = set()
    for entry in entries:
        match = _REQUIREMENT_NAME_RE.match(entry) if isinstance(entry, str) else None
        if match is not None:
            found.add(norm_name(match.group(1)))
    return found


def _group_members(groups: dict[str, object], name: str, stack: tuple[str, ...]) -> set[str]:
    """A PEP 735 group's names with every `include-group` expanded (a cycle or a missing
    group is reported where the groups are compared: here it names nothing)."""
    entries = groups.get(name)
    if name in stack or not isinstance(entries, list):
        return set()
    found = _members(entries)
    for entry in entries:
        if isinstance(entry, dict) and isinstance(entry.get("include-group"), str):
            included = norm_name(entry["include-group"])
            found |= _group_members(groups, included, (*stack, name))
    return found


def refuse_uv_toml(project_dir: Path) -> None:
    """A `uv.toml` beside the pyproject outranks `[tool.uv]` for every setting this
    check reads there (round 47): UNKNOWN."""
    if (project_dir / "uv.toml").exists():
        raise Unknown(f"{project_dir / 'uv.toml'} exists and is not read by this check")


def _refuse_required_version(tool: dict[str, object]) -> None:
    """`[tool.uv] required-version` gates `uv lock` on the uv that runs it: uv 0.8.17
    exits 2 under `>=999` or `<0.8` ("Required uv version ... does not match the running
    version") and locks under `>=0.8` or `==0.8.17`; this check runs no uv and cannot
    tell which the one that will lock is, so a non-empty specifier is UNKNOWN (Codex P2
    on #3763, round 57). `""` is read as any version. `"x"` and `1` are the
    settings-discovery warning that drops the table (`IGNORED`)."""
    if "required-version" not in tool:
        return
    value = tool["required-version"]
    if not isinstance(value, str):
        raise Unknown(f"[tool.uv] required-version = {value!r} is not a string; {IGNORED}")
    if not value.strip():
        return
    try:
        norm_spec(value)
    except Unknown as exc:
        raise Unknown(f"[tool.uv] required-version = {value!r}: {exc}; {IGNORED}") from exc
    raise Unknown(
        f"[tool.uv] required-version = {value!r}: this check does not run uv and cannot"
        " tell whether the uv that locks satisfies it; uv refuses to lock when it does not"
    )


def _refuse_index_configuration(tool: dict[str, object], lock: dict[str, object]) -> None:
    """A `[tool.uv]` index key, or a package recorded from a registry other than the
    default index (resolved under index configuration from a uv.toml, an environment
    variable or a since-removed key): UNKNOWN, round 47. `no-sources = true` makes uv
    resolve without `[tool.uv.sources]`, which this check applies to every requirement,
    so a source-backed lock is stale under it (`uv lock --check` exit 1, measured uv
    0.8.17, round 50): UNKNOWN; `false` is the default and reads; a non-boolean is the
    settings-discovery warning that drops the whole table."""
    no_sources = tool.get("no-sources", False)
    if not isinstance(no_sources, bool):
        raise Unknown(f"[tool.uv] no-sources = {no_sources!r} is not a boolean; {IGNORED}")
    if no_sources:
        raise Unknown(
            "[tool.uv] no-sources = true: uv resolves without [tool.uv.sources], which this"
            " check applies; not compared by this check"
        )
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
