---
term: Equal-power crossfade
slug: equal-power-crossfade
aliases: [constant-power crossfade, constant-power pan law, -3 dB crossfade, smooth crossfader curve]
group: Loudness and gain
level: 2
one_liner: An equal-power crossfade uses cosine and sine gains so the mix holds steady loudness, each side at -3 dB in the middle, with no dip.
why_it_matters_here: Open DJ's crossfader uses this law, so a slow blend through the center keeps the room level instead of sagging.
related: [summed-loudness-bump, gain-ramp, trim-and-loudness-matching, bass-swap, wet-dry-mix]
sources:
  - Julius O. Smith III, Physical Audio Signal Processing, CCRMA Stanford (online book), panning and power laws
  - Pioneer DJ DJM-900NXS2 operating instructions, crossfader curve selector
  - "Wikipedia: Panning (audio), https://en.wikipedia.org/wiki/Panning_(audio) (secondary pointer)"
status: draft
---

## In plain words

A crossfader decides how much of deck A and deck B you hear at each position. The obvious design
is linear: at the center each deck gets half its amplitude. But half amplitude is -6 dB, and two
different tracks (uncorrelated material) add by power, not by amplitude, so the middle of a
linear fade comes out about 3 dB quieter than either end. The room hears a sag.

Equal power fixes that by using a quarter of a circle. At position x from 0 to 1:

gain_A = cos(x * pi/2)    gain_B = sin(x * pi/2)

Because cos squared plus sin squared is always 1, total power stays constant. At the center
both gains are 0.707 (one over the square root of two), which is -3 dB each, and the two powers
add back to 0 dB. The catch is correlated material: if both decks play the same kick in phase,
amplitudes add, and the center reads about +3 dB instead.

## Picture

```text
 gain
 1.00 |A *.                                   .* B
      |      *  .                       .  *
 0.71 |           *  .  .  .  .  .  . *          <- equal power, -3 dB
      |              *    .   .   *
 0.50 |                 # # # # #                <- linear, -6 dB
      |            #               #
      |        #                       #
 0.00 |#  linear                         #
      +-------------------+-------------------> x
      0  deck A          0.5 center        1  deck B

 summed loudness, two different tracks:
   equal power  ======================================  0 dB, flat
   linear       ======-----.........-----=============  dip, -3 dB
```

## In Open DJ

`apps/audio-engine/src/mixer.rs` defines `xf_gain`: a channel assigned to A gets
`cos(x * pi/2)`, one assigned to B gets `cos((1 - x) * pi/2)` (the same as the sine), and THRU
ignores the crossfader. A unit test there, `crossfader_is_equal_power`, pins the law. The
headphone cue and master blend in `apps/webui/frontend/src/lib/player/headphones.ts` uses the
same equal-power shape. I did not find a selectable sharp "cut" curve for scratch mixing.
