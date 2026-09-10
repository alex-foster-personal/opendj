# Glossary

Terms used across this repo that a newcomer cannot reasonably google. Rekordbox internals in
particular are undocumented by Pioneer; most of what follows was established by reading bytes
and correlating against known-good audio, and several entries record a wrong first guess that
cost real time.

Cross-references in **bold**.

## Rekordbox file and database internals

**ANLZ**
Rekordbox's per-track analysis files, written alongside the audio (`.DAT`, `.EXT`, `.2EX`).
They hold the beat grid, waveform previews, and vocal-intensity data. This project reads them
with `pyrekordbox` and caches parsed results in `data/state/anlz-cache/`. ANLZ is the source
of truth for anything grid- or waveform-shaped; the database is not.

**PQTZ**
The beat-grid section inside an **ANLZ** file. Each row carries a beat number (1 to 4, where 1
is the downbeat) and a timestamp. Everything in the rig that snaps to a beat (**Quantize**,
**Beat Sync**, loops, cue seeks) resolves against real PQTZ rows. The build never interpolates
or invents grid points; a track with no PQTZ fails loudly rather than getting a synthetic grid.

**PVDI**
The vocal-intensity section inside a `.2EX` file, driving the blue vocal bars in the browser.
Written by **rekordbox 7+ analysis only** (Apr 2024 onward), so its presence tracks *analyzer
version, not vocal content*: a track can be full of vocals and have no PVDI simply because it
was analysed by an older rekordbox. Tracks without PVDI render as an honest
`not_analyzed` state, and can optionally be filled by
**demucs**/**RoFormer** separation into the **vocal-cache**. Always probe the file, never a
database flag.

**PWV6 / PWV7 / PWV4 / PWAV**
Waveform preview sections inside **ANLZ**, in descending order of fidelity. PWV6/PWV7 are
tri-band (used for the coloured waveform); PWV4 and PWAV are mono fallbacks. The fallback
chain is PWV6 -> PWV4 -> PWAV -> null, and null renders as an explicit dash, never a fake
waveform.

Two traps, both established the hard way:
- The 3-byte band order is **byte0 = low, byte1 = mid, byte2 = high**, scale 0..127. An
  earlier mid/high/low reading was wrong and was disproven by correlating against ffmpeg band
  envelopes.
- Normalise by the per-track `preview_max`, **never by 127**. Real values top out near 87, so
  dividing by 127 renders everything too dark.

`pyrekordbox`'s PWV6 `tag.get()` raises `KeyError: 0`, so `rb_vendor.py` walks the raw PMAI
sections itself.

**PMAI**
The container/section header format inside **ANLZ** files. Relevant because the raw section
walker in `rb_vendor.py` parses it directly.

**djmdContent**
The Rekordbox database table holding one row per track: file path (`FolderPath`), title,
artist, BPM, key, and so on. `FolderPath` is the field that goes stale when files move, which
is what `apps/reconcile` repairs.

**djmdCue**
The Rekordbox table holding hot cues and loops. **Cues come from here, not from ANLZ**: the
ANLZ `PCOB`/`PCO2` cue sections are empty in rekordbox 6 and 7. Getting this backwards is a
day lost. Cue "Kind" values 1 to 8 map to hot-cue slots A to H; Kinds 9 to 11 are not yet
verified and are excluded from both read and write paths.

**master.db / master.plain.db**
Rekordbox's SQLite database is encrypted (SQLCipher). `data/master.plain.db` is a **static
decrypted working copy**. Refreshing it means re-decrypting and then
`rm -rf data/state/anlz-cache/`, because the cache embeds djmdCue-derived cues.

Critical: rekordbox's recent writes live in **`master.db-wal`**, so copying `master.db` alone
silently misses them and the copy looks merely out of date rather than broken. Copy db + wal +
shm together, then `sqlcipher_export`.

**Cloud Library Sync**
A Pioneer feature that syncs a library across devices. This project holds no state for it,
which is why the reference screenshot shows a cloud icon on every row while this build shows
one only for genuinely streaming rows.

## This project's own concepts

**stable_id**
The primary key for a track across every vendor, computed by a three-tier algorithm:
ISRC, then chromaprint acoustic fingerprint, then path+size+duration. Three tiers because each
alone is insufficient: ISRC is authoritative but often absent, fingerprinting is robust to
re-encoding but costly, and path-based identity breaks the moment a file moves.

**Six-rail safety pattern**
The mandatory shape of every destructive write to an operator's files: (1) typed confirmation,
(2) running-app check via `pgrep`, (3) timestamped backup, (4) dry-run default requiring
`--live` **and** `--i-understand-the-risks`, (5) atomic write via temp file plus `os.rename`,
(6) post-write readback plus a generated reversal script. Reference implementation
`apps/sync/safety.py`.

**open-dj**
The vendor-neutral, provenance-aware metadata format this project defines, specified in
`open-dj/` under CC-BY-4.0 separately from the Apache-2.0 code, with a JSON Schema, a
conformance corpus, and per-vendor adapter mappings. Reference implementation at
`apps/open_dj/`. It is the concrete anti-lock-in play, not merely an internal serialisation
format. See `docs/architecture/open-dj.md`.

**EAV**
Entity-Attribute-Value. Track fields are stored as rows in `track_fields`
(stable_id, field, value, source, observed_at, confidence) rather than as columns, so a new
field needs no migration and every value carries its **provenance**.

**Provenance**
The record of where a value came from and how much to trust it: source, observed_at,
confidence. It is what lets the UI tell the truth about a number rather than presenting every
value as equally authoritative.

**Derivative, not authoritative**
The design rule that `state.db` can be wiped and rebuilt from vendor databases at any time,
and that nothing in it is the last copy of anything. This is what makes aggressive tooling
safe. (Whether it still holds for user-originated edits is tracked as an open question.)

**Event bus**
The in-process queue in `apps/shared/state/events.py`, with a durable tail in the `events`
table written inside the same transaction as the state change.
Consumers that miss a live event can rebuild from a cursor.

**Warm / wired / cold**
This project's tiering for how far a module has travelled toward the performance rig. Warm:
serving real data today. Wired: endpoint exists and the UI calls it, but no data has been
created yet. Cold: built and tested, not yet surfaced. Cold does not mean dead.

## Audio, DSP and separation

**Stem separation**
Splitting a finished mix back into constituent parts. Two layouts exist here and the
distinction is load-bearing:

- **demucs4** - four parts (vocals, drums, bass, other), from Meta's Demucs.
- **roformer2** - two parts (vocals, instrumental), from a Mel-Band RoFormer model. The
  instrumental part *already contains drums*, so a DRUMS control on a roformer2 deck would be
  a no-op and is therefore rendered inert rather than shipped as a dead button.

A manifest that does not declare its `layout` is rejected at the API edge; there is no default.

**Stem bundle / manifest**
A directory per track under `data/state/stems/<stable_id>/` containing the separated audio
plus a `manifest.json` declaring `schema_version`, `model`, `layout` and file list. Three
schema versions exist on disk (v1, v2, v3).

**demucs**
The stem-separation model used for the original four-part bundles. Runs only in standalone
PEP 723 scripts under `uv run`; torch never enters the repo venv.

**RoFormer / Mel-Band RoFormer**
A newer separation architecture with materially better vocal separation than demucs
(published MUSDB vocal SDR roughly 1.5 to 2.5 dB higher). Note the name: Mel-Band RoFormer is
not the same as BS-RoFormer, though they share authors.

**SDR**
Signal-to-Distortion Ratio, in dB. The standard objective measure of separation quality.
Used to compare separation quality; a benchmark result needs its scorer and input
provenance before it can establish an improvement.

**MUSDB**
The standard public benchmark dataset for separation. Caveat recorded in this repo: it is
live-band material and does not represent an electronic catalogue, so MUSDB scores are a
starting signal, not a verdict. Verify by ear on real library material.

**IoU**
Intersection over Union. Used here to compare a derived vocal-region mask against **PVDI**
ground truth.

**vocal-cache**
`data/state/vocal-cache/<stable_id>.json`, holding derived vocal regions for tracks lacking
**PVDI**. Merged into the `/anlz` response and listing rows so the UI need not care whether
bars came from rekordbox or from separation.

**Quantize (Q)**
Snapping cue, seek and loop operations to the analysed **PQTZ** beat grid. A missing or
invalid grid fails loudly rather than falling back to a synthetic one.

**Beat Sync, BAR vs BEAT**
Matching one deck's tempo and phase to another. **BAR** (the default) PREFERS an exact lock
preserving PQTZ beat numbers 1->1 through 4->4, so bar structure is maintained where it can be.
Where only a 0.5x/2x fold fits the pitch range, BAR folds and warns in orange rather than
refusing the lock - the warning is what says the bar count no longer holds.
**BEAT** is an explicit opt-out permitting 0.5x and 2x half/double normalisation silently.
Beat Sync Max ON forces BAR and requires downbeats (PQTZ n=1, beat 1 of the bar phase) to stay
aligned over the mix.

**Master Tempo (MT)**
Key-preserving tempo change, i.e. time-stretch without pitch shift, via a pinned
`signalsmith-stretch` 1.3.2 AudioWorklet. Worklet timeouts and processor errors are terminal,
never silently degraded.

**Camelot / key notation**
The harmonic-mixing wheel notation (`7A`, `12B`) that makes compatible keys adjacent numbers.
Used by the next-track suggester.

**Hot cue**
A saved jump point in a track, slots A to H in the rig, read from **djmdCue**.

**Output presentation clock**
The clock that public `position_ms`, `audible` and `transport_pending` must be derived from,
together with acknowledged schedule revisions. `AudioContext.currentTime` is only the control
and render *planning* clock. Reading position from `currentTime` makes the waveform lie about
where the music is. See `docs/architecture/audio-timing.md`.

**Transport-audible vs heard**
Two different questions that the single word "audible" has been used for, and confusing them
corrupts a recorded set. `DeckState.audible` answers the FIRST: the output presentation clock
says this deck's schedule is being presented. It knows nothing about the mixer, which is a
separate gain stage downstream, so a deck pre-cued in the headphones with its channel fader
down, or with the crossfader parked on the other bus, is transport-audible while the room
hears silence. Anything asking "did the audience hear this" must also require non-zero gain
through the master path -- trim, channel fader, crossfader, master -- which is what
`deckWasHeard()` in `apps/webui/frontend/src/lib/sets/deck-audibility.ts` does before
reporting a deck to the set recorder. Shipped forwarding the raw flag in PR #650 and fixed
after review; the symptom was `track_loaded` rows for tracks nobody played.

Four things silence a deck the transport calls audible, and the last two keep being read
backwards. They are: the mixer chain (trim, fader, crossfader, master); the final master-mute
node; every stem part gained to zero; and `?extroute=`.

The stem test is not "is every control muted", because SOLO silences every part it did not
select: a deck with VOCAL soloed AND muted plays nothing while two of its three controls read
unmuted. So it calls the engine's own `stemPartGains` rather than re-deriving the rule.

External routing REPLACES the internal master rather than adding to it. `_ensureGraph` runs
`_masterGain.connect(_masterMuteGain)` only in the `routing === null` branch, so under a
partial map an UNMAPPED deck reaches the headphone monitor and no speaker, which is the
opposite of the mapped case.

EQ is deliberately still not gated (`.planning/debt/650.md`): it is dB shelving, so
"cut enough to be silent" is a real threshold, and a wrong one deletes a play with no record
that anything was removed.

**Observation clock floor**
The rule that a deck observation's `observed_at` may not go backwards, enforced on both sides
of `POST /api/sets/deck-observations`. It is scoped to the RECORDING SESSION, not to the
process: `apps/sets/record.py` builds a fresh `OpenDjDeckSource` per recording, so the
server's `_last_submitted_at` starts null for each one, while persisting across requests
within one. A client that carries its floor across a session boundary is therefore stricter
than the server, and a wall clock that regresses between two sets makes it refuse an entire
set the server would have taken.

The general trap, which bit twice in one review round: any staleness gate keyed on a wall
clock that can move backwards is wrong in the same way, so finding one is a reason to grep for
the others rather than to fix the reported one. The second instance here was
`lastRecorderPollMs`, where a negative elapsed read as "polled very recently" and the emitter
sat out a whole set without asking who was recording.

## Process and tooling

**Fan-out**
Building two or more features in parallel across separate git worktrees and branches, by any
mix of agents. Governed by `.planning/FANOUT-CONVENTIONS.md`. One feature on one branch is not
a fan-out.

**Lane**
A file or router with a single owner during a fan-out. Others build against its contract
rather than editing it, to avoid merge collisions.

**Ledger / progress-tree**
`data/progress-tree.yaml`, the canonical record of what is being built and by whom. Claiming a
node before starting work is the locking mechanism during a fan-out. Viewable at
`/progress-tree`.

**Agent-native parity**
The project-wide rule that every UI interaction must have a matching CLI, IPC or HTTP endpoint
so agents can drive the same flow. In the rig this is `performance-ipc.svelte.ts`; a control
wired straight to the engine, bypassing the dispatcher, is a defect even if it works, because
it is invisible to automation.

**PEP 723**
The Python standard for inline script dependency metadata, letting a single-file script
declare its own dependencies and run under `uv run` with no shared environment. This is how
torch and demucs stay out of the repo venv.
