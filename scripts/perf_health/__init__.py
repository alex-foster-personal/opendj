"""Sink readers for the standing perf health check.

The CLI entry point is ``scripts/perf_health_check.py``, which is the file to read for
the full mini-PRD, the interpreter-floor contract, and how a launchd job runs it. This
package holds the per-sink implementations so no single module crosses the repo's
600-line quality-gate limit; it carries no behavior of its own.

Every module here is stdlib-only, 3.9-compatible, and imports nothing from the rest of
the repo, for the same reason ``perf_health_check.py`` doesn't: it must run standalone,
with no venv and no repo root on ``sys.path``.
"""

from __future__ import annotations
