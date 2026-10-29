---
term: Linkwitz-Riley filter
slug: linkwitz-riley-filter
aliases: [Linkwitz-Riley crossover, LR4, LR crossover, L-R filter]
group: Filters and EQ
level: 3
one_liner: A crossover filter pair whose low and high outputs add back to a perfectly flat level, because each is 6 dB down at the crossover and in phase.
why_it_matters_here: It is the standard way to build an isolator EQ whose bands add back to the original track with no bump at the crossover.
related: [crossover-and-bands, butterworth-filter, isolator-eq, filter-order-and-slope, group-delay]
sources:
  - Siegfried Linkwitz, "Active Crossover Networks for Noncoincident Drivers", Journal of the Audio Engineering Society 24(1), 1976
  - Rane Corporation, "Linkwitz-Riley Crossovers: A Primer" (RaneNote 160), https://www.ranecommercial.com/
  - Linkwitz Lab, "Crossovers", https://www.linkwitzlab.com/
status: draft
---

## In plain words

When you split a track into bands and add them back, you want the original. A plain Butterworth
low-pass and high-pass at the same frequency are each 3 dB down at the crossover; since they come
out in phase there (for the usual 4th-order case), their amplitudes add, 0.707 + 0.707 = 1.41, and
you get a 3 dB bump right at the crossover.

Siegfried Linkwitz and Russ Riley fixed this in 1976 by squaring a Butterworth filter: put two
identical Butterworth filters in a row. Each band is now 6 dB down at the crossover (amplitude
0.5), the two outputs stay in phase with each other at every frequency, and 0.5 + 0.5 = 1. The sum
is flat. The common version, LR4, is two cascaded 2nd-order Butterworth sections per side and
falls at 24 dB per octave.

Flat in level does not mean untouched: the summed signal is an all-pass, so its phase still turns
around the crossover and the lows arrive slightly later than the highs.

## Picture

```text
 what you hear around the crossover fc when the bands are summed

 Butterworth pair, each -3 dB at fc:
  +3 dB |                  .***.          <- sum bumps up 3 dB
   0 dB |*****************       *****************
        |          low -3 dB  x  -3 dB high

 Linkwitz-Riley pair, each -6 dB at fc:
   0 dB |*****************************************  <- sum is flat
  -6 dB |          low -6 dB  x  -6 dB high
        +--------------------+-------------------> Hz
                             fc

 LR4 = Butterworth 2nd order > Butterworth 2nd order (same fc)
```

## In Open DJ

The colored waveform builds each band edge from two cascaded 2-pole Butterworth biquads at the
same frequency (`apps/audio-engine/src/waveform.rs`), which is the textbook LR4 recipe, though the
waveform only measures band energy and never sums the bands back. The live deck EQ has no
crossover yet; an isolator mode would be the place an LR crossover lands.
