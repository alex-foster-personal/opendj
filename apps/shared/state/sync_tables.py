"""LWW sync-set table catalogue owned by ``apps.shared``.

Migrations and writers can name sync-set tables without importing
``apps.sync_hub``. :mod:`apps.sync_hub.protocol_common` re-exports the same
objects.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["SPEC_BY_TABLE", "SYNC_TABLES", "TableSpec", "pk_predicate"]


@dataclass(frozen=True)
class TableSpec:
    """One table in the LWW sync set."""

    name: str
    pk: tuple[str, ...]


# Order is the APPLY order and is FK-safe: parents before children. Callers
# must not reorder without re-checking the REFERENCES clauses in
# apps/shared/state/schema.py.
SYNC_TABLES: tuple[TableSpec, ...] = (
    TableSpec("tracks", ("stable_id",)),
    # Immediately after its parent: lyric_verdict's only REFERENCES clause is
    # to tracks(stable_id), so slot 1 keeps the parent adjacency the comment
    # above describes.
    TableSpec("lyric_verdict", ("stable_id",)),
    # References tracks(stable_id); playlist_memberships.edit_id needs the row first.
    TableSpec("track_edit", ("id",)),
    TableSpec("playlists", ("playlist_id",)),
    TableSpec("track_vendor_ids", ("stable_id", "vendor")),
    TableSpec("track_fields", ("stable_id", "field_name")),
    TableSpec("track_locations", ("location_id",)),
    TableSpec("sync_policies", ("machine_id", "asset_kind")),
    TableSpec("playlist_pins", ("machine_id", "playlist_id")),
    # v12 (FBSYNC-01, ADR-0013). No REFERENCES clause, so its slot in the
    # apply order is free; last keeps every existing table's rank unchanged.
    TableSpec("feedback_pins", ("pin_id",)),
    # v26 (Library Organization PRD s6). No REFERENCES clauses; a child never
    # waits on a parent, so these slots are free too.
    TableSpec("playlist_section", ("id",)),
    TableSpec("tag_type", ("id",)),
    TableSpec("tag_meta", ("name_norm",)),
    TableSpec("tag_synonym", ("term_norm", "tag_name_norm")),
    TableSpec("work", ("id",)),
    TableSpec("work_member", ("work_id", "member_kind", "member_id")),
    TableSpec("work_dismissal", ("a_member", "b_member")),
    TableSpec("work_pref", ("work_id",)),
)

SPEC_BY_TABLE: dict[str, TableSpec] = {spec.name: spec for spec in SYNC_TABLES}


def pk_predicate(spec: TableSpec) -> str:
    return " AND ".join(f"{column} = ?" for column in spec.pk)
