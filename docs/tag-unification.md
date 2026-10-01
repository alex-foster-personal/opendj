# Tag unification (Phase 7 Plan 02)

Unifies per-track tags across Rekordbox, djay, MIK, and on-disk audio
containers and writes the chosen values back into the file (ID3v2 for
MP3, Vorbis comments for FLAC and Ogg Vorbis / Opus, iTunes atoms for MP4;
all in-house writers, see `docs/decisions/ADR-NEW-permissive-audio-tag-io.md`). Every decision is stamped with provenance
so later phases (M6 open-dj) can replay.

## Modules

| Module                        | Responsibility                               |
|-------------------------------|----------------------------------------------|
| `apps.shared.tag_writer`      | read (tinytag) / write (in-house) dispatch   |
| `apps.tags.collect`           | build `TagSources` matrix per file           |
| `apps.tags.unify`             | apply precedence + provenance                |
| `apps.tags.preview`           | dry-run CSV for review                       |
| `apps.tags.apply`             | cautious live writer + backups + revert      |

## Precedence (CONTEXT D4)

| Field            | Source order                                 |
|------------------|----------------------------------------------|
| title, artist    | rb -> file -> filename -> djay               |
| album            | rb -> file -> djay                           |
| genre            | rb -> mik -> file -> djay                    |
| key / camelot    | mik -> rb -> djay -> file                    |
| bpm              | mik -> rb -> djay -> file                    |
| energy           | mik -> rb -> file                            |
| rating           | rb -> djay                                   |
| isrc             | file -> rb -> djay                           |

Confidence: MIK 0.95, RB 0.90, file 0.70, djay 0.65, filename 0.30.

Sanity checks: bpm in [40, 220]; energy in [1, 10]; key non-empty.
Failed sanity fall through to the next source.

## Workflow

### Preview

```
python -m apps.tags.preview path/to/track.mp3 path/to/track2.m4a
```

Writes `data/tags/unified-preview.csv`. Never mutates any file.

### Live apply (2-3 files first)

```
python -m apps.tags.apply --live --i-understand-the-risks \
    path/to/track.mp3
```

Per-file safety rails:

1. Skip if the file is an iCloud placeholder (`.icloud` sibling present).
2. Abort if Rekordbox OR djay is running (pgrep).
3. Copy to `data/tags/backups/YYYY-MM-DD/<sha256>.ext` before any write.
4. Write tags via the in-house writer for the container (`apps.shared.id3v2`,
   `apps.shared.flac_meta`).
5. Re-read and compare; on mismatch restore from backup and fail.
6. Insert a `tag_provenance` row per field (stable_id, field, value,
   source, confidence, modified_at).
7. Append a per-batch `reverse-tags-YYYYMMDD-HHMMSS.sh` script.

## Safety invariants

* **Never renames or moves** audio files. Only tag blocks change.
* **AIFF / WAV** raise `UnsupportedContainer` (v1 scope).
* **POPM rating** mapping is pinned: `{0: 0, 1: 51, 2: 102, 3: 153,
  4: 204, 5: 255}` (Rekordbox + Windows convention).
* **Provenance is insert-only**. If a unified value is wrong, run the
  reverse script; the original POPM/ID3 bytes come back, and the
  provenance rows from the failed attempt stay for audit.

## Integration with Plan 01

Plan 01 rewrites RB playlist links from alias paths to canonical paths.
Plan 02 writes unified tags into the canonical files. Run in that
order: links first, tags second. Otherwise a playlist entry could
briefly reference a stale alias path during the tag write.
