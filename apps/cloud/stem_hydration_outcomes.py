"""Hydration outcome records shared by single, bulk and prefetch hydration.

Split from :mod:`apps.cloud.stem_hydration` (issue #9726), which re-exports
every name here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from apps.cloud.stem_source import (
    STEM_HUB_INDEX_FAILED,
    STEM_HUB_UNREACHABLE,
    STEM_MONTHLY_CAP_REACHED,
    STEM_MONTHLY_CAP_UNSET,
    StemSourceError,
)

HydrationStatus = Literal["already_local", "hydrated", "unavailable", "reserved_skip", "error", "hub_error"]


# --- outcomes -------------------------------------------------------------------


@dataclass(frozen=True)
class HydrationOutcome:
    stable_id: str
    status: HydrationStatus
    bytes_fetched: int = 0
    reason: str | None = None
    #: The ``StemSourceError.code`` that produced this outcome, when there was
    #: one. Without it a monthly-cap refusal became a code-less "error" and
    #: every caller re-labelled it STEM_BUNDLE_HYDRATION_FAILED, so the stems
    #: route answered 502 and bulk hydration treated a spent month as an
    #: ordinary skip and kept asking for more tracks (Codex round 5).
    code: str | None = None


#: Source codes that mean "no more bundles this month", not "this bundle
#: failed": hydration outcomes carrying one stop a bulk run.
_MONTHLY_CAP_CODES = frozenset({STEM_MONTHLY_CAP_REACHED, STEM_MONTHLY_CAP_UNSET})


@dataclass(frozen=True)
class BulkHydrateReport:
    fetched: tuple[HydrationOutcome, ...]
    skipped: tuple[HydrationOutcome, ...]
    bytes_fetched: int


def _stem_source_error_from_hub_outcome(outcome: HydrationOutcome) -> StemSourceError:
    reason = outcome.reason or "hub transport failure"
    code = STEM_HUB_INDEX_FAILED if "HTTP 5" in reason else STEM_HUB_UNREACHABLE
    return StemSourceError(code, reason)
