---
term: Filter order and slope
slug: filter-order-and-slope
aliases: [filter order, poles, dB per octave, roll-off, filter slope]
group: Filters and EQ
level: 2
one_liner: A filter's order counts its stages; each one adds 6 dB per octave of slope, so higher order means a steeper, sharper cut.
why_it_matters_here: Open DJ's filter knob is a 12 dB per octave biquad and its waveform split is 24 dB per octave, and the difference is audible.
related: [butterworth-filter, biquad, cutoff-frequency, linkwitz-riley-filter, group-delay, crossover-and-bands]
sources:
  - Julius O. Smith III, "Introduction to Digital Filters with Audio Applications", CCRMA Stanford, https://ccrma.stanford.edu/~jos/filters/
  - Richard G. Lyons, "Understanding Digital Signal Processing", 3rd edition, Prentice Hall, 2010
  - Wikipedia, "Roll-off" (secondary pointer)
status: draft
---

## In plain words

Past its cutoff, a filter does not stop sound dead. It turns it down more and more the further you
go, and the rate is the slope, measured in dB per octave (an octave is a doubling of frequency, say
1 kHz to 2 kHz). The order is how many basic stages, or poles, the filter has, and each adds
6 dB per octave.

So a 1st-order filter is a gentle tilt, a 2nd-order (one biquad) falls 12 dB per octave, and a
4th-order (two biquads in a row) falls 24 dB per octave, which is the classic steep sound of DJ
filters and isolator crossovers. 48 dB per octave is close to a wall.

Steeper is not free. Each extra order adds more phase shift and more delay near the cutoff, more
ringing on drum hits, and more computation. A 24 dB slope sounds tighter on a sweep; a 12 dB slope
sounds smoother and more open.

## Picture

```text
 how far down a Butterworth low-pass is above its cutoff fc

 order  biquads  slope            at 2fc    at 4fc
 -----  -------  ---------------  --------  --------
   1     (half)   6 dB per oct     -7 dB    -12 dB
   2       1     12 dB per oct    -12 dB    -24 dB
   4       2     24 dB per oct    -24 dB    -48 dB
   8       4     48 dB per oct    -48 dB    -96 dB

   0 dB *****************.
                         |  *  .    .    .    .   1st order: gentle
                         |   *     .      .
                         |    *        .          2nd order
                         |     *                  4th order: steep
                         +---------+---------+--> Hz
                         fc       2fc       4fc
```

## In Open DJ

The deck filter knob is one low-pass or one high-pass biquad, so 12 dB per octave
(`Coeffs::lowpass` and `Coeffs::highpass` in `apps/audio-engine/src/dsp.rs`, built in
`apps/audio-engine/src/deck.rs`). The colored waveform cascades two sections per band edge for
24 dB per octave (`filter_sections` in `apps/audio-engine/src/waveform.rs`).
`(private path)` discusses 24 dB per octave for a future filter kernel.
