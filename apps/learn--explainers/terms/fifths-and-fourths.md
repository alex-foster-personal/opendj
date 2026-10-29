---
term: Fifths and fourths
slug: fifths-and-fourths
aliases: [perfect fifth, perfect fourth, circle of fifths, plus one minus one, adjacent keys]
group: Harmony and analysis
level: 2
one_liner: "One step on the Camelot wheel is a perfect fifth up or a fourth down, and the two keys differ by a single note."
why_it_matters_here: "Open DJ's default key filter offers one step either way on the wheel, and scores it just under a same-key match."
related: [camelot-wheel, relative-and-parallel-keys, semitone-and-pitch-shift, energy-boost-mix, key-clash]
sources:
  - "Mixed In Key, Harmonic Mixing book, https://mixedinkey.com/Book/"
  - "Mueller, Fundamentals of Music Processing, Springer, 2nd ed. 2021"
  - "Wikipedia, Circle of fifths (secondary pointer)"
status: draft
---

## In plain words

A fifth is the interval from C up to G: seven semitones, the "power chord" sound. Its two notes
vibrate in a simple 3 to 2 ratio, so they blend almost like one note. A fourth is the same pair
upside down (G up to C, five semitones). Going up a fifth and going down a fourth land on the
same note name, which is why +1 on the wheel covers both.

Keys a fifth apart share six of their seven notes. C major and G major differ only in F versus
F#. When you mix 8B into 9B, every chord in one track still finds most of its notes in the other,
and the one note that moves only moves a semitone. That is why +1 and -1 sound right: the
change is audible as a lift (clockwise) or a settling (counterclockwise), never as a clash.

The wheel is built from this: walk clockwise twelve fifths and you visit every key once before
returning home. So wheel steps count fifths, not piano keys.

## Picture

```text
              C  C# D  D# E  F  F# G  G# A  A# B
F major 7B    *  .  *  .  *  *  .  *  .  *  *  .    -1  one note moves
C major 8B    *  .  *  .  *  *  .  *  .  *  .  *     0  home
G major 9B    *  .  *  .  *  .  *  *  .  *  .  *    +1  one note moves

   7B  <---  8B  --->  9B
   down a fifth        up a fifth
   = up a fourth       = down a fourth
   settles             lifts
```

## In Open DJ

The `adjacent` rule in `CAMELOT_RULES` (`apps/shared/harmonic.py`) allows one step either way on
the same letter and is on by default. `camelot_compatibility` scores it 0.9 against 1.0 for the
same key. The file's notes also explain the Jaws move: -5 on the wheel equals +7, and seven
fifths up is one semitone up.
