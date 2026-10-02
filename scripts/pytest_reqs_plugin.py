"""Pytest plugin — builds ``coverage-matrix.md`` from ``@pytest.mark.requirement``.

Loaded from ``conftest.py`` at repo root via ``pytest_plugins``. The plugin:

  * Adds ``--no-coverage-matrix`` to suppress output.
  * Collects every ``@pytest.mark.requirement("<ID>")`` marker during
    collection (both module-level and parametrize-level are honored).
  * On session finish, writes ``coverage-matrix.md`` with one row per
    requirement: IDs are sourced from ``reqs.json`` so orphan markers
    (ID not in reqs.json) get flagged with ❌.
  * Also flags requirements with zero covering tests (⚠️).

The output is a **generated artifact** and is gitignored: any subset run
(``pytest tests/sync``, ``-k foo``) rewrites it, so a committed copy is
stale the moment anyone runs a partial suite. CI publishes the full-run
copy as the ``coverage-matrix`` artifact instead.

Because a partial run's totals still *read* as authoritative, every
generated file states its own run scope up front and a filtered or
non-clean run is stamped with a warning banner. This is the repo's
honest-denominator rule (``CLAUDE.md``) applied to traceability: never
quote a coverage figure without naming what it was measured over.

No coverage data is read here — this is purely **traceability**, not line
coverage. Line coverage runs separately via ``pytest --cov``.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

REPO_ROOT: Path = Path(__file__).resolve().parents[1]
REQS_JSON: Path = REPO_ROOT / "reqs.json"
OUTPUT: Path = REPO_ROOT / "coverage-matrix.md"

# Node ids whose collection failed this session. Populated by
# ``pytest_collectreport``; read once by ``pytest_sessionfinish``.
_COLLECT_ERRORS: list[str] = []


def pytest_addoption(parser) -> None:  # type: ignore[no-untyped-def]
    parser.addoption(
        "--no-coverage-matrix",
        action="store_true",
        default=False,
        help="Skip writing coverage-matrix.md after the session.",
    )
    parser.addoption(
        "--live-db",
        action="store_true",
        default=False,
        help="Run tests marked live_db (off by default to protect live DBs).",
    )
    parser.addoption(
        "--run-integration",
        action="store_true",
        default=False,
        help="Run tests marked integration (Phase 4 smoke; off by default).",
    )
    parser.addoption(
        "--collect-floor",
        type=int,
        default=0,
        metavar="N",
        help=(
            "Fail the session unless at least N tests were COLLECTED. "
            "Opt-in, so scoped runs are unaffected; the full-suite gate and "
            "CI pass it so a run can never silently shrink. Refuses to run "
            "alongside -k or -m, which would make a deselected run look full."
        ),
    )


def _fail(message: str) -> None:
    """Abort the session with a reason that survives xdist.

    An xdist WORKER raising during collection has its exception swallowed into
    a controller-side ``INTERNALERROR`` traceback with the reason stripped out,
    so the message goes to stderr on its own first. A gate that fails without
    saying why is worse than no gate.
    """
    import pytest

    print(f"ERROR: {message}", file=sys.stderr, flush=True)
    pytest.exit(message, returncode=4)


def _assert_collect_floor(config, items) -> None:  # type: ignore[no-untyped-def]
    """Fail fast if fewer tests were collected than the caller demanded.

    This is the control that keeps parallel execution honest. ``-n`` changes
    how tests are distributed, never which ones exist, so the collected count
    is the invariant that proves it: an environment that imported less of the
    tree (a missing optional dep aborting a module, a deleted directory)
    collects fewer items and is caught here rather than reported as a smaller
    green run. Counted BEFORE any skip marker is applied, so a test that is
    skipped still counts as collected -- this floor answers "did the suite
    shrink", which is a different question from "did it run".

    ``-k`` and ``-m`` are REJECTED while a floor is active rather than counted
    around. Both deselect inside pytest's own
    ``pytest_collection_modifyitems``, so whether this hook sees the items
    before or after them is a hook-ordering detail nobody should have to
    reason about -- and a filtered run must never be able to present itself as
    the protected full suite. Want a filter? Drop the floor, and own the fact
    that you ran a subset.

    Under ``-n`` the controller does not collect at all: every worker collects
    the full tree and runs a slice of it, so this runs once per worker on the
    same total. xdist separately aborts when workers DISAGREE about what they
    collected, so that case is already covered; the floor catches the case
    where every worker consistently collected too little.
    """
    floor = int(config.getoption("--collect-floor") or 0)
    if floor <= 0:
        return

    selectors = {
        "-k": str(getattr(config.option, "keyword", "") or ""),
        "-m": str(getattr(config.option, "markexpr", "") or ""),
    }
    active = [flag for flag, value in selectors.items() if value]
    if active:
        _fail(
            f"collect floor: {' and '.join(active)} cannot be combined with "
            "--collect-floor. The floor exists to prove the FULL suite ran; a "
            "deselected run that satisfies it would be exactly the false green "
            "it is meant to catch. Run the subset without the floor."
        )
        return

    if len(items) < floor:
        _fail(
            f"collect floor: {len(items)} tests collected, at least {floor} "
            "required. The suite shrank, or this environment failed to import "
            "part of it. Fix the cause or lower the floor deliberately -- "
            "never quietly."
        )


def pytest_collection_modifyitems(config, items) -> None:  # type: ignore[no-untyped-def]
    """Assert the collect floor, then apply the opt-in skip gates.

    Skips ``live_db`` tests unless ``--live-db`` was passed, and ``integration``
    tests unless ``--run-integration`` or ``pytest -m integration`` was
    explicitly passed.
    """
    import pytest

    _assert_collect_floor(config, items)

    if not config.getoption("--live-db"):
        skip_live = pytest.mark.skip(
            reason="needs --live-db; this test touches a live DB"
        )
        for item in items:
            if "live_db" in item.keywords:
                item.add_marker(skip_live)

    # If -m integration or --run-integration is set, run them; otherwise skip.
    markexpr = str(getattr(config.option, "markexpr", "") or "")
    run_integration = (
        config.getoption("--run-integration") or "integration" in markexpr
    )
    if not run_integration:
        skip_int = pytest.mark.skip(
            reason="integration test -- run via `make integration` or -m integration"
        )
        for item in items:
            if "integration" in item.keywords:
                item.add_marker(skip_int)


# ------------------------------------------------------------------ collect


def _collect_markers(items) -> dict[str, list[str]]:  # type: ignore[no-untyped-def]
    """Return ``{requirement_id: [test_nodeid, …]}`` from collected items."""
    mapping: dict[str, list[str]] = defaultdict(list)
    for item in items:
        for marker in item.iter_markers(name="requirement"):
            if not marker.args:
                continue
            rid = str(marker.args[0])
            mapping[rid].append(item.nodeid)
    return mapping


def _load_reqs() -> dict[str, dict[str, Any]]:
    """Return ``{requirement_id: {desc, status, phase, category}}``."""
    if not REQS_JSON.exists():
        return {}
    data = json.loads(REQS_JSON.read_text(encoding="utf-8"))
    out: dict[str, dict[str, Any]] = {}
    for bucket_name in ("v1", "v1.1", "v2", "v3"):
        bucket = data.get(bucket_name, {})
        for code, cat in bucket.items():
            for req in cat.get("requirements", []):
                out[req["id"]] = {
                    "desc": req.get("desc", ""),
                    "status": req.get("status", "pending"),
                    "phase": req.get("phase"),
                    "category": f"{bucket_name}/{code}",
                }
    return out


# ------------------------------------------------------------------ scope


def pytest_collectreport(report) -> None:  # type: ignore[no-untyped-def]
    """Record collection failures.

    A file that fails to import contributes no items, so its markers vanish
    and its requirements read as uncovered -- indistinguishable from having
    no tests at all. ``tests/vocals/test_envelope.py`` and
    ``test_farm_scheduling.py`` import ``modal`` at module scope and do
    exactly this when it is absent, which is why the matrix has to say so.
    Test *failures* are irrelevant here: markers are gathered at collection
    time, so a red suite still yields a complete matrix.
    """
    if report.failed:
        _COLLECT_ERRORS.append(str(report.nodeid or "<root>"))


def _run_scope(config, n_items: int) -> tuple[bool, str]:  # type: ignore[no-untyped-def]
    """Return ``(is_full_clean_run, human_description)`` for this invocation.

    A filtered run measures the filter, not the repo, so its totals are not
    comparable to a full run's. Anything that narrows collection -- ``-k``,
    ``-m``, an explicit path narrower than ``testpaths``, ``--last-failed``
    -- makes the resulting figures partial.
    """
    opt = config.option
    filters: list[str] = []
    if getattr(opt, "keyword", ""):
        filters.append(f"`-k {opt.keyword}`")
    if getattr(opt, "markexpr", ""):
        filters.append(f"`-m {opt.markexpr}`")
    if getattr(opt, "lf", False) or getattr(opt, "failedfirst", False):
        filters.append("`--last-failed`/`--failed-first`")

    # --ignore / --ignore-glob / --deselect drop items during collection while
    # leaving config.args equal to testpaths, so without these the run reports
    # itself as a full suite while whole files' markers are missing. Not a
    # hypothetical: .claude/rules/python-backend.md tells contributors to run
    # `--ignore=` for the two tests/vocals modules that need modal.
    for dest, flag in (
        ("ignore", "--ignore"),
        ("ignore_glob", "--ignore-glob"),
        ("deselect", "--deselect"),
    ):
        values = getattr(opt, dest, None)
        if values:
            filters.append(f"`{flag}=" + f"`, `{flag}=".join(values) + "`")

    # pytest backfills ``config.args`` from ``testpaths`` when no path is
    # given, so an unfiltered `pytest` and `pytest tests` both compare equal.
    testpaths = [str(p) for p in (config.getini("testpaths") or [])]
    paths = [a for a in config.args if not a.startswith("-")]
    if paths and sorted(paths) != sorted(testpaths):
        filters.append("paths `" + "`, `".join(paths) + "`")

    if _COLLECT_ERRORS:
        filters.append(
            f"{len(_COLLECT_ERRORS)} collection error(s): `"
            + "`, `".join(sorted(_COLLECT_ERRORS))
            + "`"
        )

    if filters:
        return False, (
            f"PARTIAL RUN -- {n_items} tests collected, narrowed by "
            + ", ".join(filters)
        )
    return True, f"full suite, {n_items} tests collected"


# ------------------------------------------------------------------ emit


def _build_matrix(
    reqs: dict[str, dict[str, Any]],
    covered: dict[str, list[str]],
    scope: tuple[bool, str] = (True, "full suite"),
) -> str:
    """Render the markdown matrix as a string."""
    is_full, scope_desc = scope
    all_marker_ids = set(covered.keys())
    req_ids = set(reqs.keys())
    orphan_markers = sorted(all_marker_ids - req_ids)
    orphan_reqs = sorted(req_ids - all_marker_ids)
    covered_reqs = sorted(req_ids & all_marker_ids)

    lines: list[str] = []
    lines.append("# Coverage Matrix\n")
    lines.append("")
    lines.append(
        "_Auto-generated by `scripts/pytest_reqs_plugin.py` from "
        "`@pytest.mark.requirement(...)` markers × `reqs.json`. "
        "Gitignored -- CI publishes the full-run copy as the "
        "`coverage-matrix` artifact._\n"
    )
    lines.append("")

    # Run scope, stated before any figure. Totals from a narrowed or failed
    # run describe that run, not the repo -- say so rather than letting the
    # reader assume the denominator is the whole suite.
    if is_full:
        lines.append(f"**Run scope:** {scope_desc}.\n")
    else:
        lines.append(f"> ⚠️ **{scope_desc}.**")
        lines.append(">")
        lines.append(
            "> The totals below are scoped to this run and are **not** the "
            "repo's coverage. Regenerate with a full `pytest tests` before "
            "quoting them.\n"
        )
    lines.append("")

    # Top-line totals.
    total = len(req_ids)
    covered_n = len(covered_reqs)
    pct = (100 * covered_n / total) if total else 0.0
    lines.append(f"**Totals:** {covered_n}/{total} requirements covered "
                 f"({pct:.1f}%). "
                 f"{len(orphan_reqs)} ⚠️ uncovered · {len(orphan_markers)} ❌ "
                 f"orphan markers.\n")
    lines.append("")

    # Main table.
    lines.append("| Requirement | Category | Status | Covered by | Desc |")
    lines.append("|---|---|---|---|---|")

    def _fmt_tests(tests: list[str]) -> str:
        if not tests:
            return ""
        # Keep output short; show node ids sanitized for pipe chars.
        shown = [t.replace("|", "\\|") for t in tests]
        return "<br>".join(shown)

    for rid in sorted(req_ids):
        meta = reqs[rid]
        tests = covered.get(rid, [])
        mark = "" if tests else "⚠️"
        lines.append(
            f"| {mark}`{rid}` | {meta['category']} | {meta['status']} | "
            f"{_fmt_tests(tests)} | {meta['desc']} |"
        )

    # Orphan markers (tests reference an unknown ID).
    if orphan_markers:
        lines.append("")
        lines.append("## ❌ Orphan markers (ID not in reqs.json)")
        lines.append("")
        lines.append("| Marker ID | Test |")
        lines.append("|---|---|")
        for rid in orphan_markers:
            for test in covered[rid]:
                lines.append(f"| `{rid}` | `{test}` |")

    return "\n".join(lines) + "\n"


def pytest_sessionfinish(session, exitstatus) -> None:  # type: ignore[no-untyped-def]
    config = session.config
    if config.getoption("--no-coverage-matrix"):
        return
    items = getattr(session, "items", None) or []
    if not items:
        return
    reqs = _load_reqs()
    covered = _collect_markers(items)
    scope = _run_scope(config, len(items))
    matrix = _build_matrix(reqs, covered, scope)
    OUTPUT.write_text(matrix, encoding="utf-8")


__all__ = [
    "pytest_addoption",
    "pytest_collection_modifyitems",
    "pytest_collectreport",
    "pytest_sessionfinish",
]
