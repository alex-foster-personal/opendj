"""`[tool.uv] environments` / `required-environments` against uv.lock's
`supported-markers` / `required-markers` (stdlib only, like the checker).

uv records each environment marker, in order, as it spells markers: `platform_system
== 'Linux'` becomes `sys_platform == 'linux'`, `python_version >= '3.12'` becomes
`python_full_version >= '3.12'`, clauses are re-ordered, and a single string is one
environment. `uv lock --check` is exit 1 (out of date) when the lists differ in
length, order or meaning, or when one side is absent and the other non-empty (`[]`
equals absent); it is exit 2 on `environments = [1]`, `["bad"]`, `[""]`, `""`, `1`
and on `supported-markers = [true]` or `["bad"]` (measured uv 0.8.17, Codex P2 on
#3763, round 42). The checker had compared neither pair and read a re-targeted
project as clean.
"""

from __future__ import annotations

from scripts.lock_marker_semantics import markers_equivalent
from scripts.lock_metadata_toml import parsed_marker, toml_strings
from scripts.lock_specifier_semantics import norm_marker

PAIRS = (("environments", "supported-markers"), ("required-environments", "required-markers"))


def _markers(value: object, where: str, *, one_string_ok: bool = False) -> list[str]:
    """The markers at `where`, parsed: absent is none, and `environments` may be a single
    string (uv reads it as one environment; measured, round 42)."""
    if value is None:
        return []
    if one_string_ok and isinstance(value, str):
        value = [value]
    return [parsed_marker(marker, where) for marker in toml_strings(value, where)]


def environments_delta(uv_tool: dict | None, lock: dict) -> list[str]:
    """Stale lines for each pair, compared element-wise by meaning; UNKNOWN on a shape
    uv refuses."""
    lines: list[str] = []
    for spelled_key, recorded_key in PAIRS:
        spelled = _markers(
            None if uv_tool is None else uv_tool.get(spelled_key),
            f"[tool.uv] {spelled_key}",
            one_string_ok=True,
        )
        recorded = _markers(lock.get(recorded_key), f"uv.lock {recorded_key}")
        same = len(spelled) == len(recorded) and all(
            markers_equivalent(norm_marker(a), norm_marker(b))
            for a, b in zip(spelled, recorded, strict=True)
        )
        if not same:
            lines.append(
                f"  [tool.uv] {spelled_key}: pyproject.toml {spelled!r}, "
                f"uv.lock {recorded_key} {recorded!r}"
            )
    return lines
