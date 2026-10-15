"""Compare a measured tree-read census with `ci/pr-always-run.txt` and its excusals (#6526).

The derived check (`(private path)`) infers tree
readers from syntax, so a guard spelled outside its contract slips past it. This measures instead:
run the suite once with `scripts/pytest_tree_read_census.py`, then report every test module seen
listing the checkout that is neither on the always-run list nor kept off it with a reason.

    TREE_READ_CENSUS_DIR=.tmp/census/<chunk> nice -n 15 uv run --locked python -m pytest -q -p no:cacheprovider \\
        -p scripts.pytest_tree_read_census -n 28 <test directories of that chunk>
    uv run --locked python -m scripts.ci_tree_read_census .tmp/census

The whole suite takes about 30 minutes across 28 workers, so it runs in chunks (each into its own
subdirectory) that the report merges. Keep it under `nice` on a host that also runs CI jobs: it
uses every core. A run that was killed is refused, not merged: under xdist every worker collects
every module, so the files the surviving workers wrote would mark modules measured whose tests ran
on a lost worker. Only a finished run has its controller's `main.json`. Every chunk must run on one
unchanged checkout, the one the report reads: a test edited between chunks would otherwise count
as measured without its new reads.

Only directories that hold git-tracked files count, so a runtime cache or build output is not the
tree. A gitignored directory that tracks a force-added file still counts: a pull request can
force-add another. A module whose every such listing is under a global path (`.github/*`, `ci/*`,
`tests/fixtures/*`) is not a gap: any edit there already runs the full suite. A test module the run
never collected is UNKNOWN, and one where some test phase did not pass (a skip, an error, a failure
or an xfail stops a body early) is PARTIAL; neither is clean.

Exit 0: no gap and nothing unmeasured. A child the census cannot read (any program but a Python the
hook reaches, an argv `git ls-files` or `git ls-tree`, or one that lists nothing, such as `git rev-parse`) leaves its
module PARTIAL. Exit 1: at least one gap or stale decided-off entry.
Exit 2: no gap, but PARTIAL or UNKNOWN modules remain, so the census is incomplete.
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import json
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from scripts.affected_tests import REPO
from scripts.ci_plan_sources import pytest_inputs
from scripts.ci_test_selection_rules import always_run_modules, global_reason
from scripts.pytest_tree_read_census import CHILD_READS_DIR, SESSION_READS, checkout_snapshot, tracked_directories

DERIVED_CHECK = Path("(private path)")
EXCUSALS_NAME = "KEPT_OFF_THE_LIST"
# Readers the census sees that stay off the list, each with its reason. Three kinds: NOT A GUARD (it
# lists the tree to build an input or a command, not to assert over it), REACHED (a new file in the
# directory it lists already selects it; checked by replaying the live selector, Tue 13 Oct 2026) and
# SLOW (a guard too slow for every narrowed head; it waits on an escape measurement, as in #6511).
# They cannot join KEPT_OFF_THE_LIST, whose stale check needs the static detector to find each entry.
DECIDED_OFF_THE_LIST: dict[str, str] = {
    "(private path)": "not a guard: lists the runtime stem cache, where only a force-added CLAUDE.md is tracked",
    "tests/cloudsync/test_scenario_runner_adversarial.py": "reached: a new scenario in its own dir selects it (test-area data)",
    "(private path)": "not a guard: the engine CLI's help lists the build profiles",
    "(private path)": "not a guard: the app's ingest-coverage snapshot lists runtime stem bundles",
    "(private path)": "not a guard: the app's ingest-coverage snapshot lists runtime stem bundles",
    "(private path)": "not a guard: the engine CLI's help lists the build profiles",
    "tests/engine_core/test_setup_import.py": "not a guard: pyrekordbox probes a Rekordbox install dir, the cwd on Linux",
    "(private path)": "not a guard: asserts one named profile is available",
    "(private path)": "slow: 26.4 s walking the checkout; waits on an escape measurement",
    "(private path)": "not a guard: loads the import graph to replay selections",
    "(private path)": (
        "reached: a new frontend src file selects it, and a capability file through its coupling (#6609)"
    ),
    "tests/scripts/test_engine_payload_opendj_cli.py": "not a guard: stages a tmp payload and asserts its launcher",
    "(private path)": "reached: a new capture in its fixture dir selects it (test-area data)",
    "tests/scripts/test_savepoint_gate.py": "not a guard: globs unit tests into a stubbed command, asserts step order",
    "tests/shared/state/test_ingest_rekordbox_encrypted_source.py": "not a guard: pyrekordbox probes a Rekordbox install dir, the cwd on Linux",
    "tests/shared/test_rekordbox_open_db_encryption.py": "not a guard: pyrekordbox probes a Rekordbox install dir, the cwd on Linux",
    "tests/webui/test_midi_maps.py": "reached: a new device folder under tools/ selects it (coupling)",
    "(private path)": "not a guard: the app's ingest-coverage snapshot lists runtime stem bundles",
    "tests/webui/test_stem_cache_routes.py": "reached: data/ coupling selects it; it lists runtime stem state",
}
# A decided-off entry whose read happens only on some platforms, by `sys.platform`: measured whole elsewhere it
# reads nothing and is still right, so it is STALE only on a platform where its reason says it reads.
DECIDED_OFF_ONLY_ON: dict[str, frozenset[str]] = {
    "tests/engine_core/test_setup_import.py": frozenset({"linux"}),
    "tests/shared/state/test_ingest_rekordbox_encrypted_source.py": frozenset({"linux"}),
    "tests/shared/test_rekordbox_open_db_encryption.py": frozenset({"linux"}),
}
# The directories each kept-off or decided-off reader was excused for. A read outside them is a gap, so a
# new guard reading elsewhere cannot hide behind an old excusal; one inside them shares the module's reason,
# which is about the whole module. A reader with no entry is all gap. `dir` approves a single listing of that
# directory; `dir/**` approves it and everything below, a recursive read included, and is used only
# where the module was measured listing below it. Each is the minimal covering root of what a census measured
# the module listing. First recorded Tue 13 Oct 2026 from a census of the 67 excused modules at 58764acb12;
# a rerun of the 24 exact-root modules at 27e9e41fac moved the four whole-tree walkers from `.` to `./**`
# (9ba2f892f5); on Wed 14 Oct 2026 bounding plain git pathspecs narrowed `test_waveform_native` to `apps/**`
# (c2eb2c4c0d); on Thu 15 Oct 2026 `test_ci_tree_read_census_programs`, split from `test_ci_tree_read_census`
# (89b06b1fa6), was measured on its own at 620616a66e listing `./**`. For a module a census left PARTIAL they
# are a lower bound. Excused modules that listed nothing in the tree have no entry.
APPROVED_READS: dict[str, tuple[str, ...]] = {
    "(private path)": ("(private path)",),
    "tests/cloudsync/test_scenario_runner_adversarial.py": ("tests/cloudsync/scenarios",),
    "(private path)": ("apps/feature_flags/profiles",),
    "(private path)": ("(private path)",),
    "(private path)": ("(private path)",),
    "(private path)": ("apps/feature_flags/profiles",),
    "tests/engine_core/test_setup_import.py": (".",),
    "(private path)": ("apps/feature_flags/profiles",),
    "(private path)": ("./**",),
    "tests/quality/test_frontend_typing_gate.py": ("apps/webui/frontend/**",),
    "tests/quality/test_quality_rubric_cli.py": ("./**",),
    "tests/quality/test_underscored_param_callers.py": (
        "apps/**",
        "tests/**",
    ),
    "(private path)": ("(private path)",),
    "tests/scripts/test_affected_tests.py": ("./**",),
    "(private path)": ("./**",),
    "tests/scripts/test_ci_plan_helpers.py": (
        ".planning/**",
        "app_docs",
        "apps/adapters/**",
        "(private path)/**",
        "apps/analysis/**",
        "apps/analysis_beatgrid",
        "apps/analysis_bench/**",
        "apps/analysis_key",
        "apps/analysis_loudness",
        "apps/analysis_structure/**",
        "apps/analysis_waveform",
        "apps/audit",
        "apps/cloud/**",
        "apps/database",
        "apps/dedup/**",
        "apps/dj_copilot",
        "apps/engine_core/**",
        "apps/entitlements",
        "apps/genre_infer",
        "apps/launcher/**",
        "apps/loudness",
        "apps/lyrics/**",
        "apps/mik",
        "apps/open_dj/**",
        "apps/opendj_cli",
        "apps/pairings",
        "apps/parity",
        "apps/play_analytics/**",
        "apps/playlist_sets",
        "apps/reconcile",
        "apps/sets/**",
        "apps/shared/**",
        "apps/smartlists/**",
        "apps/spotify",
        "apps/stems/**",
        "apps/streaming_transfer",
        "apps/sync/**",
        "apps/sync_hub",
        "apps/tags",
        "apps/vocals",
        "apps/voice/**",
        "apps/webui/**",
        "blog/**",
        "docs/**",
        "handoffs",
        "(private path)",
        "(private path)/**",
        "ops/cloudsync",
        "ops/quality/**",
        "research/**",
        "scripts/**",
        "specs/**",
        "tests/**",
    ),
    "(private path)": ("docs/**",),
    "(private path)": ("./**",),
    "(private path)": ("./**",),
    "(private path)": ("./**",),
    "(private path)": ("./**",),
    "(private path)": ("./**",),
    "tests/scripts/test_ci_tree_read_census_programs.py": ("./**",),
    "(private path)": ("(private path)",),
    "(private path)": (
        "apps/desktop/src-tauri/capabilities",
        "apps/webui/frontend/src/**",
    ),
    "tests/scripts/test_engine_payload_glossary.py": ("apps/**",),
    "tests/scripts/test_engine_payload_opendj_cli.py": (
        "apps/engine_core/**",
        "apps/opendj_cli",
        "apps/shared/**",
    ),
    "tests/scripts/test_engine_payload_vocals.py": ("apps/**",),
    "tests/scripts/test_oss_tip_audit_upstream_licenses.py": ("./**",),
    "(private path)": ("./**",),
    "(private path)": ("tests/scripts/fixtures/runner_canary",),
    "tests/scripts/test_runner_toolset_cargo.py": ("./**",),
    "(private path)": ("tests/**",),
    "(private path)": ("tests/**",),
    "tests/scripts/test_savepoint_gate.py": ("apps/webui/frontend/tests/unit",),
    "tests/shared/state/test_ingest_rekordbox_encrypted_source.py": (".",),
    "tests/shared/test_rekordbox_open_db_encryption.py": (".",),
    "tests/stems/test_packaged_stems_payload.py": ("apps/**",),
    "tests/sync/usb/test_onelibrary_handle.py": ("apps/**",),
    "(private path)": ("apps/webui/frontend/src/**",),
    "tests/test_uv_run_no_sync.py": (
        "apps/**",
        "scripts/**",
    ),
    "tests/webui/test_midi_maps.py": ("tools/deck-diagrams/devices",),
    "(private path)": ("apps/**",),
    "(private path)": ("(private path)",),
    "tests/webui/test_stem_cache_routes.py": ("(private path)",),
    "(private path)": ("apps/**",),
}
COMPLETED_EXIT_STATUSES = frozenset({0, 1})  # pytest.ExitCode.OK, TESTS_FAILED: every test ran
FINISHED_RUN_FILE = "main.json"  # the run's own process writes it last; xdist workers write gw<N>.json
NOT_MEASURED = (
    "listings made inside a child the census cannot read (a program that is not a hooked Python, an argv "
    "`git ls-files` or `git ls-tree`, or one that lists nothing; a shell, tree scanner or `env -S`), whose module "
    "reads PARTIAL; an `importlib.metadata` search over `sys.path` itself (its default, or `path=sys.path`, which "
    "it cannot tell apart once the search lists), which the import system makes at every entry-point lookup, so "
    "distributions a test finds in a checkout directory it put on `sys.path` are not a read; "
    "and a listing behind a "
    "process-wide cache or a session- or package-scoped fixture, which is credited only to the first "
    "module per worker that triggers it; a multiprocessing forkserver's children, whose listings land "
    "unplaced under <session> (the report ends UNKNOWN; in (private path)); and what native code does "
    "itself: a listing it makes in-process (a C or Rust extension calling opendir), or a child it starts "
    "(posix_spawn, Rust's Command), neither of which raises an audit event, so that child is never counted; "
    "and startup code (a `.pth` file's `import` line) that changes a child's argv, working directory or "
    "environment before the hook loads and lists only under the start it erased, since each child replays its "
    "startup code from the start it reached (no executable `.pth` line in the locked environment does)"
)
TEST_FILE_PATTERNS = ("test_*.py", "*_test.py")  # pytest's default `python_files`, which this repo keeps


@dataclass(frozen=True)
class Census:
    measured: frozenset[str]
    incomplete: frozenset[str]  # collected, but some test body never ran
    reads: dict[str, frozenset[str]]
    platform: str  # the `sys.platform` every run in it was measured on


@dataclass(frozen=True)
class Report:
    gaps: dict[str, frozenset[str]]
    listed_readers: frozenset[str]
    excused_readers: frozenset[str]
    decided_off_readers: frozenset[str]
    stale: frozenset[str]
    partial: frozenset[str]
    unknown: frozenset[str]
    unplaced: frozenset[str]  # tracked directories listed outside any test module: repository code, older threads


# -----------------------------------------------------------------------------
def _hooked_starts_and_launches(children: Path) -> tuple[Counter[str], Counter[str]]:
    """Each module's hooked child starts and launches, starts read first (see below)."""
    started = Counter(f.read_text(encoding="utf-8") for f in children.glob("*.started"))
    launched = Counter(m for f in children.glob("*.launched") for m in f.read_text(encoding="utf-8").splitlines())
    return started, launched


def _children_that_never_checked_in(children: Path) -> set[str]:
    """The modules whose hooked Python launches and starts do not pair. More launches: a child still starting
    when the report loads, or one whose hook another sitecustomize shadowed, may list the tree unseen. More
    starts: a Python the census did not count as a launch (from `bash script.sh`, `node`) could cancel one
    that never started. Starts are counted before launches, since a launch is written before its child
    starts: a child that starts while this runs is then counted as launched and not started."""
    started, launched = _hooked_starts_and_launches(children)
    return {module for module in launched.keys() | started.keys() if started[module] != launched[module]}


def _children_started_without_a_launch(children: Path) -> set[str]:
    """The modules with more hooked starts than launches. A spawn on a pool thread counts no launch, yet its
    child inherits the running module's name, so those modules' child listings cannot be told apart."""
    started, launched = _hooked_starts_and_launches(children)
    return {module for module in started if started[module] > launched[module]}


def _children_the_run_never_checked(children: Path) -> set[str]:
    """The modules whose hooked child has not exited normally and is not among those the controller checked
    for liveness at the end: one that loaded the hook during or after that check may still be listing."""
    started = sorted(children.glob("*.started"))
    if not started:
        return set()
    checked = set(json.loads((children.parent / FINISHED_RUN_FILE).read_text(encoding="utf-8"))["children_checked"])
    unchecked = (s for s in started if s.name not in checked and not s.with_suffix(".done").exists())
    return {marker.read_text(encoding="utf-8") for marker in unchecked}


def load_census(directory: Path) -> Census:
    """Merge the per-process files the plugin wrote, in `directory` or one subdirectory per chunk run.
    An empty directory, a run without its controller's file (killed before it finished), runs from
    different checkouts or a checkout changed since is an error, not a clean run. A module with a
    collected test whose body never ran to a pass in that run, on any worker, is incomplete."""
    files = sorted(directory.rglob("*.json"))
    if not files:
        raise FileNotFoundError(f"no census files in {directory}; did the run load the plugin?")
    finished = {_check_the_run_finished(run) for run in sorted({f.parent for f in files})}
    snapshots = {snapshot for snapshot, _ in finished}
    if len(snapshots) > 1:
        raise RuntimeError(f"census runs in {directory} come from different checkouts; rerun every chunk on one")
    platforms = {platform for _, platform in finished}
    if len(platforms) > 1:  # a platform-scoped decision is STALE on one and right on another
        raise RuntimeError(
            f"census runs in {directory} come from different platforms {sorted(platforms)}; rerun on one"
        )
    if snapshots != {checkout_snapshot()}:
        raise RuntimeError(f"the checkout changed since the census in {directory} ran; rerun it")
    measured: set[str] = set()
    incomplete: set[str] = set()
    reads: dict[str, set[str]] = {}
    for children in sorted({f.parent / CHILD_READS_DIR for f in files}):  # Python children of measured tests
        # Child state first, then their logs: a child that starts or finishes during the read stays unfinished.
        incomplete |= _children_that_never_checked_in(children) | _children_the_run_never_checked(children)
        incomplete |= {m for f in children.glob("*.unhooked") for m in f.read_text(encoding="utf-8").splitlines()}
        unplaceable = _children_started_without_a_launch(children)  # its child listings go unplaced
        for line in (
            line for f in sorted(children.glob("*.jsonl")) for line in f.read_text(encoding="utf-8").splitlines()
        ):
            child = json.loads(line)
            module = SESSION_READS if child["module"] in unplaceable else child["module"]
            reads.setdefault(module, set()).update(child["dirs"])
    for run in sorted({f.parent for f in files}):  # per run: a test another chunk ran never ran in this one
        collected: dict[str, set[str]] = {}
        ran: dict[str, set[str]] = {}
        for file in (f for f in files if f.parent == run):
            part = json.loads(file.read_text(encoding="utf-8"))
            measured |= set(part["measured"])
            incomplete |= set(part["incomplete"])
            for field, merged in (("reads", reads), ("collected", collected), ("ran", ran)):
                for module, values in part[field].items():
                    merged.setdefault(module, set()).update(values)
        incomplete |= {module for module, ids in collected.items() if not ids <= ran.get(module, set())}
    reads_by_module = {m: frozenset(d) for m, d in reads.items()}
    return Census(frozenset(measured), frozenset(incomplete), reads_by_module, platforms.pop())


def _check_the_run_finished(run: Path) -> tuple[str, str]:
    """A run counts only if its controller wrote `main.json` with a passing or failing exit (not an
    interrupt or an internal error), every worker that wrote a file shut down cleanly and the checkout
    did not change while it ran. Returns that checkout's snapshot and the platform the run measured."""
    controller = run / FINISHED_RUN_FILE
    if not controller.is_file():
        raise RuntimeError(f"census run never finished (no {FINISHED_RUN_FILE}), rerun it: {run}")
    summary = json.loads(controller.read_text(encoding="utf-8"))
    if summary["exitstatus"] not in COMPLETED_EXIT_STATUSES:
        raise RuntimeError(f"census run ended with pytest exit status {summary['exitstatus']}, rerun it: {run}")
    workers: dict[str, str | None] = summary["workers"]
    if failed := {worker: error for worker, error in workers.items() if error is not None}:
        raise RuntimeError(f"census run lost workers {failed}, rerun it: {run}")
    written = {f.stem for f in run.glob("*.json")} - {controller.stem}
    if written != set(workers):
        raise RuntimeError(f"census run files {sorted(written)} do not match its workers {sorted(workers)}: {run}")
    if summary["snapshot"] != summary["snapshot_at_end"]:
        raise RuntimeError(f"the checkout changed during the census run, rerun it: {run}")
    snapshot: str = summary["snapshot"]
    platform: str = summary["platform"]  # a run without it predates platform-scoped decisions: rerun it
    return snapshot, platform


def excused_modules(root: Path = REPO) -> frozenset[str]:
    """The keys of the derived check's excusal map, read without importing a test module."""
    tree = ast.parse((root / DERIVED_CHECK).read_text(encoding="utf-8"))
    for node in tree.body:
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == EXCUSALS_NAME
            and node.value is not None
        ):
            return frozenset(ast.literal_eval(node.value))
    raise LookupError(f"{EXCUSALS_NAME} not found in {DERIVED_CHECK}")


def collectable_test_modules(root: Path = REPO) -> frozenset[str]:
    """The pytest inputs pytest collects as test modules, so each one must appear in a full census."""
    return frozenset(
        path.relative_to(root).as_posix()
        for path in pytest_inputs(root / "tests")
        if any(fnmatch.fnmatch(path.name, pattern) for pattern in TEST_FILE_PATTERNS)
    )


def _outside_global_paths(directory: str) -> bool:
    return global_reason(f"{directory}/_") is None


def _within(read: str, roots: tuple[str, ...]) -> bool:
    """`dir` approves one listing of that directory; `dir/**` approves it and every directory below it. A
    recursive read (`dir/**`, which only an argv `git ls-files` or `git ls-tree` records) needs a subtree approval."""
    base = _tree_base(read)
    subtrees = [_tree_base(root) for root in roots if root.endswith("/**")]
    exact = not read.endswith("/**") and base in roots
    return exact or any(top in {".", base} or base.startswith(f"{top}/") for top in subtrees)


def _tree_base(read: str) -> str:
    """The directory a read starts at: `apps` for both `apps` and the recursive `apps/**`."""
    return read.removesuffix("/**")


def compare(
    census: Census,
    listed: frozenset[str],
    excused: frozenset[str],
    decided_off: frozenset[str],
    approved: Mapping[str, tuple[str, ...]],
    tracked: frozenset[str],
    inputs: frozenset[str],
    only_on: Mapping[str, frozenset[str]],
) -> Report:
    """A listed reader is always covered. A kept-off or decided-off reader is covered only inside the
    roots in `approved`; any other reader, or read, is a gap. A decided-off entry in `only_on` can be STALE
    only on a platform named there, where its reason says it reads."""
    tree_reads = {
        m: frozenset(d for d in dirs if _tree_base(d) in tracked and _outside_global_paths(_tree_base(d)))
        for m, dirs in census.reads.items()
    }
    unplaced = tree_reads.pop(SESSION_READS, frozenset())  # no module to credit, so no verdict either
    if SESSION_READS in census.incomplete:  # a long-lived helper's child it could not read, or one still running
        unplaced |= {"."}
    readers = {m: dirs for m, dirs in tree_reads.items() if dirs}
    off_the_list = excused | decided_off
    uncovered = {
        m: frozenset(d for d in dirs if not (m in off_the_list and _within(d, approved.get(m, ()))))
        for m, dirs in readers.items()
        if m not in listed
    }
    return Report(
        gaps={m: dirs for m, dirs in sorted(uncovered.items()) if dirs},
        listed_readers=frozenset(readers) & listed,
        excused_readers=frozenset(readers) & excused,
        decided_off_readers=frozenset(readers) & decided_off,
        stale=frozenset(
            m
            for m in (decided_off & census.measured) - census.incomplete - frozenset(readers)
            if m not in only_on or census.platform in only_on[m]
        ),
        partial=census.incomplete & inputs,
        unknown=inputs - census.measured,
        unplaced=unplaced,
    )


def _exit_code(report: Report) -> int:
    if report.gaps or report.stale:
        return 1
    if report.partial or report.unknown or report.unplaced:
        return 2
    if not (report.gaps or report.stale or report.partial or report.unknown or report.unplaced):
        return 0
    raise AssertionError("unreachable")


# -----------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("census_dir", type=Path)
    args = parser.parse_args(argv)
    census = load_census(args.census_dir)
    listed, excused = frozenset(always_run_modules(REPO)), excused_modules()
    decided_off = frozenset(DECIDED_OFF_THE_LIST)
    report = compare(
        census,
        listed,
        excused,
        decided_off,
        APPROVED_READS,
        tracked_directories(),
        collectable_test_modules(),
        DECIDED_OFF_ONLY_ON,
    )
    whole = len(census.measured - census.incomplete)
    readers = report.listed_readers | report.excused_readers | report.decided_off_readers | frozenset(report.gaps)
    print(
        f"[census] {len(census.measured)} modules whose collection began (an import error counts), {whole} measured whole, "
        f"{len(readers)} listed a tracked directory outside the global paths"
    )
    print(
        f"[census] readers: {len(report.listed_readers)} listed, {len(report.excused_readers)} kept off, "
        f"{len(report.decided_off_readers)} decided off"
    )
    print(f"[census] STALE: {len(report.stale)} decided-off entries measured whole but no longer reading the checkout")
    for module in sorted(report.stale):
        print(f"  {module}")
    print(
        f"[census] GAP: {len(report.gaps)} readers neither listed nor kept off, or reading outside their approved roots"
    )
    for module, dirs in report.gaps.items():
        print(f"  {module}: {len(dirs)} dirs, e.g. {', '.join(sorted(dirs)[:3])}")
    print(
        f"[census] PARTIAL: {len(report.partial)} test modules where some test did not run to a pass, was deselected, or started an unmeasured child"
    )
    for module in sorted(report.partial):
        print(f"  {module}")
    print(f"[census] UNKNOWN: {len(report.unknown)} test modules never collected, so not measured")
    for module in sorted(report.unknown):
        print(f"  {module}")
    print(
        f"[census] UNKNOWN: repository code or a thread older than the running module listed {len(report.unplaced)}"
        " tracked directories outside any test module, so no module is credited (`.` also stands for a child"
        " the census could not read)"
    )
    for directory in sorted(report.unplaced):
        print(f"  {directory}")
    print(f"[census] NOT MEASURED, on any run: {NOT_MEASURED}")
    return _exit_code(report)


if __name__ == "__main__":
    raise SystemExit(main())
