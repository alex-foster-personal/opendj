# djay Pro adapter

**Status:** v0.2 (Phase 15, Apr 2026)
**Implements:** OPEN-02 (djay side)
**Code:** [`apps/open_dj/adapters/djay.py`](../../apps/open_dj/adapters/djay.py)
**Licence:** Apache-2.0.

## Scope

Clean-room adapter between Algoriddim djay Pro's `MediaLibrary.db` (TSAF
blobs + SQLite) and the open-dj v0.2 document shape. Export is read-only
in v0.2; the TSAF reader surface is still evolving in Phase 4, so cue
coverage is opportunistic.

The hot path (`build_library`) takes plain `DjayTrackInput` /
`DjayPlaylistInput` dataclass iterables. Live-DB export opens the
`MediaLibrary.db` via `apps.shared.djay_db.iter_tracks` /
`iter_playlists`, which connects in true read-only mode
(`mode=ro&immutable=1`) so there is no risk of writing through the reader.

Write path is **deferred**. Mutations land through Phase 4's cautious
cue / beatgrid writers plus `apps.sync.apply_ratings` for ratings; Phase
16 wires adapter-driven imports on top of those foundations.

## Capabilities (open-dj fields)

| open-dj field        | support        | notes                                                                |
|----------------------|----------------|----------------------------------------------------------------------|
| `track_id`           | lossless       | Computed by `compute_track_id_with_tier` over isrc / fingerprint / duration+size / absolute_path+mtime. |
| `file_path`          | lossless       | `DjayTrack.file_path`; empty string when the track is streaming-only.|
| `title`              | lossless       |                                                                      |
| `artists[]`          | lossy          | djay stores a single string; the adapter splits on `,` and strips.   |
| `isrc`               | lossless       | Normalised via `normalise_isrc`.                                     |
| `duration_ms`        | lossless       | Derived from `duration_s * 1000`.                                    |
| `size_bytes`         | lossless       | When surfaced by the reader.                                         |
| `bpm`                | lossless       | Wrapped with `source = "djay"` provenance.                           |
| `key`                | lossless       | Wrapped when present on the input.                                   |
| `rating`             | lossless       | Wrapped with provenance; 0 values are dropped (djay uses 0 for unrated). |
| `vendor_ids.djay`    | lossless       | Original `uuid` preserved verbatim.                                  |
| `content_hash`       | lossless / synthetic | Uses `content_hash_hex` when supplied; otherwise synthesises a deterministic sha256 from `(size_bytes, mtime, duration_ms, file_path)` and flags the track with `x_content_hash_mode = "inferred"`. |
| `x_djay_streaming`   | lossless       | Emitted as `true` for tracks where `is_local == False` (SoundCloud / Tidal / Beatport Link items). |
| `cue_points[]`       | read-only      | Emitted when the caller provides them; tagged with `source = "djay"` in provenance. |
| `playlists[]`        | lossless       | Parent / child edges preserved as `djay_pl_<uuid>`.                  |

Authoritative source:
[`apps/open_dj/adapters/djay.py`](../../apps/open_dj/adapters/djay.py).

## Export entry points

- `build_library(tracks, playlists, *, include_cues=True)` -- pure,
  dataclass-driven, returns an `ExportResult`.
- `export_library(source_path=..., out_path=..., include_cues=True)` --
  live-DB wrapper. Iterates the djay DB via `apps.shared.djay_db` (which
  enforces `mode=ro&immutable=1`) and writes canonical JSON via
  `open_dj.canon.to_canonical_bytes` when `out_path` is provided.

## Safety

The adapter itself is export-only; no live djay DB is ever mutated by
code in this module. Any future write path will go through the same
cautious-writer stack that guards ratings + cues today:

1. **pgrep gate** on the djay Pro process.
2. **Backup** of `MediaLibrary.db` before mutation.
3. **Reversal log** under `.planning/adapters/djay/reversal-log/`
   (gitignored).
4. **Dry-run-first** diff preview.
5. **Schema-version check** against the pinned TSAF range.
6. **Per-row verify** after write.

## Known gaps

- **Write path** is deferred (v0.3 / Phase 16). Ratings import reuses
  `apps.sync.apply_ratings`; cue / beatgrid writes go through the Phase 4
  cautious writers when wired.
- **Cue coverage** depends on the TSAF reader surface; some binary
  variants still decode to opaque extension blobs.
- **Streaming-only tracks** (SoundCloud, Tidal, Beatport Link) carry
  `x_djay_streaming = true` and an empty `file_path`; most round-trips
  into Rekordbox / Serato / Traktor drop them with a warning (D6).
- **Real-world sign-off** on a production `MediaLibrary.db` is Phase 16
  work.
