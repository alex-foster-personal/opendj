"""PEP 440 specifiers normalized the way `uv lock --check` compares them.

Companion to scripts/lock_metadata_check.py (stdlib only, like it). uv records a
dependency's specifier VERBATIM in uv.lock's `requires-dist` and, on `uv lock
--check`, compares the pyproject.toml spelling with it operator by operator with
the version operands compared as PEP 440 VERSIONS, not text. Measured against
uv 0.8.17 on Tue 22 Sep 2026 (a scratch project locked with one spelling, the
dependency respelled, `uv lock --check` run; Codex P2 on PR #3763, round 23):

    >=1.16      -> >=1.16.0       accepted (also >=1.16.0.0.0, and the reverse)
    ==1.17      -> ==1.17.0       accepted
    <2          -> <2.0           accepted
    !=1.16      -> !=1.16.0       accepted
    >=1.16.0rc1 -> >=1.16rc1      accepted
    ~=1.16.0    -> ~=1.16         accepted (a different PEP 440 range: uv does not care)
    ==1.17.*    -> ==1.17.0.*     accepted (a different wildcard width: same)
    ~=1.3       -> >=1.3,<2       REJECTED (the operator changed; round 21)
    >=01.026    -> >=1.26         accepted (round 8)

So a clause is `<op>` + the operand's canonical PEP 440 spelling with trailing
zero release components dropped (`1.26.0` is `1.26`), the operator kept as
written. For requires-python only, uv records the TIGHTEST bounds instead of the
spelling, so `~=` expands to its two bounds there (`norm_spec(...,
expand_compatible=True)`), which scripts/lock_metadata_check.py compares by
meaning.
"""

from __future__ import annotations

import re

from scripts.lock_marker_parser import _MarkerParser, tokenize_marker
from scripts.lock_marker_semantics import (
    _VERSION_RE,
    Unknown,
    _release_parts,
    canonical_version,
    release_literal,
)

CLAUSE_RE = re.compile(r"^(?P<op>===|==|!=|<=|>=|<|>|~=)\s*(?P<version>.+)$")
# A spelled clause: whitespace may surround the operator and the operand, never sit
# inside either (`> =3.11`, `>=3 .11`; uv rejects both, round 49).
SPELLED_CLAUSE_RE = re.compile(r"(===|==|!=|<=|>=|<|>|~=)\s*((?![=<>!~\s])\S+)")


def norm_clause(clause: str, *, expand_compatible: bool) -> list[str]:
    """One specifier clause with its version operand canonicalized, so a valid but
    noncanonical spelling in pyproject.toml (`>=01.026`) matches the `>=1.26` uv wrote.
    For requires-python a compatible release expands to its two bounds (`~=3.11` ->
    `>=3.11`, `<4`; `~=3.11.2` -> `>=3.11.2`, `<3.12`), which is how uv records it. A
    DEPENDENCY's `~=` uv keeps verbatim, and `uv lock --check` fails when its spelling
    changes to the bounds form or back (measured uv 0.8.17, round 21), so there it
    stays `~=` with the operand canonicalized and never equals `>=x,<y`."""
    match = CLAUSE_RE.match(clause)
    if match is None:
        raise Unknown(f"unparseable specifier clause: {clause!r}")
    op, version = match.group("op"), match.group("version")
    if op == "===":
        return [op + version]  # arbitrary equality compares the raw string, by definition
    if op == "~=" and expand_compatible:
        return [">=" + canonical_version(version), "<" + compatible_upper(version)]
    if op in ("==", "!=") and version.endswith(".*"):
        return [op + trim_release(canonical_version(version[:-2])) + ".*"]
    return [op + trim_release(canonical_version(version))]


def trim_release(canonical: str) -> str:
    """`1.26.0` and `1.26` are one PEP 440 version and `uv lock --check` reads a
    respelling between them as no change (module docstring; the same for
    `[project] version`, `1.0` -> `1.0.0` and `01.0` accepted, `1.0.0rc1` not,
    round 24), so trailing zero release components are dropped, whatever the
    operator."""
    match = _VERSION_RE.match(canonical)
    assert match is not None, canonical  # canonical_version emitted it
    parts = match.group("release").split(".")
    while len(parts) > 1 and parts[-1] == "0":
        parts.pop()
    start, end = match.span("release")
    return canonical[:start] + ".".join(parts) + canonical[end:]


def compatible_upper(version: str) -> str:
    """The exclusive upper bound of `~=version`: the release with its last component
    dropped and the one before it incremented (PEP 440), spelled as uv spells it."""
    release = release_literal(version).split(".")
    if len(release) < 2:
        raise Unknown(f"`~=` needs at least two release components: {version!r}")
    return ".".join([*release[:-2], str(int(release[-2]) + 1)])


def norm_spec(spec: str, *, expand_compatible: bool = False, empty_clauses: str = "reject") -> str:
    """The specifier's clauses normalized and sorted. An empty comma-separated clause
    (`>=3.11,,`, `,>=3.11`, `>=3.11, ,<4`) is refused by uv where `empty_clauses` is
    "reject" (requires-python, `uv lock --check` exit 2) and where it is "trailing"
    except for ONE trailing comma (`localdep>=1,` in `[project] dependencies` is read,
    `localdep>=1,,` and `localdep,>=1` are "Failed to generate package metadata";
    measured uv 0.8.17, Codex P2 on #3763, round 38). The old `if p.strip()` filter
    had dropped every empty clause and certified the malformed side as matching.
    Whitespace may surround an operator or operand (`>= 3.11`, ` >= 3.11 `, `six >= 1`
    are read) but never split one: `> =3.11` and `>=3 .11` are "Failed to parse" for
    requires-python and "must be pep508" for a dependency, `uv lock --check` exit 2
    (measured uv 0.8.17, round 49); deleting every space had collapsed them back onto
    the valid spelling the lock was made from."""
    raw = spec.split(",")
    if empty_clauses == "trailing" and len(raw) > 1 and not raw[-1].strip():
        raw = raw[:-1]
    if len(raw) > 1 and any(not p.strip() for p in raw):
        raise Unknown(f"empty specifier clause in {spec!r}; uv rejects the file")
    parts = []
    for p in raw:
        if not p.strip():
            continue
        match = SPELLED_CLAUSE_RE.fullmatch(p.strip())
        if match is None:
            raise Unknown(
                f"whitespace inside a specifier token, or no operator, in {p.strip()!r};"
                " uv rejects the file"
            )
        parts.append(match.group(1) + match.group(2))
    return ",".join(
        sorted(
            clause for p in parts for clause in norm_clause(p, expand_compatible=expand_compatible)
        )
    )


# A release-only bound: (release key with trailing zeros dropped, inclusive), or None for
# unbounded. An interval is (lower, upper); a specifier is a union of intervals.
_Bound = tuple[tuple[int, ...], bool] | None
_Interval = tuple[_Bound, _Bound]


def python_specs_equivalent(want: str, have: str) -> bool:
    """requires-python compared the way `uv lock --check` accepts an existing lock: by
    the BOUNDING RANGE of the release-only range each side admits (its lowest bound and
    its highest, inclusive or exclusive), never by full meaning. Measured uv 0.8.17,
    Codex P2 on #3763, rounds 12 and 54: `>=3.11,!=3.12.*`, `>=3.11,!=3.11.2`,
    `>=3.11,!=3.12.*,!=3.13.*`, `>=3.11,!=4.*` and `>=3.11rc1` each exit 0 against a
    lock recording `>=3.11` (the holes and the pre-release are not compared), as do
    `>=3.11,!=3.12.*,<4` against `>=3.11, <4`, `>=3.11,<4` against `>=3.11, !=3.12.*, <4`
    (a hole only the lock has), `>=3.11,!=3.11.0,!=3.11.1` against `>3.11.0` and
    `>=3.11,<3.12` against `==3.11.*`; while a hole that moves a bound is a change:
    `>=3.11,!=3.11.0` (lower becomes exclusive), `>=3.11,!=3.11.*` (lower becomes 3.12)
    and `>=3.11,!=3.13.*,<3.13` (upper 3.13 against a lock's `<4`) each exit 1. Comparing
    every clause by meaning (round 12) had reported `>=3.11,!=3.12.*` stale while uv
    kept the lock. A fresh `uv lock` writes the holes it can spell, so the lock a
    contributor commits after such an edit still passes here: its bounds are the same.
    An empty range is UNKNOWN ("Found conflicting Python requirements", exit 2); a
    missing lower bound is only a uv warning and compares like any other bound."""
    if not want or not have:
        return want == have
    return _bounding_range(want) == _bounding_range(have)


def _bounding_range(spec: str) -> _Interval:
    """The lowest and highest bound of the union of intervals `spec` admits, from the
    clauses AS SPELLED: `norm_spec` trims a wildcard's trailing zeros, but uv keeps
    its width (`>=3.12,!=3.12.0.*` is `>=3.12.1`, not `>=3.13`; measured round 54)."""
    norm_spec(spec, expand_compatible=True)  # UNKNOWN for a spelling uv rejects
    intervals: list[_Interval] = [(None, None)]
    for clause in spec.split(","):
        match = SPELLED_CLAUSE_RE.fullmatch(clause.strip())
        assert match is not None, clause  # norm_spec accepted it
        # Two sorted unions of disjoint intervals met in this nesting stay sorted, so
        # the first interval's lower bound and the last's upper bound are the extremes.
        intervals = [
            met
            for one in intervals
            for other in _clause_intervals(match.group(1), match.group(2))
            if (met := _meet(one, other)) is not None
        ]
    if not intervals:
        raise Unknown(f"requires-python {spec!r} admits no version; uv refuses to lock")
    return intervals[0][0], intervals[-1][1]


def _clause_intervals(op: str, version: str) -> list[_Interval]:
    """The release-only intervals one clause admits (pre-, post- and dev-release cut,
    as uv cuts them: `>=3.11rc1` and `!=3.12.0rc1` are `>=3.11` and `!=3.12.0`)."""
    if op == "===":
        raise Unknown(f"requires-python `==={version}`: arbitrary equality is not compared")
    if "!" in version:
        raise Unknown(f"requires-python `{op}{version}`: an epoch is not compared")
    if op == "~=":
        low = _key(_release_parts(release_literal(version)))
        return [((low, True), (_key(_release_parts(compatible_upper(version))), False))]
    if op in ("==", "!=") and version.endswith(".*"):
        parts = _release_parts(release_literal(version[:-2]))
        low, high = _key(parts), _key((*parts[:-1], parts[-1] + 1))
        if op == "==":
            return [((low, True), (high, False))]
        return [(None, (low, False)), ((high, True), None)]
    key = _key(_release_parts(release_literal(version)))
    by_op: dict[str, list[_Interval]] = {
        ">=": [((key, True), None)],
        ">": [((key, False), None)],
        "<": [(None, (key, False))],
        "<=": [(None, (key, True))],
        "==": [((key, True), (key, True))],
        "!=": [(None, (key, False)), ((key, False), None)],
    }
    return by_op[op]


def _key(parts: tuple[int, ...]) -> tuple[int, ...]:
    """A release with trailing zeros dropped, so 3.11 and 3.11.0 are one bound."""
    while len(parts) > 1 and parts[-1] == 0:
        parts = parts[:-1]
    return parts


def _meet(one: _Interval, other: _Interval) -> _Interval | None:
    """The intersection of two intervals, or None when empty (a lower bound above an
    upper, or the same version with either side exclusive)."""
    lows = [b for b in (one[0], other[0]) if b is not None]
    highs = [b for b in (one[1], other[1]) if b is not None]
    low = max(lows, key=lambda b: (b[0], not b[1])) if lows else None
    high = min(highs, key=lambda b: (b[0], b[1])) if highs else None
    if (
        low is not None
        and high is not None
        and (low[0] > high[0] or (low[0] == high[0] and not (low[1] and high[1])))
    ):
        return None
    return low, high


def norm_marker(marker: str | None) -> tuple[str, ...]:
    """The marker's top-level `and` clauses, each `lhs op rhs` with single spaces, sorted;
    a marker with `or` or parentheses is kept whole (re-spaced) for the semantic compare.
    Split on TOKENS, not text: `os_name == 'posix and stuff'` is one clause, and uv
    records that literal verbatim (Codex P2 on #3763, round 8)."""
    if marker is None or not marker.strip():
        return ()
    # Parse before any spelling compare: a marker uv refuses ("Expected a quoted
    # string or a valid marker name, found `made_up`", `uv lock --check` exit 2;
    # measured uv 0.8.17, Codex P2 on #3763, round 31) that both files spell the
    # same way matched exactly and never reached the parser.
    _MarkerParser(marker).parse()
    toks = tokenize_marker(marker)
    if any(tok == ("word", "or") or tok[0] == "lp" for tok in toks):
        return (" ".join(text for _kind, text in toks),)
    clauses: list[list[tuple[str, str]]] = [[]]
    for tok in toks:
        if tok == ("word", "and"):
            clauses.append([])
        else:
            clauses[-1].append(tok)
    out: list[str] = []
    for clause in clauses:
        if len(clause) == 4 and clause[1:3] == [("word", "not"), ("word", "in")]:
            out.append(f"{clause[0][1]} not in {clause[3][1]}")
        elif len(clause) == 3 and (clause[1][0] == "op" or clause[1] == ("word", "in")):
            out.append(f"{clause[0][1]} {clause[1][1]} {clause[2][1]}")
        else:
            raise Unknown(f"unparseable marker clause in {marker!r}")
    return tuple(sorted(out))
