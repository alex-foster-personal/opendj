"""Migration ladder (v10) for the shared state schema: karaoke lyric verdicts.

Fourth module in the split ladder (:mod:`apps.shared.state.migrations` holds
v1-v5, :mod:`apps.shared.state.migrations_v6_v8` v6-v8 and
:mod:`apps.shared.state.migrations_v9` v9, machine enrollment). The split is a
file-size decision, not a semantic one (issue #1583); v10 gets its own module
so ``migrations_v6_v8.py`` stays well clear of the 600-line gate and so the
one migration that REBUILDS a synced table in this round is easy to find.
:data:`apps.shared.state.schema.MIGRATIONS` assembles all four parts in
order and ``schema.apply_migrations`` is the runner.

Two things happen in step 9 -> 10, and they belong together because the second
is what makes the first useful on a fleet:

1. ``lyric_verdict`` -- one synced row per track carrying the karaoke lyrics
   verdict, its provenance and the content hash of the word-timing artifact.
   ``specs/karaoke-lyrics-operational-plan.md`` D13.1.
2. ``sync_policies`` is REBUILT to widen its ``asset_kind`` CHECK from four
   kinds to six, admitting ``lyrics_cache`` (which unblocks #1470) and the new
   ``karaoke_words`` artifact kind (D13.2).

Five deliberate readings, recorded here so a reader does not have to diff the
spec:

1. ``updated_at TEXT NOT NULL``, matching ``tracks`` and ``track_locations``
   rather than the DDL-nullable ``sync_policies``/``playlist_pins`` shape.
   ``apps.sync_hub.engine_apply._upsert`` turns an IntegrityError into a
   SyncApplyError that fails the whole batch, so a peer offering a
   NULL-stamped verdict row is refused loudly instead of storing a row that
   would read as epoch-old and silently lose every conflict it takes part in.
   The ``verdict`` CHECK enum has the same property: only ever widen it by a
   rebuild in a LATER ladder step, never in place.
2. ``CREATE INDEX idx_lyric_verdict_red`` is BARE -- no ``IF NOT EXISTS``.
   Verified on SQLite 3.51.0: renaming a table aside carries its indexes with
   it under their original names, so a DB that still holds a branch-era
   ``lyric_verdict`` renamed to ``lyric_verdict_legacy`` also still holds an
   ``idx_lyric_verdict_red`` attached to THAT table. With ``IF NOT EXISTS``
   this statement would be a silent no-op and the new table would ship
   without its triage index. Bare, it fails the migration loudly and the
   D13.6 runbook's ``DROP INDEX`` step is not optional. Same reasoning as the
   bare ``CREATE TABLE`` in the v7 track_locations rebuild.
3. The ``sync_policies`` rebuild copies column names, order and types
   VERBATIM. ``apps.sync_hub.protocol.table_digest`` hashes the column list
   from ``PRAGMA table_info`` and never sees CHECK text, so a v9 peer and a
   v10 peer still agree on the sync_policies digest. Reorder or rename one
   column and they no longer do.
4. ``deleted_at`` is copied explicitly in the rebuild's INSERT. Dropping
   tombstones would make this machine's digest disagree with every peer that
   still remembers the delete.
5. The rebuild writes NO ``local_changelog`` entry. Row identity
   ``(machine_id, asset_kind)`` is preserved and no value changes, so nothing
   happened that a peer needs to hear about.

The new table's ``ON DELETE CASCADE`` never actually fires: hard ``DELETE``
of a ``tracks`` row is forbidden repo-wide (``tests/cloudsync/
test_soft_delete.py``), so readers filter the parent's ``deleted_at`` as well
as their own. The FK is declared anyway, for the same reason
``track_availability`` declares one.
"""
from __future__ import annotations

#: Every value the ``sync_policies.asset_kind`` CHECK admits from v10 onward.
#: The single source of truth for the CHECK text below, and the set the
#: artifact-tier vocabulary (``apps.cloud.policy.ARTIFACT_KINDS``,
#: ``apps.cloud.hydration_core.ASSET_KINDS``, the cloudsync route's
#: ``AssetKind`` literal and the frontend's ``ASSET_KINDS``) is pinned equal
#: to by ``tests/cloud/test_asset_kind_vocabulary.py``. Order is the v7 order
#: plus the two new kinds appended, so the stored CHECK text of an existing
#: kind never moves.
ASSET_KIND_CHECK_VALUES: tuple[str, ...] = (
    "audio",
    "stem_bundle",
    "anlz_cache",
    "vocal_cache",
    "lyrics_cache",
    "karaoke_words",
)


def _sql_in_list(values: tuple[str, ...]) -> str:
    """``('a','b')``-style SQL literal list for an ``IN`` CHECK constraint.

    Spelled without spaces after the commas because that is how every other
    CHECK in this ladder is written, and the consolidated mirror in
    ``apps/engine_core/store/schema.py`` is compared against the stored text
    character for character by ``tests/engine_core/test_store_schema.py``.
    """
    return ",".join(f"'{value}'" for value in values)


_V10: list[str] = [
    # --- the karaoke lyrics verdict row (D13.1) --------------------------
    # Sixteen columns: stable_id, twelve domain columns, then the sync trio
    # LAST, exactly as every other synced table in this ladder orders them.
    # The three sync columns are declared character-for-character as
    # ``tracks`` declares them (TEXT NOT NULL / TEXT / TEXT).
    #
    # words_content_hash is the sha256 of the canonical bytes of the
    # ``karaoke_words`` artifact and is the ONLY location record for it --
    # there is deliberately no track_locations row for a words artifact (a
    # words hash reaching audio resolution would poison playback; see D13.2).
    # The length CHECK is the cheapest possible guard against a truncated or
    # base64 digest being stored where a hex sha256 is expected.
    """
    CREATE TABLE lyric_verdict (
        stable_id          TEXT PRIMARY KEY REFERENCES tracks(stable_id) ON DELETE CASCADE,
        verdict            TEXT NOT NULL CHECK (verdict IN
                             ('vocal','sparse','no-lyrics','unknown')),
        coverage_pct       REAL,
        source             TEXT,
        language_iso3      TEXT,
        n_words            INTEGER,
        n_lines            INTEGER,
        pct_witness_red    REAL,
        override           TEXT CHECK (override IS NULL OR override IN
                             ('vocal','sparse','no-lyrics')),
        override_note      TEXT,
        pipeline_version   TEXT NOT NULL,
        words_content_hash TEXT CHECK (words_content_hash IS NULL OR
                             length(words_content_hash) = 64),
        computed_at        TEXT NOT NULL,
        updated_at         TEXT NOT NULL,
        origin_device_id   TEXT,
        deleted_at         TEXT
    )
    """,
    # The triage view the library table sorts on: worst-first by suspect
    # share. BARE on purpose -- see reading 2 in the module docstring.
    "CREATE INDEX idx_lyric_verdict_red ON lyric_verdict(pct_witness_red DESC)",
    # --- sync_policies rebuild: four asset kinds -> six (D13.2) ----------
    # The v7 track_locations precedent, one step shorter because grep
    # confirms sync_policies carries no indexes and nothing REFERENCES it:
    # bare CREATE of the _v10 table, explicit-column INSERT ... SELECT, DROP,
    # RENAME. Everything except the asset_kind CHECK list is copied verbatim
    # from the v7 DDL.
    f"""
    CREATE TABLE sync_policies_v10 (
        machine_id       TEXT NOT NULL
                           REFERENCES machines(machine_id) ON DELETE CASCADE,
        asset_kind       TEXT NOT NULL CHECK (asset_kind IN
                           ({_sql_in_list(ASSET_KIND_CHECK_VALUES)})),
        mode             TEXT NOT NULL CHECK (mode IN
                           ('pinned','cached','stream','excluded')),
        cache_budget_mb  INTEGER,
        updated_at       TEXT,
        origin_device_id TEXT,
        deleted_at       TEXT,
        PRIMARY KEY (machine_id, asset_kind)
    )
    """,
    """
    INSERT INTO sync_policies_v10(
        machine_id, asset_kind, mode, cache_budget_mb, updated_at,
        origin_device_id, deleted_at
    )
    SELECT machine_id, asset_kind, mode, cache_budget_mb, updated_at,
           origin_device_id, deleted_at
    FROM sync_policies
    """,
    "DROP TABLE sync_policies",
    "ALTER TABLE sync_policies_v10 RENAME TO sync_policies",
]

__all__ = ["ASSET_KIND_CHECK_VALUES", "_V10"]
