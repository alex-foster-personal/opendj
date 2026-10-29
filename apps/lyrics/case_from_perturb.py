"""Map a single-op :class:`~apps.lyrics.perturb.PerturbedSheet` to a :class:`~apps.lyrics.cases.VersionCase`.

Kept out of ``cases.py`` so eval-harness construction helpers stay small in the
case builder module without widening its perfbatch correctness-only diffs.
"""

from __future__ import annotations

from apps.lyrics.perturb import PerturbedSheet


def version_case_from_perturb(
    case: str,
    sheet: PerturbedSheet,
    expected_verdict: str,
    expect_kind: str | None,
    true_words: list[str],
    *,
    target_words: list[str] | None = None,
):
    """Return a :class:`~apps.lyrics.cases.VersionCase` (import deferred)."""
    from apps.lyrics.cases import VersionCase

    (op,) = sheet.ops
    block = (
        target_words
        if target_words is not None
        else true_words[op.word_start : op.word_end + 1]
    )
    return VersionCase(
        case,
        sheet.words,
        expected_verdict,
        expect_kind,
        block,
        list(sheet.ops),
        source_idx=sheet.source_idx,
    )
