---
term: Moog ladder filter
slug: moog-ladder-filter
aliases: [ladder filter, transistor ladder, Moog filter, 24 dB ladder]
group: Filters and EQ
level: 2
one_liner: Robert Moog's resonant 24 dB per octave low-pass, four stages with feedback, the warm squelchy sound many DJ filter sweeps imitate.
why_it_matters_here: It is the reference for the character a resonant DJ filter sweep is expected to have, which Open DJ's plain filter knob does not yet aim for.
related: [q-and-resonance, cutoff-frequency, filter-order-and-slope, taper-law, butterworth-filter]
sources:
  - Robert A. Moog, US Patent 3,475,623, "Electronic high-pass and low-pass filters employing the base to emitter diode resistance of bipolar transistors", 1969
  - Tim Stilson and Julius O. Smith, "Analyzing the Moog VCF with Considerations for Digital Implementation", International Computer Music Conference, 1996
  - Antti Huovilainen, "Non-linear Digital Implementation of the Moog Ladder Filter", DAFx, 2004
status: draft
---

## In plain words

In the 1960s Robert Moog built a low-pass filter from a ladder of transistor pairs: four simple
6 dB per octave stages in a row, giving 24 dB per octave. The trick is a feedback path that takes
the output, flips it, and adds it back at the input. Turn that feedback up and the filter
resonates: a peak grows at the cutoff, the sound gets vocal and squelchy, and near the top of the
range it starts to sing a pure tone of its own, called self-oscillation.

Two quirks are part of its sound. As resonance rises the bass below the cutoff gets quieter,
because the feedback works against the passband. And the transistors saturate on loud input, a
soft distortion that makes sweeps sound warm rather than clinical. Digital models try to keep both;
a plain biquad has neither.

DJ mixers and software filters with a resonance knob often borrow this behavior, so "Moog-style"
has become shorthand for a resonant, slightly driven low-pass sweep.

## Picture

```text
     +---------------- feedback x k (resonance) ----------------+
     |                                                          |
     v                                                          |
 in -> sum (minus) -> stage 1 -> stage 2 -> stage 3 -> stage 4 -+-> out
                      -6 dB      -6 dB      -6 dB      -6 dB
                      per oct    per oct    per oct    per oct

 response as k rises
  dB
 +12 |                     *          high k: peak at cutoff, sings
     |                    * *
   0 |********************   *       low k: smooth corner
     |*********.....          *
     |              ...        *      24 dB per octave beyond cutoff
     +---------------------+-----------> Hz
                           fc
     (bass drops a little as k rises)
```

## In Open DJ

Not yet. The filter knob is a single 12 dB per octave biquad per side with fixed Q
(`apps/audio-engine/src/deck.rs`, `apps/audio-engine/src/mixer.rs`).
`apps/webui/frontend/src/lib/player/constants.ts` reserves resonance and character shaping for
MIXUX-05, after an ear calibration; a ladder model would belong there.
