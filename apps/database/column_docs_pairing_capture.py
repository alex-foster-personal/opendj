"""Column docs for the pairing-capture tables in the shared state DB.

``apps/shared/pairings/schema_sql.py`` holds TWO ladders, not one.
``ensure_phase08_tables`` builds ``pairings`` and ``smartlists``, which
``apps/shared/state/schema.py`` has always declared in
``FOREIGN_AUTHORITY_TABLES``. ``apply_pairing_capture_migrations`` is the
second, and it was declared nowhere: it has its own version counter
(``pairing_capture_schema_meta``, currently v1) and it runs on the daemon's
WRITABLE state.db because ``PairingCaptureRepo`` defaults to
``ensure_schema=True``, so the first POST to a route in
``apps/webui/server/routes/pairing_capture.py`` creates all three tables in
the live file.

Found Wed 9 Sep 2026 by ``scripts/sync_drift_lint.py`` D-08, on the run that
added the second entry point to ``scripts.sync_drift_subject.
STATE_AUTHORITIES``. Until then no inventory, no test and no generated
document knew these tables existed: the state-schema tripwire hand-copied
the authority list and never had the capture ladder in its copy.

``column_docs.py`` merges this dict into ``COLUMN_DOCS``, so
:mod:`apps.database.generate_agents_md` sees one flat mapping regardless of
which module a table's docs live in. The matching one-paragraph table
descriptions are in :mod:`apps.database.table_docs`.
"""

from __future__ import annotations

PAIRING_CAPTURE_COLUMN_DOCS: dict[str, dict[str, str]] = {
    "pairing_capture_schema_meta": {
        "version": (
            "Applied version of the pairing-capture ladder, its OWN counter "
            "(apps.shared.pairings.schema_sql.PAIRING_CAPTURE_SCHEMA_VERSION, "
            "currently 1). Deliberately separate from schema_meta: this "
            "ladder is applied lazily by a webui route rather than by "
            "apps.shared.state.schema.apply_migrations, so folding it into "
            "the shared counter would make the shared runner claim a version "
            "whose DDL it does not own."
        ),
        "applied_at": (
            "RFC 3339 UTC timestamp the version row was written, from "
            "datetime.now(UTC).isoformat() inside "
            "apply_pairing_capture_migrations."
        ),
    },
    "pairing_sync_snapshots": {
        "id": (
            "Primary key, a uuid4 minted by "
            "apps.shared.pairings.capture_repo.PairingCaptureRepo."
            "add_snapshot. Client-supplied ids are never accepted."
        ),
        "stable_a": (
            "The A-side track. NOT FK-declared despite pointing at "
            "tracks(stable_id); the repo enforces only that the two sides "
            "are non-empty and distinct."
        ),
        "stable_b": "The B-side track, under the same rules as stable_a.",
        "master_side": (
            "CHECK IN ('a','b'). Which side was the tempo master when the "
            "snapshot was taken, so a replay knows which deck the other was "
            "following."
        ),
        "sync_mode": (
            "CHECK IN ('bar','beat'). Whether the sync was locked at bar or "
            "at beat granularity."
        ),
        "a_tempo_ratio": (
            "Playback rate applied to the A-side at capture time, as a "
            "ratio of its own analyzed tempo (1.0 = unpitched). Validated "
            "finite and non-negative."
        ),
        "b_tempo_ratio": "The same ratio for the B-side.",
        "a_position_beat_n": (
            "A-side position as a beat ordinal within the analyzed grid, or "
            "NULL when the capture carried no grid-relative position."
        ),
        "a_position_phase": (
            "A-side fractional position WITHIN that beat, or NULL. Paired "
            "with a_position_beat_n; neither is validated by the repo, "
            "which checks only the millisecond positions."
        ),
        "a_position_ms": (
            "A-side playhead position in MILLISECONDS, the authoritative "
            "one: NOT NULL and validated finite and non-negative, where the "
            "beat/phase pair above is optional."
        ),
        "b_position_beat_n": "The B-side beat ordinal, under the same rules.",
        "b_position_phase": "The B-side within-beat fraction, under the same rules.",
        "b_position_ms": "The B-side playhead position in milliseconds.",
        "captured_at": (
            "RFC 3339 UTC timestamp the snapshot was recorded, stamped "
            "server-side by the repo (datetime.now(UTC).isoformat()) rather "
            "than taken from the request. Indexed DESC alongside each side's "
            "stable id (idx_pairing_snapshots_a / _b) so the most recent "
            "capture for a track is a leading-edge lookup."
        ),
    },
    "pairing_alignments": {
        "id": (
            "Primary key, a uuid4 minted by PairingCaptureRepo.add_alignment."
        ),
        "stable_a": (
            "The A-side track. NOT FK-declared, same as the snapshot table."
        ),
        "stable_b": "The B-side track.",
        "anchor_a_kind": (
            "CHECK IN ('hotcue','ms'). Whether the A-side anchor is a named "
            "hot cue or a raw millisecond offset. When it is 'hotcue', "
            "anchor_a_slot names the cue and anchor_a_ms carries the "
            "resolved position."
        ),
        "anchor_b_kind": "The same discriminator for the B-side anchor.",
        "anchor_a_slot": (
            "Hot-cue slot label for the A-side anchor, or NULL when "
            "anchor_a_kind is 'ms'. Free text at the DB layer: the CHECK "
            "constrains the KIND, not the slot."
        ),
        "anchor_b_slot": "The B-side slot label, under the same rules.",
        "anchor_a_ms": (
            "Resolved A-side anchor position in MILLISECONDS, NOT NULL for "
            "both kinds, so a reader never has to resolve a hot cue to use "
            "the alignment."
        ),
        "anchor_b_ms": "The resolved B-side anchor position in milliseconds.",
        "label": (
            "Optional human label for this alignment mark (NULL when the "
            "caller supplied none). Display only; nothing keys on it."
        ),
        "created_at": (
            "RFC 3339 UTC timestamp the alignment was recorded, stamped "
            "server-side. Indexed DESC alongside each side's stable id "
            "(idx_pairing_alignments_a / _b)."
        ),
    },
}

__all__ = ["PAIRING_CAPTURE_COLUMN_DOCS"]
