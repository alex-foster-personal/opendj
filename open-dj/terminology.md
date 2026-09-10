# Cross-vendor terminology

This glossary maps what rekordbox, djay Pro, Serato, and Traktor call a
library field, and what this repo calls it in code and in the UI.

The machine-readable twin is [`synonym-map.json`](synonym-map.json).
Load it with a standard JSON reader, or look up a term:

```
python -m apps.open_dj.glossary lookup tbpm
```

`docs/glossary.md` is a different document. It covers rekordbox file
internals (ANLZ, PQTZ, PVDI), not the cross-vendor field names.

A shipped adapter must have a `vendors.<name>` object on every entry,
and a new `Track` or `CapabilityField` must gain a matching row. The
guard is `tests/open_dj/test_glossary.py`.

| canonical | ui | rekordbox | djay | serato | traktor | notes |
| --- | --- | --- | --- | --- | --- | --- |
| track_id | Track ID | identity chain | identity chain | pfil + tsng | LOCATION path | Canonical open-dj identity hash for a track. |
| file_path | File Path | FolderPath | source_uri | pfil | VOLUME + DIR + FILE | Absolute path to the audio file on disk. |
| title | Track Title | Title | title | tsng | TITLE | Track title. |
| artists | Artist | Artist | artist | tart | ARTIST | Ordered list of artist names. |
| album | Album | Album | unsupported | talb | ALBUM @TITLE | Album title. |
| bpm | BPM | BPM | BPM | tbpm | TEMPO @BPM | Tempo in beats per minute. |
| key_camelot | Key | Key | key | tkey | MUSICAL_KEY @VALUE | Musical key in Camelot notation. |
| rating | Rating | Rating | rating | unsupported | INFO @RANKING | Star rating on a 0..5 scale; unset is distinct from zero. |
| duration_ms | Time | Length | duration_s | unsupported | INFO @PLAYTIME | Track duration in milliseconds. |
| play_count | plays | history | play_count | play_count | play_count | Aggregated play count from DJ history. |
| color_rgb | Color | track color | color_index | COLOR | INFO @COLOR | Track color as 24-bit RGB. |
| cue_points | Cue Points | djmdCue | TSAF cues | Serato Markers2 | CUE_V2 | Family of hot cues, memory cues, loops, and related markers. |
| cue_points.hot | Hot Cue | djmdCue Kind 1..8 | hot cues | Markers2 CUE | CUE_V2 @TYPE="0" | Hot-cue pad / slot marker. |
| cue_points.memory | Memory Cue | memory cues | memory cues | unsupported | no native memory cue | Memory cue (not a hot-cue pad). |
| cue_points.loop | Loop | loops | loops | Markers2 LOOP | CUE_V2 @TYPE="5" | Saved loop with a start and length. |
| cue_points.color | Cue Color | cue color | cue color | CUE RGB | per-type palette | Color of an individual cue or loop marker. |
| beats | Beatgrid | PQTZ | TSAF beatgrid | Serato BeatGrid | tempo grid | Beatgrid anchors (position plus locked BPM). |
| isrc | ISRC | ISRC | isrc | x_serato_isrc | x_traktor_isrc | International Standard Recording Code. |
| content_hash | Content Hash | content_hash_hex | content_hash_hex | computed | computed | sha256 content hash of the audio file. |
| size_bytes | Size | file_size | file_size | unsupported | unsupported | Audio file size in bytes. |
| genre | Genre | Genre | unsupported | unsupported | unsupported | Genre tags. Library UI prioritizes rekordbox. |
| energy | Energy | Mixed In Key | unsupported | unsupported | unsupported | Energy 1-9, imported from Mixed In Key. |
| playlists | Playlists | djmdPlaylist | djay_pl_<uuid> | _Serato_/Subcrates/*.crate | NML playlist NODE | Ordered playlists and crates. |
| vendor_ids | Vendor IDs | rb_id | uuid | unsupported | unsupported | Original per-vendor identifiers preserved on import. |
| smart_crates | Smart Crates | unsupported | unsupported | x_serato_smart_crate | unsupported | Serato smart crates, treated as an opaque extension. |
| smart_playlists | Smart Playlists | unsupported | unsupported | unsupported | x_traktor_smartlist | Traktor SMARTLIST nodes, treated as an opaque extension. |
| extended_data | Extended Data | unsupported | unsupported | unsupported | EXTENDEDDATA | Traktor EXTENDEDDATA blob preserved as a base64 extension. |
| x_djay_streaming | Streaming | unsupported | is_local | unsupported | unsupported | djay streaming-only track flag (SoundCloud / Tidal / Beatport Link). |
