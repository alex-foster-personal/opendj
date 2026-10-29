---
term: Relative and parallel keys
slug: relative-and-parallel-keys
aliases: [relative major, relative minor, parallel major, parallel minor, A-B swap, mode swap]
group: Harmony and analysis
level: 2
one_liner: "Relative keys share all seven notes (8A and 8B). Parallel keys share a root but not a mood (A minor and A major)."
why_it_matters_here: "The relative swap is a default-on match in Open DJ's key filter, while a parallel swap is a bigger move it does not offer by default."
related: [camelot-wheel, fifths-and-fourths, key-clash, key-confidence, chroma]
sources:
  - "Mixed In Key, Harmonic Mixing book, https://mixedinkey.com/Book/"
  - "Mixed In Key, Camelot Wheel, https://mixedinkey.com/camelot-wheel/"
  - "Wikipedia, Relative key and Parallel key (secondary pointers)"
status: draft
---

## In plain words

Every major key has a minor twin built from exactly the same seven notes. C major and A minor
use only the white keys of a piano; the difference is which note feels like home. On the Camelot
wheel the twins sit on the same number: 8B and 8A. Swapping between them is called a relative
move, and it is the smoothest mood change in DJing: the notes stay, the color shifts from bright
to moody or back.

A parallel pair is different: same home note, different scale. A minor (8A) and A major (11B)
both resolve to A, but three notes move up a semitone (C, F and G become C#, F# and G#). On the
wheel that is three steps away plus a letter change, so it is not "adjacent". Some DJs use the
parallel swap on purpose for a sudden lift, usually over a short overlap or a vocal hook, because
holding two tracks in parallel keys for 32 bars lets those three notes rub.

## Picture

```text
              C  C# D  D# E  F  F# G  G# A  A# B
A minor 8A    *  .  *  .  *  *  .  *  .  *  .  *
C major 8B    *  .  *  .  *  *  .  *  .  *  .  *  relative  same 7
A major 11B   .  *  *  .  *  .  *  .  *  *  .  *  parallel  3 move

   8A  <====>  8B       same number    swap letter    safe
   8A  <---->  11B      same root      three steps    use with care
```

## In Open DJ

The `relative` rule in `CAMELOT_RULES` (`apps/shared/harmonic.py`) matches 8A to 8B and back and
is on by default. There is no parallel rule. When Open DJ grades its own key detector against
tags, `apps/analysis_bench/scorers/key_lane.py` treats a relative, parallel or fifth-apart answer
as a related miss rather than a flat wrong one, because detectors confuse these pairs most often.
