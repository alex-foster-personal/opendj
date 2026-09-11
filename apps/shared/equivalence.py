"""Equivalence-verdict gate: refuse to import a field nobody proved maps 1:1.

An agreement rate is meaningless until the two fields are
proven to mean the same thing, in the same units, on the same scale. So a
mapping without a PASSING equivalence test is UNVERIFIED and must not feed
the precedence policy or land in ``track_fields``.

This module is the enforcement point. It reads the verdict file produced by
the equivalence-test unit and answers one question per field: may I write it?

Verdict file: ``<data-dir>/state/equivalence-verdicts.json``.

**This module is the AUTHORITY on the verdict-file shape.** The producer
(``apps/equivalence``) says so in its own ``verdict.py`` docstring, so any
change to the contract lands here first and is mirrored in
``docs/analysis-retention.md``.

Shape -- a JSON object mapping OUR field name to a verdict object::

    {
      "energy":  {"status": "passed",   "normaliser": "mik_energy_1_10",
                  "checked_at": "2026-07-28T01:22:00+00:00"},
      "key":     {"status": "passed",   "normaliser": "camelot_nfc",
                  "checked_at": "2026-07-28T01:22:00+00:00"},
      "loudness": {"status": "failed",  "normaliser": null,
                  "checked_at": "2026-07-28T01:22:00+00:00"},
      "energy_segments": {"status": "passed_single_source",
                  "normaliser": "mik_seconds_to_ms",
                  "checked_at": "2026-07-28T01:22:00+00:00",
                  "verified_by": "apps.equivalence"}
    }

A top-level ``fields`` key is also accepted (``{"fields": {...}, "meta": ...}``)
so the producer may carry metadata alongside; if present it MUST be an object
and it is then the authoritative map. Unknown keys inside an entry are carried
for humans and ignored here; ``basis`` and ``suite_status`` are NOT among the
ignored ones, because the single-source case turns on them (see below).

Two statuses allow a write, two block:

==========================  ========================================================
status                      meaning
==========================  ========================================================
``passed``                  the equivalence suite passed. Allows a write
``passed_single_source``    also passed, accepted as an explicit alias of
                            ``passed`` + ``basis: single_source``. Allows a write
``failed``                  a mapping bug was found. Blocks
``untested``                no verdict, or not decidable. Blocks
==========================  ========================================================

**BASIS is the load-bearing field, not the status.** Every entry declares how
the verdict was reached, and the two are NOT equally strong evidence:

==================  ========================================================
basis               evidence
==================  ========================================================
``cross_source``    two independent sources hold the field, were normalised
                    onto one scale, and agree post-normalisation. Strongest
``single_source``   only ONE source holds the field, so no agreement rate is
                    possible even in principle. Evidence is SKILL 4b step 1
                    (full-column range and cardinality probe) plus step 5
                    (the normaliser is total over the observed domain) plus,
                    for a time series, the structural invariants, with step 3
                    (cross-source agreement) omitted as inapplicable.
                    Sufficient to WRITE, deliberately WEAKER, recorded as such
``unverified``      failed or untested. Never written without an override
==================  ========================================================

Agreed contract with the producer (``apps/equivalence``, verified against its
real output Tue 28 Jul 2026): it emits ``status: "passed"`` with a MANDATORY
``basis`` on EVERY entry, plus a corroborating ``suite_status`` of
``passed_single_source`` for the one-sided case.

One subtlety that bit us, and is now pinned by test: the producer's ``basis``
describes the PAIRING KIND, not the outcome. It sends
``basis: "cross_source"`` even on an ``untested`` field, meaning "this field
has a two-source pairing", not "this field was cross-validated". So the two
readings are separated here:

* :attr:`Verdict.pairing` -- the producer's declared pairing kind, verbatim.
* :attr:`Verdict.basis`   -- the EVIDENCE actually obtained, which is what we
  persist. A field that did not pass has verified nothing, so its basis is
  ``unverified`` regardless of the pairing it would have used.

Resolution order for the evidence basis of a PASSING verdict:

1. an explicit ``basis`` key, if present (the producer always sends one);
2. else ``status == "passed_single_source"`` implies ``single_source``;
3. else ``suite_status == "passed_single_source"`` implies ``single_source``
   -- the belt-and-braces rung that closes the hole a lenient consumer would
   otherwise fall through, since a bare ``passed`` with a dropped ``basis``
   would read as cross-validated;
4. else derive from the status.

For a passing verdict, a basis that CONTRADICTS the status or the suite status
is rejected outright: one of them is wrong and the file cannot say which.

Fail-fast rules, all deliberate:

* File ABSENT  -> every field is ``untested``. Nothing is writable. This is
  not an error, it is the correct default before the tests have been run.
* File present but unparseable, or an entry missing ``status``, or a status
  outside the enum -> ``EquivalenceGateError``. A malformed gate must never
  silently degrade to "allow".
* A field with no entry -> ``untested``. Absence is never consent.
* A source-unique field (MIK's energy time series, MIK's clipped-peak count)
  has no cross-source counterpart, so it can never earn a ``cross_source``
  basis. It stays blocked until it earns a ``single_source`` verdict.
  Fail-closed by design: the fix is a single-source verdict, never an
  exemption.
* A passing verdict that names no usable basis, or a passing verdict whose
  basis contradicts its status or suite status -> ``EquivalenceGateError``.

The override (``allow_unverified=True``, wired to
``--i-know-equivalence-is-unverified``) exists so a human can force an
exploratory load, and it logs loudly at WARNING for every field it waves
through. It is not a default anywhere.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from apps.shared.equivalence_parse import _mismatched_sources, _parse_entry, _verdict_map
from apps.shared.equivalence_types import (
    BASES,
    BASIS_BY_STATUS,
    BASIS_CROSS_SOURCE,
    BASIS_NONE,
    BASIS_SINGLE_SOURCE,
    FAILED,
    PASSED,
    PASSED_SINGLE_SOURCE,
    STATUSES,
    SUITE_STATUS_SINGLE_SOURCE,
    UNTESTED,
    UNTESTED_REASON,
    VERDICT_FILENAME,
    WRITABLE_STATUSES,
    EquivalenceGateError,
    Status,
    Verdict,
    verdict_path,
)

log = logging.getLogger(__name__)

# --------------------------------------------------------------- parsing


# ------------------------------------------------------------------ gate


class EquivalenceGate:
    """Per-field write permission derived from the verdict file.

    Construct via :meth:`load`. ``allow_unverified`` flips every non-passing
    field to writable and logs a WARNING per field, once.
    """

    def __init__(
        self,
        verdicts: dict[str, Verdict],
        *,
        source_path: Path,
        file_present: bool,
        allow_unverified: bool = False,
    ) -> None:
        self.verdicts = verdicts
        self.source_path = source_path
        self.file_present = file_present
        self.allow_unverified = allow_unverified
        self._warned: set[str] = set()

    @classmethod
    def load(
        cls,
        data_dir: Path,
        *,
        allow_unverified: bool = False,
        sources: dict[str, Path] | None = None,
    ) -> EquivalenceGate:
        """Load the verdict file.

        ``sources`` binds this load to the ACTUAL databases about to be read
        (e.g. ``{"mik": store_path}``), and is checked against the content
        fingerprint the producer stamped into ``meta.sources`` at verdict
        time (P1 regression, PR #383 review). Without it, a verdict computed
        against one ``--mik-db`` authorized values from a completely
        different store or a since-upgraded one, because nothing tied the
        PASSING status to the specific file it was proven against. Omitting
        ``sources`` (the default) skips this check entirely -- it exists for
        callers that read no external source themselves (there is nothing to
        bind), not as a way around it for callers that do.
        """
        path = verdict_path(data_dir)
        if not path.exists():
            log.warning(
                "equivalence verdict file absent at %s; every field is "
                "UNTESTED and nothing will be written%s",
                path,
                " (overridden by --i-know-equivalence-is-unverified)"
                if allow_unverified
                else "",
            )
            return cls(
                {},
                source_path=path,
                file_present=False,
                allow_unverified=allow_unverified,
            )
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise EquivalenceGateError(
                f"{path}: unreadable equivalence verdict file: {exc}"
            ) from exc
        raw_map = _verdict_map(payload, path)
        verdicts = {
            str(field): _parse_entry(str(field), raw, path)
            for field, raw in raw_map.items()
        }
        if sources:
            mismatched = _mismatched_sources(payload, sources)
            if mismatched:
                override_note = (
                    " (overridden by --i-know-equivalence-is-unverified: "
                    "every field is UNTESTED and nothing will be written "
                    "without the override flag)"
                    if allow_unverified
                    else ""
                )
                message = (
                    f"{path}: verdict was computed against a different "
                    f"{'/'.join(mismatched)} database than the one now being "
                    f"loaded (path unchanged, content fingerprint does not "
                    f"match). A passing verdict here would authorize values "
                    f"from a source whose domains and mappings were never "
                    f"probed{override_note}"
                )
                if not allow_unverified:
                    raise EquivalenceGateError(message)
                log.warning(message)
                verdicts = {}
        return cls(
            verdicts,
            source_path=path,
            file_present=True,
            allow_unverified=allow_unverified,
        )

    # ---- queries

    def verdict(self, field: str) -> Verdict:
        """Verdict for ``field``; an absent entry is ``untested``, never allowed."""
        found = self.verdicts.get(field)
        if found is not None:
            return found
        return Verdict(
            field=field,
            status=UNTESTED,
            normaliser=None,
            checked_at=None,
            basis=BASIS_NONE,
        )

    def may_write(self, field: str) -> bool:
        """True if ``field`` may be written. Logs once per overridden field.

        A ``passed_single_source`` field is writable, and says so at INFO so a
        run's log records that the evidence was one-sided.
        """
        verdict = self.verdict(field)
        if verdict.writable:
            if verdict.is_single_source and field not in self._warned:
                self._warned.add(field)
                log.info(
                    "field %r is verified SINGLE-SOURCE (normaliser %s, "
                    "verified_by %s): a scale probe plus a totality assertion, "
                    "with no cross-source agreement because the field exists "
                    "in one source only. Weaker evidence than a cross-source "
                    "pass, and recorded as such",
                    field,
                    verdict.normaliser,
                    verdict.verified_by or "unattributed",
                )
            return True
        if self.allow_unverified:
            if field not in self._warned:
                self._warned.add(field)
                log.warning(
                    "EQUIVALENCE OVERRIDE: writing field %r whose verdict is "
                    "%s (%s). This value is UNVERIFIED against our own "
                    "semantics and may be silently wrong. Source: %s",
                    field,
                    verdict.status,
                    verdict.normaliser or UNTESTED_REASON,
                    self.source_path,
                )
            return True
        return False

    def blocked_reason(self, field: str) -> str:
        """Human-readable reason ``field`` is blocked. Raises if it is not."""
        verdict = self.verdict(field)
        if verdict.writable:
            raise ValueError(f"field {field!r} is not blocked")
        if not self.file_present:
            return (
                f"no verdict file at {self.source_path}; field {field!r} is "
                f"untested"
            )
        if verdict.status == UNTESTED:
            return f"field {field!r}: {UNTESTED_REASON} in {self.source_path}"
        return (
            f"field {field!r}: equivalence test FAILED"
            + (f" (normaliser {verdict.normaliser!r})" if verdict.normaliser else "")
        )

    def summary(self, fields: list[str]) -> dict[str, str]:
        """``{field: status}`` for ``fields``, for CLI/report output."""
        return {field: self.verdict(field).status for field in fields}

    def provenance_rows(self, fields: list[str]) -> list[dict[str, Any]]:
        """Verification provenance to PERSIST alongside the values written.

        The ruling this implements (coordinator, Tue 28 Jul 2026): a reader six
        months from now must not be able to mistake a one-sided check for a
        cross-validated one. So the basis travels into the DB with the data,
        not just into a log line.
        """
        rows: list[dict[str, Any]] = []
        for field_name in fields:
            verdict = self.verdict(field_name)
            rows.append(
                {
                    "field_name": field_name,
                    "status": verdict.status,
                    "basis": verdict.basis,
                    "normaliser": verdict.normaliser,
                    "checked_at": verdict.checked_at,
                    "verified_by": verdict.verified_by,
                    "pairing": verdict.pairing,
                    "overridden": bool(
                        self.allow_unverified and not verdict.writable
                    ),
                }
            )
        return rows


__all__ = [
    "BASES",
    "BASIS_BY_STATUS",
    "BASIS_CROSS_SOURCE",
    "BASIS_NONE",
    "BASIS_SINGLE_SOURCE",
    "FAILED",
    "PASSED",
    "PASSED_SINGLE_SOURCE",
    "STATUSES",
    "SUITE_STATUS_SINGLE_SOURCE",
    "UNTESTED",
    "VERDICT_FILENAME",
    "WRITABLE_STATUSES",
    "EquivalenceGate",
    "EquivalenceGateError",
    "Status",
    "Verdict",
    "verdict_path",
]
