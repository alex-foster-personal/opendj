# open-dj v0 -- Specification

**Status:** `draft -- expect breakage`
**Version:** `0.2-draft`
**Date:** 2026-04-16
**Last-modified:** 2026-04-17
**License:** spec text CC-BY-4.0; reference code Apache-2.0
**Audience:** DJs, DJ-software vendors, library-tool authors, archivists

> **TL;DR**
> - open-dj is a **vendor-neutral, provenance-aware metadata format** for DJ libraries:
>   tracks, cue points, beatgrids, playlists, pairings, and play sessions.
> - It is **canonical JSON** (RFC 8785 JCS) with a published JSON Schema, so every
>   field is hashable, diffable, and round-trippable.
> - It carries a **provenance envelope** (`value / source / confidence / modified_at`)
>   on every analysed field, so two vendors disagreeing about BPM is data, not a bug.
> - The pitch is not altruism. The pitch is: **your library survives rekordbox dying.**

---

## 0. Reading this document

Sections 1–3 frame intent. Section 4 is the spec core (schemas + examples).
Sections 5–9 lock down the contracts that make round-tripping tractable.
Section 10 lists what is deliberately unresolved at v0. Sections 11–13 cover
versioning, governance, and the road to 1.0. Appendices A–B give a worked
example and a conformance corpus bootstrap list.

Schema fragments are illustrative at v0. As of v0.2 each schema carries a
resolvable `$id` under `https://open-dj.org/schema/0.2/`; the hosting URL
is not yet live but the identifier is stable and round-trips through
`$ref`.

---

## 1. Mission

Every serious DJ eventually hits the same wall: their library is held hostage by
whichever application analysed it first. Rekordbox owns cue points in a SQLite
schema nobody else reads fluently. djay Pro owns them in a proprietary binary
blob (TSAF). Serato hides them inside ID3 `GEOB` frames whose layout was never
officially published. Traktor exports a dialect of XML (`collection.nml`) with
its own cue-point type enum and a fixed five-colour palette. None of these
formats carries provenance — a BPM is just a number, with no record of who
analysed it, when, or with what confidence.

The practical cost: moving 10k tracks between applications is a destructive
operation. Ratings vanish (Serato has no rating field). Cue-point colours
collapse to whichever palette survives. Memory cues become hot cues or the
reverse. Analysis you paid a human ear for gets silently overwritten by the
next app's auto-analysis. Tools like Lexicon DJ exist to paper over this —
profitably — precisely because no open format does.

open-dj is the format that *should* exist underneath Lexicon. It is deliberately
scoped as a **library interchange and archival format**, not a runtime database
and not a transport protocol.

### 1.1 Relationship to OneLibrary

In October 2025 AlphaTheta, Algoriddim, and Native Instruments announced
**OneLibrary**, a shared USB-drive library format aimed at letting a single
stick carry a set between rekordbox, djay Pro, and Traktor on club hardware.
OneLibrary is **not** open-dj and does not replace the need for it:

- OneLibrary is proprietary; the schema is not published.
- OneLibrary is USB-on-CDJ-scoped — it is a *playback transport*, not an
  archival or sync format.
- Serato is not a participant.
- Vendors explicitly do not guarantee full metadata fidelity; cue points and
  beatgrids are delivered best-effort.
- There is no provenance layer: a BPM is still just a number.

open-dj is the complement: a local, text-first, hash-stable, versioned format
with a preserved-unknowns rule and a provenance envelope. If OneLibrary ever
opens its schema, open-dj will add an adapter to it like any other vendor.

---

## 2. Non-goals

open-dj is **not**:

1. **A runtime database.** Applications keep their own indexes. open-dj is the
   interchange / archival layer.
2. **A sync protocol.** Merge semantics, conflict resolution, and transport
   (filesystem, S3, CRDT, git-of-metadata) are out of scope for v0. The
   provenance envelope is designed to *enable* a sync protocol later.
3. **An audio-file format.** open-dj references audio by path + content hash.
4. **A waveform format.** Waveform summary data (peaks, RMS, spectral) is not
   in v0. Adapters may carry vendor waveform blobs behind `x_*` keys.
5. **A controller or firmware spec.** MIDI mappings, pad layouts, and hardware
   pairing are out of scope.
6. **A DRM or streaming-rights scheme.** open-dj has no opinion about licences
   attached to audio content.
7. **OneLibrary-equivalent.** open-dj does not define a USB-on-CDJ bootable
   library. See §1.1.
8. **A cloud service.** There is no registry, no account system, no server.

---

## 3. Design principles

Five principles. In tension, earlier principles win.

### 3.1 Read-first, ratify existing

open-dj should *recognise* more than it *invents*. Concretely: borrow Camelot
notation for key, MIK's 1–10 scale for energy, Rekordbox XML cue-point
semantics as the hot/memory baseline, and the `sha1(ISRC | chromaprint + dur +
size)` stable-ID shape that the community has converged on. Invention is
reserved for the glue — the provenance envelope, pairings, and named
play-orders — where nothing comparable exists.

### 3.2 Provenance is a first-class field, not a comment

Every analysed value carries `{value, source, confidence?, modified_at}`.
This is the ETag for a future sync protocol. It also means a BPM disagreement
between Rekordbox and MIK is *representable* rather than a destructive
overwrite.

### 3.3 Preserve unknown fields on round-trip

Parsers MUST preserve unknown non-schema keys — specifically every `x_*`
extension — when reading and writing. This is the rule every lossy converter
violates. It is how vendors can experiment without blocking the spec.

### 3.4 Canonical, hashable, diffable

The on-disk format is canonical JSON per RFC 8785 (JCS): sorted keys, UTF-8,
no insignificant whitespace, shortest round-trip numbers. Consequence: a
`sha256` of the file is a stable content ID. Two tools writing the same
library produce byte-identical output.

### 3.5 Honest about scope, honest about open questions

v0 is a strawman. Section 10 lists what is not yet decided. "We'll figure it
out" is not a spec commitment. Where a design choice is load-bearing and
unresolved, it is flagged, not papered over.

### 3.6 Serialization choice: JSON + JSON Schema 2020-12

Considered and rejected:

- **TOML** — weak for nested arrays of objects; cue-point arrays look terrible.
- **YAML (as canonical)** — anchor/alias ambiguity, multiple dialects, weaker
  hash-stability story. Accepted as an *optional* human-edit mirror.
- **Protobuf / FlatBuffers** — tooling burden for hobbyists; not diffable.
- **SQLite as file format** — not text-diffable; worse for git.

JSON + JSON Schema wins on: tooling ubiquity (`ajv`, `jsonschema`), canonical
form has an RFC, JSON Lines extends it naturally for bulk streams, and every
target language parses it in the standard library.

---

## 4. Core entities

The v0 core is nine entities: **Track, CuePoint, BeatGrid, Playlist,
PlayOrder, Pairing, Session, Transition, ProvenanceValue**.

Schema fragments below omit `$schema` and `$id` for brevity; the normative
package will declare both and use `draft 2020-12`.

### 4.1 `ProvenanceValue<T>` — the envelope

Most analysed fields are wrapped. Identity fields (title, artist, ISRC) are
*not* wrapped — they are observations, not opinions.

```json
{
  "type": "object",
  "required": ["value", "source", "modified_at"],
  "properties": {
    "value": {},
    "source": {
      "enum": ["mik", "rekordbox", "djay", "serato", "traktor",
               "open-dj-tool", "manual", "inferred"]
    },
    "confidence": { "type": "number", "minimum": 0, "maximum": 1 },
    "modified_at": { "type": "string", "format": "date-time" }
  },
  "additionalProperties": true
}
```

Example (BPM analysed by Rekordbox on import, then overridden by a human):

```json
{ "value": 128.02, "source": "manual", "confidence": 1.0,
  "modified_at": "2026-03-11T18:42:11Z",
  "x_prior": { "value": 128.03, "source": "rekordbox",
               "modified_at": "2026-02-02T09:00:00Z" } }
```

### 4.2 `Track`

```json
{
  "type": "object",
  "required": ["track_id", "title", "artists", "duration_ms",
               "file_path", "content_hash"],
  "properties": {
    "track_id":      { "type": "string", "pattern": "^[0-9a-f]{40}$" },
    "title":         { "type": "string" },
    "artists":       { "type": "array", "items": { "type": "string" } },
    "album":         { "type": "string" },
    "isrc":          { "type": "string" },
    "duration_ms":   { "type": "integer", "minimum": 0 },
    "file_path":     { "type": "string" },
    "content_hash":  { "type": "string", "pattern": "^sha256:[0-9a-f]{64}$" },
    "bpm":           { "$ref": "#/$defs/ProvenanceValue" },
    "key":           { "$ref": "#/$defs/ProvenanceValue" },
    "energy":        { "$ref": "#/$defs/ProvenanceValue" },
    "rating":        { "$ref": "#/$defs/ProvenanceValue" },
    "cue_points":    { "type": "array", "items": { "$ref": "#/$defs/CuePoint" } },
    "beatgrid":      { "$ref": "#/$defs/BeatGrid" },
    "vendor_ids":    { "type": "object", "additionalProperties": { "type": "string" } }
  },
  "patternProperties": { "^x_": {} }
}
```

Canonical example:

```json
{
  "track_id": "7d865e959b2466918c9863afca942d0fb89d7c9a",
  "title": "Lanterns",
  "artists": ["Marlow Quay"],
  "album": "Paper Harbors",
  "isrc": "GBCEN0900132",
  "duration_ms": 634000,
  "file_path": "music/Marlow Quay/lanterns.flac",
  "content_hash": "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
  "bpm":   { "value": 128.0, "source": "rekordbox", "modified_at": "2026-02-02T09:00:00Z" },
  "key":   { "value": "8A",  "source": "mik",       "modified_at": "2026-02-02T09:02:00Z" },
  "energy":{ "value": 7,     "source": "mik",       "modified_at": "2026-02-02T09:02:00Z" },
  "rating":{ "value": 5,     "source": "manual",    "modified_at": "2026-02-10T20:01:00Z" },
  "vendor_ids": { "rekordbox": "12345678", "djay": "B3F1-..." }
}
```

Vendor mapping (abbreviated — full tables in §7):

| field | Rekordbox | djay | Serato | Traktor |
|---|---|---|---|---|
| `bpm` | `DjmdContent.BPM` (×100) | TSAF `bpm` | GEOB `Serato BeatGrid` | `TEMPO.BPM` |
| `key` | `DjmdKey.ScaleName` | TSAF `key` | GEOB `Serato Markers2` subtag | `INFO.KEY` |

### 4.3 `CuePoint`

Superset of vendor cue-point types. Not every vendor round-trips every type.

```json
{
  "type": "object",
  "required": ["position_ms", "type"],
  "properties": {
    "position_ms": { "type": "integer", "minimum": 0 },
    "type": {
      "enum": ["hot", "memory", "loop_in", "loop_out",
               "grid", "fade_in", "fade_out", "load"]
    },
    "slot":   { "type": "integer", "minimum": 0, "maximum": 31 },
    "name":   { "type": "string" },
    "color":  { "type": "string", "pattern": "^#[0-9a-fA-F]{6}$" },
    "length_ms": { "type": "integer", "minimum": 0 },
    "source": { "$ref": "#/$defs/ProvenanceValue" }
  },
  "patternProperties": { "^x_": {} }
}
```

Notes:

- `slot` is the hot-cue pad index when `type = hot`. Rekordbox A–H map to 0–7.
  Serato 8 pads map to 0–7. Traktor supports up to 32 hot-cues per track.
- `color` is sRGB hex, not a palette index. Palette translation is best-effort;
  see §7.2.
- Loops are represented as two cue points (`loop_in`, `loop_out`) sharing a
  `slot`, or as a single `loop_in` with `length_ms`. Readers MUST accept both.

### 4.4 `BeatGrid`

```json
{
  "type": "object",
  "required": ["origin_ms", "bpm"],
  "properties": {
    "origin_ms": { "type": "number" },
    "bpm":       { "type": "number", "minimum": 1 },
    "version":   { "type": "integer", "minimum": 1, "default": 1 },
    "algorithm": { "enum": ["constant", "variable", "manual", "unknown"] },
    "beats":     {
      "type": "array",
      "items": { "type": "number" },
      "description": "Optional explicit beat positions in ms; required when algorithm = variable."
    },
    "source":    { "$ref": "#/$defs/ProvenanceValue" }
  }
}
```

For constant-tempo tracks, `origin_ms + bpm` suffices and `beats[]` is omitted.
Variable-tempo tracks MUST provide `beats[]`; algorithmic reconstruction is
explicitly out of scope.

### 4.5 `Playlist`

```json
{
  "type": "object",
  "required": ["playlist_id", "name", "tracks_ordered"],
  "properties": {
    "playlist_id": { "type": "string" },
    "parent_id":   { "type": "string" },
    "name":        { "type": "string" },
    "tracks_ordered": {
      "type": "array",
      "items": { "type": "string", "description": "track_id" }
    },
    "play_orders": {
      "type": "array",
      "items": { "$ref": "#/$defs/PlayOrder" }
    }
  }
}
```

### 4.6 `PlayOrder`

open-dj-native. Named orderings of the same playlist for different contexts
(open set, peak-time, warm-down). No vendor currently round-trips multiple
named orders; export behaviour is defined in §7.4.

```json
{
  "type": "object",
  "required": ["name", "entries"],
  "properties": {
    "name": { "type": "string" },
    "entries": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["track_id", "position"],
        "properties": {
          "track_id":    { "type": "string" },
          "position":    { "type": "integer", "minimum": 0 },
          "target_key":  { "type": "string" },
          "target_tempo":{ "type": "number" },
          "key_sync":    { "type": "boolean" }
        }
      }
    }
  }
}
```

### 4.7 `Pairing`

Track-to-track relationship. Directed. Sourced.

```json
{
  "type": "object",
  "required": ["from_track_id", "to_track_id", "direction", "source"],
  "properties": {
    "from_track_id": { "type": "string" },
    "to_track_id":   { "type": "string" },
    "direction":     { "enum": ["into", "out_of", "either"] },
    "source":        { "enum": ["manual", "learned", "ai"] },
    "notes":         { "type": "string" },
    "confidence":    { "type": "number", "minimum": 0, "maximum": 1 }
  }
}
```

`direction = either` is the symmetric case. Transitivity is **not** implied:
A→B and B→C do not imply A→C.

### 4.8 `Session` and 4.9 `Transition`

A session is a performance instance — a gig, a practice run, a radio recording.
Sessions carry an event stream, which the adapter may compact into transitions.

```json
{
  "type": "object",
  "required": ["session_id", "started_at", "events"],
  "properties": {
    "session_id":  { "type": "string" },
    "name":        { "type": "string" },
    "started_at":  { "type": "string", "format": "date-time" },
    "ended_at":    { "type": "string", "format": "date-time" },
    "events": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["timestamp", "deck", "action"],
        "properties": {
          "timestamp": { "type": "string", "format": "date-time" },
          "deck":      { "type": "integer", "minimum": 1 },
          "track_id":  { "type": "string" },
          "action":    { "enum": ["load","play","pause","cue","seek",
                                   "fader","eq","fx","tempo"] },
          "value":     {}
        }
      }
    },
    "transitions": {
      "type": "array",
      "items": { "$ref": "#/$defs/Transition" }
    }
  }
}
```

```json
{
  "type": "object",
  "required": ["at", "from_deck", "to_deck", "class"],
  "properties": {
    "at":         { "type": "string", "format": "date-time" },
    "from_deck":  { "type": "integer" },
    "to_deck":    { "type": "integer" },
    "from_track_id": { "type": "string" },
    "to_track_id":   { "type": "string" },
    "class":      { "enum": ["cut","blend","filter_sweep","fx","quick_double"] },
    "duration_ms":{ "type": "integer", "minimum": 0 }
  }
}
```

---

## 5. Identifier strategy

`track_id` is the primary key across the format. It is a 40-character lowercase
hex SHA-1 computed as follows:

1. If a normalised ISRC is present: `sha1(normalized_isrc)`.
2. Otherwise: `sha1(chromaprint_fingerprint[:64] || duration_ms || size_bytes)`
   with `||` denoting concatenation of UTF-8 / decimal representations joined
   by `|`.
3. Otherwise (identity-of-last-resort): `sha1(absolute_path || mtime)` —
   flagged with `source: "inferred"` and strongly discouraged for archival.

ISRC normalisation: strip non-alphanumerics, uppercase, reject anything not
matching `^[A-Z]{2}[A-Z0-9]{3}[0-9]{7}$`.

Lookup chain order is frozen in v0: ISRC first, fingerprint second, path+mtime
last. This order is what makes re-encoded files (same content, different
container) collide on `track_id` — the desirable behaviour for archival.

Supporting IDs:

- `content_hash` — `sha256:…` of the audio file bytes. Integrity only.
- `vendor_ids` — opaque map of vendor-native IDs, used for writing adapters
  that need to locate the source row on re-export.

---

## 6. Analysis provenance

Analysed fields are wrapped in `ProvenanceValue<T>` (§4.1). Rules:

- `source` is enum-constrained. Adapters MUST use the canonical vendor name.
- `confidence` is optional and vendor-defined; when absent, consumers treat as
  "unknown-but-trusted".
- `modified_at` is RFC 3339 UTC, second precision minimum.
- History is **not** a list field. Superseded values live under `x_prior` as a
  `ProvenanceValue` (singular) or `x_history` as an ordered array if a tool
  wants to keep more than one generation. The core spec only requires the
  current `value`.

Worked example: BPM analysed by Rekordbox (128.03), re-analysed by MIK
(128.02), then manually locked by the DJ. The final envelope:

```json
"bpm": {
  "value": 128.0, "source": "manual", "confidence": 1.0,
  "modified_at": "2026-03-11T18:42:11Z",
  "x_history": [
    { "value": 128.03, "source": "rekordbox", "modified_at": "2026-02-02T09:00:00Z" },
    { "value": 128.02, "source": "mik",       "modified_at": "2026-02-05T14:11:00Z" }
  ]
}
```

Identity fields — `title`, `artists`, `album`, `isrc`, `duration_ms`,
`file_path`, `content_hash`, `track_id` — are **not** provenance-wrapped. They
are observed facts; if they change, the `track_id` (typically) changes too.

---

## 7. Vendor mapping tables

These tables are the honest part. Every row that says *"lossy"* is a place where
round-tripping a library will lose information without manual reconciliation.

### 7.1 Track basics

| Field | Rekordbox DB | Rekordbox XML | djay Pro (TSAF) | Serato | Traktor NML |
|---|---|---|---|---|---|
| title | `DjmdContent.Title` | `Name` | TSAF `title` | ID3 `TIT2` | `ENTRY.TITLE` |
| artist | `DjmdContent.ArtistID→Name` | `Artist` | TSAF `artist` | ID3 `TPE1` | `ENTRY.ARTIST` |
| album | `DjmdContent.AlbumID→Name` | `Album` | TSAF `album` | ID3 `TALB` | `ALBUM.TITLE` |
| duration_ms | `DjmdContent.Length` (s ×1000) | `TotalTime` (s) | TSAF `duration` | ID3 `TLEN` | `INFO.PLAYTIME` |
| bpm | `DjmdContent.BPM` (×100) | `AverageBpm` | TSAF `bpm` | GEOB `Serato BeatGrid` | `TEMPO.BPM` |
| key | `DjmdKey.ScaleName` (Camelot) | `Tonality` | TSAF `key` | GEOB `Serato Markers2` | `INFO.KEY` |
| isrc | `DjmdContent.ISRC` | `ISRC` attr | TSAF `isrc` | ID3 `TSRC` | `INFO.ISRC` |
| file_path | `DjmdContent.FolderPath` | `Location` (URL-encoded) | TSAF `filePath` | filesystem | `LOCATION` |

### 7.2 Cue points — the hardest row

Cue-point models diverge on type vocabulary, slot count, colour palette, loop
representation, and whether memory cues exist at all.

| Concept | Rekordbox | djay | Serato | Traktor | open-dj |
|---|---|---|---|---|---|
| hot cues | A–H (8) | slots (TBD, M2 P4) | 8 pads | up to 32 | `type=hot`, `slot=0..` |
| memory cues | unlimited | unlimited | **not supported** | merged into cue list | `type=memory` |
| loops | as memory cue with length | TSAF loop record | GEOB Markers2 loops | `CUE_V2 TYPE=5` | `loop_in`/`loop_out` |
| grid marker | first memory cue or explicit | beatgrid origin | GEOB BeatGrid | `CUE_V2 TYPE=4` | `type=grid` |
| load marker | n/a | n/a | n/a | `CUE_V2 TYPE=3` | `type=load` |
| fade in/out | n/a | n/a | n/a | `CUE_V2 TYPE=1,2` | `type=fade_in`/`fade_out` |
| colour palette | 16 curated colours | 8 colours (observed) | 18 colours | **fixed 5** | free sRGB hex |
| colour translation | nearest-Lab | nearest-Lab | nearest-Lab | nearest-Lab + type-forced | — |

Traktor's colour field is not user-defined: hot cues are blue, loops green,
grid white, load yellow, fades orange. Writing open-dj→Traktor, `color` is
discarded and reconstructed from `type`. Writing Traktor→open-dj, `color` is
derived from `type` on read.

Serato does not expose memory cues; open-dj `memory` points are dropped on
Serato export or downgraded to `hot` if a free pad exists (adapter policy).

### 7.3 Ratings

| Vendor | Storage | Raw values | Stars |
|---|---|---|---|
| Rekordbox DB | `DjmdContent.Rating` | 0..5 | direct |
| Rekordbox XML | `Rating` attr | 0 / 51 / 102 / 153 / 204 / 255 | raw / 51 |
| djay Pro | TSAF tail `0x0f <u8>` | 1..5 | direct |
| Traktor | NML `RANKING` attr | 0 / 51 / 102 / 153 / 204 / 255 | raw / 51 |
| ID3 POPM (neutral) | `POPM` frame with email key | 0..255 | tool-defined |
| **Serato** | **no native field** | — | **lossy** |

open-dj stores `rating.value` as an integer 0..5. Writing to Serato requires
either dropping the rating or encoding it into `x_serato_rating_column` (an
abused Composer/Grouping column). Neither is round-trippable without
per-library policy.

### 7.4 Playlists and play-orders

| Concept | Rekordbox | djay | Serato | Traktor | open-dj |
|---|---|---|---|---|---|
| hierarchical folders | yes (`DjmdPlaylist.ParentID`) | yes | yes (crates + subcrates) | yes (`NODE` tree) | `Playlist.parent_id` |
| track order | `DjmdSongPlaylist.TrackNo` | rowid array | crate file order | `ENTRY` sequence | `tracks_ordered[]` |
| named alt orders | **no** | **no** | **no** | **no** | `play_orders[]` |
| per-track sync override | **no** | **no** | **no** | **no** | `PlayOrder.entries[].key_sync` |

Named alt orders and per-track overrides are open-dj-native. On export, the
first `play_orders[]` entry replaces `tracks_ordered[]`; additional entries
are dropped with a warning (lossy) or serialised into an adapter-specific
comment field behind `x_*` keys (best-effort).

### 7.5 Play history

| Vendor | Location | Granularity |
|---|---|---|
| Rekordbox | `DjmdHistoryLog` + `DjmdSongHistory` | per-track timestamps |
| djay Pro | `historySessions` / `historySessionItems` tables | per-session + per-track |
| Serato | `_Serato_/History/Sessions/*.session` | per-session binary log |
| Traktor | `history_YYYY-MM-DD.nml` | per-date NML |

open-dj `Session.events[]` is a superset. Import from any vendor is lossless
modulo vendor-specific action enums, which land under `x_*`.

---

## 8. Serialization

**Canonical form:** RFC 8785 JSON Canonicalization Scheme (JCS).

- UTF-8, no BOM.
- Object keys sorted lexicographically (code-point order).
- No insignificant whitespace; single space after `:` and `,` is **not**
  canonical — zero whitespace is.
- Numbers in shortest round-trip `ECMA-262` form.
- Lowercase hex for all hash-like strings (`track_id`, `content_hash`).

**File shapes:**

- `track.open-dj.json` — single-track sidecar, placed next to the audio file
  or in a parallel metadata tree.
- `library.open-dj.json` — aggregate root document with `tracks[]`,
  `playlists[]`, `pairings[]`, `sessions[]`.
- `*.open-dj.jsonl` — one canonical JSON document per line, for bulk streams
  and append-only logs. Document type is carried in a mandatory top-level
  `kind` field (`"track" | "playlist" | …`).

**Content hash of the metadata itself:**
`open-dj-hash = sha256(canonical_bytes)`. This is how a sync protocol (future)
detects "nothing to do".

**Compression:** `.zst` and `.gz` are permitted transport wrappers. They are
not part of canonical form; the hash is always taken over the uncompressed
canonical bytes.

---

## 9. Extension mechanism

Any key matching the regex `^x_[a-z0-9][a-z0-9_]*$` is an extension. Rules:

1. Extensions MAY appear at any object level, including inside
   `ProvenanceValue`.
2. Unknown extensions MUST be preserved on round-trip by conforming parsers.
   This is not a SHOULD.
3. Extensions MUST NOT be required for a document to validate against the
   core schema.
4. Extensions do not bump the schema version. Adding `x_mik_rhythmic_class`
   in a tool does not require a spec change.
5. Reserved vendor prefixes at v0:
   `x_rekordbox_`, `x_djay_`, `x_serato_`, `x_traktor_`, `x_mik_`,
   `x_onelibrary_`. Other authors SHOULD prefix with a project/author name,
   e.g. `x_muxlab_heat_score`.
6. An extension that proves its usefulness SHOULD be submitted for promotion
   to a core field at the next MINOR release.

The unknown-preservation rule is the single most important compatibility
guarantee in the spec. It is what stops open-dj from becoming a lossy
lowest-common-denominator.

---

## 10. Open questions

These are deliberately unresolved at v0. Each will block a specific later
milestone (noted inline).

1. **Cue-point colour cross-vendor translation.** Nearest-colour in CIE Lab
   is the default, but palette-index-of-intent (e.g. "user meant green") is
   sometimes better. Blocks 0.5 (Serato adapter).
2. **Serato rating policy.** Drop, `x_serato_rating_column`, or fail-loud?
   Per-library config likely. Blocks 0.5.
3. **Are sessions/transitions core or a sibling spec?** Arguments for
   separating `open-dj-sessions/1.0` so the library format can hit 1.0
   independently. Decision at 0.6.
4. **Pairings transitivity and symmetry.** v0 says direction = `either` is
   the symmetric form; transitivity is not implied. Revisit at 0.4.
5. **Multiple named play-orders.** No vendor round-trips them. Drop-with-
   warning vs encode-behind-`x_*` comment. Per-adapter decision at 0.3.
6. **Beatgrid representation.** `origin + bpm` vs explicit `beats[]` vs
   both-always. Variable-tempo handling is the forcing function. Decision
   at 0.2.
7. **Energy normalisation.** MIK 1–10 vs 0.0–1.0. Current default is MIK
   (§3.1). Revisit if a second analyser becomes common.
8. **Waveform data.** Currently out of scope. Revisit if a vendor-neutral
   waveform format emerges.
9. **Fingerprint algorithm.** Chromaprint locked in at v0. Provenance-tagged
   alternative fingerprints may appear as `x_fingerprint_*`.
10. **Play-history privacy.** Timestamps of listening habits are GDPR-adjacent.
    Exporting a library for review should have a documented redaction mode.
    Blocks 0.7.
11. **Serato / Traktor adapter licence risk.** The *spec* is unencumbered.
    Adapters that link against reverse-engineered parsers may inherit
    licence constraints from those parsers.
12. **Content hash definition.** Current: `sha256` of full file bytes. A
    format-invariant hash (audio-stream only, skipping ID3/metadata) would
    survive tag edits without changing `content_hash`. Blocks 0.6.

---

## 11. Versioning

open-dj uses a modified SemVer suited to a spec:

- `schema_version: "0.1"` appears at every document root.
- During `0.x`, breaking changes are permitted at any MINOR bump. A CHANGELOG
  documents each.
- Unknown non-`x_*` fields: forward-compatible readers MUST ignore them at
  MINOR; MAJOR may redefine them. Readers MUST NOT fail validation on
  unknown non-`x_*` fields at the same MAJOR.
- `x_*` fields: ALWAYS preserved on round-trip (§9), across all versions.

**Acceptance criteria for 1.0 (freeze):**

1. Two independent round-trip implementations exist (different authors,
   different languages).
2. All four v0 vendor adapters (Rekordbox, djay Pro, Traktor, Serato) pass
   100% of a frozen conformance corpus (see §13 / Appendix B).
3. One public dog-fooded user — the `music-dj-tools` repository itself
   round-trips its real library without data loss.
4. A written deprecation policy covering removal of deprecated fields
   between MAJOR versions.

---

## 12. License and governance

**Reference code:** Apache-2.0. Patent grant matters for adapters that touch
reverse-engineered vendor formats.

**Spec text:** **CC-BY-4.0**. Deliberately *not* CC0. Attribution is how a
small spec gets adopted: every derived work or vendor-mode document must cite
open-dj, which compounds into a signal other vendors can point at when asking
internally "should we support this?". CC0 removes that signal for nothing in
return.

**Governance during 0.x:** single-maintainer BDFL model, bias to merge.
Contributions welcome; no CLA.

**Governance at 1.0:** migrate to a lightweight RFC process — numbered
proposals, 14-day comment window, BDFL or designated successors approve.

**Trademarks / vendor names:** "Rekordbox", "djay Pro", "Serato", "Traktor",
"OneLibrary", and "MIK" are the trademarks of their respective owners. Use in
this document is descriptive and nominative only.

---

## 13. Roadmap to 1.0

| Milestone | Scope | Gate |
|---|---|---|
| **0.1** | This document. Schemas inline. | Published. |
| **0.2** | Reference **Rekordbox** adapter (read/write), JSON Schema repo with `$id`. | 10-track seed corpus round-trips. |
| **0.3** | **djay Pro** adapter. Unblocked by M2 Phase 4 TSAF write. | 10-track corpus covers djay. |
| **0.4** | **Traktor** `collection.nml` adapter. | Named play-orders export strategy chosen (§10.5). |
| **0.5** | **Serato** adapter (GEOB + `database V2`). | Serato rating policy chosen (§10.2). Cue colour strategy chosen (§10.1). |
| **0.6** | Conformance suite ≥ 50 tracks. Sessions split decision (§10.3). | All four adapters green on full suite. |
| **0.7** | `open-dj-tool` CLI stable: `diff`, `merge-prep`, `validate`, `canon`. Play-history privacy mode (§10.10). | Non-trivial real-library round-trip published. |
| **0.8** | Public spec site; RFC process drafted. | Docs site live. |
| **0.9** | Second independent implementation (community or intentional fork). | Interop tested head-to-head. |
| **1.0** | Freeze. Deprecation policy published. | §11 criteria met. |

---

## Appendix A — Canonical example

A minimal but non-trivial library document that exercises every entity.

```json
{"schema_version":"0.1","kind":"library","pairings":[{"confidence":0.9,"direction":"into","from_track_id":"7d865e959b2466918c9863afca942d0fb89d7c9a","notes":"energy match","source":"manual","to_track_id":"a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"}],"playlists":[{"name":"Late Night","play_orders":[{"entries":[{"key_sync":true,"position":0,"target_tempo":128.0,"track_id":"7d865e959b2466918c9863afca942d0fb89d7c9a"},{"key_sync":true,"position":1,"target_tempo":130.0,"track_id":"a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"}],"name":"peak-time"}],"playlist_id":"pl_001","tracks_ordered":["7d865e959b2466918c9863afca942d0fb89d7c9a","a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"]}],"sessions":[{"events":[{"action":"load","deck":1,"timestamp":"2026-03-20T22:00:00Z","track_id":"7d865e959b2466918c9863afca942d0fb89d7c9a"},{"action":"play","deck":1,"timestamp":"2026-03-20T22:00:05Z"},{"action":"load","deck":2,"timestamp":"2026-03-20T22:07:10Z","track_id":"a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"}],"name":"Demo set","session_id":"s_001","started_at":"2026-03-20T22:00:00Z","transitions":[{"at":"2026-03-20T22:08:00Z","class":"blend","duration_ms":16000,"from_deck":1,"from_track_id":"7d865e959b2466918c9863afca942d0fb89d7c9a","to_deck":2,"to_track_id":"a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"}]}],"tracks":[{"artists":["Marlow Quay"],"bpm":{"modified_at":"2026-02-02T09:00:00Z","source":"rekordbox","value":128.0},"content_hash":"sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855","cue_points":[{"color":"#ff3366","name":"intro","position_ms":0,"slot":0,"type":"hot"},{"color":"#33ccff","name":"drop","position_ms":128000,"slot":1,"type":"hot"}],"duration_ms":634000,"file_path":"music/Marlow Quay/lanterns.flac","isrc":"GBCEN0900132","key":{"modified_at":"2026-02-02T09:02:00Z","source":"mik","value":"8A"},"rating":{"modified_at":"2026-02-10T20:01:00Z","source":"manual","value":5},"title":"Lanterns","track_id":"7d865e959b2466918c9863afca942d0fb89d7c9a"}]}
```

The document above is already in JCS canonical form: a single line, keys
sorted lexicographically, no insignificant whitespace. Its `sha256` is the
`open-dj-hash` referenced in §8.

---

## Appendix B — Conformance corpus bootstrap list

The v0.2 conformance corpus seeds with ~15 tracks chosen to hit the edge
cases adapters tend to regress on. Each track is described by the *properties
it tests*, not by title (actual audio is sourced per-implementor under licence).

1. **Classic ISRC, ASCII metadata** — baseline.
2. **Unicode title + artist (CJK)** — UTF-8 normalisation, NFC vs NFD.
3. **Multi-artist** (3+ artists, with `feat.`) — `artists[]` vs joined string.
4. **Missing ISRC** — forces fingerprint-based `track_id`.
5. **Re-encoded same content** (FLAC + 320 MP3) — same `track_id`, different
   `content_hash`. Critical.
6. **Remix / edit with shared ISRC-stem** — distinct `track_id` despite
   similar fingerprint.
7. **Variable-tempo track** — forces explicit `beats[]`.
8. **Track with 8 hot cues + 4 memory cues + 2 loops** — vocabulary coverage.
9. **Streaming-only track (no audio file)** — `file_path` + `content_hash`
   policy edge case.
10. **Track with all four vendors analysing BPM differently** — provenance
    stress test.
11. **Track with no rating on Serato, 5★ elsewhere** — ratings lossy-lane.
12. **Track with custom cue colour that has no Traktor equivalent** —
    colour translation.
13. **Playlist with two named play-orders** — PlayOrder export strategy.
14. **Session with rapid cue juggles (Flip-style)** — events granularity.
15. **Track with a 3-hop pairing graph (A→B→C, A↔C)** — pairing non-
    transitivity.

---

*End of open-dj v0 strawman. Feedback, PRs, and sharp disagreement welcome.*
