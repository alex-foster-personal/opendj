"""SYNC-05 analysis-apply field support and refusal errors.

Kept apart from ``apps.sync.apply_analysis`` so the classes have one
identity: run as ``python -m apps.sync.apply_analysis`` that module is
``__main__``, and a class defined there differs from the one
``analysis_csv_live`` would import, so ``main()`` could not catch it.
"""
from __future__ import annotations

# Codex P04-02: RB-side writes are only implemented for these fields.
# ``apply_analysis`` used to silently skip any other field and still
# exit 0, which made the CLI report success after a partial apply.
SUPPORTED_RB_WRITE_FIELDS = frozenset({
    "bpm", "key", "loudness_lufs", "loudness_dbtp", "pqtz",
})


class UnsupportedRbFieldError(RuntimeError):
    """RB-side write scheduled for a field we can't safely write.

    Codex P04-02: ``_write_rb_field`` previously returned ``False`` for
    ``manual_bpm`` / ``key_camelot`` / ``tags`` and the CLI still exited
    ``0``. This exception is how we now refuse unsupported fields
    loudly; ``apply_analysis.main()`` converts it to a non-zero exit.
    """


class UnsupportedDjayFieldError(RuntimeError):
    """djay-side write scheduled for a field ``_write_djay_field`` can't write.

    Raised before any row is applied, the djay-side twin of
    :class:`UnsupportedRbFieldError`; without it such a row failed its
    verify mid-run, after earlier rows were already written.
    """
