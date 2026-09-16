"""Absolute paths to the macOS system tools this repo shells out to.

Resolve these by absolute path, never through PATH. A process started by a
launchd agent, a packaged app's sidecar, or any other launcher that sets its
own PATH need not have ``/usr/sbin`` on it, and a bare argv then raises
``FileNotFoundError`` before the tool runs.

That is not hypothetical. Wed 16 Sep 2026 the launchd-supervised review
preview's engine could not find ``sysctl``, so its machine-pressure sampler
reported unavailable and every pressure shed was blind, while the identical
engine started from a shell sampled fine.
``tests/shared/test_native_tool_paths.py`` pins it, including a
platform-independent guard against a bare ``"sysctl"`` argv anywhere in
``apps/``.

Only tools OUTSIDE the directories every launcher keeps (``/usr/bin``,
``/bin``) need an entry here. ``vm_stat`` and ``vmmap`` live in ``/usr/bin``.

Its own package, and deliberately a leaf that imports nothing: this started
under ``apps.shared``, where it put ``apps.diagnostics`` in a mutual-import
pair with ``apps.shared`` (``shared.machine_pressure`` already reaches into
``diagnostics.probe_native_metrics``). A constant naming a filesystem path
belongs to neither side of that, so it sits outside both and cannot be drawn
into a cycle by anything that imports it later.
"""

from __future__ import annotations

SYSCTL = "/usr/sbin/sysctl"
