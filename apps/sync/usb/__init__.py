"""USB sync package (Phase 10, CAT-02).

Subcommands
-----------

* ``python -m apps.sync.usb.plan``    -- dry-run diff report.
* ``python -m apps.sync.usb.apply``   -- cautious / bulk apply.
* ``python -m apps.sync.usb.verify``  -- post-apply hash verify.

Modules
-------

* ``profile``          -- YAML profile loader + validation (dataclass-based).
* ``state``            -- canonical-tracks loader (pyrekordbox shim until
  Phase 5 shared state lands).
* ``diff``             -- plan engine (typed ``Op`` list).
* ``preflight``        -- pre-apply safety checks.
* ``copy``             -- copy + transcode primitives (atomic rename).
* ``playlist_writer``  -- M3U8 emitter.
* ``apply``            -- cautious / bulk / remediate-drift CLI.
* ``verify``           -- hash-compare engine + CLI.

See ``.planning/phases/10-usb-sync/`` for context + plans.
"""
from __future__ import annotations

__all__: list[str] = []
