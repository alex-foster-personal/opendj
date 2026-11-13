"""Bounded, user-initiated browser helper for provider plugins (issue #11256).

The helper may guide signup, run one analysis or search the user started, and
return provenance. It never evades quotas, scrapes private catalogs, rotates
accounts, automates purchases, or runs without an explicit user initiation.
Live browser automation is out of scope here; this module is the policy gate
other callers must pass before opening a browser session.

-Cursor-cloud
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field

from apps.plugins.manifest import CAPABILITY_KINDS
from apps.plugins.providers import ProviderError, refuse_advertised_stable_api, store_surface

BrowserHelperAction = Literal["signup_guide", "user_search", "user_analysis"]

_ALLOWED_ACTIONS: frozenset[str] = frozenset({"signup_guide", "user_search", "user_analysis"})

_BROWSER_HELPER_KINDS: frozenset[str] = frozenset({"browser-only", "public browse"})


class BrowserHelperError(ProviderError):
    """A browser-helper request the terms or policy must refuse."""


@dataclass(frozen=True)
class BrowserHelperProvenance:
    provider_id: str
    capability_kind: str
    store_surface: str
    action: BrowserHelperAction
    terms_url: str | None
    user_initiated: bool

    def to_wire(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "capability_kind": self.capability_kind,
            "store_surface": self.store_surface,
            "action": self.action,
            "terms_url": self.terms_url,
            "user_initiated": self.user_initiated,
        }


class BrowserHelperRequest(BaseModel):
    provider_id: str = Field(min_length=1)
    capability_kind: str
    action: BrowserHelperAction
    user_initiated: bool
    terms_url: str | None = None
    retry_after_s: int | None = None
    wait_elapsed_s: float = 0.0
    rotate_account: bool = False
    scrape_private_catalog: bool = False
    submit_purchase: bool = False
    advertised_kind: str | None = None


def validate_browser_helper(request: BrowserHelperRequest) -> BrowserHelperProvenance:
    """Refuse quota evasion, private scrape, purchase automation, and non-initiated runs."""
    if request.capability_kind not in CAPABILITY_KINDS:
        raise BrowserHelperError(f"unknown capability kind {request.capability_kind!r}")
    if request.capability_kind not in _BROWSER_HELPER_KINDS:
        raise BrowserHelperError(
            f"browser helper is only for browser-only or public browse providers, not {request.capability_kind!r}"
        )
    if request.advertised_kind is not None:
        refuse_advertised_stable_api(
            actual_kind=request.capability_kind,
            advertised_kind=request.advertised_kind,
        )
    if request.action not in _ALLOWED_ACTIONS:
        raise BrowserHelperError(f"unsupported browser helper action {request.action!r}")
    if not request.user_initiated:
        raise BrowserHelperError("browser helper requires an explicit user initiation")
    if request.rotate_account:
        raise BrowserHelperError("browser helper refuses account rotation")
    if request.scrape_private_catalog:
        raise BrowserHelperError("browser helper refuses private catalog scraping")
    if request.submit_purchase:
        raise BrowserHelperError("browser helper refuses purchase automation")
    retry_after = request.retry_after_s
    if retry_after is not None and retry_after > 0 and request.wait_elapsed_s < float(retry_after):
        raise BrowserHelperError("browser helper refuses retry before retry-after elapses")
    return BrowserHelperProvenance(
        provider_id=request.provider_id,
        capability_kind=request.capability_kind,
        store_surface=store_surface(request.capability_kind),
        action=request.action,
        terms_url=request.terms_url,
        user_initiated=True,
    )


__all__ = [
    "BrowserHelperAction",
    "BrowserHelperError",
    "BrowserHelperProvenance",
    "BrowserHelperRequest",
    "validate_browser_helper",
]
