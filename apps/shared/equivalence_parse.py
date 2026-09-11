"""Verdict-file parsing internals of :mod:`apps.shared.equivalence`, split
out to keep that module under the file-size review threshold
(``scripts/quality_gate.py``). Purely mechanical: ``EquivalenceGate.load``
in ``equivalence.py`` still calls ``_verdict_map``/``_parse_entry``/
``_mismatched_sources`` exactly as before.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from apps.shared.source_fingerprint import matches as _source_matches

from .equivalence_types import (
    BASES,
    BASIS_BY_STATUS,
    BASIS_CROSS_SOURCE,
    BASIS_NONE,
    BASIS_SINGLE_SOURCE,
    PASSED_SINGLE_SOURCE,
    STATUSES,
    SUITE_STATUS_SINGLE_SOURCE,
    WRITABLE_STATUSES,
    EquivalenceGateError,
    Verdict,
)

# --------------------------------------------------------------- parsing


def _mismatched_sources(
    payload: Any, sources: dict[str, Path]
) -> list[str]:
    """Names in ``sources`` whose declared fingerprint does not match the
    live file at that path right now. Empty if the verdict file carries no
    ``meta.sources`` at all (an old-format or hand-written verdict file --
    nothing recorded to check against, so this cannot report a mismatch;
    the caller decides separately whether "unrecorded" is itself acceptable).
    """
    if not isinstance(payload, dict):
        return []
    meta = payload.get("meta")
    if not isinstance(meta, dict):
        return []
    declared_sources = meta.get("sources")
    if not isinstance(declared_sources, dict):
        return []
    mismatched = []
    for name, live_path in sources.items():
        if name not in declared_sources:
            continue
        if not _source_matches(declared_sources[name], live_path):
            mismatched.append(name)
    return mismatched


def _verdict_map(payload: Any, path: Path) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise EquivalenceGateError(
            f"{path}: expected a JSON object at the top level, got "
            f"{type(payload).__name__}"
        )
    if "fields" in payload:
        fields = payload["fields"]
        if not isinstance(fields, dict):
            raise EquivalenceGateError(
                f"{path}: top-level 'fields' must be an object, got "
                f"{type(fields).__name__}"
            )
        return fields
    return payload


def _optional_str_field(field: str, raw: dict[str, Any], key: str, path: Path) -> str | None:
    """Reads an optional string-or-null key off a raw verdict entry, split out
    of ``_parse_entry`` to keep it under the complexity ceiling."""
    value = raw.get(key)
    if value is not None and not isinstance(value, str):
        raise EquivalenceGateError(
            f"{path}: {key!r} for field {field!r} must be a string or "
            f"null, got {type(value).__name__}"
        )
    return value


def _parse_entry(field: str, raw: Any, path: Path) -> Verdict:
    if not isinstance(raw, dict):
        raise EquivalenceGateError(
            f"{path}: verdict for field {field!r} must be an object, got "
            f"{type(raw).__name__}"
        )
    if "status" not in raw:
        raise EquivalenceGateError(
            f"{path}: verdict for field {field!r} has no 'status' key; a "
            f"verdict without a status is not a verdict"
        )
    status = raw["status"]
    if status not in STATUSES:
        raise EquivalenceGateError(
            f"{path}: verdict for field {field!r} has status {status!r}; "
            f"expected one of {sorted(STATUSES)}"
        )
    normaliser = _optional_str_field(field, raw, "normaliser", path)
    checked_at = _optional_str_field(field, raw, "checked_at", path)
    if status in WRITABLE_STATUSES and not normaliser:
        raise EquivalenceGateError(
            f"{path}: field {field!r} is marked {status} but names no "
            f"'normaliser'; a passing verdict must record HOW the values were "
            f"made comparable (SKILL 4b: record the test, not just the result)"
        )
    verified_by = _optional_str_field(field, raw, "verified_by", path)
    suite_status = _optional_str_field(field, raw, "suite_status", path)
    basis, pairing = _resolve_basis(field, raw, status, suite_status, path)
    return Verdict(
        field=field,
        status=status,  # type: ignore[arg-type]
        normaliser=normaliser,
        checked_at=checked_at,
        basis=basis,
        pairing=pairing,
        verified_by=verified_by,
        suite_status=suite_status,
    )


def _validate_declared_basis(
    field: str, declared: str | None, path: Path
) -> None:
    """The shape-check half of ``_resolve_basis``, split out to keep it under
    the complexity ceiling."""
    if declared is not None and not isinstance(declared, str):
        raise EquivalenceGateError(
            f"{path}: 'basis' for field {field!r} must be a string or null, "
            f"got {type(declared).__name__}"
        )
    if declared is not None and declared not in BASES:
        raise EquivalenceGateError(
            f"{path}: field {field!r} declares basis {declared!r}; expected "
            f"one of {sorted(BASES)}"
        )


def _resolve_basis(
    field: str,
    raw: dict[str, Any],
    status: str,
    suite_status: str | None,
    path: Path,
) -> tuple[str, str | None]:
    """Return ``(evidence_basis, declared_pairing)``.

    The producer's ``basis`` is the PAIRING KIND and is sent on every entry,
    including untested ones. The evidence basis is what we persist, and a field
    that did not pass has verified nothing. See the module docstring.
    """
    declared = raw.get("basis")
    _validate_declared_basis(field, declared, path)

    if status not in WRITABLE_STATUSES:
        # Did not pass, so it verified nothing, whatever pairing it declared.
        return BASIS_NONE, declared

    return _resolve_passed_basis(field, declared, status, suite_status, path), declared


def _resolve_passed_basis(
    field: str,
    declared: str | None,
    status: str,
    suite_status: str | None,
    path: Path,
) -> str:
    """The ``status in WRITABLE_STATUSES`` half of ``_resolve_basis``, split
    out to keep it under the complexity ceiling."""
    implied_by_suite = (
        BASIS_SINGLE_SOURCE if suite_status == SUITE_STATUS_SINGLE_SOURCE else None
    )
    implied_by_status = (
        BASIS_SINGLE_SOURCE if status == PASSED_SINGLE_SOURCE else None
    )
    if declared == BASIS_NONE:
        raise EquivalenceGateError(
            f"{path}: field {field!r} passed but declares basis {declared!r}; a "
            f"passing verdict must say whether it rests on cross-source "
            f"agreement or a single-source probe"
        )
    candidate = declared if declared in (BASIS_CROSS_SOURCE, BASIS_SINGLE_SOURCE) else None
    basis = candidate or implied_by_status or implied_by_suite or BASIS_BY_STATUS[status]

    if basis == BASIS_NONE:
        raise EquivalenceGateError(
            f"{path}: field {field!r} passed but declares basis {declared!r}; a "
            f"passing verdict must say whether it rests on cross-source "
            f"agreement or a single-source probe"
        )
    for other, label in (
        (implied_by_status, f"status {status!r}"),
        (implied_by_suite, f"suite_status {suite_status!r}"),
    ):
        if other is not None and basis != other:
            raise EquivalenceGateError(
                f"{path}: field {field!r} resolves to basis {basis!r} but "
                f"{label} implies {other!r}; a contradictory verdict is not a "
                f"verdict"
            )
    return basis


