---
term: Camelot wheel
slug: camelot-wheel
aliases: [Camelot, Camelot key, harmonic wheel, Open Key, OpenKey]
group: Harmony and analysis
level: 1
one_liner: "A clock of 24 keys: numbers 1 to 12 around, A for minor and B for major. One step around or straight across mixes cleanly."
why_it_matters_here: "Open DJ reads every key as a wheel position, so the wheel decides which tracks the suggester offers and how a key match is scored."
related: [relative-and-parallel-keys, fifths-and-fourths, energy-boost-mix, key-clash, key-confidence]
sources:
  - "Mixed In Key, Camelot Wheel, https://mixedinkey.com/camelot-wheel/"
  - "Mixed In Key, Harmonic Mixing book, https://mixedinkey.com/Book/"
  - "Native Instruments, Traktor Pro manual, Open Key notation"
  - "Wikipedia, Circle of fifths (secondary pointer)"
status: draft
---

## In plain words

The Camelot wheel is a cheat sheet that turns music theory into numbers. Twelve numbers sit
around a clock face, and each number holds two keys: a minor key on the inner ring (A) and a
major key on the outer ring (B). "Adjacent" means one step around the clock on the same letter
(8A to 7A or 9A) or straight across on the same number (8A to 8B). Each of those moves changes at
most one note of the scale, so two tracks overlap without sour notes.

The precise term: the wheel is the circle of fifths relabeled, so each step clockwise is up a
perfect fifth. Traktor uses the same idea under the name Open Key, with m for minor and d for
major, rotated so that C major is 1d where Camelot calls it 8B. Converting is a fixed offset:
the Open Key number is the Camelot number minus 7, wrapping around 12 (8A is 1m, 1B is 6d).

## Picture

```text
                           12B
                  11B      12A      1B
                      11A       1A
           10B    10A                 2A    2B

           9B     9A     outer B = major    3A    3B
                         inner A = minor
           8B   * 8A *                4A    4B
                      7A        5A
                  7B       6A       5B
                           6B

   from 8A = A minor
     7A  9A    one step around       safe
     8B        straight across       safe
     10A       two steps             energy boost
```

## In Open DJ

`apps/shared/harmonic.py` parses Camelot ("8A") and standard ("Am") spellings into one
`CamelotKey`, measures wheel distance, and names the allowed moves in `CAMELOT_RULES`: same key,
one step either way, and relative are on by default; diagonal, energy boost and drop, Jaws and
"pay attention" are opt-in. `camelot_compatibility` turns distance into a 0 to 1 score that feeds
track suggestions. Analysis records also store an Open Key label (`key_openkey`, filled in
`apps/analysis/backends/own_key.py`) beside the Camelot one.
