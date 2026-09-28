"""Pull-request scope of the two merge-gating workflows: the ONE place each list lives.

`ci.yml` and `e2e.yml` used to scope themselves with a `pull_request` trigger filter,
so a docs-only change set never created a workflow run at all. A merge queue that
waits for named check-runs (Trunk Merge Queue in draft-PR mode, issue #4168) then
waits forever on a docs-only batch, because nothing ever reports. Both workflows now
trigger on every pull request and decide scope INSIDE the run: a cheap `scope` job
evaluates the lists below against the merge commit's changed files, the heavy jobs
run only when it says in-scope, and one always-created aggregator check-run (`ci gate`,
`e2e verdict`) reports the verdict either way. ADR: docs/decisions/
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
- `E2E_PATHS_IGNORE` is a `paths-ignore` list: in scope when at least one changed file
  is not ignored. `e2e.yml`'s `push` trigger must carry the same list as a literal,
  because GitHub reads trigger filters from the workflow file and nowhere else;
  `tests/scripts/test_ci_required_gate_wiring.py` pins that copy equal to this one.

Every consumer reads these constants (the scope job, `scripts.review_docs_only`
through `WorkflowCatalog`, `scripts.ci_wait`, and the planner's tests). Nothing
here imports beyond the standard library, so the scope job can run it with the
runner image's own `python3`.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Sequence

#: ci.yml: every path except documentation, with the requirements ledger re-included.
CI_PULL_REQUEST_PATHS: tuple[str, ...] = (
    "**",
    "!**.md",
    "!docs/**",
    "!handoffs/**",
    "!.planning/**",
    "!specs/**",
    "!blog/**",
    ".planning/REQUIREMENTS.md",
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
)

#: Workflow file -> (`paths`, `paths-ignore`) its in-run scope job evaluates for a
#: pull_request event. Exactly one of the pair is set, the same shape
#: `WorkflowCatalog.workflow_filters` carries for a trigger-level filter.
IN_RUN_PULL_REQUEST_SCOPES: dict[str, tuple[tuple[str, ...] | None, tuple[str, ...] | None]] = {
    "ci.yml": (CI_PULL_REQUEST_PATHS, None),
    "e2e.yml": (None, E2E_PATHS_IGNORE),
}


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
    return any(_github_glob_matches(pattern, path) for pattern in patterns)


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
