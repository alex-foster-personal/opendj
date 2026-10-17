"""The HOSTED-hub switch, read from the environment exactly once, strictly.

    MDT_SYNC_HUB_HOSTED              unset, "" or "0": self-hosted (default)
                                     "1": hosted; anything else is refused
    MDT_SYNC_HUB_ENTITLEMENTS_PROVIDER
                                     unset or "static": the file below;
                                     "polar": Polar (merchant of record)
    MDT_SYNC_HUB_ENTITLEMENTS_FILE   required for "static": the JSON table the
                                     shipped StaticEntitlementSource serves

POLAR (``apps.entitlements.polar``, ADR polar-billing-cloudsync), every key
required, none defaulted::

    POLAR_ACCESS_TOKEN          organization access token (Doppler, never .env)
    MDT_POLAR_SERVER            "sandbox" or "production"
    MDT_POLAR_PRODUCTS          JSON {"<polar product id>": "<feature id>", ...}
    MDT_POLAR_READ_ONLY_DAYS    days a lapsed owner keeps pull before archived
    MDT_POLAR_SUCCESS_URL       https URL Polar returns the browser to

A Polar-backed hub also requires ``MDT_SYNC_CREDENTIAL_MODE=enforce``: selling
access while the hub trusts the ``machine_id`` a caller names would let anyone
who learned a paying machine's id sync for free. Startup refuses otherwise,
and the gate re-checks per request because the mode is read per request.

A self-hosted hub (the only kind today) never reads the file and never opens
the DB here, so an unset environment configures exactly what the app did
before this module existed: ``app.state.sync_hub_hosted = False`` and no
source, which :mod:`apps.sync_hub.entitlement_gate` treats as always entitled.

Hosted mode fails at STARTUP, not at the first request, when it cannot work:
no entitlements file, a malformed one, or a hub DB that already holds more
than one owner. The last one is a security rule, not a tidiness one: pull is
not owner-scoped (``engine.hub_changes_since`` returns every changelog row),
so one hosted hub DB shared by two owners would hand each the other's
library. Until pull is owner-scoped, a hosted hub is ONE DB PER OWNER. The
gate re-checks that on every hosted request, because an enrollment after
startup could add a second owner.

The file format, every key required and no others accepted::

    {"provider": "comped-2026",
     "standings": [{"subject": "<google sub>", "feature_id": "cloudsync.hosted_hub",
                    "state": "active", "quota": null}]}
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from fastapi import FastAPI

from apps.entitlements import EntitlementSource, Standing, StaticEntitlementSource
from apps.entitlements.lifecycle import STATES, LifecycleState
from apps.entitlements.polar import PolarConfig, PolarEntitlementSource
from apps.shared.state import db as state_db
from apps.sync_hub import machine_credentials
from apps.sync_hub.client_transport_ops import state_db_path

HOSTED_ENV: str = "MDT_SYNC_HUB_HOSTED"
ENTITLEMENTS_FILE_ENV: str = "MDT_SYNC_HUB_ENTITLEMENTS_FILE"
ENTITLEMENTS_PROVIDER_ENV: str = "MDT_SYNC_HUB_ENTITLEMENTS_PROVIDER"
POLAR_TOKEN_ENV: str = "POLAR_ACCESS_TOKEN"
POLAR_SERVER_ENV: str = "MDT_POLAR_SERVER"
POLAR_PRODUCTS_ENV: str = "MDT_POLAR_PRODUCTS"
POLAR_READ_ONLY_DAYS_ENV: str = "MDT_POLAR_READ_ONLY_DAYS"
POLAR_SUCCESS_URL_ENV: str = "MDT_POLAR_SUCCESS_URL"
#: The snapshot file a Polar source keeps, beside the hub DB.
POLAR_SNAPSHOT_NAME: str = "entitlements-polar.sqlite"

#: Mirrors :data:`apps.sync_hub.entitlement_gate.HOSTED_FLAG_ATTR` /
#: ``SOURCE_ATTR``; the gate imports these, so there is one spelling.
HOSTED_FLAG_ATTR: str = "sync_hub_hosted"
SOURCE_ATTR: str = "entitlement_source"

#: The most owners a hosted hub DB may hold until pull is owner-scoped.
MAX_HOSTED_OWNERS: int = 1

_FILE_KEYS: frozenset[str] = frozenset({"provider", "standings"})
_STANDING_KEYS: frozenset[str] = frozenset({"subject", "feature_id", "state", "quota"})


class HostedConfigError(RuntimeError):
    """Hosted mode is on but cannot run safely. Raised at startup."""


@dataclass(frozen=True)
class HostedConfig:
    """What the environment says. ``source`` is None exactly when not hosted."""

    hosted: bool
    source: EntitlementSource | None

    def describe(self) -> dict[str, Any]:
        """The JSON-safe readout the CLI and ``/sync/status`` report."""
        return {
            "hosted": self.hosted,
            "entitlement_provider": None if self.source is None else self.source.provider,
        }


# ----- parsing ---------------------------------------------------------------


def parse_hosted(env: Mapping[str, str]) -> bool:
    """Strict: only "1" is on. "true", "yes" or " 1" would be a guess."""
    raw = env.get(HOSTED_ENV, "")
    if raw in ("", "0"):
        return False
    if raw == "1":
        return True
    raise HostedConfigError(
        f"{HOSTED_ENV} must be unset, '0' or '1', got {raw!r}; refusing to guess whether this hub enforces billing."
    )


def _standing_entry(entry: object, where: str) -> tuple[tuple[str, str], Standing]:
    if not isinstance(entry, dict) or set(entry) != _STANDING_KEYS:
        raise HostedConfigError(f"{where} must be an object with keys {sorted(_STANDING_KEYS)}")
    for key in ("subject", "feature_id"):
        if not (isinstance(entry[key], str) and entry[key].strip()):
            raise HostedConfigError(f"{where}.{key} must be a non-empty string")
    subject, feature_id, state, quota = (entry[k] for k in ("subject", "feature_id", "state", "quota"))
    if state not in STATES:
        raise HostedConfigError(f"{where}.state must be one of {list(STATES)}, got {state!r}")
    if quota is not None and (not isinstance(quota, int) or isinstance(quota, bool)):
        raise HostedConfigError(f"{where}.quota must be null (unlimited) or an integer")
    return (subject, feature_id), Standing(cast(LifecycleState, state), quota)


def load_static_source(path: Path) -> StaticEntitlementSource:
    """The operator's fixed table, validated in full before anything is served."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HostedConfigError(f"{ENTITLEMENTS_FILE_ENV}={path} is unreadable: {exc}") from exc
    if not isinstance(payload, dict) or set(payload) != _FILE_KEYS:
        raise HostedConfigError(f"{path} must be an object with keys {sorted(_FILE_KEYS)}")
    provider, standings = payload["provider"], payload["standings"]
    if not (isinstance(provider, str) and provider.strip()):
        raise HostedConfigError(f"{path}: provider must be a non-empty string")
    if not isinstance(standings, list):
        raise HostedConfigError(f"{path}: standings must be a list")
    table = dict(_standing_entry(e, f"{path}: standings[{i}]") for i, e in enumerate(standings))
    if len(table) != len(standings):
        raise HostedConfigError(f"{path}: a (subject, feature_id) pair is listed twice")
    return StaticEntitlementSource(table, provider=provider)


def _require_env(env: Mapping[str, str], key: str) -> str:
    raw = env.get(key, "").strip()
    if not raw:
        raise HostedConfigError(
            f"{ENTITLEMENTS_PROVIDER_ENV}=polar but {key} is unset. Every Polar "
            "setting is required and none is defaulted; see "
            "apps/sync_hub/hosted_config.py for the list."
        )
    return raw


def polar_config(env: Mapping[str, str], *, snapshot_dir: Path) -> PolarConfig:
    """Read every Polar setting strictly, or raise naming the first bad one."""
    try:
        products = json.loads(_require_env(env, POLAR_PRODUCTS_ENV))
    except json.JSONDecodeError as exc:
        raise HostedConfigError(f"{POLAR_PRODUCTS_ENV} is not JSON: {exc}") from exc
    if not (isinstance(products, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in products.items())):
        raise HostedConfigError(f'{POLAR_PRODUCTS_ENV} must be a JSON object {{"<product id>": "<feature id>"}}')
    raw_days = _require_env(env, POLAR_READ_ONLY_DAYS_ENV)
    if not raw_days.isdigit():
        raise HostedConfigError(f"{POLAR_READ_ONLY_DAYS_ENV} must be a whole number, got {raw_days!r}")
    server = _require_env(env, POLAR_SERVER_ENV)
    try:
        return PolarConfig(
            access_token=_require_env(env, POLAR_TOKEN_ENV),
            server=cast(Any, server),
            product_features=products,
            read_only_days=int(raw_days),
            success_url=_require_env(env, POLAR_SUCCESS_URL_ENV),
            snapshot_path=snapshot_dir / POLAR_SNAPSHOT_NAME,
        )
    except ValueError as exc:
        raise HostedConfigError(f"Polar configuration refused: {exc}") from exc


def require_enforce_for_polar(env: Mapping[str, str]) -> str | None:
    """Why a Polar-backed hub must not serve now, or None when it may."""
    try:
        mode = machine_credentials.configured_mode(env)
    except machine_credentials.CredentialModeError as exc:
        return str(exc)
    if mode == "enforce":
        return None
    return (
        f"{ENTITLEMENTS_PROVIDER_ENV}=polar needs {machine_credentials.MODE_ENV}=enforce, "
        f"got {mode!r}. Without per-machine credentials the hub trusts the "
        "machine_id a caller names, so anyone who learned a paying machine's id "
        "could sync on that plan."
    )


def from_env(env: Mapping[str, str], *, snapshot_dir: Path | None = None) -> HostedConfig:
    """Read the switch, and the source only when the switch is on."""
    if not parse_hosted(env):
        return HostedConfig(hosted=False, source=None)
    provider = env.get(ENTITLEMENTS_PROVIDER_ENV, "").strip()
    if provider == "polar":
        if env.get(ENTITLEMENTS_FILE_ENV, "").strip():
            raise HostedConfigError(
                f"{ENTITLEMENTS_PROVIDER_ENV}=polar and {ENTITLEMENTS_FILE_ENV} are both "
                "set; one hub has one source. Unset one."
            )
        refusal = require_enforce_for_polar(env)
        if refusal is not None:
            raise HostedConfigError(refusal)
        if snapshot_dir is None:
            raise HostedConfigError("a Polar source needs the hub data dir for its snapshot")
        return HostedConfig(
            hosted=True,
            source=PolarEntitlementSource(polar_config(env, snapshot_dir=snapshot_dir)),
        )
    if provider not in ("", "static"):
        raise HostedConfigError(f"{ENTITLEMENTS_PROVIDER_ENV} must be unset, 'static' or 'polar', got {provider!r}")
    raw_path = env.get(ENTITLEMENTS_FILE_ENV, "").strip()
    if not raw_path:
        raise HostedConfigError(
            f"{HOSTED_ENV}=1 but {ENTITLEMENTS_FILE_ENV} is unset. A hosted hub "
            "with no entitlement source cannot say who may sync, and will not "
            f"guess. Point it at a standings file, set {ENTITLEMENTS_PROVIDER_ENV}=polar, "
            f"or unset {HOSTED_ENV}."
        )
    return HostedConfig(hosted=True, source=load_static_source(Path(raw_path)))


# ----- the one-owner rule ---------------------------------------------------


def owner_count(conn: sqlite3.Connection) -> int:
    """Distinct owners with ANY ownership row, revoked included: their rows stay."""
    return int(conn.execute("SELECT COUNT(DISTINCT google_sub) FROM machine_owners").fetchone()[0])


def too_many_owners(count: int) -> str | None:
    """Why a hosted hub must not serve this DB, or None when it may."""
    if count <= MAX_HOSTED_OWNERS:
        return None
    return (
        f"this hosted hub DB holds {count} owners, and a hosted hub serves "
        f"at most {MAX_HOSTED_OWNERS}: pull is not owner-scoped yet, so every "
        "owner would pull every other owner's library. Run one hub DB per "
        "owner until pull is owner-scoped."
    )


def _require_single_owner(db_path: Path) -> None:
    if not db_path.exists():
        return  # A fresh hub: no owners yet, and the gate re-checks per request.
    conn = state_db.open_rw(db_path)
    try:
        reason = too_many_owners(owner_count(conn))
    finally:
        conn.close()
    if reason is not None:
        raise HostedConfigError(reason)


def configure(app: FastAPI, env: Mapping[str, str], *, db_path: Path) -> HostedConfig:
    """Stamp the switch and source onto ``app.state``, or raise at startup."""
    config = from_env(env, snapshot_dir=db_path.parent)
    if config.hosted:
        _require_single_owner(db_path)
    setattr(app.state, HOSTED_FLAG_ATTR, config.hosted)
    setattr(app.state, SOURCE_ATTR, config.source)
    return config


def describe_for_cli(env: Mapping[str, str], data_dir: Path) -> dict[str, Any]:
    """``python -m apps.sync_hub hosted``: the same checks startup runs, as JSON."""
    db_path = state_db_path(data_dir)
    config = from_env(env, snapshot_dir=db_path.parent)
    if config.hosted:
        _require_single_owner(db_path)
    readout = config.describe()
    readout["owners"] = None
    if db_path.exists():
        conn = state_db.open_rw(db_path)
        try:
            readout["owners"] = owner_count(conn)
        finally:
            conn.close()
    return readout


__all__ = [
    "ENTITLEMENTS_FILE_ENV",
    "ENTITLEMENTS_PROVIDER_ENV",
    "HOSTED_ENV",
    "HOSTED_FLAG_ATTR",
    "MAX_HOSTED_OWNERS",
    "POLAR_PRODUCTS_ENV",
    "POLAR_READ_ONLY_DAYS_ENV",
    "POLAR_SERVER_ENV",
    "POLAR_SUCCESS_URL_ENV",
    "POLAR_TOKEN_ENV",
    "SOURCE_ATTR",
    "HostedConfig",
    "HostedConfigError",
    "configure",
    "describe_for_cli",
    "from_env",
    "load_static_source",
    "owner_count",
    "parse_hosted",
    "polar_config",
    "require_enforce_for_polar",
    "too_many_owners",
]
