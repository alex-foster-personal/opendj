"""Stdlib-only Darwin diagnostics probes for the OpenDJ performance sampler.

Implementation modules are :mod:`apps.diagnostics.probe_types` (shared
constants and helpers) and :mod:`apps.diagnostics.probe_native_metrics`
(``proc_pid_rusage``, ``vmmap``, ``sysctl``, ``vm_stat``). Scripts under
``scripts/diagnostics/`` re-export them for launchd and CLI entry points.

INTERPRETER FLOOR: like the rest of this package, these modules run under
``/usr/bin/python3`` (3.9 on current macOS) and must stay stdlib-only.
"""
