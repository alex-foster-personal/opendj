"""Column docs for the karaoke lyrics verdict table (schema v10).

Split out of ``column_docs.py`` for the same reason
:mod:`apps.database.column_docs_analysis_retention` and
:mod:`apps.database.column_docs_native_analysis` were (the 600-line-per-
Python-file gate); ``column_docs.py`` merges this dict into ``COLUMN_DOCS``,
so callers still read one map.

Schema authority: ``apps/shared/state/migrations_v10.py`` (``_V10``), mirrored
into the consolidated engine schema's ``lyrics`` domain.
Design: ``specs/karaoke-lyrics-operational-plan.md`` D13.1 and D13.2.

-Claude
"""

from __future__ import annotations

LYRICS_COLUMN_DOCS: dict[str, dict[str, str]] = {
    "lyric_verdict": {
        "stable_id": (
            "Primary key AND the FK -> tracks(stable_id) ON DELETE CASCADE: "
            "one verdict per track. The cascade never actually fires -- a "
            "hard DELETE of a tracks row is forbidden repo-wide -- which is "
            "why every reader filters the PARENT's deleted_at too."
        ),
        "verdict": (
            "CHECK IN ('vocal', 'sparse', 'no-lyrics', 'unknown') -- the "
            "COMPUTED verdict. Mirrored in Python as "
            "apps.shared.state.schema.LYRIC_VERDICTS. Read it through the "
            "effective-verdict rule (override first, this second), never on "
            "its own."
        ),
        "coverage_pct": (
            "Percentage 0-100 of the track's vocal-bearing time that "
            "aligned words cover. NULL when no alignment was attempted. "
            "The no-lyrics threshold calibrated against the maintainer's ear lives in "
            "apps/lyrics/vocal_presence.py, not here."
        ),
        "source": (
            "Which provider the lyric text came from, as a prefix-matchable "
            "string (e.g. 'lrclib', 'musixmatch'). The purge CLI selects on "
            "source LIKE <prefix>%, so this column IS the licensing lever."
        ),
        "language_iso3": (
            "ISO 639-3 language code the aligner detected for the lyric "
            "text, or NULL when undetermined."
        ),
        "n_words": (
            "Number of words in the karaoke_words artifact. Denormalised "
            "onto the row so a SQL filter never has to open the JSON; the "
            "ingest fails loudly if it disagrees with the artifact."
        ),
        "n_lines": (
            "Number of lines derived from those words by "
            "apps.lyrics.lines.derive_lines. Denormalised for the same "
            "reason as n_words, and the column that replaced the retired "
            "per-word table's bulk_line_counts query."
        ),
        "pct_witness_red": (
            "Fraction 0-1 of words the ASR witness flagged as suspect. The "
            "triage sort key: idx_lyric_verdict_red orders worst-first on "
            "it. NULL when the witness did not run."
        ),
        "override": (
            "CHECK IN ('vocal', 'sparse', 'no-lyrics') or NULL -- the "
            "HUMAN verdict, which wins over the computed one and survives "
            "every recompute (a recompute writes every other column and "
            "deliberately never touches this one). 'unknown' is absent from "
            "the vocabulary on purpose: reverting to 'we do not know' is "
            "spelled NULL. Mirrored as schema.LYRIC_OVERRIDES."
        ),
        "override_note": (
            "Free-text reason the human gave for the override, or NULL. "
            "Written and cleared with override, never on its own."
        ),
        "pipeline_version": (
            "The apps.lyrics.karaoke_cache.PIPELINE_VERSION that produced "
            "this row, grammar '<YYYY.MM.DD>-<round>' (e.g. "
            "'2026.09.09-round3a'). Bumped whenever the aligner, the "
            "witness or the writer changes output bytes, so a row can be "
            "told apart from a row a later round would produce."
        ),
        "words_content_hash": (
            "sha256 (64 lowercase hex chars, CHECKed on length) of the "
            "canonical bytes of the karaoke_words artifact, or NULL when "
            "there are no words. This is the ONLY location record for that "
            "artifact: there is deliberately no track_locations row for it, "
            "because track_locations has no asset_kind column and a words "
            "hash reaching audio resolution would poison playback."
        ),
        "computed_at": (
            "RFC 3339 UTC timestamp of the alignment run that produced this "
            "row. Distinct from updated_at, which moves on any write "
            "including an override or a tombstone."
        ),
        "updated_at": (
            "Sync LWW timestamp. NOT NULL, unlike sync_policies' -- a peer "
            "offering a NULL-stamped verdict row fails the whole apply "
            "batch instead of storing a row that silently loses every "
            "conflict it takes part in."
        ),
        "origin_device_id": "Writing machine's machine_id.",
        "deleted_at": (
            "Tombstone timestamp. NULL = live. Set by the licensing purge, "
            "which never issues a DELETE; the tombstone syncs, so peers "
            "stop hydrating the words too. Sticky: an upsert refuses to "
            "revive a tombstoned row unless asked to in so many words."
        ),
    },
}

__all__ = ["LYRICS_COLUMN_DOCS"]
