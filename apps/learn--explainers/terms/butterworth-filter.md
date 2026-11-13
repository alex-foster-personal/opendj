---
term: Butterworth filter
slug: butterworth-filter
aliases: [Butterworth, maximally flat filter]
group: Filters and EQ
level: 2
one_liner: The filter shape with the flattest possible passband: no bumps, no ripple, then a smooth bend down through -3 dB at the cutoff.
why_it_matters_here: It is the default low-pass and high-pass shape in audio, used in Open DJ's waveform split and approximated by the deck filter knob.
related: [filter-order-and-slope, cutoff-frequency, linkwitz-riley-filter, bessel-filter, q-and-resonance, biquad]
sources:
  - Stephen Butterworth, "On the Theory of Filter Amplifiers", Experimental Wireless and the Wireless Engineer 7, 1930
  - Robert Bristow-Johnson, "Audio EQ Cookbook" (LPF and HPF with Q = 1 divided by root 2), W3C note, https://www.w3.org/TR/audio-eq-cookbook/
  - Wikipedia, "Butterworth filter" (secondary pointer)
status: draft
---

## In plain words

Stephen Butterworth's 1930 design asks for one thing: the part of the sound the filter keeps
should come through as flat as mathematically possible. No hump before the cutoff, no ripple, just
level, then a smooth bend down. Every Butterworth filter, whatever its order, is exactly 3 dB down
at its cutoff, and beyond it falls at 6 dB per octave for each order.

That makes it the sensible default whenever a filter should be heard as "less of the top" or
"less of the bottom" and nothing else. Its compromise sits in the middle of the alternatives: it
cuts more sharply near the cutoff than a Bessel filter but overshoots a little on drum hits,
more as the order rises, and it has none of the passband ripple steeper designs accept.

A 2nd-order Butterworth is one biquad with Q = 0.707 (1 divided by the square root of 2).

## Picture

```text
 dB
   0 |BBBBBBBBBBBBBBBBBAA
  -3 |                 BBBBBB
     |                      ABBB
     |                       AA BBB
 -12 |                         A   BBB
     |                          A     BB
     |                           AA     BBB
     |                             A       BB
 -24 |                              A        BB
     |                               A
     |                                AA
     |                                  A
 -36 |                                   A
     |                                    A
     |                                     AA
     |                                       A
 -48 |                                        A
     +--------------------+---------+---------+--> Hz
                         fc       2fc       4fc
 flat passband, no ripple; every order passes -3 dB at fc
 A: 4th order, 24 dB per octave   B: 2nd order, 12 dB per octave
```

## In Open DJ

The colored waveform uses Butterworth biquads with Q = 1 divided by root 2, two per band edge
(`apps/audio-engine/src/waveform.rs`). The deck filter knob passes Q = 0.707 to Web Audio
(`FILTER_Q_DB` in `apps/audio-engine/src/mixer.rs`), but Web Audio reads low-pass and high-pass Q in
dB, so the corner carries about +0.7 dB of lift instead of -3 dB: close to Butterworth, not
exactly it (`apps/audio-engine/src/dsp.rs` documents this).
