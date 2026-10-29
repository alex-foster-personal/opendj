---
term: Bell EQ
slug: bell-eq
aliases: [peaking EQ, peak filter, parametric band, mid EQ]
group: Filters and EQ
level: 2
one_liner: An EQ band that boosts or cuts a hill of frequencies around one center point and leaves both ends of the spectrum alone.
why_it_matters_here: The MID knob on every Open DJ deck is a bell at 1.2 kHz, so it shapes vocals and leads but can never silence them.
related: [shelf-eq, q-and-resonance, biquad, crossover-and-bands, full-kill-eq]
sources:
  - Robert Bristow-Johnson, "Audio EQ Cookbook" (peakingEQ), W3C note, https://www.w3.org/TR/audio-eq-cookbook/
  - W3C, "Web Audio API", BiquadFilterNode type "peaking", https://www.w3.org/TR/webaudio/
  - Bob Katz, "Mastering Audio: The Art and the Science", Focal Press, 2015 (chapter on equalization)
status: draft
---

## In plain words

A bell EQ raises or lowers a band of frequencies around a center point. Plot what it does and you
get a hill (boost) or a valley (cut) shaped like a bell, which is where the name comes from. Below
and above the bell the sound is untouched.

It has three settings. Frequency says where the center sits. Gain says how high or deep the bell
goes, in dB. Q says how wide it is: a low Q is a broad gentle hump, a high Q a narrow spike. On a
studio parametric EQ you get all three knobs; on a DJ mixer the mid knob is usually a bell with
the frequency and Q fixed, so only gain is yours.

The precise name is peaking EQ. Because the skirts of the bell always fade back to 0 dB, even a
very deep cut leaves the edges of the band in place: a bell can carve, but it cannot kill a band.

## Picture

```text
 gain
 (dB)
  +6 |                       ***                    boost
     |                    ***   ***
     |                 ***         ***
   0 +* * * * * * * ***               *** * * * * * * *  flat
     |                 ***         ***
     |                    ***   ***
 -26 |                       ***                    cut
     +--------+---------+----+----+---------+-------> frequency
             100       300  1.2k  5k        20k         (Hz)
                             |
                       center frequency
                  <---- width set by Q ---->
```

## In Open DJ

The deck MID band is a peaking biquad at 1.2 kHz with Q = 1, gain from -26 dB at the bottom of
the knob to +6 dB at the top (`EQ_FREQ_MID_HZ`, `EQ_MID_Q`, `EQ_MIN_DB`, `EQ_MAX_DB` in
`apps/audio-engine/src/mixer.rs`; coefficients in `apps/audio-engine/src/dsp.rs`, which has a test
that the bell reaches its gain exactly at the center). The browser engine uses Web Audio's
`peaking` node with the same numbers.
