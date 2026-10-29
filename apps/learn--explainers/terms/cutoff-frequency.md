---
term: Cutoff frequency
slug: cutoff-frequency
aliases: [cutoff, corner frequency, -3 dB point, fc, knee]
group: Filters and EQ
level: 1
one_liner: The frequency where a filter has turned the sound down by 3 dB, the line between what it keeps and what it removes.
why_it_matters_here: Turning Open DJ's filter knob moves the cutoff, sweeping it from 20 kHz down to 130 Hz on the low-pass side.
related: [butterworth-filter, filter-order-and-slope, q-and-resonance, taper-law, moog-ladder-filter]
sources:
  - Julius O. Smith III, "Introduction to Digital Filters with Audio Applications", CCRMA Stanford, https://ccrma.stanford.edu/~jos/filters/
  - W3C, "Web Audio API", BiquadFilterNode frequency attribute, https://www.w3.org/TR/webaudio/
  - Wikipedia, "Cutoff frequency" (secondary pointer)
status: draft
---

## In plain words

A low-pass filter keeps the lows and removes the highs, but there is no hard wall between them.
The cutoff frequency is the agreed marker: the point where the filter has turned the signal down
by 3 dB. That is half the power, or about 71 percent of the amplitude, a step quiet enough to
notice but far from silent.

Below the cutoff (for a low-pass) is the passband, kept almost unchanged. Above it the sound
fades away at the filter's slope, so content just past the cutoff is still clearly there, only
quieter. A high-pass is the mirror image. When a DJ turns a filter knob, what moves is the cutoff;
how fast the sound disappears beyond it is the order, and any bump right at it is the resonance.

One wrinkle: on a resonant filter the response near the cutoff is lifted, so the true -3 dB point
moves away from the number on the dial. Engineers then say "corner" or "center" frequency instead.

## Picture

```text
 low-pass filter
 dB
   0 |*********************.
     |                       *.
  -3 |- - - - - - - - - - - - -o   <- cutoff fc: half the power
     |                         | *
     |                         |   *
 -12 |                         |     *
     |                         |       *
 -24 |                         |         *
     +-------------------------+---------+---------> Hz
                               fc       2fc
       passband: kept          |   stopband: fading out
```

## In Open DJ

The filter knob maps position to cutoff exponentially in Hz, which is even steps in octaves:
the low-pass side sweeps 20 kHz down to 130 Hz, the high-pass side 30 Hz up to 6.3 kHz, with a
2 percent dead zone at the center (`FILTER_*` in `apps/audio-engine/src/mixer.rs`,
`filterParamsFromKnob` in `apps/webui/frontend/src/lib/rb/audio-engine-guards.ts`). Because Web
Audio reads Q in dB, the dial value is a corner with slight lift, not an exact -3 dB point.
