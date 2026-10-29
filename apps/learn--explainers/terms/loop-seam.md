---
term: Loop seam
slug: loop-seam
aliases: [loop point, loop wrap, loop click, loop declick]
group: Time, sync and loops
level: 2
one_liner: "The instant a loop jumps from its end back to its start; if the waveform does not match there, you hear a click."
why_it_matters_here: "Beat loops and rolls cross the seam every repeat, so a click there repeats on every beat the DJ holds the loop."
related: [loop-roll, slip-mode, equal-power-crossfade, gain-ramp, sample-accurate-scheduling]
sources:
  - "Zolzer (ed.), DAFX: Digital Audio Effects, 2nd ed., Wiley, 2011 (crossfades and discontinuities)"
  - "Roads, The Computer Music Tutorial, MIT Press, 1996 (looping and splice points in samplers)"
status: draft
---

## In plain words

A loop plays a stretch of audio, then jumps back to the start and plays it again. That jump is
the seam. Sound is a wave, and if the wave is high at the loop's end and low at its start, the
speaker is asked to move from one to the other in a single sample. That sudden step is a click,
and in a short loop it clicks on every repeat, which turns a clean roll into a ticking mess.

Two classic fixes. Put the loop points where the wave crosses zero, so both sides of the jump are
near silence (it helps, but a beat-accurate loop rarely lands on a zero crossing in both
channels). Or crossfade: for a few milliseconds around the seam, fade the loop's tail out while
fading its head in, so the jump is spread over hundreds of samples instead of one.

## Picture

```text
the seam, sample by sample (stereo left channel)

  loop end ...  0.55  0.68  0.80  |  -0.30 -0.21 -0.10  ... loop start
                                  ^ seam: a step of 1.10 in one sample
                                    = a broadband click, every repeat

fix 1, zero crossings: move both points to where the wave is near 0
          ...  0.04  0.01  0.00  |   0.00  0.02  0.05  ...  no step

fix 2, crossfade the seam over a few ms:
  tail of loop    ######======------.....         fading out
  head of loop    .....------======######         fading in
                  |<-------- 5 ms ------->|       no step to hear
```

## In Open DJ

Partly. The Rust engine wraps its read position inside an engaged loop by whole frames, so its
interpolation never mixes in audio from outside the loop (`hermite` in
`apps/audio-engine/src/deck.rs`); that avoids a smear, not a waveform step. A search of both engines on Tue 27 Oct 2026 found no
crossfade across the loop seam. The same technique already exists for a
different jump: the backward sync seek blends old and new audio with scheduled gain ramps in
`apps/webui/frontend/src/lib/rb/follower-backward-blend.ts`, which is where a loop declick would
borrow from.
