"""Docs-only PR detection for the review-coverage gate.

Policy (the maintainer, relayed Wed 16 Sep 2026): "docs merges should just go straight
in. Codex can review it async and we'll just spot these things later." A PR
whose every changed path is documentation does not need reviewer coverage
before merge -- `scripts.review_coverage.triage` waives `EXPECTED_REVIEWERS`
for it and prints `ok docs-only: coverage not required, review is async`
plus the path list, so the exemption stays visible rather than a silent
pass. Everything else `just review-triage` checks (the bot-thread
three-state rule, debt checks, gate freshness) is UNCHANGED for a docs-only
PR; only the reviewer-coverage requirement is waived. See
docs/decisions/ADR-NEW-docs-only-review-async.md and REQUIREMENTS.md's
REVIEW-09.

SINGLE SOURCE OF TRUTH: ci.yml's own `pull_request.paths` filter already
enumerates the exact set of paths that do NOT need CI (docs/**, *.md,
handoffs/**, .planning/** except REQUIREMENTS.md, specs/**, blog/**), with
GitHub's own "later pattern wins" negation rule. Re-declaring that set here
by hand is exactly the drift risk this module exists to avoid --
`scripts/pr_ci_coverage.py`'s `EXCLUDED_DOCS_PREFIXES` is precisely that
unproven duplicate, hand-maintained and never checked against ci.yml. So
this module parses ci.yml directly through
`scripts.ci_wait_workflows.WorkflowCatalog`, the same glob evaluator
`scripts/ci_wait.py` already trusts to know when ci.yml itself would run.
"Is this PR docs-only" is defined as "would CI's own pull_request trigger
fire for these files", which is definitionally the question ci.yml answers,
so the two cannot drift the way two independently maintained lists could.

Since issue #4168 ci.yml triggers on every pull request and scopes the run from
its own `scope` job, so the list itself lives in `scripts/ci_pr_scope.py`.
`WorkflowCatalog` reads it from there for ci.yml, which keeps this module's
answer identical to what that job decides.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from scripts.ci_pr_scope import workflow_would_run_for_files
from scripts.ci_wait_workflows import WorkflowCatalog, default_workflows_dir

#: The workflow whose pull-request `paths` scope defines "docs-only" for the
#: whole repo (its in-run scope in `scripts/ci_pr_scope.py`, surfaced by
#: `WorkflowCatalog`). If that scope stops being a `paths` list (e.g. someone
#: switches it to `paths-ignore`, which this module does not read),
#: `_ci_pull_request_paths` raises rather than silently treating every PR as
#: requiring CI or as docs-only.
CI_WORKFLOW_FILE = "ci.yml"


def _ci_pull_request_paths() -> tuple[str, ...]:
    """The exact `paths:` list ci.yml's `pull_request` trigger declares, in
    file order (order matters: GitHub's negation rule is "later pattern
    wins", reproduced by `workflow_would_run_for_files`).
    """
    catalog = WorkflowCatalog.from_workflows_dir(default_workflows_dir())
    filters = catalog.workflow_filters.get(CI_WORKFLOW_FILE)
    if filters is None:
        raise RuntimeError(
            f"{CI_WORKFLOW_FILE} has no pull_request trigger; docs-only "
            "detection has nothing to parse"
        )
    paths, _paths_ignore = filters
    if not paths:
        raise RuntimeError(
            f"{CI_WORKFLOW_FILE}'s pull_request trigger declares no `paths:` "
            "filter; docs-only detection cannot tell a documentation change "
            "from a code change"
        )
    return paths


def _changed_paths(changed_files: Sequence[str | Mapping[str, str]]) -> list[str]:
    """Normalize GitHub ``/pulls/{n}/files`` rows or plain path strings."""
    paths: list[str] = []
    for entry in changed_files:
        if isinstance(entry, str):
            paths.append(entry)
            continue
        if isinstance(entry, Mapping):
            filename = entry.get("filename")
            if not isinstance(filename, str) or not filename:
                raise TypeError(
                    "is_docs_only expects path strings or file objects with "
                    f"a non-empty filename, got {entry!r}"
                )
            paths.append(filename)
            continue
        raise TypeError(
            f"is_docs_only expects path strings or file objects, got {type(entry).__name__}"
        )
    return paths


def is_docs_only(changed_files: Sequence[str | Mapping[str, str]]) -> bool:
    """True only when EVERY changed path is one ci.yml's own filter treats as
    documentation.

    A PR with ZERO changed files is never docs-only: an empty diff is an
    unmeasured or malformed PR, not a documentation one, and must fall
    through to the normal (failing) reviewer-coverage path rather than being
    read as trivially "all paths match". Checked explicitly because
    `workflow_would_run_for_files` returns `False` (CI would not run) for an
    empty list too, which is the right answer to a different question.
    """
    paths = _changed_paths(changed_files)
    if not paths:
        return False
    return not workflow_would_run_for_files(paths, paths=_ci_pull_request_paths())


def render_docs_only_pass(
    pr: str, head_sha: str, gate_commit: str, changed_files: Sequence[str]
) -> str:
    """The exact board `scripts.review_coverage.triage` prints for a
    docs-only PR. Pure and split out from `triage` itself so the exemption's
    output shape -- the literal `ok docs-only` line the policy requires stay
    visible rather than a silent pass -- is directly testable with no `gh`
    call involved, and so this module (not `review_coverage.py`, which is
    already close to this repo's 600-line file-size ratchet) owns the growth.
    """
    lines = [
        f"[review-coverage] PR #{pr} @ head {head_sha} (gate code has main's {gate_commit[:9]})",
        "",
        "ok docs-only: coverage not required, review is async",
    ]
    lines.extend(f"    {path}" for path in sorted(changed_files))
    return "\n".join(lines)
