"""Pull-request scope of the two merge-gating workflows: the ONE place each list lives.

`ci.yml` and `e2e.yml` used to scope themselves with a `pull_request` trigger filter,
so a docs-only change set never created a workflow run at all. A merge queue that
waits for named check-runs (Trunk Merge Queue in draft-PR mode, issue #4168) then
waits forever on a docs-only batch, because nothing ever reports. Both workflows now
trigger on every pull request and decide scope INSIDE the run: a cheap `scope` job
evaluates the lists below against the merge commit's changed files, the heavy jobs
run only when it says in-scope, and one always-created aggregator check-run (`ci gate`,
`e2e verdict`) reports the verdict either way. ADR: (private path)/
ADR-NEW-required-check-aggregators.md.

Supersedes: the `pull_request` `paths` / `paths-ignore` trigger filters that ci.yml and
e2e.yml carried; both lists were moved here verbatim and deleted from the YAML.

Both lists keep GitHub's own filter semantics, evaluated by
`workflow_would_run_for_files` below (also `scripts.ci_wait`'s evaluator):

- `CI_PULL_REQUEST_PATHS` is an inclusive `paths` list with `!` negation where the
  LATER pattern wins. `.planning/REQUIREMENTS.md` is written back in after the
  exclusions because it is a CI input, not documentation: `reqs.json` is generated
  from it and gated against it, and three commits that drifted the ledger (fa98ffda,
  643f896b, 9bc4128f) touched only `.planning` and so skipped the PR gate.
  `docs/perf/*.json` is written back in for the same reason: the perf KPI ledger, cards
  and map are read by the test suite (`(private path)` serves the real ledger
  against the real cards), so the nightly ledger PR (#4473) that only appends ledger rows
  was docs-only, ran no pytest, and could land undeclared KPIs that break that test on main.
  `(private path)`, the fan-out ledger, is excluded (Tue 27 Oct 2026, bundle
  merges, (private path)): PR #7592 changed only it and a spec and ran the
  whole fast lane, every fast-tier leg, the frontend job and the e2e gate, because the
  selector's `data/` coupling reached 221 modules. Seven test modules do read it as a
  seed, so this is a measured trade, not an oversight: a ledger edit is normally written
  by the progress API, which validates it, and the nightly full suite (full-ci.yml) runs
  those readers on main; a broken hand edit is fixed forward from there.
- `E2E_PATHS_IGNORE` is a `paths-ignore` list: in scope when at least one changed file
  is not ignored. `e2e.yml`'s `push` trigger must carry the same list as a literal,
  because GitHub reads trigger filters from the workflow file and nowhere else;
  `(private path)` pins that copy equal to this one.

Every consumer reads these constants (the scope job, `scripts.review_docs_only`
through `WorkflowCatalog`, `scripts.ci_wait`, and the planner's tests). Nothing
here imports beyond the standard library, so the scope job can run it with the
runner image's own `python3`.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Collection, Sequence

#: ci.yml: every path except documentation, with the requirements ledger re-included.
CI_PULL_REQUEST_PATHS: tuple[str, ...] = (
    "**",
    "!**.md",
    "!docs/**",
    "!handoffs/**",
    "!.planning/**",
    "!specs/**",
    "!blog/**",
    "!(private path)",
    ".planning/REQUIREMENTS.md",
    "docs/perf/*.json",
)

#: e2e.yml: documentation, plus workflow YAML and pytest-only trees that cannot change
#: Playwright behavior.
E2E_PATHS_IGNORE: tuple[str, ...] = (
    "**.md",
    "docs/**",
    "handoffs/**",
    ".planning/**",
    "specs/**",
    "blog/**",
    ".github/workflows/docs.yml",
    ".github/workflows/e2e.yml",
    "tests/perf/**",
    "tests/docs/**",
    # Generated ledgers no Playwright run reads (TEST-CUT, Fri 2 Oct 2026): the
    # requirements index (`python -m scripts.build_reqs_json`), the pytest-split
    # durations ledger, and the write-once app docs. 45 of 55 PRs that ran the PR-head
    # e2e gate 11:00-17:45Z touched reqs.json, which kept a pytest-only PR in scope.
    "reqs.json",
    ".test_durations",
    "app_docs/**",
    # The fan-out ledger (bundle merges, Tue 27 Oct 2026): the frontend names the
    # `/progress-tree` route, never this file, and no Playwright run reads it.
    "(private path)",
    # TEST-BUDGET round 1 (Fri 30 Oct 2026): the pytest tree and the agent skills. No
    # Playwright run imports `tests/` (the specs live in apps/webui/frontend/tests/e2e and
    # e2e.yml shells out to `scripts/`, never `tests/`), and `.agents/` is read by coding
    # agents only. Measured on the 459 PRs the queue tested Thu 1 to Fri 16 Oct 2026: the
    # gate ran for 88% of them; with these two entries it runs for 79%, which is 22
    # runner-minutes and an e2e seat per skipped draft.
    "tests/**",
    ".agents/**",
    # ...except the two things under tests/ that Playwright does consume: the media
    # fixtures its specs read (tests/fixtures/phase7-dedup) and the helper the
    # deck-load fixture builder imports (tests.support.embed_picture).
    "!tests/fixtures/**",
    "!tests/support/**",
)

#: ci.yml's fixed jobs on a pull request (TEST-BUDGET round 4; the maintainer, Fri 30 Oct 2026:
#: a HARD cap of 10 runner-minutes per PR head and queue draft). One inclusive GitHub
#: `paths` list per job, evaluated per in-scope changed file with the same `!`
#: semantics as `CI_PULL_REQUEST_PATHS`; a job runs for a pull_request only when at
#: least one in-scope changed file matches its list. An EMPTY list is a job that never
#: runs on a pull_request (ordinary head or trunk-merge draft alike): it runs on the push
#: to main (the checkpoint cadence the maintainer approved, with auto-revert) and on the nightly.
#:
#: - `contracts` (reqs-check, native wheel, Rust nextest, contract drift; 2.5 min) and
#:   `quality` (the ratchet; 5.4 min) never run on a pull request. Before this they ran
#:   on every in-scope one, and their footprints measured near-universal (the ratchet
#:   scores the whole tree, the openapi dump imports every router), so under the cap
#:   they move to main entirely.
#: - `frontend` (unit + check + build; 3.2 min) runs only when `apps/webui/frontend/**`
#:   or `apps/desktop/electron/**` changes (the job also runs the Electron shell's
#:   `pnpm test`, which nothing else runs on a pull request). Its unit tests also read a few files outside that tree (the Tauri shell,
#:   the entitlements resolver, perf helpers; tests/scripts/
#:   test_ci_fixed_jobs_by_footprint.py keeps the list); a change there is caught on
#:   main, by decision.
#: - `frontend-build` (0.9 min) runs when `frontend` runs (it needs the artifact) and
#:   whenever the e2e gate will run for the same change set (`E2E_PATHS_IGNORE`, the
#:   e2e.yml scope decision; round 2's tiers narrow the steps inside the gate, never
#:   whether it runs), because `scripts/ci_frontend_build_acquire_from_ci.sh` refuses a
#:   sha whose `frontend-build` job did not succeed.
#:
#: - `import-contracts` (the ratchet's one hard gate, import-linter over .importlinter;
#:   33 s of lint-imports) runs when any Python file, the contract file or the pinned
#:   tool manifest changes (Wed 4 Nov 2026, ADR-NEW-import-contracts-gate-on-pull-
#:   requests): #6474 broke the `public_surface` contract on main after a green pull
#:   request run because the ratchet, which carries that gate, runs on main only.
#:
#: `test` keeps its own selector (scripts/ci_test_selection.py) and is never listed here.
#: Pushes and dispatches run every job: the decision is pull_request only. ADR:
#: (private path).
CI_JOB_PATHS: dict[str, tuple[str, ...]] = {
    "contracts": (),
    "frontend": ("apps/webui/frontend/**", "apps/desktop/electron/**"),
    "frontend-build": ("apps/webui/frontend/**", "apps/desktop/electron/**"),
    "import-contracts": ("**.py", ".importlinter", "ops/quality/requirements.txt"),
    "quality": (),
}

#: The jobs `CI_JOB_PATHS` decides, in the order `skip_jobs` lists them.
CI_FIXED_JOBS: tuple[str, ...] = tuple(CI_JOB_PATHS)

#: The fixed jobs that never run on a pull_request (empty list above). ci.yml's `if:` on
#: each also carries `github.event_name != 'pull_request'`, so the YAML says it too.
CI_JOBS_NEVER_ON_PULL_REQUEST: tuple[str, ...] = tuple(job for job, patterns in CI_JOB_PATHS.items() if not patterns)

#: Workflow file -> (`paths`, `paths-ignore`) its in-run scope job evaluates for a
#: pull_request event. Exactly one of the pair is set, the same shape
#: `WorkflowCatalog.workflow_filters` carries for a trigger-level filter.
IN_RUN_PULL_REQUEST_SCOPES: dict[str, tuple[tuple[str, ...] | None, tuple[str, ...] | None]] = {
    "ci.yml": (CI_PULL_REQUEST_PATHS, None),
    "e2e.yml": (None, E2E_PATHS_IGNORE),
}


#: e2e.yml tiers (TEST-BUDGET round 2, Fri 30 Oct 2026; (private path)/
#: ADR-NEW-e2e-smoke-tier-by-footprint.md). An in-scope pull request runs the FULL gate
#: unless every in-scope file lies under one of these roots, none lies under a full-tier
#: root, and none is a script the gate itself runs: then the gate runs its SMOKE tier only,
#: the deck-load smoke and the preflight boot gate, which prove the engine still boots,
#: serves the production build and loads a deck. Any root not listed here is full tier,
#: so an unfamiliar path can only add tests, never drop them. Measured on the 441 PRs
#: merged Thu 1 to Fri 16 Oct 2026: 179 (41%) are smoke tier, 172 (39%) full, 90 out of scope.
E2E_SMOKE_TIER_PATHS: tuple[str, ...] = (
    "apps/*.py",
    "ops/**",
    "ci/**",
    "scripts/**",
    "tests/**",
    ".agents/**",
    ".github/**",
    "justfile",
    "Makefile",
    ".gitignore",
    "NOTICE",
    ".mergify.yml",
)

#: Always full tier, whatever else the change set holds: the web app and the engine that
#: every Playwright run boots, and e2e.yml itself (a change to the workflow can break a full-only
#: step, which a smoke run would skip and then pass). The scripts e2e.yml runs are read from the
#: workflow itself (`e2e_gate_scripts`), so a new gate helper needs no entry here.
E2E_FULL_TIER_PATHS: tuple[str, ...] = ("apps/webui/**", ".github/workflows/e2e.yml")

E2E_TIERS = ("smoke", "full")


def e2e_push_paths() -> tuple[str, ...]:
    """`E2E_PATHS_IGNORE` as a `paths` filter, for e2e.yml's push trigger.

    GitHub documents `!` only under `paths`; under `paths-ignore` a re-inclusion (`!tests/fixtures/**`)
    is not a documented form. `paths` expresses the same set: everything, then each ignore as a
    negation, then each re-inclusion as a plain pattern (later patterns win).
    """
    return ("**", *(p[1:] if p.startswith("!") else f"!{p}" for p in E2E_PATHS_IGNORE))


# ----- path filter evaluation ------------------------------------------------


def _github_glob_matches(pattern: str, path: str) -> bool:
    return fnmatch.fnmatch(path, pattern)


def _file_matches_paths_list(path: str, patterns: Sequence[str]) -> bool:
    """Inclusive `paths` filter with `!` negation; later patterns win."""
    included = False
    for pattern in patterns:
        if pattern.startswith("!"):
            if _github_glob_matches(pattern[1:], path):
                included = False
        elif _github_glob_matches(pattern, path):
            included = True
    return included


def _file_matches_paths_ignore(path: str, patterns: Sequence[str]) -> bool:
    """`paths-ignore` with `!` re-inclusion, like `paths`: later patterns win."""
    return _file_matches_paths_list(path, patterns)


def workflow_would_run_for_files(
    changed_files: Sequence[str],
    *,
    paths: Sequence[str] | None = None,
    paths_ignore: Sequence[str] | None = None,
) -> bool:
    """True when a `pull_request` workflow would schedule for `changed_files`."""
    if not changed_files:
        return False
    if paths is not None:
        return any(_file_matches_paths_list(path, paths) for path in changed_files)
    if paths_ignore is not None:
        return any(not _file_matches_paths_ignore(path, paths_ignore) for path in changed_files)
    return True


# ----- e2e tiers ------------------------------------------------------------------


_GATE_SCRIPT_RE = re.compile(r"(?<!tests/)(?<![\w-])scripts/[\w.-]+(?:/[\w.-]+)*(?<!\.)")


def e2e_gate_scripts(workflow_text: str) -> frozenset[str]:
    """Every `scripts/...` path e2e.yml names (steps and comments alike), extensionless helpers included.

    `tests/scripts/...` mentions are excluded by the lookbehind (`$GITHUB_WORKSPACE/scripts/...`
    still matches). A directory mention (`scripts/lib`) is kept too: text cannot tell it from an
    extensionless file, and `e2e_tier` tests membership of a changed FILE path, which a directory
    never equals, so keeping it only ever widens what is full tier.
    A path that ends a comment sentence keeps its name and loses the full stop.
    """
    # A path ending a sentence ("... scripts/x.py.") captures the full stop as part of the name.
    return frozenset(match.rstrip(".") for match in _GATE_SCRIPT_RE.findall(workflow_text))


def e2e_tier(in_scope_files: Sequence[str], gate_scripts: Collection[str]) -> str:
    """`smoke` when every in-scope file is smoke tier, else `full`; no files is `full`."""
    if not in_scope_files:
        return "full"
    for path in in_scope_files:
        if path in gate_scripts or _file_matches_paths_ignore(path, E2E_FULL_TIER_PATHS):
            return "full"
        if not _file_matches_paths_ignore(path, E2E_SMOKE_TIER_PATHS):
            return "full"
    return "smoke"


# ----- per-job footprint (TEST-BUDGET round 4) ---------------------------------


def e2e_gate_will_run(changed_files: Sequence[str]) -> bool:
    """Whether e2e.yml's gate job runs for a pull_request with this change set.

    The e2e scope decision (`E2E_PATHS_IGNORE`), nothing finer: round 2's tiers decide
    which steps run inside the gate, and the gate acquires the production build either
    way. An empty change set is unmeasured, so the gate runs.
    """
    return not changed_files or workflow_would_run_for_files(changed_files, paths_ignore=E2E_PATHS_IGNORE)


def ci_fixed_jobs_to_skip(changed_files: Sequence[str]) -> dict[str, str]:
    """`CI_JOB_PATHS` over a pull request's change set: job -> why it is skipped.

    Only the files `CI_PULL_REQUEST_PATHS` keeps in ci.yml scope count; an out-of-scope
    file (documentation) decides nothing. A job with an empty list is skipped on every
    pull request. `frontend-build` also runs whenever the e2e gate will. An empty
    change set is unmeasured: the footprint jobs run, the never-on-a-pull-request
    jobs still do not.
    """
    in_scope = [path for path in changed_files if _file_matches_paths_list(path, CI_PULL_REQUEST_PATHS)]
    skipped: dict[str, str] = {}
    for job, patterns in CI_JOB_PATHS.items():
        if not patterns:
            skipped[job] = "never runs on a pull request; it runs on the push to main and nightly"
        elif not changed_files:
            continue
        elif not any(_file_matches_paths_list(path, patterns) for path in in_scope):
            skipped[job] = f"none of the {len(in_scope)} in-scope changed file(s) is under its footprint"
    if "frontend-build" in skipped and e2e_gate_will_run(changed_files):
        del skipped["frontend-build"]
    return skipped
