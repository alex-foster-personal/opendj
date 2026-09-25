"""Fail closed on a stale uv.lock without resolving, fetching, or running package code.

`uv lock --check` is the full check, and it stays on a GitHub-hosted runner
because resolving a dependency PR can build sdists (docs/security/ci-supply-chain.md).
When the hosted job is skipped, its result is unavailable evidence rather
than a lockfile verdict (Codex P1 on PR #3763). This is the safe path that stays
fail-closed meanwhile, and it runs anywhere: uv.lock records the root package's
`requires-dist`, verbatim from pyproject.toml at the last `uv lock`, so a
pyproject.toml edit that was not followed by `uv lock` shows up as a
requirement in one set and not the other. Stdlib only (tomllib), no network,
no environment, nothing imported from the tree beyond its stdlib-only companions
scripts/lock_marker_semantics.py (markers by meaning) and
scripts/lock_specifier_semantics.py (specifiers as `uv lock --check` compares them).

Requirements (mini-PRD)
- [if] every requirement in pyproject.toml (dependencies + optional-dependencies)
  has a matching requires-dist entry in uv.lock's root package and vice versa
  [then] exit 0 and print the count, [else stop] ✔︎ ✅ 🎯
- [if] a requirement differs (added, removed, specifier, extras, marker or
  `[tool.uv.sources]` path source changed; a path source's specifier is ignored,
  as uv ignores it, and a version respelled `1.26` / `1.26.0` is not a change, as
  `uv lock --check` accepts it), or an extra group is declared on one side only (uv records
  every group in `provides-extras`, an empty one included)
  [then] exit 1 naming each side's odd entries, [else stop] ✔︎ ✅ 🎯
- [if] a `[dependency-groups]` group (PEP 735, compared as uv records `requires-dev`)
  is added, removed or has different entries [then] exit 1 naming the group, [else stop] ✔︎ ✅ 🎯
- [if] requires-python differs, including one side missing it [then] exit 1 naming both;
  omitted in pyproject.toml with the lock recording `>=X.Y`, the default uv takes from
  the interpreter it finds (not observed here), [then] exit 2 UNKNOWN, [else stop] ✔︎ ✅ 🎯
- [if] [project].version differs from the root package version uv.lock recorded
  (a dynamic version is recorded as none) [then] exit 1 naming both, [else stop] ✔︎ ✅ 🎯
- [if] the root's recorded source (`editable = "."` for a package, `virtual = "."`
  otherwise) disagrees with `[build-system]` / `[tool.uv] package` [then] exit 1
  naming both, [else stop] ✔︎ ✅ 🎯
- [if] the lock has no root package entry (a missing requires-dist is the empty
  record uv writes for a dependency-free project, compared as such), a file is unreadable,
  a requirement cannot be parsed (a URL requirement, for instance), a dependency
  group includes a missing group or itself through any chain, or the project is
  one uv rejects (no version and not dynamic, both, a number for a version, a
  non-boolean flag, a string or list field of another TOML type, `managed = false`) [then] exit 2
  UNKNOWN naming the cause, never a verdict, [else stop] ✔︎ ✅ 🎯

What this cannot see: a lock whose pinned VERSIONS are stale against the index
with pyproject.toml unchanged. That is `uv lock --check`'s half (resolution),
and the hosted job keeps it; the stale-lock incident this guards against
(PR #2740: pyproject.toml bumped, uv.lock not regenerated) is the metadata
half, which this catches.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tomllib
from collections import Counter
from functools import partial
from pathlib import Path

from scripts.lock_environments import environments_delta
from scripts.lock_marker_parser import tokenize_marker
from scripts.lock_marker_semantics import (
    Unknown,
    canonical_version,
)
from scripts.lock_metadata_toml import (
    lock_schema,
    managed_sources,
    norm_name,
    package_records,
    recorded_marker,
    toml_flag,
    toml_list,
    toml_optional_string,
    toml_string,
    toml_strings,
    uv_table,
)
from scripts.lock_requirement_keys import Key, fmt_key, pair_by_meaning, same_requirement
from scripts.lock_settings import (
    refuse_dynamic_dependencies,
    refuse_unscoped_sources,
    refuse_uv_toml,
    settings_delta,
)
from scripts.lock_specifier_semantics import (
    norm_marker,
    norm_spec,
    python_specs_equivalent,
    trim_release,
)

EXIT_OK = 0
EXIT_STALE = 1
EXIT_UNKNOWN = 2

_REQ_RE = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)\s*"
    r"(?:\[(?P<extras>[^\]]*)\])?\s*"
    r"(?P<spec>[^;@]*?)\s*"
    r"(?:;\s*(?P<marker>.*?)\s*)?$"
)
# `dep;` with nothing after the `;` is "`project.dependencies[0]` must be pep508"
# (exit 2, measured uv 0.8.17, round 32); the optional group above matched it as
# the unmarked requirement the lock records. `text.rstrip()` keeps `dep; ` in it.
_EMPTY_MARKER_RE = re.compile(r";\s*$")


# (name, extras, specifier, marker clauses, source): source is "" for an index
# requirement, else the form uv records for a `[tool.uv.sources]` path source
# (measured with uv 0.8.17): `{ path = "./d/" }` -> `directory = "d"`,
# `{ path = "d", editable = true }` -> `editable = "d"`,
# `{ path = "d", package = false }` -> `virtual = "d"`. A git, url, index or
# workspace source is not modeled and is UNKNOWN, never a verdict.
def _pyproject_source(name: str, sources: dict, project_dir: Path) -> str:
    """The `[tool.uv.sources]` entry for a requirement, as uv records it in
    requires-dist; anything but a plain path source is UNKNOWN (Codex P2 on #3763,
    round 13: a path swapped without `uv lock` must not read as clean).

    Measured with uv 0.8.17 (round 14): an absolute path is recorded relative to
    the project (`/repo/dep` -> `directory = "dep"`), a path outside it as
    `../dep`; the target's OWN `[tool.uv] package = false` makes it `virtual`
    unless the source says `package = true` or `editable = true`, while a target
    without a build system, or without a [project] table, is still `directory`.
    So the target's pyproject.toml is read to predict the form, and a target that
    cannot be read is UNKNOWN."""
    source = sources.get(norm_name(name))
    if source is None:
        return ""
    if not isinstance(source, dict) or "path" not in source:
        raise Unknown(f"[tool.uv.sources] {name} is not a path source, not compared: {source!r}")
    if not set(source) <= {"path", "editable", "package"}:
        raise Unknown(f"[tool.uv.sources] {name} has keys not compared: {source!r}")
    raw = toml_string(source["path"], f"[tool.uv.sources] {name} path")
    path = os.path.relpath(raw, project_dir) if os.path.isabs(raw) else os.path.normpath(raw)
    target = project_dir / path
    if not target.is_dir():
        raise Unknown(f"[tool.uv.sources] {name} path {raw!r} is not a directory here")
    try:
        target_toml = tomllib.loads((target / "pyproject.toml").read_text(encoding="utf-8"))
    except FileNotFoundError:
        target_toml = {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise Unknown(
            f"[tool.uv.sources] {name}: cannot read the target's pyproject: {exc}"
        ) from exc
    target_uv = uv_table(target_toml, f"{path}/pyproject.toml")
    target_package = (
        toml_flag(target_uv, "package", f"{path}/pyproject.toml [tool.uv]")
        if target_uv is not None
        else None
    )
    # The source's own `package` wins over the target's, and `editable` wins over
    # both (measured, round 15: `{ package = true }` on a package=false target is
    # `directory`, `{ editable = true }` on it is `editable`).
    own_package = toml_flag(source, "package", f"[tool.uv.sources] {name}")
    virtual = (own_package if own_package is not None else target_package) is False
    if toml_flag(source, "editable", f"[tool.uv.sources] {name}"):
        return f"editable={path}"
    if virtual:
        return f"virtual={path}"
    return f"directory={path}"


def parse_requirement(
    text: str, extra: str | None, sources: dict | None = None, project_dir: Path | None = None
) -> Key:
    """One PEP 508 line from pyproject.toml, as the key uv.lock would record it."""
    if "@" in text.split(";", 1)[0]:
        # `name @ url`: the `@` sits before any marker. One inside a marker literal
        # (`os_name == 'a@b'`, which uv records verbatim) is not a URL.
        raise Unknown(f"URL requirement is not comparable by metadata: {text!r}")
    match = _REQ_RE.match(text)
    if match is None or _EMPTY_MARKER_RE.search(text):
        raise Unknown(f"unparseable requirement: {text!r}")
    # `dep[]` and `dep[ ]` are the unextra'd requirement (exit 0), but an EMPTY SLOT
    # (`dep[,]`, `dep[a,]`, `dep[,a]`, `dep[a,,b]`) is "must be pep508", exit 2
    # (measured uv 0.8.17, Codex P2 on #3763, round 33); filtering empties had read
    # every one of them as `dep`.
    slots = [e.strip() for e in (match.group("extras") or "").split(",")]
    if len(slots) > 1 and any(not slot for slot in slots):
        raise Unknown(f"unparseable requirement (empty extra): {text!r}")
    extras = tuple(sorted(norm_name(slot) for slot in slots if slot))
    marker = list(norm_marker(match.group("marker")))
    if extra is not None:
        if len(marker) == 1 and any(
            tok == ("word", "or") or tok[0] == "lp" for tok in tokenize_marker(marker[0])
        ):
            # `dep; a or b` under an extra: uv guards BOTH branches with the extra,
            # `(a and extra == 'x') or (b and extra == 'x')` (measured, round 16).
            # The whole marker is one clause here, and the semantic compare joins
            # clauses with `and`, so parenthesize it or the extra binds to `b` alone.
            marker = [f"( {marker[0]} )"]
        # uv records the extra in its normalized form (`foo_bar` -> `foo-bar`).
        marker.append(f"extra == '{norm_name(extra)}'")
    spec = match.group("spec").strip()
    if spec.startswith("(") and spec.endswith(")"):
        spec = spec[1:-1]  # PEP 508 allows `name (>=1.26)`; uv records `>=1.26`
    source = _pyproject_source(match.group("name"), sources or {}, project_dir or Path("."))
    # Validated BEFORE a path source discards it (round 36); one trailing comma is
    # read by uv, any other empty clause is not (round 38).
    normalized = norm_spec(spec, empty_clauses="trailing")
    if source:
        # The path decides the version: uv records a path-sourced requirement with
        # no specifier, and `uv lock --check` passes after the specifier is edited
        # (measured, Codex P2 on #3763, round 18), so there is nothing to compare.
        normalized = ""
    return (
        norm_name(match.group("name")),
        extras,
        normalized,
        tuple(sorted(marker)),
        source,
    )


def lock_requirement(entry: dict) -> Key:
    """One requires-dist entry from uv.lock's root package, as the same key."""
    name = entry.get("name")
    if not isinstance(name, str):
        raise Unknown(f"requires-dist entry without a name: {entry!r}")
    source_keys = {"directory", "editable", "virtual"} & set(entry)
    if len(source_keys) > 1:
        raise Unknown(f"requires-dist entry names more than one source: {entry!r}")
    unexpected = set(entry) - {"name", "specifier", "marker", "extras", *source_keys}
    if unexpected:
        # git, url, index, or a key this check does not know: not an index
        # requirement it can compare, so UNKNOWN rather than a verdict.
        raise Unknown(f"requires-dist entry is not compared ({sorted(unexpected)}): {entry!r}")
    source = "".join(
        f"{key}={os.path.normpath(toml_string(entry[key], f'requires-dist {key}'))}"
        for key in source_keys
    )
    # `extras = "socks"` is exit 2 ("expected a sequence"; uv 0.8.17, round 27).
    extras = tuple(
        sorted(norm_name(e) for e in toml_strings(entry.get("extras", []), "requires-dist extras"))
    )
    # A registry record's `specifier = 1` or empty clause (`>=1,`, `,>=1`; `""` is
    # read) is "Failed to parse `uv.lock`", exit 2; a path-sourced record's specifier
    # is not read at all (`>=1,,` and `= 1` pass: the path decides the version, as in
    # pyproject_requirement), though its marker is (round 38).
    marker = recorded_marker(entry, f"requires-dist {name!r}")
    specifier = ""
    if not source_keys:
        specifier = toml_string(entry.get("specifier", ""), f"requires-dist {name!r} specifier")
    return (
        norm_name(name),
        extras,
        norm_spec(specifier),
        norm_marker(marker),
        source,
    )


def pyproject_requirements(
    project: dict, sources: dict | None = None, project_dir: Path | None = None
) -> Counter[Key]:
    normed = {norm_name(str(k)): v for k, v in (sources or {}).items()}
    # A requirement listed twice (any spelling: uv parses first) is recorded ONCE
    # (measured uv 0.8.17, Codex P2 on #3763, round 23), so the count is one.
    # And once under EQUIVALENT markers (`python_version < '3.12'` beside
    # `python_full_version < '3.12'`), which uv canonicalizes first (round 26).
    keys: Counter[Key] = Counter()
    extras = project.get("optional-dependencies", {})
    if not isinstance(extras, dict):
        raise Unknown(
            f"[project] optional-dependencies = {extras!r} is not a table; uv rejects the file"
        )
    deps = toml_strings(project.get("dependencies", []), "[project] dependencies")
    listed: list[tuple[str, str | None]] = [(dep, None) for dep in deps]
    for extra, group in extras.items():
        where = f"[project.optional-dependencies] {extra}"
        listed += [(dep, extra) for dep in toml_strings(group, where)]
    for dep, extra in listed:
        key = parse_requirement(dep, extra, normed, project_dir)
        if not any(same_requirement(key, seen) for seen in keys):
            keys[key] = 1
    return keys


def lock_root(lock: dict, project_name: str) -> dict:
    """The root package entry uv.lock records for the project, or Unknown. Every
    package record is checked first: a `[[package]]` without `name` or `source` is
    "missing field", and `package = "bad"` is "expected a sequence", each `uv lock
    --check` exit 2 (measured uv 0.8.17, Codex P2 on #3763, round 33), while a record
    without `version` is read (a path source has none) but one present must be a string
    on EVERY record (`version = 0.0` on a registry package: "invalid type: floating
    point", round 35). The root filter had dropped the malformed record quietly."""
    packages = package_records(lock)
    roots = [p for p in packages if norm_name(p["name"]) == norm_name(project_name)]
    if len(roots) != 1:
        raise Unknown(
            f"uv.lock has {len(roots)} package entries named {project_name!r}; expected the root"
        )
    return roots[0]


def lock_requirements(root: dict) -> Counter[Key]:
    """uv writes no `requires-dist` (no `[package.metadata]` at all) for a project with
    no dependencies and no extras, and `uv lock --check` passes on that pair, so a
    missing table is the EMPTY record: clean under an empty pyproject.toml, stale under
    one that declares anything (measured uv 0.8.17, round 21). Never UNKNOWN."""
    metadata = root.get("metadata", {})
    if not isinstance(metadata, dict):
        raise Unknown(f"uv.lock root [package.metadata] is not a table: {metadata!r}")
    entries = toml_list(metadata.get("requires-dist", []), "uv.lock requires-dist")
    return Counter(lock_requirement(e) for e in entries)


def _group_keys(
    groups: dict[str, list],
    name: str,
    sources: dict,
    project_dir: Path | None,
    stack: tuple[str, ...],
) -> set[Key]:
    """One PEP 735 group as the SET uv records (an `include-group` expanded inline, a
    duplicate listed once; a cycle or a missing group is a uv error: UNKNOWN)."""
    if name in stack or not isinstance(groups.get(name), list):
        cause = "cycle" if name in stack else "missing or not a list"
        raise Unknown(f"[dependency-groups] {' -> '.join((*stack, name))}: {cause}")
    keys: set[Key] = set()
    for entry in groups[name]:
        if isinstance(entry, str):
            found = {parse_requirement(entry, None, sources, project_dir)}
        elif isinstance(entry, dict) and set(entry) == {"include-group"}:
            included = norm_name(
                toml_string(entry["include-group"], f"[dependency-groups] {name} include-group")
            )
            found = _group_keys(groups, included, sources, project_dir, (*stack, name))
        else:
            raise Unknown(f"[dependency-groups] {name} entry is not compared: {entry!r}")
        keys |= {k for k in found if not any(same_requirement(k, seen) for seen in keys)}
    return keys


def _dependency_groups_delta(
    pyproject: dict, root: dict, sources: dict, project_dir: Path | None, uv_tool: dict | None
) -> list[str]:
    """Stale lines for `[dependency-groups]` against the root's `requires-dev`: uv
    records every group (an empty one as `[]`) and `--check` fails on any change.
    The legacy `[tool.uv] dev-dependencies` is the `dev` group too: uv records its
    entries beside `[dependency-groups] dev`, a duplicate once, and an empty legacy
    list as `dev = []` (measured uv 0.8.17, Codex P2 on #3763, round 44)."""
    declared_raw = pyproject.get("dependency-groups", {})
    if not isinstance(declared_raw, dict):
        raise Unknown("[dependency-groups] is not a table; uv rejects the file")
    groups = {norm_name(str(k)): v for k, v in declared_raw.items()}
    if len(groups) != len(declared_raw):
        raise Unknown("[dependency-groups] names two groups the same after normalization")
    if uv_tool is not None and "dev-dependencies" in uv_tool:
        legacy = toml_strings(uv_tool["dev-dependencies"], "[tool.uv] dev-dependencies")
        dev = groups.get("dev", [])
        if not isinstance(dev, list):
            raise Unknown(f"[dependency-groups] dev = {dev!r} is not a list; uv rejects the file")
        groups["dev"] = [*dev, *legacy]
    recorded_raw = root.get("metadata", {}).get("requires-dev", {})
    if not isinstance(recorded_raw, dict):
        raise Unknown(f"uv.lock requires-dev is not a table: {recorded_raw!r}")
    declared = {name: _group_keys(groups, name, sources, project_dir, ()) for name in groups}
    recorded = {
        norm_name(str(name)): {
            lock_requirement(e) for e in toml_list(entries, f"uv.lock requires-dev {name}")
        }
        for name, entries in recorded_raw.items()
    }
    lines: list[str] = []
    for name in sorted(set(declared) | set(recorded)):
        if name not in recorded:
            lines.append(f"  dependency group {name!r}: in pyproject.toml, not in uv.lock")
            continue
        if name not in declared:
            lines.append(f"  dependency group {name!r}: in uv.lock, not in pyproject.toml")
            continue
        want, have = Counter(declared[name]), Counter(recorded[name])
        missing, extra_in_lock = pair_by_meaning(want - have, have - want)
        lines.extend(
            f"  dependency group {name!r}: in pyproject.toml, not in uv.lock: {fmt_key(key)}"
            for key in sorted(missing)
        )
        lines.extend(
            f"  dependency group {name!r}: in uv.lock, not in pyproject.toml: {fmt_key(key)}"
            for key in sorted(extra_in_lock)
        )
    return lines


def _extras_delta(project: dict, root: dict) -> str | None:
    """The stale line for extra GROUPS, or None. Every declared extra is recorded in
    provides-extras, an EMPTY group included: `empty = []` adds no requires-dist entry,
    and `uv lock --check` still rejects the lock that lacks it (measured, Codex P2 on
    #3763, round 16). A project without extras gets no key at all, so a missing key is
    the empty list; names compare normalized (`Foo_Bar` is recorded as `foo-bar`)."""
    declared = sorted({norm_name(str(k)) for k in project.get("optional-dependencies", {})})
    provided = toml_strings(
        root.get("metadata", {}).get("provides-extras", []), "uv.lock provides-extras"
    )
    provided = sorted({norm_name(name) for name in provided})
    if declared == provided:
        return None
    return f"  provides-extras: pyproject.toml {declared}, uv.lock {provided}"


def _root_source_delta(pyproject: dict, root: dict) -> str | None:
    """The stale line for the ROOT package's source, or None. uv records the project
    as `editable = "."` when it is a package (a `[build-system]`, or `[tool.uv]
    package = true` without one) and as `virtual = "."` otherwise (`package = false`,
    or no build-system), and `uv lock --check` rejects the lock after either toggle
    (measured, uv 0.8.17, Codex P2 on #3763, round 17). Any other recorded root
    (another path, a registry, two keys) is not modeled: UNKNOWN, never a verdict."""
    uv_tool = uv_table(pyproject)
    package = toml_flag(uv_tool, "package", "[tool.uv]") if isinstance(uv_tool, dict) else None
    if package is not None:
        reason = f"[tool.uv] package = {str(package).lower()}"
    else:
        package = "build-system" in pyproject
        reason = "[build-system] " + ("present" if package else "absent")
    expected = "editable" if package else "virtual"
    source = root.get("source")
    if (
        not isinstance(source, dict)
        or len(source) != 1
        or next(iter(source)) not in ("editable", "virtual")
        or next(iter(source.values())) != "."
    ):
        raise Unknown(f"uv.lock root source is not compared: {source!r}")
    recorded = next(iter(source))
    if recorded == expected:
        return None
    return (
        f"  root source: pyproject.toml says {expected} = '.' ({reason}), "
        f"uv.lock recorded {recorded} = '.'"
    )


def _version_delta(project: dict, root: dict) -> str | None:
    """The stale line for the root package VERSION, or None. A version-only bump is
    the most routine pyproject.toml edit and the one requires-dist cannot see; uv.lock
    records the root's version too. Measured (uv 0.8.17, Codex P2 on #3763, round 19):
    a version listed in `project.dynamic` is recorded as NO version (a bump of the
    dynamic value passes `uv lock --check`; the lock left from a static version does
    not), a project with neither a version nor the dynamic entry is a file uv refuses
    to parse, one with both is refused by the build backend, and a `dynamic` that is
    not a list of strings, or a version that is a TOML number (`version = 1.0`, refused
    as "invalid type: floating point"), on either side, is a parse error: UNKNOWN
    (rounds 20, 29). Compared as PEP 440 versions: `uv lock --check` accepts `1.0`
    respelled `1.0.0` or `01.0` and rejects `1.0.0rc1` (round 24)."""
    dynamic = toml_strings(project.get("dynamic", []), "[project] dynamic")
    want = project.get("version")
    if want is not None and not isinstance(want, str):
        raise Unknown(f"[project] version = {want!r} is not a string; uv rejects the file")
    have = root.get("version")
    if have is not None and not isinstance(have, str):
        # "invalid type: floating point", exit 2 (uv 0.8.17, round 29); str() read it.
        raise Unknown(f"uv.lock root version = {have!r} is not a string; uv rejects the file")
    is_dynamic = "version" in dynamic
    if want is not None and is_dynamic:
        raise Unknown(
            "[project] version is both static and listed in dynamic; the build rejects it"
        )
    if want is None and not is_dynamic:
        raise Unknown(
            "[project] has no version and does not list it in dynamic; uv rejects the file"
        )
    if is_dynamic:
        if have is None:
            return None
        return f"  [project] version: pyproject.toml dynamic, uv.lock root {have!r}"
    if have is None:
        return (
            f"  [project] version: pyproject.toml {want!r}, uv.lock root none (locked as dynamic)"
        )
    if trim_release(canonical_version(str(want))) == trim_release(canonical_version(str(have))):
        return None
    return f"  [project] version: pyproject.toml {want!r}, uv.lock root {have!r}"


def check(pyproject_path: Path, lock_path: Path) -> tuple[int, str]:
    try:
        pyproject = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
        lock = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return EXIT_UNKNOWN, f"UNKNOWN: cannot read {pyproject_path} / {lock_path}: {exc}"
    project = pyproject.get("project")
    if not isinstance(project, dict) or "name" not in project:
        return EXIT_UNKNOWN, "UNKNOWN: pyproject.toml has no [project] name"
    try:
        lock_schema(lock)
        root = lock_root(lock, project["name"])
        uv_tool = uv_table(pyproject)
        sources = managed_sources(uv_tool)
        refuse_unscoped_sources(pyproject, sources)  # round 53
        project_dir = pyproject_path.resolve().parent
        refuse_uv_toml(project_dir)  # round 47
        refuse_dynamic_dependencies(project)  # round 48
        expected = pyproject_requirements(project, sources, project_dir)
        recorded = lock_requirements(root)
        missing, extra_in_lock = pair_by_meaning(expected - recorded, recorded - expected)
        extras_line = _extras_delta(project, root)
        root_line = _root_source_delta(pyproject, root)
        group_lines = _dependency_groups_delta(pyproject, root, sources, project_dir, uv_tool)
        group_lines.extend(environments_delta(uv_tool, lock))  # round 42
        spelled = partial(parse_requirement, extra=None, sources=sources, project_dir=project_dir)
        group_lines.extend(  # round 43
            settings_delta(uv_tool, lock, norm_name(project["name"]), spelled, lock_requirement)
        )
        # `requires-python = 0` on either side is "invalid type: integer `0`, expected
        # a string", `uv lock --check` exit 2 (round 35); `0 or ""` had compared as "".
        want_python = toml_optional_string(
            project.get("requires-python"), "[project] requires-python"
        )
        have_python = toml_optional_string(lock.get("requires-python"), "uv.lock requires-python")
    except Unknown as exc:
        return EXIT_UNKNOWN, f"UNKNOWN: {exc}"
    lines: list[str] = [
        f"  in pyproject.toml, not in uv.lock: {fmt_key(key)}" for key in sorted(missing)
    ]
    lines.extend(
        f"  in uv.lock, not in pyproject.toml: {fmt_key(key)}" for key in sorted(extra_in_lock)
    )
    lines.extend(group_lines)
    lines.extend(line for line in (extras_line, root_line) if line is not None)
    # Compared unconditionally: a requires-python REMOVED from pyproject.toml
    # without `uv lock` leaves the lock's old constraint behind, which is the
    # same staleness as a changed one (Codex P2 on PR #3763).
    unobserved = None
    if want_python is None and re.fullmatch(r">=\d+\.\d+", str(have_python).replace(" ", "")):
        # uv fills an omitted requires-python with `>=X.Y` of the interpreter it
        # discovers, and `uv lock --check` fails under another one (measured uv
        # 0.8.17, round 23): a verdict needs the interpreter, which is not seen here.
        unobserved = (
            f"UNKNOWN: pyproject.toml omits requires-python and uv.lock records "
            f"{have_python!r}, uv's default from an interpreter this check does not "
            "observe: declare requires-python"
        )
    else:
        try:
            same = python_specs_equivalent(str(want_python or ""), str(have_python or ""))
        except Unknown as exc:
            return EXIT_UNKNOWN, f"UNKNOWN: {exc}"
        if not same:
            lines.append(
                f"  requires-python: pyproject.toml {want_python!r}, uv.lock {have_python!r}"
            )
    try:
        version_line = _version_delta(project, root)
    except Unknown as exc:
        return EXIT_UNKNOWN, f"UNKNOWN: {exc}"
    if version_line is not None:
        lines.append(version_line)
    if lines:
        return EXIT_STALE, "uv.lock is stale against pyproject.toml (run `uv lock`):\n" + "\n".join(
            lines
        )
    if unobserved is not None:
        return EXIT_UNKNOWN, unobserved
    return (
        EXIT_OK,
        f"uv.lock metadata matches pyproject.toml: {sum(expected.values())} requirements, "
        f"requires-python {want_python!r}",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pyproject", type=Path, default=Path("pyproject.toml"))
    parser.add_argument("--lock", type=Path, default=Path("uv.lock"))
    args = parser.parse_args(argv)
    code, message = check(args.pyproject, args.lock)
    print(message, file=sys.stderr if code else sys.stdout)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
