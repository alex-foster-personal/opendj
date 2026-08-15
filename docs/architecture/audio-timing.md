# Audio timing and alignment

*Deep-dive. Overview: [`../architecture.md`](../architecture.md). Terms:
[`../glossary.md`](../glossary.md).*

Sources: `apps/webui/frontend/src/lib/rb/audio-engine.svelte.ts`, `beat-sync-math.ts`,
`stem-graph.ts`, `stretch-adapter.ts`.

This is the hardest engineering in the project. Everything here exists to keep one promise:
**the interface must never claim something happened before the listener heard it.** That is
the rule of the build applied to time.

## Two clocks, and why

| Clock | Answers | Use |
|:---------------------------------|:-----------------------------------|:-------------|
| `AudioContext.currentTime` | "when can I safely schedule the next command?" | planning only |
| `context.getOutputTimestamp()` | "what has the listener actually heard?" | transport truth |

`currentTime` runs **ahead** of what is audible by construction: commands are scheduled into
the future (`SYNC_SCHEDULE_SAFETY_S = 0.1`) plus worklet latency. So reading `position_ms`
from it would report the scheduling clock, and the UI would show a cue, loop or tempo change
as done while it is still in flight. The code states the boundary directly:

> Render/control time may be ahead of the listener and must never leak here.

Public `position_ms`, `audible` and `transport_pending` therefore come from
`getOutputTimestamp()` plus acknowledged schedule revisions. `performanceTime` is explicitly
diagnostic-only and "never transport authority".

## The revision protocol

Bridging the two clocks is the reason `transport_pending` exists at all.

1. Every mutation (play, pause, seek, tempo, key, loop) takes `revision = ++nextScheduleRevision`.
2. It enters `PresentedTransportTimeline.schedules[]` **only after the Signalsmith worklet
   acknowledges it**. An unacknowledged command is pending, not presented.
3. Each animation frame, `observePresentedTransportTimeline` reads the real
   `getOutputTimestamp()` and selects the **highest-revision schedule whose
   `startContextTime <= contextTime`**, i.e. the newest one output has actually crossed.
   That becomes the published position.

Three guards make it robust against hostile clock behaviour, each earned:

- **Presented state cannot rewind.** If a selected schedule's revision is lower than
  `timeline.presented_revision`, it throws. A late or out-of-order timestamp can never walk
  the transport backwards.
- **`contextTime === 0` means "no output yet"**, even when `performanceTime` is positive,
  because Chromium exposes a running performance clock while the output frame is still zero
  during device warm-up.
- **A regressing `contextTime` is rejected**, not absorbed.

A newer command landing before an older one is presented marks the older
`supersededByRevision`. Already-presented schedules are protected.

## Beat Sync: phase-lock, not tempo-match

Matching BPM alone gets two decks running at the same speed and out of step. Phase-lock
matches *where in the bar* each deck is.

The master's fractional progress through its current beat:

```
beatPhase = (projectedMasterPositionSec - masterBeat.t) / masterBeatIntervalSec
```

`_bestFollowerAnchor` then scans the follower's beats; for each candidate it computes the
position at the *same fractional offset* into the equivalent follower interval, and keeps
whichever lands closest to the follower's current position. That target position and the
tempo ratio `(masterBpm * masterTempoRatio) / followerBpm` are scheduled as **one command**,
so both decks' beat boundaries coincide at `syncAtContextTimeSec`.

**BAR versus BEAT** is two extra constraints, nothing more:

| Mode | Constraints |
|:---------|:--------------------------------------------------------------|
| `BAR` (default) | candidate must share the master's beat number (`beat.n`), and any candidate needing 0.5x/2x normalisation is rejected |
| `BEAT` | both constraints dropped |

That is the literal mechanism preserving PQTZ 1->1 through 4->4.

### Tempo comes from intervals, never the PQTZ `bpm` field

A documented trap, from the file header:

> Tempo ratios use beat INTERVALS (60/dt), never the PQTZ bpm field alone - that field can
> disagree with .t (Proper Education: field 124.72 vs dt->125).

Aggregation is two-stage (median then mean) specifically to avoid millisecond-quantisation
bias in the grid.

### When BAR legitimately cannot work

If no anchor satisfies both the bar-number match and an in-range tempo, it throws with a
remediation message naming the fix (select BEAT, or widen the follower tempo range). Two
deliberate softenings sit on top:

- **Per-follower isolation.** Each follower is planned in its own try/catch, because "one
  unsyncable deck must not abort BeatSyncMax for the rest". A failed follower gets
  `sync_error` and is excluded; only if *every* follower fails does the call throw.
- **AutoPlay preflights.** Before playing the next track it runs `computeFollowerSyncPlan`,
  and if it would fail, disables follower Beat Sync and raises a toast rather than failing the
  handoff, playing free-tempo instead.

That second one is the single place the strict fail-fast default is relaxed for UX. It is
still surfaced, never masked, and it is worth knowing it is there.

## Master Tempo is implemented as an inversion

The non-obvious bit, and pleasing once seen:

```
composeStretchSemitones = masterTempoSemitones(tempoRatio, enabled) + keyShiftSemitones
masterTempoSemitones(ratio, enabled) = enabled ? 0 : 12 * log2(ratio)
```

Master Tempo **on** contributes zero semitones: the worklet changes speed and holds pitch.
Master Tempo **off** injects `12 * log2(ratio)`, deliberately *adding back* the pitch shift a
naive `playbackRate` change would have produced.

So keylock is the natural behaviour of the DSP and "no keylock" is the special case
synthesised on top. There is no algorithm switch. Manual key shift adds on top additively and
"never by changing transport rate".

`setPitchRange` throws if the current pitch already exceeds the requested range, rather than
clamping. Clamping would be an unannounced tempo jump on a possibly-playing deck; the caller
must reset pitch toward 1.0 first. Explicit, never a silent clamp.

## Stem alignment

`validateStemBufferAlignment` runs before any processor is connected. It takes `vocals` as
reference and requires every other part to match sample rate, frame count and channel count
**exactly**, and duration to within one sample (`1/sampleRate`).

The failure being prevented: stems are separately decoded files. A drift of even one sample,
from a different encode, `decodeAudioData` rounding, or a stale cache entry, makes the parts
phase against each other once summed, producing comb-filtering rather than a clean
reconstruction. Validating metadata before connecting means a bad stem set never becomes
audible.

Scheduling uses `Promise.allSettled` plus group-disconnect rather than per-part scheduling,
because the parts are four independent worklets that must honour the same `outputTime`.
Fire-and-forget risks a partial commit (drums accepted, vocals rejected), leaving stems
desynced mid-playback with no way to detect it. Instead every branch is awaited, failures
aggregate into an `AggregateError` naming each one, and any failure disconnects all four
together.

## Quantize, and failing loudly

Q snaps to `quantizeToNearestBeat`: the `.t` of the nearest real PQTZ beat, ties resolving to
the earlier beat. Applied at every timing entry point, not just seeks: `pause()` snaps the
stored cue, `cueJump` and `quantizedSeek` snap the target, `pressCue` snaps a freshly-set cue,
`setLoop` and `engageBeatLoop` snap both endpoints.

Every one of those routes through `_requireBeatGrid`, which runs `validateBeatGrid`:
at least 2 beats, `n` cycling 1->2->3->4, strictly increasing `.t`, finite positive bpm.
On any failure it throws a wrapped error naming the deck and operation, with the original as
`cause`.

There is no fallback grid and no silent no-op. A Quantize-enabled action on a track with a
bad grid throws synchronously up through the IPC dispatcher.

## Drift: a deliberate non-choice

**There is no continuous drift-correction loop.** This is a design decision, not an
oversight, and the code contrasts itself with Mixxx explicitly:

> Unlike Mixxx's open-ended per-tick phase-error nudge, the target here is already known...
> so this lands on it exactly at a fixed, bounded duration instead of an unbounded correction
> loop.

Beat Sync computes one target position and tempo, then ramps to it over
`REANCHOR_RAMP_DURATION_SEC = 0.25`. After that ramp, nothing keeps master and follower
locked.

The consequence is documented in the same file: grid-noise bias produced a ~0.18 BPM residual
error, which is **one beat of drift every ~5.5 minutes**. Nothing re-corrects it until the
user re-triggers sync or seeks.

Whether that is acceptable is a real product question. A bounded, predictable correction is
easier to reason about and cannot oscillate; an unbounded loop holds lock indefinitely but
can hunt. The current choice favours predictability, which is consistent with the rest of the
build, but it means long blends will walk.

There is also **no system output-latency compensation**. The worklet's own reported
`latencySec()` is folded into scheduling safety margins, but that covers only that deck's
processing latency, not cross-deck or hardware output drift.

## Decoded duration is transport truth

The decoded `AudioBuffer.duration` is canonical; tag or backend metadata duration is never
consulted for transport maths:

> The decoded buffer is the audio actually scheduled. Metadata can differ, so it must not
> define waveform bounds or transport truth.

## Failure modes

| Failure | Surfaced as |
|:-----------------------------|:-------------------------------------------------|
| Worklet timeout or crash | Poisons the command gate; all later commands on that processor reject. Deck runtime torn down, `processor_error` set, toast raised |
| Sample-rate change mid-session | `validateStretchScheduleBounds` throws before the worklet is asked to act |
| Stem misalignment | `AggregateError` naming every failed part, pre-connection; group disconnect |
| Missing or invalid beatgrid | `_requireBeatGrid` throws with the original error as `cause` |
| BAR phase-lock impossible | Throws per-follower with remediation text; isolated so other decks still sync. AutoPlay softens to a toast |
| Stale or superseded schedule | Structurally guarded; presented revision cannot regress |
| Missing audio file, streaming URI | Rejected at `api-rb.ts`, `deckLoadErrors[deck]` set and toasted, promise rejects |
| Load/state divergence | `assertDeckLoadConsistency` throws if `stable_id` and processor state disagree |

The pattern throughout: validate before connecting, throw with a named cause, isolate
per-deck failures, and never degrade silently. The one deliberate exception is AutoPlay's
Beat Sync softening, and it still raises a toast.
