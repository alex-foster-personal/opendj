"""Types for the CloudSync data-class registry (:mod:`apps.sync_hub.data_classes`).

Split out so the two catalogs (``data_classes_tables``, ``data_classes_files``)
and the registry module can share them without an import cycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

#: Where the bytes of a data class physically live.
StorageKind = Literal[
    "state_db_table",  # a table in <data>/state/state.db (either ladder)
    "cache_db_table",  # a table in the engine's regenerable cache.db
    "vendor_db_table",  # a table inside a vendor DB (rekordbox master.db)
    "file",  # a path or glob on disk; <data> = MDT_DATA_DIR, <repo> = checkout
    "r2",  # an object key template in the Cloudflare R2 asset bucket
    "git",  # tracked content of this git repository
    "external",  # held by another system (Doppler, browser localStorage)
]

#: How a data class moves between machines, if at all.
Mechanism = Literal[
    "sync_hub_changelog",  # LWW rows through the hub changelog (SYNC_TABLES)
    "sync_hub_registry",  # the owner-scoped machines registry snapshot
    "r2_asset_tier",  # content-addressed R2 objects governed by sync_policies
    "git",  # the repo itself, kept current by autoreposync
    "machine_local",  # never leaves the machine, by design
    "not_yet_built",  # should move between machines but nothing moves it yet
]

MECHANISMS: tuple[Mechanism, ...] = (
    "sync_hub_changelog",
    "sync_hub_registry",
    "r2_asset_tier",
    "git",
    "machine_local",
    "not_yet_built",
)
"""Every mechanism, in the order the generated doc groups them."""

#: The per-machine modes ``sync_policies.mode`` admits (schema v7 CHECK).
PolicyMode = Literal["pinned", "cached", "stream", "excluded"]

POLICY_MODES: tuple[PolicyMode, ...] = ("pinned", "cached", "stream", "excluded")

#: How a gitignored path is classified.
IgnoredKind = Literal[
    "user_data",  # library, caches or config a user would lose; names a class
    "build_output",  # regenerated from source by a build or tool install
    "runtime_state",  # locks, logs, agent bookkeeping: machine churn, not data
]


@dataclass(frozen=True)
class Location:
    """One place a data class's bytes live."""

    kind: StorageKind
    where: str


@dataclass(frozen=True)
class Dependency:
    """One data class this class needs to be meaningful.

    ``fk`` mirrors a schema ``REFERENCES`` clause (pinned by a test against
    ``PRAGMA foreign_key_list`` on both ladders); ``logical`` is a
    relationship the schema does not declare.
    """

    target: str
    kind: Literal["fk", "logical"]


@dataclass(frozen=True)
class DataClass:
    """One class of data, with how (and whether) it moves between machines.

    ``configurable_per_machine`` is True exactly when a machine may pick its
    own mode, which today is exactly the six ``sync_policies.asset_kind``
    values; ``allowed_modes`` is empty otherwise and ``asset_kind`` names the
    ``sync_policies`` key for a configurable class.
    """

    id: str
    title: str
    storage: tuple[Location, ...]
    mechanism: Mechanism
    configurable_per_machine: bool
    allowed_modes: tuple[PolicyMode, ...]
    depends_on: tuple[Dependency, ...]
    reason: str
    asset_kind: str | None

    def tables(self) -> tuple[str, ...]:
        """Every database table this class owns, in declaration order."""
        return tuple(
            loc.where
            for loc in self.storage
            if loc.kind in ("state_db_table", "cache_db_table", "vendor_db_table")
        )


@dataclass(frozen=True)
class IgnoredPath:
    """One ``.gitignore`` pattern and what it holds."""

    pattern: str
    kind: IgnoredKind
    data_class: str | None


# ----- constructors (keep the catalogs short) ---------------------------------


def fk(target: str) -> Dependency:
    """A dependency that mirrors a schema ``REFERENCES`` clause."""
    return Dependency(target, "fk")


def logical(target: str) -> Dependency:
    """A dependency the schema does not declare."""
    return Dependency(target, "logical")


def state_tables(*names: str) -> tuple[Location, ...]:
    """``state.db`` table locations, one per name."""
    return tuple(Location("state_db_table", name) for name in names)


def files(*paths: str) -> tuple[Location, ...]:
    """File or glob locations, one per path."""
    return tuple(Location("file", path) for path in paths)


def fixed(
    class_id: str,
    title: str,
    storage: tuple[Location, ...],
    mechanism: Mechanism,
    reason: str,
    depends_on: tuple[Dependency, ...],
) -> DataClass:
    """A class no machine can configure: it syncs, or it does not, for everyone."""
    return DataClass(
        id=class_id,
        title=title,
        storage=storage,
        mechanism=mechanism,
        configurable_per_machine=False,
        allowed_modes=(),
        depends_on=depends_on,
        reason=reason,
        asset_kind=None,
    )
