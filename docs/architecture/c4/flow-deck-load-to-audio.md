# Dynamic views: deck load to audible output, and the library read path

*Describes commit `9f36f1b426`, Tue 27 Oct 2026. Source flows:
`(private path)` (mapped Sat 15 Aug 2026), "Data flow: deck load through to
audible output" and "Data flow: library listing", checked against the code named below. Frontend
paths are relative to `apps/webui/frontend/src/`.*

Both sequences describe the default path: Web Audio in the page. The opt-in Rust engine mode is
noted after the first diagram.

## Deck load through to audible output

```mermaid
sequenceDiagram
    participant DJ as DJ at the rig
    participant UI as Deck and BrowserPanel - lib/components/rb
    participant IPC as Dispatcher - lib/rb/performance-ipc.svelte.ts
    participant ENG as Web Audio engine - lib/rb/audio-engine.svelte.ts
    participant DEC as Fetch and decode - lib/rb/audio-prefetch-cache.svelte.ts
    participant API as Daemon routes - apps/webui/server/routes/rb_assets.py and rb_assets_audio.py
    participant RBV as Rekordbox asset path - apps/webui/server/rb_vendor.py
    participant DB as Files - (private path), ANLZ files, (private path)
    participant WK as Stretch worklet - lib/rb/stretch-adapter.ts
    participant DEV as Audio output device

    DJ->>UI: double-click or drag a row onto a deck
    UI->>IPC: load command via runPerformanceCommandFromUi
    IPC->>ENG: scheduled load on the deck scope
    ENG->>API: GET api/v1/tracks/stable_id/anlz over HTTP
    API->>RBV: resolve stable_id to vendor ids
    RBV->>DB: djmdContent and djmdCue rows over SQLite, ANLZ PWV6 and PQTZ over files, cache read or fill
    DB-->>RBV: metadata, cues, tri-band waveform, beat grid
    RBV-->>API: anlz payload
    API-->>ENG: JSON anlz
    ENG->>DEC: fetch audio for stable_id
    DEC->>API: GET api/v1/tracks/stable_id/audio over HTTP, range streaming
    API->>RBV: resolve_playable_audio
    RBV-->>API: file path on disk, or a missing-file error
    API-->>DEC: audio bytes, or 404 with a code such as AUDIO_FILE_MISSING
    DEC->>DEC: decodeAudioData into an AudioBuffer
    DEC-->>ENG: AudioBuffer, its duration is transport truth
    ENG->>WK: build source, TRIM, EQ, fader, crossfader, master chain through the stretch processor
    ENG-->>UI: DeckState title, bpm, key, duration, grid published to stores
    DJ->>UI: press PLAY
    UI->>IPC: play command
    IPC->>ENG: play with revision n
    ENG->>WK: schedule at a future context time
    WK-->>ENG: acknowledge revision n
    WK->>DEV: PCM over Web Audio destination
    ENG->>ENG: each frame read getOutputTimestamp, select the newest acked schedule output has crossed
    ENG-->>UI: position_ms, audible true, transport_pending false
```

### Legend

| Participant | C4 element |
|:------------|:-----------|
| DJ at the rig | Person |
| Deck and BrowserPanel, Dispatcher, Web Audio engine, Fetch and decode, Stretch worklet | Components of the web UI container |
| Daemon routes, Rekordbox asset path | Components of the engine daemon container |
| Files | Data store containers |
| Audio output device | External system |

### What the code confirms

- `audio-engine.svelte.ts` states the contract in its header: `load()` fetches audio and required
  analysis via `api-rb`, then `decodeAudioData`, then the per-deck chain; a backend 404 such as
  `AUDIO_FILE_MISSING` raises a toast, sets `deckLoadErrors` and rejects, never a silent fallback;
  a failed analysis fetch rejects the load and publishes no unusable deck.
- `rb_assets.py` serves `GET /{stable_id}/anlz`; `rb_assets_audio.py` serves `GET` and `HEAD` for
  `/{stable_id}/audio` and picks the file with `rb_vendor.resolve_playable_audio`.
- `audio-prefetch-cache.svelte.ts` calls `ctx.decodeAudioData` and copies prefetched bytes so the
  cache is not detached.
- Cues come from `djmdCue`, never ANLZ PCOB or PCO2, which are empty in rekordbox 6 and 7.
- Position comes from `getOutputTimestamp()` plus acknowledged revisions, never
  `AudioContext.currentTime` (`docs/architecture/audio-timing.md`).
- For a stemmed deck the single source is replaced by `AlignedStemDeckProcessor` in
  `lib/rb/stem-graph.ts`, one stretch processor and gain per part, validated before connecting
  and scheduled with `Promise.allSettled` so a partial failure disconnects the whole group.

### Not confirmed for this diagram

- **Order of the two fetches.** The diagram shows anlz then audio, following the planning doc. The
  engine may issue them concurrently or reuse a prefetched entry (`audio-engine.svelte.ts` reuses a
  ready anlz cache entry); the exact ordering at this commit was not traced.
- **Range streaming** is named in the planning doc; whether the default load requests a range or
  the whole file was not checked.

### Rust engine mode, opt-in

With `?engine=rust`, `performance-ipc.svelte.ts` hands audio commands to `executeInRustEngine`
first. The page still fetches the same anlz for the waveform, but the load goes through
`POST /api/v1/audio-engine/load` (`apps/engine_core/audio_engine_load.py`), which resolves the same
file and beatgrid and sends a protocol v1 `load` to `odj-audio`. The engine decodes with symphonia,
renders through cpal, and reports `position_ms` and `rate` about 30 times a second over its
loopback WebSocket; the page extrapolates between reports and never runs ahead of one
(`lib/audio-engine/client.ts`, `predictPositionMs`). See
[`c4-3-components-audio-engine.md`](c4-3-components-audio-engine.md).

## Library read path

```mermaid
sequenceDiagram
    participant DJ as DJ at the rig
    participant BP as BrowserPanel and TrackTable - lib/components/rb/browser
    participant API as HTTP client - lib/rb/api-rb.ts
    participant PL as Playlists router - apps/webui/server/routes/playlists.py
    participant BE as State backend - apps/webui/server/sqlite_backend.py
    participant RBV as Row builder - apps/webui/server/rb_vendor.py
    participant SDB as state.db - (private path)

    DJ->>BP: select a playlist
    BP->>API: listPlaylistTracksPage with limit and offset
    API->>PL: GET api/v1/playlists/playlist_id/tracks over HTTP
    PL->>BE: get_playlist_page for the requested window only
    BE->>SDB: membership and track_fields over SQLite
    SDB-->>BE: rows with value, source, observed_at, confidence
    BE-->>PL: page and total
    PL->>BE: get_tracks_bulk for the slice
    PL->>RBV: build_track_rows with vocals, preview waveform, availability
    RBV-->>PL: hydrated rows
    PL-->>API: PlaylistTracksPage with ETag header and next_offset
    API-->>BP: hydrated rows
    BP-->>DJ: rows, missing files grayed and never dropped
    BP->>API: next page while next_offset is set
```

### Legend

| Participant | C4 element |
|:------------|:-----------|
| DJ at the rig | Person |
| BrowserPanel and TrackTable, HTTP client | Components of the web UI container |
| Playlists router, State backend, Row builder | Components of the engine daemon container |
| state.db | Data store container |

### What the code confirms, and what changed since the planning doc

- The planning doc describes `GET /api/v1/playlists/{id}` hydrating every row in one call. That
  route still exists (`getPlaylistHydrated` in `lib/rb/api-rb.ts`), but the paged route is now the
  first-paint path: `list_playlist_tracks` in `routes/playlists.py` reads only the requested
  window (LIBM-133), returns an `ETag`, and is called by `listPlaylistTracksPage` from
  `lib/components/rb/browser/fill-playlist-pane.ts`.
- A playlist member with no track row is a `500 PLAYLIST_MEMBER_MISSING`, not a skipped row.
- Rows for files that are not on disk are annotated, not filtered, so an agent's counts match what
  a human scrolls past (`docs/architecture.md`, the 74 percent dead paths note).

### Not confirmed for this diagram

- Whether every browser pane uses the paged route, or some still call the full hydrated detail,
  was not audited across `lib/components/rb/browser/`.
- The `?available=` filter described in the planning doc is on `GET /playlists/{id}`
  (`routes/playlists.py`); whether the paged route honors it was not checked, so it is not drawn.
