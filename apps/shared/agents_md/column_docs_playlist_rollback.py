"""Column docs for leftover playlists columns from reverted library-org v25.

#7886 added parent_id, kind, stage, set_status, sort_index to playlists.
#7921 reverted that schema. SQLite cannot DROP COLUMN, so a library that
ran the reverted v25 still has these five columns. open_rw regenerates
AGENTS.md and MissingColumnDocsError refuses an owned-table column with
no doc (#7922). These entries keep that open path working.

They are not live on a fresh main DB. generate_agents_md renders only
live columns, so a new library never sees them in the sidecar.

When PR #7926 (schema v26 reland) lands, it re-adds
column_docs_library_org.PLAYLIST_ORG_COLUMN_DOCS with the same keys.
Delete this module in that follow-up; until then a duplicate spread is
harmless (later ** wins).
"""

from __future__ import annotations

PLAYLIST_ROLLBACK_COLUMN_DOCS: dict[str, str] = {
    "parent_id": "The folder or list this sits under; NULL at the top level.",
    "kind": "'folder', 'playlist' or 'set'.",
    "stage": "Optional workflow stage: inbox, sorting, ready or archive.",
    "set_status": "For a set only: draft, ready, performed or background.",
    "sort_index": "Order among siblings; NULL for the default order.",
}
