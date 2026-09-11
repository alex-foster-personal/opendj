# Rekordbox adapter

**Status:** v0.2 (Phase 15, Apr 2026)
**Implements:** OPEN-02 (Rekordbox side)
**Code:** [`apps/open_dj/adapters/rekordbox.py`](../../apps/open_dj/adapters/rekordbox.py)
**Licence:** Apache-2.0.

## Scope

Clean-room adapter between Pioneer Rekordbox's `master.db` and the open-dj
v0.2 document shape. Read path relies on
[pyrekordbox](https://pypi.org/project/pyrekordbox/) to decrypt + open the
local SQLite file; the adapter's hot path (`build_library`) takes plain
`RBTrackInput` / `RBPlaylistInput` dataclass iterables so tests run with
in-memory fixtures and never touch a live DB.

Write path is intentionally **not** performed by this adapter. Mutations
go through the v0.2 cautious writers:

- Ratings: [`apps.sync.apply_ratings`](../../apps/sync/apply_ratings.py)
- Playlists: [`apps.sync.playlist_apply`](../../apps/sync/playlist_apply.py)

Those writers own the 6-rail safety pattern (pgrep gate, backup, reversal
log, dry-run-first, schema-version check, per-row verify) so the adapter
stays a pure transform.

## Capabilities (open-dj fields)

| open-dj field        | support      | notes                                                                  |
|----------------------|--------------|------------------------------------------------------------------------|
| `track_id`           | lossless     | Computed by `compute_track_id_with_tier` over isrc / fingerprint / duration+size / absolute_path+mtime. |
| `file_path`          | lossless     | From `RBTrack.file_path`.                                              |
| `title`              | lossless     |                                                                        |
| `artists[]`          | lossy        | Rekordbox stores a single string; the adapter splits on `,` and strips. |
| `album`              | lossless     |                                                                        |
| `isrc`               | lossless     | Normalised via `normalise_isrc` (uppercase, punctuation stripped).     |
| `duration_ms`        | lossless     | Derived from `duration_s * 1000`.                                      |
| `size_bytes`         | lossless     | `RBTrack.file_size`.                                                   |
| `bpm`                | lossless     | Wrapped with `source = "rekordbox"` provenance.                        |
| `key`                | lossless     | Wrapped with provenance when present.                                  |
| `rating`             | lossless     | Wrapped with provenance; 0..5 scale.                                   |
| `genre`              | read-only    | Carried on the input dataclass.                                        |
| `vendor_ids.rekordbox` | lossless   | Original `rb_id` preserved verbatim.                                   |
| `content_hash`       | lossless / synthetic | Uses `content_hash_hex` when supplied; otherwise synthesises a deterministic sha256 from `(size_bytes, mtime, file_path)` and flags the track with `x_content_hash_mode = "inferred"`. |
| `cue_points[]`       | read-only    | Emitted opportunistically when the caller provides them; write-back goes through the cautious cue/beatgrid writers.    |
| `beatgrid`           | read-only    | Passed through verbatim; not yet written back.                         |
| `playlists[]`        | lossless     | Parent / child edges preserved as `rb_pl_<rb_id>`.                     |

Authoritative source:
[`apps/open_dj/adapters/rekordbox.py`](../../apps/open_dj/adapters/rekordbox.py).

## Export entry points

- `build_library(tracks, playlists, *, include_cues=True)` -- pure,
  dataclass-driven, returns an `ExportResult`.
- `export_library(source_path=..., out_path=..., include_cues=True)` --
  live-DB wrapper. Opens the Rekordbox DB via
  `apps.shared.rekordbox_db.open_db`, iterates tracks + playlists, and
  (optionally) writes canonical JSON via `open_dj.canon.to_canonical_bytes`.

## Safety

The adapter itself is read-only. All live mutation goes through the v0.2
sync writers, which in turn enforce:

1. **pgrep gate** -- refuse to mutate when Rekordbox is running.
2. **Backup** -- timestamped copy of `master.db` before any write.
3. **Reversal log** -- ndjson lines per operation under
   `.planning/adapters/rekordbox/reversal-log/` (gitignored).
4. **Dry-run-first** -- every change preview surfaces as an open-dj diff.
5. **Schema-version check** -- reject if Pioneer bumps `master.db` past
   the pinned range.
6. **Per-row verify** -- post-write re-read to confirm the field lands.

## Known gaps

- **Adapter-driven writes** are still routed through `apps.sync.*`; a
  direct `RekordboxAdapter.write()` surface is Phase 16 scope.
- **Cue + beatgrid** round-trips rely on Phase 4's cue/beatgrid writer
  under `apps.sync`. The adapter emits them on read but does not own the
  write path.
- **Smart playlists** are opaque; only static playlists round-trip.
- **Real-world sign-off** on a production `master.db` is Phase 16 work.
