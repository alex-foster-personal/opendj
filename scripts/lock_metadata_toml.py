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

from scripts.lock_marker_parser import Unknown

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
