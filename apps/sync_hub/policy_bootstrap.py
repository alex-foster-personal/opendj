"""Seed this spoke's ``sync_policies`` from class defaults when CloudSync is on.

CLOUDSYNC-25 turns sync on at first run without writing ``sync_policies``.
``resolve_playback_source`` then raises ``CLOUD_POLICY_UNCONFIGURED`` for any
track that has no on-disk local copy (Sentry OPEN-DJ-BE-6Q / deck-error).
Local-only installs stay unseeded: CloudSync off means we never write
fleet-synced rows.

Writes go only through :func:`apps.sync_hub.policy_store.apply_proposal`.
A re-run is a no-op when any live cell already exists for this machine.
``run_sync`` does not call this: a first sync must not invent a policy
matrix on an unseeded enroll or a test spoke.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from apps.cloud.policy import MACHINE_CLASSES, MachineClass
from apps.shared.state import db as state_db
from apps.shared.state.machine_identity import is_hub_from_env, register_machine
from apps.sync_hub import config as sync_config
from apps.sync_hub.client_transport_ops import state_db_path
from apps.sync_hub.policy_store import (
    PolicyOutcome,
    apply_proposal,
    author_machine_id,
    seed_proposal,
)

log = logging.getLogger(__name__)

DEFAULT_SEED_CLASS: MachineClass = "portable"
SEED_CLASS_ENV: str = "MDT_CLOUDSYNC_SEED_CLASS"

BootstrapStatus = Literal[
    "skipped-off",
    "skipped-hub",
    "already-seeded",
    "seeded",
    "blocked",
]


@dataclass(frozen=True)
class BootstrapResult:
    """What automatic class-default seeding did for this machine."""

    status: BootstrapStatus
    written: bool
    machine_class: MachineClass | None
    detail: str | None = None


def resolve_seed_class(env: Mapping[str, str] | None = None) -> MachineClass:
    """Class defaults used for the first automatic seed. Portable is the default.

    Portable caches audio (CLOUDSYNC-10 / CLOUDSYNC-01) instead of pinning
    the whole library onto a fresh test device.
    """
    source = os.environ if env is None else env
    raw = str(source.get(SEED_CLASS_ENV, "")).strip() or DEFAULT_SEED_CLASS
    if raw not in MACHINE_CLASSES:
        raise sync_config.CloudSyncConfigError(f"{SEED_CLASS_ENV}={raw!r} is not one of {list(MACHINE_CLASSES)}")
    return raw  # type: ignore[return-value]


def self_has_live_policy(conn: sqlite3.Connection, machine_id: str) -> bool:
    """True when this machine already has at least one live ``sync_policies`` cell."""
    row = conn.execute(
        "SELECT 1 FROM sync_policies WHERE machine_id = ? AND deleted_at IS NULL LIMIT 1",
        (machine_id,),
    ).fetchone()
    return row is not None


def ensure_self_class_seeded(
    conn: sqlite3.Connection,
    *,
    machine_class: MachineClass | None = None,
    env: Mapping[str, str] | None = None,
    live: bool = True,
    machine_id: str | None = None,
) -> BootstrapResult:
    """Write class-default cells for this machine when it has none.

    ``machine_id`` is the registered identity; it defaults to the local
    machine-id file, but a restored data dir whose name was already taken
    adopts the stored row (``register_machine``), so the caller passes that.
    The machine must already be in ``machines``. Existing cells win: we never
    overwrite an operator or hub-synced policy. A blocked gate is returned,
    not raised, so engine boot cannot die on a fleet-completeness error.
    """
    chosen = resolve_seed_class(env) if machine_class is None else machine_class
    author = author_machine_id(conn)
    machine_id = author if machine_id is None else machine_id
    if self_has_live_policy(conn, machine_id):
        return BootstrapResult("already-seeded", False, chosen, None)
    proposed = seed_proposal(author, machine_id, chosen)
    outcome: PolicyOutcome = apply_proposal(conn, proposed, live=live)
    if outcome.blocking or not outcome.measurable:
        blocking = [v.rule_id for v in outcome.violations if v.blocking]
        detail = f"automatic {chosen} seed blocked (measurable={outcome.measurable} blocking={blocking})"
        log.warning("cloudsync policy bootstrap: %s", detail)
        return BootstrapResult("blocked", False, chosen, detail)
    if outcome.written:
        log.info(
            "cloudsync policy bootstrap: seeded %s class defaults for machine %s",
            chosen,
            machine_id,
        )
        return BootstrapResult("seeded", True, chosen, None)
    return BootstrapResult("already-seeded", False, chosen, None)


def ensure_enabled_spoke_policy(
    data_dir: Path,
    *,
    env: Mapping[str, str] | None = None,
) -> BootstrapResult:
    """Register this spoke and seed it when CloudSync is configured.

    No-op on the hub, when CloudSync is off, or when a config file is
    unreadable (same idle posture as the scheduler).
    """
    source = os.environ if env is None else env
    if is_hub_from_env(dict(source)):
        return BootstrapResult("skipped-hub", False, None, None)
    try:
        effective = sync_config.resolve_config(data_dir, env=source)
    except sync_config.CloudSyncConfigError as exc:
        log.warning("cloudsync policy bootstrap: config unreadable, leaving policies: %s", exc)
        return BootstrapResult("skipped-off", False, None, str(exc))
    if not effective.configured:
        return BootstrapResult("skipped-off", False, None, None)
    conn = state_db.open_rw(state_db_path(data_dir))
    try:
        identity = register_machine(
            conn,
            data_dir=data_dir,
            name=effective.machine_name,
        )
        return ensure_self_class_seeded(conn, env=source, machine_id=identity.machine_id)
    finally:
        conn.close()


__all__ = [
    "DEFAULT_SEED_CLASS",
    "SEED_CLASS_ENV",
    "BootstrapResult",
    "BootstrapStatus",
    "ensure_enabled_spoke_policy",
    "ensure_self_class_seeded",
    "resolve_seed_class",
    "self_has_live_policy",
]
