# Serato DJ Pro adapter

**Status:** v0.1 (Phase 16, Apr 2026)
**Implements:** OPEN-02c
**Code:** [`apps/adapters/serato/`](../../apps/adapters/serato/)
**Licence:** Apache-2.0 (file-level). Any future embed of triseratops-derived
code lives in a separate MPL-2 sub-package.

## Scope

Clean-room implementation against public documentation only. Reads + writes
Serato's per-library storage:

- `_Serato_/database V2` -- the crate catalogue (bigendian 4+4 tag stream).
- `_Serato_/Subcrates/*.crate` -- per-playlist files.
- Per-audio-file GEOB ID3 frames (`Serato Markers2`, `Serato BeatGrid`,
  `Serato Autotags`). Codec lives in
  [`geob.py`](../../apps/adapters/serato/geob.py).

No Serato source code is copied in. The tag-stream and GEOB layouts were
reconstructed from the
[triseratops](https://github.com/HoldenVR/triseratops) README (MPL-2, docs
only) and the [serato-tags](https://github.com/Holzhaus/serato-tags) Kaitai
schemas (CC-BY-SA-4.0, docs only).

## Capabilities (open-dj fields)

| open-dj field        | support      | notes                                                                  |
|----------------------|--------------|------------------------------------------------------------------------|
| `track_id`           | lossless     | Computed from `file_path` + `title`.                                   |
| `file_path`          | lossless     | UTF-16-BE in `pfil`.                                                   |
| `title`              | lossless     | UTF-16-BE in `tsng`.                                                   |
| `artists[]`          | lossy        | Serato stores a single string; we join on `" & "`.                     |
| `album`              | lossless     | `talb`.                                                                |
| `bpm`                | lossless     | `tbpm` (ASCII decimal).                                                |
| `key_camelot`        | lossless     | `tkey`.                                                                |
| `rating`             | unsupported  | Serato has no native rating. Drop-with-warning (D6).                   |
| `cue_points.hot`     | lossless     | GEOB `Serato Markers2` CUE sub-tags.                                   |
| `cue_points.memory`  | unsupported  | Serato has no memory cues (D6); opt in via `memory_as_hot`.            |
| `cue_points.loop`    | lossless     | LOOP sub-tag.                                                          |
| `cue_points.color`   | lossless     | 24-bit RGB in the CUE sub-tag.                                         |
| `beats`              | lossless     | GEOB `Serato BeatGrid`.                                                |
| `play_count`         | lossless     |                                                                        |
| `isrc`               | lossy        | Stored in `x_serato_isrc` extension.                                   |
| `smart_crates`       | unsupported  | Opaque round-trip as `x_serato_smart_crate`.                           |

Authoritative source:
[`apps/adapters/serato/capabilities.py`](../../apps/adapters/serato/capabilities.py).

## Write safety rails

1. **pgrep gate.** `safety.guard_live_write` refuses to mutate when Serato
   is running.
2. **Backup.** Timestamped copy into `backup_dir` before mutation.
3. **Reversal log.** ndjson lines per operation under
   `.planning/adapters/serato/reversal-log/` (gitignored).
4. **Dry-run-first.** Tests always write into `tmp_path`.

## Known gaps (Phase 16 scope)

- Per-file GEOB write is wired through `SeratoAdapter.write()` (in-house
  `apps.shared.id3v2` since Thu 1 Oct 2026, mutagen before) as of v1.0-rc2 (commit `d883089`); cues and beatgrid now round-trip through the
  adapter, and the `03-8-hot-cues` conformance fixture no longer masks `cues`.
- Smart crates are opaque passthrough only.
- Real-world sign-off deferred.
