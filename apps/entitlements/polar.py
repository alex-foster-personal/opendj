"""Polar (merchant of record) as an :class:`EntitlementSource` for hosted hubs.

BILL-02 picks Polar; this module is the source a hosted hub asks when its
operator sets ``MDT_SYNC_HUB_ENTITLEMENTS_PROVIDER=polar``. It answers the one
question the gate asks -- where does this Google ``sub`` stand for this
feature -- from Polar's own subscription records, keyed by the Polar customer
``external_id``, which is the Google ``sub`` (ADR: polar-billing-cloudsync).

RECONCILE, NEVER APPLY DELTAS (BILL-03).  Every refresh pulls the subject's
FULL subscription list (``GET /v1/subscriptions/?external_customer_id=``) and
overwrites the snapshot. There is no webhook handler: a webhook would only
make a refresh happen sooner, and the hub needs no public HTTPS endpoint for
correctness (BILL-08 stays deferred). After checkout the client asks the hub
to refresh synchronously, so fulfilment never waits on the browser redirect
(BILL-04) or on a webhook.

A PROVIDER OUTAGE NEVER CUTS OFF A PAYING USER ((private path)
section 3). A snapshot younger than :data:`REFRESH_AFTER_S` is served as is.
An older one is refreshed; if Polar cannot be reached, the last snapshot is
honored for up to :data:`HONOR_SNAPSHOT_FOR_S` and the outage is logged. Past
that, or with no snapshot at all, the error is raised and the gate answers
503 -- it never guesses a state.

HOW A SUBSCRIPTION BECOMES A LIFECYCLE STATE (:func:`standing_from_subscriptions`),
best state wins across every subscription whose product maps to the feature:

    active, trialing                 -> active
    past_due                         -> past_due   (Polar's dunning window)
    canceled, unpaid, paused         -> read_only  until ``read_only_days``
                                        after it ended, then archived
    incomplete, incomplete_expired   -> ignored    (never paid)

``read_only_days`` is REQUIRED configuration, never a default here: how long
a lapsed user keeps pull access is a pricing decision (lifecycle.py, "GRACE
LENGTHS ARE NOT HERE").

Card data never touches this module (BILL-06): it stores Polar ids, statuses
and dates only.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx

from apps.entitlements.lifecycle import LifecycleState
from apps.entitlements.resolver import EntitlementProviderError
from apps.entitlements.source import Standing

log = logging.getLogger("apps.entitlements.polar")

#: The name this source reports as ``provider`` in status and refusals.
PROVIDER_NAME: str = "polar"

PolarServer = Literal["sandbox", "production"]
BASE_URLS: dict[PolarServer, str] = {
    "sandbox": "https://sandbox-api.polar.sh",
    "production": "https://api.polar.sh",
}

#: A snapshot younger than this is served without asking Polar.
REFRESH_AFTER_S: float = 300.0
#: How long a snapshot is honored while Polar cannot be reached.
HONOR_SNAPSHOT_FOR_S: float = 24 * 3600.0
#: Polar's page size ceiling for list endpoints.
_PAGE_LIMIT: int = 100
#: The Polar API contract this parser was written against, pinned so a
#: quarterly Polar release cannot change it underneath us. Probed live Thu 15
#: Oct 2026: production echoes ``polar-version: 2026-10``, and a made-up
#: version answers 404. Upgrade deliberately, re-capturing the fixtures.
POLAR_API_VERSION = "2026-10"

#: A subject with more pages than this is a bug, not a customer.
_MAX_PAGES: int = 20
_HTTP_TIMEOUT_S: float = 15.0

_ACTIVE = frozenset({"active", "trialing"})
_PAST_DUE = frozenset({"past_due"})
_LAPSED = frozenset({"canceled", "unpaid", "paused"})
_NEVER_PAID = frozenset({"incomplete", "incomplete_expired"})
KNOWN_STATUSES: frozenset[str] = _ACTIVE | _PAST_DUE | _LAPSED | _NEVER_PAID

#: Best first: when a subject holds several subscriptions for one feature,
#: the one that grants the most wins.
_RANK: dict[LifecycleState, int] = {"active": 3, "past_due": 2, "read_only": 1, "archived": 0}


def _granted_features(raw: Mapping[str, str | Sequence[str]]) -> dict[str, tuple[str, ...]]:
    """Normalize ``product -> feature`` / ``product -> [features]`` to ``product -> tuple``.

    Both forms are accepted so an existing one-feature ``MDT_POLAR_PRODUCTS`` table keeps
    working verbatim, while the CloudSync product can grant ``cloudsync.hosted_hub`` AND
    ``pluginstore.access`` without a second product id (the launch plan sells them together).
    A feature listed twice under one product is a configuration mistake, not a second grant.
    """
    normalized: dict[str, tuple[str, ...]] = {}
    for product, grants in raw.items():
        if not (isinstance(product, str) and product.strip()):
            raise ValueError(f"empty Polar product id in {product!r}")
        if isinstance(grants, str):
            features: tuple[str, ...] = (grants,)
        elif isinstance(grants, (list, tuple)):
            features = tuple(grants)
        else:
            raise ValueError(f"product {product!r} must map to a feature id or a list of them, got {grants!r}")
        if not features:
            raise ValueError(f"product {product!r} grants no feature")
        for feature in features:
            if not (isinstance(feature, str) and feature.strip()):
                raise ValueError(f"empty feature id in {product!r} -> {grants!r}")
        if len(set(features)) != len(features):
            raise ValueError(f"product {product!r} lists a feature twice: {grants!r}")
        normalized[product] = features
    return normalized


class PolarError(EntitlementProviderError):
    """Polar could not answer, or answered something this module refuses to read.

    A subclass of the provider error so the hub gate answers it as 503
    ``SYNC_ENTITLEMENT_PROVIDER`` rather than a 500.
    """


class PolarNoCustomer(PolarError):
    """Polar holds no customer for this subject yet: nobody has paid, so there is no portal.

    Polar answers ``POST /v1/customer-sessions/`` for an unknown ``external_customer_id``
    with HTTP 422 naming that field and "Customer does not exist." (captured from the
    production API, ``(private path)``). Only that
    exact answer maps here; any other 422 stays a :class:`PolarError`.
    """


class PolarUnavailable(PolarError):
    """Polar could not be reached, or answered a retryable status (408, 429 or 5xx).

    The only failure the stored snapshot may paper over. A 4xx, an unreadable body or a
    subscription this module cannot map is a contract or configuration fault, and is
    raised straight away rather than hidden behind a day-old answer.
    """


@dataclass(frozen=True)
class PolarConfig:
    """Everything the source needs. Built by the hub's hosted config, strictly."""

    access_token: str
    server: PolarServer
    #: Polar product id -> the feature ids it grants. Many products may map to one
    #: feature, and one product may grant several: the CloudSync product opens both
    #: ``cloudsync.hosted_hub`` and ``pluginstore.access``. A bare string is accepted
    #: as the one-feature form and normalized to a tuple.
    product_features: Mapping[str, str | Sequence[str]]
    read_only_days: int
    #: Where Polar sends the browser after checkout; ``{CHECKOUT_ID}`` allowed.
    success_url: str
    snapshot_path: Path

    def __post_init__(self) -> None:
        if not self.access_token.strip():
            raise ValueError("Polar access token must be non-empty")
        if self.server not in BASE_URLS:
            raise ValueError(f"Polar server must be one of {sorted(BASE_URLS)}, got {self.server!r}")
        if not self.product_features:
            raise ValueError("at least one Polar product must map to a feature")
        # Normalize once, here: every reader below may then assume a tuple per product.
        object.__setattr__(self, "product_features", _granted_features(self.product_features))
        if not 0 <= self.read_only_days <= MAX_READ_ONLY_DAYS:
            raise ValueError(f"read_only_days must be 0 to {MAX_READ_ONLY_DAYS}, got {self.read_only_days}")
        parsed = urlsplit(self.success_url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError(f"success_url must be an https URL with a host, got {self.success_url!r}")

    @property
    def base_url(self) -> str:
        return BASE_URLS[self.server]

    @property
    def snapshot_scope(self) -> str:
        """Which Polar account answered: the server plus a token fingerprint.

        A snapshot from sandbox or from another organization's token must never
        stand in for this one's answer, so every snapshot row is keyed by it.
        """
        digest = hashlib.sha256(f"{self.server}\0{self.access_token}".encode()).hexdigest()
        return f"{self.server}:{digest[:16]}"

    def products_for(self, feature_id: str) -> list[str]:
        return sorted(p for p, features in self.product_features.items() if feature_id in features)

    def sold_features(self) -> list[str]:
        """Every feature id any product grants, sorted: what this hub sells."""
        return sorted({feature for features in self.product_features.values() for feature in features})


# ----- the pure mapping -----------------------------------------------------


def _parse_time(raw: object, field: str) -> datetime | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise PolarError(f"subscription.{field} must be an ISO date-time or null, got {raw!r}")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PolarError(f"subscription.{field} is not an ISO date-time: {raw!r}") from exc
    if parsed.tzinfo is None:
        raise PolarError(f"subscription.{field} carries no UTC offset: {raw!r}")
    return parsed


def _lapsed_state(sub: Mapping[str, Any], now: datetime, read_only_days: int) -> LifecycleState:
    ended = _parse_time(sub.get("ended_at"), "ended_at") or _parse_time(
        sub.get("current_period_end"), "current_period_end"
    )
    if ended is None:
        # The read-only window cannot be measured without an end, so refuse rather than grant it forever.
        raise PolarError(
            f"subscription {sub.get('id')!r} is {sub.get('status')!r} with neither ended_at nor current_period_end"
        )
    if sub.get("status") == "canceled" and ended > now:
        # Canceled before the period it already paid for ran out: that period is still owed.
        return "active"
    if now - ended < timedelta(days=read_only_days):
        return "read_only"
    return "archived"


def _product_id(sub: Mapping[str, Any]) -> str:
    product = sub.get("product_id")
    if not (isinstance(product, str) and product.strip()):
        raise PolarError(f"subscription {sub.get('id')!r} has no product_id: {product!r}")
    return product


def state_of(sub: Mapping[str, Any], now: datetime, read_only_days: int) -> LifecycleState | None:
    """One subscription's lifecycle state, or None when it never granted anything."""
    _product_id(sub)
    status = sub.get("status")
    if not isinstance(status, str) or status not in KNOWN_STATUSES:  # A list or object is unhashable.
        raise PolarError(
            f"subscription {sub.get('id')!r} has status {status!r}, which this "
            f"module does not know (known: {sorted(KNOWN_STATUSES)}). Refusing to "
            "guess a lifecycle state; extend apps/entitlements/polar.py."
        )
    if status in _ACTIVE:
        return "active"
    if status in _PAST_DUE:
        return "past_due"
    if status in _NEVER_PAID:
        return None
    return _lapsed_state(sub, now, read_only_days)


def standing_from_subscriptions(
    subscriptions: Sequence[Mapping[str, Any]],
    *,
    feature_id: str,
    product_features: Mapping[str, str | Sequence[str]],
    now: datetime,
    read_only_days: int,
) -> Standing | None:
    """The best standing ``subscriptions`` grant for ``feature_id``, or None.

    Quota is None (unlimited): the hosted hub is a boolean feature today, and
    byte metering (``cloudsync.hosted_assets``) is not built.
    """
    # Accept the raw (string-valued) table too: this function is the unit under test for
    # the mapping, and normalizing keeps "one product, several features" working there.
    grants = _granted_features(product_features)
    best: LifecycleState | None = None
    for sub in subscriptions:
        if feature_id not in grants.get(_product_id(sub), ()):
            continue
        state = state_of(sub, now, read_only_days)
        if state is not None and (best is None or _RANK[state] > _RANK[best]):
            best = state
    return None if best is None else Standing(best, None)


# ----- the HTTP client ------------------------------------------------------


Fetch = Callable[[str], list[dict[str, Any]]]


def _check_items(items: list[Any]) -> None:
    """Refuse a list page whose items are not subscription objects with a real id."""
    bad = [item for item in items if not isinstance(item, dict)]
    if bad:
        raise PolarError(f"Polar subscriptions list has a non-object item: {bad[0]!r:.200}")
    no_id = [item for item in items if not isinstance(item.get("id"), str) or not item["id"]]
    if no_id:  # The id is what paging dedupes on, so it must be a real one.
        raise PolarError(f"Polar subscriptions list has an item with no string id: {no_id[0]!r:.200}")


#: Ten years. Far past any real policy, far below what timedelta can hold, so a typo fails at startup.
MAX_READ_ONLY_DAYS: int = 3650

#: 4xx answers that mean "try again", not "this request is wrong" (RFC 9110 15.5.9, RFC 6585 4).
_RETRYABLE_4XX: frozenset[int] = frozenset({408, 429})


class PolarClient:
    """The three Polar calls a hosted hub makes. Real API, sandbox or production."""

    def __init__(self, config: PolarConfig, *, transport: httpx.BaseTransport | None = None) -> None:
        self._config = config
        #: Only tests pass one, to replay a response captured from the real API.
        self._transport = transport

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self._config.base_url,
            headers={
                "Authorization": f"Bearer {self._config.access_token}",
                "Polar-Version": POLAR_API_VERSION,
            },
            timeout=_HTTP_TIMEOUT_S,
            transport=self._transport,
        )

    @staticmethod
    def _json(response: httpx.Response, what: str) -> Any:
        if response.status_code in _RETRYABLE_4XX or response.status_code >= 500:
            raise PolarUnavailable(f"Polar {what} answered HTTP {response.status_code}: {response.text[:500]}")
        if response.status_code >= 400:
            raise PolarError(f"Polar {what} answered HTTP {response.status_code}: {response.text[:500]}")
        try:
            return response.json()
        except ValueError as exc:
            raise PolarError(f"Polar {what} answered non-JSON: {response.text[:200]}") from exc

    def subscriptions(self, subject: str) -> list[dict[str, Any]]:
        """Every subscription Polar holds for the customer whose external id is ``subject``."""
        found: list[dict[str, Any]] = []
        first_total: int | None = None
        try:
            with self._client() as client:
                for page in range(1, _MAX_PAGES + 1):
                    body = self._json(
                        client.get(
                            "/v1/subscriptions/",
                            params={
                                "external_customer_id": subject,
                                "limit": _PAGE_LIMIT,
                                "page": page,
                            },
                        ),
                        "subscriptions list",
                    )
                    items = body.get("items") if isinstance(body, dict) else None
                    pagination = body.get("pagination") if isinstance(body, dict) else None
                    if not isinstance(items, list) or not isinstance(pagination, dict):
                        raise PolarError(f"Polar subscriptions list has no items/pagination: {body!r:.300}")
                    _check_items(items)
                    max_page = pagination.get("max_page")
                    if not isinstance(max_page, int) or isinstance(max_page, bool) or max_page < 0:
                        raise PolarError(f"Polar subscriptions list has no integer max_page: {pagination!r:.200}")
                    total = pagination.get("total_count")
                    if not isinstance(total, int) or isinstance(total, bool) or total < 0:
                        raise PolarError(f"Polar subscriptions list has no integer total_count: {pagination!r:.200}")
                    if first_total is None:
                        first_total = total
                    elif total != first_total:  # The list changed between pages: rows may be lost.
                        raise PolarError(
                            f"Polar subscriptions list changed while paging: total_count "
                            f"{first_total} then {total} on page {page}"
                        )
                    found.extend(items)
                    if page >= max_page:
                        # An answer that disagrees with its own count would replace a good
                        # snapshot with a wrong one, so it is refused instead.
                        if len(found) != total:
                            raise PolarError(
                                f"Polar subscriptions list returned {len(found)} items but "
                                f"total_count {total}: {pagination!r:.200}"
                            )
                        ids = [item.get("id") for item in found]
                        if len(set(ids)) != len(ids):
                            raise PolarError("Polar subscriptions list repeated a subscription id while paging")
                        return found
        except httpx.HTTPError as exc:
            raise PolarUnavailable(f"Polar subscriptions list failed: {exc}") from exc
        raise PolarError(f"subject {subject!r} has more than {_MAX_PAGES} pages of subscriptions")

    def create_checkout(self, subject: str, feature_id: str, *, email: str | None) -> str:
        """A hosted checkout URL for ``feature_id``, tied to ``subject`` (BILL-06: hosted only)."""
        products = self._config.products_for(feature_id)
        if not products:
            raise PolarError(f"no Polar product is configured for feature {feature_id!r}")
        body: dict[str, Any] = {
            "products": products,
            "external_customer_id": subject,
            "success_url": self._config.success_url,
        }
        if email:
            body["customer_email"] = email
        try:
            with self._client() as client:
                result = self._json(client.post("/v1/checkouts/", json=body), "checkout create")
        except httpx.HTTPError as exc:
            raise PolarUnavailable(f"Polar checkout create failed: {exc}") from exc
        url = result.get("url") if isinstance(result, dict) else None
        if not (isinstance(url, str) and url.startswith("https://")):
            raise PolarError(f"Polar checkout answered no https url: {result!r:.300}")
        return url

    def customer_portal_url(self, subject: str) -> str:
        """Polar's hosted portal, where a customer updates a card or cancels."""
        try:
            with self._client() as client:
                response = client.post("/v1/customer-sessions/", json={"external_customer_id": subject})
        except httpx.HTTPError as exc:
            raise PolarUnavailable(f"Polar customer session create failed: {exc}") from exc
        if _names_unknown_customer(response):
            raise PolarNoCustomer(f"Polar has no customer {subject!r} yet: subscribe before managing a plan")
        result = self._json(response, "customer session create")
        url = result.get("customer_portal_url") if isinstance(result, dict) else None
        if not (isinstance(url, str) and url.startswith("https://")):
            raise PolarError(f"Polar customer session answered no portal url: {result!r:.300}")
        return url


def _names_unknown_customer(response: httpx.Response) -> bool:
    """True only for Polar's exact 422 that names ``external_customer_id`` as not existing."""
    if response.status_code != 422:
        return False
    try:
        detail = response.json().get("detail")
    except (ValueError, AttributeError):
        return False
    return isinstance(detail, list) and any(
        isinstance(item, dict)
        and item.get("loc") == ["body", "external_customer_id"]
        and item.get("msg") == "Customer does not exist."
        for item in detail
    )


# ----- the snapshot ---------------------------------------------------------


_SCHEMA = """
CREATE TABLE IF NOT EXISTS polar_snapshot_v2 (
    scope TEXT NOT NULL,
    subject TEXT NOT NULL,
    subscriptions TEXT NOT NULL,
    reconciled_at REAL NOT NULL,
    PRIMARY KEY (scope, subject)
)
"""


class _Snapshot:
    """The last reconciled subscription list per subject, on disk.

    Its own small sqlite file beside the hub DB rather than a state.db table:
    a hosted hub is one DB per owner today, and this cache is the provider's
    answer, not library state, so it must never sync to a spoke.
    """

    def __init__(self, path: Path, scope: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._scope = scope
        with self._conn() as conn:
            conn.execute(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path, timeout=10.0, isolation_level=None)

    def get(self, subject: str) -> tuple[list[dict[str, Any]], float] | None:
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT subscriptions, reconciled_at FROM polar_snapshot_v2 WHERE scope = ? AND subject = ?",
                (self._scope, subject),
            ).fetchone()
        finally:
            conn.close()
        return None if row is None else (json.loads(row[0]), float(row[1]))

    def put(self, subject: str, subscriptions: list[dict[str, Any]], at: float, now: float) -> None:
        conn = self._conn()
        try:
            conn.execute(
                "INSERT INTO polar_snapshot_v2 (scope, subject, subscriptions, reconciled_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(scope, subject) DO UPDATE SET "
                "subscriptions = excluded.subscriptions, reconciled_at = excluded.reconciled_at "
                # A slower, older fetch must never overwrite a newer one. A row dated after
                # this write (the clock moved back since it was stored) is not newer, it is
                # wrong, so it is always replaced.
                "WHERE excluded.reconciled_at > polar_snapshot_v2.reconciled_at "
                "OR polar_snapshot_v2.reconciled_at > ?",
                (self._scope, subject, json.dumps(subscriptions, sort_keys=True), at, now),
            )
        finally:
            conn.close()


_KEEP_FIELDS = ("id", "status", "product_id", "ended_at", "current_period_end", "canceled_at")


def _reconciled_within(reconciled_at: float, now: float, within_s: float) -> bool:
    """True when the snapshot is not from the future and still inside ``within_s``.

    A negative age means the clock moved back since the row was written, so it
    proves nothing about freshness and must not be served without asking Polar.
    """
    return 0 <= now - reconciled_at < within_s


def _slim(sub: Mapping[str, Any]) -> dict[str, Any]:
    """Only the fields the mapping reads: no customer email or address on disk."""
    return {key: sub.get(key) for key in _KEEP_FIELDS}


class PolarEntitlementSource:
    """:class:`EntitlementSource` over Polar, with a reconciled snapshot.

    ``fetch`` and ``clock`` are injectable so the cache and outage rules can be
    exercised without the network; production passes neither.
    """

    def __init__(
        self,
        config: PolarConfig,
        *,
        fetch: Fetch | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._config = config
        self._fetch: Fetch = fetch if fetch is not None else PolarClient(config).subscriptions
        self._clock = clock
        self._snapshot = _Snapshot(config.snapshot_path, config.snapshot_scope)
        self._lock = threading.Lock()
        self._subject_locks: dict[str, threading.Lock] = {}

    @property
    def provider(self) -> str:
        return f"{PROVIDER_NAME}:{self._config.server}"

    @property
    def config(self) -> PolarConfig:
        return self._config

    def reconcile(self, subject: str, *, force: bool = True) -> list[dict[str, Any]]:
        """Pull ``subject``'s full state from Polar now and overwrite the snapshot (BILL-03).

        ``force=False`` is the cached read path: a snapshot another request stored while this
        one waited for the subject lock is served instead of asking Polar again.
        """
        with self._lock:
            subject_lock = self._subject_locks.setdefault(subject, threading.Lock())
        # One fetch-and-write per subject at a time: the hub's start order says nothing about
        # the order Polar answers in, so a later answer must come from a later fetch.
        with subject_lock:
            if not force:
                cached = self._snapshot.get(subject)
                if cached is not None and _reconciled_within(cached[1], self._clock(), REFRESH_AFTER_S):
                    return cached[0]
            started = self._clock()
            subs = [_slim(s) for s in self._fetch(subject)]
            for sub in subs:  # Refuse an unreadable answer BEFORE it replaces a good one.
                state_of(sub, datetime.now(UTC), self._config.read_only_days)
            with self._lock:
                self._snapshot.put(subject, subs, started, self._clock())
                stored = self._snapshot.get(subject)
        # A newer reconcile may have won the write: answer with what is stored.
        return subs if stored is None else stored[0]

    def _subscriptions(self, subject: str) -> list[dict[str, Any]]:
        cached = self._snapshot.get(subject)
        now = self._clock()
        if cached is not None and _reconciled_within(cached[1], now, REFRESH_AFTER_S):
            return cached[0]
        try:
            return self.reconcile(subject, force=False)
        except PolarUnavailable as exc:
            cached = self._snapshot.get(subject)  # Another request may have stored a newer answer meanwhile.
            now = self._clock()  # The failed request took time: judge the outage window as of now.
            if cached is not None and _reconciled_within(cached[1], now, HONOR_SNAPSHOT_FOR_S):
                log.warning(
                    "polar unreachable for %s, honoring snapshot from %.0fs ago: %s",
                    subject,
                    now - cached[1],
                    exc,
                )
                return cached[0]
            raise

    def standing(self, subject: str, feature_id: str) -> Standing | None:
        return self.standing_in(self._subscriptions(subject), feature_id)

    def standing_in(self, subscriptions: Sequence[Mapping[str, Any]], feature_id: str) -> Standing | None:
        """``feature_id``'s standing in one subscription list, e.g. one :meth:`reconcile` answer."""
        return standing_from_subscriptions(
            subscriptions,
            feature_id=feature_id,
            product_features=self._config.product_features,
            now=datetime.fromtimestamp(self._clock(), UTC),
            read_only_days=self._config.read_only_days,
        )


__all__ = [
    "BASE_URLS",
    "HONOR_SNAPSHOT_FOR_S",
    "KNOWN_STATUSES",
    "PROVIDER_NAME",
    "REFRESH_AFTER_S",
    "PolarClient",
    "PolarConfig",
    "PolarEntitlementSource",
    "PolarError",
    "PolarNoCustomer",
    "PolarUnavailable",
    "standing_from_subscriptions",
    "state_of",
]
