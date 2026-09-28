"""Workflow path filters and PR job-name catalog for ci-wait (issue #1689).

Parses this repo's `.github/workflows/*.yml` pull_request `paths` /
`paths-ignore` filters and evaluates them against a PR's changed-files list so
`scripts/ci_wait.py` can drop check-run names whose workflows legitimately do
not run at the current head (the contraction case from PR #1685 review thread
r3976977757) without reintroducing the false-SUCCESS class #1608 closes.

GitHub's negation ordering for inclusive `paths` lists ("later patterns win")
and the complementary `paths-ignore` rule (the workflow runs when at least one
changed file is not ignored) are evaluated by `scripts.ci_pr_scope`, which the
in-run scope jobs import without PyYAML. Nothing here calls `gh` or the network.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

from scripts.ci_pr_scope import IN_RUN_PULL_REQUEST_SCOPES, workflow_would_run_for_files

#: Check names that a workflow on main USED to emit and no head can emit again. A pull
#: request whose earlier head ran one of these carries it in its baseline, and the
#: fail-closed rule below would keep waiting for it forever (a name absent from the catalog
#: is kept on purpose, because a deleted workflow is not the same as a finished one). A
#: retired name is the one case where "absent from the catalog" is known, not unknown, so
#: it is dropped here and nowhere else. Append, never remove: a name can be retired again
#: by a later rename. Round 6a (specs/ci-fail-fast.md) retired the affected-test canary.
RETIRED_CHECK_NAMES: frozenset[str] = frozenset(
    {
        "affected-test canary (non-blocking early signal)",
    }
)

# ----- workflow catalog ------------------------------------------------------


def _normalize_on(document: dict) -> dict:
    on = document.get("on")
    if on is None:
        on = document.get(True)
    if isinstance(on, str):
        return {on: None}
    if isinstance(on, list):
        return {event: None for event in on}
    assert isinstance(on, dict), "workflow on: must be a mapping"
    return on


def _pull_request_trigger(on: dict) -> dict | None:
    trigger = on.get("pull_request")
    if trigger is None:
        return None
    if trigger is None or trigger == "":
        return {}
    assert isinstance(trigger, dict), "pull_request trigger must be a mapping"
    return trigger


def _expand_matrix_job_name(name: str, matrix: dict) -> list[str]:
    if not matrix or len(matrix) != 1:
        return [name]
    key, values = next(iter(matrix.items()))
    if not isinstance(values, list) or not values:
        return [name]
    if not all(isinstance(value, (int, str)) for value in values):
        return [name]
    expr = f"${{{{ matrix.{key} }}}}"
    if expr not in name:
        return [name]
    return [name.replace(expr, str(value)) for value in values]


def _job_display_names(job_id: str, job: dict) -> list[str]:
    raw_name = job.get("name", job_id)
    if not isinstance(raw_name, str):
        raw_name = job_id
    matrix = (job.get("strategy") or {}).get("matrix") or {}
    if isinstance(matrix, dict):
        return _expand_matrix_job_name(raw_name, matrix)
    return [raw_name]


@dataclass(frozen=True)
class WorkflowCatalog:
    """Maps check-run names to workflow files and evaluates PR path filters."""

    check_to_workflow: dict[str, str]
    workflow_filters: dict[str, tuple[tuple[str, ...] | None, tuple[str, ...] | None]]
    all_job_names: frozenset[str]

    @classmethod
    def from_workflows_dir(cls, workflows_dir: Path) -> WorkflowCatalog:
        check_to_workflow: dict[str, str] = {}
        workflow_filters: dict[str, tuple[tuple[str, ...] | None, tuple[str, ...] | None]] = {}
        all_names: set[str] = set()

        for path in sorted(workflows_dir.glob("*.yml")):
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
            if not isinstance(document, dict):
                continue
            trigger = _pull_request_trigger(_normalize_on(document))
            if trigger is None:
                continue
            paths = tuple(trigger.get("paths") or ()) or None
            paths_ignore = tuple(trigger.get("paths-ignore") or ()) or None
            in_run_scope = IN_RUN_PULL_REQUEST_SCOPES.get(path.name)
            if in_run_scope is not None:
                # ci.yml and e2e.yml trigger on every pull request and scope the run
                # from its own `scope` job (scripts/ci_pr_scope.py), so their heavy
                # jobs are skipped, not absent, out of scope. Reading that scope here
                # keeps ci-wait and docs-only detection answering exactly as they did
                # when the same list was a trigger filter.
                if paths is not None or paths_ignore is not None:
                    raise ValueError(
                        f"{path.name} declares a pull_request path filter AND an in-run "
                        "scope in scripts/ci_pr_scope.py; one list must be the only one"
                    )
                paths, paths_ignore = in_run_scope
            workflow_filters[path.name] = (paths, paths_ignore)
            for job_id, job in (document.get("jobs") or {}).items():
                if not isinstance(job, dict):
                    continue
                for display_name in _job_display_names(job_id, job):
                    check_to_workflow.setdefault(display_name, path.name)
                    all_names.add(display_name)

        return cls(
            check_to_workflow=check_to_workflow,
            workflow_filters=workflow_filters,
            all_job_names=frozenset(all_names),
        )

    def applicable_pull_request_job_names(self, changed_files: Sequence[str]) -> frozenset[str]:
        applicable: set[str] = set()
        for name, workflow in self.check_to_workflow.items():
            paths, paths_ignore = self.workflow_filters[workflow]
            if workflow_would_run_for_files(changed_files, paths=paths, paths_ignore=paths_ignore):
                applicable.add(name)
        return frozenset(applicable)

    def known_check_names(self) -> frozenset[str]:
        return self.all_job_names


def derive_expected_for_changed_paths(
    baseline: frozenset[str],
    changed_files: Sequence[str],
    catalog: WorkflowCatalog,
) -> frozenset[str]:
    """Drop baseline names whose workflow would not run at the current head.

    Unknown baseline names (not in the catalog) are kept fail-closed, EXCEPT names
    in RETIRED_CHECK_NAMES, which no head can emit again and would otherwise be
    waited on until the timeout. An empty result is deliberate: expansion
    (workflows newly activated by a path change) stays with `poll_until_terminal`'s
    two-poll stabilization rule rather than inflating `expected` from YAML alone.
    """
    applicable = catalog.applicable_pull_request_job_names(changed_files)
    known = catalog.known_check_names()
    return frozenset(
        name
        for name in baseline
        if name not in RETIRED_CHECK_NAMES and (name not in known or name in applicable)
    )


def default_workflows_dir() -> Path:
    return Path(__file__).resolve().parents[1] / ".github" / "workflows"
