"""Compare two Pioneer USB export trees (thin wrapper).

The implementation has moved to
:mod:`apps.sync.usb.pioneer.differ` — this module now only re-exports
the public surface and delegates CLI invocation so existing usage
(docs, ad-hoc shell aliases) keeps working without change.

Usage
-----
.. code-block:: bash

    .venv/bin/python -m apps.scratch.diff_usb_exports \\
        --a /Volumes/LaCie/.../rb-usb-export-big/PIONEER \\
        --b /tmp/rb-usb-export-big-writer/PIONEER \\
        --out docs/rb-usb-export-writer-diff-report.md

See :mod:`apps.sync.usb.pioneer.differ` for the typed API
(``snapshot_onelibrary``, ``diff_snapshots``, ``round_trip_via_writer``)
and the matrix runner that drives the pytest harness.
"""
from __future__ import annotations

from apps.sync.usb.pioneer.differ import (
    build_report,
    diff_pair_cli as main,
    snapshot_onelibrary,
)

__all__ = ["build_report", "main", "snapshot_onelibrary"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
