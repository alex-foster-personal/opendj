"""Stdlib-only Darwin diagnostics probes for the OpenDJ performance sampler.

``apps.diagnostics`` is the in-repo package (tests, webui fixtures).
``scripts/diagnostics`` is the self-contained install copy: stdlib only, no
``apps`` imports, launched with ``-m`` from the directory that contains the
package.

Implementation modules in this package are :mod:`apps.diagnostics.probe_types`
(shared constants and helpers) and :mod:`apps.diagnostics.probe_native_metrics`
(``proc_pid_rusage``, ``vmmap``). The engine pressure sampler stays
:mod:`apps.shared.machine_metrics` (re-exported from
:mod:`apps.diagnostics.probe_native_metrics`). The machine sampler used by the
*installed* probe lives in :mod:`scripts.diagnostics.probe_machine_metrics` and
is re-exported from :mod:`scripts.diagnostics.probe_native_metrics`.

INTERPRETER FLOOR: like the rest of this package, these modules run under
``/usr/bin/python3`` (3.9 on current macOS) and must stay stdlib-only.
"""
