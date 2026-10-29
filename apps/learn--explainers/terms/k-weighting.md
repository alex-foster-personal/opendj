---
term: K-weighting
slug: k-weighting
aliases: [K-filter, BS.1770 pre-filter, RLB weighting]
group: Loudness and gain
level: 3
one_liner: K-weighting is a two-stage filter that tilts audio toward what ears notice, lifting the highs and cutting deep bass, before loudness is measured.
why_it_matters_here: It is why a bass-heavy techno track and a bright vocal track can share a LUFS figure while their peak meters disagree.
related: [lufs, integrated-short-term-momentary, biquad, crossover-and-bands]
sources:
  - ITU-R BS.1770-5 (2023), Annex 1, specification of the weighting filter
  - EBU Tech 3341, Loudness Metering, EBU Mode metering to supplement EBU R128
  - "Wikipedia: LKFS, https://en.wikipedia.org/wiki/LKFS (secondary pointer)"
status: draft
---

## In plain words

Ears are not equally sensitive across the spectrum. A lot of sub-bass energy moves the needle
on a plain energy meter but does not feel proportionally louder, while the presence range
around 2 to 5 kHz feels loud with little energy. If loudness were plain energy, bass-heavy
tracks would read too loud and get turned down too far.

K-weighting fixes that with two filters run before the energy is summed. Stage one is a high
shelf that adds about +4 dB above roughly 2 kHz, modeling the acoustic effect of a head.
Stage two is a gentle high-pass (the revised low-frequency B curve, RLB) that is about -6 dB
near 38 Hz and falls away below. Both are ordinary second-order biquad filters with
coefficients given in the standard for 48 kHz. Then each channel's power is averaged, summed and
turned into LUFS.

It is a deliberately simple model, cheaper than full psychoacoustic loudness, and it was
validated against listening tests on broadcast material.

## Picture

```text
 gain (dB)
  +4 |                                   ..........  shelf (stage 1)
  +2 |                              .....
   0 |- - - - - - - ...............- - - - - - - - - -
  -2 |          ..
  -4 |        .
  -6 |      .  <- about -6 dB near 38 Hz (stage 2, RLB high-pass)
 -10 |    .
 -20 |  .
     +----+-------+--------+--------+--------+------->
         20      50      200       2k       20k   Hz (log)
     sub-bass counts for less       presence counts for more
```

## In Open DJ

Open DJ does not implement the filter itself. The offline scan in `apps/loudness/scan.py` asks
ffmpeg's `ebur128` filter, which applies BS.1770 K-weighting inside its measurement, so every
LUFS figure in the library is K-weighted. The realtime channel meters in
`apps/webui/frontend/src/lib/rb/meter-math.ts` are peak and RMS meters with no K-weighting,
which is one reason a meter reading and a LUFS figure can disagree.
