"""Workflow path filters and PR job-name catalog for ci-wait (issue #1689).

Parses this repo's `.github/workflows/*.yml` pull_request `paths` /
`paths-ignore` filters and evaluates them against a PR's changed-files list so
`scripts/ci_wait.py` can drop check-run names whose workflows legitimately do
not run at the current head (the contraction case from PR #1685 review thread
r3976977757) without reintroducing the false-SUCCESS class #1608 closes.

GitHub's negation ordering for inclusive `paths` lists ("later patterns win",
as documented in `.github/workflows/ci.yml`) is reproduced here; `paths-ignore`
uses the complementary rule (the workflow runs when at least one changed file is
not ignored). Nothing here calls `gh` or the network.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

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
        return any(
            not _file_matches_paths_ignore(path, paths_ignore) for path in changed_files
        )
    return True


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
            if workflow_would_run_for_files(
                changed_files, paths=paths, paths_ignore=paths_ignore
            ):
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

    Unknown baseline names (not in the catalog) are kept fail-closed. An empty
    result is deliberate: expansion (workflows newly activated by a path change)
    stays with `poll_until_terminal`'s two-poll stabilization rule rather than
    inflating `expected` from YAML alone.
    """
    applicable = catalog.applicable_pull_request_job_names(changed_files)
    known = catalog.known_check_names()
    return frozenset(name for name in baseline if name not in known or name in applicable)


def default_workflows_dir() -> Path:
    return Path(__file__).resolve().parents[1] / ".github" / "workflows"
