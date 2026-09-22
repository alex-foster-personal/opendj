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

So uv's marker algebra is RELEASE-ONLY: every version literal is cut to its
release segment before the comparison is stored, whatever the operator, which
is lossy against PEP 440 (`<= '3.11rc1'` admits 3.11.0 once rewritten) and is
nevertheless what a fresh lock contains. A comparator that applied exact
PEP 440 semantics to both raw strings would read every such fresh lock as
stale (Codex P2 on PR #3763, round 6), so this one mirrors uv: literals are
cut to their release, environments are probed over release versions only, and
a local version literal, whose rewrite is not modeled (uv dropped the marker),
is UNKNOWN rather than a verdict.

The last three rows show that uv orders plain STRINGS lexically, as ranges:
that is the semantics mirrored here for `<`, `<=`, `>` and `>=` on a
non-version variable. (packaging 26 evaluates `<` and `>` on strings as
always false and `<=`, `>=` as equality, per the 2025 dependency-specifier
spec change; a uv.lock check follows uv, and the tests use packaging as an
oracle only where the two agree.)

Equivalence itself is a truth-table check: both markers are evaluated over a
grid of environments built from the literals either side mentions. For the
boolean structure over `==`/`!=` on strings a value mentioned by neither side
covers every other assignment; every substring of an `in` literal covers
membership; and for ORDERED comparisons, on versions and on plain strings
alike, the literals plus a point strictly between each adjacent pair, one below
the smallest and one above the largest cover every order interval. Membership
and ordering on the SAME string variable would need a probe per (interval,
substring) combination, so that mix is UNKNOWN too.
"""

from __future__ import annotations

import itertools
import re


class Unknown(Exception):
    """The check could not measure; the caller must report UNKNOWN, not a verdict."""


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
        prefix_release = right[1][:-1] if len(right[1]) > 1 else right[1]
        return left >= right and left[1][: len(prefix_release)] == prefix_release
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


# ----- PEP 508 marker grammar --------------------------------------------------

_VERSION_VARS = frozenset({"python_version", "python_full_version", "implementation_version"})
_ORDER_OPS = frozenset({"<", "<=", ">", ">="})
_TOKEN_RE = re.compile(
    r"\s*(?:(?P<lp>\()|(?P<rp>\))|(?P<str>'[^']*'|\"[^\"]*\")|(?P<word>[A-Za-z_][A-Za-z0-9_.]*)"
    r"|(?P<op>===|==|!=|<=|>=|<|>|~=))"
)
_GRID_CAP = 20000


def tokenize_marker(text: str) -> list[tuple[str, str]]:
    """PEP 508 marker tokens. A string literal may use either quote form and keeps
    its contents verbatim (`"posix's"` is a valid literal uv records as is); its
    token text is re-quoted canonically, single quotes unless the value holds one,
    so the two spellings of one value compare equal downstream."""
    out: list[tuple[str, str]] = []
    pos = 0
    while pos < len(text):
        if text[pos:].strip() == "":
            break
        match = _TOKEN_RE.match(text, pos)
        if match is None or match.end() == pos:
            raise Unknown(f"unparseable marker: {text!r}")
        pos = match.end()
        kind = match.lastgroup or ""
        token = match.group(kind)
        if kind == "str":
            token = quote_literal(token[1:-1])
        out.append((kind, token))
    return out


def quote_literal(value: str) -> str:
    if "'" not in value:
        return f"'{value}'"
    if '"' not in value:
        return f'"{value}"'
    raise Unknown(f"a marker literal with both quote characters is not representable: {value!r}")


class _MarkerParser:
    """Recursive descent over PEP 508's marker grammar: or > and > atom | (expr)."""

    def __init__(self, text: str) -> None:
        self.toks = tokenize_marker(text)
        self.i = 0
        self.text = text

    def _peek(self) -> tuple[str, str] | None:
        return self.toks[self.i] if self.i < len(self.toks) else None

    def _take(self) -> tuple[str, str]:
        tok = self._peek()
        if tok is None:
            raise Unknown(f"marker ends early: {self.text!r}")
        self.i += 1
        return tok

    def parse(self) -> tuple:
        node = self._or()
        if self._peek() is not None:
            raise Unknown(f"trailing tokens in marker: {self.text!r}")
        return node

    def _or(self) -> tuple:
        node = self._and()
        while self._peek() == ("word", "or"):
            self._take()
            node = ("or", node, self._and())
        return node

    def _and(self) -> tuple:
        node = self._atom()
        while self._peek() == ("word", "and"):
            self._take()
            node = ("and", node, self._atom())
        return node

    def _atom(self) -> tuple:
        tok = self._take()
        if tok[0] == "lp":
            node = self._or()
            if self._take()[0] != "rp":
                raise Unknown(f"unbalanced parentheses in marker: {self.text!r}")
            return node
        lhs = tok
        op_tok = self._take()
        if op_tok == ("word", "not"):
            if self._take() != ("word", "in"):
                raise Unknown(f"'not' without 'in' in marker: {self.text!r}")
            op = "not in"
        elif op_tok == ("word", "in"):
            op = "in"
        elif op_tok[0] == "op":
            op = op_tok[1]
        else:
            raise Unknown(f"expected an operator in marker: {self.text!r}")
        rhs = self._take()
        for side in (lhs, rhs):
            if side[0] not in ("str", "word"):
                raise Unknown(f"expected a variable or string in marker: {self.text!r}")
        return ("cmp", lhs, op, rhs)


# ----- evaluation over a probe grid ---------------------------------------------


def _eval(node: tuple, env: dict[str, str]) -> bool:
    kind = node[0]
    if kind == "true":
        return True
    if kind == "or":
        return _eval(node[1], env) or _eval(node[2], env)
    if kind == "and":
        return _eval(node[1], env) and _eval(node[2], env)
    _, lhs, op, rhs = node
    var = lhs if lhs[0] == "word" else rhs if rhs[0] == "word" else None
    left = env[lhs[1]] if lhs[0] == "word" else lhs[1][1:-1]
    right = env[rhs[1]] if rhs[0] == "word" else rhs[1][1:-1]
    if var is not None and var[1] in _VERSION_VARS and op not in ("in", "not in"):
        # Both sides cut to their release: the literal because uv stores it so,
        # the probe because the grid only holds release versions anyway.
        return _cmp_versions(release_literal(left), op, release_literal(right))
    if op == "in":
        return left in right
    if op == "not in":
        return left not in right
    return {
        "==": left == right,
        "!=": left != right,
        "<": left < right,
        "<=": left <= right,
        ">": left > right,
        ">=": left >= right,
        "===": left == right,
        "~=": left == right,
    }[op]


class _Literals:
    """Per variable: the literals it is compared with (`values`), the `in` /
    `not in` right-hand literals (`members`, since `var in 'a,b'` is true for every
    SUBSTRING of the literal), and whether it is ORDERED (`<`, `<=`, `>`, `>=`)."""

    def __init__(self) -> None:
        self.values: dict[str, set[str]] = {}
        self.members: dict[str, set[str]] = {}
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
        if lhs[0] == "word" and rhs[0] == "str":
            var, literal = lhs[1], rhs[1][1:-1]
        elif rhs[0] == "word" and lhs[0] == "str":
            if op in ("in", "not in"):
                # `'lit' in var` is true for every value CONTAINING the literal; that
                # class is not enumerable from the literals alone.
                raise Unknown(
                    f"membership with the variable on the right is not compared: {node!r}"
                )
            var, literal = rhs[1], lhs[1][1:-1]
        elif lhs[0] == "word" and rhs[0] == "word":
            self.values.setdefault(lhs[1], set())
            self.values.setdefault(rhs[1], set())
            return
        else:
            raise Unknown(f"marker compares two literals: {node!r}")
        self.values.setdefault(var, set()).add(literal)
        if op in ("in", "not in"):
            self.members.setdefault(var, set()).add(literal)
        if op in _ORDER_OPS:
            self.ordered.add(var)


def _substrings(text: str) -> set[str]:
    return {text[i:j] for i in range(len(text)) for j in range(i + 1, len(text) + 1)}


def _between(lower: str, upper: str) -> str:
    """A release version strictly between two release versions (lower < upper):
    `lower.1`, `lower.0.1`, ... Nothing between is UNKNOWN, never a guess."""
    for zeros in range(4):
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
    probes = ["0", *ordered, "9999"]
    for lower, upper in itertools.pairwise(ordered):
        probes.append(_between(lower, upper))
    if _version_key(ordered[0]) > _version_key("0"):
        probes.append(_between("0", ordered[0]))
    if _version_key(ordered[-1]) < _version_key("9999"):
        probes.append(_between(ordered[-1], "9999"))
    return sorted(set(probes), key=_version_key)


def _string_probes(values: set[str], members: set[str], ordered: bool) -> list[str]:
    """The literals, a value no literal mentions, every substring of a membership
    literal, and when the variable is ordered the order intervals too: `''` sits
    below every string and `lit + NUL` is the immediate successor of `lit`, so it
    lies strictly inside the interval up to the next literal (or above the last)."""
    if members and ordered:
        raise Unknown("membership and ordering on one marker variable are not compared")
    pool: set[str] = set(values) | {"zz-no-literal-mentions-this"}
    for member in members:
        pool |= _substrings(member)
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
    for var in lits.members:
        if var in _VERSION_VARS:
            raise Unknown(f"membership test on {var} is not compared")
    axes: list[tuple[str, list[str]]] = []
    if python_lits:
        axes.append(("python_full_version", _version_probes(python_lits)))
    for var, values in sorted(lits.values.items()):
        if var in _VERSION_VARS:
            axes.append((var, _version_probes(values) if values else ["0"]))
        else:
            probes = _string_probes(values, lits.members.get(var, set()), var in lits.ordered)
            axes.append((var, probes))
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


def markers_equivalent(marker_a: tuple[str, ...], marker_b: tuple[str, ...]) -> bool:
    """True when two (already-normalized) marker clause tuples mean the same thing;
    an empty tuple is the always-true marker uv drops."""
    ast_a, ast_b = _parse_clauses(marker_a), _parse_clauses(marker_b)
    return all(_eval(ast_a, env) == _eval(ast_b, env) for env in _grid(ast_a, ast_b))


def _parse_clauses(clauses: tuple[str, ...]) -> tuple:
    text = " and ".join(clause for clause in clauses if clause.strip())
    return _MarkerParser(text).parse() if text else ("true",)
