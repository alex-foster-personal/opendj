---
term: Gain ramp
slug: gain-ramp
aliases: [parameter smoothing, de-zippering, anti-zipper, ramp, glide]
group: Loudness and gain
level: 2
one_liner: A gain ramp glides a volume change over a few milliseconds instead of jumping, which stops the clicks and buzz called zipper noise.
why_it_matters_here: Every fader, trim, EQ and crossfader move in Open DJ is smoothed over about 10 ms, so fast hands do not produce clicks.
related: [audio-buffer-and-latency, equal-power-crossfade, trim-and-loudness-matching, isolator-eq, biquad]
sources:
  - W3C Web Audio API specification, AudioParam automation (setTargetAtTime, linearRampToValueAtTime)
  - Udo Zolzer (ed.), DAFX: Digital Audio Effects, 2nd ed., Wiley, 2011
status: draft
---

## In plain words

Audio is processed in blocks, called buffers. If a fader move is applied as a new number at the
start of each buffer, the gain jumps in steps. Each step is a tiny discontinuity in the wave,
heard as a click. Drag a fader and you get a rapid series of steps, one per buffer, which sounds
like a zipper or a buzz.

The cure is to never jump. When a gain changes, the engine slides from the old value to the new
one across many samples, per sample rather than per buffer. A linear ramp moves in a straight
line over a fixed time; a one-pole smoother eases in exponentially, covering about 63 percent of
the distance per time constant. A 10 ms ramp is short enough to feel instant and long enough to
be inaudible. "Ramp over one buffer" means the glide lasts exactly one block: at 1,024 frames
and 48 kHz that is about 21 ms.

## Picture

```text
 STEPPED: new value per buffer           RAMPED: new value per sample

 gain                                    gain
 1.0 |              +-----               1.0 |                ....-----
     |              |                        |            ...
 0.7 |         +----+   <- click             |        ...
     |         |                             |     ..
 0.4 |    +----+        <- click             |   .
     |    |                                  |  .
 0.1 |----+             <- click         0.1 |..
     +----|----|----|----> time              +----|----|----|----> time
         buffer edges                             same buffer edges
     result: zipper buzz at buffer rate       result: silent glide
```

## In Open DJ

`apps/audio-engine/src/dsp.rs` has two smoothers. `Smoothed` is a one-pole glide matching Web
Audio's `setTargetAtTime`, used for trim, fader, crossfader and filter moves with a 10 ms time
constant (`PARAM_SMOOTH_S` in `mixer.rs`). `LinearRamp` moves EQ gain in a straight line in dB
over 10 ms (`EQ_RAMP_S`), matching the page's `linearRampToValueAtTime` in
`apps/webui/frontend/src/lib/player/eq-apply.ts`. Both glide per frame, and tests in `dsp.rs`
check that a ramp lands on time and that retargeting mid-ramp starts from the current value.
