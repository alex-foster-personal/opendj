"""Policy for :mod:`scripts.sync_drift_lint`: what each rule means, and what
debt is knowingly carried.

Split out of the linter so the two questions stay apart. The checks answer
IS THIS TRUE OF THE DATABASE; this module answers WHAT WOULD WE SAY ABOUT IT
and WHICH KNOWN DEFECTS ARE ALREADY WRITTEN DOWN. Every exception here is
named, dated, and keyed narrowly enough that a NEW instance of the same
defect still fires.

OPEN, AND DELIBERATELY NOT HALF-DONE (both are the maintainer's call):

D-10 RUN THE CHECKS AGAINST A DEPLOYED DATABASE. Everything the linter does
compares source to source: the subject is built from the same tree the
registries live in, so no check can see drift inside a database a user
actually has -- a half-applied migration, a squatter table pre-created with
the wrong shape, a manual ALTER. RE-MEASURED Wed 9 Sep 2026 rather than
carried over: the shared-state ladder now runs 25 CREATE TABLE statements, 22
of them guarded by IF NOT EXISTS, covering 21 distinct tables of which 8 are
in the sync set. (The 3 unguarded ones are the rebuild temporaries
track_field_history_v2, track_fields_v3 and track_locations_v6, which is why
they are unguarded.) A wrong-shaped pre-existing table therefore survives the
ladder for all 21, and D-02 would catch the 8 against a real file. Deferred
rather than bolted on, because a --db mode is a different tool with its own
failure modes (missing file, locked file, file from an older version) and
each of those must report UNKNOWN rather than a verdict.

D-11 AN ESCAPE HATCH FOR A DELIBERATELY MACHINE-LOCAL TABLE. One carrying all
three sync columns that must NOT sync has no way out of D-01 today except
editing the linter: the changelog exemption is conditional on lacking
deleted_at, so it cannot absorb such a table, and the rule's first
remediation (register it) would ship machine-local rows to the hub. Unlikely,
because origin_device_id is a name the sync protocol coined. The shape would
be a dated MACHINE_LOCAL allowlist keyed on the table, in the style of
NAIVE_DEFAULT_ALLOWLIST below. Not added while it would be empty: an
exemption mechanism with no entries is a hole with no reason.

REVIEWED AND STILL OPEN, Wed 9 Sep 2026. A reviewer argued the hatch should
be added NOW, since the day it does fire wrongly the only move is editing the
linter under deadline. The counter-measurement is why it was not: of 46 state
tables, 10 carry the updated_at + origin_device_id pair, 8 are registered and
2 are the conditionally-exempt changelogs, and the 6 tables carrying
updated_at WITHOUT origin_device_id do not fire at all -- so the blunt pair
already ignores the plausible false-positive shape, and the residual case
needs a table that carries a column the sync protocol itself coined and must
not sync. Recorded as a disagreement rather than resolved either way: it is a
policy call about how much empty machinery to ship against a hard-zero gate,
which is the maintainer's, not a lint fix.
"""

from __future__ import annotations

__all__ = [
    "CFG",
    "DDL_SOURCE_FILES",
    "MIRROR_VERSION_DEBT",
    "NAIVE_DEFAULT_ALLOWLIST",
    "NAIVE_DEFAULT_SOURCES",
    "NON_SHIPPING_DDL_PREFIXES",
    "RULES",
    "SHIPPED_MIGRATIONS",
]


class CFG:
    """What is exempt and what counts. Change deliberately."""

    # A count-based check needs a floor (D-09), and the floors below derive
    # from DECLARATIONS rather than pinned numbers. This absolute minimum is
    # the backstop for the declaration itself being emptied: a schema module
    # declaring zero tables would otherwise set a floor of zero, and a scan
    # over nothing reads clean.
    MIN_DECLARED_TABLES: int = 10

    # ``hub_changelog`` and ``local_changelog`` carry ``updated_at`` and
    # ``origin_device_id`` as PAYLOAD -- they record the stamp of some OTHER
    # row -- rather than as sync semantics of their own, and both are
    # explicitly machine-local (apps/shared/state/schema.py migration v7,
    # readings 4 and 5).
    #
    # The exemption is CONDITIONAL on the structural fact that justifies it: a
    # log entry cannot be tombstoned, so neither table has ``deleted_at``. Give
    # one a ``deleted_at`` and it starts looking genuinely synced, the
    # exemption stops applying, and D-01 fires. An unconditional name-based
    # exemption would absorb that change silently, which is the failure this
    # whole file is about.
    CHANGELOG_EXEMPT: frozenset[str] = frozenset({"hub_changelog", "local_changelog"})


RULES: dict[str, str] = {
    "unregistered_synced_table": (
        "Carries the sync columns but no registry lists it, so it silently "
        "never syncs: no error, no digest entry, no divergence report. Add it "
        "to SYNC_TABLES in apps/sync_hub/protocol_common.py (FK-safe order: "
        "parents before children), or drop the sync columns if it is meant to "
        "stay machine-local. Do NOT register a table that must stay local "
        "just to quiet this -- that ships machine-local rows to the hub."
    ),
    "registered_table_missing": (
        "Registered for row-level LWW but the state DB lacks it, or lacks the "
        "full sync trio on it. The engine reads those columns "
        "unconditionally, so the first push against this table raises instead "
        "of syncing."
    ),
    "version_ladder_mismatch": (
        "SCHEMA_VERSION must equal len(MIGRATIONS). apply_migrations loops "
        "range(current, SCHEMA_VERSION), so a step appended without a bump "
        "NEVER RUNS and nothing complains -- the DDL is simply absent on "
        "every database forever."
    ),
    "undocumented_table_or_column": (
        "A live table or column with no entry in apps/database/table_docs.py "
        "or column_docs.py. The generated <data-root>/state/AGENTS.md would "
        "describe a schema it cannot see all of, and regenerating it raises "
        "MissingColumnDocsError."
    ),
    "naive_stamp_default": (
        "DEFAULT CURRENT_TIMESTAMP makes SQLite write a NAIVE stamp "
        "without a UTC offset. CloudSync cannot place that on the UTC "
        "line, so every row minted by this default is quarantined out of the "
        "sync set. Default to NULL and stamp it from "
        "apps.shared.state.sync_stamp instead."
    ),
    "mirror_version_mismatch": (
        "apps/engine_core/store/schema.py mirrors the shared-state ladder's "
        "terminal version in LEGACY_SHARED_STATE_VERSION, and "
        "_stamp_legacy_counters writes schema_meta rows 1..that value. "
        "ORDER MATTERS and bumping the constant alone is NOT the fix: "
        "stamping a version whose DDL the consolidated ladder does not create "
        "makes apply_migrations short-circuit, so the new step never runs on "
        "an adopted database -- the exact omission this file exists to catch. "
        "Teach the consolidated ladder the new objects FIRST "
        "(tests/engine_core/test_store_schema.py::test_object_names_match_"
        "legacy fails until you do), then bump the mirror."
    ),
    "migration_step_changed": (
        "A shipped migration step's statements changed. protocol.py hashes "
        "each synced table's COLUMN LIST into the wire digest while the "
        "handshake pins only SCHEMA_VERSION, so a machine that migrated "
        "BEFORE this edit and one that migrated after report the same version "
        "and diverge forever -- surfacing as SyncDigestMismatch, which reads "
        "as a merge bug. Editing in place is safe only while no real database "
        "has run the step (see the v7 amendment note in "
        "apps/shared/state/schema.py); otherwise append a new step, bump "
        "SCHEMA_VERSION, and record its fingerprint in SHIPPED_MIGRATIONS. "
        "The handshake now gates on WIRE_VERSION, not SCHEMA_VERSION "
        "(apps/sync_hub/wire_version.py), and D-12 pins the synced column "
        "contract to it; this check still guards the ladder itself, since an "
        "in-place edit diverges two peers on the same schema version."
    ),
    "wire_shape_changed": (
        "The synced row shape (columns, types, NOT NULL, keys, CHECK and "
        "UNIQUE constraints of every digest table, plus MachineRow's fields) "
        "no longer matches the fingerprint recorded for WIRE_VERSION. The hub "
        "and spoke gate on WIRE_VERSION alone, so a peer on the old build "
        "would pass the handshake and then 409 or diverge on the first row "
        "carrying the change. Bump WIRE_VERSION and append the new "
        "fingerprint to WIRE_FINGERPRINTS in apps/sync_hub/wire_version.py in "
        "the same commit (its docstring says what counts as a wire change), "
        "or undo the schema edit."
    ),
    "undeclared_state_table": (
        "A schema authority creates this table in state.db and NO inventory "
        "names it: neither apps/shared/state/schema.py (TABLES, "
        "FOREIGN_AUTHORITY_TABLES, INFRASTRUCTURE_TABLES) nor apps/database's "
        "table docs. Those two are what every other reader trusts to say what "
        "is supposed to be there, so an undeclared table is invisible to the "
        "tripwire test and to anyone auditing what syncs. FIX BOTH SIDES, "
        "not one: tests/database/test_agents_md_generator.py asserts "
        "set(TABLE_DOCS) EQUALS the documentable set, so adding a name to "
        "FOREIGN_AUTHORITY_TABLES without documenting it turns this "
        "violation into a red test rather than a fix. Declare the table in "
        "apps/shared/state/schema.py FOREIGN_AUTHORITY_TABLES (it is created "
        "in state.db by something other than the ladder), AND give it a "
        "paragraph in apps/database/table_docs.py and a line per column in "
        "apps/database/column_docs_sibling_apps.py."
    ),
}


# Named, dated exceptions in the style .importlinter uses for its debt: the
# list is readable and shrinks, rather than being a number nobody can act on.
# Keyed on the exact (ladder, table, column) triple, so a NEW naive default --
# including a second one on an already-listed table -- still fires.
_CONSOLIDATION_DEBT: str = (
    "Tue 1 Sep 2026. apps/engine_core/store/schema.py is the DORMANT "
    "consolidation target and its stated contract is CONSOLIDATION, NOT "
    "REDESIGN: every statement is lifted verbatim from the legacy file named "
    "in its LEGACY_SOURCES, and _audit_existing_shapes REFUSES adoption when "
    "a live object's normalized DDL differs from what the ladder would "
    "create. Removing the default here alone therefore breaks "
    "tests/engine_core/test_store_schema.py (which asserts this DDL matches "
    "{legacy}) and makes every existing database unadoptable. The fix is to "
    "change both definitions inside one migration-bearing change, which is a "
    "schema decision rather than a lint fix. UNBLOCK ORDER: this MUST be "
    "fixed BEFORE the ladder is wired up -- {domain}, so wiring it up mints "
    "the naive-stamp P0 by DDL default on real rows."
)

_LIVE_ORIGINAL_DEBT: str = (
    "Tue 1 Sep 2026. This is the LIVE original that "
    "apps/engine_core/store/schema.py copied verbatim, so it executes today "
    "via {caller}. It is listed beside its dormant twin rather than omitted, "
    "because a debt list naming only the dormant copies would read as the "
    "complete inventory while the executing half went unmeasured. Contained "
    "for now: {containment}. The two definitions must change together (see "
    "the twin's entry), so this is a migration-bearing schema decision, not a "
    "lint fix."
)

NAIVE_DEFAULT_ALLOWLIST: dict[tuple[str, str, str], str] = {
    ("engine_core_durable", "duplicate_clusters", "created_at"): _CONSOLIDATION_DEBT.format(
        legacy="apps/dedup/schema.py",
        domain="_DEDUP is in DOMAINS, the durable ladder applied to state.db",
    ),
    ("engine_core_durable", "track_aliases", "detected_at"): _CONSOLIDATION_DEBT.format(
        legacy="apps/dedup/schema.py",
        domain="_DEDUP is in DOMAINS, the durable ladder applied to state.db",
    ),
    ("engine_core_durable", "tag_provenance", "unified_at"): _CONSOLIDATION_DEBT.format(
        legacy="apps/dedup/schema.py",
        domain="_DEDUP is in DOMAINS, the durable ladder applied to state.db",
    ),
    ("engine_core_cache", "fingerprints", "computed_at"): _CONSOLIDATION_DEBT.format(
        legacy="apps/shared/fingerprints.py",
        domain=(
            "_CACHES is in CACHE_DOMAINS, a separate regenerable cache.db that "
            "does not ride hub sync today"
        ),
    ),
    ("dedup", "duplicate_clusters", "created_at"): _LIVE_ORIGINAL_DEBT.format(
        caller="apps/dedup/apply.py and apps/dedup/find_clusters.py",
        containment="data/dedup/phase7.sqlite, and no dedup table is in SYNC_TABLES",
    ),
    ("dedup", "track_aliases", "detected_at"): _LIVE_ORIGINAL_DEBT.format(
        caller="apps/dedup/apply.py and apps/dedup/find_clusters.py",
        containment="data/dedup/phase7.sqlite, and no dedup table is in SYNC_TABLES",
    ),
    ("dedup", "tag_provenance", "unified_at"): _LIVE_ORIGINAL_DEBT.format(
        caller="apps/tags/apply.py",
        containment="data/dedup/phase7.sqlite, and no dedup table is in SYNC_TABLES",
    ),
    ("fingerprints", "fingerprints", "computed_at"): _LIVE_ORIGINAL_DEBT.format(
        caller="apps.shared.fingerprints.FingerprintCache",
        containment="a regenerable cache file that does not ride hub sync",
    ),
}


NAIVE_DEFAULT_SOURCES: frozenset[str] = frozenset(
    {
        "apps/dedup/schema.py",
        "apps/engine_core/store/schema.py",
        "apps/shared/fingerprints.py",
    }
)
"""Every file under apps/ that DECLARES a ``DEFAULT CURRENT_TIMESTAMP``.

The floors in the linter prove D-05 measured a real database. They cannot
prove it measured the RIGHT databases: a naive default declared in a module
no ladder in :data:`~scripts.sync_drift_subject.OTHER_LADDERS` builds is
invisible to D-05, and invisible reads exactly like clean. This set is the
other half, and tests/quality/test_sync_drift_lint.py compares it against a
fresh grep of the tree, so a new file that starts minting naive stamps fails
by NAME rather than being silently out of scope.

Both sides are derived, so neither rots: the left is this declaration, the
right is what the source actually says today. Measured Wed 9 Sep 2026 -- the
three files here account for all 8 naive defaults the scan finds, and no
other module under apps/ declares one."""


MIRROR_VERSION_DEBT: dict[int, str] = {
    7: (
        "Wed 9 Sep 2026. The mirror is stuck at 7 while the shared ladder has "
        "moved on, and this is the omission D-06 exists to catch: v8 "
        "(analysis retention) landed on main without the "
        "LEGACY_SHARED_STATE_VERSION bump that v7 got "
        "(specs/cloudsync-spec.md records that reconciliation). NOT FIXED "
        "HERE, because bumping the constant is not the fix and would assert "
        "something false. MEASURED, not assumed: v8 creates four tables AND "
        "THREE VIEWS (tracks_available, tracks_unavailable, "
        "track_fields_available). The consolidated ladder builds the four "
        "tables -- tests/engine_core/test_store_schema.py::"
        "test_object_names_match_legacy passes -- but it builds ZERO views, "
        "and that test cannot see the gap because its `objects()` helper "
        "selects type IN ('table','index'). A fresh consolidated DB was "
        "built and queried for views to confirm it, rather than trusting "
        "the green test. Bumping the mirror to 8 would therefore declare "
        "the consolidated ladder caught up to a version whose DDL it does "
        "not fully create, which is exactly the shape of claim this rule "
        "refuses. UNBLOCK ORDER: (1) teach the consolidated ladder the "
        "three v8 views, (2) widen that test's `objects()` to include "
        "type='view' so the next missing view fails there instead of here, "
        "(3) bump LEGACY_SHARED_STATE_VERSION to match, (4) delete this "
        "entry. Steps 1 and 2 are schema decisions on a dormant "
        "consolidation target, so they are the maintainer's call rather than a lint "
        "fix. Keyed on the MIRROR value, so moving it re-arms the check; a "
        "later SCHEMA_VERSION bump deliberately does NOT, because it leaves "
        "this same debt unpaid one step further behind rather than creating "
        "a new one."
    ),
}
"""Known, dated cases of engine_core's mirror of the shared-state terminal
version being STUCK BEHIND that version.

Keyed on the mirror's exact value rather than on the rule name: an entry
suppresses ONE explained, still-behind mirror and nothing else, so moving the
mirror re-arms the check, and a mirror that gets AHEAD of the source fires
regardless of any entry. A parametrized test asserts each entry still names
the live mirror with the source still ahead of it, so a paid-off debt fails
as loudly as a new one (see tests/quality/test_sync_drift_declarations.py).

WHY NOT THE (mirror, source) PAIR, which is what this was: under pair-keying
the next ordinary migration -- one step appended, SCHEMA_VERSION bumped in
lockstep, nothing else wrong -- moved the pair off the key and fired D-06 at
whoever wrote it, demanding schema work on a dormant consolidation target
they had not touched, against a hard-zero gate with no ratchet. A gate whose
only exits are do-someone-else's-work or re-key-the-allowlist teaches people
to re-key allowlists."""


# Fingerprints of the migration steps that have ALREADY RUN on real databases.
# Whitespace-normalized, so reindenting a DDL string is not a change; anything
# that alters the statements is. Appending a step means adding an entry here,
# and D-07 says so by name rather than leaving the omission silent.
SHIPPED_MIGRATIONS: dict[int, str] = {
    1: "5498f98f76cc42ca",
    2: "85927f734811799e",
    3: "694c5658f5c01b31",
    4: "f95ef7831d921384",
    # v5 is `_V5 = _V4` in the ladder, so the identical fingerprint is the
    # aliasing showing through, not a copy-paste in this table.
    5: "f95ef7831d921384",
    6: "06d7a9a84d96d7e7",
    7: "d8c0341d8f974779",
    # v8 (analysis retention) shipped while this guard was off the tree. The
    # fingerprint was measured on current main, Wed 9 Sep 2026, and the run
    # that measured it also re-verified 1..7 against a ladder that had since
    # been SPLIT across two modules (apps/shared/state/migrations.py and
    # migrations_v6_v8.py, issue #1583): all seven still match, so the split
    # moved the statements without editing them.
    8: "102bb10ad679ecb2",
    # v9 (MACHINE ENROLLMENT, specs/design_decision_12.md). Recorded in the
    # same commit that appends the step -- which is the whole point of D-07:
    # v8 shipped with no fingerprint at all, and nothing said so until this
    # guard was written months later.
    9: "e64477deff84f6e3",
    # v10 (karaoke lyric_verdict + the sync_policies asset_kind rebuild,
    # specs/karaoke-lyrics-operational-plan.md D13.1-D13.2). Recorded in the
    # same commit that appends the step, as D-07 asks. Landed as v10 because
    # v9 went to machine enrollment while this work was in flight.
    10: "cacbe912b516bcd4",
    # v11 (per-machine sync credential, ADR 12 amendment, plan X5). Recorded
    # in the same commit that appends the step, as D-07 asks.
    11: "e34f7f14a54471f6",
    # v12 (synced feedback_pins, FBSYNC-01, ADR-0013). Recorded in the same
    # commit that appends the step, as D-07 asks.
    12: "74a5812014629532",
    # v13 (addressable playlist_memberships item_id + order_key, LIBM-20
    # substrate, apps/shared/state/migrations_v13.py). Recorded after the
    # step shipped (PR #2230) without a fingerprint; D-07 is what said so.
    13: "85dfd36dad2fbaea",
    14: "3ebe67991d775680",
    # v15 (legacy track_fields stamp backfill, issue #3101). Shipped as one
    # UPDATE, fingerprint 016101421f89b2ac. Issues #3136 and #3165 then moved
    # the work into backfill_track_fields_stamps, because the row updates and
    # their changelog appends have to land atomically and SQL in the ladder
    # cannot do that. The step is deliberately empty now, so the fingerprint
    # is the empty digest. This is the one case the immutability rule does not
    # cover: the statements did not CHANGE, they moved to a Python entry point
    # that is idempotent and marker guarded (v15_track_fields_stamp_backfill
    # in schema_meta_markers), so a database that ran the old SQL and one that
    # runs the new backfill converge instead of diverging.
    15: "e3b0c44298fc1c14",
    # v16 (schema_meta_markers plus the two changelog lookup indexes, issue
    # #3165). Recorded late: the step shipped without a fingerprint and D-07
    # is what said so.
    16: "f40a7e73019ad7e8",
    # v17 (hub_changelog stamp repair, repair_hub_changelog_stamps). Empty for
    # the same reason as v15: the repair is Python so it can write rows and
    # changelog entries together.
    17: "e3b0c44298fc1c14",
    # v18 (path_availability index, issue #1037, PERF-RB-01). Recorded in the
    # same PR that appends the step, as D-07 asks.
    18: "32147a3e232d276b",
    # v19 (tracks.audio_hash, issue #3864).
    19: "8d96066d57134071",
    # v20 (indexed CloudSync track identity lookups, issue #4397).
    20: "3f0a403c11f444cf",
    # v21 (sync_write_tokens + write-token triggers, issue #4396). Landed as
    # v21 because v20 went to the CloudSync identity indexes (#4397) first.
    21: "9ecf1935d0bbc19a",
    # v22 (bounded playlist membership reads, issue #3963). Landed as v22
    # because v20 and v21 went to the CloudSync identity indexes (#4397) and
    # write tokens (#4396) first.
    22: "c60afedcaf0b4ffc",
    # v23 (tracks.restored_at and tracks.deleted_reason, issue #4628).
    23: "f364648aac8d9582",
    # v24 (drop the never-read idx_path_availability_checked, STATE-21).
    24: "d9a16327965bb5e0",
}


DDL_SOURCE_FILES: dict[str, str] = {
    # ----- authorities over data/state/state.db ------------------------------
    "apps/shared/state/schema.py": "AUTHORITY. The migration ladder itself.",
    "apps/shared/state/migrations.py": "AUTHORITY. Ladder steps v1-v5 (issue #1583 split).",
    "apps/shared/state/migrations_v6_v8.py": "AUTHORITY. Ladder steps v6-v8.",
    "apps/shared/state/migrations_v9.py": "AUTHORITY. Ladder step v9, machine enrollment.",
    "apps/shared/state/migrations_v10.py": "AUTHORITY. Ladder step v10, karaoke lyric_verdict.",
    "apps/shared/state/migrations_v11.py": "AUTHORITY. Ladder step v11, machine sync credentials.",
    "apps/shared/state/migrations_v12.py": "AUTHORITY. Ladder step v12, synced feedback_pins.",
    "apps/shared/state/migrations_v16.py": (
        "AUTHORITY. Ladder step v16, schema_meta_markers and the changelog "
        "lookup indexes."
    ),
    "apps/shared/state/migrations_v18.py": (
        "AUTHORITY. Ladder step v18, the path_availability index (issue #1037)."
    ),
    "apps/shared/state/migrations_v21.py": (
        "AUTHORITY. Ladder step v21, sync_write_tokens plus its write-token "
        "triggers (issue #4396). Already covered by the apps/shared/state/"
        "schema.py entry in STATE_AUTHORITIES: apply_migrations runs the whole "
        "ladder, this step included, so no separate STATE_AUTHORITIES entry."
    ),
    "apps/sync_hub/engine_identity_map.py": (
        "AUTHORITY. Additive on the shared connection: apps/sync_hub/client.py "
        "runs ensure_identity_remap_table against the same state.db "
        "state_db.open_rw returns, to hold identity-collapse remaps across "
        "batched hub_apply calls. Not in the sync set itself."
    ),
    "apps/analysis/store.py": "AUTHORITY. Additive DDL on the shared connection.",
    "apps/analysis/queue_store.py": (
        "AUTHORITY. The backfill queue, additive on the same shared connection: "
        "apps/webui/server/routes/analysis_backfill.py and apps.analysis.queue_cli "
        "both call ensure_queue_tables on a state.db opened by "
        "apps.analysis.store.open_conn, so analysis_queue_batch and "
        "analysis_queue_item are created in the live file on first use."
    ),
    "apps/analysis/queue_stale.py": (
        "AUTHORITY, and a SECOND file rather than a second entry point: it owns "
        "analysis_stale on its own and ensure_queue_tables runs its DDL alongside "
        "the queue's. Declared separately because the scan reads FILES -- folding "
        "it into the line above would leave a DDL-bearing file unclassified."
    ),
    "apps/shared/pairings/schema_sql.py": "AUTHORITY, TWICE. Two ladders, two entry points.",
    "apps/shared/play_orders/schema.py": "AUTHORITY. Its own private version counter.",
    "apps/shared/playlist_sets/schema.py": "AUTHORITY. Its own private version counter.",
    "apps/spotify/state_aux.py": "AUTHORITY. Aux tables for the Spotify import.",
    "apps/launcher/scripts/bootstrap_db.py": "AUTHORITY. The launcher's additive fts5 migration.",
    "apps/launcher/src-tauri/src/state.rs": (
        "AUTHORITY, AND NOT PYTHON. get_db_path prefers <repo>/data/state/state.db "
        "whenever it exists, and ensure_launcher_meta runs CREATE TABLE IF NOT "
        "EXISTS launcher_meta on every meta_get/meta_set -- reached on every "
        "launcher start via commands::hotkey::claim_first_run_notification. Read "
        "and executed by scripts.sync_drift_subject.rust_ddl_statements."
    ),
    # ----- owns a SEPARATE sqlite file ---------------------------------------
    "apps/dedup/schema.py": "SEPARATE FILE data/dedup/phase7.sqlite. Built as its own ladder.",
    "apps/sync/analysis_writeback.py": (
        "SEPARATE FILE (sidecar odjAnalysisScalar inside rekordbox's OWN "
        "master.plain.db, same as apps/adapters/rekordbox/writer.py -- not "
        "state.db)."
    ),
    "apps/shared/fingerprints.py": "SEPARATE FILE, the chromaprint cache. Built as its own ladder.",
    "apps/engine_core/store/schema.py": "SEPARATE, DORMANT. The consolidation target.",
    "apps/engine_core/jobs/store.py": "SEPARATE FILE <data_dir>/state/jobs.db.",
    "apps/lyrics/search_index_schema.py": "SEPARATE FILE, the lyrics search index.",
    "apps/shared/hashing.py": "SEPARATE FILE, a content-hash cache.",
    "apps/sync/fingerprint.py": "SEPARATE FILE, the sync fingerprint cache.",
    "apps/voice/settings.py": "SEPARATE FILE data/voice/settings.sqlite.",
    "apps/webui/server/search_index.py": (
        "SEPARATE FILE. Its first line says so: never mutate state.db's own schema."
    ),
    "apps/webui/server/grid_quality_store.py": (
        "SEPARATE FILE <state dir>/grid-quality.db, a sidecar next to state.db. "
        "Derived beatgrid-quality verdicts (GRIDFLAG-02): rebuildable from the "
        "grids, no migration, never synced."
    ),
    "apps/sets/state.py": (
        "SEPARATE FILE data/sets/sets.db. Every apps/ call site constructs "
        "SetsState() or SetsState(db_path=...) against apps.sets.paths.SETS_DB; "
        "only tests/sets/test_state.py hands it a state.db backend. A future "
        "state.db authority, not a current one -- and if one appears, both its "
        "tables are undocumented, so D-04 says so."
    ),
    # ----- creates no durable file at all ------------------------------------
    "apps/webui/server/routes/copilot.py": "IN-MEMORY. Builds its projection in ':memory:'.",
    "apps/launcher/scripts/latency_check.py": "THROWAWAY. A benchmark file.",
    "scripts/demo_phase08.py": "THROWAWAY. A demo fixture.",
    "scripts/make_djay_fixture.py": "THROWAWAY. Builds a djay test fixture.",
    "scripts/redteam_fixture_rekordbox_schema.sql": (
        "THROWAWAY. Rekordbox's schema, rendered from pyrekordbox by "
        "scripts/redteam_fixture_schema.py; scripts/redteam_fixture_library.py "
        "runs it against the throwaway master.plain.db red-team pods default to "
        "(REDTEAM-05). Not a schema authority."
    ),
    "apps/launcher/src-tauri/src/commands/frecency.rs": "TEST FIXTURE. #[cfg(test)], in-memory.",
    "apps/launcher/src-tauri/src/commands/search.rs": "TEST FIXTURE. #[cfg(test)], in-memory.",
    # ----- names the phrase without declaring DDL ----------------------------
    "apps/analysis/selection.py": "DELEGATES to apps/analysis/store.py; declares none of its own.",
    "apps/webui/server/routes/pairing_capture.py": "CALLS an authority; declares none of its own.",
    "apps/webui/server/pairings_sqlite.py": (
        "AUTHORITY. Additive on the shared connection: ``http_pairings`` for the "
        "PAIR-04 HTTP pairing model, created by ensure_http_pairings_table on "
        "first webui pairing write."
    ),
    "apps/webui/server/sqlite_backend.py": "COMMENT ONLY, documenting a migration hazard.",
    "apps/database/generate_agents_md.py": "MATCHES sqlite_master rows; declares no DDL.",
    "apps/sync_hub/wire_version.py": (
        "READS sqlite_master CREATE TABLE text to fingerprint the synced shape "
        "(D-12); runs the ladder only in ':memory:'. Declares no DDL."
    ),
    "apps/database/agents_md.py": "PROSE about the generated file.",
    "apps/database/table_docs.py": "PROSE. A curated description quoting a table's DDL.",
    "apps/database/AGENTS.md": "PROSE. The authored half of the two-file pattern.",
    "apps/engine_core/store/REPORT.md": "PROSE. A design report.",
    "scripts/sync_drift_subject.py": "THIS TOOL. The Rust extractor and its own prose.",
    "scripts/sync_drift_rules.py": "THIS FILE. The declaration you are reading.",
}
"""Every SHIPPED tracked file that names a ``CREATE TABLE``, with one line
saying whether it writes ``data/state/state.db``.

Shipped means anywhere in the tracked tree outside the prose and test
prefixes declared in :data:`NON_SHIPPING_DDL_PREFIXES`. It is NOT a list of
files under apps/ and scripts/, and the difference is the whole point: see
that declaration for why the scan behind this dict stopped being scoped to
two directories.

THE COMPLETENESS OF THE AUTHORITY LIST IS THE MOST DANGEROUS THING IN THIS
TOOL. A check over a subject that is missing an authority reports zero and
reads exactly like a clean tree, and that has now happened twice: once for
the second ladder inside apps/shared/pairings/schema_sql.py, and once for
apps/launcher/src-tauri/src/state.rs, which no derivation restricted to
``*.py`` could ever have found.

So the list stopped being a method and became a declaration.
tests/quality/test_sync_drift_authorities.py compares this dict against a
fresh scan of the tracked tree, in EVERY language and in every directory, and
fails by name in both directions -- a new DDL-bearing file that nobody
classified, and an entry here for a file that no longer declares one. That
is the schema -> authority direction, which is the one that can catch an
omission; a test that walks this dict and confirms each file exists could
not.

Measured Wed 9 Sep 2026: 36 files, of which 9 are state.db authorities (one
of them counted once here and twice in STATE_AUTHORITIES, because
apps/shared/pairings/schema_sql.py holds two independent ladders)."""


NON_SHIPPING_DDL_PREFIXES: dict[str, str] = {
    ".agents/": (
        "PROSE. Skill files quoting schema at their readers (.claude/skills "
        "is a tracked symlink here, so git ls-files names the same prose "
        "under .agents/ only)."
    ),
    ".planning/": "PROSE. Plans, audits and reviews quoting schema.",
    "app_docs/": "PROSE. Design docs quoting schema (write-once feature records).",
    "docs/": "PROSE. Documentation quoting schema.",
    "specs/": "PROSE. Specs quoting schema as illustrative examples (write-once records).",
    "tests/": (
        "TEST CODE AND FIXTURES. Builds throwaway databases, and holds one "
        "binary djay fixture whose sqlite_master text matches. Nothing here "
        "provisions a database any user has."
    ),
}
"""Path prefixes whose ``CREATE TABLE`` is never a schema authority, and why.

The other half of :data:`DDL_SOURCE_FILES`. The scan behind that declaration
used to stop at ``apps/`` and ``scripts/``, and a scope restriction is the
same shape of blindness as the ``*.py`` restriction that hid
apps/launcher/src-tauri/src/state.rs: a file the lens cannot see contributes
no tables to the subject and reads exactly like a clean tree. The scan now
reads the WHOLE tracked tree, so every DDL-bearing file must be either
classified in DDL_SOURCE_FILES or sitting under a prefix declared here, and a
DDL writer in a new shipped location -- open-dj/, tools/, a Rust crate at the
root -- fails BY NAME rather than falling outside the lens. That is the
schema -> authority direction one level further out than the linter runs it.

Prefixes rather than files, because these six hold prose and tests whose
membership moves every day and which no reader treats as a schema authority.
The two halves may not overlap, and a test asserts it: an entry in
DDL_SOURCE_FILES under one of these prefixes would be classified twice and
audited by neither.

Measured Wed 9 Sep 2026 over 4764 tracked regular files: 134 name a
``CREATE TABLE``, 36 of them shipped (every one classified in
DDL_SOURCE_FILES) and 98 under these four prefixes -- 65 tests, 30 planning,
2 skills, 1 doc."""
