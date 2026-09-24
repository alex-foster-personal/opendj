"""TOML field types for scripts/lock_metadata_check.py (stdlib only, like it).

uv refuses a pyproject.toml or uv.lock field of the wrong TOML type ("invalid type:
integer, expected a string", "invalid type: map, expected a sequence", "expected struct
ToolUv"; `uv lock --check` exit 2, measured uv 0.8.17 across Codex rounds 18-30 on
#3763). A checker that coerces such a field (`str()`, iterating a mapping's keys,
reading a non-table as absent) can read a file uv rejects as clean, so every read
below is UNKNOWN on the wrong type and returns the value typed on the right one.
"""

from __future__ import annotations

import re
from datetime import datetime

from scripts.lock_marker_parser import Unknown, _MarkerParser
from scripts.lock_specifier_semantics import norm_spec

_NAME_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?$")


def norm_name(name: str) -> str:
    """A package, extra or dependency-group name, normalized the way uv records it
    (`Foo_Bar` -> `foo-bar`). A name uv refuses ("Not a valid package or extra name:
    ... Names must start and end with a letter or digit and may only contain -, _, .,
    and alphanumeric characters", `uv lock --check` exit 2; measured uv 0.8.17 for a
    project name, an extra and a dependency group, Codex P2 on #3763, round 31) is
    UNKNOWN before normalization, or the same invalid name in both files matched."""
    if _NAME_RE.match(name) is None:
        raise Unknown(f"{name!r} is not a valid package or extra name; uv rejects the file")
    return re.sub(r"[-_.]+", "-", name).lower()


def toml_flag(table: dict, key: str, where: str) -> bool | None:
    """A boolean setting, None when absent. A present non-boolean (`package =
    "false"`) is a file uv refuses to parse (measured: `invalid type: string "false",
    expected a boolean`), so it is UNKNOWN rather than a guess either way (round 18)."""
    if key not in table:
        return None
    value = table[key]
    if not isinstance(value, bool):
        raise Unknown(f"{where} {key} = {value!r} is not a boolean; uv rejects the file")
    return value


def toml_string(value: object, where: str) -> str:
    """`where` as the string uv requires there (round 25: str() read an integer
    include-group or source path as the clean string it replaced)."""
    if not isinstance(value, str):
        raise Unknown(f"{where} = {value!r} is not a string; uv rejects the file")
    return value


def toml_optional_string(value: object, where: str) -> str | None:
    """As toml_string for a key that may be absent (None passes through): a present
    `requires-python = 0` is "invalid type: integer `0`, expected a string" on either
    side of the pair (round 35), while `0 or ""` had read it as no constraint."""
    return None if value is None else toml_string(value, where)


def toml_list(value: object, where: str) -> list:
    """`where` as the list uv requires there. An inline table (`requires-dist = {}`,
    a `requires-dev` group `= {}`) iterates as its keys, so an EMPTY one satisfied an
    empty declaration (round 30); uv: "invalid type: map, expected a sequence"."""
    if not isinstance(value, list):
        raise Unknown(f"{where} = {value!r} is not a list; uv rejects the file")
    return value


def toml_strings(value: object, where: str) -> list[str]:
    """As toml_string, for a list of strings (a `dependencies` inline table read as
    its keys; `provides-extras = [1]` read as the extra "1", round 30)."""
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise Unknown(f"{where} = {value!r} is not a list of strings; uv rejects the file")
    return value


def uv_table(pyproject: dict, where: str = "pyproject.toml") -> dict | None:
    """`[tool.uv]` as a table, or None when absent. A top-level `tool = "bad"` is
    ignored by uv (`uv lock` exit 0) so it is no [tool.uv]; a present `[tool] uv =
    "bad"` is "expected struct ToolUv" (exit 2), at the root AND in a path
    dependency's target, whose `package` flag decides virtual vs directory (round 30
    read the target's as absent)."""
    tool = pyproject.get("tool")
    uv = tool.get("uv") if isinstance(tool, dict) else None
    if uv is None:
        return None
    if not isinstance(uv, dict):
        raise Unknown(f"{where} [tool] uv = {uv!r} is not a table; uv rejects the file")
    return uv


def lock_schema(lock: dict) -> None:
    """uv reads only schema `version = 1` ("uses an unsupported schema version (v2, but
    only v1 is supported)", and a missing field is "missing field `version`"; exit 2,
    measured uv 0.8.17, Codex P2 on #3763, round 32). `version = true` is "invalid
    type: boolean `true`, expected u32" (round 35), and `True == 1` in Python, so the
    bool is excluded by type. `revision` is NOT checked: uv accepted 1, 999 and no
    revision at all on the same pair (measured alongside)."""
    version = lock.get("version")
    if isinstance(version, bool) or version != 1:
        raise Unknown(f"uv.lock schema version = {version!r}; only 1 is readable by uv")
    resolution_markers(lock, "uv.lock")


_SOURCE_KEYS = ("registry", "git", "url", "path", "directory", "editable", "virtual")


def source_table(source: object, name: object) -> None:
    """A package `source` uv can read: a table with at least one source key holding a
    string. `source = "bad"`, `{}`, `{ registry = 1 }` and `{ bogus = "x" }` are each
    "did not match any variant of untagged enum SourceWire", `uv lock --check` exit 2,
    while an extra key beside a valid one is read (measured uv 0.8.17, Codex P2 on
    #3763, round 34). Whether a git or url source RESOLVES is not this check's
    question; a requirement on one is UNKNOWN in lock_requirement anyway."""
    if not isinstance(source, dict) or not any(
        isinstance(source.get(key), str) for key in _SOURCE_KEYS
    ):
        raise Unknown(f"uv.lock [[package]] {name!r} source = {source!r} is not a source table")


def parsed_marker(value: object, where: str) -> str:
    """A marker uv can read: a string the marker grammar accepts. `marker = 1` is a
    type error and `marker = "bad"` or `marker = ""` "Expected marker value", each
    exit 2 (measured uv 0.8.17, rounds 37 and 38). Parsed, not compared."""
    marker = toml_string(value, where)
    if not marker.strip():
        raise Unknown(f"{where} = {marker!r} is blank; uv rejects the file")
    try:
        _MarkerParser(marker).parse()
    except Unknown as exc:
        raise Unknown(f"{where}: {exc}") from exc
    return marker


_HASH_RE = re.compile(r"^(md5|sha256|sha384|sha512|blake2b):")
_WHEEL_RE = re.compile(r"^(?P<name>[^-]+)-[^-]+(-[^-]+)?-[^-]+-[^-]+-[^-]+\.whl$")


def recorded_marker(entry: dict, where: str) -> str | None:
    """A uv.lock requirement's `marker`: None when absent, else a string the grammar
    accepts. `marker = ""` is "Expected marker value" and `marker = 1` or `"bad"` are
    refused too, each `uv lock --check` exit 2 (measured uv 0.8.17, Codex P2 on #3763,
    round 38); a blank marker had read as no marker."""
    if "marker" not in entry:
        return None
    return parsed_marker(entry["marker"], f"{where} marker")


def resolution_markers(table: dict, where: str) -> None:
    """`resolution-markers`, on the lock or on a package: absent, or a list of markers
    the grammar accepts. `[true]`, `{}`, `[1]`, `["bad"]`, `[""]` and a bare string
    are each "Failed to parse `uv.lock`", `uv lock --check` exit 2, while `[]` and a
    list of valid markers are read (measured uv 0.8.17, Codex P2 on #3763, round 39).
    Whether the forks match the resolution is not modeled: uv re-resolves for that."""
    if "resolution-markers" not in table:
        return
    for marker in toml_strings(table["resolution-markers"], f"{where} resolution-markers"):
        parsed_marker(marker, f"{where} resolution-markers")


def requirement_records(value: object, where: str) -> None:
    """A metadata requirement list uv can read (`requires-dist`, or one `requires-dev`
    group), on ANY package: a list of tables each with a valid `name`, any `marker`
    the grammar accepts, any `extras` a list of valid names, and on a registry record
    any `specifier` a string with no empty clause (a path-sourced record's specifier
    is not read, round 38). `{}`, `[1]`, `[{}]`, `name = 1`, `name = "bad space"`,
    `marker = ""`, `specifier = 1`, `specifier = ">=1,"`, `extras = "x"` and
    `extras = ["bad space"]` are each "Failed to parse `uv.lock`", exit 2, while a git
    record, an unknown key, an unnormalized extra and `[]` are read (measured uv
    0.8.17 on a non-root package, Codex P2 on #3763, round 40). Compared by meaning
    only on the root, in lock_metadata_check."""
    for record in toml_list(value, where):
        if not isinstance(record, dict) or "name" not in record:
            raise Unknown(f"{where} record {record!r} is not a table with a name")
        dep = norm_name(toml_string(record["name"], f"{where} name"))
        recorded_marker(record, f"{where} {dep!r}")
        if "specifier" in record and not {"directory", "editable", "virtual"} & set(record):
            norm_spec(toml_string(record["specifier"], f"{where} {dep!r} specifier"))
        for extra in toml_strings(record.get("extras", []), f"{where} {dep!r} extras"):
            norm_name(extra)


def metadata_records(entry: dict, name: str) -> None:
    """A package's `[package.metadata]` uv can read, on ANY package, not only the root:
    a table, with `requires-dist` a requirement list, `requires-dev` a table of valid
    group names to requirement lists and `provides-extras` a list of valid names.
    `metadata = 1`, `requires-dist = {}`, `requires-dev = "bad"`, a group `= {}` or
    `= [1]`, a group named `"bad space"` and `provides-extras = [1]` or `["bad space"]`
    are each "Failed to parse `uv.lock`", exit 2 (measured uv 0.8.17, Codex P2 on
    #3763, round 40); the validators had read only the root's."""
    if "metadata" not in entry:
        return
    where = f"uv.lock [[package]] {name!r} metadata"
    metadata = entry["metadata"]
    if not isinstance(metadata, dict):
        raise Unknown(f"{where} = {metadata!r} is not a table; uv rejects the file")
    if "requires-dist" in metadata:
        requirement_records(metadata["requires-dist"], f"{where} requires-dist")
    if "requires-dev" in metadata:
        groups = metadata["requires-dev"]
        if not isinstance(groups, dict):
            raise Unknown(f"{where} requires-dev = {groups!r} is not a table; uv rejects it")
        for group, records in groups.items():
            requirement_records(records, f"{where} requires-dev {norm_name(group)}")
    for extra in toml_strings(metadata.get("provides-extras", []), f"{where} provides-extras"):
        norm_name(extra)


def package_records(lock: dict) -> list[dict]:
    """Every `[[package]]` of the lock, each typed as uv types it: a table with `name`
    and `source` (a record without either is "missing field"; `package = "bad"` is
    "expected a sequence"; round 33), any `version` a string on EVERY record (round
    35), then its dependencies (which must name packages of this list), artifacts,
    resolution markers and metadata. Returned for the root lookup."""
    packages = toml_list(lock.get("package", []), "uv.lock package")
    for entry in packages:
        if not isinstance(entry, dict):
            raise Unknown(f"uv.lock [[package]] entry is not a table: {entry!r}")
        for field in ("name", "source"):
            if field not in entry:
                raise Unknown(f"uv.lock [[package]] entry without {field!r}: {entry!r}")
    names = frozenset(
        norm_name(toml_string(entry["name"], "uv.lock [[package]] name")) for entry in packages
    )
    for entry in packages:
        name = toml_string(entry["name"], "uv.lock [[package]] name")
        source_table(entry["source"], name)
        if "version" in entry:
            toml_string(entry["version"], f"uv.lock [[package]] {name!r} version")
        dependency_records(entry, name, names)
        artifact_records(entry, name)
        resolution_markers(entry, f"uv.lock [[package]] {name!r}")
        metadata_records(entry, name)
    return packages


def _artifact_fields(record: dict, where: str) -> None:
    if "hash" in record:
        digest = toml_string(record["hash"], f"{where} hash")
        if _HASH_RE.match(digest) is None:  # `sha1:`, `bogus:`, no colon, "" refused
            raise Unknown(f"{where} hash = {digest!r} names no hash algorithm; uv rejects the file")
    if "size" in record:
        size = record["size"]
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise Unknown(f"{where} size = {size!r} is not a byte count; uv rejects the file")
    if "upload-time" in record:
        stamp = toml_string(record["upload-time"], f"{where} upload-time")
        try:
            aware = datetime.fromisoformat(stamp).tzinfo is not None
        except ValueError:
            aware = False
        if not aware:
            raise Unknown(
                f"{where} upload-time = {stamp!r} is not a zoned timestamp; uv rejects it"
            )


def _wheel_record(wheel: object, name: str, where: str) -> None:
    if not isinstance(wheel, dict):
        raise Unknown(f"{where} entry {wheel!r} is not a table; uv rejects the file")
    located = [key for key in ("url", "path") if key in wheel]
    if len(located) != 1:
        raise Unknown(f"{where} entry {wheel!r} has no url or path; uv rejects the file")
    location = toml_string(wheel[located[0]], f"{where} {located[0]}")
    filename = location.rsplit("/", 1)[-1]
    match = _WHEEL_RE.match(filename)
    if match is None or norm_name(match.group("name")) != name:
        raise Unknown(
            f"{where} {located[0]} = {location!r} does not end in a wheel of {name!r}; "
            "uv rejects the file"
        )
    _artifact_fields(wheel, f"{where} {filename}")


def artifact_records(entry: dict, name: str) -> None:
    """A package's `sdist` and `wheels` uv can read: `sdist` absent or a table, `wheels`
    absent or a list of tables each locating (`url` or `path`, a string) a wheel
    file named for the package; on either, any `hash` an `<algorithm>:` string uv
    knows, any `size` a non-negative integer, any `upload-time` a zoned timestamp.
    `sdist = "bad"`, `size = "34031"`, `size = true`, `size = -1`, `hash = 1`,
    `hash = "sha1:..."`, `hash = ""`, `upload-time = 1`, `"bad"`, `"2024-12-04"`,
    `wheels = {}`, `[1]`, `[[]]`, `[{}]`, a wheel `url = 1`, `"https://x/bad"`, one
    naming another package, or `path = 1` are each "Failed to parse `uv.lock`", exit
    2, while `sdist = {}`, `wheels = []`, an sdist `url = "bad"`, `hash = "sha256:"`,
    a missing hash or size, a `path` wheel, a relative `url` and an unknown key are
    read (measured uv 0.8.17, Codex P2 on #3763, round 38). The record loop had typed
    only name, source, version and dependencies."""
    if "sdist" in entry:
        where = f"uv.lock [[package]] {name!r} sdist"
        sdist = entry["sdist"]
        if not isinstance(sdist, dict):
            raise Unknown(f"{where} = {sdist!r} is not a table; uv rejects the file")
        _artifact_fields(sdist, where)
    if "wheels" in entry:
        where = f"uv.lock [[package]] {name!r} wheels"
        for wheel in toml_list(entry["wheels"], where):
            _wheel_record(wheel, name, where)


def dependency_records(entry: dict, name: str, packages: frozenset[str]) -> None:
    """A package's `dependencies` uv can read: absent, or a list of tables each with a
    valid `name` that (normalized) is a package of the lock, a string `marker` if
    present and a list of valid names `extra` if present. `dependencies = {}`, `[1]`,
    `[{}]`, `[{ name = 1 }]`, `extra = 1`, `marker = 1` and `marker = "bad"` are each
    "Failed to parse `uv.lock`", exit 2 (measured uv 0.8.17, Codex P2 on #3763, round
    37), as are `name = "bad space"`, `name = "-lead"`, `extra = ["bad space"]` ("Not a
    valid package or extra name") and a valid name no package carries ("has missing
    `source` field but has more than one matching package"; round 40), while an unknown
    key beside a valid record, `name = "SIX"` for the package `six` and an extra that
    nothing provides or is not normalized are read. The marker is parsed, not
    compared: only the root's requirements are compared by meaning."""
    if "dependencies" not in entry:
        return
    where = f"uv.lock [[package]] {name!r} dependencies"
    for record in toml_list(entry["dependencies"], where):
        if not isinstance(record, dict) or "name" not in record:
            raise Unknown(f"{where} record {record!r} is not a table with a name")
        dep = norm_name(toml_string(record["name"], f"{where} name"))
        if dep not in packages:
            raise Unknown(f"{where} names {dep!r}, which no [[package]] of the lock carries")
        if "marker" in record:
            parsed_marker(record["marker"], f"{where} {dep!r} marker")
        for extra in toml_strings(record.get("extra", []), f"{where} {dep!r} extra"):
            norm_name(extra)


_PYPROJECT_SOURCE_KINDS = ("git", "url", "path", "index")


def pyproject_source_shape(name: str, source: object) -> None:
    """One `[tool.uv.sources]` entry uv can read, declared or not: a table with exactly
    one of git/url/path/index holding a string, or `workspace` holding a bool, with
    any `editable`/`package` a bool and any `marker` a string; or a non-empty list of
    such tables. `{ path = 1 }`, `"bad"`, `{}`, `{ bogus = "x" }`, `{ git = 1 }`,
    `{ path = ..., git = ... }`, `[]`, `[{ path = 1 }]`, `editable = 1` and
    `marker = 1` are each "Failed to parse: `pyproject.toml`", exit 2, while
    `{ workspace = false }`, an unresolvable path and `[{ path = "dep" }]` are read
    (measured uv 0.8.17, Codex P2 on #3763, round 37); an entry with no requirement
    had passed unread."""
    where = f"[tool.uv.sources] {name}"
    if isinstance(source, list):
        if not source:
            raise Unknown(f"{where} = [] names no source; uv rejects the file")
        for item in source:
            pyproject_source_shape(name, item)
        return
    if not isinstance(source, dict):
        raise Unknown(f"{where} = {source!r} is not a source table; uv rejects the file")
    kinds = [k for k in _PYPROJECT_SOURCE_KINDS if k in source] + (
        ["workspace"] if "workspace" in source else []
    )
    if len(kinds) != 1:
        raise Unknown(f"{where} = {source!r} names {len(kinds)} source kinds, not one")
    if kinds[0] == "workspace":
        toml_flag(source, "workspace", where)
    else:
        toml_string(source[kinds[0]], f"{where} {kinds[0]}")
    toml_flag(source, "editable", where)
    toml_flag(source, "package", where)
    if "marker" in source:
        # `marker = ""` and `marker = "bad"` on any entry, used or not, are "Expected
        # marker value", exit 2 (round 38); typing it as a string had let both through.
        parsed_marker(source["marker"], f"{where} marker")


def managed_sources(uv_tool: dict | None) -> dict:
    """`[tool.uv.sources]`, once the project is known to be uv-managed: `managed = false`
    makes `uv lock` and `uv lock --check` exit 2 ("The project is marked as
    unmanaged"), so its lock is uncheckable and UNKNOWN (measured uv 0.8.17, round 26)."""
    if uv_tool is None:
        return {}
    if toml_flag(uv_tool, "managed", "[tool.uv]") is False:
        raise Unknown("[tool.uv] managed = false: the project is unmanaged, uv refuses to lock it")
    sources = uv_tool.get("sources")
    if sources is None:
        return {}
    if not isinstance(sources, dict):
        raise Unknown(f"[tool.uv] sources = {sources!r} is not a table; uv rejects the file")
    seen: dict[str, str] = {}
    for name, source in sources.items():  # every entry, declared or not (round 37)
        normalized = norm_name(name)
        if normalized in seen:
            # `foo_bar = ...` beside `foo-bar = ...` is "duplicate sources for package",
            # "Failed to parse: `pyproject.toml`", exit 2 (round 39); the normalized
            # lookup downstream had kept one of them quietly.
            raise Unknown(
                f"[tool.uv.sources] {name} and {seen[normalized]} are both {normalized!r}; "
                "uv rejects the file"
            )
        seen[normalized] = name
        pyproject_source_shape(normalized, source)
    return sources
