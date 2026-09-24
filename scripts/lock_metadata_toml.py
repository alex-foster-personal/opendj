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

from scripts.lock_marker_parser import Unknown, _MarkerParser

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


def dependency_records(entry: dict, name: str) -> None:
    """A package's `dependencies` uv can read: absent, or a list of tables each with a
    string `name`, a string `marker` if present and a list of strings `extra` if
    present. `dependencies = {}`, `[1]`, `[{}]`, `[{ name = 1 }]`, `extra = 1`,
    `marker = 1` and `marker = "bad"` are each "Failed to parse `uv.lock`", exit 2,
    while an unknown key beside a valid record and an extra that nothing provides are
    read (measured uv 0.8.17, Codex P2 on #3763, round 37). The marker is parsed, not
    compared: only the root's requirements are compared by meaning."""
    if "dependencies" not in entry:
        return
    where = f"uv.lock [[package]] {name!r} dependencies"
    for record in toml_list(entry["dependencies"], where):
        if not isinstance(record, dict) or "name" not in record:
            raise Unknown(f"{where} record {record!r} is not a table with a name")
        toml_string(record["name"], f"{where} name")
        if "marker" in record:
            marker = toml_string(record["marker"], f"{where} {record['name']!r} marker")
            try:
                _MarkerParser(marker).parse()  # `marker = "bad"` is refused too
            except Unknown as exc:
                raise Unknown(f"{where} {record['name']!r} marker: {exc}") from exc
        if "extra" in record:
            toml_strings(record["extra"], f"{where} {record['name']!r} extra")


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
        toml_string(source["marker"], f"{where} marker")


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
    for name, source in sources.items():  # every entry, declared or not (round 37)
        pyproject_source_shape(norm_name(name), source)
    return sources
