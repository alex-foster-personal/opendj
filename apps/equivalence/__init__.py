"""Apples-to-apples equivalence suite for cross-source analysis fields.

Implements section 4b of
the cross-source equivalence protocol as runnable code.

The suite answers ONE question per candidate field pair: are these two
columns the same thing, in the same units, on the same scale? An agreement
rate computed before that question is answered is not evidence.

Pipeline, in the order the skill mandates:

1. :mod:`apps.equivalence.probe`       range + cardinality per source per field
2. :mod:`apps.equivalence.normalisers` TOTAL normalisers (map or raise)
3. :mod:`apps.equivalence.compare`     post-normalisation agreement + CSV dump
4. :mod:`apps.equivalence.compare`     systematic-offset clustering
5. :mod:`apps.equivalence.known`       known-answer fixture harness
6. :mod:`apps.equivalence.verdict`     the gate, written for apps.shared.equivalence

CLI: ``python -m apps.equivalence run --json`` (see ``__main__.py``).
Docs: ``docs/cross-source-equivalence.md``.
"""

from __future__ import annotations

SUITE_VERSION = "1"
"""Bumped when a verdict's MEANING changes, so a stale verdict file is visible."""

__all__ = ["SUITE_VERSION"]
