---
term: Headroom and clipping
slug: headroom-and-clipping
aliases: [headroom, clipping, digital clipping, overs, red light, clip light]
group: Loudness and gain
level: 1
one_liner: Headroom is the space left below 0 dBFS; clipping is what happens when a signal runs out of it and the tops of the wave are cut flat.
why_it_matters_here: Two decks playing together need more headroom than one, so Open DJ levels tracks below the ceiling and lights a clip warning just under full scale.
related: [dbfs, true-peak, summed-loudness-bump, trim-and-loudness-matching, pre-vs-post-fader]
sources:
  - IEC 60268-10, Peak programme level meters
  - EBU R128 (2023), permitted maximum true-peak level
  - Pioneer DJ DJM-900NXS2 operating instructions, channel level and master level indicators
status: draft
---

## In plain words

Headroom is the distance between where your music peaks and the ceiling, 0 dBFS. A track
peaking at -6 dBFS has 6 dB of headroom. Clipping is running out of it: the wave tries to go
higher than the system can represent, and its tops are sliced off flat. Flat tops add harsh
distortion, the crunchy, fizzy sound of a mixer "in the red".

DJ mixing eats headroom in a way single-track playback does not. When two tracks overlap, their
levels add, so a pair of tracks each peaking at -2 dBFS can sum well past 0. That is why
mixers ask you to keep channel meters out of the red, and why loud masters (which often peak
above 0 dBTP already) are trimmed down before they meet each other.

## Picture

```text
  BEFORE: the wave wants +3 dBFS        AFTER: trimmed by -6 dB

 +3 |   .  .     .  .   <- lost    +3 |
  0 |==####=====####=====  ceiling   0 |=========================
    |  #    #   #    #                 |   headroom: 3 dB
 -3 | #      # #      #             -3 |   .*.         .*.
    |#        #        #               |  #   #       #   #
 -6 |                                -6 | #     #     #     #
    |                                  |#       #   #       #
    +----------------------------      +-------#-#-----------
     flat tops = clipping: harsh,       whole wave intact,
     fizzy distortion                   room for the second deck
```

## In Open DJ

Clip warnings come from `apps/webui/frontend/src/lib/rb/meter-math.ts`, which latches the clip
light at -0.1 dBFS (`CLIP_DBFS`) rather than at exactly 0, since a sample of exactly full scale
is legal. The offline scan in `apps/loudness/scan.py` clamps any per-track gain to keep true
peak under a ceiling and flags when that clamp decided the number. The mixer itself computes in
floating point, so a sum above 1.0 is not cut until it is written to a fixed-point output, such
as the recorder and WAV writer in `apps/audio-engine/src/`, which clamp samples to -1 to 1.
I did not find a master limiter in the engine.
