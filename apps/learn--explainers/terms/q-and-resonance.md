---
term: Q and resonance
slug: q-and-resonance
aliases: [Q, Q factor, quality factor, resonance, reso, emphasis]
group: Filters and EQ
level: 2
one_liner: Q sets how narrow and peaked a filter is; push it high and the filter rings at its cutoff, the squelchy whistle of a resonant sweep.
why_it_matters_here: Open DJ's filter knob uses a fixed low Q with no resonance control yet, and its mid bell uses Q = 1.
related: [bell-eq, cutoff-frequency, moog-ladder-filter, butterworth-filter, biquad, group-delay]
sources:
  - Robert Bristow-Johnson, "Audio EQ Cookbook" (Q, bandwidth and slope), W3C note, https://www.w3.org/TR/audio-eq-cookbook/
  - W3C, "Web Audio API", BiquadFilterNode Q attribute, https://www.w3.org/TR/webaudio/
  - Wikipedia, "Q factor" (secondary pointer)
status: draft
---

## In plain words

Q, the quality factor, measures how sharply a filter is tuned. On a bell EQ it is the center
frequency divided by the width of the bell: Q = 1 is broad and musical, Q = 10 is a narrow
surgical notch or spike.

On a low-pass or high-pass, Q shapes the corner. At about 0.7 the corner is smooth with no bump
(the Butterworth shape). Raise it and a peak grows right at the cutoff: that band is boosted
above everything else. This is the resonance knob on a synth or a DJ filter, and sweeping a
resonant peak through a track gives the classic squelchy, vocal "wow".

High Q rings. A sharply tuned filter keeps sounding at its own frequency after the input moves on,
like a struck bell, because it stores energy and lets it out slowly. The ring dies away over
roughly Q divided by (pi times the frequency) seconds: at Q = 10 and 200 Hz, about 16 ms, long
enough to blur a kick. Push resonance far enough and some filters ring forever, which is
self-oscillation.

## Picture

```text
 low-pass response at three Q values, same cutoff fc
 dB
 +12 |                             CC
     |                           CC  C
  +6 |                        CCBBBBBBC
     |                 CBBBBBBBB      BBC
   0 |AAAAAAAAAAAAAAAAAAAAAAAAAAA       BC
     |                           AAAAAA  BB
  -6 |                                 AAA BB
     |                                    AAABB
 -12 |                                       AAAB
     |                                          AAB
 -18 |                                            AAA
     +------------------------------+--------------------> Hz
                                    fc
 A: Q = 0.707, smooth corner, -3 dB at fc
 B: Q = 2, about +6 dB peak      C: Q = 4, about +12 dB peak, rings
```

## In Open DJ

The mid bell uses Q = 1 (`EQ_MID_Q` in `apps/audio-engine/src/mixer.rs`). The filter knob passes
Q = 0.707 to Web Audio, which reads it in dB for low-pass and high-pass, giving about +0.7 dB at
the corner (`FILTER_Q`; explained in `apps/audio-engine/src/dsp.rs`). A resonance control is
deliberately not built: `apps/webui/frontend/src/lib/player/constants.ts` names it MIXUX-05,
waiting on an ear calibration.
