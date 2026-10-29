---
term: Group delay
slug: group-delay
aliases: [envelope delay, phase delay spread, frequency-dependent delay]
group: Filters and EQ
level: 2
one_liner: How late each frequency comes out of a filter; when that lateness differs from band to band, the parts of a kick arrive apart and it smears.
why_it_matters_here: Steep crossovers and EQs delay the bass more than the click, which softens kicks and can blur a mix of two tracks with different filters.
related: [bessel-filter, minimum-vs-linear-phase, zero-phase-filtering, linkwitz-riley-filter, filter-order-and-slope, q-and-resonance]
sources:
  - Jens Blauert and Peter Laws, "Group delay distortions in electroacoustical systems", Journal of the Acoustical Society of America 63(5), 1978
  - Julius O. Smith III, "Introduction to Digital Filters with Audio Applications" (phase and group delay), CCRMA Stanford, https://ccrma.stanford.edu/~jos/filters/
  - Wikipedia, "Group delay and phase delay" (secondary pointer)
status: draft
---

## In plain words

A filter changes not only how loud each frequency is but also when it comes out. Group delay is
that timing, measured per frequency, in milliseconds: how long the envelope of a sound at that
frequency is held back by the filter.

If the group delay is the same at every frequency, the whole sound is simply a bit late, which is
just latency and the shape survives. If it differs, the sound comes apart in time. A kick is a
low thump, a mid body and a high click that should land together; through a filter that delays
the lows more, the click arrives first and the thump trails behind, so the kick sounds softer and
smeared. Steep filters and high-Q filters have the largest delay near their cutoff.

The precise definition is the negative slope of the filter's phase response against frequency,
which is why "phase" and "delay" are discussed together. Our ears are least sensitive to it in
the bass, but differences of several milliseconds between bands can be heard on sharp drums.

## Picture

```text
 BEFORE: one kick hit, every part lands together

   sub   50 Hz  |*****
   body 120 Hz  |*****
   click  3 kHz |*****
                +----+----+----+----> time (ms)
                0    5    10   15

 AFTER: a filter that delays the lows more than the highs

   sub   50 Hz  |         *****       <- about 9 ms late
   body 120 Hz  |   *****             <- about 3 ms late
   click  3 kHz |*****                <- on time
                +----+----+----+----> time (ms)
                0    5    10   15

 (illustrative numbers)  the click and the thump now land apart
```

## In Open DJ

Not yet. No code measures or limits group delay today. It belongs beside the EQ coefficients in
`apps/audio-engine/src/dsp.rs` as a measured property of each band, and in the choice of crossover
for any isolator mode, where both decks must share the same delay.
