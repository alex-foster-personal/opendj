---
term: Loudness continuity
slug: loudness-continuity
aliases: [seam loudness, loudness seam metric, LU gap]
group: Loudness and gain
level: 3
one_liner: Loudness continuity scores a transition by the largest LU gap between the mix's loudness and a straight line from the outgoing to the incoming track.
why_it_matters_here: It turns "the blend surged" or "the room dropped" into one number an automatic transition can be tuned against.
related: [summed-loudness-bump, integrated-short-term-momentary, lufs, equal-power-crossfade, p95-and-percentiles, beat-grid]
sources:
  - ITU-R BS.1770-5 (2023), loudness measurement
  - EBU Tech 3341, Loudness Metering (short-term 3 s window)
  - "Open DJ proposal for a rendered-transition scorer (project source, not yet on main, not a public standard)"
status: draft
---

## In plain words

A good transition hands the room from one track to the next without the level jumping. If the
outgoing track sits at -9 LUFS and the incoming one at -11 LUFS, the ideal seam drifts smoothly
from -9 to -11 across the overlap: a straight line. Loudness continuity measures how far the
real mix strays from that line.

The method: render the transition, measure loudness through it in short windows (short-term,
3 s, or per bar), draw the straight line between the two tracks' own levels, and report the
largest gap in LU, above or below. A bump from both faders being open shows up as a positive
gap; a hole from pulling one fader too early shows up as a negative gap. Smaller is better, and
0 LU is a perfect hand-over.

continuity = max over t of | L_mix(t) - line(t) |

## Picture

```text
 LUFS (short-term, through the overlap)
  -6 |                 .*.                  * = measured mix
  -7 |               .*   *.   <- worst gap: +3 LU above the line
  -8 |             *        *.
  -9 |*  *  *  *  -  -  -  -  -  -  -  -        - = straight line
 -10 |                          -  - *-  -  -        from A to B
 -11 |                                  *  *  *  *
     +----+----------------------------+--------> time
       A alone        overlap           B alone
     outgoing -9                       incoming -11

     score = 3 LU (bump). A good seam stays within about 1 LU.
```

## In Open DJ

Not yet on main. The metric is proposed for a rendered-transition scorer that has not landed;
once landed it belongs
with the render metrics beside the other scorers under `apps/analysis_bench/scorers/`. Note that
`apps/analysis_bench/scorers/continuity.py` is unrelated: it scores beat-grid continuity
(CMLt, AMLt), not loudness.
