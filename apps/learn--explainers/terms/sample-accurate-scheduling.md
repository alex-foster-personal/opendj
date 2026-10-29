---
term: Sample-accurate scheduling
slug: sample-accurate-scheduling
aliases: [scheduled parameter change, setValueAtTime, timed automation]
group: Time, sync and loops
level: 3
one_liner: "Telling the audio engine to change a gain at an exact sample, rather than as soon as it can, so a cut lands precisely on the beat."
why_it_matters_here: "Cuts, crossfades and sync seeks only sound tight when the change is booked at an audio time, not applied when a message arrives."
related: [audio-clock-vs-page-tick, gain-ramp, equal-power-crossfade, audio-buffer-and-latency, loop-seam]
sources:
  - "Web Audio API, W3C Recommendation (AudioParam automation: setValueAtTime, linearRampToValueAtTime; render quantum of 128 frames), https://www.w3.org/TR/webaudio/"
  - "Wilson, A Tale of Two Clocks: Scheduling Web Audio with Precision, HTML5 Rocks / web.dev, 2013"
status: draft
---

## In plain words

Audio is made in small blocks, 128 samples at a time in a browser (about 2.7 ms at 48 kHz).
If you tell the engine "mute this now", it can only act at the start of whichever block it is
working on when your message arrives, and you do not control which block that is. If instead you
say "mute this at sample 48,050", the engine applies it exactly there, even in the middle of a
block. That is sample-accurate scheduling: you book the change at an audio time in advance.

A hard switch at an exact sample can still click (see loop seam), so the usual move is to book a
short ramp: start at sample N, reach zero by sample N plus a few milliseconds. Booked ramps are
how cuts, kills and crossfades stay both tight and clean.

## Picture

```text
render blocks of 128 samples (2.67 ms at 48 kHz, 8 samples per char)
           block 374       block 375       block 376
       ...|...............|...............|...............|...
                                ^ sample 48,050: offset 50 into 375

gain, "set it now" (applied when the next block starts after the
message lands; could be 376, could be 377)
          ================================|_______________________

gain, setValueAtTime(0, 48050 / 48000)
          ======================|_________________________________

gain, linear ramp from 48,050 to 48,290 (5 ms, no click)
          ======================##########**********..........____
```

## In Open DJ

The page engine books changes with Web Audio's `AudioParam` automation. The backward sync seek
blend in `apps/webui/frontend/src/lib/rb/follower-backward-blend.ts` sets a gain at a start time
and ramps it to the end time with `setValueAtTime` and `linearRampToValueAtTime`. Knob moves are
different: they use `setTargetAtTime` smoothing from the current time in
`apps/webui/frontend/src/lib/rb/audio-engine.svelte.ts`, because a knob is "now", not a beat.
The Rust engine renders in the same 128-frame quanta (`apps/audio-engine/src/stretch.rs`) and
glides parameters linearly over a fixed number of frames (`apps/audio-engine/src/dsp.rs`).
