---
term: Half and double tempo
slug: half-double-tempo
aliases: [half-time, double-time, tempo octave, octave error, x2 and /2]
group: Time, sync and loops
level: 2
one_liner: "A 70 BPM track and a 140 BPM track share every other beat, so they can be synced; the trick is choosing which octave to call it."
why_it_matters_here: "Sync can lock a deck at half or double the master's tempo, and analysis must pick the octave a DJ expects to see."
related: [beat-grid, bar-phase, phase-correction, onset-detection]
sources:
  - "Gouyon et al., An Experimental Comparison of Audio Tempo Induction Algorithms, IEEE Trans. Audio, Speech and Language Processing, 2006 (octave errors)"
  - "Pioneer DJ CDJ-3000 Operating Instructions (Beat Sync, half and double tempo), AlphaTheta"
status: draft
---

## In plain words

Tempo has octaves, like pitch. A kick every beat at 70 BPM lines up exactly with every second
kick at 140 BPM, so the two can be mixed in time without changing either track much. The same
goes for a 174 BPM drum and bass track and an 87 BPM half-time groove: same pulse, different
counting. Beat trackers often land on the "wrong" octave, which is called an octave error.

To decide which octave to use, fold the tempo ratio by doubling or halving until it is as close
to 1 as it can get. The fair boundary is the square root of 2 (about 1.414): it sits exactly
halfway between 1 and 2 when you measure on a log scale, which is how both ears and pitch faders
work. A ratio above 1.414 is closer to 2 than to 1, so halve it; below 0.707, double it.

## Picture

```text
70 BPM    #-------#-------#-------#-------#
140 BPM   #---#---#---#---#---#---#---#---#
          every 70 BPM kick lands on a 140 BPM kick

fold the ratio (follower / master) into the middle band:

  0.5 ........ 0.707 ============ 1.0 ============ 1.414 ........ 2.0
   double it    |       keep it as it is            |    halve it
             1 / sqrt 2                           sqrt 2

  example: 174 against 96 BPM, ratio 1.81 > 1.414
           halve it -> 87 against 96, ratio 0.906: an easy 9 % push
```

## In Open DJ

Sync does not use the square-root-of-2 cut. `_tempoRatioWithinRangeOrNull` in
`apps/webui/frontend/src/lib/rb/beat-sync-math.ts` tries the raw ratio, then half, then double,
and takes the first one inside the pitch range. In BAR mode a half or double lock is the
fallback, and it shows in orange because the bar count no longer holds. On the analysis side,
`apps/analysis_beatgrid/bpm.py` picks an octave inside a 70 to 180 BPM band, breaking ties toward
its geometric center of about 112 BPM, and `tempo_family.py` uses the genre tag so drum and
bass reads as 174, not 87.
