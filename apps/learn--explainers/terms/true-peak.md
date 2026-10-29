---
term: True peak
slug: true-peak
aliases: [dBTP, inter-sample peak, ISP, true peak level]
group: Loudness and gain
level: 2
one_liner: True peak is the highest point of the rebuilt analog wave, which can sit above every stored sample; -1 dBTP leaves room for that overshoot.
why_it_matters_here: Open DJ's loudness scan clamps any per-track gain so the gained track cannot push its true peak past -1 dBTP.
related: [dbfs, headroom-and-clipping, lufs, trim-and-loudness-matching]
sources:
  - ITU-R BS.1770-5 (2023), Annex 2, Guidelines for accurate measurement of true-peak level
  - EBU R128 (2023), Loudness normalisation and permitted maximum level of audio signals
  - EBU Tech 3341, Loudness Metering, EBU Mode metering to supplement EBU R128
status: draft
---

## In plain words

A file stores samples, dots taken tens of thousands of times a second. The sound you hear is the
smooth curve a converter draws through those dots, and that curve can swing higher between two
dots than either dot is. The sample peak only sees the dots; the true peak estimates the top of
the curve. Those hidden overs are inter-sample peaks, and they clip in a converter, a sound card
or a lossy encoder even when no stored sample reached 0 dBFS.

True peak is written in dBTP (decibels true peak). BS.1770 measures it by oversampling at least
four times at 48 kHz, which fills in points between the samples, and taking the maximum. EBU R128
sets -1 dBTP as the permitted maximum: one dB of margin for the overshoot a decoder or a
resampler adds after the master is finished.

## Picture

```text
 level (dBFS)
  +1 |               .*.          <- true peak, the curve: +0.7 dBTP
   0 |=============*=====*========  0 dBFS ceiling
  -1 |- - - - - - #- - - -# - - - - -1 dBTP target
  -2 |           .         .
  -3 |          #           #     # = stored samples
     |         .             .        highest sample reads -0.8 dBFS
  -6 |        #               #
     +------------------------------> time
                sample peak says "fine"; true peak says "over"
```

## In Open DJ

`apps/loudness/scan.py` runs ffmpeg's `ebur128` filter with true-peak measurement and returns
`true_peak_dbtp` with the loudness. Its gain helper takes an explicit ceiling (the module
default is the -1 dBTP mastering ceiling) and reports `gain_clamped` when peak headroom, not the
loudness target, decided the number. The module notes record a lesson from the local library:
real commercial masters regularly measure above 0 dBTP (one read +2.3 dBTP), so code must not
assume true peak is negative.
