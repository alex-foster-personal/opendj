---
term: Integrated, short-term and momentary loudness
slug: integrated-short-term-momentary
aliases: [I, S, M, integrated loudness, short-term loudness, momentary loudness, loudness range, LRA]
group: Loudness and gain
level: 2
one_liner: Loudness is read through three windows, the whole track (integrated), the last 3 s (short-term) and the last 400 ms (momentary).
why_it_matters_here: A per-track trim uses integrated loudness, while judging a transition seam needs the short-term or momentary view of a few bars.
related: [lufs, k-weighting, trim-and-loudness-matching, loudness-continuity, summed-loudness-bump]
sources:
  - ITU-R BS.1770-5 (2023), gating and integration of loudness
  - EBU Tech 3341, Loudness Metering, EBU Mode (defines M, S and I windows)
  - EBU Tech 3342, Loudness Range, a measure to supplement EBU R128 loudness normalisation
status: draft
---

## In plain words

The same LUFS measurement gives different answers depending on how long it listens.

- Momentary (M) averages the last 400 ms. It jumps with every kick and breakdown, like a fast
  meter.
- Short-term (S) averages the last 3 s, about one and a half bars at 128 BPM. It shows how loud
  a phrase feels.
- Integrated (I) covers the whole track. It is gated: blocks below -70 LUFS are ignored as
  silence, and then blocks more than 10 LU under the running average are ignored too, so a long
  quiet intro does not drag the figure down.

Loudness range (LRA, in LU) comes from the spread of short-term values, the 10th to 95th
percentile, so it says how much a track moves between quiet and loud passages. A flat club tool
might read 3 LU; a track with a long breakdown, 10 LU or more.

## Picture

```text
 LUFS
  -6 |              M    M  M             M  M
  -8 |          M  .S-S-S-S-S-.    M    .S-S-S-S.
 -10 |=I=I=I=I=I=I=I=I=I=I=I=I=I=I=I=I=I=I=I=I=I=I=  integrated -10
 -12 |       M  .S             S.     .S
 -14 |   M   .S                  S-S-S          M = 400 ms
 -18 |  .S        drop               break       S = 3 s
 -30 | S  intro                                 I = whole track,
 -70 |- - - - - - - - - - - - - - - - - - - -   gated
     +-------------------------------------------> time
```

## In Open DJ

`apps/loudness/scan.py` keeps only the integrated figure and the loudness range from ffmpeg's
`ebur128` summary. The filter prints running M and S values for every frame as well, which is
why the parser takes the last match per field: the first match is a mid-track sample, not the
integrated figure. Short-term and momentary curves are not stored or shown yet; they would
belong beside the waveform and in transition seam metrics.
