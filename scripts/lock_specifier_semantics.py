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
    canonical_version,
    markers_equivalent,
    release_literal,
)

CLAUSE_RE = re.compile(r"^(?P<op>===|==|!=|<=|>=|<|>|~=)\s*(?P<version>.+)$")


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


def norm_spec(spec: str, *, expand_compatible: bool = False) -> str:
    parts = [p.strip().replace(" ", "") for p in spec.split(",") if p.strip()]
    return ",".join(
        sorted(
            clause for p in parts for clause in norm_clause(p, expand_compatible=expand_compatible)
        )
    )


def python_specs_equivalent(want: str, have: str) -> bool:
    """requires-python compared BY MEANING: uv records the tightest bounds, not the
    spelling (measured with uv 0.8.17: `>=3.10,>=3.11,<4,<5` -> `>=3.11, <4`,
    `>3.10,>=3.11` -> `>=3.11`, `>=3.11,==3.12.*` -> `==3.12.*`), while a
    dependency's specifier it keeps verbatim. Each side becomes the marker
    `python_full_version <op> '<version>'` per clause and the two go through the
    marker evaluator's probe grid (Codex P2 on #3763, round 12). A missing side is
    a difference."""
    normed = [norm_spec(want, expand_compatible=True), norm_spec(have, expand_compatible=True)]
    if normed[0] == normed[1]:
        return True
    if not normed[0] or not normed[1]:
        return False
    markers = []
    for spec in normed:
        clauses = []
        for clause in spec.split(","):
            match = CLAUSE_RE.match(clause)
            assert match is not None, clause  # _norm_spec emitted it
            clauses.append(f"python_full_version {match.group('op')} '{match.group('version')}'")
        markers.append(tuple(clauses))
    return markers_equivalent(markers[0], markers[1])


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
