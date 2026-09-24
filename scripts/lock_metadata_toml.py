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
from scripts.lock_marker_semantics import spelled_markers_equivalent
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
    if "revision" in lock:
        # `revision = "3"`, `true`, `-1`, `1.5` and 2**32 are "invalid type ... expected
        # u32", exit 2; absent, 0, 3 and 2**32 - 1 are read (round 41).
        revision = lock["revision"]
        if isinstance(revision, bool) or not isinstance(revision, int) or not 0 <= revision < 2**32:
            raise Unknown(f"uv.lock revision = {revision!r} is not a u32; uv rejects the file")


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
    by_name: dict[str, list[dict]] = {}
    for entry in packages:
        name = toml_string(entry["name"], "uv.lock [[package]] name")
        source_table(entry["source"], name)
        if "version" in entry:
            toml_string(entry["version"], f"uv.lock [[package]] {name!r} version")
        # Two records of one identity (name, version, source) are refused whether or
        # not anything depends on them, while a second version or source of the same
        # name is read (round 41); a name set had collapsed the duplicate.
        twins = by_name.setdefault(norm_name(name), [])
        if any(
            p.get("version") == entry.get("version") and p["source"] == entry["source"]
            for p in twins
        ):
            raise Unknown(
                f"uv.lock [[package]] {name!r} {entry.get('version')!r} {entry['source']!r} "
                "is recorded twice; uv rejects the file"
            )
        twins.append(entry)
    for entry in packages:
        name = toml_string(entry["name"], "uv.lock [[package]] name")
        dependency_records(entry, name, by_name)
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
        if size > _U64_MAX:
            raise Unknown(f"{where} size = {size} exceeds 64 bits; uv rejects the file")
        if size > _I64_MAX:
            # uv 0.8.17 reads a size up to 2**64-1; uv 0.7.22 stops at 2**63-1 ("number
            # too large to fit in target type", Codex, round 55). Not certified either way.
            raise Unknown(
                f"{where} size = {size} exceeds a signed 64-bit integer; uv 0.7.22 rejects it"
            )
    if "upload-time" in record:
        upload_time(toml_string(record["upload-time"], f"{where} upload-time"), where)


_I64_MAX = 2**63 - 1
_U64_MAX = 2**64 - 1
# The timestamp grammar uv reads for `upload-time` (jiff, ISO 8601 / RFC 3339 / RFC 9557;
# measured uv 0.8.17, Codex P2 on #3763, round 55): a 4-digit or signed 6-digit year,
# month and day with or without dashes, `T`/`t`/space, an hour with optional minute,
# second and 1-9 fraction digits (`.` or `,`), colons optional but consistent, then `Z`
# or a `+HH[[:]MM[[:]SS]]` offset up to 25:59:59, then an optional `[...]` annotation.
# `datetime.fromisoformat` had accepted a week date (`2024-W01-1T...`), which uv rejects,
# and rejected the basic forms and `[UTC]`, which uv reads.
_UPLOAD_TIME_RE = re.compile(
    r"^(?P<year>\d{4}|[+-]\d{6})(?P<ds>-?)(?P<month>\d{2})(?P=ds)(?P<day>\d{2})"
    r"[Tt ](?P<hour>\d{2})(?:(?P<ts>:?)(?P<minute>\d{2})"
    r"(?:(?P=ts)(?P<second>\d{2})(?:[.,]\d{1,9})?)?)?"
    r"(?P<zone>[Zz]|(?P<sign>[+-])(?P<oh>\d{2})(?::?(?P<om>\d{2})(?::?(?P<os>\d{2}))?)?)"
    r"(?:\[[^\]]*\])?$"
)


def upload_time(stamp: str, where: str) -> None:
    """`upload-time` as uv reads it (grammar at `_UPLOAD_TIME_RE`), on a real calendar
    date, hour 0-23, minute 0-59, second 0-60 (a leap second is read), an offset of at
    most 25:59:59, and a year 0000-9998: uv reads `0000-01-01T00:00:00+01:00` and
    `9999-12-30T00:00:00Z` but not `9999-12-30T23:59:59Z`, the edge of its range, so
    year 9999 and beyond, and a negative year, are not modeled here (measured round 55).
    Everything else is "Failed to parse `uv.lock`", exit 2."""
    match = _UPLOAD_TIME_RE.match(stamp)
    if match is None:
        raise Unknown(f"{where} upload-time = {stamp!r} is not a timestamp uv reads; uv rejects it")
    year, month, day = (int(match.group(k)) for k in ("year", "month", "day"))
    hour, minute, second = (int(match.group(k) or 0) for k in ("hour", "minute", "second"))
    if not (1 <= month <= 12 and 1 <= day <= _days_in_month(year, month)):
        raise Unknown(f"{where} upload-time = {stamp!r} is not a calendar date; uv rejects it")
    if hour > 23 or minute > 59 or second > 60:
        raise Unknown(f"{where} upload-time = {stamp!r} is not a time of day; uv rejects it")
    if match.group("sign"):
        offset = (int(match.group("oh")), int(match.group("om") or 0), int(match.group("os") or 0))
        if offset[1] > 59 or offset[2] > 59 or offset > (25, 59, 59):
            raise Unknown(f"{where} upload-time = {stamp!r} offset is past 25:59:59; uv rejects it")
    if not 0 <= year <= 9998:
        raise Unknown(
            f"{where} upload-time = {stamp!r}: year {year} is at or past the edge of uv's"
            " timestamp range, not modeled by this check"
        )


def _days_in_month(year: int, month: int) -> int:
    if month == 2:
        return 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28
    return 30 if month in (4, 6, 9, 11) else 31


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


def dependency_records(entry: dict, name: str, packages: dict[str, list[dict]]) -> None:
    """A package's `dependencies` uv can read: absent, or a list of tables each with a
    valid `name` that (normalized, with any `version` and `source` the record carries)
    matches exactly ONE package of the lock, a string `marker` if present and a list
    of valid names `extra` if present. `dependencies = {}`, `[1]`, `[{}]`,
    `[{ name = 1 }]`, `extra = 1`, `marker = 1` and `marker = "bad"` are each "Failed
    to parse `uv.lock`", exit 2 (measured uv 0.8.17, Codex P2 on #3763, round 37), as
    are `name = "bad space"`, `name = "-lead"`, `extra = ["bad space"]` ("Not a valid
    package or extra name"), a valid name no package carries, a bare name two
    packages carry, `version = 1`, `source = "bad"`, and a version or source no
    package of that name carries (rounds 40-41), while an unknown key beside a valid
    record, `name = "SIX"` for the package `six`, a record whose `version` and
    `source` pick one of two, and an extra that nothing provides or is not normalized
    are read. The marker is parsed, not compared: only the root's requirements are
    compared by meaning."""
    where = f"uv.lock [[package]] {name!r}"
    if "dependencies" in entry:
        _dependency_list(entry["dependencies"], f"{where} dependencies", packages)
    # `[package.optional-dependencies]` and `[package.dev-dependencies]` hold the same
    # records per extra or group, on ANY package: `format = {}`, `[1]`, `[{}]`,
    # `[{ name = 1 }]`, a name no package carries, `marker = "bad"`, a group named
    # `"bad space"` and a bare `= "bad"` are each "Failed to parse `uv.lock`", exit 2,
    # while `format = []` and a record naming a package are read (round 44).
    for key in ("optional-dependencies", "dev-dependencies"):
        if key not in entry:
            continue
        table = entry[key]
        if not isinstance(table, dict):
            raise Unknown(f"{where} {key} = {table!r} is not a table; uv rejects the file")
        for group, records in table.items():
            _dependency_list(records, f"{where} {key} {norm_name(group)}", packages)


def _dependency_list(records: object, where: str, packages: dict[str, list[dict]]) -> None:
    for record in toml_list(records, where):
        if not isinstance(record, dict) or "name" not in record:
            raise Unknown(f"{where} record {record!r} is not a table with a name")
        dep = norm_name(toml_string(record["name"], f"{where} name"))
        candidates = packages.get(dep, [])
        if "version" in record:
            version = toml_string(record["version"], f"{where} {dep!r} version")
            candidates = [p for p in candidates if p.get("version") == version]
        if "source" in record:
            source_table(record["source"], dep)
            candidates = [p for p in candidates if p["source"] == record["source"]]
        if not candidates:
            raise Unknown(f"{where} names {dep!r}, which no [[package]] of the lock carries")
        if len(candidates) > 1:
            raise Unknown(
                f"{where} names {dep!r}, which {len(candidates)} [[package]] records match; "
                "uv needs the version and source that pick one"
            )
        if "marker" in record:
            parsed_marker(record["marker"], f"{where} {dep!r} marker")
        for extra in toml_strings(record.get("extra", []), f"{where} {dep!r} extra"):
            norm_name(extra)


# The keys uv 0.8.17 reads on a `[tool.uv.sources]` entry, per source kind (Codex P2 on
# #3763, round 51; every rejected pair below is "Failed to parse: `pyproject.toml`",
# exit 2, and every allowed one is read, measured on an entry no requirement uses):
# an unknown key is "unknown field"; `git`/`url` reject `editable` and `package`;
# `path`/`url`/`index` reject `rev`, `tag` and `branch`; `index` rejects `editable` and
# `package`; `workspace` rejects `package`; `git` takes at most one of `rev`/`tag`/
# `branch`; `editable = true` with `package = false` is a conflict; `extra` and `group`
# never both. `subdirectory` is read on every kind, `editable` on `workspace` too.
_PYPROJECT_SOURCE_KEYS: dict[str, frozenset[str]] = {
    "git": frozenset({"git", "subdirectory", "rev", "tag", "branch"}),
    "url": frozenset({"url", "subdirectory"}),
    "path": frozenset({"path", "subdirectory", "editable", "package"}),
    "index": frozenset({"index", "subdirectory"}),
    "workspace": frozenset({"workspace", "subdirectory", "editable"}),
}
_PYPROJECT_SOURCE_COMMON = frozenset({"marker", "extra", "group"})
_PYPROJECT_SOURCE_FLAGS = ("editable", "package", "workspace")


def pyproject_source_shape(name: str, source: object) -> None:
    """One `[tool.uv.sources]` entry uv can read, declared or not: a table with exactly
    one of git/url/path/index holding a string, or `workspace` holding a bool, carrying
    only the keys `_PYPROJECT_SOURCE_KEYS` allows that kind plus `marker`/`extra`/
    `group`, each typed (strings; `editable`/`package`/`workspace` bools; `marker`
    parsed); or a non-empty list of such tables. `{ path = 1 }`, `"bad"`, `{}`,
    `{ bogus = "x" }`, `{ git = 1 }`, `{ path = ..., git = ... }`, `[]`, `[{ path = 1 }]`,
    `editable = 1` and `marker = 1` are each "Failed to parse: `pyproject.toml`", exit 2,
    while `{ workspace = false }`, an unresolvable path and `[{ path = "dep" }]` are read
    (measured uv 0.8.17, rounds 37, 38 and 51); an entry with no requirement had
    passed unread, then an extra key beside a valid kind had."""
    where = f"[tool.uv.sources] {name}"
    if isinstance(source, list):
        if not source:
            raise Unknown(f"{where} = [] names no source; uv rejects the file")
        for item in source:
            pyproject_source_shape(name, item)
        if len(source) > 1:
            _source_list_markers(where, source)
        return
    if not isinstance(source, dict):
        raise Unknown(f"{where} = {source!r} is not a source table; uv rejects the file")
    kind = _source_kind(where, source)
    for key in sorted(set(source) - _PYPROJECT_SOURCE_KEYS[kind] - _PYPROJECT_SOURCE_COMMON):
        raise Unknown(f"{where} = {source!r}: cannot specify both {kind!r} and {key!r}")
    for key, value in source.items():
        if key in _PYPROJECT_SOURCE_FLAGS:
            toml_flag(source, key, where)
        elif key != "marker":
            toml_string(value, f"{where} {key}")
    _source_entry_conflicts(where, source)


_SCOPED_MARKER_RE = re.compile(r"\b(extra|dependency_groups)\b")


def _source_list_markers(where: str, entries: list) -> None:
    """A source LIST's cross-entry rule (measured uv 0.8.17, Codex P2 on #3763, round
    56): among the entries sharing one scope (the same normalized `extra`, the same
    `group`, or neither), two or more need a `marker` each ("When multiple sources are
    provided, each source must include a platform marker") and those markers must be
    pairwise disjoint ("Source markers must be disjoint, but the following markers
    overlap"), each "Failed to parse: `pyproject.toml`", exit 2. `[{ path = "x" },
    { path = "y" }]`, one marked and one not, the same marker twice or respelled,
    `sys_platform == 'linux'` beside `python_version < '3.12'` or beside a subset of
    itself, `platform_system == 'Linux'` beside `sys_platform == 'linux'`, and two
    markers empty under requires-python (`python_version < '3.11'` and `< '3.10'`) are
    refused; `sys_platform == 'linux'` beside `'darwin'` or `!= 'linux'`, a python
    split (`python_full_version >= '3.12'` beside `python_version < '3.12'`),
    `os_name == 'nt'` beside `sys_platform == 'linux'`, and entries in different scopes
    (`extra = "dev"` beside a plain one, or beside `group = "g"`, or with the same
    marker) are read. `extra == 'a'` and `extra == 'b'` OVERLAP for uv (extras are a
    set), which the marker evaluator does not model, so such a marker is UNKNOWN.
    Each entry had been checked on its own, so the list-level refusals had passed."""
    scopes: dict[tuple[str, str], list[str | None]] = {}
    for entry in entries:
        extra = norm_name(entry["extra"]) if "extra" in entry else ""
        group = norm_name(entry["group"]) if "group" in entry else ""
        scopes.setdefault((extra, group), []).append(entry.get("marker"))
    for markers in scopes.values():
        if len(markers) < 2:
            continue
        if any(marker is None for marker in markers):
            raise Unknown(
                f"{where}: when multiple sources are provided, each source must include a"
                " platform marker; uv rejects the file"
            )
        spelled = [marker for marker in markers if marker is not None]
        for i, one in enumerate(spelled):
            for other in spelled[i + 1 :]:
                if _SCOPED_MARKER_RE.search(one) or _SCOPED_MARKER_RE.search(other):
                    raise Unknown(
                        f"{where}: markers {one!r} and {other!r} name an extra or group;"
                        " their overlap is not compared by this check"
                    )
                if not spelled_markers_equivalent((f"({one})", f"({other})"), (_NEVER,)):
                    raise Unknown(
                        f"{where}: source markers must be disjoint, but {one!r} and"
                        f" {other!r} overlap; uv rejects the file"
                    )


_NEVER = "python_version < '0'"  # the marker uv spells for "never" (its own hint text)


def _source_kind(where: str, source: dict) -> str:
    """The one source kind an entry names; an unknown key or zero/several kinds is a
    file uv refuses to parse."""
    known = _PYPROJECT_SOURCE_COMMON.union(*_PYPROJECT_SOURCE_KEYS.values())
    unknown = sorted(set(source) - known)
    if unknown:
        raise Unknown(f"{where} = {source!r}: unknown field {unknown[0]!r}; uv rejects the file")
    kinds = [k for k in _PYPROJECT_SOURCE_KEYS if k in source]
    if len(kinds) != 1:
        raise Unknown(f"{where} = {source!r} names {len(kinds)} source kinds, not one")
    return kinds[0]


def _source_entry_conflicts(where: str, source: dict) -> None:
    """The pairs uv refuses on one entry whose keys are each allowed for its kind."""
    if sum(k in source for k in ("rev", "tag", "branch")) > 1:
        raise Unknown(f"{where} = {source!r}: expected at most one of rev, tag, or branch")
    if source.get("editable") is True and source.get("package") is False:
        raise Unknown(
            f"{where} = {source!r}: cannot specify both editable = true and package = false"
        )
    if "extra" in source and "group" in source:
        raise Unknown(f"{where} = {source!r}: cannot specify both extra and group")
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
