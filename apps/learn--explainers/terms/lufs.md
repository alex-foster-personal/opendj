---
term: LUFS and LU
slug: lufs
aliases: [LKFS, loudness units relative to full scale, loudness unit, LU, integrated loudness, loudness units]
group: Loudness and gain
level: 2
one_liner: LUFS is a loudness level on a scale built to match hearing; LU is a step on it, and one LU equals one dB.
why_it_matters_here: Open DJ scans every track to an integrated LUFS figure, and seam metrics for transitions are expressed in LU rather than raw dB.
related: [k-weighting, integrated-short-term-momentary, dbfs, replaygain, trim-and-loudness-matching, loudness-continuity]
sources:
  - ITU-R BS.1770-5 (2023), Algorithms to measure audio programme loudness and true-peak audio level
  - EBU R128 (2023), Loudness normalisation and permitted maximum level of audio signals
  - EBU Tech 3341, Loudness Metering, EBU Mode metering to supplement EBU R128
status: draft
---

## In plain words

Peak meters say how high a wave goes; they do not say how loud it feels. A quiet track with one
sharp snare can peak higher than a dense, compressed track that feels twice as loud. LUFS
(Loudness Units relative to Full Scale) measures what people hear: it weights the audio the way
ears do (K-weighting), averages the energy, and skips silence. ITU-R BS.1770 defines it, and the
US name LKFS is the same thing.

A level is written in LUFS, and a difference between two levels is written in LU. One LU is one
dB, so a track at -8 LUFS needs -6 dB of trim to sit with one at -14 LUFS. Common targets are
-23 LUFS for broadcast (EBU R128) and about -14 LUFS for streaming services; club masters
often measure -9 to -6 LUFS integrated.

loudness = -0.691 + 10 * log10(sum of K-weighted channel power)

## Picture

```text
  LUFS (integrated)
   -6 |  ####  loud club master
   -8 |  ####
  -10 |  ####                      gap = 6 LU = 6 dB of trim
  -12 |  ####
  -14 |  ####  ....  ####  streaming-style master
  -16 |  ####        ####
  -18 |  ####        ####
  -23 |  ####        ####  ....  ####  EBU R128 broadcast target
      +------------------------------------------------
        track A      track B         broadcast
```

## In Open DJ

`apps/loudness/scan.py` measures one file with ffmpeg's `ebur128` filter (run as a separate
process, never linked) and returns integrated loudness, loudness range and true peak. The parser
takes the last value per field, because the filter also prints running figures mid-track.
`apps/analysis_loudness/` wraps that scan as an analysis lane and backfills it across the
library. The track browser's row wiring (`browser-row-wire.ts` under
`apps/webui/frontend/src/lib/components/rb/browser/`) carries that loudness lane per track, with
the reason "loudness_lufs not analyzed" when no scan exists. The scan defaults to the EBU -23 LUFS target; callers pass `--target-lufs -14`
for a streaming-style level.
