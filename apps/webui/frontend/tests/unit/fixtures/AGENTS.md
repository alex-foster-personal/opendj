# state.db -- generated table reference

GENERATED FILE. Do not hand-edit -- it is overwritten the next time
migrations run against this database file. Curated descriptions live in
`apps/database/column_docs.py` (repo); narrative context and the founding
brief live in `apps/database/AGENTS.md` (repo). Regenerate with:

```
python -m apps.database.generate_agents_md --data-dir <data-dir>
```

One fenced YAML block per table below, introspected from THIS database file
at generation time. `columns` holds the curated one-line meaning of each
column; `schema` holds its raw declared type/constraints as SQLite reports
them right now; `foreign_keys` lists what this table currently references.
fts5 shadow tables (SQLite's own opaque index storage for a virtual table,
not application columns) are omitted.

## `adapters`

```yaml
adapters:
  description: Last-run bookkeeping, one row per adapter (rekordbox, djay, serato, traktor, mik, ...). Not part of the sync set -- each machine's adapters reflect what actually ran on that machine.
  columns:
    adapter_id: Adapter name, primary key.
    last_run_at: RFC 3339 UTC timestamp of the adapter's most recent run, or NULL if it has never run.
    last_ok: 1 if the last run succeeded, 0 otherwise. Defaults to 0 -- an adapter that has never run has not proven itself ok.
    notes: Free-text detail from the last run, e.g. an error message.
  schema:
    adapter_id: TEXT PRIMARY KEY
    last_run_at: TEXT
    last_ok: INTEGER NOT NULL DEFAULT 0
    notes: TEXT
  foreign_keys: []
```

## `analysis_field_verification`

```yaml
analysis_field_verification:
  description: 'How each field was verified, stored next to the values it produced: cross-source agreement, a one-sided single-source probe, or unverified. Written by apps.mik.load.record_verification from apps.shared.equivalence.EquivalenceGate.provenance_rows; overridden records a human forcing a write past a non-passing verdict.'
  columns:
    source: Which source's mapping this verification concerns.
    field_name: Which field this verification concerns.
    status: The verdict status recorded at verification time.
    basis: 'Strength of evidence: ''cross_source'' (agreement between two independent sources), ''single_source'' (a one-sided probe), or ''unverified''.'
    normaliser: Which normaliser was used to compare values, if any.
    checked_at: RFC 3339 UTC timestamp of the verification.
    verified_by: Who or what performed the verification.
    overridden: 1 if a human forced a write past a non-passing verdict.
    recorded_at: RFC 3339 UTC timestamp of when this row was written.
  schema:
    source: TEXT PRIMARY KEY NOT NULL
    field_name: TEXT PRIMARY KEY NOT NULL
    status: TEXT NOT NULL
    basis: TEXT NOT NULL
    normaliser: TEXT
    checked_at: TEXT
    verified_by: TEXT
    overridden: INTEGER NOT NULL DEFAULT 0
    recorded_at: TEXT NOT NULL
  foreign_keys: []
```

## `auth_sessions`

```yaml
auth_sessions:
  description: One browser session for one user. Stores the sha256 of the bearer session token, never the token itself, so a stolen database cannot be replayed as a live cookie; also holds the Google refresh/access token pair server-side so the browser never sees a Google credential. Cascades on user delete.
  columns:
    session_token_sha256: Primary key. sha256 hex digest of the bearer session token the browser holds; the raw token itself is never stored, so a stolen database cannot be replayed as a live cookie.
    google_sub: FK -> users(google_sub), ON DELETE CASCADE.
    refresh_token: The Google OAuth refresh token that produced this session. Stays server-side and is never exposed over the API.
    access_token: The current Google OAuth access token, if one is cached.
    access_expires_at: RFC 3339 UTC expiry of access_token, or NULL if none is cached.
    created_at: RFC 3339 UTC timestamp this session was created (sign-in time).
    last_seen_at: RFC 3339 UTC timestamp of this session's most recent authenticated request.
    expires_at: RFC 3339 UTC timestamp this session stops being valid, regardless of activity.
  schema:
    session_token_sha256: TEXT PRIMARY KEY
    google_sub: TEXT NOT NULL
    refresh_token: TEXT
    access_token: TEXT
    access_expires_at: TEXT
    created_at: TEXT NOT NULL
    last_seen_at: TEXT NOT NULL
    expires_at: TEXT NOT NULL
  foreign_keys:
  - column: google_sub
    references_table: users
    references_column: google_sub
```

## `enrollment_grants`

```yaml
enrollment_grants:
  description: 'Short-lived single-use credentials for the DEV enrollment path: an operator with an authenticated session on the hub mints one, carries it to the machine that is joining, and that machine spends it at POST /api/v1/sync/enroll. Only the sha256 is stored, never the redeemable value, on the same reasoning as auth_sessions.session_token_sha256.'
  columns:
    grant_token_sha256: 'Primary key: sha256 of the raw grant, which is the only form that reaches the database. The redeemable value is returned exactly once, to the operator who minted it.'
    google_sub: 'FK to users: who this grant will make the owner. Read from THIS row at redemption time and never from the request body, so an enrolling machine cannot name whose machine it is becoming.'
    created_at: When the grant was minted, canonical UTC.
    expires_at: When the grant stops being redeemable, canonical UTC. Default TTL is 15 minutes (apps.sync_hub.enrollment_credentials.GRANT_TTL_S).
    redeemed_at: 'When the grant was spent, NULL while unspent. Single use: a redeemed grant presented by a DIFFERENT machine is refused. Re-presenting it for the machine that already spent it is allowed and resolves to the same owner, which is what makes the enroll command safely re-runnable.'
    redeemed_machine_id: 'Which machine spent this grant. Not a foreign key: it is an audit record of what happened, and it must survive the machine row being removed.'
  schema:
    grant_token_sha256: TEXT PRIMARY KEY
    google_sub: TEXT NOT NULL
    created_at: TEXT NOT NULL
    expires_at: TEXT NOT NULL
    redeemed_at: TEXT
    redeemed_machine_id: TEXT
  foreign_keys:
  - column: google_sub
    references_table: users
    references_column: google_sub
```

## `events`

```yaml
events:
  description: Durable append-only event log backing the in-process EventBus (apps.shared.state.events), the INFRA-01 bus floor. Machine-local -- never rides hub sync (design_decision_04.md SYNC SET v1).
  columns:
    id: Surrogate AUTOINCREMENT primary key.
    ts: RFC 3339 UTC timestamp the event was emitted.
    kind: Event type/name, free text (e.g. 'track.field_written').
    stable_id: The track the event concerns, if any. Not FK-enforced -- events must survive a track's own deletion.
    payload_json: Event payload, JSON-encoded.
    actor: Who/what emitted the event (adapter name, 'webui', a user, etc.), if known.
  schema:
    id: INTEGER PRIMARY KEY
    ts: TEXT NOT NULL
    kind: TEXT NOT NULL
    stable_id: TEXT
    payload_json: TEXT
    actor: TEXT
  foreign_keys: []
```

## `feedback_pins`

```yaml
feedback_pins:
  description: One row per Open DJ feedback comment pin, carrying the whole pin as JSON (text, page, anchor, x/y, status, replies, author, build and environment stamps, attachment metadata). Synced through CloudSync so every machine enrolled to the same hub sees the same pins; the per-machine feedback/comments.json store is reconciled into and out of it. Archive is a tombstone (deleted_at), never a DELETE.
  columns:
    pin_id: 'Primary key: the 12-hex pin id minted when the pin was dropped. The SAME id on two machines is the same pin, so stores that held a copy each collapse to one row.'
    doc: The whole pin as JSON, in the exact shape the feedback router's CommentOut model returns (CHECK json_valid). Includes status, agent_note, issue_url, author, the build stamp, the environment (machine name, UI kind, viewport, release) and attachment METADATA. Attachment BYTES do not sync (FBSYNC-05).
    updated_at: NOT NULL. The pin's own last-edit time (its updated_at, else its created_at), canonical UTC. What last-writer-wins orders on, so it must be the edit time and never the sync time.
    origin_device_id: machine_id of the machine whose edit this row carries. The LWW tiebreak when two machines stamped the same instant.
    deleted_at: Archive tombstone. Set when the pin was archived on any machine; the doc then carries status 'archived'. Never inferred from a pin being missing from a comments.json.
  schema:
    pin_id: TEXT PRIMARY KEY
    doc: TEXT NOT NULL
    updated_at: TEXT NOT NULL
    origin_device_id: TEXT
    deleted_at: TEXT
  foreign_keys: []
```

## `hub_changelog`

```yaml
hub_changelog:
  description: Hub-local (agentbox-only) monotonic log of every row accepted from a push, used to hand each spoke only the rows newer than its pull watermark. AUTOINCREMENT is correct here specifically because this table never crosses a machine boundary -- seq IS the pull watermark.
  columns:
    seq: Primary key, AUTOINCREMENT. Monotonically increasing hub sequence number; the watermark spokes pull against.
    table_name: Which synced table the logged row belongs to.
    row_pk: The logged row's primary key, as text -- composite keys are serialised by the sync layer, not this table.
    updated_at: The logged row's own updated_at at the moment it was accepted, copied here for audit/debugging -- not itself a watermark.
    origin_device_id: The machine that originated the logged row.
    received_at: RFC 3339 UTC timestamp the hub accepted this push. Distinct from updated_at (when the source machine wrote it) -- this is when the hub saw it, which is what clock skew (design_decision_04.md consequence 2) makes the two potentially very different.
  schema:
    seq: INTEGER PRIMARY KEY
    table_name: TEXT NOT NULL
    row_pk: TEXT NOT NULL
    updated_at: TEXT NOT NULL
    origin_device_id: TEXT NOT NULL
    received_at: TEXT NOT NULL
  foreign_keys: []
```

## `local_changelog`

```yaml
local_changelog:
  description: 'Spoke-local twin of hub_changelog (design_decision_08.md point 3): same shape, appended by apps.shared.state.sync_stamp on every write to a synced table. sync_state.last_push_seq fences against this table''s seq, replacing the wall-clock push watermark that lost rows under clock skew (round 1 finding 3). Never crosses a machine boundary and never rides hub sync itself.'
  columns:
    seq: Primary key, AUTOINCREMENT. Monotonically increasing spoke-local sequence number; sync_state.last_push_seq is the floor into this table (design_decision_08.md point 3).
    table_name: Which synced table the logged row belongs to.
    row_pk: The logged row's primary key, as text -- composite keys are serialised by the sync layer, not this table.
    updated_at: The logged row's own updated_at at the moment this machine wrote it, copied here for audit/debugging -- not itself a watermark.
    origin_device_id: This machine's machine_id.
    received_at: RFC 3339 UTC timestamp this machine's own write was appended here. Same field name as hub_changelog.received_at for shape symmetry, but there is no remote hop to distinguish it from updated_at at this end of the pipe.
  schema:
    seq: INTEGER PRIMARY KEY
    table_name: TEXT NOT NULL
    row_pk: TEXT NOT NULL
    updated_at: TEXT NOT NULL
    origin_device_id: TEXT NOT NULL
    received_at: TEXT NOT NULL
  foreign_keys: []
```

## `lyric_verdict`

```yaml
lyric_verdict:
  description: One row per track holding the karaoke lyrics verdict (vocal, sparse, no-lyrics or unknown), the human override that survives a recompute, the alignment provenance, and the sha256 of the track's karaoke_words artifact. Synced, so a verdict computed on one machine is a verdict everywhere; deletes are tombstones only (the licensing purge sets deleted_at, it never DELETEs), which is also what makes an accidental re-ingest of purged lyrics a no-op.
  columns:
    stable_id: 'Primary key AND the FK -> tracks(stable_id) ON DELETE CASCADE: one verdict per track. The cascade never actually fires -- a hard DELETE of a tracks row is forbidden repo-wide -- which is why every reader filters the PARENT''s deleted_at too.'
    verdict: CHECK IN ('vocal', 'sparse', 'no-lyrics', 'unknown') -- the COMPUTED verdict. Mirrored in Python as apps.shared.state.schema.LYRIC_VERDICTS. Read it through the effective-verdict rule (override first, this second), never on its own.
    coverage_pct: Percentage 0-100 of the track's vocal-bearing time that aligned words cover. NULL when no alignment was attempted. The no-lyrics threshold calibrated against the maintainer's ear lives in apps/lyrics/vocal_presence.py, not here.
    source: Which provider the lyric text came from, as a prefix-matchable string (e.g. 'lrclib', 'musixmatch'). The purge CLI selects on source LIKE <prefix>%, so this column IS the licensing lever.
    language_iso3: ISO 639-3 language code the aligner detected for the lyric text, or NULL when undetermined.
    n_words: Number of words in the karaoke_words artifact. Denormalised onto the row so a SQL filter never has to open the JSON; the ingest fails loudly if it disagrees with the artifact.
    n_lines: Number of lines derived from those words by apps.lyrics.lines.derive_lines. Denormalised for the same reason as n_words, and the column that replaced the retired per-word table's bulk_line_counts query.
    pct_witness_red: 'Fraction 0-1 of words the ASR witness flagged as suspect. The triage sort key: idx_lyric_verdict_red orders worst-first on it. NULL when the witness did not run.'
    override: 'CHECK IN (''vocal'', ''sparse'', ''no-lyrics'') or NULL -- the HUMAN verdict, which wins over the computed one and survives every recompute (a recompute writes every other column and deliberately never touches this one). ''unknown'' is absent from the vocabulary on purpose: reverting to ''we do not know'' is spelled NULL. Mirrored as schema.LYRIC_OVERRIDES.'
    override_note: Free-text reason the human gave for the override, or NULL. Written and cleared with override, never on its own.
    pipeline_version: The apps.lyrics.karaoke_cache.PIPELINE_VERSION that produced this row, grammar '<YYYY.MM.DD>-<round>' (e.g. '2026.09.09-round3a'). Bumped whenever the aligner, the witness or the writer changes output bytes, so a row can be told apart from a row a later round would produce.
    words_content_hash: 'sha256 (64 lowercase hex chars, CHECKed on length) of the canonical bytes of the karaoke_words artifact, or NULL when there are no words. This is the ONLY location record for that artifact: there is deliberately no track_locations row for it, because track_locations has no asset_kind column and a words hash reaching audio resolution would poison playback.'
    computed_at: RFC 3339 UTC timestamp of the alignment run that produced this row. Distinct from updated_at, which moves on any write including an override or a tombstone.
    updated_at: Sync LWW timestamp. NOT NULL, unlike sync_policies' -- a peer offering a NULL-stamped verdict row fails the whole apply batch instead of storing a row that silently loses every conflict it takes part in.
    origin_device_id: Writing machine's machine_id.
    deleted_at: 'Tombstone timestamp. NULL = live. Set by the licensing purge, which never issues a DELETE; the tombstone syncs, so peers stop hydrating the words too. Sticky: an upsert refuses to revive a tombstoned row unless asked to in so many words.'
  schema:
    stable_id: TEXT PRIMARY KEY
    verdict: TEXT NOT NULL
    coverage_pct: REAL
    source: TEXT
    language_iso3: TEXT
    n_words: INTEGER
    n_lines: INTEGER
    pct_witness_red: REAL
    override: TEXT
    override_note: TEXT
    pipeline_version: TEXT NOT NULL
    words_content_hash: TEXT
    computed_at: TEXT NOT NULL
    updated_at: TEXT NOT NULL
    origin_device_id: TEXT
    deleted_at: TEXT
  foreign_keys:
  - column: stable_id
    references_table: tracks
    references_column: stable_id
```

## `machine_credentials`

```yaml
machine_credentials:
  description: 'Per-machine sync credentials (migration v11, the ADR 12 amendment for plan X5): the secret a machine_id is not. The hub mints one at enroll and returns the raw value exactly once; this table keeps only its sha256. Under MDT_SYNC_CREDENTIAL_MODE=enforce every sync endpoint except enroll refuses a caller whose bearer does not hash to this row. Hub-local and outside the sync set.'
  columns:
    machine_id: 'Primary key and FK to machines, ON DELETE CASCADE. One live credential per machine: re-minting replaces the hash, so the previous bearer stops working the moment the new one exists.'
    credential_sha256: sha256 of the raw bearer (prefix odjsync_), the only form that reaches the database, so a stolen hub DB hands nobody a working credential. UNIQUE, so one hash can never authenticate two machines. Deleted by revoke.
    minted_at: When this credential was minted, canonical UTC.
  schema:
    machine_id: TEXT PRIMARY KEY
    credential_sha256: TEXT NOT NULL
    minted_at: TEXT NOT NULL
  foreign_keys:
  - column: machine_id
    references_table: machines
    references_column: machine_id
```

## `machine_owners`

```yaml
machine_owners:
  description: 'Which user owns which machine, one live row per machine. Hub-authoritative and deliberately OUTSIDE the sync set (specs/design_decision_12.md section A): ownership is asserted only by a call that carried a live credential to the hub holding the row, so a restored or hostile spoke cannot push itself an owner under last-writer-wins. Separate from machines rather than a column on it for three reasons -- no wire payload changes shape, a revoked owner stays distinguishable from a machine that was never claimed, and the machines snapshot that peers exchange can no longer carry a forged ownership claim.'
  columns:
    machine_id: 'Primary key and FK to machines. One owner per machine: a shared or household machine needs a further migration, which this table makes a primary-key change rather than a redesign.'
    google_sub: FK to users. Google's 'sub' claim, the only identifier Google guarantees is stable and never reused -- email is not the key because a Google account's email can change. ON DELETE CASCADE, so deleting an account (the ACCT-03 privacy promise) un-owns its fleet and the machines survive reading unowned.
    hub_machine_id: The machine id of the hub that WROTE this row. Honored only when it equals this hub's live id read from <data-dir>/machine-id; a row that fails that test is reported as FOREIGN and the machine reads unowned. That is what stops a hub restored from another machine's backup silently adopting that machine's whole fleet, since the DB travels in a restore but the id file does not.
    enrolled_at: 'When this machine joined, canonical UTC. Never rewritten by a re-run: enrolling an already-enrolled machine is a no-op, so this column stays the audit trail of when the machine ACTUALLY joined.'
    enrolled_via: 'How the owner was established: ''grant'' (the dev path, a single-use token carried by a human), ''google_id_token'' (the zero-ceremony user path, an install proving its signed-in user to the hub), or ''adopt'' (claimed retroactively by an operator, for a machine that was already syncing before ownership existed). CHECK-constrained; the vocabulary lives in apps.shared.state.migrations_v9.ENROLLED_VIA_VALUES.'
    revoked_at: When the owner released this machine, NULL while the row is live. A separate column rather than deleting the row so 'released' stays distinguishable from 'never claimed' -- the two want different handling and different messages.
  schema:
    machine_id: TEXT PRIMARY KEY
    google_sub: TEXT NOT NULL
    hub_machine_id: TEXT NOT NULL
    enrolled_at: TEXT NOT NULL
    enrolled_via: TEXT NOT NULL
    revoked_at: TEXT
  foreign_keys:
  - column: google_sub
    references_table: users
    references_column: google_sub
  - column: machine_id
    references_table: machines
    references_column: machine_id
```

## `machines`

```yaml
machines:
  description: 'Fleet registry: one row per machine that has ever synced. Part of the synced set itself (design_decision_04.md SYNC SET v1) so any machine can see the whole fleet, not just itself.'
  columns:
    machine_id: Primary key. uuid4 hex minted once at first run and stored OUTSIDE this table, in a plain file at <data-dir>/machine-id (apps.shared.state.machine_identity) -- so a DB restored from Litestream onto a different physical machine cannot inherit the old machine's identity and start impersonating it in hub_changelog.
    name: Friendly, human-chosen machine name (e.g. 'silver', 'agentbox'). UNIQUE -- this is what a human picks in the CloudSync config UI; hostname stays display-only elsewhere.
    platform: CHECK IN ('macos', 'windows', 'linux').
    is_hub: 1 if this machine is the sync hub (agentbox, per design_decision_04.md), 0 otherwise. Defaults to 0.
    data_root: The data directory this machine reported at registration (its MDT_DATA_DIR-resolved root), for operator visibility only -- never used to resolve a path on a different machine.
    first_seen: RFC 3339 UTC timestamp of this machine's first registration.
    last_seen: RFC 3339 UTC timestamp of this machine's most recent registration/heartbeat.
  schema:
    machine_id: TEXT PRIMARY KEY
    name: TEXT NOT NULL
    platform: TEXT NOT NULL
    is_hub: INTEGER NOT NULL DEFAULT 0
    data_root: TEXT
    first_seen: TEXT NOT NULL
    last_seen: TEXT NOT NULL
  foreign_keys: []
```

## `path_availability`

```yaml
path_availability:
  description: 'Resolver-namespaced cache of disk-truth answers for rekordbox library paths (FolderPath / track file_path strings before containment mapping). Keys are (resolver_namespace, logical_path); materialised_size is NULL when the path is missing or a dataless stub. Written by bounded listing hydration and a background refresher (issue #1037, PERF-RB-01); survives process restarts.'
  columns:
    resolver_namespace: SHA-256 fingerprint of the active path-map entries plus this machine's machine-id file. Rows from an old namespace are ignored after a path-map change.
    logical_path: Library path string before containment resolution (rekordbox FolderPath, tracks.file_path, or track_locations path).
    materialised_size: Materialised st_size in bytes from fs_residency, or NULL when the resolved path is missing, not a file, or a dataless stub.
    checked_at: RFC 3339 UTC timestamp when this row was last probed or written.
  schema:
    resolver_namespace: TEXT PRIMARY KEY NOT NULL
    logical_path: TEXT PRIMARY KEY NOT NULL
    materialised_size: INTEGER
    checked_at: TEXT NOT NULL
  foreign_keys: []
```

## `playlist_memberships`

```yaml
playlist_memberships:
  description: Ordered track membership of a playlist. PK is (playlist_id, position), so reordering rewrites positions rather than moving rows between them. Synced as a whole-list replace, not row-level LWW (design_decision_04.md, PLAYLIST MEMBERSHIP) -- row-level merge on an ordered list produces interleaved garbage.
  columns:
    playlist_id: FK -> playlists(playlist_id), ON DELETE CASCADE.
    stable_id: FK -> tracks(stable_id), ON DELETE CASCADE.
    position: Ordinal within the playlist, vendor-native ordering. Part of the primary key together with playlist_id.
    updated_at: Added in v6. Membership rows are replaced wholesale on sync, so this column matters less than playlists.updated_at, which is the actual sync trigger.
    origin_device_id: Writing machine's machine_id; added in v6.
    deleted_at: Tombstone timestamp; added in v6. NULL = live.
    item_id: Stable membership row id; added in v13.
    order_key: Fractional sort key string; added in v13. Integer position is computed on read.
  schema:
    playlist_id: TEXT PRIMARY KEY NOT NULL
    stable_id: TEXT NOT NULL
    position: INTEGER PRIMARY KEY NOT NULL
    updated_at: TEXT
    origin_device_id: TEXT
    deleted_at: TEXT
    item_id: TEXT
    order_key: TEXT
  foreign_keys:
  - column: stable_id
    references_table: tracks
    references_column: stable_id
  - column: playlist_id
    references_table: playlists
    references_column: playlist_id
```

## `playlist_pins`

```yaml
playlist_pins:
  description: Per-machine, per-playlist pin override -- e.g. keep a gig playlist's audio always local on gig machines even though the machine's general audio policy is 'stream'. PK is (machine_id, playlist_id).
  columns:
    machine_id: FK -> machines(machine_id), ON DELETE CASCADE.
    playlist_id: FK -> playlists(playlist_id), ON DELETE CASCADE.
    mode: CHECK IN ('pinned', 'cached', 'stream', 'excluded'), same vocabulary as sync_policies.mode. A playlist_pins row overrides the machine's general sync_policies row for tracks in that playlist.
    updated_at: Sync LWW timestamp.
    origin_device_id: Writing machine's machine_id.
    deleted_at: Tombstone timestamp. NULL = live.
  schema:
    machine_id: TEXT PRIMARY KEY NOT NULL
    playlist_id: TEXT PRIMARY KEY NOT NULL
    mode: TEXT NOT NULL
    updated_at: TEXT
    origin_device_id: TEXT
    deleted_at: TEXT
  foreign_keys:
  - column: playlist_id
    references_table: playlists
    references_column: playlist_id
  - column: machine_id
    references_table: machines
    references_column: machine_id
```

## `playlists`

```yaml
playlists:
  description: 'One row per vendor playlist, unique on (vendor, vendor_pl_id): re-ingesting the same playlist from the same vendor updates in place, but the same name in two vendors is deliberately two rows -- vendors do not share a playlist namespace.'
  columns:
    playlist_id: Primary key, minted at ingest.
    name: Playlist display name.
    vendor: 'Owning vendor: rekordbox, djay, serato, traktor, etc.'
    vendor_pl_id: The vendor's own playlist identifier. UNIQUE together with vendor.
    created_at: First-ingest timestamp.
    updated_at: Most recent write; also the sync LWW timestamp and, per design_decision_04.md PLAYLIST MEMBERSHIP, the trigger for a whole-list membership replace on sync.
    origin_device_id: Writing machine's machine_id; added in v6.
    deleted_at: Tombstone timestamp; added in v6. NULL = live.
    forbid_duplicates: When 1, reject extra copies of an already-present stable_id on :add and duplicate stable_ids on PUT; default 0 allows repeats.
  schema:
    playlist_id: TEXT PRIMARY KEY
    name: TEXT NOT NULL
    vendor: TEXT NOT NULL
    vendor_pl_id: TEXT NOT NULL
    created_at: TEXT NOT NULL
    updated_at: TEXT NOT NULL
    origin_device_id: TEXT
    deleted_at: TEXT
    forbid_duplicates: INTEGER NOT NULL DEFAULT 0
  foreign_keys: []
```

## `schema_meta`

```yaml
schema_meta:
  description: Migration bookkeeping for apps.shared.state.schema.apply_migrations -- infrastructure, not domain data. One row per applied schema version.
  columns:
    version: Primary key. Schema version this row records as applied.
    applied_at: RFC 3339 UTC timestamp this version was applied.
  schema:
    version: INTEGER PRIMARY KEY
    applied_at: TEXT NOT NULL
  foreign_keys: []
```

## `schema_meta_markers`

```yaml
schema_meta_markers:
  description: Durable completion markers for one-time migration repairs (for example the v15 track_fields stamp backfill). One row per repair name; repeat opens check the marker instead of rescanning.
  columns:
    marker: Primary key. Durable name of a one-time migration repair.
    applied_at: RFC 3339 UTC timestamp the repair completed.
  schema:
    marker: TEXT PRIMARY KEY
    applied_at: TEXT NOT NULL
  foreign_keys: []
```

## `sync_policies`

```yaml
sync_policies:
  description: 'Per-machine, per-asset-kind policy: what this machine keeps pinned, caches on demand, streams, or excludes entirely. PK is (machine_id, asset_kind) -- the CloudSync config surface''s quick toggles write here (cloudsync-spec.md D4/D5).'
  columns:
    machine_id: FK -> machines(machine_id), ON DELETE CASCADE.
    asset_kind: CHECK IN ('audio', 'stem_bundle', 'anlz_cache', 'vocal_cache', 'lyrics_cache', 'karaoke_words') -- widened from four kinds to six by the schema v10 table rebuild (apps/shared/state/migrations_v10.ASSET_KIND_CHECK_VALUES is the one source of that list).
    mode: CHECK IN ('pinned', 'cached', 'stream', 'excluded') -- pinned is always-local, cached is opportunistic with LRU eviction, stream fetches only on play, excluded never touches this machine.
    cache_budget_mb: Optional cap on local cache size for this asset_kind on this machine, in megabytes. NULL means unbounded.
    updated_at: Sync LWW timestamp.
    origin_device_id: Writing machine's machine_id -- note this is the machine that SET the policy, not necessarily the machine the policy is FOR (that is machine_id, the FK column).
    deleted_at: Tombstone timestamp. NULL = live.
  schema:
    machine_id: TEXT PRIMARY KEY NOT NULL
    asset_kind: TEXT PRIMARY KEY NOT NULL
    mode: TEXT NOT NULL
    cache_budget_mb: INTEGER
    updated_at: TEXT
    origin_device_id: TEXT
    deleted_at: TEXT
  foreign_keys:
  - column: machine_id
    references_table: machines
    references_column: machine_id
```

## `sync_state`

```yaml
sync_state:
  description: 'Machine-local watermarks for the hub sync protocol: how far this machine has pushed to, and pulled from, each peer. Never synced itself -- syncing your own sync watermarks would be incoherent (design_decision_04.md, CHANGE MODEL).'
  columns:
    peer: Primary key -- identifies the sync peer these watermarks are against (currently always the hub, e.g. 'hub' or the hub's machine_id).
    last_push_seq: Highest hub_changelog.seq this machine has successfully pushed past, i.e. its push watermark. Defaults to 0.
    last_pull_seq: Highest hub_changelog.seq this machine has pulled, i.e. its pull watermark. Defaults to 0.
    last_sync_at: RFC 3339 UTC timestamp of the last successful sync with this peer, or NULL if never synced.
    peer_generation: The generation token this peer reported at the last completed sync (apps/sync_hub/generation.py). A DIFFERENT token on the next hello means the peer's database moved backwards -- a Litestream point-in-time restore -- so both watermarks reset to 0 and this machine re-offers its library. Keying restore detection on the token rather than on the peer's MAX(seq) going backwards is what stops a routine changelog prune from looking like a restore. NULL until the first sync completes.
  schema:
    peer: TEXT PRIMARY KEY
    last_push_seq: INTEGER NOT NULL DEFAULT 0
    last_pull_seq: INTEGER NOT NULL DEFAULT 0
    last_sync_at: TEXT
    peer_generation: TEXT
  foreign_keys: []
```

## `track_availability`

```yaml
track_availability:
  description: One row per stable_id recording whether its audio is present on disk right now, distinct from the analysed values that stay in track_fields regardless. Safe-default views (tracks_available, tracks_unavailable, track_fields_available) read this as a dimension so an aggregate cannot silently include unplayable rows.
  columns:
    stable_id: The track this availability row concerns. FK ON DELETE CASCADE -- but a soft delete only sets tracks.deleted_at and never issues a physical DELETE, so this row survives a tombstone; readers still need an explicit deleted_at filter.
    state: 'Whether the audio is present on disk right now: ''present'', ''absent'', ''awaiting_volume'' (path is on an unmounted volume), or ''streaming'' (no local file expected).'
    checked_path: The path that was actually probed, if any.
    checked_at: RFC 3339 UTC timestamp of the last availability check.
  schema:
    stable_id: TEXT PRIMARY KEY
    state: TEXT NOT NULL
    checked_path: TEXT
    checked_at: TEXT NOT NULL
  foreign_keys:
  - column: stable_id
    references_table: tracks
    references_column: stable_id
```

## `track_energy_segments`

```yaml
track_energy_segments:
  description: Time-series destination for a source's energy-over-time data (e.g. MIK's ZENERGYSEGMENT), in milliseconds per the repo's time-series-vs-scalar convention. track_fields is one row per (stable_id, field_name) and cannot hold a series.
  columns:
    stable_id: The track this energy segment belongs to.
    seq: Sequence number of this segment within the track, from 0.
    start_ms: Segment start time in milliseconds. See start_clamped for the 77 rows whose source start time was negative float dust.
    length_ms: Segment length in milliseconds.
    energy: Energy level for this segment, 1-10.
    source: Which vendor or pipeline produced this segment (e.g. 'mik').
    confidence: Confidence score for this segment, if any.
    start_clamped: 1 if the source start time was clamped up from negative float dust to 0, so a reader can tell a genuine 0 start from a clamped one.
    modified_at: When this segment was last written.
  schema:
    stable_id: TEXT PRIMARY KEY NOT NULL
    seq: INTEGER PRIMARY KEY NOT NULL
    start_ms: INTEGER NOT NULL
    length_ms: INTEGER NOT NULL
    energy: INTEGER NOT NULL
    source: TEXT PRIMARY KEY NOT NULL
    confidence: REAL
    start_clamped: INTEGER NOT NULL DEFAULT 0
    modified_at: TEXT NOT NULL
  foreign_keys:
  - column: stable_id
    references_table: tracks
    references_column: stable_id
```

## `track_field_history`

```yaml
track_field_history:
  description: 'Append-only audit trail: every value a track_fields cell has ever held moves here once superseded. Machine-local -- design_decision_04.md''s SYNC SET v1 explicitly excludes it, so it carries no updated_at / origin_device_id / deleted_at.'
  columns:
    id: Surrogate AUTOINCREMENT primary key. The original PK (stable_id, field_name, superseded_at) could silently collide when two rewrites landed in the same clock tick (migration v1 -> v2, schema.py comment [I1]); id makes history truly append-only.
    stable_id: The track this history row concerns. Not a live FK to tracks -- history intentionally outlives a track's current field row.
    field_name: Which field's value this history row records.
    value_json: The value as it stood before being superseded.
    source: Who produced that value, at the time.
    confidence: Confidence score at the time, if any.
    modified_at: When that value was originally written.
    superseded_at: When it was overwritten by the next value.
  schema:
    id: INTEGER PRIMARY KEY
    stable_id: TEXT NOT NULL
    field_name: TEXT NOT NULL
    value_json: TEXT NOT NULL
    source: TEXT NOT NULL
    confidence: REAL
    modified_at: TEXT NOT NULL
    superseded_at: TEXT NOT NULL
  foreign_keys: []
```

## `track_fields`

```yaml
track_fields:
  description: 'Provenance-wrapped analysed values, EAV-shaped: one row per (stable_id, field_name), e.g. bpm, key, energy. Mirrors the open-dj v0 strawman ProvenanceValue envelope (apps.shared.state.types) -- this is where per-value confidence and source live, separate from the vendor-sourced facts on tracks.'
  columns:
    stable_id: FK -> tracks(stable_id), ON DELETE CASCADE.
    field_name: Analysed field name, e.g. 'bpm', 'key', 'energy'. Free text -- the vocabulary lives in application code, not a CHECK constraint.
    value_json: The field's value, JSON-encoded so it can hold a number, string, or structured value.
    source: 'Which system produced this value. CHECK-constrained to apps.shared.state.types.SOURCES: mik, rekordbox, djay, serato, traktor, open-dj-tool, manual, inferred, webui.'
    confidence: Optional 0-1 confidence score for inferred/analysed values; NULL for vendor facts that carry no confidence signal.
    modified_at: RFC 3339 UTC timestamp this specific field value was last written. Superseded values move to track_field_history.
    updated_at: Sync LWW timestamp; added in v6. Distinct from modified_at -- modified_at is domain semantics (when the value changed), updated_at is the sync protocol's conflict-resolution timestamp; in practice a writer stamps both together.
    origin_device_id: Writing machine's machine_id; added in v6.
    deleted_at: Tombstone timestamp; added in v6. NULL = live.
  schema:
    stable_id: TEXT PRIMARY KEY NOT NULL
    field_name: TEXT PRIMARY KEY NOT NULL
    value_json: TEXT NOT NULL
    source: TEXT NOT NULL
    confidence: REAL
    modified_at: TEXT NOT NULL
    updated_at: TEXT
    origin_device_id: TEXT
    deleted_at: TEXT
  foreign_keys:
  - column: stable_id
    references_table: tracks
    references_column: stable_id
```

## `track_locations`

```yaml
track_locations:
  description: Every playable location a track's audio can be found at -- local disk, a remote URL, a specific venue's copy -- distinct from the single legacy tracks.file_path. Multiple rows per track are normal; role='primary' is the one the play path prefers. Rebuilt in v6 from an INTEGER AUTOINCREMENT key to a minted uuid4-hex location_id specifically because this is THE cross-machine table and an autoincrement key collides the instant two machines each mint a row 1 (design_decision_05.md, section 3).
  columns:
    location_id: Primary key. 32-char lowercase hex, DEFAULT (lower(hex(randomblob(16)))) so even a writer that forgets to name the column cannot insert an unsyncable NULL-keyed row (design_decision_05.md, reading 2).
    stable_id: FK -> tracks(stable_id), ON DELETE CASCADE.
    machine_id: 'FK -> machines(machine_id). Added in the v6 PK rebuild (design_decision_08.md point 1): joins the natural key so each machine''s view of where a file lives -- file_path, available, probed_at, venue_* -- is its own row rather than one global row two machines can mint different location_id values for (round 1 findings 1 and 7a). DDL-nullable, not NOT NULL, for the same reason origin_device_id is: pure-SQL migration cannot know this machine''s id, so it is backfilled immediately after migration by apps.shared.state.sync_stamp.backfill_local_machine_id and the NOT NULL invariant is enforced by a tripwire test, not DDL.'
    kind: '''local'' (a file path on this machine) or ''remote'' (a URL).'
    role: '''primary'' (the play path prefers this one) or ''alternate''. Defaults to ''alternate''.'
    file_path: Filesystem path, when kind='local'. Exactly one of file_path/remote_url must be non-empty (table CHECK).
    remote_url: URL, when kind='remote'. Exactly one of file_path/remote_url must be non-empty (table CHECK).
    venue_key: Optional identifier for the venue/machine context this location is scoped to (a gig laptop's local copy, for instance).
    venue_rank: Optional ordering hint among multiple venue-scoped locations for the same track.
    available: 1 if this location was last probed as reachable/playable, 0 otherwise. Defaults to 0 -- unprobed is not assumed available.
    probed_at: RFC 3339 UTC timestamp of the last availability probe, or NULL if never probed.
    content_hash: Content-addressed hash of the file at this location, for cross-machine dedup and future R2 keying.
    created_at: First-insert timestamp.
    updated_at: Most recent write; also the sync LWW timestamp.
    origin_device_id: Writing machine's machine_id; added in v6.
    deleted_at: Tombstone timestamp; added in v6. NULL = live.
  schema:
    location_id: TEXT PRIMARY KEY NOT NULL DEFAULT lower(hex(randomblob(16)))
    stable_id: TEXT NOT NULL
    machine_id: TEXT
    kind: TEXT NOT NULL
    role: TEXT NOT NULL DEFAULT 'alternate'
    file_path: TEXT
    remote_url: TEXT
    venue_key: TEXT
    venue_rank: INTEGER
    available: INTEGER NOT NULL DEFAULT 0
    probed_at: TEXT
    content_hash: TEXT
    created_at: TEXT NOT NULL
    updated_at: TEXT NOT NULL
    origin_device_id: TEXT
    deleted_at: TEXT
  foreign_keys:
  - column: machine_id
    references_table: machines
    references_column: machine_id
  - column: stable_id
    references_table: tracks
    references_column: stable_id
```

## `track_vendor_ids`

```yaml
track_vendor_ids:
  description: Opaque vendor-library identifiers for a track, keyed by (stable_id, vendor). Lets a writer round-trip a change back to the vendor's own database (a rekordbox djmdContent id, a Serato crate entry, etc.) without a vendor id ever becoming the primary key.
  columns:
    stable_id: FK -> tracks(stable_id), ON DELETE CASCADE.
    vendor: Vendor name, e.g. 'rekordbox', 'djay', 'serato', 'traktor', 'mik'. Free text, not CHECK-constrained (compare to track_fields.source, which is).
    vendor_id: The vendor's own opaque identifier for this track (its native primary key or equivalent).
    updated_at: Sync LWW timestamp; added in v6, NULL on pre-v6 rows.
    origin_device_id: Writing machine's machine_id; added in v6.
    deleted_at: Tombstone timestamp; added in v6. NULL = live.
  schema:
    stable_id: TEXT PRIMARY KEY NOT NULL
    vendor: TEXT PRIMARY KEY NOT NULL
    vendor_id: TEXT NOT NULL
    updated_at: TEXT
    origin_device_id: TEXT
    deleted_at: TEXT
  foreign_keys:
  - column: stable_id
    references_table: tracks
    references_column: stable_id
```

## `tracks`

```yaml
tracks:
  description: 'Canonical per-track identity row: one per song, regardless of how many vendor libraries or playable locations reference it.'
  columns:
    stable_id: Primary key. 40-char lowercase hex SHA-1 minted by apps.shared.state.ids.stable_id from ISRC, fingerprint+duration+size, or path+mtime, in that preference order (see stable_id_tier).
    stable_id_tier: 'Which of the three stable_id derivations produced this row''s key: ''isrc'' (strongest; two tracks sharing an ISRC intentionally collide -- that is the archival property), ''fingerprint'', or ''inferred'' (path+mtime, last resort, re-keyed by dedup when a better signal appears).'
    title: Track title, vendor-sourced.
    artists_json: JSON-encoded list of artist name strings; not a normalised artist table.
    album: Album name, vendor-sourced.
    isrc: Raw ISRC as read from the vendor library, pre-normalisation -- normalisation only happens inside ids.stable_id.
    duration_ms: Track duration in milliseconds.
    file_path: Legacy primary playable path, pre-dating track_locations. New code should prefer a track_locations row with role='primary'; this column is not removed because ingest still writes it and migration v4 seeded track_locations from it.
    content_hash: Content-addressed hash of the audio file, for cross-machine dedup and future R2 keying. NULL on all 9,986 rows as of Fri 28 Aug 2026 (apps/shared/state/ingest/rekordbox.py:273 does not populate it) -- this blocks every content-addressed sync operation until the CLOUDSYNC content-hash-backfill phase runs (cloudsync-spec.md section 4).
    created_at: RFC 3339 UTC timestamp of first insert. Never rewritten.
    updated_at: RFC 3339 UTC timestamp of the most recent write to this row. Also this table's LWW sync timestamp (design_decision_04.md, CONFLICT RULE).
    origin_device_id: machine_id of the machine that made the most recent write. NULL on rows written before migration v6 -- such rows sync as epoch-old until a real edit or the seed push touches them (design_decision_04.md consequence 7).
    deleted_at: RFC 3339 UTC tombstone timestamp; NULL means live. A synced delete is an ordinary update that sets this column and propagates like any other write -- readers MUST filter deleted_at IS NULL or ghosts reappear (design_decision_04.md, DELETES).
    audio_hash: Tag-independent SHA-256 of the decoded container audio payload; used beside content_hash for CloudSync identity merges.
  schema:
    stable_id: TEXT PRIMARY KEY
    stable_id_tier: TEXT NOT NULL
    title: TEXT
    artists_json: TEXT
    album: TEXT
    isrc: TEXT
    duration_ms: INTEGER
    file_path: TEXT
    content_hash: TEXT
    created_at: TEXT NOT NULL
    updated_at: TEXT NOT NULL
    origin_device_id: TEXT
    deleted_at: TEXT
    audio_hash: TEXT
  foreign_keys: []
```

## `unmatched_source_analysis`

```yaml
unmatched_source_analysis:
  description: Staging for an analysed source row (MIK, rekordbox, ...) that matches no tracks row at all, so there is no stable_id to hang a track_fields row on. unmatched_reason records why; promoted_stable_id is the one-way door into track_fields once a match is later found (see docs/analysis-retention.md).
  columns:
    id: Surrogate AUTOINCREMENT primary key.
    source: Which vendor or pipeline produced this analysis row (e.g. 'mik', 'rekordbox').
    source_row_id: The source's own row identifier, opaque here.
    field_name: Which analysed field this row carries.
    value_json: The analysed value, JSON-encoded.
    unmatched_reason: 'Why no stable_id was assigned: ''no_candidate'' (nothing matched), ''ambiguous_candidates'' (several equally good matches), or ''lost_collision'' (a better-tier source row won the same track). All three mean the same thing operationally -- we do not know which track this analysis belongs to.'
    confidence: Confidence score for this value, if any.
    title: Track title as read from the source, for human triage.
    artist: Track artist as read from the source, for human triage.
    album: Track album as read from the source, for human triage.
    isrc: Raw ISRC as read from the source, if any.
    duration_ms: Track duration in milliseconds, as read from the source.
    source_path: The source's own file path, for human triage.
    modified_at: When the source last modified this value.
    imported_at: RFC 3339 UTC timestamp of when this row was staged.
    promoted_stable_id: The tracks row this analysis was later matched to, if any. The one-way door out of staging into track_fields (docs/analysis-retention.md).
    promoted_at: RFC 3339 UTC timestamp of the promotion, if any.
  schema:
    id: INTEGER PRIMARY KEY
    source: TEXT NOT NULL
    source_row_id: TEXT NOT NULL
    field_name: TEXT NOT NULL
    value_json: TEXT NOT NULL
    unmatched_reason: TEXT NOT NULL
    confidence: REAL
    title: TEXT
    artist: TEXT
    album: TEXT
    isrc: TEXT
    duration_ms: INTEGER
    source_path: TEXT
    modified_at: TEXT NOT NULL
    imported_at: TEXT NOT NULL
    promoted_stable_id: TEXT
    promoted_at: TEXT
  foreign_keys:
  - column: promoted_stable_id
    references_table: tracks
    references_column: stable_id
```

## `users`

```yaml
users:
  description: 'One Google account that has signed in to the webui, keyed on the OIDC ''sub'' claim rather than email -- an email can be reassigned, a sub cannot. Sign-in is identity only, never authorization: the app behaves identically whether or not anyone is signed in (migration v6 comment, apps/shared/state/schema.py).'
  columns:
    google_sub: Primary key. Google's OIDC 'sub' claim -- the only identifier Google guarantees is stable and never reused, unlike email.
    email: Google account email at last sign-in. UNIQUE, but not the key -- see google_sub.
    name: Google profile display name, or NULL if Google did not return one.
    avatar_url: Google profile picture URL, or NULL if Google did not return one.
    created_at: RFC 3339 UTC timestamp of this account's first sign-in.
    updated_at: RFC 3339 UTC timestamp of the most recent profile refresh.
  schema:
    google_sub: TEXT PRIMARY KEY
    email: TEXT NOT NULL
    name: TEXT
    avatar_url: TEXT
    created_at: TEXT NOT NULL
    updated_at: TEXT NOT NULL
  foreign_keys: []
```
