# djay Pro MediaLibrary.db — Schema Notes

Source: `~/Music/djay/djay Media Library.djayMediaLibrary/MediaLibrary.db`
Format: YapDatabase KV store with custom **TSAF** binary value format (NOT Core Data, NOT NSKeyedArchiver).

## Key tables

- `database2(rowid, collection, key, data BLOB, metadata BLOB)` — all domain objects live here
- `relationship_relationship(rowid, name, src INT, dst BLOB, rules, manual)` — typed edges between database2 rows
- `secondaryIndex_mediaItemLocationIndex(rowid, fileName)` — mirrors database2.rowid for local tracks
- `secondaryIndex_mediaItemAnalyzedDataIndex(rowid, bpm, manualBPM, keySignatureIndex)`
- `view_mediaItemPlaylistView_page(group, data)` — pagination; data = packed int64 LE rowid arrays

## Collections in `database2`

| collection | count | contents |
|---|---|---|
| `mediaItemUserData` | 6574 | all tracks (playCount, rating, color, tags) |
| `globalMediaItemLocations` | 18 | streaming tracks (Spotify URIs) |
| `localMediaItemLocations` | 6 | local file tracks (file:// URLs + bookmark blob) |
| `mediaItemPlaylists` | 1 | playlists (currently only root) |
| `mediaItemAnalyzedData` | 24 | BPM/key analysis |
| `historySessions` / `historySessionItems` | 2/31 | per-session play history |

## TSAF format

Header: magic `TSAF` (0x54534146), uint16 version (=3), uint16 flags, uint32 unknown, uint32 field_count.

Field stream tokens:
- `0x08 <utf8>\0` — string
- `0x13 <float64 LE>` — double
- `0x2b 0x08 <classname>\0` — start nested object
- `0x0b ...` — nested array
- `0x2d <byte>` — enum
- `0x05 <byte>` — small int/bool
- `0x00` — null / terminator

**String pairing:** The first string is the class name (no key). After that, strings alternate as `value` then `key` — i.e. the value comes BEFORE its field-name label.

## Track location fields

Local (`collection='localMediaItemLocations'`): `uuid`, `title`, `artist`, `duration`, `isrc`, `sourceURIs` (= `file:///...`), `urlBookmarkData` (BLOB within TSAF), plus nested `ADCMediaItemTitleID` with album/ISRC info.

Streaming (`collection='globalMediaItemLocations'`): same but `sourceURIs` = `spotify:track:...` or similar, no bookmark.

## Gotchas

- All media items share a UUID across `globalMediaItemLocations`/`localMediaItemLocations` and `mediaItemUserData`. Join on `database2.key`.
- Playlist track membership is in `view_mediaItemPlaylistView_page.data` as int64 LE arrays of `database2.rowid`. Currently only the root playlist exists.
- To read safely: `sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)`.
