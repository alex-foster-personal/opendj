"""Sink paths, thresholds, and failure signatures shared by every perf-health check.

See ``scripts/perf_health_check.py`` for the full contract these constants serve.
"""

from __future__ import annotations

import re
from pathlib import Path

# ----- configuration -----------------------------------------------------------------

HOME = Path.home()

# macOS derives the packaged app's data dir from its bundle identifier, so the engine's
# logs move when the identifier does. Same candidate list, same reason, as
# scripts/diagnostics/opendj_performance_probe.py after the OPS-08 bake-off.
BUNDLE_IDS = ("com.opendj.desktop", "com.opendj.desktop.lane-b")
ENGINE_LOG_DIRS = tuple(
    HOME / "Library" / "Application Support" / bundle_id / "logs" for bundle_id in BUNDLE_IDS
)
# apps/webui/server/client_logs.DEFAULT_LOG_DIR: where the dev daemon writes.
LEGACY_LOG_DIR = HOME / ".local" / "share" / "music-dj-tools" / "webui"
CLIENT_LOG_DIRS = (*ENGINE_LOG_DIRS, LEGACY_LOG_DIR)

CLIENT_ERROR_PREFIX = "webui-client-errors"
# Q5 in docs/perf/QUEUE.md. Probed leniently so the check reports the day it lands.
CLIENT_PERF_PREFIX = "webui-client-perf"
LOG_DATE_RE = re.compile(r"-(\d{4}-\d{2}-\d{2})\.log$")

DEFAULT_ERROR_LOG_DAYS = 7
# Enough rows to see a pattern, few enough that one bad day cannot bury the summary. The
# full count is always printed, so truncation never hides the denominator.
MAX_EVIDENCE_ROWS = 5

# DUPLICATED, deliberately: scripts/ci_health_metrics.ITERATION_METRICS_PATH holds the
# same value. Importing it would put the repo root on the required-precondition list for
# a check that must run from a scheduled copy. One constant, two readers, one comment.
ITERATION_METRICS_PATH = HOME / ".local" / "share" / "mdt-iteration-metrics" / "metrics.jsonl"
ITERATION_METRICS_WINDOW_HOURS = 48

WATCHDOG_LOG_PATH = HOME / "Library" / "Logs" / "mdt-ci-health.log"
WATCHDOG_REPO_PATH = HOME / ".local" / "share" / "mdt-ci-health" / "repo"
# The watchdog runs 4-hourly, so 8h is two consecutive missed runs, not one late one.
WATCHDOG_MAX_RUN_AGE_HOURS = 8.0
# Self-update (PR #538) pulls --ff-only before every run. A checkout that has not moved
# in a week means the pull is failing, and the watchdog is running week-old checks.
WATCHDOG_MAX_CHECKOUT_AGE_DAYS = 7.0
WATCHDOG_LOG_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\s")
WATCHDOG_START_MARKER = "starting check"

PROBE_DIR = HOME / "Library" / "Application Support" / "OpenDJ Diagnostics" / "performance"
# The probe samples every 15 seconds under launchd, so a newest sample older than a day
# means it stopped, not that it is quiet.
PROBE_MAX_SAMPLE_AGE_HOURS = 24.0
PROBE_INSTALL_DOC = "docs/perf/diagnostics-probe.md"

QUEUE_DOC = "docs/perf/QUEUE.md"

SEVERITY_OK = "OK"
SEVERITY_INFO = "INFO"
SEVERITY_WARN = "WARN"
SEVERITY_ERROR = "ERROR"

EXIT_OK = 0
EXIT_ERROR = 1

# ----- signatures ----------------------------------------------------------------------

# Each signature is counted separately so the headline number is decomposable: "4
# deck-load failures" is only useful if you can see it was 3 broken links and 1 real
# engine failure. Order is irrelevant; a row is attributed to the first match.
SIGNALSMITH_SIGNATURES = (
    ("signalsmith-timeout", re.compile(r"Signalsmith\b.*\btimed out\b", re.IGNORECASE)),
)
DECK_LOAD_SIGNATURES = (
    ("engine-load-failed", re.compile(r"Deck \d+ load failed")),
    ("drop-load-failed", re.compile(r"drop load failed")),
    # 'Performance command failed - Error: load: deck 4 must be fully stopped ...'
    ("load-precondition", re.compile(r"\bload: deck \d+")),
    ("audio-missing", re.compile(r"cannot load: ")),
)
# PR #539 stamps source='deck-load' onto the failure context, which is the channel that
# does not depend on message wording. Message regexes stay as the pre-#539 fallback.
DECK_LOAD_CONTEXT_SOURCE = "deck-load"
STAGE_PREFIX = "stage_"

SIGNALSMITH_POINTER = (
    "check worklet-ack p95 rows and xrun events around that time (shipped by PR #550), "
    "and whether decodeMix dominated the preceding deck-load"
)
DECK_LOAD_POINTER = (
    "read the stage_* fields for WHERE the load died (PR #539 stamps them); a dominant "
    "stage_decodeMix or stage_stemProcessorCreate is Q6/Q7 in " + QUEUE_DOC + ", not a new bug"
)
