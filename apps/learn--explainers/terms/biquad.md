---
term: Biquad filter
slug: biquad
aliases: [biquad, second-order section, SOS, two-pole filter]
group: Filters and EQ
level: 2
one_liner: A tiny five-number filter recipe that nearly every digital EQ band, shelf and sweep filter is built from.
why_it_matters_here: Every EQ band and both sides of the filter knob on an Open DJ deck are one biquad each, run the same way in the browser and the native engine.
related: [bell-eq, shelf-eq, q-and-resonance, filter-order-and-slope, butterworth-filter, cutoff-frequency]
sources:
  - Robert Bristow-Johnson, "Audio EQ Cookbook", W3C Audio Working Group note, https://www.w3.org/TR/audio-eq-cookbook/
  - W3C, "Web Audio API", BiquadFilterNode section, https://www.w3.org/TR/webaudio/
  - Julius O. Smith III, "Introduction to Digital Filters with Audio Applications", CCRMA Stanford, https://ccrma.stanford.edu/~jos/filters/
status: draft
---

## In plain words

A biquad is a very small filter. For every new sample it looks at that sample, the two samples
before it and its own two previous outputs, multiplies each by a fixed number and adds them up.
Those five numbers (the coefficients) decide what it does: the same little machine becomes a
low-pass, a high-pass, a bell, a shelf or a notch just by swapping them.

That is why DSP builds almost everything from biquads. One block is cheap, well understood and
stays numerically stable, and a steeper filter is simply several biquads in a row, called a
cascade of second-order sections. "Biquad" is short for biquadratic: its transfer function is one
quadratic divided by another. The recipe itself is the point:

y[n] = b0 x[n] + b1 x[n-1] + b2 x[n-2] - a1 y[n-1] - a2 y[n-2]

## Picture

```text
  x now ------> * b0 ------->+
    |                        |
  delay                      |
    |                        |
  x 1 ago ----> * b1 ------->+
    |                        |
  delay                      |
    |                        |
  x 2 ago ----> * b2 ------->+=========> y now (output)
                             |             |
                             |           delay
                             |             |
                             +<-- * -a1 <--+ y 1 ago
                             |             |
                             |           delay
                             |             |
                             +<-- * -a2 <--+ y 2 ago

  "*" multiply by a coefficient   "+" add   "delay" one sample
  same block, five other numbers: low-pass, high-pass, bell, shelf
```

## In Open DJ

`apps/audio-engine/src/dsp.rs` computes biquad coefficients in exactly the forms the Web Audio
spec's `BiquadFilterNode` uses, so the native engine can be null-tested against the browser.
Each deck strip in `apps/audio-engine/src/deck.rs` runs a low shelf at 250 Hz, a bell at 1.2 kHz
and a high shelf at 5 kHz, plus one low-pass and one high-pass biquad for the filter knob
(constants in `apps/audio-engine/src/mixer.rs`). The waveform band split in
`apps/audio-engine/src/waveform.rs` cascades Butterworth biquads.
