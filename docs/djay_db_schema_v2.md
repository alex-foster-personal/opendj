# djay Pro `MediaLibrary.db` — Schema v2

> **Source DB:** `~/Music/djay/djay Media Library.djayMediaLibrary/MediaLibrary.db`  
> **Format:** [YapDatabase](https://github.com/yapstudios/YapDatabase) — a plain SQLite file with a KV-collection store. Values are encoded in Algoriddim's proprietary **TSAF** binary serialisation.  
> **Scope:** Collection schemas and decoding rules; captured library contents and row counts are not included.  
> **Read-only access:** `sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)`

---

## 1. Table Inventory

### `database2` — primary domain table

| `collection` | Description |
|---|---|
| `mediaItemUserData` | All tracks — play history, rating, color, tags, cue points |
| `contentPacks` | Looper/stem packs (ADCLooperPack) |
| `contentPackMediaItems` | Individual samples inside content packs |
| `historySessionItems` | Individual play events (title, deck, startTime) |
| `mediaItemAnalyzedData` | BPM/key analysis results |
| `mediaItemTitleIDs` | Stand-alone TitleID records (ISRC + title/artist) |
| `globalMediaItemLocations` | Streaming tracks (Spotify URIs etc.) |
| `localMediaItemLocations` | Local file tracks (`file:///` URIs + bookmark blob) |
| `cloudKit` | CloudKit sync state (zone, subscription, changeToken, userRecordID) |
| `databaseInfo` | DB format version, model version, platform (NSKeyedArchiver blobs) |
| `historySessions` | Per-session containers (deviceName, startDate/endDate, itemUUIDs) |
| `products` | Installed app product IDs (`ADCProduct`) |
| `backfillTitleIDMigratorTask` | Migration task record |
| `dataMigrator` | Data migration state |
| `mediaItemPlaylists` | Root playlist (`ADCMediaItemPlaylist`) |
| `queues` | DJ queue state |

### Auxiliary SQLite tables

| Table | Schema |
|---|---|
| `relationship_relationship` | `(rowid, name, src INT, dst BLOB, rules INT, manual INT)` |
| `secondaryIndex_contentPackIndex` | `(rowid PK, name TEXT, type TEXT, addedDate REAL)` |
| `secondaryIndex_mediaItemAnalyzedDataIndex` | `(rowid PK, bpm REAL, manualBPM REAL, keySignatureIndex INT)` |
| `secondaryIndex_mediaItemIndex` | `(rowid PK, titleID TEXT, bpm REAL, musicalKeySignatureIndex INT)` |
| `secondaryIndex_mediaItemLocationIndex` | `(rowid PK, fileName TEXT)` |
| `secondaryIndex_mediaItemPlaylistIndex` | `(rowid PK, name TEXT)` |
| `secondaryIndex_mediaItemUserDataIndex` | `(rowid PK, tags TEXT, manualBPM REAL)` |
| `cloudKit_mapping_cloudKit` | `(rowid PK, recordTable_hash TEXT)` — maps database2.rowid → CKRecord hash |
| `cloudKit_queue_cloudKit` | `(uuid PK, prev, databaseIdentifier, deletedRecordIDs BLOB, modifiedRecords BLOB)` |
| `cloudKit_record_cloudKit` | `(hash PK, databaseIdentifier TEXT, ownerCount INT, record BLOB)` — NSKeyedArchive CKRecord |
| `fts_searchIndex` | FTS4 virtual table: `title, artist, genre, album, grouping, comments, composer, playlist` |
| `fts_searchIndex_content` | shadow table |
| `fts_mediaItemTitleIDSearchIndex` | FTS4 virtual table: `titleIDs, isrcs` |
| `fts_mediaItemTitleIDSearchIndex_content` | shadow table |
| `yap2` | `(extension, key, data)` — YapDatabase extension registry + snapshot |

### YapDatabase view tables (all have `_map` + `_page` variants)

| Base name | Purpose |
|---|---|
| `view_allAvailableContentPacksView` | All content packs (downloaded or not) |
| `view_allInstalledContentPacksView` | Installed content packs |
| `view_contentPackMediaItemsView` | Samples grouped by pack |
| `view_contentPacksView` | Content pack listing |
| `view_fileSystemMetadataWriterTasksView` | Pending file-metadata write tasks |
| `view_historySessionView` | History session listing |
| `view_installedContentPacksView` | Installed packs (subset) |
| `view_mediaItemKeySignatureIndexView` | Tracks grouped by key signature index |
| `view_mediaItemOriginalSourceView` | Tracks grouped by origin source |
| `view_mediaItemPlaylistView` | Playlist contents (int64-LE rowid arrays in `data`) |
| `view_mediaItemPlaylistsView` | Playlist listing |
| `view_mediaItemsView` | All media items |
| `view_mediaView` | Media display view |
| `view_queueView` | DJ queue |
| `view_recentlyAddedMediaItemsView` | Recently added tracks |
| `view_userContentPackMediaItemsView` | User-created pack samples |
| `view_userContentPacksView` | User-created packs |

---

## 2. Complete TSAF Byte-Format Specification

### 2.1 Header (20 bytes, always at offset 0)

```
Offset  Len  Type      Value / Notes
------  ---  --------  ----------------------------------------------------
 0      4    bytes     Magic: 0x54 0x53 0x41 0x46  ("TSAF")
 4      2    uint16 LE Version: always 3 (0x03 0x00)
 6      2    uint16 LE Flags:   always 3 (0x03 0x00)
 8      4    uint32 LE field_count — number of named fields in the schema block
12      4    uint32 LE Always 0x00000000 (padding / reserved)
16      4    uint32 LE schema_size_hint — empirically: total token count in schema block
```

**Example (5-star `mediaItemUserData`, 286 bytes):**
```
0000: 54 53 41 46 03 00 03 00 04 00 00 00 00 00 00 00   TSAF............
0010: 0e 00 00 00                                       ....
```
→ `field_count=4`, `schema_size_hint=14`

### 2.2 Schema Block (immediately after header)

The schema block declares field names for all subsequent value fields.
It always begins with a nested-object marker (`0x2b`) for the root class:

```
0x2b 0x08 <classname UTF-8> 0x00   — root class declaration
0x0b <field_count uint32 LE>       — schema array header
  (repeat field_count times:)
  0x08 <fieldname UTF-8> 0x00      — field name string
```

**Field indices** are 0-based within the schema array. The value section uses
`0x05 <field_index>` to reference these field names.

**Example (5-star `mediaItemUserData`):**
```
0014: 2b 08 41 44 43 4d 65 64 69 61 49 74 65 6d 55 73  +.ADCMediaItemUs
0024: 65 72 44 61 74 61 00                              erData.
      [class = "ADCMediaItemUserData"]

0053: 0b 04 00 00 00                                   — schema array (4 fields)
0058: 08 63 6f 6c 6f 72 49 6e 64 65 78 00              — field[0] = "colorIndex"
0064: 08 74 69 74 6c 65 49 44 73 00                    — field[1] = "titleIDs"
006e: 08 70 6c 61 79 43 6f 75 6e 74 00                 — field[2] = "playCount"
0079: 08 72 61 74 69 6e 67 00                           — field[3] = "rating"
```

### 2.3 Token Reference Table

All tokens are **value-first, key-second**: the value payload precedes the field-name key (or field-ref index).

| Token | Total size | Payload | Description |
|---|---|---|---|
| `0x08` | `1 + len(s) + 1` | NUL-terminated UTF-8 string | String value or field name |
| `0x13` | `1 + 3 + 4 = 8` | 3 zero-padding bytes + float32 LE | Float32 value (duration, BPM, etc.) |
| `0x0f` | `2` | 1 byte uint8 | Small integer (rating 1–5, colorIndex, etc.) |
| `0x2d` | `2` | 1 byte enum value | Enum (playlist type, track source, etc.) |
| `0x05` | `2` | 1 byte field index | Field reference (the "key" part of a pair) |
| `0x0b` | `5` | uint32 LE count | Array/object start with element count |
| `0x2b` | `1 + 1 + len(cls) + 1` | `0x08 <classname> 0x00` | Nested object class marker |
| `0x30` | `9` | 8 bytes float64 LE Core Data timestamp | Date/timestamp (seconds since 2001-01-01 UTC) |
| `0x2e` | `2` | 1 byte enum value | Alternative enum (seen for `deviceType`, `owner`, `source`, `state`) |
| `0x0d` | `1` | (no payload) | Boolean false / marker (seen before `isStraightGrid`) |
| `0x02` | `1` | (no payload) | Unknown marker / boolean true variant |
| `0x06` | `1` | (no payload) | Unknown terminator (seen at end of rated blobs) |
| `0x00` | `1` | (no payload) | Null value / end-of-object terminator |
| `0x25` | `1` | (no payload) | Unknown (seen at offset 0x10 in some historySessions) |

> **Important:** The `0x13` float32 token is **8 bytes total**: `0x13` + 3 zero-pad bytes + 4-byte LE float32. The 4 value bytes are always the 4 bytes immediately preceding `\x08fieldName\x00`. Read those bytes relative to the field-name token.

> **`0x30` timestamps:** Core Data NSDate format — seconds since 2001-01-01 UTC as float64 LE. The 8 data bytes are the 8 bytes immediately preceding `\x08fieldName\x00`. Convert: `datetime(2001,1,1) + timedelta(seconds=value)`.

### 2.4 Float32 (`0x13`) — Annotated

```
Hex:  13  00 00 00  be df 9c 43  08 64 75 72 61 74 69 6f 6e 00
      ↑   ↑-------  ↑---------  ↑ d u r  a  t  i  o  n
      tok 3-byte    float32 LE  \x08 field-name string
          padding   = 313.748 s
```

> Note: The bytes between `0x30` and the float64 value are variable-length. The reliable extraction method is "read 8 bytes immediately before `\x08fieldName\x00`".

### 2.6 Rating Tail Format (`mediaItemUserData`)

The rating is stored in the **value section** at the blob tail, not near the `"rating"` string in the schema. It is always within the last 30 bytes:

```
...  0f <rating_uint8>  05 <field_idx>  ...  00
     ↑  ↑               ↑  ↑
     tok uint8 1-5       tok field ref for "rating"
```

**Extraction algorithm:**
```python
def extract_rating_from_tsaf(data: bytes) -> int:
    tail = data[-30:]
    for i in range(len(tail) - 1):
        if tail[i] == 0x0F:
            val = tail[i + 1]
            if 1 <= val <= 5:
                return val
    return 0
```

**Warning:** Token `0x0f` can appear in the tail for other uint8 fields (e.g. `keySignatureIndex`). A more robust approach is to also verify the following `0x05 <field_idx>` matches the schema-declared index for `"rating"` (typically field index 3 in `mediaItemUserData` blobs with 4 fields).

### 2.7 Known Enum Values

**`0x2d` token — enum type**

| Context | Value | Meaning |
|---|---|---|
| `deckNumber` | 0x00 | Deck A |
| `deckNumber` | 0x01 | Deck B |
| `mediaItemPlaylists.type` | 0x00 | Root/folder playlist |

**`0x2e` token — secondary enum (seen in contentPacks)**

| Context | Value | Meaning |
|---|---|---|
| `access` | various | Content pack access level |
| `owner` | various | Pack ownership (Algoriddim vs user) |
| `source` | various | Pack source (built-in, store, user) |
| `state` | various | Installation state |

---

## 3. Per-Collection Field Maps

### 3.1 `globalMediaItemLocations`

**Class:** `ADCMediaItemLocation`  
**Key:** 32-char hex UUID (lowercase, no dashes)

| Field | Token | Type | Semantic |
|---|---|---|---|
| `uuid` | `0x08` | string | Track UUID — join key across all collections |
| `title` | `0x08` | string | Track title |
| `artist` | `0x08` | string | Artist name(s), comma-separated |
| `duration` | `0x13` | float32 | Duration in seconds |
| `isrc` | `0x08` | string | ISRC code |
| `titleIDs` | `0x0b` | array | Array of nested `ADCMediaItemTitleID` objects |
| `sourceURIs` | `0x08` | string | Streaming URI: `spotify:track:...` |

**Nested `ADCMediaItemTitleID` object (inside `titleIDs` array):**

| Field | Token | Type | Semantic |
|---|---|---|---|
| `uuid` | `0x08` | string | TitleID UUID (32-char hex) |
| `title` | `0x08` | string | Canonical title |
| `artist` | `0x08` | string | Canonical artist |
| `duration` | `0x13` | float32 | Duration in seconds |
| `isrc` | `0x08` | string | ISRC code |


### 3.2 `localMediaItemLocations`

**Class:** `ADCMediaItemLocation`  
Same fields as `globalMediaItemLocations` plus:

| Field | Token | Type | Semantic |
|---|---|---|---|
| `sourceURIs` | `0x08` | string | Local file URI: `file:///Users/.../track.mp3` (URL-encoded) |
| `urlBookmarkData` | blob | BLOB | macOS security-scoped bookmark (raw bytes, follows a string key) |

The `urlBookmarkData` is stored as raw bytes in the TSAF stream, not as a string. It follows the field-name string `"urlBookmarkData"` in the value section.

### 3.3 `mediaItemUserData`

**Class:** `ADCMediaItemUserData`  
**Key:** 32-char hex UUID  
**Typical field_count:** 4 (colorIndex, titleIDs, playCount, rating)

| Field | Schema idx | Token | Type | Semantic |
|---|---|---|---|---|
| `colorIndex` | 0 | `0x0f` | uint8 | Color label (0=none, 1–7 = colors) |
| `titleIDs` | 1 | `0x0b` | array | Array of nested `ADCMediaItemTitleID` — contains title/artist/ISRC/duration |
| `playCount` | 2 | `0x0f` | uint8 | Number of times played |
| `rating` | 3 | `0x0f` | uint8 | Star rating 1–5 (0 = unrated) — stored at blob TAIL |
| `userChangedCloudKeys` | n/a | `0x08` | string | Fields changed locally and not yet synced |
| `cuePoints` | n/a | `0x0b` | array | Cue point objects (positions, names) |
| `loopRegions` | n/a | `0x0b` | array | Loop region objects |
| `startPoint` | n/a | `0x13` | float32 | Manual start cue offset in seconds |
| `manualGain` | n/a | `0x13` | float32 | Manual gain adjustment |
| `title` | n/a | `0x08` | string | Track title (mirrored from location for quick access) |
| `artist` | n/a | `0x08` | string | Artist (mirrored) |

**Note on artist/title:** Many `mediaItemUserData` blobs have a nested `ADCMediaItemTitleID` in the `titleIDs` array that carries the authoritative title/artist/ISRC. Some blobs also mirror title and artist at the top level as string fields.

**Minimal blob structure (131 bytes, field_count=2):**
```
TSAF header (20 bytes)
  field_count=2, schema fields: [rating, colorIndex, ...]
Class marker: ADCMediaItemUserData
Schema 0x0b: field_count fields
UUID value + "uuid" key
(no nested titleIDs section in minimal blobs)
0x00 terminator
```

### 3.4 `mediaItemPlaylists` (1 row — root playlist)

**Class:** `ADCMediaItemPlaylist`  
**Key:** `mediaItemPlaylist-root`

| Field | Token | Type | Semantic |
|---|---|---|---|
| `uuid` | `0x08` | string | `"mediaItemPlaylist-root"` |
| `name` | `0x08` | string | `"Playlists"` |
| `type` | `0x2d` | enum uint8 | Playlist type (0 = root folder) |

**Hex dump (98 bytes):**
```
0000: 54 53 41 46 03 00 03 00 01 00 00 00 00 00 00 00   TSAF............
0010: 06 00 00 00 2b 08 41 44 43 4d 65 64 69 61 49 74   ....+.ADCMediaIt
0020: 65 6d 50 6c 61 79 6c 69 73 74 00                  emPlaylist.
[uuid value] 08 6d65 6469 6149 7465 6d50 6c61 796c 6973 742d 726f 6f74 00
             "mediaItemPlaylist-root"
[key]        08 75 75 69 64 00                           "uuid"
[name value] 08 50 6c61 796c 6973 7473 00               "Playlists"
[key]        08 6e61 6d65 00                             "name"
[type]       2d  00                                     enum=0
[key]        08 74 7970 65 00                            "type"
```

### 3.5 `mediaItemAnalyzedData`

**Class:** `ADCMediaItemAnalyzedData`  
**Key:** 32-char hex UUID (same UUID as matching `globalMediaItemLocations`/`localMediaItemLocations` row)

| Field | Token | Type | Semantic |
|---|---|---|---|
| `uuid` | `0x08` | string | Track UUID |
| `titleIDs` | `0x0b` | array | Nested `ADCMediaItemTitleID` (title/artist/isrc/duration) |
| `bpm` | `0x13` | float32 | Detected BPM (also in `secondaryIndex_mediaItemAnalyzedDataIndex.bpm`) |
| `isStraightGrid` | `0x0d` | bool/marker | Whether beat grid is regular (0x0d = false, grid is irregular) |
| `manualBPM` | `0x13` | float32 | User-overridden BPM (also in secondary index) |
| `keySignatureIndex` | `0x0f` | uint8 | Key signature index 0–23 (also in secondary index; Camelot mapping documented separately) |

**Key extraction:** `secondaryIndex_mediaItemAnalyzedDataIndex` is the fastest way to read BPM + key without TSAF parsing:
```sql
SELECT d.key, s.bpm, s.manualBPM, s.keySignatureIndex
FROM database2 d
JOIN secondaryIndex_mediaItemAnalyzedDataIndex s ON d.rowid = s.rowid
WHERE d.collection = 'mediaItemAnalyzedData';
```

### 3.6 `mediaItemTitleIDs`

**Class:** `ADCMediaItemTitleID`  
**Key:** 32-char hex UUID (same UUID as track)

These are "flat" TitleID records (not nested inside a location blob). They contain the same fields as the nested `ADCMediaItemTitleID` objects:

| Field | Token | Type | Semantic |
|---|---|---|---|
| `uuid` | `0x08` | string | Track UUID |
| `title` | `0x08` | string | Track title |
| `artist` | `0x08` | string | Artist |
| `duration` | `0x13` | float32 | Duration in seconds |
| `isrc` | `0x08` | string | ISRC code |

**Also indexed in:** `fts_mediaItemTitleIDSearchIndex` — `titleIDs` column contains `{{uuid}}`, `isrcs` column contains `{{ISRC}}`.

### 3.7 `historySessions`

**Class:** `ADCHistorySession`  
**Key:** UUID in uppercase dash format

| Field | Token | Type | Semantic |
|---|---|---|---|
| `uuid` | `0x08` | string | Session UUID |
| `deviceName` | `0x08` | string | Device name (e.g. `"someone's MacBook Pro"`) |
| `deviceType` | `0x2e` | enum | Device type |
| `startDate` | `0x30` | float64 | Session start — Core Data timestamp |
| `endDate` | `0x30` | float64 | Session end — Core Data timestamp |
| `itemUUIDs` | `0x0b` + `0x08` | array | UUIDs of `historySessionItems` |

**Core Data timestamp decoding:**
```python
import struct, datetime
# Read 8 bytes immediately before b'\x08startDate\x00'
raw = blob[startDate_pos - 8 : startDate_pos]
seconds = struct.unpack('<d', raw)[0]
ts = datetime.datetime(2001, 1, 1) + datetime.timedelta(seconds=seconds)
```

### 3.8 `historySessionItems`

**Class:** `ADCHistorySessionItem`  
**Key:** UUID uppercase dash format

| Field | Token | Type | Semantic |
|---|---|---|---|
| `uuid` | `0x08` | string | Item UUID |
| `sessionUUID` | `0x08` | string | Parent session UUID |
| `titleID` | `0x08` | string | Nested `ADCMediaItemTitleID` UUID |
| `title` | `0x08` | string | Track title at play time |
| `artist` | `0x08` | string | Artist at play time |
| `duration` | `0x13` | float32 | Duration in seconds |
| `deckNumber` | `0x2d` | enum | Deck: 0=A, 1=B |
| `startTime` | `0x30` | float64 | Play start — Core Data timestamp |
| `originSourceID` | `0x08` | string | Source identifier (e.g. `"finder"`) |

**Relationship table:** `relationship_relationship` links type `"historySessionItemSession"` with `src = historySessionItem.rowid` and `dst = historySession.rowid` (encoded as BLOB integer).

### 3.9 `contentPacks` / `contentPackMediaItems`

**Class:** `ADCLooperPack` / `ADCLooperPackMediaItem`  
**Key:** human-readable slug (e.g. `looper-pack-80s-retro-bounces-110`)

`contentPacks` fields: `uuid`, `name`, `summary`, `duration` (0x13), `bpm` (0x13), `keySignatureIndex` (0x0f), `access` (0x2e), `owner` (0x2e), `source` (0x2e), `state` (0x2e), `automationBudget`, and others.

`secondaryIndex_contentPackIndex` mirrors: `name`, `type`, `addedDate` (Core Data float as REAL).

---

## 4. Playlist Membership Decoding

### `view_mediaItemPlaylistView_page.data` format

The `data` column in `view_mediaItemPlaylistView_page` is a **packed array of int64 values, little-endian**, where each int64 is a `database2.rowid` of a `mediaItemUserData` row:

```python
import struct, sqlite3

con = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)
rows = con.execute(
    'SELECT "group", count, data FROM view_mediaItemPlaylistView_page'
).fetchall()
for group, count, data in rows:
    n = len(data) // 8
    rowids = struct.unpack(f'<{n}q', data[:n * 8])
    print(f"playlist '{group}': {count} tracks, rowids={rowids}")
```

Playlist membership pages and the playlist-listing pages are separate views; a root playlist need not have member tracks.

### Resolving rowid → UUID → track

```python
# Playlist rowid → database2.key (= UUID)
uuid = con.execute(
    "SELECT key FROM database2 WHERE rowid=?", (rowid,)
).fetchone()[0]

# UUID → location (title, artist, sourceURI)
location_row = con.execute(
    "SELECT collection, data FROM database2 "
    "WHERE key=? AND collection IN "
    "('localMediaItemLocations','globalMediaItemLocations')",
    (uuid,)
).fetchone()

# UUID → user data (rating, playCount, colorIndex)
userdata_row = con.execute(
    "SELECT data FROM database2 WHERE key=? AND collection='mediaItemUserData'",
    (uuid,)
).fetchone()
```

---

## 5. Join Key Reference

The **track UUID** (`database2.key`) is the universal join key across all collections.

```
database2.key (UUID, 32-char hex)
  ├── collection = 'localMediaItemLocations'   → file path, bookmark
  ├── collection = 'globalMediaItemLocations'  → streaming URI
  ├── collection = 'mediaItemUserData'         → rating, playCount, colorIndex, cues
  ├── collection = 'mediaItemAnalyzedData'     → BPM, key signature
  └── collection = 'mediaItemTitleIDs'         → canonical title, artist, ISRC

database2.rowid
  ├── secondaryIndex_mediaItemAnalyzedDataIndex.rowid → BPM, key (fast lookup)
  ├── secondaryIndex_mediaItemLocationIndex.rowid     → fileName (fast lookup)
  ├── secondaryIndex_mediaItemUserDataIndex.rowid     → tags, manualBPM
  ├── cloudKit_mapping_cloudKit.rowid                 → CKRecord hash
  └── view_*_map.rowid                                → view page membership

relationship_relationship (name='historySessionItemSession')
  src = historySessionItem.database2.rowid
  dst = historySession.database2.rowid (BLOB-encoded integer)
```

**Full track join (UUID-to-everything):**
```sql
SELECT
    loc.key                                              AS uuid,
    loc_kv.title,                                        -- from TSAF parse
    loc_kv.artist,
    loc_kv.source_uri,
    sai.bpm,
    sai.keySignatureIndex,
    -- rating requires TSAF parse of userdata blob
    ud.data                                              AS userdata_blob
FROM database2 loc
LEFT JOIN database2 ud
       ON ud.key = loc.key AND ud.collection = 'mediaItemUserData'
LEFT JOIN database2 ad
       ON ad.key = loc.key AND ad.collection = 'mediaItemAnalyzedData'
LEFT JOIN secondaryIndex_mediaItemAnalyzedDataIndex sai
       ON sai.rowid = ad.rowid
WHERE loc.collection IN ('localMediaItemLocations','globalMediaItemLocations');
```

---

## 6. CloudKit

### Tables

| Table | Purpose |
|---|---|
| `cloudKit_mapping_cloudKit` | Maps `database2.rowid` → CKRecord hash (SHA-1 base64). One row per synced database2 row. |
| `cloudKit_record_cloudKit` | Cached CKRecord blobs (NSKeyedArchive binary plists, `bplist00` header). `databaseIdentifier = 'iCloud.com.algoriddim.userdata'`. |
| `cloudKit_queue_cloudKit` | Pending upload/delete operations queued between sync cycles. |

### `database2.collection = 'cloudKit'`

| key | Content |
|---|---|
| `cloudKitHasZone` | TSAF bool — whether the CloudKit zone exists |
| `cloudKitHasZoneSubscription` | TSAF bool — whether subscribed to zone changes |
| `cloudKitServerChangeToken` | TSAF blob — `CKServerChangeToken` (tracks sync cursor) |
| `cloudKitUserRecordID` | TSAF blob — `CKRecordID` for the signed-in iCloud user |

### What CloudKit does

djay Pro uses **YapDatabaseCloudKit** to sync `mediaItemUserData` rows to iCloud private database zone `iCloud.com.algoriddim.userdata`. Every `mediaItemUserData` row has a corresponding `cloudKit_record_cloudKit` entry. When djay Pro detects that you've modified a `database2` row, it queues the delta in `cloudKit_queue_cloudKit` and eventually uploads the full CKRecord.

**Risk for writes:** If you write directly to `database2.data` without updating:
1. `cloudKit_record_cloudKit.record` — djay will detect a "stale" CKRecord and may overwrite your changes on next launch by fetching from iCloud.
2. `cloudKit_mapping_cloudKit` — no change needed (rowid → hash mapping stays valid as long as you don't change the hash).

**Recommended mitigation:** Force iCloud Documents to "Keep on This Mac" before any write pass, close djay Pro, write, then re-open. Or: invalidate `cloudKitServerChangeToken` so djay re-fetches everything (nuclear option, triggers full re-sync). The safest approach for Phase 4 is to write to `database2.data`, write a matching `cloudKit_record_cloudKit.record` blob (NSKeyedArchive of the updated CKRecord), and leave `cloudKit_queue_cloudKit` empty.

**CAUTION:** Constructing a valid NSKeyedArchive CKRecord is non-trivial. Until that is implemented, the safest write approach is: quit djay → write → launch djay (djay will re-upload the modified record). Never write while iCloud is actively syncing.

---

## 7. YapDatabase Views (`view_*`)

Every YapDatabase "extension" has:
- A `_map` table: `(rowid INTEGER PK, pageKey CHAR)` — maps database2.rowid to the page it belongs to.
- A `_page` table: `(pageKey CHAR PK, group CHAR, prevPageKey CHAR, count INT, data BLOB)` — pages of sorted rowids.

The `data` blob in `_page` is a **packed int64 LE array** of `database2.rowid` values (same encoding as playlist data).

### View registry (from `yap2`)

| Extension | YapDatabase Class | Status |
|---|---|---|
| `mediaItemPlaylistView` | `YapDatabaseAutoView` | Playlist membership |
| `mediaItemPlaylistsView` | `YapDatabaseAutoView` | Playlist listing |
| `mediaItemsView` | `YapDatabaseAutoView` | Media items |
| `mediaItemKeySignatureIndexView` | `YapDatabaseAutoView` | Tracks grouped by key |
| `historySessionView` | `YapDatabaseAutoView` | History sessions |
| `contentPacksView` | `YapDatabaseAutoView` | Content packs |
| `mediaView` | `YapDatabaseAutoView` | Media display |
| `recentlyAddedMediaItemsView` | `YapDatabaseAutoView` | Recently added items |
| `queueView` | `YapDatabaseAutoView` | Queue |
| `mediaItemOriginalSourceView` | `YapDatabaseAutoView` | Original-source grouping |

### Do we need to invalidate views after writes?

**YapDatabaseAutoView** rebuilds automatically: YapDatabase views are maintained via triggers/hooks inside the framework. They are rebuilt by djay Pro on next launch by replaying the extension's `versionTag` check. **We do not need to manually update view tables.** Djay will rebuild them when it opens the DB after our write.

However, leaving stale view data does no harm (djay ignores stale pages that don't match `versionTag`). The only safe thing to do is leave view tables **as-is** — do NOT attempt to manually update them.

---

## 8. FTS Indexes

### `fts_searchIndex`

FTS4 virtual table with columns: `title, artist, genre, album, grouping, comments, composer, playlist`.  
This index can include playlist documents; inspect the collection and use the title-ID index for title-ID search.

### `fts_mediaItemTitleIDSearchIndex`

FTS4 virtual table with columns: `titleIDs, isrcs`.  
Rows index title-ID and location records.

Format: `titleIDs = {{uuid}}` and `isrcs = {{ISRC_CODE}}` (double-brace format is Algoriddim's token delimiter for FTS).

**Do we need to rebuild FTS after writes?**

FTS4 does **not** auto-rebuild. If you modify `database2.data` for a `mediaItemTitleIDs` or location row, you should also update:
```sql
UPDATE fts_mediaItemTitleIDSearchIndex_content SET c1isrcs = '{{NEW_ISRC}}' WHERE docid = ?;
-- Then rebuild FTS index:
INSERT INTO fts_mediaItemTitleIDSearchIndex(fts_mediaItemTitleIDSearchIndex) VALUES('rebuild');
```
For rating-only writes (`mediaItemUserData`), FTS does not need updating.

---

## 9. Secondary Indexes

All secondary indexes share `rowid` with `database2`:

| Table | Columns | Auto-maintained? | Update needed? |
|---|---|---|---|
| `secondaryIndex_mediaItemAnalyzedDataIndex` | `bpm, manualBPM, keySignatureIndex` | By YapDatabase extension (on djay launch) | No — djay rebuilds |
| `secondaryIndex_mediaItemLocationIndex` | `fileName` | By extension | No |
| `secondaryIndex_mediaItemPlaylistIndex` | `name` | By extension | No |
| `secondaryIndex_mediaItemUserDataIndex` | `tags, manualBPM` | By extension | No |
| `secondaryIndex_mediaItemIndex` | `titleID, bpm, musicalKeySignatureIndex` | By extension | No |
| `secondaryIndex_contentPackIndex` | `name, type, addedDate` | By extension | No |
| `secondaryIndex_userContentPackIndex` | (unknown columns) | By extension | No |

**Version tag checking:** YapDatabase extensions check `yap2(extension, key='versionTag')` on open. If our write changes the data but not the versionTag, the extension will see the modified data and rebuild its index on next djay launch.

---

## 10. Writing Safely

### Minimum write procedure for `mediaItemUserData` (rating/playCount/colorIndex)

1. **Pre-flight checks:**
   - Confirm djay Pro is not running: `pgrep -x djay` must return nothing.
   - Confirm iCloud is not actively syncing: `brctl log --wait 2 | grep MediaLibrary` (or just wait 30s after closing djay).
   - Take a backup: `cp "$HOME/Music/djay/djay Media Library.djayMediaLibrary/MediaLibrary.db" /tmp/djay_backup_$(date +%Y%m%dT%H%M%S).db`

2. **What to update atomically:**
   ```sql
   BEGIN IMMEDIATE;
   UPDATE database2 SET data = ? WHERE collection = 'mediaItemUserData' AND key = ?;
    -- secondaryIndex_mediaItemUserDataIndex is rebuilt by YapDatabase
   -- cloudKit_record_cloudKit: ideally update but leave for now (djay will re-upload)
   COMMIT;
   ```

3. **What NOT to touch:**
   - `view_*_map`, `view_*_page` — leave as-is, djay rebuilds on open.
   - `fts_searchIndex`, `fts_mediaItemTitleIDSearchIndex` — not affected by userData writes.
   - `cloudKit_record_cloudKit` — risky to touch without NSKeyedArchive knowledge; leave for now.
   - `yap2.snapshot` — do NOT change (djay uses this to detect external changes).

4. **Post-write verification:**
   ```python
   con = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)
   data = con.execute(
       "SELECT data FROM database2 WHERE collection='mediaItemUserData' AND key=?",
       (uuid,)
   ).fetchone()[0]
   rating = extract_rating_from_tsaf(data)
   assert rating == expected_rating
   ```

5. **CloudKit fight risk:**  
   On next djay launch, djay will notice the `database2.data` changed (via its own read-back) but `cloudKit_record_cloudKit.record` is stale. It will either: (a) accept our local value and upload it to iCloud, or (b) fetch from iCloud and overwrite ours if the iCloud version is newer. Behaviour (a) is the likely outcome when djay is the sole writer. To be safe, do a read-back after reopening djay (within 30 seconds) and confirm the value stuck.

6. **YapDatabase `yap2` bookkeeping:**  
   The `yap2(extension='', key='snapshot')` value tracks the database change sequence. YapDatabase will detect the external modification via SQLite's built-in WAL/journal. **Do not modify `yap2`.** Djay re-reads the DB on open.

### Cross-platform note

Windows djay and macOS djay produce DB files with different table counts (Win: 53 tables, Mac: 59 tables). Do not use a Windows-produced DB as a template for macOS writes or vice versa.

---

## 11. Unknown / Partially-Understood Tokens

| Token | Observed context | Best hypothesis | Confidence |
|---|---|---|---|
| `0x0d` | Before `\x08isStraightGrid\x00` in analyzed data | Boolean marker (false/absent) | Medium |
| `0x02` | After `0x00` in some blob tails | Boolean false or end-of-section | Low |
| `0x06` | After field references in a blob tail | Unknown field separator or padding | Low |
| `0x25` | At offset 0x10 in some `historySessions` (preceding `0x00`) | Unknown header extension | Low |
| `0x2e` | Before field names in `contentPacks` (`access`, `owner`, `source`, `state`) | Secondary enum type (different from `0x2d`) | Medium |
| `0x0b` with count `01000000` (LE) | Before nested `ADCMediaItemTitleID` in location blobs | Array count = 1 (endian already correct: value 1) | High |
| Bytes `be df 9c 43` etc. | In `0x13` token payload regions | Part of float32 value payload | High — confirmed |


The `0x05 04`, `0x05 06`, `0x05 07` etc. are field references **beyond the declared schema size** — suggesting there are additional fields not listed in the `0x0b` schema array (possibly a v2 extension of the schema block). They likely correspond to `userChangedCloudKeys`, `cuePoints`, `loopRegions`, `startPoint`, etc.

---

## 12. Appendix: Quick Reference

### Reading a rating
```python
def get_rating(db_path, uuid: str) -> int:
    con = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)
    row = con.execute(
        "SELECT data FROM database2 WHERE collection='mediaItemUserData' AND key=?",
        (uuid,)
    ).fetchone()
    con.close()
    if not row:
        return 0
    data = row[0]
    tail = data[-30:]
    for i in range(len(tail) - 1):
        if tail[i] == 0x0f and 1 <= tail[i + 1] <= 5:
            return tail[i + 1]
    return 0
```

### Reading BPM (fast path via secondary index)
```python
def get_bpm(db_path, uuid: str) -> float | None:
    con = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)
    row = con.execute(
        """SELECT COALESCE(s.manualBPM, s.bpm)
           FROM database2 d
           JOIN secondaryIndex_mediaItemAnalyzedDataIndex s ON d.rowid = s.rowid
           WHERE d.collection = 'mediaItemAnalyzedData' AND d.key = ?""",
        (uuid,)
    ).fetchone()
    con.close()
    return row[0] if row else None
```

### ISRC lookup (fast path via FTS)
```python
def find_uuid_by_isrc(db_path, isrc: str) -> str | None:
    con = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)
    row = con.execute(
        """SELECT d.key FROM fts_mediaItemTitleIDSearchIndex_content f
           JOIN database2 d ON d.rowid = f.docid
           WHERE f.c1isrcs = ? AND d.collection = 'mediaItemTitleIDs'""",
        (f"{{{{{isrc}}}}}",)
    ).fetchone()
    con.close()
    return row[0] if row else None
```

---

*This reference preserves schema and decoding notes. Original research captures and private companion sources are withheld from the public derivative.*
