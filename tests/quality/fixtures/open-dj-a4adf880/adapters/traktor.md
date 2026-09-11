# Traktor adapter

**Status:** v0.1 (Phase 16, Apr 2026)
**Implements:** OPEN-02d
**Code:** [`apps/adapters/traktor/`](../../apps/adapters/traktor/)
**Licence:** Apache-2.0.

## Scope

Pure-stdlib Python (`xml.etree.ElementTree`) implementation of read + write
for Native Instruments Traktor's `collection.nml`. No third-party Traktor
library pulled in; mapping tables live in
[`mappers.py`](../../apps/adapters/traktor/mappers.py).

Unknown attributes and child elements round-trip verbatim (open-dj §9). The
canonical writer sorts attributes lexicographically and emits 2-space
indentation + UTF-8 + XML declaration so two consecutive writes of the same
document are byte-identical.

## Capabilities

| open-dj field        | support      | notes                                                                  |
|----------------------|--------------|------------------------------------------------------------------------|
| `track_id`           | lossless     | Computed from `LOCATION` path.                                         |
| `file_path`          | lossless     | Split into `VOLUME` + `DIR` + `FILE`.                                  |
| `title` / `artists`  | lossless / lossy | Artist is a single string; we join on `" & "`.                     |
| `album`              | lossless     | `ALBUM @TITLE`.                                                        |
| `bpm`                | lossless     | `TEMPO @BPM`.                                                          |
| `key_camelot`        | lossless     | Mapped via a 24-entry open-key <-> Camelot table.                      |
| `rating`             | lossless     | `INFO @RANKING` in 51-unit steps.                                      |
| `duration_ms`        | lossless     | `INFO @PLAYTIME` in seconds (float).                                   |
| `cue_points.hot`     | lossless     | `CUE_V2 @TYPE="0"`.                                                    |
| `cue_points.loop`    | lossless     | `CUE_V2 @TYPE="5"` + `@LEN`.                                           |
| `cue_points.memory`  | lossy        | No native concept; demoted to hot on write.                            |
| `cue_points.color`   | lossy        | Traktor paints each cue type from a fixed palette (D6); user colours on write are discarded with a warning. |
| `color_rgb`          | lossy        | Traktor `COLOR` is a 1..16 palette index.                              |
| `isrc`               | unsupported  | Stored in `x_traktor_isrc` extension.                                  |
| `smart_playlists`    | unsupported  | Opaque passthrough as `x_traktor_smartlist`.                           |
| `extended_data`      | lossless     | `EXTENDEDDATA` preserved verbatim when `preserve_extended_data=True`.  |

## Safety

No real Traktor library is ever touched in tests; every fixture is a
synthetic, UTF-8 XML blob < 20KB. The canonical writer escapes untrusted
strings through ElementTree's built-in escaping, so an attacker-controlled
`<EXTENDEDDATA>` payload never triggers XML re-entry.

## Known gaps

- **Folder-tree playlists**: v0 flattens the Traktor playlist tree into
  top-level open-dj `Playlist` items. Hierarchy is lost on round-trip.
- **MODIFIED_DATE / MODIFIED_TIME** are not plumbed into open-dj
  `provenance` yet; Phase 15's provenance wrapper is the right home.
- **Real-world `collection.nml` sign-off** is deferred -- we have no live
  Traktor install in this repo.
