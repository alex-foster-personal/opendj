"""Duplicate rule for ``stems.separate`` enqueues (STEM-77, ADR-NEW stems-separate-enqueue-dedup).

Lives with the jobs API rather than in ``apps/stems`` because it reads job
payloads at the store boundary and changes nothing in the separation pipeline.
"""

from __future__ import annotations

import logging
from itertools import pairwise
from pathlib import Path
from typing import Any

from apps.engine_core.jobs.store import ActiveJob
from apps.stems.job import LOCAL_WORKER_SCRIPT, R2_FIRST_WORKER_SCRIPT, SCOPE_PENDING, WORKER_SCRIPT
from apps.stems.routing import EXECUTOR_LOCAL, EXECUTOR_MODAL, effective_tier
from apps.stems.tiers import DEFAULT_TIER

log = logging.getLogger(__name__)


def library_identity(data_dir: object, default_data_dir: Path) -> Path:
    """The library a ``stems.separate`` payload's ``data_dir`` names.

    A missing or None ``data_dir`` is the engine's own library: the engine
    hands its worker ``MDT_DATA_DIR`` = its ``--data-dir``
    (``apps.engine_core.config.apply_env_contract``), which is what the
    worker reads when the payload names none. The UI omits the field while
    the CLI always sends its ``--data-dir`` default, so both spellings must be
    one library. Paths are resolved (``..``, symlinks, a trailing separator),
    so two spellings of one directory are one library too.
    """
    raw = default_data_dir if data_dir is None else Path(str(data_dir))
    return raw.expanduser().resolve(strict=False)


def covering_job(
    active: list[ActiveJob],
    requested: dict[str, Any],
    *,
    default_data_dir: Path,
) -> str | None:
    """The id of the active job that already does ALL of ``requested``, or None.

    ``active`` is every other active ``stems.separate`` row, newest first;
    ``requested`` is the canonical payload being enqueued or re-enqueued. The
    jobs API asks this inside the store's write transaction. The rule and
    why each part holds: (private path).
    """
    library = library_identity(requested.get("data_dir"), default_data_dir)
    executor = requested.get("executor")
    tier = requested.get("tier")
    same_library = [
        row
        for row in active
        if _covers_config(row, executor, tier)
        and library_identity(row.payload.get("data_dir"), default_data_dir) == library
    ]
    requested_ids = requested.get("stable_ids")
    if not isinstance(requested_ids, list):
        return _pending_scope_job(same_library)
    explicit_request = requested.get("scope") is None and requested.get("resolved_from") is None
    return _superset_job(same_library, requested_ids, explicit_request=explicit_request)


def _pending_scope_job(rows: list[ActiveJob]) -> str | None:
    """The queued pending-scope row a pending-scope request joins, or None."""
    return next(
        (
            row.id
            for row in rows
            if row.status == "queued"
            and row.payload.get("stable_ids") is None
            and row.payload.get("scope") == SCOPE_PENDING
        ),
        None,
    )


def _is_scope_snapshot(row: ActiveJob) -> bool:
    block = row.payload.get("resolved_from")
    return isinstance(block, dict) and block.get("scope") == SCOPE_PENDING


def _superset_job(
    rows: list[ActiveJob],
    requested_ids: list[Any],
    *,
    explicit_request: bool = False,
) -> str | None:
    """The row whose ids include every one of ``requested_ids``, or None for an empty list."""
    if not requested_ids:
        return None
    wanted = set(requested_ids)
    return next(
        (
            row.id
            for row in rows
            if not (explicit_request and _is_scope_snapshot(row))
            and isinstance(row.payload.get("stable_ids"), list)
            and wanted <= set(row.payload["stable_ids"])
        ),
        None,
    )


def _covers_config(row: ActiveJob, executor: object, tier: object) -> bool:
    """Whether ``row`` runs at the request's (executor, tier).

    A spawned worker whose argv this module cannot read fails closed: it is
    counted as covering, so the request waits on it instead of starting a
    second paid separation of the same tracks (Sol on #6819).
    """
    if row.worker_argv is not None and _launched_as(row.worker_argv) is None:
        log.warning("stems job %s runs an argv the dedup rule does not know; treating it as covering", row.id)
        return True
    return _runs_as(row, executor) == (executor, tier)


def _runs_as(row: ActiveJob, executor: object) -> tuple[str, str] | None:
    """The (executor, tier) ``row`` runs at, or None when that cannot be told.

    A spawned worker runs what its recorded argv says; a row not yet spawned
    runs what the runner derives at launch from its payload under the policy
    of that moment, which is ``executor``, the one the request was
    canonicalized with.
    """
    if row.worker_argv is not None:
        return _launched_as(row.worker_argv)
    if not isinstance(executor, str):
        return None
    return executor, effective_tier(row.payload.get("tier", DEFAULT_TIER), executor)


def _launched_as(argv: tuple[str, ...]) -> tuple[str, str] | None:
    """(executor, tier) read off a ``stems.separate`` worker argv
    (``apps.stems.job.build_argv``), or None for an argv it does not know."""
    flags = {flag: value for flag, value in pairwise(argv) if flag in ("--executor", "--tier")}
    names = {Path(part).name for part in argv}
    if Path(R2_FIRST_WORKER_SCRIPT).name in names and "--executor" in flags and "--tier" in flags:
        return flags["--executor"], flags["--tier"]
    if Path(LOCAL_WORKER_SCRIPT).name in names:
        return EXECUTOR_LOCAL, effective_tier(DEFAULT_TIER, EXECUTOR_LOCAL)
    if Path(WORKER_SCRIPT).name in names and "--tier" in flags:
        return EXECUTOR_MODAL, flags["--tier"]
    return None


__all__ = ["covering_job", "library_identity"]
