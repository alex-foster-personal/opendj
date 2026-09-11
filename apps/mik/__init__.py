"""Mixed In Key (MIK) reader + matcher + loader.

READ-ONLY towards MIK. Nothing in this package opens a ``.mikdb`` for
writing; :func:`apps.mik.mikdb.open_ro` is the only door and it uses the
``file:...?mode=ro`` URI form so a write attempt raises at execute time.

Modules:

* :mod:`apps.mik.bookmark`     -- macOS bookmark-blob path parser (ZBOOKMARKDATA)
* :mod:`apps.mik.mikdb`        -- read-only store reader -> dataclasses
* :mod:`apps.mik.match`        -- three-tier matcher against ``tracks``
* :mod:`apps.mik.availability` -- ``track_availability`` probe + writer
* :mod:`apps.mik.load`         -- the gated loader (dry-run default)
* :mod:`apps.mik.promote`      -- ``unmatched_source_analysis`` -> ``track_fields``
* :mod:`apps.mik.cli`          -- ``python -m apps.mik``

See ``.agents/skills/mixed-in-key-integration-and-sync/SKILL.md`` and
``docs/analysis-retention.md``.
"""
from __future__ import annotations

SOURCE = "mik"
"""The ``track_fields.source`` / ``unmatched_source_analysis.source`` string."""

__all__ = ["SOURCE"]
