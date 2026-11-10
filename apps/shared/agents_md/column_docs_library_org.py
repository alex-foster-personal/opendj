"""Column docs for the library organization tables (schema v26)."""

from __future__ import annotations

_CREATED = "UTC stamp of when this row was first written."
_UPDATED = "Canonical sync stamp of the last edit; the last-writer-wins order."
_ORIGIN = "Machine that made the last edit; the last-writer-wins tiebreak."
_DELETED = "Tombstone stamp. Set means removed everywhere; NULL means live."


def _with_sync(columns: dict[str, str]) -> dict[str, str]:
    return {
        **columns,
        "created_at": _CREATED,
        "updated_at": _UPDATED,
        "origin_device_id": _ORIGIN,
        "deleted_at": _DELETED,
    }


LIBRARY_ORG_COLUMN_DOCS: dict[str, dict[str, str]] = {
    "playlist_section": _with_sync(
        {
            "id": "Primary key, a random hex id.",
            "owner_kind": "'playlist' or 'set': what kind of list this section sits in.",
            "owner_id": "The playlist_id, or the playlist_sets id as text.",
            "before_position": "The section renders before the entry at this position.",
            "title": "The section's name. Never blank.",
            "color": "Optional colour name; NULL for none.",
        }
    ),
    "tag_type": _with_sync(
        {
            "id": "Primary key, e.g. general, instrument, mood.",
            "label": "Plural name shown as a group or column heading.",
            "singular": "Name for one tag of this type.",
            "show_as_column": "1 when this type has its own table column.",
            "show_in_tags": "1 when this type shows in the Tags cell and tab.",
            "column_width": "Column width in pixels; NULL for the default.",
            "sort_index": "Order among tag types.",
        }
    ),
    "tag_meta": _with_sync(
        {
            "name_norm": "Primary key: the tag name, NFKC, casefolded and trimmed.",
            "display_name": "The tag name as the user typed it.",
            "type": "The tag_type id. Enforced by the service, not a foreign key.",
            "color": "Optional colour name; NULL for the type's colour.",
            "source_category": "The external grouping it was imported from. Provenance only.",
        }
    ),
    "tag_synonym": _with_sync(
        {
            "term_norm": "The normalized term someone might type.",
            "tag_name_norm": "The tag_meta name it should suggest.",
        }
    ),
    "work": _with_sync(
        {
            "id": "Primary key, a random hex id.",
            "kind": "'classical' or 'song'.",
            "title": "The work's title.",
            "composer": "Composer, classical works mostly.",
            "catalogue": "Catalogue number, e.g. K. 626.",
            "movement": "Movement, when the work is one movement.",
            "aliases": "JSON list of other names for this work.",
            "musicbrainz_work_id": "MusicBrainz work id, when known.",
            "iswc": "International Standard Musical Work Code, when known.",
        }
    ),
    "work_member": _with_sync(
        {
            "work_id": "The work.",
            "member_kind": "'track' or 'edit'.",
            "member_id": "The track stable_id or the edit id.",
            "role": "original, recording, remix, cover, edit, live or mashup.",
            "is_primary": "1 for the member's primary work. The service keeps at most one.",
            "sort_index": "Order inside the stack.",
        }
    ),
    "work_dismissal": _with_sync(
        {
            "a_member": "The lower-sorting member of the dismissed pair.",
            "b_member": "The higher-sorting member of the dismissed pair.",
            "work_id": "The work the suggestion named, if any.",
        }
    ),
    "work_pref": _with_sync(
        {
            "work_id": "Primary key: the work.",
            "top_member_id": "The member shown first. Display only.",
        }
    ),
    "track_edit": _with_sync(
        {
            "id": "Primary key, a random hex id.",
            "source_track_id": "The source track whose audio is reused; service-enforced, not a foreign key.",
            "label": "The edit title shown in the library.",
            "in_ms": "Inclusive playback start in source milliseconds.",
            "out_ms": "Exclusive playback end in source milliseconds.",
            "fx": "JSON object of stored effect parameters.",
            "created_by": "Undo session or actor that created the edit.",
        }
    ),
    "library_completeness_config": {
        "id": "Singleton primary key; always 1.",
        "expect_hq_stems_and_lyric_match": (
            "When 1, every track that can have HQ stems and a lyric match is expected to have both."
        ),
        "seeded_at": "RFC 3339 UTC when the row was first written by migration v31.",
    },
    "undo_journal": {
        "id": "Primary key and the undo_id a write returns.",
        "session_id": "The undo stack this entry belongs to.",
        "ts": "When the operation ran.",
        "seq": "Order within the session.",
        "op": "Operation name, e.g. section.add.",
        "before": "JSON list of each changed row before the write.",
        "after": "JSON list of each changed row after the write.",
        "undone_at": "When it was undone; NULL while it can still be undone.",
    },
}

PLAYLIST_ORG_COLUMN_DOCS: dict[str, str] = {
    "parent_id": "The folder or list this sits under; NULL at the top level.",
    "kind": "'folder', 'playlist' or 'set'.",
    "stage": "Optional workflow stage: inbox, sorting, ready or archive.",
    "set_status": "For a set only: draft, ready, performed or background.",
    "sort_index": "Order among siblings; NULL for the default order.",
}

__all__ = ["LIBRARY_ORG_COLUMN_DOCS", "PLAYLIST_ORG_COLUMN_DOCS"]
