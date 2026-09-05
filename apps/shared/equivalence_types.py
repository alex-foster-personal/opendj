"""Shared vocabulary for :mod:`apps.shared.equivalence` and
:mod:`apps.shared.equivalence_parse`: the status/basis constants, the
``Verdict`` dataclass, and ``EquivalenceGateError``.

Split into its own module (rather than living in ``equivalence.py``, which
is where a reader would look first) purely to break the import cycle a
parsing-internals module would otherwise have with ``equivalence.py``
(``equivalence_parse`` needs these; ``equivalence.py`` needs
``equivalence_parse``'s functions back). ``equivalence.py`` re-exports every
name here, so ``from apps.shared.equivalence import Verdict`` (the existing
import shape used throughout the codebase) is unaffected.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

VERDICT_FILENAME = "equivalence-verdicts.json"

Status = Literal["passed", "passed_single_source", "failed", "untested"]

PASSED: Status = "passed"
PASSED_SINGLE_SOURCE: Status = "passed_single_source"
FAILED: Status = "failed"
UNTESTED: Status = "untested"

STATUSES: frozenset[str] = frozenset(
    {PASSED, PASSED_SINGLE_SOURCE, FAILED, UNTESTED}
)

# The two statuses that unlock a write. Ordered strongest first.
WRITABLE_STATUSES: tuple[str, ...] = (PASSED, PASSED_SINGLE_SOURCE)

# ``basis`` is optional in the file but derived here for every verdict, so the
# provenance we persist always states how the field was verified.
BASIS_CROSS_SOURCE = "cross_source"
BASIS_SINGLE_SOURCE = "single_source"
BASIS_NONE = "unverified"
BASES: frozenset[str] = frozenset(
    {BASIS_CROSS_SOURCE, BASIS_SINGLE_SOURCE, BASIS_NONE}
)
BASIS_BY_STATUS: dict[str, str] = {
    PASSED: BASIS_CROSS_SOURCE,
    PASSED_SINGLE_SOURCE: BASIS_SINGLE_SOURCE,
    FAILED: BASIS_NONE,
    UNTESTED: BASIS_NONE,
}

# The producer's corroborating signal for the one-sided case. Read as a
# fallback so a dropped ``basis`` cannot silently downgrade to cross_source.
SUITE_STATUS_SINGLE_SOURCE = "passed_single_source"

UNTESTED_REASON = "no verdict recorded (equivalence test has not run)"


class EquivalenceGateError(RuntimeError):
    """The verdict file exists but cannot be trusted. Never downgrade to allow."""


@dataclass(frozen=True)
class Verdict:
    """One field's equivalence verdict."""

    field: str
    status: Status
    normaliser: str | None
    checked_at: str | None
    basis: str = BASIS_NONE
    """Evidence actually obtained. ``unverified`` unless the verdict passed."""
    pairing: str | None = None
    """The producer's declared pairing kind, verbatim. Informational: a field
    can declare a ``cross_source`` pairing and still be ``untested``."""
    verified_by: str | None = None
    suite_status: str | None = None

    @property
    def writable(self) -> bool:
        return self.status in WRITABLE_STATUSES

    @property
    def is_single_source(self) -> bool:
        """True when the evidence is one-sided and therefore weaker."""
        return self.basis == BASIS_SINGLE_SOURCE


def verdict_path(data_dir: Path) -> Path:
    """Canonical verdict-file location under a ``data/`` root."""
    return Path(data_dir) / "state" / VERDICT_FILENAME
