"""Whether the real-library tier runs on a library that lacks its premise.

The tier's premise is that its library still holds unorderable stored stamps
(``real_library._require_legacy_stamps``). The DEFAULT source, the packaged
app's own library, is normally already stamp-repaired, so it usually does not.
This module decides what that means, with the same default-vs-override split
``real_library.skip_unless_real_library`` applies to an absent file:

* a library the tier found on its own SKIPS, naming the reason and the path,
  so a plain ``pytest tests/cloudsync`` says what it did not measure, and
  ``just cloudsync-slow`` still fails because its tier floor counts that skip
  as ``real_library executed 0``;
* a library an operator named in ``MDT_REAL_LIBRARY_STATE_DB`` raises
  :class:`LibraryPremiseMissing`: they pointed here on purpose.

A module of its own because ``real_library.py`` sits at the repo's 600-line
Python file limit.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from .real_library import (
    REAL_LIBRARY_ENV_VAR,
    PreparedLibrary,
    prepare_library,
    prepare_library_unrepaired,
)
from .real_library_source import LibraryPremiseMissing


def prepared_or_skip(source: Path, dest_dir: Path, *, repaired: bool) -> PreparedLibrary:
    """Build the repaired or unrepaired library, or SKIP a default source lacking the premise."""
    build = prepare_library if repaired else prepare_library_unrepaired
    try:
        return build(source, dest_dir)
    except LibraryPremiseMissing as exc:
        if os.environ.get(REAL_LIBRARY_ENV_VAR):
            raise
        # An explicit raise rather than pytest.skip(), so the type checker sees
        # no fall-through: the isolated mypy env types pytest as Any.
        raise pytest.skip.Exception(
            f"{exc} (default source {source}; set {REAL_LIBRARY_ENV_VAR} to a "
            f"pre-repair library to run this tier)"
        ) from exc


__all__ = ["LibraryPremiseMissing", "prepared_or_skip"]
