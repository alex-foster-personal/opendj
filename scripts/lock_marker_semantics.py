"""PEP 440 versions and PEP 508 markers compared BY MEANING, the way uv records them.

Companion to scripts/lock_metadata_check.py, which matches pyproject.toml
requirements against uv.lock's `requires-dist` textually first and hands the
leftovers here. Stdlib only, on purpose: `packaging` is not on the runner the
check runs on, and a dependency the check itself must trust would defeat a
resolution-free gate.

uv does not record a marker verbatim. Measured against uv 0.8.17 on
Mon 22 Sep 2026 (a scratch project locked with each spelling, then the root's
`requires-dist` read back):

    python_version < '3.11'                    -> python_full_version < '3.11'
    python_version <= '3.11rc1'                -> python_full_version < '3.12'
    python_full_version < '3.11rc1'            -> python_full_version < '3.11'
    python_full_version <= '3.11rc1'           -> python_full_version <= '3.11'
    python_full_version == '3.11rc1'           -> python_full_version == '3.11'
    python_full_version > '3.13.0b2'           -> python_full_version > '3.13'
    python_full_version >= '3.11.dev0'         -> python_full_version >= '3.11'
    python_full_version < '3.11.post1'         -> python_full_version < '3.11'
    python_full_version ~= '3.11.1'            -> >= '3.11.1' and < '3.12'
    python_full_version ~= '3.10.0'            -> python_full_version == '3.10.*'
    python_full_version ~= '3.10.0.0'          -> >= '3.10' and < '3.10.1'
    python_full_version ~= '3.10'              -> >= '3.10' and < '4'
    python_full_version ~= '3'                 -> (marker dropped entirely)
    os_name == 'a@b'                           -> kept verbatim (`@` in a literal)
    python_full_version == '3.11.0'            -> python_full_version == '3.11'
    python_full_version == '3.11.*'            -> python_full_version == '3.11.*'
    python_full_version == '3.11.0.*'          -> >= '3.11.0' and < '3.11.1'
    python_full_version != '3.11.0.*'          -> < '3.11.0' or >= '3.11.1'
    implementation_version < '3.11rc1'         -> implementation_version < '3.11'
    python_full_version >= '3.11.2+local'      -> (marker dropped entirely)
    python_full_version <= '1!3'               -> python_full_version <= '3'
    os_name == 'posix and stuff'               -> kept verbatim (quoted `and`)
    os_name == "posix's"                       -> kept verbatim (double quotes)
    os_name == "posix"                         -> os_name == 'posix'
    sys_platform < 'win32' and sys_platform != 'win32'  -> sys_platform < 'win32'
    sys_platform < 'linux' or sys_platform == 'linux'   -> sys_platform <= 'linux'
    sys_platform < 'win32' or sys_platform >= 'win32'   -> (marker dropped: always true)
    a and b / (a or b) and c                   -> re-associated, operands re-ordered
    os_name != sys_platform                    -> (marker dropped entirely)
    os_name in sys_platform                    -> (marker dropped entirely)
    python_version >= '3.10' and os_name != sys_platform -> python_full_version >= '3.10'
    os_name != sys_platform or sys_platform == 'win32'   -> sys_platform == 'win32'
    (os_name != sys_platform) and python_version < '3.99' -> python_full_version < '3.99'
    'posix' == os_name                         -> os_name == 'posix'
    '3.11.*' == python_full_version            -> (marker dropped entirely)
    '3.11.*' != python_full_version            -> (marker dropped entirely)
    python_full_version < '3.11.*'             -> (marker dropped entirely)
    python_full_version ~= '3.11.*'            -> (marker dropped entirely)
    python_full_version in '3.11.*'            -> (marker dropped entirely)
    python_full_version != '3.11.*'            -> python_full_version != '3.11.*'
    '3.11.*' == python_full_version and os_name == 'posix' -> os_name == 'posix'
    '3.11.*' == python_full_version or sys_platform == 'win32' -> sys_platform == 'win32'
    os_name == '3.11.*'                        -> kept verbatim (a string)
    os_name ~= 'posix'                         -> (marker dropped entirely)
    'posix' ~= os_name                         -> uv panics (exit 101): UNKNOWN here
    '3.11' ~= python_version                   -> >= '3.11' and < '4'
    os_name == 'x' and os_name != 'x'          -> python_version < '0' (uv's always-false)
    os_name in 'ab' or os_name == 'a'          -> kept verbatim (`in` is an opaque atom)
    os_name in 'ab' and os_name == 'c'         -> kept verbatim (not reduced to false)
    os_name in 'ab' and os_name not in 'ab'    -> python_version < '0' (`not in` negates it)
    os_name in 'ab' or os_name not in 'ab'     -> (marker dropped: always true)
    os_name in 'ab' or os_name in 'ab'         -> os_name in 'ab'
    'a' in os_name or os_name == 'a'           -> kept verbatim (a distinct atom)
    platform_system == 'Linux'                 -> sys_platform == 'linux'
    platform_system != 'Windows'               -> sys_platform != 'win32'
    platform_system == 'Darwin'                -> sys_platform == 'darwin'
    platform_system == 'FreeBSD' / 'linux' / in 'Linux' / < 'M' -> kept verbatim
    os_name == 'nt' and sys_platform == 'linux'         -> python_version < '0'
    os_name == 'nt' and sys_platform == 'darwin'/'ios'  -> python_version < '0'
    os_name == 'posix' and sys_platform == 'win32'      -> python_version < '0'
    os_name == 'nt' and sys_platform == 'android'/'cygwin'/'freebsd' -> kept verbatim
    os_name != 'posix' and sys_platform == 'linux'      -> kept verbatim
    (os_name == 'nt' or os_name == 'posix') and sys_platform == 'linux'
        -> (os_name == 'nt' and sys_platform == 'linux') or (os_name == 'posix' and ...)

So uv's marker algebra is RELEASE-ONLY: every version literal is cut to its
release segment before the comparison is stored, whatever the operator, which
is lossy against PEP 440 (`<= '3.11rc1'` admits 3.11.0 once rewritten) and is
nevertheless what a fresh lock contains. A comparator that applied exact
PEP 440 semantics to both raw strings would read every such fresh lock as
stale (Codex P2 on PR #3763, round 6), so this one mirrors uv: literals are
cut to their release, environments are probed over release versions only, and
a local version literal, whose rewrite is not modeled (uv dropped the marker),
is UNKNOWN rather than a verdict.

A comparison between two VARIABLES (`os_name != sys_platform`) is ERASED from
whatever it sits in: it leaves an `and` and an `or` alike (so it is not a truth
value, which would have made the `or` always true), and a marker made only of
such clauses is dropped. `_erase_dropped_clauses` mirrors that before anything
is evaluated.
A `.*` WILDCARD is a prefix pattern, meaningful only as the right operand of
`==` / `!=` against a version variable. Anywhere else (`'3.11.*' ==
python_full_version`, `python_full_version < '3.11.*'`, `~=`, `in`) it is not
a PEP 440 comparison, and uv drops the clause the same way: erased from an
`and` and an `or` alike, never a truth value (measured, round 16).
`~=` on a STRING variable (`os_name ~= 'posix'`) is dropped the same way; with
the literal on the LEFT uv 0.8.17 panics and locks nothing: UNKNOWN (round 20).

The string-ordering rows show that uv orders plain STRINGS lexically, as ranges:
that is the semantics mirrored here for `<`, `<=`, `>` and `>=` on a
non-version variable. (packaging 26 evaluates `<` and `>` on strings as
always false and `<=`, `>=` as equality, per the 2025 dependency-specifier
spec change; a uv.lock check follows uv, and the tests use packaging as an
oracle only where the two agree.)

`platform_system` compared with `==` / `!=` to exactly Linux, Darwin or Windows
is REWRITTEN to the `sys_platform` form (rows above), both sides here. An `and`
chain, as written, holding `os_name == 'nt'` beside `sys_platform ==` linux,
darwin or ios, or `os_name == 'posix'` beside `sys_platform == 'win32'`, is
recorded as false: a finite table of `==` pairs, not an implication (`os_name
!= 'posix' and sys_platform == 'linux'` stays, and the same pair split across
an `or` is kept on both branches once uv distributes it). It is applied to the
SPELLED side only, since uv applied it when it recorded the other side (Codex
P2 on #3763, round 24).

Equivalence itself is a truth-table check: both markers are evaluated over a
grid of environments built from the literals either side mentions. For the
boolean structure over `==`/`!=` on strings a value mentioned by neither side
covers every other assignment; for ORDERED comparisons, on versions and on
plain strings alike, the literals plus a point strictly between each adjacent
pair, one below the smallest and one above the largest cover every order
interval. An `in` / `not in` clause is an OPAQUE ATOM, exactly as uv treats it
(the rows above: never related to `==` on the same variable or to the
literal's substrings, `not in` its negation), so each distinct one is a boolean
axis of its own. Reading it as substring membership accepted `os_name in 'ab'`
for the disjunction of its substrings, which uv records as a different marker
(Codex P2 on #3763, round 23).
"""

from __future__ import annotations

import itertools
import re

from scripts.lock_marker_parser import Unknown, _MarkerParser

# PEP 440 public grammar (the spec's appendix, case-insensitive).
_VERSION_RE = re.compile(
    r"^\s*v?"
    r"(?:(?P<epoch>[0-9]+)!)?"
    r"(?P<release>[0-9]+(?:\.[0-9]+)*)"
    r"(?P<pre>[-_.]?(?P<pre_l>alpha|a|beta|b|preview|pre|c|rc)[-_.]?(?P<pre_n>[0-9]+)?)?"
    r"(?P<post>(?:-(?P<post_n1>[0-9]+))|(?:[-_.]?(?P<post_l>post|rev|r)[-_.]?(?P<post_n2>[0-9]+)?))?"
    r"(?P<dev>[-_.]?(?P<dev_l>dev)[-_.]?(?P<dev_n>[0-9]+)?)?"
    r"(?:\+(?P<local>[a-z0-9]+(?:[-_.][a-z0-9]+)*))?"
    r"\s*$",
    re.IGNORECASE,
)
_PRE_TAGS = {
    "alpha": "a",
    "a": "a",
    "beta": "b",
    "b": "b",
    "c": "rc",
    "pre": "rc",
    "preview": "rc",
    "rc": "rc",
}


def canonical_version(text: str) -> str:
    """The PEP 440 normalized spelling uv records (`01.026` -> `1.26`, `1.0-rc1` -> `1.0rc1`),
    or Unknown for a string that is not a PEP 440 version."""
    match = _VERSION_RE.match(text)
    if match is None:
        raise Unknown(f"not a PEP 440 version: {text!r}")
    out = ""
    if match.group("epoch") and int(match.group("epoch")):
        out += f"{int(match.group('epoch'))}!"
    out += ".".join(str(int(part)) for part in match.group("release").split("."))
    if match.group("pre"):
        out += _PRE_TAGS[match.group("pre_l").lower()] + str(int(match.group("pre_n") or 0))
    if match.group("post"):
        out += ".post" + str(int(match.group("post_n1") or match.group("post_n2") or 0))
    if match.group("dev"):
        out += ".dev" + str(int(match.group("dev_n") or 0))
    if match.group("local"):
        # Each all-numeric local segment compares by integer value, so PEP 440
        # (and uv) spell `1.0+01` as `1.0+1`; alphanumeric segments only lowercase.
        segments = re.split(r"[-_.]", match.group("local").lower())
        out += "+" + ".".join(str(int(seg)) if seg.isdigit() else seg for seg in segments)
    return out


def release_literal(text: str) -> str:
    """A marker's version literal as uv stores it: the release segment only (a `.*`
    wildcard kept), epoch, pre-, post- and dev-release cut off. A local version is
    UNKNOWN: uv dropped the whole marker for one, which this does not model."""
    wildcard = text.endswith(".*")
    match = _VERSION_RE.match(text[:-2] if wildcard else text)
    if match is None:
        raise Unknown(f"not a PEP 440 version: {text!r}")
    if match.group("local"):
        raise Unknown(f"a local version in a marker is not compared: {text!r}")
    out = ".".join(str(int(part)) for part in match.group("release").split("."))
    return out + ".*" if wildcard else out


def _version_key(release: str) -> tuple[int, tuple[int, ...]]:
    """Ordering key for a release-only version: epoch, then the release with
    trailing zeros dropped (3.11 == 3.11.0)."""
    match = _VERSION_RE.match(release)
    assert match is not None
    parts = [int(p) for p in match.group("release").split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return (int(match.group("epoch") or 0), tuple(parts))


def _release_parts(release: str) -> tuple[int, ...]:
    match = _VERSION_RE.match(release)
    assert match is not None
    return tuple(int(p) for p in match.group("release").split("."))


def _cmp_versions(lhs: str, op: str, rhs: str) -> bool:
    """`lhs op rhs` over release versions, both sides already cut by release_literal:
    plain ordering, a `.*` wildcard as a prefix match on epoch + release, `~=` as
    the compatible range, `===` as string equality."""
    if op == "===":
        return lhs == rhs
    if lhs.endswith(".*") or (rhs.endswith(".*") and op not in ("==", "!=")):
        # Erased by _erase_dropped_clauses before evaluation (uv drops the clause);
        # reaching here is a programming error, UNKNOWN rather than a crash.
        raise Unknown(f"wildcard comparison survived erasure: {lhs!r} {op} {rhs!r}")
    if op in ("==", "!=") and rhs.endswith(".*"):
        # The prefix keeps its WIDTH: `3.11.0.*` admits 3.11.0.x only, not 3.11.5, which
        # is why uv records it as `>= '3.11.0' and < '3.11.1'` and `3.11.*` as itself.
        prefix = _release_parts(rhs[:-2])
        candidate = _release_parts(lhs)
        candidate += (0,) * (len(prefix) - len(candidate))
        hit = candidate[: len(prefix)] == prefix
        return hit if op == "==" else not hit
    left, right = _version_key(lhs), _version_key(rhs)
    if op == "~=":
        # The prefix keeps its WIDTH too: `~= '3.10.0'` is `== '3.10.*'` (uv records it
        # so), not `>= '3.10' and < '4'` as the trailing-zero-stripping key would say.
        prefix = _compatible_prefix(rhs)
        candidate = _release_parts(lhs)
        candidate += (0,) * (len(prefix) - len(candidate))
        return left >= right and candidate[: len(prefix)] == prefix
    compare = {
        "==": left == right,
        "!=": left != right,
        "<": left < right,
        "<=": left <= right,
        ">": left > right,
        ">=": left >= right,
    }
    if op not in compare:
        raise Unknown(f"unsupported version operator {op!r}")
    return compare[op]


def _compatible_prefix(release: str) -> tuple[int, ...]:
    """The prefix `~= release` pins, at the literal's own width: `3.10.0` -> (3, 10).
    A one-component literal has no prefix (PEP 440 forbids it; uv drops the marker,
    a rewrite not modeled), so it is UNKNOWN rather than a verdict."""
    parts = _release_parts(release)
    if len(parts) < 2:
        raise Unknown(f"compatible release {release!r} needs at least two components")
    return parts[:-1]


def _compatible_upper(release: str) -> str:
    """The first release `~= release` excludes, spelled at the prefix's width."""
    prefix = _compatible_prefix(release)
    return ".".join(map(str, (*prefix[:-1], prefix[-1] + 1)))


# ----- PEP 508 marker grammar --------------------------------------------------

_VERSION_VARS = frozenset({"python_version", "python_full_version", "implementation_version"})
_ORDER_OPS = frozenset({"<", "<=", ">", ">="})
_GRID_CAP = 20000


def _eval(node: tuple, env: dict[str, str]) -> bool:
    kind = node[0]
    if kind == "true":
        return True
    if kind == "or":
        return _eval(node[1], env) or _eval(node[2], env)
    if kind == "and":
        return _eval(node[1], env) and _eval(node[2], env)
    _, lhs, op, rhs = node
    if op in ("in", "not in"):
        return (env[_atom(lhs, rhs)] == "1") == (op == "in")
    var = lhs if lhs[0] == "word" else rhs if rhs[0] == "word" else None
    left = env[lhs[1]] if lhs[0] == "word" else lhs[1][1:-1]
    right = env[rhs[1]] if rhs[0] == "word" else rhs[1][1:-1]
    if var is not None and var[1] in _VERSION_VARS:
        # Both sides cut to their release: the literal because uv stores it so,
        # the probe because the grid only holds release versions anyway.
        return _cmp_versions(release_literal(left), op, release_literal(right))
    if op == "~=":  # erased by _erase_dropped_clauses before evaluation (uv drops it)
        raise Unknown(f"string ~= survived erasure: {lhs!r} {rhs!r}")
    literal = rhs if lhs[0] == "word" else lhs
    if var is not None and op in ("<", "<=", ">", ">=") and literal[1][1:-1] == "":
        # `sys_platform >= ''` is true of every string, yet uv RECORDS it as written
        # and rejects the unmarked lock (measured uv 0.8.17, Codex P2 on #3763,
        # round 29), unlike `python_version >= '0'`, which it erases. That
        # keep-or-erase table is not modeled: UNKNOWN, never "equivalent".
        raise Unknown(f"ordering against the empty string is not compared: {node!r}")
    return {
        "==": left == right,
        "!=": left != right,
        "<": left < right,
        "<=": left <= right,
        ">": left > right,
        ">=": left >= right,
        "===": left == right,
    }[op]


def _atom(lhs: tuple, rhs: tuple) -> str:
    """The grid axis of an `in` / `not in` clause: one per (side, variable, literal),
    the literal re-quoted so `"ab"` and `'ab'` are the same atom."""
    return " in ".join(t[1] if t[0] == "word" else repr(t[1][1:-1]) for t in (lhs, rhs))


class _Literals:
    """Per variable: the literals it is compared with (`values`) and whether it is
    ORDERED (`<`, `<=`, `>`, `>=`); plus the `in` / `not in` atoms (`atoms`)."""

    def __init__(self) -> None:
        self.values: dict[str, set[str]] = {}
        self.atoms: set[str] = set()
        self.ordered: set[str] = set()

    def collect(self, node: tuple) -> None:
        kind = node[0]
        if kind == "true":
            return
        if kind in ("or", "and"):
            self.collect(node[1])
            self.collect(node[2])
            return
        _, lhs, op, rhs = node
        if op in ("in", "not in") and {lhs[0], rhs[0]} == {"word", "str"}:
            if (lhs[1] if lhs[0] == "word" else rhs[1]) in _VERSION_VARS:
                raise Unknown(f"membership test on a version variable is not compared: {node!r}")
            self.atoms.add(_atom(lhs, rhs))
            return
        if lhs[0] == "word" and rhs[0] == "str":
            var, literal = lhs[1], rhs[1][1:-1]
        elif rhs[0] == "word" and lhs[0] == "str":
            var, literal = rhs[1], lhs[1][1:-1]
        elif lhs[0] == "word" and rhs[0] == "word":
            # Erased by _erase_dropped_clauses before any grid is built; reaching
            # here is a programming error, reported as UNKNOWN rather than a verdict.
            raise Unknown(f"variable-to-variable comparison survived erasure: {node!r}")
        else:
            raise Unknown(f"marker compares two literals: {node!r}")
        self.values.setdefault(var, set()).add(literal)
        if op == "~=" and var in _VERSION_VARS:
            # The range's upper bound is a boundary the literal alone does not name.
            self.values[var].add(_compatible_upper(release_literal(literal)))
        if op in _ORDER_OPS:
            self.ordered.add(var)


def _between(lower: str, upper: str) -> str:
    """A release version strictly between two release versions (lower < upper):
    `lower.1`, `lower.0.1`, ... One more component than `upper` has always suffices
    (`3.11.15` < `3.11.15.0.0.0.0.0.1` < `3.11.15.0.0.0.0.1`; a fixed four read a
    valid deep bound as UNKNOWN, round 21). Nothing between is UNKNOWN, never a guess."""
    for zeros in range(len(_release_parts(upper)) + 2):
        candidate = lower + ".0" * zeros + ".1"
        if _version_key(lower) < _version_key(candidate) < _version_key(upper):
            return candidate
    raise Unknown(f"no probe version strictly between {lower!r} and {upper!r}")


def _version_probes(literals: set[str]) -> list[str]:
    """Every literal, a point strictly between each adjacent pair, one below the
    smallest and one above the largest: complete for the order intervals any
    comparison against these literals can carve out. A wildcard `X.*` contributes
    both of its bounds (X and the next release at X's width: `3.11.0.*` is
    `>= 3.11.0 and < 3.11.1`), so the point between them has the width that
    separates it from the wider `3.11.*`. Release versions only, because uv's
    marker algebra is (module docstring); one spelling per value (`3.11`, never
    `3.11.0`) so the between-points do not depend on which literal was seen first."""
    points: set[tuple[int, tuple[int, ...]]] = set()
    for literal in literals:
        release = release_literal(literal)
        if release.endswith(".*"):
            parts = _release_parts(release[:-2])
            points.add(_version_key(release[:-2]))
            points.add((0, (*parts[:-1], parts[-1] + 1)))
        else:
            points.add(_version_key(release))
    ordered = [".".join(map(str, key[1])) for key in sorted(points)]
    # `0` is the smallest release there is. The ceiling is DERIVED, the next
    # major above the largest point, or a literal beyond a fixed ceiling would
    # have no probe above it (Codex P2 on #3763, round 13).
    ceiling = str(max(points)[1][0] + 1)
    probes = ["0", *ordered, ceiling]
    for lower, upper in itertools.pairwise(ordered):
        probes.append(_between(lower, upper))
    if _version_key(ordered[0]) > _version_key("0"):
        probes.append(_between("0", ordered[0]))
    probes.append(_between(ordered[-1], ceiling))
    return sorted(set(probes), key=_version_key)


def _string_probes(values: set[str], ordered: bool) -> list[str]:
    """The literals, a value no literal mentions, and when the variable is ordered
    the order intervals too: `''` sits below every string and `lit + NUL` is the
    immediate successor of `lit`, so it lies strictly inside the interval up to the
    next literal (or above the last)."""
    pool: set[str] = set(values)
    # A value no clause mentions. Derived, not fixed, or a marker naming the
    # sentinel itself would read as matching everything (Codex P2 on #3763, round 12).
    unmentioned = "zz-no-literal-mentions-this"
    while unmentioned in pool:
        unmentioned += "-"
    pool.add(unmentioned)
    if ordered:
        pool |= {""} | {value + "\x00" for value in values}
    return sorted(pool)


def _grid(ast_a: tuple, ast_b: tuple) -> list[dict[str, str]]:
    lits = _Literals()
    lits.collect(ast_a)
    lits.collect(ast_b)
    python_lits: set[str] = set()
    for var in ("python_version", "python_full_version"):
        python_lits |= lits.values.pop(var, set())
    axes: list[tuple[str, list[str]]] = [(atom, ["", "1"]) for atom in sorted(lits.atoms)]
    if python_lits:
        axes.append(("python_full_version", _version_probes(python_lits)))
    for var, values in sorted(lits.values.items()):
        if var in _VERSION_VARS:
            axes.append((var, _version_probes(values) if values else ["0"]))
        else:
            axes.append((var, _string_probes(values, var in lits.ordered)))
    size = 1
    for _axis, probes_on_axis in axes:
        size *= len(probes_on_axis)
    if size > _GRID_CAP:
        raise Unknown(f"marker probe grid too large ({size} environments)")
    envs: list[dict[str, str]] = [{}]
    for axis, probes_on_axis in axes:
        envs = [dict(env, **{axis: value}) for env in envs for value in probes_on_axis]
    for env in envs:
        if "python_full_version" in env:
            env["python_version"] = _python_version_of(env["python_full_version"])
    return envs


def _python_version_of(full: str) -> str:
    """`python_version` is the release major.minor of `python_full_version`
    (`3.11.0rc1` -> `3.11`), exactly as the interpreter and packaging derive it."""
    match = _VERSION_RE.match(full)
    assert match is not None
    parts = [*match.group("release").split("."), "0"]
    return f"{int(parts[0])}.{int(parts[1])}"


def markers_equivalent(spelled: tuple[str, ...], recorded: tuple[str, ...]) -> bool:
    """True when the marker as SPELLED in pyproject.toml and the one uv RECORDED
    (both already normalized clause tuples) mean the same thing; an empty tuple is
    the always-true marker uv drops. Ordered: uv's contradiction table (module
    docstring) is applied to the spelled side, as uv applied it to the recorded one."""
    if spelled == recorded:
        return True  # the same spelling needs no model of uv's rewrites
    return _same_truth(_false_chains(_parse_clauses(spelled)), _parse_clauses(recorded))


def spelled_markers_equivalent(one: tuple[str, ...], other: tuple[str, ...]) -> bool:
    """True when two markers as SPELLED in pyproject.toml mean the same thing to uv,
    which canonicalizes each before recording (so a requirement repeated under
    `python_version < '3.12'` and `python_full_version < '3.12'`, or under
    `platform_system == 'Linux'` and `sys_platform == 'linux'`, is recorded ONCE:
    measured uv 0.8.17, Codex P2 on #3763, round 26). Symmetric: both sides get the
    spelled-side rewrites."""
    if one == other:
        return True
    return _same_truth(_false_chains(_parse_clauses(one)), _false_chains(_parse_clauses(other)))


def _same_truth(ast_a: tuple, ast_b: tuple) -> bool:
    return all(_eval(ast_a, env) == _eval(ast_b, env) for env in _grid(ast_a, ast_b))


def _parse_clauses(clauses: tuple[str, ...]) -> tuple:
    text = " and ".join(clause for clause in clauses if clause.strip())
    if not text:
        return ("true",)
    node = _erase_dropped_clauses(_MarkerParser(text).parse()) or ("true",)
    return _rewrite_platform_system(node)


_SYS_PLATFORM_OF = {"'Linux'": "'linux'", "'Darwin'": "'darwin'", "'Windows'": "'win32'"}
_FALSE = ("cmp", ("word", "python_version"), "<", ("str", "'0'"))
_CONFLICTS = {("'nt'", "'linux'"), ("'nt'", "'darwin'"), ("'nt'", "'ios'"), ("'posix'", "'win32'")}


def _rewrite_platform_system(node: tuple) -> tuple:
    """uv's `platform_system` -> `sys_platform` rewrite (module docstring)."""
    if node[0] in ("or", "and"):
        return (node[0], _rewrite_platform_system(node[1]), _rewrite_platform_system(node[2]))
    if node[0] != "cmp" or node[2] not in ("==", "!="):
        return node
    _, lhs, op, rhs = node
    if lhs[0] == "str" and rhs[0] == "word":
        lhs, rhs = rhs, lhs
    if lhs == ("word", "platform_system") and rhs[0] == "str" and rhs[1] in _SYS_PLATFORM_OF:
        return ("cmp", ("word", "sys_platform"), op, ("str", _SYS_PLATFORM_OF[rhs[1]]))
    return node


def _false_chains(node: tuple) -> tuple:
    """uv's os_name / sys_platform contradiction table (module docstring): an `and`
    chain, as written, holding a listed `==` pair is the false marker uv records."""
    if node[0] == "or":
        return ("or", _false_chains(node[1]), _false_chains(node[2]))
    if node[0] != "and":
        return node
    equal: dict[str, set[str]] = {}
    for leaf in _and_leaves(node):
        if leaf[0] == "cmp" and leaf[2] == "==" and {leaf[1][0], leaf[3][0]} == {"word", "str"}:
            word, literal = (leaf[1], leaf[3]) if leaf[1][0] == "word" else (leaf[3], leaf[1])
            equal.setdefault(word[1], set()).add(literal[1])
    for os_name in equal.get("os_name", ()):
        for platform in equal.get("sys_platform", ()):
            if (os_name, platform) in _CONFLICTS:
                return _FALSE
    return ("and", _false_chains(node[1]), _false_chains(node[2]))


def _and_leaves(node: tuple) -> list[tuple]:
    if node[0] == "and":
        return _and_leaves(node[1]) + _and_leaves(node[2])
    return [node]


def _erase_dropped_clauses(node: tuple) -> tuple | None:
    """uv erases a comparison between two variables from the marker it records
    (module docstring: `os_name != sys_platform` leaves both an `and` and an `or`,
    and a marker made only of such clauses is dropped), and a wildcard where it is
    not a PEP 440 comparison the same way. Mirror it on the parsed tree: None is an
    erased subtree, which its parent then skips."""
    kind = node[0]
    if kind == "true":
        return node
    if kind in ("or", "and"):
        left, right = _erase_dropped_clauses(node[1]), _erase_dropped_clauses(node[2])
        if left is None:
            return right
        if right is None:
            return left
        return (kind, left, right)
    _, lhs, op, rhs = node
    if lhs[0] == "word" and rhs[0] == "word":
        return None
    if _uv_drops_clause(lhs, op, rhs):
        return None
    return node


def _uv_drops_clause(lhs: tuple, op: str, rhs: tuple) -> bool:
    """A `.*` wildcard is a prefix pattern, meaningful only as the RIGHT operand of `==`
    / `!=` against a version variable; anywhere else uv drops the clause (round 16), as
    it does `~=` on a STRING variable (round 20). Against a string variable a wildcard
    is a plain string, kept. `'posix' ~= os_name` makes uv 0.8.17 panic instead of
    locking, so nothing it could have recorded exists: UNKNOWN, never a verdict."""
    if lhs[0] == "word" and rhs[0] == "str":
        var, literal, literal_on_right = lhs[1], rhs[1][1:-1], True
    elif rhs[0] == "word" and lhs[0] == "str":
        var, literal, literal_on_right = rhs[1], lhs[1][1:-1], False
    else:
        return False
    if op == "~=" and var not in _VERSION_VARS:
        if literal_on_right:
            return True
        raise Unknown(f"uv cannot lock a marker with {literal!r} ~= {var} (it panics)")
    if var not in _VERSION_VARS or not literal.endswith(".*"):
        return False
    return not (literal_on_right and op in ("==", "!="))
