"""Column docs for the four analysis-retention tables, split out of
:mod:`apps.database.column_docs` (the file-size review gate; same pattern as
the round 2 ``TABLE_DOCS`` split into :mod:`apps.database.table_docs`).

``column_docs.py`` merges this dict into ``COLUMN_DOCS``, so
:mod:`apps.database.generate_agents_md` sees one flat mapping regardless of
which module a table's docs happen to live in.

See :mod:`apps.database.column_docs` for the coverage rules these tables
must satisfy.
"""

from __future__ import annotations

ANALYSIS_RETENTION_COLUMN_DOCS: dict[str, dict[str, str]] = {
    "track_availability": {
        "stable_id": (
            "The track this availability row concerns. FK ON DELETE "
            "CASCADE -- but a soft delete only sets tracks.deleted_at and "
            "never issues a physical DELETE, so this row survives a "
            "tombstone; readers still need an explicit deleted_at filter."
        ),
        "state": (
            "Whether the audio is present on disk right now: 'present', "
            "'absent', 'awaiting_volume' (path is on an unmounted "
            "volume), or 'streaming' (no local file expected)."
        ),
        "checked_path": "The path that was actually probed, if any.",
        "checked_at": "RFC 3339 UTC timestamp of the last availability check.",
    },
    "unmatched_source_analysis": {
        "id": "Surrogate AUTOINCREMENT primary key.",
        "source": (
            "Which vendor or pipeline produced this analysis row (e.g. "
            "'mik', 'rekordbox')."
        ),
        "source_row_id": "The source's own row identifier, opaque here.",
        "field_name": "Which analysed field this row carries.",
        "value_json": "The analysed value, JSON-encoded.",
        "unmatched_reason": (
            "Why no stable_id was assigned: 'no_candidate' (nothing "
            "matched), 'ambiguous_candidates' (several equally good "
            "matches), or 'lost_collision' (a better-tier source row won "
            "the same track). All three mean the same thing operationally "
            "-- we do not know which track this analysis belongs to."
        ),
        "confidence": "Confidence score for this value, if any.",
        "title": "Track title as read from the source, for human triage.",
        "artist": "Track artist as read from the source, for human triage.",
        "album": "Track album as read from the source, for human triage.",
        "isrc": "Raw ISRC as read from the source, if any.",
        "duration_ms": "Track duration in milliseconds, as read from the source.",
        "source_path": "The source's own file path, for human triage.",
        "modified_at": "When the source last modified this value.",
        "imported_at": "RFC 3339 UTC timestamp of when this row was staged.",
        "promoted_stable_id": (
            "The tracks row this analysis was later matched to, if any. "
            "The one-way door out of staging into track_fields "
            "(docs/analysis-retention.md)."
        ),
        "promoted_at": "RFC 3339 UTC timestamp of the promotion, if any.",
    },
    "track_energy_segments": {
        "stable_id": "The track this energy segment belongs to.",
        "seq": "Sequence number of this segment within the track, from 0.",
        "start_ms": (
            "Segment start time in milliseconds. See start_clamped for "
            "the 77 rows whose source start time was negative float dust."
        ),
        "length_ms": "Segment length in milliseconds.",
        "energy": "Energy level for this segment, 1-10.",
        "source": (
            "Which vendor or pipeline produced this segment (e.g. 'mik')."
        ),
        "confidence": "Confidence score for this segment, if any.",
        "start_clamped": (
            "1 if the source start time was clamped up from negative "
            "float dust to 0, so a reader can tell a genuine 0 start from "
            "a clamped one."
        ),
        "modified_at": "When this segment was last written.",
    },
    "analysis_field_verification": {
        "source": "Which source's mapping this verification concerns.",
        "field_name": "Which field this verification concerns.",
        "status": "The verdict status recorded at verification time.",
        "basis": (
            "Strength of evidence: 'cross_source' (agreement between two "
            "independent sources), 'single_source' (a one-sided probe), "
            "or 'unverified'."
        ),
        "normaliser": "Which normaliser was used to compare values, if any.",
        "checked_at": "RFC 3339 UTC timestamp of the verification.",
        "verified_by": "Who or what performed the verification.",
        "overridden": (
            "1 if a human forced a write past a non-passing verdict."
        ),
        "recorded_at": "RFC 3339 UTC timestamp of when this row was written.",
    },
}
