---
term: Shelf EQ
slug: shelf-eq
aliases: [shelving EQ, low shelf, high shelf, bass and treble knob]
group: Filters and EQ
level: 2
one_liner: An EQ that lifts or lowers everything below (low shelf) or above (high shelf) a corner frequency by the same amount.
why_it_matters_here: Open DJ's LOW and HIGH knobs are shelves, so turning them all the way down makes a band much quieter but never silent.
related: [bell-eq, full-kill-eq, isolator-eq, crossover-and-bands, biquad]
sources:
  - Robert Bristow-Johnson, "Audio EQ Cookbook" (lowShelf, highShelf, shelf slope S), W3C note, https://www.w3.org/TR/audio-eq-cookbook/
  - W3C, "Web Audio API", BiquadFilterNode types "lowshelf" and "highshelf", https://www.w3.org/TR/webaudio/
  - Wikipedia, "Equalization (audio)", shelving filters section (secondary pointer)
status: draft
---

## In plain words

A shelf moves a whole end of the spectrum up or down together. A low shelf works on everything
below its corner frequency, a high shelf on everything above it, and in between there is a ramp
from the changed level back to 0 dB. Plot it and it looks like a step or a shelf, hence the name.
The bass and treble knobs on a hi-fi are shelves, and so are the low and high knobs on most DJ
mixers.

Two things to remember. First, the far end is flat: a shelf set to -26 dB turns the bass down by
26 dB and stops there. Second, the ramp is gradual, so at the corner frequency itself you get only
about half the cut in dB, and some sound just above the corner is touched too. The steepness of
that ramp is the shelf slope (called S in the cookbook formulas).

## Picture

```text
 LOW SHELF, corner 250 Hz             HIGH SHELF, corner 5 kHz
 dB                                   dB
  +6 |*****                            +6 |                 *****
     |     **                             |               **
   0 |- - - -****** * * * *             0 |* * * * ******- - - -
     |     **                             |               **
 -26 |*****                           -26 |                 *****
     +----+-----+----+----> Hz            +----+----+-----+----> Hz
         50    250   1k   10k                 100  1k    5k   20k
     left of the corner moves,            right of the corner moves,
     right of it stays at 0 dB            left of it stays at 0 dB
```

## In Open DJ

Each deck runs a low shelf at 250 Hz and a high shelf at 5 kHz with slope S = 1, from -26 dB to
+6 dB (`apps/audio-engine/src/mixer.rs`, `apps/audio-engine/src/dsp.rs`, mirrored in
`apps/webui/frontend/src/lib/player/constants.ts`). The transitions research measured the low
shelf at its floor cutting only about 13 dB at 250 Hz, half its nominal depth, which is why a
bass swap on shelves still leaks some bass.
