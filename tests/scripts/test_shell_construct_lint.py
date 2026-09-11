"""Mutation tests for scripts/shell_construct_lint.py.

A lint that cannot fail is exactly the class of defect this lint exists to stop,
so every rule here is proven in BOTH directions: the bad fixture must trip it and
the safe fixture must not.

The mutations remove the CAPABILITY, never the cosmetics. That distinction is the
whole point: construct (c) returned 0 lines on one run and 1 on the next from the
same command, so a check keyed on the VALUE passes half the time. Each rule below
therefore gets two mutations:

  * a REMOVAL, which deletes the construct and must go clean, and
  * a COSMETIC edit (rename the variable, change the path, change which zsh
    modifier is triggered), which leaves the construct in place and must STILL
    be flagged.

If a fixture does not trip the lint, suspect the fixture before the lint, then
verify which. Every fixture below was run through the real linter and, for the
zsh modifier rule, through a real zsh to confirm the mangling it describes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.shell_construct_lint import (
    CFG,
    discover,
    lint_file,
    lint_paths,
    main,
)

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures" / "shell_lint"

# rule -> (bad fixture, expected violation count, safe fixtures)
CASES: dict[str, tuple[str, int, tuple[str, ...]]] = {
    "pipeline-status": (
        "bad_pipeline_status.sh",
        1,
        ("good_pipeline_status.sh", "good_pipeline_status_pipefail.sh"),
    ),
    "gh-api-arg": ("bad_gh_api_arg.sh", 1, ("good_gh_api_arg.sh",)),
    "zsh-modifier-path": ("bad_zsh_modifier.sh", 2, ("good_zsh_modifier.sh",)),
    "status-after-substitution": (
        "bad_status_after_substitution.sh",
        3,
        ("good_status_after_substitution.sh",),
    ),
}

# rule -> (removal: every instance of the construct deleted,
#          cosmetic: the construct kept, everything observable about it changed)
Edits = list[tuple[str, str]]
MUTATIONS: dict[str, tuple[Edits, Edits]] = {
    "pipeline-status": (
        [("EXIT_CODE=$?", 'EXIT_CODE="${PIPESTATUS[0]}"')],
        [("EXIT_CODE=$?", "RC=$?")],
    ),
    "gh-api-arg": (
        [('--arg want "$2"', "")],
        [('--arg want "$2"', '--arg expected "$2"')],
    ),
    "zsh-modifier-path": (
        # The bad fixture carries both live instances, so the removal has to
        # brace both. Bracing one and calling it clean is the same mistake the
        # rule is about.
        [('"$SHA:apps/', '"${SHA}:apps/'), ('"$S:apps/', '"${S}:apps/')],
        # Cosmetics only: a different variable name, a different path, and a
        # different modifier letter (:a absolute-path -> :t tail), so every
        # observable value changes while the construct stays what it was.
        [('"$SHA:apps/webui/server/app.py"', '"$COMMIT:tests/scripts/app.py"')],
    ),
    "status-after-substitution": (
        # All three instances captured first, the shipped fix.
        [
            ('echo "[$(date', 'rc=$?; echo "[$(date'),
            ("returned rc=$?", "returned rc=$rc"),
            ('echo "`date +%s` rc=${?}"', 'rc=$?; echo "`date +%s` rc=${rc}"'),
            ("stamp=$(date +%s) rc=$?", "rc=$? stamp=$(date +%s)"),
        ],
        # Cosmetics only: a different command inside each substitution and a
        # different variable name, so the logged text changes and the order of
        # substitution-then-status does not.
        [
            ("date '+%H:%M:%S'", "hostname"),
            ("`date +%s`", "`uname -n`"),
            ("stamp=$(date +%s) rc=$?", "host=$(uname -n) status=$?"),
        ],
    ),
}


def _rules(path: Path) -> list[str]:
    return [v.rule for v in lint_file(path)]


def _mutate(name: str, edits: Edits, dest: Path) -> Path:
    """Write a mutated copy of a fixture, failing loudly if an edit matched nothing."""
    source = (FIXTURES / name).read_text(encoding="utf-8")
    for old, new in edits:
        assert old in source, f"mutation target {old!r} missing from {name}"
        source = source.replace(old, new)
    mutated = dest / name
    mutated.write_text(source, encoding="utf-8")
    return mutated


# ----- fixture-fails / fixture-passes --------------------------------------


@pytest.mark.parametrize("rule", sorted(CASES))
def test_bad_fixture_trips_its_rule(rule: str) -> None:
    """The lint FAILS on the bad form. Without this, nothing else here means anything."""
    name, count, _ = CASES[rule]
    found = _rules(FIXTURES / name)
    assert found == [rule] * count, f"{name}: expected {count}x {rule}, got {found}"


@pytest.mark.parametrize(
    "name", sorted({n for _, _, safe in CASES.values() for n in safe})
)
def test_good_fixture_passes(name: str) -> None:
    """The lint is quiet on the safe form, so it is usable rather than disabled."""
    assert lint_file(FIXTURES / name) == []


# ----- mutation: capability, not cosmetics ---------------------------------


@pytest.mark.parametrize("rule", sorted(MUTATIONS))
def test_removing_the_construct_clears_the_rule(
    rule: str, tmp_path: Path
) -> None:
    """Deleting the construct itself must go clean -- the lint keys on the construct."""
    name, _, _ = CASES[rule]
    assert rule not in _rules(_mutate(name, MUTATIONS[rule][0], tmp_path))


@pytest.mark.parametrize("rule", sorted(MUTATIONS))
def test_cosmetic_edits_do_not_hide_the_construct(
    rule: str, tmp_path: Path
) -> None:
    """Renaming variables and changing paths must NOT clear the rule.

    This is the guard against a value-based check. The zsh case swaps which
    modifier fires, so the mangled output is entirely different, and the lint
    must still flag it.
    """
    name, count, _ = CASES[rule]
    assert _rules(_mutate(name, MUTATIONS[rule][1], tmp_path)) == [rule] * count


# ----- the scan itself ------------------------------------------------------


@pytest.mark.parametrize("rule", sorted(CASES))
def test_discovery_finds_a_planted_violation(rule: str, tmp_path: Path) -> None:
    """A bad file at a DISCOVERED path is caught.

    tests/fixtures/shell_lint/ is excluded from discovery so the fixtures do not
    red the gate they exist to prove. This test is what stops that exclusion from
    being the reason the repo scans clean: the same bytes at an ordinary path are
    found by the walk, not just by an explicit argument.
    """
    name, count, _ = CASES[rule]
    planted = tmp_path / "scripts" / "planted.sh"
    planted.parent.mkdir(parents=True)
    planted.write_text((FIXTURES / name).read_text(encoding="utf-8"), encoding="utf-8")
    assert discover(tmp_path) == [planted]
    assert [v.rule for v in lint_paths(discover(tmp_path))] == [rule] * count


def test_justfile_recipes_are_in_scope() -> None:
    """The repo justfile is scanned, and as recipe bodies rather than just syntax."""
    files = discover(REPO)
    assert REPO / "justfile" in files
    from scripts.shell_construct_lint import _justfile_shell_only

    kept, block_of = _justfile_shell_only((REPO / "justfile").read_text(encoding="utf-8"))
    assert len([line for line in kept.split("\n") if line.strip()]) > 50
    # Recipes are separate shells, so pipefail in one must not exempt the next.
    assert len(set(block_of.values())) > 10


def test_repo_scan_is_clean_over_a_real_file_set() -> None:
    """The gate itself: zero violations, over a file set proven non-empty.

    The floor is the point. A clean scan over nothing looks exactly like a clean
    scan over everything, so the count is asserted before the verdict is trusted.
    """
    files = discover(REPO)
    assert len(files) >= CFG.MIN_FILES, f"scan collapsed to {len(files)} files"
    violations = lint_paths(files)
    assert violations == [], "\n".join(v.render(REPO) for v in violations)


def test_empty_scan_aborts_instead_of_reporting_pass(tmp_path: Path) -> None:
    """S-04: an unmeasurable subject reports UNKNOWN, never a verdict."""
    with pytest.raises(SystemExit) as excinfo:
        main(["--root", str(tmp_path)])
    assert "ABORT" in str(excinfo.value)


def test_main_exit_codes(tmp_path: Path) -> None:
    """Exit 1 on a violation, 0 on a clean file: what CI actually reads."""
    assert main([str(FIXTURES / "bad_zsh_modifier.sh")]) == 1
    assert main([str(FIXTURES / "good_zsh_modifier.sh")]) == 0
