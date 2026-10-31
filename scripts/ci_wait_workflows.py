"""Workflow path filters and PR job-name catalog for ci-wait (issue #1689).

Parses this repo's `.github/workflows/*.yml` pull_request `paths` /
`paths-ignore` filters and evaluates them against a PR's changed-files list so
`scripts/ci_wait.py` can drop check-run names whose workflows legitimately do
not run at the current head (the contraction case from PR #1685 review thread
r3976977757) without reintroducing the false-SUCCESS class #1608 closes.

GitHub's negation ordering for inclusive `paths` lists ("later patterns win")
and the complementary `paths-ignore` rule (the workflow runs when at least one
changed file is not ignored) are evaluated by `scripts.ci_pr_scope`, which the
in-run scope jobs import without PyYAML. Nothing here calls `gh` or the network;
`scripts/ci_wait.py` reads a pinned SHA's workflow texts and hands them in.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from scripts.ci_pr_scope import IN_RUN_PULL_REQUEST_SCOPES, workflow_would_run_for_files

#: Check names that a workflow on main USED to emit and no head can emit again. A pull
#: request whose earlier head ran one of these carries it in its baseline, and the
#: fail-closed rule below would keep waiting for it forever (a name absent from the catalog
#: is kept on purpose, because a deleted workflow is not the same as a finished one). A
#: retired name is the one case where "absent from the catalog" is known, not unknown, so
#: it is dropped here and nowhere else. Append, never remove: a name can be retired again
#: by a later rename. Round 6a ((private path)) retired the affected-test canary.
#: A job that still exists under new names (a renamed matrix, issue #5205) does not belong
#: here: `derive_expected_for_changed_paths` maps it through the baseline head's catalog.
RETIRED_CHECK_NAMES: frozenset[str] = frozenset(
    {
        "affected-test canary (non-blocking early signal)",
    }
)

#: (workflow file name, job id): the identity a check name keeps across a rename.
JobKey = tuple[str, str]

#: A job `if:` that reads the in-run scope decision (`needs.<scope job>.outputs.in_scope`).
_READS_IN_RUN_SCOPE = re.compile(r"needs\.[\w-]+\.outputs\.in_scope\b")
#: Status functions that stop a job inheriting a skipped `needs` entry's skip.
_RUNS_PAST_SKIPPED_NEEDS = re.compile(r"\b(?:always|cancelled)\(\)")

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


def _needs_ids(job: dict) -> list[str]:
    needs = job.get("needs") or []
    return [needs] if isinstance(needs, str) else list(needs)


def _scope_gated_job_ids(jobs: dict[str, dict]) -> frozenset[str]:
    """Jobs of an in-run-scoped workflow that the scope decision can skip.

    A job is scope-gated when its `if:` reads the scope output, or when it needs a
    gated job and has no `always()` / `cancelled()` to run past that job's skip.
    Every other job (the scope job itself, the `ci gate` / `e2e verdict` aggregators)
    is created on every pull request, docs-only or not.
    """
    gated: set[str] = set()
    changed = True
    while changed:
        changed = False
        for job_id, job in jobs.items():
            if job_id in gated:
                continue
            condition = str(job.get("if") or "")
            reads_scope = bool(_READS_IN_RUN_SCOPE.search(condition))
            inherits_skip = not _RUNS_PAST_SKIPPED_NEEDS.search(condition) and any(
                need in gated for need in _needs_ids(job)
            )
            if reads_scope or inherits_skip:
                gated.add(job_id)
                changed = True
    return frozenset(gated)


def _raw_job_name(job_id: str, job: dict) -> str:
    raw_name = job.get("name", job_id)
    return raw_name if isinstance(raw_name, str) else job_id


def _job_display_names(job_id: str, job: dict) -> list[str]:
    raw_name = _raw_job_name(job_id, job)
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
    #: Names an in-run-scoped workflow creates on every pull request whatever the
    #: changed paths, so a docs-only head must keep waiting for them.
    always_created_job_names: frozenset[str] = frozenset()
    #: Every name a job can report under, keyed by the job's identity.
    job_names: dict[JobKey, frozenset[str]] = field(default_factory=dict)
    #: A matrix job GitHub skips reports ONE check under its unexpanded template
    #: (`leg ${{ matrix.leg }} of 2`); a job that runs reports every leg instead. Each
    #: name maps to the names that answer for it, so neither shape waits on the other.
    matrix_alternatives: dict[str, frozenset[str]] = field(default_factory=dict)

    @classmethod
    def from_workflows_dir(cls, workflows_dir: Path) -> WorkflowCatalog:
        return cls.from_workflow_texts(
            {path.name: path.read_text(encoding="utf-8") for path in workflows_dir.glob("*.yml")}
        )

    @classmethod
    def from_workflow_texts(cls, texts: Mapping[str, str]) -> WorkflowCatalog:
        """Build from `{file name: YAML text}`, so a catalog can describe any commit's
        workflows (read from the API at a pinned SHA), not only this checkout's."""
        check_to_workflow: dict[str, str] = {}
        workflow_filters: dict[str, tuple[tuple[str, ...] | None, tuple[str, ...] | None]] = {}
        always_created: set[str] = set()
        job_names: dict[JobKey, frozenset[str]] = {}
        alternatives: dict[str, frozenset[str]] = {}

        for name in sorted(texts):
            document = yaml.safe_load(texts[name])
            if not isinstance(document, dict):
                continue
            trigger = _pull_request_trigger(_normalize_on(document))
            if trigger is None:
                continue
            paths = tuple(trigger.get("paths") or ()) or None
            paths_ignore = tuple(trigger.get("paths-ignore") or ()) or None
            in_run_scope = IN_RUN_PULL_REQUEST_SCOPES.get(name)
            if in_run_scope is not None:
                # ci.yml and e2e.yml trigger on every pull request and scope the run
                # from its own `scope` job (scripts/ci_pr_scope.py), so their heavy
                # jobs are skipped, not absent, out of scope. Reading that scope here
                # keeps ci-wait and docs-only detection answering exactly as they did
                # when the same list was a trigger filter.
                if paths is not None or paths_ignore is not None:
                    raise ValueError(
                        f"{name} declares a pull_request path filter AND an in-run "
                        "scope in scripts/ci_pr_scope.py; one list must be the only one"
                    )
                paths, paths_ignore = in_run_scope
            workflow_filters[name] = (paths, paths_ignore)
            jobs = {job_id: job for job_id, job in (document.get("jobs") or {}).items() if isinstance(job, dict)}
            gated = _scope_gated_job_ids(jobs) if in_run_scope is not None else frozenset()
            for job_id, job in jobs.items():
                expanded = frozenset(_job_display_names(job_id, job))
                names = expanded | {_raw_job_name(job_id, job)}
                job_names[(name, job_id)] = names
                if len(names) > len(expanded):
                    template = next(iter(names - expanded))
                    alternatives[template] = names
                    alternatives.update({leg: frozenset({leg, template}) for leg in expanded})
                for display_name in names:
                    check_to_workflow.setdefault(display_name, name)
                    if in_run_scope is not None and job_id not in gated:
                        always_created.add(display_name)

        return cls(
            check_to_workflow=check_to_workflow,
            workflow_filters=workflow_filters,
            all_job_names=frozenset(check_to_workflow),
            always_created_job_names=frozenset(always_created),
            job_names=job_names,
            matrix_alternatives=alternatives,
        )

    def applicable_pull_request_job_names(self, changed_files: Sequence[str]) -> frozenset[str]:
        applicable: set[str] = set(self.always_created_job_names)
        for name, workflow in self.check_to_workflow.items():
            paths, paths_ignore = self.workflow_filters[workflow]
            if workflow_would_run_for_files(changed_files, paths=paths, paths_ignore=paths_ignore):
                applicable.add(name)
        return frozenset(applicable)

    def known_check_names(self) -> frozenset[str]:
        return self.all_job_names

    def job_of(self, check_name: str) -> JobKey | None:
        return next((key for key, names in self.job_names.items() if check_name in names), None)


def _renamed_to(name: str, catalog: WorkflowCatalog, baseline_catalog: WorkflowCatalog | None) -> frozenset[str] | None:
    """The head's names for a baseline name whose job still exists but no longer reports
    under it (a matrix resized, a job retitled). `None` when the name is still live, or
    when its job cannot be traced to the same (workflow, job id) at the head."""
    if baseline_catalog is None:
        return None
    key = baseline_catalog.job_of(name)
    if name in catalog.known_check_names():
        # Still live only if the SAME (workflow, job id) still reports it. If the
        # original job was retitled and another job took the old name, the owed job
        # is the original, under its new names (Sol, PR #5997, ci_wait_workflows.py:239).
        if key is None or key not in catalog.job_names or catalog.job_of(name) == key:
            return None
        if name in catalog.job_names[key]:
            return None
    return catalog.job_names.get(key) if key is not None else None


def renamed_alternatives(
    baseline: frozenset[str],
    catalog: WorkflowCatalog,
    baseline_catalog: WorkflowCatalog | None = None,
) -> dict[str, frozenset[str]]:
    """Baseline names the head no longer reports under, mapped to the head names that
    replaced them (a matrix resized, a job retitled).

    `derive_expected_for_changed_paths` uses this mapping to decide what the head OWES; the
    waiter needs the same mapping a second time to decide what it must IGNORE. Without it,
    a run whose renamed legs FAILED stays in the observed snapshot forever: its names are not
    keys of the head catalog's `matrix_alternatives`, so `_superseded` never groups them with
    the newer suite that replaced them and the verdict reports FAILURE for a head whose
    current matrix passed (Codex, PR #5997 r4268197515). Names that are still live under
    their own job, and names no head can emit again, are omitted: neither is renamed.
    """
    out: dict[str, frozenset[str]] = {}
    for name in baseline - RETIRED_CHECK_NAMES:
        renamed = _renamed_to(name, catalog, baseline_catalog)
        if renamed:
            out[name] = renamed
    return out


def derive_expected_for_changed_paths(
    baseline: frozenset[str],
    changed_files: Sequence[str],
    catalog: WorkflowCatalog,
    baseline_catalog: WorkflowCatalog | None = None,
) -> frozenset[str]:
    """Drop baseline names whose workflow would not run at the current head.

    `catalog` describes the HEAD's workflows and `baseline_catalog` the baseline
    head's. A baseline name its job no longer reports under is REPLACED by that job's
    names at the head (issue #5205: a main merge cut the fast tier from four legs to
    two), so the job stays owed under the names it can actually report. Unknown baseline
    names (in neither catalog) are kept fail-closed, EXCEPT names in
    RETIRED_CHECK_NAMES, which no head can emit again. An empty result is deliberate:
    expansion (workflows newly activated by a path change) stays with
    `poll_until_terminal`'s two-poll stabilization rule rather than inflating
    `expected` from YAML alone.
    """
    applicable = catalog.applicable_pull_request_job_names(changed_files)
    known = catalog.known_check_names()
    expected: set[str] = set()
    for name in baseline - RETIRED_CHECK_NAMES:
        renamed = _renamed_to(name, catalog, baseline_catalog)
        if renamed is not None:
            expected |= renamed & applicable
        elif name not in known or name in applicable:
            expected.add(name)
    return frozenset(expected)


def default_workflows_dir() -> Path:
    return Path(__file__).resolve().parents[1] / ".github" / "workflows"
