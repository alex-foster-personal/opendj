"""Per-client read model for the headphone state (CUEOUT-18).

Every open performance page publishes its screen once a second, and the UI
mirror keeps only the last document it was handed. Two tabs therefore take
turns owning it. For most of the mirror that is a known limit; for the
headphone state it is a wrong answer, because the tabs can honestly disagree:
a tab with device access denied sees one output where the operator's tab sees
nine, and whichever published last used to be what an agent read.

This store keeps each client's latest headphone report and answers with the
best-informed one:

- a client's own later report always replaces its own earlier one;
- a report whose device lists were actually read (`listed`) outranks one from
  a different client whose lists were not (`permission_denied`,
  `not_checked` and the rest);
- between equals, the most recent report wins;
- a client that stops reporting expires and stops competing.

No report is ever invented: with nothing on record the caller gets None.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from starlette.applications import Starlette

# A page publishes once a second. A hidden tab that is not playing audio has
# its timers throttled to as little as one wake a minute, so a shorter bound
# would expire an open, healthy tab between two of its own reports.
HEADPHONE_REPORT_STALE_S = 90.0

# The client id of a page that sent none (a build from before CUEOUT-18).
ANONYMOUS_CLIENT_ID = "anonymous"

_LISTED = "listed"


@dataclass
class HeadphoneReport:
    client_id: str
    headphones: dict[str, Any]
    received_monotonic_s: float


def client_id_of(document: dict[str, Any]) -> str:
    """The reporting client named by a mirror document, else the anonymous one."""
    client_id = document.get("client_id")
    if isinstance(client_id, str) and client_id.strip() != "":
        return client_id
    return ANONYMOUS_CLIENT_ID


def _device_lists_read(headphones: dict[str, Any]) -> bool:
    access = headphones.get("device_access")
    return isinstance(access, dict) and access.get("status") == _LISTED


def _deep_merge(base: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


class HeadphoneReports:
    def __init__(self, now: Callable[[], float] = time.monotonic) -> None:
        self._now = now
        self._reports: dict[str, HeadphoneReport] = {}

    def record(self, client_id: str, headphones: dict[str, Any]) -> None:
        """Store this client's report, replacing its earlier one."""
        self._reports[client_id] = HeadphoneReport(client_id, deepcopy(headphones), self._now())

    def merge(self, client_id: str, changes: dict[str, Any]) -> None:
        """Apply a command's headphone delta to a client already on record."""
        report = self._reports.get(client_id)
        if report is None:
            return
        report.headphones = _deep_merge(report.headphones, changes)
        report.received_monotonic_s = self._now()

    def forget(self, client_id: str | None) -> None:
        """Drop one client's report, or every report when no client is named."""
        if client_id is None:
            self._reports.clear()
        else:
            self._reports.pop(client_id, None)

    def age_ms(self, report: HeadphoneReport) -> float:
        return (self._now() - report.received_monotonic_s) * 1000.0

    def best(self) -> HeadphoneReport | None:
        """The best-informed current report, or None with nothing on record.

        Stale clients are dropped as soon as a live one exists. When every
        client is stale the freshest is still returned, with its real age:
        an old answer that says it is old is more use than none.
        """
        if not self._reports:
            return None
        cutoff = self._now() - HEADPHONE_REPORT_STALE_S
        live = {
            client_id: report for client_id, report in self._reports.items() if report.received_monotonic_s >= cutoff
        }
        if not live:
            return max(self._reports.values(), key=lambda report: report.received_monotonic_s)
        self._reports = live
        return max(
            live.values(),
            key=lambda report: (_device_lists_read(report.headphones), report.received_monotonic_s),
        )


def headphone_reports(app: Starlette) -> HeadphoneReports:
    """The app's report store, created on first use."""
    reports = getattr(app.state, "headphone_reports", None)
    if reports is None:
        reports = HeadphoneReports()
        app.state.headphone_reports = reports
    if not isinstance(reports, HeadphoneReports):
        raise TypeError("app.state.headphone_reports has an invalid type")
    return reports
