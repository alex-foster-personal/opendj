"""A parameter renamed for a lint rule must not strand its keyword callers.

Ruff's ARG rules (unused argument) are satisfied by prefixing a parameter with
an underscore. That is correct for a private helper and WRONG for a public
signature: the parameter's name IS the interface, so renaming `state` to
`_state` turns every `f(state=...)` call site into a TypeError at run time,
which nothing catches until that code path executes in production.

Commit 17c7e99da (PR #2534, "pay down ruff lint debt") did exactly this to at
least ten public parameters. Measured on Sat 19 Sep 2026, the damage that had
reached main:

  - `apps.sets.record.status`'s `state` -> 500 on every GET /api/v1/sets/recorder,
    which the performance page polls, so the e2e gate failed on every PR in the
    repo, in all four of its steps.
  - `RescueStore.list_entries`'s `now_ms` -> TypeError from two call sites in
    its own module, failing all 11 rescue API tests.
  - `apps.sync.analysis_writeback.run_undo`'s `rb_db_path` -> `apply_analysis
    --undo` raised TypeError in production.
  - plus `compute_features`, two `live_run`s, `compare`, `run_ratings_apply`,
    `assert_merge_safe` and `bundle_remote_size`.

None of them was caught by review, by ruff, or by mypy: the call sites are
keyword arguments, and a renamed parameter is a valid signature.

So this test walks every function in `apps/` that has a leading-underscore
parameter, and every call in `apps/` and `tests/` that passes that parameter's
bare name as a keyword to a function of that name. A match is either a real
breakage or a name collision the reader must look at; both deserve a line here.

Regression lines:
  - if a public parameter is underscored while a caller still passes its bare
    name then that call raises TypeError when it runs, so broken
  - if this test reports zero while a known-broken pair exists then the walk
    stopped matching and is protecting nothing, so broken
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEF_ROOTS = (REPO / "apps",)
CALL_ROOTS = (REPO / "apps", REPO / "tests")


def _underscored_params(roots: tuple[Path, ...]) -> dict[str, set[str]]:
    """Map function name -> bare names of its leading-underscore parameters."""
    found: dict[str, set[str]] = {}
    for root in roots:
        for path in root.rglob("*.py"):
            try:
                tree = ast.parse(path.read_text(errors="ignore"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                args = node.args
                for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
                    name = arg.arg
                    if name.startswith("_") and not name.startswith("__") and len(name) > 1:
                        found.setdefault(node.name, set()).add(name[1:])
    return found


def _stranded_callers(
    def_roots: tuple[Path, ...],
    call_roots: tuple[Path, ...],
    *,
    relative_to: Path,
) -> list[str]:
    """Calls passing the bare name of a parameter its definition underscored."""
    params = _underscored_params(def_roots)
    hits: set[str] = set()
    for root in call_roots:
        for path in root.rglob("*.py"):
            try:
                tree = ast.parse(path.read_text(errors="ignore"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if isinstance(func, ast.Attribute):
                    name = func.attr
                elif isinstance(func, ast.Name):
                    name = func.id
                else:
                    continue
                for keyword in node.keywords:
                    if keyword.arg in params.get(name, set()):
                        rel = path.relative_to(relative_to)
                        hits.add(
                            f"{rel}:{node.lineno} {name}({keyword.arg}=...) "
                            f"but the def takes _{keyword.arg}"
                        )
    return sorted(hits)


def test_no_caller_passes_the_bare_name_of_an_underscored_parameter() -> None:
    """[if] a caller passes a keyword the def underscored [then ⛔️] broken"""
    stranded = _stranded_callers(DEF_ROOTS, CALL_ROOTS, relative_to=REPO)
    assert not stranded, (
        "if a caller passes the bare name of a parameter its definition "
        "underscored then that call raises TypeError when it runs, and no "
        "linter or type checker sees it - broken:\n  " + "\n  ".join(stranded)
    )


def test_the_scanner_itself_finds_a_planted_breakage(tmp_path: Path) -> None:
    """[if] the real scanner reports zero on a planted breakage [then ⛔️] broken.

    This drives `_stranded_callers` ITSELF over a planted tree rather than
    re-implementing its check (P2 r4055614967). That distinction is the whole
    value of the test: a scanner that stopped traversing -- an empty root, a
    glob that no longer matches, an AST shape it no longer recognises -- would
    report a clean repo forever, and a canary with its own private copy of the
    logic would keep agreeing with it.

    The planted pair is the real shape of the `record.status` bug.
    """
    (tmp_path / "planted.py").write_text(
        "def status(*, _state=None):\n"
        "    return 1\n"
        "\n"
        "def caller():\n"
        "    return status(state=object())\n"
    )

    stranded = _stranded_callers((tmp_path,), (tmp_path,), relative_to=tmp_path)

    assert stranded == ["planted.py:5 status(state=...) but the def takes _state"], (
        "if the scanner does not report a planted, known-broken pair then the "
        "repo-wide test above is protecting nothing - broken, got: " + repr(stranded)
    )


def test_the_production_roots_actually_point_at_code() -> None:
    """[if] the configured roots are empty or absent [then ⛔️] broken.

    The canary above proves the scanner still works. It cannot prove the
    scanner is still being POINTED anywhere, because it supplies its own roots.
    Emptying `DEF_ROOTS` or `CALL_ROOTS` leaves the repo-wide test green on a
    walk over nothing and leaves the canary green too, which was measured
    directly (P2 r4055667602): both globals set to `()` and both tests passed.

    So assert the configuration itself, and assert it has reach: a root that
    exists but holds no Python is the same silent nothing as a missing one.
    """
    for label, roots in (("DEF_ROOTS", DEF_ROOTS), ("CALL_ROOTS", CALL_ROOTS)):
        assert roots, f"{label} is empty, so the scan walks nothing - broken"
        for root in roots:
            assert root.is_dir(), f"{label} entry {root} is not a directory - broken"
            assert next(root.rglob("*.py"), None) is not None, (
                f"{label} entry {root} contains no Python, so it contributes "
                "nothing to the scan - broken"
            )
    assert REPO / "apps" in DEF_ROOTS, (
        "DEF_ROOTS no longer covers apps/, where the public signatures this "
        "test exists for live - broken"
    )


def test_the_repo_wide_scan_reaches_a_known_underscored_signature() -> None:
    """[if] the walk finds no underscored parameter at all [then ⛔️] broken.

    The strongest available statement that the scan has reach, short of
    planting a breakage in production code. `_stranded_callers` reports only
    stranded PAIRS, and a clean repo correctly reports none -- so "zero" is
    both the healthy answer and the answer a scan over nothing gives. The
    underscored definitions it collects on the way are not zero, and must not
    be: 17c7e99da alone left ten of them in `apps/`.
    """
    underscored = _underscored_params(DEF_ROOTS)

    assert underscored, (
        "the walk over apps/ found no leading-underscore parameter anywhere. "
        "Either the roots stopped resolving or the AST shape changed; either "
        "way the repo-wide test above is scanning nothing - broken"
    )
