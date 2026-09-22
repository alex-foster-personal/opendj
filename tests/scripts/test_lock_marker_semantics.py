"""scripts/lock_marker_semantics.py: PEP 508 markers compared BY MEANING, the way uv
records them (Codex rounds 4-11 on PR #3763). The check()-level tests through the
fixture pair live in test_lock_metadata_check.py; these drive the comparator directly.
"""

from __future__ import annotations

import re

import pytest

from scripts.lock_marker_semantics import (
    Unknown,
    _eval,
    _grid,
    _MarkerParser,
    markers_equivalent,
    release_literal,
    tokenize_marker,
)


def _compares_variables(marker: str) -> bool:
    """Whether any clause compares two marker VARIABLES (`os_name != sys_platform`)."""

    def walk(node: tuple) -> bool:
        if node[0] in ("and", "or"):
            return walk(node[1]) or walk(node[2])
        return node[0] == "cmp" and node[1][0] == "word" and node[3][0] == "word"

    return walk(_MarkerParser(marker).parse())


def _has_membership(marker: str) -> bool:
    """Whether any clause is `in` / `not in`: an opaque atom to uv (and to the
    evaluator), which packaging reads as substring membership, so no oracle."""
    return ("word", "in") in tokenize_marker(marker)


def _orders_a_string(marker: str) -> bool:
    ordered = re.findall(r"([a-z_]+)\s*(?:<=|>=|<|>)\s*'", marker)
    return any(
        var not in ("python_version", "python_full_version", "implementation_version")
        for var in ordered
    )


def _as_uv_stores_it(marker: str) -> str:
    """The marker with every version literal cut to its release, which is the spelling
    uv records (scripts/lock_marker_semantics.py, measured table). Over release-only
    environments packaging's PEP 440 evaluation of THAT spelling is uv's semantics."""
    return re.sub(
        r"(python_full_version|python_version|implementation_version)\s*"
        r"(===|==|!=|<=|>=|<|>|~=)\s*'([^']*)'",
        lambda m: f"{m.group(1)} {m.group(2)} '{release_literal(m.group(3))}'",
        marker,
    )


@pytest.mark.parametrize(
    ("spelled", "recorded", "same"),
    [
        ("python_version < '3.11'", "python_full_version < '3.11'", True),
        ("python_version >= '3.10'", "python_full_version >= '3.10'", True),
        ("python_version <= '3.10'", "python_full_version < '3.11'", True),
        ("python_version > '3.10'", "python_full_version >= '3.11'", True),
        ("python_version == '3.10'", "python_full_version == '3.10.*'", True),
        (
            "python_version == '3.10'",
            "python_full_version >= '3.10' and python_full_version < '3.11'",
            True,
        ),
        ("python_version != '3.10'", "python_full_version != '3.10.*'", True),
        (
            "(sys_platform == 'win32' or sys_platform == 'darwin') and extra == 'dev'",
            "(extra == 'dev' and sys_platform == 'darwin') "
            "or (extra == 'dev' and sys_platform == 'win32')",
            True,
        ),
        ("python_version < '3.11'", "python_full_version < '3.12'", False),
        ("sys_platform == 'darwin'", "sys_platform != 'darwin'", False),
        (
            "sys_platform == 'darwin'",
            "sys_platform == 'darwin' or platform_machine == 'arm64'",
            False,
        ),
        ("python_full_version < '3.11.3'", "python_full_version < '3.11.4'", False),
        # Only a NEIGHBOR of a mentioned literal separates these two: every literal
        # itself evaluates the same on both sides, 3.10.100 does not.
        ("python_full_version < '3.11'", "python_full_version <= '3.10.99'", False),
        ("python_full_version >= '3.11'", "python_full_version > '3.10.99'", False),
        # uv cuts every version literal to its release before storing it (measured
        # against uv 0.8.17, table in scripts/lock_marker_semantics.py), so a fresh lock
        # spells `<= '3.11rc1'` as `<= '3.11'`: still not `< '3.11'`, they part at 3.11.
        ("python_full_version < '3.11'", "python_full_version <= '3.11rc1'", False),
        ("python_full_version < '3.11'", "python_full_version < '3.11rc0'", True),
        ("python_full_version < '3.11rc1'", "python_full_version < '3.11'", True),
        ("python_full_version > '3.13.0b2'", "python_full_version > '3.13'", True),
        ("python_full_version < '3.11.post1'", "python_full_version < '3.11'", True),
        ("python_full_version == '3.11rc1'", "python_full_version == '3.11'", True),
        ("python_full_version == '3.11.0'", "python_full_version == '3.11'", True),
        # A wildcard keeps its width: uv records `3.11.0.*` as the 3.11.0.x range
        # (Codex P2 on #3763, round 7), and it is NOT `3.11.*`.
        (
            "python_full_version == '3.11.0.*'",
            "python_full_version >= '3.11.0' and python_full_version < '3.11.1'",
            True,
        ),
        (
            "python_full_version != '3.11.0.*'",
            "python_full_version < '3.11.0' or python_full_version >= '3.11.1'",
            True,
        ),
        ("python_full_version == '3.11.0.*'", "python_full_version == '3.11.*'", False),
        ("python_version <= '3.11rc1'", "python_full_version < '3.12'", True),
        ("implementation_version < '3.11rc1'", "implementation_version < '3.11'", True),
        # uv ERASES a comparison between two variables from an `and` and from an
        # `or` alike, and drops a marker made only of them (measured, same table;
        # Codex P2 on #3763, round 10). Erased, not true: `X or <erased>` is X.
        ("os_name != sys_platform", "", True),
        ("os_name in sys_platform", "", True),
        (
            "python_version >= '3.10' and os_name != sys_platform",
            "python_full_version >= '3.10'",
            True,
        ),
        ("os_name != sys_platform or sys_platform == 'win32'", "sys_platform == 'win32'", True),
        (
            "(os_name != sys_platform) and python_version < '3.99'",
            "python_full_version < '3.99'",
            True,
        ),
        ("os_name != sys_platform or sys_platform == 'win32'", "", False),
        ("os_name != sys_platform and sys_platform == 'win32'", "", False),
        ("'posix' == os_name", "os_name == 'posix'", True),
        # `~=` keeps its literal's width: uv records `~= '3.10.0'` as `== '3.10.*'` and
        # `~= '3.10.0.0'` as the 3.10.0.x range (Codex P2 on #3763, round 11). The
        # upper bound is a probe of its own, or `~= '3.10.0'` and `~= '3.10'` never part.
        ("python_full_version ~= '3.10.0'", "python_full_version == '3.10.*'", True),
        (
            "python_full_version ~= '3.10.0.0'",
            "python_full_version >= '3.10' and python_full_version < '3.10.1'",
            True,
        ),
        (
            "python_version ~= '3.10'",
            "python_full_version >= '3.10' and python_full_version < '4'",
            True,
        ),
        ("implementation_version ~= '3.10.0'", "implementation_version == '3.10.*'", True),
        ("python_full_version ~= '3.10.0'", "python_full_version ~= '3.10'", False),
        ("python_full_version ~= '3.10.0'", "python_full_version == '3.10.0.*'", False),
        # The unmentioned-value probe is derived from the literals, so a marker that
        # names the old fixed sentinel is still told apart from no marker (round 12).
        ("sys_platform == 'zz-no-literal-mentions-this'", "", False),
        ("sys_platform in 'zz-no-literal-mentions-this-and-more'", "", False),
        ("sys_platform != 'zz-no-literal-mentions-this'", "", False),
        # The probe above the largest literal is derived from it, so a boundary past
        # the old fixed ceiling still gets a probe above it (round 13).
        ("python_full_version <= '10000'", "", False),
        ("python_full_version > '9999'", "", False),
        ("python_full_version < '99999'", "python_full_version < '99998'", False),
        ("python_full_version >= '10000'", "python_full_version > '9999.9'", False),
        # uv drops an epoch too (measured, same table).
        ("python_full_version <= '1!3'", "python_full_version <= '3'", True),
        ("python_full_version >= '1!3.11'", "python_full_version >= '3.11'", True),
        (
            "python_full_version ~= '3.11.1'",
            "python_full_version >= '3.11.1' and python_full_version < '3.12'",
            True,
        ),
        # Strings order lexically too (packaging and uv both compare them so): only
        # a value strictly BETWEEN 'linux' and 'win32' separates these, e.g. 'netbsd'.
        ("sys_platform <= 'linux'", "sys_platform < 'win32'", False),
        # uv's own rewrites of ordered string markers (measured, same table):
        ("sys_platform < 'win32'", "sys_platform < 'win32' and sys_platform != 'win32'", True),
        ("sys_platform > 'linux'", "sys_platform >= 'linux' and sys_platform != 'linux'", True),
        ("sys_platform <= 'linux'", "sys_platform < 'linux' or sys_platform == 'linux'", True),
        ("sys_platform < 'win32' or sys_platform >= 'win32'", "", True),
        ("sys_platform < 'win32' or sys_platform > 'win32'", "", False),
        # `in` / `not in` is an OPAQUE atom to uv: it never relates the literal's
        # substrings to `==`, so `os_name in 'ab'` is not the disjunction of its
        # substrings (Codex P2 on #3763, round 23); `not in` negates the same atom, a
        # literal on the left is another atom, and the atom is not reduced against `==`.
        ("sys_platform in 'linux,darwin'", "sys_platform == 'linux,darwin'", False),
        (
            "sys_platform in 'linux,darwin'",
            "sys_platform == 'linux' or sys_platform == 'darwin'",
            False,
        ),
        ("sys_platform not in 'win32'", "sys_platform != 'win32'", False),
        (
            "os_name in 'ab'",
            "os_name == '' or os_name == 'a' or os_name == 'b' or os_name == 'ab'",
            False,
        ),
        ("os_name in 'ab' and os_name not in 'ab'", "python_version < '0'", True),
        ("os_name in 'ab' or os_name not in 'ab'", "", True),
        ("os_name in 'ab' or os_name in 'ab'", "os_name in 'ab'", True),
        ("os_name in 'ab' or os_name == 'a'", "os_name in 'ab'", False),
        ("os_name in 'ab' and os_name == 'c'", "python_version < '0'", False),
        ("'a' in os_name or os_name == 'a'", "'a' in os_name", False),
        ("'a' in os_name", "os_name in 'a'", False),
        ("os_name in 'ab' and 'ab' in os_name", "'ab' in os_name and os_name in 'ab'", True),
        ("sys_platform in 'linux' and sys_platform < 'linux'", "sys_platform < 'linux'", False),
        ("'lin' in sys_platform", "sys_platform == 'linux'", False),
        ("python_full_version >= '3.10'", "python_full_version >= '3.10.dev0'", True),
        ("python_full_version > '3.10'", "python_full_version > '3.10.post1'", True),
    ],
)
def test_markers_compare_by_meaning(spelled: str, recorded: str, same: bool) -> None:
    from packaging.markers import Marker

    assert markers_equivalent((spelled,), (recorded,)) is same
    if not spelled or not recorded or _compares_variables(spelled) or _compares_variables(recorded):
        # uv ERASES a variable-to-variable comparison when it records a marker; packaging
        # evaluates it against the real environment, so it is not an oracle for that
        # rewrite (nor for an empty marker, which it cannot parse). The erasure has its
        # own check()-level test below.
        return
    if _has_membership(spelled) or _has_membership(recorded):
        return
    if _orders_a_string(spelled) or _orders_a_string(recorded):
        # packaging 26 evaluates `<`/`>` on strings as always false and `<=`/`>=` as
        # equality; uv orders them lexically (measured table in the module docstring),
        # and a uv.lock check follows uv. No packaging oracle for these rows; the
        # lexical semantics has its own test below.
        return
    uv_spelled, uv_recorded = _as_uv_stores_it(spelled), _as_uv_stores_it(recorded)
    # ORACLE over the checker's own probe grid, both directions: packaging, on the
    # spelling uv stores, must agree with our evaluator on EVERY probe environment (so
    # the evaluator is right), and for the false cases separate the two markers on at
    # least one (so the grid is complete).
    ast_a, ast_b = _MarkerParser(spelled).parse(), _MarkerParser(recorded).parse()
    envs = _grid(ast_a, ast_b)
    for env in envs:
        env.setdefault("platform_machine", "arm64")
        env.setdefault("extra", "dev")
        env.setdefault("sys_platform", "linux")
        env.setdefault("python_full_version", "3.11.0")
        env.setdefault("python_version", "3.11")
        for text, ast in ((uv_spelled, ast_a), (uv_recorded, ast_b)):
            assert _eval(ast, env) == Marker(text).evaluate(env), (text, env)
    if not same:
        assert any(
            Marker(uv_spelled).evaluate(env) != Marker(uv_recorded).evaluate(env) for env in envs
        ), (spelled, recorded, envs)
    # ORACLE for the true cases: packaging agrees on a hand-picked environment grid.
    if same:
        for full in ("3.9.7", "3.10.0", "3.10.12", "3.11.0", "3.11.3", "3.12.1", "3.13.0"):
            for platform in ("darwin", "win32", "linux", "netbsd"):
                env = {
                    "python_full_version": full,
                    "python_version": full.rsplit(".", 1)[0],
                    "implementation_version": full,
                    "sys_platform": platform,
                    "platform_machine": "x86_64",
                    "extra": "dev",
                }
                assert Marker(uv_spelled).evaluate(env) == Marker(uv_recorded).evaluate(env), (
                    spelled,
                    recorded,
                    env,
                )


@pytest.mark.parametrize(
    ("value", "below_win32", "at_most_linux"),
    [
        ("darwin", True, True),
        ("linux", True, True),
        ("netbsd", True, False),
        ("win32", False, False),
    ],
)
def test_string_ordering_is_lexical_as_uv_ranges_it(
    value: str, below_win32: bool, at_most_linux: bool
) -> None:
    """`sys_platform < 'win32'` holds for every platform that sorts before it, which is
    how uv's marker algebra ranges strings (it folds `< 'win32' and != 'win32'` to
    `< 'win32'`); 'netbsd' is the value between 'linux' and 'win32' that separates
    `<= 'linux'` from `< 'win32'`."""
    env = {"sys_platform": value}
    assert _eval(_MarkerParser("sys_platform < 'win32'").parse(), env) is below_win32
    assert _eval(_MarkerParser("sys_platform <= 'linux'").parse(), env) is at_most_linux


def test_a_local_version_in_a_marker_is_unknown_not_a_verdict() -> None:
    """uv dropped the marker outright for `>= '3.11.2+local'` (measured); that rewrite is
    not modeled, so the compare must say so rather than guess either way."""
    with pytest.raises(Exception, match="local version"):
        markers_equivalent(
            ("python_full_version >= '3.11.2+local'",), ("python_full_version >= '3.11.2'",)
        )


def test_a_one_component_compatible_release_is_unknown_not_a_verdict() -> None:
    """PEP 440 forbids `~= '3'` and uv drops the marker (measured); that rewrite is not
    modeled, so the compare must say so rather than guess either way."""
    with pytest.raises(Exception, match="at least two components"):
        markers_equivalent(("python_full_version ~= '3'",), ())


def test_a_compatible_release_on_a_string_variable_is_erased_as_uv_drops_it() -> None:
    """`~=` is a PEP 440 operator; on a string variable (`os_name ~= 'posix'`,
    `platform_release ~= '5.15'`) uv drops the clause rather than record it (measured,
    round 20): erased from a conjunction and a disjunction alike, never a truth value.
    On a version variable it stays the width range, whichever side the literal is on."""
    for spelled in ("os_name ~= 'posix'", "platform_release ~= '5.15'"):
        assert markers_equivalent((spelled,), ()), spelled
        with_and = (f"{spelled} and sys_platform == 'linux'",)
        assert markers_equivalent(with_and, ("sys_platform == 'linux'",)), spelled
        with_or = (f"sys_platform == 'linux' or {spelled}",)
        assert markers_equivalent(with_or, ("sys_platform == 'linux'",)), spelled
    assert not markers_equivalent(("implementation_version ~= '3.11'",), ())
    assert markers_equivalent(
        ("'3.11' ~= python_version",),
        ("python_full_version >= '3.11' and python_full_version < '4'",),
    )


def test_a_literal_left_compatible_release_on_a_string_variable_is_unknown() -> None:
    """`'posix' ~= os_name` is a marker uv cannot lock at all (uv 0.8.17 panics, exit
    101, measured round 20), so no record of it exists to compare with: UNKNOWN, never a
    verdict, wherever the clause sits."""
    for spelled in (
        "'posix' ~= os_name",
        "'posix' ~= os_name and sys_platform == 'linux'",
        "sys_platform == 'linux' or 'posix' ~= os_name",
    ):
        with pytest.raises(Unknown):
            markers_equivalent((spelled,), ())


def test_a_wildcard_uv_cannot_compare_is_erased_as_uv_drops_it() -> None:
    """A `.*` wildcard is a prefix pattern, meaningful only as the RIGHT operand of `==` /
    `!=` against a version variable. Anywhere else (`'3.11.*' == python_full_version`,
    `python_full_version < '3.11.*'`, `~=`, `in`) it is not a PEP 440 comparison, and uv
    drops the clause (measured, round 16): erased from a conjunction and a disjunction
    alike, never a crash. The wildcard equality uv keeps still compares, and a wildcard
    against a STRING variable is a plain string."""
    for spelled in (
        "'3.11.*' == python_full_version",
        "'3.11.*' != python_version",
        "'3.11.*' < python_full_version",
        "python_full_version < '3.11.*'",
        "python_full_version >= '3.11.*'",
        "python_full_version ~= '3.11.*'",
        "python_full_version in '3.11.*'",
    ):
        assert markers_equivalent((spelled,), ()), spelled
        with_and = (f"{spelled} and os_name == 'posix'",)
        assert markers_equivalent(with_and, ("os_name == 'posix'",)), spelled
        with_or = (f"{spelled} or os_name == 'posix'",)
        assert markers_equivalent(with_or, ("os_name == 'posix'",)), spelled
    for kept in (
        "python_full_version == '3.11.*'",
        "python_full_version != '3.11.*'",
        "os_name == '3.11.*'",
    ):
        assert not markers_equivalent((kept,), ()), kept


def test_a_probe_between_releases_of_any_depth_exists() -> None:
    """Between two distinct releases there is always a release (`lower.0...0.1` with one
    more component than the upper bound has), so bounds deeper than four components
    compare rather than read UNKNOWN (Codex P2 on #3763, round 21)."""
    deep = "python_full_version >= '3.11.15' and python_full_version < '3.11.15.0.0.0.0.1'"
    assert markers_equivalent((deep,), (deep,))
    assert not markers_equivalent((deep,), (deep.replace("0.0.0.0.1", "0.0.0.0.2"),))
    assert not markers_equivalent((deep,), ("python_full_version == '3.11.15'",))
